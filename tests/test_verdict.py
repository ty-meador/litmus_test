"""The PASS verdict must be non-forgeable by every route a suite could reach for.

The evidence it carries must be a detached, read-only copy (issue #118).
"""

import copy
import pickle

import pytest

from litmus import PASS, Verdict


class TestPassSentinel:
    def test_is_a_singleton(self) -> None:
        assert Verdict() is PASS
        assert Verdict() is Verdict()

    def test_repr_is_the_bare_word(self) -> None:
        assert repr(PASS) == "PASS"
        assert f"{PASS!r}" == "PASS"

    def test_the_string_pass_is_not_the_sentinel(self) -> None:
        # The whole point of a type check over equality: a suite that returns the
        # literal string cannot claim a verdict it did not produce.
        assert PASS != "PASS"
        assert not isinstance("PASS", Verdict)

    def test_no_truthy_value_is_the_sentinel(self) -> None:
        for impostor in (True, 1, "PASS", ["PASS"], {"verdict": "PASS"}, object()):
            assert impostor is not PASS
            assert not isinstance(impostor, Verdict)

    def test_copying_cannot_mint_a_second_instance(self) -> None:
        assert copy.copy(PASS) is PASS
        assert copy.deepcopy(PASS) is PASS

    def test_a_pickle_round_trip_cannot_mint_a_second_instance(self) -> None:
        # Artifacts and cross-process fan-out both pickle; a round trip that produced
        # an equal-but-not-identical sentinel would pass the eye and fail the check.
        assert pickle.loads(pickle.dumps(PASS)) is PASS

    def test_carries_no_mutable_state_to_tamper_with(self) -> None:
        assert not hasattr(PASS, "__dict__")
        assert PASS.evidence is None
        with pytest.raises(AttributeError, match="immutable"):
            PASS.evidence = {"forged": True}  # pyright: ignore[reportAttributeAccessIssue]


class TestPassWithEvidence:
    def test_a_carrying_verdict_is_a_verdict_and_not_the_sentinel(self) -> None:
        verdict = PASS({"output_voltage_v": 12.1})

        assert isinstance(verdict, Verdict)
        assert verdict is not PASS
        assert verdict.evidence == {"output_voltage_v": 12.1}

    def test_the_payload_is_a_detached_read_only_copy(self) -> None:
        readings = {"output_voltage_v": 12.1}
        verdict = PASS(readings)
        readings["output_voltage_v"] = 0.0

        assert verdict.evidence == {"output_voltage_v": 12.1}
        assert verdict.evidence is not None
        with pytest.raises(TypeError):
            verdict.evidence["output_voltage_v"] = 0.0  # pyright: ignore[reportIndexIssue]
        with pytest.raises(AttributeError, match="immutable"):
            verdict._evidence = {}  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]

    def test_repr_shows_the_readings(self) -> None:
        assert repr(PASS({"a": 1})) == "PASS({'a': 1})"

    def test_copying_keeps_the_same_verdict(self) -> None:
        verdict = PASS({"a": 1})
        assert copy.copy(verdict) is verdict
        assert copy.deepcopy(verdict) is verdict

    def test_a_pickle_round_trip_keeps_the_readings(self) -> None:
        payload: dict[str, object] = {"a": [1, 2]}
        verdict = PASS(payload)
        restored = pickle.loads(pickle.dumps(verdict))

        assert isinstance(restored, Verdict)
        assert restored is not PASS
        assert restored.evidence == {"a": [1, 2]}

    def test_a_carrying_verdict_can_mint_another(self) -> None:
        # The call lives on the class, so the bare sentinel is the only entry point a
        # suite needs; a chained call still produces a detached verdict.
        first = PASS({"a": 1})
        second = first({"b": 2})
        assert second.evidence == {"b": 2}
        assert first.evidence == {"a": 1}
