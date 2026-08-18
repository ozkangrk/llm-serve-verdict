# Serving Verdict

**Stop averaging your way into production.** Serving Verdict turns bound
benchmark evidence — exact files, pinned SHA-256, operator-authored policy —
into one deterministic, tamper-evident decision: `PROMOTE`, `REJECT`, or
`INCONCLUSIVE`. No dashboard to scroll, no LLM opinion, no "it's probably
fine."

A throughput win that fails the process-stability gate is a `REJECT`. A
candidate that passes every required gate *and* clears the effect threshold
is a `PROMOTE`. Everything else is `INCONCLUSIVE`, and that answer is a
first-class, digested, verifiable artifact — not an error you have to
re-litigate.

## 30-second demo

```bash
uv sync --extra dev
uv run serving-verdict demo --out-dir demo      # runs both bundled fixture cases
uv run serving-verdict list demo                 # both verdicts, one JSON object
uv run serving-verdict verify demo/<bundle>.verdict.json
uv run serving-verdict serve --host 127.0.0.1 --port 8787 --data-dir demo
# open http://127.0.0.1:8787
```

`demo` is the portable path: it runs the two fixture cases that ship with the
repository (self-contained, no access to any external source tree required),
writes one verdict bundle per case under `--out-dir`, and exits `0` when all
bundles are valid. The two fixture cases are chosen to exercise both sides of
the decision surface — and both sides are first-class:

| fixture | verdict | why |
|---|---|---|
| `fixture-dspark` | `PROMOTE` | synthetic effect gain clears the threshold and every required gate passes |
| `fixture-sglang` | `REJECT` | synthetic throughput win is overridden by a failed `process_stability` gate |

A `REJECT` is a success of the tool, not a failure of it. Both bundles verify
offline with the same command, and neither is more "interesting" than the
other: they are the same kind of object with different verdicts.

![Verdict primitive](docs/verdict-primitive.svg)

## What it is

- **Case policy + hash** — operator-authored YAML binds exact source-relative
  artifact paths and SHA-256 hashes under one approved source root.
- **Path-safe evidence loader** — canonicalizes the root, rejects absolute
  child paths, `..` traversal, symlink escape, special files, and >20 MiB
  files. It never executes artifact content.
- **Schema adapters** — two recognized artifact schemas
  (`qwen38.dspark-ab.v1`, `qwen38.sglang-vllm-ab.v1`); unknown schemas are
  indexed as `UNSUPPORTED` and cannot produce a decision.
- **Metric semantic registry** — `decode_tokens_per_s`,
  `e2e_output_tokens_per_s`, `aggregate_output_tokens_per_s`, `ttft_s`,
  `api_latency_s` have fixed semantics; two values are comparable only when
  every semantic dimension matches.
- **Deterministic gate engine** — pure function of bound evidence + policy,
  evaluated in a fixed rule order (hard gates before effect, TTFT regression
  before promote). No LLM participates.
- **Tamper-evident digest bundle** — canonical-JSON digest over the payload
  (excluding the volatile `created_at` and the digest field itself);
  re-verifyable offline.
- **Loopback UI** — read-only FastAPI app on `127.0.0.1` with a self-contained
  offline HTML/JS/CSS front end.

![Architecture](docs/architecture-v0.2.svg)

The real flow, left to right:
**case policy + hash → path-safe loader → schema adapter → metric semantics →
gate engine → digest bundle → loopback UI.** There is no other path to a
verdict: anything not loaded through the path-safe loader and recognized by a
schema adapter cannot reach the gate engine, and anything the gate engine does
not emit cannot be served.

## What it is not

- Not a GPU dashboard, and not a monitoring system.
- Not a load generator: it consumes benchmark artifacts; it never generates
  load.
- Not a runtime manager: it never starts, stops, or configures a server,
  container, or accelerator.
- Not a generic benchmark comparator: only the two recognized artifact
  schemas above are authoritative; every other schema is `UNSUPPORTED` by
  design.
- Not an opinion: there is no model in the verdict path, no free-text
  justification field, and no override that flips a `REJECT` to `PROMOTE`.
- Not a security scanner, and not a compliance tool.

## Honest test matrix and claim boundaries

What is actually exercised, as of this tree:

| claim | status |
|---|---|
| Decision engine rule order, comparability, direction-aware effect gate | covered by unit tests (`tests/test_engine.py`) |
| Path-safe loader (traversal, symlink escape, size bound, special files) | covered by unit tests (`tests/test_evidence_loader.py`) |
| Canonical digest (determinism, NaN rejection, mutation sensitivity) | covered by unit tests (`tests/test_canonical.py`) |
| Both artifact adapters against minimized fixtures | covered by unit tests (`tests/test_adapters.py`) |
| CLI exit codes, `--json` contract, loopback server E2E (incl. port release on SIGTERM) | covered by tests (`tests/test_cli.py`, `tests/test_server.py`, `tests/test_ui_dom.py`) |
| CI matrix: Linux + macOS, Python 3.11 + 3.12 (pytest, ruff, mypy, build) | defined in `.github/workflows/ci.yaml`; not yet run on the `demo`/history commands |
| `demo` command and fixture-portable quickstart | provided by the v0.2 backend workstream; not exercised on this branch |
| Windows | **not** tested and **not** supported in v0.2 |
| The two real-world case configs under `configs/cases/` | bind absolute source-tree paths on one machine (see Advanced); not reproducible from a fresh checkout |

Claim boundary: **a Serving Verdict verdict is a claim about the bound
artifacts, on the machine and source tree that produced them, under the
operator's policy.** It is not a claim about the model's quality in general,
nor about any workload not covered by the bound evidence. Bundles that fail
integrity verification are excluded from the index and UI by design
(fail-closed).

## CLI

```text
serving-verdict demo --out-dir DIR [--json]
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
- Read-only: no POST/PUT/PATCH/DELETE. No live system probing (post-v0.2).

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
configs/cases/           operator-authored case configs (bind real source hashes)
tests/                   unit + integration + E2E tests, with minimized fixtures
docs/                    tdd-journal.md, diagrams, and supporting notes
```

## Security invariants

Never executes artifact content, never invokes a shell or Docker, never
accepts a user-provided source root over HTTP (the CLI case config is the
only source-root input), canonicalizes and constrains paths, bounds file size
to 20 MiB, rejects symlink escape and special files, serves read-only, and
releases the port on shutdown. Report vulnerabilities via
[SECURITY.md](SECURITY.md) — not via a public issue.

## Advanced: running the real cases

The shipped `configs/cases/*.yaml` files bind an **absolute source root on a
specific machine** (`/home/ozkangu/Desktop/Qwen3.8-27B-DGX-Spark-RTX-6000`)
because case configs are deliberately opinionated about *which bytes* are
evidence. On another machine those exact imports will produce
`INCONCLUSIVE` bundles (`EVIDENCE_UNAVAILABLE`) — that is the loader working,
not a bug. To run real cases of your own:

1. Author a `case.yaml` with `source_root` pointing at *your* source tree and
   `sha256` pins computed against the exact artifact files.
2. `uv run serving-verdict import-case your-case.yaml --out data/your-case.verdict.json`
3. Verify, then serve or archive the bundle.

The `demo` command is the supported, portable substitute for steps 1–2 when
you do not yet have your own bound source tree.

## License

MIT. See [LICENSE](LICENSE).
