"""The failure taxonomy: what each type means, and that all of it reaches JSON intact."""

import json
from typing import Any

from litmus import (
    ArpResolutionFailure,
    ContractViolation,
    ExternalIntegrationFailure,
    FrameworkContractViolation,
    LitmusError,
    MissingVerdict,
    OperatorInterrupt,
    TestFailure,
)
from litmus.errors import MISSING


class DimmFailure(TestFailure):
    """A suite-authored subclass, as a real DIMM test would define."""


class TestTaxonomyShape:
    def test_test_failure_is_a_verdict_of_fail(self) -> None:
        failure = TestFailure("firmware mismatch", expected="2.4.1", received="2.3.9")
        assert isinstance(failure, LitmusError)
        assert failure.fail_message == "firmware mismatch"
        assert failure.has_comparison is True
        assert failure.is_bug is False

    def test_arp_resolution_failure_is_not_a_test_failure(self) -> None:
        failure = ArpResolutionFailure("missing", mac="aa:bb:cc:dd:ee:ff", host="local")
        assert isinstance(failure, LitmusError)
        assert not isinstance(failure, TestFailure)
        assert failure.mac == "aa:bb:cc:dd:ee:ff"
        assert failure.host == "local"

    def test_a_self_evident_failure_carries_no_comparison(self) -> None:
        assert TestFailure("could not resolve IP from ARP cache").has_comparison is False

    def test_a_supplied_none_is_a_comparison(self) -> None:
        # MISSING exists so "expected nothing" and "expected None" stay distinguishable.
        assert TestFailure("no serial", expected=None).has_comparison is True
        assert TestFailure("no serial").expected is MISSING

    def test_subclasses_still_classify_as_fail(self) -> None:
        assert isinstance(DimmFailure("bad rank"), TestFailure)

    def test_integration_failure_is_not_a_test_failure(self) -> None:
        # Peer taxonomies: the recovery is different, so the type must be too. A
        # `except TestFailure` anywhere must not intercept a run-level halt.
        failure = ExternalIntegrationFailure("before_each", RuntimeError("db down"))
        assert isinstance(failure, LitmusError)
        assert not isinstance(failure, TestFailure)
        assert failure.is_bug is False

    def test_integration_failure_preserves_the_integrators_error(self) -> None:
        cause = RuntimeError("shop-floor database refused the record")
        failure = ExternalIntegrationFailure("after_each", cause)
        assert failure.cause is cause
        assert failure.__cause__ is cause
        assert failure.hook == "after_each"

    def test_missing_verdict_is_not_a_failure(self) -> None:
        # Aborted is a peer of Pass and Fail. If this ever became a TestFailure, the
        # framework would be forging a verdict the test never produced.
        missing = MissingVerdict("dimm-0", returned=None)
        assert isinstance(missing, LitmusError)
        assert not isinstance(missing, TestFailure)
        assert missing.is_bug is True
        assert missing.returned is None

    def test_operator_interrupt_names_the_operator_and_is_not_a_test_failure(self) -> None:
        failure = OperatorInterrupt(2)
        assert isinstance(failure, LitmusError)
        assert not isinstance(failure, TestFailure)
        assert not isinstance(failure, ExternalIntegrationFailure)
        assert failure.is_bug is False
        assert "operator" in failure.fail_message
        assert failure.signal_name == "SIGINT"
        assert failure.signum == 2

    def test_an_unknown_signum_still_names_the_operator(self) -> None:
        failure = OperatorInterrupt(99)
        assert failure.signal_name == "signal 99"
        assert "operator" in failure.fail_message
        assert "signal 99" in failure.fail_message


class TestBugSignal:
    def test_contract_violation_lands_in_fail_and_says_it_is_a_bug(self) -> None:
        violation = ContractViolation("dimm-0", cause=ValueError("kaboom"))
        assert isinstance(violation, TestFailure)
        assert violation.is_bug is True
        assert violation.fail_message.startswith("CONTRACT VIOLATION IN dimm-0:")

    def test_the_two_guard_layers_have_distinct_banners(self) -> None:
        developer = ContractViolation("dimm-0")
        framework = FrameworkContractViolation("dimm-0")
        assert developer.BANNER == "CONTRACT VIOLATION"
        assert framework.BANNER == "FRAMEWORK CONTRACT VIOLATION"
        assert developer.BANNER != framework.BANNER
        assert framework.fail_message.startswith("FRAMEWORK CONTRACT VIOLATION IN dimm-0:")

    def test_the_framework_layer_tells_the_reader_where_to_escalate(self) -> None:
        assert "maintainers" in FrameworkContractViolation("dimm-0").fail_message

    def test_missing_verdict_carries_the_developer_banner(self) -> None:
        assert MissingVerdict("dimm-0", returned="PASS").BANNER == "CONTRACT VIOLATION"


class TestJsonSerialization:
    def _round_trip(self, payload: dict[str, Any]) -> dict[str, Any]:
        decoded: dict[str, Any] = json.loads(json.dumps(payload))
        return decoded

    def test_a_plain_failure_serializes(self) -> None:
        payload = self._round_trip(
            TestFailure("voltage out of range", expected=3.3, received=2.9).to_json()
        )
        assert payload["name"] == "TestFailure"
        assert payload["state"] == "Fail"
        assert payload["expected"] == 3.3
        assert payload["received"] == 2.9

    def test_an_omitted_comparison_is_omitted_not_nulled(self) -> None:
        payload = TestFailure("unreachable").to_json()
        assert "expected" not in payload
        assert "received" not in payload

    def test_a_live_handle_is_stripped_rather_than_thrown_over(self) -> None:
        # `received` is whatever the suite observed and may be an open SSH interface.
        # Serialization must degrade, never raise: a failure that cannot be written is
        # a failure that never reaches the FT.
        class SshInterface:
            def __repr__(self) -> str:
                return "<SshInterface host=dut-12>"

        payload = self._round_trip(
            TestFailure("unexpected handle", received=SshInterface()).to_json()
        )
        assert payload["received"] == "<SshInterface host=dut-12>"

    def test_a_nested_failure_keeps_its_structure(self) -> None:
        inner = TestFailure("rank 1 mismatch", expected=8, received=7)
        payload = self._round_trip(TestFailure("dimm failed", received=inner).to_json())
        assert payload["received"]["name"] == "TestFailure"
        assert payload["received"]["expected"] == 8

    def test_subclass_identity_survives_serialization(self) -> None:
        assert DimmFailure("bad rank").to_json()["name"] == "DimmFailure"

    def test_a_contract_violation_serializes_its_banner_and_cause(self) -> None:
        payload = self._round_trip(
            ContractViolation("dimm-0", cause=ValueError("kaboom")).to_json()
        )
        assert payload["banner"] == "CONTRACT VIOLATION"
        assert payload["bug"] is True
        assert payload["node"] == "dimm-0"
        assert payload["cause"] == {"name": "ValueError", "message": "kaboom"}

    def test_a_missing_verdict_serializes_what_was_returned(self) -> None:
        payload = self._round_trip(MissingVerdict("dimm-0", returned="PASS").to_json())
        assert payload["state"] == "Aborted"
        assert payload["returned"] == "PASS"
        assert payload["bug"] is True

    def test_an_integration_failure_serializes_its_hook_and_cause(self) -> None:
        payload = self._round_trip(
            ExternalIntegrationFailure("before_each", RuntimeError("db down")).to_json()
        )
        assert payload["state"] == "Aborted"
        assert payload["hook"] == "before_each"
        assert payload["cause"] == {"name": "RuntimeError", "message": "db down"}

    def test_an_integration_failure_wrapping_a_test_failure_recurses(self) -> None:
        payload = self._round_trip(
            ExternalIntegrationFailure(
                "after_each", TestFailure("gate closed", expected=1, received=0)
            ).to_json()
        )
        assert payload["cause"]["name"] == "TestFailure"
        assert payload["cause"]["received"] == 0

    def test_a_pathological_cause_degrades_to_a_string(self) -> None:
        payload = self._round_trip(
            TestFailure("odd value", received={1: "non-string key"}).to_json()
        )
        assert isinstance(payload["received"], str)

    def test_an_operator_interrupt_serializes_the_signal_and_names_the_operator(
        self,
    ) -> None:
        payload = self._round_trip(OperatorInterrupt(15).to_json())
        assert payload["name"] == "OperatorInterrupt"
        assert payload["state"] == "Aborted"
        assert payload["operator"] is True
        assert payload["signal"] == "SIGTERM"
        assert "operator" in payload["fail_message"]
        assert "SIGTERM" in payload["fail_message"]

    def test_nested_serializable_containers_pass_through(self) -> None:
        payload = self._round_trip(
            TestFailure("bank readings", received={"banks": [1, 2, None], "ok": False}).to_json()
        )
        assert payload["received"] == {"banks": [1, 2, None], "ok": False}
