"""``litmus/method-reference``: ``case()`` takes a method reference, not a wrapper.

The cases mirror the legacy language server's ``arrow-mandate`` suite, translated to
Python (``lambda`` and nested ``def`` in place of arrow functions). The port flags
every wrapper, not only the trivial ``lambda u: self._foo(u)`` shape: extra logic
belongs in a real method.
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.method_reference import MethodReferenceRule

CODE = "litmus/method-reference"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(MethodReferenceRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestMethodReferenceFires:
    def test_lambda_with_no_params(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.case("x", lambda: self._run())
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 31, CODE)]
        assert "method reference" in violations[0].message

    def test_lambda_that_forwards_the_parameter(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.case("x", lambda u: self._run(u))
        """
        assert _lines(source) == [4]

    def test_lambda_with_extra_logic_still_fires(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.case("x", lambda u: self._run(u, 42))
        """
        assert _lines(source) == [4]

    def test_keyword_test_argument(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.case("x", test=lambda u: self._run(u))
        """
        assert _lines(source) == [4]

    def test_nested_def_passed_by_name(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    async def _inner(utils: TestUtils) -> Verdict:
                        if True:
                            return PASS
                        raise TestFailure("no")
                    return self.case("x", _inner)
        """
        assert _lines(source) == [8]


class TestMethodReferenceStaysQuiet:
    def test_method_reference(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.case("x", self._run)
                async def _run(self, utils: TestUtils) -> Verdict:
                    if True:
                        return PASS
                    raise TestFailure("no")
        """
        assert _lines(source) == []

    def test_module_level_function_and_non_case_calls_are_ignored(self) -> None:
        source = """
            async def helper(utils: TestUtils) -> Verdict:
                if True:
                    return PASS
                raise TestFailure("no")
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    def other() -> None:
                        return None
                    del other
                    return self.sequence(self.name, [self.case("x", helper)])
        """
        assert _lines(source) == []

    def test_non_litmus_classes_are_ignored(self) -> None:
        source = """
            class Plain:
                def build_suite(self):
                    return self.case("x", lambda: self._run())
        """
        assert _lines(source) == []

    def test_missing_fn_starargs_and_class_body_calls_are_ignored(self) -> None:
        source = """
            class Suite(LitmusTest):
                dangling = object.case("x", lambda: None)
                def build_suite(self) -> LitmusTest:
                    def other() -> None:
                        return None
                    return self.case("x")
                    return self.case("y", extra=other)
                def factory(*args) -> LitmusTest:
                    return args[0].case("z", lambda: None)
        """
        assert _lines(source) == []


class TestMethodReferenceAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:26:44: {CODE} ")
        assert lines[-1] == "Found 1 violation."
