"""``litmus/immutable-self``: ``self.<attr>`` is written only inside ``__init__``.

The cases mirror the legacy language server's ``this-mutation`` suite, translated to
Python's write forms (augmented and annotated assignment, ``del``, loop and context
targets, tuple unpacking).
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.immutable_self import ImmutableSelfRule

CODE = "litmus/immutable-self"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(ImmutableSelfRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestImmutableSelfFires:
    def test_assignment_inside_configure(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    self._ip = "10.0.0.1"
                    return {}
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 9, CODE)]
        assert "self._ip written outside __init__" in violations[0].message

    def test_assignment_inside_a_test_method_and_a_private_helper(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    self._count = 1
                    if self._count == 1:
                        return PASS
                    raise TestFailure("no")
                def _helper(self) -> None:
                    self._flag = True
        """
        assert _lines(source) == [4, 9]

    def test_augmented_and_annotated_assignment(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    self._count += 1
                    self._label: str = "x"
                    return PASS
        """
        assert _lines(source) == [4, 5]

    def test_delete(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def cleanup(self, utils: TestUtils) -> None:
                    del self._handle
        """
        assert _lines(source) == [4]

    def test_tuple_loop_and_context_targets(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    self._a, (self._b, *rest) = utils.ctx["pair"], [1, 2]
                    for self._i in range(3):
                        pass
                    async with utils.exec("x") as self._result:
                        pass
                    return PASS
        """
        assert _lines(source) == [4, 4, 5, 7]

    def test_nested_def_inside_init_is_not_the_constructor(self) -> None:
        source = """
            class Suite(LitmusTest):
                def __init__(self, *, name: str) -> None:
                    super().__init__(name=name)
                    def later() -> None:
                        self._late = True
                    self._hook = later
        """
        assert _lines(source) == [6]

    def test_receiver_is_the_first_parameter_whatever_its_name(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(this, utils: TestUtils) -> Verdict:
                    this._x = 1
                    self._y = 2
                    return PASS
        """
        assert _lines(source) == [4]

    def test_transitive_subclasses_are_covered(self) -> None:
        source = """
            class Base(LitmusTest): ...
            class Suite(Base):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    self._x = 1
                    return {}
        """
        assert _lines(source) == [5]


class TestImmutableSelfStaysQuiet:
    def test_assignments_in_the_constructor(self) -> None:
        source = """
            class Suite(LitmusTest):
                def __init__(self, *, name: str, mac: str) -> None:
                    super().__init__(name=name)
                    self._mac = mac
                    self._retries: int = 3
                    self._retries += 1
                    self.use_survey(self._snapshot)
        """
        assert _lines(source) == []

    def test_nested_object_mutation_and_subscripts(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    self._children.append(1)
                    self._config.host = "x"
                    self._table["k"] = 1
                    utils.ctx["ip"] = "10.0.0.1"
                    utils.ctx.update(ip="10.0.0.2")
                    return PASS
        """
        assert _lines(source) == []

    def test_framework_bypass_is_not_a_lint_finding(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    object.__setattr__(self, "_x", 1)
                    return PASS
        """
        assert _lines(source) == []

    def test_classes_that_are_not_litmus_tests(self) -> None:
        source = """
            class Plain:
                def method(self) -> None:
                    self.x = 1
            class Cache(dict):
                def touch(self) -> None:
                    self.hits += 1
        """
        assert _lines(source) == []

    def test_locals_and_static_methods(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    result = 1
                    result += 1
                    return PASS
                @staticmethod
                def helper(config: object) -> None:
                    config.x = 1
        """
        assert _lines(source) == []


class TestImmutableSelfAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:21:9: {CODE} ")
        assert lines[-1] == "Found 1 violation."
