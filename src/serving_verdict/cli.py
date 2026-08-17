"""Command-line interface for Serving Verdict.

Commands (MVP spec):
    import-case CASE.yaml --out BUNDLE.json [--json]
    verify BUNDLE.json [--json]
    list DATA_DIR [--json]
    show BUNDLE.json [--json]
    serve --host 127.0.0.1 --port 8787 --data-dir DATA_DIR

Exit codes: 0 success (incl. valid REJECT/INCONCLUSIVE imports and passing
verify); 2 usage/config/load error; 4 bundle integrity verification failure.
JSON mode emits exactly one JSON object on stdout; diagnostics go to stderr.
The data payload of `list` is emitted on stdout in BOTH modes (diagnostics
only go to stderr); `list` therefore always produces parseable stdout.
"""
from __future__ import annotations

import argparse
import json
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
    CaseConfigError,
    IntegrityError,
    ServingVerdictError,
    UsageError,
)
from serving_verdict.metrics import registry as METRIC_REGISTRY

ONLY_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def _emit_json(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _diag(message: str) -> None:
    print(f"serving-verdict: {message}", file=sys.stderr)


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
        bundle = import_case(args.case)
    except CaseConfigError as exc:
        _diag(f"case config error: {exc}")
        return 2
    except UsageError as exc:
        _diag(f"cannot produce a bundle: {exc}")
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


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        bundle = load_bundle(args.bundle)
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
    if args.json:
        _emit_json(
            {
                "valid": True,
                "case_id": bundle["case_id"],
                "verdict": bundle["verdict"],
                "digest": report["digest"],
            }
        )
    else:
        _diag(f"verified {bundle['case_id']} ({bundle['verdict']}): {report['digest']}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        if args.json:
            _emit_json({"data_dir": str(data_dir), "verdicts": [], "error": "data dir not found"})
        else:
            _diag(f"data dir not found: {data_dir}")
        return 2
    verdicts: list[dict[str, Any]] = []
    for path in sorted(data_dir.glob("*.json")):
        try:
            bundle = load_bundle(path)
            if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
                continue
        except (UsageError, ServingVerdictError):
            continue
        try:
            verify_bundle(bundle)
        except IntegrityError:
            # A tampered/corrupt file is never indexed; it is not evidence
            # (same fail-closed rule as the server's /api/v1/verdicts index).
            _diag(f"skipping bundle failing integrity check: {path.name}")
            continue
        verdicts.append(
            {
                "case_id": bundle.get("case_id"),
                "file": path.name,
                "verdict": bundle.get("verdict"),
                "reason_codes": bundle.get("reason_codes", []),
                "bundle_digest": bundle.get("bundle_digest"),
                "created_at": bundle.get("created_at"),
            }
        )
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


def _cmd_serve(args: argparse.Namespace) -> int:
    if args.host != ONLY_BIND_HOST:
        _diag(
            f"refusing to bind {args.host!r}: MVP only binds {ONLY_BIND_HOST!r} "
            "(no override flag)"
        )
        return 2
    data_dir = Path(args.data_dir)
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
    p_import.add_argument("--json", action="store_true")

    p_verify = sub.add_parser("verify", help="verify a bundle's integrity offline")
    p_verify.add_argument("bundle")
    p_verify.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="list verdict bundles in a data dir")
    p_list.add_argument("data_dir")
    p_list.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="print one verdict bundle")
    p_show.add_argument("bundle")
    p_show.add_argument("--json", action="store_true")

    p_serve = sub.add_parser("serve", help="serve bundles read-only on loopback")
    p_serve.add_argument("--host", default=ONLY_BIND_HOST)
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.add_argument("--data-dir", default="data")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "import-case": _cmd_import,
        "verify": _cmd_verify,
        "list": _cmd_list,
        "show": _cmd_show,
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
