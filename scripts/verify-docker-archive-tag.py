#!/usr/bin/env python3
"""Verify that a Docker image archive contains exactly the expected tag."""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
from pathlib import Path


MAX_MANIFEST_SIZE = 4 * 1024 * 1024


class ArchiveTagError(ValueError):
    pass


def archive_tags(path: Path) -> list[str]:
    try:
        with tarfile.open(path, mode="r:*") as archive:
            manifests = [
                member
                for member in archive.getmembers()
                if member.name == "manifest.json"
            ]
            if len(manifests) != 1:
                raise ArchiveTagError("archive must contain exactly one manifest.json")
            manifest = manifests[0]
            if not manifest.isfile() or manifest.size > MAX_MANIFEST_SIZE:
                raise ArchiveTagError("archive manifest.json is not a bounded regular file")
            stream = archive.extractfile(manifest)
            if stream is None:
                raise ArchiveTagError("archive manifest.json cannot be read")
            document = json.load(stream)
    except (OSError, tarfile.TarError, json.JSONDecodeError) as error:
        raise ArchiveTagError(f"invalid Docker archive: {error}") from error

    if not isinstance(document, list) or not document:
        raise ArchiveTagError("archive manifest.json must contain a non-empty list")
    tags: list[str] = []
    for entry in document:
        if not isinstance(entry, dict) or not isinstance(entry.get("RepoTags"), list):
            raise ArchiveTagError("archive manifest entry has no RepoTags list")
        if not all(isinstance(tag, str) and tag for tag in entry["RepoTags"]):
            raise ArchiveTagError("archive manifest contains an invalid image tag")
        tags.extend(entry["RepoTags"])
    return tags


def verify(path: Path, expected: str) -> None:
    tags = archive_tags(path)
    if tags != [expected]:
        rendered = ", ".join(tags) if tags else "none"
        raise ArchiveTagError(
            f"archive image tag mismatch: found [{rendered}], expected [{expected}]"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--expected", required=True)
    args = parser.parse_args()
    try:
        verify(args.archive, args.expected)
    except ArchiveTagError as error:
        print(f"docker-archive-tag: ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Verified Docker archive image tag: {args.expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
