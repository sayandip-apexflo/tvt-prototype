"""Resolve control-plane image tags and render digest-pinned manifests."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from apexfabric.solution_management.catalog import DIGEST_RE, resolve_registry_digest


COMPONENTS = {
    "node-reporter": "apexfabric/node-reporter",
    "node-status-controller": "apexfabric/node-status-controller",
}


def create_image_lock(registry: str, version: str) -> dict[str, Any]:
    parsed = urlparse(registry if "://" in registry else f"http://{registry}")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise ValueError("registry must be an http(s) registry origin")
    images = {}
    for component, repository in COMPONENTS.items():
        digest = resolve_registry_digest(registry, repository, version)
        images[component] = f"{parsed.netloc}/{repository}@{digest}"
    return {"version": 1, "registry": parsed.netloc, "images": images}


def validate_image_lock(lock: Any) -> dict[str, str]:
    if not isinstance(lock, dict) or lock.get("version") != 1:
        raise ValueError("unsupported image lock format")
    images = lock.get("images")
    if not isinstance(images, dict) or set(images) != set(COMPONENTS):
        raise ValueError("image lock must contain exactly the node-management images")
    registry = lock.get("registry")
    if (
        not isinstance(registry, str)
        or not registry
        or "://" in registry
        or "/" in registry
        or any(character.isspace() for character in registry)
    ):
        raise ValueError("image lock registry must be a HOST[:PORT] value")
    for component, reference in images.items():
        if not isinstance(reference, str) or "@" not in reference:
            raise ValueError(f"{component} must be pinned by digest")
        repository, digest = reference.rsplit("@", 1)
        expected_repository = f"{registry}/{COMPONENTS[component]}"
        if repository != expected_repository or not DIGEST_RE.fullmatch(digest):
            raise ValueError(f"{component} has an invalid digest reference")
    return images


def render_manifest(template: Path, lock: dict[str, Any]) -> str:
    images = validate_image_lock(lock)
    resources = list(yaml.safe_load_all(template.read_text(encoding="utf-8")))
    replaced: set[str] = set()
    for resource in resources:
        if resource.get("kind") == "DaemonSet" and resource.get("metadata", {}).get("name") == "node-reporter":
            resource["spec"]["template"]["spec"]["containers"][0]["image"] = images["node-reporter"]
            replaced.add("node-reporter")
        if resource.get("kind") == "Deployment" and resource.get("metadata", {}).get("name") == "node-status-controller":
            resource["spec"]["template"]["spec"]["containers"][0]["image"] = images["node-status-controller"]
            replaced.add("node-status-controller")
    if replaced != set(COMPONENTS):
        raise ValueError("node-management template is missing expected workloads")
    return yaml.safe_dump_all(resources, sort_keys=False)


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--registry", required=True)
    create.add_argument("--version", required=True)
    create.add_argument("--output", required=True, type=Path)
    render = commands.add_parser("render")
    render.add_argument("--lock", required=True, type=Path)
    render.add_argument("--template", required=True, type=Path)
    render.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            lock = create_image_lock(args.registry, args.version)
            write_private(args.output, json.dumps(lock, indent=2, sort_keys=True) + "\n")
        else:
            lock = json.loads(args.lock.read_text(encoding="utf-8"))
            write_private(args.output, render_manifest(args.template, lock))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
