"""Saved timeline references cannot borrow another run's chronology."""

import json
from pathlib import Path

import pytest

from litmus.inspector_timeline import TimelineDiagnostic, TimelineReader, TimelineRecord


def _line(message: str) -> bytes:
    return (
        json.dumps(
            {
                "format_version": 1,
                "timestamp": "2026-09-08T12:00:00Z",
                "scope": ["rack", "dut/01"],
                "level": "info",
                "message": message,
            }
        )
        + "\n"
    ).encode()


class TestContainedTimeline:
    @pytest.mark.parametrize("spelling", ["absolute", "relative", "symlink", "parent"])
    async def test_operator_root_spellings_select_the_same_chronology(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
    ) -> None:
        root = tmp_path / "run"
        root.mkdir()
        path = root / "timeline.jsonl"
        original = _line("selected run")
        path.write_bytes(original)
        if spelling == "relative":
            monkeypatch.chdir(tmp_path)
            root = Path("run")
        elif spelling == "symlink":
            alias = tmp_path / "latest"
            alias.symlink_to(root, target_is_directory=True)
            root = alias
        elif spelling == "parent":
            child = root / "child"
            child.mkdir()
            root = child / ".."

        result = await TimelineReader().read_reference(root, "timeline.jsonl")
        assert result.path == path
        assert result.reference == "timeline.jsonl"
        assert len(result.rows) == 1
        row = result.rows[0]
        assert isinstance(row, TimelineRecord)
        assert row.data["message"] == "selected run"
        assert path.read_bytes() == original

    async def test_missing_unit_never_borrows_a_siblings_matching_basename(
        self, tmp_path: Path
    ) -> None:
        sibling = tmp_path / "dut-02"
        sibling.mkdir()
        (sibling / "timeline.jsonl").write_bytes(_line("wrong unit"))
        reference = "dut-01/timeline.jsonl"
        reader = TimelineReader()
        missing = await reader.read_reference(tmp_path, reference)
        assert missing.reference == reference
        assert missing.path is None
        assert len(missing.rows) == 1
        diagnostic = missing.rows[0]
        assert isinstance(diagnostic, TimelineDiagnostic)
        assert diagnostic.kind == "unavailable"

        restored = tmp_path / reference
        restored.parent.mkdir()
        restored.write_bytes(_line("right unit"))
        available = await reader.read_reference(tmp_path, reference)
        row = available.rows[0]
        assert isinstance(row, TimelineRecord)
        assert row.data["message"] == "right unit"
        restored.write_bytes(_line("fresh evidence"))
        refreshed = await reader.read_reference(tmp_path, reference)
        row = refreshed.rows[0]
        assert isinstance(row, TimelineRecord)
        assert row.data["message"] == "fresh evidence"

    async def test_full_relative_tail_selects_the_right_unit_after_a_move(
        self, tmp_path: Path
    ) -> None:
        original = tmp_path / "original"
        for name in ("dut-01", "dut-02"):
            directory = original / name
            directory.mkdir(parents=True)
            (directory / "timeline.jsonl").write_bytes(_line(name))
        moved = tmp_path / "moved"
        original.rename(moved)

        for name in ("dut-01", "dut-02"):
            reference = f"{name}/timeline.jsonl"
            result = await TimelineReader().read_reference(moved, reference)
            assert result.reference == reference
            assert result.path == moved / reference
            assert len(result.rows) == 1
            row = result.rows[0]
            assert isinstance(row, TimelineRecord)
            assert row.data["message"] == name

    @pytest.mark.parametrize(
        "reference", ["", ".", "../outside.jsonl", "dut/../../outside.jsonl", "/outside.jsonl"]
    )
    async def test_invalid_reference_is_unavailable_without_opening_a_file(
        self, tmp_path: Path, reference: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse_open(*args: object, **kwargs: object) -> None:
            pytest.fail("an invalid reference reached file open")

        monkeypatch.setattr(Path, "open", refuse_open)
        result = await TimelineReader().read_reference(tmp_path, reference)
        assert result.reference == reference
        assert result.path is None
        assert len(result.rows) == 1
        diagnostic = result.rows[0]
        assert isinstance(diagnostic, TimelineDiagnostic)
        assert diagnostic.kind == "unavailable"
        assert diagnostic.line_number is None
        assert reference in diagnostic.message

    @pytest.mark.parametrize("directory_link", [False, True])
    async def test_symlink_escape_cannot_read_another_runs_timeline(
        self, tmp_path: Path, directory_link: bool
    ) -> None:
        root = tmp_path / "run"
        outside = tmp_path / "neighbor"
        root.mkdir()
        outside.mkdir()
        (outside / "timeline.jsonl").write_bytes(_line("wrong run"))
        if directory_link:
            (root / "dut").symlink_to(outside, target_is_directory=True)
            reference = "dut/timeline.jsonl"
        else:
            (root / "timeline.jsonl").symlink_to(outside / "timeline.jsonl")
            reference = "timeline.jsonl"

        result = await TimelineReader().read_reference(root, reference)
        assert result.reference == reference
        assert result.path is None
        assert len(result.rows) == 1
        diagnostic = result.rows[0]
        assert isinstance(diagnostic, TimelineDiagnostic)
        assert diagnostic.kind == "unavailable"
        assert "escapes" in diagnostic.message

    @pytest.mark.parametrize("target", ["missing", "directory", "loop", "missing-root"])
    async def test_unavailable_paths_retain_the_reference_and_do_not_poison_the_next_read(
        self, tmp_path: Path, target: str
    ) -> None:
        root = tmp_path
        path = root / "timeline.jsonl"
        if target == "directory":
            path.mkdir()
        elif target == "loop":
            path.symlink_to(path)
        elif target == "missing-root":
            root = root / "absent"
        reader = TimelineReader()

        result = await reader.read_reference(root, "timeline.jsonl")
        assert result.reference == "timeline.jsonl"
        assert result.path is None
        diagnostic = result.rows[0]
        assert isinstance(diagnostic, TimelineDiagnostic)
        assert diagnostic.kind == "unavailable"

        (tmp_path / "good.jsonl").write_bytes(_line("available"))
        good = await reader.read_reference(tmp_path, "good.jsonl")
        assert isinstance(good.rows[0], TimelineRecord)

    async def test_internal_symlink_keeps_reference_and_reports_the_actual_file(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "actual.jsonl"
        path.write_bytes(_line("same run"))
        (tmp_path / "alias.jsonl").symlink_to(path)
        result = await TimelineReader().read_reference(tmp_path, "alias.jsonl")
        assert result.reference == "alias.jsonl"
        assert result.path == path
        assert isinstance(result.rows[0], TimelineRecord)

    async def test_complete_records_and_damage_keep_their_physical_positions(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "timeline.jsonl"
        raw = _line("before") + b"not JSON\n" + _line("after") + b'{"message":"cut'
        path.write_bytes(raw)
        result = await TimelineReader().read_reference(tmp_path, path.name)
        assert result.reference == path.name
        assert [row.line_number for row in result.rows] == [1, 2, 3, 4]
        assert isinstance(result.rows[0], TimelineRecord)
        assert isinstance(result.rows[2], TimelineRecord)
        invalid, truncated = result.rows[1], result.rows[3]
        assert isinstance(invalid, TimelineDiagnostic)
        assert invalid.kind == "invalid-record"
        assert isinstance(truncated, TimelineDiagnostic)
        assert truncated.kind == "truncated"
        assert path.read_bytes() == raw
