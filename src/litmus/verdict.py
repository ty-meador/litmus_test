"""The non-forgeable PASS sentinel and the `Verdict` type every test method returns.

The verdict contract is enforced at runtime by identity, not by equality: the child
boundary asks `result is PASS`. Nothing a suite can construct — a string, a truthy
value, a parsed config — satisfies that check. `PASS` can only be obtained by
importing it.
"""

from typing import ClassVar, final


@final
class _Pass:
    """The single value that means "this node passed".

    Contract:
      - Exactly one instance exists per interpreter. `_Pass()` returns it; copying,
        deep-copying, and unpickling all return it. There is no second object that
        `is PASS`, so the sentinel cannot be forged by any route the framework does
        not control.
      - `repr()` is the bare word `PASS`, so logs and artifacts read as the FT
        expects rather than showing an object address.

    Instantiate nothing here; import `PASS`.
    """

    __slots__ = ()

    _instance: ClassVar[_Pass | None] = None

    def __new__(cls) -> _Pass:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "PASS"

    def __copy__(self) -> _Pass:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _Pass:
        return self

    def __reduce__(self) -> tuple[type[_Pass], tuple[()]]:
        # Unpickling must route back through __new__ so a round-trip cannot mint a
        # second instance that would fail the identity check while looking correct.
        return (_Pass, ())


PASS = _Pass()

type Verdict = _Pass
"""Return annotation for every test method: `async def _check(...) -> Verdict`.

Static counterpart to the runtime check. A method annotated `-> Verdict` that falls
off its end is a pyright error at write time; the runtime check remains the guarantee.
"""
