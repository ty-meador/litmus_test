"""The framework-owned configure, suite, and cleanup lifecycle."""

import asyncio

import pytest

from litmus import PASS, ContractViolation, LitmusTest, TestFailure, Verdict
from litmus.lifecycle import State


class StubUtils:
    def __init__(self, **ctx: object) -> None:
        self.ctx: dict[str, object] = dict(ctx)


class TestLifecycleOrder:
    async def test_configure_context_reaches_suite_and_cleanup_in_order(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class Suite(LitmusTest):
            async def configure(self, utils: StubUtils) -> dict[str, object]:
                calls.append(("configure", dict(utils.ctx)))
                return {"serial": "SN-0042", "station": "west"}

            def build_suite(self) -> LitmusTest:
                calls.append(("build_suite", {}))
                return self.case("claim", self._claim)

            async def _claim(self, utils: StubUtils) -> Verdict:
                calls.append(("test", dict(utils.ctx)))
                return PASS

            async def cleanup(self, utils: StubUtils) -> None:
                calls.append(("cleanup", dict(utils.ctx)))

        utils = StubUtils(station="east")

        outcome = await Suite(name="rack").main(utils)

        assert outcome.state is State.PASS
        assert utils.ctx == {"serial": "SN-0042", "station": "west"}
        assert calls == [
            ("configure", {"station": "east"}),
            ("build_suite", {}),
            ("test", {"serial": "SN-0042", "station": "west"}),
            ("cleanup", {"serial": "SN-0042", "station": "west"}),
        ]

    async def test_configure_failure_skips_suite_but_still_cleans_up(self) -> None:
        calls: list[str] = []
        readiness = TestFailure("device did not reach a testable state")

        class Suite(LitmusTest):
            async def configure(self, utils: StubUtils) -> dict[str, object]:
                calls.append("configure")
                raise readiness

            def build_suite(self) -> LitmusTest:
                calls.append("build_suite")
                return self.case("claim", self._claim)

            async def _claim(self, utils: StubUtils) -> Verdict:
                calls.append("test")
                return PASS

            async def cleanup(self, utils: StubUtils) -> None:
                calls.append("cleanup")

        outcome = await Suite(name="rack").main(StubUtils())

        assert outcome.state is State.FAIL
        assert outcome.failure is readiness
        assert calls == ["configure", "cleanup"]

    async def test_plain_configure_error_is_a_bug_signaled_failure(self) -> None:
        calls: list[str] = []

        class Suite(LitmusTest):
            async def configure(self, utils: StubUtils) -> dict[str, object]:
                raise ValueError("malformed readiness probe")

            async def cleanup(self, utils: StubUtils) -> None:
                calls.append("cleanup")

        outcome = await Suite(name="rack").main(StubUtils())

        assert outcome.state is State.FAIL
        assert isinstance(outcome.failure, ContractViolation)
        assert isinstance(outcome.failure.cause, ValueError)
        assert calls == ["cleanup"]


class TestCleanupGuarantee:
    async def test_test_failure_still_reaches_cleanup(self) -> None:
        calls: list[str] = []
        failed_check = TestFailure("firmware revision mismatch")

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: StubUtils) -> Verdict:
                raise failed_check

            async def cleanup(self, utils: StubUtils) -> None:
                calls.append("cleanup")

        outcome = await Suite(name="rack").main(StubUtils())

        assert outcome.state is State.FAIL
        assert outcome.failure is failed_check
        assert calls == ["cleanup"]

    async def test_cleanup_error_is_logged_and_does_not_mask_the_verdict(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: StubUtils) -> Verdict:
                return PASS

            async def cleanup(self, utils: StubUtils) -> None:
                raise RuntimeError("relay refused neutral state")

        with caplog.at_level("ERROR", logger="litmus"):
            outcome = await Suite(name="rack").main(StubUtils())

        assert outcome.state is State.PASS
        assert len(caplog.records) == 1
        assert "cleanup failed (verdict unaffected)" in caplog.records[0].getMessage()
        assert "CONTRACT VIOLATION IN rack" in caplog.records[0].getMessage()

    async def test_cancellation_waits_for_shielded_cleanup(self) -> None:
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        calls: list[str] = []

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: StubUtils) -> Verdict:
                calls.append("test")
                return PASS

            async def cleanup(self, utils: StubUtils) -> None:
                calls.append("cleanup-started")
                cleanup_started.set()
                await cleanup_release.wait()
                calls.append("cleanup-finished")

        running = asyncio.create_task(Suite(name="rack").main(StubUtils()))
        await cleanup_started.wait()

        running.cancel()
        await asyncio.sleep(0)

        assert not running.done()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert calls == ["test", "cleanup-started", "cleanup-finished"]

    async def test_second_cancellation_still_waits_for_cleanup(self) -> None:
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        calls: list[str] = []

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: StubUtils) -> Verdict:
                calls.append("test")
                return PASS

            async def cleanup(self, utils: StubUtils) -> None:
                calls.append("cleanup-started")
                cleanup_started.set()
                await cleanup_release.wait()
                calls.append("cleanup-finished")

        running = asyncio.create_task(Suite(name="rack").main(StubUtils()))
        await cleanup_started.wait()

        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        await asyncio.sleep(0)

        assert not running.done()
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert calls == ["test", "cleanup-started", "cleanup-finished"]


class TestBuildSuiteClassification:
    """Pinned decision from the !17 review: build_suite runs outside any child
    boundary, so a plain exception raised there escapes main raw. Classification
    of composition-time errors is deferred to the tier-2 checks (#12, #13)."""

    async def test_plain_build_suite_error_escapes_raw_but_still_cleans_up(self) -> None:
        calls: list[str] = []

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                raise ValueError("composition table is malformed")

            async def cleanup(self, utils: StubUtils) -> None:
                calls.append("cleanup")

        with pytest.raises(ValueError, match="composition table is malformed"):
            await Suite(name="rack").main(StubUtils())

        assert calls == ["cleanup"]
