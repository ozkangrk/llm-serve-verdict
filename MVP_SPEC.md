# Serving Verdict MVP v0.1 — Normative Contract

> This document narrows `PRODUCT_SPEC.md`. On conflict, this MVP contract wins.

## One-sentence product

A local CLI and read-only web UI that turns bound inference evidence into a deterministic, tamper-evident `PROMOTE`, `REJECT`, or `INCONCLUSIVE` decision.

## Explicit non-goals

- No generic GPU dashboard.
- No new load generator.
- No runtime/model manager.
- No Docker start/stop/config mutation.
- No autonomous optimizer or LLM judge.
- No support claim beyond exercised artifact adapters.
- No React/Node toolchain in v0.1; serve one self-contained offline HTML/JS/CSS UI from FastAPI.
- No database; bundles are immutable JSON files.
- No remote bind, auth system, telemetry, or SaaS.

## User flow

```bash
uv sync --extra dev
uv run serving-verdict import-case configs/cases/dspark-k7.yaml --out data/dspark-k7.verdict.json
uv run serving-verdict import-case configs/cases/sglang-eagle.yaml --out data/sglang-eagle.verdict.json
uv run serving-verdict verify data/dspark-k7.verdict.json
uv run serving-verdict serve --host 127.0.0.1 --port 8787 --data-dir data
```

Open `http://127.0.0.1:8787`.

## MVP supported sources

Only these adapters are implemented and may produce authoritative metrics:

1. `qwen38.dspark-ab.v1`
2. `qwen38.sglang-vllm-ab.v1`

Unknown schemas are indexed as `UNSUPPORTED` and cannot produce a decision.

Read-only source root for the first real cases:

`/home/ozkangu/Desktop/Qwen3.8-27B-DGX-Spark-RTX-6000`

Case configs bind exact source-relative paths and expected SHA-256 values. The importer canonicalizes the approved root once and rejects absolute child paths, `..`, and symlink escape.

## Metric semantic registry

Every metric ID has fixed semantics:

- `decode_tokens_per_s`: post-first-token decode rate, tok/s, higher is better.
- `e2e_output_tokens_per_s`: completion tokens divided by full request wall time, tok/s, higher is better.
- `aggregate_output_tokens_per_s`: sum completion tokens divided by common concurrent group wall interval, tok/s, higher is better.
- `ttft_s`: request start to first generated token, seconds, lower is better.
- `api_latency_s`: full API-call wall time, seconds, lower is better.

Two values are comparable only when metric ID, unit, procedure version, workload ID, concurrency, output budget, thinking mode, warm/cold status and aggregation semantics match. No automatic conversion between decode/e2e/aggregate.

## Case config v0.1

YAML is operator-authored policy and references evidence; it is not itself measurement authority.

```yaml
schema_version: "serving-verdict.case.v0.1"
id: qwen38-dspark-k7
source_root: /approved/root
baseline:
  artifact: benchmarks/results/dspark_ab_incumbent_mtp2.json
  sha256: "..."
candidate:
  artifact: benchmarks/results/dspark_ab_candidate_k7.json
  sha256: "..."
policy:
  primary_metric: decode_tokens_per_s
  workload: edit_cold
  min_relative_improvement: 0.15
  max_ttft_regression: 0.10
  required_gates:
    - request_success
    - arithmetic
    - tool_call
    - process_stability
    - rollback
supplemental_evidence:
  - id: arithmetic
    kind: operator_attested
    status: pass
    source: benchmarks/results/DSPARK_REPORT.md
    sha256: "..."
claim_boundary: "..."
```

`operator_attested` is visible as such; it is never mislabeled as machine-measured. MVP policy may accept operator-attested non-performance gates only when their source file hash matches. Performance values must come from recognized JSON adapters.

## Verdict contract v0.1

```json
{
  "schema_version": "serving-verdict.bundle.v0.1",
  "case_id": "qwen38-dspark-k7",
  "verdict": "PROMOTE",
  "reason_codes": ["PRIMARY_EFFECT_PASSED", "ALL_REQUIRED_GATES_PASSED"],
  "baseline": {"artifact_id": "...", "sha256": "..."},
  "candidate": {"artifact_id": "...", "sha256": "..."},
  "comparisons": [],
  "gates": [],
  "claim_boundary": "...",
  "created_at": "volatile metadata",
  "bundle_digest": "sha256 over canonical payload excluding created_at and bundle_digest"
}
```

Canonical JSON: UTF-8, `ensure_ascii=true`, sorted keys, separators `(',', ':')`, no NaN/Infinity. Lists preserve order.

## Deterministic decision rules

Evaluate in order:

1. **Load/integrity:** missing file, hash mismatch, invalid JSON/YAML, unsupported schema, non-finite value → `INCONCLUSIVE` bundle when case identity can be established; otherwise hard CLI error exit 2.
2. **Comparability:** primary metric cannot be extracted under identical semantic dimensions → `INCONCLUSIVE`.
3. **Hard gates:** any required correctness, request-success, process-stability or rollback gate with `fail` → `REJECT`, regardless of speed.
4. **Missing gates:** any required gate absent/unverifiable → `INCONCLUSIVE`.
5. **TTFT gate:** candidate relative TTFT regression above policy maximum → `REJECT`.
6. **Effect gate:** candidate primary metric relative improvement below threshold → `REJECT` with `INSUFFICIENT_EFFECT`.
7. Otherwise → `PROMOTE`.

The SGLang case must reject because process stability/production replay failed even though synthetic throughput improved. The DSpark case must promote only when every referenced source hash matches.

## CLI

```text
serving-verdict import-case CASE.yaml --out BUNDLE.json [--json]
serving-verdict verify BUNDLE.json [--json]
serving-verdict list DATA_DIR [--json]
serving-verdict show BUNDLE.json [--json]
serving-verdict serve --host 127.0.0.1 --port 8787 --data-dir DATA_DIR
```

Exit codes:

- 0: command succeeded; verify valid; import produced any valid verdict including REJECT/INCONCLUSIVE.
- 2: usage/config/load error where no valid bundle can be produced.
- 4: bundle integrity verification failure.

JSON mode emits exactly one JSON object on stdout; diagnostics go to stderr.

## HTTP/UI

API:

- `GET /api/v1/health`
- `GET /api/v1/verdicts`
- `GET /api/v1/verdicts/{case_id}`
- `GET /api/v1/metrics`
- `GET /` serves the self-contained app.

Rules:

- Default and MVP-only bind: `127.0.0.1`. Any other host is rejected; no override flag.
- Read-only APIs; no POST/PUT/PATCH/DELETE.
- The UI shows verdict first, then reason codes, comparable metrics, gate authority (`machine_measured` vs `operator_attested`), hashes and claim boundary.
- Green is not the only status signal; PROMOTE/REJECT/INCONCLUSIVE are text labels.
- No fake live GPU tiles. A small read-only runtime status is allowed only if sourced from an existing bundle; live system probing is post-MVP.
- Visual system: Linear near-black precision, NVIDIA green only for measured/pass accents, red for reject, amber for inconclusive, system fonts only, offline.

## Real fixture cases

Create case configs that bind current local evidence:

- `configs/cases/dspark-k7.yaml`
- `configs/cases/sglang-eagle.yaml`

Do not copy giant logs into the new repo. Reference and hash the source files. Unit tests use minimized fixture copies under `tests/fixtures/` with the same schemas.

Expected:

- DSpark: `PROMOTE`
- SGLang: `REJECT`

If actual source hashes differ from config, import returns `INCONCLUSIVE` with `EVIDENCE_HASH_MISMATCH`; tests must not update expected hashes automatically.

## Security/runtime invariants

- Never execute artifact content.
- Never invoke a shell or Docker.
- Never accept a user-provided source root over HTTP.
- CLI case config is the only source-root input; canonicalize and constrain paths.
- Bound file size: 20 MiB JSON/YAML/Markdown in MVP.
- Reject symlink escape and special files.
- FastAPI data directory is read-only by convention; app never writes while serving.
- Shutdown must close the server and release the port; E2E test verifies.
- Error responses never include file contents or secrets.

## Tests required before commit

- canonical JSON/digest determinism and volatile timestamp exclusion;
- substantive mutation invalidates digest;
- path traversal, absolute child path and symlink escape;
- file-size/special-file rejection;
- hash mismatch → INCONCLUSIVE import;
- unsupported schema → INCONCLUSIVE;
- NaN/Infinity rejection;
- metric mismatch → INCONCLUSIVE;
- hard gate fail overrides performance win → REJECT;
- missing gate → INCONCLUSIVE;
- TTFT regression → REJECT;
- insufficient effect → REJECT;
- all gates + effect → PROMOTE;
- DSpark minimized fixture → PROMOTE;
- SGLang minimized fixture → REJECT;
- verify detects tampering exit 4;
- JSON stdout exactly one object;
- API list/detail/404 contracts;
- non-loopback serve rejected;
- server start/health/shutdown/port cleanup E2E;
- frontend renders all three verdict states, gate authority and hashes.

## Quality gates

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
uv build
```

Manual dogfood:

1. Import both real cases.
2. Verify both bundles offline.
3. Start UI.
4. Inspect desktop and 390px mobile screenshots.
5. Confirm source trees remain clean/unmodified.
6. Confirm production Qwen endpoint/container remains untouched.

## Kill criteria

Kill or reduce to two written case studies if any occurs:

1. A deterministic `PROMOTE/REJECT/INCONCLUSIVE` engine cannot be expressed without an LLM opinion.
2. The two real cases cannot be imported without fabricating authoritative evidence.
3. Offline `verify` cannot detect substantive tampering.
4. The SGLang hard-failure case can accidentally promote due to throughput.
5. After public launch, no external user verifies/replays a bundle within 14 days.
