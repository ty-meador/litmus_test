"""Saved tree navigation preserves the claims and their evidence ownership."""

import json
from pathlib import Path

import pytest

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict
from litmus.inspector_artifacts import ArtifactDiagnostic
from litmus.inspector_tree import InspectorTree, TreeReader
from litmus.run_data import JsonObject, JsonValue


def _node(name: str, **changes: JsonValue) -> JsonObject:
    return {
        "name": name,
        "type": "test",
        "state": "Pass",
        "failure": None,
        "timeout": None,
        "log_path": None,
        "children": [],
        "evidence": None,
        "evidence_declared": None,
        **changes,
    }


def _save(root: Path, node: JsonObject) -> Path:
    path = root / "result.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "state": "Aborted",
                "failure": None,
                "timeout": None,
                "test_case": node["name"],
                "type": "suite",
                "ancestor_names": [node["name"]],
                "bread_crumbs": node["name"],
                "summary_tree": node,
                "physical_location": "unknown",
                "env": {},
                "run_data": {"measurements": []},
                "future": {"keep": True},
            }
        )
    )
    return path


class TestInspectorTree:
    async def test_declaration_order_name_components_and_duplicate_names_stay_distinct(
        self, tmp_path: Path
    ) -> None:
        node = _node(
            "rack/a",
            children=[
                _node("same", children=[_node("leaf/b")]),
                _node("same"),
            ],
        )
        path = _save(tmp_path, node)
        original = path.read_bytes()
        tree = await TreeReader().read(path)
        assert isinstance(tree, InspectorTree)
        assert [row.ancestor_names for row in tree.rows] == [
            ("rack/a",),
            ("rack/a", "same"),
            ("rack/a", "same", "leaf/b"),
            ("rack/a", "same"),
        ]
        assert [row.index for row in tree.rows] == [0, 1, 2, 3]
        assert [row.parent_index for row in tree.rows] == [None, 0, 1, 0]
        assert tree.result["future"] == {"keep": True}
        assert tree.rows[0].record == node
        tree.select(3)
        assert tree.selected is tree.rows[3]
        assert path.read_bytes() == original

    async def test_navigation_and_failure_projection_keep_both_peer_states_without_wrapping(
        self, tmp_path: Path
    ) -> None:
        path = _save(
            tmp_path,
            _node(
                "rack",
                state="Aborted",
                children=[
                    _node("good"),
                    _node("bad", state="Fail"),
                    _node("pending", state=None),
                    _node("no verdict", state="Aborted"),
                    _node("last"),
                ],
            ),
        )
        tree = await TreeReader().read(path)
        assert isinstance(tree, InspectorTree)
        assert [row.index for row in tree.failures] == [0, 2, 4]
        assert [row.state for row in tree.failures] == ["Aborted", "Fail", "Aborted"]
        assert tree.selected.index == 0
        tree.previous_failure()
        assert tree.selected.index == 0
        for expected in (2, 4, 4):
            tree.next_failure()
            assert tree.selected.index == expected
        tree.move(1)
        assert tree.selected.index == 5
        tree.next_failure()
        assert tree.selected.index == 5
        for expected in (4, 2, 0, 0):
            tree.previous_failure()
            assert tree.selected.index == expected
        tree.move(-100)
        assert tree.selected.index == 0
        tree.move(100)
        assert tree.selected.index == 5
        for index in (-1, 6):
            with pytest.raises(IndexError, match="tree row"):
                tree.select(index)
            assert tree.selected.index == 5

    async def test_pending_nodes_never_borrow_chronology_and_rollup_uses_nearest_owner(
        self, tmp_path: Path
    ) -> None:
        path = _save(
            tmp_path,
            _node(
                "rack",
                log_path="rack/timeline.jsonl",
                children=[
                    _node("rolled", children=[_node("leaf")]),
                    _node("owner", log_path="dut/timeline.jsonl", children=[_node("leaf")]),
                    _node("pending", state=None, log_path="misleading/timeline.jsonl"),
                ],
            ),
        )
        tree = await TreeReader().read(path)
        assert isinstance(tree, InspectorTree)
        for index, owner in enumerate((0, 0, 0, 3, 3, None)):
            tree.select(index)
            assert tree.log_owner is (None if owner is None else tree.rows[owner])
        tree.select(4)
        assert tree.log_owner is not None
        assert tree.log_owner.record["log_path"] == "dut/timeline.jsonl"
        assert not (tmp_path / "dut/timeline.jsonl").exists()

    async def test_no_owner_and_no_failures_remain_absent(self, tmp_path: Path) -> None:
        tree = await TreeReader().read(_save(tmp_path, _node("root")))
        assert isinstance(tree, InspectorTree)
        assert tree.log_owner is None
        assert tree.failures == ()
        tree.next_failure()
        tree.previous_failure()
        tree.move(1)
        assert tree.selected.index == 0

    async def test_missing_readings_use_presence_and_preserve_failure_timeout_and_additions(
        self, tmp_path: Path
    ) -> None:
        node = _node(
            "voltage",
            state="Fail",
            failure={
                "name": "TestFailure",
                "state": "Fail",
                "fail_message": "rail low",
                "cause": {"raw": "kept"},
            },
            timeout={
                "reason": "timeout",
                "declared": "fail_after",
                "budget_seconds": 1,
                "elapsed_seconds": 1,
                "clock": "voltage",
            },
            evidence={"zero": 0, "null": None, "false": False},
            evidence_declared=["zero", "missing", "null", "false", "last"],
            future={"reading": []},
        )
        tree = await TreeReader().read(_save(tmp_path, node))
        assert isinstance(tree, InspectorTree)
        assert tree.selected.not_taken == ("missing", "last")
        assert tree.selected.record == node
        node["evidence"] = None
        tree = await TreeReader().read(_save(tmp_path, node))
        assert isinstance(tree, InspectorTree)
        assert tree.selected.not_taken == ("zero", "missing", "null", "false", "last")

    @pytest.mark.parametrize(
        ("raw", "kind"),
        [
            ("{}", "unsupported-version"),
            ("[]", "invalid-record"),
            ('{"format_version":1}', "invalid-record"),
        ],
    )
    async def test_invalid_result_is_a_diagnostic_without_a_fabricated_tree(
        self, tmp_path: Path, raw: str, kind: str
    ) -> None:
        path = tmp_path / "result.json"
        path.write_text(raw)
        result = await TreeReader().read(path)
        assert isinstance(result, ArtifactDiagnostic)
        assert result.kind == kind
        assert path.read_text() == raw

    async def test_missing_result_is_unavailable(self, tmp_path: Path) -> None:
        result = await TreeReader().read(tmp_path / "result.json")
        assert isinstance(result, ArtifactDiagnostic)
        assert result.kind == "unavailable"

    async def test_recorded_seventy_dut_rack_is_navigable_after_the_run_moves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Device(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("voltage", self._voltage)

            async def _voltage(self, utils: TestUtils) -> Verdict:
                if self.name != "dut-35":
                    return PASS({"volts": 3.3})
                raise TestFailure("rail low", evidence={"volts": 0})

        class Rack(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel("devices", [Device(name=f"dut-{i:02d}") for i in range(70)])

        rack = Rack(name="rack")
        result = await rack.run()
        assert rack.log_path is not None
        moved = tmp_path / "moved"
        rack.log_path.parent.parent.rename(moved)
        recorded = result.to_json()
        del rack, result

        def refuse_suite_execution(*args: object, **kwargs: object) -> None:
            raise AssertionError("inspection attempted suite execution")

        monkeypatch.setattr(LitmusTest, "run", refuse_suite_execution)
        tree = await TreeReader().read(moved / "result.json")
        assert isinstance(tree, InspectorTree)
        assert tree.result == recorded
        assert len(tree.rows) == 142
        assert [row.index for row in tree.failures] == [0, 1, 72, 73]
        for i in range(70):
            tree.select(3 + i * 2)
            assert tree.selected.ancestor_names == ("rack", "devices", f"dut-{i:02d}", "voltage")
            assert tree.selected.not_taken == ()
            owner = tree.log_owner
            assert owner is tree.rows[2 + i * 2]
            assert owner is not None
            assert owner.record["name"] == f"dut-{i:02d}"
