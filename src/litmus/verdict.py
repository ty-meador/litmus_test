"""The non-forgeable PASS sentinel, the `Verdict` every test method returns, and the
evidence a verdict carries.

A test method has exactly two exits: `return PASS(...)` and `raise TestFailure(...)`.
Evidence leaves with the verdict. The bare `PASS` is a verdict with no evidence;
`PASS(evidence)` is a verdict carrying a mapping of readings. The child boundary
accepts a `Verdict` instance and nothing else: a string, a truthy value, a parsed
config all fail the type check, and a `Verdict` can only be obtained by importing
`PASS` or calling it. The payload is validated at the boundary, not here, so the
violation can name the leaf as well as the key (issue #118).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar, Never, cast, final


@final
class Verdict[E: Mapping[str, object] = Mapping[str, object]]:
    """The value that means "this node passed", with or without evidence.

    Contract:
      - Exactly one bare instance exists per interpreter. `Verdict()` returns it;
        copying, deep-copying, and unpickling all return it. It is the value bound to
        `PASS`.
      - `PASS(evidence)` mints a verdict that carries a detached copy of `evidence`.
        It is a distinct object, so identity with `PASS` is never the check: the
        child boundary asks `isinstance(result, Verdict)`.
      - `evidence` is read-only. The boundary copies it into `run_data` after checking
        every entry; a carrying verdict cannot be edited after it is returned.
      - `repr()` is the bare word `PASS`, or `PASS({...})` for a carrying verdict, so
        logs and artifacts read as the FT expects rather than showing an address.

    The type parameter is the evidence shape. A method annotated
    `-> Verdict[MyEvidence]` with a `TypedDict` has its `PASS({...})` payload checked
    against that shape by pyright; the bare `-> Verdict` accepts any mapping.
    """

    __slots__ = ("_evidence",)

    _evidence: Mapping[str, object] | None
    _bare: ClassVar[Verdict[Never] | None] = None

    def __new__(cls) -> Verdict[Never]:
        if cls._bare is None:
            bare = cast("Verdict[Never]", super().__new__(cls))
            object.__setattr__(bare, "_evidence", None)
            cls._bare = bare
        return cls._bare

    def __call__[P: Mapping[str, object]](self, evidence: P) -> Verdict[P]:
        """Mint a verdict carrying a detached copy of ``evidence``."""
        carrying = cast("Verdict[P]", object.__new__(Verdict))
        object.__setattr__(carrying, "_evidence", MappingProxyType(dict(evidence)))
        return carrying

    @property
    def evidence(self) -> Mapping[str, object] | None:
        """The readings this verdict carries, or ``None`` for the bare ``PASS``."""
        return self._evidence

    def __setattr__(self, name: str, value: object) -> Never:
        msg = "a Verdict is immutable; mint a new one with PASS(evidence)"
        raise AttributeError(msg)

    def __repr__(self) -> str:
        if self._evidence is None:
            return "PASS"
        return f"PASS({dict(self._evidence)!r})"

    def __copy__(self) -> Verdict[E]:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Verdict[E]:
        return self

    def __reduce__(self) -> tuple[object, tuple[object, ...]]:
        # Unpickling must route back through __new__ so a round-trip cannot mint a
        # second bare instance that would fail the type check while looking correct.
        # A carrying verdict rebuilds through PASS so its payload stays a copy.
        if self._evidence is None:
            return (Verdict, ())
        return (_carrying, (dict[str, object](self._evidence),))


def _carrying(evidence: Mapping[str, object]) -> Verdict[Mapping[str, object]]:
    return PASS(evidence)


PASS: Verdict[Never] = Verdict()
"""The verdict. `return PASS` passes with no evidence; `return PASS({...})` passes
and carries the readings that prove it."""
