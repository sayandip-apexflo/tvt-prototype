#!/usr/bin/env python3
"""Read-only qualification checks for an ApexFabric Ubuntu compute node."""

from __future__ import annotations

import argparse
import grp
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PASS = "PASS"
FAIL = "FAIL"
NOT_APPLICABLE = "NOT_APPLICABLE"

BASE_PACKAGES = (
    "ca-certificates", "curl", "iproute2", "iptables", "conntrack", "socat",
    "pciutils", "procps", "util-linux", "python3", "python3-yaml", "python3-jsonschema", "containerd",
    "gstreamer1.0-tools", "gstreamer1.0-plugins-base",
    "gstreamer1.0-plugins-good", "gstreamer1.0-plugins-bad",
    "gstreamer1.0-libav", "vainfo",
)


@dataclass(frozen=True)
class Result:
    name: str
    status: str
    detail: str


class SystemProbe:
    def command_exists(self, command: str) -> bool:
        return shutil.which(command) is not None

    def run(self, command: str, *args: str) -> tuple[int, str]:
        if not self.command_exists(command):
            return 127, ""
        try:
            result = subprocess.run([command, *args], check=False, capture_output=True, text=True, timeout=10)
            return result.returncode, (result.stdout + result.stderr).strip()
        except (OSError, subprocess.TimeoutExpired):
            return 126, ""

    def package_installed(self, package: str) -> bool:
        code, output = self.run("dpkg-query", "-W", "-f=${db:Status-Abbrev}", package)
        return code == 0 and output.startswith("ii")

    def read(self, path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def path(self, path: str) -> Path:
        return Path(path)

    def group_gid(self, name: str) -> int | None:
        try:
            return grp.getgrnam(name).gr_gid
        except KeyError:
            return None


def result(condition: bool, name: str, good: str, bad: str) -> Result:
    return Result(name, PASS if condition else FAIL, good if condition else bad)


def parse_os_release(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


def check_directories(probe: SystemProbe) -> list[Result]:
    checks = []
    expected_gid = probe.group_gid("apexfabric")
    for directory in ("/etc/apexfabric", "/var/lib/apexfabric", "/var/log/apexfabric"):
        path = probe.path(directory)
        exists = path.is_dir()
        secure = False
        if exists:
            try:
                metadata = path.stat()
                mode = stat.S_IMODE(metadata.st_mode)
                secure = mode == 0o750 and metadata.st_uid == 0 and metadata.st_gid == expected_gid
            except OSError:
                secure = False
        checks.append(result(
            exists and secure, f"directory:{directory}",
            "root:apexfabric mode 0750",
            "must be root:apexfabric mode 0750; run bootstrap-node.sh --apply",
        ))
    return checks


def check_node(capabilities: dict[str, Any], probe: SystemProbe | None = None) -> list[Result]:
    probe = probe or SystemProbe()
    checks: list[Result] = []

    os_info = parse_os_release(probe.read("/etc/os-release"))
    ubuntu = os_info.get("ID") == "ubuntu"
    checks.append(result(ubuntu, "os", f"Ubuntu {os_info.get('VERSION_ID', 'unknown')}", "Ubuntu is required"))

    missing = [package for package in BASE_PACKAGES if not probe.package_installed(package)]
    checks.append(result(not missing, "packages", "all base packages installed", "missing: " + ", ".join(missing)))

    runtime = probe.command_exists("containerd") or probe.command_exists("k3s")
    checks.append(result(runtime, "container_runtime", "containerd runtime available", "containerd/K3s runtime unavailable"))

    for module in ("overlay", "br_netfilter"):
        loaded = probe.path(f"/sys/module/{module}").is_dir()
        checks.append(result(loaded, f"kernel_module:{module}", "loaded", "not loaded"))
    for name, path in (
        ("ip_forward", "/proc/sys/net/ipv4/ip_forward"),
        ("bridge_ipv4", "/proc/sys/net/bridge/bridge-nf-call-iptables"),
        ("bridge_ipv6", "/proc/sys/net/bridge/bridge-nf-call-ip6tables"),
    ):
        checks.append(result(probe.read(path).strip() == "1", f"security:{name}", "enabled", "must equal 1"))

    network = capabilities.get("network", {})
    interfaces = network.get("interfaces", [])
    active = [item.get("name") for item in interfaces if item.get("name") != "lo" and item.get("state") == "UP"]
    checks.append(result(bool(active), "network:active_interface", ", ".join(active), "no active non-loopback interface"))
    connectivity = network.get("connectivity", {})
    checks.append(result(bool(connectivity.get("dns_configured")), "network:dns", "configured", "no DNS nameserver detected"))

    decoder = capabilities.get("decoder", {})
    gst_decoders = decoder.get("gstreamer_decoder_elements", [])
    checks.append(result(bool(gst_decoders), "gstreamer:decoders", f"{len(gst_decoders)} registered", "no decoder elements registered"))

    gpu = capabilities.get("accelerators", {}).get("gpu", {})
    vendors = {item.get("vendor") for item in gpu.get("devices", [])}
    va_applicable = bool(vendors & {"AMD", "Intel"})
    if va_applicable:
        checks.append(result(
            bool(decoder.get("va_api", {}).get("available")), "video_decode:va_api",
            "available", "GPU/iGPU detected but vainfo could not initialize VA-API",
        ))
        driver_package = "mesa-va-drivers" if "AMD" in vendors else "intel-media-va-driver"
        checks.append(result(
            probe.package_installed(driver_package), f"gpu_driver:{driver_package}",
            "installed", "required for detected GPU/iGPU",
        ))
    else:
        checks.append(Result("video_decode:va_api", NOT_APPLICABLE, "no supported AMD/Intel GPU detected"))
        checks.append(Result("gpu_driver", NOT_APPLICABLE, "no applicable GPU/iGPU"))

    metis = capabilities.get("accelerators", {}).get("metis", {})
    voyager = capabilities.get("software", {}).get("voyager_sdk", {})
    if not metis.get("present"):
        checks.append(Result("metis_hardware", NOT_APPLICABLE, "NOT_PRESENT"))
        checks.append(Result("metis_driver", NOT_APPLICABLE, "hardware NOT_PRESENT"))
        checks.append(Result("voyager_sdk", NOT_APPLICABLE, "hardware NOT_PRESENT"))
    else:
        checks.append(Result("metis_hardware", PASS, "PRESENT"))
        checks.append(result(bool(metis.get("driver", {}).get("loaded")), "metis_driver", "loaded", "Metis present but driver not loaded"))
        checks.append(result(bool(voyager.get("present")), "voyager_sdk", "installed", "Metis present but Voyager SDK unavailable"))
        checks.append(result(bool(metis.get("axdevice", {}).get("present")), "metis:axdevice", "available", "axdevice unavailable"))

    checks.extend(check_directories(probe))
    return checks


def render(checks: list[Result]) -> str:
    width = max(len(item.name) for item in checks)
    lines = [f"{item.status:<14} {item.name:<{width}}  {item.detail}" for item in checks]
    qualified = all(item.status != FAIL for item in checks)
    lines.extend(("", "QUALIFIED" if qualified else "NOT QUALIFIED"))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify an ApexFabric compute node")
    parser.add_argument("--capabilities", help="existing capability JSON; otherwise discover live")
    parser.add_argument("--json", action="store_true", help="print machine-readable report")
    parser.add_argument("--output", default="node-qualification.json", help="qualification JSON artifact path")
    args = parser.parse_args()
    if args.capabilities:
        capabilities = json.loads(Path(args.capabilities).read_text(encoding="utf-8"))
    else:
        from apexfabric.node_management.discovery.discovery import discover
        capabilities = discover()
    checks = check_node(capabilities)
    qualified = all(item.status != FAIL for item in checks)
    report = {"qualification": "QUALIFIED" if qualified else "NOT QUALIFIED", "checks": [item.__dict__ for item in checks]}
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(checks))
        print(f"\nQualification artifact: {args.output}", file=sys.stderr)
    return 0 if qualified else 1


if __name__ == "__main__":
    raise SystemExit(main())
