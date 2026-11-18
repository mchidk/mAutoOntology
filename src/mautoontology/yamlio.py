"""Round-trip-safe YAML I/O.

Humans edit the ontology files, so reading and writing must never drop their
comments or reorder their keys. Everything goes through ruamel.yaml in
round-trip mode; generated content is converted to commented maps with a
canonical key order so that diffs stay small and reviewable.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

#: Canonical key order for generated elements (unknown keys keep insertion order after these).
KEY_ORDER: dict[str, list[str]] = {
    "file": ["schema", "kind", "id", "domain", "label", "description", "entity_type"],
    "entity": [
        "schema", "kind", "id", "label", "description", "domain", "entity_type", "source", "grain",
        "grain_description", "keys", "history", "default_filters", "segments", "attributes",
        "relationships", "caveats", "status", "origin", "owner", "reviewed_by", "reviewed_at",
        "rationale", "evidence",
    ],
    "attribute": [
        "id", "column", "expr", "label", "description", "role", "family", "semantic_type", "time",
        "values", "values_complete", "money", "scale", "aggregation", "additivity", "non_additive_over",
        "pii", "nullable", "synonyms", "caveats", "status", "origin", "reviewed_by", "reviewed_at",
        "rationale", "evidence",
    ],
    "relationship": [
        "id", "to", "role", "on", "cardinality", "optional", "through", "as_of", "condition",
        "description", "status", "origin", "reviewed_by", "reviewed_at", "rationale", "evidence",
    ],
    "metric": [
        "id", "label", "description", "type", "entity", "agg", "expr", "filter", "numerator",
        "denominator", "inputs", "time", "grains", "unit", "synonyms", "disambiguation", "status",
        "origin", "owner", "reviewed_by", "reviewed_at", "rationale", "evidence",
    ],
    "evidence": ["kind", "ref", "score", "detail"],
}


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def load_text(text: str) -> Any:
    return _yaml().load(text)


def load_file(path: str | Path) -> Any:
    return load_text(Path(path).read_text(encoding="utf-8"))


def dump_text(data: Any) -> str:
    buffer = io.StringIO()
    _yaml().dump(data, buffer)
    return buffer.getvalue()


def write_file(path: str | Path, data: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = dump_text(data)
    # Write bytes so Windows never converts LF to CRLF (keeps git diffs clean).
    target.write_bytes(text.encode("utf-8"))
    return target


def ordered(data: Any, kind: str | None = None) -> Any:
    """Convert plain dicts/lists into commented containers with canonical key order."""
    if isinstance(data, CommentedMap):
        return data
    if isinstance(data, dict):
        order = KEY_ORDER.get(kind or "", [])
        result = CommentedMap()
        keys = [k for k in order if k in data] + [k for k in data if k not in order]
        for key in keys:
            value = data[key]
            if value is None or value == [] or value == {}:
                continue
            result[key] = ordered(value, _child_kind(kind, key))
        return result
    if isinstance(data, (list, tuple)):
        seq = CommentedSeq()
        child = _list_kind(kind)
        for item in data:
            seq.append(ordered(item, child))
        if data and all(not isinstance(i, (dict, list)) for i in data) and len(data) <= 8:
            seq.fa.set_flow_style()
        return seq
    return data


def _child_kind(parent: str | None, key: str) -> str | None:
    if key == "attributes":
        return "attributes"
    if key == "relationships":
        return "relationships"
    if key == "metrics":
        return "metrics"
    if key == "evidence":
        return "evidences"
    return None


def _list_kind(kind: str | None) -> str | None:
    return {
        "attributes": "attribute",
        "relationships": "relationship",
        "metrics": "metric",
        "evidences": "evidence",
    }.get(kind or "")


def prune(data: Any) -> Any:
    """Drop None / empty values recursively (what ``ordered`` writes is what gets fingerprinted)."""
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            value = prune(value)
            if value is None or value == [] or value == {}:
                continue
            out[str(key)] = value
        return out
    if isinstance(data, (list, tuple)):
        return [prune(v) for v in data]
    return data


def plain(data: Any) -> Any:
    """Convert ruamel containers and scalar subclasses into plain built-in Python types."""
    if isinstance(data, dict):
        return {str(k): plain(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [plain(v) for v in data]
    if data is None or isinstance(data, bool):
        return data
    if isinstance(data, str):
        return str(data)  # drops ruamel ScalarString subclasses
    if isinstance(data, int):
        return int(data)
    if isinstance(data, float):
        return float(data)
    return data
