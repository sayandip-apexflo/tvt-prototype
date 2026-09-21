"""TVT delivery metadata loader (Postgres catalog path).

This preserves the pre-sync TVT implementation that validates a vendored
delivery (originally Traffic, now the combined tvt-mills-pilot pack -- see
docs/contracts/tvt-mills-v1/README.md) against ``provenance.json`` including
the TVT-only files (``metrics.schema.json``, ``analytics-event.schema.json``,
``analytics-event.example.json``) which the user chose to keep.

``apexfabric/solution_management/catalog.py`` is now an exact copy of
``k3s-prototype@5ada504`` (SQLite ``SolutionCatalog``) and must not be edited.
TVT code imports :func:`load_delivery_metadata` from here instead.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from apexfabric.solution_management.catalog import CatalogError


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CatalogError(f"cannot read valid JSON metadata {path.name}: {error}") from error


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise CatalogError(f"cannot read catalog metadata {path.name}: {error}") from error


def load_delivery_metadata(directory: Path, expected_name: str = "tvt-mills-pilot") -> dict[str, Any]:
    """Load and verify a vendored delivery against its provenance document."""

    provenance = _read_json(directory / "provenance.json")
    if not isinstance(provenance, dict) or provenance.get("format_version") != 1:
        raise CatalogError("unsupported or invalid provenance format")
    files = provenance.get("files")
    if not isinstance(files, dict) or not files:
        raise CatalogError("provenance does not record catalog file checksums")
    checksums: dict[str, str] = {}
    for filename, record in files.items():
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not isinstance(record, dict)
        ):
            raise CatalogError("provenance contains an invalid file record")
        expected = record.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise CatalogError(f"provenance checksum is invalid for {filename}")
        actual = _sha256(directory / filename)
        if actual != expected:
            raise CatalogError(f"catalog metadata checksum mismatch for {filename}")
        checksums[filename] = actual

    try:
        contract = yaml.safe_load(
            (directory / "image-contract.yaml").read_text(encoding="utf-8")
        )
    except (OSError, yaml.YAMLError) as error:
        raise CatalogError(f"cannot read valid image contract: {error}") from error
    schema = _read_json(directory / "desired-state.schema.json")
    example = _read_json(directory / "desired-state.example.json")
    metrics_schema = _read_json(directory / "metrics.schema.json")
    event_schema = _read_json(directory / "analytics-event.schema.json")
    event_example = _read_json(directory / "analytics-event.example.json")
    if not isinstance(contract, dict):
        raise CatalogError("image contract must be an object")
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(example)
        jsonschema.Draft202012Validator.check_schema(metrics_schema)
        jsonschema.Draft202012Validator.check_schema(event_schema)
        jsonschema.Draft202012Validator(
            event_schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(event_example)
    except (jsonschema.SchemaError, jsonschema.ValidationError) as error:
        raise CatalogError(f"desired-state metadata is invalid: {error.message}") from error

    delivery = provenance.get("delivery") or {}
    archive = provenance.get("archive") or {}
    local_image = provenance.get("local_image") or {}
    if contract.get("name") != expected_name:
        raise CatalogError("unexpected solution name in image contract")
    if str(contract.get("version")) != delivery.get("version"):
        raise CatalogError("image contract version disagrees with provenance")
    if contract.get("architectures") != ["amd64"]:
        raise CatalogError(f"{expected_name} delivery must declare only amd64")
    if contract.get("hardwareProfile") != "intel-285h":
        raise CatalogError(f"{expected_name} delivery hardware profile is not intel-285h")
    if (contract.get("models") or {}).get("delivery") != "baked-in":
        raise CatalogError(f"{expected_name} delivery must declare baked-in models")
    loaded_image = archive.get("loaded_image")
    if not isinstance(loaded_image, str) or not re.fullmatch(
        rf"localhost/[a-z0-9][a-z0-9._/-]*:intel-285h-{re.escape(str(contract['version']))}",
        loaded_image,
    ):
        raise CatalogError("loaded image name or version disagrees with the image contract")
    expected_catalog_id = f"{contract['name']}:{contract['version']}"
    if provenance.get("catalog_id") != expected_catalog_id:
        raise CatalogError("catalog ID disagrees with the image contract")
    if local_image.get("tag") != f"intel-285h-{contract['version']}":
        raise CatalogError("local image tag disagrees with the image contract")

    return {
        "catalog_id": expected_catalog_id,
        "solution_name": contract["name"],
        "version": str(contract["version"]),
        "hardware_profile": contract["hardwareProfile"],
        "architectures": contract["architectures"],
        "repository": local_image.get("repository"),
        "tag": local_image.get("tag"),
        "contract": contract,
        "desired_state_schema": schema,
        "desired_state_example": example,
        "metrics_schema": metrics_schema,
        "analytics_event_schema": event_schema,
        "analytics_event_example": event_example,
        "provenance": provenance,
        "checksums": checksums,
    }
