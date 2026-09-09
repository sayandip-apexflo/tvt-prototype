# TVT edge release build runbook

This runbook defines the scripted process for producing a self-contained,
versioned TVT edge installation package while no CI release pipeline exists.
It also defines how to rebuild that package after changes are pushed to GitHub.

The Git repository is a development input. An edge release is a separate,
immutable deliverable assembled from one selected Git commit plus reviewed
binary artifacts. Pushing a commit does not create or update a release package
automatically.

## Roles and locations

Assign one person as the release owner for each build. Build on a trusted
Ubuntu 24.04 `amd64` workstation or disposable VM, not on a production edge
host. The machine needs network access, Python 3.12, Node/NPM, Docker, Git LFS,
and enough disk for multiple copies of the approximately 1.93 GB Traffic image
and the remaining image/package closures.

Keep these areas separate:

```text
/srv/tvt-release/source/       selected clean Git checkout
/srv/tvt-release/inputs/       reviewed release inputs; not committed
/srv/tvt-release/output/       generated release directories and archives
```

Do not place credentials, camera URLs, private keys, site configuration, or
runtime data in any release directory.

## One-time preparation

Install build tooling:

```bash
sudo apt-get update
sudo apt-get install -y \
  ca-certificates curl git git-lfs jq openssl \
  python3 python3-venv python3-pip \
  nodejs npm docker.io
sudo systemctl enable --now docker.service
git lfs install
```

The release command creates the input library automatically with this layout:

```text
inputs/
├── images/
│   ├── registry.tar
│   ├── node-reporter.tar
│   ├── node-status-controller.tar
│   └── traffic-edge-runtime-v4.tar
├── k3s/
│   ├── install.sh
│   └── k3s
├── hardware/
│   ├── driver-recipe.json
│   ├── linux-npu-driver.tar.gz
│   ├── wheels/
│   └── voyager-wheels/        # only when recipe voyager.enabled is true
└── apt/
    └── *.deb
```

The `apt/` directory contains the complete Ubuntu 24.04 `amd64` dependency
closure, not merely the top-level packages. The hardware recipe records the
build host's qualified kernel and whether an Axelera device was detected. The
K3s installer/binary, Registry image, Traffic image, Intel NPU release, and
Ubuntu build container are selected from the pins under `config/`.

Do not add files manually to an automatically generated input tree. The input
lock binds every artifact byte, configuration pin, version, and source commit.

## Automated input build

The release front door owns input acquisition. When the selected input directory
does not exist or is empty, it automatically:

1. pulls and verifies the digest-pinned Registry image;
2. builds both node-management images for Linux amd64;
3. downloads K3s and validates its official amd64 checksum and reported version;
4. fetches the exact Traffic Git LFS object and validates its configured size,
   checksum, and image tag;
5. downloads the configured Intel NPU release and the Python 3.12 OpenVINO
   wheel closure;
6. detects Axelera PCI hardware and, when present, includes the pinned Metis
   1.4.17 and Voyager 1.6.1 closures; and
7. resolves the complete Ubuntu 24.04 amd64 Debian dependency closure inside
   the digest-pinned Ubuntu build container.

Artifacts are first written to a temporary sibling directory, validated, and
locked. The complete input tree is published atomically only after validation
passes. A sibling `cache/` retains K3s, PIPELINE Git LFS, and Intel NPU downloads
for subsequent releases. The cache is never copied into the release.

An existing locked input tree is treated as immutable and verified before use.
The builder refuses to replace a non-empty unlocked tree. The legacy
`--create-input-lock` option remains available only when a release owner
intentionally supplies and accepts a manually populated tree.

The front door delegates build, installation, and verification work to
subcommands in the single `scripts/tvt-edge-operations.sh` dispatcher. That
same dispatcher is copied into the offline bundle and used by the host
entrypoints and persistent Traffic synchronization service.

## Build the first release

### 1. Select an immutable source revision

```bash
cd /srv/tvt-release/source
git clone git@github.com:sayandip-apexflo/tvt-prototype.git
cd tvt-prototype
git fetch --tags origin
git checkout --detach <approved-commit-sha>
test -z "$(git status --porcelain)"
git rev-parse HEAD
```

Save the full commit SHA in the release record. Do not build from an
unrecorded branch tip or a dirty worktree.

### 2. Assign the release version

`tvt_edge.__version__` is the canonical application version. The Python build
reads it dynamically; API metrics and alert-receiver metrics import it; and the
version script keeps the UI package metadata and release manifest template in
sync. The current version is `0.1.0`.

Check the version surfaces before building:

```bash
python3 scripts/tvt-version.py --check --expected 0.1.0
```

For a later release, update all derived surfaces in one operation, review the
diff, and rerun the check:

```bash
python3 scripts/tvt-version.py --set 0.1.1
git diff -- tvt_edge/__init__.py ui/package.json ui/package-lock.json \
  release/manifest.template.json
python3 scripts/tvt-version.py --check --expected 0.1.1
```

`NODE_MANAGEMENT_IMAGE_VERSION` is an independent artifact pin. Change it only
when either node-management image changes. Likewise, do not change Traffic,
K3s, schema, or credential-format versions just to match the application.

Create a release branch at the approved functional commit, make the version
changes, and commit them:

```bash
git switch -c release/0.1.0 <approved-commit-sha>
# Run scripts/tvt-version.py --set 0.1.0, then:
git add release/manifest.template.json ui/package.json ui/package-lock.json \
  tvt_edge/__init__.py
git commit -m 'chore: prepare TVT edge release 0.1.0'
test -z "$(git status --porcelain)"
git rev-parse HEAD
```

The resulting release commit—not its pre-version-bump parent—is the SHA to put
in the release record and annotated tag. The package version and release
directory name must match.

### 3. Run source tests

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
npm --prefix ui ci
npm --prefix ui test -- --run
bash -n prepare-tvt-edge-host.sh install-tvt-edge-host.sh scripts/*.sh scripts/lib/*.sh
git diff --check
```

Resolve every failure before producing a release candidate.

### 4. Generate the complete installation package

Choose a new, empty input path and output path. Never overwrite an earlier
release. This is the only release-build command:

```bash
cd /srv/tvt-release/source/tvt-prototype

./scripts/make-tvt-edge-release.sh \
  --input-directory /srv/tvt-release/inputs \
  --output-directory /srv/tvt-release/output/tvt-edge-release-0.1.0 \
  --archive-directory /srv/tvt-release/output \
  --version 0.1.0 \
  --source-commit "$(git rev-parse HEAD)"
```

The front-door script creates the workspace when necessary (using a narrowly
scoped sudo directory creation if `/srv` is not writable), builds and locks all
inputs, runs Python and UI tests, invokes the lower-level assembler, and
independently verifies the result. It refuses a dirty source tree, mismatched
version or commit, changed locked inputs, wrong output name, non-empty output
directory, or an existing archive/report. It creates:

```text
tvt-edge-release-0.1.0/                     verified release directory
tvt-edge-release-0.1.0.tar.gz               reproducible transport archive
tvt-edge-release-0.1.0.tar.gz.sha256        archive checksum
tvt-edge-release-0.1.0.release-report.json  non-secret build evidence
```

The transport file is a GNU/POSIX tar stream compressed with deterministic
gzip (`gzip -n`). Its members are name-sorted, use numeric root ownership, and
share the selected source commit timestamp so identical inputs produce
identical archive bytes. It contains one top-level
`tvt-edge-release-<version>/` directory.

The output directory contains the installers, manifest, input lock, all
offline artifacts, runtime resources, Python wheels, and
`checksums.sha256`. Use `--create-input-lock` only when intentionally accepting
a manually populated input library. `--skip-tests` and `--allow-dirty-source` are
development escape hatches; the release report records their use, and their
outputs must not be published as production releases.

### 5. Verify the assembled directory independently

```bash
release_dir=/srv/tvt-release/output/tvt-edge-release-0.1.0

./scripts/tvt-edge-operations.sh verify-release --bundle "${release_dir}"

python3 -m json.tool "${release_dir}/manifest.json"
python3 -m json.tool \
  /srv/tvt-release/output/tvt-edge-release-0.1.0.release-report.json
```

The verifier checks all bundle checksums, structure and permissions, manifest
identity, input-lock identity and coverage, application-wheel metadata, built
UI version, locked external bytes, and the bundled configuration hashes.
Also review the file inventory and confirm that no credential-like files were
included:

```bash
find "${release_dir}" -type f -printf '%P\n' | sort
find "${release_dir}" -type l -print
```

The second command must print nothing.

### 6. Approve the transport archive

The generation script already creates the archive and its checksum. Verify the
transport file before publication:

```bash
cd /srv/tvt-release/output
sha256sum --check tvt-edge-release-0.1.0.tar.gz.sha256
```

Sign the archive or its checksum using the organization's approved signing
method when one is available. The bundle's internal checksums detect
corruption, while a trusted signature proves who published it.

### 7. Record and publish the release

The generated release report captures the release identity, build time, test
gate result, archive hash, bundle-checksum hash, input-lock hash, and whether a
dirty source was permitted. Supplement it with the remaining approval data.
The complete release record must include:

- release version;
- full Git commit SHA;
- build date and release owner;
- supported OS, architecture, kernel, and hardware profile;
- external input names, origins, versions, and SHA-256 values;
- application, K3s, node-management, and Traffic versions;
- source/UI test results;
- bundle and transport-archive SHA-256 values; and
- qualification results from the clean-host rehearsal.

Create an annotated Git tag only after selecting the release commit:

```bash
git tag -a v0.1.0 <release-commit-sha> -m 'TVT edge release 0.1.0'
git push origin v0.1.0
```

Upload the archive, archive checksum, signature, and release record to the
approved artifact location. This may be a GitHub Release if its policy and file
limits are suitable, or a controlled file/object store. Do not commit large
binary artifacts to the normal Git history.

### 8. Rehearse on a clean host

Extract the archive on a clean supported Intel edge box and follow the normal
two-command procedure around the required reboot. The installer performs the
post-reboot preparation verification before it starts application installation:

```bash
sudo ./prepare-tvt-edge-host.sh --bundle "$PWD" --mode offline
sudo reboot
sudo ./install-tvt-edge-host.sh --bundle "$PWD" \
  --site-config /secure/site.yaml --prepare-mode offline
```

Preserve `/var/lib/tvt/install/installation-report.json` as release evidence.
Do not publish the release as qualified merely because it built successfully.

## Rebuild after a GitHub commit

A push to GitHub is an input event, not a release event. For each desired
release, the release owner performs the following process.

### 1. Choose the candidate commit

```bash
cd /srv/tvt-release/source/tvt-prototype
git fetch origin
git checkout --detach <new-approved-commit-sha>
test -z "$(git status --porcelain)"
git log -1 --format='%H %cI %s'
```

Review every change since the last release:

```bash
git diff --stat v0.1.0..<new-approved-commit-sha>
git diff --name-status v0.1.0..<new-approved-commit-sha>
```

Create a new release branch from that candidate before making and committing
the new version changes; the final release commit must have a clean worktree.

### 2. Select a new version

Never rebuild different content under an existing version. Assign a new patch,
minor, or major version with `scripts/tvt-version.py --set` and commit the
resulting files. For example, code fixes after `0.1.0` normally become `0.1.1`.
The existing `0.1.0` package remains immutable.

### 3. Review which generated inputs will change

Use this impact matrix:

| Changed paths or pins | Required rebuild |
|---|---|
| Python, UI, installer scripts, templates, migrations, or Solution Pack schemas | Rebuild the release directory and application wheel. Cached immutable downloads may be reused. |
| `pyproject.toml` dependencies | The release builder re-resolves the application wheel closure. |
| `ui/` | Rebuild the UI before building the application wheel. |
| `apexfabric/node_management/reporter/` | Bump the node-management image version; the builder rebuilds `node-reporter.tar`. |
| `apexfabric/node_management/status_controller/` | Bump the node-management image version; the builder rebuilds `node-status-controller.tar`. |
| Traffic revision, image contract, models, schemas, or `config/pipeline.env` | Update every Traffic pin; the builder fetches and verifies the matching archive. |
| K3s pin or installation flags | Update the K3s pin; the builder downloads and verifies the new pair. |
| Hardware recipe, Intel packages, OpenVINO pins, or qualified kernel | The builder regenerates the hardware, wheel, and offline APT closures. |
| Host package list in `prepare-tvt-edge-host.sh` | The builder regenerates the complete offline APT closure. |
| Documentation only | A new package is optional unless policy requires one package per commit. Never silently replace an already published package. |

Use a new empty input path for every release identity. The shared cache avoids
downloading unchanged immutable payloads, while the builder regenerates and
relocks the complete published input tree.

### 4. Repeat the complete release gates

For the new version, run `make-tvt-edge-release.sh` with a new input path, new
version, and new output directory. The script builds and locks the inputs,
performs source tests, assembly, bundle verification, and archive/report
generation. Then test on a clean host, sign the checksum, create a new
annotated Git tag, and publish without deleting the prior release.

Do not copy the previous release directory and edit it in place. Always invoke
the builder from a clean selected source revision.

## Current limitations

- Artifact acquisition and package generation are automated, but final
  qualification still requires the clean supported-hardware rehearsal.
- Internal SHA-256 coverage is implemented, but publisher signing is an
  organizational step.
- The host installer supports clean installation and same-version resume. It
  currently refuses an in-place upgrade when `install-state.json` belongs to a
  different release version. Building `0.1.1` therefore does not by itself add
  an upgrade path from an installed `0.1.0` host.

Future release work should add signed publisher provenance and an automated
clean-host qualification test. CI can later execute these same scripts rather
than defining a different release process.
