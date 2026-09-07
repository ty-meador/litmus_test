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
    TestUtils,
    Verdict,
)
from litmus.lifecycle import AuditSignal, Outcome, RunAbort, Slot, State, TimeBudgetClock


class _NonTerminalChild:
    async def _run_lifecycle(
        self,
        _utils: TestUtils,
        _run_abort: RunAbort,
        *,
        audit_signal: AuditSignal | None = None,
        time_budget_clocks: tuple[TimeBudgetClock, ...] = (),
    ) -> Outcome:
        return Outcome(Slot.CONFIGURE, "corrupt child", None)


class _InconsistentChildren:
    def __iter__(self) -> Iterator[LitmusTest]:
        return iter(())

    def __len__(self) -> int:
        return 1


class TestCompositionFactories:
    async def test_test_sequence_and_parallel_compose_with_bound_methods(self) -> None:
        calls: list[tuple[str, LitmusTest]] = []

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    self.name,
                    [
                        self.test("first claim", self._first_claim),
                        self.parallel(
                            "independent claims",
                            [self.test("second claim", self._second_claim)],
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

        outcome = await suite.main(TestUtils())

        assert outcome.state is State.PASS
        assert calls == [("first", suite), ("second", suite)]

    def test_composites_require_at_least_one_child(self) -> None:
        suite = LitmusTest(name="rack")

        with pytest.raises(ContractViolation, match=r"sequence.*at least one child"):
            suite.sequence("sequence", [])
        with pytest.raises(ContractViolation, match=r"parallel node.*at least one child"):
            suite.parallel("parallel", [])

    async def test_leaves_enumerate_at_runtime_by_passing_the_varying_argument(self) -> None:
        stressed: list[int] = []

        class Compute(LitmusTest):
            def __init__(self, *, name: str, cpu_count: int) -> None:
                super().__init__(name=name)
                self._cpu_count = cpu_count

            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "stress",
                    [
                        self.test(f"cpu{i}_stress", self._cpu_stress, cpu=i)
                        for i in range(self._cpu_count)
                    ],
                )

            async def _cpu_stress(self, utils: object, *, cpu: int) -> Verdict:
                stressed.append(cpu)
                return PASS

        outcome = await Compute(name="compute", cpu_count=3).main(TestUtils())

        assert outcome.state is State.PASS
        leaves = outcome.summary_tree.children[0].children
        assert [leaf.name for leaf in leaves] == ["cpu0_stress", "cpu1_stress", "cpu2_stress"]
        assert all(leaf.state is State.PASS for leaf in leaves)
        assert sorted(stressed) == [0, 1, 2]

    async def test_positional_arguments_reach_the_method_after_utils(self) -> None:
        seen: list[tuple[str, float]] = []

        class Rails(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    "rails",
                    [
                        self.test("v12", self._rail, "12V", 0.5, fail_after=5.0),
                        self.test("v5", self._rail, "5V", limit=0.25),
                    ],
                )

            async def _rail(self, utils: object, rail: str, limit: float) -> Verdict:
                seen.append((rail, limit))
                return PASS

        outcome = await Rails(name="psu").main(TestUtils())

        assert outcome.state is State.PASS
        assert seen == [("12V", 0.5), ("5V", 0.25)]

    def test_arguments_that_do_not_fit_the_method_fail_at_composition(self) -> None:
        suite = LitmusTest(name="rack")

        async def probe(_utils: object, *, cpu: int) -> Verdict:
            del cpu
            return PASS

        with pytest.raises(
            ContractViolation, match=r"missing a required keyword-only argument: 'cpu'"
        ):
            suite.test("no cpu", probe)
        with pytest.raises(ContractViolation, match=r"unexpected keyword argument 'core'"):
            suite.test("wrong name", probe, cpu=0, core=0)
        with pytest.raises(ContractViolation, match=r"too many positional arguments"):
            suite.test("extra", probe, 1, 2)

    def test_budgets_are_the_leaf_s_and_never_reach_the_method(self) -> None:
        suite = LitmusTest(name="rack")

        async def clashes(_utils: object, *, fail_after: float) -> Verdict:
            del fail_after
            return PASS

        with pytest.raises(
            ContractViolation, match=r"missing a required keyword-only argument: 'fail_after'"
        ):
            suite.test("clash", clashes, fail_after=1.0)

    def test_test_rejects_a_function_that_is_not_a_coroutine_function(self) -> None:
        suite = LitmusTest(name="rack")

        def plain(_utils: object) -> Verdict:
            return PASS

        with pytest.raises(TypeError, match="must be defined with async def"):
            suite.test("plain", plain)  # pyright: ignore[reportArgumentType]
        with pytest.raises(TypeError, match="must be defined with async def"):
            suite.test("lambda", lambda _utils: PASS)  # pyright: ignore[reportArgumentType, reportUnknownLambdaType]

    def test_composed_nodes_own_a_timeline_unless_rolled_up(self) -> None:
        suite = LitmusTest(name="rack")

        async def claim(_utils: object) -> Verdict:
            return PASS

        child = suite.test("claim", claim)

        # Rolling a node's records into its parent is the opt-in; the default is a
        # timeline of its own, which is what the FT opens for that node.
        assert suite.parallel("parallel", [child]).discrete_log_file is True
        assert suite.sequence("sequence", [child]).discrete_log_file is True
        rolled = suite.parallel("rolled", [child], discrete_log_file=False)
        assert rolled.discrete_log_file is False

    async def test_internal_nodes_report_direct_build_as_framework_bugs(self) -> None:
        async def passes(_utils: object) -> Verdict:
            return PASS

        suite = LitmusTest(name="rack")
        leaf = suite.test("leaf", passes)
        escalate = "report it to the framework maintainers with the full log and reproduction steps"
        nodes = (
            (leaf, f"an internal test has no child suite; {escalate}"),
            (
                suite.parallel("parallel", [leaf]),
                f"an internal parallel node has no child suite; {escalate}",
            ),
            (
                suite.sequence("sequence", [leaf]),
                f"an internal sequence has no child suite; {escalate}",
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
            await sequence._run_sequence(TestUtils(), RunAbort(), (child,))

        assert raised.value.node == sequence.name
        assert raised.value.detail == (
            "a sequence child settled without a terminal state; "
            "report it to the framework maintainers with the full log and reproduction steps"
        )

    async def test_sequence_reports_an_incomplete_pass_check_as_a_framework_bug(
        self,
    ) -> None:
        sequence = LitmusTest(name="dependent claims")
        children = cast("tuple[LitmusTest, ...]", _InconsistentChildren())

        with pytest.raises(FrameworkContractViolation) as raised:
            await sequence._run_sequence(TestUtils(), RunAbort(), children)

        assert raised.value.node == sequence.name
        assert raised.value.detail == (
            "a sequence reached no explicit terminal verdict; "
            "report it to the framework maintainers with the full log and reproduction steps"
        )


class TestSequenceExecution:
    async def test_sequence_bails_at_the_first_failing_child(self) -> None:
        calls: list[str] = []

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    "dependent claims",
                    [
                        self.test("first claim", self._first_claim),
                        self.test("failing claim", self._failing_claim),
                        self.test("unreachable claim", self._unreachable_claim),
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

        outcome = await Suite(name="rack").main(TestUtils())
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
                        self.test("first claim", self._first_claim),
                        self.test("second claim", self._second_claim),
                    ],
                )

            async def _first_claim(self, utils: object) -> Verdict:
                calls.append("first")
                return PASS

            async def _second_claim(self, utils: object) -> Verdict:
                calls.append("second")
                return PASS

        outcome = await Suite(name="rack").main(TestUtils())
        children = cast("tuple[Outcome, ...]", outcome.value)

        assert calls == ["first", "second"]
        assert outcome.state is State.PASS
        assert len(children) == 2
        assert all(child.state is State.PASS for child in children)
