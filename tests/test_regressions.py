"""Regression tests for defects found in adversarial code review.

Each test pins a specific bug that the original suite did not cover:

* string/non-list metadata exploded per character (data corruption);
* the CSRF token riding httpx's redirect header carry-over to a foreign host;
* ``get_submission_form``'s throwaway-draft fallback writing into a
  non-whitelisted collection;
* deposit wrongly refusing when the license is already granted;
* write 422s inheriting the read-only "unknown search filter" wording;
* non-ASCII digits accepted as workspace-item ids;
* JSON-Pointer metadata keys interpolated unescaped.
"""

from __future__ import annotations

import contextlib

import httpx
import pytest
import respx
from dspace_mcp.client import DSpaceError

from conftest import API, make_config
from dspace_mcp_write import tools
from dspace_mcp_write.client import WriteClient
from dspace_mcp_write.patch import item_metadata_patch, normalize_values

COLLECTION = "11111111-1111-4111-8111-111111111111"
OTHER = "99999999-9999-4999-8999-999999999999"
ITEM = "22222222-2222-4222-8222-222222222222"
WS_ID = "1001"
WS = f"{API}/submission/workspaceitems/{WS_ID}"


@contextlib.asynccontextmanager
async def make_client(config=None):
    config = config or make_config()
    http = WriteClient.build_http(config)
    client = WriteClient(config, http)
    client._jwt = "Bearer test-jwt"
    client._csrf = "test-csrf"
    try:
        yield client
    finally:
        await http.aclose()


# --------------------------------------------------------------------------- #
# Blocker 1 — string / non-list metadata must never be exploded per character
# --------------------------------------------------------------------------- #
def test_normalize_values_rejects_bare_string():
    with pytest.raises(TypeError):
        normalize_values("New title")  # type: ignore[arg-type]


def test_item_metadata_patch_rejects_bare_string_value():
    with pytest.raises(TypeError):
        item_metadata_patch({}, {"dc.title": "New title"})  # type: ignore[dict-item]


@respx.mock
async def test_update_item_metadata_string_value_raises_before_any_http():
    get = respx.get(f"{API}/core/items/{ITEM}").mock(
        return_value=httpx.Response(200, json={})
    )
    patch = respx.patch(f"{API}/core/items/{ITEM}").mock(
        return_value=httpx.Response(200, json={})
    )
    async with make_client() as client:
        with pytest.raises(DSpaceError) as ei:
            # The MCP schema is dict[str, Any]; a model can pass a bare string.
            await tools.update_item_metadata(
                client, ITEM, {"dc.title": "New"}, confirm=True
            )
    assert "must be a list" in str(ei.value)
    assert not get.called  # validated before the GET
    assert not patch.called  # and certainly before the PATCH


@respx.mock
async def test_create_workspace_item_string_value_makes_no_draft():
    post = respx.post(f"{API}/submission/workspaceitems").mock(
        return_value=httpx.Response(201, json={"id": 1001})
    )
    async with make_client() as client:
        with pytest.raises(DSpaceError):
            await tools.create_workspace_item(
                client, COLLECTION, {"dc.title": "New"}, confirm=True
            )
    assert not post.called  # no orphan draft created


# --------------------------------------------------------------------------- #
# Major 2 — no credential leaves the DSpace origin on a cross-origin redirect
# --------------------------------------------------------------------------- #
@respx.mock
async def test_no_auth_headers_leak_to_cross_origin_redirect():
    respx.get(f"{API}/core/bitstreams/x/content").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://cdn.example.net/file"}
        )
    )
    foreign = respx.get("https://cdn.example.net/file").mock(
        return_value=httpx.Response(200, content=b"payload")
    )
    async with make_client() as client:
        data = await client.stream_bytes(
            f"{API}/core/bitstreams/x/content", max_bytes=1000
        )
    assert data == b"payload"
    sent = {k.lower() for k in foreign.calls.last.request.headers}
    assert "authorization" not in sent
    assert "x-xsrf-token" not in sent


# --------------------------------------------------------------------------- #
# Major 3 — get_submission_form honours the whitelist before any write
# --------------------------------------------------------------------------- #
@respx.mock
async def test_get_submission_form_rejects_non_whitelisted_collection():
    post = respx.post(f"{API}/submission/workspaceitems").mock(
        return_value=httpx.Response(201, json={"id": 1001, "sections": {}})
    )
    config = make_config(write_collections=(OTHER,))
    async with make_client(config) as client:
        with pytest.raises(DSpaceError) as ei:
            await tools.get_submission_form(client, COLLECTION)
    assert "not in the allowed write list" in str(ei.value)
    assert not post.called  # the throwaway-draft fallback never fired


# --------------------------------------------------------------------------- #
# Minor 4 — an already-granted license must not block deposit
# --------------------------------------------------------------------------- #
@respx.mock
async def test_deposit_proceeds_when_license_already_granted():
    draft = {
        "id": int(WS_ID),
        "sections": {"publicationStep": {}, "license": {"granted": True}},
        "_embedded": {"collection": {"uuid": COLLECTION}},
        "_links": {"self": {"href": WS}},
    }
    respx.get(WS).mock(return_value=httpx.Response(200, json=draft))
    grant = respx.patch(WS).mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{API}/workflow/workflowitems").mock(
        return_value=httpx.Response(
            201,
            json={"id": 7, "_links": {"item": {"href": f"{API}/core/items/{ITEM}"}}},
        )
    )
    respx.get(f"{API}/core/items/{ITEM}").mock(
        return_value=httpx.Response(200, json={"inArchive": True})
    )
    async with make_client() as client:
        # grant_license=False, yet the license is already granted -> publish.
        result = await tools.deposit_workspace_item(
            client, WS_ID, grant_license=False, confirm=True
        )
    assert result.get("status") != "license_required"
    assert result.get("archived") is True
    assert not grant.called  # no redundant grant PATCH


# --------------------------------------------------------------------------- #
# Minor 5 — write 422 uses write wording, not the read "search filter" text
# --------------------------------------------------------------------------- #
@respx.mock
async def test_write_422_wording_is_not_search_filter():
    respx.patch(f"{API}/core/items/{ITEM}").mock(
        return_value=httpx.Response(422, json={"message": "dc.title is required"})
    )
    async with make_client() as client:
        with pytest.raises(DSpaceError) as ei:
            await client.mutate(
                "PATCH", f"/core/items/{ITEM}", json=[], where="update item metadata"
            )
    msg = str(ei.value)
    assert "search filter" not in msg
    assert "Validation failed" in msg
    assert "dc.title is required" in msg  # body detail is appended


# --------------------------------------------------------------------------- #
# Nit 9 / Nit 10 — id validation and JSON-Pointer escaping
# --------------------------------------------------------------------------- #
def test_require_ws_id_rejects_non_ascii_digits():
    with pytest.raises(DSpaceError):
        tools._require_ws_id("٣")  # Arabic-Indic digit three


def test_item_metadata_patch_escapes_pointer_special_chars():
    ops = item_metadata_patch({}, {"weird/key~name": ["v"]})
    assert ops == [
        {"op": "add", "path": "/metadata/weird~1key~0name", "value": [{"value": "v"}]}
    ]
