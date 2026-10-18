"""``litmus/shell-in-configure``: a route is declared in ``configure()``, nowhere else.

The cases carry the ``useShell``-outside-``configure`` half of the legacy language
server's ``hook-outside-constructor`` suite, which the port left behind until the
runtime contract it mirrors existed (issue #76).
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.shell_in_configure import ShellInConfigureRule

CODE = "litmus/shell-in-configure"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(ShellInConfigureRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestShellInConfigureFires:
    def test_declaration_inside_a_test_method(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    await utils.use_shell(host="dut", username="root")
                    if utils.ctx["ok"]:
                        return PASS
                    raise TestFailure("no")
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 15, CODE)]
        assert "use_shell declared a route outside configure()" in violations[0].message

    def test_policy_only_declaration_inside_cleanup(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def cleanup(self, utils: TestUtils) -> None:
                    await utils.use_shell(inherit=False)
        """
        assert _lines(source) == [4]

    def test_declaration_inside_a_survey(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _snapshot(self, utils: TestUtils) -> object:
                    shell = await utils.use_shell(propagate=False)
                    return shell
        """
        assert _lines(source) == [4]

    def test_declaration_from_a_helper_called_by_configure(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    await self._open(utils)
                    return {}
                async def _open(self, utils: TestUtils) -> None:
                    await utils.use_shell(host="dut", username="root")
        """
        assert _lines(source) == [7]

    def test_closure_defined_in_configure_is_not_configure(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    async def later() -> None:
                        await utils.use_shell(host="dut", username="root")
                    self._later = later
                    return {}
        """
        assert _lines(source) == [5]

    def test_receiver_name_does_not_matter(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, u: TestUtils) -> Verdict:
                    await u.use_shell(inherit=False)
                    return PASS
        """
        assert _lines(source) == [4]

    def test_positional_argument_is_still_a_declaration(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    await utils.use_shell(jump)
                    return PASS
        """
        assert _lines(source) == [4]

    def test_use_serial_is_the_same_declaration(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    await utils.use_serial(device="/dev/ttyUSB0")
                    return PASS
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 15, CODE)]
        assert violations[0].message.startswith("use_serial declared a route outside configure()")
        assert "call use_serial() with no arguments" in violations[0].message

    def test_transitive_subclasses_are_covered(self) -> None:
        source = """
            class Base(LitmusTest): ...
            class Suite(Base):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    await utils.use_shell(host="dut", username="root")
                    return PASS
        """
        assert _lines(source) == [5]


class TestShellInConfigureStaysQuiet:
    def test_declarations_inside_configure_including_a_chain(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    jump = await utils.use_shell(host="jump", username="root")
                    await utils.use_shell(host="dut", username="root", via=jump)
                    await utils.use_shell(inherit=False, propagate=False)
                    return {}
        """
        assert _lines(source) == []

    def test_bare_read_anywhere(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    shell = await utils.use_shell()
                    if shell.ssh is None:
                        return PASS
                    raise TestFailure("routed")
                async def cleanup(self, utils: TestUtils) -> None:
                    await utils.use_shell()
        """
        assert _lines(source) == []

    def test_get_shell_is_not_a_declaration(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    extra = await utils.get_shell(host="aux", username="root")
                    if extra:
                        return PASS
                    raise TestFailure("no aux")
        """
        assert _lines(source) == []

    def test_use_serial_in_configure_and_its_read_and_aux_forms_anywhere(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    await utils.use_serial(device="/dev/ttyUSB0", baud=9600)
                    return {}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    console = await utils.use_serial()
                    aux = await utils.get_serial(device="/dev/ttyUSB1")
                    if console and aux:
                        return PASS
                    raise TestFailure("no console")
        """
        assert _lines(source) == []

    def test_classes_that_are_not_litmus_tests(self) -> None:
        source = """
            class Plain:
                async def method(self, utils: TestUtils) -> None:
                    await utils.use_shell(host="dut", username="root")
        """
        assert _lines(source) == []

    def test_calls_at_module_or_class_scope_have_no_method(self) -> None:
        source = """
            utils = object()
            utils.use_shell(host="dut", username="root")
            class Suite(LitmusTest):
                declared = utils.use_shell(inherit=False)
        """
        assert _lines(source) == []


class TestShellInConfigureAtTheCommandLine:
    def test_seeded_fixture_is_caught(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:60:15: {CODE} ")
        assert lines[-1] == "Found 1 violation."
