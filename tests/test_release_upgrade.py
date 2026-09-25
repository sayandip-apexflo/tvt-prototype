from __future__ import annotations

import json
from pathlib import Path
import runpy
import subprocess
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

    def test_prepare_parser_requires_explicit_platform_maintenance_intent(self) -> None:
        arguments = MODULE["parser"]().parse_args(
            [
                "prepare", "--bundle", "/release",
                "--platform-maintenance", "--operation-id", "upgrade-1",
            ]
        )
        self.assertTrue(arguments.platform_maintenance)
        self.assertEqual(arguments.operation_id, "upgrade-1")

    def test_helper_environment_keeps_release_bundle_immutable(self) -> None:
        environment = MODULE["helper_environment"](
            {
                "staged_release": "/opt/tvt/releases/1.1.0",
                "operation_directory": "/var/lib/tvt/install/release-upgrades/test",
            }
        )

        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")

    def test_stage_release_builds_entry_point_at_final_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            bundle = base / "bundle"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(
                json.dumps({"artifacts": {"application_wheel": "wheels/application.whl"}}),
                encoding="utf-8",
            )
            (bundle / "checksums.sha256").write_text("checksums\n", encoding="utf-8")
            (bundle / "wheels").mkdir()
            (bundle / "wheels/application.whl").touch()

            globals_ = MODULE["stage_release"].__globals__
            original_opt_tvt = globals_["OPT_TVT"]
            original_resource_items = globals_["RESOURCE_ITEMS"]
            original_command = globals_["command"]

            def fake_command(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if arguments[:4] == ["python3", "-m", "venv", "--clear"]:
                    venv = Path(arguments[4])
                    (venv / "bin").mkdir(parents=True)
                    (venv / "bin/python").touch(mode=0o755)
                elif arguments[0].endswith("/venv/bin/python"):
                    python = Path(arguments[0])
                    executable = python.with_name("tvt-edge")
                    executable.write_text(f"#!{python}\n", encoding="utf-8")
                    executable.chmod(0o755)
                return subprocess.CompletedProcess(arguments, 0)

            try:
                globals_["OPT_TVT"] = base / "opt/tvt"
                globals_["RESOURCE_ITEMS"] = ("manifest.json", "checksums.sha256")
                globals_["command"] = fake_command

                release = MODULE["stage_release"](bundle, "1.2.3")

                executable = release / "venv/bin/tvt-edge"
                self.assertEqual(
                    executable.read_text(encoding="utf-8").splitlines()[0],
                    f"#!{release}/venv/bin/python",
                )
                self.assertTrue(
                    MODULE["staged_release_is_valid"](
                        release, MODULE["sha256"](bundle / "checksums.sha256")
                    )
                )
            finally:
                globals_["OPT_TVT"] = original_opt_tvt
                globals_["RESOURCE_ITEMS"] = original_resource_items
                globals_["command"] = original_command

    def test_staged_release_rejects_relocated_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "releases/1.2.3"
            executable = release / "venv/bin/tvt-edge"
            executable.parent.mkdir(parents=True)
            executable.write_text(
                "#!/opt/tvt/releases/.1.2.3.prepare.abcd/venv/bin/python\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            (release / "resources").mkdir()
            (release / "resources/manifest.json").write_text("{}", encoding="utf-8")
            (release / ".prepared-bundle-sha256").write_text("digest\n", encoding="utf-8")

            self.assertFalse(MODULE["staged_release_is_valid"](release, "digest"))


if __name__ == "__main__":
    unittest.main()
