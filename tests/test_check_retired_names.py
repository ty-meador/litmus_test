"""``litmus/retired-names``: ``self.case`` and ``self.group`` name the replacement.

Issue #110 removed both names without a deprecation shim. The rule exists so a suite
written against the old vocabulary fails the gate with a message that says what to
write, rather than with an ``AttributeError`` at composition time.
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.retired_names import RetiredNamesRule

CODE = "litmus/retired-names"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(RetiredNamesRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestRetiredNamesFire:
    def test_case_names_test_as_the_replacement(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.case("x", self._x)
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 16, CODE)]
        assert "case() was retired" in violations[0].message
        assert "test()" in violations[0].message

    def test_group_names_parallel_as_the_replacement(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.group("x", [self.test("y", self._y)])
        """
        violations = _check(source)

        assert [(v.line, v.column) for v in violations] == [(4, 16)]
        assert "group() was retired" in violations[0].message
        assert "parallel()" in violations[0].message

    def test_nested_and_keyword_spellings_are_all_caught(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.sequence(
                        self.name,
                        [
                            self.case(name="a", fn=self._a),
                            self.group("b", [self.case("c", self._c)]),
                        ],
                    )
        """
        assert _lines(source) == [7, 8, 8]

    def test_a_renamed_receiver_still_counts(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(this) -> LitmusTest:
                    return this.case("x", this._x)
        """
        assert _lines(source) == [4]

    def test_transitive_subclasses_are_covered(self) -> None:
        source = """
            class Base(LitmusTest):
                pass

            class Derived(Base):
                def build_suite(self) -> LitmusTest:
                    return self.group("x", [])
        """
        assert _lines(source) == [7]


class TestRetiredNamesStaySilent:
    def test_the_live_vocabulary_is_clean(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.sequence(
                        self.name,
                        [
                            self.test("a", self._a),
                            self.parallel("b", [self.test("c", self._c)]),
                        ],
                    )
        """
        assert _lines(source) == []

    def test_regex_match_group_is_not_a_composition(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _version(self, utils: TestUtils) -> Verdict:
                    match = re.search(r"v(\\d+)", utils.ctx["banner"])
                    if match.group(1) == "2":
                        return PASS
                    raise TestFailure("version", "2", match.group(1))
        """
        assert _lines(source) == []

    def test_other_receivers_and_class_bodies_are_ignored(self) -> None:
        source = """
            class Suite(LitmusTest):
                dangling = object.case("x", None)
                def build_suite(self) -> LitmusTest:
                    return other.group("x", [])
                def factory(*args) -> LitmusTest:
                    return args[0].case("z", None)
        """
        assert _lines(source) == []

    def test_non_litmus_classes_are_ignored(self) -> None:
        source = """
            class Plain:
                def build_suite(self):
                    return self.case("x", self._x)
        """
        assert _lines(source) == []


class TestRetiredNamesAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:27:16: {CODE} ")
        assert lines[-1] == "Found 1 violation."
