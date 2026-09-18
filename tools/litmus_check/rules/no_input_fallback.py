"""``litmus/no-input-fallback`` — one source per ``configure()`` value: identity or discovery.

Ruled on issue #142 (2026-09-12) and written into ``docs/idioms.md`` under "The Node
Names What It Needs; the Framework Decides Where It Comes From": a constructor argument
is identity fixed at composition time, runtime state comes from ``utils`` in
``configure()`` as one call with no fallback. ``self._ip or await
utils.get_arp_resolver(mac=self._mac).resolve()`` is two sources of truth for one
value, a branch the suite engineer wrote, and a record that cannot say which path
produced it. Ty ruled on issue #144 that this rule ships in 1.0.0 (issue #147).

Flagged, inside ``configure()`` of a statically known ``LitmusTest``:

- an ``or`` chain or a conditional expression whose alternatives are a ``self``
  attribute on one side and a discovery call on the other, in either order;
- a local name bound from a ``self`` attribute by one assignment and from a discovery
  call by another (an ``if``/``else``, a ``try``/``except``, a re-assignment after a
  ``None`` check), reported at the assignment that completes the pair.

A discovery call is ``get_arp_resolver``, ``use_shell``, ``get_shell``, ``get_http``, or
``get_coap`` on the method's second parameter, awaited or not, including a call,
attribute, or subscript chained off its result (``.resolve()``, ``["ip"]``). A local name
bound to either side counts as that side, through any chain of local names.

Not flagged: identity fed *into* discovery (``await utils.get_shell(host=self._host)``);
identity read from the run's environment (``self._mac or utils.env.require("DUT_MAC")``),
which issue #143 recorded as the declared input of a root node, not discovery; two
identities or two discoveries as alternatives; a helper of ``self`` called as one side
(``self._probe()`` is a method, not a stored attribute, and what it does is out of
reach); the same shapes outside ``configure``, including a closure defined inside it.

Tier-1 counterpart: none, by design. The rule guards a style the ruling forbids, not
hardware or verdict integrity; the runtime records which utility provided a value
either way, and a suite that skips the linter loses the early warning, not a guard
(PLAN section 3.4, the tier check recorded on issue #144).
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from litmus_check.model import Rule, SuiteModule, Violation

type FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef
type Binding = tuple[ast.stmt | ast.expr, ast.expr]

_CONFIGURE = "configure"
_DISCOVERY = frozenset({"get_arp_resolver", "use_shell", "get_shell", "get_http", "get_coap"})
_TAIL = (
    "the node names what it needs: take it from the constructor or from utils in "
    "configure(), never one with the other as fallback (issue #142)"
)


class NoInputFallbackRule(Rule):
    """Flag a ``configure()`` value that falls back between a ``self`` attribute and ``utils``."""

    code = "litmus/no-input-fallback"
    summary = "configure() takes a value from the constructor or from utils, never one as fallback"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        """Report every identity-or-discovery fallback inside ``configure()``.

        See the ``litmus/no-input-fallback`` row in ``docs/litmus-check.md``.
        """
        for klass in module.litmus_classes():
            configure = _member(klass, _CONFIGURE)
            if configure is None:
                continue
            reading = _Configure.of(module, configure)
            if reading is None:
                continue
            found = [*reading.choice_violations(self.code), *reading.binding_violations(self.code)]
            yield from sorted(found)


@dataclass(frozen=True, slots=True)
class _Sources:
    """Where a value can come from: ``self`` attributes and discovery utilities."""

    identity: frozenset[str] = frozenset()
    discovery: frozenset[str] = frozenset()

    def __or__(self, other: _Sources) -> _Sources:
        return _Sources(self.identity | other.identity, self.discovery | other.discovery)

    @property
    def mixed(self) -> bool:
        """True when one value already carries both an identity and a discovery."""
        return bool(self.identity and self.discovery)


_NOTHING = _Sources()


class _Configure:
    """The static reading of one ``configure()`` body: its local bindings and its choices."""

    def __init__(self, module: SuiteModule, method: FunctionDef, receiver: str, utils: str) -> None:
        self._module = module
        self._method = method
        self._receiver = receiver
        self._utils = utils
        self._bindings: dict[str, list[Binding]] = self._collect_bindings()

    @classmethod
    def of(cls, module: SuiteModule, method: FunctionDef) -> _Configure | None:
        """Read ``method`` when it has the ``(self, utils)`` shape; ``None`` otherwise."""
        positional = method.args.posonlyargs + method.args.args
        if len(positional) < 2:
            return None
        return cls(module, method, positional[0].arg, positional[1].arg)

    # --- the two shapes -------------------------------------------------------------

    def choice_violations(self, code: str) -> Iterator[Violation]:
        """An ``or`` chain or a conditional expression choosing between the two sources."""
        for node in self._own_nodes():
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                alternatives = node.values
            elif isinstance(node, ast.IfExp):
                alternatives = [node.body, node.orelse]
            else:
                continue
            pair = _fallback_pair([self.sources(value) for value in alternatives])
            if pair is None:
                continue
            attribute, utility = pair
            yield self._module.violation(
                node,
                code,
                f"{self._receiver}.{attribute} falls back to {self._utils}.{utility}(): {_TAIL}",
            )

    def binding_violations(self, code: str) -> Iterator[Violation]:
        """A local name bound from one source by one assignment and the other by another."""
        for name, bindings in self._bindings.items():
            seen = _NOTHING
            for statement, value in bindings:
                sources = self.sources(value)
                if sources.mixed:
                    # An ``or``/conditional reported as a choice, or an alias of a name
                    # already reported; either way not this binding's finding.
                    break
                pair = _fallback_pair([seen, sources])
                if pair is not None:
                    attribute, utility = pair
                    yield self._module.violation(
                        statement,
                        code,
                        f'"{name}" is bound from {self._receiver}.{attribute} and from '
                        f"{self._utils}.{utility}(): {_TAIL}",
                    )
                    break
                seen = seen | sources

    # --- where a value comes from ---------------------------------------------------

    def sources(self, node: ast.expr, visiting: frozenset[str] = frozenset()) -> _Sources:
        """What ``node`` can evaluate to, through ``or``, conditionals, and local names."""
        if isinstance(node, ast.Await | ast.NamedExpr):
            return self.sources(node.value, visiting)
        if isinstance(node, ast.BoolOp):
            return _union(self.sources(value, visiting) for value in node.values)
        if isinstance(node, ast.IfExp):
            return self.sources(node.body, visiting) | self.sources(node.orelse, visiting)
        if isinstance(node, ast.Name):
            if node.id in visiting:
                return _NOTHING
            inner = visiting | {node.id}
            return _union(
                self.sources(value, inner) for _, value in self._bindings.get(node.id, [])
            )
        attribute = self._identity(node)
        if attribute is not None:
            return _Sources(identity=frozenset({attribute}))
        utility = self._discovery(node)
        if utility is not None:
            return _Sources(discovery=frozenset({utility}))
        if isinstance(node, ast.Attribute | ast.Subscript):
            # ``resolved["ip"]`` or ``shell.host``: a chain off a local name.
            return self.sources(node.value, visiting)
        return _NOTHING

    def _identity(self, node: ast.expr) -> str | None:
        """The attribute name of a ``self.<attr>`` read, or of an attribute chain on one."""
        while isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == self._receiver:
                return node.attr
            node = node.value
        return None

    def _discovery(self, node: ast.expr) -> str | None:
        """The utility name when ``node`` is a discovery call or a chain off its result."""
        called = False
        while True:
            if isinstance(node, ast.Await):
                node = node.value
            elif isinstance(node, ast.Call):
                called = True
                node = node.func
            elif isinstance(node, ast.Subscript):
                node = node.value
            elif isinstance(node, ast.Attribute):
                value = node.value
                if isinstance(value, ast.Name) and value.id == self._utils:
                    return node.attr if called and node.attr in _DISCOVERY else None
                node = value
            else:
                return None

    # --- the body -------------------------------------------------------------------

    def _own_nodes(self) -> Iterator[ast.AST]:
        """Nodes of ``configure`` itself, not of a closure defined inside it."""
        for node in ast.walk(self._method):
            if node is self._method or self._module.enclosing_function(node) is self._method:
                yield node

    def _collect_bindings(self) -> dict[str, list[Binding]]:
        """Every assignment to a simple local name, in source order, with its value.

        Sources are read lazily through :meth:`sources`, so a value may name a local
        assigned later in the text; the cycle guard there keeps the reading finite.
        """
        raw: list[tuple[str, ast.stmt | ast.expr, ast.expr]] = []
        for node in self._own_nodes():
            if isinstance(node, ast.Assign):
                raw.extend(
                    (t.id, node, node.value) for t in node.targets if isinstance(t, ast.Name)
                )
            elif isinstance(node, ast.AnnAssign):
                if node.value is not None and isinstance(node.target, ast.Name):
                    raw.append((node.target.id, node, node.value))
            elif isinstance(node, ast.NamedExpr):
                raw.append((node.target.id, node, node.value))
        raw.sort(key=lambda item: (item[1].lineno, item[1].col_offset))
        bindings: dict[str, list[Binding]] = {}
        for name, statement, value in raw:
            bindings.setdefault(name, []).append((statement, value))
        return bindings


def _fallback_pair(alternatives: Iterable[_Sources]) -> tuple[str, str] | None:
    """The ``(attribute, utility)`` when one alternative is identity and another discovery."""
    listed = list(alternatives)
    for index, left in enumerate(listed):
        for offset, right in enumerate(listed):
            if index != offset and left.identity and right.discovery:
                return min(left.identity), min(right.discovery)
    return None


def _union(items: Iterable[_Sources]) -> _Sources:
    total = _NOTHING
    for item in items:
        total = total | item
    return total


def _member(klass: ast.ClassDef, name: str) -> FunctionDef | None:
    for member in klass.body:
        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) and member.name == name:
            return member
    return None
