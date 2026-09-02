"""The single public test node and its framework-owned lifecycle."""

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, final

from litmus.artifacts import Artifact
from litmus.errors import (
    ContractViolation,
    ExternalIntegrationFailure,
    FrameworkContractViolation,
    TestFailure,
)
from litmus.evidence import JsonObject, snapshot_run_data
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
from litmus.lifecycle import AuditSignal, ChildBoundary, Outcome, RunAbort, Slot, State
from litmus.results import NodeType, RunResult, SummaryTreeNode
from litmus.utils import TestUtils
from litmus.verdict import Verdict

type _TestFunction = Callable[[TestUtils], Awaitable[Verdict]]

_SEALED_METHODS = frozenset(
    {
        "__delattr__",
        "__init_subclass__",
        "__setattr__",
        "case",
        "group",
        "main",
        "run",
        "run_configure_only",
        "run_cleanup_only",
        "sequence",
        "_run_lifecycle",
        "_run_group",
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
            "finalization timeout"
        )
        raise ContractViolation(node, detail=detail, cause=error) from error
    finally:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume_finalization_exception)


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
        or (isinstance(annotation, str) and annotation.rsplit(".", maxsplit=1)[-1] == "Verdict")
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


class LitmusTest(metaclass=_LitmusTestMeta):
    """A claim about hardware and the procedure that produces its evidence.

    Instances accept configuration during construction and are immutable afterwards.
    Calling :meth:`main` consumes the instance: a second call raises
    :class:`ContractViolation`, preserving the first run as evidence.
    """

    __slots__ = (
        "_audits",
        "_children",
        "_configure_only_task",
        "_configure_only_utils",
        "_ctx",
        "_frozen",
        "_has_run",
        "_name",
        "_surveys",
        "_test",
        "_via",
        "_via_lease_count",
        "_via_leased",
    )

    def __init__(self, *, name: str, via: LitmusTest | None = None) -> None:
        """Create a named, not-yet-run test node.

        Args:
            name: Stable node name used by logs, artifacts, and failures.
            via: Optional gateway Testable. The framework brings it up before this
                node's configure, routes exec through its SSH hop, and cleans it up
                after this node's cleanup. Authors never call configure/cleanup on it.
        """
        self._frozen = False
        self._has_run = False
        self._name = name
        self._via = via
        self._via_leased = False
        self._via_lease_count = 0
        self._configure_only_task: asyncio.Task[Outcome] | None = None
        self._configure_only_utils: TestUtils | None = None
        self._surveys: list[SurveyHook] = []
        self._audits: list[AuditHook] = []
        self._test: _TestFunction | None = None
        self._children: tuple[LitmusTest, ...] | None = None
        self._ctx: dict[str, Any] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject malformed authored classes before an instance can touch hardware."""
        super().__init_subclass__(**kwargs)

        for name in cls.__dict__:
            if name in _SEALED_METHODS or name.startswith("_safe_"):
                msg = f"LitmusTest.{name} is framework-owned and cannot be overridden"
                raise TypeError(msg)

        for name in _LIFECYCLE_COROUTINES:
            member = cls.__dict__.get(name)
            if member is not None and not inspect.iscoroutinefunction(_function(member)):
                msg = f"{cls.__name__}.{name} must be defined with async def"
                raise TypeError(msg)

        for name, member in cls.__dict__.items():
            function = _function(member)
            if (
                inspect.isfunction(function)
                and _is_declared_test_method(cls, name, function)
                and not inspect.iscoroutinefunction(function)
            ):
                msg = f"{cls.__name__}.{name} must be defined with async def"
                raise TypeError(msg)

        build_suite = inspect.getattr_static(cls, "build_suite")
        if build_suite is LitmusTest.build_suite:
            msg = f"{cls.__name__}.build_suite must be overridden"
            raise TypeError(msg)
        build_suite_function = _function(build_suite)
        if not inspect.isfunction(build_suite_function):
            msg = f"{cls.__name__}.build_suite must be defined with def"
            raise TypeError(msg)
        if inspect.iscoroutinefunction(build_suite_function):
            msg = f"{cls.__name__}.build_suite must be defined with def, not async def"
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
    def run_data(self) -> JsonObject:
        """A detached JSON-safe snapshot of this node's serializable context."""
        return snapshot_run_data(self._ctx)

    def case(self, name: str, test: _TestFunction) -> LitmusTest:
        """Create an internal leaf node from an already-bound verdict method."""
        if not inspect.iscoroutinefunction(test):
            method = getattr(test, "__qualname__", repr(test))
            msg = f"test method {method} must be defined with async def"
            raise TypeError(msg)
        return _Case(name=name, test=test)

    def group(self, name: str, tests: Sequence[LitmusTest]) -> LitmusTest:
        """Compose independent children under one structured-concurrency owner."""
        if not tests:
            raise ContractViolation(name, "a test group must contain at least one child")
        return _Group(name=name, tests=tests)

    def sequence(self, name: str, tests: Sequence[LitmusTest]) -> LitmusTest:
        """Compose dependent children that stop at the first non-pass verdict."""
        if not tests:
            raise ContractViolation(name, "a test sequence must contain at least one child")
        return _Sequence(name=name, tests=tests)

    @final
    def use_survey(self, fn: HookFunction) -> None:
        """Register a survey as immutable test identity during construction."""
        if self._frozen:
            raise ContractViolation(self.name, "hooks may be registered only during construction")
        self._surveys.append(SurveyHook(fn=fn))

    @final
    def use_audit(self, *, interval: float, fn: HookFunction) -> None:
        """Register an interval audit as immutable test identity during construction."""
        if self._frozen:
            raise ContractViolation(self.name, "hooks may be registered only during construction")
        self._audits.append(AuditHook(interval=interval, fn=fn))

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Prepare the test and return context for its suite and cleanup."""
        return {}

    def build_suite(self) -> LitmusTest:
        """Return this node's authored composition.

        Subclasses provide a pure table of contents assembled with ``self.case()``,
        ``self.sequence()``, and ``self.group()``.
        """
        detail = "build_suite() must return a case, sequence, or group composition"
        raise ContractViolation(self.name, detail)

    async def cleanup(self, utils: TestUtils) -> None:
        """Restore state after the suite settles, regardless of its outcome."""

    @final
    async def run(
        self,
        *,
        env: Mapping[str, str] | None = None,
        before_each: BeforeEachHook | None = None,
        after_each: AfterEachHook | None = None,
        allow_unpaired_hooks: bool = False,
    ) -> RunResult:
        """Execute once, fanning root-owned integration hooks across every node."""
        hooks = IntegrationHooks(
            before_each=before_each,
            after_each=after_each,
            allow_unpaired=allow_unpaired_hooks,
        )
        return await self.main(TestUtils(env=env, _integration_hooks=hooks))

    @final
    async def main(self, utils: TestUtils | None = None) -> RunResult:
        """Execute the sealed lifecycle with an optional framework test seed."""
        seed = TestUtils() if utils is None else utils
        run_abort = RunAbort()
        result = await self._run_lifecycle(seed, run_abort, isolate_context=False)
        if run_abort.fired:
            # Sweep once more at the root so a transport that settled open while its
            # task was being cancelled cannot outlive the returned Aborted tree.
            await seed._cancel_in_flight()  # pyright: ignore[reportPrivateUsage]
        return result

    @final
    async def run_configure_only(self, utils: TestUtils | None = None) -> dict[str, Any]:
        """Bring this node and its via up without running the suite.

        Idempotent. Framework-owned: Inspector isolation and via-device bring-up.
        Suite authors pass ``via=`` at construction instead of calling this.
        """
        seed = TestUtils() if utils is None else utils
        outcome = await self._run_configure_only(seed, RunAbort())
        if outcome.state is None:
            configured = self._configure_only_utils
            if configured is None:
                return {}
            return dict(configured.ctx)
        if outcome.failure is not None:
            raise outcome.failure
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
    ) -> RunResult:
        """Execute one node while preserving the tree's shared halt signal."""
        if self._has_run:
            raise ContractViolation(self.name, "a LitmusTest instance may run only once")
        if self._configure_only_utils is not None:
            raise ContractViolation(self.name, "this instance is already up as a via target")
        object.__setattr__(self, "_has_run", True)

        children = self._children
        test = self._test
        authored_instance = children is None and test is None
        node_audit_signal = AuditSignal(audit_signal) if self._audits else audit_signal
        utils = utils._for_node(  # pyright: ignore[reportPrivateUsage]
            self.name,
            run_abort,
            isolate_context=isolate_context and authored_instance,
            audit_signal=node_audit_signal,
        )
        object.__setattr__(self, "_ctx", utils.ctx)

        integration_hooks = utils._integration_hooks  # pyright: ignore[reportPrivateUsage]
        boundary = ChildBoundary(self.name, run_abort, utils.logger, node_audit_signal)
        after_each_started = False
        summary_children: tuple[SummaryTreeNode, ...] = ()
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
                utils._log_exec_routing()  # pyright: ignore[reportPrivateUsage]

            if children is not None:
                # Sealing forbids subclass dispatch, so the engine names its two internal
                # composite shapes here; a third shape earns a real strategy seam.
                if isinstance(self, _Sequence):
                    result = await self._run_sequence(
                        utils,
                        run_abort,
                        children,
                        audit_signal=node_audit_signal,
                    )
                else:
                    result = await self._run_group(
                        utils,
                        run_abort,
                        children,
                        audit_signal=node_audit_signal,
                    )
                summary_children = result.summary_tree.children
                after_each_started = True
                return await self._run_after_each(boundary, utils, run_abort, result)

            if test is not None:
                # ChildBoundary is the sole enforcement point for authored verdicts.
                outcome = await boundary.run(Slot.TEST_METHOD, test(utils))
                result = self._to_result(outcome, utils)
                after_each_started = True
                return await self._run_after_each(boundary, utils, run_abort, result)

            try:
                # Hop first, teardown last: surveys and audits exec through the via,
                # not the test host. Provenance (item 6) and via ownership (item 12).
                via_failed = await self._bring_up_via(utils, run_abort)
                if via_failed is not None:
                    outcome = self._adopt_via_outcome(via_failed)
                else:
                    await self._run_surveys(boundary, utils, "pre-configure")
                    configured = await boundary.run(
                        Slot.CONFIGURE, self._configure_and_merge_context(utils)
                    )
                    if configured.state is not None:
                        outcome = configured
                    else:
                        utils._log_exec_routing()  # pyright: ignore[reportPrivateUsage]
                        await self._run_surveys(boundary, utils, "post-configure")
                        suite = self.build_suite()
                        await self._run_surveys(boundary, utils, "pre-test")
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
                            )
                        else:
                            suite_result = await suite._run_lifecycle(
                                utils,
                                run_abort,
                                audit_signal=node_audit_signal,
                            )
                            audit_outcome = None
                        await self._run_surveys(boundary, utils, "post-test")

                        if audit_outcome is not None:
                            outcome = audit_outcome
                            summary_children = (suite._pending_summary_tree(),)
                        elif suite_result is None:
                            raise FrameworkContractViolation(
                                self.name,
                                detail="suite settled without a result or audit verdict",
                            )
                        else:
                            outcome = Outcome(
                                Slot.TEST_METHOD,
                                self.name,
                                suite_result.state,
                                failure=suite_result.failure,
                                value=suite_result.value,
                            )
                            summary_children = (suite_result.summary_tree,)
            finally:
                try:
                    await self._run_surveys(boundary, utils, "pre-cleanup")
                finally:
                    try:
                        await self._run_cleanup(boundary, utils)
                    finally:
                        try:
                            await self._run_surveys(boundary, utils, "post-cleanup")
                        finally:
                            await self._tear_down_via()
            result = self._to_result(outcome, utils, children=summary_children)
            after_each_started = True
            return await self._run_after_each(boundary, utils, run_abort, result)
        except ExternalIntegrationFailure:
            await utils._cancel_in_flight()  # pyright: ignore[reportPrivateUsage]
            result = self._aborted_result(
                boundary,
                utils,
                run_abort,
                children=summary_children or self._pending_children(),
            )
            if after_each_started:
                return result
            return await self._run_after_each(boundary, utils, run_abort, result)
        except asyncio.CancelledError:
            if not run_abort.fired:
                raise
            result = self._aborted_result(
                boundary,
                utils,
                run_abort,
                children=summary_children or self._pending_children(),
            )
            if after_each_started:
                return result
            return await self._run_after_each(boundary, utils, run_abort, result)

    @final
    async def _run_after_each(
        self,
        boundary: ChildBoundary,
        utils: TestUtils,
        run_abort: RunAbort,
        result: RunResult,
    ) -> RunResult:
        """Dispatch the root-owned completion hook for one settled node."""
        integration_hooks = utils._integration_hooks  # pyright: ignore[reportPrivateUsage]
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
                await utils._cancel_in_flight()  # pyright: ignore[reportPrivateUsage]
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
    def _aborted_result(
        self,
        boundary: ChildBoundary,
        utils: TestUtils,
        run_abort: RunAbort,
        *,
        children: tuple[SummaryTreeNode, ...],
    ) -> RunResult:
        """Seal the shared integration failure at this node without rewrapping it."""
        reason = run_abort.reason
        if reason is None:
            raise FrameworkContractViolation(
                self.name,
                detail="an integration abort reached a node without its reason",
            )
        boundary_outcome = boundary.outcome
        slot = Slot.TEST_METHOD if boundary_outcome is None else boundary_outcome.slot
        return self._to_result(
            Outcome(slot, self.name, State.ABORTED, failure=reason),
            utils,
            children=children,
        )

    @final
    async def _run_group(
        self,
        utils: TestUtils,
        run_abort: RunAbort,
        children: tuple[LitmusTest, ...],
        *,
        audit_signal: AuditSignal | None = None,
    ) -> RunResult:
        """Run every child to settlement unless a harness-level halt escapes."""
        tasks: list[asyncio.Task[RunResult]] = []
        try:
            async with asyncio.TaskGroup() as task_group:
                tasks.extend(
                    task_group.create_task(
                        child._run_lifecycle(utils, run_abort, audit_signal=audit_signal)
                    )
                    for child in children
                )
                active = set(tasks)
                while active:
                    done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                    active.difference_update(done)
                    if run_abort.fired:
                        for task in active:
                            task.cancel()
                        break
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

        # Aborted outranks Fail, and the first child in that state tells the group's story.
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
        children: tuple[LitmusTest, ...],
        *,
        audit_signal: AuditSignal | None = None,
    ) -> RunResult:
        """Run dependent children in order and stop at the first non-pass verdict."""
        outcomes: list[RunResult] = []
        for index, child in enumerate(children):
            outcome = await child._run_lifecycle(utils, run_abort, audit_signal=audit_signal)
            outcomes.append(outcome)
            if outcome.state in (State.FAIL, State.ABORTED):
                aggregate = Outcome(
                    Slot.TEST_METHOD,
                    self.name,
                    outcome.state,
                    failure=outcome.failure,
                    value=tuple(outcomes),
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
        if isinstance(self, _Case):
            return NodeType.TEST_CASE
        if isinstance(self, _Group):
            return NodeType.TEST_GROUP
        if isinstance(self, _Sequence):
            return NodeType.TEST_SEQUENCE
        return NodeType.TEST_SUITE

    @final
    def _pending_summary_tree(self) -> SummaryTreeNode:
        """Snapshot a declared node that a failed sequence did not execute."""
        children = self._children
        if children is not None:
            child_trees = tuple(child._pending_summary_tree() for child in children)
        elif self._test is not None:
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
        summary_tree = SummaryTreeNode(
            name=self.name,
            type=node_type,
            state=outcome.state,
            failure=outcome.failure,
            children=children,
        )
        return RunResult(
            slot=outcome.slot,
            node=self.name,
            state=outcome.state,
            failure=outcome.failure,
            value=outcome.value,
            swallowed=outcome.swallowed,
            ancestor_names=list(utils.logger.scope),
            bread_crumbs=utils.logger.bread_crumbs,
            summary_tree=summary_tree,
            type=node_type,
            env=utils.env,
            run_data=self.run_data,
        )

    @final
    async def _configure_and_merge_context(self, utils: TestUtils) -> dict[str, Any]:
        """Keep configure and its required ctx merge inside one child boundary."""
        configured = await self.configure(utils)
        utils.ctx.update(configured)
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
        phase: str,
    ) -> None:
        """Dispatch every survey at one phase boundary without masking the verdict."""
        for index, hook in enumerate(self._surveys, start=1):
            description = f"survey {phase} hook #{index}"
            utils.logger.debug("dispatching %s", description)
            await boundary.run(
                Slot.SURVEY,
                self._capture_hook_result(
                    hook,
                    utils,
                    artifact_name=f"{phase}-{index}",
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
        suite: LitmusTest,
        utils: TestUtils,
        run_abort: RunAbort,
        audit_signal: AuditSignal,
    ) -> tuple[RunResult | None, Outcome | None]:
        """Race the authored suite against independent framework-owned audit loops."""
        suite_task = asyncio.create_task(
            suite._run_lifecycle(utils, run_abort, audit_signal=audit_signal)
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
        """Finish cleanup before allowing cancellation to leave this node."""
        cleanup = asyncio.create_task(
            boundary.run(
                Slot.CLEANUP,
                _bounded_finalization(self.cleanup(utils), node=self.name, operation="cleanup"),
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
        finally:
            await utils._disconnect_opened()  # pyright: ignore[reportPrivateUsage]

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
                utils = seed._for_node(  # pyright: ignore[reportPrivateUsage]
                    self.name,
                    run_abort,
                    isolate_context=True,
                    inherit_route=False,
                )
                object.__setattr__(self, "_configure_only_utils", utils)
                via_failed = await self._bring_up_via(utils, run_abort)
                if via_failed is not None:
                    return via_failed
                boundary = ChildBoundary(self.name, run_abort, utils.logger)
                outcome = await boundary.run(
                    Slot.CONFIGURE, self._configure_and_merge_context(utils)
                )
                if outcome.state is None:
                    utils._log_exec_routing()  # pyright: ignore[reportPrivateUsage]
                return outcome

            task = asyncio.create_task(bring_up())
            object.__setattr__(self, "_configure_only_task", task)
        return await task

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
        boundary = ChildBoundary(self.name, utils._halt_signal(), utils.logger)  # pyright: ignore[reportPrivateUsage]
        try:
            await self._run_cleanup(boundary, utils)
        finally:
            await self._tear_down_via()

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
        router = via_utils._routing.router  # pyright: ignore[reportPrivateUsage]
        if router is not None:
            utils._inherit_router(router)  # pyright: ignore[reportPrivateUsage]
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
            value=outcome.value,
            swallowed=outcome.swallowed,
        )


@final
class _Case(LitmusTest):
    """Internal leaf specialization created only by :meth:`LitmusTest.case`."""

    __slots__ = ()

    def __init__(self, *, name: str, test: _TestFunction) -> None:
        super().__init__(name=name)
        self._test = test

    def build_suite(self) -> LitmusTest:
        """Satisfy the structural contract; leaf execution never composes a suite."""
        raise FrameworkContractViolation(
            self.name,
            detail="an internal case has no child suite",
        )


@final
class _Group(LitmusTest):
    """Internal concurrent specialization created only by :meth:`LitmusTest.group`."""

    __slots__ = ()

    def __init__(self, *, name: str, tests: Sequence[LitmusTest]) -> None:
        super().__init__(name=name)
        self._children = tuple(tests)

    def build_suite(self) -> LitmusTest:
        """Satisfy the structural contract; group execution owns its child tasks."""
        raise FrameworkContractViolation(
            self.name,
            detail="an internal group has no child suite",
        )


@final
class _Sequence(LitmusTest):
    """Internal serial specialization created only by :meth:`LitmusTest.sequence`."""

    __slots__ = ()

    def __init__(self, *, name: str, tests: Sequence[LitmusTest]) -> None:
        super().__init__(name=name)
        self._children = tuple(tests)

    def build_suite(self) -> LitmusTest:
        """Satisfy the structural contract; sequence execution owns child order."""
        raise FrameworkContractViolation(
            self.name,
            detail="an internal sequence has no child suite",
        )
