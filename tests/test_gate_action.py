"""Offline contract checks for the local promotion-gate action."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "gate" / "action.yml"


def test_gate_action_is_valid_composite_and_fail_closed_by_default() -> None:
    doc = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    assert doc["runs"]["using"] == "composite"
    assert doc["inputs"]["fail-inconclusive"]["default"] == "true"
    assert doc["inputs"]["require"]["default"] == "PROMOTE"
    assert set(doc["outputs"]) == {"verdict", "decision", "reason", "result-digest"}


def test_gate_action_refs_are_sha_pinned_and_inputs_are_not_evaled() -> None:
    text = ACTION.read_text(encoding="utf-8")
    assert "astral-sh/setup-uv@d0d8abe699bfb85fec6de9f7adb5ae17292296ff" in text
    assert "eval " not in text
    assert "shell=True" not in text
    assert 'args=(gate "${SV_BUNDLE}" --require "${SV_REQUIRE}"' in text
    assert 'case "${SV_FAIL_INCONCLUSIVE,,}" in' in text
    assert 'case "${SV_REQUIRE_SIGNATURE,,}" in' in text
    assert "true|1|yes" in text and "false|0|no" in text
    assert "invalid fail-inconclusive boolean" in text
    assert "invalid require-signature boolean" in text
    assert 'uv run --frozen serving-verdict "${args[@]}"' in text
