"""A ctx key named in ``redact_ctx`` reaches the record as ``"<redacted>"`` (issue #154).

The case is the bench's Pi-hole session id. The claims read it from ctx, and before
this change every run published it in ``run_data`` until logout. These tests hold
the record to the rule on every surface a run writes, and they hold the declaration
to it at class definition, before an instance can touch hardware.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from litmus import PASS, AfterEachContext, JsonObject, LitmusTest, RunResult, TestUtils, Verdict
from litmus.lifecycle import State
from litmus.run_data import REDACTED

SECRET = "s3cret-session-id"


class SessionSuite(LitmusTest):
    """Logs in during configure and hands the session id to one claim through ctx."""

    redact_ctx = ("sid", r".*_token")

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        return {
            "sid": SECRET,
            "api_token": SECRET,
            "token_count": 2,
            "inside_temp": 21.5,
            "domain": "blocked.example",
        }

    def build_suite(self) -> LitmusTest:
        return self.test("query", self._query)

    async def _query(self, utils: TestUtils) -> Verdict:
        # The claim still reads the real value; redaction is the record's, not ctx's.
        assert utils.ctx["sid"] == SECRET
        return PASS


async def _run_collecting(suite: LitmusTest) -> tuple[RunResult, dict[str, RunResult]]:
    """Run ``suite`` and keep every node's settled result, keyed by node name."""
    settled: dict[str, RunResult] = {}

    async def after_each(context: AfterEachContext) -> None:
        settled[context.result.ancestor_names[-1]] = context.result

    result = await suite.run(after_each=after_each, allow_unpaired_hooks=True)
    return result, settled


def _files_under(root: Path) -> dict[Path, str]:
    """Every file a finished run left under ``root``, with its text."""
    return {path: path.read_text(errors="replace") for path in root.rglob("*") if path.is_file()}


class TestRecordWithholdsTheValue:
    async def test_root_and_leaf_run_data_carry_the_marker_and_keep_the_key(self) -> None:
        result, settled = await _run_collecting(SessionSuite(name="pihole"))

        assert result.state is State.PASS
        for run_data in (result.run_data, settled["query"].run_data):
            assert run_data["sid"] == REDACTED
            assert run_data["api_token"] == REDACTED
            assert run_data["domain"] == "blocked.example"

    async def test_a_rule_matches_the_whole_key_and_no_more(self) -> None:
        result, _ = await _run_collecting(SessionSuite(name="pihole"))

        assert result.run_data["token_count"] == 2
        assert result.run_data["inside_temp"] == 21.5

    async def test_no_file_the_run_writes_and_no_serialized_result_holds_the_secret(
        self, tmp_path: Path
    ) -> None:
        result, settled = await _run_collecting(SessionSuite(name="pihole"))

        assert SECRET not in json.dumps(result.to_json())
        assert SECRET not in json.dumps(settled["query"].to_json())
        written = _files_under(tmp_path)
        assert written
        assert [path for path, text in written.items() if SECRET in text] == []


class OverridingSuite(SessionSuite):
    """A run_data override that rebuilds the view from ctx, secret included."""

    @property
    def run_data(self) -> JsonObject:
        return {"sid": self._query_sid(), "computed": True}

    def _query_sid(self) -> str:
        return SECRET


class TestOverrideCannotRepublish:
    async def test_redaction_applies_to_what_the_override_returned(self) -> None:
        result, _ = await _run_collecting(OverridingSuite(name="pihole"))

        assert result.run_data["sid"] == REDACTED
        assert result.run_data["computed"] is True


class PinnedSuite(SessionSuite):
    """Names its own key; the base's keys must stay withheld."""

    redact_ctx = (re.compile("PIN", re.IGNORECASE),)

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        return {**await super().configure(utils), "pin": "0000"}


class TestSubclassAddsToItsBases:
    async def test_a_subclass_declaration_keeps_the_base_rules(self) -> None:
        result, _ = await _run_collecting(PinnedSuite(name="pihole"))

        assert result.run_data["pin"] == REDACTED
        assert result.run_data["sid"] == REDACTED
        assert result.run_data["api_token"] == REDACTED


class EvidenceSuite(SessionSuite):
    """A reading whose name a rule happens to match is evidence, not ctx."""

    def build_suite(self) -> LitmusTest:
        return self.test("count", self._count)

    async def _count(self, utils: TestUtils) -> Verdict:
        return PASS({"reply_token": "visible"})


class TestEvidenceIsNotRedacted:
    async def test_a_verdict_carries_what_the_author_chose_to_publish(self) -> None:
        _, settled = await _run_collecting(EvidenceSuite(name="pihole"))

        assert settled["count"].run_data["reply_token"] == "visible"
        assert settled["count"].run_data["sid"] == REDACTED


class TestDeclarationIsCheckedAtClassDefinition:
    @pytest.mark.parametrize(
        ("declared", "message"),
        [
            pytest.param("sid", 'not redact_ctx = "sid"', id="bare-string"),
            pytest.param(["sid("], "not a valid pattern", id="invalid-regex"),
            pytest.param([""], "non-empty str", id="empty-name"),
            pytest.param([7], "non-empty str", id="not-a-string"),
            pytest.param([re.compile(b"sid")], "compiled str pattern", id="bytes-pattern"),
            pytest.param({"sid": True}, "list of ctx key names", id="mapping"),
        ],
    )
    def test_a_malformed_declaration_is_a_type_error(self, declared: object, message: str) -> None:
        with pytest.raises(TypeError, match=re.escape(message)):
            type(
                "Malformed",
                (SessionSuite,),
                {"redact_ctx": declared, "__module__": __name__},
            )
