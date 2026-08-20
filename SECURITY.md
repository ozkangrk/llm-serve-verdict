# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.4.x | ✅ |
| 0.3.x | ❌ (superseded) |
| 0.2.x | ❌ (superseded) |
| 0.1.x | ❌ (superseded) |

Only the latest 0.3.x minor is supported. Older releases are not patched.

## Reporting a vulnerability

**Please do not open a public GitHub issue for a security concern.** Public
issues are discoverable and may disclose a weakness before a fix exists.

Instead:

1. Use the repository's **security advisories** feature:
   GitHub → this repo → *Security* tab → *New draft security advisory*.
   This keeps the report private until you (or a maintainer) publish it.
2. Or email the maintainers directly (see the author list in `pyproject.toml`)
   with the subject prefix `[security] serving-verdict`.

Include:

- The affected version.
- A minimal reproduction (a `case.yaml`, the bound artifact files, and the
  command) if you can share it.
- What the expected behavior was and what actually happened.

We aim to acknowledge within **5 business days** and to coordinate a fix and
disclosure on a reasonable timeline. If we publish a fix, we will reference it
in a private advisory and note the affected range.

## What counts as a security issue

This project is deliberately narrow, so "security" here is specific. Things
that *are* in scope:

- A way to make the tool **execute** or **interpret** artifact content as code
  (it must only parse it as data).
- A path traversal / symlink escape that lets the loader read files **outside
  the operator-approved source root**.
- An integrity (canonical-digest) bypass that lets a tampered bundle pass
  `verify` or appear in the loopback UI.
- The loopback server binding to a non-loopback host, or accepting a
  user-supplied source root over HTTP.
- An Automation endpoint accepting remote/credential-bearing URLs, exposing an
  API-key value, writing benchmark state into the trial/data store, or allowing
  unbounded concurrent jobs.
- Raw replay prompts, tool arguments, credentials or remote error bodies
  appearing in artifacts, API responses, logs or CI summaries.

Things that are **not** vulnerabilities (by design):

- A `REJECT` or `INCONCLUSIVE` verdict — that is the tool working correctly.
- A bundle that fails integrity verification and is *excluded* — that is the
  fail-closed path working correctly.
- The two real-world case configs not reproducing on your machine — they bind
  absolute source-root paths on one machine; that is documented, not a flaw.

## Our design invariants (the contract)

The security model rests on these invariants, enforced in code and covered by
tests (`tests/test_evidence_loader.py`, `tests/test_server.py`,
`tests/test_canonical.py`):

- Never executes artifact content; never invokes a shell or Docker.
- Canonicalizes the source root and rejects absolute child paths, `..`
  traversal, symlink escape, special files, and files >20 MiB.
- Serves over `127.0.0.1` only; any other host is rejected at startup. Bundle,
  trial and artifact APIs are read-only. Automation POST routes create bounded
  in-memory jobs only and never mutate the data directory or runtime.
- Automation endpoint targets are loopback-only, credentials are environment-
  only, and public job payloads never contain secret values or raw remote
  errors. Cancellation is cooperative and discards any later result.
- Replay artifacts retain keyed fingerprints and bounded measurements, not raw
  message content.
- Verdicts are a deterministic function of the bound evidence and case policy;
  no model participates and no flag can flip a `REJECT` to a `PROMOTE`.
- Bundles that fail the canonical-digest check are never indexed or served.

## Security contact

Contact details are the maintainers of record listed in `pyproject.toml`.
Prefer the private security-advisory channel so nothing is disclosed publicly
before a fix is available.
