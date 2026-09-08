"""Mechanical fixes: gated-pass wraps the return; method-reference lifts the lambda."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from litmus_check.lsp import LitmusLsp, LspCodeAction, LspRange

_SUITE = Path("suite.py")


def _action(source: str, code: str) -> tuple[str, LspCodeAction]:
    text = dedent(source)
    adapter = LitmusLsp()
    diagnostics = [item for item in adapter.diagnostics(_SUITE, text) if item.code == code]
    assert diagnostics, f"expected a {code} diagnostic"
    diagnostic = diagnostics[0]
    actions = adapter.code_actions(_SUITE, text, diagnostic.range)
    assert len(actions) == 1
    return text, actions[0]


class TestGatedPassWrapsTheReturn:
    def test_inverts_a_single_not_equal_guard(self) -> None:
        source, action = _action(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                if utils.ctx["exit_code"] != 0:
                    raise TestFailure("non-zero exit")
                return PASS
            """,
            "litmus/gated-pass",
        )

        assert "Derive pass predicate" in action.title
        result = action.apply(source)
        leftover = [
            item
            for item in LitmusLsp().diagnostics(_SUITE, result)
            if item.code == "litmus/gated-pass"
        ]
        assert leftover == []
        assert "if utils.ctx['exit_code'] == 0: return PASS" in result
        assert 'raise TestFailure("non-zero exit")' in result
        assert "unknown failure mode" in result

    def test_ands_multiple_guards_in_source_order(self) -> None:
        source, action = _action(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                if utils.ctx["exit_code"] != 0:
                    raise TestFailure("a")
                if utils.ctx["stderr"]:
                    raise TestFailure("b")
                return PASS
            """,
            "litmus/gated-pass",
        )

        result = action.apply(source)
        assert "if utils.ctx['exit_code'] == 0 and not utils.ctx['stderr']: return PASS" in result

    def test_cancels_a_not_guard(self) -> None:
        source, action = _action(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                if not utils.ctx["ready"]:
                    raise TestFailure("not ready")
                return PASS
            """,
            "litmus/gated-pass",
        )

        assert "if utils.ctx['ready']: return PASS" in action.apply(source)

    def test_flips_a_relational_operator(self) -> None:
        source, action = _action(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                if utils.ctx["temperature"] < 0:
                    raise TestFailure("too cold")
                return PASS
            """,
            "litmus/gated-pass",
        )

        assert "if utils.ctx['temperature'] >= 0: return PASS" in action.apply(source)

    def test_wraps_or_in_not_parens(self) -> None:
        source, action = _action(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                if utils.ctx["a"] or utils.ctx["b"]:
                    raise TestFailure("either")
                return PASS
            """,
            "litmus/gated-pass",
        )

        assert "if not (utils.ctx['a'] or utils.ctx['b']): return PASS" in action.apply(source)

    def test_fallback_wraps_a_bare_return_when_there_are_no_guards(self) -> None:
        source, action = _action(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                await utils.wait_for(lambda: True, looking_for="ready")
                return PASS
            """,
            "litmus/gated-pass",
        )

        assert action.title.startswith("Wrap return PASS")
        result = action.apply(source)
        assert "if False:  # what does a passing device look like?" in result
        assert "if ...:" not in result
        assert "return PASS" in result
        assert "raise TestFailure(" in result

    def test_adds_the_testfailure_import_when_missing(self) -> None:
        source, action = _action(
            """
            from litmus import PASS

            async def claim(utils):
                return PASS
            """,
            "litmus/gated-pass",
        )

        result = action.apply(source)
        assert "from litmus import PASS, TestFailure" in result

    def test_fallback_cannot_return_pass_unconditionally(self) -> None:
        source, action = _action(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                return PASS
            """,
            "litmus/gated-pass",
        )
        result = action.apply(source)
        leftover = [
            item
            for item in LitmusLsp().diagnostics(_SUITE, result)
            if item.code == "litmus/gated-pass"
        ]

        assert "if False:" in result
        assert "if ...:" not in result
        assert "raise TestFailure(" in result
        assert leftover == []

    def test_binding_between_guard_and_return_falls_back(self) -> None:
        source, action = _action(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                if not utils.ctx["ready"]:
                    raise TestFailure("not ready", "ready", "not ready")
                result = utils.exec("status")
                if result.exit_code != 0:
                    raise TestFailure("bad status", "0", "nonzero")
                return PASS
            """,
            "litmus/gated-pass",
        )
        result = action.apply(source)
        assign_at = result.index("result = utils.exec")

        assert action.title.startswith("Wrap return PASS")
        assert "if False:" in result
        assert "result.exit_code == 0" not in result
        assert "result" not in result[:assign_at]

    def test_walrus_in_a_guard_test_falls_back(self) -> None:
        source, action = _action(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                if (code := utils.ctx["exit_code"]) != 0:
                    raise TestFailure("non-zero exit")
                if code > 10:
                    raise TestFailure("too high")
                return PASS
            """,
            "litmus/gated-pass",
        )
        result = action.apply(source)

        assert action.title.startswith("Wrap return PASS")
        assert "if False:" in result
        assert "code == 0" not in result

    def test_call_in_a_guard_test_falls_back(self) -> None:
        source, action = _action(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                if utils.exec("reset").exit_code != 0:
                    raise TestFailure("reset failed", "0", "nonzero")
                return PASS
            """,
            "litmus/gated-pass",
        )
        result = action.apply(source)

        assert action.title.startswith("Wrap return PASS")
        assert result.count("utils.exec") == 1
        assert "if False:" in result


class TestMethodReferenceLiftsTheLambda:
    def test_rewrites_a_zero_arg_lambda(self) -> None:
        source, action = _action(
            """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", lambda: self._run())
            """,
            "litmus/method-reference",
        )

        assert action.title == "Replace with `self._run`"
        assert 'self.test("x", self._run)' in action.apply(source)
        assert "lambda" not in action.apply(source)

    def test_rewrites_a_forwarding_lambda(self) -> None:
        source, action = _action(
            """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", lambda u: self._run(u))
            """,
            "litmus/method-reference",
        )

        assert 'self.test("x", self._run)' in action.apply(source)

    def test_keyword_fn_argument(self) -> None:
        source, action = _action(
            """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", fn=lambda u: self._run(u))
            """,
            "litmus/method-reference",
        )

        assert 'self.test("x", fn=self._run)' in action.apply(source)

    def test_extra_logic_is_not_rewritten(self) -> None:
        text = dedent(
            """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", lambda u: self._run(u, 42))
            """
        )
        adapter = LitmusLsp()
        diagnostics = [
            item
            for item in adapter.diagnostics(_SUITE, text)
            if item.code == "litmus/method-reference"
        ]
        assert diagnostics
        actions = adapter.code_actions(_SUITE, text, diagnostics[0].range)

        assert actions == ()

    def test_nested_def_is_not_rewritten(self) -> None:
        text = dedent(
            """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    async def _inner(utils: TestUtils) -> Verdict:
                        if True:
                            return PASS
                        raise TestFailure("no")
                    return self.test("x", _inner)
            """
        )
        adapter = LitmusLsp()
        diagnostics = [
            item
            for item in adapter.diagnostics(_SUITE, text)
            if item.code == "litmus/method-reference"
        ]
        assert diagnostics
        actions = adapter.code_actions(_SUITE, text, diagnostics[0].range)

        assert actions == ()


class TestActionsStayOnTheSelection:
    def test_a_disjoint_caret_gets_no_action(self) -> None:
        text = dedent(
            """
            from litmus import PASS

            async def claim(utils):
                return PASS
            """
        )
        actions = LitmusLsp().code_actions(_SUITE, text, LspRange(0, 0, 0, 0))

        assert actions == ()


class TestActionsAreNotPreferred:
    def test_gated_pass_is_not_preferred(self) -> None:
        _source, action = _action(
            """
            from litmus import PASS, TestFailure

            async def claim(utils):
                if utils.ctx["exit_code"] != 0:
                    raise TestFailure("non-zero exit")
                return PASS
            """,
            "litmus/gated-pass",
        )

        assert action.is_preferred is False
        assert action.to_lsp("file:///suite.py")["isPreferred"] is False

    def test_method_reference_is_not_preferred(self) -> None:
        _source, action = _action(
            """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", lambda: self._run())
            """,
            "litmus/method-reference",
        )

        assert action.is_preferred is False
        assert action.to_lsp("file:///suite.py")["isPreferred"] is False
