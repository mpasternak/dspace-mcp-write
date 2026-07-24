"""Unit tests for the write tools (respx + httpx, no live network).

Each tool is exercised for: the happy path, the ``confirm=False`` preview that
issues no mutating request, whitelist rejection, validation errors with body
detail, the upload size limit, deposit license handling, and the create
partial-failure that keeps the draft. A real ``WriteClient`` is built against
respx mocks with its auth state set directly, so login is skipped.
"""

from __future__ import annotations

import base64
import contextlib

import httpx
import pytest
import respx
from dspace_mcp.client import DSpaceError

from conftest import API, make_config
from dspace_mcp_write import tools
from dspace_mcp_write.client import WriteClient

COLLECTION = "11111111-1111-4111-8111-111111111111"
OTHER = "99999999-9999-4999-8999-999999999999"
ITEM = "22222222-2222-4222-8222-222222222222"
BUNDLE = "33333333-3333-4333-8333-333333333333"
WS_ID = "1001"
WS = f"{API}/submission/workspaceitems/{WS_ID}"

FORM_PUBLICATION = {
    "id": "publicationStep",
    "rows": [
        {
            "fields": [
                {"selectableMetadata": [{"metadata": "dc.title"}], "mandatory": True}
            ]
        },
        {
            "fields": [
                {
                    "selectableMetadata": [{"metadata": "dc.contributor.author"}],
                    "mandatory": False,
                }
            ]
        },
    ],
}


@contextlib.asynccontextmanager
async def make_client(config=None):
    config = config or make_config()
    http = WriteClient.build_http(config)
    client = WriteClient(config, http)
    # Skip the login handshake by seeding the auth state the hooks read.
    client._jwt = "Bearer test-jwt"
    client._csrf = "test-csrf"
    try:
        yield client
    finally:
        await http.aclose()


def _mock_submission_forms() -> None:
    """publicationStep is a form (200); the rest are not (400)."""
    respx.get(f"{API}/config/submissionforms/publicationStep").mock(
        return_value=httpx.Response(200, json=FORM_PUBLICATION)
    )
    for sid in ("upload", "license", "collection", "traditionalpagetwo"):
        respx.get(f"{API}/config/submissionforms/{sid}").mock(
            return_value=httpx.Response(400, json={"message": "not a form"})
        )


def _create_response() -> dict:
    return {
        "id": int(WS_ID),
        "sections": {
            "publicationStep": {},
            "upload": {},
            "license": {"granted": False},
            "collection": {},
        },
        "_links": {"self": {"href": WS}},
    }


def _draft(collection: str = COLLECTION, with_license: bool = False) -> dict:
    sections: dict = {"publicationStep": {}}
    if with_license:
        sections["license"] = {"granted": False}
    return {
        "id": int(WS_ID),
        "sections": sections,
        "_embedded": {"collection": {"uuid": collection}},
        "_links": {"self": {"href": WS}},
    }


# --------------------------------------------------------------------------- #
# create_workspace_item
# --------------------------------------------------------------------------- #
@respx.mock
async def test_create_preview_makes_no_write():
    post = respx.post(f"{API}/submission/workspaceitems").mock(
        return_value=httpx.Response(201, json=_create_response())
    )
    async with make_client() as client:
        result = await tools.create_workspace_item(
            client, COLLECTION, {"dc.title": ["Hello"]}, confirm=False
        )
    assert result["confirm_required"] is True
    assert result["preview"]["collection"] == COLLECTION
    assert not post.called


@respx.mock
async def test_create_whitelist_rejects_before_write():
    post = respx.post(f"{API}/submission/workspaceitems").mock(
        return_value=httpx.Response(201, json=_create_response())
    )
    config = make_config(write_collections=(OTHER,))
    async with make_client(config) as client:
        with pytest.raises(DSpaceError):
            await tools.create_workspace_item(
                client, COLLECTION, {"dc.title": ["Hi"]}, confirm=True
            )
    assert not post.called


@respx.mock
async def test_create_happy_path():
    respx.post(f"{API}/submission/workspaceitems").mock(
        return_value=httpx.Response(201, json=_create_response())
    )
    _mock_submission_forms()
    patch = respx.patch(WS).mock(return_value=httpx.Response(200, json={"id": 1001}))
    async with make_client() as client:
        result = await tools.create_workspace_item(
            client, COLLECTION, {"dc.title": ["Hello"]}, confirm=True
        )
    assert result["workspaceitem_id"] == WS_ID
    assert result["routed_sections"] == {"publicationStep": ["dc.title"]}
    assert patch.called


@respx.mock
async def test_create_partial_failure_keeps_draft():
    respx.post(f"{API}/submission/workspaceitems").mock(
        return_value=httpx.Response(201, json=_create_response())
    )
    _mock_submission_forms()
    respx.patch(WS).mock(
        return_value=httpx.Response(
            422, json={"errors": [{"message": "date.issued required"}]}
        )
    )
    delete = respx.delete(WS).mock(return_value=httpx.Response(204))
    async with make_client() as client:
        result = await tools.create_workspace_item(
            client, COLLECTION, {"dc.title": ["Hello"]}, confirm=True
        )
    assert result["workspaceitem_id"] == WS_ID
    assert result["status"] == "metadata_validation_failed"
    assert "errors" in result
    assert not delete.called  # draft left for repair


@respx.mock
async def test_create_unroutable_discards_and_raises():
    respx.post(f"{API}/submission/workspaceitems").mock(
        return_value=httpx.Response(201, json=_create_response())
    )
    _mock_submission_forms()
    delete = respx.delete(WS).mock(return_value=httpx.Response(204))
    async with make_client() as client:
        with pytest.raises(DSpaceError):
            await tools.create_workspace_item(
                client, COLLECTION, {"dc.nonexistent": ["x"]}, confirm=True
            )
    assert delete.called


# --------------------------------------------------------------------------- #
# update_workspace_item_metadata
# --------------------------------------------------------------------------- #
@respx.mock
async def test_update_ws_whitelist_rejects():
    respx.get(WS).mock(return_value=httpx.Response(200, json=_draft()))
    patch = respx.patch(WS).mock(return_value=httpx.Response(200, json={}))
    config = make_config(write_collections=(OTHER,))
    async with make_client(config) as client:
        with pytest.raises(DSpaceError):
            await tools.update_workspace_item_metadata(
                client, WS_ID, {"dc.title": ["x"]}, confirm=True
            )
    assert not patch.called


@respx.mock
async def test_update_ws_happy_path():
    respx.get(WS).mock(return_value=httpx.Response(200, json=_draft()))
    _mock_submission_forms()
    patch = respx.patch(WS).mock(return_value=httpx.Response(200, json={}))
    async with make_client() as client:
        result = await tools.update_workspace_item_metadata(
            client, WS_ID, {"dc.title": ["New"]}, confirm=True
        )
    assert result["workspaceitem_id"] == WS_ID
    assert patch.called


# --------------------------------------------------------------------------- #
# upload_file_to_workspace_item
# --------------------------------------------------------------------------- #
@respx.mock
async def test_upload_preview_makes_no_write():
    respx.get(WS).mock(return_value=httpx.Response(200, json=_draft()))
    post = respx.post(WS).mock(return_value=httpx.Response(201, json={}))
    b64 = base64.b64encode(b"a pdf").decode()
    async with make_client() as client:
        result = await tools.upload_file_to_workspace_item(
            client, WS_ID, content_base64=b64, name="a.pdf", confirm=False
        )
    assert result["confirm_required"] is True
    assert result["preview"]["filename"] == "a.pdf"
    assert not post.called


@respx.mock
async def test_upload_happy_path():
    respx.get(WS).mock(return_value=httpx.Response(200, json=_draft()))
    upload = respx.post(WS).mock(
        return_value=httpx.Response(
            201,
            json={
                "sections": {"upload": {"files": [{"uuid": "bs-1", "sizeBytes": 5}]}}
            },
        )
    )
    b64 = base64.b64encode(b"a pdf").decode()
    async with make_client() as client:
        result = await tools.upload_file_to_workspace_item(
            client, WS_ID, content_base64=b64, name="a.pdf", confirm=True
        )
    assert result["bitstream"] == "bs-1"
    assert upload.called


async def test_upload_rejects_oversized_base64():
    config = make_config(upload_max_mb=1)
    oversized = "A" * 1_400_004  # len*3/4 > 1 MB, refused before decoding
    async with make_client(config) as client:
        with pytest.raises(DSpaceError):
            await tools.upload_file_to_workspace_item(
                client, WS_ID, content_base64=oversized, confirm=True
            )


async def test_upload_rejects_both_inputs():
    async with make_client() as client:
        with pytest.raises(DSpaceError):
            await tools.upload_file_to_workspace_item(
                client, WS_ID, file_path="/x", content_base64="AAAA", confirm=True
            )


# --------------------------------------------------------------------------- #
# deposit_workspace_item
# --------------------------------------------------------------------------- #
@respx.mock
async def test_deposit_license_required_without_grant():
    respx.get(WS).mock(return_value=httpx.Response(200, json=_draft(with_license=True)))
    workflow = respx.post(f"{API}/workflow/workflowitems").mock(
        return_value=httpx.Response(201, json={})
    )
    async with make_client() as client:
        result = await tools.deposit_workspace_item(
            client, WS_ID, grant_license=False, confirm=True
        )
    assert result["status"] == "license_required"
    assert not workflow.called


@respx.mock
async def test_deposit_grants_license_and_publishes():
    respx.get(WS).mock(return_value=httpx.Response(200, json=_draft(with_license=True)))
    grant = respx.patch(WS).mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{API}/workflow/workflowitems").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 7,
                "type": "workflowitem",
                "_links": {"item": {"href": f"{API}/core/items/{ITEM}"}},
            },
        )
    )
    respx.get(f"{API}/core/items/{ITEM}").mock(
        return_value=httpx.Response(200, json={"inArchive": True})
    )
    async with make_client() as client:
        result = await tools.deposit_workspace_item(
            client, WS_ID, grant_license=True, confirm=True
        )
    assert grant.called
    assert result["archived"] is True


@respx.mock
async def test_deposit_without_license_section_publishes():
    respx.get(WS).mock(return_value=httpx.Response(200, json=_draft()))
    workflow = respx.post(f"{API}/workflow/workflowitems").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 8,
                "type": "workflowitem",
                "_links": {"item": {"href": f"{API}/core/items/{ITEM}"}},
            },
        )
    )
    respx.get(f"{API}/core/items/{ITEM}").mock(
        return_value=httpx.Response(200, json={"inArchive": False})
    )
    async with make_client() as client:
        result = await tools.deposit_workspace_item(
            client, WS_ID, grant_license=False, confirm=True
        )
    assert workflow.called
    assert result["in_workflow"] is True


@respx.mock
async def test_deposit_preview_makes_no_write():
    respx.get(WS).mock(return_value=httpx.Response(200, json=_draft(with_license=True)))
    workflow = respx.post(f"{API}/workflow/workflowitems").mock(
        return_value=httpx.Response(201, json={})
    )
    grant = respx.patch(WS).mock(return_value=httpx.Response(200, json={}))
    async with make_client() as client:
        result = await tools.deposit_workspace_item(
            client, WS_ID, grant_license=True, confirm=False
        )
    assert result["confirm_required"] is True
    assert result["preview"]["license_section"] == "license"
    assert not workflow.called
    assert not grant.called


# --------------------------------------------------------------------------- #
# discard_workspace_item
# --------------------------------------------------------------------------- #
@respx.mock
async def test_discard_preview_makes_no_write():
    delete = respx.delete(WS).mock(return_value=httpx.Response(204))
    async with make_client() as client:
        result = await tools.discard_workspace_item(client, WS_ID, confirm=False)
    assert result["confirm_required"] is True
    assert not delete.called


@respx.mock
async def test_discard_happy_path():
    delete = respx.delete(WS).mock(return_value=httpx.Response(204))
    async with make_client() as client:
        result = await tools.discard_workspace_item(client, WS_ID, confirm=True)
    assert result["discarded"] is True
    assert delete.called


# --------------------------------------------------------------------------- #
# add_file_to_item
# --------------------------------------------------------------------------- #
@respx.mock
async def test_add_file_whitelist_rejects():
    respx.get(f"{API}/core/items/{ITEM}").mock(
        return_value=httpx.Response(
            200, json={"_embedded": {"owningCollection": {"uuid": COLLECTION}}}
        )
    )
    bitstreams = respx.post(f"{API}/core/bundles/{BUNDLE}/bitstreams").mock(
        return_value=httpx.Response(201, json={})
    )
    config = make_config(write_collections=(OTHER,))
    b64 = base64.b64encode(b"x").decode()
    async with make_client(config) as client:
        with pytest.raises(DSpaceError):
            await tools.add_file_to_item(
                client, ITEM, content_base64=b64, name="x.bin", confirm=True
            )
    assert not bitstreams.called


@respx.mock
async def test_add_file_happy_path_existing_bundle():
    respx.get(f"{API}/core/items/{ITEM}").mock(
        return_value=httpx.Response(
            200, json={"_embedded": {"owningCollection": {"uuid": COLLECTION}}}
        )
    )
    respx.get(f"{API}/core/items/{ITEM}/bundles").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {"bundles": [{"uuid": BUNDLE, "name": "ORIGINAL"}]},
                "page": {"totalElements": 1},
            },
        )
    )
    bitstreams = respx.post(f"{API}/core/bundles/{BUNDLE}/bitstreams").mock(
        return_value=httpx.Response(201, json={"uuid": "bs-2", "sizeBytes": 1})
    )
    b64 = base64.b64encode(b"x").decode()
    async with make_client() as client:
        result = await tools.add_file_to_item(
            client, ITEM, content_base64=b64, name="x.bin", confirm=True
        )
    assert result["bitstream"] == "bs-2"
    assert result["bundle"] == BUNDLE
    assert bitstreams.called


@respx.mock
async def test_add_file_creates_missing_bundle():
    respx.get(f"{API}/core/items/{ITEM}").mock(
        return_value=httpx.Response(
            200, json={"_embedded": {"owningCollection": {"uuid": COLLECTION}}}
        )
    )
    respx.get(f"{API}/core/items/{ITEM}/bundles").mock(
        return_value=httpx.Response(
            200, json={"_embedded": {"bundles": []}, "page": {"totalElements": 0}}
        )
    )
    make_bundle = respx.post(f"{API}/core/items/{ITEM}/bundles").mock(
        return_value=httpx.Response(201, json={"uuid": BUNDLE, "name": "ORIGINAL"})
    )
    respx.post(f"{API}/core/bundles/{BUNDLE}/bitstreams").mock(
        return_value=httpx.Response(201, json={"uuid": "bs-3", "sizeBytes": 1})
    )
    b64 = base64.b64encode(b"x").decode()
    async with make_client() as client:
        result = await tools.add_file_to_item(
            client, ITEM, content_base64=b64, name="x.bin", confirm=True
        )
    assert make_bundle.called
    assert result["bitstream"] == "bs-3"


# --------------------------------------------------------------------------- #
# update_item_metadata
# --------------------------------------------------------------------------- #
@respx.mock
async def test_update_item_preview_makes_no_write():
    respx.get(f"{API}/core/items/{ITEM}").mock(
        return_value=httpx.Response(
            200,
            json={
                "metadata": {"dc.title": [{"value": "Old"}]},
                "_embedded": {"owningCollection": {"uuid": COLLECTION}},
            },
        )
    )
    patch = respx.patch(f"{API}/core/items/{ITEM}").mock(
        return_value=httpx.Response(200, json={})
    )
    async with make_client() as client:
        result = await tools.update_item_metadata(
            client, ITEM, {"dc.title": ["New"]}, confirm=False
        )
    assert result["confirm_required"] is True
    assert not patch.called


@respx.mock
async def test_update_item_happy_path():
    respx.get(f"{API}/core/items/{ITEM}").mock(
        return_value=httpx.Response(
            200,
            json={
                "metadata": {"dc.title": [{"value": "Old"}]},
                "_embedded": {"owningCollection": {"uuid": COLLECTION}},
            },
        )
    )
    patch = respx.patch(f"{API}/core/items/{ITEM}").mock(
        return_value=httpx.Response(200, json={"uuid": ITEM})
    )
    async with make_client() as client:
        result = await tools.update_item_metadata(
            client, ITEM, {"dc.title": ["New"]}, confirm=True
        )
    assert result["item"] == ITEM
    assert patch.called


# --------------------------------------------------------------------------- #
# create_collection / create_community
# --------------------------------------------------------------------------- #
@respx.mock
async def test_create_collection_preview_makes_no_write():
    post = respx.post(f"{API}/core/collections").mock(
        return_value=httpx.Response(201, json={})
    )
    async with make_client() as client:
        result = await tools.create_collection(
            client, COLLECTION, "New Collection", confirm=False
        )
    assert result["confirm_required"] is True
    assert not post.called


@respx.mock
async def test_create_collection_happy_path():
    post = respx.post(f"{API}/core/collections").mock(
        return_value=httpx.Response(201, json={"uuid": "coll-new", "name": "New"})
    )
    async with make_client() as client:
        result = await tools.create_collection(client, COLLECTION, "New", confirm=True)
    assert result["collection"] == "coll-new"
    assert post.called


@respx.mock
async def test_create_community_happy_path():
    post = respx.post(f"{API}/core/communities").mock(
        return_value=httpx.Response(201, json={"uuid": "comm-new", "name": "Root"})
    )
    async with make_client() as client:
        result = await tools.create_community(client, "Root", confirm=True)
    assert result["community"] == "comm-new"
    assert post.called


# --------------------------------------------------------------------------- #
# get_submission_form (read-only path)
# --------------------------------------------------------------------------- #
@respx.mock
async def test_get_submission_form_read_only():
    respx.get(f"{API}/config/submissiondefinitions/search/findByCollection").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {
                    "sections": {
                        "_embedded": {
                            "submissionsections": [
                                {"id": "publicationStep"},
                                {"id": "upload"},
                                {"id": "license"},
                            ]
                        }
                    }
                }
            },
        )
    )
    _mock_submission_forms()
    async with make_client() as client:
        result = await tools.get_submission_form(client, COLLECTION)
    assert result["collection"] == COLLECTION
    assert "publicationStep" in result["sections"]
    assert "dc.title" in result["required"]
