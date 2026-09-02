# TVT edge hardware-driver commands

These commands install and verify the Intel 285H hardware-driver stack used by
TVT. The installer follows the `k3s-prototype` resolve-once policy: its first
run selects the current versions of the same driver packages, saves their exact
versions and hashes, and reuses that lock on later runs.

The installer supports Ubuntu 24.04 on an `amd64` Intel Core Ultra 9 285H-class
host. It is not a Jetson installer; JetPack/L4T must be installed as a matched
NVIDIA BSP image.

## 1. Review the installer

```bash
less scripts/install-tvt-hardware-drivers.sh
```

This displays the script before granting it root access. Confirm the target OS,
hardware checks, package list, download sources, state paths, and cache paths.

## 2. Install the hardware drivers

Run this command from the repository root on the TVT edge device:

```bash
sudo ./scripts/install-tvt-hardware-drivers.sh
```

The command:

- verifies Ubuntu 24.04, `amd64`, Intel 285H hardware, and the required kernel
  modules;
- enables the Intel graphics PPA used by `k3s-prototype`;
- resolves and installs the matching Intel GPU, media, Level Zero, OpenCL,
  oneVPL, VA-API, and NPU packages from the Internet;
- installs `openvino` and `openvino-genai` in
  `/opt/apexfabric/openvino-env`;
- writes the exact resolved recipe to
  `/var/lib/tvt/hardware-driver-recipe.json`; and
- caches the locked NPU archive and Python wheel closure under
  `/var/cache/tvt/hardware-drivers` for repeatable retries.

If an audited Intel host is compatible but its CPU model string is not exactly
285H, use the explicit override:

```bash
sudo env TVT_ALLOW_UNVERIFIED_HARDWARE=true \
  ./scripts/install-tvt-hardware-drivers.sh
```

This bypasses only the CPU-model-name check. OS, architecture, kernel-module,
version-lock, and artifact-integrity checks still run.

Re-running the normal installation command reuses the existing recipe and
cached artifacts. Do not delete the recipe merely to retry a failed install;
deleting it authorizes the selection of newer versions.

## 3. Inspect the locked versions

```bash
sudo python3 -m json.tool /var/lib/tvt/hardware-driver-recipe.json
```

This prints the exact APT versions, OpenVINO versions, wheel hashes, Intel NPU
release URL and digest, OS, architecture, and kernel tuple selected during the
first resolution.

## 4. Reboot the edge device

```bash
sudo reboot
```

The reboot loads the installed GPU/NPU firmware and kernel/userspace stack as a
matched runtime. Wait for the device to return before continuing.

## 5. Verify kernel devices and modules

```bash
test -e /dev/dri/renderD128
test -e /dev/accel/accel0
lsmod | grep -E '^(i915|xe|intel_vpu)\b'
```

These commands confirm that the GPU render node, NPU accelerator node, Intel
graphics module, and Intel NPU module are present after reboot. A non-zero exit
status means hardware qualification has not succeeded.

## 6. Verify the media and compute runtimes

```bash
vainfo --display drm --device /dev/dri/renderD128
clinfo -l
```

`vainfo` checks the Intel VA-API media driver against the render device.
`clinfo -l` lists the OpenCL platforms and devices exposed by the installed
Intel runtime.

## 7. Verify OpenVINO device discovery

```bash
/opt/apexfabric/openvino-env/bin/python - <<'PY'
from openvino import Core

devices = set(Core().available_devices)
print("OpenVINO devices:", sorted(devices))
missing = {"CPU", "GPU", "NPU"} - devices
if missing:
    raise SystemExit("missing OpenVINO devices: " + ", ".join(sorted(missing)))
PY
```

This uses the isolated OpenVINO environment created by the installer and fails
unless OpenVINO detects the CPU, GPU, and NPU.

## 8. Clear the reboot-required marker

Run this only after all verification commands succeed:

```bash
sudo rm -f /var/lib/tvt/hardware-driver-reboot-required
```

This records operationally that post-reboot qualification is complete. It does
not remove the version recipe or cached driver artifacts.
