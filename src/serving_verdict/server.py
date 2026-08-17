"""Loopback-only read-only FastAPI server for verdict bundles.

MVP contract:
- Default and MVP-only bind is ``127.0.0.1``; any other host is rejected
  (``UsageError``) and there is no override flag.
- Read-only APIs only: ``GET /api/v1/health``, ``GET /api/v1/verdicts``,
  ``GET /api/v1/verdicts/{case_id}``, ``GET /api/v1/metrics``, ``GET /``.
  Any other method yields 405; unknown paths yield 404.
- The data directory is read by convention; the app never writes.
- Error responses carry a stable machine-readable ``error`` string and never
  include file contents or secrets.
- ``run_server`` blocks until the server shuts down; on SIGTERM uvicorn
  closes the listeners and releases the port (E2E-verified in tests).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from serving_verdict.engine import BUNDLE_SCHEMA_VERSION, load_bundle, verify_bundle
from serving_verdict.errors import IntegrityError, ServingVerdictError, UsageError
from serving_verdict.metrics import registry as METRIC_REGISTRY
from serving_verdict.web import web_root

logger = logging.getLogger("serving_verdict.server")

ONLY_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def _bundle_rows(data_dir: Path) -> list[dict[str, Any]]:
    """Read-only index of valid bundles in the data dir (CLI `list` parity)."""
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
            # A tampered/corrupt file is never indexed; it is not evidence.
            logger.warning("skipping bundle failing integrity check: %s", path.name)
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
    return verdicts


def _find_bundle(data_dir: Path, case_id: str) -> tuple[dict[str, Any], Path] | None:
    for path in sorted(data_dir.glob("*.json")):
        try:
            bundle = load_bundle(path)
        except (UsageError, ServingVerdictError):
            continue
        if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
            continue
        if bundle.get("case_id") == case_id:
            return bundle, path
    return None


def create_app(host: str, port: int, data_dir: str | Path) -> FastAPI:
    """Build the read-only FastAPI app.

    Raises UsageError (exit 2) unless ``host`` is exactly the loopback bind.
    """
    if host != ONLY_BIND_HOST:
        raise UsageError(
            f"refusing to bind {host!r}: MVP only binds {ONLY_BIND_HOST!r} (no override flag)"
        )
    data = Path(data_dir).resolve()

    app = FastAPI(title="Serving Verdict", version="0.1.0")

    @app.exception_handler(HTTPException)
    async def _flat_http_error(_request: Any, exc: HTTPException) -> Any:
        """Stable flat error body: {"error": "..."} — never file contents."""
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            message: str = detail["error"]
        elif isinstance(detail, str):
            message = detail
        else:
            message = "error"
        return JSONResponse(status_code=exc.status_code, content={"error": message})

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "bind_host": ONLY_BIND_HOST,
            "port": port,
            "read_only": True,
        }

    @app.get("/api/v1/verdicts")
    def verdicts() -> dict[str, Any]:
        if not data.is_dir():
            return {"data_dir": str(data), "verdicts": []}
        return {"data_dir": str(data), "verdicts": _bundle_rows(data)}

    @app.get("/api/v1/verdicts/{case_id}")
    def verdict_detail(case_id: str) -> dict[str, Any]:
        found = _find_bundle(data, case_id) if data.is_dir() else None
        if found is None:
            raise HTTPException(status_code=404, detail={"error": "case not found"})
        bundle, _path = found
        try:
            verify_bundle(bundle)
            integrity: dict[str, Any] = {"valid": True}
        except IntegrityError as exc:
            # Stable error string; no file contents or hashes of the file leak.
            raise HTTPException(
                status_code=422, detail={"error": f"bundle integrity verification failed: {exc}"}
            ) from exc
        return {
            "bundle": bundle,
            "integrity": integrity,
            # Denormalized, UI-convenience views (all derived from the bundle;
            # the bundle remains the single source of truth).
            "case_id": bundle["case_id"],
            "verdict": bundle["verdict"],
            "reason_codes": bundle["reason_codes"],
            "baseline": bundle["baseline"],
            "candidate": bundle["candidate"],
            "comparisons": bundle["comparisons"],
            "gates": bundle["gates"],
            "claim_boundary": bundle["claim_boundary"],
            "bundle_digest": bundle["bundle_digest"],
        }

    @app.get("/api/v1/metrics")
    def metrics() -> dict[str, Any]:
        return {
            "metrics": {
                metric_id: {
                    "unit": d.unit,
                    "direction": d.direction,
                    "procedure_version": d.procedure_version,
                    "aggregation": d.aggregation,
                    "definition": d.definition,
                }
                for metric_id, d in METRIC_REGISTRY.items()
            }
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(web_root() / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/ui.js")
    def ui_js() -> FileResponse:
        return FileResponse(web_root() / "ui.js", media_type="text/javascript; charset=utf-8")

    @app.get("/ui.css")
    def ui_css() -> FileResponse:
        return FileResponse(web_root() / "ui.css", media_type="text/css; charset=utf-8")

    return app


def run_server(host: str, port: int, data_dir: str | Path) -> None:
    """Run the loopback server until shutdown (blocking)."""
    import uvicorn

    app = create_app(host, port, data_dir)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
