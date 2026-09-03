#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INPUT_DIRECTORY=""
OUTPUT_DIRECTORY=""
ARCHIVE_DIRECTORY=""
RELEASE_VERSION=""
SOURCE_COMMIT=""
CREATE_INPUT_LOCK=false
SKIP_TESTS=false
ALLOW_DIRTY_SOURCE=false

usage() {
  cat >&2 <<'EOF'
usage: scripts/make-tvt-edge-release.sh \
  --input-directory DIR --output-directory DIR [options]

options:
  --version VERSION          must equal the canonical TVT version
  --source-commit SHA        defaults to the checked-out commit
  --archive-directory DIR    defaults to the output directory's parent
  --create-input-lock        create/replace release-inputs.lock.json explicitly
  --skip-tests               skip source test gates (recorded in the report)
  --allow-dirty-source       development only; production builds must be clean
EOF
}

while (($#)); do
  case "$1" in
    --input-directory) INPUT_DIRECTORY="${2:-}"; shift 2 ;;
    --output-directory) OUTPUT_DIRECTORY="${2:-}"; shift 2 ;;
    --archive-directory) ARCHIVE_DIRECTORY="${2:-}"; shift 2 ;;
    --version) RELEASE_VERSION="${2:-}"; shift 2 ;;
    --source-commit) SOURCE_COMMIT="${2:-}"; shift 2 ;;
    --create-input-lock) CREATE_INPUT_LOCK=true; shift ;;
    --skip-tests) SKIP_TESTS=true; shift ;;
    --allow-dirty-source) ALLOW_DIRTY_SOURCE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
[[ -n ${INPUT_DIRECTORY} && -n ${OUTPUT_DIRECTORY} ]] || { usage; exit 2; }
[[ -d ${INPUT_DIRECTORY} && ! -L ${INPUT_DIRECTORY} ]] || { echo "input directory is missing or symlinked" >&2; exit 1; }
INPUT_DIRECTORY="$(cd "${INPUT_DIRECTORY}" && pwd -P)"
mkdir -p "$(dirname "${OUTPUT_DIRECTORY}")"
OUTPUT_DIRECTORY="$(cd "$(dirname "${OUTPUT_DIRECTORY}")" && pwd -P)/$(basename "${OUTPUT_DIRECTORY}")"
[[ -n ${ARCHIVE_DIRECTORY} ]] || ARCHIVE_DIRECTORY="$(dirname "${OUTPUT_DIRECTORY}")"
mkdir -p "${ARCHIVE_DIRECTORY}"
ARCHIVE_DIRECTORY="$(cd "${ARCHIVE_DIRECTORY}" && pwd -P)"

path_is_within() {
  case "${1}/" in
    "${2}/"*) return 0 ;;
    *) return 1 ;;
  esac
}
if path_is_within "${INPUT_DIRECTORY}" "${REPO_ROOT}" \
  || path_is_within "${OUTPUT_DIRECTORY}" "${REPO_ROOT}" \
  || path_is_within "${ARCHIVE_DIRECTORY}" "${REPO_ROOT}"; then
  echo "release inputs and outputs must be outside the Git checkout" >&2
  exit 1
fi
if path_is_within "${OUTPUT_DIRECTORY}" "${INPUT_DIRECTORY}" \
  || path_is_within "${ARCHIVE_DIRECTORY}" "${INPUT_DIRECTORY}"; then
  echo "release outputs must be outside the reviewed input directory" >&2
  exit 1
fi

cd "${REPO_ROOT}"
canonical_version="$(python3 scripts/tvt-version.py --check)"
[[ -n ${RELEASE_VERSION} ]] || RELEASE_VERSION="${canonical_version}"
[[ ${RELEASE_VERSION} == "${canonical_version}" ]] || {
  echo "requested release ${RELEASE_VERSION} does not equal canonical ${canonical_version}" >&2
  exit 1
}
[[ -n ${SOURCE_COMMIT} ]] || SOURCE_COMMIT="$(git rev-parse HEAD)"
[[ ${SOURCE_COMMIT} =~ ^[0-9a-f]{40}$ && ${SOURCE_COMMIT} == "$(git rev-parse HEAD)" ]] || {
  echo "--source-commit must be the full checked-out Git commit" >&2
  exit 1
}
if ! ${ALLOW_DIRTY_SOURCE} && [[ -n $(git status --porcelain) ]]; then
  echo "release generation requires a clean worktree" >&2
  exit 1
fi
expected_name="tvt-edge-release-${RELEASE_VERSION}"
[[ $(basename "${OUTPUT_DIRECTORY}") == "${expected_name}" ]] || {
  echo "output directory must be named ${expected_name}" >&2
  exit 1
}
archive="${ARCHIVE_DIRECTORY}/${expected_name}.tar.gz"
archive_checksum="${archive}.sha256"
report="${ARCHIVE_DIRECTORY}/${expected_name}.release-report.json"
for path in "${archive}" "${archive_checksum}" "${report}"; do
  [[ ! -e ${path} ]] || { echo "refusing to overwrite existing release output: ${path}" >&2; exit 1; }
done

input_lock="${INPUT_DIRECTORY}/release-inputs.lock.json"
if ${CREATE_INPUT_LOCK}; then
  python3 scripts/tvt-release-inputs.py create \
    --input-directory "${INPUT_DIRECTORY}" --output "${input_lock}" \
    --release-version "${RELEASE_VERSION}" --source-commit "${SOURCE_COMMIT}" \
    --platform-config config/platform.env --pipeline-config config/pipeline.env
fi
[[ -f ${input_lock} ]] || {
  echo "missing ${input_lock}; review inputs and rerun with --create-input-lock" >&2
  exit 1
}
python3 scripts/tvt-release-inputs.py verify \
  --input-directory "${INPUT_DIRECTORY}" --lock "${input_lock}" \
  --release-version "${RELEASE_VERSION}" --source-commit "${SOURCE_COMMIT}" \
  --platform-config config/platform.env --pipeline-config config/pipeline.env

tests_status=skipped
if ! ${SKIP_TESTS}; then
  test_python=python3
  if [[ -x .venv/bin/python ]]; then test_python=.venv/bin/python; fi
  "${test_python}" -m pytest -q
  npm --prefix ui ci
  npm --prefix ui test -- --run
  bash -n prepare-tvt-edge-host.sh install-tvt-edge-host.sh scripts/*.sh scripts/lib/*.sh
  tests_status=passed
fi

dirty_argument=()
if ${ALLOW_DIRTY_SOURCE}; then dirty_argument=(--allow-dirty-source); fi
./scripts/build-tvt-edge-release.sh \
  --output "${OUTPUT_DIRECTORY}" \
  --version "${RELEASE_VERSION}" --source-commit "${SOURCE_COMMIT}" \
  --input-lock "${input_lock}" \
  --registry-image "${INPUT_DIRECTORY}/images/registry.tar" \
  --node-reporter-image "${INPUT_DIRECTORY}/images/node-reporter.tar" \
  --node-status-controller-image "${INPUT_DIRECTORY}/images/node-status-controller.tar" \
  --traffic-image "${INPUT_DIRECTORY}/images/traffic-edge-runtime-v4.tar" \
  --k3s-installer "${INPUT_DIRECTORY}/k3s/install.sh" \
  --k3s-binary "${INPUT_DIRECTORY}/k3s/k3s" \
  --hardware-directory "${INPUT_DIRECTORY}/hardware" \
  --apt-directory "${INPUT_DIRECTORY}/apt" "${dirty_argument[@]}"

./scripts/verify-tvt-edge-release.sh --bundle "${OUTPUT_DIRECTORY}"

source_epoch="$(git show -s --format=%ct "${SOURCE_COMMIT}")"
tar --sort=name --mtime="@${source_epoch}" --owner=0 --group=0 --numeric-owner \
  -C "$(dirname "${OUTPUT_DIRECTORY}")" -cf - "${expected_name}" | gzip -n >"${archive}"
archive_digest="$(sha256sum "${archive}" | awk '{print $1}')"
printf '%s  %s\n' "${archive_digest}" "$(basename "${archive}")" >"${archive_checksum}"
bundle_checksums_digest="$(sha256sum "${OUTPUT_DIRECTORY}/checksums.sha256" | awk '{print $1}')"
input_lock_digest="$(sha256sum "${input_lock}" | awk '{print $1}')"
python3 - "${report}" "${RELEASE_VERSION}" "${SOURCE_COMMIT}" "${tests_status}" \
  "${ALLOW_DIRTY_SOURCE}" \
  "$(basename "${archive}")" "${archive_digest}" "${bundle_checksums_digest}" \
  "${input_lock_digest}" <<'PY'
import datetime, json, pathlib, sys
output, version, commit, tests, dirty_allowed, archive, archive_sha, checksums_sha, input_sha = sys.argv[1:]
document = {
    "schema_version": 1,
    "release_version": version,
    "source_commit": commit,
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "source_tests": tests,
    "dirty_source_allowed": dirty_allowed == "true",
    "archive": {"filename": archive, "sha256": archive_sha},
    "bundle_checksums_sha256": checksums_sha,
    "release_inputs_lock_sha256": input_sha,
    "qualified_on_clean_host": False,
    "credentials_included": False,
}
pathlib.Path(output).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
chmod 0644 "${archive_checksum}"
chmod 0600 "${report}"
echo "Built TVT edge release ${RELEASE_VERSION} from ${SOURCE_COMMIT}."
echo "Bundle: ${OUTPUT_DIRECTORY}"
echo "Archive: ${archive}"
echo "Report: ${report}"
