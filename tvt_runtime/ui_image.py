"""Digest-pinned lock and manifest rendering for the site UI image.

Mirrors ``tvt_runtime.image_lock`` (node-management images) for the single
``apexfabric/ui`` image vendored under ``deploy/ui/web/`` (smoke/fire logic
purged; TVT runs no smoke/fire CV pipelines). The UI talks to the k3s control
plane on loopback ``:8088``; the TVT host API stays on ``:8089``.
"""

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


REPOSITORY = "apexfabric/ui"
PLACEHOLDER_PREFIX = "__APEXFABRIC_REGISTRY__"


def create_ui_lock(registry: str, version: str) -> dict[str, Any]:
    parsed = urlparse(registry if "://" in registry else f"http://{registry}")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise ValueError("registry must be an http(s) registry origin")
    digest = resolve_registry_digest(registry, REPOSITORY, version)
    return {
        "version": 1,
        "registry": parsed.netloc,
        "images": {"ui": f"{parsed.netloc}/{REPOSITORY}@{digest}"},
    }


def validate_ui_lock(lock: Any) -> str:
    if not isinstance(lock, dict) or lock.get("version") != 1:
        raise ValueError("unsupported UI image lock format")
    images = lock.get("images")
    if not isinstance(images, dict) or set(images) != {"ui"}:
        raise ValueError("UI image lock must contain exactly the ui image")
    registry = lock.get("registry")
    if (
        not isinstance(registry, str)
        or not registry
        or "://" in registry
        or "/" in registry
        or any(character.isspace() for character in registry)
    ):
        raise ValueError("UI image lock registry must be a HOST[:PORT] value")
    reference = images["ui"]
    if not isinstance(reference, str) or "@" not in reference:
        raise ValueError("ui must be pinned by digest")
    repository, digest = reference.rsplit("@", 1)
    if repository != f"{registry}/{REPOSITORY}" or not DIGEST_RE.fullmatch(digest):
        raise ValueError("ui has an invalid digest reference")
    return reference


def render_ui_manifest(template: Path, lock: dict[str, Any]) -> str:
    reference = validate_ui_lock(lock)
    resources = list(yaml.safe_load_all(template.read_text(encoding="utf-8")))
    replaced = False
    for resource in resources:
        if resource.get("kind") == "Deployment" and resource.get("metadata", {}).get("name") == "apexfabric-ui":
            containers = resource["spec"]["template"]["spec"]["containers"]
            if len(containers) != 1 or not str(containers[0].get("image", "")).startswith(PLACEHOLDER_PREFIX):
                raise ValueError("UI template is missing the placeholder image")
            containers[0]["image"] = reference
            replaced = True
    if not replaced:
        raise ValueError("UI template is missing the apexfabric-ui Deployment")
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
            lock = create_ui_lock(args.registry, args.version)
            write_private(args.output, json.dumps(lock, indent=2, sort_keys=True) + "\n")
        else:
            lock = json.loads(args.lock.read_text(encoding="utf-8"))
            write_private(args.output, render_ui_manifest(args.template, lock))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
