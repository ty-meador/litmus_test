"""Mechanical fixes for the two rules that have one.

``litmus/gated-pass`` wraps an unconditional ``return PASS`` in an explicit
predicate (derived from preceding ``if ...: raise`` guards when those exist
and are safe to invert). A shape we cannot rewrite gets a fail-closed
scaffold — never a truthy placeholder that would silence the rule and still
return PASS. ``litmus/method-reference`` lifts a trivial ``lambda`` to the
method reference it forwards to.
"""

from __future__ import annotations

import ast
from abc import ABC, abstractmethod

from litmus_check.lsp import (
    LspCodeAction,
    LspDiagnostic,
    TextEdit,
    ensure_testfailure_import,
    inner_indent,
    line_indent,
    node_at,
    node_span,
)
from litmus_check.model import SuiteModule

_UNKNOWN_MSG = (
    "unknown failure mode reached — extend the pass predicate or add a specific guard above"
)
_UNKNOWN_EXPECTED = "all enumerated failure modes to be false"
_UNKNOWN_ACTUAL = "none of the guards matched but the pass predicate did not hold"
_FALLBACK_MSG = "What went wrong? be specific, this needs to be actionable by a floor technician"
_FALLBACK_EXPECTED = "what did you expect to happen?"
_FALLBACK_ACTUAL = "what actually happened?"

_FLIP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.LtE: ast.Gt,
    ast.Gt: ast.LtE,
    ast.GtE: ast.Lt,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}


class CodeFix(ABC):
    """One mechanical rewrite attached to one rule code."""

    code: str

    @abstractmethod
    def offer(
        self,
        module: SuiteModule,
        source: str,
        diagnostic: LspDiagnostic,
    ) -> LspCodeAction | None:
        """Return a quick-fix for ``diagnostic``, or ``None`` when the shape is not rewritable."""


class GatedPassFix(CodeFix):
    """Wrap an unconditional ``return PASS`` in an explicit pass predicate."""

    code = "litmus/gated-pass"

    def offer(
        self,
        module: SuiteModule,
        source: str,
        diagnostic: LspDiagnostic,
    ) -> LspCodeAction | None:
        node = node_at(module.tree, diagnostic.start_line, diagnostic.start_character, ast.Return)
        if not isinstance(node, ast.Return) or node.value is None:
            return None
        smart = self._smart(module, source, diagnostic, node)
        if smart is not None:
            return smart
        return self._fallback(module, source, diagnostic, node)

    def _smart(
        self,
        module: SuiteModule,
        source: str,
        diagnostic: LspDiagnostic,
        return_node: ast.Return,
    ) -> LspCodeAction | None:
        parent = module.parent_of(return_node)
        statements = _statement_list(parent)
        if statements is None:
            return None
        guards = _contiguous_guards(statements, return_node)
        if not guards:
            return None
        if any(_contains_call(guard.test) for guard in guards):
            return None
        loaded: set[str] = set()
        for guard in guards:
            loaded |= _loaded_names(guard.test)
        if loaded & _names_bound_from(statements, guards[0]):
            return None
        predicate = " and ".join(_negate(guard.test) for guard in guards)
        first = guards[0]
        first_span = node_span(first)
        return_span = node_span(return_node)
        if first_span is None or return_span is None or return_node.value is None:
            return None
        indent = line_indent(source, first_span[0])
        inner = inner_indent(line_indent(source, return_span[0]))
        pass_expr = ast.unparse(return_node.value)
        # Inserted above the contiguous guard block, not in place of the return:
        # the pass predicate is then the first statement of the shape the rule
        # teaches, and the original raises stay as the named failure modes.
        # Insertion above a non-contiguous statement is how a name bound
        # between guard and return became a NameError, and how a call in a
        # guard test ran twice — those shapes fall back instead of guessing.
        insert = TextEdit(
            first_span[0],
            first_span[1],
            first_span[0],
            first_span[1],
            f"if {predicate}: return {pass_expr}\n{indent}",
        )
        replace = TextEdit(
            return_span[0],
            return_span[1],
            return_span[2],
            return_span[3],
            _raise_testfailure(inner, indent, _UNKNOWN_MSG, _UNKNOWN_EXPECTED, _UNKNOWN_ACTUAL),
        )
        edits = [insert, replace]
        imported = ensure_testfailure_import(module)
        if imported is not None:
            edits.append(imported)
        return LspCodeAction(
            title="Derive pass predicate from preceding failure conditions",
            diagnostic=diagnostic,
            edits=tuple(edits),
        )

    def _fallback(
        self,
        module: SuiteModule,
        source: str,
        diagnostic: LspDiagnostic,
        return_node: ast.Return,
    ) -> LspCodeAction | None:
        span = node_span(return_node)
        if span is None or return_node.value is None:
            return None
        indent = line_indent(source, span[0])
        inner = inner_indent(indent)
        pass_expr = ast.unparse(return_node.value)
        raise_stmt = _raise_testfailure(
            inner, indent, _FALLBACK_MSG, _FALLBACK_EXPECTED, _FALLBACK_ACTUAL
        )
        # `False` cannot be mistaken for a passing device. `...` (Ellipsis) is
        # truthy, so a leftover `if ...:` scaffold returned PASS unconditionally
        # and silenced the rule that was meant to catch that.
        new_text = (
            f"if False:  # what does a passing device look like?\n"
            f"{inner}return {pass_expr}\n"
            f"{indent}{raise_stmt}"
        )
        edits = [TextEdit(span[0], span[1], span[2], span[3], new_text)]
        imported = ensure_testfailure_import(module)
        if imported is not None:
            edits.append(imported)
        return LspCodeAction(
            title="Wrap return PASS in an explicit pass predicate",
            diagnostic=diagnostic,
            edits=tuple(edits),
        )


class MethodReferenceFix(CodeFix):
    """Replace a trivial ``lambda`` passed to ``test()`` with the method it forwards."""

    code = "litmus/method-reference"

    def offer(
        self,
        module: SuiteModule,
        source: str,
        diagnostic: LspDiagnostic,
    ) -> LspCodeAction | None:
        del source
        node = node_at(module.tree, diagnostic.start_line, diagnostic.start_character, ast.Lambda)
        if not isinstance(node, ast.Lambda):
            return None
        method_ref = _lambda_method_ref(node)
        if method_ref is None:
            return None
        span = node_span(node)
        if span is None:
            return None
        return LspCodeAction(
            title=f"Replace with `{method_ref}`",
            diagnostic=diagnostic,
            edits=(TextEdit(span[0], span[1], span[2], span[3], method_ref),),
        )


def _statement_list(parent: ast.AST | None) -> list[ast.stmt] | None:
    if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module):
        return list(parent.body)
    if isinstance(parent, ast.For | ast.AsyncFor | ast.While | ast.If | ast.With | ast.AsyncWith):
        return list(parent.body)
    if isinstance(parent, ast.ExceptHandler):
        return list(parent.body)
    if isinstance(parent, ast.Try):
        return list(parent.body)
    return None


def _contiguous_guards(statements: list[ast.stmt], return_node: ast.Return) -> list[ast.If]:
    """Pure ``if ...: raise`` statements sitting flush against ``return_node``.

    Leading setup before the first such guard is ignored. A non-guard between
    two guards, or between a guard and the return, is not a subset we invert:
    that is how a name bound in between was read before it existed.
    """
    guards: list[ast.If] = []
    for statement in statements:
        if statement is return_node:
            break
        if isinstance(statement, ast.If) and _is_pure_raise_guard(statement):
            guards.append(statement)
            continue
        if guards:
            return []
    return guards


def _contains_call(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Call) for child in ast.walk(node))


def _loaded_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }


def _bound_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
    }


def _names_bound_from(statements: list[ast.stmt], start: ast.stmt) -> set[str]:
    names: set[str] = set()
    seen = False
    for statement in statements:
        if statement is start:
            seen = True
        if seen:
            names |= _bound_names(statement)
    return names


def _is_pure_raise_guard(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.If) or statement.orelse:
        return False
    if len(statement.body) != 1:
        return False
    return isinstance(statement.body[0], ast.Raise)


def _negate(expr: ast.expr) -> str:
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        return ast.unparse(expr.operand)
    if isinstance(expr, ast.Compare) and len(expr.ops) == 1:
        flipped = _FLIP.get(type(expr.ops[0]))
        if flipped is not None:
            negated = ast.Compare(left=expr.left, ops=[flipped()], comparators=expr.comparators)
            return ast.unparse(negated)
    if isinstance(expr, ast.Name | ast.Attribute | ast.Subscript | ast.Constant):
        return f"not {ast.unparse(expr)}"
    return f"not ({ast.unparse(expr)})"


def _lambda_method_ref(node: ast.Lambda) -> str | None:
    body: ast.expr = node.body
    if isinstance(body, ast.Await):
        body = body.value
    if not isinstance(body, ast.Call):
        return None
    if node.args.vararg or node.args.kwarg or node.args.kwonlyargs or node.args.defaults:
        return None
    if body.keywords:
        return None
    params = [arg.arg for arg in node.args.args]
    if len(body.args) != len(params):
        return None
    for param, arg in zip(params, body.args, strict=True):
        if not isinstance(arg, ast.Name) or arg.id != param:
            return None
    if not isinstance(body.func, ast.Attribute | ast.Name):
        return None
    return ast.unparse(body.func)


def _raise_testfailure(inner: str, indent: str, message: str, expected: str, actual: str) -> str:
    return (
        f"raise TestFailure(\n"
        f"{inner}{message!r},\n"
        f"{inner}{expected!r},\n"
        f"{inner}{actual!r},\n"
        f"{indent})"
    )
