"""``litmus/shell-in-configure`` — a route is declared in ``configure()``, nowhere else.

``use_shell`` with arguments is a declaration: it decides where this node's
``utils.exec`` lands, and the framework writes that decision to the timeline as one
``exec routing:`` line when ``configure()`` returns. A declaration from a test method,
``cleanup``, a survey, or an audit would move the route after that line was written,
so the Floor Technician would read a route the later commands never used.
``use_serial`` with arguments is the same declaration for a console by device path.

Flagged: ``<receiver>.use_shell(...)`` or ``<receiver>.use_serial(...)`` called with at
least one argument inside a statically known ``LitmusTest`` subclass when the
enclosing method is not ``configure`` itself. A closure defined in ``configure`` and
called later is not ``configure`` either. The receiver is not checked by name:
``utils`` is the convention, not the rule, and a helper may take the service object
under any name.

Not flagged: the bare ``use_shell()`` / ``use_serial()`` read, which may run anywhere,
and ``get_shell`` / ``get_serial``, which open an auxiliary endpoint without touching
the route.

Tier-1 counterpart: ``TestUtils.use_shell`` raises ``ContractViolation`` for a
declaring call (``host``, ``via``, ``inherit=False``, ``propagate=False``) from any slot
but ``configure``, and ``TestUtils.use_serial`` does the same for ``device``. This rule
moves that error from the first run to the editor. It reads "any argument" where the
runtime reads "any route-shaping argument" because a credential passed without a host
is a mistake in either reading.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from litmus_check.model import Rule, SuiteModule, Violation

_CONFIGURE = "configure"
_DECLARERS = frozenset({"use_shell", "use_serial"})


class ShellInConfigureRule(Rule):
    """Flag ``use_shell(...)`` / ``use_serial(...)`` with arguments outside ``configure``."""

    code = "litmus/shell-in-configure"
    summary = "use_shell and use_serial declare the exec route only inside configure()"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        """Report a declaring ``use_shell(...)`` or ``use_serial(...)`` outside ``configure``.

        See the ``litmus/shell-in-configure`` row in ``docs/litmus-check.md``.
        """
        for klass in module.litmus_classes():
            for node in ast.walk(klass):
                if isinstance(node, ast.Call):
                    declarer = _declaring_call(node)
                    if declarer is not None:
                        yield from self._check_call(module, node, declarer)

    def _check_call(
        self, module: SuiteModule, call: ast.Call, declarer: str
    ) -> Iterable[Violation]:
        method = module.enclosing_method(call)
        if method is None:
            return
        if method.name == _CONFIGURE and module.enclosing_function(call) is method:
            return
        yield module.violation(
            call,
            self.code,
            f"{declarer} declared a route outside configure(): the exec route is fixed when "
            "configure() returns so the 'exec routing:' line stays true for the whole "
            f"node; declare it there, or call {declarer}() with no arguments to read it",
        )


def _declaring_call(call: ast.Call) -> str | None:
    """The declarer's name when the call carries any argument; the bare call only reads."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in _DECLARERS):
        return None
    if not (call.args or call.keywords):
        return None
    return func.attr
