"""``litmus/logger-not-print``: suite code logs through ``utils.logger``.

Only the scoped logger reaches the timeline. ``print`` reaches stdout, which the
framework does not capture and the Floor Technician never opens.
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.logger_not_print import LoggerNotPrintRule

CODE = "litmus/logger-not-print"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(LoggerNotPrintRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestLoggerNotPrintFire:
    def test_print_in_a_test_method_names_the_logger(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _leaf(self, utils: TestUtils) -> Verdict:
                    print("checking")
                    if utils.ctx["ok"]:
                        return PASS
                    raise TestFailure("no", "yes", "no")
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 9, CODE)]
        assert "utils.logger" in violations[0].message

    def test_every_lifecycle_slot_is_covered(self) -> None:
        source = """
            class Suite(LitmusTest):
                def __init__(self, *, name: str) -> None:
                    super().__init__(name=name)
                    print("built")
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    print("configuring")
                    return {}
                async def cleanup(self, utils: TestUtils) -> None:
                    print("done", file=sys.stderr)
        """
        assert _lines(source) == [5, 7, 10]


class TestLoggerNotPrintStaySilent:
    def test_the_scoped_logger_is_the_idiom(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _leaf(self, utils: TestUtils) -> Verdict:
                    utils.logger.info("checking %s", utils.ctx["target"])
                    if utils.ctx["ok"]:
                        return PASS
                    raise TestFailure("no", "yes", "no")
        """
        assert _lines(source) == []

    def test_print_outside_a_suite_class_is_the_harness_business(self) -> None:
        source = """
            def main() -> None:
                print("usage: ...")

            class Plain:
                def show(self) -> None:
                    print(self)

            if __name__ == "__main__":
                print(asyncio.run(main()))
        """
        assert _lines(source) == []

    def test_a_method_named_print_on_another_object_is_not_the_builtin(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _leaf(self, utils: TestUtils) -> Verdict:
                    report.print()
                    if utils.ctx["ok"]:
                        return PASS
                    raise TestFailure("no", "yes", "no")
        """
        assert _lines(source) == []


class TestLoggerNotPrintAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:107:9: {CODE} ")
        assert lines[-1] == "Found 1 violation."
