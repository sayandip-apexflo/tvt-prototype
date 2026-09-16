#!/usr/bin/env python3
"""Validate an ApexFabric-only registry archive before importing it as root."""
import argparse
import hashlib
import os
import tempfile
import json
from pathlib import Path, PurePosixPath
import re
import tarfile


def inspect_archive(archive_path):
    """Check every blob and reference before deriving an inventory from the tar."""
    blobs, references, tags = set(), set(), set()
    with tarfile.open(archive_path, 'r|') as archive:
        for item in archive:
            path = PurePosixPath(item.name)
            if path.is_absolute() or '..' in path.parts or not (item.isdir() or item.isfile()):
                raise ValueError(f'Unsafe archive member: {item.name}')
            repo_prefix = 'docker/registry/v2/repositories/apexfabric'
            if item.name == repo_prefix or item.name.startswith(repo_prefix + '/'):
                if item.isfile():
                    if not item.name.endswith('/link') or item.size > 128:
                        raise ValueError(f'Unexpected repository metadata: {item.name}')
                    digest = archive.extractfile(item).read().decode().strip()
                    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
                        raise ValueError('Invalid metadata digest')
                    references.add(digest)
                    if '/_manifests/tags/' in item.name and item.name.endswith('/current/link'):
                        repo, tag = item.name.split('/repositories/')[1].split('/_manifests/tags/')
                        tags.add((repo, tag.split('/')[0], digest))
                continue
            match = re.fullmatch(r'docker/registry/v2/blobs/sha256/([0-9a-f]{2})/([0-9a-f]{64})/data', item.name)
            if not match or not item.isfile() or match[1] != match[2][:2]:
                raise ValueError(f'Non-ApexFabric archive path: {item.name}')
            digest = hashlib.sha256()
            small = bytearray() if item.size < 4 * 1024 * 1024 else None
            reader = archive.extractfile(item)
            while chunk := reader.read(1024 * 1024):
                digest.update(chunk)
                if small is not None:
                    small.extend(chunk)
            if digest.hexdigest() != match[2]:
                raise ValueError(f'Corrupt image blob: {item.name}')
            blobs.add('sha256:' + match[2])
            if small is not None:
                try:
                    data = json.loads(small)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(data, dict) and data.get('schemaVersion') == 2:
                    descriptors = data.get('manifests', []) + data.get('layers', [])
                    if 'config' in data:
                        descriptors.append(data['config'])
                    references.update(d['digest'] for d in descriptors)
    if not tags or references - blobs:
        raise ValueError('Registry inventory or referenced image blobs are incomplete')
    return {
        'images': [{'repository': repo, 'tag': tag, 'digest': digest}
                   for repo, tag, digest in sorted(tags)],
        'blob_count': len(blobs),
    }


def archive_checksum(archive_path):
    with open(archive_path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify(archive_path, inventory_path):
    inventory = json.loads(Path(inventory_path).read_text())
    if archive_checksum(archive_path) != inventory['sha256']:
        raise ValueError('Registry archive checksum mismatch')
    actual = inspect_archive(archive_path)
    expected = {(x['repository'], x['tag'], x['digest']) for x in inventory['images']}
    observed = {(x['repository'], x['tag'], x['digest']) for x in actual['images']}
    if expected != observed:
        raise ValueError('Registry inventory does not match archived tags')
    print(f"Validated {len(observed)} ApexFabric tags and {actual['blob_count']} blobs; no Smart City repositories.", flush=True)


def recover_inventory(archive_path, inventory_path):
    """Recover missing metadata only when all archived images pass validation."""
    inventory_path = Path(inventory_path)
    if inventory_path.exists():
        raise ValueError('Inventory already exists; refusing to overwrite it')
    before = Path(archive_path).stat()
    print('Inventory missing. Checking every archived image layer before recovery…', flush=True)
    inventory = inspect_archive(archive_path)
    inventory['sha256'] = archive_checksum(archive_path)
    after = Path(archive_path).stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('Archive changed during recovery; stop the writer and retry')
    fd, temporary = tempfile.mkstemp(prefix=inventory_path.name + '.partial-', dir=inventory_path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(inventory, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Publish without overwriting an inventory created concurrently.
        os.link(temporary, inventory_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(f"Recovered {inventory_path}: {len(inventory['images'])} tags, {inventory['blob_count']} verified blobs.", flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recover-inventory', action='store_true')
    parser.add_argument('archive', type=Path)
    parser.add_argument('inventory', type=Path)
    args = parser.parse_args()
    try:
        (recover_inventory if args.recover_inventory else verify)(args.archive, args.inventory)
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f'Registry validation failed: {error}\n')
