"""``litmus/measurement-name-unique``: one name records one reading per node.

The static half of the duplicate-name guard in ``docs/measurements.md`` (*Tiers*): the
runtime refuses the second call; the rule shows the developer both calls first.
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.measurement_name_unique import MeasurementNameUniqueRule

CODE = "litmus/measurement-name-unique"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(MeasurementNameUniqueRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestMeasurementNameUniqueFires:
    def test_same_literal_name_twice_in_a_test_method(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _rail(self, utils: TestUtils) -> Verdict:
                    utils.measure("rail_v", 3.3)
                    utils.measure("rail_v", 3.31, unit="V")
                    if utils.ctx["ok"]:
                        return PASS
                    raise TestFailure("no")
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(5, 9, CODE)]
        assert "measurement 'rail_v' is recorded twice in _rail" in violations[0].message

    def test_both_spellings_share_one_namespace(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _rail(self, utils: TestUtils) -> Verdict:
                    utils.measure("rail_v", 3.3)
                    measure("rail_v", 3.31)
                    return PASS
        """
        assert _lines(source) == [5]

    def test_name_keyword_is_the_same_name(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _rail(self, utils: TestUtils) -> Verdict:
                    utils.measure("rail_v", 3.3)
                    utils.measure(value=3.31, name="rail_v")
                    return PASS
        """
        assert _lines(source) == [5]

    def test_every_repeat_is_reported(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _rail(self, utils: TestUtils) -> Verdict:
                    utils.measure("rail_v", 3.3)
                    utils.measure("rail_v", 3.31)
                    utils.measure("rail_v", 3.32)
                    return PASS
        """
        assert _lines(source) == [5, 6]

    def test_configure_is_a_node_too(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    utils.measure("boot_s", 1.0)
                    utils.measure("boot_s", 1.5)
                    return {}
        """
        assert _lines(source) == [5]

    def test_a_closure_records_on_the_same_node(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _rail(self, utils: TestUtils) -> Verdict:
                    def again() -> None:
                        utils.measure("rail_v", 3.31)
                    utils.measure("rail_v", 3.3)
                    again()
                    return PASS
        """
        assert _lines(source) == [6]

    def test_receiver_name_does_not_matter(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _rail(self, u: TestUtils) -> Verdict:
                    u.measure("rail_v", 3.3)
                    u.measure("rail_v", 3.31)
                    return PASS
        """
        assert _lines(source) == [5]


class TestMeasurementNameUniqueStaysQuiet:
    def test_distinct_names(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _rail(self, utils: TestUtils) -> Verdict:
                    utils.measure("rail_v", 3.3)
                    utils.measure("rail_a", 0.2)
                    return PASS
        """
        assert _lines(source) == []

    def test_the_same_name_in_two_methods_is_two_nodes(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _rail_a(self, utils: TestUtils) -> Verdict:
                    utils.measure("rail_v", 3.3)
                    return PASS
                async def _rail_b(self, utils: TestUtils) -> Verdict:
                    utils.measure("rail_v", 3.3)
                    return PASS
        """
        assert _lines(source) == []

    def test_computed_names_are_not_compared(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _rail(self, utils: TestUtils) -> Verdict:
                    utils.measure(f"rail_{n}", 3.3)
                    utils.measure(f"rail_{n}", 3.31)
                    utils.measure(NAME, 1.0)
                    utils.measure(NAME, 2.0)
                    return PASS
        """
        assert _lines(source) == []

    def test_one_call_in_a_loop_is_one_call(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _rail(self, utils: TestUtils) -> Verdict:
                    for _ in range(3):
                        utils.measure("rail_v", 3.3)
                    return PASS
        """
        assert _lines(source) == []

    def test_classes_that_are_not_litmus_tests(self) -> None:
        source = """
            class Plain:
                def record(self, utils: TestUtils) -> None:
                    utils.measure("rail_v", 3.3)
                    utils.measure("rail_v", 3.31)
        """
        assert _lines(source) == []

    def test_other_methods_named_measure_need_a_string_name(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _rail(self, utils: TestUtils) -> Verdict:
                    ruler.measure()
                    ruler.measure()
                    return PASS
        """
        assert _lines(source) == []


class TestMeasurementNameUniqueAtTheCommandLine:
    def test_seeded_fixture_is_caught(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:65:9: {CODE} ")
        assert lines[-1] == "Found 1 violation."
