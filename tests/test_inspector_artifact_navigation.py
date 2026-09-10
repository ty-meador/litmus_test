"""Capture navigation preserves identity and never compares a capture with itself."""

from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from litmus import PASS, Artifact, LitmusTest, TestUtils, Verdict
from litmus.inspector_artifact_navigation import ArtifactNavigation
from litmus.inspector_artifacts import ArtifactContent, ArtifactDiagnostic, ArtifactReader
from litmus.inspector_run import SavedRun, SavedRunReader
from litmus.inspector_selection import InspectorSelection
from litmus.inspector_timeline import TimelineDiagnostic, TimelineRead, TimelineRecord
from litmus.inspector_timeline_view import TimelineView


def _view(*types: str) -> TimelineView:
    records = tuple(
        TimelineRecord(
            number * 2,
            {
                "format_version": 1,
                "timestamp": "2026-09-09T18:00:00Z",
                "scope": ["rack", f"dut/{number}"],
                "level": "info",
                "message": "capture",
                "artifact_name": "reading",
                "artifact_type": kind,
                "artifact_path": f"/original/run/{number}/reading.json",
                "artifact_timestamp": "2026-09-09T18:00:00Z",
                "extra": {"null": None, "zero": 0},
            },
        )
        for number, kind in enumerate(types, 1)
    )
    damage = TimelineDiagnostic(None, "unavailable", "incomplete read")
    return TimelineView(TimelineRead(None, (*records, damage)), page_size=1)


class TestArtifactNavigation:
    def test_groups_by_first_appearance_and_opens_exact_hidden_capture(self) -> None:
        timeline = _view("survey", "run-result", "survey", "other", "survey")
        before = deepcopy(timeline.timeline)
        timeline.toggle_errors()
        navigation = ArtifactNavigation(timeline, initial_number=5)
        assert navigation.types == ("survey", "run-result", "other")
        assert navigation.artifact_type == "survey"
        assert navigation.numbers == (1, 3, 5)
        assert navigation.current.number == 5
        assert navigation.current.record is timeline.artifact(5)
        assert navigation.current.kind == "artifact"
        assert navigation.comparison is None
        navigation.previous_artifact()
        assert navigation.current.number == 3
        timeline.resize(10)
        timeline.toggle_errors()
        assert navigation.current.record is timeline.artifact(3)
        assert timeline.timeline == before

    @pytest.mark.parametrize("number", [0, -1, 3, True, 1.5, "1"])
    def test_missing_capture_is_rejected_instead_of_opening_another(self, number: object) -> None:
        with pytest.raises(ValueError, match="capture number"):
            ArtifactNavigation(_view("survey", "survey"), initial_number=cast(int, number))

    def test_empty_timeline_cannot_open_a_modal(self) -> None:
        with pytest.raises(ValueError, match="capture number"):
            ArtifactNavigation(_view(), initial_number=1)

    def test_numbers_above_shortcut_range_and_type_spelling_remain_distinct(self) -> None:
        view = _view(*(["survey", "Survey", ""] * 4))
        view.toggle_errors()
        navigation = ArtifactNavigation(view, initial_number=12)
        assert navigation.types == ("survey", "Survey", "")
        assert navigation.artifact_type == ""
        assert navigation.numbers == (3, 6, 9, 12)
        assert navigation.current.record is view.artifact(12)
        navigation.previous_artifact()
        assert navigation.current.number == 9

    def test_type_navigation_wraps_and_resets_while_capture_navigation_clamps(self) -> None:
        navigation = ArtifactNavigation(_view("a", "b", "a", "b"), initial_number=3)
        navigation.next_artifact()
        assert navigation.current.number == 3
        navigation.previous_artifact()
        navigation.previous_artifact()
        assert navigation.current.number == 1
        navigation.previous_type()
        assert navigation.artifact_type == "b"
        assert navigation.current.number == 2
        navigation.next_artifact()
        assert navigation.current.number == 4
        navigation.next_type()
        assert navigation.artifact_type == "a"
        assert navigation.current.number == 1
        navigation.next_type()
        assert navigation.current.number == 2

    @pytest.mark.parametrize("initial", [1, 2, 3])
    def test_diff_opens_with_current_capture_and_exits_to_single_selection(
        self, initial: int
    ) -> None:
        navigation = ArtifactNavigation(_view("a", "a", "a"), initial_number=initial)
        navigation.toggle_diff()
        pair = navigation.comparison
        assert pair is not None
        assert tuple(target.number for target in pair) == (initial, 2 if initial == 1 else 1)
        navigation.next_artifact()
        navigation.next_type()
        navigation.toggle_diff()
        assert navigation.comparison is None
        assert navigation.current.number == initial

    def test_single_capture_type_cannot_enter_diff(self) -> None:
        navigation = ArtifactNavigation(_view("a", "b", "b"), initial_number=1)
        navigation.toggle_diff()
        assert navigation.comparison is None
        assert navigation.current.number == 1

    @pytest.mark.parametrize(
        "action", ["next_type", "previous_type", "next_artifact", "previous_artifact"]
    )
    def test_two_capture_collision_swaps_both_sides(self, action: str) -> None:
        navigation = ArtifactNavigation(_view("a", "b", "a"), initial_number=1)
        navigation.toggle_diff()
        getattr(navigation, action)()
        pair = navigation.comparison
        assert pair is not None
        assert tuple(target.number for target in pair) == (3, 1)
        assert navigation.artifact_type == "a"
        getattr(navigation, action)()
        pair = navigation.comparison
        assert pair is not None
        assert tuple(target.number for target in pair) == (1, 3)

    def test_diff_sides_cycle_within_type_and_swap_only_on_collision(self) -> None:
        navigation = ArtifactNavigation(_view("a", "b", "a", "a"), initial_number=1)
        navigation.toggle_diff()
        for action, expected in [
            (navigation.next_artifact, (1, 4)),
            (navigation.next_artifact, (4, 1)),
            (navigation.previous_type, (3, 1)),
            (navigation.previous_type, (1, 3)),
            (navigation.previous_artifact, (3, 1)),
            (navigation.next_type, (4, 1)),
        ]:
            action()
            pair = navigation.comparison
            assert pair is not None
            assert tuple(target.number for target in pair) == expected
            assert navigation.artifact_type == "a"

    async def test_moved_seventy_dut_captures_use_existing_readers_without_execution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Device(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("voltage", self._voltage)

            async def _voltage(self, utils: TestUtils) -> Verdict:
                for value in [None, self.name]:
                    await utils.capture_artifact(
                        Artifact(name="reading", type="survey", data=value)
                    )
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
            raise AssertionError("capture navigation attempted suite execution")

        monkeypatch.setattr(LitmusTest, "run", refuse_execution)
        before = {path: path.read_bytes() for path in moved.rglob("*") if path.is_file()}
        saved = await SavedRunReader().read(moved / "result.json")
        assert isinstance(saved, SavedRun)
        selection = InspectorSelection(saved.tree, saved.run_root)
        for number in range(70):
            await selection.select(3 + number * 2)
            timeline = selection.view.timeline
            assert timeline is not None
            view = TimelineView(timeline, page_size=1)
            view.toggle_errors()
            navigation = ArtifactNavigation(view, initial_number=2)
            navigation.toggle_diff()
            navigation.next_artifact()
            pair = navigation.comparison
            assert pair is not None
            assert tuple(target.number for target in pair) == (1, 2)
            for target, expected in zip(pair, [None, f"dut-{number:02d}"], strict=True):
                content = await saved.artifacts.read(target.reference, kind=target.kind)
                assert isinstance(content, ArtifactContent)
                assert content.data["data"] == expected
                assert target.record.data["scope"] == [
                    "rack",
                    "devices",
                    f"dut-{number:02d}",
                    "voltage",
                ]
            assert selection.view.selected.state == "Pass"

        assert saved.root_timeline is not None
        root_view = TimelineView(saved.root_timeline, page_size=1)
        root = ArtifactNavigation(root_view, initial_number=root_view.artifact_count)
        assert root.current.kind == "result"
        content = await saved.artifacts.read(root.current.reference, kind=root.current.kind)
        assert isinstance(content, ArtifactContent)
        assert content.data == saved.tree.result
        assert {path: path.read_bytes() for path in before} == before

    async def test_empty_recorded_path_remains_selectable_and_diagnosed(
        self, tmp_path: Path
    ) -> None:
        view = _view("survey")
        pointer = view.artifact(1)
        assert pointer is not None
        pointer.data["artifact_path"] = ""
        navigation = ArtifactNavigation(view, initial_number=1)
        capture = await ArtifactReader(tmp_path).read(navigation.current.reference)
        assert isinstance(capture, ArtifactDiagnostic)
        assert capture.reference == ""
        assert navigation.current.number == 1
