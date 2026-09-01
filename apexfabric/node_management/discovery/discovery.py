#!/usr/bin/env python3
"""Minimal, GStreamer-free capability discovery for the single TVT node."""

from __future__ import annotations

import glob
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _device_nodes(pattern: str) -> list[str]:
    return sorted(path for path in glob.glob(pattern) if Path(path).exists())


def _module_loaded(*names: str) -> bool:
    return any((Path("/sys/module") / name).exists() for name in names)


def _va_api_available() -> tuple[bool, str | None]:
    executable = shutil.which("vainfo")
    if executable is None:
        return False, None
    try:
        result = subprocess.run(
            [executable],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "LIBVA_DISPLAY": os.getenv("LIBVA_DISPLAY", "drm")},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    output = (result.stdout + result.stderr).strip()
    version = next(
        (line.strip() for line in output.splitlines() if "VA-API version" in line),
        None,
    )
    return result.returncode == 0, version


def discover() -> dict[str, Any]:
    """Return only the capabilities required by node qualification.

    Video decode is checked through VA-API. No GStreamer binaries, plugins, or
    gateway process are required by this reporter.
    """

    gpu_nodes = _device_nodes("/dev/dri/renderD*")
    npu_nodes = _device_nodes("/dev/accel/accel*")
    va_available, va_version = _va_api_available()
    return {
        "schema_version": "tvt-1.0.0",
        "hardware": {
            "hostname": platform.node(),
            "cpu": {
                "architecture": platform.machine(),
                "logical_processors": os.cpu_count() or 1,
            },
        },
        "accelerators": {
            "gpu": {
                "present": bool(gpu_nodes),
                "device_nodes": gpu_nodes,
                "driver": {"loaded": _module_loaded("i915", "xe")},
            },
            "npu": {
                "present": bool(npu_nodes),
                "device_nodes": npu_nodes,
                "driver": {"loaded": _module_loaded("intel_vpu")},
            },
            "metis": {"present": False, "device_nodes": []},
        },
        "decoder": {
            "va_api": {"available": va_available, "version": va_version},
        },
    }
