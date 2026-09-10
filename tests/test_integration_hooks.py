"""Root-owned integration hooks dispatched around every node in the test tree."""

import asyncio
from typing import Any

import pytest
from tests.mock_connector import ConnectorInstaller

from litmus import (
    PASS,
    AfterEachContext,
    AfterEachHook,
    BeforeEachContext,
    BeforeEachHook,
    ExternalIntegrationFailure,
    LitmusTest,
    TestUtils,
    Verdict,
)
from litmus.executor import ExecResult
from litmus.lifecycle import State
from litmus.remote import RemoteInterface
from litmus.results import SummaryTreeNode


class HookedSuite(LitmusTest):
    """A small authored tree whose structure makes fractal dispatch visible."""

    def build_suite(self) -> LitmusTest:
        return self.sequence(
            "claims",
            [
                self.test("identity", self._identity),
                self.test("health", self._health),
            ],
        )

    async def _identity(self, utils: TestUtils) -> Verdict:
        if utils.logger.scope[-1] == "identity":
            return PASS
        raise AssertionError("identity case received the wrong scope")

    async def _health(self, utils: TestUtils) -> Verdict:
        if utils.logger.scope[-1] == "health":
            return PASS
        raise AssertionError("health case received the wrong scope")


class ParallelHookedSuite(LitmusTest):
    """The same visible tree under a parallel parent, where a halt races its siblings."""

    def build_suite(self) -> LitmusTest:
        return self.parallel(
            "claims",
            [
                self.test("identity", self._identity),
                self.test("health", self._health),
            ],
        )

    async def _identity(self, utils: TestUtils) -> Verdict:
        return PASS

    async def _health(self, utils: TestUtils) -> Verdict:
        return PASS


def _summary_states(node: SummaryTreeNode) -> dict[str, State | None]:
    states = {node.name: node.state}
    for child in node.children:
        states.update(_summary_states(child))
    return states


class _ParkedTransport(RemoteInterface):
    def __init__(self) -> None:
        super().__init__()
        self.in_flight = asyncio.Event()
        self.cancel_count = 0
        self._parked: set[asyncio.Future[ExecResult]] = set()

    @property
    def has_in_flight(self) -> bool:
        return bool(self._parked)

    async def _connect(self) -> None:
        return

    async def _disconnect(self) -> None:
        self._fail_parked()

    async def _exec(self, command: str) -> ExecResult:
        del command
        parked: asyncio.Future[ExecResult] = asyncio.get_running_loop().create_future()
        self._parked.add(parked)
        self.in_flight.set()
        try:
            return await parked
        finally:
            self._parked.discard(parked)

    async def _cancel_in_flight(self) -> None:
        self.cancel_count += 1
        self._fail_parked()

    def _fail_parked(self) -> None:
        for parked in tuple(self._parked):
            if not parked.done():
                parked.set_exception(ConnectionError("transport aborted"))


class TestDispatchOrder:
    """Hooks wrap every node, fractally, in declaration order."""

    async def test_root_registration_dispatches_fractally_around_every_node(self) -> None:
        calls: list[tuple[str, str]] = []

        async def before_each(ctx: BeforeEachContext) -> None:
            calls.append(("before", ctx.bread_crumbs))

        async def after_each(ctx: AfterEachContext) -> None:
            assert ctx.result.state is State.PASS
            calls.append(("after", ctx.bread_crumbs))

        result = await HookedSuite(name="rack").run(
            before_each=before_each,
            after_each=after_each,
        )

        assert result.state is State.PASS
        assert calls == [
            ("before", "rack"),
            ("before", "rack/claims"),
            ("before", "rack/claims/identity"),
            ("after", "rack/claims/identity"),
            ("before", "rack/claims/health"),
            ("after", "rack/claims/health"),
            ("after", "rack/claims"),
            ("after", "rack"),
        ]

    async def test_lambda_may_return_an_async_integration_call(self) -> None:
        calls: list[tuple[str, str]] = []

        async def record(kind: str, ctx: BeforeEachContext | AfterEachContext) -> None:
            calls.append((kind, ctx.bread_crumbs))

        # Lambdas are deliberate here: the public idiom forwards an async integration call.
        before_each: BeforeEachHook = lambda ctx: record("before", ctx)  # noqa: E731
        after_each: AfterEachHook = lambda ctx: record("after", ctx)  # noqa: E731
        result = await HookedSuite(name="rack").run(
            before_each=before_each,
            after_each=after_each,
        )

        assert result.state is State.PASS
        assert calls[0] == ("before", "rack")
        assert calls[-1] == ("after", "rack")


class TestNarrowContexts:
    """A hook sees the node's identity and environment, never its hardware."""

    async def test_context_is_narrow_at_runtime_and_after_each_receives_the_result(self) -> None:
        observed: list[BeforeEachContext | AfterEachContext] = []

        async def before_each(ctx: BeforeEachContext) -> None:
            observed.append(ctx)

        async def after_each(ctx: AfterEachContext) -> None:
            observed.append(ctx)

        result = await HookedSuite(name="rack").run(
            env={"STATION_ID": "west-04"},
            before_each=before_each,
            after_each=after_each,
        )

        assert observed
        for ctx in observed:
            assert not hasattr(ctx, "exec")
            assert not hasattr(ctx, "get_shell")
            assert not hasattr(ctx, "capture_artifact")
            assert ctx.env["STATION_ID"] == "west-04"
            assert ctx.logger.extra is not None
            assert ctx.logger.extra.get("litmus_scope") == ctx.ancestor_names
        after_contexts = [ctx for ctx in observed if isinstance(ctx, AfterEachContext)]
        assert after_contexts[-1].result is result


class TestRegistration:
    """Registration is checked before the lifecycle starts."""

    @pytest.mark.parametrize(
        ("hook_name", "allow_unpaired_hooks", "warning_count"),
        [
            ("before_each", False, 1),
            ("after_each", False, 1),
            ("before_each", True, 0),
            ("after_each", True, 0),
        ],
    )
    async def test_unpaired_registration_warns_once_unless_allowed(
        self,
        hook_name: str,
        allow_unpaired_hooks: bool,
        warning_count: int,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def hook(ctx: BeforeEachContext | AfterEachContext) -> None:
            del ctx

        kwargs: dict[str, Any] = {
            hook_name: hook,
            "allow_unpaired_hooks": allow_unpaired_hooks,
        }
        with caplog.at_level("WARNING", logger="litmus"):
            result = await HookedSuite(name="rack").run(**kwargs)

        assert result.state is State.PASS
        warnings = [
            record
            for record in caplog.records
            if "UNPAIRED INTEGRATION HOOK" in record.getMessage()
        ]
        assert len(warnings) == warning_count

    async def test_sync_hook_is_rejected_before_lifecycle_execution(self) -> None:
        lifecycle_started = False

        class GuardedSuite(HookedSuite):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                nonlocal lifecycle_started
                lifecycle_started = True
                return {}

        def sync_hook(ctx: BeforeEachContext) -> None:
            del ctx

        result = await GuardedSuite(name="rack").run(
            before_each=sync_hook,  # pyright: ignore[reportArgumentType]
            allow_unpaired_hooks=True,
        )

        assert not lifecycle_started
        assert result.state is State.ABORTED
        assert isinstance(result.failure, ExternalIntegrationFailure)
        assert isinstance(result.failure.cause, TypeError)
        assert str(result.failure.cause) == (
            "before_each hook must return an awaitable; "
            "define it with async def so the call returns a coroutine"
        )


class TestPairInvariant:
    """Every before_each that ran is balanced by an after_each, whatever threw."""

    @pytest.mark.parametrize(
        "failing_node",
        ["rack", "rack/claims", "rack/claims/identity", "rack/claims/health"],
    )
    async def test_after_each_balances_every_before_each_that_throws(
        self, failing_node: str
    ) -> None:
        started: list[str] = []
        finished: list[str] = []
        cause = RuntimeError(f"shop-floor start rejected {failing_node}")

        async def before_each(ctx: BeforeEachContext) -> None:
            started.append(ctx.bread_crumbs)
            if ctx.bread_crumbs == failing_node:
                raise cause

        async def after_each(ctx: AfterEachContext) -> None:
            finished.append(ctx.bread_crumbs)

        result = await HookedSuite(name="rack").run(
            before_each=before_each,
            after_each=after_each,
        )

        assert result.state is State.ABORTED
        assert isinstance(result.failure, ExternalIntegrationFailure)
        assert result.failure.hook == "before_each"
        assert result.failure.cause is cause
        assert set(finished) == set(started)
        assert len(finished) == len(started)

    @pytest.mark.parametrize(
        "failing_node",
        ["rack", "rack/claims", "rack/claims/identity", "rack/claims/health"],
    )
    async def test_parallel_after_each_balances_every_before_each_that_throws(
        self,
        failing_node: str,
    ) -> None:
        """A sibling's halt must not strip the throwing node of its own finished row."""
        started: list[str] = []
        finished: list[str] = []
        cause = RuntimeError(f"shop-floor start rejected {failing_node}")

        async def before_each(ctx: BeforeEachContext) -> None:
            started.append(ctx.bread_crumbs)
            if ctx.bread_crumbs == failing_node:
                raise cause

        async def after_each(ctx: AfterEachContext) -> None:
            finished.append(ctx.bread_crumbs)

        result = await ParallelHookedSuite(name="rack").run(
            before_each=before_each,
            after_each=after_each,
        )

        assert result.state is State.ABORTED
        assert isinstance(result.failure, ExternalIntegrationFailure)
        assert result.failure.hook == "before_each"
        assert result.failure.cause is cause
        assert failing_node in finished
        assert set(finished) == set(started)
        assert len(finished) == len(started)

    @pytest.mark.parametrize(
        "failing_node",
        ["rack/claims/identity", "rack/claims/health"],
    )
    async def test_parallel_after_each_that_throws_still_pairs_every_started_node(
        self,
        failing_node: str,
    ) -> None:
        """The completion hook's own failure is still a completion for that node."""
        started: list[str] = []
        finished: list[str] = []
        cause = RuntimeError(f"shop-floor finish rejected {failing_node}")

        async def before_each(ctx: BeforeEachContext) -> None:
            started.append(ctx.bread_crumbs)

        async def after_each(ctx: AfterEachContext) -> None:
            finished.append(ctx.bread_crumbs)
            if ctx.bread_crumbs == failing_node:
                raise cause

        result = await ParallelHookedSuite(name="rack").run(
            before_each=before_each,
            after_each=after_each,
        )

        assert result.state is State.ABORTED
        assert isinstance(result.failure, ExternalIntegrationFailure)
        assert result.failure.hook == "after_each"
        assert result.failure.cause is cause
        assert set(finished) == set(started)
        assert len(finished) == len(started)


class TestFailurePropagation:
    """A hook failure is an abort that suite code cannot outrun or swallow."""

    async def test_after_each_failure_aborts_ancestors_without_running_later_sequence_nodes(
        self,
    ) -> None:
        started: list[str] = []
        finished: list[str] = []
        cause = RuntimeError("shop-floor finish rejected identity")

        async def before_each(ctx: BeforeEachContext) -> None:
            started.append(ctx.bread_crumbs)

        async def after_each(ctx: AfterEachContext) -> None:
            finished.append(ctx.bread_crumbs)
            if ctx.bread_crumbs == "rack/claims/identity":
                raise cause

        result = await HookedSuite(name="rack").run(
            before_each=before_each,
            after_each=after_each,
        )

        assert result.state is State.ABORTED
        assert isinstance(result.failure, ExternalIntegrationFailure)
        assert result.failure.hook == "after_each"
        assert result.failure.cause is cause
        assert set(finished) == set(started)
        assert "rack/claims/health" not in started

    async def test_parallel_abort_cancels_siblings_and_returns_one_aborted_tree(self) -> None:
        slow_started = asyncio.Event()
        never_release = asyncio.Event()

        class GroupedSuite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "parallel",
                    [
                        self.test("slow", self._slow),
                        self.test("halt", self._halt),
                    ],
                )

            async def _slow(self, utils: TestUtils) -> Verdict:
                slow_started.set()
                await never_release.wait()
                return PASS

            async def _halt(self, utils: TestUtils) -> Verdict:
                return PASS

        async def before_each(ctx: BeforeEachContext) -> None:
            if ctx.name == "halt":
                await slow_started.wait()
                raise RuntimeError("coordinator unavailable")

        result = await GroupedSuite(name="rack").run(
            before_each=before_each,
            allow_unpaired_hooks=True,
        )

        assert result.state is State.ABORTED
        assert _summary_states(result.summary_tree) == {
            "rack": State.ABORTED,
            "parallel": State.ABORTED,
            "slow": State.ABORTED,
            "halt": State.ABORTED,
        }

    @pytest.mark.parametrize(
        "caught_type",
        [Exception, BaseException],
        ids=["exception", "base-exception"],
    )
    async def test_suite_code_cannot_swallow_an_abort_and_forge_pass(
        self,
        caught_type: type[BaseException],
    ) -> None:
        parked_started = asyncio.Event()
        caught_cancellation = asyncio.Event()
        cause = RuntimeError("shop-floor database refused the record")

        class GroupedSuite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "parallel",
                    [
                        self.test("parked", self._parked),
                        self.test("halt", self._halt),
                    ],
                )

            async def _parked(self, utils: TestUtils) -> Verdict:
                parked_started.set()
                try:
                    await asyncio.Event().wait()
                except caught_type:
                    caught_cancellation.set()
                    return PASS
                raise AssertionError("parked test resumed without cancellation")

            async def _halt(self, utils: TestUtils) -> Verdict:
                return PASS

        async def before_each(ctx: BeforeEachContext) -> None:
            if ctx.name == "halt":
                await parked_started.wait()
                raise cause

        result = await GroupedSuite(name="rack").run(
            before_each=before_each,
            allow_unpaired_hooks=True,
        )

        assert caught_cancellation.is_set() is (caught_type is BaseException)
        assert result.state is State.ABORTED
        assert isinstance(result.failure, ExternalIntegrationFailure)
        assert result.failure.hook == "before_each"
        assert result.failure.cause is cause
        assert _summary_states(result.summary_tree) == {
            "rack": State.ABORTED,
            "parallel": State.ABORTED,
            "parked": State.ABORTED,
            "halt": State.ABORTED,
        }


class TestAbortTeardown:
    """An abort hard-closes every opened transport before the run returns."""

    async def test_hook_abort_hard_closes_a_parked_remote_before_returning(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        transport = _ParkedTransport()

        class FlashSuite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, transport)
                await utils.use_shell(host="dut", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "parallel",
                    [
                        self.test("flash", self._flash),
                        self.test("halt", self._halt),
                    ],
                )

            async def _flash(self, utils: TestUtils) -> Verdict:
                await utils.exec("flash")
                return PASS

            async def _halt(self, utils: TestUtils) -> Verdict:
                return PASS

        async def before_each(ctx: BeforeEachContext) -> None:
            if ctx.name == "halt":
                await transport.in_flight.wait()
                raise RuntimeError("audit database unavailable")

        result = await FlashSuite(name="rack").run(
            before_each=before_each,
            allow_unpaired_hooks=True,
        )

        assert result.state is State.ABORTED
        assert transport.cancel_count == 1
        assert transport.connected is False
        assert transport.has_in_flight is False

    async def test_cleanup_exec_after_abort_is_not_a_contract_violation(
        self,
        caplog: pytest.LogCaptureFixture,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        transport = _ParkedTransport()
        seen: dict[str, ExecResult] = {}

        class FlashSuite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, transport)
                await utils.use_shell(host="dut", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "parallel",
                    [
                        self.test("flash", self._flash),
                        self.test("halt", self._halt),
                    ],
                )

            async def _flash(self, utils: TestUtils) -> Verdict:
                seen["flash"] = await utils.exec("flash")
                return PASS

            async def _halt(self, utils: TestUtils) -> Verdict:
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                seen["cleanup"] = await utils.exec("id")

        async def before_each(ctx: BeforeEachContext) -> None:
            if ctx.name == "halt":
                await transport.in_flight.wait()
                raise RuntimeError("audit database unavailable")

        with caplog.at_level("ERROR", logger="litmus"):
            result = await FlashSuite(name="rack").run(
                before_each=before_each,
                allow_unpaired_hooks=True,
            )

        assert result.state is State.ABORTED
        assert seen["cleanup"].exit_code == -1
        assert "not connected" in seen["cleanup"].stderr
        assert seen["flash"].exit_code == -1
        banners = [
            record.message for record in caplog.records if "CONTRACT VIOLATION" in record.message
        ]
        cleanup_banners = [
            record.message for record in caplog.records if "cleanup failed" in record.message
        ]
        assert banners == []
        assert cleanup_banners == []
        assert transport.connected is False

    async def test_abort_hard_closes_every_opened_transport(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        hop = _ParkedTransport()
        dut = _ParkedTransport()

        class FlashSuite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, {"jump": hop, "dut": dut})
                await utils.get_shell(host="jump", username="ops")
                await utils.use_shell(host="dut", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "parallel",
                    [
                        self.test("flash", self._flash),
                        self.test("halt", self._halt),
                    ],
                )

            async def _flash(self, utils: TestUtils) -> Verdict:
                await utils.exec("flash")
                return PASS

            async def _halt(self, utils: TestUtils) -> Verdict:
                return PASS

        async def before_each(ctx: BeforeEachContext) -> None:
            if ctx.name == "halt":
                await dut.in_flight.wait()
                raise RuntimeError("audit database unavailable")

        result = await FlashSuite(name="rack").run(
            before_each=before_each,
            allow_unpaired_hooks=True,
        )

        assert result.state is State.ABORTED
        assert hop.cancel_count == 1
        assert dut.cancel_count == 1
        assert hop.connected is False
        assert dut.connected is False
