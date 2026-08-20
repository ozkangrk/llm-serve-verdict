"""Tests for the GitHub Actions security/release contract checks.

Covers (offline, no network):
- workflow YAML validity,
- pinned action refs only (no floating refs), closed verified-SHA registry,
- least-privilege permissions (exact maps; no wildcard/admin),
- SHA256SUMS + provenance attestation contract in release.yaml,
- release version consistency (pyproject / __version__ / CHANGELOG).

Positive cases use temporary fixture workflows so the real .github/workflows
files are only asserted on for their known-bad properties (which the test
suite's companion fix — pinning + permissions — resolves).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

spec = importlib.util.spec_from_file_location(
    "check_workflows", REPO_ROOT / "scripts" / "check_workflows.py"
)
assert spec is not None and spec.loader is not None
check_workflows = importlib.util.module_from_spec(spec)
sys.modules["check_workflows"] = check_workflows
spec.loader.exec_module(check_workflows)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

GOOD_WF = """\
name: Fixture CI
on: [push]
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262
      - name: echo
        run: echo hi
"""

GOOD_NAME = "fixture-ci.yaml"


@pytest.fixture()
def wf_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    # Register the default fixture workflow in the least-privilege policy
    # map so the permissions check has something exact to compare against.
    # Tests that check fail-closed behaviour for UNKNOWN names use their own
    # file names and are unaffected by this registration.
    monkeypatch.setitem(
        check_workflows.MAX_PERMISSIONS, GOOD_NAME, {"contents": "read"}
    )
    return d


@pytest.fixture()
def good_wf(wf_dir: Path) -> Path:
    p = wf_dir / GOOD_NAME
    p.write_text(GOOD_WF, encoding="utf-8")
    return p


def _project_scaffold(root: Path, version: str = "0.3.0") -> None:
    (root / "src" / "serving_verdict").mkdir(parents=True)
    (root / "src" / "serving_verdict" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "serving-verdict"\nversion = "0.3.0"\n', encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [0.3.0]\n", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True)


# ---------------------------------------------------------------------------
# YAML validity
# ---------------------------------------------------------------------------


def test_parse_rejects_invalid_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: [unclosed\n  : : :\n", encoding="utf-8")
    check = check_workflows.check_workflow_file(bad)
    assert not check.ok
    assert any("YAML parse failed" in e for e in check.errors)


def test_parse_rejects_non_mapping_document(tmp_path: Path) -> None:
    bad = tmp_path / "list.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    check = check_workflows.check_workflow_file(bad)
    assert not check.ok
    assert any("not a mapping" in e for e in check.errors)


def test_parse_accepts_valid_workflow(good_wf: Path) -> None:
    check = check_workflows.check_workflow_file(good_wf)
    assert check.ok, check.errors


# ---------------------------------------------------------------------------
# pinned refs
# ---------------------------------------------------------------------------

def _wf_with_uses(uses: str, name: str = "fixture-ci.yaml") -> str:
    return f"""name: Fixture
on: [push]
permissions:
  contents: read
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - uses: {uses}
"""


@pytest.mark.parametrize(
    "uses",
    [
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
        "github/codeql-action@ff2f1c621b7f889edc0d3c761ac2e6a3f8cdb0dd",
        "gitleaks/gitleaks-action@ff98106e4c7b2bc287b24eaf42907196329070c7",
    ],
)
def test_verified_sha_refs_accepted(wf_dir: Path, uses: str) -> None:
    p = wf_dir / GOOD_NAME
    p.write_text(_wf_with_uses(uses), encoding="utf-8")
    check = check_workflows.check_workflow_file(p)
    assert check.ok, check.errors


@pytest.mark.parametrize(
    "uses",
    [
        "actions/checkout@v4",  # floating minor
        "actions/checkout@main",  # branch ref
        "actions/checkout",  # no ref at all
        "actions/checkout@deadbeef",  # not 40 hex
        "actions/checkout@" + "0" * 40,  # 40-hex but not in verified registry
        "someorg/someaction@11d5960a326750d5838078e36cf38b85af677262",  # unverified repo
        "a/b/c@11d5960a326750d5838078e36cf38b85af677262",  # malformed repo
    ],
)
def test_unpinned_or_unverified_refs_rejected(wf_dir: Path, uses: str) -> None:
    p = wf_dir / GOOD_NAME
    p.write_text(_wf_with_uses(uses), encoding="utf-8")
    check = check_workflows.check_workflow_file(p)
    assert not check.ok
    assert any(
        "not pinned" in e or "not in the verified PINNED_REFS" in e
        or "malformed" in e or "unversioned" in e
        for e in check.errors
    ), check.errors


def test_local_composite_action_allowed(wf_dir: Path) -> None:
    p = wf_dir / GOOD_NAME
    p.write_text(_wf_with_uses("./.github/actions/setup"), encoding="utf-8")
    check = check_workflows.check_workflow_file(p)
    assert check.ok, check.errors


def test_registry_is_closed_and_verified() -> None:
    """Every entry is a 40-hex SHA mapped to a tag-like label."""
    for (org, repo), entries in check_workflows.PINNED_REFS.items():
        assert org and repo
        for sha, tag in entries.items():
            assert check_workflows._SHA_RE.match(sha)
            assert tag


# ---------------------------------------------------------------------------
# permissions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "permissions_block, reason",
    [
        ("", "missing top-level"),
        ("permissions:\n  contents: read\n  packages: write\n", "not allowed"),
        ("permissions:\n  contents: write\n", "exceeds the required"),
        ("permissions:\n  contents: '*'\n", "wildcard"),
        ("permissions:\n  admin: read\n", "forbidden"),
        ("permissions:\n  contents: read\n  id-token: write\n", "not allowed"),
    ],
)
def test_bad_permissions_rejected(wf_dir: Path, permissions_block: str, reason: str) -> None:
    p = wf_dir / GOOD_NAME
    p.write_text(f"name: F\non: [push]\n{permissions_block}\njobs:\n  j:\n    steps: []\n")
    check = check_workflows.check_workflow_file(p)
    assert not check.ok
    assert any(reason in e for e in check.errors), check.errors


def test_missing_policy_entry_fails_closed(wf_dir: Path) -> None:
    p = wf_dir / "unknown-ci.yaml"
    p.write_text(GOOD_WF, encoding="utf-8")
    check = check_workflows.check_workflow_file(p)
    assert not check.ok
    assert any("MAX_PERMISSIONS" in e for e in check.errors)


# ---------------------------------------------------------------------------
# release contract
# ---------------------------------------------------------------------------

RELEASE_MIN = """\
name: Release
on:
  push:
    tags:
      - "v*"
permissions:
  contents: write
  id-token: write
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262
      - name: Verify tag matches pyproject version
        run: true
      - name: Build
        run: uv build
      - name: Checksums
        run: |
          cd dist
          sha256sum *.whl *.tar.gz > SHA256SUMS
      - name: Attest
        uses: actions/attest-build-provenance@e4d4f7c39adfa4c260fb5c147f0622000aa14b99
        with:
          subject-path: |
            dist/*.whl
            dist/*.tar.gz
            dist/SHA256SUMS
      - name: Upload
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02
        with:
          name: dist-release
          path: dist/
"""


def test_release_contract_happy_path(tmp_path: Path) -> None:
    p = tmp_path / "release.yaml"
    p.write_text(RELEASE_MIN, encoding="utf-8")
    assert check_workflows.check_release_contract(p) == []


@pytest.mark.parametrize(
    "missing_fragment",
    ["sha256sum", "attest-build-provenance", "Verify tag matches pyproject version"],
)
def test_release_contract_missing_element_fails(tmp_path: Path, missing_fragment: str) -> None:
    text = RELEASE_MIN.replace(missing_fragment, "REMOVED")
    # For the checksum file name, strip the whole SHA256SUMS line too.
    if missing_fragment == "sha256sum":
        text = text.replace("SHA256SUMS", "CHECKSUMS")
    p = tmp_path / "release.yaml"
    p.write_text(text, encoding="utf-8")
    errors = check_workflows.check_release_contract(p)
    assert errors, "contract check must fail when an element is missing"


def test_release_contract_requires_sha256sums_upload(tmp_path: Path) -> None:
    text = RELEASE_MIN.replace("dist/SHA256SUMS\n", "")
    p = tmp_path / "release.yaml"
    p.write_text(text, encoding="utf-8")
    errors = check_workflows.check_release_contract(p)
    assert any("SHA256SUMS" in e for e in errors)


# ---------------------------------------------------------------------------
# version consistency
# ---------------------------------------------------------------------------


def test_version_consistency_passes(tmp_path: Path) -> None:
    _project_scaffold(tmp_path)
    assert check_workflows.check_version_consistency(tmp_path) == []


def test_version_consistency_mismatch_fails(tmp_path: Path) -> None:
    _project_scaffold(tmp_path)
    (tmp_path / "src" / "serving_verdict" / "__init__.py").write_text(
        '__version__ = "9.9.9"\n', encoding="utf-8"
    )
    errors = check_workflows.check_version_consistency(tmp_path)
    assert any("version mismatch" in e for e in errors)


def test_version_consistency_missing_changelog_entry_fails(tmp_path: Path) -> None:
    _project_scaffold(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    errors = check_workflows.check_version_consistency(tmp_path)
    assert any("CHANGELOG.md" in e for e in errors)


def test_version_consistency_missing_version_fails(tmp_path: Path) -> None:
    _project_scaffold(tmp_path)
    (tmp_path / "src" / "serving_verdict" / "__init__.py").write_text(
        "# no version\n", encoding="utf-8"
    )
    errors = check_workflows.check_version_consistency(tmp_path)
    assert any("__version__ not found" in e for e in errors)


# ---------------------------------------------------------------------------
# real-repo audit (the hardening itself: after this change these must pass)
# ---------------------------------------------------------------------------


def test_real_workflows_all_pass() -> None:
    results = check_workflows.audit_all(REPO_ROOT)
    failures = [r for r in results if not r.ok]
    detail = "; ".join(f"{r.name}: {r.errors}" for r in failures)
    assert not failures, detail


def test_real_workflows_expected_set() -> None:
    names = {r.name for r in check_workflows.audit_all(REPO_ROOT)}
    assert {
        "ci.yaml",
        "codeql.yaml",
        "dependency-audit.yaml",
        "secret-scan.yaml",
        "release.yaml",
        "publish-pypi.yaml",
    } <= names
    assert "version-consistency" in names


def test_yaml_roundtrip_stable_for_real_workflows() -> None:
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml")):
        doc = check_workflows.parse_workflow(path)
        assert isinstance(doc, dict)
        assert "jobs" in doc


def test_secret_scan_has_full_history_and_pr_token() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "secret-scan.yaml").read_text(
        encoding="utf-8"
    )
    assert "fetch-depth: 0" in text
    assert "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in text
