"""Content-addressed artifact store (v0.2).

Layout (under a store root ``DIR``)::

    DIR/
      objects/<first-two-hex>/<full-64-hex>

The store is fail-closed:
- sources must be regular, non-symlink files (symlinks are rejected
  outright — including ones whose target stays inside the base dir);
- when a ``base_dir`` is given, the resolved source must stay inside the
  resolved base dir (symlink escape is rejected);
- files over 20 MiB are rejected (same bound as the evidence loader);
- the copy is streamed to a temp file in the destination directory, re-
  hashed after the copy, and only then renamed into place — a copy that
  does not re-hash to its digest aborts with :class:`ArchiveError` and
  leaves no partial object behind.

Nothing is executed; content is treated as opaque bytes.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from serving_verdict.errors import ArchiveError
from serving_verdict.evidence import MAX_FILE_SIZE_BYTES

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ManifestEntry:
    """One content-addressed object in the artifacts manifest."""

    sha256: str
    size_bytes: int
    path: str  # "objects/<2-hex>/<64-hex>"


class ArchiveStore:
    """A content-addressed store rooted at ``root``.

    ``put`` copies one verified source file into the store and returns its
    SHA-256. ``object_path`` maps a digest to its store path and validates
    that the resulting path cannot escape the store root.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def object_path(self, sha256: str) -> Path:
        """Return the store path for ``sha256``.

        Raises ArchiveError for non-64-hex digests or any path that would
        escape the store root (fail-closed).
        """
        if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
            raise ArchiveError(f"invalid artifact digest (expected 64 lowercase hex): {sha256!r}")
        candidate = (self.root / "objects" / sha256[:2] / sha256).resolve()
        root = self.root.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ArchiveError(f"store object path escapes the store root: {sha256!r}") from exc
        return self.root / "objects" / sha256[:2] / sha256

    def put(self, source: Path, base_dir: Path | None = None) -> ManifestEntry:
        """Copy ``source`` into the store, verifying the copy by re-hash.

        ``base_dir`` (when given) is the directory the source must stay
        inside after resolution (the canonical source root). Raises
        ArchiveError on any safety violation; no partial object is left.
        """
        src = Path(source)
        if not src.exists():
            raise ArchiveError(f"artifact to archive not found: {src}")
        st = src.lstat()
        if stat.S_ISLNK(st.st_mode):
            raise ArchiveError(f"symlinked artifacts are not allowed in the archive: {src}")
        if not stat.S_ISREG(st.st_mode):
            raise ArchiveError(f"special files are not allowed in the archive: {src}")
        if st.st_size > MAX_FILE_SIZE_BYTES:
            raise ArchiveError(f"artifact exceeds {MAX_FILE_SIZE_BYTES} byte limit: {src}")
        resolved = src.resolve()
        if base_dir is not None:
            base = Path(base_dir).resolve()
            try:
                resolved.relative_to(base)
            except ValueError as exc:
                raise ArchiveError(f"artifact escapes the source root: {src}") from exc

        dest = self.object_path(_hash_stream(resolved).hexdigest())
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with open(resolved, "rb") as fin, open(tmp, "wb") as fout:
                while True:
                    chunk = fin.read(1024 * 1024)
                    if not chunk:
                        break
                    fout.write(chunk)
            # copy-after hash verify: the stored bytes must re-hash to the
            # digest derived from the source.
            actual = _hash_stream(tmp).hexdigest()
            if actual != dest.name:
                tmp.unlink(missing_ok=True)
                raise ArchiveError(f"archive copy failed verification for {src.name}")
            os.replace(tmp, dest)
        except ArchiveError:
            raise
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise ArchiveError(f"cannot store artifact {src.name}: {exc}") from exc
        return ManifestEntry(sha256=dest.name, size_bytes=st.st_size, path=f"objects/{dest.name[:2]}/{dest.name}")


def _hash_stream(path: Path) -> hashlib._Hash:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
