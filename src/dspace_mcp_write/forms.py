"""Submission-form detection and field routing (spec D5).

Section names and required fields come from ``submission-forms.xml`` /
``item-submission.xml`` and differ between collections, so we must *discover*
them rather than hardcode. The reliable signal (verified live) is
``GET /config/submissionforms/{section-id}``:

* 200 -> the section is a submission form; its fields expose DC metadata keys;
* 400/404 -> the section is not a form (e.g. ``license``, ``upload``,
  ``collection``).

Everything here is pure except :func:`detect_form_sections`, whose only I/O is
that config GET. Routing maps each requested metadata key to the first form
section that declares it.
"""

from __future__ import annotations

from dspace_mcp.client import DSpaceError  # imported ONLY for the except below

MetadataValue = str | dict
MetadataDict = dict[str, list[MetadataValue]]


def form_fields(form_json: dict) -> list[dict]:
    """Flatten a submission-form definition into ``[{"metadata", "mandatory"}]``.

    Collects **every** ``rows[].fields[].selectableMetadata[].metadata`` entry,
    not just ``[0]``: qualdrop fields (e.g. ``dc.identifier.*``) declare several
    selectable metadata, and taking only the first would drop
    ``dc.identifier.isbn`` and friends. The ``mandatory`` flag is read per field
    and applied to each of its selectable metadata keys.
    """
    fields: list[dict] = []
    for row in form_json.get("rows", []):
        for field in row.get("fields", []):
            mandatory = bool(field.get("mandatory", False))
            for selectable in field.get("selectableMetadata", []):
                metadata = selectable.get("metadata")
                if metadata:
                    fields.append({"metadata": metadata, "mandatory": mandatory})
    return fields


async def detect_form_sections(client, section_ids: list[str]) -> dict[str, list[dict]]:
    """Return ``{section_id: [fields...]}`` for the section ids that are forms.

    For each id, ``GET /config/submissionforms/{id}``: a 200 body is flattened
    with :func:`form_fields`; a 400/404 means "not a submission form" and is
    skipped. ``client.get`` raises :class:`DSpaceError` on 4xx.
    """
    sections: dict[str, list[dict]] = {}
    for section_id in section_ids:
        try:
            form_json = await client.get(f"/config/submissionforms/{section_id}")
        except DSpaceError as exc:
            # 400/404 from submissionforms means this id is not a form section
            # (license/upload/collection); 7.x may answer either code. Any other
            # status is a real error and must propagate - never swallow it.
            if exc.status in (400, 404):
                continue
            raise
        sections[section_id] = form_fields(form_json)
    return sections


def route_metadata(
    metadata: MetadataDict, sections: dict[str, list[dict]]
) -> tuple[dict[str, MetadataDict], list[str]]:
    """Route each metadata key to the first form section that declares it.

    Returns ``(routed, unroutable_keys)`` where ``routed`` is
    ``{section_id: {key: values}}``. "First" follows the insertion order of
    ``sections`` (i.e. the order sections were detected). A key that no section
    declares is collected into ``unroutable_keys`` so the caller can raise a
    clear error instead of provoking a blind 422.
    """
    routed: dict[str, MetadataDict] = {}
    unroutable: list[str] = []
    for key, values in metadata.items():
        target: str | None = None
        for section_id, fields in sections.items():
            if any(field["metadata"] == key for field in fields):
                target = section_id
                break
        if target is None:
            unroutable.append(key)
        else:
            routed.setdefault(target, {})[key] = values
    return routed, unroutable


def required_fields(sections: dict[str, list[dict]]) -> list[str]:
    """Return the mandatory metadata keys across all form sections (deduped)."""
    required: list[str] = []
    for fields in sections.values():
        for field in fields:
            if field["mandatory"] and field["metadata"] not in required:
                required.append(field["metadata"])
    return required
