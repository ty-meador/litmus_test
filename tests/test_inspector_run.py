"""Saved-run relocation requires publication evidence, never a filename guess."""

import json
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from litmus import PASS, Artifact, LitmusTest, TestUtils, Verdict
from litmus.inspector_artifacts import ArtifactContent, ArtifactDiagnostic
from litmus.inspector_run import OriginDiagnostic, SavedRun, SavedRunReader
from litmus.inspector_timeline import TimelineDiagnostic, TimelineReader, TimelineRecord
from litmus.run_data import JsonObject, JsonValue


def _publication(**changes: JsonValue) -> JsonObject:
    return {
        "format_version": 1,
        "timestamp": "2026-09-08T05:00:00Z",
        "scope": ["rack/a"],
        "level": "info",
        "message": "run result persisted",
        "artifact_name": "result",
        "artifact_type": "run-result",
        "artifact_timestamp": "2026-09-08T05:00:00Z",
        "artifact_path": "/original/run/result.json",
        **changes,
    }


def _save(root: Path, *, log_path: str | None = "rack/timeline.jsonl") -> Path:
    result: JsonObject = {
        "format_version": 1,
        "state": "Pass",
        "failure": None,
        "timeout": None,
        "test_case": "rack/a",
        "type": "suite",
        "ancestor_names": ["rack/a"],
        "bread_crumbs": "rack/a",
        "physical_location": "unknown",
        "env": {},
        "run_data": {"measurements": []},
        "summary_tree": {
            "name": "rack/a",
            "type": "suite",
            "state": "Pass",
            "failure": None,
            "timeout": None,
            "log_path": log_path,
            "children": [],
            "evidence": None,
            "evidence_declared": None,
        },
    }
    path = root / "result.json"
    path.write_text(json.dumps(result))
    timeline = root / "rack/timeline.jsonl"
    timeline.parent.mkdir(exist_ok=True)
    timeline.write_text(json.dumps(_publication()) + "\n")
    return path


class TestSavedRunReader:
    @pytest.mark.parametrize("state", ["Pass", "Fail", "Aborted"])
    async def test_origin_does_not_reinterpret_the_recorded_verdict(
        self, tmp_path: Path, state: str
    ) -> None:
        path = _save(tmp_path)
        result = cast(JsonObject, json.loads(path.read_text()))
        result["state"] = state
        cast(JsonObject, result["summary_tree"])["state"] = state
        path.write_text(json.dumps(result))

        saved = await SavedRunReader().read(path)

        assert isinstance(saved, SavedRun)
        assert saved.origin == PurePosixPath("/original/run")
        assert saved.tree.result == result
        assert saved.tree.selected.state == state

    async def test_disagreeing_result_and_tree_roots_cannot_establish_origin(
        self, tmp_path: Path
    ) -> None:
        path = _save(tmp_path)
        result = cast(JsonObject, json.loads(path.read_text()))
        result["ancestor_names"] = ["other"]
        path.write_text(json.dumps(result))

        saved = await SavedRunReader().read(path)

        assert isinstance(saved, SavedRun)
        assert isinstance(saved.origin, OriginDiagnostic)
        assert saved.tree.result == result

    @pytest.mark.parametrize("selection", ["symlink-loop", "null-byte"])
    async def test_unresolvable_operator_selection_is_an_unavailable_result(
        self, tmp_path: Path, selection: str
    ) -> None:
        path = tmp_path / "bad\x00name"
        if selection == "symlink-loop":
            path = tmp_path / "loop"
            path.symlink_to(path)

        loaded = await SavedRunReader().read(path)

        assert isinstance(loaded, ArtifactDiagnostic)
        assert loaded.kind == "unavailable"
        assert loaded.reference == str(path)

    async def test_ordinary_and_child_records_do_not_hide_a_final_root_publication(
        self, tmp_path: Path
    ) -> None:
        path = _save(tmp_path)
        ordinary = _publication(scope=["rack/a", "child"], artifact_type="survey")
        (tmp_path / "rack/timeline.jsonl").write_text(
            json.dumps(ordinary) + "\n" + json.dumps(_publication()) + "\n"
        )

        saved = await SavedRunReader().read(path)

        assert isinstance(saved, SavedRun)
        assert saved.origin == PurePosixPath("/original/run")
        assert saved.root_timeline is not None
        assert len(saved.root_timeline.rows) == 2

    @pytest.mark.parametrize("selection", ["direct", "symlink", "parent", "relative"])
    async def test_operator_selection_establishes_the_resolved_run_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
    ) -> None:
        root = tmp_path / "moved"
        root.mkdir()
        path = _save(root)
        alias = tmp_path / "latest"
        alias.symlink_to(root, target_is_directory=True)
        monkeypatch.chdir(tmp_path)
        paths = {
            "direct": path,
            "symlink": alias / "result.json",
            "parent": root / ".." / "moved/result.json",
            "relative": Path("moved/result.json"),
        }

        saved = await SavedRunReader().read(paths[selection])

        assert isinstance(saved, SavedRun)
        assert saved.run_root == root
        assert saved.origin == PurePosixPath("/original/run")
        assert saved.tree.selected.ancestor_names == ("rack/a",)
        loaded = await saved.artifacts.read("/original/run/result.json", kind="result")
        assert isinstance(loaded, ArtifactContent)
        assert loaded.data == saved.tree.result
        assert loaded.path == path

    @pytest.mark.parametrize("raw", [None, "{}", '{"format_version": 2}'])
    async def test_missing_or_invalid_result_returns_its_diagnostic_before_reading_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str | None
    ) -> None:
        path = tmp_path / "result.json"
        if raw is not None:
            path.write_text(raw)

        async def forbidden(self: TimelineReader, run_root: Path, reference: str) -> None:
            pytest.fail("an invalid result must not select a timeline")

        monkeypatch.setattr(TimelineReader, "read_reference", forbidden)
        result = await SavedRunReader().read(path)
        assert isinstance(result, ArtifactDiagnostic)
        assert result.reference == str(path)

    @pytest.mark.parametrize("link", [None, "missing.jsonl", "../outside.jsonl", "/outside.jsonl"])
    async def test_missing_or_escaping_root_log_keeps_the_tree_without_guessing_origin(
        self, tmp_path: Path, link: str | None
    ) -> None:
        path = _save(tmp_path, log_path=link)
        saved = await SavedRunReader().read(path)
        assert isinstance(saved, SavedRun)
        assert isinstance(saved.origin, OriginDiagnostic)
        assert saved.origin.reference == link
        assert saved.tree.selected.state == "Pass"
        if link is not None:
            assert saved.root_timeline is not None
            assert isinstance(saved.root_timeline.rows[0], TimelineDiagnostic)

    @pytest.mark.parametrize(
        "changes",
        [
            {"scope": ["rack", "a"]},
            {"scope": ["rack/a", "child"]},
            {"artifact_name": "another-result"},
            {"artifact_type": "survey"},
            {"artifact_path": "relative/result.json"},
            {"artifact_path": "/original/run/../result.json"},
            {"artifact_path": "/original/run/other.json"},
            {"artifact_path": "/original/\x00/result.json"},
        ],
    )
    async def test_misplaced_or_invalid_publication_never_establishes_a_mapping(
        self, tmp_path: Path, changes: dict[str, JsonValue]
    ) -> None:
        path = _save(tmp_path)
        (tmp_path / "rack/timeline.jsonl").write_text(json.dumps(_publication(**changes)))
        saved = await SavedRunReader().read(path)
        assert isinstance(saved, SavedRun)
        assert isinstance(saved.origin, OriginDiagnostic)
        assert saved.origin.reference == "rack/timeline.jsonl"
        assert saved.tree.selected.state == "Pass"
        loaded = await saved.artifacts.read("/original/run/result.json", kind="result")
        assert isinstance(loaded, ArtifactDiagnostic)
        assert loaded.kind == "unavailable"

    @pytest.mark.parametrize(
        "records",
        [
            [],
            [_publication(), _publication()],
            [_publication(artifact_path="/another/run/result.json"), _publication()],
            [_publication(), {"format_version": 1}],
            [{"format_version": 1}, _publication()],
            [_publication(), _publication(artifact_type="survey")],
        ],
    )
    async def test_absent_ambiguous_damaged_or_nonfinal_publication_is_explicit(
        self, tmp_path: Path, records: list[JsonObject]
    ) -> None:
        path = _save(tmp_path)
        (tmp_path / "rack/timeline.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records)
        )
        saved = await SavedRunReader().read(path)
        assert isinstance(saved, SavedRun)
        assert isinstance(saved.origin, OriginDiagnostic)
        assert saved.origin.message
        assert saved.root_timeline is not None
        assert len(saved.root_timeline.rows) == len(records)

    async def test_truncated_publication_can_be_reopened_after_repair_without_cached_origin(
        self, tmp_path: Path
    ) -> None:
        path = _save(tmp_path)
        timeline = tmp_path / "rack/timeline.jsonl"
        complete = timeline.read_bytes()
        damaged = complete[:-9]
        timeline.write_bytes(damaged)
        reader = SavedRunReader()
        saved = await reader.read(path)
        assert isinstance(saved, SavedRun)
        assert isinstance(saved.origin, OriginDiagnostic)
        assert timeline.read_bytes() == damaged
        assert saved.root_timeline is not None
        diagnostic = saved.root_timeline.rows[-1]
        assert isinstance(diagnostic, TimelineDiagnostic)
        assert diagnostic.kind == "truncated"

        timeline.write_bytes(complete.rstrip(b"\n"))
        reopened = await reader.read(path)
        assert isinstance(reopened, SavedRun)
        assert reopened.origin == PurePosixPath("/original/run")

    async def test_missing_mapping_still_allows_evidence_already_beneath_the_opened_root(
        self, tmp_path: Path
    ) -> None:
        path = _save(tmp_path, log_path=None)
        saved = await SavedRunReader().read(path)
        assert isinstance(saved, SavedRun)
        assert isinstance(saved.origin, OriginDiagnostic)
        assert isinstance(await saved.artifacts.read(str(path), kind="result"), ArtifactContent)

    async def test_discovers_mapping_from_a_moved_seventy_dut_run_without_live_objects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Device(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("snapshot", self._snapshot)

            async def _snapshot(self, utils: TestUtils) -> Verdict:
                path = await utils.capture_artifact(
                    Artifact(name="voltage", type="survey", data={"dut": self.name})
                )
                if path:
                    return PASS
                raise AssertionError("capture did not settle")

        class Rack(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel("devices", [Device(name=f"dut-{i:02d}") for i in range(70)])

        rack = Rack(name="rack")
        result = await rack.run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path / "station")})
        assert rack.log_path is not None
        original = rack.log_path.parent.parent
        moved = tmp_path / "moved"
        original.rename(moved)
        before = {p.relative_to(moved): p.read_bytes() for p in moved.rglob("*") if p.is_file()}

        async def forbidden(self: LitmusTest) -> None:
            pytest.fail("saved inspection cannot execute a suite")

        monkeypatch.setattr(LitmusTest, "run", forbidden)
        saved = await SavedRunReader().read(moved / "result.json")
        assert isinstance(saved, SavedRun)
        assert saved.origin == PurePosixPath(original)
        assert saved.tree.result == result.to_json()
        captures: list[str] = []
        results: list[JsonObject] = []
        for node in saved.tree.rows:
            reference = node.record["log_path"]
            if reference is None:
                continue
            timeline = await TimelineReader().read_reference(saved.run_root, cast(str, reference))
            for row in timeline.rows:
                assert isinstance(row, TimelineRecord)
                if "artifact_path" not in row.data:
                    continue
                kind = "result" if row.data["artifact_type"] == "run-result" else "artifact"
                loaded = await saved.artifacts.read(cast(str, row.data["artifact_path"]), kind=kind)
                assert isinstance(loaded, ArtifactContent)
                assert loaded.path.is_relative_to(moved)
                if kind == "result":
                    results.append(loaded.data)
                else:
                    captures.append(cast(str, cast(JsonObject, loaded.data["data"])["dut"]))
        assert sorted(captures) == [f"dut-{i:02d}" for i in range(70)]
        assert results == [saved.tree.result]
        assert {
            p.relative_to(moved): p.read_bytes() for p in moved.rglob("*") if p.is_file()
        } == before
