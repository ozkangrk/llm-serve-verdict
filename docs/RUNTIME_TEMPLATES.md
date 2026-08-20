# Inference Lab runtime templates and pure planner

Status: v0.5 foundation. This layer is inert: it cannot access Docker, start a
process, bind a port or call an endpoint.

## Built-in blueprints

- `vllm.openai` — repository `docker.io/vllm/vllm-openai`
- `sglang.openai` — repository `docker.io/lmsysorg/sglang`
- `llamacpp.server` — repository `ghcr.io/ggml-org/llama.cpp`

No mutable image tag or guessed digest is shipped. An operator must bind a
blueprint to an exact `<repository>@sha256:<64-hex>` image before a template can
exist. A repository mismatch, unknown blueprint or tag-only image fails closed.

The repository names identify the expected upstream namespace but do not assert
that any particular image digest is trusted. Operators must obtain and review
an official digest through their own supply-chain policy.

## Typed parameter contract

Parameters are fixed declarations with canonical names, one flag, exact type,
optional min/max or enum, a default and explicit aliases. Unknown fields,
canonical-plus-alias conflicts, booleans supplied as integers, wrong types and
out-of-range values are rejected before any execution layer is reachable.

The effective argv is an immutable tuple built in canonical parameter-name
order. It is evidence, not shell text. No shell executes it.

Direct `RuntimeTemplate` construction is capability-gated. Dataclass replacement
cannot forge an image, digest or prohibited fixed argument. The template rejects
representations of privileged/host namespace/capability/mount/Docker-socket
access and verifies its canonical self-digest at construction.

## Model manifest

`build_model_manifest()` accepts an operator model root and a relative model
reference. It rejects:

- absolute paths and traversal;
- symlink model roots, model directories or nested entries;
- sockets, devices, FIFOs and other special files;
- file-count and total-byte bounds;
- files that change to a symlink/special file while opening.

Each regular file is opened with `O_NOFOLLOW`, streamed through SHA-256 and
recorded only as model-relative path, size and digest. Host paths are never
stored. The canonical manifest digest changes when any bound file changes.

## Lab run plan

`plan_lab_run()` is pure and deterministic. It binds:

- template ID/version/self-digest and image digest;
- exact effective argv using the fixed in-container model mount path;
- model manifest digest;
- benchmark profile digest;
- repeated trial count and statistical seed;
- telemetry interval and bounded sample count.

The plan digest covers the complete substantive plan. `run_id` is derived from
that canonical body; timestamps, random values and secrets do not participate.
The planner imports no Docker/process/network implementation, so invalid inputs
have zero runtime side effects by construction.

## Current limitations

- This slice does not pull or verify a registry signature; it only enforces the
  exact image digest/repository contract.
- It does not start Docker or inspect GPU capabilities.
- llama.cpp model-file selection inside the read-only model directory will be
  finalized by the lifecycle adapter; the current plan binds the entire model
  directory and uses the fixed `/models/current` container path.
- Built-in parameter coverage is intentionally narrow. New flags require a
  schema/test change; arbitrary passthrough flags are not supported.
