"""The Inspector screen renders the models and forwards the guide's keys, nothing more."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from tests.inspector_rack import (
    MIXED_ROWS,
    PARK,
    device_row,
    mixed_suite,
    parked_suite,
    record_mixed_run,
    record_parked_run,
    record_seventy_dut_run,
    saved_bytes,
)

from litmus.inspector_app import (
    ArtifactScreen,
    InspectorApp,
    RowDisplay,
    _log_header,  # pyright: ignore[reportPrivateUsage]
    _log_row_text,  # pyright: ignore[reportPrivateUsage]
    _modal_body,  # pyright: ignore[reportPrivateUsage]
    _modal_title,  # pyright: ignore[reportPrivateUsage]
    _row_text,  # pyright: ignore[reportPrivateUsage]
    _tree_prefixes,  # pyright: ignore[reportPrivateUsage]
    _window,  # pyright: ignore[reportPrivateUsage]
)
from litmus.inspector_artifact_view import ArtifactModal
from litmus.inspector_artifacts import ArtifactReader
from litmus.inspector_rerun import RerunRecord, RerunSession
from litmus.inspector_run import SavedRun
from litmus.inspector_selection import SelectionView
from litmus.inspector_timeline import TimelineDiagnostic, TimelineRead, TimelineRecord
from litmus.inspector_timeline_view import TimelineEntry, TimelineView
from litmus.inspector_tree import TreeRow

SIZE = (100, 30)

MIXED_TREE = (
    "> rack  Aborted",
    "  └─ steps  Aborted",
    "     ├─ power  Pass",
    "     ├─ checks  Aborted",
    "     │  ├─ volts  Fail",
    "     │  └─ slow  Aborted",
    "     └─ later  Pending",
)
"""The mixed run's Inspect tree with the root highlighted: the legacy glyphs, read back."""


def _row_name(line: str) -> str:
    """The node name on a rendered tree line, whatever its depth, glyphs, or cursor."""
    return line[2:].lstrip(" │├└─").split("  ")[0]


def _selected(app: InspectorApp) -> str:
    return app.selection.view.selected.ancestor_names[-1]


def _text_of(file: str) -> str:
    return Path(file).read_text(encoding="utf-8")


def _truncate_leaf_log(saved: SavedRun, name: str) -> Path:
    """Simulate an interrupted run: an unfinished record at the end of the log ``name`` reads."""
    row = next(row for row in saved.tree.rows if row.ancestor_names[-1] == name)
    assert row.log_owner_index is not None
    reference = cast(str, saved.tree.rows[row.log_owner_index].record["log_path"])
    path = saved.run_root / reference
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"format_version": 1, "timestamp": "2026-09-09T2')
    return path


class TestInspectorApp:
    async def test_opens_on_the_root_with_its_log_loaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch)
        app = InspectorApp(saved)

        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()

            assert app.tab == "inspect"
            assert [_row_name(line) for line in app.tree_lines] == list(MIXED_ROWS)
            assert app.tree_lines == MIXED_TREE
            assert app.log_lines[0] == "log: rack"
            assert app.log_lines[1].startswith("page ")
            assert app.timeline_view is not None
            # A chronology opens at its tail, where the run stopped.
            assert app.timeline_view.page_index == app.timeline_view.page_count - 1

    async def test_cursor_movement_loads_the_highlighted_node_s_log_without_enter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch)
        app = InspectorApp(saved)

        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("down", "down")
            await pilot.pause()

            assert _selected(app) == "power"
            assert app.tree_lines[2].startswith(">    ├─ power  Pass")
            # A leaf composed with self.test rolls into its parent's chronology; the
            # pane names the owner and says so, never showing the log under the leaf,
            # and the owner's row carries the legacy arrow so the eye finds it.
            assert app.log_lines[0] == "log: rack/steps  (rolled up: power has no own log)"
            assert app.tree_lines[1] == "  └─ steps  Aborted  ←"
            assert sum(line.endswith("←") for line in app.tree_lines) == 1
            assert any("[1] " in line and "rail" in line for line in app.log_lines)
            assert any("ripple above expectation" in line for line in app.log_lines)
            assert "captures: 1" in app.log_lines[1]

            await pilot.press("up")
            await pilot.pause()
            assert _selected(app) == "steps"
            assert app.log_lines[0] == "log: rack/steps"
            assert not any(line.endswith("←") for line in app.tree_lines)

    async def test_arrows_jump_between_failures_skipping_passed_and_pending_without_wrap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch)
        app = InspectorApp(saved)

        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("right")
            await pilot.pause()
            assert _selected(app) == "steps"
            await pilot.press("right")
            await pilot.pause()
            assert _selected(app) == "checks"
            await pilot.press("right")
            await pilot.pause()
            assert _selected(app) == "volts"
            await pilot.press("right")
            await pilot.pause()
            assert _selected(app) == "slow"
            await pilot.press("right")
            await pilot.pause()
            assert _selected(app) == "slow"
            assert app.log_lines[0] == "log: rack/steps/checks  (rolled up: slow has no own log)"

            for _ in range(6):
                await pilot.press("left")
            await pilot.pause()
            assert _selected(app) == "rack"

    async def test_h_hides_passing_rows_and_keeps_the_cursor_on_a_visible_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch)
        app = InspectorApp(saved)

        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("down", "down")
            await pilot.pause()
            assert _selected(app) == "power"

            await pilot.press("h")
            await pilot.pause()
            assert app.hide_passes is True
            names = [_row_name(line) for line in app.tree_lines]
            assert names == ["rack", "steps", "checks", "volts", "slow", "later"]
            # The highlighted row vanished with the passes; the cursor lands on the
            # row above it, and the glyphs close the branch on what is still shown.
            assert _selected(app) == "steps"
            assert app.tree_lines[1] == "> └─ steps  Aborted"
            assert app.tree_lines[2] == "     ├─ checks  Aborted"
            assert app.tree_lines[5] == "     └─ later  Pending"

            await pilot.press("down")
            await pilot.pause()
            assert _selected(app) == "checks"
            assert app.log_lines[0] == "log: rack/steps/checks"

            await pilot.press("h")
            await pilot.pause()
            assert app.hide_passes is False
            assert [_row_name(line) for line in app.tree_lines] == list(MIXED_ROWS)
            assert _selected(app) == "checks"
            assert app.tree_lines[3].startswith("> ")

    async def test_paging_and_the_errors_filter_keep_a_truncation_marker_visible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch)
        _truncate_leaf_log(saved, "power")
        app = InspectorApp(saved)

        async with app.run_test(size=(100, 12)) as pilot:
            await pilot.press("down", "down")
            await pilot.pause()
            assert _selected(app) == "power"
            assert app.timeline_view is not None
            assert app.timeline_view.page_count > 1
            last = app.timeline_view.page_index
            assert any(line.startswith("--- truncated") for line in app.log_lines)

            await pilot.press("p")
            await pilot.pause()
            assert app.timeline_view.page_index == last - 1
            await pilot.press("n", "n")
            await pilot.pause()
            assert app.timeline_view.page_index == last

            await pilot.press("f")
            await pilot.pause()
            assert app.timeline_view.errors_only is True
            assert "errors only: on" in app.log_lines[1]
            body = app.log_lines[2:]
            assert any("ripple above expectation" in line for line in body)
            assert any(line.startswith("--- truncated") for line in body)
            assert not any("rail settled" in line for line in body)

            # A pending node has no chronology: the page and filter keys have nothing
            # to act on and the pane says why.
            for _ in range(4):
                await pilot.press("down")
            await pilot.press("n", "p", "f")
            await pilot.pause()
            assert _selected(app) == "later"
            assert app.timeline_view is None
            assert app.log_lines == ("log: not run: the node never executed", "")

    async def test_quit_keys_leave_the_saved_bytes_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch)
        before = saved_bytes(saved.run_root)

        for key in ("q", "ctrl+c"):
            app = InspectorApp(saved)
            async with app.run_test(size=SIZE) as pilot:
                await pilot.press("down")
                await pilot.press(key)
                await pilot.pause()
            assert app.return_code == 0

        assert saved_bytes(saved.run_root) == before

    async def test_a_tall_tree_windows_around_the_selection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_seventy_dut_run(tmp_path, monkeypatch)
        app = InspectorApp(saved)

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            pane_height = len(app.tree_lines)
            assert 1 < pane_height < len(saved.tree.rows)

            for _ in range(device_row(25)):
                await pilot.press("down")
            await pilot.pause()

            assert app.selection.view.selected.index == device_row(25)
            assert app.selection.view.selected.ancestor_names == (
                "rack",
                "devices",
                "dut-25",
                "voltage",
            )
            assert len(app.tree_lines) == pane_height
            assert sum(line.startswith("> ") for line in app.tree_lines) == 1
            assert app.log_lines[0] == (
                "log: rack/devices/dut-25  (rolled up: voltage has no own log)"
            )
            assert "captures: 2" in app.log_lines[1]

            screenshot = app.save_screenshot(filename="inspector.svg", path=str(tmp_path))
            assert "Inspect" in _text_of(screenshot)

            # A taller terminal resizes the same presentation model rather than
            # rebuilding it, so the page cursor survives the resize.
            view_before = app.timeline_view
            await pilot.resize_terminal(80, 48)
            await pilot.pause()
            assert app.timeline_view is view_before
            assert "captures: 2" in app.log_lines[1]

            # Every node passed: hiding passes says so instead of showing nothing.
            await pilot.press("h")
            await pilot.pause()
            assert app.tree_lines == ("every node passed; h shows them again",)
            await pilot.press("down", "h")
            await pilot.pause()
            assert 1 < len(app.tree_lines) < len(saved.tree.rows)
            assert sum(line.startswith("> ") for line in app.tree_lines) == 1


class TestRerunTab:
    async def test_enter_re_runs_the_highlighted_node_and_shows_its_fresh_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch, refuse_execution=False)
        before = saved_bytes(saved.run_root)
        session = RerunSession(saved, mixed_suite, scratch_parent=tmp_path)
        app = InspectorApp(saved, session)

        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("down", "down")
            await pilot.pause()
            assert _selected(app) == "power"

            # Enter on Inspect never runs anything: the archive has no such key.
            await pilot.press("enter")
            await pilot.pause()
            assert session.scratch_root is None

            await pilot.press("tab")
            await pilot.pause()
            assert app.tab == "rerun"
            assert session.available is True, session.unavailable
            assert _selected(app) == "power"
            assert [_row_name(line) for line in app.tree_lines] == list(MIXED_ROWS)
            assert all(line.endswith("not re-run") for line in app.tree_lines)
            assert app.log_lines == (
                "log: not re-run yet: press Enter on a node to execute it",
                "",
            )

            await pilot.press("enter")
            await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]
            await pilot.pause()

            assert session.record(2) is not None
            # The bench re-ran one leaf. Its ancestors configured and rolled their
            # state up over that one child, so they carry no verdict here; drawing
            # them Pass would hide the sibling failure the archive still holds.
            assert app.tree_lines[0] == "  rack  partly re-run"
            assert app.tree_lines[1] == "  └─ steps  partly re-run  ←"
            assert app.tree_lines[2] == ">    ├─ power  Pass"
            assert app.tree_lines[4] == "     │  ├─ volts  not re-run"
            assert app.log_lines[0] == "log: rack/steps  (rolled up: power has no own log)"
            assert any("ripple above expectation" in line for line in app.log_lines)

            # Moving onto a node the bench never reached says so; the archive's log
            # for that node is not shown in its place.
            await pilot.press("down", "down")
            await pilot.pause()
            assert _selected(app) == "volts"
            assert app.log_lines[0] == "log: not re-run yet: press Enter on a node to execute it"

            await pilot.press("tab")
            await pilot.pause()
            assert app.tab == "inspect"
            assert app.tree_lines[4].startswith(">    │  ├─ volts  Fail")
            assert app.log_lines[0] == "log: rack/steps/checks  (rolled up: volts has no own log)"

        # The archive is untouched, and the bench's evidence went with the screen.
        assert saved_bytes(saved.run_root) == before
        assert session.scratch_root is None

    async def test_without_a_suite_the_tab_says_why_and_enter_is_inert(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch)
        app = InspectorApp(saved)

        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("tab", "down", "down", "enter")
            await pilot.pause()

            assert app.tab == "rerun"
            assert app.session.available is False
            assert app.log_lines[0] == (
                "log: re-run unavailable: no suite was supplied to this Inspector"
            )
            assert app.session.scratch_root is None

    async def test_tab_is_held_during_a_live_re_run_and_q_interrupts_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_parked_run(tmp_path)
        session = RerunSession(saved, parked_suite, scratch_parent=tmp_path)
        app = InspectorApp(saved, session)
        started = asyncio.Event()
        monkeypatch.setitem(PARK, "event", started)
        at_close: list[RerunRecord | None] = []
        original_close = session.close

        def spy_close() -> None:
            at_close.append(session.record(1))
            original_close()

        monkeypatch.setattr(session, "close", spy_close)

        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("tab", "down")
            await pilot.pause()
            assert app.tab == "rerun"
            assert _selected(app) == "wait"

            await pilot.press("enter")
            await asyncio.wait_for(started.wait(), 5)
            await pilot.pause()
            assert session.running == 1
            assert "Running" in app.tree_lines[1]
            assert app.log_lines[0] == "log: running bench/wait"

            # Tab is disabled while hardware is live, and a second Enter starts nothing.
            await pilot.press("tab", "enter")
            await pilot.pause()
            assert app.tab == "rerun"
            assert session.running == 1

            await pilot.press("q")
            await asyncio.wait_for(
                app.workers.wait_for_complete(),  # pyright: ignore[reportUnknownMemberType]
                10,
            )
            await pilot.pause()

        assert app.return_code == 0
        record = at_close[0]
        assert record is not None
        assert record.row.state == "Aborted"


class TestRendering:
    def test_tree_row_shows_cursor_glyphs_state_readings_not_taken_and_owner(self) -> None:
        display = RowDisplay(
            name="volts",
            prefix="│  ├─ ",
            label="Fail",
            style="bold red",
            not_taken=("missing",),
            selected=True,
            owner=False,
        )

        assert _row_text(display).plain == "> │  ├─ volts  Fail  not taken: missing"
        assert (
            _row_text(replace(display, selected=False, owner=True)).plain
            == "  │  ├─ volts  Fail  not taken: missing  ←"
        )

    def test_tree_prefixes_close_each_branch_on_its_last_visible_child(self) -> None:
        assert _tree_prefixes([0, 1, 2, 2, 3, 3, 2]) == [
            "",
            "└─ ",
            "   ├─ ",
            "   ├─ ",
            "   │  ├─ ",
            "   │  └─ ",
            "   └─ ",
        ]
        # Two children of the root: the first keeps a vertical line beside its subtree.
        assert _tree_prefixes([0, 1, 2, 1, 2]) == ["", "├─ ", "│  └─ ", "└─ ", "   └─ "]
        assert _tree_prefixes([0]) == [""]

    def test_log_rows_number_captures_and_keep_damage_in_place(self) -> None:
        record = TimelineRecord(
            7,
            {
                "timestamp": "2026-09-09T21:00:00Z",
                "level": "warning",
                "message": "measurement: rail = 12.1 V limits=[null, 12.0] in_limits=false",
                "artifact_path": "/run/rail.json",
            },
        )
        entry = _log_row_text(TimelineEntry(record, 3)).plain
        assert entry.startswith("[3] 2026-09-09T21:00:00Z warning  measurement: rail")
        assert _log_row_text(TimelineEntry(record, None)).plain.startswith("    2026-09-09")
        damage = _log_row_text(TimelineDiagnostic(12, "truncated", "unfinished record")).plain
        assert damage == "--- truncated at line 12: unfinished record ---"
        assert (
            _log_row_text(TimelineDiagnostic(None, "unavailable", "gone")).plain
            == "--- unavailable: gone ---"
        )

    @pytest.mark.parametrize(
        ("count", "selected", "height", "expected"),
        [
            (5, 0, 10, range(0, 5)),
            (20, 0, 5, range(0, 5)),
            (20, 10, 5, range(8, 13)),
            (20, 19, 5, range(15, 20)),
            (20, 3, 0, range(3, 4)),
        ],
    )
    def test_window_keeps_the_selection_visible(
        self, count: int, selected: int, height: int, expected: range
    ) -> None:
        assert _window(count, selected, height) == expected

    async def test_the_mixed_recording_is_what_the_screen_tests_assume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The screen tests read row positions by name; pin the recording's shape once.
        saved = await record_mixed_run(tmp_path, monkeypatch)
        result = json.loads((saved.run_root / "result.json").read_text())
        assert result["state"] == "Aborted"
        assert [row.ancestor_names[-1] for row in saved.tree.rows] == list(MIXED_ROWS)
        assert [row.state for row in saved.tree.failures] == [
            "Aborted",
            "Aborted",
            "Aborted",
            "Fail",
            "Aborted",
        ]


class TestLogHeader:
    def test_states_other_than_ready_are_named_and_carry_no_page_line(self) -> None:
        row = TreeRow(
            index=6,
            parent_index=1,
            ancestor_names=("rack", "steps", "later"),
            record={"name": "later", "state": None, "evidence": None, "evidence_declared": None},
            log_owner_index=None,
        )

        owner, page = _log_header(SelectionView(row, None, "loading"), None)
        assert (owner.plain, page.plain) == ("log: loading", "")
        owner, page = _log_header(SelectionView(row, None, "not-run"), None)
        assert (owner.plain, page.plain) == ("log: not run: the node never executed", "")
        owner, page = _log_header(SelectionView(row, None, "unavailable", None, "gone"), None)
        assert (owner.plain, page.plain) == ("log: unavailable: gone", "")
        owner, page = _log_header(SelectionView(row, None, "idle"), None)
        assert (owner.plain, page.plain) == ("log: idle: no chronology", "")


class TestArtifactScreen:
    async def test_a_digit_opens_the_numbered_capture_and_b_returns_to_the_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch)
        app = InspectorApp(saved)

        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("down", "down")
            await pilot.pause()
            assert _selected(app) == "power"

            # A digit with no capture behind it does nothing.
            await pilot.press("5")
            await pilot.pause()
            assert not isinstance(app.screen, ArtifactScreen)

            await pilot.press("1")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ArtifactScreen)
            assert screen.modal_lines[0].startswith(
                "type 1/1: survey   capture 1/1   [1] rail (survey)"
            )
            assert screen.modal_lines[1].startswith("page 1/1")
            assert '  "data": {' in screen.modal_lines
            assert '    "v": 12.1' in screen.modal_lines

            await pilot.press("b")
            await pilot.pause()
            assert not isinstance(app.screen, ArtifactScreen)
            assert _selected(app) == "power"

    async def test_the_modal_owns_its_keys_and_compares_two_captures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_seventy_dut_run(tmp_path, monkeypatch)
        before = saved_bytes(saved.run_root)
        app = InspectorApp(saved)

        async with app.run_test(size=(100, 16)) as pilot:
            for _ in range(device_row(3)):
                await pilot.press("down")
            await pilot.press("2")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ArtifactScreen)
            assert screen.modal_lines[0].startswith("type 1/1: survey   capture 2/2   [2] reading")
            assert '  "data": "dut-03",' in screen.modal_lines

            # The arrows move between captures inside the modal, not the tree cursor.
            await pilot.press("left")
            await pilot.pause()
            assert screen.modal_lines[0].startswith("type 1/1: survey   capture 1/2   [1] reading")
            assert '  "data": null,' in screen.modal_lines
            assert _selected(app) == "voltage"

            await pilot.press("d")
            await pilot.pause()
            assert screen.modal.diff is True
            assert "diff  [1] reading" in screen.modal_lines[0]
            assert "|  [2] reading" in screen.modal_lines[0]
            body = screen.modal_lines[2:]
            assert any('"data": null,' in line and '"data": "dut-03",' in line for line in body)

            # A collision swaps the sides rather than comparing a capture with itself.
            await pilot.press("right")
            await pilot.pause()
            assert "diff  [2] reading" in screen.modal_lines[0]
            assert "|  [1] reading" in screen.modal_lines[0]

            await pilot.press("n")
            await pilot.pause()
            assert screen.modal.page_index == min(1, screen.modal.page_count - 1)
            await pilot.press("p")
            await pilot.pause()
            assert screen.modal.page_index == 0

            await pilot.press("d")
            await pilot.pause()
            assert screen.modal.diff is False
            assert screen.modal_lines[0].startswith("type 1/1: survey   capture 1/2")

            # One type only: the bracket keys wrap onto the same type's first capture.
            await pilot.press("right_square_bracket")
            await pilot.pause()
            assert screen.modal_lines[0].startswith("type 1/1: survey   capture 1/2")
            await pilot.press("right", "left_square_bracket")
            await pilot.pause()
            assert screen.modal_lines[0].startswith("type 1/1: survey   capture 1/2")

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ArtifactScreen)

        assert saved_bytes(saved.run_root) == before

    async def test_quitting_from_the_modal_quits_the_inspector(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch)
        app = InspectorApp(saved)

        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("down", "down", "1")
            await pilot.pause()
            assert isinstance(app.screen, ArtifactScreen)
            await pilot.press("q")
            await pilot.pause()
        assert app.return_code == 0


class TestModalRendering:
    async def test_the_modal_says_loading_until_its_read_lands(self, tmp_path: Path) -> None:
        root = tmp_path / "run"
        capture = root / "1" / "reading.json"
        capture.parent.mkdir(parents=True)
        capture.write_text(
            json.dumps(
                {
                    "data": {"v": 12.1},
                    "format_version": 1,
                    "name": "reading",
                    "timestamp": "2026-09-09T18:00:00Z",
                    "type": "survey",
                }
            )
        )
        pointer = TimelineRecord(
            2,
            {
                "format_version": 1,
                "timestamp": "2026-09-09T18:00:00Z",
                "scope": ["rack"],
                "level": "info",
                "message": "capture",
                "artifact_name": "reading",
                "artifact_type": "survey",
                "artifact_path": str(capture),
                "artifact_timestamp": "2026-09-09T18:00:00Z",
            },
        )
        view = TimelineView(TimelineRead(None, (pointer,)), page_size=1)
        modal = ArtifactModal(view, ArtifactReader(root), initial_number=1, page_size=4)

        assert _modal_title(modal).plain.split("\n") == [
            "type 1/1: survey   loading",
            "page 1/1   b back  [ ] type  ← → capture  n p page  d diff",
        ]
        assert [line.plain for line in _modal_body(modal, 80)] == ["loading"]

        await modal.open()

        assert _modal_title(modal).plain.startswith(
            "type 1/1: survey   capture 1/1   [1] reading (survey) 2026-09-09T18:00:00Z"
        )
        assert [line.plain for line in _modal_body(modal, 80)] == [
            "{",
            '  "data": {',
            '    "v": 12.1',
            "  },",
        ]

    async def test_a_diagnostic_is_rendered_in_warning_style(self, tmp_path: Path) -> None:
        missing = tmp_path / "run" / "gone.json"
        pointer = TimelineRecord(
            2,
            {
                "format_version": 1,
                "timestamp": "2026-09-09T18:00:00Z",
                "scope": ["rack"],
                "level": "info",
                "message": "capture",
                "artifact_name": "reading",
                "artifact_type": "survey",
                "artifact_path": str(missing),
                "artifact_timestamp": "2026-09-09T18:00:00Z",
            },
        )
        view = TimelineView(TimelineRead(None, (pointer,)), page_size=1)
        modal = ArtifactModal(view, ArtifactReader(tmp_path / "run"), initial_number=1, page_size=4)
        await modal.open()

        assert modal.left is not None
        diagnostic = modal.left.diagnostic
        assert diagnostic is not None
        body = _modal_body(modal, 80)
        assert [line.plain for line in body] == [
            f"unavailable: {diagnostic.message}",
            f"recorded reference: {missing}",
        ]
        assert all(str(span.style) == "bold yellow" for line in body for span in line.spans)
