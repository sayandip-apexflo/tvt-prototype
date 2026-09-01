"""Small local runtime for validating, rendering, and applying Solution Packs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from apexfabric.solution_management.renderer import Kubectl, reconcile, render
from apexfabric.solution_management.validation import load_yaml, validate_bundle
from tvt_runtime.camera_secrets import build_camera_secret_list, secret_names


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "solution-packs/schema/deployment-bundle.schema.json"


def load_and_validate(bundle_path: Path, schema_path: Path) -> dict[str, Any]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    bundle = load_yaml(bundle_path)
    errors = validate_bundle(bundle, schema)
    if errors:
        raise ValueError("bundle validation failed:\n  - " + "\n  - ".join(errors))
    if not isinstance(bundle, dict):
        raise ValueError("bundle must be an object")
    return bundle


def kubectl_client(kubeconfig: str | None) -> Kubectl:
    command = ["kubectl"]
    if not kubeconfig and Path("/usr/local/bin/k3s").exists():
        command = ["k3s", "kubectl"]
    if kubeconfig:
        command.extend(["--kubeconfig", kubeconfig])
    return Kubectl(command)


def replace_registry(bundle: dict[str, Any], registry: str | None) -> None:
    if registry is None:
        return
    registry = registry.strip().rstrip("/")
    if not registry or "://" in registry or any(character.isspace() for character in registry):
        raise ValueError("registry must be a host[:port] value without a URL scheme")
    for application in bundle["applications"]:
        repository = application["image"]["repository"]
        application["image"]["repository"] = repository.replace(
            "__APEXFABRIC_REGISTRY__", registry
        )


def read_secret_inputs(path: Path | None) -> Any:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("secret-inputs file must contain a JSON object")
    return value


def apply_command(args: argparse.Namespace, bundle: dict[str, Any]) -> dict[str, Any]:
    client = kubectl_client(args.kubeconfig)
    secret_list = build_camera_secret_list(
        bundle, read_secret_inputs(args.secret_inputs), args.namespace
    )
    if args.dry_run:
        report = reconcile(bundle, args.namespace, client, dry_run=True)
        return {
            "validated": True,
            "namespace": args.namespace,
            "configured_secrets": secret_names(secret_list),
            "desired_objects": [
                {"kind": item["kind"], "name": item["metadata"]["name"]}
                for item in report["desired"]
            ],
        }

    if secret_list is not None:
        client.run("apply", "-f", "-", input_text=json.dumps(secret_list))
    report = reconcile(bundle, args.namespace, client)
    if secret_list is not None:
        for deployment in report["observed"]:
            name = deployment["name"]
            client.run(
                "rollout", "restart", f"deployment/{name}", "-n", args.namespace
            )
            client.run(
                "rollout",
                "status",
                f"deployment/{name}",
                "-n",
                args.namespace,
                f"--timeout={args.rollout_timeout}s",
            )
    return {**report, "configured_secrets": secret_names(secret_list)}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="TVT single-box Solution Pack runtime")
    root.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    commands = root.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate a DeploymentBundle")
    validate.add_argument("bundle", type=Path)

    render_command = commands.add_parser("render", help="render Kubernetes YAML")
    render_command.add_argument("bundle", type=Path)
    render_command.add_argument("--namespace", default="apexfabric")
    render_command.add_argument("--registry", required=True)

    apply = commands.add_parser("apply", help="server-side apply a DeploymentBundle")
    apply.add_argument("bundle", type=Path)
    apply.add_argument("--namespace", default="apexfabric")
    apply.add_argument("--registry", required=True)
    apply.add_argument("--secret-inputs", type=Path)
    apply.add_argument("--kubeconfig")
    apply.add_argument("--rollout-timeout", type=int, default=180)
    apply.add_argument("--dry-run", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        bundle = load_and_validate(args.bundle, args.schema)
        replace_registry(bundle, getattr(args, "registry", None))
        if args.command == "validate":
            print(f"VALID: {args.bundle}")
        elif args.command == "render":
            print(yaml.safe_dump_all(render(bundle, args.namespace), sort_keys=False))
        else:
            print(json.dumps(apply_command(args, bundle), indent=2, sort_keys=True))
        return 0
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        jsonschema.SchemaError,
        subprocess.CalledProcessError,
    ) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
        print(f"ERROR: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
