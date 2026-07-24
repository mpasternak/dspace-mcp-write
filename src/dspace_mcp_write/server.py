"""MCP adapter: a shared authenticated client plus tool registration.

All logic lives in :mod:`dspace_mcp_write.tools`; here we keep only the tool
descriptions the model reads when choosing a tool, and the reduction of
exceptions to a terse ``{"error": ...}``.

This server is a superset of the read-only ``dspace-mcp``: it registers that
package's :data:`READ_TOOLS` one-to-one and adds the write layer. Every mutating
tool takes ``confirm``; with ``confirm=False`` it returns a preview and performs
no write (design spec D7).
"""

from __future__ import annotations

import functools
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from dspace_mcp.client import DSpaceError
from dspace_mcp.pdf import PdfError
from dspace_mcp.server import READ_TOOLS
from mcp.server.fastmcp import Context, FastMCP

from . import tools
from .client import WriteClient
from .config import WriteConfig, parse_args


@dataclass
class AppContext:
    """Lifespan context — one authenticated client for the whole process."""

    client: WriteClient


def _client(ctx: Context) -> WriteClient:
    return ctx.request_context.lifespan_context.client


def _guard(fn: Callable) -> Callable:
    """Turn model-facing exceptions into an ordinary response.

    A model that receives a stack trace retries forever; a model that receives
    an English sentence changes the request or asks the user.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except (DSpaceError, PdfError) as exc:
            return {"error": str(exc)}

    return wrapper


@_guard
async def get_submission_form(ctx: Context, collection: str) -> dict[str, Any]:
    """Show which metadata fields a collection's submission form accepts.

    Call this before create_workspace_item to learn the field names and which
    ones are required, because they differ between collections.

    Args:
        collection: UUID of the collection you intend to deposit into.
    """
    return await tools.get_submission_form(_client(ctx), collection)


@_guard
async def create_workspace_item(
    ctx: Context,
    collection: str,
    metadata: dict[str, Any],
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a draft record (workspace item) and fill in its metadata.

    This does not publish anything: it leaves a draft you can add files to and
    later deposit. Metadata is a flat map like
    {"dc.title": ["A title"], "dc.contributor.author": ["Doe, Jane"]}.
    With confirm=False you get a preview and nothing is written.

    Args:
        collection: UUID of the target collection (must be write-allowed).
        metadata: flat DC map; values are strings or objects with a "value" key.
        confirm: set true to actually create the draft.
    """
    return await tools.create_workspace_item(
        _client(ctx), collection, metadata, confirm
    )


@_guard
async def update_workspace_item_metadata(
    ctx: Context,
    workspaceitem_id: str,
    metadata: dict[str, Any],
    confirm: bool = False,
) -> dict[str, Any]:
    """Add or fix metadata on an existing draft (workspace item).

    Use this to repair a draft whose metadata was rejected on creation. With
    confirm=False you get a preview and nothing is written.

    Args:
        workspaceitem_id: integer id of the draft.
        metadata: flat DC map of fields to set.
        confirm: set true to actually apply the metadata.
    """
    return await tools.update_workspace_item_metadata(
        _client(ctx), workspaceitem_id, metadata, confirm
    )


@_guard
async def upload_file_to_workspace_item(
    ctx: Context,
    workspaceitem_id: str,
    file_path: str | None = None,
    content_base64: str | None = None,
    name: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Upload a file into a draft (workspace item).

    Provide exactly one of file_path (a path on the server machine) or
    content_base64 (small inline content). With confirm=False you get a preview
    (filename and size) and nothing is uploaded.

    Args:
        workspaceitem_id: integer id of the draft.
        file_path: path to a local file to upload.
        content_base64: base64-encoded file content, as an alternative.
        name: filename to record; required to name inline content sensibly.
        confirm: set true to actually upload.
    """
    return await tools.upload_file_to_workspace_item(
        _client(ctx), workspaceitem_id, file_path, content_base64, name, confirm
    )


@_guard
async def deposit_workspace_item(
    ctx: Context,
    workspaceitem_id: str,
    grant_license: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Publish a draft: accept the deposit license and submit it.

    This is the only tool that publishes. grant_license is legally meaningful:
    setting it true accepts the repository's deposit license on behalf of the
    configured account. If the collection requires a license and grant_license
    is false, nothing is published. With confirm=False you get a preview.

    Args:
        workspaceitem_id: integer id of the draft to publish.
        grant_license: set true to accept the deposit license.
        confirm: set true to actually submit for publication.
    """
    return await tools.deposit_workspace_item(
        _client(ctx), workspaceitem_id, grant_license, confirm
    )


@_guard
async def discard_workspace_item(
    ctx: Context, workspaceitem_id: str, confirm: bool = False
) -> dict[str, Any]:
    """Delete one of your own drafts (workspace items).

    This only removes unpublished drafts, never an archived record. With
    confirm=False you get a preview and nothing is deleted.

    Args:
        workspaceitem_id: integer id of the draft to discard.
        confirm: set true to actually delete the draft.
    """
    return await tools.discard_workspace_item(_client(ctx), workspaceitem_id, confirm)


@_guard
async def add_file_to_item(
    ctx: Context,
    item: str,
    file_path: str | None = None,
    content_base64: str | None = None,
    name: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Attach a file to an existing, already-archived item.

    Adds the file to the item's ORIGINAL bundle (creating it if needed). Provide
    exactly one of file_path or content_base64. With confirm=False you get a
    preview and nothing is uploaded.

    Args:
        item: UUID of the archived item.
        file_path: path to a local file to upload.
        content_base64: base64-encoded file content, as an alternative.
        name: filename to record for inline content.
        confirm: set true to actually upload.
    """
    return await tools.add_file_to_item(
        _client(ctx), item, file_path, content_base64, name, confirm
    )


@_guard
async def update_item_metadata(
    ctx: Context,
    item: str,
    metadata: dict[str, Any],
    confirm: bool = False,
) -> dict[str, Any]:
    """Set metadata on an existing, already-archived item.

    For each key you supply, the item's whole list of values for that key is
    replaced; keys you do not mention are left untouched; an empty list removes
    a field. With confirm=False you get a preview and nothing is written.

    Args:
        item: UUID of the archived item.
        metadata: flat DC map of fields to set (each key replaces that field).
        confirm: set true to actually apply the changes.
    """
    return await tools.update_item_metadata(_client(ctx), item, metadata, confirm)


@_guard
async def create_collection(
    ctx: Context,
    community: str,
    name: str,
    metadata: dict[str, Any] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a collection inside a community.

    Note: structure tools are not limited by the collection whitelist, which
    only restricts deposits and file uploads. With confirm=False you get a
    preview and nothing is created.

    Args:
        community: UUID of the parent community.
        name: display name of the new collection.
        metadata: optional extra DC metadata for the collection.
        confirm: set true to actually create the collection.
    """
    return await tools.create_collection(
        _client(ctx), community, name, metadata, confirm
    )


@_guard
async def create_community(
    ctx: Context,
    name: str,
    parent: str | None = None,
    metadata: dict[str, Any] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a community, optionally nested under a parent community.

    Note: structure tools are not limited by the collection whitelist. With
    confirm=False you get a preview and nothing is created.

    Args:
        name: display name of the new community.
        parent: UUID of a parent community; omit for a top-level community.
        metadata: optional extra DC metadata for the community.
        confirm: set true to actually create the community.
    """
    return await tools.create_community(_client(ctx), name, parent, metadata, confirm)


WRITE_TOOLS = (
    get_submission_form,
    create_workspace_item,
    update_workspace_item_metadata,
    upload_file_to_workspace_item,
    deposit_workspace_item,
    discard_workspace_item,
    add_file_to_item,
    update_item_metadata,
    create_collection,
    create_community,
)


def build_server(config: WriteConfig) -> FastMCP:
    """Assemble the MCP server: read tools plus the authenticated write tools."""

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[AppContext]:
        http = WriteClient.build_http(config)
        client = WriteClient(config, http)
        async with http:
            # Probe first (non-fatal): it corrects a missing "/server" in the
            # base URL, so the single most common config mistake isn't
            # misdiagnosed by login() as bad credentials. A blocked /api root
            # is not fatal — login still works — so we only warn.
            try:
                await client.probe()
            except DSpaceError as exc:
                print(
                    f"dspace-mcp-write: startup probe failed: {exc}",
                    file=sys.stderr,
                )
            # Fail fast: without a working login the write tools are useless,
            # so we surface the problem at startup instead of on first call.
            try:
                await client.login()
            except DSpaceError as exc:
                print(
                    f"dspace-mcp-write: login failed: {exc}",
                    file=sys.stderr,
                )
                raise
            yield AppContext(client=client)

    mcp = FastMCP("dspace-mcp-write", lifespan=lifespan)
    for fn in READ_TOOLS:
        mcp.tool()(fn)
    for fn in WRITE_TOOLS:
        mcp.tool()(fn)
    return mcp


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (``dspace-mcp-write``)."""
    try:
        config = parse_args(argv)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    build_server(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
