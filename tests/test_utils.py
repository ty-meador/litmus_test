"""Per-node TestUtils ownership and scoped evidence logging."""

import ast
import asyncio
import gc
import inspect
import shlex
import sys
import time
import tracemalloc
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

import pytest
from tests.mock_connector import ConnectorInstaller

import litmus
from litmus import PASS, ExecResult, LitmusTest, RunResult, TestFailure, TestUtils, Verdict
from litmus.lifecycle import Outcome, State
from litmus.mock import MockTransport
from litmus.remote import RemoteInterface
from litmus.routing import ExecRouter, RouterRunParams


class _ScopedLogRecord(Protocol):
    litmus_node: str
    litmus_scope: tuple[str, ...]
    measurement: int


class _Endpoint(RemoteInterface):
    def __init__(self, host: str) -> None:
        super().__init__()
        self.host = host

    async def _connect(self) -> None:
        pass

    async def _disconnect(self) -> None:
        pass

    async def _exec(self, command: str) -> ExecResult:
        return ExecResult.settled(command, "", 0)


class TestExecRoute:
    """`exec_route` is derived from the router each time it is read, never stored."""

    def test_a_node_that_never_declared_a_route_is_local(self) -> None:
        assert TestUtils().exec_route == "local"

    def test_a_routed_node_names_the_endpoint_of_its_hop(self) -> None:
        utils = TestUtils()
        edge = _Endpoint("10.10.10.10")
        utils._routing.apply_run_params(  # pyright: ignore[reportPrivateUsage]
            RouterRunParams(ExecRouter.for_ssh(edge))
        )
        assert utils.exec_route == "10.10.10.10"

    def test_a_route_forced_local_answers_local(self) -> None:
        async def on_host(command: str) -> ExecResult:
            return ExecResult.settled(command, "", 0)

        utils = TestUtils()
        utils._routing.apply_run_params(  # pyright: ignore[reportPrivateUsage]
            RouterRunParams(ExecRouter.local(on_host))
        )
        assert utils.exec_route == "local"


class TestTestUtilsPublicSurface:
    def test_test_utils_is_the_exported_lifecycle_surface(self) -> None:
        assert litmus.TestUtils is TestUtils
        assert "TestUtils" in litmus.__all__
        assert litmus.ExecResult is ExecResult
        assert "ExecResult" in litmus.__all__
        assert not hasattr(litmus, "LocalExec")

        utils = TestUtils()

        assert callable(utils.exec)
        assert callable(utils.wait_for)
        assert callable(utils.get_arp_resolver)
        assert isinstance(utils.ctx, dict)
        assert utils.logger.scope == ()
        assert not utils.signal.fired


class TestTestUtilsInjection:
    async def test_run_constructs_the_root_utility_seed(self) -> None:
        received: list[TestUtils] = []

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                received.append(utils)
                return PASS

        outcome = await Suite(name="rack").run(env={"STATION": "west"})

        assert outcome.state is State.PASS
        assert received[0].env["STATION"] == "west"
        assert received[0].logger.scope == ("rack", "claim")

    async def test_every_node_receives_its_own_scoped_utils(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        seen: dict[str, TestUtils] = {}

        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                seen["configure"] = utils
                return {"serial": "SN-0042"}

            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    "claims",
                    [
                        self.test("first", self._first),
                        self.test("second", self._second),
                    ],
                )

            async def _first(self, utils: TestUtils) -> Verdict:
                seen["first"] = utils
                utils.logger.info("first observation", extra={"measurement": 1})
                return PASS

            async def _second(self, utils: TestUtils) -> Verdict:
                seen["second"] = utils
                utils.logger.info("second observation")
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                seen["cleanup"] = utils

        source_env = {"STATION": "west"}
        seed = TestUtils(ctx={"injected": True}, env=source_env)
        source_env["STATION"] = "changed after snapshot"

        with caplog.at_level("INFO", logger="litmus"):
            outcome = await Suite(name="rack").main(seed)

        assert outcome.state is State.PASS
        assert seen["configure"] is seen["cleanup"]
        assert len({id(utils) for utils in seen.values()}) == 3
        assert all(utils.ctx is seed.ctx for utils in seen.values())
        assert seed.ctx == {"injected": True, "serial": "SN-0042"}
        assert seen["configure"].logger.scope == ("rack",)
        assert seen["first"].logger.scope == ("rack", "claims", "first")
        assert seen["second"].logger.scope == ("rack", "claims", "second")
        assert all(utils.env["STATION"] == "west" for utils in seen.values())
        assert all(not utils.signal.fired for utils in seen.values())

        records = [
            cast("_ScopedLogRecord", record)
            for record in caplog.records
            if "observation" in record.message
        ]
        assert [record.litmus_node for record in records] == ["first", "second"]
        assert [record.litmus_scope for record in records] == [
            ("rack", "claims", "first"),
            ("rack", "claims", "second"),
        ]
        assert records[0].measurement == 1

        with pytest.raises(TypeError):
            cast("dict[str, str]", seen["configure"].env)["STATION"] = "mutated"


class TestWaitForContract:
    @staticmethod
    def _assert_timeout_failure(outcome: Outcome, looking_for: str) -> None:
        assert outcome.state is State.FAIL
        assert isinstance(outcome.failure, TestFailure)

        artifact = outcome.failure.to_json()
        assert artifact["fail_message"] == f"timed out waiting for {looking_for}"
        assert artifact["expected"] == looking_for
        assert artifact["received"] == "timeout"

    def test_looking_for_is_a_required_keyword(self) -> None:
        parameter = inspect.signature(TestUtils.wait_for).parameters["looking_for"]

        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty

    async def test_configure_timeout_fails_without_building_the_suite(self) -> None:
        looking_for = "UUT boot flag present"
        built: list[str] = []

        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                await utils.wait_for(
                    lambda: False,
                    looking_for=looking_for,
                    timeout_seconds=0,
                )
                return {}

            def build_suite(self) -> LitmusTest:
                built.append("built")
                return self.test("unreachable", self._unreachable)

            async def _unreachable(self, utils: TestUtils) -> Verdict:
                return PASS

        outcome = await Suite(name="configure gate").run()

        self._assert_timeout_failure(outcome, looking_for)
        assert built == []

    async def test_test_method_timeout_is_a_failure_artifact(self) -> None:
        looking_for = "stress test log present on UUT"

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("wait for stress test", self._wait_for_stress_test)

            async def _wait_for_stress_test(self, utils: TestUtils) -> Verdict:
                await utils.wait_for(
                    lambda: False,
                    looking_for=looking_for,
                    timeout_seconds=0,
                )
                return PASS

        outcome = await Suite(name="verdict wait").run()

        self._assert_timeout_failure(outcome, looking_for)


class TestFrameworkLoggingDiscipline:
    def test_framework_code_never_calls_print(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "litmus"
        violations: list[str] = []

        for source_path in source_root.rglob("*.py"):
            tree = ast.parse(source_path.read_text(), filename=str(source_path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"
                ):
                    relative = source_path.relative_to(source_root)
                    violations.append(f"{relative}:{node.lineno}")

        assert violations == []


class _HostChild:
    """Start a host process `utils.exec` can sit on. Not production API."""

    @staticmethod
    def pid_sleep_command(pid_path: Path, seconds: int = 60) -> str:
        script = (
            "import os, time; "
            f"open({str(pid_path)!r}, 'w').write(str(os.getpid())); "
            f"time.sleep({seconds})"
        )
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    @staticmethod
    def pidfile_ready(pid_path: Path) -> bool:
        if not pid_path.exists():
            return False
        return bool(pid_path.read_text().strip())

    @staticmethod
    async def wait_for_pidfile(pid_path: Path, seconds: float = 2.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if _HostChild.pidfile_ready(pid_path):
                return
            await asyncio.sleep(0.02)
        raise AssertionError("child never wrote its pid")


class TestUtilsExecNeverThrows:
    """A failed command is evidence. The test method decides the verdict."""

    async def test_a_successful_command_returns_stdout(self) -> None:
        result = await TestUtils().exec("echo hello")

        assert result.exit_code == 0
        assert result.stdout.strip() == "hello"
        assert result.stderr == ""

    async def test_a_nonzero_exit_is_evidence_not_an_exception(self) -> None:
        result = await TestUtils().exec("exit 7")

        assert result == ExecResult(stdout="", stderr="", exit_code=7)

    async def test_stderr_is_captured(self) -> None:
        result = await TestUtils().exec("echo err >&2")

        assert result.exit_code == 0
        assert result.stderr.strip() == "err"

    async def test_a_missing_command_is_still_a_result(self) -> None:
        result = await TestUtils().exec("litmus-no-such-command-on-purpose")

        assert result.exit_code != 0
        assert result.exit_code != -1

    async def test_a_signal_killed_child_yields_minus_one(self) -> None:
        # `kill -TERM $$` delivers SIGTERM to the shell — the direct child —
        # so settlement sees a signal death rather than a numeric exit status.
        result = await TestUtils().exec("kill -TERM $$")

        assert result.exit_code == -1

    async def test_abort_settles_as_minus_one_not_an_exception(self, tmp_path: Path) -> None:
        utils = TestUtils()
        pid_path = tmp_path / "pid"
        task = asyncio.create_task(utils.exec(_HostChild.pid_sleep_command(pid_path)))
        await _HostChild.wait_for_pidfile(pid_path)

        # Abort is a framework action on the seed's executor, not an author API.
        await object.__getattribute__(utils, "_local_exec").cancel_in_flight()
        result = await task

        assert result.exit_code == -1

    async def test_task_cancellation_still_propagates(self, tmp_path: Path) -> None:
        utils = TestUtils()
        pid_path = tmp_path / "pid"
        task = asyncio.create_task(utils.exec(_HostChild.pid_sleep_command(pid_path)))
        await _HostChild.wait_for_pidfile(pid_path)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_a_suite_method_inspects_failure_and_decides_the_verdict(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("service", self._service)

            async def _service(self, utils: TestUtils) -> Verdict:
                result = await utils.exec("exit 1")
                if result.exit_code != 0:
                    raise TestFailure("service down", "0", str(result.exit_code))
                return PASS

        outcome = await Suite(name="service").run()

        assert outcome.state is State.FAIL
        assert isinstance(outcome.failure, TestFailure)
        assert outcome.failure.expected == "0"
        assert outcome.failure.received == "1"

    async def test_exec_logs_land_on_the_leaf(self, caplog: pytest.LogCaptureFixture) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("probe", self._probe)

            async def _probe(self, utils: TestUtils) -> Verdict:
                await utils.exec("echo observed")
                return PASS

        with caplog.at_level("INFO", logger="litmus"):
            outcome = await Suite(name="rack").run()

        assert outcome.state is State.PASS
        records = [
            record
            for record in caplog.records
            if "attempting to run command" in record.message
            or "exit code:" in record.message
            or "stdout:" in record.message
        ]
        assert [cast("_ScopedLogRecord", record).litmus_node for record in records] == [
            "probe",
            "probe",
            "probe",
        ]
        messages = [record.message for record in records]
        assert any("echo observed" in message for message in messages)
        assert any("exit code: 0" in message for message in messages)

    async def test_routed_exec_after_cancel_settles_minus_one(
        self, install_mock_connector: ConnectorInstaller
    ) -> None:
        transport = MockTransport()
        utils = TestUtils()
        install_mock_connector(utils, transport)
        await utils.use_shell(host="dut", username="root")

        await object.__getattribute__(utils, "_transports").cancel_in_flight()
        result = await utils.exec("uname")

        assert result.exit_code == -1
        assert "not connected" in result.stderr
        assert transport.connected is False

    async def test_in_flight_routed_exec_settles_minus_one(
        self, install_mock_connector: ConnectorInstaller
    ) -> None:
        transport = MockTransport()
        transport.park("flash")
        utils = TestUtils()
        install_mock_connector(utils, transport)
        await utils.use_shell(host="dut", username="root")
        task = asyncio.create_task(utils.exec("flash"))
        deadline = asyncio.get_running_loop().time() + 2
        while transport.in_flight_count < 1:
            assert asyncio.get_running_loop().time() < deadline, "parked exec never started"
            await asyncio.sleep(0)

        await object.__getattribute__(utils, "_transports").cancel_in_flight()
        result = await task

        assert result.exit_code == -1
        assert "transport closed" in result.stderr
        assert transport.connected is False
        assert transport.in_flight_count == 0

    async def test_shell_exec_after_cancel_settles_minus_one(
        self, install_mock_connector: ConnectorInstaller
    ) -> None:
        transport = MockTransport()
        utils = TestUtils()
        install_mock_connector(utils, transport)
        shell = await utils.use_shell(host="dut", username="root")

        await transport.cancel_in_flight()
        result = await shell.exec("uname")

        assert result.exit_code == -1
        assert "not connected" in result.stderr


class _EnvSuite(LitmusTest):
    def build_suite(self) -> LitmusTest:
        return self.parallel("claims", [self.test("probe", self._probe)])

    async def _probe(self, _utils: TestUtils) -> Verdict:
        return PASS


class TestEnvSnapshotFilter:
    """Issue #53 ruling: the run publishes its environment by allow list or deny list."""

    _SOURCE: Mapping[str, str] = MappingProxyType(
        {
            "PHYSICAL_LOCATION": "bench-7",
            "GITLAB_TOKEN": "glpat-secret",
            "AWS_SECRET_ACCESS_KEY": "hunter2",
            "HOME": "/home/ops",
            "HOMEBREW_PREFIX": "/opt/homebrew",
        }
    )

    def test_no_list_publishes_the_source_verbatim(self) -> None:
        utils = TestUtils(env=self._SOURCE)

        assert dict(utils.env) == self._SOURCE

    def test_an_allow_list_keeps_only_matching_names(self) -> None:
        utils = TestUtils(env=self._SOURCE, env_allow=["^PHYSICAL_LOCATION$", "^HOME$"])

        assert dict(utils.env) == {"PHYSICAL_LOCATION": "bench-7", "HOME": "/home/ops"}

    def test_a_deny_list_withholds_matching_names(self) -> None:
        utils = TestUtils(env=self._SOURCE, env_deny=["TOKEN", "SECRET"])

        assert dict(utils.env) == {
            "PHYSICAL_LOCATION": "bench-7",
            "HOME": "/home/ops",
            "HOMEBREW_PREFIX": "/opt/homebrew",
        }

    def test_patterns_search_the_name_so_anchors_are_the_authors_choice(self) -> None:
        unanchored = TestUtils(env=self._SOURCE, env_allow=["HOME"])
        anchored = TestUtils(env=self._SOURCE, env_allow=["^HOME$"])

        assert set(unanchored.env) == {"HOME", "HOMEBREW_PREFIX"}
        assert set(anchored.env) == {"HOME"}

    def test_values_are_never_rewritten_only_present_or_absent(self) -> None:
        utils = TestUtils(env=self._SOURCE, env_deny=["TOKEN"])

        assert "GITLAB_TOKEN" not in utils.env
        assert utils.env["AWS_SECRET_ACCESS_KEY"] == "hunter2"

    def test_allow_and_deny_together_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            TestUtils(env=self._SOURCE, env_allow=["HOME"], env_deny=["TOKEN"])

    async def test_run_rejects_both_lists_before_anything_executes(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            await _EnvSuite(name="rack").run(env=self._SOURCE, env_allow=["HOME"], env_deny=["X"])

    def test_debug_mode_publishes_everything_and_says_so(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        source = {**self._SOURCE, "environment": "DEBUG"}

        with caplog.at_level("WARNING", logger="litmus"):
            utils = TestUtils(env=source, env_deny=["TOKEN", "SECRET"])

        assert dict(utils.env) == source
        assert [record.message for record in caplog.records] == [
            "DEBUG mode detected, ignoring provided deny list and including ALL "
            "environment variables"
        ]

    def test_debug_mode_names_the_list_it_ignored(self, caplog: pytest.LogCaptureFixture) -> None:
        source = {**self._SOURCE, "environment": "DEBUG"}

        with caplog.at_level("WARNING", logger="litmus"):
            TestUtils(env=source, env_allow=["^HOME$"])

        assert "ignoring provided allow list" in caplog.records[0].message

    def test_debug_mode_is_the_exact_key_and_value(self, caplog: pytest.LogCaptureFixture) -> None:
        source = {**self._SOURCE, "environment": "debug", "ENVIRONMENT": "DEBUG"}

        with caplog.at_level("WARNING", logger="litmus"):
            utils = TestUtils(env=source, env_deny=["TOKEN"])

        assert "GITLAB_TOKEN" not in utils.env
        assert caplog.records == []

    def test_no_list_never_logs_even_in_debug_mode(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="litmus"):
            TestUtils(env={**self._SOURCE, "environment": "DEBUG"})

        assert caplog.records == []

    async def test_the_filtered_view_is_what_every_result_publishes(self) -> None:
        result = await _EnvSuite(name="rack").run(env=self._SOURCE, env_deny=["TOKEN", "SECRET"])
        payload = result.to_json()
        children = cast("tuple[RunResult, ...]", result.value)

        assert payload["format_version"] == 1
        assert payload["env"] == {
            "PHYSICAL_LOCATION": "bench-7",
            "HOME": "/home/ops",
            "HOMEBREW_PREFIX": "/opt/homebrew",
        }
        assert payload["physical_location"] == "bench-7"
        assert [dict(child.env) for child in children] == [payload["env"]]

    async def test_an_allow_list_that_drops_the_station_id_reports_unknown(self) -> None:
        result = await _EnvSuite(name="rack").run(env=self._SOURCE, env_allow=["^HOME$"])

        assert result.to_json()["physical_location"] == "unknown"


_OVERFLOW_DUT_COUNT = 70
_OVERFLOW_BUDGET = 256
_OVERFLOW_MEMORY_ROUNDS = 4
_OVERFLOW_RETAINED_LIMIT_BYTES = 256 * 1024


class _OverflowDut(LitmusTest):
    """One DUT whose probe dumps past the run's exec output budget."""

    def build_suite(self) -> LitmusTest:
        return self.test("overflow", self._overflow)

    async def _overflow(self, utils: TestUtils) -> Verdict:
        result = await utils.exec("head -c 65536 /dev/zero")
        if not result.truncated:
            raise TestFailure(
                "overflow was not truncated",
                "truncated output",
                f"captured {result.captured_bytes}",
            )
        return PASS


class _OverflowRack(LitmusTest):
    """Seventy overflowing DUT lifecycles under one run seed."""

    def __init__(self) -> None:
        super().__init__(name="overflow rack")
        self._duts = tuple(
            _OverflowDut(name=f"dut-{index:02d}") for index in range(_OVERFLOW_DUT_COUNT)
        )

    def build_suite(self) -> LitmusTest:
        return self.parallel("rack DUTs", self._duts)


async def _run_overflow_rack() -> None:
    """Run one overflowing rack and drop the result so it cannot pin memory."""
    result = await _OverflowRack().run(output_budget=_OVERFLOW_BUDGET)
    assert result.state is State.PASS
    del result


def _retained_litmus_bytes() -> int:
    snapshot = tracemalloc.take_snapshot().filter_traces(
        (tracemalloc.Filter(True, "*/src/litmus/*"),)
    )
    return sum(stat.size for stat in snapshot.statistics("filename"))


class TestUtilsOutputBudget:
    """The run seed owns the budget; utils.exec names truncation in the scoped log."""

    async def test_truncation_is_evidence_not_an_exception(self) -> None:
        utils = TestUtils(output_budget=32)
        result = await utils.exec("head -c 4096 /dev/zero")
        assert result.truncated is True
        assert result.captured_bytes == 32

    async def test_the_scoped_log_names_truncation(self, caplog: pytest.LogCaptureFixture) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("probe", self._probe)

            async def _probe(self, utils: TestUtils) -> Verdict:
                await utils.exec("head -c 4096 /dev/zero")
                return PASS

        with caplog.at_level("WARNING", logger="litmus"):
            outcome = await Suite(name="rack").run(output_budget=32)

        assert outcome.state is State.PASS
        records = [record for record in caplog.records if "exec output truncated" in record.message]
        assert len(records) == 1
        assert cast("_ScopedLogRecord", records[0]).litmus_node == "probe"
        assert "captured 32 bytes" in records[0].message
        assert "budget 32" in records[0].message

    async def test_seventy_dut_run_completes_when_every_exec_hits_the_budget(self) -> None:
        await _run_overflow_rack()

    async def test_retained_memory_stays_flat_across_overflowing_racks(self) -> None:
        await _run_overflow_rack()
        gc.collect()
        tracemalloc.start()
        try:
            await _run_overflow_rack()
            gc.collect()
            baseline = _retained_litmus_bytes()
            samples: list[int] = []
            for _ in range(_OVERFLOW_MEMORY_ROUNDS):
                await _run_overflow_rack()
                gc.collect()
                samples.append(_retained_litmus_bytes())
        finally:
            tracemalloc.stop()

        retained_growth = max(samples) - baseline
        assert retained_growth <= _OVERFLOW_RETAINED_LIMIT_BYTES, (
            f"retained Litmus memory grew by {retained_growth} bytes; "
            f"limit is {_OVERFLOW_RETAINED_LIMIT_BYTES} bytes"
        )
