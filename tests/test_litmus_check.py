"""The ``litmus_check`` skeleton: suite model, registry, checker, and CLI contract."""

import ast
import io
import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from litmus_check import Rule, RuleRegistry, SuiteChecker, SuiteModule, Violation
from litmus_check.cli import EXIT_CLEAN, EXIT_ERROR, EXIT_VIOLATIONS, LitmusCheckCommand


class _ReturnPassRule(Rule):
    """Test-local rule: flag every ``return PASS`` so the plumbing has something to carry."""

    code = "test/return-pass"
    summary = "flags every return of PASS"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        for node in ast.walk(module.tree):
            is_pass = isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
            if is_pass and node.value.id == "PASS":  # type: ignore[union-attr]
                yield module.violation(node, self.code, "PASS returned")


class _ClassCountRule(Rule):
    code = "test/class-count"
    summary = "flags every class"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ClassDef):
                yield module.violation(node, self.code, node.name)


def _module(source: str) -> SuiteModule:
    return SuiteModule(Path("suite.py"), source)


class TestSuiteModule:
    def test_bare_attribute_and_transitive_bases_are_litmus_classes(self) -> None:
        module = _module(
            "import litmus\n"
            "from litmus import LitmusTest\n"
            "class Direct(LitmusTest): ...\n"
            "class Qualified(litmus.LitmusTest): ...\n"
            "class Derived(Direct): ...\n"
            "class Twice(Derived): ...\n"
            "class Plain: ...\n"
            "class Other(object): ...\n"
        )
        names = sorted(node.name for node in module.litmus_classes())

        assert names == ["Derived", "Direct", "Qualified", "Twice"]

    def test_nested_classes_inside_functions_are_found(self) -> None:
        module = _module(
            "from litmus import LitmusTest\n"
            "def factory():\n"
            "    class Inner(LitmusTest): ...\n"
            "    return Inner\n"
        )

        assert [node.name for node in module.litmus_classes()] == ["Inner"]

    def test_structural_queries_walk_upward(self) -> None:
        module = _module(
            "from litmus import LitmusTest\n"
            "class Suite(LitmusTest):\n"
            "    def method(self):\n"
            "        def inner():\n"
            "            return 1\n"
            "        return inner\n"
        )
        returns = [node for node in ast.walk(module.tree) if isinstance(node, ast.Return)]
        innermost = min(returns, key=lambda node: node.lineno == 6)

        function = module.enclosing_function(innermost)
        method = module.enclosing_method(innermost)
        klass = module.enclosing_class(innermost)

        assert isinstance(function, ast.FunctionDef)
        assert function.name == "inner"
        assert method is not None
        assert method.name == "method"
        assert klass is not None
        assert klass.name == "Suite"
        assert module.parent_of(module.tree) is None

    def test_violation_uses_one_based_columns(self) -> None:
        module = _module("x = 1\n")
        assign = module.tree.body[0]

        violation = module.violation(assign, "c", "m")

        assert violation == Violation(Path("suite.py"), 1, 1, "c", "m")
        assert violation.render() == "suite.py:1:1: c m"


class TestRuleRegistry:
    def test_orders_by_code_and_rejects_duplicates(self) -> None:
        registry = RuleRegistry([_ReturnPassRule(), _ClassCountRule()])

        assert registry.codes() == ("test/class-count", "test/return-pass")
        assert len(registry) == 2
        with pytest.raises(ValueError, match="duplicate rule code"):
            RuleRegistry([_ClassCountRule(), _ClassCountRule()])

    def test_select_and_ignore_narrow_by_code(self) -> None:
        registry = RuleRegistry([_ReturnPassRule(), _ClassCountRule()])

        assert registry.select(["test/return-pass"]).codes() == ("test/return-pass",)
        assert registry.ignore(["test/return-pass"]).codes() == ("test/class-count",)

    def test_unknown_codes_fail_loudly(self) -> None:
        registry = RuleRegistry([_ReturnPassRule()])

        with pytest.raises(KeyError, match="unknown rule code"):
            registry.select(["test/typo"])
        with pytest.raises(KeyError, match="unknown rule code"):
            registry.ignore(["test/typo"])

    def test_discover_loads_only_concrete_rules_with_unique_codes(self) -> None:
        registry = RuleRegistry.discover()

        codes = registry.codes()
        assert len(set(codes)) == len(codes)
        assert all(isinstance(rule, Rule) for rule in registry)
        assert all(rule.summary for rule in registry)


class TestSuiteChecker:
    def test_walks_directories_and_skips_caches_and_dot_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("def f():\n    return PASS\n")
        nested = tmp_path / "pkg"
        nested.mkdir()
        (nested / "b.py").write_text("def g():\n    return PASS\n")
        (nested / "__pycache__").mkdir()
        (nested / "__pycache__" / "c.py").write_text("def h():\n    return PASS\n")
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "d.py").write_text("def i():\n    return PASS\n")
        (tmp_path / "notes.txt").write_text("return PASS\n")

        report = SuiteChecker(RuleRegistry([_ReturnPassRule()])).check([tmp_path])

        assert report.errors == ()
        assert [v.path.name for v in report.violations] == ["a.py", "b.py"]
        assert not report.clean

    def test_violations_are_sorted_by_location(self, tmp_path: Path) -> None:
        (tmp_path / "z.py").write_text("class B: ...\nclass A: ...\n")
        (tmp_path / "a.py").write_text("class C: ...\n")

        report = SuiteChecker(RuleRegistry([_ClassCountRule()])).check([tmp_path])

        assert [(v.path.name, v.line, v.message) for v in report.violations] == [
            ("a.py", 1, "C"),
            ("z.py", 1, "B"),
            ("z.py", 2, "A"),
        ]

    def test_unparseable_and_missing_paths_are_errors_not_silence(self, tmp_path: Path) -> None:
        (tmp_path / "broken.py").write_text("def (:\n")

        report = SuiteChecker(RuleRegistry([])).check([tmp_path, tmp_path / "missing"])

        assert report.violations == ()
        assert len(report.errors) == 2
        assert report.errors[0].startswith(str(tmp_path / "broken.py"))
        assert "no such file" in report.errors[1]
        assert not report.clean

    def test_clean_report(self, tmp_path: Path) -> None:
        (tmp_path / "ok.py").write_text("def f():\n    return 1\n")

        assert SuiteChecker(RuleRegistry([_ReturnPassRule()])).check([tmp_path]).clean


class TestLitmusCheckCommand:
    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        status = LitmusCheckCommand(stdout=out, stderr=err).run(argv)
        return status, out.getvalue(), err.getvalue()

    def test_no_paths_is_a_usage_error(self) -> None:
        status, _, err = self._run()

        assert status == EXIT_ERROR
        assert "at least one path is required" in err

    def test_list_rules_prints_catalogue_and_exits_clean(self) -> None:
        status, out, _ = self._run("--list-rules")

        assert status == EXIT_CLEAN
        assert out == "".join(f"{r.code}\t{r.summary}\n" for r in RuleRegistry.discover())

    def test_unknown_rule_code_is_an_error(self, tmp_path: Path) -> None:
        status, _, err = self._run("--select", "litmus/no-such-rule", str(tmp_path))

        assert status == EXIT_ERROR
        assert "unknown rule code" in err

    def test_clean_directory_exits_zero(self, tmp_path: Path) -> None:
        (tmp_path / "ok.py").write_text("x = 1\n")

        status, out, err = self._run(str(tmp_path))

        assert (status, err) == (EXIT_CLEAN, "")
        assert out == "Found 0 violations.\n"

    def test_missing_path_exits_two(self, tmp_path: Path) -> None:
        status, _, err = self._run(str(tmp_path / "gone"))

        assert status == EXIT_ERROR
        assert "no such file or directory" in err

    def test_syntax_error_exits_two_in_json_too(self, tmp_path: Path) -> None:
        (tmp_path / "broken.py").write_text("def (:\n")

        status, out, _ = self._run("--format", "json", str(tmp_path))

        assert status == EXIT_ERROR
        payload = json.loads(out)
        assert payload["violations"] == []
        assert len(payload["errors"]) == 1

    def test_exit_codes_are_importable_constants(self) -> None:
        assert (EXIT_CLEAN, EXIT_VIOLATIONS, EXIT_ERROR) == (0, 1, 2)
