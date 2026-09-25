from __future__ import annotations

import json
from pathlib import Path
import runpy
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE = runpy.run_path(str(ROOT / "scripts/lib/tvt-release-upgrade.py"))


class ReleaseUpgradePlanTests(unittest.TestCase):
    def write_release(
        self,
        root: Path,
        *,
        version: str,
        overrides: dict[str, str] | None = None,
        rollback_compatible: bool = True,
    ) -> None:
        artifacts = {
            "application_wheel": "wheels/application.whl",
            "input_lock": "release-inputs.lock.json",
            "registry_image": "images/registry.tar",
            "node_reporter_image": "images/node-reporter.tar",
            "node_status_controller_image": "images/node-status-controller.tar",
            "traffic_image": "images/traffic.tar",
            "ui_image": "images/ui.tar",
            "k3s_installer": "k3s/install.sh",
            "k3s_binary": "k3s/k3s",
        }
        manifest = {
            "schema_version": 1,
            "bundle_contract": 1,
            "product": "tvt-edge",
            "release_version": version,
            "source_commit": "1" * 40,
            "artifacts": artifacts,
            "upgrade": {
                "database": {
                    "strategy": "expand-contract",
                    "rollback_compatible": rollback_compatible,
                },
                "operator_actions": [
                    {
                        "operation": "purge-unnamed-persons",
                        "timing": "after_activation",
                        "required": False,
                    }
                ],
            },
        }
        root.mkdir(parents=True)
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        digests = {path: "a" * 64 for path in artifacts.values()}
        digests.update(
            {
                "alembic.ini": "a" * 64,
                "scripts/tvt-edge-operations.sh": "a" * 64,
                "config/pipeline.env": "a" * 64,
                "deploy/systemd/tvt-edge.service": "a" * 64,
                "deploy/k8s/apexfabric-foundation.yaml": "a" * 64,
                "solution-packs/schema/deployment-bundle.schema.json": "a" * 64,
                "hardware/driver-recipe.json": "a" * 64,
                "packages/apt/runtime.deb": "a" * 64,
            }
        )
        digests.update(overrides or {})
        (root / "checksums.sha256").write_text(
            "".join(f"{digest}  {path}\n" for path, digest in sorted(digests.items())),
            encoding="utf-8",
        )

    def test_plan_separates_application_and_cv_changes_from_platform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            current = base / "current"
            target = base / "target"
            self.write_release(current, version="1.0.0")
            self.write_release(
                target,
                version="1.1.0",
                overrides={
                    "wheels/application.whl": "b" * 64,
                    "images/traffic.tar": "c" * 64,
                },
            )

            plan = MODULE["build_plan"](target, current)

            self.assertEqual(plan["activation_mode"], "in_place")
            self.assertTrue(plan["changes"]["host_application"])
            self.assertTrue(plan["changes"]["cv_solution"])
            self.assertFalse(plan["changes"]["node_management"])
            self.assertEqual(plan["platform_changes"], [])
            self.assertEqual(
                plan["upgrade_policy"]["operator_actions"][0]["operation"],
                "purge-unnamed-persons",
            )

    def test_plan_routes_k3s_changes_to_platform_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            current = base / "current"
            target = base / "target"
            self.write_release(current, version="1.0.0")
            self.write_release(
                target,
                version="2.0.0",
                overrides={"k3s/k3s": "d" * 64},
            )

            plan = MODULE["build_plan"](target, current)

            self.assertEqual(plan["activation_mode"], "platform_maintenance")
            self.assertEqual(plan["platform_changes"], ["k3s"])
            self.assertIn(
                "full platform maintenance/reboot may be required",
                plan["availability_impact"],
            )

    def test_plan_routes_forward_only_database_change_to_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            current = base / "current"
            target = base / "target"
            self.write_release(current, version="1.0.0")
            self.write_release(
                target,
                version="1.1.0",
                overrides={"wheels/application.whl": "e" * 64},
                rollback_compatible=False,
            )

            plan = MODULE["build_plan"](target, current)

            self.assertEqual(plan["activation_mode"], "platform_maintenance")
            self.assertIn("database_offline_migration", plan["platform_changes"])


if __name__ == "__main__":
    unittest.main()
