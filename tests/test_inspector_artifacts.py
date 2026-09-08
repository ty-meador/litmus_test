"""Saved captures stay attached to their run, even after the directory moves."""

import asyncio
import json
from importlib.metadata import distribution
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from litmus import PASS, Artifact, LitmusTest, TestUtils, Verdict
from litmus.inspector_artifacts import ArtifactContent, ArtifactDiagnostic, ArtifactReader
from litmus.inspector_timeline import TimelineReader, TimelineRecord
from litmus.run_data import JsonObject, JsonValue


def _capture(**changes: JsonValue) -> JsonObject:
    return {
        "format_version": 1,
        "name": "voltage",
        "type": "survey",
        "timestamp": "2026-09-08T05:00:00Z",
        "data": {"voltage_v": 3.3},
        **changes,
    }


class TestArtifactReader:
    @pytest.mark.parametrize("name", ["artifact", "result"])
    def test_installed_schema_is_the_published_contract(self, name: str) -> None:
        relative = f"{name}-v1.schema.json"
        published = Path(__file__).resolve().parents[1] / "docs/schema" / relative
        installed = distribution("litmus").locate_file(f"litmus_data/{relative}")
        assert installed.read_bytes() == published.read_bytes()

    @pytest.mark.parametrize("data", [None, {}, [], "", False, 0, "π"])
    async def test_preserves_empty_null_and_additive_evidence_without_editing_it(
        self, tmp_path: Path, data: JsonValue
    ) -> None:
        path = tmp_path / "capture.json"
        record = _capture(data=data, future={"reading": [None, 3.3]})
        original = json.dumps(record, ensure_ascii=False).encode()
        path.write_bytes(original)

        result = await ArtifactReader(tmp_path).read(str(path))

        assert result == ArtifactContent(str(path), path, record)
        assert path.read_bytes() == original

    async def test_relocation_keeps_full_tail_and_never_falls_back_to_the_old_run(
        self, tmp_path: Path
    ) -> None:
        old = tmp_path / "original"
        moved = tmp_path / "moved"
        for root in (old, moved):
            for node in ("dut-01", "dut-02"):
                target = root / node / "capture.json"
                target.parent.mkdir(parents=True)
                target.write_text(json.dumps(_capture(data=f"{root.name}/{node}")))
        reader = ArtifactReader(moved, recorded_root=PurePosixPath(old))

        for node in ("dut-01", "dut-02"):
            result = await reader.read(str(old / node / "capture.json"))
            assert isinstance(result, ArtifactContent)
            assert result.data["data"] == f"moved/{node}"
            assert result.path == moved / node / "capture.json"

        (moved / "dut-01/capture.json").unlink()
        reference = str(old / "dut-01/capture.json")
        missing = await reader.read(reference)
        assert isinstance(missing, ArtifactDiagnostic)
        assert missing.kind == "unavailable"
        assert missing.reference == reference
        assert reference in missing.message

    @pytest.mark.parametrize(
        "reference", ["/unknown/run/capture.json", "capture.json", "/old/run/../capture.json"]
    )
    async def test_unknown_relative_and_traversing_references_do_not_use_basename_matches(
        self, tmp_path: Path, reference: str
    ) -> None:
        (tmp_path / "capture.json").write_text(json.dumps(_capture()))
        result = await ArtifactReader(tmp_path).read(reference)
        assert isinstance(result, ArtifactDiagnostic)
        assert result.kind == "unavailable"
        assert result.reference == reference
        assert reference in result.message

    async def test_mapping_must_be_absolute_and_cannot_traverse(self, tmp_path: Path) -> None:
        for root in (PurePosixPath("old/run"), PurePosixPath("/old/../run")):
            with pytest.raises(ValueError, match="recorded root"):
                ArtifactReader(tmp_path, recorded_root=root)

    @pytest.mark.parametrize("directory_link", [False, True])
    async def test_symlinks_cannot_escape_the_opened_root(
        self, tmp_path: Path, directory_link: bool
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "capture.json"
        target.write_text(json.dumps(_capture(data="another run")))
        root = tmp_path / "run"
        root.mkdir()
        link = root / "link"
        link.symlink_to(outside if directory_link else target)
        pointer = link / "capture.json" if directory_link else link

        result = await ArtifactReader(root).read(str(pointer))

        assert isinstance(result, ArtifactDiagnostic)
        assert result.kind == "unavailable"
        assert str(pointer) in result.message

    @pytest.mark.parametrize("exists", [False, True])
    async def test_missing_file_or_directory_is_unavailable(
        self, tmp_path: Path, exists: bool
    ) -> None:
        reference = str(tmp_path if exists else tmp_path / "absent")
        result = await ArtifactReader(tmp_path).read(reference)
        assert isinstance(result, ArtifactDiagnostic)
        assert result.kind == "unavailable"
        assert reference in result.message

    @pytest.mark.parametrize("version", [2, 0, "1", True, None, 1.0])
    async def test_reads_version_before_any_other_envelope_field(
        self, tmp_path: Path, version: JsonValue
    ) -> None:
        path = tmp_path / "capture.json"
        path.write_text(json.dumps({"format_version": version}))
        result = await ArtifactReader(tmp_path).read(str(path))
        assert isinstance(result, ArtifactDiagnostic)
        assert result.kind == "unsupported-version"
        assert repr(version) in result.message

    @pytest.mark.parametrize(
        "raw",
        [
            b"not json",
            b"\xff",
            b"[]",
            b"null",
            b'{"format_version":1}',
            b'{"format_version":1,"data":null,"data":3}',
            json.dumps(_capture(data=float("nan"))).encode(),
            json.dumps(_capture(data=1.0)).encode().replace(b"1.0", b"1e9999"),
            json.dumps(_capture(name="../capture")).encode(),
            json.dumps(_capture(name="capture\n")).encode(),
            json.dumps(_capture(type="")).encode(),
            json.dumps(_capture(timestamp="yesterday")).encode(),
            b'{"format_version":1,"data":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
        ],
        ids=[
            "not-json",
            "invalid-utf8",
            "array",
            "null",
            "missing-fields",
            "duplicate-key",
            "nan",
            "overflow",
            "unsafe-name",
            "newline-name",
            "empty-type",
            "bad-timestamp",
            "deep-input",
        ],
    )
    async def test_damaged_capture_is_a_diagnostic_and_cannot_poison_the_next_read(
        self, tmp_path: Path, raw: bytes
    ) -> None:
        path = tmp_path / "capture.json"
        path.write_bytes(raw)
        reader = ArtifactReader(tmp_path)
        result = await reader.read(str(path))
        assert isinstance(result, ArtifactDiagnostic)
        assert result.kind == "invalid-record"
        assert result.reference == str(path)
        assert path.read_bytes() == raw
        path.write_text(json.dumps(_capture()))
        assert isinstance(await reader.read(str(path)), ArtifactContent)

    async def test_missing_version_is_explicit(self, tmp_path: Path) -> None:
        path = tmp_path / "capture.json"
        path.write_text("{}")
        result = await ArtifactReader(tmp_path).read(str(path))
        assert isinstance(result, ArtifactDiagnostic)
        assert result.kind == "unsupported-version"
        assert "missing" in result.message

    async def test_reads_captures_and_root_result_from_a_moved_seventy_dut_run(
        self, tmp_path: Path
    ) -> None:
        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, str]:
                return {"dut": self.name}

            def build_suite(self) -> LitmusTest:
                return self.test("snapshot", self._snapshot)

            async def _snapshot(self, utils: TestUtils) -> Verdict:
                captured = await utils.capture_artifact(
                    Artifact(name="voltage", type="survey", data={"dut": utils.ctx["dut"]})
                )
                if captured:
                    return PASS
                raise AssertionError("capture did not settle")

        class Rack(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel("devices", [Suite(name=f"dut-{i:02d}") for i in range(70)])

        suite = Rack(name="rack")
        result = await suite.run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path / "original")})
        assert suite.log_path is not None
        original_root = suite.log_path.parent.parent
        paths = await asyncio.to_thread(lambda: sorted(original_root.rglob("timeline.jsonl")))
        timelines = [await TimelineReader().read(path) for path in paths]
        moved = tmp_path / "moved"
        original_root.rename(moved)
        reader = ArtifactReader(moved, recorded_root=PurePosixPath(original_root))
        captures: list[JsonObject] = []
        results: list[JsonObject] = []

        for row in (row for timeline in timelines for row in timeline.rows):
            assert isinstance(row, TimelineRecord)
            if "artifact_path" not in row.data:
                continue
            reference = cast(str, row.data["artifact_path"])
            kind = "result" if row.data["artifact_type"] == "run-result" else "artifact"
            loaded = await reader.read(reference, kind=kind)
            assert isinstance(loaded, ArtifactContent)
            assert loaded.reference == reference
            assert loaded.path.is_relative_to(moved)
            if kind == "result":
                results.append(loaded.data)
            else:
                captures.append(loaded.data)

        assert len(captures) == 70
        names: list[str] = []
        for capture in captures:
            observation = capture["data"]
            assert isinstance(observation, dict)
            name = observation["dut"]
            assert isinstance(name, str)
            names.append(name)
        assert sorted(names) == [f"dut-{i:02d}" for i in range(70)]
        assert results == [result.to_json()]
        wrong_decoder = await reader.read(str(original_root / "result.json"))
        assert isinstance(wrong_decoder, ArtifactDiagnostic)
        assert wrong_decoder.kind == "invalid-record"
