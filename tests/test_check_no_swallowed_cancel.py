"""``litmus/no-swallowed-cancel``: cancellation is the abort signal and suite code lets it pass.

A run-abort, a deadline, and an operator interrupt all arrive as
``asyncio.CancelledError`` at an ``await``. A handler that keeps it converts the abort
into a verdict the hardware did not earn.
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.no_swallowed_cancel import NoSwallowedCancelRule

CODE = "litmus/no-swallowed-cancel"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(NoSwallowedCancelRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestNoSwallowedCancelFire:
    def test_bare_except_around_an_await_names_the_fix(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _leaf(self, utils: TestUtils) -> Verdict:
                    try:
                        result = await utils.exec("true")
                    except:
                        raise TestFailure("interrupted", "exit 0", "cancelled") from None
                    if result.exit_code == 0:
                        return PASS
                    raise TestFailure("no", "0", str(result.exit_code))
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(6, 9, CODE)]
        assert "a bare except" in violations[0].message
        assert "bare raise" in violations[0].message

    def test_base_exception_and_both_cancelled_error_spellings_are_caught(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _a(self, utils: TestUtils) -> Verdict:
                    try:
                        await utils.exec("true")
                    except BaseException:
                        pass
                    return PASS
                async def _b(self, utils: TestUtils) -> Verdict:
                    try:
                        await utils.exec("true")
                    except asyncio.CancelledError:
                        pass
                    return PASS
                async def _c(self, utils: TestUtils) -> Verdict:
                    try:
                        await utils.exec("true")
                    except (TestFailure, CancelledError):
                        pass
                    return PASS
        """
        assert _lines(source) == [6, 12, 18]

    def test_contextlib_suppress_of_cancellation_is_caught(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def cleanup(self, utils: TestUtils) -> None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await utils.exec("poweroff")
                    with suppress(BaseException):
                        await utils.exec("sync")
        """
        assert _lines(source) == [4, 6]


class TestNoSwallowedCancelStaySilent:
    def test_catching_exception_lets_cancellation_through(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _leaf(self, utils: TestUtils) -> Verdict:
                    try:
                        result = await utils.exec("true")
                    except Exception as error:
                        raise TestFailure("exec broke", "a result", repr(error)) from error
                    if result.exit_code == 0:
                        return PASS
                    raise TestFailure("no", "0", str(result.exit_code))
        """
        assert _lines(source) == []

    def test_a_handler_that_reraises_after_cleanup_is_fine(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _leaf(self, utils: TestUtils) -> Verdict:
                    try:
                        await utils.exec("start")
                    except asyncio.CancelledError:
                        utils.logger.warning("interrupted; releasing the rail")
                        raise
                    return PASS
        """
        assert _lines(source) == []

    def test_a_try_with_no_await_cannot_be_cancelled_there(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _leaf(self, utils: TestUtils) -> Verdict:
                    try:
                        value = float(utils.ctx["reading"])
                    except BaseException:
                        value = 0.0
                    if value > 0:
                        return PASS
                    raise TestFailure("no", "> 0", str(value))
        """
        assert _lines(source) == []

    def test_synchronous_methods_and_other_classes_are_ignored(self) -> None:
        source = """
            class Suite(LitmusTest):
                def _parse(self, text: str) -> float:
                    try:
                        return float(text)
                    except BaseException:
                        return 0.0

            class Helper:
                async def go(self) -> None:
                    try:
                        await asyncio.sleep(1)
                    except BaseException:
                        pass
        """
        assert _lines(source) == []


class TestNoSwallowedCancelAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:116:9: {CODE} ")
        assert lines[-1] == "Found 1 violation."
