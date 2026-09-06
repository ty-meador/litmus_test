"""The docs site is the published form of ``docs/`` plus the public API reference.

Issue #82: mkdocs + mkdocstrings, ``make docs`` is the local build, the gate
fails on a broken cross-reference, GitLab Pages publishes on a version tag,
and the README records the URL that belongs on a station label.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import litmus

ROOT = Path(__file__).resolve().parents[1]
MKDOCS = ROOT / "mkdocs.yml"
API_PAGE = ROOT / "docs" / "api.md"
README = ROOT / "README.md"
MAKEFILE = ROOT / "Makefile"
CI = ROOT / ".gitlab-ci.yml"

PAGES_URL = "https://tyler-meador.gitlab.io/litmus_test/"
STATION_URL = f"{PAGES_URL}floor-technician/"

_IDENT = re.compile(r"^::: litmus\.([A-Za-z_][A-Za-z0-9_]*)$", re.MULTILINE)
_NAV_ENTRY = re.compile(r":\s*([A-Za-z0-9_./-]+\.md)\s*$", re.MULTILINE)


def _recipe(target: str, makefile: str) -> str:
    """Body of a Makefile target, from the header to the next unindented rule."""
    lines = makefile.splitlines()
    body: list[str] = []
    collecting = False
    for line in lines:
        if collecting:
            if line.startswith("\t"):
                body.append(line)
                continue
            if not line.strip() or line.startswith("#") or line.startswith("."):
                continue
            break
        if re.match(rf"^{re.escape(target)}\s*:", line):
            collecting = True
    return "\n".join(body)


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    dest = tmp_path_factory.mktemp("site")
    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "-d", str(dest)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return dest


class TestDocsSiteConfig:
    def test_mkdocs_yml_exists_and_is_strict(self) -> None:
        text = MKDOCS.read_text(encoding="utf-8")
        assert "strict: true" in text
        assert "mkdocstrings:" in text
        assert f"site_url: {PAGES_URL}" in text

    def test_every_markdown_page_is_in_the_nav(self) -> None:
        nav_pages = set(_NAV_ENTRY.findall(MKDOCS.read_text(encoding="utf-8")))
        on_disk = {path.name for path in (ROOT / "docs").glob("*.md")}
        assert on_disk, "docs/ grew no markdown pages"
        missing = on_disk - nav_pages
        extra = nav_pages - on_disk
        assert missing == set()
        assert extra == set()

    def test_api_page_identifiers_match_dunder_all(self) -> None:
        names = _IDENT.findall(API_PAGE.read_text(encoding="utf-8"))
        assert names == list(litmus.__all__)


class TestReadmeRecordsThePublishedUrl:
    def test_readme_names_the_pages_url(self) -> None:
        readme = README.read_text(encoding="utf-8")
        assert PAGES_URL in readme

    def test_readme_names_the_station_url(self) -> None:
        readme = README.read_text(encoding="utf-8")
        assert STATION_URL in readme
        assert "docs/floor-technician.md" in readme


class TestGateBuildsTheSite:
    def test_makefile_docs_target_is_in_check(self) -> None:
        makefile = MAKEFILE.read_text(encoding="utf-8")
        assert _recipe("docs", makefile)
        assert "$(MAKE) docs" in _recipe("check", makefile)

    def test_pages_job_runs_on_tags(self) -> None:
        ci = CI.read_text(encoding="utf-8")
        assert re.search(r"^pages:", ci, re.MULTILINE)
        assert "if: $CI_COMMIT_TAG" in ci
        pages = ci.split("pages:", 1)[1]
        assert "make docs" in pages
        assert "public" in pages

    def test_workflow_allows_tag_pipelines(self) -> None:
        ci = CI.read_text(encoding="utf-8")
        assert "$CI_COMMIT_TAG" in ci.split("pages:", 1)[0]


class TestBuiltSite:
    def test_homepage_and_station_page_exist(self, site: Path) -> None:
        assert (site / "index.html").is_file()
        assert (site / "floor-technician" / "index.html").is_file()

    def test_every_public_name_appears_in_the_api_reference(self, site: Path) -> None:
        html = (site / "api" / "index.html").read_text(encoding="utf-8")
        missing = [name for name in litmus.__all__ if name not in html]
        assert missing == []

    def test_published_schemas_are_copied(self, site: Path) -> None:
        assert (site / "schema" / "result-v1.schema.json").is_file()
        assert (site / "schema" / "timeline-v1.schema.json").is_file()
