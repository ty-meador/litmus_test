"""The single public test node and its framework-owned lifecycle."""

import asyncio
import contextlib
import functools
import inspect
import math
import os
import signal
import weakref
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast, final

from litmus.artifacts import Artifact
from litmus.errors import (
    ContractViolation,
    ExternalIntegrationFailure,
    FrameworkContractViolation,
    OperatorInterrupt,
    TestFailure,
)
from litmus.evidence import declared_evidence
from litmus.executor import DEFAULT_EXEC_OUTPUT_BUDGET
from litmus.hooks import (
    AfterEachContext,
    AfterEachHook,
    AuditHook,
    BeforeEachContext,
    BeforeEachHook,
    HookFunction,
    IntegrationHooks,
    SurveyHook,
    invoke_integration_hook,
)
from litmus.lifecycle import (
    AuditSignal,
    ChildBoundary,
    EvidenceContract,
    Outcome,
    Phase,
    RunAbort,
    Slot,
    State,
    TimeBudget,
    TimeBudgetClock,
    TimeoutEvidence,
)
from litmus.measurement import current_utils
from litmus.results import NodeType, RunResult, SummaryTreeNode
from litmus.run_data import MEASUREMENTS_KEY, JsonObject, snapshot_run_data
from litmus.utils import NodeRuntime, TestUtils, node_runtime
from litmus.verdict import Verdict

type _TestFunction = Callable[..., Awaitable[Verdict]]
type _TimedCall = Callable[[tuple[TimeBudgetClock, ...], object], Awaitable[object]]

_SEALED_METHODS = frozenset(
    {
        "__delattr__",
        "__init_subclass__",
        "__setattr__",
        "test",
        "discrete_log_file",
        "parallel",
        "log_path",
        "main",
        "_execute_run",
        "run",
        "run_configure_only",
        "run_cleanup_only",
        "sequence",
        "_run_lifecycle",
        "_run_timed_window",
        "_settle_deadline_cut",
        "_deadline_outcome",
        "_deadline_failure",
        "_run_parallel",
        "_run_sequence",
        "_run_configure_only",
        "_run_cleanup_only",
        "_bring_up_via",
        "_tear_down_via",
        "_adopt_via_outcome",
        "_node_type",
        "_pending_summary_tree",
        "_pending_children",
        "_to_result",
        "_finish_result",
        "_aborted_result",
        "_configure_and_merge_context",
        "_capture_hook_artifact",
        "_capture_hook_result",
        "_run_audit_loop",
        "_run_surveys",
        "_run_suite_with_audits",
        "_run_after_each",
        "_run_cleanup",
        "use_survey",
        "use_audit",
    }
)
_LIFECYCLE_COROUTINES = frozenset({"configure", "cleanup"})
_FINALIZATION_TIMEOUT_SECONDS = 30.0
_OPERATOR_SIGNALS: tuple[signal.Signals, signal.Signals] = (signal.SIGINT, signal.SIGTERM)


def _escalate_operator_interrupt(signum: int) -> None:
    """Second operator signal during finalization: leave immediately."""
    os._exit(128 + int(signum))


@final
class _OperatorSignalGuard:
    """SIGINT/SIGTERM for one live run. First signal aborts; second exits.

    Installed on the running loop only while `_execute_run` is in flight. The first
    signal fires the shared `RunAbort` with an `OperatorInterrupt`, hard-closes
    every registered transport, and cancels the lifecycle task. A second signal
    inside that window calls `_escalate_operator_interrupt`.
    """

    def __init__(
        self,
        run_abort: RunAbort,
        seed: NodeRuntime,
        lifecycle: asyncio.Task[RunResult | None],
    ) -> None:
        self._run_abort = run_abort
        self._seed = seed
        self._lifecycle = lifecycle
        self._signals_seen = 0
        self._installed: tuple[signal.Signals, ...] = ()
        self._close_task: asyncio.Task[None] | None = None

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind SIGINT and SIGTERM on `loop`. Records each bind as it succeeds.

        A later bind that fails still leaves the earlier handlers on `_installed`
        so `remove` can drop a partial install.
        """
        for sig in _OPERATOR_SIGNALS:
            loop.add_signal_handler(sig, self.handle, sig)
            self._installed = (*self._installed, sig)

    def remove(self, loop: asyncio.AbstractEventLoop) -> None:
        """Drop this run's handlers so library callers never leak them."""
        for sig in self._installed:
            loop.remove_signal_handler(sig)
        self._installed = ()

    def handle(self, signum: int) -> None:
        """Route one operator signal. First aborts the run; later signals exit."""
        self._signals_seen += 1
        if self._signals_seen > 1:
            _escalate_operator_interrupt(signum)
            return
        reason = OperatorInterrupt(signum)
        self._run_abort.fire(reason)
        if not self._lifecycle.done():
            self._lifecycle.cancel()
        loop = asyncio.get_running_loop()
        self._close_task = loop.create_task(self._seed.cancel_in_flight())

    async def drain_close(self) -> None:
        """Wait for the hard-close scheduled by the first signal, if any."""
        close = self._close_task
        if close is not None:
            with contextlib.suppress(Exception):
                await close


_DEADLINE_CANCELLATION = object()
_UTILS_PLACEHOLDER = object()
_DEADLINE_CANCEL_COUNTS: weakref.WeakKeyDictionary[asyncio.Task[Any], int] = (
    weakref.WeakKeyDictionary()
)


def _time_budget(
    node: str,
    *,
    fail_after: float | None,
    abort_after: float | None,
) -> TimeBudget | None:
    """Validate one public deadline declaration before it can touch hardware."""
    if fail_after is not None and abort_after is not None:
        raise ContractViolation(
            node,
            "fail_after and abort_after are mutually exclusive; declare exactly one or neither",
        )
    raw = fail_after if fail_after is not None else abort_after
    if raw is None:
        return None
    name = "fail_after" if fail_after is not None else "abort_after"
    value = cast("object", raw)
    duration = (
        f"{name} must be a positive finite number of seconds; "
        "pass a number of seconds greater than zero"
    )
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ContractViolation(node, duration)
    try:
        seconds = float(value)
    except Exception as error:
        raise ContractViolation(node, duration, cause=error) from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise ContractViolation(node, duration)
    if fail_after is not None:
        return TimeBudget(name="fail_after", seconds=seconds)
    return TimeBudget(name="abort_after", seconds=seconds)


@contextlib.asynccontextmanager
async def _active_time_budgets(
    clocks: tuple[TimeBudgetClock, ...],
    lineage: object,
) -> AsyncGenerator[None]:
    """Charge time while at least one descendant lineage is inside its window."""
    for clock in clocks:
        clock.activate(lineage)
    try:
        yield
    finally:
        for clock in reversed(clocks):
            clock.deactivate(lineage)


def _cancel_for_deadline(task: asyncio.Task[Any], *, requests: int = 1) -> None:
    """Cancel a task with a marker and remember how many requests are framework-owned."""
    _DEADLINE_CANCEL_COUNTS[task] = _DEADLINE_CANCEL_COUNTS.get(task, 0) + requests
    task.cancel(_DEADLINE_CANCELLATION)


def _consume_deadline_cancellation(error: asyncio.CancelledError) -> bool:
    """Consume only requests emitted by this deadline engine, preserving collisions."""
    if not error.args or error.args[0] is not _DEADLINE_CANCELLATION:
        return False
    task = asyncio.current_task()
    if task is None:
        return False
    owned = _DEADLINE_CANCEL_COUNTS.pop(task, 0)
    for _ in range(owned):
        task.uncancel()
    # Any count left after the framework-owned requests are removed belongs to a
    # caller or harness collision and must keep travelling as cancellation.
    return owned > 0 and not task.cancelling()


def _consume_finalization_exception(future: asyncio.Future[object]) -> None:
    """Retrieve a late exception from a timed-out finalizer without masking its log."""
    if not future.cancelled():
        future.exception()


async def _bounded_finalization(call: Awaitable[object], *, node: str, operation: str) -> object:
    """Run shielded user finalization within the framework's evidence budget."""

    async def await_call() -> object:
        return await call

    task = asyncio.create_task(await_call())
    try:
        async with asyncio.timeout(_FINALIZATION_TIMEOUT_SECONDS):
            return await asyncio.shield(task)
    except TimeoutError as error:
        detail = (
            f"{operation} exceeded its {_FINALIZATION_TIMEOUT_SECONDS:g}-second "
            f"finalization timeout; return from {operation} within the evidence budget"
        )
        raise ContractViolation(node, detail=detail, cause=error) from error
    finally:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume_finalization_exception)


def _bind_test_arguments(
    fn: _TestFunction, args: tuple[object, ...], kwargs: dict[str, object]
) -> _TestFunction:
    """Close the developer's arguments over ``fn`` so the leaf is called as ``fn(utils)``.

    ``utils`` stays the first positional argument, ahead of anything the suite passed,
    so a plain ``functools.partial`` would put it in the wrong slot.
    """
    if not args and not kwargs:
        return fn

    @functools.wraps(fn)
    async def bound(utils: TestUtils) -> Verdict:
        return await fn(utils, *args, **kwargs)

    return bound


def _function(member: object) -> object:
    """Return an instance function; class/static descriptors are not authored methods."""
    if isinstance(member, (classmethod, staticmethod)):
        return None
    return member


def _returns_verdict(member: object) -> bool:
    """Recognize the mandatory test-method return contract without resolving hints."""
    function = _function(member)
    if not inspect.isfunction(function):
        return False
    annotation = inspect.signature(function).return_annotation
    return (
        annotation is Verdict
        or getattr(annotation, "__name__", None) == "Verdict"
        or (
            isinstance(annotation, str)
            and annotation.split("[", 1)[0].rsplit(".", maxsplit=1)[-1] == "Verdict"
        )
    )


def _is_declared_test_method(cls: type[object], name: str, member: object) -> bool:
    """Find a Verdict method, including a specialization that omits its annotation."""
    if _returns_verdict(member):
        return True
    return any(_returns_verdict(base.__dict__.get(name)) for base in cls.__mro__[1:])


class _LitmusTestMeta(type):
    """Freeze an instance only after its most-derived constructor has returned."""

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        instance = super().__call__(*args, **kwargs)
        object.__setattr__(instance, "_frozen", True)
        return instance


class LitmusTest[CtxT: Mapping[str, Any] = Mapping[str, Any]](metaclass=_LitmusTestMeta):
    """A claim about hardware and the procedure that produces its evidence.

    Instances accept configuration during construction and are immutable afterwards.
    Calling :meth:`main` consumes the instance: a second call raises
    :class:`ContractViolation`, preserving the first run as evidence.

    ``CtxT`` is opt-in sugar (issue #75): a suite that narrows ``configure``'s return
    to a local ``TypedDict`` and declares ``class MySuite(LitmusTest[MyCtx])`` gets
    ``utils: TestUtils[MyCtx]`` in ``configure``, ``cleanup``, and any test method that
    asks for it. Composition and internal plumbing (``via``, sequence/parallel
    children, the leaf/sequence/parallel specializations) hold ``LitmusTest[Any]``
    instead of the bare, defaulted alias: they compose nodes of any ``CtxT`` and must
    not force every via-gateway or child onto one suite's context shape.
    """

    __slots__ = (
        "_audits",
        "_children",
        "_configure_only_task",
        "_configure_only_utils",
        "_ctx",
        "_discrete_log_file",
        "_evidence_declared",
        "_fn",
        "_frozen",
        "_has_run",
        "_log_path",
        "_name",
        "_summary_tree",
        "_surveys",
        "_time_budget",
        "_via",
        "_via_lease_count",
        "_via_leased",
    )

    def __init__(
        self,
        *,
        name: str,
        via: LitmusTest[Any] | None = None,
        discrete_log_file: bool = True,
        fail_after: float | None = None,
        abort_after: float | None = None,
    ) -> None:
        """Create a named, not-yet-run test node.

        Args:
            name: Stable node name used by logs, artifacts, and failures.
            via: Optional gateway Testable. The framework brings it up before this
                node's pre-configure survey, routes exec through its SSH hop, and
                tears it down after this node's post-cleanup survey and after the
                shells this node opened have closed. Authors never call
                configure/cleanup on it.
            discrete_log_file: Own a per-node timeline. Set false only when this
                composed node should roll its records into its parent.
            fail_after: Positive finite seconds before this subtree renders Fail.
            abort_after: Positive finite seconds before this subtree renders Aborted.
        """
        self._frozen = False
        self._has_run = False
        self._name = name
        self._via = via
        self._discrete_log_file = discrete_log_file
        self._time_budget = _time_budget(
            name,
            fail_after=fail_after,
            abort_after=abort_after,
        )
        self._log_path: Path | None = None
        self._via_leased = False
        self._via_lease_count = 0
        self._configure_only_task: asyncio.Task[Outcome] | None = None
        self._configure_only_utils: TestUtils | None = None
        self._surveys: list[SurveyHook] = []
        self._audits: list[AuditHook] = []
        self._fn: _TestFunction | None = None
        self._evidence_declared: tuple[str, ...] | None = None
        self._children: tuple[LitmusTest[Any], ...] | None = None
        self._ctx: Mapping[str, Any] = {}
        self._summary_tree: SummaryTreeNode | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject malformed authored classes before an instance can touch hardware."""
        super().__init_subclass__(**kwargs)

        for name in cls.__dict__:
            if name in _SEALED_METHODS or name.startswith("_safe_"):
                msg = (
                    f"LitmusTest.{name} is framework-owned and cannot be overridden; "
                    "rename the method or call the public API instead of replacing it"
                )
                raise TypeError(msg)

        for name in _LIFECYCLE_COROUTINES:
            member = cls.__dict__.get(name)
            if member is not None and not inspect.iscoroutinefunction(_function(member)):
                msg = (
                    f"{cls.__name__}.{name} must be defined with async def; "
                    f"write it as async def {name}(self, utils)"
                )
                raise TypeError(msg)

        for name, member in cls.__dict__.items():
            function = _function(member)
            if (
                inspect.isfunction(function)
                and _is_declared_test_method(cls, name, function)
                and not inspect.iscoroutinefunction(function)
            ):
                msg = (
                    f"{cls.__name__}.{name} must be defined with async def; "
                    f"write it as async def {name}(self, utils)"
                )
                raise TypeError(msg)

        build_suite = inspect.getattr_static(cls, "build_suite")
        if build_suite is LitmusTest.build_suite:
            msg = (
                f"{cls.__name__}.build_suite must be overridden; "
                "define def build_suite(self) -> LitmusTest on the subclass"
            )
            raise TypeError(msg)
        build_suite_function = _function(build_suite)
        if not inspect.isfunction(build_suite_function):
            msg = (
                f"{cls.__name__}.build_suite must be defined with def; "
                "assign a method, not a data attribute"
            )
            raise TypeError(msg)
        if inspect.iscoroutinefunction(build_suite_function):
            msg = (
                f"{cls.__name__}.build_suite must be defined with def, not async def; "
                "build_suite is a pure table of contents and must not await"
            )
            raise TypeError(msg)

    def __setattr__(self, name: str, value: object) -> None:
        """Permit author state only while the most-derived constructor is running."""
        if getattr(self, "_frozen", False):
            msg = f"{type(self).__name__} is immutable after construction"
            raise AttributeError(msg)
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        """Apply the immutable-self contract to deletion as well as assignment."""
        if getattr(self, "_frozen", False):
            msg = f"{type(self).__name__} is immutable after construction"
            raise AttributeError(msg)
        object.__delattr__(self, name)

    @property
    def name(self) -> str:
        """The stable node name used by logs, artifacts, and failures."""
        return self._name

    @property
    def discrete_log_file(self) -> bool:
        """Whether this node owns a timeline instead of rolling into its parent."""
        return self._discrete_log_file

    @property
    def log_path(self) -> Path | None:
        """Authoritative JSON Lines path, or ``None`` for a rolled-up/pending node."""
        return self._log_path

    @property
    def run_data(self) -> JsonObject:
        """A detached JSON-safe snapshot of this node's serializable context.

        This is the ctx view: what ``configure()`` provided, minus anything that is
        not JSON. A leaf's ``RunResult.run_data`` is this view plus the evidence its
        verdict carried; the merge happens when the leaf settles, so an override here
        sees only the context (issue #118).
        """
        return snapshot_run_data(self._ctx)

    def test(
        self,
        name: str,
        fn: _TestFunction,
        /,
        *args: object,
        fail_after: float | None = None,
        abort_after: float | None = None,
        **kwargs: object,
    ) -> LitmusTest[Any]:
        """Create the leaf node: a name, a verdict method, and that method's arguments.

        The framework calls ``fn(utils, *args, **kwargs)``. A suite stamps out one leaf
        per core, rail, or channel from a single method by naming each leaf and passing
        the value that varies. ``fail_after`` and ``abort_after`` are the leaf's budget
        and never reach ``fn``; a test method cannot take parameters of those names.

        The arguments are checked against ``fn``'s signature here, at composition time,
        so a leaf that could never be called fails ``build_suite`` before any hardware
        is touched. Static checkers cannot see through this call (PEP 612 forbids
        keyword parameters between ``*args`` and ``**kwargs`` of a ``ParamSpec``).
        """
        if not inspect.iscoroutinefunction(fn):
            method = getattr(fn, "__qualname__", repr(fn))
            msg = (
                f"test method {method} must be defined with async def; "
                "write it as async def and return PASS or raise TestFailure"
            )
            raise TypeError(msg)
        try:
            inspect.signature(fn).bind(_UTILS_PLACEHOLDER, *args, **kwargs)
        except TypeError as error:
            method = getattr(fn, "__qualname__", repr(fn))
            raise ContractViolation(
                name,
                f"arguments do not fit test method {method} ({error}); "
                "pass arguments that match the method (utils is supplied by the framework)",
            ) from error
        return _Test(
            name=name,
            fn=_bind_test_arguments(fn, args, kwargs),
            fail_after=fail_after,
            abort_after=abort_after,
            evidence_declared=declared_evidence(fn),
        )

    def parallel(
        self,
        name: str,
        children: Sequence[LitmusTest[Any]],
        *,
        discrete_log_file: bool = True,
        fail_after: float | None = None,
        abort_after: float | None = None,
    ) -> LitmusTest[Any]:
        """Compose independent children under one structured-concurrency owner."""
        if not children:
            raise ContractViolation(
                name,
                "a parallel node must contain at least one child; "
                "pass one or more LitmusTest instances",
            )
        return _Parallel(
            name=name,
            children=children,
            discrete_log_file=discrete_log_file,
            fail_after=fail_after,
            abort_after=abort_after,
        )

    def sequence(
        self,
        name: str,
        children: Sequence[LitmusTest[Any]],
        *,
        discrete_log_file: bool = True,
        fail_after: float | None = None,
        abort_after: float | None = None,
    ) -> LitmusTest[Any]:
        """Compose dependent children that stop at the first non-pass verdict."""
        if not children:
            raise ContractViolation(
                name,
                "a sequence must contain at least one child; pass one or more LitmusTest instances",
            )
        return _Sequence(
            name=name,
            children=children,
            discrete_log_file=discrete_log_file,
            fail_after=fail_after,
            abort_after=abort_after,
        )

    @final
    def use_survey(self, fn: HookFunction, *, phases: Iterable[Phase] | None = None) -> None:
        """Register a survey as immutable test identity during construction.

        ``phases`` names the dispatch points the survey observes; ``None`` means all
        six. An empty selection is a ``ValueError`` here, not a survey that never
        fires. The selection is the schedule, not a guarantee of reach: a boundary the
        lifecycle never arrives at is never dispatched, and a boundary it arrives at
        without a device route still is (see ``Phase``).
        """
        if self._frozen:
            raise ContractViolation(
                self.name,
                "hooks may be registered only during construction; "
                "call use_survey or use_audit from __init__ after super().__init__",
            )
        if phases is None:
            selected = frozenset(Phase)
        else:
            selected_phases: set[Phase] = set()
            for phase in phases:
                try:
                    selected_phases.add(Phase(phase))
                except ValueError:
                    names = ", ".join(member.value for member in Phase)
                    raise ValueError(
                        f"{phase!r} is not a survey phase; pass one of {names}, "
                        "or omit phases to observe every boundary"
                    ) from None
            selected = frozenset(selected_phases)
        self._surveys.append(SurveyHook(fn=fn, phases=selected))

    @final
    def use_audit(self, *, interval: float, fn: HookFunction) -> None:
        """Register an interval audit as immutable test identity during construction."""
        if self._frozen:
            raise ContractViolation(
                self.name,
                "hooks may be registered only during construction; "
                "call use_survey or use_audit from __init__ after super().__init__",
            )
        self._audits.append(AuditHook(interval=interval, fn=fn))

    async def configure(self, utils: TestUtils[CtxT]) -> CtxT:
        """Prepare the test and return context for its suite and cleanup.

        The default returns an empty mapping typed as ``CtxT`` for the unparameterized
        case; the cast is the framework's, not a suite author's — see issue #75.
        """
        return cast(CtxT, {})

    def build_suite(self) -> LitmusTest[Any]:
        """Return this node's authored composition.

        Subclasses provide a pure table of contents assembled with ``self.test()``,
        ``self.sequence()``, and ``self.parallel()``.
        """
        detail = (
            "build_suite() must return a test, sequence, or parallel composition; "
            "return self.test(...), self.sequence(...), or self.parallel(...)"
        )
        raise ContractViolation(self.name, detail)

    async def cleanup(self, utils: TestUtils[CtxT]) -> None:
        """Restore state after the suite settles, regardless of its outcome."""

    @final
    async def run(
        self,
        *,
        env: Mapping[str, str] | None = None,
        env_allow: Iterable[str] | None = None,
        env_deny: Iterable[str] | None = None,
        before_each: BeforeEachHook | None = None,
        after_each: AfterEachHook | None = None,
        allow_unpaired_hooks: bool = False,
        fail_after: float | None = None,
        abort_after: float | None = None,
        output_budget: int = DEFAULT_EXEC_OUTPUT_BUDGET,
    ) -> RunResult:
        """Execute once, fanning root-owned integration hooks across every node.

        ``env_allow`` / ``env_deny`` select which environment variables the run
        publishes; see ``TestUtils`` for the contract. Raises ``ValueError`` when both
        are given. ``output_budget`` is the combined stdout+stderr ceiling for every
        ``exec`` this run issues, local or SSH.
        """
        run_budget = _time_budget(
            self.name,
            fail_after=fail_after,
            abort_after=abort_after,
        )
        if run_budget is not None and self._time_budget is not None:
            raise ContractViolation(
                self.name,
                "a root time budget is already declared; declare exactly one "
                "fail_after or abort_after across construction and run",
            )
        hooks = IntegrationHooks(
            before_each=before_each,
            after_each=after_each,
            allow_unpaired=allow_unpaired_hooks,
        )
        utils = TestUtils(
            env=env,
            env_allow=env_allow,
            env_deny=env_deny,
            output_budget=output_budget,
            _integration_hooks=hooks,
        )
        return await self._execute_run(utils, run_budget)

    @final
    async def main(self, utils: TestUtils | None = None) -> RunResult:
        """Execute the sealed lifecycle with an optional framework test seed."""
        seed = TestUtils() if utils is None else utils
        return await self._execute_run(seed, None)

    @final
    async def _execute_run(self, seed: TestUtils, run_budget: TimeBudget | None) -> RunResult:
        """Execute one full run with an optional root declaration from ``run()``."""
        runtime = node_runtime(seed)
        await runtime.begin_run()
        run_abort = RunAbort()
        extra = () if run_budget is None else (run_budget,)
        loop = asyncio.get_running_loop()
        lifecycle: asyncio.Task[RunResult | None] | None = None
        guard: _OperatorSignalGuard | None = None
        escaped: BaseException | None = None

        async def execute() -> RunResult | None:
            nonlocal escaped
            try:
                return await self._run_lifecycle(
                    seed,
                    run_abort,
                    isolate_context=False,
                    extra_time_budgets=extra,
                )
            except BaseException as exc:
                # Task.__step__ re-raises KeyboardInterrupt/SystemExit into the loop
                # and would skip _execute_run's finally. Swallow here; re-raise after cleanup.
                if type(exc) is KeyboardInterrupt or type(exc) is SystemExit:
                    escaped = exc
                    return None
                raise

        try:
            lifecycle = asyncio.create_task(execute())
            guard = _OperatorSignalGuard(run_abort, runtime, lifecycle)
            guard.install(loop)
            result = await lifecycle
            await guard.drain_close()
            if escaped is not None:
                raise escaped
            if result is None:
                raise FrameworkContractViolation(
                    self.name, detail="the live run settled without a result"
                )
            if run_abort.fired:
                # Sweep once more at the root so a transport that settled open while its
                # task was being cancelled cannot outlive the returned Aborted tree.
                await runtime.cancel_in_flight()
            return result
        finally:
            if lifecycle is not None and not lifecycle.done():
                lifecycle.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await lifecycle
            if guard is not None:
                # Hard-close while the second-signal handler is still installed, then
                # drop it. A SIGINT during drain must escalate, not become KeyboardInterrupt.
                await guard.drain_close()
                guard.remove(loop)
            await runtime.end_run()

    @final
    async def run_configure_only(self, utils: TestUtils | None = None) -> dict[str, Any]:
        """Bring this node and its via up without running the suite.

        Idempotent. Framework-owned: Inspector isolation and via-device bring-up.
        Suite authors pass ``via=`` at construction instead of calling this.
        A declared ``abort_after`` expiry raises ``TimeoutError`` on this imperative
        adapter; internal consumers retain and adopt the structured Aborted outcome.
        """
        seed = TestUtils() if utils is None else utils
        try:
            outcome = await self._run_configure_only(seed, RunAbort())
        except asyncio.CancelledError:
            # Shielded waiters do not cancel the shared via handshake. This node's
            # own bring-up still has to stop so configure does not run after cancel.
            task = self._configure_only_task
            if task is not None and not task.done():
                task.cancel()
            raise
        if outcome.state is None:
            configured = self._configure_only_utils
            if configured is None:
                return {}
            return dict(configured.ctx)
        if outcome.failure is not None:
            raise outcome.failure
        if outcome.timeout is not None and outcome.state is State.ABORTED:
            raise TimeoutError(
                f"{self.name} configure-only expired: "
                f"{outcome.timeout.declared}={outcome.timeout.budget_seconds:g}s"
            )
        raise FrameworkContractViolation(
            self.name,
            detail="configure-only settled in a terminal state without a failure",
        )

    @final
    async def run_cleanup_only(self) -> None:
        """Tear this node and its via down after :meth:`run_configure_only`.

        Idempotent. Paired with :meth:`run_configure_only`; a full :meth:`run`
        already performs this teardown.
        """
        await self._run_cleanup_only()

    @final
    async def _run_lifecycle(
        self,
        utils: TestUtils,
        run_abort: RunAbort,
        *,
        isolate_context: bool = True,
        audit_signal: AuditSignal | None = None,
        time_budget_clocks: tuple[TimeBudgetClock, ...] = (),
        extra_time_budgets: tuple[TimeBudget, ...] = (),
    ) -> RunResult:
        """Execute one node while preserving the tree's shared halt signal."""
        if self._has_run:
            raise ContractViolation(
                self.name,
                "a LitmusTest instance may run only once; construct a new instance for each run",
            )
        if self._configure_only_utils is not None:
            raise ContractViolation(
                self.name,
                "this instance is already up as a via target; "
                "construct a separate instance to run, or tear the hop down first",
            )
        object.__setattr__(self, "_has_run", True)

        children = self._children
        fn = self._fn
        authored_instance = children is None and fn is None
        node_audit_signal = AuditSignal(audit_signal) if self._audits else audit_signal
        runtime = node_runtime(utils).for_node(
            self.name,
            run_abort,
            discrete_log_file=self.discrete_log_file,
            isolate_context=isolate_context and authored_instance,
            audit_signal=node_audit_signal,
        )
        utils = runtime.utils
        object.__setattr__(self, "_ctx", utils.ctx)
        runtime.own_context(self.name)
        object.__setattr__(self, "_log_path", runtime.owned_log_path())

        integration_hooks = runtime.integration_hooks
        boundary = ChildBoundary(
            self.name,
            run_abort,
            utils.logger,
            node_audit_signal,
            time_budget_clocks=time_budget_clocks,
        )
        after_each_started = False
        summary_children: tuple[SummaryTreeNode, ...] = ()
        settled_outcome: Outcome | None = None
        settled_result: RunResult | None = None
        if len(utils.logger.scope) == 1:
            integration_hooks.warn_if_unpaired(utils.logger)
        try:
            if integration_hooks.before_each is not None:
                await boundary.run(
                    Slot.BEFORE_EACH,
                    invoke_integration_hook(
                        "before_each",
                        integration_hooks.before_each,
                        BeforeEachContext(
                            name=self.name,
                            ancestor_names=utils.logger.scope,
                            bread_crumbs=utils.logger.bread_crumbs,
                            logger=utils.logger,
                            env=utils.env,
                        ),
                    ),
                )

            if not authored_instance:
                # A composite or leaf node declares no route of its own, so its topology
                # is final the moment its utils exist.
                runtime.log_exec_routing()

            if children is not None:

                async def run_composite(
                    active_clocks: tuple[TimeBudgetClock, ...],
                    _lineage: object,
                ) -> RunResult:
                    nonlocal boundary
                    boundary = ChildBoundary(
                        self.name,
                        run_abort,
                        utils.logger,
                        node_audit_signal,
                        time_budget_clocks=active_clocks,
                    )
                    if isinstance(self, _Sequence):
                        return await self._run_sequence(
                            utils,
                            run_abort,
                            children,
                            audit_signal=node_audit_signal,
                            time_budget_clocks=active_clocks,
                        )
                    return await self._run_parallel(
                        utils,
                        run_abort,
                        children,
                        audit_signal=node_audit_signal,
                        time_budget_clocks=active_clocks,
                    )

                value, timeout = await self._run_timed_window(
                    utils,
                    run_abort,
                    time_budget_clocks,
                    extra_time_budgets,
                    run_composite,
                )
                if timeout is not None:
                    summary_children = self._pending_children()
                    result = self._to_result(
                        self._deadline_outcome(
                            timeout,
                            children=summary_children,
                        ),
                        utils,
                        children=summary_children,
                    )
                elif not isinstance(value, RunResult):
                    raise FrameworkContractViolation(
                        self.name,
                        detail="a composite time window settled without a RunResult",
                    )
                else:
                    result = value
                summary_children = result.summary_tree.children
                settled_result = result
                after_each_started = True
                result = await self._run_after_each(boundary, utils, run_abort, result)
                return await self._finish_result(result, utils)

            if fn is not None:

                async def run_case(
                    active_clocks: tuple[TimeBudgetClock, ...],
                    lineage: object,
                ) -> Outcome:
                    nonlocal boundary
                    boundary = ChildBoundary(
                        self.name,
                        run_abort,
                        utils.logger,
                        node_audit_signal,
                        time_budget_clocks=active_clocks,
                        evidence_contract=EvidenceContract(
                            declared=self._evidence_declared,
                            reserved=frozenset(utils.ctx),
                        ),
                    )
                    async with _active_time_budgets(active_clocks, lineage):
                        # The test method is one of the two slots that may measure.
                        # Binding the node here, and only here, is what makes
                        # `litmus.measure` find this node and what refuses a call from
                        # cleanup or an audit task, which run outside the binding.
                        measuring = current_utils.set(utils)
                        try:
                            return await boundary.run(Slot.TEST_METHOD, fn(utils))
                        finally:
                            current_utils.reset(measuring)

                value, timeout = await self._run_timed_window(
                    utils,
                    run_abort,
                    time_budget_clocks,
                    extra_time_budgets,
                    run_case,
                )
                if timeout is not None:
                    slot = (
                        boundary.outcome.slot
                        if boundary.outcome is not None and boundary.outcome.timeout == timeout
                        else Slot.TEST_METHOD
                    )
                    outcome = self._deadline_outcome(timeout, slot=slot)
                elif not isinstance(value, Outcome):
                    raise FrameworkContractViolation(
                        self.name,
                        detail="a test time window settled without an Outcome",
                    )
                else:
                    outcome = value
                result = self._to_result(outcome, utils)
                settled_result = result
                after_each_started = True
                result = await self._run_after_each(boundary, utils, run_abort, result)
                return await self._finish_result(result, utils)

            ran_test = False
            suite: LitmusTest[Any] | None = None
            try:
                # Hop first, teardown last. The test budget begins at configure,
                # after the borrowed via and pre-configure survey are ready.
                via_failed = await self._bring_up_via(utils, run_abort)
                if via_failed is None:
                    await self._run_surveys(boundary, utils, Phase.PRE_CONFIGURE)
                if via_failed is not None:
                    outcome = self._adopt_via_outcome(via_failed)
                else:

                    async def run_authored(
                        active_clocks: tuple[TimeBudgetClock, ...],
                        lineage: object,
                    ) -> Outcome:
                        nonlocal boundary, ran_test, suite, summary_children
                        boundary = ChildBoundary(
                            self.name,
                            run_abort,
                            utils.logger,
                            node_audit_signal,
                            time_budget_clocks=active_clocks,
                        )
                        async with _active_time_budgets(active_clocks, lineage):
                            configured = await boundary.run(
                                Slot.CONFIGURE, self._configure_and_merge_context(utils)
                            )
                            if configured.state is not None:
                                return configured
                            runtime.log_exec_routing()
                            await self._run_surveys(boundary, utils, Phase.POST_CONFIGURE)
                            suite = self.build_suite()
                            await self._run_surveys(boundary, utils, Phase.PRE_TEST)
                        if self._audits:
                            if node_audit_signal is None:
                                raise FrameworkContractViolation(
                                    self.name,
                                    detail="registered audits have no cancellation signal",
                                )
                            suite_result, audit_outcome = await self._run_suite_with_audits(
                                suite,
                                utils,
                                run_abort,
                                node_audit_signal,
                                time_budget_clocks=active_clocks,
                            )
                        else:
                            suite_result = await suite._run_lifecycle(
                                utils,
                                run_abort,
                                audit_signal=node_audit_signal,
                                time_budget_clocks=active_clocks,
                            )
                            audit_outcome = None
                        ran_test = True
                        if audit_outcome is not None:
                            summary_children = (suite._pending_summary_tree(),)
                            return audit_outcome
                        if suite_result is None:
                            raise FrameworkContractViolation(
                                self.name,
                                detail="suite settled without a result or audit verdict",
                            )
                        summary_children = (suite_result.summary_tree,)
                        return Outcome(
                            Slot.TEST_METHOD,
                            self.name,
                            suite_result.state,
                            failure=suite_result.failure,
                            value=suite_result.value,
                            timeout=suite_result.timeout,
                        )

                    value, timeout = await self._run_timed_window(
                        utils,
                        run_abort,
                        time_budget_clocks,
                        extra_time_budgets,
                        run_authored,
                    )
                    if timeout is not None:
                        if suite is not None:
                            summary_children = (suite._pending_summary_tree(),)
                        else:
                            summary_children = self._pending_children()
                        slot = (
                            boundary.outcome.slot
                            if boundary.outcome is not None and boundary.outcome.timeout == timeout
                            else Slot.TEST_METHOD
                        )
                        outcome = self._deadline_outcome(
                            timeout,
                            slot=slot,
                            children=summary_children,
                        )
                    elif not isinstance(value, Outcome):
                        raise FrameworkContractViolation(
                            self.name,
                            detail="an authored time window settled without an Outcome",
                        )
                    else:
                        outcome = value
                settled_outcome = outcome
            finally:
                try:
                    if ran_test:
                        await self._run_surveys(boundary, utils, Phase.POST_TEST)
                finally:
                    try:
                        await self._run_surveys(boundary, utils, Phase.PRE_CLEANUP)
                    finally:
                        try:
                            await self._run_cleanup(boundary, utils)
                        finally:
                            try:
                                await self._run_surveys(boundary, utils, Phase.POST_CLEANUP)
                            finally:
                                # Owned shells outlive the post-cleanup survey and close
                                # before the hop they may tunnel through (#115).
                                try:
                                    await runtime.disconnect_opened()
                                finally:
                                    await self._tear_down_via()
            result = self._to_result(outcome, utils, children=summary_children)
            settled_result = result
            after_each_started = True
            result = await self._run_after_each(boundary, utils, run_abort, result)
            return await self._finish_result(result, utils)
        except ExternalIntegrationFailure:
            settled = asyncio.create_task(
                self._settle_integration_abort(
                    boundary,
                    utils,
                    run_abort,
                    children=summary_children or self._pending_children(),
                    after_each_started=after_each_started,
                )
            )
            try:
                await asyncio.shield(settled)
            except asyncio.CancelledError:
                # The boundary fired the run abort before raising, so this cancellation
                # is that same halt arriving from a parallel parent, not a caller's.
                # Section 4.2: repeated cancellations must not abandon the wait — the
                # started row this node opened is owed its finished row.
                while not settled.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.shield(settled)
            return settled.result()
        except asyncio.CancelledError as error:
            if run_abort.fired:
                result = self._aborted_result(
                    boundary,
                    utils,
                    run_abort,
                    children=summary_children or self._pending_children(),
                )
            elif node_audit_signal is not None and node_audit_signal.reason is not None:
                # An audit cancels only its subtree, but each started node still owes
                # the integrator its completion and the Inspector its failure evidence.
                await runtime.cancel_opened()
                if after_each_started:
                    if settled_result is None:
                        raise FrameworkContractViolation(
                            self.name,
                            detail="after_each began without a settled node result",
                        ) from error
                    return await self._finish_result(settled_result, utils)
                slot = Slot.TEST_METHOD if boundary.outcome is None else boundary.outcome.slot
                result = self._to_result(
                    Outcome(slot, self.name, State.FAIL, failure=node_audit_signal.reason),
                    utils,
                    children=summary_children or self._pending_children(),
                )
                # This is a new lifecycle call, not suite code suppressing the
                # delivered cancellation. Give its boundary a fresh task so a real
                # integration error cannot be mistaken for that stale cancellation.
                completion = asyncio.create_task(
                    self._run_after_each(boundary, utils, run_abort, result)
                )
                return await self._finish_result(await completion, utils)
            elif _consume_deadline_cancellation(error):
                if after_each_started:
                    if settled_result is None:
                        raise FrameworkContractViolation(
                            self.name,
                            detail="after_each began without a settled node result",
                        ) from error
                    return await self._finish_result(settled_result, utils)
                if settled_outcome is not None:
                    result = self._to_result(
                        settled_outcome,
                        utils,
                        children=summary_children,
                    )
                else:
                    fired = [clock for clock in time_budget_clocks if clock.fired]
                    if not fired:
                        raise
                    clock = min(
                        fired,
                        key=lambda candidate: candidate.fired_at or float("inf"),
                    )
                    await runtime.cancel_opened()
                    timeout = clock.evidence()
                    slot = (
                        boundary.outcome.slot
                        if boundary.outcome is not None and boundary.outcome.timeout == timeout
                        else Slot.TEST_METHOD
                    )
                    summary_children = summary_children or self._pending_children()
                    result = self._to_result(
                        self._deadline_outcome(
                            timeout,
                            slot=slot,
                            children=summary_children,
                        ),
                        utils,
                        children=summary_children,
                    )
            else:
                raise
            if after_each_started:
                return await self._finish_result(result, utils)
            return await self._finish_result(
                await self._run_after_each(boundary, utils, run_abort, result),
                utils,
            )
        except BaseException:
            runtime.close_timeline()
            raise

    @final
    async def _run_timed_window(
        self,
        utils: TestUtils,
        run_abort: RunAbort,
        inherited_clocks: tuple[TimeBudgetClock, ...],
        extra_budgets: tuple[TimeBudget, ...],
        call: _TimedCall,
    ) -> tuple[object | None, TimeoutEvidence | None]:
        """Race one configure-through-test window against every active clock."""
        declarations = (*extra_budgets, *((self._time_budget,) if self._time_budget else ()))
        owned_clocks = tuple(
            TimeBudgetClock(budget, clock=utils.logger.bread_crumbs) for budget in declarations
        )
        active_clocks = (*inherited_clocks, *owned_clocks)
        settled_at: list[float | None] = [None]
        lineage = object()
        escaped: BaseException | None = None

        async def tracked_call() -> object:
            nonlocal escaped
            try:
                return await call(active_clocks, lineage)
            except BaseException as exc:
                if type(exc) is KeyboardInterrupt or type(exc) is SystemExit:
                    escaped = exc
                    return None
                raise
            finally:
                settled_at[0] = asyncio.get_running_loop().time()

        async def result_of_core() -> object:
            value = await core
            if escaped is not None:
                raise escaped
            return value

        core = asyncio.create_task(tracked_call())
        watchers = [asyncio.create_task(clock.event.wait()) for clock in active_clocks]
        try:
            if not watchers:
                return await result_of_core(), None
            await asyncio.wait([core, *watchers], return_when=asyncio.FIRST_COMPLETED)
            if run_abort.fired:
                # A harness halt is run-wide and class-intact. If its settlement and
                # a node clock become observable in the same loop turn, the deadline
                # must not consume the integration failure as a node-local cut.
                if not core.done():
                    core.cancel()
                return await result_of_core(), None
            fired = [
                clock
                for clock in active_clocks
                if clock.fired
                and (
                    settled_at[0] is None
                    or (clock.fired_at is not None and clock.fired_at <= settled_at[0])
                )
            ]
            if not fired:
                return await result_of_core(), None
            clock = min(fired, key=lambda candidate: candidate.fired_at or float("inf"))
            await self._settle_deadline_cut(core, utils)
            evidence = clock.evidence()
            utils.logger.error(
                "time budget expired: reason=%s declared=%s budget=%gs elapsed=%gs clock=%s",
                evidence.reason,
                evidence.declared,
                evidence.budget_seconds,
                evidence.elapsed_seconds,
                evidence.clock,
            )
            return None, evidence
        except asyncio.CancelledError as error:
            if run_abort.fired:
                raise
            if not _consume_deadline_cancellation(error):
                raise
            fired = [clock for clock in active_clocks if clock.fired]
            if not fired:
                raise
            clock = min(fired, key=lambda candidate: candidate.fired_at or float("inf"))
            await self._settle_deadline_cut(core, utils)
            evidence = clock.evidence()
            utils.logger.error(
                "time budget expired: reason=%s declared=%s budget=%gs elapsed=%gs clock=%s",
                evidence.reason,
                evidence.declared,
                evidence.budget_seconds,
                evidence.elapsed_seconds,
                evidence.clock,
            )
            return None, evidence
        finally:
            for watcher in watchers:
                if not watcher.done():
                    watcher.cancel()
            if watchers:
                await asyncio.gather(*watchers, return_exceptions=True)
            for clock in owned_clocks:
                clock.close()

    @final
    async def _settle_deadline_cut(
        self,
        core: asyncio.Task[object],
        utils: TestUtils,
    ) -> None:
        """Cancel the coroutine and the node-owned resources before returning evidence."""
        _cancel_for_deadline(core)

        async def settle() -> None:
            await node_runtime(utils).cancel_opened()
            await asyncio.gather(core, return_exceptions=True)

        settling = asyncio.create_task(settle())
        try:
            await asyncio.shield(settling)
        except asyncio.CancelledError:
            while not settling.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(settling)
            raise

    @final
    def _deadline_outcome(
        self,
        timeout: TimeoutEvidence,
        *,
        slot: Slot = Slot.TEST_METHOD,
        children: tuple[SummaryTreeNode, ...] = (),
    ) -> Outcome:
        """Apply timeout state and ordinary composite precedence to a cut node."""
        aborted = next((child for child in children if child.state is State.ABORTED), None)
        if aborted is not None:
            return Outcome(
                slot,
                self.name,
                State.ABORTED,
                failure=aborted.failure,
                timeout=timeout,
            )
        if timeout.declared == "abort_after":
            return Outcome(
                slot,
                self.name,
                State.ABORTED,
                timeout=timeout,
            )
        return Outcome(
            slot,
            self.name,
            State.FAIL,
            failure=self._deadline_failure(timeout),
            timeout=timeout,
        )

    @final
    def _deadline_failure(self, timeout: TimeoutEvidence) -> TestFailure:
        """Describe a fail-after expiry without introducing another failure class."""
        return TestFailure(
            f"time budget exceeded: {timeout.declared}={timeout.budget_seconds:g}s "
            f"at {timeout.clock}",
            {"budget_seconds": timeout.budget_seconds},
            {
                "reason": timeout.reason,
                "elapsed_seconds": timeout.elapsed_seconds,
                "clock": timeout.clock,
            },
        )

    @final
    async def _run_after_each(
        self,
        boundary: ChildBoundary,
        utils: TestUtils,
        run_abort: RunAbort,
        result: RunResult,
    ) -> RunResult:
        """Dispatch the root-owned completion hook for one settled node."""
        integration_hooks = node_runtime(utils).integration_hooks
        if integration_hooks.after_each is not None:
            try:
                await boundary.run(
                    Slot.AFTER_EACH,
                    invoke_integration_hook(
                        "after_each",
                        integration_hooks.after_each,
                        AfterEachContext(
                            name=self.name,
                            ancestor_names=utils.logger.scope,
                            bread_crumbs=utils.logger.bread_crumbs,
                            logger=utils.logger,
                            env=utils.env,
                            result=result,
                        ),
                    ),
                )
            except ExternalIntegrationFailure:
                await node_runtime(utils).cancel_in_flight()
                return self._aborted_result(
                    boundary,
                    utils,
                    run_abort,
                    children=result.summary_tree.children,
                )
            except asyncio.CancelledError:
                if not run_abort.fired:
                    raise
                return self._aborted_result(
                    boundary,
                    utils,
                    run_abort,
                    children=result.summary_tree.children,
                )
        return result

    @final
    async def _settle_integration_abort(
        self,
        boundary: ChildBoundary,
        utils: TestUtils,
        run_abort: RunAbort,
        *,
        children: tuple[SummaryTreeNode, ...],
        after_each_started: bool,
    ) -> RunResult:
        """Hard-close this node and pay the completion it already owes the integrator."""
        await node_runtime(utils).cancel_in_flight()
        result = self._aborted_result(boundary, utils, run_abort, children=children)
        if not after_each_started:
            result = await self._run_after_each(boundary, utils, run_abort, result)
        return await self._finish_result(result, utils)

    @final
    def _aborted_result(
        self,
        boundary: ChildBoundary,
        utils: TestUtils,
        run_abort: RunAbort,
        *,
        children: tuple[SummaryTreeNode, ...],
    ) -> RunResult:
        """Seal the shared halt reason at this node without rewrapping it."""
        reason = run_abort.reason
        if reason is None:
            raise FrameworkContractViolation(
                self.name,
                detail="a run abort reached a node without its reason",
            )
        boundary_outcome = boundary.outcome
        slot = Slot.TEST_METHOD if boundary_outcome is None else boundary_outcome.slot
        return self._to_result(
            Outcome(slot, self.name, State.ABORTED, failure=reason),
            utils,
            children=children,
        )

    @final
    async def _run_parallel(
        self,
        utils: TestUtils,
        run_abort: RunAbort,
        children: tuple[LitmusTest[Any], ...],
        *,
        audit_signal: AuditSignal | None = None,
        time_budget_clocks: tuple[TimeBudgetClock, ...] = (),
    ) -> RunResult:
        """Run every child to settlement unless a harness-level halt escapes."""
        tasks: list[asyncio.Task[RunResult]] = []
        try:
            async with asyncio.TaskGroup() as task_group:
                tasks.extend(
                    task_group.create_task(
                        child._run_lifecycle(
                            utils,
                            run_abort,
                            audit_signal=audit_signal,
                            time_budget_clocks=time_budget_clocks,
                        )
                    )
                    for child in children
                )
                active = set(tasks)
                try:
                    while active:
                        done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                        active.difference_update(done)
                        if run_abort.fired:
                            for task in active:
                                task.cancel()
                            break
                except asyncio.CancelledError as error:
                    if error.args and error.args[0] is _DEADLINE_CANCELLATION:
                        for task in active:
                            # TaskGroup issues its own second cancel while unwinding.
                            _cancel_for_deadline(task, requests=2)
                    else:
                        message = error.args[0] if error.args else None
                        for task in active:
                            task.cancel(message)
                    raise
        except* ExternalIntegrationFailure as failures:
            # TaskGroup has already cancelled and joined every sibling. Restore the
            # taxonomy's class-intact halt instead of leaking an ExceptionGroup.
            raise failures.exceptions[0] from None

        outcomes = tuple(task.result() for task in tasks)
        if all(outcome.state is State.PASS for outcome in outcomes):
            aggregate = Outcome(Slot.TEST_METHOD, self.name, State.PASS, value=outcomes)
            return self._to_result(
                aggregate,
                utils,
                children=tuple(outcome.summary_tree for outcome in outcomes),
            )

        # Aborted outranks Fail, and the first child in that state tells the parallel node's story.
        if any(outcome.state is State.ABORTED for outcome in outcomes):
            state = State.ABORTED
            reported = next(outcome for outcome in outcomes if outcome.state is State.ABORTED)
        else:
            state = State.FAIL
            reported = next(outcome for outcome in outcomes if outcome.state is not State.PASS)
        aggregate = Outcome(
            Slot.TEST_METHOD,
            self.name,
            state,
            failure=reported.failure,
            value=outcomes,
            timeout=reported.timeout,
        )
        return self._to_result(
            aggregate,
            utils,
            children=tuple(outcome.summary_tree for outcome in outcomes),
        )

    @final
    async def _run_sequence(
        self,
        utils: TestUtils,
        run_abort: RunAbort,
        children: tuple[LitmusTest[Any], ...],
        *,
        audit_signal: AuditSignal | None = None,
        time_budget_clocks: tuple[TimeBudgetClock, ...] = (),
    ) -> RunResult:
        """Run dependent children in order and stop at the first non-pass verdict."""
        outcomes: list[RunResult] = []
        for index, child in enumerate(children):
            outcome = await child._run_lifecycle(
                utils,
                run_abort,
                audit_signal=audit_signal,
                time_budget_clocks=time_budget_clocks,
            )
            outcomes.append(outcome)
            if outcome.state in (State.FAIL, State.ABORTED):
                aggregate = Outcome(
                    Slot.TEST_METHOD,
                    self.name,
                    outcome.state,
                    failure=outcome.failure,
                    value=tuple(outcomes),
                    timeout=outcome.timeout,
                )
                pending = tuple(child._pending_summary_tree() for child in children[index + 1 :])
                return self._to_result(
                    aggregate,
                    utils,
                    children=(
                        *(result.summary_tree for result in outcomes),
                        *pending,
                    ),
                )
            if outcome.state is not State.PASS:
                raise FrameworkContractViolation(
                    self.name,
                    detail="a sequence child settled without a terminal state",
                )

        all_ran = len(outcomes) == len(children)
        all_passed = all(outcome.state is State.PASS for outcome in outcomes)
        if not (all_ran and all_passed):
            raise FrameworkContractViolation(
                self.name,
                detail="a sequence reached no explicit terminal verdict",
            )
        aggregate = Outcome(Slot.TEST_METHOD, self.name, State.PASS, value=tuple(outcomes))
        return self._to_result(
            aggregate,
            utils,
            children=tuple(outcome.summary_tree for outcome in outcomes),
        )

    @final
    def _node_type(self) -> NodeType:
        """Name the framework-owned node shape for the Inspector contract."""
        if isinstance(self, _Test):
            return NodeType.TEST
        if isinstance(self, _Parallel):
            return NodeType.PARALLEL
        if isinstance(self, _Sequence):
            return NodeType.SEQUENCE
        return NodeType.SUITE

    @final
    def _pending_summary_tree(self) -> SummaryTreeNode:
        """Return sealed evidence, or describe a declared node that did not execute."""
        if self._summary_tree is not None:
            return self._summary_tree
        children = self._children
        if children is not None:
            child_trees = tuple(child._pending_summary_tree() for child in children)
        elif self._fn is not None:
            child_trees = ()
        else:
            # build_suite is a pure table of contents by contract. Inspecting it here
            # preserves the entire declared tree without touching lifecycle methods.
            child_trees = (self.build_suite()._pending_summary_tree(),)
        return SummaryTreeNode(
            name=self.name,
            type=self._node_type(),
            state=None,
            children=child_trees,
            evidence_declared=self._evidence_declared,
        )

    @final
    def _pending_children(self) -> tuple[SummaryTreeNode, ...]:
        """Describe descendants that had no chance to produce terminal evidence."""
        return self._pending_summary_tree().children

    @final
    def _to_result(
        self,
        outcome: Outcome,
        utils: TestUtils,
        *,
        children: tuple[SummaryTreeNode, ...] = (),
    ) -> RunResult:
        """Seal one terminal boundary outcome into self-contained run evidence."""
        if outcome.state is None:
            raise FrameworkContractViolation(
                self.name,
                detail="a completed test node settled without a terminal state",
            )
        node_type = self._node_type()
        run_data = self.run_data
        if outcome.evidence is not None:
            run_data.update(outcome.evidence)
        # Measurements are sealed here, on every terminal state including Aborted, and
        # written into the slot after the author's run_data override ran, so an
        # override that drops or rebuilds the dict cannot lose the readings. The same
        # list goes on the tree node because result.json is the root's record and a
        # leaf's run_data never reaches it (docs/measurements.md).
        measurements = tuple(
            measurement.to_json() for measurement in node_runtime(utils).seal_measurements()
        )
        run_data[MEASUREMENTS_KEY] = list(measurements)
        summary_tree = SummaryTreeNode(
            name=self.name,
            type=node_type,
            state=outcome.state,
            failure=outcome.failure,
            timeout=outcome.timeout,
            log_path=node_runtime(utils).owned_log_reference(),
            children=children,
            evidence=outcome.evidence,
            evidence_declared=self._evidence_declared,
            measurements=measurements,
        )
        object.__setattr__(self, "_summary_tree", summary_tree)
        return RunResult(
            slot=outcome.slot,
            node=self.name,
            state=outcome.state,
            failure=outcome.failure,
            timeout=outcome.timeout,
            value=outcome.value,
            swallowed=outcome.swallowed,
            ancestor_names=list(utils.logger.scope),
            bread_crumbs=utils.logger.bread_crumbs,
            summary_tree=summary_tree,
            type=node_type,
            env=utils.env,
            run_data=run_data,
        )

    @final
    async def _finish_result(self, result: RunResult, utils: TestUtils) -> RunResult:
        """Publish the root result, then close this node's durable timeline."""
        runtime = node_runtime(utils)
        try:
            if len(utils.logger.scope) == 1 and runtime.owned_log_path() is not None:
                persistence = asyncio.create_task(
                    _bounded_finalization(
                        runtime.persist_result(result),
                        node=self.name,
                        operation="root result persistence",
                    )
                )
                try:
                    await asyncio.shield(persistence)
                except asyncio.CancelledError:
                    # A terminal result is accepted evidence. Repeated shutdown
                    # requests may not separate its file from the final pointer.
                    while not persistence.done():
                        with contextlib.suppress(asyncio.CancelledError):
                            await asyncio.shield(persistence)
                    persistence.result()
                    raise
        finally:
            runtime.close_timeline()
        return result

    @final
    async def _configure_and_merge_context(self, utils: TestUtils) -> Mapping[str, Any]:
        """Keep configure and its required ctx merge inside one child boundary.

        The context closes and the route-declaration window shuts when configure
        settles, whether it returned or raised: cleanup after a failed configure reads
        the context and the route and may not write either.
        """
        runtime = node_runtime(utils)
        runtime.enter_configure()
        # configure() is the other slot that may measure (a bring-up reading belongs
        # here); the binding closes with the slot so cleanup after a failed configure
        # cannot record either.
        measuring = current_utils.set(utils)
        try:
            # The framework calls every node's configure() through the same erased
            # TestUtils; CtxT only exists for the suite author's own annotations, so
            # the framework narrows here rather than asking user code to cast (#75).
            configured = await self.configure(cast("TestUtils[CtxT]", utils))
            runtime.merge_context(configured)
        finally:
            current_utils.reset(measuring)
            runtime.exit_configure()
            runtime.freeze_context()
        return configured

    @final
    async def _capture_hook_artifact(
        self,
        utils: TestUtils,
        artifact: Artifact,
        *,
        description: str,
    ) -> None:
        """Finish an accepted observation's publication before cancellation escapes."""
        capture = asyncio.create_task(
            _bounded_finalization(
                utils.capture_artifact(artifact),
                node=self.name,
                operation=f"{description} artifact capture",
            )
        )
        try:
            await asyncio.shield(capture)
        except asyncio.CancelledError:
            # Once a hook has produced an observation, its artifact belongs to the
            # authoritative record. Repeated shutdown requests may not orphan it.
            while not capture.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(capture)
            raise

    @final
    async def _capture_hook_result(
        self,
        hook: SurveyHook | AuditHook,
        utils: TestUtils,
        *,
        artifact_name: str,
        description: str,
    ) -> object:
        """Run one hook and publish every non-``None`` observation it produced."""
        value = await hook.fn(utils)
        if value is None:
            utils.logger.info("%s returned no value; skipping artifact capture", description)
            return value
        await self._capture_hook_artifact(
            utils,
            Artifact(name=artifact_name, type="survey", data=value),
            description=description,
        )
        return value

    @final
    async def _run_surveys(
        self,
        boundary: ChildBoundary,
        utils: TestUtils,
        phase: Phase,
    ) -> None:
        """Dispatch every survey scheduled at one phase boundary without masking the verdict."""
        for index, hook in enumerate(self._surveys, start=1):
            description = f"survey {phase.value} hook #{index}"
            if phase not in hook.phases:
                utils.logger.debug("%s not scheduled at this phase; skipping", description)
                continue
            utils.logger.debug("dispatching %s", description)
            await boundary.run(
                Slot.SURVEY,
                self._capture_hook_result(
                    hook,
                    utils,
                    artifact_name=f"{phase.value}-{index}",
                    description=description,
                ),
            )
            utils.logger.debug("%s complete", description)

    @final
    async def _run_audit_loop(
        self,
        hook: AuditHook,
        hook_index: int,
        utils: TestUtils,
        run_abort: RunAbort,
        audit_signal: AuditSignal,
    ) -> Outcome:
        """Run one audit on its own interval until it produces a failure verdict."""
        boundary = ChildBoundary(self.name, run_abort, utils.logger, audit_signal)
        count = 0
        while True:
            await asyncio.sleep(hook.interval)
            count += 1
            description = f"audit hook #{hook_index} dispatch #{count}"
            utils.logger.debug("dispatching %s", description)
            outcome = await boundary.run(
                Slot.AUDIT,
                self._capture_hook_result(
                    hook,
                    utils,
                    artifact_name=f"audit-{hook_index}-{count}",
                    description=description,
                ),
            )
            if outcome.state is State.FAIL:
                return outcome
            utils.logger.debug("%s complete", description)

    @final
    async def _run_suite_with_audits(
        self,
        suite: LitmusTest[Any],
        utils: TestUtils,
        run_abort: RunAbort,
        audit_signal: AuditSignal,
        *,
        time_budget_clocks: tuple[TimeBudgetClock, ...] = (),
    ) -> tuple[RunResult | None, Outcome | None]:
        """Race the authored suite against independent framework-owned audit loops."""
        suite_task = asyncio.create_task(
            suite._run_lifecycle(
                utils,
                run_abort,
                audit_signal=audit_signal,
                time_budget_clocks=time_budget_clocks,
            )
        )
        audit_tasks = [
            asyncio.create_task(self._run_audit_loop(hook, index, utils, run_abort, audit_signal))
            for index, hook in enumerate(self._audits, start=1)
        ]
        try:
            done, _ = await asyncio.wait(
                [suite_task, *audit_tasks],
                return_when=asyncio.FIRST_COMPLETED,
            )
            completed_audits = [task for task in audit_tasks if task in done]
            if completed_audits:
                # An audit verdict cannot be discarded merely because the suite
                # settled in the same event-loop turn. Evidence outranks timing luck.
                outcome = await completed_audits[0]
                failure = outcome.failure
                if not isinstance(failure, TestFailure):
                    raise FrameworkContractViolation(
                        self.name,
                        detail="an audit stopped without a TestFailure verdict",
                    )
                audit_signal.fire(failure)
                suite_task.cancel()
                await asyncio.gather(suite_task, return_exceptions=True)
                # A paired completion hook can fail while the audit's subtree winds
                # down. That run-wide halt must escape the earlier local verdict.
                if isinstance(run_abort.reason, ExternalIntegrationFailure):
                    raise run_abort.reason
                return None, outcome

            result = await suite_task
            audit_signal.stop()
            return result, None
        finally:
            if audit_signal.reason is None:
                audit_signal.stop()
            if not suite_task.done():
                suite_task.cancel()
            for task in audit_tasks:
                if not task.done():
                    task.cancel()

            async def settle_owned_tasks() -> None:
                await asyncio.gather(suite_task, *audit_tasks, return_exceptions=True)

            settled = asyncio.create_task(settle_owned_tasks())
            try:
                await asyncio.shield(settled)
            except asyncio.CancelledError:
                # Cancellation may arrive again while an accepted observation is
                # publishing. Do not release this node until every owned task settles.
                while not settled.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.shield(settled)
                raise

    @final
    async def _run_cleanup(self, boundary: ChildBoundary, utils: TestUtils) -> None:
        """Finish cleanup before allowing cancellation to leave this node.

        Shells this node opened stay connected here: the post-cleanup survey still
        execs through them. The caller disconnects them after that survey and
        before the via lease is released.
        """
        cleanup = asyncio.create_task(
            boundary.run(
                Slot.CLEANUP,
                _bounded_finalization(
                    # Same erased-TestUtils narrowing as _configure_and_merge_context (#75).
                    self.cleanup(cast("TestUtils[CtxT]", utils)),
                    node=self.name,
                    operation="cleanup",
                ),
            )
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # Section 4.2: repeated cancellations must not abandon the wait —
            # restoration finishes before cancellation may leave this node.
            while not cleanup.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(cleanup)
            raise

    @final
    async def _run_configure_only(self, seed: TestUtils, run_abort: RunAbort) -> Outcome:
        """Configure this node and its via, without building or running a suite.

        Idempotent: concurrent and later callers await the same bring-up rather than
        reading a half-built hop. Nested via devices come up first so this node's
        configure already execs through them.
        """
        task = self._configure_only_task
        if task is None:

            async def bring_up() -> Outcome:
                runtime = node_runtime(seed).for_node(
                    self.name,
                    run_abort,
                    discrete_log_file=self.discrete_log_file,
                    isolate_context=True,
                    inherit_route=False,
                )
                utils = runtime.utils
                object.__setattr__(self, "_configure_only_utils", utils)
                object.__setattr__(
                    self,
                    "_log_path",
                    runtime.owned_log_path(),
                )
                via_failed = await self._bring_up_via(utils, run_abort)
                if via_failed is not None:
                    return via_failed
                boundary = ChildBoundary(self.name, run_abort, utils.logger)

                async def configure_window(
                    active_clocks: tuple[TimeBudgetClock, ...],
                    lineage: object,
                ) -> Outcome:
                    nonlocal boundary
                    boundary = ChildBoundary(
                        self.name,
                        run_abort,
                        utils.logger,
                        time_budget_clocks=active_clocks,
                    )
                    async with _active_time_budgets(active_clocks, lineage):
                        return await boundary.run(
                            Slot.CONFIGURE,
                            self._configure_and_merge_context(utils),
                        )

                value, timeout = await self._run_timed_window(
                    utils,
                    run_abort,
                    (),
                    (),
                    configure_window,
                )
                if timeout is not None:
                    outcome = self._deadline_outcome(timeout, slot=Slot.CONFIGURE)
                elif isinstance(value, Outcome):
                    outcome = value
                else:
                    raise FrameworkContractViolation(
                        self.name,
                        detail="a configure-only time window settled without an Outcome",
                    )
                if outcome.state is None:
                    runtime.log_exec_routing()
                return outcome

            task = asyncio.create_task(bring_up())
            object.__setattr__(self, "_configure_only_task", task)
        # Shielded: cancelling one waiter must not cancel a shared via handshake.
        return await asyncio.shield(task)

    @final
    async def _run_cleanup_only(self) -> None:
        """Idempotent teardown of a configure-only node and its via chain."""
        task = self._configure_only_task
        if task is not None:
            if not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    while not task.done():
                        with contextlib.suppress(asyncio.CancelledError):
                            await asyncio.shield(task)
                    raise
                except Exception:
                    # Bring-up already failed; teardown still has to run.
                    pass
            object.__setattr__(self, "_configure_only_task", None)
        utils = self._configure_only_utils
        if utils is None:
            return
        object.__setattr__(self, "_configure_only_utils", None)
        runtime = node_runtime(utils)
        boundary = ChildBoundary(self.name, runtime.halt_signal(), utils.logger)
        try:
            await self._run_cleanup(boundary, utils)
        finally:
            try:
                await runtime.disconnect_opened()
            finally:
                try:
                    await self._tear_down_via()
                finally:
                    runtime.close_timeline()

    @final
    async def _bring_up_via(self, utils: TestUtils, run_abort: RunAbort) -> Outcome | None:
        """Configure the via device, then borrow its exec hop for this node.

        Returns a terminal outcome when the via failed to reach a testable state.
        A second consumer waits for bring-up to finish; teardown fires on last release.
        """
        via = self._via
        if via is None:
            return None
        object.__setattr__(via, "_via_lease_count", via._via_lease_count + 1)
        object.__setattr__(self, "_via_leased", True)
        outcome = await via._run_configure_only(utils, run_abort)
        if outcome.state is not None:
            return outcome
        via_utils = via._configure_only_utils
        if via_utils is None:
            raise FrameworkContractViolation(
                self.name,
                detail="a via hop was declared but the via device is not up",
            )
        router = node_runtime(via_utils).routing.router
        node_runtime(utils).inherit_router(router)
        return None

    @final
    async def _tear_down_via(self) -> None:
        """Release this consumer's via lease; the last holder tears the device down."""
        via = self._via
        if via is None or not self._via_leased:
            return
        object.__setattr__(self, "_via_leased", False)
        remaining = via._via_lease_count - 1
        if remaining < 0:
            raise FrameworkContractViolation(
                self.name,
                detail="a via lease was released more times than it was acquired",
            )
        object.__setattr__(via, "_via_lease_count", remaining)
        if remaining == 0:
            await via._run_cleanup_only()

    @final
    def _adopt_via_outcome(self, outcome: Outcome) -> Outcome:
        """Attribute a via bring-up failure to this consumer's configure slot."""
        return Outcome(
            Slot.CONFIGURE,
            self.name,
            outcome.state,
            failure=outcome.failure,
            timeout=outcome.timeout,
            value=outcome.value,
            swallowed=outcome.swallowed,
        )


@final
class _Test(LitmusTest[Any]):
    """Internal leaf specialization created only by :meth:`LitmusTest.test`."""

    __slots__ = ()

    def __init__(
        self,
        *,
        name: str,
        fn: _TestFunction,
        fail_after: float | None,
        abort_after: float | None,
        evidence_declared: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            discrete_log_file=False,
            fail_after=fail_after,
            abort_after=abort_after,
        )
        self._fn = fn
        self._evidence_declared = evidence_declared

    def build_suite(self) -> LitmusTest[Any]:
        """Satisfy the structural contract; leaf execution never composes a suite."""
        raise FrameworkContractViolation(
            self.name,
            detail="an internal test has no child suite",
        )


@final
class _Parallel(LitmusTest[Any]):
    """Internal concurrent specialization created only by :meth:`LitmusTest.parallel`."""

    __slots__ = ()

    def __init__(
        self,
        *,
        name: str,
        children: Sequence[LitmusTest[Any]],
        discrete_log_file: bool,
        fail_after: float | None,
        abort_after: float | None,
    ) -> None:
        super().__init__(
            name=name,
            discrete_log_file=discrete_log_file,
            fail_after=fail_after,
            abort_after=abort_after,
        )
        self._children = tuple(children)

    def build_suite(self) -> LitmusTest[Any]:
        """Satisfy the structural contract; parallel execution owns its child tasks."""
        raise FrameworkContractViolation(
            self.name,
            detail="an internal parallel node has no child suite",
        )


@final
class _Sequence(LitmusTest[Any]):
    """Internal serial specialization created only by :meth:`LitmusTest.sequence`."""

    __slots__ = ()

    def __init__(
        self,
        *,
        name: str,
        children: Sequence[LitmusTest[Any]],
        discrete_log_file: bool,
        fail_after: float | None,
        abort_after: float | None,
    ) -> None:
        super().__init__(
            name=name,
            discrete_log_file=discrete_log_file,
            fail_after=fail_after,
            abort_after=abort_after,
        )
        self._children = tuple(children)

    def build_suite(self) -> LitmusTest[Any]:
        """Satisfy the structural contract; sequence execution owns child order."""
        raise FrameworkContractViolation(
            self.name,
            detail="an internal sequence has no child suite",
        )
