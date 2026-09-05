"""``litmus/gated-pass``: a ``return PASS`` must sit inside a conditional branch.

The cases mirror the legacy language server's ``unconditional-pass`` suite, translated
to the Python idiom (``if`` bodies, ``elif`` chains, ``match`` arms, nested defs).
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.gated_pass import GatedPassRule

CODE = "litmus/gated-pass"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(GatedPassRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestGatedPassFires:
    def test_bare_return_at_the_bottom_of_a_test_method(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return PASS
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 9, CODE)]
        assert "unconditional PASS" in violations[0].message

    def test_bare_return_after_a_guard_raise_above(self) -> None:
        source = """
            async def claim(utils):
                if utils.ctx["exit_code"] != 0:
                    raise TestFailure("bad exit")
                if utils.ctx["stderr"]:
                    raise TestFailure("stderr")
                return PASS
        """
        assert _lines(source) == [7]

    def test_bare_return_after_an_awaited_wait_for(self) -> None:
        source = """
            async def claim(utils):
                await utils.wait_for(lambda: True, looking_for="ready")
                return PASS
        """
        assert _lines(source) == [4]

    def test_qualified_sentinel_counts(self) -> None:
        source = """
            async def claim(utils):
                return litmus.PASS
        """
        assert _lines(source) == [3]

    def test_multiple_bare_returns_in_one_function_are_reported_separately(self) -> None:
        source = """
            async def claim(utils):
                for attempt in range(3):
                    return PASS
                while True:
                    return PASS
                try:
                    return PASS
                finally:
                    pass
        """
        assert _lines(source) == [4, 6, 8]

    def test_nested_functions_are_separate_scopes(self) -> None:
        source = """
            async def outer(utils):
                async def inner():
                    return PASS
                if utils.ctx["ok"]:
                    return PASS
                raise TestFailure("no")
        """
        assert _lines(source) == [4]

    def test_gated_outer_does_not_launder_a_bare_inner_return(self) -> None:
        source = """
            async def outer(utils):
                if utils.ctx["ok"]:
                    def inner():
                        return PASS
                    return PASS
                raise TestFailure("no")
        """
        assert _lines(source) == [5]


class TestGatedPassStaysQuiet:
    def test_single_line_if_body(self) -> None:
        source = """
            async def claim(utils):
                if utils.ctx["ok"]: return PASS
                raise TestFailure("no")
        """
        assert _lines(source) == []

    def test_if_block(self) -> None:
        source = """
            async def claim(utils):
                if utils.ctx["exit_code"] == 0 and not utils.ctx["stderr"]:
                    utils.logger.info("clean")
                    return PASS
                raise TestFailure("unknown failure mode")
        """
        assert _lines(source) == []

    def test_nested_if_and_else_and_elif_branches(self) -> None:
        source = """
            async def claim(utils):
                if utils.ctx["a"]:
                    if utils.ctx["b"]:
                        return PASS
                    raise TestFailure("b")
                elif utils.ctx["c"]:
                    return PASS
                else:
                    return PASS
        """
        assert _lines(source) == []

    def test_match_arm(self) -> None:
        source = """
            async def claim(utils):
                match utils.ctx["state"]:
                    case "ready":
                        return PASS
                    case other:
                        raise TestFailure(f"state {other}")
        """
        assert _lines(source) == []

    def test_gate_inside_a_loop_counts(self) -> None:
        source = """
            async def claim(utils):
                for reading in utils.ctx["readings"]:
                    if reading > 0:
                        return PASS
                raise TestFailure("no positive reading")
        """
        assert _lines(source) == []

    def test_other_returns_and_module_level_pass_are_ignored(self) -> None:
        source = """
            PASS = object()
            def helper():
                return "PASS"
            def other():
                return None
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", self._claim)
        """
        assert _lines(source) == []


class TestGatedPassAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:32:9: {CODE} ")
        assert lines[-1] == "Found 1 violation."
