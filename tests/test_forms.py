"""Tests for submission-form detection and metadata routing (spec D5)."""

from __future__ import annotations

import pytest
from dspace_mcp.client import DSpaceError

from dspace_mcp_write.forms import (
    detect_form_sections,
    form_fields,
    required_fields,
    route_metadata,
)

# --- form definition fixtures (shape verified against demo.dspace.org) ------

PUBLICATION_STEP = {
    "id": "publicationStep",
    "rows": [
        {
            "fields": [
                {"mandatory": True, "selectableMetadata": [{"metadata": "dc.title"}]}
            ]
        },
        {
            "fields": [
                {
                    "mandatory": False,
                    "selectableMetadata": [{"metadata": "dc.contributor.author"}],
                }
            ]
        },
        {
            "fields": [
                {
                    "mandatory": True,
                    "selectableMetadata": [{"metadata": "dc.date.issued"}],
                },
                {
                    "mandatory": True,
                    "selectableMetadata": [{"metadata": "dc.type"}],
                },
            ]
        },
    ],
}

# A qualdrop field: one field with several selectable metadata.
IDENTIFIER_FORM = {
    "id": "identifiers",
    "rows": [
        {
            "fields": [
                {
                    "mandatory": False,
                    "selectableMetadata": [
                        {"metadata": "dc.identifier.issn"},
                        {"metadata": "dc.identifier.isbn"},
                        {"metadata": "dc.identifier.doi"},
                    ],
                }
            ]
        }
    ],
}

TRADITIONAL_PAGE_TWO = {
    "id": "traditionalpagetwo",
    "rows": [
        {
            "fields": [
                {"mandatory": False, "selectableMetadata": [{"metadata": "dc.subject"}]}
            ]
        },
        {
            "fields": [
                {
                    "mandatory": False,
                    "selectableMetadata": [{"metadata": "dc.description.abstract"}],
                }
            ]
        },
    ],
}


class FakeClient:
    """Minimal async client exposing ``get(path)`` like ``DSpaceClient``.

    ``responses`` maps a section id to either a form-JSON dict (-> 200) or an
    int HTTP status (-> raises ``DSpaceError`` with ``.status`` set, mirroring
    how the real client reports 4xx/5xx).
    """

    def __init__(self, responses: dict[str, dict | int]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def get(self, path: str) -> dict:
        self.calls.append(path)
        section_id = path.rsplit("/", 1)[-1]
        outcome = self._responses.get(section_id)
        if outcome is None:
            error = DSpaceError(f"no such form {section_id!r}")
            error.status = 404
            raise error
        if isinstance(outcome, int):
            error = DSpaceError(f"HTTP {outcome} for {path}")
            error.status = outcome
            raise error
        return outcome


# --- form_fields -----------------------------------------------------------


def test_form_fields_collects_all_selectable_metadata() -> None:
    fields = form_fields(IDENTIFIER_FORM)
    metas = [f["metadata"] for f in fields]
    # All three qualdrop entries survive - not just [0].
    assert metas == ["dc.identifier.issn", "dc.identifier.isbn", "dc.identifier.doi"]


def test_form_fields_reads_mandatory_flags() -> None:
    by_key = {f["metadata"]: f["mandatory"] for f in form_fields(PUBLICATION_STEP)}
    assert by_key["dc.title"] is True
    assert by_key["dc.type"] is True
    assert by_key["dc.contributor.author"] is False


def test_form_fields_empty_form() -> None:
    assert form_fields({}) == []
    assert form_fields({"rows": []}) == []


# --- detect_form_sections --------------------------------------------------


async def test_detect_form_sections_mixed_status() -> None:
    client = FakeClient(
        {
            "publicationStep": PUBLICATION_STEP,  # 200 -> form
            "license": 400,  # 400 -> not a form
            "upload": 404,  # 404 -> not a form
        }
    )
    sections = await detect_form_sections(
        client, ["license", "publicationStep", "upload"]
    )
    assert set(sections) == {"publicationStep"}
    assert sections["publicationStep"]  # has fields
    # Every requested section was probed.
    assert client.calls == [
        "/config/submissionforms/license",
        "/config/submissionforms/publicationStep",
        "/config/submissionforms/upload",
    ]


async def test_detect_form_sections_reraises_other_errors() -> None:
    client = FakeClient({"boom": 500})
    with pytest.raises(DSpaceError):
        await detect_form_sections(client, ["boom"])


async def test_detect_form_sections_preserves_order() -> None:
    client = FakeClient(
        {
            "publicationStep": PUBLICATION_STEP,
            "traditionalpagetwo": TRADITIONAL_PAGE_TWO,
        }
    )
    sections = await detect_form_sections(
        client, ["publicationStep", "traditionalpagetwo"]
    )
    assert list(sections) == ["publicationStep", "traditionalpagetwo"]


# --- route_metadata --------------------------------------------------------


def test_route_metadata_multi_selectable_key() -> None:
    sections = {"identifiers": form_fields(IDENTIFIER_FORM)}
    routed, unroutable = route_metadata(
        {"dc.identifier.isbn": ["978-0-00-000000-0"]}, sections
    )
    assert routed == {"identifiers": {"dc.identifier.isbn": ["978-0-00-000000-0"]}}
    assert unroutable == []


def test_route_metadata_multiple_sections() -> None:
    sections = {
        "publicationStep": form_fields(PUBLICATION_STEP),
        "traditionalpagetwo": form_fields(TRADITIONAL_PAGE_TWO),
    }
    metadata = {
        "dc.title": ["A title"],
        "dc.subject": ["Physics"],
        "dc.contributor.author": ["Kowalski, Jan"],
    }
    routed, unroutable = route_metadata(metadata, sections)
    assert routed["publicationStep"] == {
        "dc.title": ["A title"],
        "dc.contributor.author": ["Kowalski, Jan"],
    }
    assert routed["traditionalpagetwo"] == {"dc.subject": ["Physics"]}
    assert unroutable == []


def test_route_metadata_first_section_wins() -> None:
    # dc.title is declared by both; it must route to the first-detected section.
    sections = {
        "pageA": [{"metadata": "dc.title", "mandatory": True}],
        "pageB": [{"metadata": "dc.title", "mandatory": False}],
    }
    routed, unroutable = route_metadata({"dc.title": ["T"]}, sections)
    assert list(routed) == ["pageA"]
    assert unroutable == []


def test_route_metadata_unroutable_key() -> None:
    sections = {"publicationStep": form_fields(PUBLICATION_STEP)}
    routed, unroutable = route_metadata({"dc.unknownfield": ["x"]}, sections)
    assert routed == {}
    assert unroutable == ["dc.unknownfield"]


# --- required_fields -------------------------------------------------------


def test_required_fields() -> None:
    sections = {
        "publicationStep": form_fields(PUBLICATION_STEP),
        "traditionalpagetwo": form_fields(TRADITIONAL_PAGE_TWO),
    }
    required = required_fields(sections)
    assert set(required) == {"dc.title", "dc.date.issued", "dc.type"}
    assert "dc.contributor.author" not in required
    assert "dc.subject" not in required


def test_required_fields_dedup() -> None:
    sections = {
        "pageA": [{"metadata": "dc.title", "mandatory": True}],
        "pageB": [{"metadata": "dc.title", "mandatory": True}],
    }
    assert required_fields(sections) == ["dc.title"]


# --- end-to-end detect + route ---------------------------------------------


async def test_detect_then_route() -> None:
    client = FakeClient(
        {
            "license": 400,
            "publicationStep": PUBLICATION_STEP,
            "traditionalpagetwo": TRADITIONAL_PAGE_TWO,
            "upload": 404,
        }
    )
    sections = await detect_form_sections(
        client, ["license", "publicationStep", "upload", "traditionalpagetwo"]
    )
    routed, unroutable = route_metadata(
        {"dc.title": ["T"], "dc.description.abstract": ["Summary"]}, sections
    )
    assert routed == {
        "publicationStep": {"dc.title": ["T"]},
        "traditionalpagetwo": {"dc.description.abstract": ["Summary"]},
    }
    assert unroutable == []
