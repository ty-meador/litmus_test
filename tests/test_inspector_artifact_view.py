"""The artifact modal shows what the run pointed at, pages it, and compares two captures."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import pytest
from tests.inspector_rack import device_row, record_seventy_dut_run, saved_bytes

from litmus.inspector_artifact_view import (
    ArtifactModal,
    DiffRow,
    _render_lines,  # pyright: ignore[reportPrivateUsage]
    align,
    diff_rows,
    single_rows,
)
from litmus.inspector_artifacts import ArtifactContent, ArtifactDiagnostic, ArtifactReader
from litmus.inspector_selection import InspectorSelection
from litmus.inspector_timeline import TimelineDiagnostic, TimelineRead, TimelineRecord
from litmus.inspector_timeline_view import TimelineView
from litmus.run_data import JsonValue


def _capture(root: Path, number: int, kind: str, data: JsonValue) -> str:
    """Write one version-1 artifact envelope and return its absolute recorded path."""
    path = root / str(number) / "reading.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "data": data,
                "format_version": 1,
                "name": "reading",
                "timestamp": "2026-09-09T18:00:00Z",
                "type": kind,
            }
        )
    )
    return str(path)


def _view(root: Path, *captures: tuple[str, JsonValue | None]) -> TimelineView:
    """A chronology of numbered pointers; ``None`` data leaves the pointer dangling."""
    records: list[TimelineRecord | TimelineDiagnostic] = []
    for number, (kind, data) in enumerate(captures, 1):
        reference = (
            str(root / "missing" / f"{number}.json")
            if data is None
            else _capture(root, number, kind, data)
        )
        records.append(
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
                    "artifact_path": reference,
                    "artifact_timestamp": "2026-09-09T18:00:00Z",
                },
            )
        )
    records.append(TimelineDiagnostic(None, "unavailable", "incomplete read"))
    return TimelineView(TimelineRead(None, tuple(records)), page_size=1)


class ControlledReader(ArtifactReader):
    """Expose read completion order without sleeps or filesystem timing assumptions."""

    requests: asyncio.Queue[tuple[str, asyncio.Future[ArtifactContent | ArtifactDiagnostic]]]

    def __init__(self, run_root: Path) -> None:
        super().__init__(run_root)
        object.__setattr__(self, "requests", asyncio.Queue())

    async def read(
        self, reference: str, *, kind: str = "artifact"
    ) -> ArtifactContent | ArtifactDiagnostic:
        del kind
        future: asyncio.Future[ArtifactContent | ArtifactDiagnostic] = (
            asyncio.get_running_loop().create_future()
        )
        await self.requests.put((reference, future))
        return await future


def _content(reference: str, data: JsonValue) -> ArtifactContent:
    return ArtifactContent(reference, Path(reference), {"data": data})


class TestArtifactModal:
    async def test_opens_on_the_pressed_capture_at_its_first_page(self, tmp_path: Path) -> None:
        view = _view(
            tmp_path, ("survey", {"a": 1}), ("survey", {"rows": [1, 2, 3, 4]}), ("other", 0)
        )
        modal = ArtifactModal(view, ArtifactReader(tmp_path), initial_number=2, page_size=3)
        assert modal.status == "loading"
        assert modal.rows == ()

        await modal.open()

        assert modal.status == "ready"
        assert modal.diff is False
        assert modal.right is None
        assert modal.left is not None
        assert modal.left.title == "[2] reading (survey) 2026-09-09T18:00:00Z"
        assert modal.left.diagnostic is None
        assert modal.page_index == 0
        # Fourteen lines: the envelope's five keys around a four-element list.
        assert modal.page_count == 5
        assert single_rows(modal) == ("{", '  "data": {', '    "rows": [')
        assert modal.navigation.numbers == (1, 2)

    async def test_paging_clamps_and_resize_keeps_the_first_visible_row(
        self, tmp_path: Path
    ) -> None:
        view = _view(tmp_path, ("survey", {"rows": [1, 2, 3, 4]}))
        with pytest.raises(ValueError, match="page size"):
            ArtifactModal(view, ArtifactReader(tmp_path), initial_number=1, page_size=0)
        modal = ArtifactModal(view, ArtifactReader(tmp_path), initial_number=1, page_size=3)
        assert modal.page_size == 3
        await modal.open()
        assert modal.left is not None
        lines = modal.left.lines
        assert len(lines) == 14
        assert modal.page_count == 5

        modal.previous_page()
        assert modal.page_index == 0
        for _ in range(6):
            modal.next_page()
        assert modal.page_index == 4
        assert single_rows(modal) == lines[12:14]

        # The first visible line was 12; at four lines a page that is page 3.
        modal.resize(4)
        assert modal.page_count == 4
        assert modal.page_index == 3
        assert single_rows(modal) == lines[12:14]
        with pytest.raises(ValueError, match="page size"):
            modal.resize(0)

    async def test_an_unreadable_capture_shows_the_diagnostic_with_its_reference(
        self, tmp_path: Path
    ) -> None:
        view = _view(tmp_path, ("survey", None))
        modal = ArtifactModal(view, ArtifactReader(tmp_path), initial_number=1, page_size=5)

        await modal.open()

        assert modal.left is not None
        diagnostic = modal.left.diagnostic
        assert diagnostic is not None
        assert diagnostic.kind == "unavailable"
        reference = str(tmp_path / "missing" / "1.json")
        assert diagnostic.reference == reference
        assert single_rows(modal) == (
            f"unavailable: {diagnostic.message}",
            f"recorded reference: {reference}",
        )

    async def test_diff_aligns_both_sides_and_pages_them_together(self, tmp_path: Path) -> None:
        view = _view(tmp_path, ("survey", {"a": 1, "b": 2}), ("survey", {"a": 1, "b": 3}))
        modal = ArtifactModal(view, ArtifactReader(tmp_path), initial_number=1, page_size=3)
        await modal.open()
        modal.next_page()

        await modal.toggle_diff()

        assert modal.diff is True
        assert modal.page_index == 0
        assert modal.left is not None
        assert modal.right is not None
        assert (modal.left.target.number, modal.right.target.number) == (1, 2)
        assert modal.page_count == 4
        assert diff_rows(modal) == (
            DiffRow("{", "{", True),
            DiffRow('  "data": {', '  "data": {', True),
            DiffRow('    "a": 1,', '    "a": 1,', True),
        )
        modal.next_page()
        assert diff_rows(modal) == (
            DiffRow('    "b": 2', '    "b": 3', False),
            DiffRow("  },", "  },", True),
            DiffRow('  "format_version": 1,', '  "format_version": 1,', True),
        )
        with pytest.raises(ValueError, match="comparing"):
            single_rows(modal)

        await modal.toggle_diff()

        assert modal.diff is False
        assert modal.right is None
        assert modal.left.target.number == 1
        assert modal.page_index == 0
        with pytest.raises(ValueError, match="one capture"):
            diff_rows(modal)

    async def test_diff_on_a_singleton_type_stays_single_without_reloading(
        self, tmp_path: Path
    ) -> None:
        view = _view(tmp_path, ("survey", 1), ("other", 2))
        reader = ControlledReader(tmp_path)
        modal = ArtifactModal(view, reader, initial_number=2, page_size=5)
        opening = asyncio.create_task(modal.open())
        reference, future = await reader.requests.get()
        future.set_result(_content(reference, 2))
        await opening

        await modal.toggle_diff()

        assert modal.diff is False
        assert reader.requests.empty()
        assert modal.left is not None
        assert modal.left.target.number == 2

    async def test_moving_between_types_reloads_and_returns_to_the_first_page(
        self, tmp_path: Path
    ) -> None:
        view = _view(tmp_path, ("survey", {"rows": [1, 2, 3]}), ("other", 0), ("survey", 1))
        modal = ArtifactModal(view, ArtifactReader(tmp_path), initial_number=1, page_size=2)
        await modal.open()
        modal.next_page()
        assert modal.page_index == 1

        await modal.next_type()

        assert modal.navigation.artifact_type == "other"
        assert modal.page_index == 0
        assert modal.left is not None
        assert modal.left.target.number == 2
        assert modal.left.content == ArtifactContent(
            modal.left.target.reference,
            Path(modal.left.target.reference),
            {
                "data": 0,
                "format_version": 1,
                "name": "reading",
                "timestamp": "2026-09-09T18:00:00Z",
                "type": "other",
            },
        )

        await modal.previous_type()
        await modal.next_artifact()
        assert modal.left.target.number == 3
        await modal.previous_artifact()
        assert modal.left.target.number == 1

    async def test_a_delayed_read_never_replaces_a_newer_selection(self, tmp_path: Path) -> None:
        view = _view(tmp_path, ("survey", 1), ("survey", 2))
        reader = ControlledReader(tmp_path)
        modal = ArtifactModal(view, reader, initial_number=1, page_size=5)

        opening = asyncio.create_task(modal.open())
        first_reference, first = await reader.requests.get()
        moving = asyncio.create_task(modal.next_artifact())
        second_reference, second = await reader.requests.get()
        assert (first_reference, second_reference) != (second_reference, first_reference)

        second.set_result(_content(second_reference, 2))
        await moving
        assert modal.left is not None
        assert modal.left.target.number == 2
        assert modal.status == "ready"

        first.set_result(_content(first_reference, 1))
        await opening
        # The stale read landed after the newer selection and changed nothing.
        assert modal.left.target.number == 2
        assert modal.status == "ready"
        assert single_rows(modal) == ("{", '  "data": 2', "}")

    def test_align_pairs_replaced_runs_and_pads_the_shorter_side(self) -> None:
        rows = align(("a", "b", "c", "d"), ("a", "x", "y", "z", "d", "e"))

        assert rows == (
            DiffRow("a", "a", True),
            DiffRow("b", "x", False),
            DiffRow("c", "y", False),
            DiffRow(None, "z", False),
            DiffRow("d", "d", True),
            DiffRow(None, "e", False),
        )
        assert align((), ()) == ()

    def test_render_keeps_recorded_key_order_and_unicode(self) -> None:
        content = ArtifactContent("ref", Path("ref"), {"z": "°C", "a": [1, None]})

        assert _render_lines(content) == (
            "{",
            '  "z": "°C",',
            '  "a": [',
            "    1,",
            "    null",
            "  ]",
            "}",
        )

    async def test_seventy_dut_run_compares_each_device_s_pair_without_execution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_seventy_dut_run(tmp_path, monkeypatch)
        before = saved_bytes(saved.run_root)
        selection = InspectorSelection(saved.tree, saved.run_root)

        for index in range(70):
            await selection.select(device_row(index))
            timeline = selection.view.timeline
            assert timeline is not None
            modal = ArtifactModal(
                TimelineView(timeline, page_size=1), saved.artifacts, initial_number=2, page_size=8
            )
            await modal.open()
            assert modal.left is not None
            assert modal.left.diagnostic is None
            assert cast(ArtifactContent, modal.left.content).data["data"] == f"dut-{index:02d}"

            await modal.toggle_diff()
            assert modal.right is not None
            assert (modal.left.target.number, modal.right.target.number) == (2, 1)
            rows = diff_rows(modal)
            assert DiffRow(f'  "data": "dut-{index:02d}",', '  "data": null,', False) in rows
            # The two captures were taken at different instants, so their timestamps
            # differ too; every other line of the envelope pairs as equal.
            differing = {row.left for row in rows if not row.same}
            assert {line.split(":")[0].strip() for line in differing if line} == {
                '"data"',
                '"timestamp"',
            }

        assert saved_bytes(saved.run_root) == before
