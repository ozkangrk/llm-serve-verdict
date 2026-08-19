"""Deterministic rule engine, ranked recommendations, and recipe assembly.

``advise`` is a pure function of the raw input document:

1. parse + validate the typed input (fail closed on anything odd);
2. validate current flags against the runtime allowlist (fail closed on
   unsafe/unknown/bad-type/out-of-range/conflicting/secret-looking flags);
3. evaluate the fixed rule set in a fixed priority order; a rule only fires
   when its evidence metric is present (no-evidence rules never fire);
4. with no usable evidence at all the status is INCONCLUSIVE and no
   recommendations or recipe are emitted;
5. build the inert launch/rollback recipe (dry-run only) and stamp the result
   with the canonical artifact digest.

Rules never claim guaranteed gains; every recommendation carries its
confidence boundary and a one-variable experiment plan.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from serving_verdict.advisor import ADVISOR_SCHEMA_VERSION, AdvisorError
from serving_verdict.advisor.flags import validate_mapping
from serving_verdict.advisor.profiles import get_profile
from serving_verdict.advisor.recipe import Recipe, build_recipe
from serving_verdict.advisor.schema import AdvisorInput, parse_advisor_input

# ---------------------------------------------------------------------------
# thresholds (fixed, deterministic)
# ---------------------------------------------------------------------------

TTFT_HIGH_S = 1.0
THROUGHPUT_LOW_TPS = 32.0
KV_PRESSURE = 0.8
REQUEST_FAILURE_RATE = 0.05
TOOL_QUALITY_MIN = 0.95

RULE_ORDER: tuple[str, ...] = (
    "OOM_RISK",
    "TTFT_HIGH",
    "THROUGHPUT_LOW",
    "KV_PRESSURE",
    "CONCURRENCY_HEADROOM",
    "REQUEST_FAILURES",
    "TOOL_QUALITY_LOW",
)

# Rules that push capacity are suppressed when memory is in an OOM state.
_OOM_SUPPRESSES = frozenset({"THROUGHPUT_LOW", "CONCURRENCY_HEADROOM"})


@dataclass(frozen=True)
class ExperimentPlan:
    variable: str
    to_value: str
    hold_fixed: tuple[str, ...]
    success_metric: str
    abort_condition: str


@dataclass(frozen=True)
class Rollback:
    command_label: str
    argv: list[str]


@dataclass(frozen=True)
class Recommendation:
    rank: int
    rule_id: str
    title: str
    reason: str
    evidence_metric: str
    evidence_value: str
    expected_direction: str
    risk: str
    confidence_boundary: str
    experiment: ExperimentPlan
    rollback: Rollback


@dataclass(frozen=True)
class AdvisorResult:
    schema_version: str
    runtime_family: str
    status: str  # "OK" | "INCONCLUSIVE"
    reason_codes: tuple[str, ...]
    recommendations: tuple[Recommendation, ...]
    recipe: Recipe | None
    flag_state: dict[str, Any]
    digest: str
    created_at: str


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _to_value(
    mode: str | Any, current: Any, spec_min: float = 0.0, spec_max: float = 1.0
) -> Any:
    """Compute the single-variable target for a recommendation."""
    if isinstance(mode, (int, float, bool)):
        return mode
    if mode == "double":
        return int(round(float(current) * 2))
    if mode == "half":
        return max(int(round(spec_min)), int(float(current)) // 2)
    if mode == "reduce":
        return round(float(current) - 0.05, 2)
    raise AdvisorError(f"unknown to_value mode: {mode!r}")


def _clamped(value: Any, lo: float, hi: float) -> Any:
    if isinstance(value, bool):
        return value
    num = float(value)
    if num < lo:
        num = float(lo)
    if num > hi:
        num = float(hi)
    return num


def _build_rules() -> dict[str, dict[str, Any]]:
    """Per-family rule metadata: evidence, text, experiment variable/mode."""
    return {
        "vllm": {
            "OOM_RISK": {
                "metric": "memory_status",
                "direction": "stabilize",
                "risk": "high",
                "title": "Reduce memory pressure to stabilize serving",
                "experiment": ("gpu_memory_utilization", 0.85, "memory_status",
                               "GPU memory headroom restored without new OOM events"),
                "reason": (
                    "Memory status is 'oom'. Raising batch or context capacity in this state risks "
                    "hard failure, so capacity is being stabilized instead of pushed."
                ),
            },
            "TTFT_HIGH": {
                "metric": "ttft_s",
                "direction": "decrease",
                "risk": "medium",
                "title": "Cut time-to-first-token with chunked prefill",
                "experiment": ("enable_chunked_prefill", True, "ttft_s",
                               "TTFT improves and decode throughput holds within 10%"),
                "reason": (
                    "TTFT is well above the 1.0 s threshold. Enabling chunked prefill schedules "
                    "prefill in pieces so new requests stop waiting for long prefills."
                ),
            },
            "THROUGHPUT_LOW": {
                "metric": "decode_tokens_per_s",
                "direction": "increase",
                "risk": "medium",
                "title": "Raise allowed max tokens to increase decode throughput",
                "experiment": ("allowed_max_tokens", "double", "e2e_tokens_per_s",
                               "Decode throughput rises and request failure rate stays <= 5%"),
                "reason": (
                    "Decode throughput is below the 32 tok/s threshold at the measured concurrency. "
                    "Larger per-request token budgets let the scheduler batch decode work more "
                    "effectively; this is a hypothesis to verify, not an assured outcome."
                ),
            },
            "KV_PRESSURE": {
                "metric": "kv_cache_usage",
                "direction": "decrease",
                "risk": "medium",
                "title": "Relieve KV cache pressure by lowering batched-token cap",
                "experiment": ("max_num_batched_tokens", "half", "kv_cache_usage",
                               "KV usage drops below 80% without throughput collapse"),
                "reason": (
                    "KV cache usage is above 80%. Halving the batched-token cap reduces peak KV "
                    "pressure per scheduling step; watch for throughput trade-off."
                ),
            },
            "CONCURRENCY_HEADROOM": {
                "metric": "max_concurrency",
                "direction": "increase",
                "risk": "medium",
                "title": "Increase scheduled concurrency toward the target",
                "experiment": ("max_num_seqs", "double", "max_concurrency",
                               "Sustained concurrency reaches target with e2e regression < 15%"),
                "reason": (
                    "Benchmark concurrency is far below the operator target. Raising the sequence "
                    "cap lets the runtime schedule more in-flight requests."
                ),
            },
            "REQUEST_FAILURES": {
                "metric": "request_failure_rate",
                "direction": "decrease",
                "risk": "medium",
                "title": "Investigate elevated request failure rate",
                "experiment": ("workload: retry/backoff policy", "bounded retries with backoff",
                               "request_failure_rate",
                               "Failure rate drops below 5% with no latency collapse"),
                "reason": (
                    "Request failure rate exceeds the 5% threshold. Failures are not explained by "
                    "any single allowlisted flag; the experiment targets the client/workload retry "
                    "policy rather than the launch flags."
                ),
            },
            "TOOL_QUALITY_LOW": {
                "metric": "tool_call_success_rate",
                "direction": "increase",
                "risk": "high",
                "title": "Improve tool-call success rate at the model layer",
                "experiment": ("model: decoding settings / tool prompt",
                               "lower temperature + tool-call prompt revision",
                               "tool_call_success_rate",
                               "Tool success rate >= 95% with no regression in e2e quality checks"),
                "reason": (
                    "Tool-call success is below the 95% threshold. This is a model/prompt-layer "
                    "issue; launch flags cannot fix it, so the experiment targets decoding settings "
                    "and the tool-calling prompt."
                ),
            },
        },
        "sglang": {
            "OOM_RISK": {
                "metric": "memory_status",
                "direction": "stabilize",
                "risk": "high",
                "title": "Reduce static memory fraction to stabilize serving",
                "experiment": ("mem_fraction_static", 0.8, "memory_status",
                               "GPU memory headroom restored without new OOM events"),
                "reason": (
                    "Memory status is 'oom'. Lowering the static memory fraction returns GPU "
                    "memory to the CUDA allocator instead of holding it statically."
                ),
            },
            "TTFT_HIGH": {
                "metric": "ttft_s",
                "direction": "decrease",
                "risk": "medium",
                "title": "Cut time-to-first-token with larger prefill chunks",
                "experiment": ("chunked_prefill_size", 4096, "ttft_s",
                               "TTFT improves and decode throughput holds within 10%"),
                "reason": (
                    "TTFT is well above the 1.0 s threshold. Larger prefill chunks let the "
                    "scheduler overlap prefill and decode more effectively."
                ),
            },
            "THROUGHPUT_LOW": {
                "metric": "decode_tokens_per_s",
                "direction": "increase",
                "risk": "medium",
                "title": "Raise max running requests to increase throughput",
                "experiment": ("max_running_requests", "double", "e2e_tokens_per_s",
                               "Decode throughput rises and request failure rate stays <= 5%"),
                "reason": (
                    "Decode throughput is below the 32 tok/s threshold. Allowing more concurrent "
                    "running requests can raise aggregate decode throughput; verify memory headroom."
                ),
            },
            "KV_PRESSURE": {
                "metric": "kv_cache_usage",
                "direction": "decrease",
                "risk": "medium",
                "title": "Relieve KV cache pressure by trimming static memory",
                "experiment": ("mem_fraction_static", "reduce", "kv_cache_usage",
                               "KV usage drops below 80% without throughput collapse"),
                "reason": (
                    "KV cache usage is above 80%. Trimming the static memory fraction reclaims "
                    "memory for the CUDA allocator under KV pressure."
                ),
            },
            "CONCURRENCY_HEADROOM": {
                "metric": "max_concurrency",
                "direction": "increase",
                "risk": "medium",
                "title": "Increase running-request cap toward the target",
                "experiment": ("max_running_requests", "double", "max_concurrency",
                               "Sustained concurrency reaches target with e2e regression < 15%"),
                "reason": (
                    "Benchmark concurrency is far below the operator target. Raising the "
                    "running-request cap lets more requests be scheduled concurrently."
                ),
            },
            "REQUEST_FAILURES": {
                "metric": "request_failure_rate",
                "direction": "decrease",
                "risk": "medium",
                "title": "Investigate elevated request failure rate",
                "experiment": ("workload: retry/backoff policy", "bounded retries with backoff",
                               "request_failure_rate",
                               "Failure rate drops below 5% with no latency collapse"),
                "reason": (
                    "Request failure rate exceeds the 5% threshold. The experiment targets the "
                    "client/workload retry policy rather than the launch flags."
                ),
            },
            "TOOL_QUALITY_LOW": {
                "metric": "tool_call_success_rate",
                "direction": "increase",
                "risk": "high",
                "title": "Improve tool-call success rate at the model layer",
                "experiment": ("model: decoding settings / tool prompt",
                               "lower temperature + tool-calling prompt revision",
                               "tool_call_success_rate",
                               "Tool success rate >= 95% with no regression in e2e quality checks"),
                "reason": (
                    "Tool-call success is below the 95% threshold. Launch flags cannot fix this; "
                    "the experiment targets decoding settings and the tool-calling prompt."
                ),
            },
        },
        "llama.cpp": {
            "OOM_RISK": {
                "metric": "memory_status",
                "direction": "stabilize",
                "risk": "high",
                "title": "Reduce context size to stabilize serving",
                "experiment": ("ctx_size", "half", "memory_status",
                               "GPU/VRAM headroom restored without new OOM events"),
                "reason": (
                    "Memory status is 'oom'. Halving the context size halves the worst-case "
                    "KV memory footprint."
                ),
            },
            "TTFT_HIGH": {
                "metric": "ttft_s",
                "direction": "decrease",
                "risk": "medium",
                "title": "Enable flash attention to cut time-to-first-token",
                "experiment": ("flash_attn", True, "ttft_s",
                               "TTFT improves and output quality holds"),
                "reason": (
                    "TTFT is well above the 1.0 s threshold. Flash attention reduces prefill "
                    "memory traffic and typically shortens first-token latency."
                ),
            },
            "THROUGHPUT_LOW": {
                "metric": "decode_tokens_per_s",
                "direction": "increase",
                "risk": "medium",
                "title": "Raise token batch size to increase decode throughput",
                "experiment": ("n_batch", "double", "e2e_tokens_per_s",
                               "Decode throughput rises and request failure rate stays <= 5%"),
                "reason": (
                    "Decode throughput is below the 32 tok/s threshold. A larger prompt/completion "
                    "batch improves kernel utilization at the cost of latency per step."
                ),
            },
            "KV_PRESSURE": {
                "metric": "kv_cache_usage",
                "direction": "decrease",
                "risk": "medium",
                "title": "Relieve KV cache pressure by trimming context",
                "experiment": ("ctx_size", "half", "kv_cache_usage",
                               "KV usage drops below 80% without throughput collapse"),
                "reason": (
                    "KV cache usage is above 80%. Halving the context size reduces the KV "
                    "footprint per sequence."
                ),
            },
            "CONCURRENCY_HEADROOM": {
                "metric": "max_concurrency",
                "direction": "increase",
                "risk": "medium",
                "title": "Increase parallel slots toward the target",
                "experiment": ("n_parallel", "double", "max_concurrency",
                               "Sustained concurrency reaches target with e2e regression < 15%"),
                "reason": (
                    "Benchmark concurrency is far below the operator target. More parallel "
                    "scheduling slots let the runtime serve more simultaneous requests."
                ),
            },
            "REQUEST_FAILURES": {
                "metric": "request_failure_rate",
                "direction": "decrease",
                "risk": "medium",
                "title": "Investigate elevated request failure rate",
                "experiment": ("workload: retry/backoff policy", "bounded retries with backoff",
                               "request_failure_rate",
                               "Failure rate drops below 5% with no latency collapse"),
                "reason": (
                    "Request failure rate exceeds the 5% threshold. The experiment targets the "
                    "client/workload retry policy rather than the launch flags."
                ),
            },
            "TOOL_QUALITY_LOW": {
                "metric": "tool_call_success_rate",
                "direction": "increase",
                "risk": "high",
                "title": "Improve tool-call success rate at the model layer",
                "experiment": ("model: decoding settings / tool prompt",
                               "lower temperature + tool-calling prompt revision",
                               "tool_call_success_rate",
                               "Tool success rate >= 95% with no regression in e2e quality checks"),
                "reason": (
                    "Tool-call success is below the 95% threshold. Launch flags cannot fix this; "
                    "the experiment targets decoding settings and the tool-calling prompt."
                ),
            },
        },
    }


def _evidence(inp: AdvisorInput, rule: str) -> tuple[str, Any] | None:
    """Return (metric, value) evidence for a rule, or None when no evidence.

    A metric is usable evidence only when it is present and meaningful:
    throughput metrics must be strictly positive, rates are bounded in
    [0, 1]. Zero throughput is not evidence of "low throughput" — it is
    missing/invalid evidence.
    """
    b, c = inp.benchmark, inp.capacity
    if rule == "OOM_RISK":
        if c.memory_status == "oom":
            return ("memory_status", "oom")
        return None
    if rule == "TTFT_HIGH":
        if b.ttft_s is not None and b.ttft_s > TTFT_HIGH_S:
            return ("ttft_s", b.ttft_s)
        return None
    if rule == "THROUGHPUT_LOW":
        if b.decode_tokens_per_s is not None and 0 < b.decode_tokens_per_s < THROUGHPUT_LOW_TPS:
            return ("decode_tokens_per_s", b.decode_tokens_per_s)
        return None
    if rule == "KV_PRESSURE":
        if c.kv_cache_usage is not None and c.kv_cache_usage > KV_PRESSURE:
            return ("kv_cache_usage", c.kv_cache_usage)
        return None
    if rule == "CONCURRENCY_HEADROOM":
        if (
            b.max_concurrency is not None
            and c.concurrency_target is not None
            and b.max_concurrency < c.concurrency_target
        ):
            return ("max_concurrency", b.max_concurrency)
        return None
    if rule == "REQUEST_FAILURES":
        if b.request_failure_rate is not None and b.request_failure_rate > REQUEST_FAILURE_RATE:
            return ("request_failure_rate", b.request_failure_rate)
        return None
    if rule == "TOOL_QUALITY_LOW":
        if (
            b.tool_call_success_rate is not None
            and b.tool_call_success_rate < TOOL_QUALITY_MIN
        ):
            return ("tool_call_success_rate", b.tool_call_success_rate)
        return None
    return None


def _to_dict(result: AdvisorResult) -> dict[str, Any]:
    """Plain-JSON dict for the artifact (canonicalize-safe)."""
    recs = []
    for r in result.recommendations:
        recs.append({
            "rank": r.rank,
            "rule_id": r.rule_id,
            "title": r.title,
            "reason": r.reason,
            "evidence_metric": r.evidence_metric,
            "evidence_value": r.evidence_value,
            "expected_direction": r.expected_direction,
            "risk": r.risk,
            "confidence_boundary": r.confidence_boundary,
            "experiment": {
                "variable": r.experiment.variable,
                "to_value": r.experiment.to_value,
                "hold_fixed": list(r.experiment.hold_fixed),
                "success_metric": r.experiment.success_metric,
                "abort_condition": r.experiment.abort_condition,
            },
            "rollback": {
                "command_label": r.rollback.command_label,
                "argv": list(r.rollback.argv),
            },
        })
    recipe_dict = None
    if result.recipe is not None:
        recipe_dict = {
            "family": result.recipe.family,
            "launch_argv": list(result.recipe.launch_argv),
            "rollback_argv": list(result.recipe.rollback_argv),
            "flag_diff": [
                {
                    "flag": d.flag,
                    "cli": d.cli,
                    "before": d.before,
                    "after": d.after,
                }
                for d in result.recipe.flag_diff
            ],
            "rendered_shell": result.recipe.rendered_shell,
            "rendered_rollback_shell": result.recipe.rendered_rollback_shell,
        }
    return {
        "schema_version": result.schema_version,
        "runtime_family": result.runtime_family,
        "status": result.status,
        "reason_codes": list(result.reason_codes),
        "recommendations": recs,
        "recipe": recipe_dict,
        "flag_state": {
            "current": dict(result.flag_state.get("current", {})),
            "overrides": dict(result.flag_state.get("overrides", {})),
        },
        "created_at": result.created_at,
        "digest": result.digest,
    }


def advise(doc: dict[str, Any]) -> AdvisorResult:
    """Run the deterministic advisor over a raw input document."""
    inp = parse_advisor_input(doc)

    # Fail closed on any current-flag violation.
    profile = get_profile(inp.runtime_family)
    violations = validate_mapping(profile.command.flag_map, inp.current_flags)
    if violations:
        detail = "; ".join(f"{v.flag}: {v.detail}" for v in violations)
        raise AdvisorError(f"flag violations (fail-closed): {detail}")

    usable = inp.benchmark.usable() or inp.capacity.usable()
    if not usable:
        provisional = AdvisorResult(
            schema_version=ADVISOR_SCHEMA_VERSION,
            runtime_family=inp.runtime_family,
            status="INCONCLUSIVE",
            reason_codes=("NO_EVIDENCE",),
            recommendations=(),
            recipe=None,
            flag_state={"current": dict(inp.current_flags), "overrides": {}},
            digest="",
            created_at="",
        )
        payload = _to_dict(provisional)
        payload.pop("created_at", None)
        payload.pop("digest", None)
        from serving_verdict.canonical import canonicalize, digest_payload

        digest = digest_payload(canonicalize(payload))
        return AdvisorResult(
            schema_version=ADVISOR_SCHEMA_VERSION,
            runtime_family=inp.runtime_family,
            status="INCONCLUSIVE",
            reason_codes=("NO_EVIDENCE",),
            recommendations=(),
            recipe=None,
            flag_state={"current": dict(inp.current_flags), "overrides": {}},
            digest=digest,
            created_at=datetime.now(UTC).isoformat(),
        )

    # Fixed-priority rule evaluation with OOM suppression. Suppression only
    # applies when the rule would otherwise fire on evidence; an absent
    # metric is never "suppressed".
    meta = _build_rules()[inp.runtime_family]
    suppress = _OOM_SUPPRESSES if inp.capacity.memory_status == "oom" else frozenset()
    fired: list[tuple[str, str, Any]] = []  # (rule_id, metric, value)
    for rule_id in RULE_ORDER:
        ev = _evidence(inp, rule_id)
        if ev is None:
            continue
        if rule_id in suppress:
            continue
        fired.append((rule_id, *ev))

    # Build the recipe once: top recommendation drives a single-variable
    # override, but only for allowlisted flags that are already set in the
    # current configuration (recipes never introduce new flags).
    overrides: dict[str, Any] = {}
    if fired:
        top_rule, _top_metric, _top_value = fired[0]
        exp = meta[top_rule]["experiment"]
        var, mode = exp[0], exp[1]
        flag_spec = profile.command.flag_map.get(var)
        if flag_spec is not None and var in inp.current_flags:
            target = _to_value(mode, inp.current_flags[var])
            target = _clamped(target, flag_spec.min, flag_spec.max)
            from serving_verdict.advisor.flags import validate_flag

            violation = validate_flag(flag_spec, target)
            if violation is not None:
                raise AdvisorError(f"computed override rejected (fail-closed): {violation.detail}")
            overrides[var] = target

    model_path = inp.model_path
    if model_path is None:
        model_path = "model"  # opaque placeholder; never a real path

    recipe = build_recipe(
        family=inp.runtime_family,
        model_path=model_path,
        current_flags=dict(inp.current_flags),
        overrides=overrides,
    )

    # Assemble ranked recommendations.
    recs: list[Recommendation] = []
    hold_fixed = tuple(
        k for k in sorted(inp.current_flags) if k not in overrides
    )
    for rank, (rule_id, metric, value) in enumerate(fired, start=1):
        m = meta[rule_id]
        exp = m["experiment"]
        var, mode, success_metric, success = exp[0], exp[1], exp[2], exp[3]
        to_value = _fmt(mode) if var not in inp.current_flags else _fmt(
            _to_value(mode, inp.current_flags[var])
        )
        recs.append(
            Recommendation(
                rank=rank,
                rule_id=rule_id,
                title=m["title"],
                reason=m["reason"],
                evidence_metric=metric,
                evidence_value=f"{metric}={_fmt(value)}",
                expected_direction=m["direction"],
                risk=m["risk"],
                confidence_boundary=(
                    "Rule-of-thumb bound from a single measurement point: no variance estimate, "
                    "no A/B re-benchmark performed. Verify with the one-variable experiment "
                    "under an identical workload before treating this as evidence; no outcome "
                    "is assured."
                ),
                experiment=ExperimentPlan(
                    variable=var,
                    to_value=to_value,
                    hold_fixed=hold_fixed,
                    success_metric=success_metric,
                    abort_condition=success,
                ),
                rollback=Rollback(
                    command_label=f"restore current {inp.runtime_family} launch configuration",
                    argv=recipe.rollback_argv,
                ),
            )
        )

    reason_codes = [r.rule_id for r in recs] if recs else ["NO_BOTTLENECK_DETECTED"]

    flag_state = {"current": dict(inp.current_flags), "overrides": overrides}

    # Canonical artifact digest over the payload minus volatile fields.
    provisional = AdvisorResult(
        schema_version=ADVISOR_SCHEMA_VERSION,
        runtime_family=inp.runtime_family,
        status="OK",
        reason_codes=tuple(reason_codes),
        recommendations=tuple(recs),
        recipe=recipe,
        flag_state=flag_state,
        digest="",
        created_at="",
    )
    payload = _to_dict(provisional)
    payload.pop("created_at", None)
    payload.pop("digest", None)
    from serving_verdict.canonical import canonicalize, digest_payload

    digest = digest_payload(canonicalize(payload))
    return AdvisorResult(
        schema_version=ADVISOR_SCHEMA_VERSION,
        runtime_family=inp.runtime_family,
        status="OK",
        reason_codes=tuple(reason_codes),
        recommendations=tuple(recs),
        recipe=recipe,
        flag_state=flag_state,
        digest=digest,
        created_at=datetime.now(UTC).isoformat(),
    )


# ``to_dict`` is the public serializer used by recipe.artifact_to_dict.
to_dict = _to_dict

__all__ = ["advise", "to_dict", "AdvisorResult", "ExperimentPlan", "Rollback", "Recommendation"]
