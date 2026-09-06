"""``litmus run``: discovery, selection, construction, exit codes, and the records.

The exit status is the station contract (``docs/litmus-run.md``): 0 Pass, 1 Fail,
2 Aborted, 3 usage error. Every path that can end in 3 is pinned here with the
message the developer reads, so the runner tells them the fix rather than a traceback.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from textwrap import dedent
from xml.etree.ElementTree import fromstring

import pytest

from litmus.cli import (
    EXIT_ABORTED,
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_USAGE,
    LitmusCommand,
    SuiteLoader,
    UsageError,
    main,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

_PASSING = '''
from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict


class Passing(LitmusTest):
    """One claim that holds."""

    def build_suite(self) -> LitmusTest:
        return self.test("claim", self._claim)

    async def _claim(self, utils: TestUtils) -> Verdict:
        utils.logger.info("claim observed")
        if utils.env is not None:
            return PASS
        raise TestFailure("no environment", "a mapping", "(none)")
'''

_FAILING = '''
from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict


class Failing(LitmusTest):
    """One claim that does not hold."""

    def build_suite(self) -> LitmusTest:
        return self.test("claim", self._claim)

    async def _claim(self, utils: TestUtils) -> Verdict:
        if utils.env is None:
            return PASS
        raise TestFailure("rail low", ">= 11.5 V", "3.2 V")
'''

_ABORTING = '''
import asyncio

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict


class Aborting(LitmusTest):
    """A root budget that expires before the claim can settle."""

    def __init__(self, *, name: str) -> None:
        super().__init__(name=name, abort_after=0.05)

    def build_suite(self) -> LitmusTest:
        return self.test("claim", self._claim)

    async def _claim(self, utils: TestUtils) -> Verdict:
        await asyncio.sleep(30)
        if utils.env is not None:
            return PASS
        raise TestFailure("unreachable", "", "")
'''

_TWO_SUITES = '''
from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict


class First(LitmusTest):
    """The first of two."""

    def build_suite(self) -> LitmusTest:
        return self.test("claim", self._claim)

    async def _claim(self, utils: TestUtils) -> Verdict:
        if utils.env is not None:
            return PASS
        raise TestFailure("no environment", "a mapping", "(none)")


class Second(First):
    """The second of two."""
'''

_NO_SUITES = '''
class NotASuite:
    """A module with nothing to run."""
'''

_REQUIRES_ARGUMENTS = '''
from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict


class NeedsHost(LitmusTest):
    """A constructor the runner cannot satisfy from the name alone."""

    def __init__(self, *, name: str, host: str) -> None:
        super().__init__(name=name)
        self._host = host

    def build_suite(self) -> LitmusTest:
        return self.test("claim", self._claim)

    async def _claim(self, utils: TestUtils) -> Verdict:
        if self._host:
            return PASS
        raise TestFailure("no host", "a host", "(empty)")
'''

_IMPORTS_A_SUITE = """
from examples.hello_world import HelloWorld

__all__ = ["HelloWorld"]
"""


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(dedent(source), encoding="utf-8")
    return path


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = LitmusCommand(stdout=out, stderr=err).run(argv)
    return status, out.getvalue(), err.getvalue()


class TestExitStatus:
    """The three terminal states map onto three exit codes; nothing else does."""

    def test_pass_exits_zero_with_the_verdict_on_stderr(self, tmp_path: Path) -> None:
        suite = _write(tmp_path, "passing.py", _PASSING)

        status, out, err = _run("run", str(suite))

        assert status == EXIT_PASS
        assert out == ""
        assert "litmus run: Passing: Pass" in err

    def test_fail_exits_one_and_names_the_failure(self, tmp_path: Path) -> None:
        suite = _write(tmp_path, "failing.py", _FAILING)

        status, _, err = _run("run", str(suite))

        assert status == EXIT_FAIL
        assert "litmus run: Failing: Fail (TestFailure:" in err
        assert "rail low" in err

    def test_aborted_exits_two(self, tmp_path: Path) -> None:
        suite = _write(tmp_path, "aborting.py", _ABORTING)

        status, _, err = _run("run", str(suite))

        assert status == EXIT_ABORTED
        assert "litmus run: Aborting: Aborted" in err

    def test_exit_codes_are_distinct_and_usage_is_highest(self) -> None:
        assert (EXIT_PASS, EXIT_FAIL, EXIT_ABORTED, EXIT_USAGE) == (0, 1, 2, 3)

    def test_main_raises_system_exit_with_the_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        suite = _write(tmp_path, "failing.py", _FAILING)
        monkeypatch.setattr("sys.argv", ["litmus", "run", str(suite)])

        with pytest.raises(SystemExit) as exit_info:
            main()

        assert exit_info.value.code == EXIT_FAIL
        assert "litmus run: Failing: Fail" in capsys.readouterr().err


class TestUsageErrors:
    """Every way a run cannot start is exit 3 with the fix in the message."""

    def test_no_command_is_a_usage_error(self) -> None:
        status, _, err = _run()

        assert status == EXIT_USAGE
        assert "litmus: error:" in err
        assert "command" in err

    def test_argparse_never_exits_with_two(self) -> None:
        status, _, err = _run("run")

        assert status == EXIT_USAGE
        assert "TARGET" in err

    def test_help_exits_zero_through_argparse(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            _run("run", "--help")

        assert exit_info.value.code == 0
        assert "--artifact-root" in capsys.readouterr().out

    def test_missing_file(self, tmp_path: Path) -> None:
        status, _, err = _run("run", str(tmp_path / "absent.py"))

        assert status == EXIT_USAGE
        assert "absent.py: no such file" in err

    def test_file_that_is_not_python(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "notes.txt", "not a suite\n")

        status, _, err = _run("run", str(path))

        assert status == EXIT_USAGE
        assert "not an importable Python file" in err

    def test_file_that_fails_to_import_names_the_exception(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "broken.py", "class Broken(\n")

        status, _, err = _run("run", str(path))

        assert status == EXIT_USAGE
        assert "broken.py: SyntaxError:" in err

    def test_unknown_module_name(self) -> None:
        status, _, err = _run("run", "no_such_module_anywhere")

        assert status == EXIT_USAGE
        assert "no_such_module_anywhere" in err
        assert "importable module name" in err

    def test_module_that_fails_to_import_names_the_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "explodes_on_import.py", "raise RuntimeError('boom at import')\n")
        monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])

        status, _, err = _run("run", "explodes_on_import")

        assert status == EXIT_USAGE
        assert "explodes_on_import: RuntimeError: boom at import" in err

    def test_empty_source_before_selector(self) -> None:
        status, _, err = _run("run", "::Passing")

        assert status == EXIT_USAGE
        assert "must precede '::'" in err

    def test_module_with_no_suite(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "empty.py", _NO_SUITES)

        status, _, err = _run("run", str(path))

        assert status == EXIT_USAGE
        assert "defines no LitmusTest subclass" in err

    def test_module_with_two_suites_needs_a_selector(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "pair.py", _TWO_SUITES)

        status, _, err = _run("run", str(path))

        assert status == EXIT_USAGE
        assert "defines 2 suites (First, Second)" in err
        assert f"{path}::SuiteClass" in err

    def test_selector_naming_an_absent_class(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "pair.py", _TWO_SUITES)

        status, _, err = _run("run", f"{path}::Third")

        assert status == EXIT_USAGE
        assert "no LitmusTest subclass named 'Third'; name one of: First, Second" in err

    def test_suite_needing_constructor_arguments(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "needs_host.py", _REQUIRES_ARGUMENTS)

        status, _, err = _run("run", str(path))

        assert status == EXIT_USAGE
        assert "NeedsHost cannot be constructed from its name alone" in err
        assert "give every other __init__ parameter a default" in err

    def test_contract_violation_at_construction_is_a_usage_error(self, tmp_path: Path) -> None:
        source = _PASSING.replace(
            "class Passing(LitmusTest):",
            "class Passing(LitmusTest):\n"
            "    def __init__(self, *, name: str) -> None:\n"
            "        super().__init__(name=name, fail_after=1, abort_after=1)\n",
        )
        path = _write(tmp_path, "double_budget.py", source)

        status, _, err = _run("run", str(path))

        assert status == EXIT_USAGE
        assert "litmus run: error: Passing:" in err


class TestSelection:
    """Which class runs, from the file or module and the optional ``::SuiteClass``."""

    def test_selector_picks_one_of_several(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "pair.py", _TWO_SUITES)

        status, _, err = _run("run", f"{path}::Second")

        assert status == EXIT_PASS
        assert "litmus run: Second: Pass" in err

    def test_dotted_module_name_is_imported(self) -> None:
        status, _, err = _run("run", "examples.hello_world")

        assert status == EXIT_PASS
        assert "litmus run: HelloWorld: Pass" in err

    def test_imported_suite_counts_only_when_named(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "reexport.py", _IMPORTS_A_SUITE)

        implicit, _, err = _run("run", str(path))
        explicit, _, _ = _run("run", f"{path}::HelloWorld")

        assert implicit == EXIT_USAGE
        assert "defines no LitmusTest subclass" in err
        assert explicit == EXIT_PASS

    def test_a_file_stem_already_in_sys_modules_is_not_displaced(self, tmp_path: Path) -> None:
        # ``json`` is imported by this test module; a suite file named json.py must
        # load under another name and leave the standard library alone.
        path = _write(tmp_path, "json.py", _PASSING)

        suite_class = SuiteLoader().load(str(path))

        assert suite_class.__name__ == "Passing"
        assert suite_class.__module__ == "json_1"
        assert json.dumps({"still": "json"}) == '{"still": "json"}'

    def test_loader_raises_usage_error_directly(self, tmp_path: Path) -> None:
        with pytest.raises(UsageError, match="no such file"):
            SuiteLoader().load(str(tmp_path / "absent.py"))


class TestRecords:
    """``--json``, ``--junit``, and ``--artifact-root`` publish the run's evidence."""

    def test_json_prints_the_format_version_one_record_to_stdout(self, tmp_path: Path) -> None:
        suite = _write(tmp_path, "passing.py", _PASSING)

        status, out, err = _run("run", "--json", str(suite))
        record = json.loads(out)

        assert status == EXIT_PASS
        assert record["format_version"] == 1
        assert record["test_case"] == "Passing"
        assert record["summary_tree"]["name"] == "Passing"
        assert record["state"] == "Pass"
        assert "litmus run: Passing: Pass" in err

    def test_junit_writes_a_report(self, tmp_path: Path) -> None:
        suite = _write(tmp_path, "failing.py", _FAILING)
        report = tmp_path / "out" / "report.xml"
        report.parent.mkdir()

        status, _, err = _run("run", "--junit", str(report), str(suite))
        text = report.read_text(encoding="utf-8")
        parsed = fromstring(text.split("\n", 1)[1])

        assert status == EXIT_FAIL
        assert parsed.tag == "testsuites"
        assert parsed.get("name") == "Failing"
        assert f"junit report written to {report}" in err

    def test_junit_path_that_cannot_be_written_keeps_the_verdict_status(
        self, tmp_path: Path
    ) -> None:
        # A traceback would exit 1, which the contract reads as Fail; the run settled
        # Pass and the status says so, with the write failure on stderr.
        suite = _write(tmp_path, "passing.py", _PASSING)
        report = tmp_path / "missing-directory" / "report.xml"

        status, out, err = _run("run", "--junit", str(report), str(suite))

        assert status == EXIT_PASS
        assert out == ""
        assert not report.exists()
        assert "litmus run: Passing: Pass" in err
        assert f"litmus run: error: junit report not written to {report}" in err

    def test_artifact_root_overrides_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        suite = _write(tmp_path, "passing.py", _PASSING)
        environment_root = tmp_path / "from-environment"
        override_root = tmp_path / "from-flag"
        monkeypatch.setenv("LITMUS_ARTIFACT_ROOT", str(environment_root))

        status, _, _ = _run("run", "--artifact-root", str(override_root), str(suite))

        assert status == EXIT_PASS
        assert list(override_root.rglob("result.json"))
        assert not environment_root.exists()

    def test_without_the_flag_the_environment_root_is_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        suite = _write(tmp_path, "passing.py", _PASSING)
        environment_root = tmp_path / "from-environment"
        monkeypatch.setenv("LITMUS_ARTIFACT_ROOT", str(environment_root))

        status, _, _ = _run("run", str(suite))

        assert status == EXIT_PASS
        assert list(environment_root.rglob("result.json"))


_SSH_TARGET_VARS = (
    "SSH_HOST",
    "SSH_USER",
    "SSH_PASSWORD",
    "SSH_PORT",
    "JUMP_HOST",
    "JUMP_USER",
    "JUMP_PASSWORD",
    "JUMP_PORT",
    "TARGET_HOST",
    "TARGET_USER",
    "TARGET_PASSWORD",
    "TARGET_PORT",
    "TARGET_ADDRESS",
    "DUT_MAC",
    "GATEWAY_HOST",
    "GATEWAY_USER",
    "GATEWAY_PASSWORD",
    "GATEWAY_PORT",
)

_README_LITMUS_RUN_TARGETS = (
    "examples/hello_world.py",
    "examples/test_composition.py::SequencedDevice",
    "examples/test_composition.py::ParallelDevice",
    "examples/wait_for_patterns.py::ConfigureGatedDevice",
    "examples/wait_for_patterns.py::MainAssertionDevice",
    "examples/hardware_device_lifecycle.py",
    "examples/run_data_evidence.py",
    "examples/remote_device_ssh.py",
    "examples/ssh_tunneling.py",
    "examples/networked_device.py::DutDevice",
    "examples/pi_hole_server.py",
)


class TestExamples:
    """Every example runs under ``litmus run`` as the README says."""

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ("hello_world.py", EXIT_PASS),
            ("test_composition.py::SequencedDevice", EXIT_PASS),
            ("test_composition.py::ParallelDevice", EXIT_FAIL),
            ("wait_for_patterns.py::ConfigureGatedDevice", EXIT_PASS),
            ("wait_for_patterns.py::MainAssertionDevice", EXIT_PASS),
            ("hardware_device_lifecycle.py", EXIT_PASS),
            ("run_data_evidence.py", EXIT_PASS),
        ],
    )
    def test_example_exit_status(self, target: str, expected: int) -> None:
        file_name, _, suite_class = target.partition("::")
        spec = str(EXAMPLES / file_name)
        if suite_class:
            spec = f"{spec}::{suite_class}"

        status, _, _ = _run("run", spec)

        assert status == expected

    @pytest.mark.parametrize(
        ("target", "missing"),
        [
            ("remote_device_ssh.py", "SSH_HOST"),
            ("ssh_tunneling.py", "JUMP_HOST"),
            ("networked_device.py::DutDevice", "DUT_MAC"),
            ("pi_hole_server.py", "TARGET_ADDRESS"),
        ],
    )
    def test_ssh_example_without_target_fails_in_configure(
        self,
        target: str,
        missing: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for name in _SSH_TARGET_VARS:
            monkeypatch.delenv(name, raising=False)
        file_name, _, suite_class = target.partition("::")
        spec = str(EXAMPLES / file_name)
        if suite_class:
            spec = f"{spec}::{suite_class}"

        status, _, err = _run("run", spec, "--artifact-root", str(tmp_path))

        assert status == EXIT_FAIL
        assert "TestFailure" in err
        assert "did not reach a testable state" in err
        assert missing in err
        assert "cannot be constructed from its name alone" not in err

    def test_readme_runs_every_local_example_through_litmus_run(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for target in _README_LITMUS_RUN_TARGETS:
            assert f"uv run litmus run {target}" in readme, target
        assert "uv run python examples/" not in readme
