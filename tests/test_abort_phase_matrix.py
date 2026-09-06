"""Phase, composition, and stop-source evidence, pairing, and resource guarantees.

Configure/cleanup/surveys belong to an authored suite around each composition.
An enclosing audit stays live throughout that suite's lifecycle, including teardown.
Audit verdicts remain Fail; only integration errors and operator signals halt the run.
"""

import asyncio
import json
import os
import signal
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
from tests.mock_connector import ConnectorInstaller

from litmus import (
    PASS,
    AfterEachContext,
    Artifact,
    BeforeEachContext,
    ExecResult,
    ExternalIntegrationFailure,
    LitmusTest,
    OperatorInterrupt,
    TestFailure,
    TestUtils,
    Verdict,
)
from litmus.lifecycle import AuditSignal, RunAbort, State
from litmus.mock import MockTransport
from litmus.results import SummaryTreeNode


class _Phase(StrEnum):
    BEFORE_EACH = "before_each"
    CONFIGURE = "configure"
    TEST_METHOD = "test_method"
    AUDIT_TICK = "audit_tick"
    CLEANUP = "cleanup"
    FINALIZATION = "finalization"
    AFTER_EACH = "after_each"


class _Kind(StrEnum):
    LEAF = "leaf"
    SEQUENCE = "sequence"
    PARALLEL = "parallel"


class _Source(StrEnum):
    BEFORE_HOOK = "before-hook"
    AFTER_HOOK = "after-hook"
    AUDIT = "audit"
    SIGINT = "SIGINT"
    SIGTERM = "SIGTERM"


@dataclass
class _TransportProbe:
    """Observe resource shutdown independently of the cancelled lifecycle task."""

    transport: MockTransport = field(default_factory=MockTransport)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    hard_closed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Controls:
    phase: _Phase
    kind: _Kind
    source: _Source
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    fired: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    before: list[str] = field(default_factory=list[str])
    after: list[str] = field(default_factory=list[str])
    observations: list[int] = field(default_factory=list[int])
    completed: list[str] = field(default_factory=list[str])
    operations: list[asyncio.Task[ExecResult]] = field(
        default_factory=list[asyncio.Task[ExecResult]]
    )
    audit_failure: TestFailure = field(default_factory=lambda: TestFailure("thermal interlock"))
    integration_cause: RuntimeError = field(
        default_factory=lambda: RuntimeError("coordinator lost")
    )

    async def checkpoint(self, phase: _Phase) -> None:
        if self.phase is phase:
            self.reached.set()
            if phase in (_Phase.CLEANUP, _Phase.FINALIZATION):
                # Both are shielded; prove shutdown before permitting settlement.
                await self.release.wait()
            else:
                await asyncio.Event().wait()

    async def before_each(self, ctx: BeforeEachContext) -> None:
        self.before.append(ctx.bread_crumbs)
        if ctx.name == "target":
            await self.checkpoint(_Phase.BEFORE_EACH)
        if ctx.name == "trigger" and self.source is _Source.BEFORE_HOOK:
            await self.reached.wait()
            raise self.integration_cause

    async def after_each(self, ctx: AfterEachContext) -> None:
        self.after.append(ctx.bread_crumbs)
        if ctx.name == "target":
            await self.checkpoint(_Phase.AFTER_EACH)
        if ctx.name == "trigger" and self.source is _Source.AFTER_HOOK:
            await self.reached.wait()
            raise self.integration_cause


class _Target(LitmusTest):
    def __init__(self, controls: _Controls) -> None:
        super().__init__(name="target")
        self._controls = controls
        self.use_survey(self._survey)
        self.use_audit(interval=0.000_001, fn=self._audit)

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        await self._controls.checkpoint(_Phase.CONFIGURE)
        self._controls.completed.append("configure")
        return {"configured": True}

    def build_suite(self) -> LitmusTest:
        if self._controls.kind is _Kind.LEAF:
            return self.test("claim", self._claim)
        children = [self.test("claim", self._claim), self.test("sibling", self._sibling)]
        if self._controls.kind is _Kind.SEQUENCE:
            return self.sequence("claims", children)
        return self.parallel("claims", children)

    async def _claim(self, utils: TestUtils) -> Verdict:
        await self._controls.checkpoint(_Phase.TEST_METHOD)
        if self._controls.phase is _Phase.AUDIT_TICK:
            await asyncio.Event().wait()
        if utils.ctx["configured"]:
            return PASS
        raise TestFailure("configure context missing")

    async def _sibling(self, utils: TestUtils) -> Verdict:
        if utils.ctx["configured"]:
            return PASS
        raise TestFailure("configure context missing")

    async def _audit(self, utils: TestUtils) -> object:
        await self._controls.checkpoint(_Phase.AUDIT_TICK)
        return {"audit": "healthy"}

    async def cleanup(self, utils: TestUtils) -> None:
        await self._controls.checkpoint(_Phase.CLEANUP)
        self._controls.completed.append("cleanup")

    async def _survey(self, utils: TestUtils) -> object:
        observation = len(self._controls.observations) + 1
        self._controls.observations.append(observation)
        return {"observation": observation}


class _Harness(LitmusTest):
    def __init__(
        self,
        controls: _Controls,
        probes: list[_TransportProbe],
        install_connector: ConnectorInstaller,
    ) -> None:
        super().__init__(name="rack")
        self._controls = controls
        self._probes = probes
        self._install_connector = install_connector
        self._target = _Target(controls)
        if controls.source is _Source.AUDIT:
            self.use_audit(interval=0.000_001, fn=self._audit)

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        self._install_connector(
            utils, {f"transport-{i}": probe.transport for i, probe in enumerate(self._probes)}
        )
        for i in range(len(self._probes)):
            await utils.get_shell(host=f"transport-{i}", username="operator")
        for probe in self._probes:
            self._controls.operations.append(
                asyncio.create_task(probe.transport.exec("firmware write"))
            )
            await probe.started.wait()
        return {}

    def build_suite(self) -> LitmusTest:
        if self._controls.source in (_Source.BEFORE_HOOK, _Source.AFTER_HOOK):
            return self.parallel("harness", [self._target, self.test("trigger", self._trigger)])
        return self._target

    async def _trigger(self, utils: TestUtils) -> Verdict:
        if self._controls.source is _Source.AFTER_HOOK:
            return PASS
        raise TestFailure("before-hook trigger body must be unreachable")

    async def _audit(self, utils: TestUtils) -> object:
        await self._controls.reached.wait()
        raise self._controls.audit_failure


def _walk(node: SummaryTreeNode) -> list[SummaryTreeNode]:
    return [node, *(descendant for child in node.children for descendant in _walk(child))]


class TestAbortPhaseMatrix:
    @pytest.mark.parametrize("phase", list(_Phase))
    @pytest.mark.parametrize("kind", list(_Kind))
    @pytest.mark.parametrize("source", list(_Source))
    async def test_stop_preserves_taxonomy_pairs_resources_and_evidence(
        self,
        phase: _Phase,
        kind: _Kind,
        source: _Source,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        controls = _Controls(phase, kind, source)
        probes = [_TransportProbe(), _TransportProbe()]
        transports = [probe.transport for probe in probes]
        for transport in transports:
            transport.park("firmware write")
        root = _Harness(controls, probes, install_mock_connector)
        original_abort = RunAbort.fire
        original_audit = AuditSignal.fire
        original_exec = MockTransport._exec  # pyright: ignore[reportPrivateUsage]
        original_cancel = MockTransport._cancel_in_flight  # pyright: ignore[reportPrivateUsage]
        original_capture = TestUtils.capture_artifact

        def observe_abort(
            abort: RunAbort, reason: ExternalIntegrationFailure | OperatorInterrupt
        ) -> None:
            original_abort(abort, reason)
            controls.fired.set()

        def observe_audit(audit: AuditSignal, reason: TestFailure) -> None:
            original_audit(audit, reason)
            controls.fired.set()

        async def observe_exec(transport: MockTransport, command: str) -> ExecResult:
            next(probe for probe in probes if probe.transport is transport).started.set()
            return await original_exec(transport, command)

        async def observe_cancel(transport: MockTransport) -> None:
            await original_cancel(transport)
            next(probe for probe in probes if probe.transport is transport).hard_closed.set()

        async def capture_final_observation(utils: TestUtils, artifact: Artifact) -> Path:
            path = await original_capture(utils, artifact)
            if utils.logger.scope[-1] == "target" and artifact.name == "post-cleanup-1":
                # Hold an accepted observation inside the real bounded publication
                # shield, not merely inside the authored survey callback.
                await controls.checkpoint(_Phase.FINALIZATION)
            return path

        # Observe the real signal transition; do not manufacture the halt or verdict.
        monkeypatch.setattr(RunAbort, "fire", observe_abort)
        monkeypatch.setattr(AuditSignal, "fire", observe_audit)
        monkeypatch.setattr(MockTransport, "_exec", observe_exec)
        monkeypatch.setattr(MockTransport, "_cancel_in_flight", observe_cancel)
        monkeypatch.setattr(TestUtils, "capture_artifact", capture_final_observation)
        running = asyncio.create_task(
            root.run(before_each=controls.before_each, after_each=controls.after_each)
        )
        try:
            async with asyncio.timeout(5):
                await controls.reached.wait()
                assert all(transport.connect_count == 1 for transport in transports)
                assert all(transport.connected for transport in transports)
                assert all(transport.has_in_flight for transport in transports)
                if source in (_Source.SIGINT, _Source.SIGTERM):
                    os.kill(os.getpid(), signal.Signals[source.value])
                await controls.fired.wait()
                if source is not _Source.AUDIT:
                    await asyncio.gather(*(probe.hard_closed.wait() for probe in probes))
                    assert all(not transport.connected for transport in transports)
                controls.release.set()
                result = await running
        finally:
            controls.release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            operation_results = await asyncio.gather(*controls.operations, return_exceptions=True)

        expected_state = State.FAIL if source is _Source.AUDIT else State.ABORTED
        assert result.state is expected_state
        assert result.summary_tree.state is expected_state
        assert len(operation_results) == 2
        assert all(isinstance(error, ConnectionError) for error in operation_results)
        if source is _Source.AUDIT:
            assert result.failure is controls.audit_failure
        elif source in (_Source.BEFORE_HOOK, _Source.AFTER_HOOK):
            assert isinstance(result.failure, ExternalIntegrationFailure)
            assert result.failure.cause is controls.integration_cause
            assert result.failure.hook == (
                "before_each" if source is _Source.BEFORE_HOOK else "after_each"
            )
        else:
            assert isinstance(result.failure, OperatorInterrupt)
            assert result.failure.signal_name == source.value

        assert Counter(controls.before) == Counter(controls.after)
        assert all(count == 1 for count in Counter(controls.before).values())
        assert "rack/target" in controls.before or "rack/harness/target" in controls.before
        for transport in transports:
            assert not transport.connected
            assert not transport.has_in_flight
            if source is not _Source.AUDIT:
                assert transport.cancel_count == 1

        assert phase is _Phase.BEFORE_EACH or "cleanup" in controls.completed
        assert controls.observations or phase is _Phase.BEFORE_EACH
        artifacts = [
            json.loads(path.read_text())
            for path in (tmp_path / "artifacts").rglob("*")
            if path.suffix in (".json", ".artifact")
        ]
        observed = [
            artifact["data"]["observation"]
            for artifact in artifacts
            if isinstance(artifact.get("data"), dict) and "observation" in artifact["data"]
        ]
        assert sorted(observed) == controls.observations
        survey_names = {
            artifact["name"]
            for artifact in artifacts
            if isinstance(artifact.get("data"), dict) and "observation" in artifact["data"]
        }
        completed_phase_surveys: dict[_Phase, set[str]] = {
            _Phase.BEFORE_EACH: set(),
            _Phase.CONFIGURE: {"pre-configure-1"},
            _Phase.TEST_METHOD: {"pre-configure-1", "post-configure-1", "pre-test-1"},
            _Phase.AUDIT_TICK: {"pre-configure-1", "post-configure-1", "pre-test-1"},
            _Phase.CLEANUP: {
                "pre-configure-1",
                "post-configure-1",
                "pre-test-1",
                "post-test-1",
                "pre-cleanup-1",
            },
            _Phase.FINALIZATION: {
                "pre-configure-1",
                "post-configure-1",
                "pre-test-1",
                "post-test-1",
                "pre-cleanup-1",
                "post-cleanup-1",
            },
            _Phase.AFTER_EACH: {
                "pre-configure-1",
                "post-configure-1",
                "pre-test-1",
                "post-test-1",
                "pre-cleanup-1",
                "post-cleanup-1",
            },
        }
        assert completed_phase_surveys[phase] <= survey_names
        records = [
            json.loads(line)
            for path in (tmp_path / "artifacts").rglob("timeline.jsonl")
            for line in path.read_text().splitlines()
        ]
        pointers = [record for record in records if record.get("artifact_name") in survey_names]
        assert len(pointers) == len(observed)
        assert {Path(pointer["artifact_path"]) for pointer in pointers} <= set(
            (tmp_path / "artifacts").rglob("*.artifact")
        )
        persisted = [artifact for artifact in artifacts if "summary_tree" in artifact]
        assert persisted == [result.to_json()]
        nodes = _walk(result.summary_tree)
        target = next(node for node in nodes if node.name == "target")
        target_state = expected_state
        if source is _Source.AUDIT and phase is _Phase.AFTER_EACH:
            target_state = State.PASS  # The completion hook already accepted this verdict.
        assert target.state is target_state
        for node in nodes:
            if node.state is expected_state:
                assert node.failure is result.failure

    async def test_completion_hook_failure_during_audit_shutdown_halts_the_run(
        self, install_mock_connector: ConnectorInstaller
    ) -> None:
        controls = _Controls(_Phase.TEST_METHOD, _Kind.PARALLEL, _Source.AUDIT)
        root = _Harness(controls, [], install_mock_connector)

        async def after_each(ctx: AfterEachContext) -> None:
            await controls.after_each(ctx)
            if ctx.name == "target":
                assert ctx.result.failure is controls.audit_failure
                raise controls.integration_cause

        async with asyncio.timeout(5):
            result = await root.run(before_each=controls.before_each, after_each=after_each)

        assert result.state is State.ABORTED
        assert isinstance(result.failure, ExternalIntegrationFailure)
        assert result.failure.cause is controls.integration_cause
        assert Counter(controls.before) == Counter(controls.after)
