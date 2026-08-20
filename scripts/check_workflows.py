"""Security checks for GitHub Actions workflows and release contracts.

Offline-only (no network): validates that every workflow in
``.github/workflows`` is well-formed YAML, declares an explicit least
``permissions`` map, and references third-party actions only by immutable
40-hex commit SHAs that were verified against the official upstream
repositories (see ``PINNED_REFS`` for the verification record).

Also validates the release contract: the release workflow must produce
``SHA256SUMS`` for the wheel/sdist, upload it with the artifacts, attach
it to the GitHub release, and attest build provenance via GitHub's
``actions/attest-build-provenance`` (GitHub artifact attestation — not
Sigstore/cosign, and no reproducible bit-identical build claim).

And release version consistency: ``pyproject.toml`` version, runtime
``__version__``, and the CHANGELOG entry must all agree.

Run directly: ``python scripts/check_workflows.py`` (exit code 0 = clean).
"""
from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# --------------------------------------------------------------------------
# Pinned-action verification record.
#
# Every (org, repo) -> {commit_sha: tag} entry below was verified on
# 2026-08-20 against the official GitHub repository via
#   1. GET /repos/<org>/<repo>/git/refs/tags/<tag>  (tag -> commit), and
#   2. `git ls-remote https://github.com/<org>/<repo>.git refs/tags/<tag>`
# The registry is CLOSED: a workflow may only pin a SHA that appears here,
# so an unverified SHA fails the check even if it is well-formed.
# --------------------------------------------------------------------------
PINNED_REFS: dict[tuple[str, str], dict[str, str]] = {
    ("actions", "checkout"): {
        "11d5960a326750d5838078e36cf38b85af677262": "v4.4.0",
    },
    ("actions", "upload-artifact"): {
        "ea165f8d65b6e75b540449e92b4886f43607fa02": "v4.6.2",
    },
    ("actions", "download-artifact"): {
        "d3f86a106a0bac45b974a628896c90dbdf5c8093": "v4.3.0",
    },
    ("actions", "attest-build-provenance"): {
        "e4d4f7c39adfa4c260fb5c147f0622000aa14b99": "v4.0.0",
    },
    ("astral-sh", "setup-uv"): {
        "f94ec6bedd8674c4426838e6b50417d36b6ab231": "v5.3.1",
    },
    ("github", "codeql-action"): {
        "ff2f1c621b7f889edc0d3c761ac2e6a3f8cdb0dd": "v4.37.7",
    },
    ("gitleaks", "gitleaks-action"): {
        "ff98106e4c7b2bc287b24eaf42907196329070c7": "v2.3.9",
    },
    ("softprops", "action-gh-release"): {
        "aec2ec56f94eb8180ceec724245f64ef008b89f5": "v2.4.0",
    },
    ("pypa", "gh-action-pypi-publish"): {
        "dc37677b2e1c63e2034f94d8a5b11f265b73ba33": "v1.14.2",
    },
}

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Per-workflow maximum allowed top-level permission map. Anything outside
# this map (an extra scope, a higher access level, or the ``*`` wildcard)
# is a least-privilege violation. Unknown workflow files fail.
MAX_PERMISSIONS: dict[str, dict[str, str]] = {
    "ci.yaml": {"contents": "read"},
    "codeql.yaml": {"contents": "read", "security-events": "write"},
    "dependency-audit.yaml": {"contents": "read"},
    "secret-scan.yaml": {"contents": "read"},
    "release.yaml": {"contents": "write", "id-token": "write"},
    "publish-pypi.yaml": {"contents": "read", "id-token": "write"},
}

_FORBIDDEN_SCOPES = {"admin"}


@dataclass(frozen=True)
class WorkflowCheck:
    """Result of checking one workflow file."""

    name: str
    errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_workflow(path: Path) -> Any:
    """Parse a workflow file with PyYAML (safe load, no arbitrary tags)."""
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise ValueError(f"{path.name}: top-level YAML document is not a mapping")
    return doc


def _uses_refs(doc: Any) -> list[str]:
    """Collect all ``uses:`` references from every job step."""
    refs: list[str] = []
    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, dict):
        return refs
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("uses"), str):
                refs.append(step["uses"])
    return refs


def _check_pinned_refs(name: str, doc: Any) -> list[str]:
    errors: list[str] = []
    for ref in _uses_refs(doc):
        if ref.startswith("./"):
            continue  # local composite action
        if "@" not in ref:
            errors.append(f"{name}: unversioned action reference '{ref}'")
            continue
        repo_part, pin = ref.rsplit("@", 1)
        parts = repo_part.split("/")
        # Composite actions may be referenced as org/repo/subpath@sha —
        # the registry keys off the first two components.
        if len(parts) < 2 or not all(parts):
            errors.append(f"{name}: malformed action reference '{ref}'")
            continue
        org, repo = parts[0], parts[1]
        if not _SHA_RE.match(pin):
            errors.append(
                f"{name}: '{ref}' is not pinned to an immutable 40-hex commit SHA "
                f"(floating refs like @v4 or @main are not allowed)"
            )
            continue
        known = PINNED_REFS.get((org, repo))
        if known is None or pin not in known:
            errors.append(
                f"{name}: '{ref}' is not in the verified PINNED_REFS registry "
                f"(only SHAs verified against the official repository may be used)"
            )
    return errors


def _check_permissions(name: str, doc: Any) -> list[str]:
    allowed = MAX_PERMISSIONS.get(name)
    if allowed is None:
        return [
            f"{name}: not present in the MAX_PERMISSIONS policy map — add an "
            f"explicit least-privilege entry (or remove the workflow)"
        ]
    perms = doc.get("permissions")
    if perms is None:
        return [f"{name}: missing top-level 'permissions:' map (implicit default "
                f"is not least-privilege)"]
    if not isinstance(perms, dict) or not perms:
        return [f"{name}: 'permissions:' must be a non-empty mapping"]
    errors: list[str] = []
    for scope, level in perms.items():
        if scope in _FORBIDDEN_SCOPES:
            errors.append(f"{name}: forbidden permission scope '{scope}'")
            continue
        if level == "*" or not isinstance(level, str):
            errors.append(
                f"{name}: permission '{scope}: {level}' uses a wildcard or "
                f"is not an access level string"
            )
            continue
        max_level = allowed.get(scope)
        if max_level is None:
            errors.append(
                f"{name}: permission scope '{scope}' is not allowed "
                f"(max map: {sorted(allowed)})"
            )
            continue
        if level != max_level:
            errors.append(
                f"{name}: permission '{scope}: {level}' exceeds the required "
                f"'{scope}: {max_level}'"
            )
    for scope in allowed:
        if scope not in perms:
            errors.append(
                f"{name}: missing required permission scope '{scope}' "
                f"(the map must state the exact least set)"
            )
    return errors


def check_workflow_file(path: Path) -> WorkflowCheck:
    """Parse + pinned-ref + least-privilege checks for one workflow."""
    name = path.name
    try:
        doc = parse_workflow(path)
    except (yaml.YAMLError, ValueError, OSError) as exc:
        return WorkflowCheck(name, [f"{name}: YAML parse failed: {exc}"])
    errors = _check_pinned_refs(name, doc) + _check_permissions(name, doc)
    return WorkflowCheck(name, errors)


def check_release_contract(path: Path) -> list[str]:
    """Checksum + provenance contract for the release workflow (text-level)."""
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    if "SHA256SUMS" not in text:
        errors.append("release.yaml: must generate a SHA256SUMS file")
    if "sha256sum" not in text:
        errors.append("release.yaml: must compute checksums with sha256sum")
    if "dist/SHA256SUMS" not in text:
        errors.append("release.yaml: SHA256SUMS must be attached to the GitHub release")
    if "attest-build-provenance" not in text:
        errors.append(
            "release.yaml: must attest build provenance with "
            "actions/attest-build-provenance (GitHub artifact attestation)"
        )
    if "Verify tag matches pyproject version" not in text:
        errors.append(
            "release.yaml: must fail the release when the tag does not match "
            "the pyproject.toml version"
        )
    return errors


_VERSION_RE = re.compile(
    r'^\s*__version__\s*=\s*["\']([0-9][0-9A-Za-z.\-+]*)["\']', re.MULTILINE
)


def check_version_consistency(root: Path) -> list[str]:
    """pyproject.toml version == runtime __version__ == CHANGELOG entry."""
    errors: list[str] = []
    pyproject = root / "pyproject.toml"
    with pyproject.open("rb") as fh:
        version = str(tomllib.load(fh)["project"]["version"])
    init = root / "src" / "serving_verdict" / "__init__.py"
    match = _VERSION_RE.search(init.read_text(encoding="utf-8"))
    if match is None:
        errors.append("__init__.py: __version__ not found")
    elif match.group(1) != version:
        errors.append(
            f"version mismatch: pyproject.toml={version} "
            f"__init__.__version__={match.group(1)}"
        )
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{version}]" not in changelog:
        errors.append(
            f"CHANGELOG.md has no '## [{version}]' entry for pyproject version "
            f"{version}"
        )
    return errors


def audit_all(root: Path) -> list[WorkflowCheck]:
    """Check every workflow file under .github/workflows (plus contracts)."""
    wf_dir = root / ".github" / "workflows"
    results: list[WorkflowCheck] = []
    for path in sorted(wf_dir.glob("*.y*ml")):
        results.append(check_workflow_file(path))
    release = wf_dir / "release.yaml"
    if release.exists():
        contract_errors = check_release_contract(release)
        if contract_errors:
            idx = next((i for i, r in enumerate(results) if r.name == "release.yaml"), None)
            if idx is not None:
                results[idx] = WorkflowCheck("release.yaml", results[idx].errors + contract_errors)
            else:
                results.append(WorkflowCheck("release.yaml", contract_errors))
    version_errors = check_version_consistency(root)
    results.append(WorkflowCheck("version-consistency", version_errors))
    return results


def main(argv: list[str] | None = None) -> int:
    root = Path(".")
    if argv:
        root = Path(argv[0])
    results = audit_all(root)
    failures = 0
    for check in results:
        if check.ok:
            print(f"ok   {check.name}")
        else:
            failures += 1
            print(f"FAIL {check.name}")
            for err in check.errors:
                print(f"     - {err}")
    if failures:
        print(f"\n{failures} workflow check(s) failed")
        return 1
    print("\nall workflow checks passed (offline; no network used)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
