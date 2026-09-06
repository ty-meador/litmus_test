"""``litmus/evidence-fulfilled``: every ``return PASS({...})`` carries what ``@evidence``
declared (issue #118).

The declaration is the method's return signature. A bare ``return PASS`` in a decorated
method, or a literal payload missing a declared key, is flagged where it is written. A
payload the rule cannot read is left to tier 1.
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.evidence_fulfilled import EvidenceFulfilledRule

CODE = "litmus/evidence-fulfilled"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(EvidenceFulfilledRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestEvidenceFulfilledFires:
    def test_bare_pass_in_a_declared_method(self) -> None:
        source = """
            class Suite(LitmusTest):
                @evidence("reading_v", "ripple_mv")
                async def _claim(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["ok"]:
                        return PASS
                    raise TestFailure("no")
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(6, 13, CODE)]
        assert "@evidence declares 'reading_v', 'ripple_mv' but this PASS carries nothing" in (
            violations[0].message
        )

    def test_each_missing_key_is_one_violation(self) -> None:
        source = """
            class Suite(LitmusTest):
                @evidence("a", "b", "c")
                async def _claim(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["ok"]:
                        return PASS({"b": 1, "extra": 2})
                    return litmus.PASS(dict(c=3))
        """
        violations = _check(source)

        assert [(v.line, v.message.split(" is declared")[0]) for v in violations] == [
            (6, "'a'"),
            (6, "'c'"),
            (7, "'a'"),
            (7, "'b'"),
        ]
        assert "add it to the payload" in violations[0].message

    def test_a_typed_dict_declaration_is_read_from_the_module(self) -> None:
        source = """
            class _Base(TypedDict):
                reading_v: float
            class _Shape(_Base):
                ripple_mv: int
            class Suite(LitmusTest):
                @evidence(_Shape)
                async def _claim(self, utils: TestUtils) -> Verdict[_Shape]:
                    if utils.ctx["ok"]:
                        return PASS({"reading_v": 1.0})
                    raise TestFailure("no")
        """
        violations = _check(source)

        assert [v.line for v in violations] == [10]
        assert "'ripple_mv'" in violations[0].message

    def test_returns_inside_conditionals_and_loops_are_checked(self) -> None:
        source = """
            class Suite(LitmusTest):
                @evidence("k")
                async def _claim(self, utils: TestUtils) -> Verdict:
                    for sample in utils.ctx["samples"]:
                        if sample:
                            return PASS
                    with lock:
                        return PASS({})
        """
        assert _lines(source) == [7, 9]


class TestEvidenceFulfilledStaysQuiet:
    def test_a_fulfilled_declaration_in_both_spellings(self) -> None:
        source = """
            class _Shape(TypedDict):
                reading_v: float
            class Suite(LitmusTest):
                @evidence("reading_v")
                async def _claim(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["ok"]:
                        return PASS({"reading_v": 1.0, "extra": True})
                    raise TestFailure("no", evidence={"reading_v": 0.0})
                @litmus.evidence(_Shape)
                async def _typed(self, utils: TestUtils) -> Verdict[_Shape]:
                    if utils.ctx["ok"]:
                        return PASS(dict(reading_v=1.0))
                    raise TestFailure("no")
        """
        assert _lines(source) == []

    def test_undeclared_methods_and_non_suite_classes_are_ignored(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return PASS
                @other("k")
                async def _decorated(self, utils: TestUtils) -> Verdict:
                    return PASS
            class Helper:
                @evidence("k")
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return PASS
        """
        assert _lines(source) == []

    def test_unreadable_payloads_and_declarations_are_left_to_tier_one(self) -> None:
        source = """
            class _Partial(TypedDict, total=False):
                k: int
            class _Empty(TypedDict):
                pass
            class Suite(LitmusTest):
                @evidence("k")
                async def _local(self, utils: TestUtils) -> Verdict:
                    payload = {"k": 1}
                    return PASS(payload)
                @evidence("k")
                async def _spread(self, utils: TestUtils) -> Verdict:
                    return PASS({**base})
                @evidence("k")
                async def _splat(self, utils: TestUtils) -> Verdict:
                    return PASS(dict(**base))
                @evidence("k")
                async def _odd_call(self, utils: TestUtils) -> Verdict:
                    return PASS({"k": 1}, extra=True)
                @evidence(KEYS)
                async def _named(self, utils: TestUtils) -> Verdict:
                    return PASS
                @evidence(_Partial)
                async def _partial(self, utils: TestUtils) -> Verdict:
                    return PASS
                @evidence(_Empty)
                async def _empty(self, utils: TestUtils) -> Verdict:
                    return PASS
                @evidence(Imported)
                async def _imported(self, utils: TestUtils) -> Verdict:
                    return PASS
                @evidence(name="k")
                async def _keyword(self, utils: TestUtils) -> Verdict:
                    return PASS
                @evidence()
                async def _nothing(self, utils: TestUtils) -> Verdict:
                    return PASS
                @evidence("k", 1)
                async def _mixed(self, utils: TestUtils) -> Verdict:
                    return PASS
                @evidence
                async def _uncalled(self, utils: TestUtils) -> Verdict:
                    return PASS
        """
        assert _lines(source) == []

    def test_returns_in_nested_functions_belong_to_them(self) -> None:
        source = """
            class Suite(LitmusTest):
                @evidence("k")
                async def _claim(self, utils: TestUtils) -> Verdict:
                    async def probe() -> object:
                        return PASS
                    helper = lambda: PASS
                    class Inner:
                        def go(self):
                            return PASS
                    if await probe():
                        return PASS({"k": 1})
                    raise TestFailure("no")
        """
        assert _lines(source) == []


class TestEvidenceFulfilledAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:56:13: {CODE} ")
        assert lines[-1] == "Found 1 violation."
