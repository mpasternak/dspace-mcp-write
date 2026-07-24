"""Pure JSON-Patch builders for DSpace metadata (no I/O, stdlib only).

Metadata is expressed as a flat DC dictionary, symmetric with how ``dspace-mcp``
returns it on read::

    {"dc.title": ["A title"],
     "dc.contributor.author": [{"value": "Kowalski, Jan", "language": "pl"}]}

Each value is either a plain string (shorthand for ``{"value": s}``) or a full
object ``{"value", "language"?, "authority"?, "confidence"?}``. These functions
translate such a dictionary into JSON-Patch operations. They are deliberately
side-effect free so they can be unit-tested exhaustively.

Two distinct metadata paths (see spec D4) - do not mix them:

* **Submission (workspaceitem):** operations target form sections,
  ``/sections/<section>/<key>`` - see :func:`submission_patch`.
* **Archived item:** operations target the object's metadata map,
  ``/metadata/<key>`` - see :func:`item_metadata_patch`.
"""

from __future__ import annotations

MetadataValue = str | dict
MetadataDict = dict[str, list[MetadataValue]]


def normalize_values(values: list[MetadataValue]) -> list[dict]:
    """Normalise a list of metadata values to a list of DSpace value objects.

    A plain string ``s`` becomes ``{"value": s}``. A dict is passed through as a
    shallow copy (so the caller's object is never mutated or aliased into the
    result). Any other type is a programming error and raises ``TypeError``.
    """
    normalized: list[dict] = []
    for value in values:
        if isinstance(value, str):
            normalized.append({"value": value})
        elif isinstance(value, dict):
            normalized.append(dict(value))
        else:
            raise TypeError(
                "Metadata value must be a string or a dict, got "
                f"{type(value).__name__!r}."
            )
    return normalized


def submission_patch(
    section: str | dict[str, MetadataDict],
    metadata_by_section: dict[str, MetadataDict] | None = None,
) -> list[dict]:
    """Build JSON-Patch ``add`` ops for a submission workspaceitem.

    Given a routed mapping ``{section_id: {key: [values]}}`` it emits one
    ``add`` op per (section, key) at ``/sections/<section>/<key>`` with the
    normalised value list.

    The contract pins a leading ``section`` parameter; section names actually
    come from the mapping keys, so both call styles are accepted::

        submission_patch(routed)                 # single positional mapping
        submission_patch(section_id, routed)     # pinned two-argument form

    In the two-argument form the explicit ``section_id`` is informational and
    the mapping is authoritative.
    """
    routed = section if metadata_by_section is None else metadata_by_section
    ops: list[dict] = []
    for section_id, metadata in routed.items():
        for key, values in metadata.items():
            ops.append(
                {
                    "op": "add",
                    "path": f"/sections/{section_id}/{key}",
                    "value": normalize_values(values),
                }
            )
    return ops


def item_metadata_patch(current: dict, desired: MetadataDict) -> list[dict]:
    """Build a deterministic JSON-Patch for an archived item's ``/metadata``.

    ``current`` is the item's existing metadata map (``item["metadata"]``).
    ``desired`` maps each key to the *complete* list of values it should hold.

    Per spec D4, for each key in ``desired`` (index-free ``replace`` on an array
    is unreliable, so we remove-then-add):

    * if the key is present in ``current`` -> emit ``remove /metadata/<key>``;
    * then, if the normalised values are non-empty -> emit
      ``add /metadata/<key>`` with the whole value list (list order defines
      ``place``).

    An empty value list therefore removes the field (remove only). Keys not in
    ``desired`` are left untouched (no ops). A key that is both absent from
    ``current`` and given an empty list produces no ops at all.
    """
    ops: list[dict] = []
    for key, values in desired.items():
        if key in current:
            ops.append({"op": "remove", "path": f"/metadata/{key}"})
        normalized = normalize_values(values)
        if normalized:
            ops.append({"op": "add", "path": f"/metadata/{key}", "value": normalized})
    return ops
