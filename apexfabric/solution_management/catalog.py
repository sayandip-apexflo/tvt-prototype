"""Immutable delivery metadata loading and OCI Distribution digest resolution."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import jsonschema
import yaml

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class CatalogError(RuntimeError):
    """Raised when catalog metadata or an OCI registry response is invalid."""


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


def load_delivery_metadata(directory: Path) -> dict[str, Any]:
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
    if not isinstance(contract, dict):
        raise CatalogError("image contract must be an object")
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(example)
    except (jsonschema.SchemaError, jsonschema.ValidationError) as error:
        raise CatalogError(f"desired-state metadata is invalid: {error.message}") from error

    delivery = provenance.get("delivery") or {}
    archive = provenance.get("archive") or {}
    local_image = provenance.get("local_image") or {}
    if contract.get("name") != "traffic-edge-runtime":
        raise CatalogError("unexpected solution name in image contract")
    if str(contract.get("version")) != delivery.get("version"):
        raise CatalogError("image contract version disagrees with provenance")
    if contract.get("architectures") != ["amd64"]:
        raise CatalogError("Traffic delivery must declare only amd64")
    if contract.get("hardwareProfile") != "intel-285h":
        raise CatalogError("Traffic delivery hardware profile is not intel-285h")
    if (contract.get("models") or {}).get("delivery") != "baked-in":
        raise CatalogError("Traffic delivery must declare baked-in models")
    if archive.get("loaded_image") != (
        f"{contract['name']}:intel-285h-{contract['version']}"
    ):
        raise CatalogError("loaded image name disagrees with the image contract")
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
        "provenance": provenance,
        "checksums": checksums,
    }


def resolve_registry_digest(
    registry: str, repository: str, reference: str, timeout: int = 10
) -> str:
    """Resolve a tag and verify the digest against exact manifest bytes."""

    registry_url = registry.rstrip("/")
    if "://" not in registry_url:
        registry_url = f"http://{registry_url}"
    request = Request(
        f"{registry_url}/v2/{repository}/manifests/{reference}",
        headers={"Accept": MANIFEST_ACCEPT},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            manifest = response.read(MAX_MANIFEST_BYTES + 1)
            digest = response.headers.get("Docker-Content-Digest", "")
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise CatalogError(
            f"cannot resolve registry manifest for {repository}:{reference}"
        ) from error
    if len(manifest) > MAX_MANIFEST_BYTES:
        raise CatalogError(f"registry manifest is too large for {repository}:{reference}")
    if not DIGEST_RE.fullmatch(digest):
        raise CatalogError(
            f"registry returned an invalid digest for {repository}:{reference}"
        )
    computed = "sha256:" + hashlib.sha256(manifest).hexdigest()
    if digest != computed:
        raise CatalogError(f"registry digest mismatch for {repository}:{reference}")
    return digest
