"""``litmus/unannotated-interface`` keeps authored suite signatures explicit."""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.unannotated_interface import UnannotatedInterfaceRule

CODE = "litmus/unannotated-interface"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(UnannotatedInterfaceRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestUnannotatedInterfaceFire:
    def test_every_parameter_shape_requires_an_annotation(self) -> None:
        source = """
            class Suite(LitmusTest):
                def helper(self, left, /, right, *items, named, **options) -> None:
                    pass
        """
        violations = _check(source)

        assert [(violation.line, violation.code) for violation in violations] == [
            (3, CODE),
            (3, CODE),
            (3, CODE),
            (3, CODE),
            (3, CODE),
        ]
        assert {violation.message.split("'")[1] for violation in violations} == {
            "left",
            "right",
            "items",
            "named",
            "options",
        }

    def test_missing_return_annotation(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils):
                    return {}
        """
        violations = _check(source)

        assert [(violation.line, violation.column, violation.code) for violation in violations] == [
            (3, 5, CODE)
        ]
        assert violations[0].message == "configure requires a return type annotation"

    def test_nested_function_inside_a_suite_is_authored_code(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, object]:
                    async def ready(attempt):
                        return True
                    return {}
        """
        assert _lines(source) == [4, 4]

    def test_test_method_without_return_annotation_names_verdict_contract(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("probe", self._probe)
                async def _probe(self, utils: TestUtils):
                    return PASS
        """
        violations = _check(source)

        assert _lines(source) == [5]
        assert violations[0].message == "test method _probe must declare -> Verdict"

    def test_test_method_with_a_different_return_type_is_rejected(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(this) -> LitmusTest:
                    return this.test(name="probe", fn=this._probe)
                async def _probe(this, utils: TestUtils) -> object:
                    return PASS
        """
        assert _lines(source) == [5]

    def test_static_method_first_parameter_is_not_an_implicit_receiver(self) -> None:
        source = """
            class Suite(LitmusTest):
                @staticmethod
                def normalize(value) -> str:
                    return str(value)
        """
        assert _lines(source) == [4]


class TestUnannotatedInterfaceStayQuiet:
    def test_fully_annotated_suite_and_test_method(self) -> None:
        source = """
            class Suite(LitmusTest):
                def __init__(self, *, name: str) -> None:
                    super().__init__(name=name)
                async def configure(self, utils: TestUtils) -> dict[str, object]:
                    async def ready(attempt: int) -> bool:
                        return attempt > 0
                    return {"ready": await ready(1)}
                def build_suite(self) -> LitmusTest:
                    return self.test("probe", self._probe)
                async def _probe(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["ready"]:
                        return PASS
                    raise TestFailure("not ready")
        """
        assert _lines(source) == []

    def test_module_functions_and_plain_classes_are_out_of_scope(self) -> None:
        source = """
            def helper(value):
                return value
            class Plain:
                def method(self, value):
                    return value
            class Suite(LitmusTest):
                class Detail:
                    def render(self, value):
                        return value
                def build_suite(self) -> LitmusTest:
                    return self.test("probe", self._probe)
                async def _probe(self, utils: TestUtils) -> Verdict:
                    return PASS
        """
        assert _lines(source) == []

    def test_bare_qualified_and_forward_verdict_annotations_are_accepted(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.parallel("claims", [
                        self.test("bare", self._bare),
                        self.test("qualified", self._qualified),
                        self.test("forward", self._forward),
                    ])
                async def _bare(self, utils: TestUtils) -> Verdict:
                    return PASS
                async def _qualified(self, utils: TestUtils) -> litmus.Verdict:
                    return PASS
                async def _forward(self, utils: TestUtils) -> "Verdict":
                    return PASS
        """
        assert _lines(source) == []


class TestUnannotatedInterfaceAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:37:32: {CODE} ")
        assert lines[-1] == "Found 1 violation."
