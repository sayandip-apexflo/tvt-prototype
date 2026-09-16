"""Register bundled solution contracts against verified local registry images."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

import jsonschema
import yaml

from apexfabric.solution_management.catalog import SolutionCatalog, resolve_registry_digest

FILES = ('image-contract.yaml', 'desired-state.schema.json', 'desired-state.example.json')


def delivery(root: Path, entry: dict) -> tuple[Path, dict, str, str]:
    directory = (root / entry['directory']).resolve()
    if not directory.is_relative_to((root / 'solution-packs/catalog').resolve()):
        raise ValueError('Delivery must be inside solution-packs/catalog')
    contract = yaml.safe_load((directory / FILES[0]).read_text())
    schema = json.loads((directory / FILES[1]).read_text())
    example = json.loads((directory / FILES[2]).read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(example)
    SolutionCatalog._validate_ui_annotations(contract, schema)
    name, version = contract['name'], str(contract['version'])
    if not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*', name):
        raise ValueError('Invalid solution name')
    if entry['repository'] != 'apexfabric/' + name:
        raise ValueError('Repository must match the ApexFabric contract name')
    tag = f"{contract['hardwareProfile']}-{version}" if contract.get('hardwareProfile') else version
    return directory, contract, f'{name}:{version}', tag


def build_manifest(root: Path, selection: dict, inventory: dict) -> dict:
    images = {(item['repository'], item['tag']): item['digest'] for item in inventory['images']}
    entries = []
    for selected in selection['deliveries']:
        directory, _, catalog_id, tag = delivery(root, selected)
        digest = images.get((selected['repository'], tag))
        if not digest:
            raise ValueError(f'Bundled registry is missing {selected["repository"]}:{tag}')
        entries.append({**selected, 'catalog_id': catalog_id, 'tag': tag, 'digest': digest,
                        'files': {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in FILES}})
    manifest = {'version': 1, 'solutions': entries}
    validate_manifest(root, manifest)
    return manifest


def validate_manifest(root: Path, manifest: dict) -> list[tuple[dict, Path]]:
    if manifest.get('version') != 1 or not manifest.get('solutions'):
        raise ValueError('A non-empty version 1 catalog manifest is required')
    found, result = set(), []
    for entry in manifest['solutions']:
        directory, _, catalog_id, tag = delivery(root, entry)
        if catalog_id in found or catalog_id != entry['catalog_id'] or tag != entry['tag']:
            raise ValueError('Duplicate or inconsistent catalog identity')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', entry['digest']):
            raise ValueError('Invalid image digest')
        for name in FILES:
            if hashlib.sha256((directory / name).read_bytes()).hexdigest() != entry['files'].get(name):
                raise ValueError(f'Changed bundled contract/schema/example: {directory / name}')
        found.add(catalog_id)
        result.append((entry, directory))
    return result


def seed_manifest(catalog: SolutionCatalog, root: Path, manifest: dict, registry: str) -> None:
    validated = validate_manifest(root, manifest)
    for entry, directory in validated:
        catalog.seed_delivery(directory, registry, entry['repository'])


def register(catalog: SolutionCatalog, root: Path, manifest: dict, registry: str) -> list[dict]:
    # Validate the whole bundle and resolve every required image before writing rows.
    for entry, _ in validate_manifest(root, manifest):
        actual = resolve_registry_digest(registry, entry['repository'], entry['tag'])
        if actual != entry['digest']:
            raise ValueError(f'Image digest mismatch for {entry["catalog_id"]}')
    seed_manifest(catalog, root, manifest, registry)
    catalog.refresh()
    return verify(catalog, root, manifest, registry)


def verify(catalog: SolutionCatalog, root: Path, manifest: dict, registry: str) -> list[dict]:
    result = []
    for entry, directory in validate_manifest(root, manifest):
        row = catalog.get(entry['catalog_id'])
        if not row or row['status'] != 'available' or row['digest'] != entry['digest'] or row['registry'] != registry or row['repository'] != entry['repository']:
            raise ValueError(f'Included solution is not available as bundled: {entry["catalog_id"]}')
        if row['contract'] != yaml.safe_load((directory / FILES[0]).read_text()):
            raise ValueError(f'Installed contract differs: {entry["catalog_id"]}')
        for column, filename in [('desired_state_schema', FILES[1]), ('desired_state_example', FILES[2])]:
            if row[column] != json.loads((directory / filename).read_text()):
                raise ValueError(f'Installed {column} differs: {entry["catalog_id"]}')
        result.append({'catalog_id': row['catalog_id'], 'image': row['image'], 'status': row['status']})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'register', 'verify'])
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--selection', type=Path)
    parser.add_argument('--inventory', type=Path)
    parser.add_argument('--state-dir', type=Path, default=Path('/var/lib/apexfabric/control'))
    parser.add_argument('--registry', default='127.0.0.1:5000')
    args = parser.parse_args()
    if args.action == 'build':
        if not args.selection or not args.inventory:
            parser.error('build requires --selection and --inventory')
        manifest = build_manifest(args.root, json.loads(args.selection.read_text()), json.loads(args.inventory.read_text()))
        args.manifest.write_text(json.dumps(manifest, indent=2) + '\n')
        print('Included catalog entries:', *[x['catalog_id'] for x in manifest['solutions']], sep='\n')
    else:
        manifest = json.loads(args.manifest.read_text())
        catalog = SolutionCatalog(args.state_dir / 'catalog.sqlite3')
        result = (register if args.action == 'register' else verify)(catalog, args.root, manifest, args.registry)
        print(json.dumps({'solutions': result}, indent=2))


if __name__ == '__main__':
    main()
