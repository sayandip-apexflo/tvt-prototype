#!/usr/bin/env python3
"""Export only ApexFabric repositories and their referenced blobs, without stopping Docker."""
import argparse
import hashlib
import io
import json
import os
import tempfile
from pathlib import Path
import re
import subprocess
import tarfile

BASE = 'docker/registry/v2'
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')


def blob_path(digest):
    if not DIGEST.fullmatch(digest):
        raise ValueError(f'Unsupported digest: {digest}')
    value = digest.split(':')[1]
    return f'{BASE}/blobs/sha256/{value[:2]}/{value}/data'


def inspect_repositories(data):
    digests, manifests, images = set(), set(), []
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith('/link'):
                continue
            value = archive.extractfile(member).read().decode().strip()
            blob_path(value)
            if '/_manifests/tags/' in member.name and '/current/link' in member.name:
                repo, tag = member.name.split('/repositories/', 1)[1].split('/_manifests/tags/')
                if not repo.startswith('apexfabric/'):
                    raise ValueError('Non-ApexFabric repository in export')
                digests.add(value)
                manifests.add(value)
                images.append({'repository': repo, 'tag': tag.split('/')[0], 'digest': value})
    return digests, manifests, images


def append_live_metadata(path, repositories, digests):
    """Copy only metadata whose blob is reachable from a current tag.

    Registry layer links and historical revisions can survive garbage collection.
    They are not roots of the image graph and must not require obsolete blobs.
    """
    with tarfile.open(fileobj=io.BytesIO(repositories)) as source, tarfile.open(path, 'a') as target:
        for member in source:
            if not member.isfile() or not member.name.endswith('/link'):
                continue
            data = source.extractfile(member).read()
            if data.decode().strip() in digests:
                target.addfile(member, io.BytesIO(data))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default='apexfabric-dev-registry')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() or args.out.with_suffix('.json').exists():
        parser.error('Output already exists; use a new release directory')
    command = ['docker', 'exec', args.container]
    repositories = subprocess.check_output(command + ['tar', '-C', '/var/lib/registry', '-cf', '-', f'{BASE}/repositories/apexfabric'])
    digests, pending, images = inspect_repositories(repositories)
    visited = set()
    while pending:
        digest = pending.pop()
        if digest in visited:
            continue
        visited.add(digest)
        data = subprocess.check_output(command + ['cat', '/var/lib/registry/' + blob_path(digest)])
        if hashlib.sha256(data).hexdigest() != digest.split(':')[1]:
            raise ValueError(f'Corrupt manifest {digest}')
        manifest = json.loads(data)
        for descriptor in manifest.get('manifests', []):
            pending.add(descriptor['digest'])
            digests.add(descriptor['digest'])
        for descriptor in manifest.get('layers', []) + ([manifest['config']] if 'config' in manifest else []):
            blob_path(descriptor['digest'])
            digests.add(descriptor['digest'])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Final names are published only after tar and checksum generation succeed.
    # If interrupted between the two renames, prepare can recover the inventory
    # from the complete tar; a partial tar never appears under the final name.
    inventory_path = args.out.with_suffix('.json')
    with tempfile.TemporaryDirectory(prefix=args.out.name + '.partial-', dir=args.out.parent) as work:
        temporary_tar = Path(work) / 'registry.tar'
        temporary_json = Path(work) / 'registry.json'
        print(f'Exporting {len(images)} tags and {len(digests)} blobs; writing temporary archive…', flush=True)
        with temporary_tar.open('xb') as output:
            subprocess.run(command + ['tar', '-C', '/var/lib/registry', '-cf', '-', *[blob_path(d) for d in sorted(digests)]], stdout=output, check=True)
            output.flush()
            os.fsync(output.fileno())
        append_live_metadata(temporary_tar, repositories, digests)
        with temporary_tar.open('rb') as stream:
            os.fsync(stream.fileno())
        print('Archive written. Calculating checksum…', flush=True)
        with temporary_tar.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        temporary_json.write_text(json.dumps({'sha256': checksum, 'images': sorted(images, key=lambda x: (x['repository'], x['tag'])), 'blob_count': len(digests)}, indent=2) + '\n')
        os.replace(temporary_tar, args.out)
        os.replace(temporary_json, inventory_path)
    print(f'Exported {len(images)} ApexFabric tags, {len(digests)} blobs to {args.out}', flush=True)


if __name__ == '__main__':
    main()
