"""Resolve immutable runtime resources in development and installed releases."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def resource_root() -> Path:
    configured = os.environ.get("TVT_RESOURCE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    source_root = Path(__file__).resolve().parents[1]
    if (source_root / "alembic.ini").is_file() and (source_root / "solution-packs").is_dir():
        return source_root
    return Path(sys.prefix) / "share" / "tvt"


RESOURCE_ROOT = resource_root()
