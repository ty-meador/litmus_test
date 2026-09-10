"""``litmus/measurement-in-node``: a reading is recorded where a node is running.

The static half of the outside-a-node guard in ``docs/measurements.md`` (*Tiers*): the
runtime refuses the call; the rule names the slot so the developer moves it first.
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.measurement_in_node import MeasurementInNodeRule

CODE = "litmus/measurement-in-node"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(MeasurementInNodeRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestMeasurementInNodeFires:
    def test_inside_cleanup(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def cleanup(self, utils: TestUtils) -> None:
                    utils.measure("rail_v", 0.0)
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 9, CODE)]
        assert "measure() called in cleanup" in violations[0].message

    def test_inside_a_registered_survey_or_audit(self) -> None:
        source = """
            class Suite(LitmusTest):
                def __init__(self, *, name: str) -> None:
                    super().__init__(name=name)
                    self.use_survey(self._snapshot)
                    self.use_audit(interval=1.0, fn=self._watch)
                async def _snapshot(self, utils: TestUtils) -> object:
                    return utils.measure("temp_c", 40.0)
                async def _watch(self, utils: TestUtils) -> object:
                    return measure("temp_c", 41.0)
        """
        violations = _check(source)

        assert [v.line for v in violations] == [8, 10]
        assert all("in a survey or audit hook" in v.message for v in violations)

    def test_inside_a_function_named_like_a_run_hook(self) -> None:
        source = """
            async def before_each(context: BeforeEachContext) -> None:
                measure("hook_ms", 1.0)
            class Suite(LitmusTest):
                async def after_each(self, context: AfterEachContext) -> None:
                    measure("hook_ms", 1.0)
        """
        violations = _check(source)

        assert [v.line for v in violations] == [3, 6]
        assert all("in a before_each/after_each hook" in v.message for v in violations)

    def test_inside_a_function_passed_as_a_run_hook(self) -> None:
        source = """
            async def started(context: BeforeEachContext) -> None:
                measure("hook_ms", 1.0)
            async def main() -> None:
                await Suite(name="dut").run(
                    before_each=started,
                    after_each=lambda context: measure("hook_ms", 2.0),
                )
        """
        assert _lines(source) == [3, 7]

    def test_at_module_and_class_scope(self) -> None:
        source = """
            measure("boot_v", 1.0)
            class Suite(LitmusTest):
                seeded = measure("boot_v", 1.0)
        """
        violations = _check(source)

        assert [v.line for v in violations] == [2, 4]
        assert all("outside any function" in v.message for v in violations)

    def test_a_closure_inside_cleanup_or_a_hook_is_still_there(self) -> None:
        source = """
            async def before_each(context: BeforeEachContext) -> None:
                def later() -> None:
                    measure("hook_ms", 1.0)
                later()
            class Suite(LitmusTest):
                async def cleanup(self, utils: TestUtils) -> None:
                    def restore() -> None:
                        utils.measure("rail_v", 0.0)
                    restore()
        """
        assert _lines(source) == [4, 9]

    def test_receiver_name_does_not_matter(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def cleanup(self, u: TestUtils) -> None:
                    u.measure("rail_v", 0.0)
        """
        assert _lines(source) == [4]


class TestMeasurementInNodeStaysQuiet:
    def test_configure_and_test_methods_are_nodes(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    utils.measure("boot_s", 1.0)
                    return {}
                async def _rail(self, utils: TestUtils) -> Verdict:
                    measure("rail_v", 3.3)
                    if utils.ctx["ok"]:
                        return PASS
                    raise TestFailure("no")
        """
        assert _lines(source) == []

    def test_helpers_are_left_to_the_runtime_guard(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def cleanup(self, utils: TestUtils) -> None:
                    await self._restore(utils)
                async def _restore(self, utils: TestUtils) -> None:
                    utils.measure("rail_v", 0.0)
        """
        assert _lines(source) == []

    def test_cleanup_on_a_class_that_is_not_a_litmus_test(self) -> None:
        source = """
            class Plain:
                async def cleanup(self, utils: TestUtils) -> None:
                    utils.measure("rail_v", 0.0)
        """
        assert _lines(source) == []

    def test_an_unregistered_method_is_not_a_hook(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _snapshot(self, utils: TestUtils) -> object:
                    return utils.measure("temp_c", 40.0)
        """
        assert _lines(source) == []


class TestMeasurementInNodeAtTheCommandLine:
    def test_seeded_fixture_is_caught(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:72:5: {CODE} ")
        assert lines[-1] == "Found 1 violation."
