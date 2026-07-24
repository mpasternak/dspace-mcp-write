"""Server assembly and the version-coupling import guard.

``dspace-mcp-write`` stands on the internal API of ``dspace-mcp`` (pinned
``>=0.1.1,<0.2``). The import test fails with a clear signal if that package
moves the symbols we reuse, instead of failing obscurely at runtime.
"""

from __future__ import annotations

from conftest import make_config
from dspace_mcp_write.server import READ_TOOLS, WRITE_TOOLS, build_server

EXPECTED_WRITE = {
    "get_submission_form",
    "create_workspace_item",
    "update_workspace_item_metadata",
    "upload_file_to_workspace_item",
    "deposit_workspace_item",
    "discard_workspace_item",
    "add_file_to_item",
    "update_item_metadata",
    "create_collection",
    "create_community",
}
EXPECTED_READ = {
    "search_items",
    "get_item",
    "list_communities",
    "list_collections",
    "list_bitstreams",
    "get_bitstream_text",
    "list_facet_values",
    "get_item_statistics",
    "get_repository_info",
}


def test_reused_dspace_mcp_api_exists():
    """Guard the exact symbols we depend on from the read-only package."""
    from dspace_mcp.client import DSpaceClient
    from dspace_mcp.config import normalize_base_url
    from dspace_mcp.server import READ_TOOLS as UPSTREAM_READ_TOOLS

    assert callable(DSpaceClient.build_http)
    assert callable(normalize_base_url)
    assert len(UPSTREAM_READ_TOOLS) >= 1


def test_write_tools_tuple_names():
    names = {fn.__name__ for fn in WRITE_TOOLS}
    assert names == EXPECTED_WRITE


async def test_build_server_registers_read_and_write_tools():
    server = build_server(make_config())
    registered = {tool.name for tool in await server.list_tools()}
    assert EXPECTED_READ <= registered
    assert EXPECTED_WRITE <= registered


def test_read_tools_reexported():
    names = {fn.__name__ for fn in READ_TOOLS}
    assert EXPECTED_READ <= names
