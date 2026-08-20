# Inference Lab lifecycle contract

This module owns one disposable lab run through an injected narrow backend. It
does not import a container SDK and cannot express a generic daemon request,
container command, attach, build, push, prune, volume or secret operation.

## State machine

```text
PLANNED → PULLING → NETWORK_CREATING → STARTING → READY
→ BENCHMARKING → FINALIZING → STOPPING → SUCCEEDED

active → CANCEL_REQUESTED → STOPPING → CANCELLED
active failure → STOPPING → FAILED
cleanup or absence-proof failure → CLEANUP_FAILED
```

A successful work result is not published until owned container/network cleanup
has completed and `verify_absent` confirms both resources are gone.

## Ownership fencing

Every backend call receives immutable ownership:

- owner: `serving-verdict-lab`;
- deterministic run ID;
- exact runtime-template digest.

Container and network IDs are deterministic derivatives of the run ID. A real
backend must verify all labels plus its held resource handle before stop/remove.
A name prefix alone is never deletion authority.

## Failure and cancellation

- Backend exception text never enters the result; only fixed categories
  `lifecycle_failed`, `work_failed`, or `cleanup_failed` are exposed.
- Each attempted resource is cleanup-eligible even if create raised after a
  partial side effect.
- Stop, container remove and network remove are attempted independently so one
  failure does not skip later cleanup.
- Cancellation before start has zero backend calls.
- Cancellation during non-interruptible work is honest: the call may finish,
  but its late result is discarded before cleanup.
- `BaseException` still executes the finalizer and is then re-raised.
- `run_timeout_s` is passed as a mandatory effective deadline to the work
  capability. The benchmark/transport implementation is responsible for
  enforcing it on its owned I/O; the lifecycle does not create an unkillable
  worker thread.
- One lifecycle instance is single-flight and rejects concurrent execution.

## Success boundary

`SUCCEEDED` means the work callback returned, cancellation was not requested,
all cleanup calls succeeded, and the backend proved resources absent.
`CLEANUP_FAILED` is terminal and carries no successful result even when the
benchmark itself completed.

The fake backend suite exercises every start/cleanup failure, cancellation,
concurrency, ownership propagation and process-interruption path. Real Docker
E2E remains an opt-in release gate.
