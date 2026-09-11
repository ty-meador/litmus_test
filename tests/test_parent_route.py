"""The parent owns the route (issue #137).

A node whose ``configure()`` declares a route with ``use_shell`` hands it to every
node it composes. A device class carries no routing argument; whichever host its
parent put ``utils.exec`` on is the host it runs against. These tests pin the three
promises the ruling makes: children inherit the parent's route, ``inherit=False``
in a child restores local exec, and the hop closes in the parent's own teardown,
after every child has finished with it.
"""

from __future__ import annotations

import pytest
from tests.mock_connector import ConnectorInstaller

from litmus import (
    PASS,
    ExecResult,
    LitmusTest,
    TestFailure,
    TestUtils,
    Verdict,
)
from litmus.executor import LocalExec
from litmus.lifecycle import Slot, State
from litmus.mock import MockTransport
from litmus.utils import node_runtime


def _provenance(utils: TestUtils) -> str:
    return node_runtime(utils).routing.provenance


class TestChildrenInheritTheParentsRoute:
    async def test_a_child_execs_through_the_hop_its_parent_opened(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        hop = MockTransport()
        seen: list[str] = []

        class Child(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                # Read at the composed node itself: a leaf made by `self.test` always
                # reports "inherited", since it inherits from its authored parent.
                seen.append(_provenance(utils))
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("probe", self._probe)

            async def _probe(self, utils: TestUtils) -> Verdict:
                await utils.exec("probe")
                return PASS

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, {"gateway": hop})
                await utils.use_shell(host="gateway", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return Child(name="child")

        result = await Gateway(name="gateway").run()

        assert result.state is State.PASS
        assert "probe" in hop.commands
        assert seen == ["inherited"]

    async def test_siblings_share_one_hop_and_no_child_reopens_it(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        hop = MockTransport()

        class Child(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("probe", self._probe)

            async def _probe(self, utils: TestUtils) -> Verdict:
                await utils.exec(f"probe {self.name}")
                return PASS

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, {"gateway": hop})
                await utils.use_shell(host="gateway", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "behind the hop",
                    [Child(name="first"), Child(name="second")],
                )

        result = await Gateway(name="gateway").run()

        assert result.state is State.PASS
        assert hop.connect_count == 1
        assert {"probe first", "probe second"} <= set(hop.commands)

    async def test_the_route_reaches_a_grandchild_unchanged(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        hop = MockTransport()

        class Leaf(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("probe", self._probe)

            async def _probe(self, utils: TestUtils) -> Verdict:
                await utils.exec("deep probe")
                return PASS

        class Middle(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return Leaf(name="leaf")

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, {"gateway": hop})
                await utils.use_shell(host="gateway", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return Middle(name="middle")

        result = await Gateway(name="gateway").run()

        assert result.state is State.PASS
        assert "deep probe" in hop.commands


class TestAChildMayStepOffTheRoute:
    async def test_inherit_false_restores_local_exec_for_that_child_only(
        self,
        install_mock_connector: ConnectorInstaller,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hop = MockTransport()
        local_commands: list[str] = []
        seen: list[str] = []

        async def local_run(self: LocalExec, command: str) -> ExecResult:
            del self
            local_commands.append(command)
            return ExecResult(stdout="", stderr="", exit_code=0)

        monkeypatch.setattr(LocalExec, "run", local_run)

        class Routed(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("probe", self._probe)

            async def _probe(self, utils: TestUtils) -> Verdict:
                await utils.exec("routed probe")
                return PASS

        class Local(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                await utils.use_shell(inherit=False)
                seen.append(_provenance(utils))
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("probe", self._probe)

            async def _probe(self, utils: TestUtils) -> Verdict:
                await utils.exec("local probe")
                return PASS

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, {"gateway": hop})
                await utils.use_shell(host="gateway", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    self.name,
                    [Routed(name="routed"), Local(name="local")],
                )

        result = await Gateway(name="gateway").run()

        assert result.state is State.PASS
        assert "routed probe" in hop.commands
        assert "local probe" not in hop.commands
        assert local_commands == ["local probe"]
        assert seen == ["isolated"]


class TestTheParentTearsTheHopDownLast:
    async def test_children_clean_up_on_a_live_hop_and_the_parent_closes_it(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        hop = MockTransport()
        target = MockTransport()
        order: list[str] = []

        class Child(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                # A child's own connection form tunnels through the inherited hop.
                install_mock_connector(utils, {"target": target})
                await utils.use_shell(host="target", username="root")
                order.append("child-configure")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("probe", self._probe)

            async def _probe(self, utils: TestUtils) -> Verdict:
                await utils.exec("probe")
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                del utils
                order.append("child-cleanup")
                assert target.connected
                assert hop.connected

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, {"gateway": hop})
                await utils.use_shell(host="gateway", username="root")
                order.append("gateway-configure")
                return {}

            def build_suite(self) -> LitmusTest:
                return Child(name="child")

            async def cleanup(self, utils: TestUtils) -> None:
                del utils
                order.append("gateway-cleanup")
                # The child's own tunnel closed at the child's boundary; the hop it
                # borrowed is this node's and is still up while this node cleans up.
                assert not target.connected
                assert hop.connected

        result = await Gateway(name="gateway").run()

        assert result.state is State.PASS
        assert order == [
            "gateway-configure",
            "child-configure",
            "child-cleanup",
            "gateway-cleanup",
        ]
        assert target.disconnect_count == 1
        assert hop.disconnect_count == 1
        assert not hop.connected

    async def test_a_failing_child_does_not_take_the_hop_down_early(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        hop = MockTransport()
        hop_alive_at_second_child: list[bool] = []

        class Failing(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("probe", self._probe)

            async def _probe(self, utils: TestUtils) -> Verdict:
                del utils
                raise TestFailure("first device is bad")

        class Second(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("probe", self._probe)

            async def _probe(self, utils: TestUtils) -> Verdict:
                hop_alive_at_second_child.append(hop.connected)
                await utils.exec("second probe")
                return PASS

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, {"gateway": hop})
                await utils.use_shell(host="gateway", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "behind the hop",
                    [Failing(name="failing"), Second(name="second")],
                )

        result = await Gateway(name="gateway").run()

        assert result.state is State.FAIL
        assert hop_alive_at_second_child == [True]
        assert "second probe" in hop.commands
        assert hop.disconnect_count == 1


class TestAGatewayThatNeverComesUp:
    async def test_the_fail_lands_on_the_parent_and_the_children_never_run(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        hop = MockTransport()
        hop.fail_next_connect(ConnectionError("peer unreachable"))
        child_ran = False

        class Child(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                nonlocal child_ran
                del utils
                child_ran = True
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("probe", self._probe)

            async def _probe(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, {"gateway": hop})
                try:
                    await utils.use_shell(host="gateway", username="root")
                except Exception as error:
                    raise TestFailure(f"SSH connect failed: {error}") from error
                return {}

            def build_suite(self) -> LitmusTest:
                return Child(name="child")

        result = await Gateway(name="gateway").run()

        assert result.state is State.FAIL
        assert result.slot is Slot.CONFIGURE
        assert isinstance(result.failure, TestFailure)
        assert "peer unreachable" in result.failure.fail_message
        assert child_ran is False
        assert hop.commands == ()
