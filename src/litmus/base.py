"""The single public test node and its framework-owned lifecycle."""

import asyncio
import contextlib
import copy
import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, final

from litmus.errors import ContractViolation, ExternalIntegrationFailure
from litmus.hooks import AuditHook, HookFunction, SurveyHook
from litmus.lifecycle import ChildBoundary, Outcome, RunAbort, Slot, State
from litmus.verdict import Verdict

type _TestFunction = Callable[[Any], Awaitable[Verdict]]

_SEALED_METHODS = frozenset(
    {
        "__delattr__",
        "__init_subclass__",
        "__setattr__",
        "case",
        "group",
        "main",
        "run",
        "sequence",
        "_run_lifecycle",
        "_run_group",
        "_run_sequence",
        "_configure_and_merge_context",
        "_run_cleanup",
        "use_survey",
        "use_audit",
    }
)
_LIFECYCLE_COROUTINES = frozenset({"configure", "cleanup"})


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

    __slots__ = ("_audits", "_children", "_frozen", "_has_run", "_name", "_surveys", "_test")

    def __init__(self, *, name: str) -> None:
        """Create a named, not-yet-run test node."""
        self._frozen = False
        self._has_run = False
        self._name = name
        self._surveys: list[SurveyHook] = []
        self._audits: list[AuditHook] = []
        self._test: _TestFunction | None = None
        self._children: tuple[LitmusTest, ...] | None = None

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

    async def configure(self, utils: Any) -> dict[str, Any]:
        """Prepare the test and return context for its suite and cleanup."""
        return {}

    def build_suite(self) -> LitmusTest:
        """Return this node's authored composition.

        Subclasses provide a pure table of contents assembled with ``self.case()``,
        ``self.sequence()``, and ``self.group()``.
        """
        detail = "build_suite() must return a case, sequence, or group composition"
        raise ContractViolation(self.name, detail)

    async def cleanup(self, utils: Any) -> None:
        """Restore state after the suite settles, regardless of its outcome."""

    @final
    async def main(self, utils: Any) -> Outcome:
        """Execute the sealed lifecycle exactly once."""
        return await self._run_lifecycle(utils, RunAbort(), isolate_context=False)

    @final
    async def _run_lifecycle(
        self,
        utils: Any,
        run_abort: RunAbort,
        *,
        isolate_context: bool = True,
    ) -> Outcome:
        """Execute one node while preserving the tree's shared halt signal."""
        if self._has_run:
            raise ContractViolation(self.name, "a LitmusTest instance may run only once")
        object.__setattr__(self, "_has_run", True)

        children = self._children
        test = self._test
        if isolate_context and children is None and test is None:
            # Utilities remain shared services; mutable context belongs to one authored instance.
            utils = copy.copy(utils)
            object.__setattr__(utils, "ctx", {})

        if children is not None:
            # Sealing forbids subclass dispatch, so the engine names its two internal
            # composite shapes here; a third shape earns a real strategy seam.
            if isinstance(self, _Sequence):
                return await self._run_sequence(utils, run_abort, children)
            return await self._run_group(utils, run_abort, children)

        if test is not None:
            # ChildBoundary is the sole enforcement point for authored verdicts.
            return await ChildBoundary(self.name, run_abort).run(Slot.TEST_METHOD, test(utils))

        boundary = ChildBoundary(self.name, run_abort)
        try:
            configured = await boundary.run(
                Slot.CONFIGURE, self._configure_and_merge_context(utils)
            )
            if configured.state is not None:
                return configured

            suite = self.build_suite()
            return await suite._run_lifecycle(utils, run_abort)
        finally:
            await self._run_cleanup(boundary, utils)

    @final
    async def _run_group(
        self,
        utils: Any,
        run_abort: RunAbort,
        children: tuple[LitmusTest, ...],
    ) -> Outcome:
        """Run every child to settlement unless a harness-level halt escapes."""
        tasks: list[asyncio.Task[Outcome]] = []
        try:
            async with asyncio.TaskGroup() as task_group:
                tasks.extend(
                    task_group.create_task(child._run_lifecycle(utils, run_abort))
                    for child in children
                )
        except* ExternalIntegrationFailure as failures:
            # TaskGroup has already cancelled and joined every sibling. Restore the
            # taxonomy's class-intact halt instead of leaking an ExceptionGroup.
            raise failures.exceptions[0] from None

        outcomes = tuple(task.result() for task in tasks)
        if all(outcome.state is State.PASS for outcome in outcomes):
            return Outcome(Slot.TEST_METHOD, self.name, State.PASS, value=outcomes)

        # Aborted outranks Fail, and the first child in that state tells the group's story.
        if any(outcome.state is State.ABORTED for outcome in outcomes):
            state = State.ABORTED
            reported = next(outcome for outcome in outcomes if outcome.state is State.ABORTED)
        else:
            state = State.FAIL
            reported = next(outcome for outcome in outcomes if outcome.state is not State.PASS)
        return Outcome(
            Slot.TEST_METHOD,
            self.name,
            state,
            failure=reported.failure,
            value=outcomes,
        )

    @final
    async def _run_sequence(
        self,
        utils: Any,
        run_abort: RunAbort,
        children: tuple[LitmusTest, ...],
    ) -> Outcome:
        """Run dependent children in order and stop at the first non-pass verdict."""
        outcomes: list[Outcome] = []
        for child in children:
            outcome = await child._run_lifecycle(utils, run_abort)
            outcomes.append(outcome)
            if outcome.state in (State.FAIL, State.ABORTED):
                return Outcome(
                    Slot.TEST_METHOD,
                    self.name,
                    outcome.state,
                    failure=outcome.failure,
                    value=tuple(outcomes),
                )
            if outcome.state is not State.PASS:
                raise AssertionError("a sequence child settled without a terminal state")

        all_ran = len(outcomes) == len(children)
        all_passed = all(outcome.state is State.PASS for outcome in outcomes)
        if not (all_ran and all_passed):
            raise AssertionError("a sequence reached no explicit terminal verdict")
        return Outcome(Slot.TEST_METHOD, self.name, State.PASS, value=tuple(outcomes))

    @final
    async def _configure_and_merge_context(self, utils: Any) -> dict[str, Any]:
        """Keep configure and its required ctx merge inside one child boundary."""
        configured = await self.configure(utils)
        utils.ctx.update(configured)
        return configured

    @final
    async def _run_cleanup(self, boundary: ChildBoundary, utils: Any) -> None:
        """Finish cleanup before allowing cancellation to leave this node."""
        cleanup = asyncio.create_task(boundary.run(Slot.CLEANUP, self.cleanup(utils)))
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
class _Case(LitmusTest):
    """Internal leaf specialization created only by :meth:`LitmusTest.case`."""

    __slots__ = ()

    def __init__(self, *, name: str, test: _TestFunction) -> None:
        super().__init__(name=name)
        self._test = test

    def build_suite(self) -> LitmusTest:
        """Satisfy the structural contract; leaf execution never composes a suite."""
        raise AssertionError("an internal case has no child suite")


@final
class _Group(LitmusTest):
    """Internal concurrent specialization created only by :meth:`LitmusTest.group`."""

    __slots__ = ()

    def __init__(self, *, name: str, tests: Sequence[LitmusTest]) -> None:
        super().__init__(name=name)
        self._children = tuple(tests)

    def build_suite(self) -> LitmusTest:
        """Satisfy the structural contract; group execution owns its child tasks."""
        raise AssertionError("an internal group has no child suite")


@final
class _Sequence(LitmusTest):
    """Internal serial specialization created only by :meth:`LitmusTest.sequence`."""

    __slots__ = ()

    def __init__(self, *, name: str, tests: Sequence[LitmusTest]) -> None:
        super().__init__(name=name)
        self._children = tuple(tests)

    def build_suite(self) -> LitmusTest:
        """Satisfy the structural contract; sequence execution owns child order."""
        raise AssertionError("an internal sequence has no child suite")
