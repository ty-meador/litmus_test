"""The published Inspector JSON schemas hold over every record the framework emits."""

import copy
import json
import signal
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from litmus import (
    PASS,
    Artifact,
    ContractViolation,
    ExternalIntegrationFailure,
    LitmusTest,
    MissingVerdict,
    OperatorInterrupt,
    RunResult,
    SummaryTreeNode,
    TestFailure,
    TestUtils,
    Verdict,
)
from litmus.errors import Failure
from litmus.lifecycle import Slot, State, TimeoutEvidence
from litmus.results import NodeType

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "docs" / "schema"
RESULT_SCHEMA = SCHEMA_DIR / "result-v1.schema.json"
TIMELINE_SCHEMA = SCHEMA_DIR / "timeline-v1.schema.json"

type Json = dict[str, Any]


def _load(path: Path) -> Json:
    return cast(Json, json.loads(path.read_text(encoding="utf-8")))


def _closed(schema: Json) -> Json:
    """Return a copy in which every object that names its properties admits nothing else.

    The published file stays open so a consumer accepts the additive fields of a later
    release. The framework's own gate is stricter: an emitted field the schema does not
    name is a red test, not a silent contract change. An object that declares
    ``additionalProperties`` itself keeps that declaration.
    """

    def close(node: object) -> None:
        if isinstance(node, dict):
            mapping = cast(Json, node)
            if "properties" in mapping and "additionalProperties" not in mapping:
                mapping["additionalProperties"] = False
            for value in mapping.values():
                close(value)
        elif isinstance(node, list):
            for item in cast(list[object], node):
                close(item)

    copied = copy.deepcopy(schema)
    close(copied)
    return copied


def _validator(path: Path, *, closed: bool) -> Draft202012Validator:
    schema = _load(path)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(_closed(schema) if closed else schema)


def _errors(validator: Draft202012Validator, instance: Json) -> list[str]:
    """Every violation as ``path: message``, sorted so a failure reads the same each run.

    ``jsonschema`` ships no ``py.typed``; its ``iter_errors`` carries an untyped second
    overload, so this one call site takes the targeted ignore rather than every test.
    """
    found: Iterable[ValidationError] = validator.iter_errors(instance)  # pyright: ignore[reportUnknownMemberType]
    return sorted(f"{list(error.path)}: {error.message}" for error in found)


def _assert_valid(validator: Draft202012Validator, instance: Json) -> None:
    errors = _errors(validator, instance)
    assert not errors, "\n".join(errors)


def _accepts(validator: Draft202012Validator, instance: Json) -> bool:
    return not _errors(validator, instance)


def _round_trip(result: RunResult) -> Json:
    return cast(Json, json.loads(json.dumps(result.to_json(), allow_nan=False)))


def _root_result(tree: SummaryTreeNode, *, failure: Failure | None = None) -> RunResult:
    return RunResult(
        slot=Slot.TEST_METHOD,
        node=tree.name,
        state=tree.state or State.PASS,
        failure=failure,
        timeout=tree.timeout,
        ancestor_names=[tree.name],
        bread_crumbs=tree.name,
        summary_tree=tree,
        type=tree.type,
    )


def _records(path: Path) -> list[Json]:
    return [cast(Json, json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]


class TestPublishedSchemas:
    def test_schema_files_are_valid_2020_12_documents(self) -> None:
        result = _load(RESULT_SCHEMA)
        timeline = _load(TIMELINE_SCHEMA)
        for schema in (result, timeline):
            Draft202012Validator.check_schema(schema)
            assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
            assert schema["properties"]["format_version"] == {
                "const": 1,
                "description": schema["properties"]["format_version"]["description"],
            }
        assert result["$id"] != timeline["$id"]

    def test_closing_keeps_the_objects_the_policy_leaves_open(self) -> None:
        closed = _closed(_load(RESULT_SCHEMA))
        assert closed["additionalProperties"] is False
        assert closed["$defs"]["summaryTreeNode"]["additionalProperties"] is False
        assert closed["$defs"]["timeout"]["additionalProperties"] is False
        assert closed["$defs"]["failure"]["additionalProperties"] is True
        assert closed["properties"]["run_data"]["additionalProperties"] is True
        assert closed["properties"]["env"]["additionalProperties"] == {"type": "string"}


class TestEmittedRecordsValidate:
    async def test_a_real_run_validates_its_result_and_every_timeline_line(
        self, tmp_path: Path
    ) -> None:
        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self._checks = self.sequence(
                    "checks",
                    [
                        self.test("snapshot", self._snapshot),
                        self.test("rail", self._rail),
                        self.test("unreached", self._unreached),
                    ],
                )

            def build_suite(self) -> LitmusTest:
                return self._checks

            async def _snapshot(self, utils: TestUtils) -> Verdict:
                utils.logger.info("line one\nline two")
                await utils.capture_artifact(
                    Artifact(name="pre-flash", type="survey", data={"ready": True})
                )
                return PASS

            async def _rail(self, _utils: TestUtils) -> Verdict:
                raise TestFailure("rail outside tolerance", expected=3.3, received=2.9)

            async def _unreached(self, _utils: TestUtils) -> Verdict:
                return PASS

        suite = Suite(name="rack/name")
        result = await suite.run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})
        assert result.state is State.FAIL
        assert suite.log_path is not None
        run_root = suite.log_path.parent.parent

        result_validator = _validator(RESULT_SCHEMA, closed=True)
        persisted = _load(run_root / "result.json")
        _assert_valid(result_validator, persisted)
        _assert_valid(result_validator, _round_trip(result))
        states = [child["state"] for child in persisted["summary_tree"]["children"][0]["children"]]
        assert states == ["Pass", "Fail", None]

        timeline_validator = _validator(TIMELINE_SCHEMA, closed=True)
        files = sorted(run_root.rglob("timeline.jsonl"))
        assert len(files) == 2
        records = [record for path in files for record in _records(path)]
        for record in records:
            _assert_valid(timeline_validator, record)
        pointers = {r["artifact_type"] for r in records if "artifact_type" in r}
        assert pointers == {"survey", "run-result"}

    @pytest.mark.parametrize(
        "failure",
        [
            TestFailure("rail outside tolerance", expected={"volts": 3.3}, received=object()),
            ContractViolation("rail", "mutated self", cause=RuntimeError("boom")),
            MissingVerdict("rail", returned=None),
            ExternalIntegrationFailure("before_each", RuntimeError("hook threw")),
            OperatorInterrupt(signal.SIGINT),
        ],
        ids=lambda failure: type(failure).__name__,
    )
    def test_every_failure_shape_validates(self, failure: Failure) -> None:
        state = State.FAIL if isinstance(failure, TestFailure) else State.ABORTED
        tree = SummaryTreeNode(
            name="rack",
            type=NodeType.SUITE,
            state=state,
            failure=failure,
            log_path="0001-rack-0123456789ab/timeline.jsonl",
        )
        payload = _round_trip(_root_result(tree, failure=failure))
        _assert_valid(_validator(RESULT_SCHEMA, closed=True), payload)
        assert payload["failure"]["name"] == type(failure).__name__

    def test_timeout_evidence_and_pending_descendants_validate(self) -> None:
        timeout = TimeoutEvidence(
            reason="timeout",
            declared="abort_after",
            budget_seconds=1.5,
            elapsed_seconds=1.51,
            clock="rack/checks",
        )
        tree = SummaryTreeNode(
            name="rack",
            type=NodeType.SUITE,
            state=State.ABORTED,
            timeout=timeout,
            log_path="0001-rack-0123456789ab/timeline.jsonl",
            children=(
                SummaryTreeNode(
                    name="checks",
                    type=NodeType.SEQUENCE,
                    state=State.ABORTED,
                    timeout=timeout,
                    log_path="0002-checks-0123456789ab/timeline.jsonl",
                    children=(SummaryTreeNode(name="later", type=NodeType.TEST, state=None),),
                ),
            ),
        )
        _assert_valid(_validator(RESULT_SCHEMA, closed=True), _round_trip(_root_result(tree)))


class TestSchemasRefuseTheBreaksThePolicyNames:
    @pytest.fixture
    def result_payload(self) -> Json:
        tree = SummaryTreeNode(
            name="rack",
            type=NodeType.SUITE,
            state=State.PASS,
            log_path="0001-rack-0123456789ab/timeline.jsonl",
        )
        return _round_trip(_root_result(tree))

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("format_version",), 2),
            (("state",), "Skipped"),
            (("type",), "test-suite"),
            (("summary_tree", "log_path"), 7),
            (("summary_tree", "children"), None),
            (("env", "LOT"), 42),
            (("run_data", "measurements"), {}),
        ],
    )
    def test_result_schema_rejects_type_and_set_changes(
        self, result_payload: Json, path: tuple[str, ...], value: object
    ) -> None:
        target = result_payload
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        assert not _accepts(_validator(RESULT_SCHEMA, closed=False), result_payload)

    def test_result_schema_rejects_a_missing_required_field(self, result_payload: Json) -> None:
        del result_payload["summary_tree"]
        assert not _accepts(_validator(RESULT_SCHEMA, closed=False), result_payload)

    def test_closed_gate_rejects_an_undocumented_field_the_consumer_accepts(
        self, result_payload: Json
    ) -> None:
        result_payload["duration_seconds"] = 0.25
        assert _accepts(_validator(RESULT_SCHEMA, closed=False), result_payload)
        assert not _accepts(_validator(RESULT_SCHEMA, closed=True), result_payload)

    def test_open_objects_stay_open_under_the_closed_gate(self, result_payload: Json) -> None:
        result_payload["run_data"]["station_notes"] = ["bench 4"]
        result_payload["env"]["LOT"] = "42"
        _assert_valid(_validator(RESULT_SCHEMA, closed=True), result_payload)

    @pytest.mark.parametrize(
        "record",
        [
            {
                "format_version": 2,
                "timestamp": "2026-09-05T17:00:00Z",
                "scope": ["rack"],
                "level": "info",
                "message": "later version",
            },
            {
                "format_version": 1,
                "timestamp": "2026-09-05T17:00:00+00:00",
                "scope": ["rack"],
                "level": "info",
                "message": "offset instead of Z",
            },
            {
                "format_version": 1,
                "timestamp": "2026-09-05T17:00:00Z",
                "scope": [],
                "level": "info",
                "message": "empty scope",
            },
            {
                "format_version": 1,
                "timestamp": "2026-09-05T17:00:00Z",
                "scope": ["rack"],
                "level": "info",
                "message": "half a pointer",
                "artifact_name": "pre-flash",
                "artifact_path": "/tmp/run/pre-flash.json",
            },
        ],
        ids=["later-version", "offset-timestamp", "empty-scope", "partial-pointer"],
    )
    def test_timeline_schema_rejects_malformed_records(self, record: Json) -> None:
        assert not _accepts(_validator(TIMELINE_SCHEMA, closed=False), record)

    def test_timeline_schema_accepts_a_complete_pointer(self) -> None:
        record = {
            "format_version": 1,
            "timestamp": "2026-09-05T17:00:00.123456Z",
            "scope": ["rack", "checks", "snapshot"],
            "level": "info",
            "message": "artifact captured [survey:pre-flash] -> /tmp/run/pre-flash.json",
            "artifact_name": "pre-flash",
            "artifact_path": "/tmp/run/pre-flash.json",
            "artifact_type": "survey",
            "artifact_timestamp": "2026-09-05T17:00:00Z",
        }
        _assert_valid(_validator(TIMELINE_SCHEMA, closed=True), record)
