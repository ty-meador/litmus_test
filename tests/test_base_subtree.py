"""``LitmusTest.run(subtree=...)``: one declared node through the sealed lifecycle.

Ty's ruling of 2026-09-10 (issue #89): the Inspector's Re-Run tab executes a node with
the code that ran in production. The framework keeps ownership of configure and cleanup
on the path to that node, and leaves every other leaf pending. These tests hold the
lifecycle to that shape independently of any screen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from litmus import PASS, ContractViolation, LitmusTest, TestFailure, TestUtils, Verdict
from litmus.lifecycle import State
from litmus.results import RunResult, SummaryTreeNode


def _states(result: RunResult) -> dict[str, State | None]:
    """Every node's state by name; the bench suite's names are unique."""
    states: dict[str, State | None] = {}

    def walk(node: SummaryTreeNode) -> None:
        states[node.name] = node.state
        for child in node.children:
            walk(child)

    walk(result.summary_tree)
    return states


class _Bench(LitmusTest):
    """Records which slots ran, so a pruned run proves who executed and who did not."""

    def __init__(self, name: str, trace: list[str]) -> None:
        super().__init__(name=name)
        self.trace = trace

    async def configure(self, utils: TestUtils) -> dict[str, object]:
        self.trace.append("configure")
        return {}

    def build_suite(self) -> LitmusTest:
        return self.sequence(
            "steps",
            [
                self.test("a", self._claim, "a"),
                self.parallel(
                    "pair", [self.test("b", self._claim, "b"), self.test("c", self._claim, "c")]
                ),
                self.test("d", self._claim, "d"),
            ],
        )

    async def _claim(self, utils: TestUtils, name: str) -> Verdict:
        self.trace.append(name)
        return PASS

    async def cleanup(self, utils: TestUtils) -> None:
        self.trace.append("cleanup")


class _FailsFirst(LitmusTest):
    def build_suite(self) -> LitmusTest:
        return self.sequence(
            "steps", [self.test("broken", self._broken), self.test("fine", self._fine)]
        )

    async def _broken(self, utils: TestUtils) -> Verdict:
        raise TestFailure("always")

    async def _fine(self, utils: TestUtils) -> Verdict:
        return PASS


class TestSubtreeSelection:
    async def test_a_leaf_runs_with_its_ancestors_lifecycle_and_siblings_pending(self) -> None:
        trace: list[str] = []
        result = await _Bench("bench", trace).run(subtree=(0, 1, 1))

        # The root configured and cleaned up because those are its own slots; the
        # selected leaf ran; no other leaf was touched.
        assert trace == ["configure", "c", "cleanup"]
        assert result.state is State.PASS
        assert _states(result) == {
            "bench": State.PASS,
            "steps": State.PASS,
            "a": None,
            "pair": State.PASS,
            "b": None,
            "c": State.PASS,
            "d": None,
        }

    async def test_a_composite_runs_everything_beneath_it(self) -> None:
        trace: list[str] = []
        result = await _Bench("bench", trace).run(subtree=(0, 1))

        assert trace[0] == "configure"
        assert trace[-1] == "cleanup"
        assert set(trace[1:-1]) == {"b", "c"}
        states = _states(result)
        assert (states["a"], states["d"]) == (None, None)
        assert (states["b"], states["c"], states["pair"]) == (State.PASS,) * 3

    async def test_the_root_position_is_the_whole_run(self) -> None:
        trace: list[str] = []
        result = await _Bench("bench", trace).run(subtree=())

        assert trace == ["configure", "a", "b", "c", "d", "cleanup"] or set(trace) == {
            "configure",
            "a",
            "b",
            "c",
            "d",
            "cleanup",
        }
        assert all(state is State.PASS for state in _states(result).values())

    async def test_a_skipped_failing_sibling_does_not_stop_the_sequence(self) -> None:
        result = await _FailsFirst(name="bench").run(subtree=(0, 1))

        assert result.state is State.PASS
        assert _states(result) == {
            "bench": State.PASS,
            "steps": State.PASS,
            "broken": None,
            "fine": State.PASS,
        }

    async def test_a_position_outside_the_declared_tree_is_refused_before_anything_runs(
        self, tmp_path: Path
    ) -> None:
        trace: list[str] = []
        root = tmp_path / "never"

        with pytest.raises(ContractViolation, match=r"names child 7 of 'steps'.*3 children"):
            await _Bench("bench", trace).run(
                env={"LITMUS_ARTIFACT_ROOT": str(root)}, subtree=(0, 7)
            )

        assert trace == []
        assert not root.exists()

    async def test_a_leaf_has_no_children_to_select(self) -> None:
        with pytest.raises(ContractViolation, match=r"names child 0 of 'a'.*0 children"):
            await _Bench("bench", []).run(subtree=(0, 0, 0))


class TestSuiteTarget:
    async def test_the_target_is_recorded_when_given_and_null_otherwise(self) -> None:
        recorded = await _Bench("bench", []).run(suite_target="/bench.py::Bench")
        unrecorded = await _Bench("bench", []).run()

        assert recorded.suite_target == "/bench.py::Bench"
        assert recorded.to_json()["suite_target"] == "/bench.py::Bench"
        assert unrecorded.to_json()["suite_target"] is None
