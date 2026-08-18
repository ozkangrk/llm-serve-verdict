"""Append-only SQLite trial registry (v0.2).

Design contract:
- stdlib :mod:`sqlite3` only; the database file lives at
  ``<data_dir>/trial_store.sqlite3``.
- Schema migration uses ``PRAGMA user_version`` (v1 at first use).
- ALL SQL is parameterized; identifiers are module constants.
- The bundle file is the SOURCE OF TRUTH: the registry stores only identity,
  digest, verdict, reason codes, and the bundle-file name. Registering
  verifies the bundle (``verify_bundle``) and raises IntegrityError on a
  tampered bundle.
- History is append-only: ``register_bundle`` never updates or deletes
  event rows. Duplicate bundle digests are idempotent (``action='duplicate'``,
  no new row); a NEW bundle for the same case id appends a new event.
- ``reindex`` reconciles current state with the data dir: valid bundles are
  (re)registered (idempotently), bundles that fail verify are marked
  ``invalid`` in the state table, and vanished bundles are marked ``missing``.
  Event rows are always preserved.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from serving_verdict.engine import BUNDLE_SCHEMA_VERSION, load_bundle, verify_bundle
from serving_verdict.errors import IntegrityError

STORE_FILENAME = "trial_store.sqlite3"
SCHEMA_VERSION = 1

_MIGRATION_V1 = """
CREATE TABLE IF NOT EXISTS trials (
    case_id       TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    verdict       TEXT,
    reason_codes  TEXT NOT NULL,
    bundle_digest TEXT,
    bundle_file   TEXT
);
CREATE TABLE IF NOT EXISTS trial_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id      TEXT NOT NULL,
    status       TEXT NOT NULL,
    verdict      TEXT,
    reason_codes TEXT NOT NULL,
    bundle_digest TEXT,
    bundle_file  TEXT,
    event        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_case ON trial_events (case_id, seq);
"""


@dataclass(frozen=True)
class TrialStore:
    """A trial registry rooted at a data directory.

    Opens (and migrates, on first use) the SQLite file lazily; all methods
    open short-lived connections so the store is safe to instantiate per
    command and to share across processes (WAL is not required for the
    CLI's one-writer-at-a-time usage, but is enabled anyway for safety).
    """

    data_dir: Path
    initialize: bool = True

    def __post_init__(self) -> None:
        if self.initialize:
            conn = self._connect()
            conn.close()

    # ---------------------------------------------------------------- helpers

    @property
    def db_path(self) -> Path:
        return self.data_dir / STORE_FILENAME

    def _connect(self) -> sqlite3.Connection:
        if not self.data_dir.is_dir():
            from serving_verdict.errors import UsageError

            raise UsageError(f"data dir not found: {self.data_dir}")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        self._migrate(conn)
        return conn

    def _connect_readonly(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            from serving_verdict.errors import UsageError

            raise UsageError(f"trial store not found: {self.db_path}")
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            from serving_verdict.errors import UsageError

            raise UsageError(
                f"trial store schema v{version} is newer than this binary (v{SCHEMA_VERSION})"
            )
        if version < SCHEMA_VERSION:
            conn.executescript(_MIGRATION_V1)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

    # ---------------------------------------------------------------- events

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "seq": row["seq"],
            "event_id": row["seq"],
            "case_id": row["case_id"],
            "status": row["status"],
            "verdict": row["verdict"],
            "reason_codes": json.loads(row["reason_codes"]),
            "bundle_digest": row["bundle_digest"],
            "bundle_file": row["bundle_file"],
            "event": row["event"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _row_to_trial(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "case_id": row["case_id"],
            "status": row["status"],
            "verdict": row["verdict"],
            "reason_codes": json.loads(row["reason_codes"]),
            "bundle_digest": row["bundle_digest"],
            "bundle_file": row["bundle_file"],
        }

    def list_events(self, case_id: str | None = None) -> list[dict[str, Any]]:
        """Append-only event history, oldest first (optionally per case)."""
        conn = self._connect()
        try:
            if case_id is None:
                rows = conn.execute(
                    "SELECT * FROM trial_events ORDER BY seq ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trial_events WHERE case_id = ? ORDER BY seq ASC",
                    (case_id,),
                ).fetchall()
            return [self._row_to_event(r) for r in rows]
        finally:
            conn.close()

    def list_trials(self) -> list[dict[str, Any]]:
        """Current per-case state (the registry's own view, no disk re-check)."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM trials ORDER BY case_id ASC").fetchall()
            return [self._row_to_trial(r) for r in rows]
        finally:
            conn.close()

    def get_trial(self, case_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM trials WHERE case_id = ?", (case_id,)
            ).fetchone()
            return self._row_to_trial(row) if row is not None else None
        finally:
            conn.close()

    def list_trials_readonly(self) -> list[dict[str, Any]]:
        conn = self._connect_readonly()
        try:
            rows = conn.execute("SELECT * FROM trials ORDER BY case_id ASC").fetchall()
            return [self._row_to_trial(row) for row in rows]
        finally:
            conn.close()

    def get_trial_readonly(self, case_id: str) -> dict[str, Any] | None:
        conn = self._connect_readonly()
        try:
            row = conn.execute(
                "SELECT * FROM trials WHERE case_id = ?", (case_id,)
            ).fetchone()
            return self._row_to_trial(row) if row is not None else None
        finally:
            conn.close()

    def list_events_readonly(self, case_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._connect_readonly()
        try:
            if case_id is None:
                rows = conn.execute(
                    "SELECT * FROM trial_events ORDER BY seq ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trial_events WHERE case_id = ? ORDER BY seq ASC",
                    (case_id,),
                ).fetchall()
            return [self._row_to_event(row) for row in rows]
        finally:
            conn.close()

    def event_counts_readonly(self) -> dict[str, int]:
        conn = self._connect_readonly()
        try:
            rows = conn.execute(
                "SELECT case_id, COUNT(*) AS n FROM trial_events GROUP BY case_id"
            ).fetchall()
            return {str(row["case_id"]): int(row["n"]) for row in rows}
        finally:
            conn.close()

    def status_report_readonly(self) -> dict[str, Any]:
        conn = self._connect_readonly()
        try:
            trials = int(conn.execute("SELECT COUNT(*) AS n FROM trials").fetchone()["n"])
            events = int(conn.execute("SELECT COUNT(*) AS n FROM trial_events").fetchone()["n"])
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM trials GROUP BY status"
            ).fetchall()
            return {
                "trials": trials,
                "events": events,
                "by_status": {str(row["status"]): int(row["n"]) for row in rows},
            }
        finally:
            conn.close()

    def user_version_readonly(self) -> int:
        conn = self._connect_readonly()
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()

    # ---------------------------------------------------------------- write

    def register_bundle(self, bundle_path: str | Path) -> dict[str, Any]:
        """Register one verified bundle as a trial event.

        Returns an event dict with an added ``action`` key:
        ``"registered"`` or ``"duplicate"`` (same digest already recorded).
        Raises IntegrityError for tampered/invalid bundles.
        """
        path = Path(bundle_path)
        bundle = load_bundle(path)
        verify_bundle(bundle)  # raises IntegrityError -> exit 4
        if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
            raise IntegrityError(
                f"unsupported bundle schema_version: {bundle.get('schema_version')!r}"
            )
        case_id: str = bundle["case_id"]
        digest: str = bundle["bundle_digest"]

        conn = self._connect()
        try:
            existing = conn.execute(
                "SELECT seq FROM trial_events WHERE bundle_digest = ?", (digest,)
            ).fetchone()
            if existing is not None:
                row = conn.execute(
                    "SELECT * FROM trial_events WHERE seq = ?", (existing["seq"],)
                ).fetchone()
                event = self._row_to_event(row)
                event["action"] = "duplicate"
                return event
            _register_row(conn, bundle, path.name)
            row = conn.execute(
                "SELECT * FROM trial_events WHERE case_id = ? ORDER BY seq DESC LIMIT 1",
                (case_id,),
            ).fetchone()
            event = self._row_to_event(row)
            event["action"] = "registered"
            return event
        finally:
            conn.close()

    def reindex(self) -> dict[str, Any]:
        """Rebuild current state from the data dir (idempotent).

        The bundle files remain the source of truth: every ``*.json`` bundle
        in the data dir is loaded, verified and (re)registered by digest —
        duplicates are no-ops. Bundles that fail verify are marked
        ``invalid``; previously tracked bundles whose file is gone or no
        longer valid are marked ``missing``. History rows are never deleted
        or rewritten.
        """
        if not self.data_dir.is_dir():
            from serving_verdict.errors import UsageError

            raise UsageError(f"data dir not found: {self.data_dir}")
        conn = self._connect()
        try:
            indexed = 0
            invalid = 0
            seen: set[str] = set()
            for path in sorted(self.data_dir.glob("*.json")):
                try:
                    bundle = load_bundle(path)
                except UsageError:
                    continue  # not a parseable bundle file (same rule as `list`)
                if not isinstance(bundle, dict) or bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
                    continue
                try:
                    verify_bundle(bundle)
                except IntegrityError:
                    if isinstance(bundle.get("case_id"), str) and bundle.get("case_id"):
                        case_id: str = bundle["case_id"]
                        conn.execute(
                            "INSERT INTO trials "
                            "(case_id, status, verdict, reason_codes, bundle_digest, bundle_file) "
                            "VALUES (?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(case_id) DO UPDATE SET "
                            "status = excluded.status, verdict = excluded.verdict, "
                            "reason_codes = excluded.reason_codes, "
                            "bundle_digest = excluded.bundle_digest, "
                            "bundle_file = excluded.bundle_file",
                            (case_id, "invalid", None, json.dumps([], ensure_ascii=True), None, path.name),
                        )
                        invalid += 1
                        seen.add(case_id)
                    continue
                _register_row(conn, bundle, path.name)
                indexed += 1
                seen.add(str(bundle["case_id"]))
            missing = 0
            rows = conn.execute(
                "SELECT case_id FROM trials WHERE status IN ('valid', 'invalid')"
            ).fetchall()
            for row in rows:
                if row["case_id"] not in seen:
                    conn.execute(
                        "UPDATE trials SET status = ? WHERE case_id = ?",
                        ("missing", row["case_id"]),
                    )
                    missing += 1
            conn.commit()
            return {"indexed": indexed, "invalid": invalid, "missing": missing}
        finally:
            conn.close()

    def status_report(self) -> dict[str, Any]:
        """Counts used by /ready and the reindex CLI payload."""
        conn = self._connect()
        try:
            trials = conn.execute("SELECT COUNT(*) AS n FROM trials").fetchone()["n"]
            events = conn.execute("SELECT COUNT(*) AS n FROM trial_events").fetchone()["n"]
            by_status = {
                r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM trials GROUP BY status")
            }
            return {"trials": trials, "events": events, "by_status": by_status}
        finally:
            conn.close()


def bundle_has_identity(bundle: dict[str, Any]) -> bool:
    """A bundle has enough identity (case_id) to be tracked even when invalid."""
    return isinstance(bundle.get("case_id"), str) and bool(bundle.get("case_id"))


def _register_row(conn: sqlite3.Connection, bundle: dict[str, Any], bundle_file: str) -> int:
    """Idempotent per-digest registration of a verified bundle (shared row logic).

    Returns the trial_events row id of the event for this digest (the newly
    inserted one, or the existing one when the digest was already recorded).
    """
    from datetime import UTC, datetime

    digest: str = bundle["bundle_digest"]
    existing = conn.execute(
        "SELECT seq FROM trial_events WHERE bundle_digest = ?", (digest,)
    ).fetchone()
    now = datetime.now(UTC).isoformat()
    if existing is not None:
        return int(existing["seq"])
    cur = conn.execute(
        "INSERT INTO trial_events "
        "(case_id, status, verdict, reason_codes, bundle_digest, bundle_file, event, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            bundle["case_id"],
            "registered",
            bundle["verdict"],
            json.dumps(list(bundle["reason_codes"]), ensure_ascii=True),
            digest,
            bundle_file,
            "bundle_registered",
            now,
        ),
    )
    conn.execute(
        "INSERT INTO trials "
        "(case_id, status, verdict, reason_codes, bundle_digest, bundle_file) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(case_id) DO UPDATE SET "
        "status = excluded.status, verdict = excluded.verdict, "
        "reason_codes = excluded.reason_codes, bundle_digest = excluded.bundle_digest, "
        "bundle_file = excluded.bundle_file",
        (
            bundle["case_id"],
            "valid",
            bundle["verdict"],
            json.dumps(list(bundle["reason_codes"]), ensure_ascii=True),
            digest,
            bundle_file,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)
