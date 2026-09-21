from __future__ import annotations

import json
from pathlib import Path

import pytest

from tvt_runtime.image_compat.edge_runtime.agent.edge_agent import (
    COMPATIBILITY_ID,
    PlanCompilerError,
    compile_plan,
)


def desired_state() -> dict[str, object]:
    return {
        "edge_id": "edge-01",
        "revision": 3,
        "cameras": [
            {
                "camera_id": "camera-01",
                "source": "file:/run/secrets/apexfabric/camera-01.rtsp",
                "solution_pack": "tvt-mills-pilot",
                "fps": 5,
                "apps": ["anpr"],
                "config": {},
            }
        ],
    }


def test_compiler_emits_only_a_non_secret_deterministic_receipt(tmp_path: Path) -> None:
    source = tmp_path / "desired_state.json"
    source.write_text(json.dumps(desired_state()), encoding="utf-8")
    models = tmp_path / "models"
    models.mkdir()

    receipt_path = compile_plan(source, tmp_path / "plans", models)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert receipt["compiler"] == COMPATIBILITY_ID
    assert receipt["revision"] == 3
    assert receipt["camera_ids"] == ["camera-01"]
    assert set(receipt) == {
        "camera_ids",
        "compiler",
        "desired_state_sha256",
        "format_version",
        "revision",
    }
    assert "source" not in receipt_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(revision=0),
        lambda value: value["cameras"][0].update(source="rtsp://not-allowed"),
        lambda value: value["cameras"][0].update(apps=["unknown"]),
        lambda value: value["cameras"][0].update(camera_id="INVALID"),
    ],
)
def test_compiler_rejects_invalid_contract_inputs(
    tmp_path: Path, mutation: object
) -> None:
    document = desired_state()
    mutation(document)  # type: ignore[operator]
    source = tmp_path / "desired_state.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    models = tmp_path / "models"
    models.mkdir()

    with pytest.raises(PlanCompilerError):
        compile_plan(source, tmp_path / "plans", models)
