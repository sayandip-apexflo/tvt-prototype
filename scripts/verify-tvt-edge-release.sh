#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE=""
usage() { echo "usage: scripts/verify-tvt-edge-release.sh --bundle DIR" >&2; }
while (($#)); do
  case "$1" in
    --bundle) BUNDLE="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
[[ -d ${BUNDLE} && ! -L ${BUNDLE} ]] || { usage; exit 2; }
BUNDLE="$(cd "${BUNDLE}" && pwd -P)"

# shellcheck source=scripts/lib/tvt-installer-common.sh
source "${REPO_ROOT}/scripts/lib/tvt-installer-common.sh"
tvt_verify_bundle "${BUNDLE}"

python3 - "${BUNDLE}" <<'PY'
import hashlib
import json
import pathlib
import re
import sys
import zipfile

root = pathlib.Path(sys.argv[1])
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
lock = json.loads((root / manifest["artifacts"]["input_lock"]).read_text(encoding="utf-8"))
version = manifest["release_version"]
commit = manifest["source_commit"]
if lock.get("release_version") != version or lock.get("source_commit") != commit:
    raise SystemExit("release identity does not match the external-input lock")
wheel = root / manifest["artifacts"]["application_wheel"]
with zipfile.ZipFile(wheel) as archive:
    metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    if len(metadata_names) != 1:
        raise SystemExit("application wheel does not contain exactly one metadata record")
    metadata = archive.read(metadata_names[0]).decode("utf-8", errors="strict")
    match = re.search(r"^Version: (.+)$", metadata, flags=re.MULTILINE)
    if not match or match.group(1).strip() != version:
        raise SystemExit("application wheel version does not match the manifest")
    ui_assets = [name for name in archive.namelist() if "tvt_edge/static/" in name]
    ui_contents = [archive.read(name) for name in ui_assets]
    if (
        not ui_contents
        or not any(b"TVT Runtime" in content for content in ui_contents)
        or not any(version.encode() in content for content in ui_contents)
    ):
        raise SystemExit("built UI does not report the manifest version")

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

locked_files = lock.get("files")
if not isinstance(locked_files, dict) or not locked_files:
    raise SystemExit("external-input lock has no file records")
for relative, record in locked_files.items():
    if not isinstance(relative, str) or not isinstance(record, dict):
        raise SystemExit("external-input lock contains an invalid file record")
    installed = pathlib.PurePosixPath(relative)
    if installed.is_absolute() or not installed.parts or any(
        part in {"", ".", ".."} for part in installed.parts
    ):
        raise SystemExit(f"external-input lock contains an unsafe path: {relative}")
    if installed.parts[0] == "apt":
        installed = pathlib.PurePosixPath("packages", *installed.parts)
    path = root.joinpath(*installed.parts)
    if not path.is_file():
        raise SystemExit(f"locked release input is missing from bundle: {installed}")
    if path.stat().st_size != record.get("size") or sha256(path) != record.get("sha256"):
        raise SystemExit(f"bundled input differs from its lock: {installed}")
configuration = lock.get("configuration", {})
if not isinstance(configuration, dict):
    raise SystemExit("external-input lock contains an invalid configuration record")
if sha256(root / "config/platform.env") != configuration.get("platform_sha256"):
    raise SystemExit("bundled platform config differs from the input lock")
if sha256(root / "config/pipeline.env") != configuration.get("pipeline_sha256"):
    raise SystemExit("bundled pipeline config differs from the input lock")
print(f"Verified TVT edge release {version} from {commit}")
PY
