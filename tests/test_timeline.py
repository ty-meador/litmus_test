"""The persisted temporal backbone for each Litmus run."""

import asyncio
import json
from pathlib import Path

import pytest

from litmus import PASS, Artifact, LitmusTest, TestFailure, TestUtils, Verdict
from litmus.lifecycle import State
from litmus.timeline import RunTimeline


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestPersistedTimeline:
    async def test_run_streams_per_owner_logs_and_artifact_pointers(self, tmp_path: Path) -> None:
        captured: list[Path] = []

        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self._snapshot_case = self.case("snapshot", self._snapshot)
                self._checks = self.sequence("checks", [self._snapshot_case])

            def build_suite(self) -> LitmusTest:
                return self._checks

            async def _snapshot(self, utils: TestUtils) -> Verdict:
                utils.logger.debug("line one\nline two")
                captured.append(
                    await utils.capture_artifact(
                        Artifact(name="pre-flash", type="survey", data={"ready": True})
                    )
                )
                if captured:
                    return PASS
                raise TestFailure("snapshot was not captured")

        suite = Suite(name="rack/name")
        result = await suite.run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.PASS
        assert suite.log_path is not None
        assert suite._checks.log_path is not None
        assert suite._snapshot_case.log_path is None
        run_root = suite.log_path.parent.parent
        assert run_root.parent == tmp_path
        assert suite._checks.log_path.parent.parent == run_root
        assert captured[0].parent == run_root

        root_records = _records(suite.log_path)
        check_records = _records(suite._checks.log_path)
        assert root_records
        assert all(record["format_version"] == 1 for record in root_records)
        for record in root_records:
            scope = record["scope"]
            assert isinstance(scope, list)
            assert scope[0] == "rack/name"

        debug = next(
            record for record in check_records if record["message"] == "line one\nline two"
        )
        assert debug == {
            "format_version": 1,
            "level": "debug",
            "message": "line one\nline two",
            "scope": ["rack/name", "checks", "snapshot"],
            "timestamp": debug["timestamp"],
        }
        pointer = next(record for record in check_records if "artifact_path" in record)
        assert pointer["artifact_name"] == "pre-flash"
        assert pointer["artifact_path"] == str(captured[0])
        assert pointer["artifact_type"] == "survey"
        assert pointer["artifact_timestamp"]

        human = suite._checks.log_path.with_name("timeline.log").read_text(encoding="utf-8")
        assert "[DEBUG]: line one\\nline two\n" in human
        entries = await asyncio.to_thread(lambda: list(tmp_path.iterdir()))
        assert len(entries) == 1

    async def test_interrupted_run_keeps_every_complete_json_line(self, tmp_path: Path) -> None:
        recorded = asyncio.Event()

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("wait", self._wait)

            async def _wait(self, utils: TestUtils) -> Verdict:
                utils.logger.info("durable before cancellation")
                recorded.set()
                await asyncio.Event().wait()
                if utils.signal.fired:
                    return PASS
                raise TestFailure("wait unexpectedly returned")

        suite = Suite(name="rack")
        task = asyncio.create_task(suite.run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)}))
        await recorded.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        paths = await asyncio.to_thread(lambda: list(tmp_path.rglob("timeline.jsonl")))
        assert paths
        records = [record for path in paths for record in _records(path)]
        assert any(record["message"] == "durable before cancellation" for record in records)
        assert all(path.read_text(encoding="utf-8").endswith("\n") for path in paths)

    async def test_unreached_discrete_node_opens_no_log(self, tmp_path: Path) -> None:
        class Later(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("later claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                if utils.logger.scope:
                    return PASS
                raise TestFailure("node has no scope")

        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self._later = Later(name="later")
                self._checks = self.sequence(
                    "checks",
                    [self.case("stop", self._stop), self._later],
                )

            def build_suite(self) -> LitmusTest:
                return self._checks

            async def _stop(self, utils: TestUtils) -> Verdict:
                raise TestFailure("stop here")

        suite = Suite(name="rack")
        result = await suite.run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.FAIL
        assert suite._later.log_path is None
        paths = await asyncio.to_thread(lambda: list(tmp_path.rglob("timeline.jsonl")))
        assert len(paths) == 2

    async def test_debug_filter_override_is_explained_in_the_timeline(
        self,
        tmp_path: Path,
    ) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                if utils.env.get("SECRET") == "published":
                    return PASS
                raise TestFailure("DEBUG did not override the allow list")

        suite = Suite(name="rack")
        result = await suite.run(
            env={
                "environment": "DEBUG",
                "LITMUS_ARTIFACT_ROOT": str(tmp_path),
                "SECRET": "published",
            },
            env_allow=["^PHYSICAL_LOCATION$"],
        )

        assert result.state is State.PASS
        assert suite.log_path is not None
        messages = [record["message"] for record in _records(suite.log_path)]
        assert (
            messages.count(
                "DEBUG mode detected, ignoring provided allow list and including ALL "
                "environment variables"
            )
            == 1
        )

    @pytest.mark.parametrize(
        ("message", "args", "expected", "warns"),
        [
            ("%s %s", (1,), "%s %s (1,)", True),
            ("%(answer)s", ({"answer": 42},), "42", False),
            ("literal 100% %s", ("x",), "literal 100% %s ('x',)", True),
        ],
    )
    async def test_logging_format_errors_preserve_verdict_and_evidence(
        self,
        tmp_path: Path,
        message: str,
        args: tuple[object, ...],
        expected: str,
        warns: bool,
    ) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                utils.logger.info(message, *args)
                if utils.logger.scope:
                    return PASS
                raise TestFailure("node has no scope")

        suite = Suite(name="rack")
        result = await suite.run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.PASS
        assert suite.log_path is not None
        records = _records(suite.log_path)
        assert any(record["message"] == expected for record in records)
        warnings = [
            record
            for record in records
            if record["level"] == "warning"
            and str(record["message"]).startswith("logging message formatting failed")
        ]
        assert bool(warnings) is warns

    def test_write_after_close_appends_one_complete_entry(self, tmp_path: Path) -> None:
        timeline = RunTimeline(tmp_path)
        paths = timeline.register(("rack",))
        timeline.write(paths, scope=("rack",), level=20, message="before close", extra=None)
        timeline.close(paths)

        timeline.write(paths, scope=("rack",), level=20, message="after close", extra=None)
        timeline.close(paths)

        records = _records(paths.json_lines)
        assert [record["message"] for record in records] == ["before close", "after close"]
        assert paths.json_lines.read_text(encoding="utf-8").endswith("\n")

    async def test_sequence_roll_up_writes_into_parent_timeline(self, tmp_path: Path) -> None:
        passing_case = self._passing_case

        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self._claim = self.case("claim", passing_case)
                self._checks = self.sequence("checks", [self._claim], discrete_log_file=False)

            def build_suite(self) -> LitmusTest:
                return self._checks

        suite = Suite(name="rack")
        result = await suite.run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.PASS
        assert suite.log_path is not None
        assert suite._checks.log_path is None
        assert suite._claim.log_path is None
        records = _records(suite.log_path)
        assert any(record["scope"] == ["rack", "checks", "claim"] for record in records)

    async def test_root_always_owns_timeline_when_opted_out(self, tmp_path: Path) -> None:
        passing_case = self._passing_case

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", passing_case)

        suite = Suite(name="rack", discrete_log_file=False)
        result = await suite.run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.PASS
        assert suite.log_path is not None
        assert _records(suite.log_path)

    def test_sequence_and_group_can_roll_up_explicitly(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                raise AssertionError

        suite = Suite(name="rack")
        case = suite.case("claim", self._passing_case)
        sequence = suite.sequence("sequence", [case], discrete_log_file=False)
        group = suite.group("group", [case], discrete_log_file=False)

        assert case.discrete_log_file is False
        assert sequence.discrete_log_file is False
        assert group.discrete_log_file is False

    @staticmethod
    async def _passing_case(utils: TestUtils) -> Verdict:
        if utils.logger.scope:
            return PASS
        raise TestFailure("node has no scope")
