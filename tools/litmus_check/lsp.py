"""Editor-facing view of the ``litmus_check`` registry.

The CLI and the language-server adapter share one registry. This module converts
:class:`~litmus_check.model.Violation` values into LSP diagnostics and dispatches
the mechanical fixes; it does not import ``pylsp``. The plugin entry point lives
in :mod:`litmus_check.pylsp_plugin` and is an optional extra.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from litmus_check.checker import Report
from litmus_check.model import SuiteModule, Violation
from litmus_check.registry import RuleRegistry

LSP_SOURCE = "litmus-check"
"""``Diagnostic.source`` / ``CodeAction`` origin shown in the editor."""

DIAGNOSTIC_WARNING = 2
"""LSP ``DiagnosticSeverity.Warning``. Tier 3 is skippable; it is not a guard."""

QUICKFIX = "quickfix"


class LspObject:
    """Untyped LSP JSON payloads, narrowed for pyright strict."""

    @staticmethod
    def mapping(value: object) -> dict[str, object]:
        """Return ``value`` as ``dict[str, object]``, or raise."""
        if not isinstance(value, dict):
            msg = "expected an object"
            raise TypeError(msg)
        return cast("dict[str, object]", value)

    @staticmethod
    def integer(value: object) -> int:
        """Return ``value`` as ``int``, or raise. ``bool`` is rejected."""
        if isinstance(value, bool) or not isinstance(value, int):
            msg = "expected an integer"
            raise TypeError(msg)
        return value


@dataclass(frozen=True, slots=True)
class TextEdit:
    """One LSP text edit. Positions are 0-based, end exclusive."""

    start_line: int
    start_character: int
    end_line: int
    end_character: int
    new_text: str

    def to_lsp(self) -> dict[str, object]:
        """Render as an LSP ``TextEdit`` object."""
        return {
            "range": {
                "start": {"line": self.start_line, "character": self.start_character},
                "end": {"line": self.end_line, "character": self.end_character},
            },
            "newText": self.new_text,
        }

    @staticmethod
    def apply_all(source: str, edits: Sequence[TextEdit]) -> str:
        """Apply edits later-in-file first so earlier offsets stay valid."""
        lines = source.split("\n")
        ordered = sorted(
            edits,
            key=lambda edit: (edit.start_line, edit.start_character),
            reverse=True,
        )
        for edit in ordered:
            start = edit.start_line
            end = edit.end_line
            before = lines[start][: edit.start_character]
            after = lines[end][edit.end_character :]
            new_lines = edit.new_text.split("\n")
            if len(new_lines) == 1:
                replacement = [before + new_lines[0] + after]
            else:
                replacement = [before + new_lines[0], *new_lines[1:-1], new_lines[-1] + after]
            lines[start : end + 1] = replacement
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class LspRange:
    """An LSP range. Positions are 0-based, end exclusive."""

    start_line: int
    start_character: int
    end_line: int
    end_character: int

    def overlaps(self, other: LspRange) -> bool:
        """True when the ranges share any point, treating a caret as a hit.

        Inclusive on both ends so a collapsed cursor at the start of a diagnostic
        still selects it — editors send that shape for a lightbulb at the squiggle.
        """
        self_start = (self.start_line, self.start_character)
        self_end = (self.end_line, self.end_character)
        other_start = (other.start_line, other.start_character)
        other_end = (other.end_line, other.end_character)
        return self_start <= other_end and other_start <= self_end

    @classmethod
    def from_lsp(cls, payload: object) -> LspRange:
        """Parse an LSP ``Range`` dict."""
        data = LspObject.mapping(payload)
        start = LspObject.mapping(data.get("start"))
        end = LspObject.mapping(data.get("end"))
        return cls(
            LspObject.integer(start.get("line")),
            LspObject.integer(start.get("character")),
            LspObject.integer(end.get("line")),
            LspObject.integer(end.get("character")),
        )


@dataclass(frozen=True, slots=True)
class LspDiagnostic:
    """One rule finding as an LSP diagnostic."""

    path: Path
    code: str
    message: str
    start_line: int
    start_character: int
    end_line: int
    end_character: int
    severity: int = DIAGNOSTIC_WARNING
    source: str = LSP_SOURCE

    @property
    def range(self) -> LspRange:
        """The squiggle span."""
        return LspRange(self.start_line, self.start_character, self.end_line, self.end_character)

    def to_lsp(self) -> dict[str, object]:
        """Render as an LSP ``Diagnostic`` object."""
        return {
            "source": self.source,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "range": {
                "start": {"line": self.start_line, "character": self.start_character},
                "end": {"line": self.end_line, "character": self.end_character},
            },
        }


@dataclass(frozen=True, slots=True)
class LspCodeAction:
    """A quick-fix attached to one diagnostic.

    ``is_preferred`` stays false: the author chooses. A preferred fix is the
    shape an editor applies without being asked, and these rewrites change
    control flow.
    """

    title: str
    diagnostic: LspDiagnostic
    edits: tuple[TextEdit, ...]
    kind: str = QUICKFIX
    is_preferred: bool = False

    def apply(self, source: str) -> str:
        """Return ``source`` with this action's edits applied."""
        return TextEdit.apply_all(source, self.edits)

    def to_lsp(self, uri: str) -> dict[str, object]:
        """Render as an LSP ``CodeAction`` for ``uri``."""
        return {
            "title": self.title,
            "kind": self.kind,
            "diagnostics": [self.diagnostic.to_lsp()],
            "edit": {"changes": {uri: [edit.to_lsp() for edit in self.edits]}},
            "isPreferred": self.is_preferred,
        }


class LitmusLsp:
    """Run the rule registry over an open buffer and offer mechanical fixes.

    Contract:
      - :meth:`diagnostics` reports every rule the CLI's ``--list-rules`` lists.
        A buffer that does not parse yields no diagnostics: syntax is another
        plugin's job, and a tree we cannot build is a tree we cannot check.
      - :meth:`code_actions` offers a quick-fix for ``litmus/gated-pass`` (wrap
        the return) and ``litmus/method-reference`` (lift the lambda). Other
        rules have no mechanical fix and stay diagnostic-only.
    """

    def __init__(self, registry: RuleRegistry | None = None) -> None:
        from litmus_check.fixes import GatedPassFix, MethodReferenceFix

        self._registry = registry if registry is not None else RuleRegistry.discover()
        self._fixes = (GatedPassFix(), MethodReferenceFix())

    def report(self, path: Path, source: str) -> Report:
        """Check ``source`` as if it were the file at ``path``."""
        try:
            module = SuiteModule(path, source)
        except SyntaxError as exc:
            return Report((), (f"{path}: {exc}",))
        violations: list[Violation] = []
        for rule in self._registry:
            violations.extend(rule.check(module))
        return Report(tuple(sorted(violations)), ())

    def diagnostics(self, path: Path, source: str) -> tuple[LspDiagnostic, ...]:
        """Every finding in ``source``, as LSP diagnostics."""
        report = self.report(path, source)
        return tuple(self._diagnostic(violation, source) for violation in report.violations)

    def code_actions(
        self,
        path: Path,
        source: str,
        selection: LspRange,
    ) -> tuple[LspCodeAction, ...]:
        """Quick-fixes whose diagnostic overlaps ``selection``."""
        try:
            module = SuiteModule(path, source)
        except SyntaxError:
            return ()
        actions: list[LspCodeAction] = []
        for diagnostic in self.diagnostics(path, source):
            if not diagnostic.range.overlaps(selection):
                continue
            for fix in self._fixes:
                if fix.code != diagnostic.code:
                    continue
                action = fix.offer(module, source, diagnostic)
                if action is not None:
                    actions.append(action)
        return tuple(actions)

    @staticmethod
    def _diagnostic(violation: Violation, source: str) -> LspDiagnostic:
        start_line = violation.line - 1
        start_character = max(violation.column - 1, 0)
        lines = source.split("\n")
        if 0 <= start_line < len(lines):
            end_character = len(lines[start_line].rstrip())
            if end_character <= start_character:
                end_character = min(len(lines[start_line]), start_character + 1)
        else:
            end_character = start_character + 1
        return LspDiagnostic(
            path=violation.path,
            code=violation.code,
            message=violation.message,
            start_line=start_line,
            start_character=start_character,
            end_line=start_line,
            end_character=end_character,
        )


def node_span(node: ast.AST) -> tuple[int, int, int, int] | None:
    """Return the 0-based exclusive span of ``node``, or ``None`` if it has none."""
    lineno = getattr(node, "lineno", None)
    col_offset = getattr(node, "col_offset", None)
    end_lineno = getattr(node, "end_lineno", None)
    end_col_offset = getattr(node, "end_col_offset", None)
    if lineno is None or col_offset is None or end_lineno is None or end_col_offset is None:
        return None
    return (lineno - 1, col_offset, end_lineno - 1, end_col_offset)


def node_at(tree: ast.AST, line: int, character: int, kind: type) -> ast.AST | None:
    """Return the ``kind`` node whose start matches the 0-based ``(line, character)``."""
    for node in ast.walk(tree):
        if not isinstance(node, kind):
            continue
        span = node_span(node)
        if span is None:
            continue
        if span[0] == line and span[1] == character:
            return node
    return None


def line_indent(source: str, line: int) -> str:
    """Leading whitespace of 0-based ``line``."""
    lines = source.split("\n")
    if not (0 <= line < len(lines)):
        return ""
    text = lines[line]
    return text[: len(text) - len(text.lstrip(" \t"))]


def inner_indent(indent: str) -> str:
    """One tab or four spaces deeper than ``indent``."""
    tab = "\t" if "\t" in indent else "    "
    return indent + tab


def ensure_testfailure_import(module: SuiteModule) -> TextEdit | None:
    """Insert ``TestFailure`` into a ``from litmus import`` if it is missing."""
    tree = module.tree
    last_import_end = 0
    for node in tree.body:
        is_import = isinstance(node, ast.Import | ast.ImportFrom)
        is_docstring = isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        if is_import or is_docstring:
            last_import_end = node.end_lineno or node.lineno
        else:
            break
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module != "litmus" or node.level:
            continue
        if any(alias.name == "TestFailure" for alias in node.names):
            return None
        if not node.names or node.names[0].name == "*":
            return None
        last = node.names[-1]
        span = node_span(last)
        if span is None:
            break
        return TextEdit(span[2], span[3], span[2], span[3], ", TestFailure")
    return TextEdit(
        last_import_end,
        0,
        last_import_end,
        0,
        "from litmus import TestFailure\n",
    )
