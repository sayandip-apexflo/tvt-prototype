#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly STATE_DIR=/var/lib/tvt-online-test-install
readonly CONFIRMATION_TEXT=ERASE-TVT-ONLINE-TEST
readonly FULL_STACK_CONFIRMATION_TEXT=ERASE-ENTIRE-TVT-STACK
EXECUTE=false
CONFIRM=""
FULL_STACK=false

log() { printf 'tvt-online-reset: %s\n' "$*"; }
fail() { printf 'tvt-online-reset: ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  echo "usage: sudo bash scripts/reset-tvt-online-test-host.sh [--force-full-stack] [--execute --confirm TEXT]" >&2
}

while (($#)); do
  case "$1" in
    --execute) EXECUTE=true; shift ;;
    --confirm) CONFIRM="${2:-}"; shift 2 ;;
    --force-full-stack) FULL_STACK=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

if ${FULL_STACK}; then
  cat <<'EOF'
FULL-STACK PURGE: this ignores the pre-install baseline and permanently removes
all TVT files/accounts, every K3s/Kubernetes role and cluster state, every
Docker/containerd container/image/volume and runtime directory, every
PostgreSQL cluster/database, the Intel graphics PPA, and installed Intel
userspace GPU/NPU/media packages selected by the TVT driver recipe. Components
are removed even if they existed before TVT. Transferred files under /home and
the Ubuntu kernel (including its built-in i915, xe, and intel_vpu modules) are
preserved. A reboot is mandatory afterward.
EOF
else
  cat <<'EOF'
This baseline reset removes TVT databases, service accounts, systemd units,
K3s server state and local-storage data, the local registry and its images,
TVT runtime directories, the extracted test kit, and packages recorded as
introduced by the paired online installer. It preserves transferred files
under /home and restores captured pre-install package/service state.

It does not and cannot restore a pre-existing K3s agent: agent tokens and
secret configuration are intentionally never captured. A reboot is required
after driver/package removal before beginning the next test cycle.
EOF
fi

if ! ${EXECUTE}; then
  log "plan only; no changes made"
  if ${FULL_STACK}; then
    log "rerun with --force-full-stack --execute --confirm ${FULL_STACK_CONFIRMATION_TEXT} after reviewing this script"
  else
    log "rerun with --execute --confirm ${CONFIRMATION_TEXT} after reviewing this script"
  fi
  exit 0
fi
[[ ${EUID} -eq 0 ]] || fail "run with sudo"
if ${FULL_STACK}; then
  [[ ${CONFIRM} == "${FULL_STACK_CONFIRMATION_TEXT}" ]] || fail "full-stack confirmation text does not match"
else
  [[ ${CONFIRM} == "${CONFIRMATION_TEXT}" ]] || fail "confirmation text does not match"
fi
cd /

remove_tree() {
  local target="$1"
  case "${target}" in
    /etc/tvt|/var/lib/tvt|/var/lib/tvt-alert|/var/cache/tvt|/opt/apexfabric/openvino-env|/opt/tvt|/opt/tvt/venv|/opt/tvt/scripts|/opt/tvt/config|/opt/tvt/solution-packs|/var/lib/tvt/qualification|/var/lib/tvt/pipeline|/var/lib/tvt/registry|/var/lib/tvt-online-test-install|/opt/tvt/tvt-edge-online-test-kit-*|/var/lib/rancher|/var/lib/rancher/k3s|/etc/rancher|/etc/rancher/k3s|/etc/kubernetes|/var/lib/kubelet|/etc/cni/net.d|/var/lib/cni|/run/k3s|/run/flannel|/var/lib/docker|/var/lib/containerd|/etc/docker|/etc/containerd|/etc/postgresql|/var/lib/postgresql|/var/lib/postgresql/16/main|/var/log/postgresql|/etc/systemd/system/k3s.service.d|/etc/systemd/system/k3s-agent.service.d) ;;
    *) fail "refusing unsafe recursive removal target: ${target}" ;;
  esac
  [[ ! -e ${target} && ! -L ${target} ]] || rm -rf -- "${target}"
}

list_full_stack_packages() {
  dpkg-query -W -f='${binary:Package}\t${db:Status-Status}\n' 2>/dev/null | awk -F '\t' '
    $2 == "installed" {
      package=$1; bare=package; sub(/:.*/, "", bare)
      if (bare ~ /^(docker.*|containerd.*|runc|k3s|kubeadm|kubectl|kubelet|kubernetes-cni|cri-tools|postgresql.*|libpq.*|libze.*|intel-(opencl|gsc|media|driver-compiler|fw-npu|level-zero|npu).*|libmfx.*|libvpl.*|libva.*|va-driver-all|vainfo|clinfo|libtbb.*|libigdgmm.*|ocl-icd-.*|mesa-va-drivers|git-lfs|jq)$/) print package
    }' | sort -u
}

if ${FULL_STACK}; then
  full_units=(
    tvt-alert-dispatcher.service tvt-camera-sync.service tvt-edge.service
    tvt-retention.timer tvt-retention.service tvt-k3s-watchdog.timer
    tvt-k3s-watchdog.service tvt-pipeline-image-sync.timer
    tvt-pipeline-image-sync.service tvt-local-registry.service
    k3s.service k3s-agent.service docker.service containerd.service
    postgresql.service
  )
  log "stopping all TVT, Kubernetes, container, and PostgreSQL services"
  for unit in "${full_units[@]}"; do
    systemctl disable --now "${unit}" >/dev/null 2>&1 || true
  done

  if command -v pg_lsclusters >/dev/null 2>&1 && command -v pg_dropcluster >/dev/null 2>&1; then
    log "permanently deleting every PostgreSQL cluster"
    while read -r pg_version pg_name; do
      [[ ${pg_version} =~ ^[0-9]+$ && ${pg_name} =~ ^[A-Za-z0-9_.-]+$ ]] || fail "unsafe PostgreSQL cluster identity"
      pg_dropcluster --stop "${pg_version}" "${pg_name}"
    done < <(pg_lsclusters --no-header 2>/dev/null | awk '{print $1, $2}')
  fi

  log "running K3s uninstallers and deleting remaining Kubernetes state"
  [[ ! -x /usr/local/bin/k3s-killall.sh ]] || /usr/local/bin/k3s-killall.sh
  if [[ -x /usr/local/bin/k3s-uninstall.sh ]]; then
    /usr/local/bin/k3s-uninstall.sh
  fi
  if [[ -x /usr/local/bin/k3s-agent-uninstall.sh ]]; then
    /usr/local/bin/k3s-agent-uninstall.sh
  fi
  rm -f -- /etc/systemd/system/k3s.service /etc/systemd/system/k3s.service.env
  rm -f -- /etc/systemd/system/k3s-agent.service /etc/systemd/system/k3s-agent.service.env
  rm -f -- /usr/local/bin/k3s /usr/local/bin/kubectl /usr/local/bin/crictl /usr/local/bin/ctr
  rm -f -- /usr/local/bin/k3s-killall.sh /usr/local/bin/k3s-uninstall.sh /usr/local/bin/k3s-agent-uninstall.sh
  remove_tree /var/lib/rancher
  remove_tree /etc/rancher
  remove_tree /etc/kubernetes
  remove_tree /var/lib/kubelet
  remove_tree /etc/cni/net.d
  remove_tree /var/lib/cni
  remove_tree /run/k3s
  remove_tree /run/flannel

  log "removing TVT, OpenVINO, Docker/containerd, and PostgreSQL data"
  remove_tree /etc/tvt
  remove_tree /var/lib/tvt
  remove_tree /var/lib/tvt-alert
  remove_tree /var/cache/tvt
  remove_tree /opt/tvt
  remove_tree /opt/apexfabric/openvino-env
  remove_tree /var/lib/docker
  remove_tree /var/lib/containerd
  remove_tree /etc/docker
  remove_tree /etc/containerd
  remove_tree /etc/postgresql
  remove_tree /var/lib/postgresql
  remove_tree /var/log/postgresql

  log "removing service definitions and local accounts"
  for unit in "${full_units[@]}"; do
    rm -f -- "/etc/systemd/system/${unit}"
  done
  remove_tree /etc/systemd/system/k3s.service.d
  remove_tree /etc/systemd/system/k3s-agent.service.d
  userdel tvt-edge >/dev/null 2>&1 || true
  userdel tvt-alert >/dev/null 2>&1 || true
  groupdel tvt-edge >/dev/null 2>&1 || true
  groupdel tvt-alert >/dev/null 2>&1 || true

  if command -v add-apt-repository >/dev/null 2>&1; then
    add-apt-repository -y --remove ppa:kobuk-team/intel-graphics || true
  fi
  if grep -RqsE 'ppa\.launchpadcontent\.net/kobuk-team/intel-graphics|kobuk-team/ubuntu.*intel-graphics' /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
    fail "Intel graphics PPA remains configured; remove it before package purge"
  fi
  apt-get update

  mapfile -t purge_packages < <(list_full_stack_packages)
  if ((${#purge_packages[@]})); then
    log "blindly purging selected full-stack packages, including pre-existing versions: ${purge_packages[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get purge -y "${purge_packages[@]}"
  fi

  remove_tree /var/lib/docker
  remove_tree /var/lib/containerd
  remove_tree /etc/docker
  remove_tree /etc/containerd
  remove_tree /etc/postgresql
  remove_tree /var/lib/postgresql
  remove_tree /var/log/postgresql
  groupdel docker >/dev/null 2>&1 || true
  userdel postgres >/dev/null 2>&1 || true
  groupdel postgres >/dev/null 2>&1 || true
  systemctl daemon-reload
  systemctl reset-failed >/dev/null 2>&1 || true

  remaining_packages="$(list_full_stack_packages)"
  [[ -z ${remaining_packages} ]] || fail "full-stack packages remain installed: ${remaining_packages//$'\n'/, }"
  [[ ! -e /usr/local/bin/k3s && ! -e /etc/rancher && ! -e /var/lib/rancher ]] || fail "K3s residue remains"
  [[ ! -e /etc/tvt && ! -e /var/lib/tvt && ! -e /opt/tvt ]] || fail "TVT residue remains"
  [[ ! -e /var/lib/docker && ! -e /var/lib/containerd ]] || fail "container runtime data remains"
  [[ ! -e /etc/postgresql && ! -e /var/lib/postgresql ]] || fail "PostgreSQL data remains"
  remove_tree "${STATE_DIR}"
  rmdir /opt/apexfabric >/dev/null 2>&1 || true
  log "full-stack purge completed; files under /home and Ubuntu kernel modules were preserved"
  log "reboot before beginning the next Steps 1-10 test cycle"
  exit 0
fi

[[ -d ${STATE_DIR} && ! -L ${STATE_DIR} ]] || fail "paired installer baseline is missing"
for required in baseline-packages.tsv baseline-manual.txt baseline-docker-tags.txt baseline-unit-enabled.tsv baseline-unit-active.tsv baseline-paths.tsv baseline-k3s-state intel-ppa-preexisting kit-root new-packages.txt changed-packages.tsv removed-packages.tsv newly-manual.txt new-docker-tags.txt; do
  [[ -f ${STATE_DIR}/${required} && ! -L ${STATE_DIR}/${required} ]] || fail "baseline file is missing or unsafe: ${required}"
done

KIT_ROOT="$(<"${STATE_DIR}/kit-root")"
[[ ${KIT_ROOT} == /opt/tvt/tvt-edge-online-test-kit-* && ${KIT_ROOT} != *'..'* && ! -L ${KIT_ROOT} ]] || fail "recorded kit root is unsafe"
baseline_k3s="$(<"${STATE_DIR}/baseline-k3s-state")"
[[ ${baseline_k3s} == clean || ${baseline_k3s} == agent ]] || fail "reset refuses baseline K3s state ${baseline_k3s}"

log "stopping and disabling TVT services and timers"
units=(
  tvt-alert-dispatcher.service tvt-camera-sync.service tvt-edge.service
  tvt-retention.timer tvt-retention.service tvt-k3s-watchdog.timer
  tvt-k3s-watchdog.service tvt-pipeline-image-sync.timer
  tvt-pipeline-image-sync.service tvt-local-registry.service
)
for unit in "${units[@]}"; do
  systemctl disable --now "${unit}" >/dev/null 2>&1 || true
done

if command -v psql >/dev/null 2>&1 && id postgres >/dev/null 2>&1; then
  log "dropping the TVT PostgreSQL database and roles"
  runuser -u postgres -- psql -d postgres -v ON_ERROR_STOP=1 -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='tvt' AND pid <> pg_backend_pid();" >/dev/null
  runuser -u postgres -- dropdb --if-exists tvt
  runuser -u postgres -- dropuser --if-exists tvt-edge
  runuser -u postgres -- dropuser --if-exists tvt-alert
fi

if [[ ${baseline_k3s} == clean || -f ${STATE_DIR}/preexisting-agent-removal ]]; then
  log "uninstalling the TVT K3s server and its local cluster/storage state"
  if [[ -x /usr/local/bin/k3s-uninstall.sh ]]; then
    /usr/local/bin/k3s-uninstall.sh
  elif systemctl cat k3s.service >/dev/null 2>&1 || [[ -e /var/lib/rancher/k3s ]]; then
    log "official K3s uninstaller is missing; removing only the K3s paths absent from the captured baseline"
    [[ ${baseline_k3s} == clean || -f ${STATE_DIR}/preexisting-agent-removal ]] || fail "refusing fallback K3s cleanup"
    [[ ! -x /usr/local/bin/k3s-killall.sh ]] || /usr/local/bin/k3s-killall.sh
    systemctl disable --now k3s.service >/dev/null 2>&1 || true
    rm -f -- /etc/systemd/system/k3s.service /etc/systemd/system/k3s.service.env
    rm -f -- /usr/local/bin/k3s /usr/local/bin/kubectl /usr/local/bin/crictl
    rm -f -- /usr/local/bin/k3s-killall.sh /usr/local/bin/k3s-uninstall.sh
    remove_tree /var/lib/rancher/k3s
    remove_tree /etc/rancher/k3s
    remove_tree /var/lib/kubelet
    remove_tree /run/k3s
    remove_tree /run/flannel
  fi
fi

if command -v docker >/dev/null 2>&1; then
  docker rm --force tvt-local-registry >/dev/null 2>&1 || true
  mapfile -t new_docker_tags <"${STATE_DIR}/new-docker-tags.txt"
  for tag in "${new_docker_tags[@]}"; do
    [[ -n ${tag} && ${tag} != '<none>:<none>' ]] || continue
    docker image rm "${tag}" >/dev/null 2>&1 || true
  done
fi

log "removing TVT systemd units and registry configuration"
for unit in "${units[@]}"; do
  rm -f -- "/etc/systemd/system/${unit}"
done
rm -f -- /etc/systemd/system/k3s.service.d/20-tvt-local-registry.conf
rm -f -- /etc/postgresql/16/main/conf.d/tvt.conf
rmdir /etc/systemd/system/k3s.service.d >/dev/null 2>&1 || true
systemctl daemon-reload

log "removing TVT-owned accounts and runtime paths"
remove_tree /etc/tvt
remove_tree /var/lib/tvt
remove_tree /var/lib/tvt-alert
remove_tree /var/cache/tvt
remove_tree /opt/apexfabric/openvino-env
remove_tree /opt/tvt/venv
remove_tree /opt/tvt/scripts
remove_tree /opt/tvt/config
remove_tree /opt/tvt/solution-packs
remove_tree "${KIT_ROOT}"
userdel tvt-edge >/dev/null 2>&1 || true
userdel tvt-alert >/dev/null 2>&1 || true
groupdel tvt-edge >/dev/null 2>&1 || true
groupdel tvt-alert >/dev/null 2>&1 || true

if [[ $(<"${STATE_DIR}/intel-ppa-preexisting") == false ]]; then
  log "removing the Intel graphics PPA introduced by the installer"
  if command -v add-apt-repository >/dev/null 2>&1; then
    add-apt-repository -y --remove ppa:kobuk-team/intel-graphics
  fi
fi
apt-get update

mapfile -t restore_pins < <(awk -F '\t' 'NF == 3 {print $1 "=" $2}' "${STATE_DIR}/changed-packages.tsv"; awk -F '\t' 'NF == 2 {print $1 "=" $2}' "${STATE_DIR}/removed-packages.tsv")
if ((${#restore_pins[@]})); then
  log "restoring package versions that changed during installation"
  apt-get install -y --no-remove --allow-downgrades --allow-change-held-packages "${restore_pins[@]}"
fi

mapfile -t new_packages <"${STATE_DIR}/new-packages.txt"
if ((${#new_packages[@]})); then
  log "checking the package-removal transaction against the captured baseline"
  removal_plan="$(mktemp)"
  allowed_plan="$(mktemp)"
  apt-get --simulate purge "${new_packages[@]}" | awk '$1 == "Remv" {sub(/:.*/, "", $2); print $2}' | sort -u >"${removal_plan}"
  sed 's/:.*//' "${STATE_DIR}/new-packages.txt" | sort -u >"${allowed_plan}"
  unexpected="$(comm -23 "${removal_plan}" "${allowed_plan}")"
  rm -f -- "${removal_plan}"
  rm -f -- "${allowed_plan}"
  [[ -z ${unexpected} ]] || fail "APT would remove packages outside the captured install delta: ${unexpected//$'\n'/, }"
  apt-get purge -y "${new_packages[@]}"
fi

mapfile -t newly_manual <"${STATE_DIR}/newly-manual.txt"
if ((${#newly_manual[@]})); then
  installed_newly_manual=()
  for package in "${newly_manual[@]}"; do
    dpkg-query -W -f='${db:Status-Status}' "${package}" 2>/dev/null | grep -qx installed && installed_newly_manual+=("${package}")
  done
  if ((${#installed_newly_manual[@]})); then
    apt-mark auto "${installed_newly_manual[@]}" >/dev/null
  fi
fi

mapfile -t baseline_manual <"${STATE_DIR}/baseline-manual.txt"
if ((${#baseline_manual[@]})); then
  apt-mark manual "${baseline_manual[@]}" >/dev/null
fi

baseline_units=(docker.service postgresql.service)
for unit in "${baseline_units[@]}"; do
  baseline_enabled="$(awk -F '\t' -v wanted="${unit}" '$1 == wanted {print $2}' "${STATE_DIR}/baseline-unit-enabled.tsv")"
  baseline_active="$(awk -F '\t' -v wanted="${unit}" '$1 == wanted {print $2}' "${STATE_DIR}/baseline-unit-active.tsv")"
  if [[ ${baseline_enabled} == enabled ]]; then
    systemctl enable "${unit}" >/dev/null 2>&1 || true
  elif [[ ${baseline_enabled} == disabled ]]; then
    systemctl disable "${unit}" >/dev/null 2>&1 || true
  fi
  if [[ ${baseline_active} == active ]]; then
    systemctl start "${unit}" >/dev/null 2>&1 || true
  else
    systemctl stop "${unit}" >/dev/null 2>&1 || true
  fi
done

baseline_path_existed() {
  [[ $(awk -F '\t' -v wanted="$1" '$1 == wanted {print $2}' "${STATE_DIR}/baseline-paths.tsv") == true ]]
}
if ! baseline_path_existed /var/lib/docker; then
  remove_tree /var/lib/docker
fi
if ! baseline_path_existed /var/lib/containerd; then
  remove_tree /var/lib/containerd
fi
if ! baseline_path_existed /etc/docker; then
  remove_tree /etc/docker
fi
if ! baseline_path_existed /var/lib/postgresql/16/main; then
  remove_tree /var/lib/postgresql/16/main
fi

remove_tree "${STATE_DIR}"
rmdir /opt/tvt >/dev/null 2>&1 || true
rmdir /opt/apexfabric >/dev/null 2>&1 || true
log "reset completed; transferred archives under /home were preserved"
log "reboot before starting the next Steps 1-10 test cycle"
