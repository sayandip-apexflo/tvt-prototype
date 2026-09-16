import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apexfabric.node_management.discovery import discovery


class FixtureRunner:
    def __init__(self, commands):
        self.commands = commands

    def exists(self, command):
        return command in self.commands

    def run(self, command, *args, timeout=10):
        return self.commands.get(command, {}).get(tuple(args), (127, ""))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        fixture = ROOT / "tests" / "fixtures"
        self.files = {
            "/etc/machine-id": "fixture-machine-id\n",
            "/proc/meminfo": "MemTotal:       1000 kB\nMemAvailable:    400 kB\n",
            "/etc/os-release": 'NAME="Fixture Linux"\nVERSION_ID="1"\nID=fixture\n',
            "/etc/resolv.conf": "nameserver 192.0.2.53\n",
            "/proc/net/route": "Iface Destination Gateway Flags RefCnt Use Metric Mask\n",
            "/sys/class/metis/version": "",
        }
        self.runner = FixtureRunner({
            "lscpu": {("--json",): (0, (fixture / "lscpu.json").read_text())},
            "lsblk": {( "--json", "--bytes", "--output", "NAME,KNAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL,VENDOR,TRAN"):
                (0, (fixture / "lsblk.json").read_text())},
            "df": {("--block-size=1", "--output=source,target,size,avail", "--exclude-type=tmpfs", "--exclude-type=devtmpfs"):
                (0, "Filesystem Mounted 1B-blocks Avail\n/dev/sda1 / 900000000 300000000")},
            "lspci": {("-Dnn",): (0, "0000:00:02.0 VGA compatible controller: Intel Corporation Fixture Graphics [8086:1234]")},
            "ip": {
                ("-json", "address", "show"): (0, json.dumps([{"ifname":"eth0","operstate":"UP","address":"00:11:22:33:44:55","mtu":1500,"addr_info":[{"family":"inet","local":"192.0.2.2","prefixlen":24}]}])),
                ("-json", "route", "show", "default"): (0, '[{"dst":"default","gateway":"192.0.2.1","dev":"eth0"}]'),
            },
            "k3s": {("--version",): (0, "k3s version v1.fixture")},
            "gst-inspect-1.0": {
                ("--version",): (0, "gst-inspect-1.0 version 1.fixture"),
                (): (0, "videoparsersbad: h264parse: H.264 parser\navdec: avdec_h264: libav H.264 decoder"),
            },
            "python3": {
                ("-m", "pip", "show", "axelera-rt"): (1, ""),
                ("-m", "pip", "show", "axelera-devkit"): (1, ""),
            },
        })

    def reader(self, path):
        return self.files.get(path, "")

    def test_fixture_discovery_is_deterministic(self):
        first = discovery.discover(self.runner, self.reader)
        second = discovery.discover(self.runner, self.reader)
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], "1.1.0")
        expected = "node-" + hashlib.sha256(b"fixture-machine-id").hexdigest()[:16]
        self.assertEqual(first["node_id"], expected)
        self.assertEqual(first["hardware"]["cpu"]["cores"], 4)
        self.assertTrue(first["accelerators"]["gpu"]["present"])
        self.assertEqual(first["decoder"]["gstreamer_decoder_elements"], ["avdec:avdec_h264"])

    def test_metis_is_absent_without_hardware_evidence(self):
        self.files["/sys/class/metis/version"] = "1.5.5\n"
        metis = discovery.discover_metis(self.runner, self.reader)
        self.assertFalse(metis["present"])
        self.assertTrue(metis["driver"]["loaded"])
        self.assertIsNone(metis["vendor"])
        self.assertIsNone(metis["model"])

    def test_npu_requires_device_and_loaded_driver(self):
        original_glob = discovery.glob.glob
        try:
            discovery.glob.glob = lambda pattern: ["/dev/accel/accel0"] if pattern == "/dev/accel/accel*" else []
            npu = discovery.discover_npu(lambda path: "1.0" if path == "/sys/module/intel_vpu/version" else "")
        finally:
            discovery.glob.glob = original_glob
        self.assertTrue(npu["present"])
        self.assertTrue(npu["driver"]["loaded"])

    def test_arm_lscpu_unavailable_topology_values_are_tolerated(self):
        output = json.dumps({"lscpu": [
            {"field": "Architecture:", "data": "aarch64"},
            {"field": "CPU(s):", "data": "12"},
            {"field": "Model name:", "data": "ARMv8 Processor"},
            {"field": "Socket(s):", "data": "-"},
            {"field": "Core(s) per socket:", "data": "-"},
        ]})
        runner = FixtureRunner({"lscpu": {("--json",): (0, output)}})
        cpu = discovery.discover_cpu(runner)
        self.assertEqual(cpu["architecture"], "aarch64")
        self.assertEqual(cpu["threads"], 12)
        self.assertEqual(cpu["cores"], 12)

    def test_human_output_reports_absence(self):
        rendered = discovery.render_human(discovery.discover(self.runner, self.reader))
        self.assertIn("Metis: absent", rendered)
        self.assertIn("Fixture CPU", rendered)

    def test_configured_cameras_are_stable_and_deduplicated(self):
        self.files["/etc/apexfabric/cameras.json"] = '{"cameras":{"accessible":["camera-02","camera-01","camera-01"]}}'
        cameras = discovery.discover_cameras(self.reader)
        self.assertEqual(cameras["accessible"], ["camera-01", "camera-02"])


if __name__ == "__main__":
    unittest.main()
