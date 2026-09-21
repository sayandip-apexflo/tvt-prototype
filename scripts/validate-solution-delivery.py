#!/usr/bin/env python3
"""Fail closed when the catalog, configured pins, and OCI archive diverge."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import tarfile
from pathlib import Path

from tvt_edge.delivery_metadata import load_delivery_metadata


class DeliveryValidationError(ValueError):
    pass


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise DeliveryValidationError(f"invalid assignment at {path}:{number}")
        key, value = line.split("=", 1)
        fields = shlex.split(value, comments=False, posix=True)
        if len(fields) > 1:
            raise DeliveryValidationError(f"value must resolve to one field at {path}:{number}")
        values[key.strip()] = fields[0] if fields else ""
    return values


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def archive_tags(path: Path) -> list[str]:
    with tarfile.open(path, mode="r:*") as archive:
        names = {member.name for member in archive.getmembers()}
        if "index.json" in names and "oci-layout" in names:
            stream = archive.extractfile("index.json")
            if stream is None:
                raise DeliveryValidationError("OCI index.json cannot be read")
            index = json.load(stream)
            manifests = index.get("manifests") if isinstance(index, dict) else None
            if not isinstance(manifests, list) or len(manifests) != 1:
                raise DeliveryValidationError("OCI archive must contain one image manifest")
            descriptor = manifests[0]
            annotations = descriptor.get("annotations") if isinstance(descriptor, dict) else None
            tag = annotations.get("org.opencontainers.image.ref.name") if isinstance(annotations, dict) else None
            if not isinstance(tag, str) or not tag:
                raise DeliveryValidationError("OCI image has no reference-name annotation")
            return [tag]
        if "manifest.json" in names:
            stream = archive.extractfile("manifest.json")
            if stream is None:
                raise DeliveryValidationError("Docker manifest.json cannot be read")
            manifest = json.load(stream)
            if not isinstance(manifest, list):
                raise DeliveryValidationError("Docker manifest.json must be a list")
            return [tag for item in manifest for tag in item.get("RepoTags", [])]
    raise DeliveryValidationError("image archive is neither OCI-layout nor Docker-save format")


def validate(catalog: Path, config: Path, archive: Path) -> None:
    values = load_env(config)
    metadata = load_delivery_metadata(catalog)
    provenance = metadata["provenance"]
    pipeline = provenance.get("pipeline") or {}
    delivery = provenance.get("delivery") or {}
    archive_record = provenance.get("archive") or {}
    plan_compiler = (provenance.get("platform_compatibility") or {}).get(
        "plan_compiler"
    ) or {}
    archive_size = archive.stat().st_size
    archive_sha256 = sha256(archive)

    expected = {
        "catalog id": (metadata["catalog_id"], values.get("PIPELINE_TRAFFIC_CATALOG_ID")),
        "version": (metadata["version"], values.get("PIPELINE_TRAFFIC_VERSION")),
        "repository": (metadata["repository"], values.get("PIPELINE_TRAFFIC_LOCAL_REPOSITORY")),
        "tag": (metadata["tag"], values.get("PIPELINE_TRAFFIC_LOCAL_TAG")),
        "archive filename": (archive.name, values.get("PIPELINE_TRAFFIC_ARCHIVE")),
        "archive provenance filename": (archive_record.get("filename"), archive.name),
        "archive size": (archive_size, int(values.get("PIPELINE_TRAFFIC_ARCHIVE_SIZE", "0"))),
        "archive provenance size": (archive_record.get("size"), archive_size),
        "archive sha256": (archive_sha256, values.get("PIPELINE_TRAFFIC_ARCHIVE_SHA256")),
        "archive provenance sha256": (archive_record.get("sha256"), archive_sha256),
        "archive image": (archive_record.get("loaded_image"), values.get("PIPELINE_TRAFFIC_ARCHIVE_IMAGE")),
        "delivery directory": (delivery.get("directory"), values.get("PIPELINE_TRAFFIC_DELIVERY_DIR")),
        "source commit": (pipeline.get("commit"), values.get("PIPELINE_REVISION")),
        "plan compiler compatibility": (
            plan_compiler.get("id"),
            values.get("PIPELINE_TRAFFIC_PLAN_COMPILER_COMPATIBILITY_ID"),
        ),
        "plan compiler checksum": (
            plan_compiler.get("sha256"),
            values.get("PIPELINE_TRAFFIC_PLAN_COMPILER_SHA256"),
        ),
    }
    configured_repository = values.get("PIPELINE_REPOSITORY", "").removesuffix(".git")
    expected["source repository"] = (pipeline.get("repository"), configured_repository)
    for label, (actual, wanted) in expected.items():
        if actual != wanted:
            raise DeliveryValidationError(
                f"{label} mismatch: catalog/archive has {actual!r}, configuration requires {wanted!r}"
            )
    tags = archive_tags(archive)
    if tags != [values["PIPELINE_TRAFFIC_ARCHIVE_IMAGE"]]:
        raise DeliveryValidationError(
            f"archive image tag mismatch: found {tags!r}, expected {[values['PIPELINE_TRAFFIC_ARCHIVE_IMAGE']]!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args()
    try:
        validate(args.catalog, args.config, args.archive)
    except (DeliveryValidationError, OSError, ValueError, tarfile.TarError, json.JSONDecodeError) as error:
        parser.exit(1, f"solution-delivery: ERROR: {error}\n")
    print(f"Verified solution delivery {args.catalog.name} against {args.archive.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
