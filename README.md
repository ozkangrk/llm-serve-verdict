# Serving Verdict — MVP v0.1

A local CLI and read-only web UI that turns **bound inference evidence** into a
deterministic, tamper-evident `PROMOTE`, `REJECT`, or `INCONCLUSIVE` decision.

This is the local-first core of the Inference Engineering Studio product
(`MVP_SPEC.md` is the normative contract). It is **not** a generic GPU
dashboard, not a load generator, not a runtime manager, and it makes no claims
beyond the two exercised artifact adapters.

## What it does

- Imports **case configs** (operator-authored YAML policy) that bind exact
  source-relative artifact paths + SHA-256 hashes under one approved source root.
- Loads evidence through a **path-safe evidence loader** (canonicalizes the root,
  rejects absolute child paths, `..` traversal, symlink escape, special files,
  and >20 MiB files). It never executes artifact content.
- Extracts metrics through **two recognized artifact adapters**:
  - `qwen38.dspark-ab.v1`
  - `qwen38.sglang-vllm-ab.v1`
  Unknown schemas are indexed as `UNSUPPORTED` and cannot produce a decision.
- Keeps metric semantics fixed via the **metric semantic registry**
  (`decode_tokens_per_s`, `e2e_output_tokens_per_s`,
  `aggregate_output_tokens_per_s`, `ttft_s`, `api_latency_s`). Two values are
  comparable only when their semantic dimensions match.
- Runs a **deterministic decision engine** (no LLM opinion) that emits a
  **tamper-evident bundle**: a canonical-JSON digest over the payload
  (excluding the volatile `created_at` and the digest itself).
- Serves the bundles read-only over a **loopback-only FastAPI** app with a
  self-contained offline HTML/JS/CSS UI.

## Quick start

```bash
uv sync --extra dev
uv run serving-verdict import-case configs/cases/dspark-k7.yaml --out data/dspark-k7.verdict.json
uv run serving-verdict import-case configs/cases/sglang-eagle.yaml --out data/sglang-eagle.verdict.json
uv run serving-verdict verify data/dspark-k7.verdict.json
uv run serving-verdict serve --host 127.0.0.1 --port 8787 --data-dir data
# open http://127.0.0.1:8787
```

The first real cases bind the read-only source tree
`/home/ozkangu/Desktop/Qwen3.8-27B-DGX-Spark-RTX-6000` and expect:

- **DSpark k7**: `PROMOTE` (synthetic + concurrency + tool-call + stability gates).
- **SGLang EAGLE**: `REJECT` (synthetic throughput win, but production
  process-stability gate failed).

## CLI

```text
serving-verdict import-case CASE.yaml --out BUNDLE.json [--json]
serving-verdict verify BUNDLE.json [--json]
serving-verdict list DATA_DIR [--json]
serving-verdict show BUNDLE.json [--json]
serving-verdict serve --host 127.0.0.1 --port 8787 --data-dir DATA_DIR
```

Exit codes:

| code | meaning |
|---|---|
| `0` | command succeeded; `verify` passed; `import` produced any valid verdict (incl. `REJECT`/`INCONCLUSIVE`) |
| `2` | usage/config/load error where no valid bundle can be produced |
| `4` | bundle integrity verification failure |

`--json` emits exactly one JSON object on stdout; diagnostics go to stderr.

## HTTP / UI

- `GET /api/v1/health`, `GET /api/v1/verdicts`,
  `GET /api/v1/verdicts/{case_id}`, `GET /api/v1/metrics`, `GET /` (the UI).
- Loopback-only (`127.0.0.1`); any other host is rejected at startup.
- Read-only: no POST/PUT/PATCH/DELETE. No live system probing (post-MVP).

## Quality gates

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
uv build
```

## Repository layout

```
src/serving_verdict/     package (CLI, engine, API, UI assets)
configs/cases/           operator-authored real case configs (bind real source hashes)
data/                    generated verdict bundles (immutable JSON)
tests/                   unit + integration + E2E tests, with minimized fixtures
docs/                    tdd-journal.md and supporting notes
```

## Security invariants

Never executes artifact content, never invokes a shell or Docker, never accepts
a user-provided source root over HTTP (the CLI case config is the only
source-root input), canonicalizes and constrains paths, bounds file size to
20 MiB, rejects symlink escape and special files, serves read-only, and
releases the port on shutdown.

## License

MIT. See [LICENSE](LICENSE).
