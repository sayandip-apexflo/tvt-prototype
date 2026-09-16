#!/usr/bin/env python3
"""Read-only ApexFabric node capability discovery."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = "1.1.0"


class CommandRunner:
    def exists(self, command: str) -> bool:
        return shutil.which(command) is not None

    def run(self, command: str, *args: str, timeout: int = 10) -> tuple[int, str]:
        if not self.exists(command):
            return 127, ""
        try:
            result = subprocess.run(
                [command, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode, (result.stdout + result.stderr).strip()
        except (OSError, subprocess.TimeoutExpired):
            return 126, ""


def read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def first_line(value: str) -> str | None:
    return value.splitlines()[0].strip() if value.strip() else None


def command_version(runner: CommandRunner, command: str, *args: str) -> dict[str, Any]:
    if not runner.exists(command):
        return {"present": False, "version": None}
    code, output = runner.run(command, *args)
    return {"present": True, "version": first_line(output) if code == 0 else None}


def topology_int(value: Any, default: int) -> int:
    """Parse an lscpu topology value; ARM platforms may report '-' or blank."""
    try:
        parsed = int(str(value).strip())
        return parsed if parsed >= 0 else default
    except (TypeError, ValueError):
        return default


def discover_cpu(runner: CommandRunner) -> dict[str, Any]:
    code, output = runner.run("lscpu", "--json")
    fields: dict[str, str] = {}
    if code == 0:
        try:
            fields = {
                item["field"].rstrip(":"): item["data"]
                for item in json.loads(output).get("lscpu", [])
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            fields = {}
    threads = topology_int(fields.get("CPU(s)"), os.cpu_count() or 0)
    sockets = topology_int(fields.get("Socket(s)"), 1) or 1
    cores_per_socket = topology_int(fields.get("Core(s) per socket"), 0)
    cores = sockets * cores_per_socket or threads
    return {
        "model": fields.get("Model name") or platform.processor() or None,
        "cores": cores,
        "threads": threads,
        "architecture": fields.get("Architecture") or platform.machine(),
    }


def discover_memory(reader: Callable[[str], str]) -> dict[str, Any]:
    values = {
        key: int(value) * 1024
        for key, value in re.findall(r"^(\w+):\s+(\d+)\s+kB$", reader("/proc/meminfo"), re.M)
    }
    return {
        "total_bytes": values.get("MemTotal"),
        "available_bytes": values.get("MemAvailable"),
    }


def discover_storage(runner: CommandRunner) -> dict[str, Any]:
    columns = "NAME,KNAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL,VENDOR,TRAN"
    code, output = runner.run("lsblk", "--json", "--bytes", "--output", columns)
    disks: list[dict[str, Any]] = []
    if code == 0:
        try:
            for item in json.loads(output).get("blockdevices", []):
                if item.get("type") == "disk":
                    disks.append({
                        "name": item.get("name"),
                        "device": f"/dev/{item.get('kname') or item.get('name')}",
                        "model": (item.get("model") or "").strip() or None,
                        "vendor": (item.get("vendor") or "").strip() or None,
                        "transport": item.get("tran"),
                        "capacity_bytes": int(item.get("size") or 0),
                    })
        except (json.JSONDecodeError, TypeError, ValueError):
            disks = []

    filesystems: list[dict[str, Any]] = []
    code, output = runner.run(
        "df", "--block-size=1", "--output=source,target,size,avail", "--exclude-type=tmpfs",
        "--exclude-type=devtmpfs",
    )
    if code == 0:
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
                filesystems.append({
                    "source": parts[0], "mountpoint": parts[1],
                    "capacity_bytes": int(parts[2]), "available_bytes": int(parts[3]),
                })
    disks.sort(key=lambda item: item["device"])
    filesystems.sort(key=lambda item: (item["mountpoint"], item["source"]))
    return {
        "disks": disks,
        "total_disk_capacity_bytes": sum(item["capacity_bytes"] for item in disks),
        "mounted_filesystems": filesystems,
        "available_capacity_bytes": sum(item["available_bytes"] for item in filesystems),
    }


def parse_pci_display_devices(output: str) -> list[dict[str, str]]:
    devices = []
    for line in output.splitlines():
        if not re.search(r"VGA compatible controller|3D controller|Display controller", line, re.I):
            continue
        match = re.match(r"^(\S+)\s+([^:]+):\s+(.+)$", line.strip())
        if match:
            pci_ids = re.findall(r"\[([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\]", match.group(3))
            devices.append({
                "pci_address": match.group(1), "class": match.group(2),
                "model": match.group(3), "pci_id": pci_ids[-1] if pci_ids else "unknown",
            })
    return sorted(devices, key=lambda item: item["pci_address"])


def discover_gpu(runner: CommandRunner) -> dict[str, Any]:
    code, output = runner.run("lspci", "-Dnn")
    devices = parse_pci_display_devices(output) if code == 0 else []
    for device in devices:
        text = device["model"].lower()
        device["vendor"] = next((name for key, name in (
            ("intel", "Intel"), ("nvidia", "NVIDIA"), ("amd", "AMD"),
            ("advanced micro devices", "AMD"), ("apple", "Apple"),
        ) if key in text), "unknown")
    dri_devices = sorted(glob.glob("/dev/dri/card*") + glob.glob("/dev/dri/renderD*"))
    return {"present": bool(devices or dri_devices), "devices": devices, "device_nodes": dri_devices}


def discover_npu(reader: Callable[[str], str]) -> dict[str, Any]:
    device_nodes = sorted(glob.glob("/dev/accel/accel*"))
    driver_loaded = Path("/sys/module/intel_vpu").exists() or bool(reader("/sys/module/intel_vpu/version"))
    return {
        "present": bool(device_nodes),
        "device_nodes": device_nodes,
        "driver": {"name": "intel_vpu", "loaded": driver_loaded},
    }


def discover_metis(runner: CommandRunner, reader: Callable[[str], str]) -> dict[str, Any]:
    _, pci = runner.run("lspci", "-Dnn")
    pci_matches = sorted(line.strip() for line in pci.splitlines() if re.search(r"axelera|metis", line, re.I))
    driver_loaded = bool(reader("/sys/class/metis/version")) or Path("/sys/module/metis").exists()
    axdevice = command_version(runner, "axdevice", "--version")
    device_output = None
    if axdevice["present"]:
        code, output = runner.run("axdevice")
        device_output = first_line(output) if code == 0 else None
    device_nodes = sorted(glob.glob("/dev/metis*") + glob.glob("/dev/axelera*"))
    # Hardware presence requires hardware evidence, never SDK/driver installation alone.
    present = bool(pci_matches or device_nodes or device_output)
    return {
        "present": present,
        "vendor": "Axelera AI" if present else None,
        "model": "Metis" if present else None,
        "pci_devices": pci_matches,
        "device_nodes": device_nodes,
        "driver": {
            "loaded": driver_loaded,
            "version": first_line(reader("/sys/class/metis/version")),
        },
        "axdevice": axdevice,
        "detection_evidence": device_output,
    }


def discover_decoder(runner: CommandRunner) -> dict[str, Any]:
    va_tool = runner.exists("vainfo")
    va_available = False
    va_summary = None
    va_device = None
    if va_tool:
        code, output = runner.run("vainfo")
        va_available = code == 0
        va_summary = first_line(output)
        if not va_available:
            for device in sorted(glob.glob("/dev/dri/renderD*")):
                code, output = runner.run("vainfo", "--display", "drm", "--device", device)
                if code == 0:
                    va_available = True
                    va_summary = first_line(output)
                    va_device = device
                    break

    gst = command_version(runner, "gst-inspect-1.0", "--version")
    decoders: list[str] = []
    if gst["present"]:
        code, output = runner.run("gst-inspect-1.0", timeout=20)
        if code == 0:
            for line in output.splitlines():
                match = re.match(r"^\s*([^: ]+):\s+([^: ]+):\s+(.+)$", line)
                if match and re.search(r"decod", match.group(3), re.I):
                    decoders.append(f"{match.group(1)}:{match.group(2)}")
    return {
        "va_api": {
            "available": va_available,
            "tool_present": va_tool,
            "summary": va_summary,
            "device": va_device,
        },
        "gstreamer": gst,
        "gstreamer_decoder_elements": sorted(set(decoders)),
    }


def discover_network(runner: CommandRunner, reader: Callable[[str], str]) -> dict[str, Any]:
    interfaces: list[dict[str, Any]] = []
    code, output = runner.run("ip", "-json", "address", "show")
    address_probe_ok = code == 0
    address_probe_error = None if address_probe_ok else first_line(output)
    if code == 0:
        try:
            for item in json.loads(output):
                addresses = [
                    {"family": addr.get("family"), "address": addr.get("local"), "prefix_length": addr.get("prefixlen")}
                    for addr in item.get("addr_info", [])
                ]
                interfaces.append({
                    "name": item.get("ifname"), "state": item.get("operstate", "UNKNOWN"),
                    "mac_address": item.get("address"),
                    "mtu": item.get("mtu"), "addresses": addresses,
                })
        except (json.JSONDecodeError, TypeError):
            interfaces = []
    if not interfaces:
        # Netlink can be restricted in containers/sandboxes. Sysfs still gives a
        # truthful interface inventory, with addresses left empty when unreadable.
        for path in glob.glob("/sys/class/net/*"):
            name = Path(path).name
            mtu = reader(f"{path}/mtu").strip()
            interfaces.append({
                "name": name,
                "state": reader(f"{path}/operstate").strip().upper() or "UNKNOWN",
                "mac_address": reader(f"{path}/address").strip() or None,
                "mtu": int(mtu) if mtu.isdigit() else None,
                "addresses": [],
            })
    code, output = runner.run("ip", "-json", "route", "show", "default")
    route_probe_ok = code == 0
    route_probe_error = None if route_probe_ok else first_line(output)
    try:
        routes = json.loads(output) if code == 0 else []
    except json.JSONDecodeError:
        routes = []
    if not routes:
        route_lines = reader("/proc/net/route").splitlines()[1:]
        for line in route_lines:
            fields = line.split()
            if len(fields) >= 8 and fields[1] == "00000000":
                try:
                    gateway = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
                except (OSError, ValueError, struct.error):
                    gateway = None
                routes.append({"dst": "default", "gateway": gateway, "dev": fields[0]})
    nameservers = sorted(set(re.findall(r"^nameserver\s+(\S+)", reader("/etc/resolv.conf"), re.M)))
    interfaces.sort(key=lambda item: item["name"] or "")
    return {
        "interfaces": interfaces,
        "default_routes": sorted(routes, key=lambda item: json.dumps(item, sort_keys=True)),
        "probe_status": {
            "ip_address_available": address_probe_ok,
            "ip_address_error": address_probe_error,
            "ip_route_available": route_probe_ok,
            "ip_route_error": route_probe_error,
        },
        "connectivity": {
            "default_route_present": bool(routes),
            "dns_configured": bool(nameservers),
            "dns_nameservers": nameservers,
        },
    }


def os_release(reader: Callable[[str], str]) -> dict[str, str | None]:
    matches = re.findall(r'^([A-Z_]+)=(?:"([^"]*)"|(.*))$', reader("/etc/os-release"), re.M)
    normalized = {key: quoted or plain for key, quoted, plain in matches}
    return {"name": normalized.get("NAME"), "version": normalized.get("VERSION_ID"), "id": normalized.get("ID")}


def discover_voyager(runner: CommandRunner) -> dict[str, Any]:
    packages = {}
    for package in ("axelera-rt", "axelera-devkit"):
        code, output = runner.run("python3", "-m", "pip", "show", package)
        match = re.search(r"^Version:\s*(.+)$", output, re.M) if code == 0 else None
        packages[package] = match.group(1).strip() if match else None
    paths = sorted(path for path in glob.glob("/opt/axelera/*") if os.path.isdir(path))
    present = any(packages.values()) or bool(paths) or runner.exists("axdevice")
    return {"present": present, "packages": packages, "install_paths": paths}


def discover_software(runner: CommandRunner, reader: Callable[[str], str]) -> dict[str, Any]:
    runtimes = {
        "docker": command_version(runner, "docker", "--version"),
        "containerd": command_version(runner, "containerd", "--version"),
        "k3s_containerd": command_version(runner, "k3s", "ctr", "version"),
    }
    return {
        "os": os_release(reader),
        "kernel": platform.release(),
        "container_runtimes": runtimes,
        "gstreamer": command_version(runner, "gst-launch-1.0", "--version"),
        "k3s": command_version(runner, "k3s", "--version"),
        "voyager_sdk": discover_voyager(runner),
    }


def stable_node_id(reader: Callable[[str], str]) -> str:
    machine_id = reader("/etc/machine-id").strip()
    if machine_id:
        return "node-" + hashlib.sha256(machine_id.encode()).hexdigest()[:16]
    return socket.gethostname().lower()


def discover_cameras(reader: Callable[[str], str]) -> dict[str, Any]:
    raw = reader("/etc/apexfabric/cameras.json")
    if not raw.strip():
        return {"accessible": [], "source": None}
    try:
        configured = json.loads(raw)
        cameras = configured.get("cameras", {}).get("accessible", [])
        if not isinstance(cameras, list) or not all(isinstance(item, str) for item in cameras):
            raise ValueError
        return {"accessible": sorted(set(cameras)), "source": "/etc/apexfabric/cameras.json"}
    except (json.JSONDecodeError, AttributeError, ValueError):
        return {"accessible": [], "source": "/etc/apexfabric/cameras.json", "error": "invalid configuration"}


def discover(runner: CommandRunner | None = None, reader: Callable[[str], str] = read_text) -> dict[str, Any]:
    runner = runner or CommandRunner()
    return {
        "schema_version": SCHEMA_VERSION,
        "node_id": stable_node_id(reader),
        "hardware": {
            "cpu": discover_cpu(runner),
            "memory": discover_memory(reader),
            "storage": discover_storage(runner),
        },
        "accelerators": {
            "gpu": discover_gpu(runner),
            "npu": discover_npu(reader),
            "metis": discover_metis(runner, reader),
        },
        "decoder": discover_decoder(runner),
        "cameras": discover_cameras(reader),
        "network": discover_network(runner, reader),
        "software": discover_software(runner, reader),
    }


def human_size(value: int | None) -> str:
    if value is None:
        return "unknown"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return str(value)


def render_human(data: dict[str, Any]) -> str:
    cpu, memory, storage = (data["hardware"][key] for key in ("cpu", "memory", "storage"))
    gpu, metis = data["accelerators"]["gpu"], data["accelerators"]["metis"]
    network = data["network"]
    lines = [
        f"ApexFabric node: {data['node_id']} (schema {data['schema_version']})",
        f"CPU: {cpu['model'] or 'unknown'}; {cpu['cores']} cores/{cpu['threads']} threads; {cpu['architecture']}",
        f"Memory: {human_size(memory['total_bytes'])} total, {human_size(memory['available_bytes'])} available",
        f"Storage: {len(storage['disks'])} disk(s), {human_size(storage['total_disk_capacity_bytes'])} raw, {human_size(storage['available_capacity_bytes'])} filesystem available",
        f"GPU/iGPU: {'present' if gpu['present'] else 'absent'} ({len(gpu['devices'])} PCI display device(s))",
        f"Metis: {'present' if metis['present'] else 'absent'}; driver {'loaded' if metis['driver']['loaded'] else 'not loaded'}",
        f"VA-API: {'available' if data['decoder']['va_api']['available'] else 'unavailable'}; GStreamer decoders: {len(data['decoder']['gstreamer_decoder_elements'])}",
        f"Network: {len(network['interfaces'])} interface(s); default route {'present' if network['connectivity']['default_route_present'] else 'absent'}",
        f"OS: {data['software']['os']['name']} {data['software']['os']['version']}; kernel {data['software']['kernel']}",
        f"K3s: {data['software']['k3s']['version'] or 'not detected'}",
        f"Voyager SDK: {'present' if data['software']['voyager_sdk']['present'] else 'absent'}",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover actual ApexFabric node capabilities")
    parser.add_argument("--output", default="node-capabilities.json", help="JSON artifact path")
    parser.add_argument("--json", action="store_true", help="print JSON instead of the human summary")
    args = parser.parse_args()
    data = discover()
    rendered = json.dumps(data, indent=2, sort_keys=True) + "\n"
    Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered if args.json else render_human(data))
    print(f"JSON artifact: {args.output}", file=os.sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
