import io
import json
import logging
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tvt_edge.observability.logging import (
    JsonFormatter,
    bind_log_context,
    request_id_or_new,
    reset_log_context,
)
from tvt_edge.observability.metrics import (
    EdgeMetrics,
    LabelPolicy,
    MetricsContractError,
    render_metrics,
)
from tvt_edge.settings import Settings


ROOT = Path(__file__).resolve().parents[1]


class MetricsTests(unittest.TestCase):
    def test_metric_labels_are_allowlisted_and_camera_cardinality_is_bounded(self):
        policy = LabelPolicy()
        for number in range(1, 9):
            policy.camera_id(f"camera-{number:02d}")
        with self.assertRaisesRegex(MetricsContractError, "ceiling"):
            policy.camera_id("camera-09")
        with self.assertRaises(MetricsContractError):
            policy.reason("exception containing rtsp://user:pass@192.0.2.1/live")
        with self.assertRaises(MetricsContractError):
            policy.route("/cameras/camera-01")

    def test_product_and_http_metrics_render_only_normalized_labels(self):
        metrics = EdgeMetrics()
        metrics.camera_state(
            "camera-01", discovered=True, enabled=True, rtsp_valid=False
        )
        metrics.validation(
            "camera-01", 0.2, result="RTSP_AUTH_FAILED"
        )
        metrics.http_started()
        metrics.http_finished("GET", "/api/v1/cameras/{camera_id}", 200, 0.01)
        body, content_type = render_metrics(metrics.registry)
        rendered = body.decode()
        self.assertIn(
            'http_requests_total{method="GET",route="/api/v1/cameras/{camera_id}",service="edge-management",status_class="2xx"} 1.0',
            rendered,
        )
        self.assertIn(
            'edge_camera_validation_failures_total{camera_id="camera-01",reason="RTSP_AUTH_FAILED"} 1.0',
            rendered,
        )
        self.assertNotIn("request_id", rendered)
        self.assertTrue(content_type.startswith("text/plain"))


class LoggingTests(unittest.TestCase):
    def test_formatter_outputs_one_redacted_json_line_with_context(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter("edge-management"))
        logger = logging.getLogger("test.observability")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        token = bind_log_context(request_id="safe-request-1")
        try:
            logger.error(
                "failed rtsp://user:password@192.0.2.10/live password=hunter2 Bearer abc.def",
                extra={"event": "rtsp_connection_failed", "error_code": "RTSP_AUTH_FAILED"},
            )
        finally:
            reset_log_context(token)
        line = stream.getvalue()
        self.assertEqual(line.count("\n"), 1)
        document = json.loads(line)
        self.assertEqual(document["request_id"], "safe-request-1")
        self.assertEqual(document["event"], "rtsp_connection_failed")
        self.assertNotIn("hunter2", line)
        self.assertNotIn("192.0.2.10", line)
        self.assertNotIn("abc.def", line)
        self.assertNotIn("user:password", line)

    def test_untrusted_request_ids_are_replaced(self):
        generated = request_id_or_new("rtsp://user:password@camera/live")
        self.assertNotIn("password", generated)
        self.assertEqual(len(generated), 36)


class MonitoringManifestTests(unittest.TestCase):
    def test_all_monitoring_yaml_is_parseable_and_bounded(self):
        directory = ROOT / "deploy/monitoring"
        for path in directory.glob("*.yaml"):
            with self.subTest(path=path.name):
                documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
                self.assertTrue(documents)
        alloy = (directory / "alloy.values.yaml").read_text(encoding="utf-8")
        self.assertIn(
            'values = ["cluster", "namespace", "service", "container", "level"]',
            alloy,
        )
        self.assertNotIn('target_label  = "camera_id"', alloy)
        self.assertIn("[REDACTED_RTSP_URL]", alloy)

    def test_stack_is_single_replica_bounded_and_requires_digest_rendering(self):
        directory = ROOT / "deploy/monitoring"
        stack = yaml.safe_load(
            (directory / "kube-prometheus-stack.values.yaml").read_text()
        )
        self.assertEqual(stack["prometheus"]["prometheusSpec"]["replicas"], 1)
        self.assertEqual(stack["alertmanager"]["alertmanagerSpec"]["replicas"], 1)
        self.assertEqual(stack["prometheus"]["service"]["type"], "ClusterIP")
        self.assertEqual(stack["prometheus"]["prometheusSpec"]["retention"], "7d")
        self.assertEqual(
            stack["prometheus"]["prometheusSpec"]["externalLabels"]["site_id"],
            "__TVT_SITE_ID__",
        )
        self.assertFalse(stack["kubeEtcd"]["enabled"])
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in directory.glob("*.yaml")
        )
        self.assertNotIn(": latest", combined)
        self.assertIn("__PROMETHEUS_DIGEST__", combined)
        self.assertIn("__LOKI_DIGEST__", combined)
        self.assertIn("__ALLOY_DIGEST__", combined)


class ObservabilitySettingsTests(unittest.TestCase):
    def test_metrics_listener_is_separate_and_cannot_bind_all_interfaces(self):
        settings = Settings.from_environment()
        self.assertNotEqual(settings.listen_port, settings.metrics_port)
        with patch.dict(
            "os.environ", {"TVT_METRICS_LISTEN_HOST": "0.0.0.0"}, clear=False
        ):
            with self.assertRaisesRegex(ValueError, "every host interface"):
                Settings.from_environment()


if __name__ == "__main__":
    unittest.main()
