"""``litmus/ctx-provenance`` — every ``utils.ctx["k"]`` read is provided by ``configure()``,
and ctx is never written.

A test method that reads ``utils.ctx["k"]`` relies on ``configure()`` having returned
``k``. Nothing else may be relied on: ctx is what ``configure()`` provided and it is
read-only from the moment configure returns (issue #118). A reading belongs on the
verdict, ``PASS({...})`` or ``TestFailure(..., evidence=...)``; a value cleanup must
restore is captured in ``configure()``. A placeholder seeded so a key exists fabricates
evidence (issue #112).

Flagged, as a read: a subscript **read** ``utils.ctx["k"]`` with a string-literal key —
directly or through a local alias such as ``ctx = cast(_Ctx, utils.ctx)`` — inside
``configure``, ``cleanup``, a test method, or a survey/audit hook of a statically known
``LitmusTest``, when ``k`` is not a key of the dict literal ``configure()`` returns (or a
field of the ``TypedDict`` it is annotated to return).

Flagged, as a write: any assignment to ``utils.ctx["k"]``, an augmented assignment, ``|=``,
or a call to ``update``, ``setdefault``, ``pop``, ``popitem``, or ``clear`` on ctx, in any of
those methods including ``configure`` itself, which returns its context rather than
writing it.

Not flagged: ``utils.ctx.get("k")``, ``"k" in utils.ctx``, a computed key, and — for reads
only — any class whose ``configure()`` return cannot be read statically: a ``**spread``, a
computed key, a call, a name bound outside the method. The rule stays silent on such a
class's reads rather than guess; tier 3 is skippable by design and tier 1 is the guard.

Tier-1 counterpart: a ``[]`` read of a missing key raises ``ContractViolation`` naming
the test and the key; a write after ``configure()`` raises ``ContractViolation`` naming
the test and the key; ``get`` and ``in`` never do.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from litmus_check.model import Rule, SuiteModule, Violation

type FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef

_CONFIGURE = "configure"
_CLEANUP = "cleanup"
_NON_READERS = frozenset({"__init__", "build_suite"})
_REGISTRARS = frozenset({"use_survey", "use_audit"})
_MUTATORS = frozenset({"update", "setdefault", "pop", "popitem", "clear"})
_VERDICT = "Verdict"
_TYPED_DICT = "TypedDict"
_BASE_CLASS = "LitmusTest"
_CTX = "ctx"
_CAST = "cast"
_DICT = "dict"
_WILDCARD = "*"


class CtxProvenanceRule(Rule):
    """Flag a ``utils.ctx["k"]`` read that ``configure()`` does not provide, and any write."""

    code = "litmus/ctx-provenance"
    summary = "every utils.ctx[key] read is provided by configure(); ctx is never written"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        for klass in module.litmus_classes():
            provided = _provided_keys(module, klass)
            for method in _reader_methods(module, klass):
                yield from self._check_method(module, method, provided)

    def _check_method(
        self,
        module: SuiteModule,
        method: FunctionDef,
        provided: frozenset[str] | None,
    ) -> Iterator[Violation]:
        utils = _utils_parameter(method)
        if utils is None:
            return
        available: frozenset[str] = frozenset()
        if method.name != _CONFIGURE and provided is not None:
            available = provided
        for event in _events(method, utils):
            if event.write:
                yield module.violation(
                    event.node,
                    self.code,
                    f"ctx is read-only after configure(): {event.key} is written here; "
                    "return it from configure(), or carry it as evidence on PASS",
                )
                continue
            if provided is None and method.name != _CONFIGURE:
                continue
            if event.key in available or _WILDCARD in available:
                continue
            yield module.violation(
                event.node,
                self.code,
                f'"{event.key}" is read here but configure() does not provide it; '
                "return it from configure(), or read it with .get() if it is optional",
            )


@dataclass(frozen=True, slots=True)
class _Event:
    """One ctx access in a method body, ordered by source position."""

    position: tuple[int, int]
    write: bool
    key: str
    node: ast.AST


def _events(method: FunctionDef, utils: str) -> list[_Event]:
    """Every literal-key read and every write of ctx in ``method``, in source order.

    A read is keyed by its literal. A write is keyed by the text the violation names:
    the literal key for a subscript, ``setdefault``, or ``pop`` with one, otherwise the
    mutator call (``update(...)``). An augmented assignment is reported once, as the
    write.
    """
    aliases = _ctx_aliases(method, utils)
    events: list[_Event] = []
    for node in ast.walk(method):
        if isinstance(node, ast.Subscript):
            if isinstance(node.ctx, ast.Load) and _is_ctx(node.value, utils, aliases):
                key = _literal_key(node.slice)
                if key is not None:
                    events.append(_Event(_start(node), False, key, node))
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            events.extend(_assignment_events(node, utils, aliases))
        elif isinstance(node, ast.Call):
            label = _mutator_label(node, utils, aliases)
            if label is not None:
                events.append(_Event(_start(node), True, label, node))
    events.sort(key=lambda event: (event.position, event.write))
    return events


def _assignment_events(
    node: ast.Assign | ast.AnnAssign | ast.AugAssign, utils: str, aliases: frozenset[str]
) -> Iterator[_Event]:
    if node.value is None:
        return
    if isinstance(node, ast.AugAssign):
        targets: list[ast.expr] = [node.target]
        if _is_ctx(node.target, utils, aliases):
            # ``utils.ctx |= {...}`` writes the whole mapping.
            yield _Event(_start(node), True, "ctx |= ...", node)
            return
    else:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        if not isinstance(target, ast.Subscript) or not _is_ctx(target.value, utils, aliases):
            continue
        key = _literal_key(target.slice)
        yield _Event(_start(node), True, "a computed key" if key is None else repr(key), node)


def _mutator_keys(call: ast.Call, utils: str, aliases: frozenset[str]) -> Iterator[str]:
    """Keys written by ``update(...)`` or ``setdefault(...)`` on a local dict configure builds."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in ("update", "setdefault"):
        return
    if not _is_ctx(func.value, utils, aliases):
        return
    if func.attr == "setdefault":
        if call.args:
            key = _literal_key(call.args[0])
            if key is not None:
                yield key
        return
    for keyword in call.keywords:
        if keyword.arg is not None:
            yield keyword.arg
    if not call.args:
        return
    literal = _literal_dict_keys(call.args[0]) if isinstance(call.args[0], ast.Dict) else None
    if literal is None:
        # ``update(mapping)`` with an unreadable mapping may have written anything.
        yield _WILDCARD
        return
    yield from literal


def _mutator_label(call: ast.Call, utils: str, aliases: frozenset[str]) -> str | None:
    """What a mutating call on ctx wrote, for the violation message; ``None`` otherwise."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in _MUTATORS:
        return None
    if not _is_ctx(func.value, utils, aliases):
        return None
    if func.attr in ("setdefault", "pop") and call.args:
        key = _literal_key(call.args[0])
        if key is not None:
            return repr(key)
    return f"{func.attr}(...)"


def _ctx_aliases(method: FunctionDef, utils: str) -> frozenset[str]:
    """Local names bound to ``utils.ctx`` directly or through ``cast(..., utils.ctx)``."""
    aliases: set[str] = set()
    for node in ast.walk(method):
        if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        if _is_ctx(_uncast(node.value), utils, frozenset()):
            aliases.add(targets[0].id)
    return frozenset(aliases)


def _uncast(node: ast.expr) -> ast.expr:
    if (
        isinstance(node, ast.Call)
        and _simple_name(node.func) == _CAST
        and len(node.args) == 2
        and not node.keywords
    ):
        return node.args[1]
    return node


def _is_ctx(node: ast.expr, utils: str, aliases: frozenset[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases
    return (
        isinstance(node, ast.Attribute)
        and node.attr == _CTX
        and isinstance(node.value, ast.Name)
        and node.value.id == utils
    )


def _literal_key(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _start(node: ast.expr | ast.stmt) -> tuple[int, int]:
    return (node.lineno, node.col_offset)


# --- which methods read ctx ---------------------------------------------------------


def _reader_methods(module: SuiteModule, klass: ast.ClassDef) -> list[FunctionDef]:
    """``configure``, ``cleanup``, test methods, and registered survey/audit hooks."""
    leaves = _referenced_method_names(module, klass, {"test"})
    hooks = _referenced_method_names(module, klass, _REGISTRARS)
    readers: list[FunctionDef] = []
    for member in klass.body:
        if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if member.name in _NON_READERS:
            continue
        if (
            member.name in (_CONFIGURE, _CLEANUP)
            or member.name in leaves
            or member.name in hooks
            or _is_verdict(member.returns)
        ):
            readers.append(member)
    return readers


def _referenced_method_names(
    module: SuiteModule, klass: ast.ClassDef, registrars: Iterable[str]
) -> frozenset[str]:
    """Names of ``self.<method>`` handed to ``self.<registrar>(...)`` inside ``klass``."""
    wanted = frozenset(registrars)
    names: set[str] = set()
    for node in ast.walk(klass):
        if not isinstance(node, ast.Call) or module.enclosing_class(node) is not klass:
            continue
        method = module.enclosing_method(node)
        if method is None:
            continue
        receiver = _first_parameter(method)
        if receiver is None:
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr in wanted
            and isinstance(func.value, ast.Name)
            and func.value.id == receiver
        ):
            continue
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            if (
                isinstance(argument, ast.Attribute)
                and isinstance(argument.value, ast.Name)
                and argument.value.id == receiver
            ):
                names.add(argument.attr)
    return frozenset(names)


def _utils_parameter(function: FunctionDef) -> str | None:
    positional = function.args.posonlyargs + function.args.args
    if len(positional) < 2:
        return None
    return positional[1].arg


def _first_parameter(function: FunctionDef) -> str | None:
    positional = function.args.posonlyargs + function.args.args
    if not positional:
        return None
    return positional[0].arg


def _is_verdict(annotation: ast.expr | None) -> bool:
    if isinstance(annotation, ast.Subscript):
        annotation = annotation.value
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        bare = annotation.value.split("[", 1)[0]
        return bare.rsplit(".", maxsplit=1)[-1] == _VERDICT
    return _simple_name(annotation) == _VERDICT


def _simple_name(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


# --- what configure() provides ------------------------------------------------------


def _provided_keys(module: SuiteModule, klass: ast.ClassDef) -> frozenset[str] | None:
    """Keys ``configure()`` is statically known to return; ``None`` when unreadable."""
    configure = _member(klass, _CONFIGURE)
    if configure is None:
        return _inherited_keys(module, klass)
    keys: set[str] = set(_typed_dict_fields(module, configure.returns))
    local_dicts = _local_dicts(configure)
    for node in ast.walk(configure):
        if not isinstance(node, ast.Return) or module.enclosing_function(node) is not configure:
            continue
        if node.value is None:
            continue
        returned = _returned_keys(_uncast(node.value), local_dicts)
        if returned is None:
            return None
        keys |= returned
    return frozenset(keys)


def _returned_keys(
    value: ast.expr, local_dicts: dict[str, set[str] | None]
) -> frozenset[str] | None:
    if isinstance(value, ast.Dict):
        return _literal_dict_keys(value)
    if isinstance(value, ast.Name):
        found = local_dicts.get(value.id)
        return None if found is None else frozenset(found)
    if isinstance(value, ast.Call):
        return _dict_call_keys(value)
    return None


def _local_dicts(configure: FunctionDef) -> dict[str, set[str] | None]:
    """Dict literals bound to local names in ``configure``, with later literal writes.

    A name is ``None`` (unreadable) once anything but a literal reaches it.
    """
    found: dict[str, set[str] | None] = {}
    for node in ast.walk(configure):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            if node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    keys = _returned_keys(_uncast(node.value), found)
                    found[target.id] = None if keys is None else set(keys)
                elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                    _add_local_key(found, target.value.id, _literal_key(target.slice))
        elif isinstance(node, ast.Call):
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr in _MUTATORS
                and isinstance(func.value, ast.Name)
            ):
                continue
            name = func.value.id
            if name not in found:
                continue
            for key in _mutator_keys(node, "", frozenset({name})):
                _add_local_key(found, name, key)
    return found


def _add_local_key(found: dict[str, set[str] | None], name: str, key: str | None) -> None:
    keys = found.get(name)
    if name not in found or keys is None:
        return
    if key is None or key == _WILDCARD:
        found[name] = None
        return
    keys.add(key)


def _literal_dict_keys(node: ast.Dict) -> frozenset[str] | None:
    keys: set[str] = set()
    for key in node.keys:
        if key is None:
            return None
        literal = _literal_key(key)
        if literal is None:
            return None
        keys.add(literal)
    return frozenset(keys)


def _dict_call_keys(call: ast.Call) -> frozenset[str] | None:
    """``dict(k=v, ...)`` provides its keywords; any other call is unreadable."""
    if _simple_name(call.func) != _DICT or call.args:
        return None
    keys: set[str] = set()
    for keyword in call.keywords:
        if keyword.arg is None:
            return None
        keys.add(keyword.arg)
    return frozenset(keys)


def _inherited_keys(module: SuiteModule, klass: ast.ClassDef) -> frozenset[str] | None:
    """Without a ``configure`` of its own, a class provides what its bases provide."""
    keys: set[str] = set()
    for base in klass.bases:
        name = _simple_name(base)
        if name == _BASE_CLASS:
            continue
        parent = _class_named(module, name)
        if parent is None:
            return None
        inherited = _provided_keys(module, parent)
        if inherited is None:
            return None
        keys |= inherited
    return frozenset(keys)


def _typed_dict_fields(module: SuiteModule, annotation: ast.expr | None) -> frozenset[str]:
    """Fields of a same-module ``TypedDict`` named by ``annotation``, else nothing."""
    klass = _class_named(module, _annotation_name(annotation))
    if klass is None:
        return frozenset()
    fields = _typed_dict_class_fields(module, klass)
    return frozenset() if fields is None else fields


def _typed_dict_class_fields(module: SuiteModule, klass: ast.ClassDef) -> frozenset[str] | None:
    """Fields of ``klass`` when it is a ``TypedDict`` (directly or through same-module bases)."""
    fields: set[str] = set()
    typed = False
    for base in klass.bases:
        name = _simple_name(base)
        if name == _TYPED_DICT:
            typed = True
            continue
        parent = _class_named(module, name)
        inherited = None if parent is None else _typed_dict_class_fields(module, parent)
        if inherited is not None:
            typed = True
            fields |= inherited
    if not typed:
        return None
    for statement in klass.body:
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            fields.add(statement.target.id)
    return frozenset(fields)


def _annotation_name(annotation: ast.expr | None) -> str | None:
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value.rsplit(".", maxsplit=1)[-1]
    return _simple_name(annotation)


def _class_named(module: SuiteModule, name: str | None) -> ast.ClassDef | None:
    if name is None:
        return None
    for statement in module.tree.body:
        if isinstance(statement, ast.ClassDef) and statement.name == name:
            return statement
    return None


def _member(klass: ast.ClassDef, name: str) -> FunctionDef | None:
    for member in klass.body:
        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) and member.name == name:
            return member
    return None
