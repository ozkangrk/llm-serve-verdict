# Opt-in Docker Inference Lab backend

This is the first real execution-plane capability for LLM ServeVerdict's v0.5
Inference Lab. It is not enabled by the browser and it is not a generic Docker
proxy.

## Current status

Implemented and tested:

- trusted `LabRunSpec` factory authority with digest/run-ID recomputation;
- process-unique Docker resource suffixes while retaining deterministic run IDs;
- server-process enablement via `SERVING_VERDICT_ENABLE_LAB=1`;
- exact digest-pinned image binding and local-digest reuse;
- NVIDIA runtime capability inspection;
- dedicated labelled bridge creation with loopback-only host ingress;
- hardened container-create argv;
- loopback-only allocator-selected published port;
- HTTP readiness and metrics URLs;
- exact ownership-label revalidation before stop/remove;
- bounded Docker command output and deadlines;
- absence verification by exact ownership/resource labels.

Verified on the current DGX Spark host without creating a container:

- Docker Engine `29.2.1`;
- Docker Compose `v5.0.2`;
- NVIDIA GB10, driver `580.173.02`;
- NVIDIA container runtime present;
- local digest-pinned vLLM image resolved without a registry pull;
- zero `sv-lab-*` containers or networks remained after the smoke test.

Still release-gated:

- starting a real disposable vLLM/SGLang container with a real model;
- benchmark + telemetry orchestration through this backend;
- cancellation during model startup;
- real cleanup failure injection;
- GB10 resource-limit calibration;
- Lab/Live/Decide API and UI.

## Security boundary

Real Docker access is accepted only when the server process environment contains:

```bash
export SERVING_VERDICT_ENABLE_LAB=1
```

A browser request cannot set or override this variable. Calling a start API will
also remain an explicit per-run action.

The backend has no public methods for:

- `exec`, attach, copy, logs-follow, arbitrary inspect paths;
- Compose/YAML execution;
- image build/commit/push;
- volume creation or writable host mounts;
- Docker socket mounts;
- host network/PID/IPC;
- privileged mode or added Linux capabilities.

Docker Engine 29.2.1 does not apply host port publishing to an `--internal`
bridge. The backend therefore uses a normal dedicated bridge and restricts host
ingress with an explicit `127.0.0.1` binding. It does not claim daemon-level
egress isolation; images are pulled before container creation and model data is
provided by a local read-only mount.

## Hardened container contract

The generated `docker create` command includes:

```text
--read-only
--cap-drop=ALL
--security-opt=no-new-privileges
--gpus=1
--cpus=<bounded>
--memory=<bounded bytes>
--pids-limit=512
--shm-size=<bounded bytes>
--tmpfs=/tmp:rw,nosuid,size=<bounded bytes>
--tmpfs=/root/.cache:rw,nosuid,size=<bounded bytes>
--env=HF_HUB_OFFLINE=1
--env=TRANSFORMERS_OFFLINE=1
--mount=type=bind,src=<operator model dir>,dst=/models/current,readonly
--publish=127.0.0.1::<template port>
--entrypoint=<trusted template executable>
```

Image and effective argv come only from a trusted, recomputed `LabRunSpec` bound
to a built-in `RuntimeTemplate`. Direct construction and `dataclasses.replace`
forgeries fail before Docker is called.

## Ownership and cleanup

Every resource has all labels:

```text
serving-verdict.owner=lab
serving-verdict.run-id=<deterministic run ID>
serving-verdict.template-digest=<template digest>
serving-verdict.resource-id=<process-unique resource ID>
```

Deletion requires both the locally held daemon handle and an exact label match.
Name prefix alone is never authority. A mismatched resource is not stopped or
removed; lifecycle success is withheld.

## Why the first live smoke does not start vLLM

The host currently runs production/rollback inference workloads in shared GB10
memory. The first smoke deliberately exercised only Docker/NVIDIA capability and
exact local image-digest resolution. A real model container will be started only
inside the next opt-in dogfood gate after resource availability and rollback
state are recorded.
