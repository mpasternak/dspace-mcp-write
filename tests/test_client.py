"""Tests for WriteClient: mutate/upload, 401 relogin, 403 CSRF retry."""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
import respx
from dspace_mcp.client import DSpaceError

from conftest import API, make_config
from dspace_mcp_write.auth import CSRF_HEADER, CSRF_REQUEST_HEADER
from dspace_mcp_write.client import WriteClient


@contextlib.asynccontextmanager
async def make_client(**overrides):
    """Yield a live ``WriteClient`` over a real (respx-intercepted) http."""
    config = make_config(**overrides)
    async with WriteClient.build_http(config) as http:
        yield WriteClient(config, http)


def _mock_login(router, jwt: str = "Bearer JWT", *, delay: float = 0.0):
    """Register the handshake routes and return the login route.

    ``delay`` makes the login POST yield (as real I/O would), letting
    concurrent callers overlap so the dedup path is genuinely exercised.
    """
    router.get(f"{API}/security/csrf").mock(
        return_value=httpx.Response(200, headers={CSRF_HEADER: "c"})
    )
    if delay:

        async def login_responder(request):
            await asyncio.sleep(delay)
            return httpx.Response(200, headers={"Authorization": jwt})

        return router.post(f"{API}/authn/login").mock(side_effect=login_responder)
    return router.post(f"{API}/authn/login").mock(
        return_value=httpx.Response(200, headers={"Authorization": jwt})
    )


# --- build_http -------------------------------------------------------------


async def test_build_http_sets_write_user_agent():
    async with WriteClient.build_http(make_config()) as http:
        assert http.headers["User-Agent"].startswith("dspace-mcp-write/")


# --- error mapping ----------------------------------------------------------


async def test_error_for_status_authenticated_texts():
    async with make_client() as client:
        assert "Authentication failed" in client._error_for_status(401, "x").message
        forbidden = client._error_for_status(403, "x")
        assert "not allowed to write here" in forbidden.message
        # Non-auth statuses defer to the read-only parent (status preserved).
        assert client._error_for_status(404, "x").status == 404


# --- read path: 401 -> relogin -> retry (via _request_json) -----------------


async def test_read_path_401_relogin_retry_success():
    async with make_client() as client:
        with respx.mock(assert_all_called=False) as router:
            login_route = _mock_login(router)

            def responder(request):
                if request.headers.get("Authorization"):
                    return httpx.Response(200, json={"ok": True})
                return httpx.Response(401, json={"message": "expired"})

            data_route = router.get(f"{API}/core/items/x").mock(side_effect=responder)
            result = await client.get("/core/items/x")

        assert result == {"ok": True}
        assert login_route.call_count == 1
        assert data_route.call_count == 2  # 401, then 200 after relogin


async def test_read_path_second_401_propagates():
    async with make_client() as client:
        with respx.mock(assert_all_called=False) as router:
            login_route = _mock_login(router)
            data_route = router.get(f"{API}/core/items/x").mock(
                return_value=httpx.Response(401)
            )
            with pytest.raises(DSpaceError) as excinfo:
                await client.get("/core/items/x")

        assert excinfo.value.status == 401
        assert login_route.call_count == 1  # relogin attempted exactly once
        assert data_route.call_count == 2  # original + one retry, no more


# --- content path: 401 -> relogin -> retry (via stream_bytes) ---------------


async def test_stream_bytes_401_relogin_retry_success():
    content_url = f"{API}/core/bitstreams/x/content"
    async with make_client() as client:
        with respx.mock(assert_all_called=False) as router:
            login_route = _mock_login(router)

            def responder(request):
                if request.headers.get("Authorization"):
                    return httpx.Response(200, content=b"PDF-BYTES")
                return httpx.Response(401)

            content_route = router.get(content_url).mock(side_effect=responder)
            result = await client.stream_bytes(content_url, max_bytes=10_000)

        assert result == b"PDF-BYTES"
        assert login_route.call_count == 1
        assert content_route.call_count == 2


# --- write path: 403 -> refresh CSRF -> retry -------------------------------


async def test_mutate_403_refreshes_csrf_then_succeeds():
    async with make_client() as client:
        # Authenticated, but the CSRF token is missing/stale.
        client._jwt = "Bearer existing"
        client._csrf = None
        with respx.mock(assert_all_called=False) as router:

            def responder(request):
                # The retry carries the freshly captured token -> success.
                if request.headers.get(CSRF_REQUEST_HEADER) == "fresh":
                    return httpx.Response(200, json={"id": "ws1"})
                # DSpace returns 403 WITH a new token in the header.
                return httpx.Response(403, headers={CSRF_HEADER: "fresh"})

            route = router.patch(f"{API}/submission/workspaceitems/1").mock(
                side_effect=responder
            )
            result = await client.mutate(
                "PATCH",
                "/submission/workspaceitems/1",
                json=[{"op": "add", "path": "/sections/x/dc.title", "value": []}],
                where="update workspace item metadata",
            )

        assert result == {"id": "ws1"}
        assert route.call_count == 2
        assert client._csrf == "fresh"  # captured by the response hook


async def test_mutate_second_403_is_permission_error():
    async with make_client() as client:
        client._jwt = "Bearer existing"
        client._csrf = "stale"
        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{API}/core/items/x/bundles").mock(
                return_value=httpx.Response(
                    403,
                    headers={CSRF_HEADER: "y"},
                    json={"message": "no write permission on collection"},
                )
            )
            with pytest.raises(DSpaceError) as excinfo:
                await client.mutate(
                    "POST", "/core/items/x/bundles", json={}, where="add bundle"
                )

        assert excinfo.value.status == 403
        message = str(excinfo.value)
        assert "not allowed to write here" in message
        # Body detail surfaced (spec D9).
        assert "no write permission on collection" in message
        assert route.call_count == 2  # one CSRF retry, then it gives up


# --- write path: error body detail appended ---------------------------------


async def test_mutate_400_appends_body_detail():
    async with make_client() as client:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{API}/submission/workspaceitems").mock(
                return_value=httpx.Response(
                    400, json={"message": "dc.title is required"}
                )
            )
            with pytest.raises(DSpaceError) as excinfo:
                await client.mutate(
                    "POST", "/submission/workspaceitems", json={}, where="create item"
                )

        assert "dc.title is required" in str(excinfo.value)
        assert route.call_count == 1  # 400 is not retried


async def test_mutate_204_returns_response():
    async with make_client() as client:
        client._jwt = "Bearer existing"
        with respx.mock(assert_all_called=False) as router:
            router.delete(f"{API}/submission/workspaceitems/1").mock(
                return_value=httpx.Response(204)
            )
            result = await client.mutate(
                "DELETE", "/submission/workspaceitems/1", where="discard draft"
            )

        assert isinstance(result, httpx.Response)
        assert result.status_code == 204


# --- upload: multipart parts ------------------------------------------------


async def test_upload_sends_file_and_properties_parts():
    async with make_client() as client:
        client._jwt = "Bearer existing"
        client._csrf = "c"
        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"{API}/submission/workspaceitems/1").mock(
                return_value=httpx.Response(201, json={"id": "1"})
            )
            result = await client.upload(
                "/submission/workspaceitems/1",
                file_bytes=b"DATA-BYTES",
                filename="paper.pdf",
                properties={"dc.title": ["x"]},
                where="upload file",
            )

        assert result == {"id": "1"}
        body = route.calls.last.request.content
        assert b"paper.pdf" in body  # the file part filename
        assert b"DATA-BYTES" in body  # the file bytes
        assert b"application/json" in body  # the properties part content-type
        assert b"dc.title" in body  # the properties JSON


# --- concurrency: N concurrent 401 reads -> exactly ONE login ---------------


async def test_concurrent_401_reads_trigger_single_login():
    async with make_client() as client:
        with respx.mock(assert_all_called=False) as router:
            # A slow login lets all five reads 401 before any relogin lands.
            login_route = _mock_login(router, delay=0.02)

            def responder(request):
                if request.headers.get("Authorization"):
                    return httpx.Response(200, json={"ok": True})
                return httpx.Response(401)

            data_route = router.get(f"{API}/core/items/x").mock(side_effect=responder)
            results = await asyncio.gather(
                *(client.get("/core/items/x") for _ in range(5))
            )

        assert all(r == {"ok": True} for r in results)
        # Five concurrent 401s collapse to exactly ONE login...
        assert login_route.call_count == 1
        # ...and each of the five requests did 401 then retry (5 * 2 GETs).
        assert data_route.call_count == 10
