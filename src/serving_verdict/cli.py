"""Command-line interface for Serving Verdict.

Commands (MVP v0.1 + v0.2 portable trial backend):
    import-case CASE.yaml --out BUNDLE.json [--source-root DIR] [--archive DIR] [--json]
    verify BUNDLE.json [--archive] [--json]
    list DATA_DIR [--json]
    show BUNDLE.json [--json]
    demo [--out-dir DIR] [--json]
    history [DATA_DIR] [--json]
    reindex [DATA_DIR] [--json]
    serve --host 127.0.0.1 --port 8787 [--data-dir DATA_DIR]

Exit codes: 0 success (incl. valid REJECT/INCONCLUSIVE imports and passing
verify); 2 usage/config/load error; 4 bundle integrity verification failure.
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
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _diag(message: str) -> None:
    print(f"serving-verdict: {message}", file=sys.stderr)


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
    if root is None:
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

    archive_root = bundle_path.parent / "archive"
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
        obj = archive_root.joinpath(rel_path)
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
# argparse wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="serving-verdict",
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
    p_verify.add_argument("--json", action="store_true")

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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "import-case": _cmd_import,
        "verify": _cmd_verify,
        "list": _cmd_list,
        "show": _cmd_show,
        "demo": _cmd_demo,
        "history": _cmd_history,
        "reindex": _cmd_reindex,
        "serve": _cmd_serve,
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
