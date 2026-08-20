# Inference Lab run orchestration

This layer joins an owned runtime lifecycle to repeated benchmark trials and
bounded live telemetry. It does not make a promotion decision by itself.

```text
validated LabRunSpec + LifecyclePlan
→ Docker readiness
→ start bounded telemetry collector
→ repeated sealed quick-profile trials
→ final telemetry scrape + deterministic summaries
→ mandatory stop/remove/absence proof
→ seal serving-verdict.lab-run.v0.5
```

## Publication rule

The work callback can finish successfully while cleanup still fails. Therefore
the orchestrator keeps the lab artifact as an internal draft until
`LabLifecycle` returns both:

```text
state == SUCCEEDED
cleanup_verified == true
```

`FAILED`, `CANCELLED` and `CLEANUP_FAILED` return no promotion-eligible lab
artifact.

## Benchmark binding

Each trial must be a valid sealed `serving-verdict.benchmark-run.v1` artifact.
The orchestrator freezes and rechecks across all trials:

- endpoint identity and loopback base URL;
- requested and served model identity;
- profile name and procedure version;
- protocol hash;
- workload hash;
- hard-gate `run_status == ok`.

The profile/procedure/protocol/workload tuple is reduced to
`serving-verdict.benchmark-profile-binding.v1`. It must equal the digest already
bound into `LabRunSpec`. A self-consistent trial set for a different workload is
not accepted.

## Live telemetry

A daemon collector starts before the first benchmark trial and scrapes immediately.
All trials and scrapes share one absolute monotonic run budget. The collector then
waits the plan's bounded 1–60 second interval; it is stopped and joined before
finalization. A final scrape runs only when budget remains. A trial callback that
returns after the absolute deadline fails the run and publishes no artifact.

Only allowlisted Prometheus series enter the ring buffer. Each scrape is bounded
to 64 KiB and 64 series; total samples follow the plan's maximum. Unknown raw
metrics and disallowed labels are discarded. Raw scrape bodies and arbitrary
exception text are never stored.

Fixed scrape failure categories:

- `invalid`
- `unavailable`

Each normalized series receives deterministic display statistics:

- count;
- min;
- mean;
- p50;
- p95;
- p99;
- max;
- latest.

These statistics are recomputed by the artifact verifier. They are monitoring
evidence only and do not directly create a promotion verdict.

## Artifact integrity

`verify_lab_artifact` checks more than the outer digest:

- exact top-level and nested run-spec shape;
- recomputed plan digest and deterministic run ID;
- runtime engine/image/template cross-binding;
- exact trial count, order, status and digest syntax;
- typed telemetry samples/failures and monotonic offsets;
- recomputed telemetry summaries;
- `SUCCEEDED` lifecycle and verified cleanup proof.

## Current boundary

Implemented and unit-tested in this slice:

- runtime readiness-to-work lifecycle integration;
- repeated benchmark orchestration;
- concurrent bounded telemetry collection;
- telemetry distribution summaries;
- cleanup-gated final lab artifact;
- cross-field manifest reseal drift rejection (artifact authenticity still
  requires the pending signature/bundle gate).

Still pending before v0.5 release:

- atomic directory bundle containing each referenced benchmark artifact;
- loopback Lab run manager and `/api/lab/*` routes;
- streaming/snapshot Live API;
- baseline/candidate lab-run comparison and signed verdict;
- real vLLM/SGLang GPU lifecycle dogfood;
- Lab/Live/Decide browser UI and completed-run screenshots.
