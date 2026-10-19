"""``litmus/no-verdict-in-cleanup``: cleanup restores state and does not verdict.

The cases mirror the legacy language server's ``pass-in-cleanup`` suite, translated
to the Python idiom (``return PASS`` instead of ``return "PASS"``, ``raise TestFailure``
instead of ``throw new TestFailure``).
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.no_verdict_in_cleanup import NoVerdictInCleanupRule

CODE = "litmus/no-verdict-in-cleanup"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(NoVerdictInCleanupRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestNoVerdictInCleanupFires:
    def test_return_pass(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def cleanup(self, utils: TestUtils) -> None:
                    return PASS
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 9, CODE)]
        assert "return is discarded" in violations[0].message

    def test_qualified_sentinel_counts(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def cleanup(self, utils: TestUtils) -> None:
                    return litmus.PASS
        """
        assert _lines(source) == [4]

    def test_raise_test_failure(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def cleanup(self, utils: TestUtils) -> None:
                    raise TestFailure("dangling state")
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 9, CODE)]
        assert "will not fail the test" in violations[0].message

    def test_qualified_test_failure_still_fires(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def cleanup(self, utils: TestUtils) -> None:
                    raise litmus.TestFailure("dangling")
        """
        assert _lines(source) == [4]

    def test_nested_helper_inside_cleanup_is_still_cleanup(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def cleanup(self, utils: TestUtils) -> None:
                    def inner():
                        return PASS
                    inner()
        """
        assert _lines(source) == [5]


class TestNoVerdictInCleanupStaysQuiet:
    def test_return_pass_in_a_test_method(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    if True:
                        return PASS
                    raise TestFailure("no")
                async def cleanup(self, utils: TestUtils) -> None:
                    utils.logger.info("restored")
        """
        assert _lines(source) == []

    def test_plain_exception_in_cleanup_is_not_this_rule(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def cleanup(self, utils: TestUtils) -> None:
                    raise RuntimeError("io error")
                    raise
                    raise err
                    raise (lambda: RuntimeError("x"))()
        """
        assert _lines(source) == []

    def test_a_cleanup_that_logs_a_request_miss_is_clean(self) -> None:
        # The shape litmus/model-resolution-failure asks for in cleanup (issue #153):
        # the two rules must agree on it, so each self-test pins its own half.
        source = """
            class Suite(LitmusTest):
                async def cleanup(self, utils: TestUtils) -> None:
                    try:
                        await utils.ctx["api"].request(DeviceRequest(method="DELETE", path="/"))
                    except DeviceApiOperationFailure as error:
                        utils.logger.warning("deny-list entry left behind: %s", error)
        """
        assert _lines(source) == []

    def test_non_litmus_classes_are_ignored(self) -> None:
        source = """
            class Plain:
                async def cleanup(self) -> None:
                    return PASS
        """
        assert _lines(source) == []


class TestNoVerdictInCleanupAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:45:13: {CODE} ")
        assert lines[-1] == "Found 1 violation."
