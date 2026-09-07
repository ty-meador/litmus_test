"""Tier 1 of the route window: a route is declared in ``configure()`` (issue #76).

``use_shell`` with a route-shaping argument decides where this node's ``utils.exec``
lands; the framework admits that call only while ``configure()`` is running, so the
``exec routing:`` line written when configure returns stays true for the node's whole
run. The bare ``use_shell()`` read is admitted from any slot.
"""

import asyncio
import logging

import pytest

from litmus import (
    PASS,
    ContractViolation,
    LitmusTest,
    State,
    TestFailure,
    TestUtils,
    Verdict,
)
from litmus.results import SummaryTreeNode

DETAIL = (
    "use_shell declared a route outside configure(); the exec route is fixed when "
    "configure() returns so the 'exec routing:' line stays true for the whole node; "
    "declare it there, or call use_shell() with no arguments to read the active route"
)


def _leaf(tree: SummaryTreeNode, index: int = 0) -> SummaryTreeNode:
    return tree.children[index]


def _routing_line(utils: TestUtils) -> str:
    return utils._routing.routing_line()  # pyright: ignore[reportPrivateUsage]


class TestDeclarationOutsideConfigureIsAContractViolation:
    async def test_from_a_test_method_lands_as_a_bug_naming_the_leaf(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("rail", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                shell = await utils.use_shell(inherit=False)
                if shell.ssh is None:
                    return PASS
                raise TestFailure("routed", "local", "ssh")

        result = await Suite(name="psu").run()

        assert result.state is State.FAIL
        failure = _leaf(result.summary_tree).failure
        assert isinstance(failure, ContractViolation)
        assert failure.is_bug
        assert failure.node == "rail"
        assert failure.fail_message == f"CONTRACT VIOLATION IN rail: {DETAIL}"

    async def test_from_an_audit_fails_the_node(self) -> None:
        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_audit(interval=0.01, fn=self._retarget)

            def build_suite(self) -> LitmusTest:
                return self.test("rail", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                await asyncio.sleep(0.2)
                if utils.exec_route == "local":
                    return PASS
                raise TestFailure("routed", "local", utils.exec_route)

            async def _retarget(self, utils: TestUtils) -> object:
                await utils.use_shell(host="dut", username="root")
                return None

        result = await Suite(name="psu").run()

        assert result.state is State.FAIL
        assert isinstance(result.failure, ContractViolation)
        assert result.failure.node == "psu"

    async def test_from_a_survey_is_refused_and_logged_without_moving_the_route(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        lines: list[str] = []

        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_survey(self._retarget)

            async def configure(self, utils: TestUtils) -> dict[str, object]:
                await utils.use_shell(inherit=False)
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("rail", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                lines.append(_routing_line(utils))
                if utils.exec_route == "local":
                    return PASS
                raise TestFailure("routed", "local", utils.exec_route)

            async def _retarget(self, utils: TestUtils) -> object:
                await utils.use_shell(propagate=False)
                return None

        with caplog.at_level(logging.DEBUG):
            result = await Suite(name="psu").run()

        assert result.state is State.PASS
        assert f"CONTRACT VIOLATION IN psu: {DETAIL}" in caplog.text
        # The survey's propagate=False never landed: the leaf still inherits.
        assert lines == ["local (inherited from parent)"]

    async def test_from_cleanup_is_refused_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("rail", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                if utils.exec_route == "local":
                    return PASS
                raise TestFailure("routed", "local", utils.exec_route)

            async def cleanup(self, utils: TestUtils) -> None:
                await utils.use_shell(inherit=False)

        with caplog.at_level(logging.DEBUG):
            result = await Suite(name="psu").run()

        assert result.state is State.PASS
        assert f"CONTRACT VIOLATION IN psu: {DETAIL}" in caplog.text

    async def test_an_unscoped_seed_refuses_before_the_router_is_touched(self) -> None:
        utils = TestUtils()
        before = _routing_line(utils)

        with pytest.raises(ContractViolation) as caught:
            await utils.use_shell(inherit=False, propagate=False)

        assert caught.value.node == "<unknown>"
        assert _routing_line(utils) == before


class TestReadsAndConfigureDeclarationsAreAdmitted:
    async def test_bare_read_from_a_test_method_returns_the_active_shell(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("rail", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                shell = await utils.use_shell()
                if shell.ssh is None:
                    return PASS
                raise TestFailure("routed", "local shell", repr(shell))

        result = await Suite(name="psu").run()

        assert result.state is State.PASS

    async def test_configure_may_declare_more_than_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                await utils.use_shell(inherit=False)
                await utils.use_shell()
                await utils.use_shell(inherit=False, propagate=False)
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("rail", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                if utils.exec_route == "local":
                    return PASS
                raise TestFailure("routed", "local", utils.exec_route)

        with caplog.at_level(logging.INFO):
            result = await Suite(name="psu").run()

        assert result.state is State.PASS
        assert "exec routing: local (declared via use_shell(inherit=False))" in caplog.text

    async def test_the_window_shuts_when_configure_raises(self) -> None:
        seen: list[bool] = []

        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                raise TestFailure("bench down", "up", "down")

            def build_suite(self) -> LitmusTest:
                raise AssertionError("configure failure must skip the suite")

            async def cleanup(self, utils: TestUtils) -> None:
                try:
                    await utils.use_shell(inherit=False)
                except ContractViolation:
                    seen.append(True)

        result = await Suite(name="psu").main(TestUtils())

        assert result.state is State.FAIL
        assert seen == [True]
