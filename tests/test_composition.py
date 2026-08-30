"""Composition factories and the serial semantics they expose to authored suites."""

from collections.abc import Iterator
from typing import cast

import pytest

from litmus import (
    PASS,
    ContractViolation,
    FrameworkContractViolation,
    LitmusTest,
    TestFailure,
    Verdict,
)
from litmus.lifecycle import Outcome, RunAbort, Slot, State


class StubUtils:
    def __init__(self) -> None:
        self.ctx: dict[str, object] = {}


class _NonTerminalChild:
    async def _run_lifecycle(self, _utils: object, _run_abort: RunAbort) -> Outcome:
        return Outcome(Slot.CONFIGURE, "corrupt child", None)


class _InconsistentChildren:
    def __iter__(self) -> Iterator[LitmusTest]:
        return iter(())

    def __len__(self) -> int:
        return 1


class TestCompositionFactories:
    async def test_case_sequence_and_group_compose_with_bound_methods(self) -> None:
        calls: list[tuple[str, LitmusTest]] = []

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    self.name,
                    [
                        self.case("first claim", self._first_claim),
                        self.group(
                            "independent claims",
                            [self.case("second claim", self._second_claim)],
                        ),
                    ],
                )

            async def _first_claim(self, utils: object) -> Verdict:
                calls.append(("first", self))
                return PASS

            async def _second_claim(self, utils: object) -> Verdict:
                calls.append(("second", self))
                return PASS

        suite = Suite(name="rack")

        outcome = await suite.main(StubUtils())

        assert outcome.state is State.PASS
        assert calls == [("first", suite), ("second", suite)]

    def test_composites_require_at_least_one_child(self) -> None:
        suite = LitmusTest(name="rack")

        with pytest.raises(ContractViolation, match=r"sequence.*at least one child"):
            suite.sequence("sequence", [])
        with pytest.raises(ContractViolation, match=r"group.*at least one child"):
            suite.group("group", [])

    async def test_internal_nodes_report_direct_build_as_framework_bugs(self) -> None:
        async def passes(_utils: object) -> Verdict:
            return PASS

        suite = LitmusTest(name="rack")
        case = suite.case("case", passes)
        nodes = (
            (case, "an internal case has no child suite"),
            (suite.group("group", [case]), "an internal group has no child suite"),
            (
                suite.sequence("sequence", [case]),
                "an internal sequence has no child suite",
            ),
        )

        for node, detail in nodes:
            with pytest.raises(FrameworkContractViolation) as raised:
                node.build_suite()
            assert raised.value.node == node.name
            assert raised.value.detail == detail

    async def test_sequence_reports_a_nonterminal_child_as_a_framework_bug(self) -> None:
        sequence = LitmusTest(name="dependent claims")
        child = cast("LitmusTest", _NonTerminalChild())

        with pytest.raises(FrameworkContractViolation) as raised:
            await sequence._run_sequence(StubUtils(), RunAbort(), (child,))

        assert raised.value.node == sequence.name
        assert raised.value.detail == "a sequence child settled without a terminal state"

    async def test_sequence_reports_an_incomplete_pass_check_as_a_framework_bug(
        self,
    ) -> None:
        sequence = LitmusTest(name="dependent claims")
        children = cast("tuple[LitmusTest, ...]", _InconsistentChildren())

        with pytest.raises(FrameworkContractViolation) as raised:
            await sequence._run_sequence(StubUtils(), RunAbort(), children)

        assert raised.value.node == sequence.name
        assert raised.value.detail == "a sequence reached no explicit terminal verdict"


class TestSequenceExecution:
    async def test_sequence_bails_at_the_first_failing_child(self) -> None:
        calls: list[str] = []

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    "dependent claims",
                    [
                        self.case("first claim", self._first_claim),
                        self.case("failing claim", self._failing_claim),
                        self.case("unreachable claim", self._unreachable_claim),
                    ],
                )

            async def _first_claim(self, utils: object) -> Verdict:
                calls.append("first")
                return PASS

            async def _failing_claim(self, utils: object) -> Verdict:
                calls.append("failing")
                raise TestFailure("dependent precondition failed")

            async def _unreachable_claim(self, utils: object) -> Verdict:
                calls.append("unreachable")
                return PASS

        outcome = await Suite(name="rack").main(StubUtils())
        children = cast("tuple[Outcome, ...]", outcome.value)

        assert calls == ["first", "failing"]
        assert outcome.state is State.FAIL
        assert [child.state for child in children] == [State.PASS, State.FAIL]

    async def test_sequence_passes_only_after_every_child_passes(self) -> None:
        calls: list[str] = []

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    "dependent claims",
                    [
                        self.case("first claim", self._first_claim),
                        self.case("second claim", self._second_claim),
                    ],
                )

            async def _first_claim(self, utils: object) -> Verdict:
                calls.append("first")
                return PASS

            async def _second_claim(self, utils: object) -> Verdict:
                calls.append("second")
                return PASS

        outcome = await Suite(name="rack").main(StubUtils())
        children = cast("tuple[Outcome, ...]", outcome.value)

        assert calls == ["first", "second"]
        assert outcome.state is State.PASS
        assert len(children) == 2
        assert all(child.state is State.PASS for child in children)
