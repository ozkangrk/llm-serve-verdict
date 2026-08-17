"""Path-safe evidence loader.

- The source root is canonicalized once at construction.
- Child paths must be relative; absolute paths and `..` segments are rejected.
- Symlink escape outside the canonical root is rejected (per-path and final).
- Special files (FIFO, socket, device) are rejected.
- Files over 20 MiB are rejected.
- Content is read as bytes/text only; nothing is executed.
"""
from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path

from serving_verdict.errors import EvidenceError

MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MiB


@dataclass(frozen=True)
class EvidenceBlob:
    """A loaded evidence file with provenance."""

    source_root: str
    relative_path: str
    resolved_path: str
    sha256: str
    size_bytes: int
    text: str


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


class EvidenceLoader:
    def __init__(self, source_root: str | Path) -> None:
        root = Path(source_root)
        if not root.exists():
            raise EvidenceError(f"source root does not exist: {root}")
        canonical = root.resolve()
        if not canonical.is_dir():
            raise EvidenceError(f"source root is not a directory: {canonical}")
        self.canonical_root: Path = canonical

    def _resolve_child(self, relative_path: str) -> Path:
        if not relative_path:
            raise EvidenceError("empty artifact path")
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise EvidenceError(f"absolute artifact paths are not allowed: {relative_path}")
        if ".." in candidate.parts:
            raise EvidenceError(f"path traversal is not allowed: {relative_path}")
        resolved = (self.canonical_root / candidate).resolve()
        if not _is_within(resolved, self.canonical_root):
            raise EvidenceError(f"symlink escape outside source root: {relative_path}")
        return resolved

    def _validate_file(self, resolved: Path, relative_path: str) -> None:
        if not resolved.exists():
            raise EvidenceError(f"artifact not found: {relative_path}")
        st = resolved.lstat()
        if not stat.S_ISREG(st.st_mode):
            raise EvidenceError(f"special files are not allowed: {relative_path}")
        if st.st_size > MAX_FILE_SIZE_BYTES:
            raise EvidenceError(
                f"artifact exceeds {MAX_FILE_SIZE_BYTES} byte limit: {relative_path}"
            )

    def load_artifact(
        self, relative_path: str, expected_sha256: str | None = None
    ) -> EvidenceBlob:
        resolved = self._resolve_child(relative_path)
        self._validate_file(resolved, relative_path)
        digest = hashlib.sha256()
        size = 0
        try:
            with open(resolved, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            raise EvidenceError(f"cannot read artifact {relative_path}: {exc}") from exc
        sha = digest.hexdigest()
        if expected_sha256 is not None and sha != expected_sha256.lower():
            raise EvidenceError(
                f"sha256 mismatch for {relative_path}: expected {expected_sha256}, "
                f"got {sha}"
            )
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise EvidenceError(f"artifact is not valid UTF-8: {relative_path}") from exc
        return EvidenceBlob(
            source_root=str(self.canonical_root),
            relative_path=relative_path,
            resolved_path=str(resolved),
            sha256=sha,
            size_bytes=size,
            text=text,
        )
