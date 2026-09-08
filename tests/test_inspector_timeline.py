"""The Inspector preserves readable chronology without repairing damaged evidence."""

import json
from importlib.metadata import distribution
from pathlib import Path
from typing import cast

import pytest

from litmus import PASS, Artifact, LitmusTest, TestFailure, TestUtils, Verdict
from litmus.inspector_timeline import TimelineDiagnostic, TimelineReader, TimelineRecord
from litmus.inspector_tree import InspectorTree, TreeReader
from litmus.run_data import JsonObject, JsonValue


def _record(**changes: JsonValue) -> JsonObject:
    return {
        "format_version": 1,
        "timestamp": "2026-09-07T13:00:00Z",
        "scope": ["rack", "dut/01"],
        "level": "info",
        "message": "voltage observed\nwithin limit",
        **changes,
    }


def _line(record: JsonObject) -> bytes:
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


class TestTimelineReader:
    def test_installed_schema_is_the_published_contract(self) -> None:
        published = Path(__file__).resolve().parents[1] / "docs/schema/timeline-v1.schema.json"
        installed = distribution("litmus").locate_file("litmus_data/timeline-v1.schema.json")
        assert installed.read_bytes() == published.read_bytes()

    async def test_keeps_order_scope_pointer_and_additive_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "timeline.jsonl"
        records = [
            _record(message="π voltage", future={"reading": [3.3, None]}),
            _record(
                artifact_name="rails",
                artifact_path="/original/run/dut/rails.json",
                artifact_type="survey",
                artifact_timestamp="2026-09-07T13:00:00.123456Z",
            ),
        ]
        path.write_bytes(b"".join(_line(record) for record in records))
        before = path.read_bytes()

        result = await TimelineReader().read(path)

        assert result.path == path
        assert result.rows == tuple(
            TimelineRecord(i, record) for i, record in enumerate(records, 1)
        )
        assert path.read_bytes() == before

    @pytest.mark.parametrize("tail", [b'{"format_version":', b'{"message":"\xcf'])
    async def test_truncated_tail_preserves_complete_preceding_records(
        self, tmp_path: Path, tail: bytes
    ) -> None:
        path = tmp_path / "timeline.jsonl"
        path.write_bytes(_line(_record()) + tail)

        result = await TimelineReader().read(path)

        assert result.rows[0] == TimelineRecord(1, _record())
        diagnostic = result.rows[1]
        assert isinstance(diagnostic, TimelineDiagnostic)
        assert diagnostic.kind == "truncated"
        assert diagnostic.line_number == 2
        assert "final line" in diagnostic.message

    @pytest.mark.parametrize("ending", [b"", b"\r\n", b"\n"])
    async def test_complete_final_record_needs_no_newline(
        self, tmp_path: Path, ending: bytes
    ) -> None:
        path = tmp_path / "timeline.jsonl"
        path.write_bytes(_line(_record()).rstrip(b"\n") + ending)
        assert (await TimelineReader().read(path)).rows == (TimelineRecord(1, _record()),)

    @pytest.mark.parametrize(
        "damaged",
        [
            b"not json\n",
            b"\xff\n",
            b"\n",
            b"[]\n",
            b"null\n",
            b'{"format_version":1,"future":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}\n",
            b'{"message":"first","message":"second"}\n',
            _line(_record(extra=float("nan"))),
            _line(_record(extra=float("inf"))),
            _line(_record(extra=1.0)).replace(b"1.0", b"1e9999"),
            _line(_record(scope=[])),
            _line(_record(scope=["rack", 1])),
            _line(_record(timestamp="yesterday")),
            _line(_record(level="")),
            _line(_record(message=None)),
            _line(_record(artifact_name="orphan")),
            _line(_record()).replace(b'"message": ', b'"renamed": '),
        ],
    )
    async def test_malformed_complete_record_is_visible_between_good_records(
        self, tmp_path: Path, damaged: bytes
    ) -> None:
        path = tmp_path / "timeline.jsonl"
        path.write_bytes(_line(_record()) + damaged + _line(_record(message="after damage")))

        result = await TimelineReader().read(path)

        assert result.rows[0] == TimelineRecord(1, _record())
        diagnostic = result.rows[1]
        assert isinstance(diagnostic, TimelineDiagnostic)
        assert diagnostic.kind == "invalid-record"
        assert diagnostic.line_number == 2
        assert diagnostic.message
        assert result.rows[2] == TimelineRecord(3, _record(message="after damage"))

    @pytest.mark.parametrize("version", [2, 0, "1", True, None, 1.0])
    async def test_version_is_checked_before_other_fields_on_every_line(
        self, tmp_path: Path, version: JsonValue
    ) -> None:
        path = tmp_path / "timeline.jsonl"
        path.write_bytes(_line(_record()) + _line({"format_version": version}))

        diagnostic = (await TimelineReader().read(path)).rows[1]

        assert isinstance(diagnostic, TimelineDiagnostic)
        assert diagnostic.kind == "unsupported-version"
        assert diagnostic.line_number == 2
        assert repr(version) in diagnostic.message
        assert "accepts integer 1" in diagnostic.message

    async def test_missing_version_is_explicit(self, tmp_path: Path) -> None:
        path = tmp_path / "timeline.jsonl"
        path.write_bytes(b"{}\n")
        diagnostic = (await TimelineReader().read(path)).rows[0]
        assert isinstance(diagnostic, TimelineDiagnostic)
        assert diagnostic.kind == "unsupported-version"
        assert "missing" in diagnostic.message

    async def test_schema_error_without_final_newline_is_not_truncation(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "timeline.jsonl"
        path.write_bytes(b'{"format_version":1}')
        diagnostic = (await TimelineReader().read(path)).rows[0]
        assert isinstance(diagnostic, TimelineDiagnostic)
        assert diagnostic.kind == "invalid-record"

    async def test_empty_log_is_available_and_reusing_reader_does_not_retain_rows(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "timeline.jsonl"
        reader = TimelineReader()
        path.write_bytes(_line(_record()))
        assert len((await reader.read(path)).rows) == 1
        path.write_bytes(b"")
        assert (await reader.read(path)).rows == ()

    @pytest.mark.parametrize("exists", [False, True])
    async def test_missing_log_and_directory_are_unavailable(
        self, tmp_path: Path, exists: bool
    ) -> None:
        path = tmp_path if exists else tmp_path / "absent.jsonl"
        result = await TimelineReader().read(path)
        assert result.path == path
        diagnostic = result.rows[0]
        assert isinstance(diagnostic, TimelineDiagnostic)
        assert diagnostic.kind == "unavailable"
        assert diagnostic.line_number is None
        assert str(path) in diagnostic.message

    async def test_reads_every_timeline_from_a_moved_seventy_dut_saved_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Dut(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("voltage", self._voltage)

            async def _voltage(self, utils: TestUtils) -> Verdict:
                utils.logger.info("voltage observed: 3.3 V")
                capture = await utils.capture_artifact(
                    Artifact(name="voltage", type="survey", data={"voltage_v": 3.3})
                )
                if capture:
                    return PASS
                raise TestFailure("voltage capture unavailable")

        class Rack(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel("devices", [Dut(name=f"dut-{i:02d}") for i in range(70)])

        suite = Rack(name="rack")
        await suite.run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path / "original")})
        assert suite.log_path is not None
        original = suite.log_path.parent.parent
        moved = tmp_path / "moved"
        original.rename(moved)

        def refuse_run(*args: object, **kwargs: object) -> None:
            pytest.fail("saved timeline inspection must not execute suite code")

        monkeypatch.setattr(LitmusTest, "run", refuse_run)
        tree = await TreeReader().read(moved / "result.json")
        assert isinstance(tree, InspectorTree)
        references = [
            cast(str, row.record["log_path"])
            for row in tree.rows
            if row.record["log_path"] is not None
        ]
        assert len(references) == 72
        captures: list[JsonObject] = []
        for reference in references:
            path = moved / reference
            before = path.read_bytes()
            result = await TimelineReader().read_reference(moved, reference)
            assert result.reference == reference
            assert result.path == path
            expected = [
                cast(JsonObject, json.loads(line)) for line in path.read_bytes().splitlines()
            ]
            assert result.rows == tuple(
                TimelineRecord(i, record) for i, record in enumerate(expected, 1)
            )
            assert path.read_bytes() == before
            captures.extend(
                record for record in expected if record.get("artifact_name") == "voltage"
            )
        assert len(captures) == 70
        assert len({json.dumps(record["scope"]) for record in captures}) == 70
