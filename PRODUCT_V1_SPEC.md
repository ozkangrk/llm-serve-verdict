# Serving Verdict v1 — Benchmark, Compare, Decide

> Normative product direction after v0.2. Existing v0.1 bundle verification and fail-closed decision behavior remain compatibility requirements.

## Product promise

Connect one or two OpenAI-compatible model endpoints, run a frozen inference benchmark, compare the results, and receive an evidence-bound `PROMOTE`, `REJECT`, or `INCONCLUSIVE` decision.

```text
Endpoint A (current) ─┐
                      ├─ benchmark ─ compare ─ verdict ─ local report/UI
Endpoint B (candidate)┘
```

## User journey

1. Open the local UI.
2. Add the current endpoint and model name.
3. Run **Quick benchmark** and save it as the baseline.
4. Change the runtime/configuration or connect a second endpoint.
5. Run the same frozen benchmark as the candidate.
6. Inspect speed, reliability, tool calling, and quality-lite results.
7. Receive a scoped verdict with exact evidence hashes and limitations.

The user never needs to hand-author benchmark JSON for the normal path. Existing artifact import remains available for advanced users.

## What the product does

- Probes OpenAI-compatible model servers.
- Generates its own benchmark artifacts.
- Measures latency, TTFT, decode throughput, concurrency throughput, error rate, and completion counts.
- Runs deterministic regression gates for arithmetic, instruction following, and tool calling.
- Compares baseline and candidate only when protocol dimensions match.
- Produces a tamper-evident verdict bundle.
- Stores trial history locally.
- Explains the decision in a local read-only UI.
- Teaches each metric and limitation in plain language.

## What it does not do

- Does not train or fine-tune models.
- Does not start, stop, or reconfigure model servers.
- Does not execute model-generated code.
- Does not use an LLM judge.
- Does not claim general model intelligence from the built-in quality-lite suite.
- Does not automatically mutate production.
- Does not expose a remote multi-user control plane.

## Endpoint contract

```yaml
id: qwen-current
base_url: http://127.0.0.1:8888/v1
model: qwen38-27b-unsloth-nvfp4
api_key_env: SERVING_VERDICT_API_KEY_QWEN_CURRENT
```

Rules:

- Loopback endpoints are allowed by default.
- Remote endpoints require explicit CLI/UI acknowledgement and `--allow-remote`.
- API keys are read from environment variables and are never written to artifacts, logs, SQLite, HTML, or verdict bundles.
- URLs containing embedded credentials are rejected.
- Redirects to a different host are rejected.
- Preflight requires `/models` or a real chat-completion request; a TCP-open port is not readiness.
- Model identity returned by the server is recorded.

## Benchmark profiles

### `quick` — target under 3 minutes

- 2 warmups, excluded from metrics.
- 3 measured serial short-generation requests.
- 3 measured serial edit/rewrite requests.
- One concurrency-3 group.
- 5 deterministic arithmetic checks.
- One structured tool-call check.
- One invalid-request/error-classification probe.

Purpose: fast local configuration comparison, not model leaderboard ranking.

### `standard` — target under 20 minutes

Everything in `quick`, plus:

- 3 concurrency-3 groups.
- Short and medium prefill workloads.
- 20-item quality-lite regression set.
- 3 repeats per workload with median and spread.
- Cold and warm-prefix phases when protocol support is measurable.

Purpose: stronger promotion gate for a known application workload.

### `replay` — user-owned frozen workload

- Reads a scrubbed JSONL request set from an explicitly selected local file.
- Rejects secrets/authorization headers and unsupported fields.
- Records only the sanitized request-set hash in the public verdict.
- Raw prompts remain local and are excluded from exported reports by default.

Purpose: reproduce the real application workload without claiming broad model quality.

## Built-in workloads

### Performance

- `fresh_short`: generate a new short answer/code-like response.
- `edit_repeat`: rewrite a provided structured text with a small change.
- `prefill_medium`: medium prompt with short output.
- `concurrency_3`: three simultaneous requests with common wall-time accounting.

### Quality-lite regression gates

- Arithmetic: normalized exact numeric answer.
- Instruction following: exact constrained outputs such as fixed labels/JSON keys.
- Tool calling: fixed tool schema, tool name, and argument validation.

Quality-lite detects configuration regressions. It is not MMLU, HumanEval, or a general quality score. Optional `lm-eval-harness` integration is post-v1 and must remain a separate adapter with its own protocol identity.

## Metrics

- `ttft_s`: request start to first generated token.
- `decode_tokens_per_s`: post-first-token decode rate.
- `e2e_output_tokens_per_s`: completion tokens / full request wall time.
- `aggregate_output_tokens_per_s`: total completion tokens / common concurrent wall interval.
- `api_latency_s`: full request latency.
- `request_success_rate`: successful measured requests / attempted measured requests.
- `tool_call_pass_rate`: valid tool calls / tool-call cases.
- `quality_lite_pass_rate`: deterministic passed cases / total quality-lite cases.

Rules:

- Token counts come from API usage or explicit streamed token accounting supported by the adapter; character estimates are forbidden.
- Missing stream/usage capability yields `UNMEASURABLE`, never an invented number.
- Decode, end-to-end, and aggregate throughput are never mixed.
- Every value carries workload, concurrency, output budget, thinking mode, warm/cold phase, procedure version, model identity, and endpoint fingerprint.

## Benchmark artifact

Schema: `serving-verdict.benchmark-run.v1`.

Contains:

- run ID and timestamps;
- endpoint fingerprint without credentials;
- model identity;
- benchmark profile and protocol version;
- frozen workload-set hash;
- warmup evidence;
- per-request timing/token/status records;
- aggregated metrics;
- deterministic gate results;
- environment/tool version metadata;
- canonical artifact digest.

Raw responses are excluded from exported artifacts by default. Optional local debug storage is explicit and gitignored.

## Comparison and verdict

A baseline and candidate are comparable only when protocol identity and workload dimensions match.

Default promotion policy:

- primary performance improvement meets configured threshold;
- TTFT regression is within threshold;
- no required correctness/tool gate fails;
- request success does not regress beyond threshold;
- no process/server failure is observed;
- evidence and benchmark artifact digests verify.

Outcomes:

- `PROMOTE`: required improvement and every required gate passed.
- `REJECT`: a hard gate failed or improvement was insufficient.
- `INCONCLUSIVE`: evidence, capability, comparability, or sample requirements were not met.

Verdicts are scoped to the exact endpoints, model identities, protocol, workload set, and evidence hashes.

## CLI

```text
serving-verdict endpoint check endpoint.yaml
serving-verdict bench run --endpoint endpoint.yaml --profile quick --out run.json
serving-verdict bench compare --baseline baseline.json --candidate candidate.json --out verdict.json
serving-verdict demo --out-dir demo
serving-verdict history [--case CASE] [--json]
serving-verdict verify verdict.json [--json]
serving-verdict serve --host 127.0.0.1 --port 8787 --data-dir data
```

Exit codes remain:

- 0: successful command, including valid REJECT/INCONCLUSIVE verdict creation.
- 2: usage/config/preflight error where no valid artifact can be produced.
- 4: integrity verification failure.

## Local UI

Primary navigation:

1. **Run benchmark** — endpoint form, model discovery, profile choice, progress.
2. **Compare** — baseline/candidate selector, metric deltas, gate results, verdict.
3. **Trials** — append-only local history.
4. **Learn** — plain-language explanations tied to displayed metrics and real cases.

The UI remains loopback-only. Benchmark creation is an intentional local mutation and must use server-controlled benchmark specs; users cannot submit commands, code, arbitrary paths, or arbitrary HTTP headers.

## Benchmark lifecycle

```text
DRAFT → PREFLIGHT → WARMUP → MEASURE → GRADE → SEALED
                    └──────── failure ────────→ FAILED
```

- Every transition is persisted.
- Cancellation produces `CANCELLED`, never a partial successful run.
- Server errors, timeouts, and malformed responses are classified separately.
- Failed and rejected runs remain visible.
- Sealed artifacts are immutable.

## Safety invariants

- No `shell=True` and no arbitrary subprocess input.
- No model-generated code execution.
- No Docker/runtime lifecycle control.
- Fixed built-in request templates are server-controlled.
- Replay input is bounded, sanitized, and local-only.
- Endpoint credentials never persist.
- HTTP client timeouts and total run budgets are mandatory.
- Concurrency is bounded by profile.
- Cancellation closes pending HTTP requests.
- Exported reports contain no raw prompts unless explicitly requested.
- UI escapes all endpoint/model/artifact text.

## TDD acceptance gates

1. Endpoint URL validation, credential redaction, redirect-host rejection.
2. Real OpenAI-compatible mock server preflight and model identity capture.
3. Streaming TTFT/decode measurement with deterministic synthetic timing fixtures.
4. Non-stream/usage missing → UNMEASURABLE, never estimated.
5. Warmups excluded from every aggregate.
6. Concurrency uses a shared wall interval and correct aggregate tokens/s.
7. Tool-call arguments validated against the frozen schema.
8. Arithmetic/instruction graders reject malformed or extra output according to protocol.
9. Timeout, HTTP 500, disconnect, malformed SSE, zero-token, cancellation classifications.
10. API keys absent from all artifacts, logs, DB rows, API responses, and HTML.
11. Same run inputs → same canonical protocol/workload hashes.
12. Baseline/candidate protocol mismatch → INCONCLUSIVE.
13. Hard-gate failure overrides speed improvement.
14. Append-only history and sealed artifact immutability.
15. UI benchmark wizard, progress, cancellation, comparison, three verdict states, mobile and accessibility.
16. Fresh-clone portable demo produces one PROMOTE and one REJECT.
17. Full pytest, Ruff, mypy, build, wheel-install smoke, and adversarial exact-tree review.

## Delivery phases

### v0.2 — Product shell

- Portable demo.
- Verdict-first responsive UI.
- Trial registry and safe artifact archive.
- CI, docs, screenshots, and release packaging.

### v0.3 — Inference engineering automation (implemented)

- Credential-safe endpoint config/preflight and bounded quick profile.
- Usage-backed streaming serial/concurrency measurement and quality/tool gates.
- Serving Doctor, Capacity Planner, deterministic Config Advisor and inert
  rollback recipes.
- Baseline/candidate compare, seeded sweep planning and Pareto frontier.
- Privacy-safe replay and CI regression decision contract.
- Loopback Automation Wizard with bounded ephemeral jobs, progress and
  cooperative cancellation/result discard.

### v0.4 — Trustworthy promotion authority (implemented)

- Repeated-trial deterministic bootstrap confidence intervals and explicit
  statistical uncertainty.
- Structured evidence manifest and claim boundary in bundle schema v0.4.
- Offline DSSE + Ed25519 signing, local trust policy and required-signature
  verification.
- Formal adapter SDK with strict experimental vLLM, SGLang and GuideLLM adapters.
- Stable CI promotion gate, GitHub Action and repository/release security
  hardening.

### v0.5 — Inference Lab (foundation implemented; execution/UI in progress)

- Digest-pinned inert runtime templates and pure typed planning.
- Owned lifecycle state machine with cleanup proof and honest cancellation.
- Bounded Prometheus-text telemetry normalization and ring buffer.
- Real opt-in Docker/GPU backend, Lab/Live/Decide APIs and final UI remain
  release gates and are not part of the v0.4 execution claim.

### Future work

- A larger standard benchmark profile.
- Persisted automation-job history (current jobs are intentionally ephemeral).
- Real Docker/GPU Inference Lab E2E and external AIPerf orchestration.
- PyPI trusted publishing and Windows support.

## Release claim boundary

The repository may claim that it runs the frozen quick benchmark only for the
implemented OpenAI-compatible protocol and bounded workloads. An endpoint,
runtime or model without a linked real run remains `UNTESTED`. No claim may use
“any model,” “production-ready,” “state-of-the-art,” or general quality language
without corresponding evidence. PROMOTE remains scoped to the exact workload,
policy, gates and bound artifacts.
