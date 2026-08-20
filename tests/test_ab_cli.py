from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml
from test_benchmark_runner import mock_server

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / ".venv" / "bin" / "llm-serve-verdict"
BASE_SECRET = "ab-base-secret-123"
CANDIDATE_SECRET = "ab-candidate-secret-456"


def config_file(
    directory: Path, name: str, base_url: str, env_name: str
) -> Path:
    path = directory / f"{name}.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "serving-verdict.endpoint.v1",
                "id": name,
                "base_url": base_url,
                "model": "test-model",
                "api_key_env": env_name,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "AB_BASE_KEY": BASE_SECRET,
            "AB_CANDIDATE_KEY": CANDIDATE_SECRET,
        },
    )


def test_bench_ab_runs_two_endpoints_and_writes_atomic_experiment(tmp_path: Path) -> None:
    with mock_server("ok") as baseline_url, mock_server("ok") as candidate_url:
        baseline = config_file(tmp_path, "baseline", baseline_url, "AB_BASE_KEY")
        candidate = config_file(
            tmp_path, "candidate", candidate_url, "AB_CANDIDATE_KEY"
        )
        out = tmp_path / "ab-output"
        result = run_cli(
            "bench",
            "ab",
            "--baseline-endpoint",
            str(baseline),
            "--candidate-endpoint",
            str(candidate),
            "--trials",
            "2",
            "--threshold",
            "0.05",
            "--seed",
            "17",
            "--iterations",
            "1000",
            "--out-dir",
            str(out),
            "--json",
        )

    assert result.returncode == 0, result.stderr
    stdout, end = json.JSONDecoder().raw_decode(result.stdout)
    assert result.stdout[end:].strip() == ""
    assert stdout["ok"] is True
    assert stdout["decision"]["verdict"] in {"PROMOTE", "REJECT", "INCONCLUSIVE"}
    manifest = json.loads((out / "experiment.json").read_text())
    assert manifest["schema_version"] == "serving-verdict.ab-experiment.v0.1"
    assert manifest["execution_order"] == [
        ["baseline", "candidate"],
        ["candidate", "baseline"],
    ]
    assert len(manifest["runs"]["baseline"]) == 2
    assert len(manifest["runs"]["candidate"]) == 2
    assert (out / "statistics.json").is_file()
    all_text = result.stdout + result.stderr + "".join(
        path.read_text() for path in out.glob("*.json")
    )
    assert BASE_SECRET not in all_text
    assert CANDIDATE_SECRET not in all_text

    verified = run_cli("bench", "ab-verify", str(out), "--json")
    assert verified.returncode == 0, verified.stderr
    verification = json.loads(verified.stdout)
    assert verification["valid"] is True
    assert verification["decision"] == manifest["decision"]

    enforced = run_cli(
        "bench",
        "ab-verify",
        str(out),
        "--require",
        "PROMOTE",
        "--fail-inconclusive",
        "--json",
    )
    expected_exit = {
        "PROMOTE": 0,
        "REJECT": 5,
        "INCONCLUSIVE": 6,
    }[manifest["decision"]["verdict"]]
    assert enforced.returncode == expected_exit
    enforcement = json.loads(enforced.stdout)
    assert enforcement["required"] == "PROMOTE"
    assert enforcement["blocked"] is (expected_exit != 0)

    invalid_flags = run_cli(
        "bench", "ab-verify", str(out), "--fail-inconclusive", "--json"
    )
    assert invalid_flags.returncode == 2
    assert "requires --require PROMOTE" in json.loads(invalid_flags.stdout)["error"]

    candidate_run = out / "candidate-trial-001.json"
    tampered = json.loads(candidate_run.read_text())
    tampered["run_status"] = "degraded"
    candidate_run.write_text(json.dumps(tampered), encoding="utf-8")
    failed = run_cli("bench", "ab-verify", str(out), "--json")
    assert failed.returncode == 4
    assert json.loads(failed.stdout)["valid"] is False


def test_bench_ab_refuses_existing_output_without_mutation(tmp_path: Path) -> None:
    out = tmp_path / "existing"
    out.mkdir()
    sentinel = out / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    result = run_cli(
        "bench",
        "ab",
        "--baseline-endpoint",
        str(tmp_path / "missing-a.yaml"),
        "--candidate-endpoint",
        str(tmp_path / "missing-b.yaml"),
        "--trials",
        "2",
        "--out-dir",
        str(out),
    )
    assert result.returncode == 2
    assert sentinel.read_text() == "keep"
    assert sorted(path.name for path in out.iterdir()) == ["keep.txt"]
