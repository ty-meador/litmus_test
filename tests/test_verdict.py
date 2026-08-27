"""The PASS sentinel must be non-forgeable, by every route a suite could reach for."""

import copy
import pickle

from litmus import PASS

# The sentinel class is private on purpose — nothing outside the module should build
# one. These tests reach for it precisely to prove that reaching for it changes nothing.
from litmus.verdict import _Pass  # pyright: ignore[reportPrivateUsage]


class TestPassSentinel:
    def test_is_a_singleton(self) -> None:
        assert _Pass() is PASS
        assert _Pass() is _Pass()

    def test_repr_is_the_bare_word(self) -> None:
        assert repr(PASS) == "PASS"
        assert f"{PASS!r}" == "PASS"

    def test_the_string_pass_is_not_the_sentinel(self) -> None:
        # The whole point of identity over equality: a suite that returns the literal
        # string cannot claim a verdict it did not produce.
        assert PASS != "PASS"
        assert PASS != "PASS"

    def test_no_truthy_value_is_the_sentinel(self) -> None:
        for impostor in (True, 1, "PASS", ["PASS"], {"verdict": "PASS"}, object()):
            assert impostor is not PASS

    def test_copying_cannot_mint_a_second_instance(self) -> None:
        assert copy.copy(PASS) is PASS
        assert copy.deepcopy(PASS) is PASS

    def test_a_pickle_round_trip_cannot_mint_a_second_instance(self) -> None:
        # Artifacts and cross-process fan-out both pickle; a round trip that produced
        # an equal-but-not-identical sentinel would pass the eye and fail the check.
        assert pickle.loads(pickle.dumps(PASS)) is PASS

    def test_carries_no_mutable_state_to_tamper_with(self) -> None:
        assert not hasattr(PASS, "__dict__")
