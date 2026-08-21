# Project status and continuation target

**Status:** Paused by product decision

**As of:** 2026-08-21

**Main commit at pause:** `e79b0145a6c77abea797df13a5c38d63cdad687c`

## Decision

Active v0.5 product development is paused. The repository remains usable and its
existing evidence/verdict workflows remain supported, but no additional UI,
dashboard or broad platform features should be added until the core single-GPU
workflow below is implemented and proven on real hardware.

This is a product-scope decision, not an abandonment notice.

## Honest current product

LLM ServeVerdict is currently an **evidence-gated benchmark, comparison and
promotion-decision toolkit for LLM serving changes**.

It can:

- run and seal a frozen quick benchmark against a loopback OpenAI-compatible
  endpoint;
- import and verify serving evidence;
- compare baseline and candidate artifacts under strict comparability rules;
- apply correctness, stability and statistical promotion gates;
- produce `PROMOTE`, `REJECT` or `INCONCLUSIVE` decisions;
- verify artifacts and expose CI-friendly exit semantics;
- run repeated endpoint experiments when both endpoints already exist;
- normalize bounded runtime telemetry and compute deterministic distribution
  summaries;
- render verdict, Lab, Live and Decide states in an offline loopback UI.

The repository also contains tested v0.5 foundations for a narrow Docker Lab
backend, owned lifecycle cleanup, Lab-run orchestration and the bounded Lab/Live
API.

## What it is not yet

LLM ServeVerdict is **not yet a complete inference-engineering platform**.

In particular:

- the default server does not install a real Docker Lab executor, so Lab Start is
  intentionally disabled;
- it does not yet perform a complete model/runtime swap on a single GPU;
- it does not adopt, stop and restore an existing incumbent service;
- it does not guarantee rollback to the incumbent after candidate failure,
  cancellation or process interruption;
- it has not produced a complete real-GPU Lab-run bundle through the UI/API;
- the committed Lab/Live screenshots use a trusted deterministic injected
  executor and are explicitly **not GPU benchmark evidence**;
- Live snapshots are bounded state evidence, not a persistent Prometheus/Grafana
  replacement;
- v0.5 should not be released or marketed as a full inference platform in this
  state.

## Why development is paused

The initial automated endpoint A/B direction assumed that baseline and candidate
could be available concurrently. That is practical with multiple GPUs or remote
endpoints, but it is usually wrong for a single DGX Spark running large models in
unified memory.

Running two large runtimes together can:

- cause unified-memory or KV-cache pressure;
- contaminate latency and throughput measurements;
- create OOM/restart risk for the incumbent;
- make the comparison operationally unrealistic.

The tool should own the sequential change workflow rather than asking the
operator to provide two simultaneous local endpoints. Until it does, additional
surface area would be overbuilding around an incomplete core.

## Intended product value if resumed

The narrow product wedge is:

> **A single-GPU change-control and promotion gate for LLM serving changes.**

The product should turn:

> “This new vLLM/SGLang configuration seems faster.”

into:

> “This exact candidate was tested on the same host and workload, correctness
> passed, uncertainty was measured, the incumbent was restored and verified,
> and the evidence-bound decision is PROMOTE/REJECT/INCONCLUSIVE.”

Docker starts containers, benchmark tools measure performance and Prometheus
observes a running server. LLM ServeVerdict is valuable only if it binds those
pieces into one safe, reproducible change-and-rollback decision.

## Primary continuation target: single-GPU sequential experiment

The next implementation must use a bracketed sequential design rather than
concurrent local A/B endpoints:

```text
capture incumbent identity and rollback plan
→ start/verify incumbent if tool-owned
→ fixed warmup
→ repeated baseline block A-pre
→ stop incumbent under explicit ownership/adoption authority
→ start digest-pinned candidate
→ readiness + fixed warmup
→ repeated candidate block B
→ stop/remove candidate
→ restore exact incumbent in an unconditional finalizer
→ verify incumbent readiness
→ repeated baseline block A-post
→ compare B against the bracketing A-pre/A-post evidence
→ seal artifacts, statistics, rollback proof and decision
```

The baseline post-check is required to expose temporal drift, thermal effects,
memory pressure and failed rollback. A simple old-baseline-versus-new-candidate
comparison is not sufficient evidence on a sequential single-GPU host.

## Ownership and rollback requirements

The tool must never silently stop an arbitrary running container.

A runtime may be mutated only when one of these is true:

1. it was created and labelled by LLM ServeVerdict; or
2. the operator explicitly adopts an existing runtime using an immutable adoption
   manifest that records its container/image/model/argv/ports and exact restore
   procedure.

Mandatory invariants:

- explicit per-run mutation confirmation;
- no browser-controlled images, argv, mounts, devices or environment values;
- rollback plan validated before the incumbent is stopped;
- candidate cleanup and incumbent restore attempted from `finally`, including
  cancellation and failures;
- no successful result until incumbent readiness is re-verified;
- cleanup or restore uncertainty is terminal `CLEANUP_FAILED`/`RESTORE_FAILED`,
  never a warning attached to success;
- production endpoint identity and reachability after restore must match the
  captured incumbent contract;
- secret values never enter plans, labels, logs, artifacts or DOM.

## Monitoring after deployment

Monitoring is secondary to the controlled experiment. It should remain
read-only and compare the active runtime against a frozen, non-stale historical
envelope:

- TTFT and end-to-end latency p50/p95/p99;
- decode and aggregate throughput;
- request success/error rate;
- running/waiting requests and queue time;
- KV-cache utilization;
- GPU/unified-memory pressure;
- rolling distributions and bounded EWMA/CUSUM-style drift signals.

A monitoring anomaly should recommend a new controlled experiment. It must not
create a promotion decision by itself.

## Killer demo required before resuming broad development

The project resumes only around this end-to-end result:

```bash
llm-serve-verdict experiment run single-gpu.yaml
```

Expected operator-visible outcome:

```text
✓ incumbent identity and rollback plan captured
✓ A-pre benchmark complete
✓ candidate ready and benchmarked
✓ candidate removed
✓ incumbent restored and ready
✓ A-post benchmark complete
✓ artifacts and uncertainty verified

Decision: PROMOTE | REJECT | INCONCLUSIVE
Rollback: VERIFIED
Evidence: experiment/<run-id>/
```

## Real-hardware acceptance gate

Before a v0.5 release, one exact tree must prove on a DGX Spark/GB10:

1. digest-pinned vLLM and/or SGLang runtime provenance;
2. exact local model-manifest provenance;
3. fixed warmup and repeated A-pre/B/A-post blocks;
4. multiple seeds where stochastic behavior exists;
5. p-value where applicable, effect size and confidence interval;
6. correctness, tool-call, request-success and stability hard gates;
7. candidate startup/readiness and bounded telemetry;
8. injected candidate failure, cancellation and deadline overrun;
9. unconditional incumbent restoration in every failure probe;
10. post-restore endpoint/model/config/readiness verification;
11. zero owned container/network residue;
12. sealed raw runs, normalized statistics, decision and rollback proof;
13. fresh-clone CLI/API/browser E2E and independent exact-tree review.

Claims from a single successful synthetic run are insufficient.

## Scope cut until the acceptance gate passes

Do not add:

- more dashboard pages or cosmetic redesigns;
- generic Docker/Compose execution;
- Kubernetes/KServe/Dynamo control planes;
- model downloading or quantization pipelines;
- persistent TSDB/Grafana replacement features;
- multi-user remote control;
- autonomous LLM tuning or promotion;
- additional runtime adapters without a real sequential experiment need.

The current UI may be maintained for regressions, but it must not drive product
scope ahead of the real execution path.

## Resume order

If development resumes:

1. write the normative sequential-experiment and adoption-manifest schemas;
2. implement fail-closed restore semantics with fake-backend TDD;
3. wire the real Docker executor without browser-controlled capabilities;
4. add A-pre/B/A-post statistics and artifact-directory verification;
5. run destructive failure probes on disposable runtimes;
6. dogfood on the real DGX Spark only when resource headroom is safe;
7. replace injected screenshots with explicitly labelled real-GPU evidence;
8. independently review and decide whether v0.5 should ship.

## Stop criterion

If the project cannot demonstrate the one-command sequential experiment with
verified incumbent restoration, it should remain a narrow v0.4-era serving
verdict toolkit. In that case, the experimental Lab surface should not be
marketed as a full inference-engineering library, and no further broad product
investment is justified.
