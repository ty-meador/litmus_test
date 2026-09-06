"""Evidence travels with the verdict (issue #118).

A test method is a function from ``utils.ctx`` to a verdict. ``PASS(evidence)`` and
``TestFailure(..., evidence=...)`` carry the readings; the child boundary validates them
entry by entry and the leaf's record carries them beside the ctx view. ``@evidence``
names the keys a pass must carry; a pass without its declared evidence is not a pass.
"""

from typing import Any, TypedDict

import pytest

from litmus import (
    PASS,
    ContractViolation,
    EvidenceViolation,
    LitmusTest,
    State,
    TestFailure,
    TestUtils,
    Verdict,
    evidence,
)
from litmus.evidence import declared_evidence
from litmus.lifecycle import ChildBoundary, EvidenceContract, Slot
from litmus.results import SummaryTreeNode


class _Handle:
    """A live handle that can never be carried as evidence."""


def _leaf(tree: SummaryTreeNode, index: int = 0) -> SummaryTreeNode:
    return tree.children[index]


class _VoltageEvidence(TypedDict):
    output_voltage_v: float


class _Rail(LitmusTest):
    """One authored instance; the leaf's verdict is scripted by the constructor."""

    def __init__(self, *, name: str, verdict: Verdict[Any] | BaseException) -> None:
        super().__init__(name=name)
        self._verdict = verdict

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        return {"limit_v": 12.5, "transport": _Handle()}

    def build_suite(self) -> LitmusTest:
        return self.test("voltage", self._check_voltage)

    @evidence("output_voltage_v")
    async def _check_voltage(self, utils: TestUtils) -> Verdict:
        if isinstance(self._verdict, BaseException):
            raise self._verdict
        return self._verdict


class TestEvidenceOnPass:
    async def test_a_leaf_record_carries_the_readings_beside_the_context_view(self) -> None:
        result = await _Rail(name="psu", verdict=PASS({"output_voltage_v": 12.1})).run()

        assert result.state is State.PASS
        leaf = _leaf(result.summary_tree)
        assert leaf.evidence == {"output_voltage_v": 12.1}
        assert leaf.evidence_declared == ("output_voltage_v",)
        # The authored node is not a leaf: its record is the ctx view alone.
        assert result.run_data == {"measurements": [], "limit_v": 12.5}
        assert result.summary_tree.evidence is None
        assert result.summary_tree.evidence_declared is None
        payload = result.to_json()["summary_tree"]
        assert isinstance(payload, dict)
        assert payload["children"][0]["evidence"] == {"output_voltage_v": 12.1}
        assert payload["children"][0]["evidence_declared"] == ["output_voltage_v"]

    async def test_extra_keys_are_allowed_the_declaration_is_a_floor(self) -> None:
        result = await _Rail(
            name="psu", verdict=PASS({"output_voltage_v": 12.1, "ripple_mv": 3})
        ).run()

        assert result.state is State.PASS
        assert _leaf(result.summary_tree).evidence == {"output_voltage_v": 12.1, "ripple_mv": 3}

    async def test_a_pass_without_its_declared_evidence_is_not_a_pass(self) -> None:
        for verdict in (PASS, PASS({"ripple_mv": 3})):
            result = await _Rail(name="psu", verdict=verdict).run()

            assert result.state is State.FAIL
            failure = _leaf(result.summary_tree).failure
            assert isinstance(failure, EvidenceViolation)
            assert isinstance(failure, ContractViolation)
            assert failure.is_bug
            assert failure.key == "output_voltage_v"
            assert failure.fail_message == (
                "CONTRACT VIOLATION IN voltage: evidence incomplete: PASS did not carry "
                "declared key 'output_voltage_v'"
            )
            assert failure.to_json()["key"] == "output_voltage_v"
            assert _leaf(result.summary_tree).evidence is None

    async def test_a_value_that_is_not_json_names_the_key(self) -> None:
        result = await _Rail(
            name="psu", verdict=PASS({"output_voltage_v": 12.1, "shell": _Handle()})
        ).run()

        assert result.state is State.FAIL
        failure = _leaf(result.summary_tree).failure
        assert isinstance(failure, EvidenceViolation)
        assert failure.key == "shell"
        assert "not JSON-serializable at key 'shell'" in failure.fail_message

    async def test_evidence_may_not_shadow_the_context_or_the_reserved_slot(self) -> None:
        for key in ("limit_v", "transport", "measurements"):
            result = await _Rail(name="psu", verdict=PASS({"output_voltage_v": 12.1, key: 1})).run()

            assert result.state is State.FAIL
            failure = _leaf(result.summary_tree).failure
            assert isinstance(failure, EvidenceViolation)
            assert failure.key == key
            assert "shadows a ctx key or reserved slot" in failure.fail_message

    async def test_the_typed_dict_form_declares_its_required_keys(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("voltage", self._check_voltage)

            @evidence(_VoltageEvidence)
            async def _check_voltage(self, utils: TestUtils) -> Verdict[_VoltageEvidence]:
                # pyright checks this payload against the TypedDict; the runtime holds
                # the PASS to the same keys.
                return PASS({"output_voltage_v": 12.1})

        result = await Suite(name="psu").run()

        assert result.state is State.PASS
        leaf = _leaf(result.summary_tree)
        assert leaf.evidence == {"output_voltage_v": 12.1}
        assert leaf.evidence_declared == ("output_voltage_v",)

    async def test_an_undeclared_method_records_its_payload_unchecked(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                return PASS({"anything": [1, 2, 3]})

        result = await Suite(name="rack").run()

        assert result.state is State.PASS
        leaf = _leaf(result.summary_tree)
        assert leaf.evidence == {"anything": [1, 2, 3]}
        assert leaf.evidence_declared is None

    async def test_parallel_leaves_each_carry_their_own_evidence(self) -> None:
        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, Any]:
                return {"serial": "SN-1"}

            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "rails",
                    [self.test("a", self._a), self.test("b", self._b)],
                )

            async def _a(self, utils: TestUtils) -> Verdict:
                return PASS({"reading": 1})

            async def _b(self, utils: TestUtils) -> Verdict:
                return PASS({"reading": 2})

        result = await Suite(name="rack").run()

        assert result.state is State.PASS
        group = _leaf(result.summary_tree)
        assert group.evidence is None
        assert [child.evidence for child in group.children] == [{"reading": 1}, {"reading": 2}]
        assert result.run_data == {"measurements": [], "serial": "SN-1"}


class TestEvidenceOnFail:
    async def test_a_failure_records_what_it_is_handed(self) -> None:
        failure = TestFailure(
            "over limit", "<= 12.5 V", "13.1 V", evidence={"output_voltage_v": 13.1}
        )
        result = await _Rail(name="psu", verdict=failure).run()

        assert result.state is State.FAIL
        leaf = _leaf(result.summary_tree)
        assert leaf.failure is failure
        assert leaf.evidence == {"output_voltage_v": 13.1}
        assert failure.to_json()["evidence"] == {"output_voltage_v": 13.1}
        assert failure.evidence == {"output_voltage_v": 13.1}

    async def test_a_failure_is_not_held_to_the_declaration(self) -> None:
        result = await _Rail(
            name="psu", verdict=TestFailure("no reading", evidence={"ripple_mv": 3})
        ).run()

        assert result.state is State.FAIL
        leaf = _leaf(result.summary_tree)
        assert isinstance(leaf.failure, TestFailure)
        assert not isinstance(leaf.failure, EvidenceViolation)
        assert leaf.evidence == {"ripple_mv": 3}

    async def test_a_failure_without_evidence_records_none(self) -> None:
        result = await _Rail(name="psu", verdict=TestFailure("no reading")).run()

        leaf = _leaf(result.summary_tree)
        assert leaf.evidence is None
        assert leaf.failure is not None
        assert "evidence" not in leaf.failure.to_json()

    async def test_bad_evidence_on_the_fail_path_is_a_bug_that_keeps_its_cause(self) -> None:
        failure = TestFailure("over limit", evidence={"shell": _Handle()})
        result = await _Rail(name="psu", verdict=failure).run()

        assert result.state is State.FAIL
        leaf = _leaf(result.summary_tree)
        assert isinstance(leaf.failure, EvidenceViolation)
        assert leaf.failure.key == "shell"
        assert leaf.failure.__cause__ is failure
        assert leaf.evidence is None


class TestTheDeclaration:
    def test_string_keys_are_recorded_in_order(self) -> None:
        @evidence("b", "a")
        async def claim(utils: TestUtils) -> Verdict:
            return PASS

        assert declared_evidence(claim) == ("b", "a")

    def test_a_typed_dict_declares_its_required_keys(self) -> None:
        class _Shape(TypedDict, total=False):
            optional: int

        class _Full(_Shape):
            first: float
            second: str

        @evidence(_Full)
        async def claim(utils: TestUtils) -> Verdict[_Full]:
            return PASS({"first": 1.0, "second": "x"})

        assert declared_evidence(claim) == ("first", "second")

    def test_an_undeclared_method_declares_nothing(self) -> None:
        async def claim(utils: TestUtils) -> Verdict:
            return PASS

        assert declared_evidence(claim) is None

    def test_the_declaration_survives_argument_binding(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._claim, 3)

            @evidence("reading")
            async def _claim(self, utils: TestUtils, channel: int) -> Verdict:
                return PASS({"reading": channel})

        leaf = Suite(name="rack").build_suite()
        assert leaf._evidence_declared == ("reading",)  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.parametrize(
        ("keys", "message"),
        [
            ((), "declares nothing"),
            (("a", "a"), "declares 'a' twice"),
            (("a", int), "string keys or one TypedDict class"),
            ((int,), "needs a TypedDict class"),
        ],
    )
    def test_a_malformed_declaration_is_refused_at_definition(
        self, keys: tuple[Any, ...], message: str
    ) -> None:
        with pytest.raises(TypeError, match=message):
            evidence(*keys)

    def test_a_typed_dict_with_no_required_keys_declares_nothing(self) -> None:
        class _Empty(TypedDict, total=False):
            optional: int

        with pytest.raises(TypeError, match="no required keys"):
            evidence(_Empty)

    def test_a_method_declares_its_evidence_once(self) -> None:
        with pytest.raises(TypeError, match="already declares"):

            @evidence("a")
            @evidence("b")
            async def claim(utils: TestUtils) -> Verdict:  # pyright: ignore[reportUnusedFunction]
                return PASS


class TestTheBoundaryContract:
    async def test_the_default_contract_declares_nothing_and_reserves_only_measurements(
        self,
    ) -> None:
        boundary = ChildBoundary("node")

        async def carrying() -> object:
            return PASS({"anything": 1})

        async def reserved() -> object:
            payload: dict[str, object] = {"measurements": []}
            return PASS(payload)

        outcome = await boundary.run(Slot.TEST_METHOD, carrying())
        assert outcome.state is State.PASS
        assert outcome.evidence == {"anything": 1}

        outcome = await boundary.run(Slot.TEST_METHOD, reserved())
        assert outcome.state is State.FAIL
        assert isinstance(outcome.failure, EvidenceViolation)

    def test_the_contract_reports_the_first_violation_in_order(self) -> None:
        contract = EvidenceContract(declared=("a",), reserved=frozenset({"ctx"}))

        assert contract.violation("n", {"a": 1}, held=True) is None
        assert contract.violation("n", {}, held=False) is None
        missing = contract.violation("n", {}, held=True)
        assert missing is not None
        assert missing.key == "a"
        bad = contract.violation("n", {"a": _Handle(), "ctx": 1}, held=True)
        assert bad is not None
        assert bad.key == "a"
        shadow = contract.violation("n", {"a": 1, "ctx": 1}, held=True)
        assert shadow is not None
        assert shadow.key == "ctx"
        odd = contract.violation("n", {1: "one"}, held=False)  # pyright: ignore[reportArgumentType]
        assert odd is not None
        assert odd.key == "1"

    async def test_evidence_on_a_slot_that_demands_no_verdict_is_ignored(self) -> None:
        boundary = ChildBoundary("node")

        async def cleanup() -> object:
            raise TestFailure("cleanup complained", evidence={"shell": _Handle()})

        outcome = await boundary.run(Slot.CLEANUP, cleanup())
        assert outcome.state is None
        assert outcome.evidence is None

        async def configure() -> object:
            raise TestFailure("not testable", evidence={"stage": "boot"})

        outcome = await boundary.run(Slot.CONFIGURE, configure())
        assert outcome.state is State.FAIL
        assert outcome.evidence is None


class TestAbortLeavesNoEvidence:
    async def test_a_leaf_that_never_ran_shows_its_declaration_and_no_readings(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    "steps", [self.test("first", self._first), self.test("second", self._second)]
                )

            async def _first(self, utils: TestUtils) -> Verdict:
                raise TestFailure("first step failed")

            @evidence("reading")
            async def _second(self, utils: TestUtils) -> Verdict:
                return PASS({"reading": 1})

        result = await Suite(name="rack").run()

        assert result.state is State.FAIL
        second = _leaf(result.summary_tree).children[1]
        assert second.state is None
        assert second.evidence is None
        assert second.evidence_declared == ("reading",)
