"""``litmus/constructor-hooks``: ``use_survey`` and ``use_audit`` live in ``__init__``.

The cases mirror the legacy language server's ``hook-outside-constructor`` suite for
the two registrars the port carries; the ``useShell``-outside-``configure`` half has no
runtime counterpart in the port and is not ported (see the rule's module docstring).
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.constructor_hooks import ConstructorHooksRule

CODE = "litmus/constructor-hooks"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(ConstructorHooksRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestConstructorHooksFire:
    def test_use_survey_inside_configure(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    self.use_survey(self._snapshot)
                    return {}
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 9, CODE)]
        assert "use_survey called outside __init__" in violations[0].message

    def test_use_audit_inside_a_test_method(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    self.use_audit(interval=1.0, fn=self._thermals)
                    if utils.ctx["ok"]:
                        return PASS
                    raise TestFailure("no")
        """
        assert _lines(source) == [4]

    def test_registration_from_a_private_helper_called_by_init(self) -> None:
        source = """
            class Suite(LitmusTest):
                def __init__(self, *, name: str) -> None:
                    super().__init__(name=name)
                    self._register()
                def _register(self) -> None:
                    self.use_survey(self._snapshot)
        """
        assert _lines(source) == [7]

    def test_closure_defined_in_init_is_not_the_constructor(self) -> None:
        source = """
            class Suite(LitmusTest):
                def __init__(self, *, name: str) -> None:
                    super().__init__(name=name)
                    def later() -> None:
                        self.use_audit(interval=1.0, fn=self._thermals)
                    self._later = later
        """
        assert _lines(source) == [6]

    def test_receiver_is_the_first_parameter_whatever_its_name(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(this, utils: TestUtils) -> dict[str, Any]:
                    this.use_survey(this._snapshot)
                    return {}
        """
        assert _lines(source) == [4]

    def test_transitive_subclasses_are_covered(self) -> None:
        source = """
            class Base(LitmusTest): ...
            class Suite(Base):
                async def cleanup(self, utils: TestUtils) -> None:
                    self.use_survey(self._snapshot)
        """
        assert _lines(source) == [5]


class TestConstructorHooksStayQuiet:
    def test_registration_in_the_constructor(self) -> None:
        source = """
            class Suite(LitmusTest):
                def __init__(self, *, name: str) -> None:
                    super().__init__(name=name)
                    self.use_survey(self._snapshot)
                    self.use_survey(self._dmesg)
                    self.use_audit(interval=5.0, fn=self._thermals)
        """
        assert _lines(source) == []

    def test_other_receivers_are_not_self(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    child = Child(name="c")
                    child.use_survey(child.snapshot)
                    self._registry.use_audit(interval=1.0, fn=self._thermals)
                    await utils.use_shell(host="x", username="y")
                    return {}
        """
        assert _lines(source) == []

    def test_classes_that_are_not_litmus_tests(self) -> None:
        source = """
            class Plain:
                def method(self) -> None:
                    self.use_survey(self.snapshot)
                    self.use_audit(interval=1.0, fn=self.thermals)
        """
        assert _lines(source) == []

    def test_calls_at_module_or_class_scope_have_no_receiver(self) -> None:
        source = """
            self = object()
            self.use_survey(None)
            class Suite(LitmusTest):
                registered = self.use_survey(None)
        """
        assert _lines(source) == []


class TestConstructorHooksAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:22:9: {CODE} ")
        assert lines[-1] == "Found 1 violation."

    def test_every_shipped_rule_finds_exactly_its_seed(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run([str(fixtures)])

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        codes = sorted(line.split(": ")[1].split()[0] for line in lines[:-1])
        assert codes == [
            "litmus/constructor-hooks",
            "litmus/gated-pass",
            "litmus/immutable-self",
            "litmus/pure-build-suite",
            "litmus/unannotated-interface",
        ]
        assert all("seeded_suite.py" in line for line in lines[:-1])
        assert lines[-1] == "Found 5 violations."
