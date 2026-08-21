"""Static shipped-surface contract for the Lab / Live / Decide workspace."""
from __future__ import annotations

import json
import re
from pathlib import Path

from tests.helpers_ui import boot, click, flush, set_json

WEB = Path(__file__).resolve().parents[1] / "src" / "serving_verdict" / "web"


def ui_js() -> str:
    return (WEB / "ui.js").read_text(encoding="utf-8")


def ids(markup: str) -> set[str]:
    return set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', markup))


def luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92
        if value <= 0.04045
        else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def test_lab_live_decide_workspace_has_safe_complete_dom_contract() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    present = ids(html)
    required = {
        "nav-lab",
        "lab-view",
        "lab-tab-configure",
        "lab-tab-live",
        "lab-tab-decide",
        "lab-capability-state",
        "lab-disabled-panel",
        "lab-form",
        "lab-template",
        "lab-model-ref",
        "lab-trials",
        "lab-seed",
        "lab-telemetry-interval",
        "lab-start",
        "lab-cancel",
        "lab-job-state",
        "lab-progress-timeline",
        "lab-live-sequence",
        "lab-live-summary",
        "lab-live-failures",
        "lab-result",
        "lab-cleanup-state",
    }
    assert required <= present, sorted(required - present)
    for forbidden in (
        "lab-image",
        "lab-argv",
        "lab-entrypoint",
        "lab-mount",
        "lab-environment",
        "lab-api-key",
        "lab-docker-socket",
    ):
        assert forbidden not in present
    assert 'id="lab-view" class="hidden"' in html
    assert "SERVING_VERDICT_ENABLE_LAB=1" in html
    assert "min / mean / p50 / p95 / p99 / max / latest" in html
    assert 'role="tablist"' in html
    assert html.count('role="tab"') == 3
    assert html.count('role="tabpanel"') == 3


def test_lab_workspace_javascript_uses_only_bounded_lab_api() -> None:
    js = (WEB / "ui.js").read_text(encoding="utf-8")
    for route in (
        "/api/v1/lab/capabilities",
        "/api/v1/lab/jobs",
        "/live",
        "/cancel",
    ):
        assert route in js
    assert "showLab" in js
    assert "renderLabJob" in js
    assert "renderLabLive" in js
    assert "labTabKey" in js and 'addEventListener("keydown"' in js
    assert "lab-template" in js
    assert "lab-model-ref" in js
    for forbidden in (
        "/var/run/docker.sock",
        "--privileged",
        "--network=host",
        "docker run",
    ):
        assert forbidden not in js


def test_small_live_stat_labels_meet_wcag_aa_on_lightest_card_surface() -> None:
    css = (WEB / "ui.css").read_text(encoding="utf-8")
    matched = re.search(r"\.lab-stat-values dt \{[^}]*color:\s*(#[0-9a-fA-F]{6})", css)
    assert matched is not None
    foreground = luminance(matched.group(1))
    background = luminance("#151719")
    contrast = (max(foreground, background) + 0.05) / (
        min(foreground, background) + 0.05
    )
    assert contrast >= 4.5, contrast


def test_lab_disabled_capability_is_honest_and_start_is_disabled() -> None:
    ctx = boot(ui_js())
    set_json(
        ctx,
        "__routes",
        {"/api/v1/lab/capabilities": {"enabled": False, "executor_available": False}},
    )
    click(ctx, "nav-lab")
    assert "DISABLED" in ctx.eval("__ids['lab-capability-state'].textContent")
    assert ctx.eval("__ids['lab-start'].disabled") is True
    assert "hidden" not in ctx.eval("__ids['lab-disabled-panel'].className").split()


def test_lab_success_renders_live_distribution_and_cleanup_gated_decide() -> None:
    job_id = "a" * 32
    digest = "sha256:" + "b" * 64
    job = {
        "job_id": job_id,
        "state": "SUCCEEDED",
        "phase": "SUCCEEDED",
        "cancel_requested": False,
        "cleanup_verified": True,
        "events": [
            "PLANNED", "PULLING", "NETWORK_CREATING", "STARTING", "READY",
            "BENCHMARKING", "FINALIZING", "STOPPING", "SUCCEEDED",
        ],
        "error_kind": None,
        "result": {
            "schema_version": "serving-verdict.lab-run.v0.5",
            "artifact_digest": digest,
        },
    }
    live = {
        "job_id": job_id,
        "sequence": 4,
        "samples": [],
        "failures": [{"offset_s": 1.0, "status": "timeout"}],
        "summary": [{
            "metric_id": "runtime.requests.running",
            "labels": [["engine", "0"]],
            "unit": "requests",
            "direction": "neutral",
            "count": 4,
            "min": 1.0,
            "mean": 2.75,
            "p50": 2.5,
            "p95": 4.7,
            "p99": 4.94,
            "max": 5.0,
            "latest": 5.0,
        }],
    }
    ctx = boot(ui_js())
    set_json(
        ctx,
        "__routes",
        {
            "/api/v1/lab/capabilities": {"enabled": True},
            "/api/v1/lab/jobs": job,
            f"/api/v1/lab/jobs/{job_id}/live": live,
        },
    )
    click(ctx, "nav-lab")
    for node_id, value in {
        "lab-template": "vllm.openai",
        "lab-model-ref": "qwen-fast",
        "lab-max-model-len": "4096",
        "lab-gpu-memory": "0.8",
        "lab-trials": "3",
        "lab-seed": "17",
        "lab-telemetry-interval": "2",
        "lab-telemetry-max": "120",
    }.items():
        ctx.eval(f"__ids[{json.dumps(node_id)}].value = {json.dumps(value)}")
    ctx.eval("__ids['lab-form']._listeners.submit[0]({preventDefault:function(){}})")
    flush(ctx)
    assert ctx.eval("__ids['lab-cleanup-state'].textContent") == "VERIFIED"
    assert digest in ctx.eval("__ids['lab-result'].textContent")
    assert ctx.eval("__ids['lab-progress-timeline'].children.length") == 9
    assert ctx.eval("__ids['lab-live-sequence'].textContent") == "4"
    assert ctx.eval("__ids['lab-live-summary'].children.length") == 1
    assert "lab-shell-wide" in ctx.eval("__ids['lab-shell'].className").split()
    assert "hidden" not in ctx.eval("__ids['lab-decide-pane'].className").split()
