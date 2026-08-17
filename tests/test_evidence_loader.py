"""Path-safe evidence loader tests: traversal, symlinks, special files, size (RED)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from serving_verdict.evidence import EvidenceError, EvidenceLoader
from tests.helpers import sha256_file


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "benchmarks" / "results").mkdir(parents=True)
    return tmp_path


def write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_loads_regular_file_with_sha(root: Path) -> None:
    p = write(root, "a/b.json", '{"schema_version": "x"}')
    loader = EvidenceLoader(root)
    blob = loader.load_artifact("a/b.json")
    assert blob.sha256 == sha256_file(p)
    assert blob.source_root == str(loader.canonical_root)
    assert blob.relative_path == "a/b.json"
    assert json.loads(blob.text)


def test_rejects_dotdot_traversal(root: Path) -> None:
    loader = EvidenceLoader(root)
    with pytest.raises(EvidenceError):
        loader.load_artifact("../outside.json")
    with pytest.raises(EvidenceError):
        loader.load_artifact("a/../../etc/passwd")


def test_rejects_absolute_child_path(root: Path) -> None:
    loader = EvidenceLoader(root)
    with pytest.raises(EvidenceError):
        loader.load_artifact("/etc/passwd")
    with pytest.raises(EvidenceError):
        loader.load_artifact("a//etc/passwd")


def test_rejects_missing_file(root: Path) -> None:
    loader = EvidenceLoader(root)
    with pytest.raises(EvidenceError):
        loader.load_artifact("nope.json")


def test_rejects_symlink_escape(root: Path, tmp_path: Path) -> None:
    outside = tmp_path.parents[0] / f"{tmp_path.name}-outside.json"
    outside.write_text("secret", encoding="utf-8")
    try:
        link = root / "link.json"
        os.symlink(outside, link)
        loader = EvidenceLoader(root)
        with pytest.raises(EvidenceError):
            loader.load_artifact("link.json")
    finally:
        outside.unlink(missing_ok=True)


def test_rejects_symlinked_directory_escape(root: Path, tmp_path: Path) -> None:
    outside_dir = tmp_path.parents[0] / f"{tmp_path.name}-outside_dir"
    outside_dir.mkdir()
    try:
        (outside_dir / "data.json").write_text("secret", encoding="utf-8")
        link = root / "odir"
        os.symlink(outside_dir, link)
        loader = EvidenceLoader(root)
        with pytest.raises(EvidenceError):
            loader.load_artifact("odir/data.json")
    finally:
        (outside_dir / "data.json").unlink(missing_ok=True)
        outside_dir.rmdir()


def test_accepts_symlink_staying_inside_root(root: Path) -> None:
    target = write(root, "real_target.json", "inside")
    link = root / "alias.json"
    os.symlink(target, link)
    loader = EvidenceLoader(root)
    blob = loader.load_artifact("alias.json")
    assert blob.sha256 == sha256_file(target)


def test_rejects_fifo_special_file(root: Path) -> None:
    p = root / "fifo"
    os.mkfifo(p)
    loader = EvidenceLoader(root)
    with pytest.raises(EvidenceError):
        loader.load_artifact("fifo")


def test_rejects_file_over_20_mib(root: Path) -> None:
    p = root / "big.bin"
    with open(p, "wb") as fh:
        fh.write(b"0" * (20 * 1024 * 1024 + 1))
    loader = EvidenceLoader(root)
    with pytest.raises(EvidenceError):
        loader.load_artifact("big.bin")


def test_accepts_file_at_20_mib(root: Path) -> None:
    p = root / "exact.bin"
    with open(p, "wb") as fh:
        fh.write(b"0" * (20 * 1024 * 1024))
    loader = EvidenceLoader(root)
    blob = loader.load_artifact("exact.bin")
    assert blob.size_bytes == 20 * 1024 * 1024


def test_root_itself_may_be_symlink_to_real_dir(tmp_path: Path) -> None:
    real = tmp_path / "realroot"
    real.mkdir()
    (real / "x.json").write_text("ok", encoding="utf-8")
    link_root = tmp_path / "linkroot"
    os.symlink(real, link_root)
    loader = EvidenceLoader(link_root)
    blob = loader.load_artifact("x.json")
    assert blob.text == "ok"


def test_nonexistent_root_rejected(tmp_path: Path) -> None:
    with pytest.raises(EvidenceError):
        EvidenceLoader(tmp_path / "does-not-exist")


def test_empty_relative_path_rejected(root: Path) -> None:
    loader = EvidenceLoader(root)
    with pytest.raises(EvidenceError):
        loader.load_artifact("")


def test_verify_expected_sha(root: Path) -> None:
    p = write(root, "m.json", '{"k": 1}')
    good = sha256_file(p)
    loader = EvidenceLoader(root)
    blob = loader.load_artifact("m.json", expected_sha256=good)
    assert blob.sha256 == good
    with pytest.raises(EvidenceError):
        loader.load_artifact("m.json", expected_sha256="0" * 64)
