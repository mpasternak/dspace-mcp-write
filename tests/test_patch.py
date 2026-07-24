"""Tests for the pure JSON-Patch builders in ``patch.py``."""

from __future__ import annotations

import pytest

from dspace_mcp_write.patch import (
    item_metadata_patch,
    normalize_values,
    submission_patch,
)

# --- normalize_values ------------------------------------------------------


def test_normalize_string_value() -> None:
    assert normalize_values(["A title"]) == [{"value": "A title"}]


def test_normalize_dict_value_passthrough() -> None:
    value = {
        "value": "Kowalski, Jan",
        "language": "pl",
        "authority": "abc-123",
        "confidence": 600,
    }
    assert normalize_values([value]) == [value]


def test_normalize_mixed_values() -> None:
    result = normalize_values(["A", {"value": "B", "language": "en"}])
    assert result == [{"value": "A"}, {"value": "B", "language": "en"}]


def test_normalize_copies_input_dicts() -> None:
    original = {"value": "X"}
    result = normalize_values([original])
    result[0]["value"] = "Y"
    # The caller's dict must not be mutated or aliased into the result.
    assert original == {"value": "X"}


def test_normalize_rejects_unexpected_type() -> None:
    with pytest.raises(TypeError):
        normalize_values([123])  # type: ignore[list-item]


# --- submission_patch ------------------------------------------------------


def test_submission_patch_single_section() -> None:
    routed = {"publicationStep": {"dc.title": ["T"], "dc.type": ["Article"]}}
    ops = submission_patch(routed)
    assert ops == [
        {
            "op": "add",
            "path": "/sections/publicationStep/dc.title",
            "value": [{"value": "T"}],
        },
        {
            "op": "add",
            "path": "/sections/publicationStep/dc.type",
            "value": [{"value": "Article"}],
        },
    ]


def test_submission_patch_two_argument_form() -> None:
    # The pinned two-argument form: section names still come from the mapping.
    routed = {"publicationStep": {"dc.title": ["T"]}}
    ops = submission_patch("publicationStep", routed)
    assert ops == [
        {
            "op": "add",
            "path": "/sections/publicationStep/dc.title",
            "value": [{"value": "T"}],
        }
    ]


def test_submission_patch_multiple_sections() -> None:
    routed = {
        "page1": {"dc.title": ["T"]},
        "page2": {"dc.subject": ["S1", "S2"]},
    }
    ops = submission_patch(routed)
    paths = {op["path"] for op in ops}
    assert paths == {"/sections/page1/dc.title", "/sections/page2/dc.subject"}
    subject_op = next(o for o in ops if o["path"].endswith("dc.subject"))
    assert subject_op["value"] == [{"value": "S1"}, {"value": "S2"}]


def test_submission_patch_dict_value() -> None:
    routed = {
        "publicationStep": {
            "dc.contributor.author": [{"value": "Nowak, Anna", "language": "pl"}]
        }
    }
    ops = submission_patch(routed)
    assert ops == [
        {
            "op": "add",
            "path": "/sections/publicationStep/dc.contributor.author",
            "value": [{"value": "Nowak, Anna", "language": "pl"}],
        }
    ]


# --- item_metadata_patch ---------------------------------------------------


def test_item_patch_add_when_key_absent() -> None:
    ops = item_metadata_patch({}, {"dc.title": ["New"]})
    assert ops == [
        {"op": "add", "path": "/metadata/dc.title", "value": [{"value": "New"}]}
    ]


def test_item_patch_remove_then_add_when_key_present() -> None:
    current = {"dc.title": [{"value": "Old"}]}
    ops = item_metadata_patch(current, {"dc.title": ["New"]})
    assert ops == [
        {"op": "remove", "path": "/metadata/dc.title"},
        {"op": "add", "path": "/metadata/dc.title", "value": [{"value": "New"}]},
    ]


def test_item_patch_empty_list_removes_only_when_present() -> None:
    current = {"dc.description": [{"value": "x"}]}
    ops = item_metadata_patch(current, {"dc.description": []})
    assert ops == [{"op": "remove", "path": "/metadata/dc.description"}]


def test_item_patch_empty_list_absent_key_no_ops() -> None:
    ops = item_metadata_patch({}, {"dc.description": []})
    assert ops == []


def test_item_patch_untouched_keys_produce_no_ops() -> None:
    current = {
        "dc.title": [{"value": "Old"}],
        "dc.date.issued": [{"value": "2020"}],
    }
    ops = item_metadata_patch(current, {"dc.title": ["New"]})
    # Only dc.title is touched; dc.date.issued is left alone.
    assert all(op["path"] == "/metadata/dc.title" for op in ops)
    assert ops == [
        {"op": "remove", "path": "/metadata/dc.title"},
        {"op": "add", "path": "/metadata/dc.title", "value": [{"value": "New"}]},
    ]


def test_item_patch_dict_values() -> None:
    desired = {
        "dc.contributor.author": [
            {"value": "Kowalski, Jan", "authority": "a1", "confidence": 600}
        ]
    }
    ops = item_metadata_patch({}, desired)
    assert ops == [
        {
            "op": "add",
            "path": "/metadata/dc.contributor.author",
            "value": [{"value": "Kowalski, Jan", "authority": "a1", "confidence": 600}],
        }
    ]


def test_item_patch_multiple_keys_ordering() -> None:
    current = {"dc.title": [{"value": "Old"}]}
    desired = {
        "dc.title": ["New"],  # present -> remove + add
        "dc.subject": ["S"],  # absent -> add only
    }
    ops = item_metadata_patch(current, desired)
    assert ops == [
        {"op": "remove", "path": "/metadata/dc.title"},
        {"op": "add", "path": "/metadata/dc.title", "value": [{"value": "New"}]},
        {"op": "add", "path": "/metadata/dc.subject", "value": [{"value": "S"}]},
    ]
