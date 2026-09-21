"""ANPR zone / entry-exit line validation and compilation to vendor config.

Ports the reference shape-quality checks from
scripts/validate-tvt-mills-contract.py (which validates the checked-in
example against the same rules) so the management API rejects bad geometry
before it ever reaches a deployment preview, and compiles stored
CameraGeometryShape rows into the exact `config.zones.anpr[]`/`config.lines[]`
shape the vendor pack's desired-state.schema.json requires -- see
docs/contracts/tvt-mills-v1/README.md for the two-lines-per-gate direction
convention (one line per camera, one role each; direction is derived from
which side of the line faces the plant interior).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

VENDOR_ID = re.compile(r"^[A-Za-z0-9._-]+$")
NORM_POINT_RANGE = (0.0, 1.0)


@dataclass(frozen=True)
class ShapeInput:
    """Minimal read view over a CameraGeometryShape row (or an unsaved draft)
    needed for validation and compilation."""

    kind: str  # 'zone' | 'line'
    shape_key: str
    name: str
    points: Sequence[Sequence[float]]
    role_key: str | None = None
    direction: str | None = None  # 'entry' | 'exit'
    inside_side: str | None = None  # 'a' | 'b'
    enabled: bool = True


def _orientation(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _crosses(a, b, c, d) -> bool:
    return (
        _orientation(a, b, c) * _orientation(a, b, d) < 0
        and _orientation(c, d, a) * _orientation(c, d, b) < 0
    )


def _validate_points(points: Sequence[Sequence[float]]) -> None:
    low, high = NORM_POINT_RANGE
    for point in points:
        if len(point) != 2:
            raise ValueError("each point must be an [x, y] pair")
        x, y = point
        if not (low <= x <= high and low <= y <= high):
            raise ValueError("points must be normalized to the 0-1 range")


def validate_polygon(points: Sequence[Sequence[float]]) -> None:
    _validate_points(points)
    if len(points) < 3:
        raise ValueError("zone must have at least 3 points")
    count = len(points)
    for index in range(count):
        a, b = points[index], points[(index + 1) % count]
        for other in range(index + 1, count):
            c, d = points[other], points[(other + 1) % count]
            if {index, (index + 1) % count} & {other, (other + 1) % count}:
                continue
            if _crosses(a, b, c, d):
                raise ValueError("zone must not self-intersect")
    area = (
        abs(
            sum(
                points[index][0] * points[(index + 1) % count][1]
                - points[(index + 1) % count][0] * points[index][1]
                for index in range(count)
            )
        )
        / 2
    )
    if area == 0:
        raise ValueError("zone must have non-zero area")


def validate_line(points: Sequence[Sequence[float]]) -> None:
    _validate_points(points)
    if len(points) != 2:
        raise ValueError("line must have exactly 2 points")
    (x1, y1), (x2, y2) = points
    if x1 == x2 and y1 == y2:
        raise ValueError("line must have non-zero length")


def validate_shape_key(shape_key: str, *, kind: str) -> None:
    if not VENDOR_ID.fullmatch(shape_key):
        raise ValueError("shape id must match ^[A-Za-z0-9._-]+$")
    if kind == "line" and not shape_key.endswith(("_entry", "_exit")):
        raise ValueError(
            "line id must end in _entry or _exit (two-lines-per-gate direction convention)"
        )


def slugify(name: str) -> str:
    """Best-effort vendor-id-safe slug for an operator-supplied zone name.
    Callers are responsible for de-duplicating against existing shape_keys."""

    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip().lower()).strip("-")
    return slug or "zone"


def line_shape_key(role_key: str, direction: str) -> str:
    if not VENDOR_ID.fullmatch(role_key):
        raise ValueError("role_key must match ^[A-Za-z0-9._-]+$")
    if direction not in {"entry", "exit"}:
        raise ValueError("direction must be 'entry' or 'exit'")
    return f"{role_key}_{direction}"


def line_accepted_direction(*, direction: str, inside_side: str) -> str:
    """Which physical crossing direction ('A->B'/'B->A') this line's role
    corresponds to, given which endpoint faces the plant interior.

    entry = travel toward the inside; exit = travel toward the outside.
    """

    if direction not in {"entry", "exit"}:
        raise ValueError("direction must be 'entry' or 'exit'")
    if inside_side not in {"a", "b"}:
        raise ValueError("inside_side must be 'a' or 'b'")
    a_to_b_is_toward_inside = inside_side == "b"
    travels_toward_inside = direction == "entry"
    return "A->B" if a_to_b_is_toward_inside == travels_toward_inside else "B->A"


def validate_shape(shape: ShapeInput) -> None:
    if shape.kind not in {"zone", "line"}:
        raise ValueError("kind must be 'zone' or 'line'")
    validate_shape_key(shape.shape_key, kind=shape.kind)
    if shape.kind == "zone":
        validate_polygon(shape.points)
        if shape.role_key or shape.direction or shape.inside_side:
            raise ValueError("a zone carries no role, direction, or inside_side")
    else:
        validate_line(shape.points)
        if not shape.role_key:
            raise ValueError("a line requires role_key")
        if shape.direction not in {"entry", "exit"}:
            raise ValueError("a line requires direction 'entry' or 'exit'")
        if shape.inside_side not in {"a", "b"}:
            raise ValueError("a line requires inside_side 'a' or 'b'")
        expected_key = line_shape_key(shape.role_key, shape.direction)
        if shape.shape_key != expected_key:
            raise ValueError(f"line id must be {expected_key!r} for this role/direction")


def compile_camera_config(shapes: Iterable[ShapeInput]) -> dict[str, Any]:
    """Build the vendor pack's `config` object (config.zones.anpr[] /
    config.lines[]) from a camera's stored geometry shapes. Disabled shapes
    are omitted. Raises ValueError if any enabled shape fails validation."""

    zones: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for shape in shapes:
        if not shape.enabled:
            continue
        validate_shape(shape)
        if shape.kind == "zone":
            zones.append(
                {
                    "id": shape.shape_key,
                    "name": shape.name,
                    "poly": [[float(x), float(y)] for x, y in shape.points],
                }
            )
        else:
            accepted = line_accepted_direction(
                direction=shape.direction, inside_side=shape.inside_side
            )
            lines.append(
                {
                    "id": shape.shape_key,
                    "name": shape.name,
                    "points": [[float(x), float(y)] for x, y in shape.points],
                    "accepted": [accepted],
                }
            )
    config: dict[str, Any] = {}
    if zones:
        config["zones"] = {"anpr": zones}
    if lines:
        config["lines"] = lines
    return config
