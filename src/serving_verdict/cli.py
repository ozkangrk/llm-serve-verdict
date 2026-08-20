"""Command-line interface for LLM ServeVerdict.

Commands (MVP v0.1 + v0.2 portable trial backend):
    import-case CASE.yaml --out BUNDLE.json [--source-root DIR] [--archive DIR] [--json]
    verify BUNDLE.json [--archive] [--json]
    gate BUNDLE --require PROMOTE [--require-signature --trust-store PATH]
          [--fail-inconclusive] [--json] [--github-summary PATH]
    list DATA_DIR [--json]
    show BUNDLE.json [--json]
    demo [--out-dir DIR] [--json]
    history [DATA_DIR] [--json]
    reindex [DATA_DIR] [--json]
    serve --host 127.0.0.1 --port 8787 [--data-dir DATA_DIR]
    endpoint check ENDPOINT.yaml [--allow-remote] [--json]
    bench run --endpoint ENDPOINT.yaml --profile quick --out RUN.json [--json]

Exit codes: 0 success (incl. valid REJECT/INCONCLUSIVE imports and passing
verify); 2 usage/config/load error; 4 bundle/artifact integrity verification
failure.
``gate`` extends the stable CI contract (docs/CI_INTEGRATION.md):
0 requirement satisfied; 2 usage/config/load; 4 integrity/signature/trust;
5 valid REJECT / requirement not met; 6 valid INCONCLUSIVE only with
--fail-inconclusive (otherwise 0 with blocked=false).
JSON mode emits exactly one JSON object on stdout; diagnostics go to stderr.
The data payload of `list`, `history` and `reindex` is emitted on stdout in
BOTH modes (diagnostics only go to stderr).

Data-dir resolution for `history`/`reindex`/`serve`: explicit argument >
``SERVING_VERDICT_DATA_DIR`` environment variable > ``./data``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from serving_verdict.ci_gate import VALID_REQUIREMENTS as ci_gate_REQUIREMENTS
from serving_verdict.engine import (
    BUNDLE_SCHEMA_VERSION,
    import_case,
    load_bundle,
    verify_bundle,
)
from serving_verdict.errors import (
    ArchiveError,
    CaseConfigError,
    IntegrityError,
    ServingVerdictError,
    UsageError,
)
from serving_verdict.metrics import registry as METRIC_REGISTRY

ONLY_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DATA_DIR_ENV = "SERVING_VERDICT_DATA_DIR"
DEFAULT_DATA_DIR = "data"


def _emit_json(obj: Any) -> None:
    # Public machine-output sink. Signing paths expose only the DSSE signature,
    # public-key-derived key ID and bounded status fields; the environment-only
    # Ed25519 seed is never part of `obj` (covered by signing/CLI leak tests).
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _diag(message: str) -> None:
    print(f"llm-serve-verdict: {message}", file=sys.stderr)


def _resolve_data_dir(explicit: str | None) -> Path:
    """arg > env(SERVING_VERDICT_DATA_DIR) > ./data"""
    if explicit:
        return Path(explicit)
    env = os.environ.get(DATA_DIR_ENV)
    if env:
        return Path(env)
    return Path(DEFAULT_DATA_DIR)


# ---------------------------------------------------------------------------
# command handlers (return process exit code)
# ---------------------------------------------------------------------------


def _cmd_import(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if not out.parent.is_dir():
        _diag(
            f"output directory does not exist: {out.parent} "
            "(import-case does not create directories; create it first)"
        )
        return 2
    try:
        bundle = import_case(args.case, source_root_override=args.source_root)
    except CaseConfigError as exc:
        _diag(f"case config error: {exc}")
        return 2
    except UsageError as exc:
        _diag(f"cannot produce a bundle: {exc}")
        return 2

    if args.archive:
        try:
            manifest = _archive_evidence(args.case, bundle, args.archive, out.parent)
        except (ArchiveError, CaseConfigError) as exc:
            _diag(f"archive failed (nothing written): {exc}")
            return 2
        try:
            manifest_path = out.parent / "artifacts.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            _diag(f"cannot write archive manifest: {exc}")
            return 2

    try:
        out.write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _diag(f"cannot write bundle: {exc}")
        return 2
    if args.json:
        _emit_json(bundle)
    else:
        _diag(
            f"imported {bundle['case_id']} -> {bundle['verdict']} "
            f"({', '.join(bundle['reason_codes'])}) written to {out}"
        )
    return 0


def _archive_evidence(
    case_path: str, bundle: dict[str, Any], archive_dir: str, bundle_parent: Path
) -> dict[str, Any]:
    """Copy every referenced artifact into the content-addressed store.

    Fails closed (ArchiveError, nothing written) if any referenced artifact
    cannot be loaded safely (missing, symlink escape, special file, over the
    20 MiB bound) or if a stored copy fails its post-copy hash verification.
    """
    from serving_verdict.archive import ArchiveStore
    from serving_verdict.caseconfig import load_case_config
    from serving_verdict.engine import _resolve_source_root
    from serving_verdict.evidence import EvidenceLoader

    cfg = load_case_config(case_path)
    root = _resolve_source_root(Path(case_path), cfg.source_root)
    if root is None or not root.is_dir():
        raise ArchiveError("case source root does not exist; cannot archive evidence")
    loader = EvidenceLoader(root)
    store = ArchiveStore(archive_dir)
    refs: list[tuple[str, str]] = [
        (cfg.baseline.relative_path, cfg.baseline.sha256),
        (cfg.candidate.relative_path, cfg.candidate.sha256),
    ]
    refs.extend((e.source, e.sha256) for e in cfg.supplemental)
    seen: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for rel, expected in refs:
        if rel in seen:
            continue
        try:
            blob = loader.load_artifact(rel)
        except ServingVerdictError as exc:
            raise ArchiveError(f"cannot archive {rel}: {exc}") from exc
        if blob.sha256 != expected.lower():
            # The bound hash differs from the on-disk content: the importer
            # produced an INCONCLUSIVE (or mismatch) verdict; archiving a
            # different payload than the case bound would corrupt the
            # archived-verification story. Fail closed.
            raise ArchiveError(
                f"archive aborted: {rel} content does not match the bound sha256"
            )
        entry = store.put(Path(blob.resolved_path), base_dir=loader.canonical_root)
        seen[rel] = entry.sha256
        sizes[rel] = entry.size_bytes
    artifacts = {
        rel: {
            "sha256": sha,
            "size_bytes": sizes[rel],
            "path": f"objects/{sha[:2]}/{sha}",
        }
        for rel, sha in seen.items()
    }
    manifest = {
        "schema_version": "serving-verdict.artifacts.v0.1",
        "bundle_case_id": bundle.get("case_id"),
        "archive_root": os.path.relpath(
            Path(archive_dir).resolve(), bundle_parent.resolve()
        ),
        "artifacts": artifacts,
    }
    return manifest


def _verify_archive(bundle_path: Path) -> int:
    """Verify the archived artifacts next to a bundle. Returns the count of
    verified objects. Raises UsageError (no manifest) or IntegrityError
    (tampered/missing store object)."""
    manifest_path = bundle_path.parent / "artifacts.json"
    if not manifest_path.is_file():
        raise UsageError(
            f"no archive manifest found next to bundle: {manifest_path} "
            "(use import-case --archive to create one)"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UsageError(f"archive manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "serving-verdict.artifacts.v0.1":
        raise UsageError("archive manifest has an unsupported schema_version")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise UsageError("archive manifest is malformed: missing 'artifacts' mapping")
    import hashlib

    archive_locator = manifest.get("archive_root", "archive")
    if not isinstance(archive_locator, str) or not archive_locator:
        raise UsageError("archive manifest has an invalid archive_root")
    archive_root = (bundle_path.parent / archive_locator).resolve()
    verified = 0
    for rel, entry in artifacts.items():
        if not isinstance(entry, dict):
            raise IntegrityError(f"archive manifest entry malformed for {rel}")
        sha = entry.get("sha256")
        rel_path = entry.get("path")
        if not isinstance(sha, str) or len(sha) != 64 or not isinstance(rel_path, str):
            raise IntegrityError(f"archive manifest entry malformed for {rel}")
        # the manifest path must be the canonical content-addressed layout
        if rel_path != f"objects/{sha[:2]}/{sha}":
            raise IntegrityError(f"archive manifest path is not content-addressed: {rel}")
        obj = archive_root.joinpath(rel_path).resolve()
        try:
            obj.relative_to(archive_root)
        except ValueError as exc:
            raise IntegrityError(f"archive object escapes store root: {rel}") from exc
        if not obj.is_file():
            raise IntegrityError(f"archived artifact missing from store: {rel}")
        digest = hashlib.sha256()
        try:
            with open(obj, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError as exc:
            raise IntegrityError(f"cannot read archived artifact {rel}: {exc}") from exc
        if digest.hexdigest() != sha:
            raise IntegrityError(f"archived artifact hash mismatch: {rel}")
        verified += 1
    return verified


def _cmd_verify(args: argparse.Namespace) -> int:
    bundle_path = Path(args.bundle)
    try:
        bundle = load_bundle(bundle_path)
    except UsageError as exc:
        if args.json:
            _emit_json({"valid": False, "error": str(exc)})
        else:
            _diag(str(exc))
        return 2

    is_v04 = bundle.get("schema_version") == "serving-verdict.bundle.v0.4"
    if is_v04:
        if args.archive:
            _diag("--archive verification is only defined for compatibility bundles")
            return 2
        try:
            if args.trust_store is not None:
                from serving_verdict.signing import load_trust_store

                store = load_trust_store(args.trust_store)
            else:
                store = None
            if args.require_signature or store is not None:
                from serving_verdict.signing import verify_signed_bundle

                report = verify_signed_bundle(
                    bundle,
                    store=store,
                    require_signed=True if args.require_signature else None,
                )
            else:
                from serving_verdict.bundle_v04 import verify_v04_bundle

                digest_report = verify_v04_bundle(bundle)
                report = {
                    "status": "valid",
                    "digest": digest_report["digest"],
                    "digest_valid": True,
                    "signature_present": bundle.get("signature") is not None,
                    "signature_valid": False,
                    "signer_trusted": False,
                    "signer": None,
                    "key_id": None,
                    "offline": True,
                }
        except UsageError as exc:
            if args.json:
                _emit_json({"valid": False, "error": str(exc)})
            else:
                _diag(str(exc))
            return 2
        except IntegrityError as exc:
            error_payload = {"valid": False, "error": str(exc)}
            code = getattr(exc, "code", None)
            if code is not None:
                error_payload["code"] = code
            if args.json:
                _emit_json(error_payload)
            else:
                _diag(f"signature/integrity verification failed: {exc}")
            return 4
        v04_payload = {
            "valid": True,
            "case_id": bundle["case"]["case_id"],
            "verdict": bundle["verdict"],
            **report,
        }
        _emit_json(v04_payload)
        return 0

    if args.require_signature or args.trust_store is not None:
        error = "SIGNATURE_MISSING: compatibility bundle has no v0.4 signature"
        if args.json:
            _emit_json({"valid": False, "code": "SIGNATURE_MISSING", "error": error})
        else:
            _diag(error)
        return 4
    try:
        report = verify_bundle(bundle)
    except IntegrityError as exc:
        if args.json:
            _emit_json({"valid": False, "case_id": bundle.get("case_id"), "error": str(exc)})
        else:
            _diag(f"integrity verification failed: {exc}")
        return 4
    payload: dict[str, Any] = {
        "valid": True,
        "case_id": bundle["case_id"],
        "verdict": bundle["verdict"],
        "digest": report["digest"],
    }
    if args.archive:
        try:
            payload["artifacts_verified"] = _verify_archive(bundle_path)
        except UsageError as exc:
            if args.json:
                _emit_json({"valid": False, "case_id": bundle.get("case_id"), "error": str(exc)})
            else:
                _diag(str(exc))
            return 2
        except IntegrityError as exc:
            if args.json:
                _emit_json(
                    {
                        "valid": False,
                        "case_id": bundle.get("case_id"),
                        "digest": report["digest"],
                        "error": str(exc),
                    }
                )
            else:
                _diag(f"archive verification failed: {exc}")
            return 4
    if args.json:
        _emit_json(payload)
    else:
        suffix = f", {payload['artifacts_verified']} archived artifact(s)" if args.archive else ""
        _diag(f"verified {bundle['case_id']} ({bundle['verdict']}): {report['digest']}{suffix}")
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    """Stable production CI promotion gate (FR-7).

    Verifies the bundle through the existing integrity/signature/trust
    paths FIRST, then evaluates the sealed verdict against --require.
    Never trusts a client-claimed verdict outside the digest-sealed doc.
    """
    from serving_verdict import ci_gate

    bundle_path = Path(args.bundle)
    try:
        bundle = load_bundle(bundle_path)
    except UsageError as exc:
        _gate_error(args.json, str(exc), case_id=None)
        return 2

    store = None
    if args.trust_store is not None:
        from serving_verdict.signing import load_trust_store

        try:
            store = load_trust_store(args.trust_store)
        except UsageError as exc:
            _gate_error(args.json, f"trust store error: {exc}", case_id=None)
            return 2

    try:
        outcome = ci_gate.gate_bundle(
            bundle,
            required_verdict=args.require,
            fail_inconclusive=args.fail_inconclusive,
            store=store,
            require_signed=args.require_signature,
        )
    except UsageError as exc:
        _gate_error(
            args.json, str(exc), case_id=ci_gate._extract_case_id(bundle)
        )
        return 2
    except IntegrityError as exc:
        outcome = ci_gate.GateOutcome.integrity_failure(
            case_id=ci_gate._extract_case_id(bundle),
            bundle_version=str(bundle.get("schema_version", ""))[:64],
            error=str(exc),
            code=getattr(exc, "code", "INTEGRITY_FAILURE"),
        )

    if args.github_summary is not None:
        try:
            ci_gate.write_github_summary(outcome, args.github_summary)
        except UsageError as exc:
            _gate_error(args.json, str(exc), case_id=outcome.case_id or None)
            return 2

    if args.json:
        _emit_json(outcome.to_json_dict())
    else:
        suffix = f" -> {args.github_summary}" if args.github_summary else ""
        _diag(
            f"gate {outcome.case_id or '<unknown>'} ({outcome.verdict or 'n/a'}"
            f", require={outcome.required}): {outcome.reason} "
            f"[exit {outcome.exit_code}]{suffix}"
        )
    return outcome.exit_code


def _gate_error(json_mode: bool, message: str, case_id: str | None) -> None:
    """Emit the usage-error payload for `gate` (one JSON object on stdout)."""
    if json_mode:
        payload: dict[str, Any] = {
            "schema_version": "serving-verdict.gate-result.v0.1",
            "command": "gate",
            "case_id": case_id or "",
            "blocked": False,
            "decision": "ERROR",
            "exit_code": 2,
            "reason": f"usage/config error: {message}",
            "error": message,
        }
        _emit_json(payload)
    else:
        _diag(f"gate: {message}")


def _cmd_sign(args: argparse.Namespace) -> int:
    """Sign one valid v0.4 bundle with an environment-only Ed25519 seed."""
    try:
        bundle = load_bundle(Path(args.bundle))
    except UsageError as exc:
        _diag(str(exc))
        return 2
    if bundle.get("schema_version") != "serving-verdict.bundle.v0.4":
        _diag("sign requires a serving-verdict.bundle.v0.4 document")
        return 2
    seed = os.environ.get(args.key_env)
    if not seed:
        _diag(f"signing key environment variable is not set: {args.key_env}")
        return 2
    try:
        from serving_verdict.signing import (
            SignerIdentity,
            key_id_for_public_key,
            load_ed25519_private_key_from_seed_hex,
            public_key_bytes_of,
            sign_bundle,
        )

        private_key = load_ed25519_private_key_from_seed_hex(seed)
        identity = SignerIdentity(
            private_key=private_key,
            key_id=key_id_for_public_key(public_key_bytes_of(private_key)),
            signer=args.signer,
        )
        signed = sign_bundle(bundle, identity=identity)
    except (UsageError, IntegrityError) as exc:
        _diag(f"cannot sign bundle: {exc}")
        return 2
    out = Path(args.out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(signed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _diag(f"cannot write signed bundle: {exc}")
        return 2
    payload = {
        "signed": True,
        "case_id": signed["case"]["case_id"],
        "digest": signed["digest"],
        "signer": signed["signature"]["signer"],
        "key_id": signed["signature"]["key_id"],
        "out": str(out),
    }
    if args.json:
        _emit_json(payload)
    else:
        _diag(f"signed {payload['case_id']} -> {out}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        if args.json:
            _emit_json({"data_dir": str(data_dir), "verdicts": [], "error": "data dir not found"})
        else:
            _diag(f"data dir not found: {data_dir}")
        return 2
    from serving_verdict.server import bundle_rows

    verdicts = bundle_rows(data_dir)
    payload = {"data_dir": str(data_dir), "verdicts": verdicts}
    _emit_json(payload)  # data payload on stdout in both modes
    if not args.json:
        _diag(f"{len(verdicts)} bundle(s) in {data_dir}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        bundle = load_bundle(args.bundle)
    except UsageError as exc:
        if args.json:
            _emit_json({"error": str(exc)})
        else:
            _diag(str(exc))
        return 2
    if args.json:
        _emit_json(bundle)
    else:
        _emit_json(bundle)  # show is inherently a JSON document
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    from serving_verdict.demo import build_demo

    out_dir = Path(args.out_dir)
    if args.out_dir == "data/demo":
        out_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        bundles = build_demo(out_dir)
    except UsageError as exc:
        _diag(str(exc))
        return 2
    except OSError as exc:
        _diag(f"cannot write demo: {exc}")
        return 2
    payload = {
        "out_dir": str(out_dir),
        "cases": [
            {
                "case_id": json.loads(p.read_text(encoding="utf-8"))["case_id"],
                "bundle": p.name,
                "verdict": json.loads(p.read_text(encoding="utf-8"))["verdict"],
            }
            for p in bundles
        ],
    }
    if args.json:
        _emit_json(payload)
    else:
        for p in bundles:
            doc = json.loads(p.read_text(encoding="utf-8"))
            _diag(f"demo case {doc['case_id']} -> {doc['verdict']} ({p.parent})")
        _diag(f"demo written to {out_dir}")
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    data_dir = _resolve_data_dir(args.data_dir)
    if not data_dir.is_dir():
        if args.json:
            _emit_json({"data_dir": str(data_dir), "events": [], "error": "data dir not found"})
        else:
            _diag(f"data dir not found: {data_dir}")
        return 2
    from serving_verdict.trialstore import TrialStore

    store = TrialStore(data_dir)
    try:
        events = store.list_events()
    except UsageError as exc:
        _diag(str(exc))
        return 2
    payload = {"data_dir": str(data_dir), "events": events}
    _emit_json(payload)  # data payload on stdout in both modes
    if not args.json:
        _diag(f"{len(events)} trial event(s) in {data_dir}")
    return 0


def _cmd_reindex(args: argparse.Namespace) -> int:
    data_dir = _resolve_data_dir(args.data_dir)
    if not data_dir.is_dir():
        if args.json:
            _emit_json({"data_dir": str(data_dir), "error": "data dir not found"})
        else:
            _diag(f"data dir not found: {data_dir}")
        return 2
    from serving_verdict.trialstore import TrialStore

    store = TrialStore(data_dir)
    try:
        report = store.reindex()
    except UsageError as exc:
        _diag(str(exc))
        return 2
    payload = {"data_dir": str(data_dir), **report, "trials": store.list_trials()}
    _emit_json(payload)  # data payload on stdout in both modes
    if not args.json:
        _diag(
            f"reindexed {data_dir}: {report['indexed']} valid, "
            f"{report['invalid']} invalid, {report['missing']} missing"
        )
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    if args.host != ONLY_BIND_HOST:
        _diag(
            f"refusing to bind {args.host!r}: MVP only binds {ONLY_BIND_HOST!r} "
            "(no override flag)"
        )
        return 2
    data_dir = _resolve_data_dir(args.data_dir)
    if not data_dir.is_dir():
        _diag(f"data dir not found: {data_dir}")
        return 2
    from serving_verdict.server import run_server

    try:
        run_server(host=args.host, port=args.port, data_dir=data_dir)
    except OSError as exc:
        _diag(f"server failed: {exc}")
        return 2
    return 0


# ---------------------------------------------------------------------------
# benchmark endpoint / runner commands
# ---------------------------------------------------------------------------


def _cmd_endpoint_check(args: argparse.Namespace) -> int:
    from serving_verdict.endpoint import (
        EndpointConfigError,
        load_endpoint_config,
        resolve_api_key,
    )
    from serving_verdict.preflight import EndpointPreflightError, preflight_endpoint

    try:
        config = load_endpoint_config(args.endpoint, allow_remote=args.allow_remote)
    except EndpointConfigError as exc:
        if args.json:
            _emit_json({"ok": False, "error": f"endpoint config error: {exc}"})
        else:
            _diag(f"endpoint config error: {exc}")
        return 2
    try:
        api_key = resolve_api_key(config)
        result = preflight_endpoint(config, timeout_s=args.timeout)
    except EndpointConfigError as exc:
        # The env var holds the secret; never echo its value.
        message = f"endpoint API key is not set: {exc}"
        if args.json:
            _emit_json({"ok": False, "error": message, "endpoint_id": config.endpoint_id})
        else:
            _diag(message)
        return 2
    except EndpointPreflightError as exc:
        message = f"endpoint preflight failed: {exc}"
        if args.json:
            _emit_json({"ok": False, "error": message, "endpoint_id": config.endpoint_id})
        else:
            _diag(message)
        return 2
    del api_key  # the key never leaves this scope and never reaches output
    payload = {
        "ok": True,
        "endpoint_id": config.endpoint_id,
        "requested_model": result.requested_model,
        "served_model": result.served_model,
        "models_probe": result.models_probe,
        "chat_probe": result.chat_probe,
        "model_ids": list(result.model_ids),
    }
    if args.json:
        _emit_json(payload)
    else:
        _diag(
            f"endpoint {config.endpoint_id} preflight OK "
            f"(served model: {result.served_model})"
        )
    return 0


def _cmd_bench_run(args: argparse.Namespace) -> int:
    from serving_verdict.benchmark_runner import (
        BenchmarkRunError,
        run_quick_benchmark,
    )
    from serving_verdict.endpoint import (
        EndpointConfigError,
        load_endpoint_config,
        resolve_api_key,
    )
    from serving_verdict.preflight import EndpointPreflightError
    from serving_verdict.profile import get_profile

    try:
        profile = get_profile(args.profile)
    except LookupError as exc:
        _diag(str(exc))
        return 2
    try:
        config = load_endpoint_config(args.endpoint, allow_remote=args.allow_remote)
        api_key = resolve_api_key(config)
    except (EndpointConfigError, EndpointPreflightError, LookupError) as exc:
        if args.json:
            _emit_json({"ok": False, "error": f"endpoint configuration error: {exc}"})
        else:
            _diag(f"endpoint configuration error: {exc}")
        return 2
    out_path = Path(args.out)
    try:
        result = run_quick_benchmark(
            config,
            api_key=api_key,
            profile=profile,
            transport_timeout_s=args.timeout,
        )
    except (EndpointConfigError, EndpointPreflightError) as exc:
        _diag(f"endpoint preflight failed: {exc}")
        return 2
    except BenchmarkRunError as exc:
        _diag(f"benchmark run failed: {exc}")
        return 2
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result.artifact, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        _diag(f"cannot write benchmark artifact: {exc}")
        return 2
    summary = result.public_summary()
    summary["gates"] = result.artifact["gates"]
    summary["artifact_path"] = str(out_path)
    if args.json:
        _emit_json(summary)
    else:
        _diag(
            f"benchmark run sealed: {summary['run_id']} "
            f"(status: {summary['run_status']}) written to {out_path}"
        )
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-serve-verdict",
        description="Deterministic, tamper-evident PROMOTE/REJECT/INCONCLUSIVE decisions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import-case", help="import a case config into a verdict bundle")
    p_import.add_argument("case")
    p_import.add_argument("--out", required=True)
    p_import.add_argument(
        "--source-root",
        default=None,
        metavar="DIR",
        help="override the case's source_root (absolute directory; CLI-only)",
    )
    p_import.add_argument(
        "--archive",
        default=None,
        metavar="DIR",
        help="copy referenced evidence into a content-addressed store under DIR",
    )
    p_import.add_argument("--json", action="store_true")

    p_verify = sub.add_parser("verify", help="verify a bundle's integrity offline")
    p_verify.add_argument("bundle")
    p_verify.add_argument(
        "--archive",
        action="store_true",
        help="also verify the archived artifacts (manifest next to the bundle)",
    )
    p_verify.add_argument(
        "--require-signature",
        action="store_true",
        help="require a trusted v0.4 DSSE/Ed25519 verdict signature",
    )
    p_verify.add_argument(
        "--trust-store",
        default=None,
        metavar="PATH",
        help="strict local JSON trust store for offline signature verification",
    )
    p_verify.add_argument("--json", action="store_true")

    p_gate = sub.add_parser(
        "gate",
        help="stable production CI promotion gate (verify, then require a verdict)",
    )
    p_gate.add_argument("bundle")
    p_gate.add_argument(
        "--require",
        required=True,
        choices=sorted(ci_gate_REQUIREMENTS),
        metavar="VERDICT",
        help="deployment requirement: any non-required verdict blocks deployment",
    )
    p_gate.add_argument(
        "--require-signature",
        action="store_true",
        help="require a trusted v0.4 DSSE/Ed25519 verdict signature (v0.4 only)",
    )
    p_gate.add_argument(
        "--trust-store",
        default=None,
        metavar="PATH",
        help="strict local JSON trust store for offline signature verification",
    )
    p_gate.add_argument(
        "--fail-inconclusive",
        action="store_true",
        help="exit 6 on a valid INCONCLUSIVE verdict (default: exit 0, blocked=false)",
    )
    p_gate.add_argument(
        "--github-summary",
        default=None,
        metavar="PATH",
        help="write a bounded, escaped GitHub markdown summary (no raw evidence)",
    )
    p_gate.add_argument("--json", action="store_true")

    p_sign = sub.add_parser("sign", help="sign a valid v0.4 bundle offline")
    p_sign.add_argument("bundle")
    p_sign.add_argument("--key-env", required=True, metavar="ENV_NAME")
    p_sign.add_argument("--signer", required=True)
    p_sign.add_argument("--out", required=True)
    p_sign.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="list verdict bundles in a data dir")
    p_list.add_argument("data_dir")
    p_list.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="print one verdict bundle")
    p_show.add_argument("bundle")
    p_show.add_argument("--json", action="store_true")

    p_demo = sub.add_parser(
        "demo", help="materialize the portable in-package demo (two cases: PROMOTE + REJECT)"
    )
    p_demo.add_argument("--out-dir", default="data/demo")
    p_demo.add_argument("--json", action="store_true")

    p_history = sub.add_parser("history", help="print the append-only trial history (JSON)")
    p_history.add_argument("data_dir", nargs="?", default=None)
    p_history.add_argument("--json", action="store_true")

    p_reindex = sub.add_parser("reindex", help="rebuild trial state from the data dir (JSON)")
    p_reindex.add_argument("data_dir", nargs="?", default=None)
    p_reindex.add_argument("--json", action="store_true")

    p_serve = sub.add_parser("serve", help="serve bundles read-only on loopback")
    p_serve.add_argument("--host", default=ONLY_BIND_HOST)
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.add_argument("--data-dir", default=None)

    p_endpoint = sub.add_parser("endpoint", help="endpoint utilities")
    p_endpoint_sub = p_endpoint.add_subparsers(dest="endpoint_command", required=True)
    p_endpoint_check = p_endpoint_sub.add_parser(
        "check", help="preflight-check a configured endpoint"
    )
    p_endpoint_check.add_argument("endpoint")
    p_endpoint_check.add_argument("--allow-remote", action="store_true")
    p_endpoint_check.add_argument("--timeout", type=float, default=10.0)
    p_endpoint_check.add_argument("--json", action="store_true")

    p_bench = sub.add_parser("bench", help="run frozen benchmark profiles")
    p_bench_sub = p_bench.add_subparsers(dest="bench_command", required=True)
    p_bench_run = p_bench_sub.add_parser(
        "run", help="run a frozen benchmark profile against an endpoint"
    )
    p_bench_run.add_argument("--endpoint", required=True)
    p_bench_run.add_argument("--profile", default="quick")
    p_bench_run.add_argument("--out", required=True)
    p_bench_run.add_argument("--allow-remote", action="store_true")
    p_bench_run.add_argument("--timeout", type=float, default=120.0)
    p_bench_run.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "import-case": _cmd_import,
        "verify": _cmd_verify,
        "gate": _cmd_gate,
        "sign": _cmd_sign,
        "list": _cmd_list,
        "show": _cmd_show,
        "demo": _cmd_demo,
        "history": _cmd_history,
        "reindex": _cmd_reindex,
        "serve": _cmd_serve,
        "endpoint": _cmd_endpoint_check,
        "bench": _cmd_bench_run,
    }
    return handlers[args.command](args)


def _entrypoint() -> None:
    sys.exit(main())


if __name__ == "__main__":
    _entrypoint()


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "METRIC_REGISTRY",
    "build_parser",
    "main",
]
