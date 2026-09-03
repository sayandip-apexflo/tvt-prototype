#!/usr/bin/env python3
"""Read and validate the canonical TVT application release version."""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import tomllib


ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?")


def canonical_version() -> str:
    module = ast.parse((ROOT / "tvt_edge/__init__.py").read_text(encoding="utf-8"))
    values = [
        node.value.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    if len(values) != 1 or not VERSION.fullmatch(values[0]):
        raise ValueError("tvt_edge.__version__ must be one semantic version string")
    return values[0]


def validate(expected: str | None = None) -> str:
    version = canonical_version()
    if expected is not None and version != expected:
        raise ValueError(f"canonical version is {version}, requested {expected}")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if project.get("project", {}).get("dynamic") != ["version"]:
        raise ValueError("pyproject.toml must derive its version dynamically")
    dynamic = project.get("tool", {}).get("setuptools", {}).get("dynamic", {}).get("version", {})
    if dynamic.get("attr") != "tvt_edge.__version__":
        raise ValueError("Python package version is not derived from tvt_edge.__version__")
    ui_package = json.loads((ROOT / "ui/package.json").read_text(encoding="utf-8"))
    ui_lock = json.loads((ROOT / "ui/package-lock.json").read_text(encoding="utf-8"))
    ui_versions = {
        ui_package.get("version"),
        ui_lock.get("version"),
        ui_lock.get("packages", {}).get("", {}).get("version"),
    }
    if ui_versions != {version}:
        raise ValueError(f"UI package versions do not all equal {version}: {sorted(map(str, ui_versions))}")
    manifest = json.loads((ROOT / "release/manifest.template.json").read_text(encoding="utf-8"))
    if manifest.get("release_version") != version:
        raise ValueError("release manifest template version does not equal the canonical version")
    return version


def set_version(version: str) -> None:
    if not VERSION.fullmatch(version):
        raise ValueError("new version is not valid semantic version text")
    init_path = ROOT / "tvt_edge/__init__.py"
    init_text = init_path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'^__version__ = "[^"]+"$', f'__version__ = "{version}"', init_text, flags=re.MULTILINE
    )
    if count != 1:
        raise ValueError("could not update exactly one tvt_edge.__version__ assignment")
    init_path.write_text(updated, encoding="utf-8")
    ui_path = ROOT / "ui/package.json"
    ui = json.loads(ui_path.read_text(encoding="utf-8"))
    ui["version"] = version
    ui_path.write_text(json.dumps(ui, indent=2) + "\n", encoding="utf-8")
    lock_path = ROOT / "ui/package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["version"] = version
    lock["packages"][""]["version"] = version
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    manifest_path = ROOT / "release/manifest.template.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["release_version"] = version
    manifest["artifacts"]["application_wheel"] = f"wheels/tvt_runtime-{version}-py3-none-any.whl"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate every derived version surface")
    parser.add_argument("--expected")
    parser.add_argument("--set", dest="new_version", help="synchronize source metadata to a new canonical version")
    args = parser.parse_args()
    try:
        if args.new_version:
            set_version(args.new_version)
            version = validate(args.new_version)
        else:
            version = validate(args.expected) if args.check or args.expected else canonical_version()
    except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        parser.error(str(error))
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
