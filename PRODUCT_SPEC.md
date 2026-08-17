# Inference Engineering Studio — MVP Product Contract

> Working name. A local-first, evidence-driven UI for learning, benchmarking, and safely optimizing LLM inference.

## Product promise

Run one command and open a local application that turns heterogeneous inference evidence into an auditable engineering decision:

1. What inference hardware/runtime/model is active right now?
2. Are two benchmark results semantically comparable, or do their metric definitions differ?
3. Did a candidate improve synthetic throughput but regress representative production work?
4. Did correctness, tool calling, concurrency, process stability and rollback gates pass?
5. Is the evidence sufficient for `PROMOTE`, `REJECT`, or `INCONCLUSIVE`?
6. Why did a configuration win or fail, and which experiment should be tested next?

The application is not a hosted dashboard, not telemetry SaaS, not another load generator, and not an autonomous production mutator. It binds to loopback, imports or runs allowlisted evidence producers, challenges synthetic wins with representative workloads, and treats rollback proof as part of completion.

## Core differentiator

Existing projects already provide high-quality serving runtimes, benchmark sweeps, GPU dashboards, model switching and even LLM-proposed flag loops. Studio's core primitive is an **evidence-authoritative promotion controller**:

```text
heterogeneous artifacts
        ↓
metric semantic registry
        ↓
comparability + provenance validation
        ↓
synthetic + representative workload gates
        ↓
PROMOTE / REJECT / INCONCLUSIVE
        ↓
rollback proof + local case study
```

MVP-0 runs this controller read-only over two real fixture pairs:

- Qwen3.8 MTP2 → DSpark k=7: expected `PROMOTE`.
- vLLM → SGLang EAGLE: synthetic win followed by production crash; expected `REJECT`.

## MVP-0 user experience

```bash
uv sync --extra dev
uv run inference-studio serve --host 127.0.0.1 --port 8787
# open http://127.0.0.1:8787
```

Primary navigation:

- **Overview** — live GPU, memory, active endpoint/model/container, health, recent experiment verdict.
- **Trials** — baseline/candidate evidence, semantic comparability, gates, verdict and rollback proof.
- **Benchmarks** — raw metric views that keep decode/e2e/aggregate/TTFT definitions separate.
- **Learn** — browsable playbook generated from approved Markdown roots.
- **Evidence** — artifact paths, SHA-256, provenance, raw log availability, claim boundaries.
- **Quick Test** — an allowlisted endpoint smoke/benchmark; no arbitrary command or shell input.
- **Optimize** — read-only proposal panel in MVP-0; Qwen-generated proposal schema may be displayed but cannot execute or mutate production.

## Existing read-only sources

Defaults are configurable, but these local roots are the first integration fixtures:

- `/home/ozkangu/Desktop/DS4-Inference-Engine-Ogrenme-Paketi`
- `/home/ozkangu/Desktop/ds4-inference-research`
- `/home/ozkangu/Desktop/Qwen3.8-27B-DGX-Spark-RTX-6000`

Rules:

- Never edit these source trees.
- Resolve roots and reject symlink traversal outside each approved root.
- Only ingest `.md`, `.json`, `.jsonl`, `.csv`, `.svg`, and `.log.gz` metadata. Do not execute content.
- Artifacts retain their source-relative path and SHA-256.
- Missing/stale/invalid evidence is visible and cannot be presented as verified.

## Architecture

### Backend

Python 3.11+, FastAPI, Pydantic, Uvicorn, stdlib subprocess/urllib/pathlib/hashlib. No database in MVP-0; build an in-memory indexed snapshot from configured roots and expose versioned JSON contracts.

Modules:

- `config` — immutable effective config and approved roots.
- `system_probe` — bounded, read-only probes for NVIDIA GPU, Docker container state, memory, endpoint `/v1/models`.
- `artifact_index` — path-safe discovery and schema-light metadata extraction.
- `experiment_parser` — recognizes our known benchmark artifact schemas without fabricating unsupported fields.
- `metric_registry` — canonical metric definitions, units, aggregation semantics and comparability rules.
- `decision_engine` — deterministic gate evaluation producing `PROMOTE`, `REJECT`, or `INCONCLUSIVE` plus evidence references.
- `playbook` — Markdown catalog/search and safe rendering source contract.
- `quick_test` — fixed built-in endpoint tests only; no user-supplied command, URL scheme, headers, or shell.
- `api` — `/api/v1/...` routes with explicit error models.
- `web` — serves compiled static frontend from the same loopback origin.

### Frontend

React + TypeScript + Vite. No CDN assets; packaged fonts use system fallbacks so the app works offline.

Design direction:

- Linear-style near-black precision: `#08090a`, translucent bordered panels, Inter/system UI, compact hierarchy.
- NVIDIA green `#76b900` only as active/status/metric accent.
- JetBrains/system mono for commands, hashes, metrics.
- Data-dense but calm; no gradients, glassmorphism, fake gauges, or AI slop.
- Responsive down to 390px.

## API contract v1

```text
GET  /api/v1/health
GET  /api/v1/system
GET  /api/v1/runtime
GET  /api/v1/experiments
GET  /api/v1/experiments/{id}
GET  /api/v1/trials
GET  /api/v1/trials/{id}
GET  /api/v1/playbook
GET  /api/v1/playbook/{id}
GET  /api/v1/evidence/{artifact_id}
POST /api/v1/quick-tests/arithmetic
POST /api/v1/quick-tests/tool-call
```

Mutation semantics:

- Quick tests call only the configured loopback OpenAI-compatible endpoint.
- Endpoint is startup config, not request input.
- The request body accepts only fixed test IDs and bounded timeout/max token values.
- No Docker start/stop/update, no config edits, no production promotion in MVP-0.

## Security and runtime invariants

- Default bind must be `127.0.0.1`; non-loopback requires explicit `--allow-non-loopback` and startup warning.
- Reject non-HTTP loopback inference URLs in MVP-0.
- No `shell=True`.
- All subprocesses use argv arrays, timeout, bounded stdout/stderr, and explicit success checks.
- Register and own background resources before start; shutdown must await/verify termination.
- No secret values returned by APIs or logs.
- File roots canonicalized once; reject `..`, absolute child IDs, and symlink escape.
- JSON parsing rejects NaN/Infinity and reports source path plus error.
- UI never labels evidence “verified” unless digest recomputation and recognized contract pass.
- Client-supplied metrics never become authoritative.

## MVP-0 live probes

System probe should produce:

```json
{
  "gpu": {"name": "NVIDIA GB10", "utilization_pct": 0, "temperature_c": 50},
  "memory": {"total_bytes": 0, "available_bytes": 0, "unified": true},
  "runtime": {
    "endpoint": "http://127.0.0.1:8888/v1",
    "healthy": true,
    "models": ["qwen38-27b-unsloth-nvfp4"],
    "container": "fable-qwen38-native-256k-dspark",
    "restart_count": 0,
    "oom_killed": false
  }
}
```

Unknown fields must be `null`/unavailable, never guessed.

## First recognized experiments

1. `qwen38.dspark-ab.v1`
2. `qwen38.sglang-vllm-ab.v1`
3. DS4 serving-shape benchmark JSON where fields can be established from fixtures.

UI must keep incompatible metric definitions visibly separate. Do not compare e2e output tok/s with post-first-token decode tok/s as if identical.

## Decision contract v1

```json
{
  "trial_id": "qwen38-dspark-k7",
  "verdict": "PROMOTE",
  "baseline_artifacts": ["..."],
  "candidate_artifacts": ["..."],
  "comparable_metrics": ["decode_tokens_per_s", "ttft_s"],
  "incomparable_metrics": [],
  "gates": [
    {"id": "request_success", "status": "pass", "evidence": ["artifact:..."]},
    {"id": "tool_call", "status": "pass", "evidence": ["artifact:..."]},
    {"id": "rollback", "status": "pass", "evidence": ["artifact:..."]}
  ],
  "claim_boundary": "..."
}
```

Rules:

- Any required gate missing or unverifiable yields `INCONCLUSIVE`, not pass.
- Any required correctness/stability/rollback gate failing yields `REJECT` regardless of throughput.
- Performance promotion requires a configured minimum effect and semantically comparable metric definitions.
- LLM prose cannot change a deterministic verdict.

## Quick test contract

Arithmetic test:

- fixed prompt template and operands from a small allowlisted fixture set;
- thinking disabled;
- response must parse to expected integer;
- return latency, usage, pass/fail, model ID, timestamp and a result digest.

Tool-call test:

- fixed `get_weather(city)` schema with allowlisted city fixture;
- require a structured tool call and exact argument schema;
- do not execute any external weather call.

## Quality gates

Backend:

```bash
uv run pytest -q
uv run ruff check backend tests
uv run mypy backend
```

Frontend:

```bash
npm test -- --run
npm run typecheck
npm run build
```

E2E:

- start on ephemeral loopback port;
- health/system/runtime/artifact/playbook API smoke;
- real current Qwen arithmetic and tool-call quick tests;
- browser visual check at desktop and mobile sizes;
- shutdown proves port/process cleanup;
- no network calls other than configured loopback endpoint.

## Required tests

- non-loopback bind gate;
- endpoint URL validation;
- path traversal and symlink escape;
- malformed/NaN artifact rejection;
- SHA mismatch not verified;
- unsupported schema remains visible but unverified;
- metric-definition mismatch is incomparable, not silently normalized;
- missing required evidence yields `INCONCLUSIVE`;
- synthetic speed win plus process crash yields `REJECT`;
- DSpark fixture pair yields `PROMOTE` only when all required gates are authoritative;
- subprocess timeout/output truncation;
- Docker absent/degraded behavior;
- `nvidia-smi` unified-memory behavior;
- endpoint down behavior;
- arithmetic/tool-call pass and fail with real local fixture server;
- API error contracts;
- frontend loading/empty/error/healthy states;
- no arbitrary shell/command field anywhere in public request models.

## Non-goals for MVP-0

- No production runtime mutation.
- No arbitrary benchmark command builder.
- No remote/SaaS account.
- No telemetry.
- No model download/quantization.
- No autonomous promotion.
- No trading features.
- No unsupported benchmark leaderboard.

## MVP-1 after independent audit

- Experiment wizard with allowlisted vLLM/SGLang candidate templates.
- Isolated candidate container lifecycle and guaranteed rollback.
- Qwen optimizer emits a typed proposal only.
- Deterministic harness decides `PROMOTE`, `REJECT`, or `INCONCLUSIVE`.
- Pareto frontier across TTFT, decode, c3 throughput, memory, quality and stability.
- Every run writes an immutable evidence bundle and can publish a new local case-study page.
