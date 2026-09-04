"""``litmus/pure-build-suite``: ``build_suite`` declares one composition and no logic."""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.pure_build_suite import PureBuildSuiteRule

CODE = "litmus/pure-build-suite"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(PureBuildSuiteRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestPureBuildSuiteFires:
    def test_assignment_before_the_return(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    children = [self.case("a", self._a)]
                    return self.sequence(self.name, children)
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 9, CODE)]
        assert "Assign statement" in violations[0].message

    def test_branching_between_compositions(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    if self._fast:
                        return self.case("quick", self._quick)
                    return self.group(self.name, [self.case("slow", self._slow)])
        """
        violations = _check(source)

        assert [v.line for v in violations] == [4]
        assert "If statement" in violations[0].message

    def test_a_second_top_level_return_is_flagged_on_its_own(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.case("a", self._a)
                    return self.case("b", self._b)
        """
        violations = _check(source)

        assert [v.line for v in violations] == [5]
        assert "more than one return" in violations[0].message

    def test_loop_building_children(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    for port in self._ports:
                        self._children.append(self.case(str(port), self._probe))
                    return self.group(self.name, self._children)
        """
        assert _lines(source) == [4]

    def test_conditional_expressions_inside_the_return(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.sequence(
                        self.name,
                        [
                            self.case("a", self._a if self._fast else self._slow),
                            self.case("b", self._b) if self._enabled else self.case("c", self._c),
                        ],
                    )
        """
        assert _lines(source) == [7, 8]

    def test_comprehensions_lambdas_and_boolean_logic_inside_the_return(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.group(
                        self.name,
                        [self.case(n, self._probe) for n in self._names if n and n != "x"],
                    )
        """
        kinds = [v.message.split(":")[1].split()[0] for v in _check(source)]

        assert kinds == ["ListComp", "BoolOp", "Compare"]

    def test_await_and_walrus_inside_the_return(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def build_suite(self) -> LitmusTest:
                    return self.case((label := await self._label()), self._a)
        """
        kinds = [v.message.split(":")[1].split()[0] for v in _check(source)]

        assert kinds == ["NamedExpr", "Await"]

    def test_docstring_is_allowed_but_a_second_expression_statement_is_not(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    \"\"\"Declare the claims.\"\"\"
                    print("building")
                    return self.case("a", self._a)
        """
        assert _lines(source) == [5]

    def test_transitive_subclasses_are_covered(self) -> None:
        source = """
            class Base(LitmusTest): ...
            class Suite(Base):
                def build_suite(self) -> LitmusTest:
                    x = 1
                    return self.case("a", self._a)
        """
        assert _lines(source) == [5]


class TestPureBuildSuiteStaysQuiet:
    def test_nested_factories_fstrings_and_child_constructors(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    \"\"\"Declare the composition.\"\"\"
                    return self.sequence(
                        self.name,
                        [
                            self.case(f"probe {self._host}", self._probe),
                            self.group("sensors", [self.case("t", self._t)]),
                            Child(name="child", via=self, ports=self._ports[1:]),
                            -self._weight,
                            self._names[0] + "-suffix",
                        ],
                    )
        """
        assert _lines(source) == []

    def test_only_litmus_classes_are_inspected(self) -> None:
        source = """
            class Builder:
                def build_suite(self):
                    if True:
                        x = 1
                    return x
            def build_suite():
                y = 2
                return y
        """
        assert _lines(source) == []

    def test_other_methods_may_hold_logic(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    if utils.env.get("FAST"):
                        return {"fast": True}
                    return {"fast": False}
                def build_suite(self) -> LitmusTest:
                    return self.case("a", self._a)
        """
        assert _lines(source) == []


class TestPureBuildSuiteAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:26:9: {CODE} ")
        assert lines[-1] == "Found 1 violation."
