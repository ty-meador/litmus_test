"""Structured concurrency and verdict aggregation for internal test groups."""

import asyncio
from typing import cast

from litmus import (
    PASS,
    AfterEachContext,
    BeforeEachContext,
    ExternalIntegrationFailure,
    LitmusTest,
    MissingVerdict,
    RunResult,
    TestFailure,
    TestUtils,
    Verdict,
)
from litmus.lifecycle import State


class TestGroupStructuredConcurrency:
    async def test_one_child_failing_leaves_healthy_siblings_running(self) -> None:
        healthy_started = asyncio.Event()
        healthy_completed = asyncio.Event()

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.group(
                    "independent claims",
                    [
                        self.case("failing claim", self._failing_claim),
                        self.case("healthy claim", self._healthy_claim),
                    ],
                )

            async def _failing_claim(self, utils: object) -> Verdict:
                await healthy_started.wait()
                raise TestFailure("firmware revision mismatch")

            async def _healthy_claim(self, utils: object) -> Verdict:
                healthy_started.set()
                await asyncio.sleep(0)
                healthy_completed.set()
                return PASS

        outcome = await Suite(name="rack").main(TestUtils())
        children = cast("tuple[RunResult, ...]", outcome.value)

        assert healthy_completed.is_set()
        assert outcome.state is State.FAIL
        assert [child.state for child in children] == [State.FAIL, State.PASS]

    async def test_group_passes_when_every_child_passes(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.group(
                    "independent claims",
                    [
                        self.case("first claim", self._claim),
                        self.case("second claim", self._claim),
                    ],
                )

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        outcome = await Suite(name="rack").main(TestUtils())

        assert outcome.state is State.PASS

    async def test_node_local_abort_does_not_cancel_siblings(self) -> None:
        sibling_completed = asyncio.Event()

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.group(
                    "independent claims",
                    [
                        self.case("missing verdict", self._missing_verdict),
                        self.case("healthy claim", self._healthy_claim),
                    ],
                )

            async def _missing_verdict(self, utils: object) -> Verdict:
                return cast("Verdict", None)

            async def _healthy_claim(self, utils: object) -> Verdict:
                await asyncio.sleep(0)
                sibling_completed.set()
                return PASS

        outcome = await Suite(name="rack").main(TestUtils())
        children = cast("tuple[RunResult, ...]", outcome.value)

        assert sibling_completed.is_set()
        assert outcome.state is State.ABORTED
        assert [child.state for child in children] == [State.ABORTED, State.PASS]
        assert isinstance(children[0].failure, MissingVerdict)
        assert children[1].failure is None

    async def test_harness_abort_cancels_group_and_stays_class_intact_through_ancestors(
        self,
    ) -> None:
        sibling_started = asyncio.Event()
        sibling_cancelled = asyncio.Event()
        later_sequence_node_ran = asyncio.Event()
        cause = RuntimeError("shop-floor database refused the record")

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    "dependent stages",
                    [
                        self.group(
                            "independent claims",
                            [
                                self.case("long-running claim", self._long_running_claim),
                                self.case("integration halt", self._integration_halt),
                            ],
                        ),
                        self.case("later claim", self._later_claim),
                    ],
                )

            async def _long_running_claim(self, utils: object) -> Verdict:
                sibling_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    sibling_cancelled.set()
                return PASS

            async def _integration_halt(self, utils: object) -> Verdict:
                return PASS

            async def _later_claim(self, utils: object) -> Verdict:
                later_sequence_node_ran.set()
                return PASS

        async def before_each(ctx: BeforeEachContext) -> None:
            if ctx.name == "integration halt":
                await sibling_started.wait()
                raise cause

        async def after_each(ctx: AfterEachContext) -> None:
            del ctx

        outcome = await Suite(name="rack").run(
            before_each=before_each,
            after_each=after_each,
        )
        sequence_children = cast("tuple[RunResult, ...]", outcome.value)
        group = sequence_children[0]
        group_children = cast("tuple[RunResult, ...]", group.value)

        assert outcome.state is State.ABORTED
        assert isinstance(outcome.failure, ExternalIntegrationFailure)
        assert outcome.failure.cause is cause
        assert group.failure is outcome.failure
        assert all(child.failure is outcome.failure for child in group_children)
        assert sibling_cancelled.is_set()
        assert not later_sequence_node_ran.is_set()
