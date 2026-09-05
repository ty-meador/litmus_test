"""RunResult navigation and the stable JSON boundary for a future Inspector."""

import asyncio
import json
import threading
from pathlib import Path
from typing import cast

import pytest

import litmus
from litmus import (
    PASS,
    ExternalIntegrationFailure,
    LitmusTest,
    OperatorInterrupt,
    RunResult,
    SummaryTreeNode,
    TestFailure,
    Verdict,
)
from litmus.lifecycle import Slot, State
from litmus.results import NodeType
from litmus.utils import _ArtifactStore  # pyright: ignore[reportPrivateUsage]


class TestRunResultNavigation:
    async def test_every_executed_result_carries_its_root_to_node_path(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "claims",
                    [
                        self.test("identity", self._passes),
                        self.test("firmware", self._passes),
                    ],
                )

            async def _passes(self, _utils: object) -> Verdict:
                return PASS

        result = await Suite(name="rack").run(env={"PHYSICAL_LOCATION": "bench-7"})
        children = cast("tuple[RunResult, ...]", result.value)

        assert litmus.RunResult is RunResult
        assert litmus.SummaryTreeNode is SummaryTreeNode
        assert result.ancestor_names == ["rack"]
        assert result.bread_crumbs == "rack"
        assert [child.ancestor_names for child in children] == [
            ["rack", "claims", "identity"],
            ["rack", "claims", "firmware"],
        ]
        assert [child.bread_crumbs for child in children] == [
            "rack/claims/identity",
            "rack/claims/firmware",
        ]
        assert result.env["PHYSICAL_LOCATION"] == "bench-7"

    async def test_root_summary_tree_contains_executed_and_unreached_nodes(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    "ordered claims",
                    [
                        self.test("failing gate", self._fails),
                        self.parallel(
                            "unreached group",
                            [self.test("unreached leaf", self._passes)],
                        ),
                    ],
                )

            async def _fails(self, _utils: object) -> Verdict:
                raise TestFailure("gate remained closed")

            async def _passes(self, _utils: object) -> Verdict:
                return PASS

        result = await Suite(name="rack").run(env={})

        sequence = result.summary_tree.children[0]
        failed, pending_group = sequence.children
        pending_leaf = pending_group.children[0]

        assert result.state is State.FAIL
        assert sequence.type is NodeType.TEST_SEQUENCE
        assert failed.state is State.FAIL
        assert pending_group.state is None
        assert pending_leaf.state is None
        assert pending_group.type is NodeType.TEST_GROUP
        assert pending_leaf.type is NodeType.TEST_CASE

    async def test_summary_tree_links_every_owning_node_to_its_timeline(
        self, tmp_path: Path
    ) -> None:
        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self._gate = self.test("failing gate", self._fails)
                self._rolled = self.sequence(
                    "rolled up", [self.test("inline", self._passes)], discrete_log_file=False
                )
                self._unreached = self.parallel(
                    "unreached group", [self.test("unreached leaf", self._passes)]
                )
                self._ordered = self.sequence(
                    "ordered claims", [self._rolled, self._gate, self._unreached]
                )

            def build_suite(self) -> LitmusTest:
                return self._ordered

            async def _fails(self, _utils: object) -> Verdict:
                raise TestFailure("gate remained closed")

            async def _passes(self, _utils: object) -> Verdict:
                return PASS

        suite = Suite(name="rack")
        result = await suite.run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        root = result.summary_tree
        sequence = root.children[0]
        rolled, failed, pending_group = sequence.children
        assert result.state is State.FAIL
        assert suite.log_path is not None
        run_root = suite.log_path.parent.parent

        assert root.log_path is not None
        assert sequence.log_path is not None
        assert not Path(root.log_path).is_absolute()
        assert "\\" not in root.log_path
        assert run_root / root.log_path == suite.log_path
        assert run_root / sequence.log_path == suite._ordered.log_path
        assert (run_root / sequence.log_path).is_file()

        assert failed.state is State.FAIL
        assert failed.log_path is None
        assert rolled.state is State.PASS
        assert rolled.log_path is None
        assert rolled.children[0].log_path is None
        assert pending_group.state is None
        assert pending_group.log_path is None
        assert pending_group.children[0].log_path is None


class TestInspectorJsonContract:
    @pytest.mark.parametrize("terminal_state", [State.PASS, State.FAIL, State.ABORTED])
    async def test_run_persists_root_result_and_final_timeline_pointer(
        self,
        terminal_state: State,
        tmp_path: Path,
    ) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("terminal", self._terminal)

            async def _terminal(self, _utils: object) -> Verdict:
                if terminal_state is State.PASS:
                    return PASS
                raise TestFailure("intentional failure")

        async def abort(_context: object) -> None:
            raise RuntimeError("MES unavailable")

        suite = Suite(name="rack")
        if terminal_state is State.ABORTED:
            result = await suite.run(
                env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)},
                before_each=abort,
                allow_unpaired_hooks=True,
            )
        else:
            result = await suite.run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is terminal_state
        assert suite.log_path is not None
        run_root = suite.log_path.parent.parent
        result_path = run_root / "result.json"
        assert json.loads(result_path.read_text(encoding="utf-8")) == result.to_json()

        records = [
            json.loads(line) for line in suite.log_path.read_text(encoding="utf-8").splitlines()
        ]
        pointer = records[-1]
        assert pointer["message"] == f"run result persisted -> {result_path}"
        assert pointer["scope"] == ["rack"]
        assert pointer["artifact_name"] == "result"
        assert pointer["artifact_path"] == str(result_path)
        assert pointer["artifact_type"] == "run-result"
        assert str(pointer["artifact_timestamp"]).endswith("Z")

    async def test_result_publication_atomically_replaces_and_preserves_prior_on_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def make_result(state: State) -> RunResult:
            tree = SummaryTreeNode(
                name="rack",
                type=NodeType.TEST_SUITE,
                state=state,
            )
            return RunResult(
                slot=Slot.TEST_METHOD,
                node="rack",
                state=state,
                ancestor_names=["rack"],
                bread_crumbs="rack",
                summary_tree=tree,
                type=NodeType.TEST_SUITE,
            )

        store = _ArtifactStore(tmp_path)
        first = make_result(State.PASS)
        replacement = make_result(State.FAIL)

        result_path = await store.persist_result(first)
        assert json.loads(result_path.read_text(encoding="utf-8"))["state"] == "Pass"
        assert await store.persist_result(replacement) == result_path
        prior = result_path.read_bytes()
        assert json.loads(prior)["state"] == "Fail"

        def fail_replace(_source: Path, _destination: Path) -> None:
            raise OSError("publication failed")

        monkeypatch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError, match="publication failed"):
            await store.persist_result(first)

        assert result_path.read_bytes() == prior
        temporary_files = await asyncio.to_thread(lambda: list(tmp_path.glob(".*.result.tmp")))
        assert temporary_files == []

    async def test_failed_result_publication_leaves_no_partial_file_or_pointer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._passes)

            async def _passes(self, _utils: object) -> Verdict:
                return PASS

        def fail_replace(_source: Path, _destination: Path) -> None:
            raise OSError("publication failed")

        monkeypatch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError, match="publication failed"):
            await Suite(name="rack").run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        run_roots = await asyncio.to_thread(lambda: list(tmp_path.iterdir()))
        assert len(run_roots) == 1
        run_root = run_roots[0]
        assert not (run_root / "result.json").exists()
        temporary_files = await asyncio.to_thread(lambda: list(run_root.glob(".*.result.tmp")))
        assert temporary_files == []
        timeline_paths = await asyncio.to_thread(lambda: list(run_root.rglob("timeline.jsonl")))
        records = [
            json.loads(line)
            for path in timeline_paths
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        assert not any(record.get("artifact_type") == "run-result" for record in records)

    async def test_cancellation_waits_for_result_and_pointer_publication(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._passes)

            async def _passes(self, _utils: object) -> Verdict:
                return PASS

        persistence_started = asyncio.Event()
        persistence_release = threading.Event()
        loop = asyncio.get_running_loop()
        original = _ArtifactStore._persist_result_sync  # pyright: ignore[reportPrivateUsage]

        def blocked_persistence(store: _ArtifactStore, result: RunResult) -> Path:
            loop.call_soon_threadsafe(persistence_started.set)
            if not persistence_release.wait(timeout=2):
                raise TimeoutError("test did not release result persistence")
            return original(store, result)

        monkeypatch.setattr(_ArtifactStore, "_persist_result_sync", blocked_persistence)
        suite = Suite(name="rack")
        running = asyncio.create_task(suite.run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)}))
        await persistence_started.wait()
        try:
            running.cancel()
            await asyncio.sleep(0)
            assert not running.done()
            running.cancel()
            await asyncio.sleep(0)
            assert not running.done()
        finally:
            persistence_release.set()

        with pytest.raises(asyncio.CancelledError):
            await running

        assert suite.log_path is not None
        run_root = suite.log_path.parent.parent
        result_path = run_root / "result.json"
        assert json.loads(result_path.read_text(encoding="utf-8"))["state"] == "Pass"
        records = [
            json.loads(line) for line in suite.log_path.read_text(encoding="utf-8").splitlines()
        ]
        assert records[-1]["artifact_path"] == str(result_path)
        assert records[-1]["artifact_type"] == "run-result"

    async def test_root_result_serializes_the_full_tree_and_failure(self) -> None:
        class LiveHandle:
            def __str__(self) -> str:
                return "ssh://fixture"

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("voltage", self._fails)

            async def _fails(self, _utils: object) -> Verdict:
                raise TestFailure(
                    "rail outside tolerance",
                    expected={"volts": 3.3},
                    received=LiveHandle(),
                )

        suite = Suite(name="power")
        result = await suite.run(env={"LOT": "42"})
        payload = json.loads(json.dumps(result.to_json()))

        assert payload["format_version"] == 1
        assert payload["state"] == "Fail"
        assert payload["failure"]["received"] == "ssh://fixture"
        assert payload["ancestor_names"] == ["power"]
        assert payload["bread_crumbs"] == "power"
        assert payload["env"] == {"LOT": "42"}
        assert payload["physical_location"] == "unknown"
        assert payload["run_data"] == {"measurements": []}
        leaf = payload["summary_tree"]["children"][0]
        assert leaf["name"] == "voltage"
        assert leaf["failure"]["fail_message"] == "rail outside tolerance"
        assert suite.log_path is not None
        run_root = suite.log_path.parent.parent
        assert run_root / payload["summary_tree"]["log_path"] == suite.log_path
        assert leaf["log_path"] is None

    def test_external_integration_failure_is_json_safe_without_an_encoder(self) -> None:
        failure = ExternalIntegrationFailure("after_each", RuntimeError("MES unavailable"))
        tree = SummaryTreeNode(
            name="rack",
            type=NodeType.TEST_SUITE,
            state=State.ABORTED,
            failure=failure,
        )
        result = RunResult(
            slot=Slot.AFTER_EACH,
            node="rack",
            state=State.ABORTED,
            failure=failure,
            ancestor_names=["rack"],
            bread_crumbs="rack",
            summary_tree=tree,
            type=NodeType.TEST_SUITE,
        )

        payload = json.loads(json.dumps(result.to_json()))

        assert payload["run_data"] == {"measurements": []}
        assert payload["failure"] == {
            "name": "ExternalIntegrationFailure",
            "fail_message": (
                "external integration hook after_each() threw — test run halted: MES unavailable"
            ),
            "state": "Aborted",
            "hook": "after_each",
            "cause": {"name": "RuntimeError", "message": "MES unavailable"},
        }

    def test_operator_interrupt_is_json_safe_without_an_encoder(self) -> None:
        failure = OperatorInterrupt(2)
        tree = SummaryTreeNode(
            name="rack",
            type=NodeType.TEST_SUITE,
            state=State.ABORTED,
            failure=failure,
        )
        result = RunResult(
            slot=Slot.TEST_METHOD,
            node="rack",
            state=State.ABORTED,
            failure=failure,
            ancestor_names=["rack"],
            bread_crumbs="rack",
            summary_tree=tree,
            type=NodeType.TEST_SUITE,
        )

        payload = json.loads(json.dumps(result.to_json()))

        assert payload["failure"] == {
            "name": "OperatorInterrupt",
            "fail_message": "operator interrupted the test run (SIGINT)",
            "state": "Aborted",
            "operator": True,
            "signal": "SIGINT",
        }
