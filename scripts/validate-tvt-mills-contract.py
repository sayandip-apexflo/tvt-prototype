#!/usr/bin/env python3
"""Validate the checked-in tvt-mills-pilot canonical CV contract.

Supersedes validate-tvt-identity-contract.py, which validated the old
surveillance-edge-runtime pack extended in place by the (now retired)
docs/contracts/tvt-identity-v1/ draft. This validates the single combined
pack that replaced it -- see docs/contracts/tvt-mills-v1/README.md.
"""

import json
import math
from pathlib import Path

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "solution-packs" / "catalog" / "tvt-mills-pilot-2026.09.18-v1"

NORM_TOLERANCE = 1e-3


def _orientation(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _crosses(a, b, c, d):
    return _orientation(a, b, c) * _orientation(a, b, d) < 0 and _orientation(c, d, a) * _orientation(c, d, b) < 0


def validate_polygon(points) -> None:
    count = len(points)
    for index in range(count):
        a, b = points[index], points[(index + 1) % count]
        for other in range(index + 1, count):
            c, d = points[other], points[(other + 1) % count]
            if {index, (index + 1) % count} & {other, (other + 1) % count}:
                continue
            if _crosses(a, b, c, d):
                raise ValueError("zone must not self-intersect")
    area = abs(sum(
        points[index][0] * points[(index + 1) % count][1]
        - points[(index + 1) % count][0] * points[index][1]
        for index in range(count)
    )) / 2
    if area == 0:
        raise ValueError("zone must have non-zero area")


def validate_line(line: dict) -> None:
    (x1, y1), (x2, y2) = line["points"]
    if x1 == x2 and y1 == y2:
        raise ValueError(f"line {line['id']!r} must have non-zero length")
    if not line["id"].endswith(("_entry", "_exit")):
        raise ValueError(f"line id {line['id']!r} must end in _entry or _exit (two-lines-per-gate direction convention)")


def validate_embedding(vector: list) -> None:
    norm = math.sqrt(sum(component * component for component in vector))
    if abs(norm - 1.0) > NORM_TOLERANCE:
        raise ValueError(f"embedding must be L2-normalized (unit norm); got norm={norm:.4f}")


def validate() -> None:
    event_schema = json.loads((PACK / "analytics-event.schema.json").read_text())
    desired_schema = json.loads((PACK / "desired-state.schema.json").read_text())
    events = json.loads((PACK / "analytics-event.examples.json").read_text())
    desired = json.loads((PACK / "desired-state.example.json").read_text())

    jsonschema.Draft202012Validator.check_schema(event_schema)
    jsonschema.Draft202012Validator.check_schema(desired_schema)
    format_checker = jsonschema.FormatChecker()
    jsonschema.Draft202012Validator(desired_schema, format_checker=format_checker).validate(desired)

    for camera in desired["cameras"]:
        config = camera.get("config", {})
        for zones in config.get("zones", {}).values():
            for zone in zones:
                validate_polygon(zone["poly"])
        for line in config.get("lines", []):
            validate_line(line)

    event_validator = jsonschema.Draft202012Validator(event_schema, format_checker=format_checker)
    seen_dims: set[tuple[str, int]] = set()
    for event in events:
        event_validator.validate(event)
        payload = event["payload"]
        if payload["snapshot_url"].startswith("/snapshots/snapshots/"):
            raise ValueError("duplicated snapshot route prefix")
        subject = payload.get("subject")
        if subject:
            x1, y1, x2, y2 = subject["bbox"]
            if not x1 < x2 or not y1 < y2:
                raise ValueError("invalid bounding box ordering")
        embeddings = payload.get("embeddings")
        if embeddings:
            if "body" in embeddings:
                raise ValueError(
                    "tvt-mills-pilot v1 never emits payload.embeddings.body -- "
                    "see docs/contracts/tvt-mills-v1/README.md"
                )
            for kind, vector in embeddings.items():
                validate_embedding(vector)
                seen_dims.add((kind, len(vector)))

    dims_by_kind: dict[str, set[int]] = {}
    for kind, dim in seen_dims:
        dims_by_kind.setdefault(kind, set()).add(dim)
    for kind, dims in dims_by_kind.items():
        if len(dims) > 1:
            raise ValueError(f"inconsistent '{kind}' embedding dimension across examples: {sorted(dims)}")


if __name__ == "__main__":
    validate()
    print("tvt-mills-pilot contract examples are valid")
