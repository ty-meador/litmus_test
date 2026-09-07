"""RunEnv: the run environment as a mapping with readers that Fail the run."""

from __future__ import annotations

from pathlib import Path

import pytest

from litmus import RunEnv, TestFailure, TestUtils

_UNSET = "did not reach a testable state: TARGET_ADDRESS env var not set; set TARGET_ADDRESS"
_BAD_PORT = (
    "did not reach a testable state: TARGET_PORT env var is not a valid port; "
    "set TARGET_PORT to an integer 1-65535"
)


class TestRunEnvMapping:
    """The mapping half: what every existing `utils.env.get(...)` caller relies on."""

    def test_reads_like_a_mapping(self) -> None:
        env = RunEnv({"A": "1", "B": " two "})

        assert env["A"] == "1"
        assert env.get("B") == " two "
        assert env.get("C") is None
        assert "A" in env
        assert "C" not in env
        assert len(env) == 2
        assert sorted(env) == ["A", "B"]
        assert dict(env) == {"A": "1", "B": " two "}
        assert repr(env) == "RunEnv({'A': '1', 'B': ' two '})"

    def test_is_a_snapshot_of_its_source(self) -> None:
        source = {"A": "1"}
        env = RunEnv(source)
        source["A"] = "2"
        source["B"] = "3"

        assert dict(env) == {"A": "1"}

    def test_utils_env_is_a_run_env(self, tmp_path: Path) -> None:
        utils = TestUtils(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path), "TARGET_PORT": "2222"})

        assert isinstance(utils.env, RunEnv)
        assert utils.env.port("TARGET_PORT") == 2222
        assert utils.env.get("TARGET_PORT") == "2222"


class TestRunEnvReaders:
    """The readers: trimmed text, a required value, and a validated port."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ({}, None),
            ({"TARGET_ADDRESS": ""}, None),
            ({"TARGET_ADDRESS": "   "}, None),
            ({"TARGET_ADDRESS": " pihole.lab "}, "pihole.lab"),
        ],
    )
    def test_text_trims_and_treats_blank_as_unset(
        self, source: dict[str, str], expected: str | None
    ) -> None:
        assert RunEnv(source).text("TARGET_ADDRESS") == expected

    def test_require_returns_the_trimmed_value(self) -> None:
        assert RunEnv({"TARGET_ADDRESS": " pihole.lab "}).require("TARGET_ADDRESS") == "pihole.lab"

    @pytest.mark.parametrize("source", [{}, {"TARGET_ADDRESS": ""}, {"TARGET_ADDRESS": " "}])
    def test_require_fails_naming_the_variable(self, source: dict[str, str]) -> None:
        with pytest.raises(TestFailure) as raised:
            RunEnv(source).require("TARGET_ADDRESS")

        assert raised.value.fail_message == _UNSET

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ({}, 22),
            ({"TARGET_PORT": ""}, 22),
            ({"TARGET_PORT": "2222"}, 2222),
            ({"TARGET_PORT": " 1 "}, 1),
            ({"TARGET_PORT": "65535"}, 65535),
        ],
    )
    def test_port_parses_or_defaults(self, source: dict[str, str], expected: int) -> None:
        assert RunEnv(source).port("TARGET_PORT") == expected

    def test_port_default_is_the_callers(self) -> None:
        assert RunEnv({}).port("TARGET_PORT", default=8022) == 8022

    @pytest.mark.parametrize("value", ["abc", "22.5", "0", "-22", "65536", "70000"])
    def test_port_fails_on_anything_a_dial_would_choke_on(self, value: str) -> None:
        with pytest.raises(TestFailure) as raised:
            RunEnv({"TARGET_PORT": value}).port("TARGET_PORT")

        assert raised.value.fail_message == _BAD_PORT
