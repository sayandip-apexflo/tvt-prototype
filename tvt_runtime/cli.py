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

from apexfabric.solution_management.renderer import (
    Kubectl,
    reconcile,
    render,
    revision as bundle_revision,
)
from apexfabric.solution_management.validation import load_yaml, validate_bundle
from tvt_runtime.camera_secrets import build_camera_secret_list, secret_names
from tvt_runtime.lifecycle import camera_contract_signature, with_desired_state
from tvt_runtime.state import DEFAULT_STATE_DIR, DeploymentStore, ensure_bundle_has_no_inline_secrets
from tvt_edge.paths import RESOURCE_ROOT


ROOT = RESOURCE_ROOT
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


def live_status(
    client: Kubectl, deployment_id: str, namespace: str
) -> list[dict[str, Any]]:
    result = client.run(
        "get",
        "deployments",
        "-n",
        namespace,
        "-l",
        f"apexfabric.com/deployment-id={deployment_id}",
        "-o",
        "json",
    )
    return [
        {
            "name": item["metadata"]["name"],
            "desired_replicas": item.get("spec", {}).get("replicas", 0),
            "ready_replicas": item.get("status", {}).get("readyReplicas", 0),
            "available_replicas": item.get("status", {}).get(
                "availableReplicas", 0
            ),
            "observed_generation": item.get("status", {}).get(
                "observedGeneration"
            ),
        }
        for item in json.loads(result.stdout).get("items", [])
    ]


def observe_rollouts(
    client: Kubectl,
    deployments: list[dict[str, Any]],
    namespace: str,
    timeout: int,
    restart: bool,
) -> None:
    if timeout < 1 or timeout > 3600:
        raise ValueError("rollout timeout must be between 1 and 3600 seconds")
    for deployment in deployments:
        name = deployment["name"]
        if restart:
            client.run(
                "rollout", "restart", f"deployment/{name}", "-n", namespace
            )
        if deployment.get("desired_replicas", 0) > 0:
            client.run(
                "rollout",
                "status",
                f"deployment/{name}",
                "-n",
                namespace,
                f"--timeout={timeout}s",
            )


def apply_command(
    args: argparse.Namespace,
    bundle: dict[str, Any],
    store: DeploymentStore | None,
    action: str = "apply",
    allow_existing_secrets: bool = False,
) -> dict[str, Any]:
    ensure_bundle_has_no_inline_secrets(bundle)
    client = kubectl_client(args.kubeconfig)
    supplied_inputs = read_secret_inputs(args.secret_inputs)
    secret_list = None
    if supplied_inputs is not None or not allow_existing_secrets:
        secret_list = build_camera_secret_list(
            bundle, supplied_inputs, args.namespace
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

    try:
        if secret_list is not None:
            client.run("apply", "-f", "-", input_text=json.dumps(secret_list))
        report = reconcile(bundle, args.namespace, client)
        observe_rollouts(
            client,
            report["observed"],
            args.namespace,
            args.rollout_timeout,
            restart=secret_list is not None,
        )
        safe_report = {
            **report,
            "configured_secrets": secret_names(secret_list),
        }
        if store is not None:
            safe_report["active_revision"] = store.record_success(
                bundle,
                args.namespace,
                args.registry,
                action,
                safe_report,
            )
        return safe_report
    except Exception as error:
        if store is not None:
            store.record_failure(
                bundle["deployment_id"],
                action,
                str(error),
                attempted_revision=bundle_revision(bundle),
                secret_update_attempted=secret_list is not None,
            )
        raise


def add_runtime_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    command.add_argument("--kubeconfig")
    command.add_argument("--rollout-timeout", type=int, default=180)
    command.add_argument("--dry-run", action="store_true")


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
    add_runtime_options(apply)

    status = commands.add_parser("status", help="show persisted and live status")
    status.add_argument("deployment_id")
    status.add_argument("--local-only", action="store_true")
    status.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    status.add_argument("--kubeconfig")

    list_command = commands.add_parser("list", help="list known deployments")
    list_command.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)

    history = commands.add_parser("history", help="list stored bundle revisions")
    history.add_argument("deployment_id")
    history.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)

    for name in ("start", "stop"):
        lifecycle = commands.add_parser(name, help=f"declaratively {name} a deployment")
        lifecycle.add_argument("deployment_id")
        add_runtime_options(lifecycle)

    rollback = commands.add_parser("rollback", help="apply a stored prior revision")
    rollback.add_argument("deployment_id")
    rollback.add_argument("--revision")
    rollback.add_argument("--secret-inputs", type=Path)
    add_runtime_options(rollback)
    return root


def local_command(args: argparse.Namespace) -> dict[str, Any] | list[dict[str, Any]]:
    store = DeploymentStore(args.state_dir)
    if args.command == "list":
        return store.list_deployments()
    if args.command == "history":
        return store.history(args.deployment_id)
    deployment = store.get_deployment(args.deployment_id)
    if deployment is None:
        raise ValueError(f"unknown deployment {args.deployment_id!r}")
    if args.command == "status":
        if not args.local_only:
            deployment["live"] = live_status(
                kubectl_client(args.kubeconfig),
                args.deployment_id,
                deployment["namespace"],
            )
        return deployment

    current = store.get_bundle(args.deployment_id)
    if args.command in {"start", "stop"}:
        target = with_desired_state(
            current, "Running" if args.command == "start" else "Stopped"
        )
        args.namespace = deployment["namespace"]
        args.registry = deployment["registry"]
        args.secret_inputs = None
        return apply_command(
            args,
            target,
            None if args.dry_run else store,
            action=args.command,
            allow_existing_secrets=True,
        )

    last_event = deployment.get("last_event", {})
    recovering_failed_apply = (
        args.revision is None and last_event.get("outcome") == "failed"
    )
    selected_revision = args.revision or (
        deployment["active_revision"]
        if recovering_failed_apply
        else store.previous_revision(args.deployment_id)
    )
    target = store.get_bundle(args.deployment_id, selected_revision)
    assignments_changed = (
        camera_contract_signature(current) != camera_contract_signature(target)
    )
    failed_secret_update = (
        last_event.get("outcome") == "failed"
        and last_event.get("detail", {}).get("secret_update_attempted", False)
    )
    if (assignments_changed or failed_secret_update) and args.secret_inputs is None:
        raise ValueError(
            "rollback may need to restore camera Secrets; matching "
            "--secret-inputs are required"
        )
    args.namespace = deployment["namespace"]
    args.registry = deployment["registry"]
    return apply_command(
        args,
        target,
        None if args.dry_run else store,
        action="rollback",
        allow_existing_secrets=not assignments_changed and not failed_secret_update,
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in {"status", "list", "history", "start", "stop", "rollback"}:
            raise ValueError(
                "SQLite lifecycle commands were retired by Slice 3; use the "
                "PostgreSQL-backed tvt-edge API and synchronization worker"
            )
        else:
            bundle = load_and_validate(args.bundle, args.schema)
            replace_registry(bundle, getattr(args, "registry", None))
            if args.command == "validate":
                print(f"VALID: {args.bundle}")
            elif args.command == "render":
                print(yaml.safe_dump_all(render(bundle, args.namespace), sort_keys=False))
            else:
                if not args.dry_run:
                    raise ValueError(
                        "direct Apply was retired by Slice 4; commit assignments "
                        "through the tvt-edge API"
                    )
                print(
                    json.dumps(
                        apply_command(args, bundle, None),
                        indent=2,
                        sort_keys=True,
                    )
                )
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
