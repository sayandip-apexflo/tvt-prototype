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

Create an input library owned by the release operator:

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
│   └── wheels/
└── apt/
    └── *.deb
```

The `apt/` directory must contain the complete Ubuntu 24.04 `amd64`
dependency closure, not merely the top-level packages. The hardware directory
must come from a qualified Intel 285H preparation run and must match the
qualified kernel. The K3s installer/binary, registry image, and Traffic image
must match the pins under `config/`. Record the upstream URL, version, and
SHA-256 for every externally acquired artifact outside the Git repository.

Never substitute an artifact just because it has the expected filename. Image
archives must contain the exact tags expected by `config/platform.env` and
`config/pipeline.env`.

## Produce the input library

This is a separate acquisition step because the release builder deliberately
does not download privileged binaries or invent provenance.

### Registry archive

Pull the configured amd64 manifest, retain its configured tag, and save it:

```bash
mkdir -p /srv/tvt-release/inputs/images
set -a
source config/platform.env
set +a
docker pull --platform linux/amd64 "${LOCAL_REGISTRY_IMAGE}"
docker tag "${LOCAL_REGISTRY_IMAGE}" "${LOCAL_REGISTRY_IMAGE%%@*}"
docker save --output /srv/tvt-release/inputs/images/registry.tar \
  "${LOCAL_REGISTRY_IMAGE%%@*}"
```

Record both the configured registry digest and the resulting archive digest.

### Node-management archives

Build these only when their source or build inputs change. The unqualified
source tags are part of the archive contract consumed by the edge importer:

```bash
set -a
source config/platform.env
set +a

docker build --pull=false --provenance=false \
  -f apexfabric/node_management/reporter/Dockerfile \
  -t "apexfabric/node-reporter:${NODE_MANAGEMENT_IMAGE_VERSION}" .
docker save --output /srv/tvt-release/inputs/images/node-reporter.tar \
  "apexfabric/node-reporter:${NODE_MANAGEMENT_IMAGE_VERSION}"

docker build --pull=false --provenance=false \
  -f apexfabric/node_management/status_controller/Dockerfile \
  -t "apexfabric/node-status-controller:${NODE_MANAGEMENT_IMAGE_VERSION}" .
docker save --output /srv/tvt-release/inputs/images/node-status-controller.tar \
  "apexfabric/node-status-controller:${NODE_MANAGEMENT_IMAGE_VERSION}"
```

Inspect both images and confirm `Architecture` is `amd64` before accepting
them.

### Traffic archive

Obtain the exact Git LFS object named by `PIPELINE_TRAFFIC_ARCHIVE` from the
exact `PIPELINE_REVISION`. Do not use the current branch tip or rebuild from
mutable upstream dependencies for a production package. Copy the resulting
archive to `inputs/images/traffic-edge-runtime-v4.tar`, then verify its byte
size and SHA-256 against `config/pipeline.env`. The edge importer subsequently
checks image architecture, OCI labels, runtime command/user/port, baked model
files, and model hashes before publishing it locally.

### K3s inputs

Obtain the installer and Linux amd64 binary for the exact `K3S_VERSION` from
the approved K3s distribution source. Keep the upstream filenames and hashes
in the provenance record, review the installer, make both files executable,
and confirm the binary reports the configured version. Do not use an
unrecorded current installer response as a reusable release input.

### Hardware closure

Generate the hardware recipe on a disposable or designated Ubuntu 24.04 Intel
285H qualification host using the online hardware installer. After it resolves
and validates the closure, collect:

```text
/var/lib/tvt/hardware-driver-recipe.json
/var/cache/tvt/hardware-drivers/linux-npu-driver.tar.gz
/var/cache/tvt/hardware-drivers/wheels/
```

Copy these into `inputs/hardware/` using the names in the input layout. The
recipe records the kernel used during resolution. Do not reuse it for a
different qualified-kernel policy without repeating hardware qualification.

### Offline APT closure

Resolve packages on a clean Ubuntu 24.04 amd64 VM using the same repositories
and package pins as the target. Include the preparation packages plus every
package/version listed in `hardware/driver-recipe.json`, along with all
transitive `.deb` dependencies. Copy the resulting packages into
`inputs/apt/`.

A directory listing and successful checksum do not prove that the APT closure
is complete. The acceptance test is an installation in a clean VM with its
network disabled and only this package directory available. Preserve the
repository metadata and acquisition commands in the release record so the
closure can be regenerated.

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

### 4. Lock and validate the release inputs

Compare the input artifacts with their approved provenance record. At minimum:

```bash
sha256sum /srv/tvt-release/inputs/images/*.tar
sha256sum /srv/tvt-release/inputs/k3s/*
sha256sum /srv/tvt-release/inputs/hardware/driver-recipe.json
sha256sum /srv/tvt-release/inputs/hardware/linux-npu-driver.tar.gz
sha256sum /srv/tvt-release/inputs/hardware/wheels/*
sha256sum /srv/tvt-release/inputs/apt/*.deb
```

Verify that the K3s binary reports the pinned version and that the executable
inputs have not been modified since review:

```bash
/srv/tvt-release/inputs/k3s/k3s --version
grep '^K3S_VERSION=' config/platform.env
```

Traffic archive size and SHA-256 must equal `PIPELINE_TRAFFIC_ARCHIVE_SIZE` and
`PIPELINE_TRAFFIC_ARCHIVE_SHA256` in `config/pipeline.env`.

After the operator has reviewed the input library, create its immutable lock.
This verifies the required layout, configuration pins, Traffic size and hash,
hardware recipe and wheel closure, executable K3s files, and hashes every
accepted input. It rejects symlinks, missing files, and unexpected files:

```bash
source_commit="$(git rev-parse HEAD)"
python3 scripts/tvt-release-inputs.py create \
  --input-directory /srv/tvt-release/inputs \
  --output /srv/tvt-release/inputs/release-inputs.lock.json \
  --release-version 0.1.0 \
  --source-commit "${source_commit}" \
  --platform-config config/platform.env \
  --pipeline-config config/pipeline.env
```

Creation of a lock is an explicit acceptance action. Subsequent builds verify
the library against it and fail if any byte or relevant configuration changed.
To check it without building:

```bash
python3 scripts/tvt-release-inputs.py verify \
  --input-directory /srv/tvt-release/inputs \
  --lock /srv/tvt-release/inputs/release-inputs.lock.json \
  --release-version 0.1.0 \
  --source-commit "$(git rev-parse HEAD)" \
  --platform-config config/platform.env \
  --pipeline-config config/pipeline.env
```

### 5. Generate the complete installation package

Choose a new, empty output path. Never overwrite an earlier release:

```bash
cd /srv/tvt-release/source/tvt-prototype

./scripts/make-tvt-edge-release.sh \
  --input-directory /srv/tvt-release/inputs \
  --output-directory /srv/tvt-release/output/tvt-edge-release-0.1.0 \
  --archive-directory /srv/tvt-release/output \
  --version 0.1.0 \
  --source-commit "$(git rev-parse HEAD)"
```

The front-door script refuses a dirty source tree, mismatched version or
commit, changed input library, wrong output name, non-empty output directory,
or an existing archive/report. It runs Python and UI tests, syntax-checks the
shell entry points, invokes the lower-level assembler, independently verifies
the result, and creates:

```text
tvt-edge-release-0.1.0/                     verified release directory
tvt-edge-release-0.1.0.tar.gz               reproducible transport archive
tvt-edge-release-0.1.0.tar.gz.sha256        archive checksum
tvt-edge-release-0.1.0.release-report.json  non-secret build evidence
```

The output directory contains the installers, manifest, input lock, all
offline artifacts, runtime resources, Python wheels, and
`checksums.sha256`. Use `--create-input-lock` only when intentionally accepting
a newly reviewed input library. `--skip-tests` and `--allow-dirty-source` are
development escape hatches; the release report records their use, and their
outputs must not be published as production releases.

### 6. Verify the assembled directory independently

```bash
release_dir=/srv/tvt-release/output/tvt-edge-release-0.1.0

./scripts/verify-tvt-edge-release.sh --bundle "${release_dir}"

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

### 7. Approve the transport archive

The generation script already creates the archive and its checksum. Verify the
transport file before publication:

```bash
cd /srv/tvt-release/output
sha256sum --check tvt-edge-release-0.1.0.tar.gz.sha256
```

Sign the archive or its checksum using the organization's approved signing
method when one is available. The bundle's internal checksums detect
corruption, while a trusted signature proves who published it.

### 8. Record and publish the release

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

### 9. Rehearse on a clean host

Extract the archive on a clean supported Intel edge box and follow the normal
three-command procedure:

```bash
sudo ./prepare-tvt-edge-host.sh --bundle "$PWD" --mode offline
sudo reboot
sudo ./prepare-tvt-edge-host.sh --bundle "$PWD" --mode offline
sudo ./install-tvt-edge-host.sh --bundle "$PWD" --site-config /secure/site.yaml
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

### 3. Decide which inputs must change

Use this impact matrix:

| Changed paths or pins | Required rebuild |
|---|---|
| Python, UI, installer scripts, templates, migrations, or Solution Pack schemas | Rebuild the release directory and application wheel. Unchanged reviewed external inputs may be reused. |
| `pyproject.toml` dependencies | Re-resolve and include the complete Python wheel closure. |
| `ui/` | Rebuild the UI before building the application wheel. |
| `apexfabric/node_management/reporter/` | Bump the node-management image version and rebuild `node-reporter.tar`. |
| `apexfabric/node_management/status_controller/` | Bump the node-management image version and rebuild `node-status-controller.tar`. |
| Traffic revision, image contract, models, schemas, or `config/pipeline.env` | Obtain/rebuild the matching Traffic archive and update every Traffic checksum and provenance field. |
| K3s pin or installation flags | Review and replace the K3s installer/binary pair. |
| Hardware recipe, Intel packages, OpenVINO pins, or qualified kernel | Regenerate the hardware closure and the affected offline APT closure on the qualified platform. |
| Host package list in `prepare-tvt-edge-host.sh` | Regenerate and validate the complete offline APT closure. |
| Documentation only | A new package is optional unless policy requires one package per commit. Never silently replace an already published package. |

When uncertain, rebuild the affected artifact rather than carrying it forward.
Record every reused artifact and its unchanged SHA-256 in the new release
record.

### 4. Repeat the complete release gates

For the new version, create a new input lock tied to the final release commit,
then run `make-tvt-edge-release.sh` with the new version and output directory.
The script performs source tests, input validation, assembly, bundle
verification, and archive/report generation. Then test on a clean host, sign
the checksum, create a new annotated Git tag, and publish without deleting the
prior release.

Do not copy the previous release directory and edit it in place. Always invoke
the builder from a clean selected source revision.

## Current limitations

- Package generation is scripted, but it assembles supplied K3s, image,
  hardware, and APT artifacts; it does not
  acquire or independently qualify them.
- Internal SHA-256 coverage is implemented, but publisher signing is an
  organizational step.
- The host installer supports clean installation and same-version resume. It
  currently refuses an in-place upgrade when `install-state.json` belongs to a
  different release version. Building `0.1.1` therefore does not by itself add
  an upgrade path from an installed `0.1.0` host.

Future release work should add signed publisher provenance, policy-controlled
artifact acquisition, and an automated clean-host qualification test. CI can
later execute these same scripts rather than defining a different release
process.
