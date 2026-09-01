#!/usr/bin/env python3
"""Validate camera IDs and translate bundle requirements to node affinity."""

from __future__ import annotations

import re
from typing import Any


CAMERA_ID = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$")
LABEL_PREFIX = "cameras.apexfabric.com"


def validate_camera_id(camera_id: str) -> str:
    if not isinstance(camera_id, str) or not CAMERA_ID.fullmatch(camera_id):
        raise ValueError(f"invalid camera ID: {camera_id!r}; use a DNS-label-compatible ID")
    return camera_id


def label_key(camera_id: str) -> str:
    return f"{LABEL_PREFIX}/{validate_camera_id(camera_id)}"


def camera_affinity(camera_ids: list[str]) -> dict[str, Any]:
    cameras = sorted(set(validate_camera_id(item) for item in camera_ids))
    expressions = [{"key": label_key(item), "operator": "In", "values": ["true"]} for item in cameras]
    return {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [{"matchExpressions": expressions}]
            }
        }
    }
