"""The single public test node and its framework-owned verdict path."""

from collections.abc import Awaitable, Callable
from typing import Any, final

from litmus.errors import ContractViolation
from litmus.verdict import Verdict

type _TestFunction = Callable[[Any], Awaitable[Verdict]]


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

    __slots__ = ("_frozen", "_has_run", "_name", "_test")

    def __init__(self, *, name: str) -> None:
        """Create a named, not-yet-run test node."""
        self._frozen = False
        self._has_run = False
        self._name = name
        self._test: _TestFunction | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject attempts to replace the framework-owned verdict path."""
        super().__init_subclass__(**kwargs)
        if "main" in cls.__dict__:
            msg = "LitmusTest.main is framework-owned and cannot be overridden"
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
        return _Case(name=name, test=test)

    def build_suite(self) -> LitmusTest:
        """Return this node's authored composition.

        Subclasses provide a pure table of contents assembled with ``self.case()``
        and, once their tracked implementation lands, ``sequence`` and ``group``.
        """
        detail = "build_suite() must return a composition built with self.case()"
        raise ContractViolation(self.name, detail)

    @final
    async def main(self, utils: Any) -> Verdict:
        """Execute the sealed verdict path exactly once."""
        if self._has_run:
            raise ContractViolation(self.name, "a LitmusTest instance may run only once")
        object.__setattr__(self, "_has_run", True)

        test = self._test
        if test is not None:
            return await test(utils)

        suite = self.build_suite()
        return await suite.main(utils)


@final
class _Case(LitmusTest):
    """Internal leaf specialization created only by :meth:`LitmusTest.case`."""

    __slots__ = ()

    def __init__(self, *, name: str, test: _TestFunction) -> None:
        super().__init__(name=name)
        self._test = test
