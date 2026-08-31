"""RunResult navigation and the stable JSON boundary for a future Inspector."""

import json
from typing import cast

import litmus
from litmus import (
    PASS,
    ExternalIntegrationFailure,
    LitmusTest,
    RunResult,
    SummaryTreeNode,
    TestFailure,
    Verdict,
)
from litmus.lifecycle import Slot, State
from litmus.results import NodeType


class TestRunResultNavigation:
    async def test_every_executed_result_carries_its_root_to_node_path(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.group(
                    "claims",
                    [
                        self.case("identity", self._passes),
                        self.case("firmware", self._passes),
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
                        self.case("failing gate", self._fails),
                        self.group(
                            "unreached group",
                            [self.case("unreached leaf", self._passes)],
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


class TestInspectorJsonContract:
    async def test_root_result_serializes_the_full_tree_and_failure(self) -> None:
        class LiveHandle:
            def __str__(self) -> str:
                return "ssh://fixture"

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("voltage", self._fails)

            async def _fails(self, _utils: object) -> Verdict:
                raise TestFailure(
                    "rail outside tolerance",
                    expected={"volts": 3.3},
                    received=LiveHandle(),
                )

        result = await Suite(name="power").run(env={"LOT": "42"})
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
