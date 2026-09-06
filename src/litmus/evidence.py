"""``@evidence``: the readings a test method promises to carry on ``PASS``.

A test method is a function from ``utils.ctx`` to a verdict. The decorator is the
method's return signature: it names the keys a pass must carry, beside the code that
keeps the promise. It annotates and registers nothing; ``build_suite`` still names and
binds the method. The declaration is fixed at class definition and never computed at
run time: pass criteria that vary by SKU or fixture are a leaf argument or a different
leaf, decided before ``build_suite`` runs (issue #118).

The child boundary holds ``PASS`` to the declaration: a declared key the payload does
not carry is a ``ContractViolation`` naming the leaf and the key. Extra keys are
allowed; the declaration is a floor. ``TestFailure`` is not held to it, since a failure
may fire before every reading was taken. An undecorated method may still return
``PASS(payload)``; the payload is recorded unchecked.

Two spellings::

    @evidence("output_voltage_v")
    async def _check_voltage(self, utils: TestUtils) -> Verdict: ...

    class VoltageEvidence(TypedDict):
        output_voltage_v: float

    @evidence(VoltageEvidence)
    async def _check_voltage(self, utils: TestUtils) -> Verdict[VoltageEvidence]: ...

The ``TypedDict`` form declares its required keys at run time and lets pyright check
the ``PASS({...})`` payload against the shape at write time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, overload

if TYPE_CHECKING:
    from litmus.verdict import Verdict

DECLARATION_ATTRIBUTE = "__litmus_evidence__"

type _Method[**P, R] = Callable[P, Awaitable[R]]


@overload
def evidence[**P, R](*keys: str) -> Callable[[_Method[P, R]], _Method[P, R]]: ...


@overload
def evidence[**P, E: Mapping[str, object]](
    shape: type[E], /
) -> Callable[[_Method[P, Verdict[E]]], _Method[P, Verdict[E]]]: ...


def evidence(*keys: str | type[Mapping[str, object]]) -> Callable[[Any], Any]:
    """Declare the evidence keys a ``PASS`` from the decorated test method must carry.

    Args:
        *keys: String keys, or exactly one ``TypedDict`` class whose required keys
            are the declaration.

    Raises:
        TypeError: The declaration is empty, mixes the two spellings, repeats a key,
            or names a class that is not a ``TypedDict``.
    """
    declared = _declared_keys(keys)

    def declare(fn: Any) -> Any:
        if getattr(fn, DECLARATION_ATTRIBUTE, None) is not None:
            msg = (
                f"{getattr(fn, '__qualname__', fn)!s} already declares its evidence; "
                "declare it once"
            )
            raise TypeError(msg)
        setattr(fn, DECLARATION_ATTRIBUTE, declared)
        return fn

    return declare


def declared_evidence(fn: object) -> tuple[str, ...] | None:
    """The keys ``fn`` was decorated to carry on ``PASS``, or ``None`` when undeclared."""
    declared = getattr(fn, DECLARATION_ATTRIBUTE, None)
    if declared is None:
        return None
    return tuple(declared)


def _declared_keys(keys: tuple[str | type[Mapping[str, object]], ...]) -> tuple[str, ...]:
    if not keys:
        msg = "@evidence() declares nothing; name the keys a PASS must carry"
        raise TypeError(msg)
    if len(keys) == 1 and isinstance(keys[0], type):
        return _typed_dict_keys(keys[0])
    names: list[str] = []
    for key in keys:
        if not isinstance(key, str):
            msg = "@evidence takes string keys or one TypedDict class, not both"
            raise TypeError(msg)
        if key in names:
            msg = f"@evidence declares {key!r} twice; declare each key once"
            raise TypeError(msg)
        names.append(key)
    return tuple(names)


def _typed_dict_keys(shape: type[Mapping[str, object]]) -> tuple[str, ...]:
    required: object = getattr(shape, "__required_keys__", None)
    annotations: object = getattr(shape, "__annotations__", None)
    if not isinstance(required, frozenset) or not isinstance(annotations, dict):
        msg = f"@evidence({shape.__name__}) needs a TypedDict class; got a plain class"
        raise TypeError(msg)
    ordered = [name for name in annotations if name in required]  # pyright: ignore[reportUnknownVariableType]
    if not ordered:
        msg = f"@evidence({shape.__name__}) declares nothing; the TypedDict has no required keys"
        raise TypeError(msg)
    return tuple(str(name) for name in ordered)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
