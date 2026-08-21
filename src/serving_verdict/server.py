"""Loopback-only read-only FastAPI server for verdict bundles and trials.

MVP contract:
- Default and MVP-only bind is ``127.0.0.1``; any other host is rejected
  (``UsageError``) and there is no override flag.
- Read-only APIs only; any other method yields 405; unknown paths 404.
- The data directory and the trial registry are read by convention; the
  app never writes (the registry connection is opened read-only).
- Error responses carry a stable machine-readable ``error`` string and
  never include file contents or secrets.
- ``run_server`` blocks until the server shuts down; on SIGTERM uvicorn
  closes the listeners and releases the port (E2E-verified in tests).

v0.2 additions (v0.1 endpoints are unchanged):
- ``GET /api/v1/ready``         — readiness incl. trial-store availability.
- ``GET /api/v1/trials``        — current per-case trial state (registry +
                                  on-disk validity; the bundle file is the
                                  source of truth).
- ``GET /api/v1/trials/{id}``   — trial state + append-only event history +
                                  current bundle with integrity.
- ``GET /api/v1/artifacts/{sha}`` — serve one manifest-listed content-
                                  addressed object after re-hashing it.
"""
from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response

from serving_verdict import __version__
from serving_verdict.automation import AutomationError
from serving_verdict.endpoint import (
    EndpointConfig,
    EndpointConfigError,
    parse_endpoint_config,
    resolve_api_key,
)
from serving_verdict.engine import BUNDLE_SCHEMA_VERSION, load_bundle, verify_bundle
from serving_verdict.errors import IntegrityError, ServingVerdictError, UsageError
from serving_verdict.metrics import registry as METRIC_REGISTRY
from serving_verdict.web import web_root

logger = logging.getLogger("serving_verdict.server")

ONLY_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

ARTIFACTS_SCHEMA_VERSION = "serving-verdict.artifacts.v0.1"
_JSON_BODY = Body(...)


def bundle_rows(data_dir: Path) -> list[dict[str, Any]]:
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


def _trial_rows_readonly(data_dir: Path) -> list[dict[str, Any]]:
    """Current per-case state: registry rows + on-disk validity re-check.

    The registry is opened read-only (the server never writes); bundle files
    remain the source of truth, so a registry row is only reported ``valid``
    when its bundle file still exists and passes offline verification.
    """
    from serving_verdict.trialstore import TrialStore

    store = TrialStore(data_dir, initialize=False)
    if not store.db_path.is_file():
        # No registry yet: fall back to a pure on-disk view (all valid rows
        # only, no events) so the endpoint degrades gracefully.
        return [
            {**row, "events": 0}
            for row in (
                {
                    "case_id": r["case_id"],
                    "status": "valid",
                    "verdict": r["verdict"],
                    "reason_codes": r["reason_codes"],
                    "bundle_digest": r["bundle_digest"],
                    "bundle_file": r["file"],
                }
                for r in bundle_rows(data_dir)
            )
        ]
    try:
        rows = store.list_trials_readonly()
    except UsageError:
        return []
    disk_valid = {r["case_id"]: r["file"] for r in bundle_rows(data_dir)}
    out: list[dict[str, Any]] = []
    for row in rows:
        status = row["status"]
        if status == "valid":
            # re-check the on-disk file (source of truth)
            on_disk = disk_valid.get(row["case_id"])
            if on_disk is None or on_disk != row["bundle_file"]:
                status = "missing"
        row["status"] = status
        out.append(row)
    return out


def _load_artifacts_manifest(data_dir: Path) -> dict[str, Any] | None:
    """Locate the artifacts manifest in the data dir (first *.json parent)."""
    candidates: list[Path] = []
    for name in ("artifacts.json",):
        p = data_dir / name
        if p.is_file():
            candidates.append(p)
    if not candidates:
        # a bundle's manifest lives next to the bundle; scan data dir root
        # and any single bundle file's directory (the data dir itself).
        for path in sorted(data_dir.glob("*.json")):
            if path.name == "artifacts.json":
                candidates.append(path)
    if not candidates:
        return None
    try:
        doc = load_bundle(candidates[0])
    except (UsageError, ServingVerdictError):
        return None
    if not isinstance(doc, dict) or doc.get("schema_version") != ARTIFACTS_SCHEMA_VERSION:
        return None
    return doc


def create_app(
    host: str,
    port: int,
    data_dir: str | Path,
    *,
    automation_runner: Callable[[EndpointConfig, str], dict[str, Any]] | None = None,
    lab_executor: Any | None = None,
    lab_environment: Mapping[str, str] | None = None,
) -> FastAPI:
    """Build the read-only FastAPI app.

    Raises UsageError (exit 2) unless ``host`` is exactly the loopback bind.
    """
    if host != ONLY_BIND_HOST:
        raise UsageError(
            f"refusing to bind {host!r}: MVP only binds {ONLY_BIND_HOST!r} (no override flag)"
        )
    data = Path(data_dir).resolve()

    from serving_verdict.automation import JobManager, default_benchmark_runner

    jobs = JobManager(automation_runner or default_benchmark_runner)
    from serving_verdict.lab_jobs import LabJobManager

    lab_jobs = LabJobManager(
        lab_executor,
        environment=os.environ if lab_environment is None else lab_environment,
    )
    app = FastAPI(title="LLM ServeVerdict", version=__version__)

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

    @app.get("/api/v1/ready")
    def ready() -> Any:
        if not data.is_dir():
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": "data dir not found", "read_only": True},
            )
        from serving_verdict.trialstore import TrialStore

        db: dict[str, Any] = {"available": False}
        if (data / "trial_store.sqlite3").is_file():
            try:
                store = TrialStore(data, initialize=False)
                counts = store.status_report_readonly()
                version = store.user_version_readonly()
                db = {
                    "available": True,
                    "user_version": version,
                    "trials": counts["trials"],
                    "events": counts["events"],
                }
            except UsageError:
                db = {"available": False, "reason": "registry unreadable"}
        return {
            "status": "ready",
            "read_only": True,
            "bind_host": ONLY_BIND_HOST,
            "data_dir": str(data),
            "database": db,
        }

    @app.get("/api/v1/automation/capabilities")
    def automation_capabilities() -> dict[str, Any]:
        return jobs.capabilities()

    @app.post("/api/v1/automation/jobs", status_code=202)
    def start_automation_job(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            config = parse_endpoint_config(payload, allow_remote=False)
            api_key = resolve_api_key(config)
            job = jobs.start(config, api_key)
        except EndpointConfigError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except AutomationError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
        return job.public_payload()

    @app.get("/api/v1/automation/jobs/{job_id}")
    def automation_job(job_id: str) -> dict[str, Any]:
        try:
            return jobs.get(job_id).public_payload()
        except AutomationError as exc:
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc

    @app.post("/api/v1/automation/jobs/{job_id}/cancel")
    def cancel_automation_job(job_id: str) -> dict[str, Any]:
        try:
            return jobs.cancel(job_id).public_payload()
        except AutomationError as exc:
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc

    @app.get("/api/v1/lab/capabilities")
    def lab_capabilities() -> dict[str, Any]:
        return lab_jobs.capabilities()

    @app.post("/api/v1/lab/jobs", status_code=202)
    def start_lab_job(payload: Any = _JSON_BODY) -> dict[str, Any]:
        from serving_verdict.lab_jobs import LabJobError, parse_lab_start_payload

        if not lab_jobs.capabilities()["enabled"]:
            raise HTTPException(
                status_code=503,
                detail={"error": "Inference Lab is disabled or unavailable"},
            )
        try:
            request = parse_lab_start_payload(payload)
        except LabJobError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        try:
            return lab_jobs.start(request).public_payload()
        except LabJobError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc

    @app.get("/api/v1/lab/jobs/{job_id}")
    def lab_job(job_id: str) -> dict[str, Any]:
        from serving_verdict.lab_jobs import LabJobError

        try:
            return lab_jobs.get(job_id).public_payload()
        except LabJobError as exc:
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc

    @app.get("/api/v1/lab/jobs/{job_id}/live")
    def lab_live(job_id: str) -> dict[str, Any]:
        from serving_verdict.lab_jobs import LabJobError

        try:
            return lab_jobs.get(job_id).live_payload()
        except LabJobError as exc:
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc

    @app.post("/api/v1/lab/jobs/{job_id}/cancel")
    def cancel_lab_job(job_id: str) -> dict[str, Any]:
        from serving_verdict.lab_jobs import LabJobError

        try:
            return lab_jobs.cancel(job_id).public_payload()
        except LabJobError as exc:
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc

    @app.get("/api/v1/verdicts")
    def verdicts() -> dict[str, Any]:
        if not data.is_dir():
            return {"data_dir": str(data), "verdicts": []}
        return {"data_dir": str(data), "verdicts": bundle_rows(data)}

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
            raise HTTPException(
                status_code=422, detail={"error": f"bundle integrity verification failed: {exc}"}
            ) from exc
        return {
            "bundle": bundle,
            "integrity": integrity,
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

    @app.get("/api/v1/trials")
    def trials() -> dict[str, Any]:
        if not data.is_dir():
            raise HTTPException(status_code=404, detail={"error": "data dir not found"})
        rows = _trial_rows_readonly(data)
        # annotate with append-only event counts
        from serving_verdict.trialstore import TrialStore

        events: dict[str, int] = {}
        if (data / "trial_store.sqlite3").is_file():
            try:
                events = TrialStore(data, initialize=False).event_counts_readonly()
            except UsageError:
                events = {}
        for row in rows:
            row["events"] = events.get(row["case_id"], 0)
        return {"data_dir": str(data), "trials": rows}

    @app.get("/api/v1/trials/{case_id}")
    def trial_detail(case_id: str) -> dict[str, Any]:
        if not data.is_dir():
            raise HTTPException(status_code=404, detail={"error": "data dir not found"})
        from serving_verdict.trialstore import TrialStore

        store = (
            TrialStore(data, initialize=False)
            if (data / "trial_store.sqlite3").is_file()
            else None
        )
        trial = store.get_trial_readonly(case_id) if store is not None else None
        found = _find_bundle(data, case_id)
        if trial is None and found is None:
            raise HTTPException(status_code=404, detail={"error": "trial not found"})
        events = store.list_events_readonly(case_id) if store is not None else []
        payload: dict[str, Any] = {
            "case_id": case_id,
            "trial": trial or {
                "case_id": case_id,
                "status": "valid",
                "verdict": found[0]["verdict"] if found else None,
                "reason_codes": found[0]["reason_codes"] if found else [],
                "bundle_digest": found[0]["bundle_digest"] if found else None,
                "bundle_file": found[1].name if found else None,
            },
            "events": events,
        }
        if found is not None:
            bundle, _path = found
            try:
                verify_bundle(bundle)
                payload["bundle"] = bundle
                payload["integrity"] = {"valid": True}
            except IntegrityError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"error": f"bundle integrity verification failed: {exc}"},
                ) from exc
        return payload

    @app.get("/api/v1/artifacts/{sha}")
    def artifact(sha: str) -> Response:
        import re

        if not re.match(r"^[0-9a-f]{64}$", sha):
            raise HTTPException(status_code=404, detail={"error": "artifact not found"})
        if not data.is_dir():
            raise HTTPException(status_code=404, detail={"error": "data dir not found"})
        manifest = _load_artifacts_manifest(data)
        if manifest is None:
            raise HTTPException(status_code=404, detail={"error": "no artifact manifest"})
        entries = manifest.get("artifacts")
        if not isinstance(entries, dict):
            raise HTTPException(status_code=404, detail={"error": "no artifact manifest"})
        if sha not in {
            e.get("sha256") for e in entries.values() if isinstance(e, dict)
        }:
            raise HTTPException(status_code=404, detail={"error": "artifact not found"})
        obj = (data / "archive" / "objects" / sha[:2] / sha).resolve()
        # fail-closed layout check: the object must stay inside the data dir
        try:
            obj.relative_to(data.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail={"error": "artifact not found"}) from exc
        if not obj.is_file():
            raise HTTPException(status_code=404, detail={"error": "artifact not found"})
        digest = hashlib.sha256()
        try:
            with open(obj, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError as exc:
            raise HTTPException(
                status_code=404, detail={"error": "artifact not found"}
            ) from exc
        if digest.hexdigest() != sha:
            raise HTTPException(
                status_code=422, detail={"error": "artifact integrity verification failed"}
            )
        return Response(content=obj.read_bytes(), media_type="application/octet-stream")

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
