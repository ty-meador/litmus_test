"""Root-owned integration hooks dispatched around every node in the test tree."""

from typing import Any

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
from litmus.lifecycle import State


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

    with pytest.raises(ExternalIntegrationFailure) as caught:
        await GuardedSuite(name="rack").run(
            before_each=sync_hook,  # pyright: ignore[reportArgumentType]
            allow_unpaired_hooks=True,
        )

    assert not lifecycle_started
    assert isinstance(caught.value.cause, TypeError)
    assert str(caught.value.cause) == "before_each hook must return an awaitable"


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
