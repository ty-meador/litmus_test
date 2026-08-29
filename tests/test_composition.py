"""Composition factories and the serial semantics they expose to authored suites."""

from typing import cast

import pytest

from litmus import PASS, ContractViolation, LitmusTest, TestFailure, Verdict
from litmus.lifecycle import Outcome, State


class StubUtils:
    def __init__(self) -> None:
        self.ctx: dict[str, object] = {}


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
