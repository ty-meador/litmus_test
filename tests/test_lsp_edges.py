"""Edge paths for the LSP adapter: payload narrowing, mechanical-fix refusal, helpers."""

from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent

import pytest

from litmus_check.fixes import (
    GatedPassFix,
    MethodReferenceFix,
    _is_pure_raise_guard,  # pyright: ignore[reportPrivateUsage]
    _lambda_method_ref,  # pyright: ignore[reportPrivateUsage]
    _negate,  # pyright: ignore[reportPrivateUsage]
    _statement_list,  # pyright: ignore[reportPrivateUsage]
)
from litmus_check.lsp import (
    LitmusLsp,
    LspDiagnostic,
    LspObject,
    LspRange,
    TextEdit,
    ensure_testfailure_import,
    inner_indent,
    line_indent,
    node_at,
    node_span,
)
from litmus_check.model import SuiteModule, Violation

_SUITE = Path("suite.py")


def _module(source: str) -> SuiteModule:
    return SuiteModule(_SUITE, dedent(source))


class TestLspObject:
    def test_mapping_rejects_a_non_object(self) -> None:
        with pytest.raises(TypeError, match="expected an object"):
            LspObject.mapping(["nope"])

    def test_integer_rejects_bool_and_non_int(self) -> None:
        with pytest.raises(TypeError, match="expected an integer"):
            LspObject.integer(True)
        with pytest.raises(TypeError, match="expected an integer"):
            LspObject.integer("1")

    def test_from_lsp_round_trips_a_range(self) -> None:
        parsed = LspRange.from_lsp(
            {"start": {"line": 3, "character": 1}, "end": {"line": 3, "character": 8}}
        )

        assert parsed == LspRange(3, 1, 3, 8)


class TestTextEditApply:
    def test_multiline_replacement_keeps_neighbours(self) -> None:
        source = "aaa\nbbb\nccc\n"
        edited = TextEdit.apply_all(source, [TextEdit(1, 1, 1, 2, "X\nY")])

        assert edited == "aaa\nbX\nYb\nccc\n"


class TestDiagnosticSpan:
    def test_whitespace_only_line_extends_to_the_line_length(self) -> None:
        diagnostic = LitmusLsp._diagnostic(  # pyright: ignore[reportPrivateUsage]
            Violation(_SUITE, line=1, column=5, code="litmus/gated-pass", message="x"),
            "    \n",
        )

        assert diagnostic.start_character == 4
        assert diagnostic.end_character == 4

    def test_line_past_eof_still_renders(self) -> None:
        diagnostic = LitmusLsp._diagnostic(  # pyright: ignore[reportPrivateUsage]
            Violation(_SUITE, line=99, column=4, code="litmus/gated-pass", message="x"),
            "return PASS\n",
        )

        assert diagnostic.start_line == 98
        assert diagnostic.end_character == diagnostic.start_character + 1


class TestCodeActionsOnBrokenBuffers:
    def test_syntax_error_yields_no_actions(self) -> None:
        actions = LitmusLsp().code_actions(_SUITE, "def (:\n", LspRange(0, 0, 0, 1))

        assert actions == ()


class TestGatedPassRefusalAndParents:
    def test_wrong_node_kind_offers_nothing(self) -> None:
        module = _module(
            """
            from litmus import PASS

            async def claim(utils):
                return PASS
            """
        )
        diagnostic = LspDiagnostic(_SUITE, "litmus/gated-pass", "x", 2, 0, 2, 10)

        assert GatedPassFix().offer(module, "", diagnostic) is None

    def test_statement_list_reads_an_if_body(self) -> None:
        tree = ast.parse("if True:\n    return PASS\n")
        if_node = tree.body[0]

        assert isinstance(if_node, ast.If)
        body = _statement_list(if_node, if_node.body[0])
        assert body is not None
        assert isinstance(body[0], ast.Return)

    def test_statement_list_answers_the_block_that_holds_the_node(self) -> None:
        tree = ast.parse(
            dedent(
                """
                try:
                    x = 1
                except OSError:
                    pass
                else:
                    y = 2
                finally:
                    return PASS
                """
            )
        )
        try_node = tree.body[0]

        assert isinstance(try_node, ast.Try)
        assert _statement_list(try_node, try_node.orelse[0]) == try_node.orelse
        assert _statement_list(try_node, try_node.finalbody[0]) == try_node.finalbody
        assert _statement_list(try_node, try_node.body[0]) == try_node.body
        # A node from another block entirely is not answered with a guess.
        assert _statement_list(try_node, ast.Pass()) is None

    def test_return_inside_try_and_except(self) -> None:
        text = dedent(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                try:
                    if utils.ctx["exit_code"] != 0:
                        raise TestFailure("non-zero exit")
                    return PASS
                except RuntimeError:
                    if utils.ctx["stderr"]:
                        raise TestFailure("stderr")
                    return PASS
            """
        )
        adapter = LitmusLsp()
        diagnostics = [
            item for item in adapter.diagnostics(_SUITE, text) if item.code == "litmus/gated-pass"
        ]
        assert len(diagnostics) == 2
        rewritten = diagnostics[0]
        result = adapter.code_actions(_SUITE, text, rewritten.range)[0].apply(text)

        assert "if utils.ctx['exit_code'] == 0: return PASS" in result

    def test_return_inside_for_while_with(self) -> None:
        text = dedent(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                for _ in [1]:
                    if utils.ctx["exit_code"] != 0:
                        raise TestFailure("non-zero exit")
                    return PASS
                while False:
                    if utils.ctx["stderr"]:
                        raise TestFailure("stderr")
                    return PASS
                with utils:
                    if not utils.ctx["ready"]:
                        raise TestFailure("not ready")
                    return PASS
            """
        )
        adapter = LitmusLsp()
        diagnostics = [
            item for item in adapter.diagnostics(_SUITE, text) if item.code == "litmus/gated-pass"
        ]
        assert len(diagnostics) == 3
        result = adapter.code_actions(_SUITE, text, diagnostics[0].range)[0].apply(text)

        assert "if utils.ctx['exit_code'] == 0: return PASS" in result

    def test_smart_fix_adds_the_testfailure_import(self) -> None:
        text = dedent(
            """
            from typing import Any
            from litmus import PASS

            async def claim(utils):
                if utils.ctx["exit_code"] != 0:
                    raise TestFailure("non-zero exit")
                return PASS
            """
        )
        adapter = LitmusLsp()
        diagnostic = next(
            item for item in adapter.diagnostics(_SUITE, text) if item.code == "litmus/gated-pass"
        )
        result = adapter.code_actions(_SUITE, text, diagnostic.range)[0].apply(text)

        assert "from litmus import PASS, TestFailure" in result

    def test_fallback_inserts_import_when_none_exists(self) -> None:
        text = "async def claim(utils):\n    return PASS\n"
        adapter = LitmusLsp()
        diagnostic = adapter.diagnostics(_SUITE, text)[0]
        result = adapter.code_actions(_SUITE, text, diagnostic.range)[0].apply(text)

        assert "from litmus import TestFailure" in result

    def test_star_import_is_left_alone(self) -> None:
        module = _module(
            """
            from litmus import *

            async def claim(utils):
                return PASS
            """
        )

        assert ensure_testfailure_import(module) is None

    def test_if_with_else_is_not_a_pure_guard(self) -> None:
        text = dedent(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                if utils.ctx["exit_code"] != 0:
                    raise TestFailure("non-zero exit")
                else:
                    leftover = 1
                return PASS
            """
        )
        adapter = LitmusLsp()
        diagnostic = next(
            item for item in adapter.diagnostics(_SUITE, text) if item.code == "litmus/gated-pass"
        )
        action = adapter.code_actions(_SUITE, text, diagnostic.range)[0]

        assert action.title.startswith("Wrap return PASS")

    def test_if_with_two_body_statements_is_not_a_pure_guard(self) -> None:
        text = dedent(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                if utils.ctx["exit_code"] != 0:
                    leftover = 1
                    raise TestFailure("non-zero exit")
                return PASS
            """
        )
        adapter = LitmusLsp()
        diagnostic = next(
            item for item in adapter.diagnostics(_SUITE, text) if item.code == "litmus/gated-pass"
        )
        action = adapter.code_actions(_SUITE, text, diagnostic.range)[0]

        assert action.title.startswith("Wrap return PASS")

    def test_chained_compare_is_wrapped_in_not_parens(self) -> None:
        text = dedent(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                if 0 < utils.ctx["n"] < 10:
                    raise TestFailure("in range")
                return PASS
            """
        )
        adapter = LitmusLsp()
        diagnostic = next(
            item for item in adapter.diagnostics(_SUITE, text) if item.code == "litmus/gated-pass"
        )
        result = adapter.code_actions(_SUITE, text, diagnostic.range)[0].apply(text)

        assert "if not (0 < utils.ctx['n'] < 10): return PASS" in result


class TestMethodReferenceRefusal:
    def test_wrong_node_kind_offers_nothing(self) -> None:
        module = _module(
            """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", lambda: self._run())
            """
        )
        diagnostic = LspDiagnostic(_SUITE, "litmus/method-reference", "x", 1, 0, 1, 5)

        assert MethodReferenceFix().offer(module, "", diagnostic) is None

    def test_starargs_are_not_rewritten(self) -> None:
        text = dedent(
            """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", lambda *a: self._run(*a))
            """
        )
        adapter = LitmusLsp()
        diagnostic = next(
            item
            for item in adapter.diagnostics(_SUITE, text)
            if item.code == "litmus/method-reference"
        )

        assert adapter.code_actions(_SUITE, text, diagnostic.range) == ()

    def test_keyword_call_is_not_rewritten(self) -> None:
        text = dedent(
            """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", lambda u: self._run(u, extra=1))
            """
        )
        adapter = LitmusLsp()
        diagnostic = next(
            item
            for item in adapter.diagnostics(_SUITE, text)
            if item.code == "litmus/method-reference"
        )

        assert adapter.code_actions(_SUITE, text, diagnostic.range) == ()

    def test_subscript_callee_is_not_rewritten(self) -> None:
        text = dedent(
            """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", lambda u: table[0](u))
            """
        )
        adapter = LitmusLsp()
        diagnostic = next(
            item
            for item in adapter.diagnostics(_SUITE, text)
            if item.code == "litmus/method-reference"
        )

        assert adapter.code_actions(_SUITE, text, diagnostic.range) == ()

    def test_body_that_is_not_a_call_is_not_rewritten(self) -> None:
        text = dedent(
            """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", lambda u: u)
            """
        )
        adapter = LitmusLsp()
        diagnostic = next(
            item
            for item in adapter.diagnostics(_SUITE, text)
            if item.code == "litmus/method-reference"
        )

        assert adapter.code_actions(_SUITE, text, diagnostic.range) == ()

    def test_argument_name_mismatch_is_not_rewritten(self) -> None:
        text = dedent(
            """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", lambda u: self._run(v))
            """
        )
        adapter = LitmusLsp()
        diagnostic = next(
            item
            for item in adapter.diagnostics(_SUITE, text)
            if item.code == "litmus/method-reference"
        )

        assert adapter.code_actions(_SUITE, text, diagnostic.range) == ()


class TestHelpers:
    def test_node_span_none_without_locations(self) -> None:
        assert node_span(ast.Return()) is None

    def test_node_at_skips_nodes_without_a_span(self) -> None:
        tree = ast.Module(body=[ast.Return(value=ast.Name(id="PASS"))], type_ignores=[])

        assert node_at(tree, 0, 0, ast.Return) is None

    def test_line_indent_out_of_range_is_empty(self) -> None:
        assert line_indent("hello", 9) == ""

    def test_inner_indent_follows_tabs(self) -> None:
        assert inner_indent("\t") == "\t\t"

    def test_statement_list_rejects_unknown_parents(self) -> None:
        assert _statement_list(ast.Constant(1), ast.Pass()) is None

    def test_is_pure_raise_guard_rejects_a_plain_statement(self) -> None:
        assert _is_pure_raise_guard(ast.Pass()) is False

    def test_negate_is_and_in(self) -> None:
        assert _negate(ast.parse("x is None", mode="eval").body) == "x is not None"
        assert _negate(ast.parse("x in y", mode="eval").body) == "x not in y"

    def test_lambda_method_ref_unwraps_await(self) -> None:
        node = ast.Lambda(
            args=ast.arguments(
                posonlyargs=[],
                args=[],
                kwonlyargs=[],
                kw_defaults=[],
                defaults=[],
            ),
            body=ast.Await(value=ast.Call(func=ast.Name(id="run"), args=[], keywords=[])),
        )

        assert _lambda_method_ref(node) == "run"

    def test_method_reference_fix_refuses_a_lambda_without_span(self) -> None:
        module = _module(
            """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", lambda: self._run())
            """
        )
        lambda_node = next(node for node in ast.walk(module.tree) if isinstance(node, ast.Lambda))
        lambda_node.lineno = None  # type: ignore[assignment]
        lambda_node.col_offset = None  # type: ignore[assignment]
        diagnostic = LspDiagnostic(_SUITE, "litmus/method-reference", "x", 0, 0, 0, 0)
        # node_at will not match a span-less lambda; offer stays silent.
        assert MethodReferenceFix().offer(module, "", diagnostic) is None
