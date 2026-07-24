"""Tests for the auth handshake, CSRF handling and origin-scoped hooks."""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
import respx
from dspace_mcp.client import DSpaceError

from conftest import API, BASE_URL, make_config
from dspace_mcp_write.auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    CSRF_REQUEST_HEADER,
    same_origin,
)
from dspace_mcp_write.client import WriteClient


@contextlib.asynccontextmanager
async def make_client(**overrides):
    """Yield a live ``WriteClient`` over a real (respx-intercepted) http."""
    config = make_config(**overrides)
    async with WriteClient.build_http(config) as http:
        yield WriteClient(config, http)


# --- header/cookie names are exactly the DSpace ones ------------------------


def test_csrf_header_and_cookie_names():
    assert CSRF_HEADER == "DSPACE-XSRF-TOKEN"
    assert CSRF_COOKIE == "DSPACE-XSRF-COOKIE"
    assert CSRF_REQUEST_HEADER == "X-XSRF-TOKEN"


# --- full handshake ---------------------------------------------------------


async def test_full_handshake_captures_jwt_and_csrf():
    async with make_client() as client:
        with respx.mock(assert_all_called=False) as router:
            csrf_route = router.get(f"{API}/security/csrf").mock(
                return_value=httpx.Response(
                    200,
                    headers={
                        CSRF_HEADER: "csrf-1",
                        # DSpace also drops the token in a cookie; we set a
                        # DIFFERENT value here to prove we take the header, not
                        # the jar, as the source of truth (spec D8).
                        "Set-Cookie": f"{CSRF_COOKIE}=cookie-value; Path=/",
                    },
                )
            )
            login_route = router.post(f"{API}/authn/login").mock(
                return_value=httpx.Response(
                    200,
                    headers={
                        "Authorization": "Bearer JWT-abc",
                        CSRF_HEADER: "csrf-2",
                    },
                )
            )
            await client.login()

        # JWT stored verbatim, with the "Bearer " prefix.
        assert client._jwt == "Bearer JWT-abc"
        # The rotated token from the login response wins.
        assert client._csrf == "csrf-2"
        assert csrf_route.called
        # The login POST echoed the CSRF token as X-XSRF-TOKEN…
        login_req = login_route.calls.last.request
        assert login_req.headers[CSRF_REQUEST_HEADER] == "csrf-1"
        # …and carried the credentials as a form body.
        assert b"user=tech%40example.org" in login_req.content
        assert b"password=secret" in login_req.content


async def test_security_csrf_404_falls_back_to_status():
    async with make_client() as client:
        with respx.mock(assert_all_called=False) as router:
            csrf_route = router.get(f"{API}/security/csrf").mock(
                return_value=httpx.Response(404)
            )
            status_route = router.get(f"{API}/authn/status").mock(
                return_value=httpx.Response(200, headers={CSRF_HEADER: "csrf-fb"})
            )
            login_route = router.post(f"{API}/authn/login").mock(
                return_value=httpx.Response(
                    200, headers={"Authorization": "Bearer JWT-fb"}
                )
            )
            await client.login()

        assert csrf_route.called
        assert status_route.called
        assert client._jwt == "Bearer JWT-fb"
        # Token came from the fallback status endpoint.
        assert login_route.calls.last.request.headers[CSRF_REQUEST_HEADER] == "csrf-fb"


async def test_login_rejects_bad_credentials():
    async with make_client() as client:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/security/csrf").mock(
                return_value=httpx.Response(200, headers={CSRF_HEADER: "c"})
            )
            router.post(f"{API}/authn/login").mock(return_value=httpx.Response(401))
            with pytest.raises(DSpaceError) as excinfo:
                await client.login()
        assert "Login failed" in str(excinfo.value)
        assert client._jwt is None


# --- origin-scoped request hook (inject) ------------------------------------


async def test_inject_only_attaches_on_same_origin():
    async with make_client() as client:
        client._jwt = "Bearer X"
        client._csrf = "csrf-X"

        same = httpx.Request("GET", f"{API}/core/items/abc")
        await client._inject_auth(same)
        assert same.headers["Authorization"] == "Bearer X"
        assert same.headers[CSRF_REQUEST_HEADER] == "csrf-X"

        # Different host (a redirect target, e.g. S3): NOTHING attached.
        s3 = httpx.Request("GET", "https://s3.amazonaws.com/bucket/key")
        await client._inject_auth(s3)
        assert "Authorization" not in s3.headers
        assert CSRF_REQUEST_HEADER not in s3.headers

        # Same host, downgraded scheme (https base -> http): NOT same origin.
        downgrade = httpx.Request("GET", "http://dspace.example.org/server/api/x")
        await client._inject_auth(downgrade)
        assert "Authorization" not in downgrade.headers

        # Same host, different port (a sidecar): NOT same origin.
        other_port = httpx.Request(
            "GET", "https://dspace.example.org:8443/server/api/x"
        )
        await client._inject_auth(other_port)
        assert "Authorization" not in other_port.headers


# --- origin-scoped response hook (capture) ----------------------------------


async def test_capture_ignores_cross_origin_responses():
    async with make_client() as client:
        client._csrf = "original"

        s3_req = httpx.Request("GET", "https://s3.amazonaws.com/x")
        s3_resp = httpx.Response(200, headers={CSRF_HEADER: "evil"}, request=s3_req)
        await client._capture_csrf(s3_resp)
        assert client._csrf == "original"  # untouched by the foreign response

        dspace_req = httpx.Request("GET", f"{API}/core/items/x")
        dspace_resp = httpx.Response(
            200, headers={CSRF_HEADER: "fresh"}, request=dspace_req
        )
        await client._capture_csrf(dspace_resp)
        assert client._csrf == "fresh"


# --- same_origin unit coverage ----------------------------------------------


def test_same_origin_rules():
    base = httpx.URL(BASE_URL)  # https, default port
    assert same_origin(httpx.URL(f"{API}/x"), base)
    assert not same_origin(httpx.URL("https://other.example.org/x"), base)
    assert not same_origin(httpx.URL("http://dspace.example.org/x"), base)
    assert not same_origin(httpx.URL("https://dspace.example.org:8443/x"), base)
    # http base -> the one allowed exception is an https upgrade, same host.
    http_base = httpx.URL("http://dspace.example.org/server")
    assert same_origin(httpx.URL("https://dspace.example.org/server/api"), http_base)
    assert same_origin(httpx.URL("http://dspace.example.org/server/api"), http_base)
    assert not same_origin(httpx.URL("https://dspace.example.org:8443/api"), http_base)


# --- concurrency: N concurrent login() calls -> exactly ONE login POST ------


async def test_concurrent_logins_collapse_to_single_post():
    async with make_client() as client:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/security/csrf").mock(
                return_value=httpx.Response(200, headers={CSRF_HEADER: "c"})
            )

            async def login_responder(request):
                # Yield control (as real network I/O would) so the other five
                # callers pile up on the lock before this one finishes — the
                # genuine concurrent-401 scenario the re-check must survive.
                await asyncio.sleep(0.02)
                return httpx.Response(200, headers={"Authorization": "Bearer JWT-1"})

            login_route = router.post(f"{API}/authn/login").mock(
                side_effect=login_responder
            )
            await asyncio.gather(*(client.login() for _ in range(6)))

        # The asyncio.Lock + re-check turns six concurrent callers into ONE
        # login POST; the other five observe the refreshed JWT and return.
        assert login_route.call_count == 1
        assert client._jwt == "Bearer JWT-1"
