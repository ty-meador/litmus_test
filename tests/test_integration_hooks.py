"""Root-owned integration hooks dispatched around every node in the test tree."""

import asyncio
from typing import Any, cast

import pytest

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
from litmus.routing import ExecRouting, ShellOpenRequest


class HookedSuite(LitmusTest):
    """A small authored tree whose structure makes fractal dispatch visible."""

    def build_suite(self) -> LitmusTest:
        return self.sequence(
            "claims",
            [
                self.case("identity", self._identity),
                self.case("health", self._health),
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


async def test_root_registration_dispatches_fractally_around_every_node() -> None:
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


async def test_context_is_narrow_at_runtime_and_after_each_receives_the_result() -> None:
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
        record for record in caplog.records if "UNPAIRED INTEGRATION HOOK" in record.getMessage()
    ]
    assert len(warnings) == warning_count


async def test_sync_hook_is_rejected_before_lifecycle_execution() -> None:
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
    assert str(result.failure.cause) == "before_each hook must return an awaitable"


async def test_lambda_may_return_an_async_integration_call() -> None:
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


@pytest.mark.parametrize(
    "failing_node",
    ["rack", "rack/claims", "rack/claims/identity", "rack/claims/health"],
)
async def test_after_each_balances_every_before_each_that_throws(failing_node: str) -> None:
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


async def test_after_each_failure_aborts_ancestors_without_running_later_sequence_nodes() -> None:
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


def _summary_states(node: SummaryTreeNode) -> dict[str, State | None]:
    states = {node.name: node.state}
    for child in node.children:
        states.update(_summary_states(child))
    return states


async def test_group_abort_cancels_siblings_and_returns_one_aborted_tree() -> None:
    slow_started = asyncio.Event()
    never_release = asyncio.Event()

    class GroupedSuite(LitmusTest):
        def build_suite(self) -> LitmusTest:
            return self.group(
                "parallel",
                [
                    self.case("slow", self._slow),
                    self.case("halt", self._halt),
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

    async def after_each(ctx: AfterEachContext) -> None:
        del ctx

    result = await GroupedSuite(name="rack").run(
        before_each=before_each,
        after_each=after_each,
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
    caught_type: type[BaseException],
) -> None:
    parked_started = asyncio.Event()
    caught_cancellation = asyncio.Event()
    cause = RuntimeError("shop-floor database refused the record")

    class GroupedSuite(LitmusTest):
        def build_suite(self) -> LitmusTest:
            return self.group(
                "parallel",
                [
                    self.case("parked", self._parked),
                    self.case("halt", self._halt),
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

    async def after_each(ctx: AfterEachContext) -> None:
        del ctx

    result = await GroupedSuite(name="rack").run(
        before_each=before_each,
        after_each=after_each,
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


class _MockConnector:
    def __init__(self, transport: _ParkedTransport) -> None:
        self.transport = transport

    async def open(self, request: ShellOpenRequest) -> RemoteInterface:
        del request
        await self.transport.connect()
        return self.transport


async def test_hook_abort_hard_closes_a_parked_remote_before_returning() -> None:
    transport = _ParkedTransport()
    connector = _MockConnector(transport)

    class FlashSuite(LitmusTest):
        async def configure(self, utils: TestUtils) -> dict[str, object]:
            routing = cast("ExecRouting", object.__getattribute__(utils, "_routing"))
            object.__setattr__(routing, "_connector", connector)
            await utils.use_shell(host="dut", username="root")
            return {}

        def build_suite(self) -> LitmusTest:
            return self.group(
                "parallel",
                [
                    self.case("flash", self._flash),
                    self.case("halt", self._halt),
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

    async def after_each(ctx: AfterEachContext) -> None:
        del ctx

    result = await FlashSuite(name="rack").run(
        before_each=before_each,
        after_each=after_each,
    )

    assert result.state is State.ABORTED
    assert transport.cancel_count == 1
    assert transport.connected is False
    assert transport.has_in_flight is False
