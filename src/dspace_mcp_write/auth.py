"""Authentication engine for the DSpace write client (CSRF + JWT handshake).

This module holds the *pure* pieces of the auth layer:

* :func:`same_origin` — the origin check that decides whether the JWT and the
  CSRF token may be attached to a request (spec D8). Getting this wrong leaks
  the bearer token to redirect targets (S3/CDN) or over plaintext, so the check
  compares the **full** origin (scheme + host + port), not just the host.
* :func:`csrf_token_from_response` — reads the rotating CSRF token from the
  response header ``DSPACE-XSRF-TOKEN`` (the source of truth — not the cookie).
* :func:`login_handshake` — the CSRF ``GET`` + login ``POST`` sequence that
  yields the ``Authorization: Bearer …`` JWT (spec D8).

The event hooks themselves are bound methods on ``WriteClient`` because they
need instance state (the current JWT and CSRF token); they live in
``client.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from dspace_mcp.client import DSpaceError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

#: Response header carrying the (rotating) CSRF token — the source of truth.
CSRF_HEADER = "DSPACE-XSRF-TOKEN"
#: Cookie name DSpace also sets the token in (HttpOnly). We do NOT read this;
#: it is documented here so nobody wires the client to the cookie jar by
#: mistake (spec D8 — the header is authoritative, the cookie is fragile).
CSRF_COOKIE = "DSPACE-XSRF-COOKIE"
#: Request header we echo the token back on for mutating/login requests.
CSRF_REQUEST_HEADER = "X-XSRF-TOKEN"

#: Login-path suffixes whose requests MUST bypass the 401-retry override, so a
#: failed login cannot recurse into ``login()`` forever (spec D9).
LOGIN_PATHS = ("/security/csrf", "/authn/status", "/authn/login")

#: Default port per scheme, used to normalise ``httpx.URL.port`` (which is
#: ``None`` for the scheme's default port).
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _port_or_default(url: httpx.URL) -> int | None:
    """Return the URL's port, substituting the scheme default when implicit."""
    if url.port is not None:
        return url.port
    return _DEFAULT_PORTS.get(url.scheme)


def same_origin(url: httpx.URL, base: httpx.URL) -> bool:
    """Is ``url`` the same security origin as ``base`` (scheme + host + port)?

    Mirrors httpx's own redirect credential rules (spec D8):

    * identical scheme, host and port → same origin;
    * the one allowed exception is a same-host ``http`` → ``https`` *upgrade*
      on default ports (base is plain ``http``, request upgraded to ``https``).

    Every other case — a different host, an ``https`` → ``http`` *downgrade*
    on the same host, or a different port (a sidecar) — is a different origin.
    Host-only comparison would wrongly attach the JWT in exactly those leaky
    cases, which is the whole point of this function.
    """
    if url.host != base.host:
        return False
    url_port = _port_or_default(url)
    base_port = _port_or_default(base)
    if url.scheme == base.scheme:
        return url_port == base_port
    # Allow ONLY an http->https upgrade on the same host / default ports.
    if base.scheme == "http" and url.scheme == "https":
        return base_port == 80 and url_port == 443
    return False


def csrf_token_from_response(resp: httpx.Response) -> str | None:
    """Read the CSRF token from the ``DSPACE-XSRF-TOKEN`` response header."""
    token = resp.headers.get(CSRF_HEADER)
    return token or None


async def login_handshake(
    http: httpx.AsyncClient,
    base_api_url: str,
    username: str,
    password: str,
    get_csrf_token: Callable[[httpx.Response], str | None],
) -> tuple[str, str | None]:
    """Run the CSRF + login handshake and return ``(jwt, latest_csrf)``.

    1. ``GET {api}/security/csrf`` (DSpace 7.5+). On ``404`` (7.0–7.4) fall back
       to ``GET {api}/authn/status``, which also emits the token header.
    2. ``POST {api}/authn/login`` with form body ``user=…&password=…`` and the
       ``X-XSRF-TOKEN`` header. On success the JWT arrives in the response
       header ``Authorization: Bearer …`` — returned **verbatim**, prefix and
       all, so the caller stores exactly what the server expects back.

    The caller MUST have cleared any stale JWT before calling this, so these
    requests do not carry an expired bearer token (spec D9).
    """
    api = base_api_url.rstrip("/")
    try:
        csrf_resp = await http.get(f"{api}/security/csrf")
        if csrf_resp.status_code == 404:
            # /security/csrf exists only on 7.5+; on 7.0–7.4 this cheap GET
            # returns the same DSPACE-XSRF-TOKEN header.
            csrf_resp = await http.get(f"{api}/authn/status")
        csrf = get_csrf_token(csrf_resp)
        headers: dict[str, str] = {}
        if csrf:
            headers[CSRF_REQUEST_HEADER] = csrf
        login_resp = await http.post(
            f"{api}/authn/login",
            data={"user": username, "password": password},
            headers=headers,
        )
    except httpx.HTTPError as exc:
        # Convert a transport-level failure into a clean, model-facing error
        # rather than leaking an httpx traceback up through the retry path.
        raise DSpaceError(
            "Could not reach the DSpace authentication endpoint."
        ) from exc

    if login_resp.status_code >= 400:
        error = DSpaceError(
            "Login failed: the configured account was rejected by the "
            f"repository (HTTP {login_resp.status_code}). Check "
            "DSPACE_USERNAME and DSPACE_PASSWORD."
        )
        error.status = login_resp.status_code
        raise error

    jwt = login_resp.headers.get("Authorization")
    if not jwt:
        raise DSpaceError(
            "Login succeeded but returned no authentication token; the "
            "account may be disabled or the server may not accept password "
            "login."
        )

    # The login response often carries a freshly rotated CSRF token; prefer it.
    rotated = get_csrf_token(login_resp)
    return jwt, (rotated or csrf)
