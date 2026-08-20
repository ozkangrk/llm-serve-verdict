from __future__ import annotations

import tomllib
from pathlib import Path

from starlette.testclient import TestClient

from serving_verdict import __version__
from serving_verdict.bundle_v04 import BUNDLE_SCHEMA_VERSION_V04
from serving_verdict.server import ONLY_BIND_HOST, create_app

ROOT = Path(__file__).resolve().parents[1]


def test_distribution_and_cli_name_migration_contract() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    assert project["name"] == "llm-serve-verdict"
    assert project["scripts"]["llm-serve-verdict"] == "serving_verdict.cli:main"
    assert project["scripts"]["serving-verdict"] == "serving_verdict.cli:main"
    assert project["urls"]["Repository"].endswith("ozkangrk/llm-serve-verdict")

    # Existing artifacts remain verifiable after the product/distribution rename.
    assert BUNDLE_SCHEMA_VERSION_V04 == "serving-verdict.bundle.v0.4"


def test_readme_explains_actor_problem_workflow_and_three_decisions() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.startswith('<div align="center">\n\n# LLM ServeVerdict')
    for phrase in (
        "current and candidate vLLM",
        "Ayşe is an inference engineer",
        "tool calls become malformed",
        "confidence interval",
        "`PROMOTE`",
        "`REJECT`",
        "`INCONCLUSIVE`",
        "does **not** start or mutate production servers",
    ):
        assert phrase in readme


def test_user_facing_brand_has_no_old_repository_or_display_name() -> None:
    files = (
        ROOT / "README.md",
        ROOT / "SECURITY.md",
        ROOT / "RELEASE.md",
        ROOT / "src" / "serving_verdict" / "web" / "index.html",
        ROOT / "docs" / "architecture-v0.5.svg",
        ROOT / "docs" / "SCENARIOS.md",
    )
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "ozkangrk/serving-verdict" not in text, path
        assert "Serving Verdict" not in text, path


def test_openapi_uses_current_product_and_package_version(tmp_path: Path) -> None:
    app = create_app(ONLY_BIND_HOST, 0, tmp_path)
    document = TestClient(app).get("/openapi.json").json()
    assert document["info"] == {"title": "LLM ServeVerdict", "version": __version__}
