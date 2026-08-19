# TDD Journal — Serving Verdict MVP v0.1

> Historical MVP journal; retained as provenance, not as the current feature
> list. See `CHANGELOG.md` and `PRODUCT_V1_SPEC.md` for v0.3.

Strict TDD: tests written first (RED, with the expected failure reason), then
minimal implementation (GREEN). All commands were actually run.

## Cycle 1 — canonical digest, path-safe loader, metric registry

**Tests written (RED):**
- `tests/test_canonical.py` — canonical JSON determinism (sorted keys, compact
  separators, list order preserved, ensure_ascii), NaN/Infinity/-Infinity
  rejection, volatile-field exclusion (`created_at`, `bundle_digest`), digest
  stability across timestamps/key orders, substantive + list-order mutation
  changes digest, recompute-from-bytes round trip.
- `tests/test_evidence_loader.py` — load + sha256, `..` traversal rejection,
  absolute child path rejection, missing file, symlink escape (file and
  directory), symlink *staying inside* root accepted, FIFO special-file
  rejection, 20 MiB bound (over rejected, exact accepted), symlinked root OK,
  empty path rejected, expected-sha verification.
- `tests/test_metrics.py` — registry contains the five spec metrics with fixed
  directions, registry immutability, comparability iff all dimensions match
  (metric ID, unit, procedure version, workload, concurrency, output budget,
  thinking mode, warm/cold, aggregation).

**First RED run:** `ModuleNotFoundError: No module named 'serving_verdict.*'`
for all three modules (expected — no implementation yet).

**Pitfalls found:**
1. `tmp_path` in this uv environment is itself a symlink to
   `/tmp/pytest-*/...`; using `tmp_path / "outside"` for the *outside* fixture
   landed inside the canonical root, so symlink-escape tests did not raise.
   Fixed by placing outside files at `tmp_path.parents[0]` (the real OS parent).
2. A `MappingProxyType` registry failed `in`-style iteration expectations;
   replaced with a small immutable `Mapping` subclass so `values()`,
   `__contains__` and mutation attempts all behave.
3. `json.dumps(allow_nan=False)` raises `ValueError` — wrapped as
   `CanonicalizationError` at the bytes boundary.

**GREEN:** `uv run pytest -q` → 35 passed.

**Modules created:** `errors.py`, `canonical.py`, `metrics.py`, `evidence.py`.

## Cycle 2 — case config parsing + artifact adapters

**Tests written (RED):**
- `tests/test_adapters.py` (case-config half) — valid case loads with all
  policy fields; wrong schema_version, invalid YAML, missing `policy`,
  unknown primary metric, negative threshold all rejected (CaseConfigError);
  supplemental `operator_attested` evidence parsed; bad supplemental status
  rejected.
- `tests/test_adapters.py` (adapter half) — known schemas are exactly the two
  spec schemas; DSpark adapter extracts per-workload
  `decode_tokens_per_s` / `e2e_output_tokens_per_s` / `ttft_s` with the fixed
  procedure dimensions; non-finite metric raises; unknown schema raises
  `UnknownSchemaError`; missing `results` raises; SGLang adapter extracts
  serial medians + group aggregate (`group_wall` aggregation, concurrency=4);
  adapter output deterministic for identical documents.

**First RED run:** `ModuleNotFoundError` for `caseconfig` and `adapters`.

**Pitfalls found:**
1. Test case configs used non-64-hex placeholder shas → replaced with
   64-char hex placeholders (real shas computed at import time in the engine
   tests instead).
2. A `Policy.required_gates` tuple vs list comparison required `list(...)` in
   the assertion (tuple is the stored form).

**GREEN:** `uv run pytest -q` → 50 passed.

**Modules created:** `caseconfig.py`, `adapters.py`.

## Cycle 3 — deterministic decision engine + bundle + verify

**Tests written (RED):** `tests/test_engine.py`
- Happy-path PROMOTE: reason codes, comparisons (baseline 25.62 → candidate
  63.27), TTFT reported with negative relative_delta, gate authorities
  (`machine_measured` vs `operator_attested`), digest recompute, created_at
  excluded from digest.
- Rule ordering: hard-gate fail → REJECT (even with massive perf win + TTFT
  regression, single reason `HARD_GATE_FAILED`); missing required gate →
  INCONCLUSIVE; attested gate with wrong source hash → unverifiable →
  INCONCLUSIVE; TTFT regression → REJECT; insufficient effect → REJECT.
- Inconclusive causes: evidence hash mismatch, missing artifact, unsupported
  schema, NaN in artifact, metric/workload not comparable.
- Minimized committed fixtures: `tests/fixtures/dspark/case.yaml` → PROMOTE,
  `tests/fixtures/sglang/case.yaml` → REJECT (`HARD_GATE_FAILED`), both
  offline-verify.
- Verify detects tampered verdict / hash / comparison value / schema version;
  missing file → UsageError; bundle file round-trips.

**First RED run:** `ModuleNotFoundError` for `engine`; the three minimized
fixture tests additionally failed with `case config not found` until the
fixtures were generated (`tests/fixtures/generate_fixtures.py`, parent-index
bug: `parents[1]` is `tests/`, `parents[2]` is the repo root).

**Fixtures created:** `tests/fixtures/dspark/*` (minimized dspark-ab.v1 pair +
attested report + case.yaml) and `tests/fixtures/sglang/*` (minimized
sglang-vllm-ab.v1 pair + production-replay report with failed
`process_stability` + case.yaml).

**GREEN:** `uv run pytest -q tests/test_engine.py` → 24 passed (57 total).

**Modules created:** `engine.py`.

## Cycle 4 — CLI contracts

**RED:** subprocess tests exposed empty stdout for `list` and an incorrect
implicit parent-directory creation in `import-case`.

**GREEN:** `list` emits one JSON object on stdout in both modes; diagnostics
remain on stderr. `import-case` now returns exit 2 when the requested output
parent does not already exist. Focused and full CLI tests pass.

## Cycle 5 — read-only FastAPI server and offline UI

**RED:** `ModuleNotFoundError: serving_verdict.server`; subsequent contract
failures covered flat error bodies, loopback-only binding, UI gate-authority
markup and lifecycle cleanup.

**GREEN:** added `server.py` and a self-contained `web/` app. Fourteen server
and UI tests cover health/list/detail/metrics/index, read-only methods,
loopback rejection, all three verdict labels, gate authority, hashes, real
HTTP startup, SIGTERM and port rebind.

## Cycle 6 — closure correctness, static gates and real evidence

Two multi-workload adapter regression tests pin per-block medians and prevent
loop-variable late binding. Nested closures were replaced with the explicit
module-level `_block_median(...)` helper. Ruff findings were resolved without
changing metric semantics; mypy and hatch wheel duplication issues were fixed.

Real source-bound case configs were added. Actual imports produced:

- `data/dspark-k7.verdict.json` → `PROMOTE`
- `data/sglang-eagle.verdict.json` → `REJECT`

Both bundles pass offline integrity verification.

**Final gates run by the parent agent:**

- `uv run pytest -q` → 105 passed (one Starlette deprecation warning)
- `uv run ruff check src tests` → all checks passed
- `uv run mypy src` → success, 11 source files
- `uv build` → sdist and wheel built

**Manual live smoke:** started on `127.0.0.1:8787`; health, verdict list,
DSpark detail, SGLang detail, metric registry and index all returned HTTP 200.
The server was terminated and the parent agent rebound port 8787 successfully
(`PORT_RELEASED`). The benchmark source repo remained clean at commit
`638c99aa76c32f197994d8df666dc1c6354b99be`; production Qwen remained healthy.

## Cycle 7 — adversarial release findings F1–F5

Independent review of staged tree `a48a4ae4bfe419c9fe26fb1477597085f221efeb`
returned NO-GO: one HIGH and four MEDIUM findings. Eight regression tests were
added first and observed failing.

Fixes:

- primary effect gain now respects `higher_better` versus `lower_better`;
- duplicate/conflicting supplemental gate IDs are rejected at config load;
- supplemental SHA values use the same 64-hex contract as primary evidence;
- source roots must be absolute existing directories;
- CLI/API list views hide bundles that fail offline integrity verification;
- UI detail rendering is exercised against the real JS with QuickJS and a
  minimal DOM, including PROMOTE/REJECT/INCONCLUSIVE, authority/hash cells and
  malformed/null baseline handling;
- the adapter determinism test uses `tmp_path`, never the repository root;
- defensive TTFT control flow no longer relies on an optimized-away `assert`.

The post-fix suite contains 121 tests. Final release gates are rerun after this
entry and must all pass before commit.
