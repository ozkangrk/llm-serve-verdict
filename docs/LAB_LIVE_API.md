# Lab jobs and Live snapshot API

This v0.5 slice exposes a bounded loopback state model for the upcoming
Lab/Live/Decide UI. It does not expose Docker directly and it does not enable the
Inference Lab from browser input.

## Enablement boundary

A Lab job can start only when both are true at server construction:

1. the server process environment contains `SERVING_VERDICT_ENABLE_LAB=1`;
2. a trusted internal `LabExecutor` has been installed.

The default server currently installs no Lab executor, so the capabilities route
is visible but reports `enabled: false`. This is intentional until the validated
Docker plan factory is wired to the API in the next gate.

```text
GET /api/v1/lab/capabilities
POST /api/v1/lab/jobs
GET /api/v1/lab/jobs/{job_id}
GET /api/v1/lab/jobs/{job_id}/live
POST /api/v1/lab/jobs/{job_id}/cancel
```

## Start request

Exact schema: `serving-verdict.lab-start.v0.5`.

```json
{
  "schema_version": "serving-verdict.lab-start.v0.5",
  "template_id": "vllm.openai",
  "model_ref": "qwen-fast",
  "overrides": {
    "gpu_memory_utilization": 0.8,
    "max_model_len": 4096
  },
  "trial_count": 3,
  "statistical_seed": 17,
  "telemetry_interval_s": 2,
  "telemetry_max_samples": 120
}
```

Unknown fields are rejected. The browser cannot provide an image, argv,
entrypoint, mount, environment variable or secret. `model_ref` is bounded
relative text and rejects POSIX and Windows traversal. Overrides are flat,
bounded and immutable after trusted parsing; the runtime template performs the
engine-specific allowlist validation before Docker.

## Job contract

- one active Lab job per process;
- at most 32 retained terminal jobs by default;
- 32-hex opaque job IDs;
- at most 64 monotonic progress events;
- executor callbacks close at terminal return and cannot mutate later snapshots;
- each Live update obeys the request's telemetry sample/failure limit;
- fixed lifecycle states only;
- exception text is reduced to fixed error categories;
- cooperative cancellation sets a server-owned event;
- a cancelled job discards a late successful artifact;
- `CLEANUP_FAILED` always suppresses the result;
- only `SUCCEEDED` with `cleanup_verified: true` exposes `result`.

Jobs are ephemeral process memory. They are not the final evidence store.
Atomic lab-run bundle publication remains a separate release gate.

## Live snapshot

`GET /api/v1/lab/jobs/{job_id}/live` returns only normalized telemetry dataclasses,
never raw Prometheus text:

```json
{
  "job_id": "...",
  "sequence": 3,
  "samples": [],
  "failures": [],
  "summary": []
}
```

Each update is bounded to 3,600 samples and 3,600 fixed-category failures. Sample
and failure offsets must be sorted and non-negative. Typed samples reject unsafe
label names and secret-like/path-bearing values even when they do not come through
the Prometheus parser. Summary rows are generated server-side using the same
deterministic min/mean/p50/p95/p99/max/latest code that seals and verifies the
final lab-run artifact.

## Current boundary

Implemented and tested:

- disabled-by-default capabilities;
- strict start parsing and immutable trusted request;
- in-memory single-flight job manager;
- state, cancellation and cleanup-result precedence;
- bounded Live snapshot with deterministic distribution statistics;
- stable 400/404/409/503 API errors;
- secret-free public payloads.

Still pending:

- operator image-digest and model-root registry;
- real `LabExecutor` wiring to `DockerLabBackend` and `LabRunOrchestrator`;
- atomic evidence directory publication;
- streaming transport (the current API is bounded polling/snapshot);
- Lab/Live/Decide browser UI;
- real vLLM/SGLang GPU dogfood.
