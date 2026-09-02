"""Testable-level via: framework-owned gateway lifecycle and hop contracts."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.loopback_sshd import LoopbackSshd

from litmus import (
    PASS,
    FrameworkContractViolation,
    LitmusTest,
    TestFailure,
    TestUtils,
    Verdict,
)
from litmus.lifecycle import RunAbort, State
from litmus.remote import RemoteInterface
from litmus.routing import ExecRouter


def _router(utils: TestUtils) -> ExecRouter | None:
    return utils._routing.router  # pyright: ignore[reportPrivateUsage]


class TestViaLifecycleOrder:
    async def test_via_configures_before_the_consumer_and_cleans_up_after(self) -> None:
        calls: list[str] = []

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                calls.append("gateway-configure")
                return {"gateway": "up"}

            def build_suite(self) -> LitmusTest:
                return self.case("unused", self._unused)

            async def _unused(self, utils: TestUtils) -> Verdict:
                calls.append("gateway-test")
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                calls.append("gateway-cleanup")

        class Target(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                calls.append("target-configure")
                return {"target": "up"}

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                calls.append("target-test")
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                calls.append("target-cleanup")

        gateway = Gateway(name="gateway")
        outcome = await Target(name="target", via=gateway).main()

        assert outcome.state is State.PASS
        assert calls == [
            "gateway-configure",
            "target-configure",
            "target-test",
            "target-cleanup",
            "gateway-cleanup",
        ]

    async def test_nested_via_devices_bring_up_outside_in_and_tear_down_inside_out(
        self,
    ) -> None:
        calls: list[str] = []

        class Hop(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("unused", self._unused)

            async def configure(self, utils: TestUtils) -> dict[str, object]:
                calls.append(f"{self.name}-configure")
                return {}

            async def cleanup(self, utils: TestUtils) -> None:
                calls.append(f"{self.name}-cleanup")

            async def _unused(self, utils: TestUtils) -> Verdict:
                return PASS

        class Target(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def configure(self, utils: TestUtils) -> dict[str, object]:
                calls.append("target-configure")
                return {}

            async def cleanup(self, utils: TestUtils) -> None:
                calls.append("target-cleanup")

            async def _claim(self, utils: TestUtils) -> Verdict:
                return PASS

        jump = Hop(name="jump")
        gateway = Hop(name="gateway", via=jump)
        outcome = await Target(name="target", via=gateway).main()

        assert outcome.state is State.PASS
        assert calls == [
            "jump-configure",
            "gateway-configure",
            "target-configure",
            "target-cleanup",
            "gateway-cleanup",
            "jump-cleanup",
        ]

    async def test_via_context_does_not_cross_the_instance_boundary(self) -> None:
        seen: list[dict[str, object]] = []

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                return {"secret": "gateway-only"}

            def build_suite(self) -> LitmusTest:
                return self.case("unused", self._unused)

            async def _unused(self, utils: TestUtils) -> Verdict:
                return PASS

        class Target(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                seen.append(dict(utils.ctx))
                return {"target": "up"}

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                seen.append(dict(utils.ctx))
                return PASS

        outcome = await Target(name="target", via=Gateway(name="gateway")).main()

        assert outcome.state is State.PASS
        assert seen == [{}, {"target": "up"}]


class TestViaFailurePaths:
    async def test_via_configure_failure_skips_the_consumer_and_still_cleans_up(
        self,
    ) -> None:
        calls: list[str] = []

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                calls.append("gateway-configure")
                raise TestFailure("gateway did not reach a testable state")

            def build_suite(self) -> LitmusTest:
                return self.case("unused", self._unused)

            async def _unused(self, utils: TestUtils) -> Verdict:
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                calls.append("gateway-cleanup")

        class Target(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                calls.append("target-configure")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                calls.append("target-test")
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                calls.append("target-cleanup")

        outcome = await Target(name="target", via=Gateway(name="gateway")).main()

        assert outcome.state is State.FAIL
        assert isinstance(outcome.failure, TestFailure)
        assert outcome.failure.fail_message == "gateway did not reach a testable state"
        assert calls == [
            "gateway-configure",
            "target-cleanup",
            "gateway-cleanup",
        ]

    async def test_consumer_configure_failure_still_cleans_up_the_via(self) -> None:
        calls: list[str] = []

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                calls.append("gateway-configure")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.case("unused", self._unused)

            async def _unused(self, utils: TestUtils) -> Verdict:
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                calls.append("gateway-cleanup")

        class Target(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                calls.append("target-configure")
                raise TestFailure("target did not boot")

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                calls.append("target-test")
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                calls.append("target-cleanup")

        outcome = await Target(name="target", via=Gateway(name="gateway")).main()

        assert outcome.state is State.FAIL
        assert calls == [
            "gateway-configure",
            "target-configure",
            "target-cleanup",
            "gateway-cleanup",
        ]

    async def test_a_via_can_be_brought_up_again_after_cleanup(self) -> None:
        configures = 0

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                nonlocal configures
                configures += 1
                return {}

            def build_suite(self) -> LitmusTest:
                return self.case("unused", self._unused)

            async def _unused(self, utils: TestUtils) -> Verdict:
                return PASS

        class Target(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                return PASS

        gateway = Gateway(name="gateway")
        first = await Target(name="first", via=gateway).main()
        second = await Target(name="second", via=gateway).main()

        assert first.state is State.PASS
        assert second.state is State.PASS
        assert configures == 2


class TestViaHopContracts:
    """The via device owns its hop; the consumer borrows it through the router."""

    async def test_consumer_exec_routes_through_the_via_before_configure(self) -> None:
        jump_peer = await LoopbackSshd.start(username="ops", password="jump")
        hops: list[RemoteInterface] = []
        routed: list[str] = []

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                await utils.use_shell(
                    host="127.0.0.1",
                    port=jump_peer.port,
                    username="ops",
                    password="jump",
                    skip_host_verification=True,
                )
                hop = _router(utils)
                assert hop is not None
                assert hop.ssh is not None
                hops.append(hop.ssh)
                return {}

            def build_suite(self) -> LitmusTest:
                return self.case("unused", self._unused)

            async def _unused(self, utils: TestUtils) -> Verdict:
                return PASS

        class Target(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                result = await utils.exec("uname")
                routed.append(result.stdout)
                return {}

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                result = await utils.exec("id")
                routed.append(result.stdout)
                return PASS

        try:
            outcome = await Target(name="target", via=Gateway(name="gateway")).main()
        finally:
            await jump_peer.aclose()

        assert outcome.state is State.PASS
        assert routed == ["uname", "id"]
        assert hops[0].connected is False

    async def test_consumer_opened_tunnel_is_borrowed_and_via_hop_survives_its_cancel(
        self,
    ) -> None:
        jump_peer = await LoopbackSshd.start(username="ops", password="jump")
        target_peer = await LoopbackSshd.start(username="root", password="admin")
        hops: dict[str, RemoteInterface] = {}
        seen: dict[str, Any] = {}

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                await utils.use_shell(
                    host="127.0.0.1",
                    port=jump_peer.port,
                    username="ops",
                    password="jump",
                    skip_host_verification=True,
                )
                hop = _router(utils)
                assert hop is not None
                assert hop.ssh is not None
                hops["owned"] = hop.ssh
                return {}

            def build_suite(self) -> LitmusTest:
                return self.case("unused", self._unused)

            async def _unused(self, utils: TestUtils) -> Verdict:
                return PASS

        class Target(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                extra = await utils.get_shell(
                    host="127.0.0.1",
                    port=target_peer.port,
                    username="root",
                    password="admin",
                    skip_host_verification=True,
                )
                tunneled = extra.ssh
                assert tunneled is not None
                hops["borrowed"] = tunneled
                seen["borrowed_hop"] = tunneled.hop
                await tunneled.cancel_in_flight()
                seen["owned_after_borrowed_cancel"] = hops["owned"].connected
                result = await utils.exec("id")
                seen["exec_after_cancel"] = result.stdout
                return PASS

        try:
            outcome = await Target(name="target", via=Gateway(name="gateway")).main()
        finally:
            await jump_peer.aclose()
            await target_peer.aclose()

        assert outcome.state is State.PASS
        assert seen["borrowed_hop"] is hops["owned"]
        assert hops["borrowed"].connected is False
        assert seen["owned_after_borrowed_cancel"] is True
        assert seen["exec_after_cancel"] == "id"
        assert hops["owned"].connected is False

    async def test_shared_via_configure_is_idempotent_until_cleanup(self) -> None:
        jump_peer = await LoopbackSshd.start(username="ops", password="jump")
        configures = 0
        hops: list[RemoteInterface] = []

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                nonlocal configures
                configures += 1
                await utils.use_shell(
                    host="127.0.0.1",
                    port=jump_peer.port,
                    username="ops",
                    password="jump",
                    skip_host_verification=True,
                )
                hop = _router(utils)
                assert hop is not None
                assert hop.ssh is not None
                hops.append(hop.ssh)
                return {}

            def build_suite(self) -> LitmusTest:
                return self.case("unused", self._unused)

            async def _unused(self, utils: TestUtils) -> Verdict:
                return PASS

        class Probe(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                return PASS

        gateway = Gateway(name="gateway")
        first = Probe(name="first", via=gateway)
        second = Probe(name="second", via=gateway)
        try:
            await first.run_configure_only()
            await second.run_configure_only()
            assert configures == 1
            assert hops[0].connected is True
            await first.run_cleanup_only()
            assert hops[0].connected is True
            assert configures == 1
            await second.run_cleanup_only()
            assert hops[0].connected is False
        finally:
            await jump_peer.aclose()

    async def test_a_second_consumer_waits_out_an_in_flight_via_handshake(self) -> None:
        jump_peer = await LoopbackSshd.start(username="ops", password="jump")
        handshake_entered = asyncio.Event()
        handshake_continue = asyncio.Event()
        configures = 0
        hops: list[RemoteInterface] = []
        routed: dict[str, str] = {}

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                nonlocal configures
                configures += 1
                handshake_entered.set()
                await handshake_continue.wait()
                await utils.use_shell(
                    host="127.0.0.1",
                    port=jump_peer.port,
                    username="ops",
                    password="jump",
                    skip_host_verification=True,
                )
                hop = _router(utils)
                assert hop is not None
                assert hop.ssh is not None
                hops.append(hop.ssh)
                return {}

            def build_suite(self) -> LitmusTest:
                return self.case("unused", self._unused)

            async def _unused(self, utils: TestUtils) -> Verdict:
                return PASS

        class Probe(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                return PASS

            async def configure(self, utils: TestUtils) -> dict[str, object]:
                result = await utils.exec("id")
                routed[self.name] = result.stdout
                return {}

        gateway = Gateway(name="gateway")
        first = Probe(name="first", via=gateway)
        second = Probe(name="second", via=gateway)
        try:
            first_task = asyncio.create_task(first.run_configure_only())
            await handshake_entered.wait()
            second_task = asyncio.create_task(second.run_configure_only())
            await asyncio.sleep(0)
            handshake_continue.set()
            await first_task
            await second_task
            assert configures == 1
            assert routed == {"first": "id", "second": "id"}
            assert hops[0].connected is True
            await first.run_cleanup_only()
            assert hops[0].connected is True
            await second.run_cleanup_only()
            assert hops[0].connected is False
        finally:
            handshake_continue.set()
            await first.run_cleanup_only()
            await second.run_cleanup_only()
            await jump_peer.aclose()

    async def test_group_siblings_sharing_a_via_both_route_through_it(self) -> None:
        jump_peer = await LoopbackSshd.start(username="ops", password="jump")
        configures = 0
        hops: list[RemoteInterface] = []
        routed: dict[str, str] = {}
        hop_up_at_cleanup: dict[str, bool] = {}

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                nonlocal configures
                configures += 1
                await utils.use_shell(
                    host="127.0.0.1",
                    port=jump_peer.port,
                    username="ops",
                    password="jump",
                    skip_host_verification=True,
                )
                hop = _router(utils)
                assert hop is not None
                assert hop.ssh is not None
                hops.append(hop.ssh)
                return {}

            def build_suite(self) -> LitmusTest:
                return self.case("unused", self._unused)

            async def _unused(self, utils: TestUtils) -> Verdict:
                return PASS

        class Probe(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                result = await utils.exec("id")
                routed[self.name] = result.stdout
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                hop_up_at_cleanup[self.name] = hops[0].connected

        gateway = Gateway(name="gateway")

        class Rack(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.group(
                    "siblings",
                    [
                        Probe(name="alpha", via=gateway),
                        Probe(name="bravo", via=gateway),
                    ],
                )

        try:
            outcome = await Rack(name="rack").main()
        finally:
            await jump_peer.aclose()

        assert outcome.state is State.PASS
        assert configures == 1
        assert routed == {"alpha": "id", "bravo": "id"}
        assert hop_up_at_cleanup == {"alpha": True, "bravo": True}
        assert hops[0].connected is False

    def test_inherit_router_rejects_a_missing_hop(self) -> None:
        utils = TestUtils()._for_node(  # pyright: ignore[reportPrivateUsage]
            "target",
            RunAbort(),
            isolate_context=True,
        )
        with pytest.raises(FrameworkContractViolation, match="no exec route"):
            utils._inherit_router(None)  # pyright: ignore[reportPrivateUsage]
