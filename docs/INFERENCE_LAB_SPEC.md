# Serving Verdict Inference Lab — Normative Product and Runtime Contract

**Document:** v0.5 Inference Lab specification
**Status:** Approved for implementation
**Date:** 2026-08-20
**Normative dependencies:** `MVP_SPEC.md`, `docs/PRD-v0.4-v1.0.md`,
`docs/STATISTICS.md`, `docs/BUNDLE_SCHEMA.md`, `docs/ADAPTERS.md`

## 1. Product promise

An inference engineer selects a known serving runtime and model, launches it in
an isolated local lab, runs repeated benchmarks, watches bounded live telemetry,
and receives a signed, evidence-bound promotion verdict.

```text
Select runtime + model
→ validate immutable lab plan
→ explicit opt-in start
→ readiness + repeated benchmark + telemetry
→ sealed lab-run evidence
→ statistical/comparability/hard gates
→ signed PROMOTE / REJECT / INCONCLUSIVE
→ mandatory teardown
```

The defensible product is not container orchestration or another metrics
dashboard. It is the binding of exact runtime, image, model, flags, hardware,
workload and telemetry to a deterministic promotion decision.

## 2. Architecture boundary

```text
┌──────────────────────────────────────────────────────────────┐
│ Decision Authority (pure; no Docker/network lifecycle)       │
│ bundle + statistics + policy + signatures + verification     │
└──────────────────────────▲───────────────────────────────────┘
                           │ sealed lab-run artifact
┌──────────────────────────┴───────────────────────────────────┐
│ Inference Lab (explicitly enabled local execution plane)      │
│ templates │ planner │ lifecycle owner │ benchmark │ telemetry│
└──────────────▲──────────────────────▲────────────────────────┘
               │                      │ read-only scrape
        Docker Engine           OpenAI + /metrics
               │                      │
       disposable runtime container + private lab network
```

The Decision Authority package must never import or call Docker. Inference Lab
may be disabled entirely; every v0.1–v0.4 offline workflow remains usable.

## 3. Non-goals

Inference Lab is not:

- a Kubernetes platform, scheduler, autoscaler or gateway;
- a replacement for vLLM, SGLang, llama.cpp, AIPerf, GuideLLM, Prometheus,
  Grafana or OpenTelemetry;
- a persistent TSDB;
- a multi-user remote Docker control plane;
- an arbitrary Compose/YAML executor;
- a shell or arbitrary container command runner;
- an automatic production deployment or traffic-shifting system;
- an LLM-based tuning or promotion agent.

Kubernetes/KServe/llm-d/Dynamo integration remains evidence import and inert
recipe output until separately specified.

## 4. Security invariants

1. **Production non-mutation by default.** Lab owns only resources bearing the
   exact run label and random-free deterministic run ID it created. It may read
   existing container state but may never stop, attach, rename or remove it.
2. **Explicit enablement.** Real lifecycle requires both server/operator config
   (`SERVING_VERDICT_ENABLE_LAB=1`) and an explicit per-run start action. Browser
   input alone cannot enable Docker access.
3. **No arbitrary Compose.** Users select a shipped template and typed overrides.
   Uploaded YAML, arbitrary images, entrypoints, commands, devices, mounts,
   capabilities and environment variables are rejected.
4. **Digest-pinned images.** Every executable image reference must contain
   `@sha256:<64 lowercase hex>`. Mutable tags alone are invalid.
5. **Registry allowlist.** Initial registries: `docker.io`, `ghcr.io`, `nvcr.io`.
   Changing the allowlist is operator config, not a browser field.
6. **No privileged execution.** No privileged mode, host PID/IPC/network,
   added Linux capabilities, Docker/containerd socket mount, host device except
   the declared GPU request, or writable host bind mount.
7. **Model roots are explicit.** A local model reference resolves under an
   operator-configured model root, rejects traversal/symlinks/special files,
   and mounts read-only. The artifact binds a verified content manifest digest.
8. **Secrets stay environment-only.** Template/start payloads contain only env
   variable names. Secret values never enter run specs, Docker labels, argv,
   artifacts, logs, state, errors or DOM.
9. **Private network.** Each run gets one internal `sv-lab-<run_id>` network.
   Published ports bind only `127.0.0.1` on an allocator-selected port.
10. **No container exec.** Readiness, inference and metrics use declared HTTP
    endpoints. The lifecycle adapter exposes no exec/attach/copy API.
11. **Bounded resources.** Template declares GPU count, memory/CPU constraints,
    startup deadline, run deadline, scrape interval, maximum samples and output
    bounds. No unbounded pull log, benchmark log or telemetry series.
12. **Cleanup is part of success.** A run cannot be `SUCCEEDED` until its owned
    container and network are confirmed absent. Cleanup failure is terminal
    `CLEANUP_FAILED`, never a warning attached to success.
13. **Cancellation is honest.** Cancellation requests stop later phases and
    suppress result publication. Non-interruptible HTTP/Docker calls may finish;
    state remains `CANCEL_REQUESTED` until cleanup is verified.
14. **Docker access is root-equivalent.** This residual risk is always shown in
    CLI/UI/docs. Rootless Podman may become another backend; it is not claimed by
    this release.

## 5. Runtime template schema

Schema ID: `serving-verdict.runtime-template.v0.5`.

Required immutable fields:

```json
{
  "schema_version": "serving-verdict.runtime-template.v0.5",
  "template_id": "vllm.openai",
  "template_version": "1",
  "engine": "vllm",
  "image": "docker.io/vllm/vllm-openai@sha256:...",
  "registry": "docker.io",
  "entrypoint": ["python", "-m", "vllm.entrypoints.openai.api_server"],
  "fixed_args": ["--host", "0.0.0.0"],
  "parameters": {},
  "environment_names": [],
  "container_port": 8000,
  "readiness": {"path": "/v1/models", "method": "GET"},
  "metrics": {"path": "/metrics", "format": "prometheus-text"},
  "gpu": {"count": 1},
  "limits": {},
  "timeouts": {},
  "digest": "sha256:..."
}
```

Initial template IDs:

- `vllm.openai`
- `sglang.openai`
- `llamacpp.server`

Templates are declarative data shipped in the wheel. Template files cannot
contain code or hooks. Their self-digest excludes only the digest field.

### 5.1 Typed parameter allowlists

A parameter declaration contains a canonical flag, exact type, range/enum,
default, repeatability and whether restart is required. Initial common fields:

- model reference (separate safe model-root resolution, never raw argv);
- served model name;
- max model length;
- GPU memory utilization;
- tensor parallel size;
- max concurrent sequences;
- KV-cache dtype / quantization options when the selected engine supports them;
- seed when the runtime supports a deterministic seed.

Unknown, duplicate, canonical-plus-alias, wrong-type and out-of-range values
fail before any pull/network/container side effect. Boolean values are not ints.
Final effective argv is an immutable list and is provenance; command display is
`shlex.join` only. No shell executes it.

## 6. Lab run spec and identity

Schema ID: `serving-verdict.lab-run-spec.v0.5`.

A validated plan binds:

- template ID/version/digest;
- exact image digest and registry;
- exact effective argv;
- model identity and verified model-manifest digest;
- selected GPU IDs/count (no secret UUID persistence; stable local index/class);
- benchmark profile/digest, repeated trial count and statistical seed/spec;
- telemetry metric allowlist, interval and sample bound;
- effective deadlines and resource limits;
- engine endpoint protocol;
- operator enablement state.

`run_id = "lab-" + sha256(canonical substantive spec)[:24]`. No random nonce,
wall clock or secret participates. The run artifact digest excludes only
explicit display timestamps; resource IDs include a short process-unique suffix
to avoid collision while the artifact retains the deterministic run ID.

## 7. Lifecycle contract

States:

```text
PLANNED → PULLING → NETWORK_CREATING → STARTING → READY
→ BENCHMARKING → FINALIZING → STOPPING → SUCCEEDED

Any active state → CANCEL_REQUESTED → STOPPING → CANCELLED
Any active state → STOPPING → FAILED
STOPPING cleanup failure → CLEANUP_FAILED
```

The lifecycle owner records each resource before creation is attempted. Every
backend call receives a bounded deadline. A finalizer always runs, including
`BaseException`, process shutdown and benchmark serialization failure.

Before remove, ownership is revalidated using all of:

- exact `serving-verdict.owner=lab` label;
- exact run-resource ID;
- expected template digest;
- locally held backend resource handle.

Name prefix alone is never deletion authority.

## 8. Docker backend capability

The web process must not expose a generic Docker proxy. The backend is an
internal capability with only:

- inspect engine/version/capabilities;
- resolve/pull one digest-pinned image;
- create/remove one internal labelled network;
- create/start/inspect/stop/remove one labelled container;
- read bounded container logs;
- verify owned resources absent.

No generic request method, exec, attach, build, commit, push, prune, volume,
secret, swarm, plugin or arbitrary API path is exposed.

Unit tests use an injected fake backend. Real Docker E2E is opt-in and runs only
against disposable resources/images. Tests first prove zero calls on invalid
plans.

## 9. Benchmark orchestration

The first workflow runs the existing frozen quick benchmark for at least
`min_trials` independent measured runs, with explicit warmup outside the sample.
External benchmark tools remain adapters:

- GuideLLM v2 report adapter;
- vLLM serve result adapter;
- SGLang serve result adapter;
- AIPerf adapter is planned after its schema is frozen.

Every trial records protocol/workload/model/runtime comparability dimensions.
A transport or adapter failure is not a synthetic INCONCLUSIVE verdict; it is a
failed run. A valid but statistically insufficient sealed trial set yields
INCONCLUSIVE through the Decision Authority.

## 10. Live telemetry

MVP scrapes allowlisted Prometheus text metrics from the lab runtime only. It
does not embed Prometheus.

`TelemetrySample` binds:

- monotonic offset from measured-run start;
- canonical metric ID, source metric name and procedure version;
- finite value/unit;
- immutable labels selected by allowlist (no request/model text labels);
- scrape status (`ok`, `timeout`, `invalid`).

Storage is a bounded in-memory ring buffer:

- interval: 1–60 seconds;
- maximum 3,600 samples total per run;
- maximum 64 metric series;
- maximum 64 KiB per scrape response;
- no unbounded label cardinality;
- raw scrape body is never persisted.

The sealed artifact stores normalized samples plus aggregate min/mean/p50/p95/
max where mathematically meaningful. Missing telemetry is `UNMEASURABLE`; it is
never zero. Production monitoring is read-only and uses the existing endpoint
remote opt-in plus future Prometheus/OTel adapters; it cannot drive deployment.

## 11. Lab-run artifact

Schema ID: `serving-verdict.lab-run.v0.5`.

It contains:

- validated run spec and template digest;
- Docker engine/Compose/container runtime versions;
- resolved image repo digest;
- GPU class/count/driver/runtime and relevant memory capacity;
- model manifest/revision/quantization/tokenizer identity;
- readiness result;
- benchmark trial artifact digests;
- normalized bounded telemetry and aggregate summaries;
- lifecycle event chain and cleanup proof;
- errors as fixed categories only;
- exact claim boundary;
- canonical digest.

No raw API key, prompts, outputs, container logs, host paths, username, hostname,
GPU serial/UUID or arbitrary exception text is stored.

Only a `SUCCEEDED` artifact with verified cleanup may become promotion evidence.
`FAILED`, `CANCELLED` and `CLEANUP_FAILED` remain auditable operational records,
not candidate evidence.

## 12. API contract

Loopback server routes are disabled unless Lab is operator-enabled:

- `GET /api/lab/capabilities`
- `GET /api/lab/templates`
- `POST /api/lab/plan` (pure; no side effects)
- `POST /api/lab/runs` (explicit start of a validated plan digest)
- `GET /api/lab/runs/{id}`
- `POST /api/lab/runs/{id}/cancel`
- `GET /api/lab/runs/{id}/telemetry`
- `GET /api/lab/runs/{id}/artifact`

Start payload references a previously created plan digest; the server rebuilds
and revalidates the effective plan before execution. It does not trust browser
state. One GPU-affecting lab run is active by default. Job count, TTL and result
sizes are bounded as in the existing ephemeral automation manager.

## 13. UI — Lab / Live / Decide

The local UI has three first-class workspaces:

### Lab

Template/model selector, typed parameter controls, capacity warnings, immutable
plan preview, image/template digest and explicit security confirmation. Start is
disabled when Docker/GPU/readiness prerequisites are not satisfied.

### Live

Lifecycle timeline, warmup/measured phase, request progress and bounded charts:
TTFT p50/p95/p99, ITL, output throughput, request/error rate, concurrency, GPU
memory/utilization and KV-cache metrics when exposed. Missing series are shown as
UNMEASURABLE, never a flat zero line.

### Decide

Baseline/candidate distributions, confidence interval versus policy threshold,
hard gates, evidence trust/signature, exact candidate delta, claim boundary,
experiment lineage and inert launch/rollback recipe.

Design requirements:

- self-contained HTML/JS/CSS; no CDN/Node runtime requirement;
- premium dark visual system with strong hierarchy, not a generic admin table;
- keyboard navigation, visible focus, aria-live states, reduced motion;
- minimum 44px touch targets and verified 390px mobile layout;
- no secret or raw external string inserted with `innerHTML`;
- charts never alter decision semantics;
- real browser E2E and final desktop/mobile screenshots.

## 14. Acceptance demo

On one Docker+NVIDIA host with no manual container commands:

1. plan a digest-pinned vLLM or SGLang template;
2. reject an unpinned/unknown image before backend calls;
3. start a disposable labelled lab network/container;
4. prove readiness and capture image/runtime/GPU/model provenance;
5. run warmup plus repeated measured trials while live telemetry streams;
6. seal a lab-run artifact and verify its digest;
7. repeat with one typed candidate change;
8. produce a confidence-aware signed verdict;
9. display Lab/Live/Decide views in a real browser;
10. cancel a third run and prove result suppression plus complete teardown;
11. mutate one artifact metric and prove verify exit 4;
12. prove the pre-existing production container/endpoint remains unchanged.

## 15. Test matrix

Required RED/GREEN coverage:

- template exact keys, self-digest, image digest, registry, parameter aliases,
  types/ranges and deterministic argv;
- path traversal, symlink/special model files and manifest mutation;
- zero Docker calls on plan failure;
- backend capability cannot express exec/privileged/host mounts;
- lifecycle every transition, start failure, timeout, cancellation, serialization
  failure, cleanup failure, ownership mismatch and process shutdown;
- secret/path/raw-log absence from complete JSON/DOM/errors;
- telemetry parser bounds, malformed lines, non-finite values, cardinality,
  timeouts and ring-buffer eviction;
- repeated benchmark/statistics/comparability/hard-gate precedence;
- API disabled/enabled, plan-digest replay, one-active bound and no data-dir
  mutation before successful artifact publication;
- QuickJS DOM behavior plus real Playwright desktop/mobile E2E;
- fake backend unit suite, Docker CPU smoke, NVIDIA real-runtime smoke;
- full pytest/Ruff/mypy/build and exact-tree adversarial review.

## 16. Delivery stages

### Stage A — Safe lab foundation

1. runtime template schema + three inert templates;
2. pure planner + Docker capability interface;
3. lifecycle owner with fake backend and cleanup proofs;
4. bounded telemetry parser/ring buffer;
5. lab-run artifact schema.

### Stage B — Real vertical slice

1. opt-in Docker backend;
2. one official digest-pinned runtime template exercised on this host;
3. repeated benchmark integration;
4. loopback Lab/Live APIs;
5. Lab/Live/Decide UI;
6. real GPU acceptance demo and screenshots.

### Stage C — Ecosystem adapters

1. GuideLLM/AIPerf orchestration/import;
2. Prometheus and OpenTelemetry read-only production adapters;
3. KServe/llm-d/Dynamo inert deployment recipe/reference integrations;
4. experiment and promoted-baseline lineage.

No stage merges to main with unresolved HIGH/MEDIUM findings.
