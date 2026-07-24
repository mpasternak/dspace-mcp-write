"""Authenticated write client for DSpace 7+.

``WriteClient`` subclasses the read-only ``DSpaceClient`` and adds:

* an origin-scoped auth layer (JWT + CSRF) wired as httpx event hooks in
  ``__init__`` — NOT in ``build_http`` (a classmethod with no instance state);
* a concurrency-safe ``login()`` that collapses N simultaneous 401s into a
  single login POST;
* 401→re-login→retry-once on the whole request surface — read JSON
  (``_request_json``), bitstream downloads (``stream_bytes``) and mutations
  (``mutate`` / ``upload``) — plus a 403→refresh-CSRF→retry-once path for
  writes (spec D8/D9).

Only the auth-relevant behaviour is overridden; everything else (pagination,
the probe, capability detection) is inherited unchanged.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import httpx
from dspace_mcp.client import DSpaceClient, DSpaceError

from . import __version__
from .auth import (
    CSRF_REQUEST_HEADER,
    LOGIN_PATHS,
    csrf_token_from_response,
    login_handshake,
    same_origin,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

    from .config import WriteConfig

#: How much of a write error body to surface to the model (spec D9).
_BODY_SNIPPET_LIMIT = 500


def _is_login_path(where: str) -> bool:
    """Is ``where`` one of the login-handshake endpoints (retry bypass)?"""
    normalized = where.rstrip("/")
    return any(normalized.endswith(path) for path in LOGIN_PATHS)


class WriteClient(DSpaceClient):
    """DSpace client that authenticates and performs mutating requests."""

    def __init__(self, config: WriteConfig, http: httpx.AsyncClient) -> None:
        super().__init__(config, http)
        # Auth state lives on the instance (the hooks close over it).
        self._jwt: str | None = None
        self._csrf: str | None = None
        self._login_lock = asyncio.Lock()
        # The DSpace origin the JWT/CSRF may be attached to. Parsed once.
        self._base_origin = httpx.URL(config.base_url)
        # Hooks are registered HERE (not in build_http) because they need
        # instance state. httpx runs them on EVERY redirect hop, so both are
        # origin-scoped (see _inject_auth / _capture_csrf).
        self.http.event_hooks = {
            "request": [self._inject_auth],
            "response": [self._capture_csrf],
        }

    @classmethod
    def build_http(cls, config: WriteConfig) -> httpx.AsyncClient:
        """Build the HTTP client and stamp the write-server User-Agent.

        No event hooks here: ``build_http`` is a classmethod called before any
        instance exists, and the hooks need per-instance auth state.
        """
        http = super().build_http(config)
        http.headers["User-Agent"] = f"dspace-mcp-write/{__version__}"
        return http

    # --- event hooks (origin-scoped) ----------------------------------------

    async def _inject_auth(self, request: httpx.Request) -> None:
        """Attach the JWT and CSRF token — only for the DSpace origin.

        httpx fires request hooks on every redirect hop (bitstream ``/content``
        and ``pid/find`` 302 to S3/CDN). Attaching the bearer token off-origin
        would leak it into third-party logs and break presigned URLs, so we
        gate on the full origin, not the host.
        """
        if not same_origin(request.url, self._base_origin):
            return
        if self._jwt:
            request.headers["Authorization"] = self._jwt
        if self._csrf:
            request.headers[CSRF_REQUEST_HEADER] = self._csrf

    async def _capture_csrf(self, response: httpx.Response) -> None:
        """Capture the rotating CSRF token — only from the DSpace origin.

        Scoped on ``response.request.url`` so a redirect response from S3/CDN
        cannot poison our stored token.
        """
        if not same_origin(response.request.url, self._base_origin):
            return
        token = csrf_token_from_response(response)
        if token:
            self._csrf = token

    # --- login (concurrency-safe) -------------------------------------------

    async def login(self) -> None:
        """Perform the CSRF+JWT handshake, deduplicating concurrent callers.

        FastMCP dispatches tool calls concurrently, so when the JWT expires N
        in-flight requests all get 401 at once. Each reads the JWT it *saw*
        before contending for the lock; the first winner logs in and the rest
        observe the refreshed token and return without a second login POST.
        """
        # Snapshot BEFORE acquiring the lock (atomic — no await in between).
        stale_jwt = self._jwt
        async with self._login_lock:
            if self._jwt is not None and self._jwt != stale_jwt:
                # Another task already refreshed the token while we waited.
                return
            # Clear the expired JWT before POSTing to /authn/login: DSpace's
            # auth filter can reject a stale bearer token before it ever
            # checks the submitted credentials (spec D9). If the handshake
            # raises, the JWT stays cleared and the error propagates.
            self._jwt = None
            jwt, csrf = await login_handshake(
                self.http,
                self.api_url,
                self.config.username or "",
                self.config.password or "",
                csrf_token_from_response,
            )
            self._jwt = jwt
            if csrf:
                self._csrf = csrf

    # --- error mapping ------------------------------------------------------

    def _error_for_status(self, status: int, where: str) -> DSpaceError:
        """Map 401/403 to authenticated-account wording; else defer to parent.

        The read-only parent says "…anonymously"; here the server has a
        configured account, so the diagnosis is different.
        """
        if status == 401:
            error = DSpaceError(
                f"Authentication failed for {where}: the configured account "
                "could not be authenticated. Check DSPACE_USERNAME and "
                "DSPACE_PASSWORD."
            )
            error.status = status
            return error
        if status == 403:
            error = DSpaceError(
                f"Forbidden for {where}: the configured account does not have "
                "access to that object, or is not allowed to write here. "
                "Check its permissions on the target collection."
            )
            error.status = status
            return error
        return super()._error_for_status(status, where)

    # --- read path overrides (401 -> relogin -> retry once) -----------------

    async def _request_json(
        self, url: str, params: dict | None = None, *, where: str
    ) -> dict:
        """Wrap the parent GET+parse with a single 401→relogin→retry."""
        try:
            return await super()._request_json(url, params, where=where)
        except DSpaceError as exc:
            if not self._should_relogin(exc.status, where):
                raise
            await self.login()
            # A second 401 here propagates (not wrapped) — no infinite loop.
            return await super()._request_json(url, params, where=where)

    async def stream_bytes(self, url: str, *, max_bytes: int) -> bytes:
        """Wrap the parent bitstream download with a 401→relogin→retry.

        ``get_bitstream_text`` streams through here, bypassing
        ``_request_json``; without this override it would silently fail once
        the JWT expired (spec D9).
        """
        try:
            return await super().stream_bytes(url, max_bytes=max_bytes)
        except DSpaceError as exc:
            # ``url`` is the absolute content URL, never a login path.
            if exc.status != 401:
                raise
            await self.login()
            return await super().stream_bytes(url, max_bytes=max_bytes)

    def _should_relogin(self, status: int | None, where: str) -> bool:
        """Retry only genuine 401s that are not themselves login traffic.

        Login-path requests are already kept out of this override because
        ``login_handshake`` issues them with raw ``self.http`` calls (they never
        reach ``_request_json``); ``_is_login_path`` is a defensive backstop.
        We deliberately do NOT gate on a shared "login in progress" flag — that
        would make concurrent 401s re-raise instead of awaiting the single
        in-flight login, defeating the dedup (spec D9).
        """
        return status == 401 and not _is_login_path(where)

    # --- write path ---------------------------------------------------------

    async def mutate(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        data: Any = None,
        content: Any = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
        where: str,
    ) -> httpx.Response | dict:
        """Perform a mutating request against a path relative to ``/api``.

        401 → re-login → retry once. 403 → the response hook already captured
        the fresh CSRF token, so retry once (a second 403 is a real
        permissions error). Other ``>=400`` statuses raise the mapped error
        with a trimmed body snippet appended (DSpace returns useful field-level
        validation detail on writes — spec D9). Returns the parsed JSON dict,
        or the raw ``Response`` for 204/empty bodies.
        """
        url = self._url(path)

        async def send() -> httpx.Response:
            return await self._request_raw(
                method,
                url,
                json=json,
                data=data,
                content=content,
                files=files,
                headers=headers,
            )

        response = await self._send_with_retry(send, where)
        return self._parse_mutation_response(response)

    async def upload(
        self,
        path: str,
        *,
        file_bytes: bytes,
        filename: str,
        properties: dict | None = None,
        where: str,
        timeout: float | None = None,
    ) -> dict:
        """Upload a file as multipart, with the same 401/403 retry logic.

        Sends a ``file`` part and, when ``properties`` is given, a ``properties``
        part carrying JSON (``application/json``). Uses a longer timeout than
        the JSON API default because uploads move real payloads.
        """
        url = self._url(path)
        files: dict[str, Any] = {"file": (filename, file_bytes)}
        if properties is not None:
            # filename=None -> a named form field with an explicit content type.
            files["properties"] = (
                None,
                json.dumps(properties),
                "application/json",
            )
        send_timeout = timeout if timeout is not None else self._upload_timeout()

        async def send() -> httpx.Response:
            return await self._request_raw(
                "POST", url, files=files, timeout=send_timeout
            )

        response = await self._send_with_retry(send, where)
        parsed = self._parse_mutation_response(response)
        if isinstance(parsed, dict):
            return parsed
        # A successful upload is expected to echo the created object as JSON;
        # a 2xx with no JSON body is anomalous — surface it rather than lie
        # about the return type.
        raise DSpaceError(f"The upload to {where} succeeded but returned no JSON body.")

    async def _send_with_retry(
        self,
        send: Callable[[], Awaitable[httpx.Response]],
        where: str,
    ) -> httpx.Response:
        """Send, applying 401→relogin and 403→refresh-CSRF, each once."""
        response = await send()
        if response.status_code == 401:
            # Expired JWT mid-session: re-login once, then replay (the request
            # hook attaches the fresh bearer token). Concurrent 401s dedup to a
            # single login inside login() itself.
            await self.login()
            response = await send()
        if response.status_code == 403:
            # A 403 on a write usually means a stale CSRF token; the response
            # hook already stored the fresh DSPACE-XSRF-TOKEN, so a single
            # replay carries the new X-XSRF-TOKEN. A second 403 is real and
            # falls through to the error mapping below.
            response = await send()
        if response.status_code >= 400:
            raise self._mutation_error(response, where)
        return response

    async def _request_raw(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        """One raw mutating request, mapping transport errors to DSpaceError."""
        try:
            return await self.http.request(method, url, **kwargs)
        except httpx.ConnectError as exc:
            raise DSpaceError(
                f"Repository unreachable at {self.config.base_url}."
            ) from exc
        except httpx.TimeoutException as exc:
            raise DSpaceError("The repository did not respond in time.") from exc
        except httpx.HTTPError as exc:  # redirect loop, TLS, protocol error…
            raise DSpaceError(
                f"Repository unreachable at {self.config.base_url}."
            ) from exc

    def _upload_timeout(self) -> float:
        """A generously longer timeout for uploads than for JSON calls."""
        return max(self.config.timeout * 4.0, 60.0)

    # --- write response helpers ---------------------------------------------

    def _mutation_error(self, response: httpx.Response, where: str) -> DSpaceError:
        """Map the status to a message, appending any useful body detail."""
        error = self._error_for_status(response.status_code, where)
        snippet = self._body_snippet(response)
        if snippet:
            detailed = DSpaceError(f"{error.message} Server said: {snippet}")
            detailed.status = response.status_code
            return detailed
        return error

    @staticmethod
    def _body_snippet(
        response: httpx.Response, limit: int = _BODY_SNIPPET_LIMIT
    ) -> str:
        """Extract a short, human-useful snippet of a write error body."""
        try:
            raw = (response.text or "").strip()
        except (UnicodeDecodeError, httpx.ResponseNotRead) as exc:
            # A body we cannot decode is not worth surfacing; the status-mapped
            # message already stands on its own. Return nothing rather than
            # crash while building an error (justified narrow suppression).
            del exc
            return ""
        if not raw:
            return ""
        # Prefer a structured message when DSpace returns JSON with one.
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            for key in ("message", "error", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    raw = value.strip()
                    break
        collapsed = " ".join(raw.split())
        if len(collapsed) > limit:
            collapsed = collapsed[:limit].rstrip() + "..."
        return collapsed

    def _parse_mutation_response(
        self, response: httpx.Response
    ) -> httpx.Response | dict:
        """Return parsed JSON for a body, or the Response for 204/no-body."""
        if response.status_code in (204, 205) or not response.content:
            return response
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            # A non-JSON 2xx write body is unusual; hand back the raw Response
            # rather than guessing at its shape.
            return response
        if isinstance(payload, dict):
            return payload
        return response
