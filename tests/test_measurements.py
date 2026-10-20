"""Parametric measurements (issue #86, design note ``docs/measurements.md``).

A limit is evidence, never a verdict. These tests hold the framework to the signed note:
the recorded shape, the four tier-1 guards through both spellings of the call, sealing
on every terminal state, and publication on the node's own ``run_data`` and on every
``summary_tree`` node so a leaf's readings reach ``result.json``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, ValidationError

from litmus import (
    PASS,
    EvidenceViolation,
    JsonObject,
    LitmusTest,
    Measurement,
    NodeType,
    Phase,
    State,
    TestFailure,
    TestUtils,
    Verdict,
    evidence,
    measure,
)
from litmus.errors import (
    MeasurementDuplicate,
    MeasurementInvalid,
    MeasurementSealed,
    MeasurementUnbound,
    MeasurementViolation,
)
from litmus.results import SummaryTreeNode
from litmus.run_data import JsonValue

type Recorder = Callable[[TestUtils], object]
"""A scripted call made from inside a lifecycle slot; what it does is the test."""

type Spelling = Callable[..., Measurement]
"""One of the two ways to spell the call; both must obey every rule identically."""

RESULT_SCHEMA = Path(__file__).resolve().parent.parent / "docs" / "schema" / "result-v1.schema.json"

_NOT_NUMERIC = "value must be an int, a finite float, or None"
"""The refusal every non-number but ``None`` earns; ``None`` is the reading not taken (#159)."""


def _via_utils(utils: TestUtils, **call: Any) -> Measurement:
    """The documented home of the call."""
    return utils.measure(**call)


def _via_root(utils: TestUtils, **call: Any) -> Measurement:
    """The package-root alias; ``utils`` is deliberately unused so the binding is what resolves."""
    del utils
    return measure(**call)


SPELLINGS = pytest.mark.parametrize(
    "spelling", [_via_utils, _via_root], ids=["utils.measure", "litmus.measure"]
)


class _Bench(LitmusTest):
    """One authored node with one claim; each slot runs the recorder it was given."""

    def __init__(
        self,
        *,
        name: str,
        during_test: Recorder | None = None,
        during_configure: Recorder | None = None,
        during_cleanup: Recorder | None = None,
    ) -> None:
        super().__init__(name=name)
        self._during_test = during_test
        self._during_configure = during_configure
        self._during_cleanup = during_cleanup

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        if self._during_configure is not None:
            self._during_configure(utils)
        return {"station": "bench-1"}

    def build_suite(self) -> LitmusTest:
        return self.test("claim", self._claim)

    async def _claim(self, utils: TestUtils) -> Verdict:
        if self._during_test is not None:
            self._during_test(utils)
        return PASS

    async def cleanup(self, utils: TestUtils) -> None:
        if self._during_cleanup is not None:
            self._during_cleanup(utils)


def _leaf(tree: SummaryTreeNode, *path: int) -> SummaryTreeNode:
    node = tree
    for index in path or (0,):
        node = node.children[index]
    return node


def _only(node: SummaryTreeNode) -> JsonObject:
    assert node.measurements is not None
    [entry] = node.measurements
    return entry


def _accepts(validator: Draft202012Validator, payload: JsonObject) -> bool:
    """``jsonschema`` ships no ``py.typed``; the one call site takes the targeted ignore."""
    found: Iterable[ValidationError] = validator.iter_errors(payload)  # pyright: ignore[reportUnknownMemberType]
    return not list(found)


def _call_id(item: object) -> str | None:
    """Name a refused-argument case by the measurement it tried to record."""
    return str(item["name"]) if isinstance(item, dict) else None  # pyright: ignore[reportUnknownArgumentType]


def _minimal_result(tree: JsonObject) -> JsonObject:
    """The smallest version-1 root record around one tree node."""
    return {
        "format_version": 1,
        "state": tree["state"],
        "failure": None,
        "timeout": None,
        "test_case": tree["name"],
        "type": tree["type"],
        "ancestor_names": [tree["name"]],
        "bread_crumbs": tree["name"],
        "summary_tree": tree,
        "physical_location": "unknown",
        "env": {},
        "run_data": {"measurements": []},
    }


class TestRecordedShape:
    async def test_a_reading_in_window_is_recorded_beside_an_untouched_verdict(self) -> None:
        def record(utils: TestUtils) -> None:
            utils.measure("output_voltage_v", 5.02, unit="V", limits=(4.75, 5.25))

        result = await _Bench(name="psu", during_test=record).run()

        assert result.state is State.PASS
        entry = _only(_leaf(result.summary_tree))
        timestamp = entry.pop("timestamp")
        assert isinstance(timestamp, str)
        assert timestamp.endswith("Z")
        assert datetime.fromisoformat(timestamp).tzinfo is not None
        assert entry == {
            "name": "output_voltage_v",
            "value": 5.02,
            "unit": "V",
            "limits": {"low": 4.75, "high": 5.25},
            "in_limits": True,
        }
        # The authored node ran and measured nothing: an empty record, not null.
        assert result.summary_tree.measurements == ()
        assert result.run_data == {"measurements": [], "station": "bench-1"}

    async def test_a_reading_out_of_window_is_recorded_and_the_state_stays_pass(self) -> None:
        def record(utils: TestUtils) -> None:
            utils.measure("output_voltage_v", 5.31, unit="V", limits=(4.75, 5.25))

        result = await _Bench(name="psu", during_test=record).run()

        assert result.state is State.PASS
        assert result.failure is None
        assert _only(_leaf(result.summary_tree))["in_limits"] is False

    @pytest.mark.parametrize(
        ("limits", "value", "in_limits"),
        [
            (None, 3, None),
            ((None, 250.0), 100, True),
            ((None, 250.0), 250.0, True),
            ((0, None), -1, False),
            ((4.75, 5.25), 4.75, True),
            ((4.75, 5.25), 5.25, True),
        ],
        ids=["no-window", "open-low", "high-inclusive", "open-high", "low-inclusive", "at-high"],
    )
    async def test_the_window_is_inclusive_and_may_be_open_on_one_side(
        self, limits: tuple[float | None, float | None] | None, value: float, in_limits: bool | None
    ) -> None:
        def record(utils: TestUtils) -> None:
            utils.measure("reading", value, limits=limits)

        result = await _Bench(name="psu", during_test=record).run()

        entry = _only(_leaf(result.summary_tree))
        assert entry["in_limits"] is in_limits
        expected_limits = None if limits is None else {"low": limits[0], "high": limits[1]}
        assert entry["limits"] == expected_limits
        assert entry["unit"] is None

    async def test_readings_keep_call_order_and_the_record_survives_json(self) -> None:
        def record(utils: TestUtils) -> None:
            utils.measure("boot_time_s", 12.5, unit="s")
            utils.measure("ambient_c", 21, unit="°C", limits=(15, 30))

        result = await _Bench(name="psu", during_test=record).run()

        payload = json.loads(json.dumps(result.to_json(), allow_nan=False))
        leaf = payload["summary_tree"]["children"][0]
        assert [entry["name"] for entry in leaf["measurements"]] == ["boot_time_s", "ambient_c"]
        assert _accepts(Draft202012Validator(json.loads(RESULT_SCHEMA.read_text())), payload)

    async def test_the_returned_record_is_frozen(self) -> None:
        seen: list[Measurement] = []

        def record(utils: TestUtils) -> None:
            seen.append(utils.measure("count", 3))

        await _Bench(name="psu", during_test=record).run()

        [record_taken] = seen
        with pytest.raises(AttributeError):
            record_taken.value = 4  # pyright: ignore[reportAttributeAccessIssue]


class TestTimelineLine:
    async def test_in_window_logs_at_info_and_out_of_window_at_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def record(utils: TestUtils) -> None:
            utils.measure("output_voltage_v", 5.02, unit="V", limits=(4.75, 5.25))
            utils.measure("ripple_mv", 60.0, unit="mV", limits=(None, 50.0))
            utils.measure("cycles", 3)

        with caplog.at_level("INFO"):
            await _Bench(name="psu", during_test=record).run()

        lines = [
            (logged.levelname, logged.getMessage())
            for logged in caplog.records
            if logged.getMessage().startswith("measurement: ")
        ]
        assert lines == [
            ("INFO", "measurement: output_voltage_v = 5.02 V limits=[4.75, 5.25] in_limits=true"),
            ("WARNING", "measurement: ripple_mv = 60.0 mV limits=[null, 50.0] in_limits=false"),
            ("INFO", "measurement: cycles = 3 limits=null in_limits=null"),
        ]


class TestNotTaken:
    """``measure(name, None)`` records a reading not taken (issue #159, bench finding F10).

    The ruling: ``value`` is nullable in place under result version 1, a hole is never
    inside its window, and the framework still decides nothing about the verdict.
    """

    @SPELLINGS
    async def test_none_against_a_window_is_recorded_outside_it(self, spelling: Spelling) -> None:
        def record(utils: TestUtils) -> None:
            spelling(utils, name="root_used_pct", value=None, unit="%", limits=(None, 90))

        result = await _Bench(name="guest", during_test=record).run()

        # The framework changed nothing: the claim returned PASS without gating.
        assert result.state is State.PASS
        entry = _only(_leaf(result.summary_tree))
        entry.pop("timestamp")
        assert entry == {
            "name": "root_used_pct",
            "value": None,
            "unit": "%",
            "limits": {"low": None, "high": 90},
            "in_limits": False,
        }

    async def test_none_without_a_window_is_a_hole_in_the_trend(self) -> None:
        def record(utils: TestUtils) -> None:
            utils.measure("uptime_s", None, unit="s")

        result = await _Bench(name="guest", during_test=record).run()

        entry = _only(_leaf(result.summary_tree))
        assert entry["value"] is None
        assert entry["in_limits"] is None
        assert entry["limits"] is None

    async def test_a_claim_gated_on_the_window_fails_through_its_own_gate(self) -> None:
        class Gated(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                reading = utils.measure("rail_v", None, unit="V", limits=(4.75, 5.25))
                if reading.in_limits:
                    return PASS({"rail_v": reading.value})
                raise TestFailure("rail not read", "4.75-5.25 V", "no reading")

        result = await Gated(name="psu").run()

        assert result.state is State.FAIL
        failure = _leaf(result.summary_tree).failure
        assert isinstance(failure, TestFailure)
        assert failure.fail_message == "rail not read"
        # The hole is still on the record, beside the Fail that names it.
        assert _only(_leaf(result.summary_tree))["value"] is None

    async def test_the_record_keeps_call_order_and_validates_against_the_schema(self) -> None:
        def record(utils: TestUtils) -> None:
            utils.measure("boot_time_s", 12.5, unit="s")
            utils.measure("mem_available_mb", None, unit="MB", limits=(512, None))
            utils.measure("entropy_bits", 3000, unit="bit", limits=(256, None))

        result = await _Bench(name="guest", during_test=record).run()

        payload = json.loads(json.dumps(result.to_json(), allow_nan=False))
        leaf = payload["summary_tree"]["children"][0]
        assert [entry["value"] for entry in leaf["measurements"]] == [12.5, None, 3000]
        assert _accepts(Draft202012Validator(json.loads(RESULT_SCHEMA.read_text())), payload)

    async def test_a_reading_not_taken_logs_at_warning_in_words(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def record(utils: TestUtils) -> None:
            utils.measure("root_used_pct", None, unit="%", limits=(None, 90))
            utils.measure("uptime_s", None, unit="s")

        with caplog.at_level("INFO"):
            await _Bench(name="guest", during_test=record).run()

        lines = [
            (logged.levelname, logged.getMessage())
            for logged in caplog.records
            if logged.getMessage().startswith("measurement: ")
        ]
        assert lines == [
            ("WARNING", "measurement: root_used_pct not taken limits=[null, 90] in_limits=false"),
            ("WARNING", "measurement: uptime_s not taken limits=null in_limits=null"),
        ]

    @SPELLINGS
    async def test_the_name_is_still_unique_whether_or_not_the_reading_was_taken(
        self, spelling: Spelling
    ) -> None:
        def record(utils: TestUtils) -> None:
            spelling(utils, name="rail_v", value=None, unit="V")
            spelling(utils, name="rail_v", value=5.0, unit="V")

        result = await _Bench(name="psu", during_test=record).run()

        assert result.state is State.FAIL
        assert isinstance(_leaf(result.summary_tree).failure, MeasurementDuplicate)
        assert _only(_leaf(result.summary_tree))["value"] is None

    def test_the_schema_admits_null_and_still_refuses_every_other_non_number(self) -> None:
        validator = Draft202012Validator(json.loads(RESULT_SCHEMA.read_text()))
        entry: JsonObject = {
            "name": "root_used_pct",
            "value": None,
            "unit": "%",
            "limits": {"low": None, "high": 90},
            "in_limits": False,
            "timestamp": "2026-10-20T01:14:03.118402Z",
        }
        tree = SummaryTreeNode(
            name="guest",
            type=NodeType.SUITE,
            state=State.PASS,
            log_path="0001-guest-0123456789ab/timeline.jsonl",
            measurements=(entry,),
        )
        node = tree.to_json()
        assert _accepts(validator, _minimal_result(node))
        refusals: list[JsonValue] = ["", "5.02", True, [None]]
        for refused in refusals:
            bad = _minimal_result({**node, "measurements": [{**entry, "value": refused}]})
            assert not _accepts(validator, bad), repr(refused)


class TestGuards:
    @SPELLINGS
    @pytest.mark.parametrize(
        ("call", "reason"),
        [
            ({"name": "", "value": 1}, "name must be a non-empty str"),
            ({"name": 7, "value": 1}, "name must be a non-empty str"),
            ({"name": "flag", "value": True}, _NOT_NUMERIC),
            ({"name": "nan", "value": float("nan")}, _NOT_NUMERIC),
            ({"name": "inf", "value": float("-inf")}, _NOT_NUMERIC),
            ({"name": "text", "value": "5.0"}, _NOT_NUMERIC),
            ({"name": "unit", "value": 1, "unit": 3}, "unit must be a str or None"),
            ({"name": "none", "value": 1, "limits": (None, None)}, "bounds nothing"),
            ({"name": "backwards", "value": 1, "limits": (5, 4)}, "exceeds high"),
            ({"name": "text_bound", "value": 1, "limits": ("a", 1)}, "must be a finite number"),
            ({"name": "nan_bound", "value": 1, "limits": (float("nan"), 1)}, "finite number"),
            ({"name": "bool_bound", "value": 1, "limits": (True, 2)}, "finite number"),
            ({"name": "short", "value": 1, "limits": (1,)}, "must be a (low, high) tuple"),
            ({"name": "list", "value": 1, "limits": [1, 2]}, "must be a (low, high) tuple"),
        ],
        ids=_call_id,
    )
    async def test_a_refused_argument_fails_the_node_naming_the_measurement(
        self, spelling: Spelling, call: dict[str, Any], reason: str
    ) -> None:
        def record(utils: TestUtils) -> None:
            spelling(utils, **cast(Any, call))

        result = await _Bench(name="psu", during_test=record).run()

        assert result.state is State.FAIL
        failure = _leaf(result.summary_tree).failure
        assert isinstance(failure, MeasurementInvalid)
        assert failure.node == "claim"
        assert failure.measurement == str(call["name"])
        assert reason in failure.reason
        assert failure.fail_message.startswith("CONTRACT VIOLATION IN claim:")
        assert failure.to_json()["measurement"] == str(call["name"])
        assert failure.to_json()["bug"] is True
        # Nothing was recorded for a refused reading.
        assert _leaf(result.summary_tree).measurements == ()

    @SPELLINGS
    async def test_the_same_name_twice_in_one_node_is_refused_and_the_first_survives(
        self, spelling: Spelling
    ) -> None:
        def record(utils: TestUtils) -> None:
            spelling(utils, name="output_voltage_v", value=5.0, unit="V")
            spelling(utils, name="output_voltage_v", value=5.1, unit="V")

        result = await _Bench(name="psu", during_test=record).run()

        assert result.state is State.FAIL
        failure = _leaf(result.summary_tree).failure
        assert isinstance(failure, MeasurementDuplicate)
        assert failure.measurement == "output_voltage_v"
        # Sealed on Fail like any other state: the first reading is in the record.
        assert _only(_leaf(result.summary_tree))["value"] == 5.0

    async def test_configure_may_measure_and_the_reading_lands_on_the_authored_node(self) -> None:
        def record(utils: TestUtils) -> None:
            utils.measure("ambient_c", 21.5, unit="°C")

        result = await _Bench(name="psu", during_configure=record).run()

        assert result.state is State.PASS
        assert _only(result.summary_tree)["name"] == "ambient_c"
        assert _leaf(result.summary_tree).measurements == ()
        [entry] = cast(list[JsonObject], result.run_data["measurements"])
        assert entry["name"] == "ambient_c"

    @SPELLINGS
    async def test_cleanup_cannot_measure(self, spelling: Spelling) -> None:
        refused: list[MeasurementViolation] = []

        def late(utils: TestUtils) -> None:
            # A cleanup exception never changes the verdict (litmus/no-verdict-in-cleanup),
            # so the guard is proven by catching it, not by the run's state.
            try:
                spelling(utils, name="late", value=1)
            except MeasurementViolation as violation:
                refused.append(violation)

        result = await _Bench(name="psu", during_cleanup=late).run()

        assert result.state is State.PASS
        [violation] = refused
        assert isinstance(violation, MeasurementSealed | MeasurementUnbound)
        assert violation.measurement == "late"
        assert result.summary_tree.measurements == ()

    async def test_a_survey_cannot_measure(self) -> None:
        refused: list[BaseException] = []

        class Surveyed(_Bench):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_survey(self._survey, phases=[Phase.POST_CONFIGURE])

            async def _survey(self, utils: TestUtils) -> object:
                try:
                    utils.measure("survey_reading", 1)
                except MeasurementSealed as violation:
                    refused.append(violation)
                return None

        result = await Surveyed(name="psu").run()

        assert result.state is State.PASS
        assert len(refused) == 1
        assert result.summary_tree.measurements == ()

    async def test_an_audit_cannot_measure_even_while_the_test_method_runs(self) -> None:
        refused: list[type[BaseException]] = []

        class Audited(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_audit(interval=0.001, fn=self._audit)

            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._claim)

            async def _audit(self, utils: TestUtils) -> object:
                try:
                    utils.measure("sample", 1)
                except MeasurementSealed as violation:
                    refused.append(type(violation))
                return {}

            async def _claim(self, utils: TestUtils) -> Verdict:
                utils.measure("during_test", 1)
                await asyncio.sleep(0.02)
                return PASS

        result = await Audited(name="psu").run()

        assert result.state is State.PASS
        assert refused
        assert set(refused) == {MeasurementSealed}
        # The audit shares the node's TestUtils; only the slot's own task may record.
        assert [entry["name"] for entry in _leaf(result.summary_tree).measurements or ()] == [
            "during_test"
        ]

    @SPELLINGS
    async def test_a_settled_node_cannot_measure(self, spelling: Spelling) -> None:
        kept: list[TestUtils] = []

        def keep(utils: TestUtils) -> None:
            kept.append(utils)

        result = await _Bench(name="psu", during_test=keep).run()
        assert result.state is State.PASS

        [utils] = kept
        # The documented home sees a sealed record; the root alias sees no node at all.
        with pytest.raises((MeasurementSealed, MeasurementUnbound)) as caught:
            spelling(utils, name="afterwards", value=1)
        assert isinstance(caught.value, MeasurementViolation)
        assert caught.value.measurement == "afterwards"

    def test_the_root_alias_is_refused_outside_a_running_node(self) -> None:
        with pytest.raises(MeasurementUnbound) as caught:
            measure("bench", 1)

        assert caught.value.node == "litmus.measure"
        assert caught.value.measurement == "bench"
        assert "call utils.measure inside configure() or a test method" in str(caught.value)
        assert caught.value.to_json()["measurement"] == "bench"

    async def test_parallel_children_do_not_see_each_other_s_binding(self) -> None:
        class Pair(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "pair", [self.test("left", self._left), self.test("right", self._right)]
                )

            async def _left(self, utils: TestUtils) -> Verdict:
                await asyncio.sleep(0)
                measure("temp_c", 20.0, unit="°C")
                await asyncio.sleep(0)
                return PASS

            async def _right(self, utils: TestUtils) -> Verdict:
                measure("temp_c", 30.0, unit="°C")
                await asyncio.sleep(0)
                return PASS

        result = await Pair(name="rack").run()

        assert result.state is State.PASS
        pair = _leaf(result.summary_tree)
        assert _only(_leaf(pair, 0))["value"] == 20.0
        assert _only(_leaf(pair, 1))["value"] == 30.0
        assert pair.measurements == ()


class TestPublication:
    async def test_a_node_that_never_ran_publishes_null(self) -> None:
        class Sequenced(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    "steps", [self.test("first", self._first), self.test("later", self._later)]
                )

            async def _first(self, utils: TestUtils) -> Verdict:
                utils.measure("first_reading", 1)
                raise TestFailure("stop here", "a pass", "a fail")

            async def _later(self, utils: TestUtils) -> Verdict:
                return PASS

        result = await Sequenced(name="rack").run()

        steps = _leaf(result.summary_tree)
        assert _leaf(steps, 0).state is State.FAIL
        assert _only(_leaf(steps, 0))["name"] == "first_reading"
        assert _leaf(steps, 1).state is None
        assert _leaf(steps, 1).measurements is None
        payload = json.loads(json.dumps(result.to_json()))
        assert payload["summary_tree"]["children"][0]["children"][1]["measurements"] is None

    async def test_an_aborted_node_still_publishes_what_it_measured(self) -> None:
        class Slow(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._claim, abort_after=0.05)

            async def _claim(self, utils: TestUtils) -> Verdict:
                utils.measure("before_the_abort", 1)
                await asyncio.sleep(10)
                return PASS

        result = await Slow(name="rack").run()

        leaf = _leaf(result.summary_tree)
        assert leaf.state is State.ABORTED
        assert _only(leaf)["name"] == "before_the_abort"

    async def test_an_authored_run_data_override_cannot_drop_the_slot(self) -> None:
        class Overriding(_Bench):
            @property
            def run_data(self) -> JsonObject:
                return {"custom": True}

        def record(utils: TestUtils) -> None:
            utils.measure("ambient_c", 21)

        result = await Overriding(name="psu", during_configure=record).run()

        assert result.state is State.PASS
        measurements = cast(list[JsonObject], result.run_data["measurements"])
        assert result.run_data["custom"] is True
        assert [entry["name"] for entry in measurements] == ["ambient_c"]

    async def test_a_measurement_does_not_satisfy_an_evidence_declaration(self) -> None:
        class Declared(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._claim)

            @evidence("rail_v")
            async def _claim(self, utils: TestUtils) -> Verdict:
                utils.measure("rail_v", 5.0, unit="V")
                return PASS

        result = await Declared(name="psu").run()

        assert result.state is State.FAIL
        leaf = _leaf(result.summary_tree)
        assert isinstance(leaf.failure, EvidenceViolation)
        assert leaf.failure.key == "rail_v"
        # The reading is still in the record; it is the evidence that is missing.
        assert _only(leaf)["name"] == "rail_v"

    def test_the_published_schema_requires_every_field_of_an_entry(self) -> None:
        validator = Draft202012Validator(json.loads(RESULT_SCHEMA.read_text()))
        tree = SummaryTreeNode(
            name="rack",
            type=NodeType.SUITE,
            state=State.PASS,
            log_path="0001-rack-0123456789ab/timeline.jsonl",
            measurements=(
                {
                    "name": "output_voltage_v",
                    "value": 5.02,
                    "unit": "V",
                    "limits": {"low": 4.75, "high": 5.25},
                    "in_limits": True,
                    "timestamp": "2026-09-09T21:14:03.118402Z",
                },
            ),
        )
        node = tree.to_json()
        entry = cast(list[JsonObject], node["measurements"])[0]
        assert _accepts(validator, _minimal_result(node))
        assert not _accepts(validator, _minimal_result({**node, "measurements": [{}]}))
        assert not _accepts(validator, _minimal_result({**node, "measurements": {}}))
        assert not _accepts(
            validator, _minimal_result({**node, "measurements": [{**entry, "limits": [1, 2]}]})
        )
        assert not _accepts(
            validator, _minimal_result({**node, "measurements": [{**entry, "value": "5.02"}]})
        )
