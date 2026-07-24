"""Write-tool logic for the DSpace 7+ MCP server.

Like ``dspace_mcp.tools`` this module knows nothing about MCP: every function
takes a :class:`~dspace_mcp_write.client.WriteClient` and returns a plain dict.
That keeps the tools testable without a running server and leaves ``server.py``
a thin adapter.

Two cross-cutting rules live here (design spec D7):

* Every mutating tool takes ``confirm: bool = False``. With ``confirm=False`` the
  tool returns ``{"preview": {...}, "confirm_required": True}`` describing the
  planned operation and performs **no** mutating request (read-only GETs used to
  build a useful preview are allowed).
* The collection whitelist is enforced **before** any mutating request. Deposit
  and file tools resolve the target's owning collection with a GET first.

All user-facing strings are English: the audience is a language model.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any

from dspace_mcp.client import DSpaceError, is_uuid, require_uuid

from .client import WriteClient
from .config import WriteConfig
from .forms import detect_form_sections, required_fields, route_metadata
from .patch import item_metadata_patch, submission_patch

#: Content types we set ourselves so DSpace does not answer 415 (spec D8).
_JSON_CT = {"Content-Type": "application/json"}
_URI_LIST_CT = {"Content-Type": "text/uri-list"}

#: Fallback name for an inline (base64) upload with no explicit ``name``.
_DEFAULT_UPLOAD_NAME = "upload.bin"


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #
def require_collection_allowed(config: WriteConfig, uuid: str) -> None:
    """Raise ``DSpaceError`` if ``uuid`` is outside the configured whitelist.

    An empty whitelist means "all collections" (see ``WriteConfig``). This runs
    before any mutating request, so a disallowed target never reaches DSpace.
    """
    if not config.collection_allowed(uuid):
        raise DSpaceError(
            f"collection {uuid} is not in the allowed write list. "
            "Set DSPACE_WRITE_COLLECTIONS to include it, or pick an allowed one."
        )


def read_file_input(
    config: WriteConfig,
    file_path: str | None,
    content_base64: str | None,
) -> tuple[bytes, str]:
    """Read the upload payload from exactly one of ``file_path``/``content_base64``.

    Enforces ``config.upload_max_bytes``. For base64 the encoded length is
    checked (``len * 3 / 4``) *before* decoding, so an oversized blob is refused
    without allocating it. Returns ``(bytes, filename)``.
    """
    if bool(file_path) == bool(content_base64):
        raise DSpaceError(
            "Provide exactly one of file_path or content_base64, not both or neither."
        )

    limit = config.upload_max_bytes
    over = (
        f"file exceeds the {config.upload_max_mb} MB upload limit. "
        "Raise DSPACE_UPLOAD_MAX_MB or send a smaller file."
    )

    if file_path:
        path = Path(file_path)
        if not path.is_file():
            raise DSpaceError(f"No readable file at {file_path}.")
        if path.stat().st_size > limit:
            raise DSpaceError(over)
        return path.read_bytes(), path.name

    # base64 branch: cheap length check before the expensive decode.
    approx = len(content_base64) * 3 // 4
    if approx > limit:
        raise DSpaceError(over)
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DSpaceError("content_base64 is not valid base64.") from exc
    if len(data) > limit:
        raise DSpaceError(over)
    return data, _DEFAULT_UPLOAD_NAME


def _metadata_summary(metadata: dict[str, Any] | None) -> dict[str, int]:
    """Compact preview of a metadata dict: key -> number of values."""
    summary: dict[str, int] = {}
    for key, values in (metadata or {}).items():
        summary[key] = len(values) if isinstance(values, list) else 1
    return summary


def _normalize_value(value: Any) -> dict[str, Any]:
    """A single metadata value: string shorthand -> ``{"value": str}``."""
    if isinstance(value, str):
        return {"value": value}
    if isinstance(value, dict):
        return value
    raise DSpaceError("Metadata values must be strings or objects with a 'value' key.")


def _normalize_metadata_map(metadata: dict[str, Any] | None) -> dict[str, list]:
    """Normalize a flat DC map into the ``/metadata`` object shape."""
    result: dict[str, list] = {}
    for key, values in (metadata or {}).items():
        if not isinstance(values, list):
            raise DSpaceError(f"Metadata for '{key}' must be a list of values.")
        result[key] = [_normalize_value(v) for v in values]
    return result


def _require_ws_id(value: str) -> str:
    """Validate a workspace-item id (DSpace uses integers here, not UUIDs)."""
    text = str(value).strip()
    if not text.isdigit():
        raise DSpaceError(
            f"'{value}' is not a valid workspace item id (expected an integer)."
        )
    return text


def _self_href(payload: dict) -> str | None:
    link = payload.get("_links", {}).get("self", {})
    href = link.get("href") if isinstance(link, dict) else None
    return href if isinstance(href, str) and href else None


def _embedded_uuid(payload: dict, relation: str) -> str | None:
    """UUID of an embedded related object (``owningCollection``/``collection``)."""
    obj = payload.get("_embedded", {}).get(relation)
    if isinstance(obj, dict):
        value = obj.get("uuid") or obj.get("id")
        if value:
            return str(value)
    return None


def _uuid_from_link(payload: dict, relation: str) -> str | None:
    """Best-effort UUID for a related object from its link or embed."""
    link = payload.get("_links", {}).get(relation)
    if isinstance(link, dict):
        href = link.get("href")
        if isinstance(href, str) and href:
            return href.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return _embedded_uuid(payload, relation)


def _find_license_section(sections: dict[str, Any]) -> str | None:
    """The license section is the one exposing a ``granted`` slot (spec D2).

    We detect it rather than hardcoding the name ``license``: an installation
    may rename it. Form sections serialize as empty ``{}`` on a fresh draft, but
    the license section carries ``granted`` even before it is accepted.
    """
    for section_id, value in (sections or {}).items():
        if isinstance(value, dict) and "granted" in value:
            return section_id
    return None


def _uploaded_bitstream(payload: dict) -> tuple[str | None, int | None]:
    """UUID and size of the most recently uploaded file in a workspaceitem."""
    upload = payload.get("sections", {}).get("upload") or {}
    files = upload.get("files") if isinstance(upload, dict) else None
    if isinstance(files, list) and files and isinstance(files[-1], dict):
        last = files[-1]
        return last.get("uuid"), last.get("sizeBytes")
    return None, None


async def _discard_quietly(client: WriteClient, ws_id: str) -> str | None:
    """Best-effort discard of a draft; returns an error string on failure.

    The failure is surfaced by the caller (folded into the raised message), so
    this is a conversion, not a silent swallow.
    """
    try:
        await client.mutate(
            "DELETE",
            f"/submission/workspaceitems/{ws_id}",
            where="discard draft",
        )
    except DSpaceError as exc:
        return exc.message
    return None


# --------------------------------------------------------------------------- #
# Collection resolution for the whitelist / form checks
# --------------------------------------------------------------------------- #
async def _workspaceitem(client: WriteClient, ws_id: str) -> dict:
    """GET a draft with its owning collection embedded."""
    return await client.get(
        f"/submission/workspaceitems/{ws_id}", {"embed": "collection"}
    )


def _owning_collection(payload: dict, relation: str) -> str:
    uuid = _embedded_uuid(payload, relation)
    if not uuid:
        # Fall back to the relation link only when its href ends in a real UUID
        # (a bare relation endpoint like ``.../collection`` does not).
        link = payload.get("_links", {}).get(relation)
        href = link.get("href") if isinstance(link, dict) else None
        if isinstance(href, str) and href:
            tail = href.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
            if is_uuid(tail):
                uuid = tail
    if not uuid:
        raise DSpaceError(
            "Could not determine the owning collection of the target; the "
            "object may be a template or lack an owning collection."
        )
    return uuid


# --------------------------------------------------------------------------- #
# Submission form
# --------------------------------------------------------------------------- #
def _section_ids_from_definition(definition: dict) -> list[str]:
    """Extract submission section ids from a submission definition resource."""
    embedded = definition.get("_embedded")
    if not isinstance(embedded, dict):
        return []
    sections = embedded.get("sections")
    candidates: list = []
    if isinstance(sections, list):
        candidates = sections
    elif isinstance(sections, dict):
        inner = sections.get("_embedded")
        if isinstance(inner, dict):
            for value in inner.values():
                if isinstance(value, list):
                    candidates = value
                    break
    return [str(e["id"]) for e in candidates if isinstance(e, dict) and e.get("id")]


async def _section_ids_via_throwaway(client: WriteClient, collection: str) -> list[str]:
    """Fallback: create a draft to read its section names, then discard it."""
    created = await client.mutate(
        "POST",
        f"/submission/workspaceitems?owningCollection={collection}",
        json={},
        headers=_JSON_CT,
        where="probe submission form",
    )
    ws_id = created.get("id")
    try:
        return list((created.get("sections") or {}).keys())
    finally:
        if ws_id is not None:
            await _discard_quietly(client, str(ws_id))


async def _collection_section_ids(client: WriteClient, collection: str) -> list[str]:
    """Section ids for a collection, read-only when the instance supports it."""
    try:
        definition = await client.get(
            "/config/submissiondefinitions/search/findByCollection",
            {"uuid": collection},
        )
    except DSpaceError as exc:
        # Endpoint absent on some versions -> fall back to a throwaway draft.
        if exc.status not in (400, 404, 405):
            raise
        return await _section_ids_via_throwaway(client, collection)
    ids = _section_ids_from_definition(definition)
    return ids or await _section_ids_via_throwaway(client, collection)


async def get_submission_form(client: WriteClient, collection: str) -> dict[str, Any]:
    """Report a collection's submission fields and which are required.

    Read-only when the instance exposes the submission definition; only if that
    is unavailable does it fall back to a throwaway draft (discarded in a
    ``finally``).
    """
    uuid = require_uuid(collection, "collection")
    section_ids = await _collection_section_ids(client, uuid)
    sections = await detect_form_sections(client, section_ids)
    fields = {
        section: [f["metadata"] for f in flds] for section, flds in sections.items()
    }
    return {
        "collection": uuid,
        "sections": fields,
        "required": required_fields(sections),
        "message": (
            "Supply at least the required fields when creating a workspace item."
        ),
    }


# --------------------------------------------------------------------------- #
# Deposit group
# --------------------------------------------------------------------------- #
async def create_workspace_item(
    client: WriteClient,
    collection: str,
    metadata: dict[str, Any],
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a draft (workspaceitem) in ``collection`` and set its metadata."""
    config: WriteConfig = client.config
    uuid = require_uuid(collection, "collection")
    require_collection_allowed(config, uuid)

    if not confirm:
        return {
            "preview": {
                "action": "create_workspace_item",
                "method": "POST",
                "endpoint": f"/submission/workspaceitems?owningCollection={uuid}",
                "collection": uuid,
                "metadata": _metadata_summary(metadata),
            },
            "confirm_required": True,
        }

    created = await client.mutate(
        "POST",
        f"/submission/workspaceitems?owningCollection={uuid}",
        json={},
        headers=_JSON_CT,
        where="create workspace item",
    )
    ws_id = str(created.get("id"))
    self_href = _self_href(created)
    section_ids = list((created.get("sections") or {}).keys())
    sections = await detect_form_sections(client, section_ids)
    routed, unroutable = route_metadata(metadata or {}, sections)

    if unroutable:
        cleanup = await _discard_quietly(client, ws_id)
        tail = (
            " The empty draft was discarded."
            if cleanup is None
            else f" The empty draft {ws_id} could not be discarded ({cleanup})."
        )
        raise DSpaceError(
            "No submission form section accepts these metadata keys: "
            f"{', '.join(sorted(unroutable))}." + tail
        )

    if routed:
        try:
            await client.mutate(
                "PATCH",
                f"/submission/workspaceitems/{ws_id}",
                json=submission_patch(routed),
                headers=_JSON_CT,
                where="set workspace item metadata",
            )
        except DSpaceError as exc:
            if exc.status == 422:
                # Partial failure: keep the draft so it can be repaired.
                return {
                    "workspaceitem_id": ws_id,
                    "collection": uuid,
                    "self_href": self_href,
                    "status": "metadata_validation_failed",
                    "errors": exc.message,
                    "message": (
                        "The draft was created but the metadata was rejected "
                        "(422). The draft was kept; fix the values and call "
                        "update_workspace_item_metadata."
                    ),
                }
            raise

    return {
        "workspaceitem_id": ws_id,
        "collection": uuid,
        "self_href": self_href,
        "routed_sections": {sec: list(km) for sec, km in routed.items()},
        "message": (
            "Draft created. Upload files, then call deposit_workspace_item to publish."
        ),
    }


async def update_workspace_item_metadata(
    client: WriteClient,
    workspaceitem_id: str,
    metadata: dict[str, Any],
    confirm: bool = False,
) -> dict[str, Any]:
    """Add or fix metadata on an existing draft (repairs a partial create)."""
    config: WriteConfig = client.config
    ws_id = _require_ws_id(workspaceitem_id)
    draft = await _workspaceitem(client, ws_id)
    collection = _owning_collection(draft, "collection")
    require_collection_allowed(config, collection)

    if not confirm:
        return {
            "preview": {
                "action": "update_workspace_item_metadata",
                "method": "PATCH",
                "endpoint": f"/submission/workspaceitems/{ws_id}",
                "workspaceitem_id": ws_id,
                "collection": collection,
                "metadata": _metadata_summary(metadata),
            },
            "confirm_required": True,
        }

    section_ids = list((draft.get("sections") or {}).keys())
    sections = await detect_form_sections(client, section_ids)
    routed, unroutable = route_metadata(metadata or {}, sections)
    if unroutable:
        raise DSpaceError(
            "No submission form section accepts these metadata keys: "
            f"{', '.join(sorted(unroutable))}."
        )
    if routed:
        await client.mutate(
            "PATCH",
            f"/submission/workspaceitems/{ws_id}",
            json=submission_patch(routed),
            headers=_JSON_CT,
            where="update workspace item metadata",
        )
    return {
        "workspaceitem_id": ws_id,
        "collection": collection,
        "updated_sections": {sec: list(km) for sec, km in routed.items()},
        "message": "Draft metadata updated.",
    }


async def upload_file_to_workspace_item(
    client: WriteClient,
    workspaceitem_id: str,
    file_path: str | None = None,
    content_base64: str | None = None,
    name: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Upload a file (bitstream) into a draft's upload section."""
    config: WriteConfig = client.config
    ws_id = _require_ws_id(workspaceitem_id)
    data, filename = read_file_input(config, file_path, content_base64)
    if name:
        filename = name

    draft = await _workspaceitem(client, ws_id)
    collection = _owning_collection(draft, "collection")
    require_collection_allowed(config, collection)

    if not confirm:
        return {
            "preview": {
                "action": "upload_file_to_workspace_item",
                "method": "POST",
                "endpoint": f"/submission/workspaceitems/{ws_id}",
                "workspaceitem_id": ws_id,
                "collection": collection,
                "filename": filename,
                "size_bytes": len(data),
            },
            "confirm_required": True,
        }

    result = await client.upload(
        f"/submission/workspaceitems/{ws_id}",
        file_bytes=data,
        filename=filename,
        where="upload file to workspace item",
    )
    bitstream, size = _uploaded_bitstream(result)
    return {
        "workspaceitem_id": ws_id,
        "bitstream": bitstream,
        "filename": filename,
        "size_bytes": size if size is not None else len(data),
        "message": "File uploaded to the draft.",
    }


async def deposit_workspace_item(
    client: WriteClient,
    workspaceitem_id: str,
    grant_license: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Publish a draft: grant the deposit license, then push it to workflow.

    ``grant_license`` is legally meaningful: it accepts the repository's deposit
    license on behalf of the configured account. With ``grant_license=False``
    and a license section present, nothing is published.
    """
    config: WriteConfig = client.config
    ws_id = _require_ws_id(workspaceitem_id)
    draft = await _workspaceitem(client, ws_id)
    collection = _owning_collection(draft, "collection")
    require_collection_allowed(config, collection)

    license_section = _find_license_section(draft.get("sections") or {})
    self_href = _self_href(draft)

    if not confirm:
        return {
            "preview": {
                "action": "deposit_workspace_item",
                "method": "POST",
                "endpoint": "/workflow/workflowitems",
                "workspaceitem_id": ws_id,
                "collection": collection,
                "license_section": license_section,
                "grant_license": bool(grant_license),
                "license_required": bool(license_section) and not grant_license,
            },
            "confirm_required": True,
        }

    if license_section and not grant_license:
        return {
            "workspaceitem_id": ws_id,
            "status": "license_required",
            "license_section": license_section,
            "message": (
                "This collection requires accepting the deposit license. "
                "Re-call deposit_workspace_item with grant_license=true to "
                "accept it on behalf of the configured account."
            ),
        }

    if license_section and grant_license:
        await client.mutate(
            "PATCH",
            f"/submission/workspaceitems/{ws_id}",
            json=[
                {
                    "op": "add",
                    "path": f"/sections/{license_section}/granted",
                    "value": "true",
                }
            ],
            headers=_JSON_CT,
            where="grant deposit license",
        )

    if not self_href:
        raise DSpaceError("Could not find the draft's self link needed to submit it.")

    workflow = await client.mutate(
        "POST",
        "/workflow/workflowitems",
        content=self_href,
        headers=_URI_LIST_CT,
        where="deposit workspace item",
    )
    if not isinstance(workflow, dict):
        workflow = {}
    return await _deposit_result(client, ws_id, workflow)


async def _deposit_result(
    client: WriteClient, ws_id: str, workflow: dict
) -> dict[str, Any]:
    """GET the resulting object and report archived-vs-workflow (spec D2)."""
    item_uuid = _uuid_from_link(workflow, "item")
    archived: bool | None = None
    if item_uuid:
        try:
            item = await client.get(f"/core/items/{item_uuid}")
        except DSpaceError as exc:
            # Deposit already succeeded; report an unknown state rather than
            # masking a successful publish as a failure.
            return {
                "workspaceitem_id": ws_id,
                "workflowitem_id": workflow.get("id"),
                "item": item_uuid,
                "archived": None,
                "note": f"could not confirm archive state: {exc.message}",
                "message": "Deposit submitted; final state unknown.",
            }
        archived = bool(item.get("inArchive"))

    if archived:
        message = "The item was archived and is now publicly available."
    elif archived is False:
        message = "The item entered the collection's review workflow."
    else:
        message = "Deposit submitted; the resulting state is unknown."
    return {
        "workspaceitem_id": ws_id,
        "workflowitem_id": workflow.get("id"),
        "item": item_uuid,
        "archived": archived,
        "in_workflow": archived is False,
        "message": message,
    }


async def discard_workspace_item(
    client: WriteClient,
    workspaceitem_id: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete one of the account's own drafts (never an archived item)."""
    ws_id = _require_ws_id(workspaceitem_id)
    if not confirm:
        return {
            "preview": {
                "action": "discard_workspace_item",
                "method": "DELETE",
                "endpoint": f"/submission/workspaceitems/{ws_id}",
                "workspaceitem_id": ws_id,
            },
            "confirm_required": True,
        }
    await client.mutate(
        "DELETE",
        f"/submission/workspaceitems/{ws_id}",
        where="discard workspace item",
    )
    return {"workspaceitem_id": ws_id, "discarded": True, "message": "Draft discarded."}


# --------------------------------------------------------------------------- #
# Item group (archived records)
# --------------------------------------------------------------------------- #
async def add_file_to_item(
    client: WriteClient,
    item: str,
    file_path: str | None = None,
    content_base64: str | None = None,
    name: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Attach a file to an existing archived item's ORIGINAL bundle."""
    config: WriteConfig = client.config
    uuid = require_uuid(item, "item")
    data, filename = read_file_input(config, file_path, content_base64)
    if name:
        filename = name

    record = await client.get(f"/core/items/{uuid}", {"embed": "owningCollection"})
    collection = _owning_collection(record, "owningCollection")
    require_collection_allowed(config, collection)

    if not confirm:
        return {
            "preview": {
                "action": "add_file_to_item",
                "method": "POST",
                "endpoint": "/core/bundles/{ORIGINAL}/bitstreams",
                "item": uuid,
                "collection": collection,
                "filename": filename,
                "size_bytes": len(data),
            },
            "confirm_required": True,
        }

    bundles, _ = await client.get_page(f"/core/items/{uuid}/bundles", key="bundles")
    original = next((b for b in bundles if b.get("name") == "ORIGINAL"), None)
    if original is not None:
        bundle_uuid = original.get("uuid") or original.get("id")
    else:
        created = await client.mutate(
            "POST",
            f"/core/items/{uuid}/bundles",
            json={"name": "ORIGINAL", "metadata": {}},
            headers=_JSON_CT,
            where="create ORIGINAL bundle",
        )
        bundle_uuid = created.get("uuid") or created.get("id")
    if not bundle_uuid:
        raise DSpaceError("Could not resolve the ORIGINAL bundle for this item.")

    result = await client.upload(
        f"/core/bundles/{bundle_uuid}/bitstreams",
        file_bytes=data,
        filename=filename,
        where="add file to item",
    )
    return {
        "item": uuid,
        "bundle": str(bundle_uuid),
        "bitstream": result.get("uuid") or result.get("id"),
        "filename": filename,
        "size_bytes": result.get("sizeBytes", len(data)),
        "message": "File added to the item.",
    }


async def update_item_metadata(
    client: WriteClient,
    item: str,
    metadata: dict[str, Any],
    confirm: bool = False,
) -> dict[str, Any]:
    """Set metadata on an archived item (deterministic remove+add, spec D4)."""
    config: WriteConfig = client.config
    uuid = require_uuid(item, "item")
    record = await client.get(f"/core/items/{uuid}", {"embed": "owningCollection"})
    collection = _owning_collection(record, "owningCollection")
    require_collection_allowed(config, collection)

    if not confirm:
        return {
            "preview": {
                "action": "update_item_metadata",
                "method": "PATCH",
                "endpoint": f"/core/items/{uuid}",
                "item": uuid,
                "collection": collection,
                "metadata": _metadata_summary(metadata),
            },
            "confirm_required": True,
        }

    ops = item_metadata_patch(record.get("metadata", {}), metadata or {})
    if not ops:
        return {
            "item": uuid,
            "updated_keys": [],
            "message": "No metadata changes were requested.",
        }
    await client.mutate(
        "PATCH",
        f"/core/items/{uuid}",
        json=ops,
        headers=_JSON_CT,
        where="update item metadata",
    )
    return {
        "item": uuid,
        "updated_keys": sorted(metadata or {}),
        "message": "Item metadata updated.",
    }


# --------------------------------------------------------------------------- #
# Structure group (NOT whitelist-checked, spec D7.2)
# --------------------------------------------------------------------------- #
async def create_collection(
    client: WriteClient,
    community: str,
    name: str,
    metadata: dict[str, Any] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a collection under a community. Not covered by the whitelist."""
    parent = require_uuid(community, "community")
    body_metadata = _normalize_metadata_map(metadata)
    body_metadata.setdefault("dc.title", [{"value": name}])
    body = {"name": name, "metadata": body_metadata}

    if not confirm:
        return {
            "preview": {
                "action": "create_collection",
                "method": "POST",
                "endpoint": f"/core/collections?parent={parent}",
                "community": parent,
                "name": name,
                "metadata": _metadata_summary(metadata),
            },
            "confirm_required": True,
        }

    created = await client.mutate(
        "POST",
        f"/core/collections?parent={parent}",
        json=body,
        headers=_JSON_CT,
        where="create collection",
    )
    return {
        "collection": created.get("uuid") or created.get("id"),
        "name": name,
        "community": parent,
        "self_href": _self_href(created),
        "message": "Collection created.",
    }


async def create_community(
    client: WriteClient,
    name: str,
    parent: str | None = None,
    metadata: dict[str, Any] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a community, optionally under ``parent``. Not whitelist-checked."""
    parent_uuid = require_uuid(parent, "parent") if parent else None
    query = f"?parent={parent_uuid}" if parent_uuid else ""
    body_metadata = _normalize_metadata_map(metadata)
    body_metadata.setdefault("dc.title", [{"value": name}])
    body = {"name": name, "metadata": body_metadata}

    if not confirm:
        return {
            "preview": {
                "action": "create_community",
                "method": "POST",
                "endpoint": f"/core/communities{query}",
                "name": name,
                "parent": parent_uuid,
                "metadata": _metadata_summary(metadata),
            },
            "confirm_required": True,
        }

    created = await client.mutate(
        "POST",
        f"/core/communities{query}",
        json=body,
        headers=_JSON_CT,
        where="create community",
    )
    return {
        "community": created.get("uuid") or created.get("id"),
        "name": name,
        "parent": parent_uuid,
        "self_href": _self_href(created),
        "message": "Community created.",
    }
