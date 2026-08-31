"""Constructor-registered survey and audit execution."""

import asyncio
import json
import logging
from pathlib import Path

import pytest

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict
from litmus.lifecycle import Slot, State


def _artifact_payloads(root: Path) -> dict[str, object]:
    return {
        path.name.split(".survey.", maxsplit=1)[0]: json.loads(path.read_text())["data"]
        for path in root.glob("*.artifact")
    }


class TestSurveyHooks:
    async def test_surveys_capture_empty_values_at_every_reachable_phase(
        self, tmp_path: Path
    ) -> None:
        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_survey(self._survey)

            async def configure(self, utils: TestUtils) -> dict[str, object]:
                return {"ready": True}

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def cleanup(self, utils: TestUtils) -> None:
                utils.ctx["cleaned"] = True

            async def _survey(self, utils: TestUtils) -> object:
                return {}

            async def _claim(self, utils: TestUtils) -> Verdict:
                if utils.ctx["ready"] is True:
                    return PASS
                raise TestFailure("suite was not ready")

        result = await Suite(name="rack").run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.PASS
        assert _artifact_payloads(tmp_path) == {
            "post-cleanup-1": {},
            "post-configure-1": {},
            "post-test-1": {},
            "pre-cleanup-1": {},
            "pre-configure-1": {},
            "pre-test-1": {},
        }

    async def test_explicit_none_is_captured_but_fallthrough_is_not(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_survey(self._explicit_none)
                self.use_survey(self._fallthrough)

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _explicit_none(self, utils: TestUtils) -> object:
                return None

            async def _fallthrough(self, utils: TestUtils) -> object:
                utils.logger.info("observed")

            async def _claim(self, utils: TestUtils) -> Verdict:
                if True:
                    return PASS
                raise TestFailure("unreachable")

        with caplog.at_level(logging.INFO, logger="litmus"):
            result = await Suite(name="rack").run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.PASS
        assert len(_artifact_payloads(tmp_path)) == 6
        assert all(name.endswith("-1") for name in _artifact_payloads(tmp_path))
        skipped = [
            record
            for record in caplog.records
            if "returned no value; skipping artifact capture" in record.getMessage()
        ]
        assert len(skipped) == 6

    async def test_survey_failures_are_logged_and_never_replace_the_verdict(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_survey(self._broken_survey)

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _broken_survey(self, utils: TestUtils) -> object:
                raise TestFailure("sensor unavailable")

            async def _claim(self, utils: TestUtils) -> Verdict:
                if True:
                    return PASS
                raise TestFailure("unreachable")

        with caplog.at_level(logging.ERROR, logger="litmus"):
            result = await Suite(name="rack").run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.PASS
        assert await asyncio.to_thread(lambda: list(tmp_path.glob("*.artifact"))) == []
        assert sum("verdict unaffected" in record.getMessage() for record in caplog.records) == 6


class TestAuditHooks:
    @pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), True])
    def test_interval_must_be_positive_and_finite(self, interval: float) -> None:
        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_audit(interval=interval, fn=self._audit)

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _audit(self, utils: TestUtils) -> object:
                return {}

            async def _claim(self, utils: TestUtils) -> Verdict:
                if True:
                    return PASS
                raise TestFailure("unreachable")

        with pytest.raises(ValueError, match="positive finite"):
            Suite(name="rack")

    async def test_audit_failure_cancels_the_test_and_becomes_the_verdict(
        self, tmp_path: Path
    ) -> None:
        cancelled = asyncio.Event()
        cleanup_ran = asyncio.Event()
        observed_signal: list[bool] = []

        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_audit(interval=0.001, fn=self._audit)

            def build_suite(self) -> LitmusTest:
                return self.case("long-running claim", self._claim)

            async def cleanup(self, utils: TestUtils) -> None:
                cleanup_ran.set()

            async def _audit(self, utils: TestUtils) -> object:
                raise TestFailure("temperature exceeded limit", expected=80, received=95)

            async def _claim(self, utils: TestUtils) -> Verdict:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    observed_signal.append(utils.signal.fired)
                    cancelled.set()
                    raise
                raise TestFailure("unreachable")

        result = await Suite(name="rack").run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.slot is Slot.AUDIT
        assert result.state is State.FAIL
        assert result.failure is not None
        assert result.failure.fail_message == "temperature exceeded limit"
        assert cancelled.is_set()
        assert cleanup_ran.is_set()
        assert observed_signal == [True]

    async def test_audit_values_use_the_survey_artifact_contract_and_stop_with_the_suite(
        self, tmp_path: Path
    ) -> None:
        dispatches = 0

        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_audit(interval=0.001, fn=self._audit)

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _audit(self, utils: TestUtils) -> object:
                nonlocal dispatches
                dispatches += 1
                return {}

            async def _claim(self, utils: TestUtils) -> Verdict:
                await asyncio.sleep(0.01)
                if True:
                    return PASS
                raise TestFailure("unreachable")

        result = await Suite(name="rack").run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})
        settled_dispatches = dispatches
        await asyncio.sleep(0.005)

        payloads = _artifact_payloads(tmp_path)
        assert result.state is State.PASS
        assert settled_dispatches > 0
        assert dispatches == settled_dispatches
        assert len(payloads) == dispatches
        assert all(name.startswith("audit-1-") for name in payloads)
        assert all(value == {} for value in payloads.values())

    async def test_a_child_audit_failure_does_not_cancel_group_siblings(
        self, tmp_path: Path
    ) -> None:
        sibling_finished = asyncio.Event()

        class AuditedChild(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_audit(interval=0.001, fn=self._audit)

            def build_suite(self) -> LitmusTest:
                return self.case("watched claim", self._claim)

            async def _audit(self, utils: TestUtils) -> object:
                raise TestFailure("watchdog tripped")

            async def _claim(self, utils: TestUtils) -> Verdict:
                await asyncio.Event().wait()
                raise TestFailure("unreachable")

        class HealthySibling(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("healthy claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                await asyncio.sleep(0.005)
                sibling_finished.set()
                if sibling_finished.is_set():
                    return PASS
                raise TestFailure("unreachable")

        class Root(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.group(
                    "independent children",
                    [
                        AuditedChild(name="watched child"),
                        HealthySibling(name="healthy child"),
                    ],
                )

        result = await Root(name="rack").run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.FAIL
        assert result.failure is not None
        assert result.failure.fail_message == "watchdog tripped"
        assert sibling_finished.is_set()
