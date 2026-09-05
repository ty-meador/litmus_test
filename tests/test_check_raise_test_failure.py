"""``litmus/raise-test-failure``: a test method raises ``TestFailure``, not a plain error.

The cases mirror the legacy language server's ``wrong-error-in-main`` suite, translated
to the Python idiom (test methods handed to ``self.case``, ``-> Verdict`` annotations,
nested helpers inside a test method).
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.raise_test_failure import RaiseTestFailureRule

CODE = "litmus/raise-test-failure"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(RaiseTestFailureRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestRaiseTestFailureFires:
    def test_value_error_in_a_verdict_annotated_method(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    raise ValueError("temperature out of range")
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 9, CODE)]
        assert "plain ValueError" in violations[0].message
        assert "Raise TestFailure instead" in violations[0].message

    def test_runtime_error_in_a_method_passed_to_case(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.case("x", self._claim)
                async def _claim(self, utils):
                    raise RuntimeError("lost the rail")
        """
        assert _lines(source) == [6]

    def test_qualified_constructor_and_uncalled_class_both_count(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    raise builtins.KeyError("missing")
                    raise TypeError
        """
        assert _lines(source) == [4, 5]

    def test_nested_helper_inside_a_test_method_is_still_the_test_method(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    def inner():
                        raise OSError("io")
                    inner()
                    if True:
                        return PASS
                    raise TestFailure("no")
        """
        assert _lines(source) == [5]

    def test_string_annotation_and_keyword_case_still_count(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.case("x", test=self._claim)
                async def _claim(self, utils: TestUtils) -> "Verdict":
                    raise ValueError("string-annotated")
        """
        assert _lines(source) == [6]


class TestRaiseTestFailureStaysQuiet:
    def test_test_failure_is_the_verdict(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    raise TestFailure("temperature out of range", 85, 92)
                    raise TestFailure("wrapped") from err
        """
        assert _lines(source) == []

    def test_configure_and_cleanup_are_not_test_methods(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, object]:
                    raise ValueError("missing env var")
                async def cleanup(self, utils: TestUtils) -> None:
                    raise RuntimeError("cleanup failed")
                def build_suite(self) -> LitmusTest:
                    return self.case("x", self._claim)
                async def _claim(self, utils: TestUtils) -> Verdict:
                    if True:
                        return PASS
                    raise TestFailure("no")
        """
        assert _lines(source) == []

    def test_survey_hook_and_bare_reraise_and_name_raise_are_ignored(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _snapshot(self, utils: TestUtils) -> object:
                    raise ValueError("survey")
                    raise
                    raise err
                async def _claim(self, utils: TestUtils) -> Verdict:
                    if True:
                        return PASS
                    raise TestFailure("no")
        """
        assert _lines(source) == []

    def test_non_litmus_classes_are_ignored(self) -> None:
        source = """
            class Plain:
                async def _claim(self, utils: TestUtils) -> Verdict:
                    raise ValueError("unrelated")
        """
        assert _lines(source) == []

    def test_unreadable_raise_and_nested_class_case_are_ignored(self) -> None:
        source = """
            class Suite(LitmusTest):
                class Inner:
                    def build_suite(self) -> LitmusTest:
                        return self.case("x", self._claim)
                def build_suite(*args) -> LitmusTest:
                    return args[0].case("x")
                async def _claim(self, utils: TestUtils) -> Verdict:
                    raise (lambda: ValueError("x"))()
                    if True:
                        return PASS
                    raise TestFailure("no")
        """
        assert _lines(source) == []


class TestRaiseTestFailureAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:41:9: {CODE} ")
        assert lines[-1] == "Found 1 violation."
