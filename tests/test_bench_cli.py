"""CLI contracts for `endpoint check` and `bench run` (quick profile).

Exit codes: 0 success (including a degraded but sealed run), 2 usage/config/
preflight error (no artifact produced), 4 artifact integrity failure.
JSON mode emits exactly one JSON object on stdout; diagnostics go to stderr.
The artifact file and all stdout must never contain the API key.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml
from test_benchmark_runner import mock_server

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / ".venv" / "bin" / "serving-verdict"
SECRET = "cli-secret-abc123xyz"


@contextmanager
def cli_env(mode: str = "ok") -> Iterator[str]:
    with mock_server(mode) as url:
        yield url


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "SERVING_VERDICT_API_KEY_CLI": SECRET},
    )


def endpoint_file(directory: Path, base_url: str) -> Path:
    path = directory / "endpoint.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "serving-verdict.endpoint.v1",
                "id": "cli-target",
                "base_url": base_url,
                "model": "test-model",
                "api_key_env": "SERVING_VERDICT_API_KEY_CLI",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_endpoint_check_exit_0_json_and_secret_free(tmp_path: Path) -> None:
    with cli_env() as base_url:
        cfg = endpoint_file(tmp_path, base_url)
        r = run_cli("endpoint", "check", str(cfg), "--json")
    assert r.returncode == 0, r.stderr
    doc, end = json.JSONDecoder().raw_decode(r.stdout)
    assert r.stdout[end:].strip() == ""
    assert doc["ok"] is True
    assert doc["endpoint_id"] == "cli-target"
    assert doc["served_model"] == "served-test-model"
    assert doc["models_probe"] == "matched"
    assert SECRET not in r.stdout
    assert SECRET not in r.stderr


def test_endpoint_check_usage_errors_exit_2_no_stdout(tmp_path: Path) -> None:
    # missing file
    r = run_cli("endpoint", "check", str(tmp_path / "nope.yaml"))
    assert r.returncode == 2
    assert r.stdout.strip() == ""
    # bad yaml
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: wrong\n", encoding="utf-8")
    r = run_cli("endpoint", "check", str(bad))
    assert r.returncode == 2
    # env var not set
    with mock_server() as base_url:
        cfg = tmp_path / "nok.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "serving-verdict.endpoint.v1",
                    "id": "cli-target",
                    "base_url": base_url,
                    "model": "test-model",
                    "api_key_env": "SVC_UNSET_KEY_XYZ",
                }
            ),
            encoding="utf-8",
        )
        r = run_cli("endpoint", "check", str(cfg))
    assert r.returncode == 2
    assert "not set" in r.stderr.lower()


def test_bench_run_exit_0_writes_verifiable_artifact(tmp_path: Path) -> None:
    with cli_env() as base_url:
        cfg = endpoint_file(tmp_path, base_url)
        out = tmp_path / "run.json"
        r = run_cli(
            "bench", "run",
            "--endpoint", str(cfg),
            "--profile", "quick",
            "--out", str(out),
            "--json",
        )
    assert r.returncode == 0, r.stderr
    assert out.is_file()
    doc = json.loads(out.read_text())
    assert doc["schema_version"] == "serving-verdict.benchmark-run.v1"
    assert doc["gates"]["arithmetic"]["passed"] is True
    stdout_doc, end = json.JSONDecoder().raw_decode(r.stdout)
    assert r.stdout[end:].strip() == ""
    assert stdout_doc["run_id"] == doc["run_id"]
    assert stdout_doc["gates"]["tool_call"]["passed"] is True
    assert SECRET not in r.stdout
    assert SECRET not in r.stderr
    assert SECRET not in out.read_text()


def test_bench_run_unknown_profile_exit_2(tmp_path: Path) -> None:
    with cli_env() as base_url:
        cfg = endpoint_file(tmp_path, base_url)
        r = run_cli(
            "bench", "run",
            "--endpoint", str(cfg),
            "--profile", "standard",
            "--out", str(tmp_path / "o.json"),
        )
    assert r.returncode == 2
    assert "standard" in r.stderr


class _BoomHandler(BaseHTTPRequestHandler):
    """Models list fine, chat probe always 500 -> preflight must fail."""

    def log_message(self, format: str, *args: object) -> None:  # type: ignore[override]
        return

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"data": [{"id": "test-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(size)
        body = json.dumps({"error": "down"}).encode()
        self.send_response(500)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_bench_run_preflight_failure_exit_2_no_artifact(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BoomHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = endpoint_file(
            tmp_path, f"http://127.0.0.1:{server.server_port}/v1"
        )
        out = tmp_path / "out.json"
        r = run_cli(
            "bench", "run",
            "--endpoint", str(cfg),
            "--profile", "quick",
            "--out", str(out),
        )
        assert r.returncode == 2
        assert r.stdout.strip() == ""
        assert "preflight" in r.stderr.lower()
        assert not out.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
