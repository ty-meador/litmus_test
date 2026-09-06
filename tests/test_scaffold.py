"""``litmus new``: the gated-PASS template, the files it writes, and example shape.

The generator is the single source for the scaffolding shape (issue #79). Teaching
examples keep their own details; each ``LitmusTest`` subclass in ``examples/`` still
has an annotated ``__init__``, a ``build_suite``, and a gated ``return PASS``.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from litmus import State
from litmus.cli import EXIT_PASS, EXIT_USAGE, LitmusCommand
from litmus.scaffold import SuiteScaffolder
from litmus_check.cli import EXIT_CLEAN, LitmusCheckCommand

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
_REQUIRED_CORE = frozenset({"__init__", "build_suite"})
_REQUIRED_FULL = frozenset(SuiteScaffolder.required_suite_methods())


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = LitmusCommand(stdout=out, stderr=err).run(argv)
    return status, out.getvalue(), err.getvalue()


def _generated(tmp_path: Path, class_name: str = "WidgetSuite") -> tuple[Path, Path]:
    status, out, err = _run("new", class_name, "--directory", str(tmp_path))
    assert status == 0, err
    stem = SuiteScaffolder.module_stem(class_name)
    suite = tmp_path / f"{stem}.py"
    test = tmp_path / f"test_{stem}.py"
    assert suite.is_file(), out
    assert test.is_file(), out
    return suite, test


class TestLitmusNewWritesTheTemplate:
    def test_default_cwd_write_prints_both_paths(self, tmp_path: Path) -> None:
        # conftest chdirs into tmp_path, so the default directory is this test's tmp.
        status, out, err = _run("new", "WidgetSuite")

        assert status == 0
        assert err == ""
        assert "wrote " in out
        assert (tmp_path / "widget_suite.py").is_file()
        assert (tmp_path / "test_widget_suite.py").is_file()

    def test_directory_flag_writes_into_that_folder(self, tmp_path: Path) -> None:
        dest = tmp_path / "out"
        dest.mkdir()

        status, _, err = _run("new", "PowerRail", "--directory", str(dest))

        assert status == 0, err
        assert (dest / "power_rail.py").is_file()
        assert (dest / "test_power_rail.py").is_file()

    def test_existing_files_are_a_usage_error_without_force(self, tmp_path: Path) -> None:
        _generated(tmp_path)

        status, _, err = _run("new", "WidgetSuite", "--directory", str(tmp_path))

        assert status == EXIT_USAGE
        assert "already exist" in err
        assert "--force" in err

    def test_force_overwrites(self, tmp_path: Path) -> None:
        suite, _ = _generated(tmp_path)
        suite.write_text("stale\n", encoding="utf-8")

        status, _, err = _run("new", "WidgetSuite", "--directory", str(tmp_path), "--force")

        assert status == 0, err
        assert "class WidgetSuite" in suite.read_text(encoding="utf-8")

    def test_lowercase_name_is_a_usage_error(self) -> None:
        status, _, err = _run("new", "widgetSuite")

        assert status == EXIT_USAGE
        assert "not a suite class name" in err
        assert "WidgetSuite" in err

    def test_keyword_name_is_a_usage_error(self) -> None:
        status, _, err = _run("new", "False")

        assert status == EXIT_USAGE
        assert "not a suite class name" in err

    def test_missing_directory_is_a_usage_error(self, tmp_path: Path) -> None:
        status, _, err = _run("new", "WidgetSuite", "--directory", str(tmp_path / "absent"))

        assert status == EXIT_USAGE
        assert "not an existing directory" in err

    def test_new_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            _run("new", "--help")

        assert exit_info.value.code == 0
        captured = capsys.readouterr()
        assert "SUITE" in captured.out
        assert "--directory" in captured.out
        assert "--force" in captured.out


class TestGeneratedSuiteClearsTheGates:
    def test_generated_suite_passes_litmus_check(self, tmp_path: Path) -> None:
        suite, _ = _generated(tmp_path)
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run([str(suite)])

        assert status == EXIT_CLEAN, out.getvalue()

    def test_generated_suite_passes_ruff(self, tmp_path: Path) -> None:
        suite, test = _generated(tmp_path)

        check = subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(suite), str(test)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        fmt = subprocess.run(
            [sys.executable, "-m", "ruff", "format", "--check", str(suite), str(test)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        assert check.returncode == 0, check.stdout + check.stderr
        assert fmt.returncode == 0, fmt.stdout + fmt.stderr

    def test_generated_suite_passes_pyright_strict(self, tmp_path: Path) -> None:
        _generated(tmp_path)
        config = tmp_path / "pyrightconfig.json"
        config.write_text(
            json.dumps(
                {
                    "include": ["."],
                    "exclude": ["pyrightconfig.json"],
                    "extraPaths": [str(ROOT / "src")],
                    "typeCheckingMode": "strict",
                    "pythonVersion": "3.14",
                    "venvPath": str(ROOT),
                    "venv": ".venv",
                }
            ),
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, "-m", "pyright", "--project", str(tmp_path)],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stdout + result.stderr

    def test_generated_suite_runs_pass_under_litmus_run(self, tmp_path: Path) -> None:
        suite, _ = _generated(tmp_path)

        status, _, err = _run("run", str(suite))

        assert status == EXIT_PASS, err
        assert "Pass" in err

    def test_generated_self_test_passes(self, tmp_path: Path) -> None:
        _, test = _generated(tmp_path)

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(test)],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "LITMUS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
                "PYTHONPATH": str(ROOT / "src"),
            },
        )

        assert result.returncode == 0, result.stdout + result.stderr

    def test_empty_target_fails_configure_as_not_testable(self, tmp_path: Path) -> None:
        suite, _ = _generated(tmp_path)
        spec = importlib.util.spec_from_file_location("widget_suite", suite)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        instance = module.WidgetSuite(name="WidgetSuite", target="")

        result = asyncio.run(instance.run())

        assert result.state is State.FAIL
        assert result.failure is not None
        assert "did not reach a testable state" in str(result.failure)


class TestTemplateIsTheSingleSource:
    def test_generated_suite_defines_the_full_shape(self, tmp_path: Path) -> None:
        suite, _ = _generated(tmp_path)
        tree = ast.parse(suite.read_text(encoding="utf-8"))
        methods = _suite_methods(tree)

        assert methods >= _REQUIRED_FULL
        assert _has_gated_pass(tree)

    def test_every_example_suite_matches_the_core_shape(self) -> None:
        missing: list[str] = []
        for path in sorted(EXAMPLES.glob("*.py")):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for class_name, methods, gated in _example_suites(tree):
                if not methods >= _REQUIRED_CORE:
                    missing.append(f"{path.name}:{class_name} methods={sorted(methods)}")
                if not gated:
                    missing.append(f"{path.name}:{class_name} missing gated PASS")
                if not _init_is_annotated(tree, class_name):
                    missing.append(f"{path.name}:{class_name} __init__ is not fully annotated")
        assert missing == []

    def test_scaffolder_source_matches_what_new_writes(self, tmp_path: Path) -> None:
        suite, test = _generated(tmp_path, "AmpereRail")
        scaffolder = SuiteScaffolder()

        assert suite.read_text(encoding="utf-8") == scaffolder.suite_source(
            "AmpereRail", "ampere_rail"
        )
        assert test.read_text(encoding="utf-8") == scaffolder.test_source(
            "AmpereRail", "ampere_rail"
        )


def _suite_methods(tree: ast.Module) -> set[str]:
    methods: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and _extends_litmus_test(node):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    methods.add(item.name)
    return methods


def _example_suites(tree: ast.Module) -> list[tuple[str, set[str], bool]]:
    found: list[tuple[str, set[str], bool]] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not _extends_litmus_test(node):
            continue
        methods = {
            item.name
            for item in node.body
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        found.append((node.name, methods, _class_has_gated_pass(node)))
    return found


def _extends_litmus_test(node: ast.ClassDef) -> bool:
    return any(isinstance(base, ast.Name) and base.id == "LitmusTest" for base in node.bases)


def _has_gated_pass(tree: ast.Module) -> bool:
    return any(
        _class_has_gated_pass(node)
        for node in tree.body
        if isinstance(node, ast.ClassDef) and _extends_litmus_test(node)
    )


def _class_has_gated_pass(node: ast.ClassDef) -> bool:
    for item in node.body:
        if not isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for child in ast.walk(item):
            if not isinstance(child, ast.Return) or not _returns_pass(child):
                continue
            if _return_is_gated(item, child):
                return True
    return False


def _return_is_gated(function: ast.FunctionDef | ast.AsyncFunctionDef, node: ast.Return) -> bool:
    for parent in ast.walk(function):
        if isinstance(parent, ast.If) and _contains(parent, node):
            return True
        if isinstance(parent, ast.match_case) and _contains(parent, node):
            return True
    return False


def _contains(root: ast.AST, target: ast.AST) -> bool:
    return any(child is target for child in ast.walk(root))


def _returns_pass(node: ast.Return) -> bool:
    value = node.value
    if isinstance(value, ast.Call):
        # ``return PASS({...})`` carries evidence and is still a gated PASS.
        value = value.func
    if isinstance(value, ast.Name):
        return value.id == "PASS"
    return isinstance(value, ast.Attribute) and value.attr == "PASS"


def _init_is_annotated(tree: ast.Module, class_name: str) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef) or item.name != "__init__":
                continue
            if item.returns is None:
                return False
            for arg in (*item.args.posonlyargs, *item.args.args, *item.args.kwonlyargs):
                if arg.arg == "self":
                    continue
                if arg.annotation is None:
                    return False
            return True
    return False
