"""Paging and filtering must never conceal damage or renumber a capture."""

import json
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from litmus import PASS, Artifact, LitmusTest, TestUtils, Verdict
from litmus.inspector_artifacts import ArtifactContent, ArtifactDiagnostic, ArtifactReader
from litmus.inspector_run import SavedRun, SavedRunReader
from litmus.inspector_selection import InspectorSelection
from litmus.inspector_timeline import (
    TimelineDiagnostic,
    TimelineRead,
    TimelineReader,
    TimelineRecord,
)
from litmus.inspector_timeline_view import TimelineEntry, TimelineView
from litmus.run_data import JsonValue


def _record(number: int, **changes: JsonValue) -> TimelineRecord:
    return TimelineRecord(
        number,
        {
            "format_version": 1,
            "timestamp": "2026-09-09T12:00:00Z",
            "scope": ["rack", f"dut/{number}"],
            "level": "info",
            "message": f"line {number}",
            **changes,
        },
    )


def _pointer(number: int, **changes: JsonValue) -> TimelineRecord:
    return _record(
        number,
        artifact_name="reading",
        artifact_type="survey",
        artifact_path=f"/original/run/dut-{number}/reading.json",
        artifact_timestamp="2026-09-09T12:00:00Z",
        **changes,
    )


class TestTimelineView:
    def test_pages_open_at_tail_and_clamp_without_losing_physical_rows(self) -> None:
        records = tuple(_record(number) for number in range(1, 6))
        timeline = TimelineRead(None, records, "rack/timeline.jsonl")
        view = TimelineView(timeline, page_size=2)
        assert view.timeline is timeline
        assert view.page_count == 3
        assert view.page_index == 2
        assert not view.errors_only
        assert view.rows == (TimelineEntry(records[4], None),)
        view.next_page()
        assert view.page_index == 2
        view.previous_page()
        assert view.rows == tuple(TimelineEntry(row, None) for row in records[2:4])
        view.previous_page()
        view.previous_page()
        assert view.page_index == 0
        assert view.rows == tuple(TimelineEntry(row, None) for row in records[:2])

    def test_filter_keeps_all_diagnostics_and_numbers_from_complete_chronology(self) -> None:
        info = _pointer(1)
        failure = _pointer(3, level="error", extra={"untouched": [None, 0]})
        diagnostics = (
            TimelineDiagnostic(2, "invalid-record", "bad record"),
            TimelineDiagnostic(4, "unsupported-version", "future version"),
            TimelineDiagnostic(5, "truncated", "unfinished tail"),
            TimelineDiagnostic(None, "unavailable", "read interrupted"),
        )
        timeline = TimelineRead(None, (info, diagnostics[0], failure, *diagnostics[1:]))
        before = deepcopy(timeline)
        view = TimelineView(timeline, page_size=2)
        assert view.page_index == 2
        view.toggle_errors()
        assert view.errors_only
        assert view.page_index == 0
        assert view.rows == (diagnostics[0], TimelineEntry(failure, 2))
        assert view.artifact(1) is info
        assert view.artifact(2) is failure
        view.next_page()
        assert view.rows == diagnostics[1:3]
        view.next_page()
        assert view.rows == (diagnostics[3],)
        view.toggle_errors()
        assert not view.errors_only
        assert view.page_index == 0
        assert view.rows == (TimelineEntry(info, 1), diagnostics[0])
        assert timeline == before

    @pytest.mark.parametrize(
        ("level", "visible"),
        [
            ("error", True),
            ("critical", True),
            ("ERROR", True),
            ("CRITICAL", True),
            ("warning", False),
            ("info", False),
            ("debug", False),
            ("custom", False),
        ],
    )
    def test_errors_filter_uses_python_levels_without_inventing_custom_severity(
        self, level: str, visible: bool
    ) -> None:
        record = _record(1, level=level)
        view = TimelineView(TimelineRead(None, (record,)), page_size=5)
        view.toggle_errors()
        assert view.rows == ((TimelineEntry(record, None),) if visible else ())
        assert view.page_count == 1

    def test_pointer_lookup_is_global_and_ignores_message_paths_and_duplicate_filenames(
        self,
    ) -> None:
        pointers = tuple(_pointer(number) for number in range(1, 12))
        message = _record(12, message="capture at /somewhere/reading.json [12]")
        view = TimelineView(TimelineRead(None, (*pointers, message)), page_size=1)
        assert view.artifact_count == 11
        for number, record in enumerate(pointers, 1):
            assert view.artifact(number) is record
        assert view.rows == (TimelineEntry(message, None),)
        view.previous_page()
        assert view.rows == (TimelineEntry(pointers[-1], 11),)
        view.toggle_errors()
        assert view.rows == ()
        assert view.artifact(11) is pointers[-1]
        assert view.artifact(0) is None
        assert view.artifact(-1) is None
        assert view.artifact(12) is None
        assert view.artifact(True) is None

    def test_empty_log_has_one_empty_page_without_fabricated_diagnostics(self) -> None:
        view = TimelineView(TimelineRead(None, ()), page_size=3)
        view.next_page()
        view.previous_page()
        view.toggle_errors()
        assert view.rows == ()
        assert view.page_count == 1
        assert view.page_index == 0
        assert view.artifact_count == 0
        assert view.artifact(1) is None

    def test_resize_clamps_page_and_never_changes_capture_targets(self) -> None:
        pointers = tuple(_pointer(number) for number in range(1, 6))
        view = TimelineView(TimelineRead(None, pointers), page_size=2)
        view.resize(4)
        assert view.page_count == 2
        assert view.page_index == 1
        assert view.rows == (TimelineEntry(pointers[-1], 5),)
        view.resize(1)
        assert view.page_index == 1
        assert view.rows == (TimelineEntry(pointers[1], 2),)
        assert view.artifact(5) is pointers[-1]

    @pytest.mark.parametrize("size", [0, -1, True, 1.5, "2"])
    def test_invalid_page_size_is_rejected_without_changing_current_page(
        self, size: object
    ) -> None:
        with pytest.raises(ValueError, match="positive integer"):
            TimelineView(TimelineRead(None, ()), page_size=cast(int, size))
        view = TimelineView(TimelineRead(None, (_record(1), _record(2))), page_size=1)
        with pytest.raises(ValueError, match="positive integer"):
            view.resize(cast(int, size))
        assert view.page_index == 1
        assert view.page_count == 2

    async def test_validated_empty_pointer_is_numbered_and_remains_unavailable(
        self, tmp_path: Path
    ) -> None:
        # The version-1 schema permits empty pointer strings. Keep the reference
        # visible and let the capture reader diagnose it, rather than dropping it.
        record = _pointer(1)
        record.data["artifact_path"] = ""
        path = tmp_path / "timeline.jsonl"
        path.write_text(json.dumps(record.data))
        timeline = await TimelineReader().read(path)
        view = TimelineView(timeline, page_size=1)
        assert isinstance(timeline.rows[0], TimelineRecord)
        assert view.artifact(1) is timeline.rows[0]
        capture = await ArtifactReader(tmp_path).read(
            cast(str, timeline.rows[0].data["artifact_path"])
        )
        assert isinstance(capture, ArtifactDiagnostic)
        assert capture.kind == "unavailable"
        assert capture.reference == ""

    async def test_moved_seventy_dut_run_preserves_numbered_captures_and_saved_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Device(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("voltage", self._voltage)

            async def _voltage(self, utils: TestUtils) -> Verdict:
                await utils.capture_artifact(
                    Artifact(name="reading", type="survey", data=self.name)
                )
                utils.logger.error("survey warning for %s", self.name)
                if self.name:
                    return PASS
                raise AssertionError("device name required")

        class Rack(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel("devices", [Device(name=f"dut-{i:02d}") for i in range(70)])

        rack = Rack(name="rack")
        await rack.run()
        assert rack.log_path is not None
        moved = tmp_path / "moved"
        rack.log_path.parent.parent.rename(moved)
        del rack

        def refuse_execution(*args: object, **kwargs: object) -> None:
            raise AssertionError("timeline presentation attempted suite execution")

        monkeypatch.setattr(LitmusTest, "run", refuse_execution)
        before = {path: path.read_bytes() for path in moved.rglob("*") if path.is_file()}
        saved = await SavedRunReader().read(moved / "result.json")
        assert isinstance(saved, SavedRun)
        selection = InspectorSelection(saved.tree, saved.run_root)
        for number in range(70):
            await selection.select(3 + number * 2)
            selected = selection.view
            assert selected.selected.state == "Pass"
            assert selected.timeline is not None
            view = TimelineView(selected.timeline, page_size=2)
            pointer = view.artifact(1)
            assert pointer is not None
            assert pointer.data["scope"] == ["rack", "devices", f"dut-{number:02d}", "voltage"]
            view.previous_page()
            view.toggle_errors()
            assert view.artifact(1) is pointer
            assert all(isinstance(row, TimelineEntry) for row in view.rows)
            assert any(
                isinstance(row, TimelineEntry) and row.record.data["level"] == "error"
                for row in view.rows
            )
            capture = await saved.artifacts.read(cast(str, pointer.data["artifact_path"]))
            assert isinstance(capture, ArtifactContent)
            assert capture.data["data"] == f"dut-{number:02d}"
            assert selected.selected.state == "Pass"
        assert saved.root_timeline is not None
        root_view = TimelineView(saved.root_timeline, page_size=1)
        publication = root_view.artifact(root_view.artifact_count)
        assert publication is not None
        assert publication.data["artifact_type"] == "run-result"
        result = await saved.artifacts.read(
            cast(str, publication.data["artifact_path"]), kind="result"
        )
        assert isinstance(result, ArtifactContent)
        assert result.data == saved.tree.result
        assert {path: path.read_bytes() for path in before} == before
