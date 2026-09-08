"""``python-lsp-server`` hooks over :class:`~litmus_check.lsp.LitmusLsp`.

Loaded only when the ``pylsp`` extra is installed. The adapter itself does not
depend on ``pylsp``; this module is the thin translation to hookimpl signatures.
"""

from __future__ import annotations

from pathlib import Path

from pylsp import hookimpl

from litmus_check.lsp import LitmusLsp, LspObject, LspRange

_PLUGIN_NAME = "litmus_check"
_adapter = LitmusLsp()


class PylspAdapter:
    """Translate pylsp hook arguments onto :class:`LitmusLsp`."""

    def __init__(self, adapter: LitmusLsp | None = None) -> None:
        self._adapter = adapter if adapter is not None else _adapter

    def settings(self) -> dict[str, object]:
        """Enable the plugin by default; authors opt out in pylsp config."""
        return {"plugins": {_PLUGIN_NAME: {"enabled": True}}}

    def lint(self, config: object, document: object) -> list[dict[str, object]]:
        """Publish every registry finding on the open document as a diagnostic."""
        if not self._enabled(config):
            return []
        path, source = self._document(document)
        return [diagnostic.to_lsp() for diagnostic in self._adapter.diagnostics(path, source)]

    def code_actions(
        self,
        config: object,
        document: object,
        selection: object,
        context: object,
    ) -> list[dict[str, object]]:
        """Quick-fixes for gated-pass and method-reference overlapping the selection."""
        if not self._enabled(config):
            return []
        if not self._wants_quickfix(context):
            return []
        path, source = self._document(document)
        uri = self._uri(document)
        try:
            lsp_range = LspRange.from_lsp(selection)
        except TypeError, KeyError, ValueError:
            return []
        return [
            action.to_lsp(uri) for action in self._adapter.code_actions(path, source, lsp_range)
        ]

    @staticmethod
    def _enabled(config: object) -> bool:
        plugin_settings = getattr(config, "plugin_settings", None)
        if not callable(plugin_settings):
            return True
        try:
            typed = LspObject.mapping(plugin_settings(_PLUGIN_NAME))
        except TypeError:
            return True
        return typed.get("enabled", True) is not False

    @staticmethod
    def _wants_quickfix(context: object) -> bool:
        try:
            typed = LspObject.mapping(context)
        except TypeError:
            return True
        only = typed.get("only")
        if only is None:
            return True
        if not isinstance(only, list):
            return True
        return "quickfix" in only

    @staticmethod
    def _document(document: object) -> tuple[Path, str]:
        raw_path = getattr(document, "path", None)
        if isinstance(raw_path, Path):
            path = raw_path
        elif isinstance(raw_path, str) and raw_path:
            path = Path(raw_path)
        else:
            path = Path("untitled.py")
        source = getattr(document, "source", "")
        if not isinstance(source, str):
            source = ""
        return path, source

    @staticmethod
    def _uri(document: object) -> str:
        uri = getattr(document, "uri", None)
        if isinstance(uri, str) and uri:
            return uri
        path, _ = PylspAdapter._document(document)
        resolved = path if path.is_absolute() else path.resolve()
        return resolved.as_uri()


_hooks = PylspAdapter()


@hookimpl
def pylsp_settings() -> dict[str, object]:
    """pylsp plugin settings hook."""
    return _hooks.settings()


@hookimpl
def pylsp_lint(
    config: object,
    workspace: object,
    document: object,
    is_saved: bool,
) -> list[dict[str, object]]:
    """pylsp lint hook: run the registry over the buffer."""
    del workspace, is_saved
    return _hooks.lint(config, document)


@hookimpl
def pylsp_code_actions(
    config: object,
    workspace: object,
    document: object,
    range: object,
    context: object,
) -> list[dict[str, object]]:
    """pylsp code-action hook: gated-pass wrap and method-reference lift."""
    del workspace
    return _hooks.code_actions(config, document, range, context)
