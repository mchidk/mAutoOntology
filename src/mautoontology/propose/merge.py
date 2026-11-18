"""Merge regenerated proposals into existing YAML without ever overwriting human work.

Each generated element (an entity's top-level fields, each attribute, each
relationship, each metric) is fingerprinted when written. The fingerprints are
committed in ``semantics/_evidence/generated.json``. On the next run an element
is refreshed only if its current content still matches the fingerprint, i.e.
nobody touched it. Elements a human edited, commented, approved, rejected or
deleted are left alone (a deleted entity file is not recreated either); if the generator now disagrees, the difference is reported as a
conflict in ``_proposals/`` for review.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..yamlio import load_file, ordered, plain, prune, write_file

FINAL_STATUSES = {"approved", "rejected", "deprecated"}
_CHILD_LISTS = {"attributes": "attribute", "relationships": "relationship"}


def fingerprint(element: Any) -> str:
    text = json.dumps(plain(element), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class MergeResult:
    path: Path
    created: bool = False
    updated: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    kept_human: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, str] = field(default_factory=dict)
    changed: bool = False


def _top_level(doc: dict[str, Any], list_keys: set[str]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in list_keys}


def merge_document(
    path: Path,
    new_doc: dict[str, Any],
    previous_state: dict[str, str],
    *,
    kind: str,
    list_keys: dict[str, str] | None = None,
) -> MergeResult:
    """Merge ``new_doc`` into the YAML file at ``path``.

    ``list_keys`` maps list fields holding id-keyed child elements to their
    element kind (``attributes`` -> ``attribute``); everything else is the
    document's top level, merged as one element.
    """
    list_keys = list_keys if list_keys is not None else _CHILD_LISTS
    result = MergeResult(path=path)
    new_plain = prune(plain(new_doc))
    if not path.exists():
        doc = ordered(new_plain, kind)
        write_file(path, doc)
        result.created = result.changed = True
        result.state = _state_for(new_plain, list_keys)
        return result

    existing = load_file(path)
    state: dict[str, str] = {}

    # --- top level -------------------------------------------------------
    new_top = _top_level(new_plain, set(list_keys))
    old_top = _top_level(plain(existing), set(list_keys))
    old_fp = fingerprint(old_top)
    new_fp = fingerprint(new_top)
    untouched = (previous_state.get("@top") == old_fp and old_top.get("status") not in FINAL_STATUSES
                 and not _own_comments(existing, set(list_keys)))
    if old_fp == new_fp:
        state["@top"] = new_fp
    elif untouched:
        rebuilt = ordered(new_top, kind)
        for key in list(existing.keys()):
            if key not in list_keys and key not in rebuilt:
                del existing[key]
        for key, value in rebuilt.items():
            existing[key] = value
        _reorder(existing, rebuilt, list_keys)
        state["@top"] = new_fp
        result.updated.append("@top")
        result.changed = True
    else:
        state["@top"] = previous_state.get("@top", "")
        result.kept_human.append("@top")
        _add_conflict(result, "@top", old_top, new_top)

    # --- id-keyed children -------------------------------------------------
    for list_key, element_kind in list_keys.items():
        new_items = {item["id"]: item for item in new_plain.get(list_key) or [] if isinstance(item, dict) and "id" in item}
        seq = existing.get(list_key)
        if seq is None and new_items:
            existing[list_key] = ordered([], None)
            seq = existing[list_key]
        positions = {item.get("id"): i for i, item in enumerate(seq or []) if isinstance(item, dict)}
        for item_id, new_item in new_items.items():
            key = f"{element_kind}:{item_id}"
            child_kind = element_kind
            if item_id in positions:
                current = plain(seq[positions[item_id]])
                cur_fp = fingerprint(current)
                untouched = (previous_state.get(key) == cur_fp and current.get("status") not in FINAL_STATUSES
                             and not has_comments(seq[positions[item_id]]))
                if cur_fp == fingerprint(new_item):
                    state[key] = cur_fp
                elif untouched:
                    seq[positions[item_id]] = ordered(new_item, child_kind)
                    state[key] = fingerprint(new_item)
                    result.updated.append(key)
                    result.changed = True
                else:
                    state[key] = previous_state.get(key, "")
                    result.kept_human.append(key)
                    _add_conflict(result, key, current, new_item)
            elif key in previous_state:
                state[key] = previous_state[key]  # tombstone: a human deleted it on purpose
                result.kept_human.append(key)
            else:
                seq.append(ordered(new_item, child_kind))
                state[key] = fingerprint(new_item)
                result.added.append(key)
                result.changed = True
        # Preserve state for children that exist only in the file (human-added or dropped upstream).
        for item_id in positions:
            key = f"{element_kind}:{item_id}"
            if key not in state and key in previous_state:
                state[key] = previous_state[key]
    for key, value in previous_state.items():
        state.setdefault(key, value)

    if result.changed:
        write_file(path, existing)
    result.state = state
    return result


def has_comments(node: Any) -> bool:
    """True when a ruamel node (or anything inside it) carries a YAML comment."""
    ca = getattr(node, "ca", None)
    if ca is not None:
        if any(part for part in (ca.comment or []) if part):
            return True
        if any(any(part for part in (entry or []) if part) for entry in (ca.items or {}).values()):
            return True
    if isinstance(node, dict):
        return any(has_comments(value) for value in node.values())
    if isinstance(node, list):
        return any(has_comments(value) for value in node)
    return False


def _own_comments(doc: Any, list_keys: set[str]) -> bool:
    """Comments on the document's top level (outside the id-keyed child lists)."""
    ca = getattr(doc, "ca", None)
    if ca is not None:
        if any(part for part in (ca.comment or []) if part):
            return True
        for key, entry in (ca.items or {}).items():
            if key not in list_keys and any(part for part in (entry or []) if part):
                return True
    return any(has_comments(value) for key, value in doc.items() if key not in list_keys)


def _add_conflict(result: MergeResult, element: str, current: dict[str, Any], proposed: dict[str, Any]) -> None:
    diff = _diff(current, proposed)
    if diff:
        result.conflicts.append({"element": element, "proposed": diff})


def _reorder(existing: Any, rebuilt: Any, list_keys: dict[str, str]) -> None:
    """Keep list fields after the top-level fields they followed (stable layout)."""
    order = list(rebuilt.keys())
    trailing = [k for k in existing.keys() if k not in order]
    for key in order + trailing:
        value = existing.pop(key)
        existing[key] = value


def _state_for(doc: dict[str, Any], list_keys: dict[str, str]) -> dict[str, str]:
    state = {"@top": fingerprint(_top_level(doc, set(list_keys)))}
    for list_key, element_kind in list_keys.items():
        for item in doc.get(list_key) or []:
            if isinstance(item, dict) and "id" in item:
                state[f"{element_kind}:{item['id']}"] = fingerprint(item)
    return state


#: Human-owned fields: governance decisions and prose never count as conflicts.
HUMAN_FIELDS = {
    "status", "reviewed_by", "reviewed_at", "owner", "rationale", "origin", "evidence", "label", "description",
    "synonyms", "caveats", "disambiguation", "grain_description", "segments",
}


def _diff(current: dict[str, Any], proposed: dict[str, Any]) -> dict[str, Any]:
    changed = {}
    for key in sorted(set(current) | set(proposed)):
        if key in HUMAN_FIELDS or key not in proposed:
            continue  # human-owned field, or a human addition the generator has no opinion on
        if current.get(key) != proposed.get(key):
            changed[key] = {"current": current.get(key), "proposed": proposed.get(key)}
    return changed
