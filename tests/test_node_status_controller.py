import unittest
from datetime import datetime, timedelta, timezone

from apexfabric.node_management.status_controller.controller import report_labels


def node(ready=True):
    return {
        "metadata": {"name": "node-01", "labels": {"kubernetes.io/arch": "amd64"}},
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


def intel_node(ready=True):
    candidate = node(ready)
    candidate["metadata"]["labels"]["apexfabric.com/hardware-profile"] = "intel-285h"
    return candidate


def generic_node(ready=True):
    candidate = node(ready)
    candidate["metadata"]["labels"]["apexfabric.com/hardware-profile"] = "generic-amd64"
    return candidate


def report(observed_at=None):
    return {"metadata": {"name": "node-01"}, "spec": {
        "nodeName": "node-01", "reporterVersion": "0.1.0",
        "observedAt": (observed_at or datetime.now(timezone.utc)).isoformat(),
        "capabilities": {
            "hardware": {"cpu": {"architecture": "x86_64"}},
            "accelerators": {"metis": {"present": False}},
            "decoder": {"va_api": {"available": True}},
        },
    }}


class NodeStatusControllerTests(unittest.TestCase):
    def test_fresh_matching_ready_report_is_accepted(self):
        labels, accepted, reason = report_labels(report(), node())
        self.assertTrue(accepted)
        self.assertEqual(reason, "Accepted")
        self.assertEqual(labels["apexfabric.com/architecture"], "amd64")
        self.assertEqual(labels["apexfabric.com/decoder"], "vaapi")

    def test_stale_or_not_ready_report_is_rejected(self):
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        _, accepted, reason = report_labels(report(old), node(False))
        self.assertFalse(accepted)
        self.assertIn("ReportStale", reason)
        self.assertIn("NodeNotReady", reason)

    def test_architecture_mismatch_is_rejected(self):
        candidate = report()
        candidate["spec"]["capabilities"]["hardware"]["cpu"]["architecture"] = "aarch64"
        _, accepted, reason = report_labels(candidate, node())
        self.assertFalse(accepted)
        self.assertEqual(reason, "ArchitectureMismatch")

    def test_intel_profile_requires_gpu_vaapi_and_npu_driver(self):
        candidate = report()
        candidate["spec"]["capabilities"]["accelerators"].update({
            "gpu": {"present": True, "device_nodes": ["/dev/dri/renderD128"]},
            "npu": {"present": True, "device_nodes": ["/dev/accel/accel0"], "driver": {"loaded": True}},
        })
        _, accepted, reason = report_labels(candidate, intel_node())
        self.assertTrue(accepted, reason)

        candidate["spec"]["capabilities"]["accelerators"]["npu"]["driver"]["loaded"] = False
        _, accepted, reason = report_labels(candidate, intel_node())
        self.assertFalse(accepted)
        self.assertIn("IntelNpuDriverUnavailable", reason)

    def test_generic_amd64_profile_does_not_require_intel_devices(self):
        candidate = report()
        candidate["spec"]["capabilities"]["accelerators"] = {"metis": {"present": False}}
        candidate["spec"]["capabilities"]["decoder"]["va_api"]["available"] = False
        _, accepted, reason = report_labels(candidate, generic_node())
        self.assertTrue(accepted, reason)


if __name__ == "__main__": unittest.main()
