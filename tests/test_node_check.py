import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apexfabric.node_management import qualification as node_check


class FakeStat:
    st_mode = 0o40750
    st_uid = 0
    st_gid = 123


class FakePath:
    def __init__(self, exists=True):
        self.exists = exists

    def is_dir(self):
        return self.exists

    def stat(self):
        return FakeStat()


class FakeProbe:
    def __init__(self, missing_packages=()):
        self.missing_packages = set(missing_packages)

    def command_exists(self, command):
        return command in {"containerd", "dpkg-query"}

    def package_installed(self, package):
        return package not in self.missing_packages

    def read(self, path):
        values = {
            "/etc/os-release": 'ID=ubuntu\nVERSION_ID="24.04"\n',
            "/proc/sys/net/ipv4/ip_forward": "1\n",
            "/proc/sys/net/bridge/bridge-nf-call-iptables": "1\n",
            "/proc/sys/net/bridge/bridge-nf-call-ip6tables": "1\n",
        }
        return values.get(path, "")

    def path(self, path):
        return FakePath()

    def group_gid(self, name):
        return 123


def capabilities(metis=False):
    return {
        "network": {
            "interfaces": [{"name": "eth0", "state": "UP"}],
            "connectivity": {"dns_configured": True},
        },
        "decoder": {
            "va_api": {"available": True},
            "gstreamer_decoder_elements": ["fixture:h264dec"],
        },
        "accelerators": {
            "gpu": {"devices": [{"vendor": "AMD"}]},
            "metis": {
                "present": metis,
                "driver": {"loaded": metis},
                "axdevice": {"present": metis},
            },
        },
        "software": {"voyager_sdk": {"present": metis}},
    }


class NodeCheckTests(unittest.TestCase):
    def test_qualified_without_absent_metis(self):
        checks = node_check.check_node(capabilities(), FakeProbe())
        self.assertFalse(any(item.status == node_check.FAIL for item in checks))
        metis = next(item for item in checks if item.name == "metis_hardware")
        self.assertEqual(metis.status, node_check.NOT_APPLICABLE)
        self.assertEqual(metis.detail, "NOT_PRESENT")
        self.assertTrue(node_check.render(checks).endswith("QUALIFIED"))

    def test_missing_dependency_is_clear_failure(self):
        checks = node_check.check_node(capabilities(), FakeProbe({"socat"}))
        packages = next(item for item in checks if item.name == "packages")
        self.assertEqual(packages.status, node_check.FAIL)
        self.assertIn("socat", packages.detail)
        self.assertTrue(node_check.render(checks).endswith("NOT QUALIFIED"))

    def test_present_metis_requires_vendor_stack(self):
        data = capabilities(metis=True)
        data["accelerators"]["metis"]["driver"]["loaded"] = False
        data["software"]["voyager_sdk"]["present"] = False
        checks = node_check.check_node(data, FakeProbe())
        self.assertEqual(next(x for x in checks if x.name == "metis_driver").status, node_check.FAIL)
        self.assertEqual(next(x for x in checks if x.name == "voyager_sdk").status, node_check.FAIL)


if __name__ == "__main__":
    unittest.main()
