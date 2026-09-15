#!/usr/bin/env python3
"""Collect and verify the TVT edge hardware inventory.

The probe runs on the edge device (read-only, no mutations, no secrets) and
emits a canonical JSON document. The workstation consumes that document with
``--edge-inventory`` instead of inspecting its own PCI/kernel state.

Environment overrides (tests and exotic roots only):
  TVT_OS_RELEASE_FILE, TVT_CPUINFO_FILE, TVT_PCI_SYSFS_ROOT,
  TVT_KERNEL_RELEASE, TVT_PROC_MODULES_FILE, TVT_DEV_DRI_PATH,
  TVT_DEV_ACCEL_PATH
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess


SCHEMA_VERSION = 1
AXELERA_VENDOR = "0x1f9d"


def _read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="strict")


def _os_release(path: pathlib.Path) -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in _read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values.get("ID", ""), values.get("VERSION_ID", "")


def _cpu_model(path: pathlib.Path) -> str:
    for line in _read_text(path).splitlines():
        if line.lower().startswith("model name"):
            _, _, model = line.partition(":")
            return model.strip()
    return ""


def _modules(path: pathlib.Path) -> set[str]:
    loaded: set[str] = set()
    try:
        text = _read_text(path)
    except OSError:
        return loaded
    for line in text.splitlines():
        parts = line.split()
        if parts:
            loaded.add(parts[0])
    return loaded


def _axelera_scan(root: pathlib.Path) -> tuple[bool, str | None]:
    if not root.is_dir():
        return False, None
    for child in sorted(root.iterdir()):
        vendor_file = child / "vendor"
        try:
            vendor = vendor_file.read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if vendor == AXELERA_VENDOR:
            return True, child.name
    return False, None


def _pci_devices(root: pathlib.Path) -> list[dict[str, str | None]]:
    devices: list[dict[str, str | None]] = []
    if not root.is_dir():
        return devices
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue

        def value(name: str) -> str:
            try:
                return (child / name).read_text(encoding="utf-8").strip()
            except OSError:
                return ""

        driver_link = child / "driver"
        driver = driver_link.resolve().name if driver_link.exists() else None
        devices.append(
            {
                "address": child.name,
                "vendor_id": value("vendor").lower(),
                "device_id": value("device").lower(),
                "class": value("class").lower(),
                "driver": driver,
            }
        )
    return devices


def _architecture() -> str:
    try:
        result = subprocess.run(
            ["dpkg", "--print-architecture"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())


def _secure_boot() -> str:
    try:
        result = subprocess.run(
            ["mokutil", "--sb-state"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (result.stdout + result.stderr).strip() or "unknown"


def collect() -> dict:
    os_release = pathlib.Path(os.environ.get("TVT_OS_RELEASE_FILE", "/etc/os-release"))
    cpuinfo = pathlib.Path(os.environ.get("TVT_CPUINFO_FILE", "/proc/cpuinfo"))
    pci_root = pathlib.Path(os.environ.get("TVT_PCI_SYSFS_ROOT", "/sys/bus/pci/devices"))
    modules_file = pathlib.Path(
        os.environ.get("TVT_PROC_MODULES_FILE", "/proc/modules")
    )
    dri = pathlib.Path(os.environ.get("TVT_DEV_DRI_PATH", "/dev/dri/renderD128"))
    accel = pathlib.Path(os.environ.get("TVT_DEV_ACCEL_PATH", "/dev/accel/accel0"))

    os_id, os_version = _os_release(os_release)
    kernel = os.environ.get("TVT_KERNEL_RELEASE") or platform.uname().release
    axelera_present, axelera_address = _axelera_scan(pci_root)
    pci_devices = _pci_devices(pci_root)
    loaded = _modules(modules_file)
    meminfo = pathlib.Path("/proc/meminfo")
    memory_mib = None
    try:
        for line in _read_text(meminfo).splitlines():
            if line.startswith("MemTotal:"):
                memory_mib = int(line.split()[1]) // 1024
                break
    except (IndexError, ValueError, OSError):
        pass
    try:
        disk_free_mib = shutil.disk_usage("/opt").free // (1024 * 1024)
    except OSError:
        disk_free_mib = None
    return {
        "schema_version": SCHEMA_VERSION,
        "collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "os_id": os_id,
        "os_version_id": os_version,
        "architecture": _architecture(),
        "cpu_model": _cpu_model(cpuinfo),
        "kernel_version": kernel,
        "memory_mib": memory_mib,
        "disk_free_mib": disk_free_mib,
        "secure_boot": _secure_boot(),
        "pci_devices": pci_devices,
        "axelera_present": axelera_present,
        "axelera_pci_address": axelera_address,
        "gpu_render_node": dri.exists(),
        "npu_node": accel.exists(),
        "modules": {
            "i915": "i915" in loaded,
            "xe": "xe" in loaded,
            "intel_vpu": "intel_vpu" in loaded,
            "metis": "metis" in loaded,
        },
    }


def validate(document: dict) -> dict:
    if not isinstance(document, dict):
        raise ValueError("inventory must be a JSON object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported inventory schema: {document.get('schema_version')!r}")
    for key in ("os_id", "os_version_id", "architecture", "cpu_model", "kernel_version"):
        value = document.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"inventory {key} must be a non-empty string")
    if (document["os_id"], document["os_version_id"]) != ("ubuntu", "24.04"):
        raise ValueError("inventory OS must be Ubuntu 24.04")
    if document["architecture"] != "amd64":
        raise ValueError("inventory architecture must be amd64")
    if not re.search(r"Intel.*285H", document["cpu_model"], re.IGNORECASE):
        raise ValueError("inventory CPU must be an Intel Core Ultra 285H")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_~-]*", document["kernel_version"]):
        raise ValueError("inventory kernel_version is not a bounded version string")
    if not isinstance(document.get("axelera_present"), bool):
        raise ValueError("inventory axelera_present must be boolean")
    address = document.get("axelera_pci_address")
    if document["axelera_present"]:
        if not isinstance(address, str) or not address:
            raise ValueError("inventory axelera_pci_address is required when Axelera is present")
    elif address is not None:
        raise ValueError("inventory axelera_pci_address must be null without Axelera hardware")
    modules = document.get("modules")
    if not isinstance(modules, dict) or {
        "i915", "xe", "intel_vpu", "metis",
    } != set(modules) or not all(isinstance(v, bool) for v in modules.values()):
        raise ValueError("inventory modules must map i915/xe/intel_vpu/metis to booleans")
    for key in ("gpu_render_node", "npu_node"):
        if not isinstance(document.get(key), bool):
            raise ValueError(f"inventory {key} must be boolean")
    for key in ("memory_mib", "disk_free_mib"):
        if document.get(key) is not None and (
            not isinstance(document[key], int) or document[key] < 0
        ):
            raise ValueError(f"inventory {key} must be a non-negative integer or null")
    if not isinstance(document.get("secure_boot"), str):
        raise ValueError("inventory secure_boot must be text")
    devices = document.get("pci_devices")
    if not isinstance(devices, list):
        raise ValueError("inventory pci_devices must be an array")
    for device in devices:
        if not isinstance(device, dict):
            raise ValueError("inventory PCI devices must be objects")
        for key in ("address", "vendor_id", "device_id", "class"):
            if not isinstance(device.get(key), str) or not device[key]:
                raise ValueError(f"inventory PCI device {key} is missing")
    # Secret-adjacent values must never ride the probe file.
    for key in ("ip", "address", "serial", "token", "password", "secret", "key"):
        if key in document:
            raise ValueError(f"inventory must not carry {key}")
    return document


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_inventory(path: pathlib.Path, document: dict) -> str:
    if str(path) == "-":
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        print(text, end="")
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    if path.is_symlink():
        raise ValueError(f"refusing symlinked inventory output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    digest = _sha256(path)
    sidecar = path.parent / f"{path.name}.sha256"
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    os.chmod(sidecar, 0o644)
    return digest


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe", help="collect the local edge inventory")
    probe.add_argument("--output", required=True, type=pathlib.Path)
    verify = commands.add_parser("verify", help="validate an inventory file")
    verify.add_argument("--inventory", required=True, type=pathlib.Path)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "probe":
            document = validate(collect())
            digest = write_inventory(args.output, document)
            if str(args.output) != "-":
                print(f"Wrote edge inventory: {args.output}")
                print(f"Checksum sidecar: {args.output}.sha256")
                print(f"sha256: {digest}")
        else:
            if args.inventory.is_symlink():
                raise ValueError(f"refusing symlinked inventory: {args.inventory}")
            try:
                document = json.loads(args.inventory.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(f"invalid inventory file: {error}") from error
            validate(document)
            print(
                f"Verified edge inventory: axelera_present="
                f"{str(document['axelera_present']).lower()} "
                f"kernel={document['kernel_version']}"
            )
        return 0
    except (ValueError, OSError) as error:
        print(f"tvt-hardware-inventory: ERROR: {error}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
