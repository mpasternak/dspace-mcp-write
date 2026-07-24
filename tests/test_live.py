"""Live round-trip against demo.dspace.org (deselected by default).

Runs only with ``-m live`` and real credentials in the environment:
``DSPACE_TEST_USER`` / ``DSPACE_TEST_PASSWORD``. The demo instance is a public,
periodically-reset sandbox, so assertions are loose (existence, not counts) and
every draft is discarded in a ``finally`` — the draft id is surfaced even on
partial failure so cleanup always has something to delete.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from dspace_mcp_write import tools
from dspace_mcp_write.client import WriteClient
from dspace_mcp_write.config import WriteConfig

pytestmark = pytest.mark.live

BASE_URL = "https://demo.dspace.org/server"
FIXTURE = Path(__file__).parent / "fixtures" / "tiny.pdf"


def _config() -> WriteConfig:
    user = os.environ.get("DSPACE_TEST_USER")
    password = os.environ.get("DSPACE_TEST_PASSWORD")
    if not user or not password:
        pytest.skip("set DSPACE_TEST_USER and DSPACE_TEST_PASSWORD to run live tests")
    return WriteConfig(base_url=BASE_URL, username=user, password=password)


async def test_round_trip_create_upload_discard():
    config = _config()
    http = WriteClient.build_http(config)
    client = WriteClient(config, http)
    workspaceitem_id: str | None = None
    async with http:
        await client.login()

        collections, _ = await client.get_page("/core/collections", key="collections")
        if not collections:
            pytest.skip("no collections available on the demo instance")
        collection = collections[0].get("uuid") or collections[0].get("id")

        try:
            created = await tools.create_workspace_item(
                client,
                collection,
                {"dc.title": ["dspace-mcp-write live upload test"]},
                confirm=True,
            )
            workspaceitem_id = created.get("workspaceitem_id")
            assert workspaceitem_id is not None

            payload = base64.b64encode(FIXTURE.read_bytes()).decode()
            uploaded = await tools.upload_file_to_workspace_item(
                client,
                str(workspaceitem_id),
                content_base64=payload,
                name="tiny.pdf",
                confirm=True,
            )
            assert uploaded.get("bitstream")
        finally:
            if workspaceitem_id is not None:
                await tools.discard_workspace_item(
                    client, str(workspaceitem_id), confirm=True
                )
