"""The pylsp adapter runs the same registry the CLI lists, over an open buffer."""

from __future__ import annotations

import io
import tomllib
from pathlib import Path

from litmus_check.cli import LitmusCheckCommand
from litmus_check.lsp import DIAGNOSTIC_WARNING, LSP_SOURCE, LitmusLsp, LspObject, LspRange
from litmus_check.registry import RuleRegistry

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "litmus_check"
# The seeds are split across two files so each file's pinned line numbers stay put
# (seeded_seams.py says why); the catalogue is covered by the pair, not by either one.
SEEDED = (FIXTURES / "seeded_suite.py", FIXTURES / "seeded_seams.py")
CLEAN = FIXTURES / "clean_suite.py"


def _listed_codes() -> tuple[str, ...]:
    return RuleRegistry.discover().codes()


def _seeded_codes() -> set[str]:
    lsp = LitmusLsp()
    return {
        item.code
        for path in SEEDED
        for item in lsp.diagnostics(path, path.read_text(encoding="utf-8"))
    }


class TestDiagnosticsCoverTheCatalogue:
    def test_every_listed_rule_fires_on_the_seeded_fixture(self) -> None:
        assert _seeded_codes() == set(_listed_codes())

    def test_list_rules_cli_matches_the_registry_the_adapter_uses(self) -> None:
        out = io.StringIO()
        LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(["--list-rules"])
        listed = tuple(line.split("\t", 1)[0] for line in out.getvalue().splitlines() if line)

        assert listed == _listed_codes()
        assert _seeded_codes() == set(listed)

    def test_clean_fixture_is_silent(self) -> None:
        diagnostics = LitmusLsp().diagnostics(CLEAN, CLEAN.read_text(encoding="utf-8"))

        assert diagnostics == ()

    def test_syntax_error_yields_no_diagnostics(self) -> None:
        diagnostics = LitmusLsp().diagnostics(Path("broken.py"), "def (:\n")

        assert diagnostics == ()

    def test_diagnostic_shape_is_lsp(self) -> None:
        source = "async def claim(utils):\n    return PASS\n"
        diagnostic = LitmusLsp().diagnostics(Path("suite.py"), source)[0]
        payload = diagnostic.to_lsp()

        assert diagnostic.code == "litmus/gated-pass"
        assert diagnostic.source == LSP_SOURCE
        assert diagnostic.severity == DIAGNOSTIC_WARNING
        assert diagnostic.start_line == 1
        assert payload["source"] == LSP_SOURCE
        assert payload["code"] == "litmus/gated-pass"
        assert payload["severity"] == DIAGNOSTIC_WARNING
        start = LspObject.mapping(LspObject.mapping(payload["range"])["start"])
        assert start["line"] == 1


class TestOptionalExtra:
    def test_core_dependencies_do_not_include_pylsp(self) -> None:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        core = data["project"]["dependencies"]
        extra = data["project"]["optional-dependencies"]["pylsp"]

        assert not any("python-lsp-server" in item for item in core)
        assert any("python-lsp-server" in item for item in extra)

    def test_entry_point_points_at_the_plugin_module(self) -> None:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        assert data["project"]["entry-points"]["pylsp"]["litmus_check"] == (
            "litmus_check.pylsp_plugin"
        )

    def test_adapter_module_does_not_import_pylsp(self) -> None:
        source = (ROOT / "tools" / "litmus_check" / "lsp.py").read_text(encoding="utf-8")

        assert "import pylsp" not in source
        assert "from pylsp" not in source


class TestRangeOverlap:
    def test_caret_at_diagnostic_start_selects_it(self) -> None:
        diagnostic = LspRange(4, 8, 4, 20)
        caret = LspRange(4, 8, 4, 8)

        assert diagnostic.overlaps(caret)
        assert caret.overlaps(diagnostic)

    def test_disjoint_ranges_do_not_overlap(self) -> None:
        assert not LspRange(1, 0, 1, 4).overlaps(LspRange(2, 0, 2, 4))
