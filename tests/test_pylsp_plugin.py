"""The python-lsp-server hooks are a thin translation of :class:`LitmusLsp`."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

from litmus_check.lsp import LspObject
from litmus_check.pylsp_plugin import (
    PylspAdapter,
    pylsp_code_actions,
    pylsp_lint,
    pylsp_settings,
)
from litmus_check.registry import RuleRegistry

SEEDED = Path(__file__).parent / "fixtures" / "litmus_check" / "seeded_suite.py"


class _Config:
    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    def plugin_settings(self, name: str) -> dict[str, object]:
        del name
        return {"enabled": self._enabled}


def _document(source: str, path: str = "suite.py") -> SimpleNamespace:
    return SimpleNamespace(path=path, source=source, uri=f"file:///{path}")


class TestPylspHooks:
    def test_settings_enable_the_plugin(self) -> None:
        plugin = LspObject.mapping(LspObject.mapping(pylsp_settings()["plugins"])["litmus_check"])
        assert plugin["enabled"] is True

    def test_lint_publishes_every_catalogue_rule_on_the_seeded_fixture(self) -> None:
        document = _document(SEEDED.read_text(encoding="utf-8"), str(SEEDED))
        diagnostics = pylsp_lint(_Config(), None, document, False)
        found = {item["code"] for item in diagnostics}

        assert found == set(RuleRegistry.discover().codes())
        assert all(item["source"] == "litmus-check" for item in diagnostics)

    def test_lint_respects_enabled_false(self) -> None:
        document = _document("async def claim(utils):\n    return PASS\n")
        diagnostics = pylsp_lint(_Config(enabled=False), None, document, False)

        assert diagnostics == []

    def test_code_actions_wrap_gated_pass(self) -> None:
        source = dedent(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                return PASS
            """
        )
        document = _document(source)
        diagnostics = pylsp_lint(_Config(), None, document, False)
        gated = next(item for item in diagnostics if item["code"] == "litmus/gated-pass")
        actions = pylsp_code_actions(
            _Config(),
            None,
            document,
            gated["range"],
            {"diagnostics": [gated]},
        )

        assert len(actions) == 1
        assert "Wrap return PASS" in str(actions[0]["title"])
        changes = LspObject.mapping(LspObject.mapping(actions[0]["edit"])["changes"])
        assert document.uri in changes

    def test_code_actions_skip_when_only_is_not_quickfix(self) -> None:
        source = "async def claim(utils):\n    return PASS\n"
        document = _document(source)
        actions = pylsp_code_actions(
            _Config(),
            None,
            document,
            {"start": {"line": 1, "character": 4}, "end": {"line": 1, "character": 4}},
            {"only": ["refactor"]},
        )

        assert actions == []

    def test_adapter_class_is_the_hook_implementation(self) -> None:
        adapter = PylspAdapter()
        document = _document("async def claim(utils):\n    return PASS\n")
        diagnostics = adapter.lint(_Config(), document)

        assert any(item["code"] == "litmus/gated-pass" for item in diagnostics)

    def test_code_actions_respect_enabled_false(self) -> None:
        document = _document("async def claim(utils):\n    return PASS\n")
        actions = pylsp_code_actions(
            _Config(enabled=False),
            None,
            document,
            {"start": {"line": 1, "character": 4}, "end": {"line": 1, "character": 4}},
            {},
        )

        assert actions == []

    def test_malformed_selection_yields_no_actions(self) -> None:
        document = _document("async def claim(utils):\n    return PASS\n")
        actions = pylsp_code_actions(_Config(), None, document, "not-a-range", {})

        assert actions == []

    def test_only_quickfix_still_offers_actions(self) -> None:
        source = "async def claim(utils):\n    return PASS\n"
        document = _document(source)
        diagnostics = pylsp_lint(_Config(), None, document, False)
        gated = next(item for item in diagnostics if item["code"] == "litmus/gated-pass")
        actions = pylsp_code_actions(
            _Config(),
            None,
            document,
            gated["range"],
            {"only": ["quickfix"]},
        )

        assert len(actions) == 1

    def test_non_dict_only_is_treated_as_any_kind(self) -> None:
        source = "async def claim(utils):\n    return PASS\n"
        document = _document(source)
        diagnostics = pylsp_lint(_Config(), None, document, False)
        gated = next(item for item in diagnostics if item["code"] == "litmus/gated-pass")
        actions = pylsp_code_actions(
            _Config(), None, document, gated["range"], {"only": "quickfix"}
        )

        assert len(actions) == 1

    def test_non_mapping_context_is_treated_as_any_kind(self) -> None:
        source = "async def claim(utils):\n    return PASS\n"
        document = _document(source)
        diagnostics = pylsp_lint(_Config(), None, document, False)
        gated = next(item for item in diagnostics if item["code"] == "litmus/gated-pass")
        actions = pylsp_code_actions(_Config(), None, document, gated["range"], ["not-an-object"])

        assert len(actions) == 1

    def test_config_without_plugin_settings_stays_enabled(self) -> None:
        document = _document("async def claim(utils):\n    return PASS\n")
        diagnostics = pylsp_lint(SimpleNamespace(), None, document, False)

        assert any(item["code"] == "litmus/gated-pass" for item in diagnostics)

    def test_plugin_settings_that_are_not_a_mapping_stay_enabled(self) -> None:
        class WeirdConfig:
            def plugin_settings(self, name: str) -> object:
                del name
                return "nope"

        document = _document("async def claim(utils):\n    return PASS\n")
        diagnostics = pylsp_lint(WeirdConfig(), None, document, False)

        assert any(item["code"] == "litmus/gated-pass" for item in diagnostics)

    def test_path_object_and_missing_uri_still_lint(self) -> None:
        path = Path("suite.py")
        document = SimpleNamespace(path=path, source="async def claim(utils):\n    return PASS\n")
        adapter = PylspAdapter()
        diagnostics = adapter.lint(_Config(), document)
        actions = adapter.code_actions(
            _Config(),
            document,
            diagnostics[0]["range"],
            {},
        )

        assert diagnostics
        assert actions
        changes = LspObject.mapping(LspObject.mapping(actions[0]["edit"])["changes"])
        assert path.resolve().as_uri() in changes

    def test_untitled_buffer_with_non_string_source(self) -> None:
        document = SimpleNamespace(path="", source=None, uri="")
        diagnostics = PylspAdapter().lint(_Config(), document)

        assert diagnostics == []
