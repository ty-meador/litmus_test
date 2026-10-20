"""``redact_ctx``: a ctx value the suite marks not-for-the-record stays off it (#154).

The standard is the one ``test_credential_containment.py`` holds an SSH credential to:
the value never reaches the scoped log, ``run_data``, the env snapshot,
``RunResult.to_json()``, a failure's rendering, or any file in the run directory. Here
the secret is the suite's own, obtained in ``configure()`` and read by the claims, so
the sweep also covers the places an author can put it by hand: a log line, a
``TestFailure`` comparison, evidence on a verdict, and a captured artifact.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from litmus import (
    PASS,
    Artifact,
    ContractViolation,
    Failure,
    LitmusTest,
    RunResult,
    SummaryTreeNode,
    TestFailure,
    TestUtils,
    Verdict,
)
from litmus.lifecycle import ChildBoundary, Slot, State
from litmus.redaction import REDACTED, Redactions, compile_redact_ctx, key_is_redacted
from litmus.run_data import snapshot_run_data
from litmus.utils import RunContext, ScopedLogger

_SID = "litmus-154-sid-7c1e9a42b0d8"
_TOKEN = "litmus-154-token-f00dcafe9911"


class _Handle:
    """A live object under a redacted key: shown as redacted, never as a needle."""


# --- the sweep, as #65 defined it ---------------------------------------------------


def _artifact_bytes(root: Path) -> bytes:
    chunks: list[bytes] = []
    if root.exists():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                chunks.append(path.read_bytes())
    return b"\n".join(chunks)


def _failure_texts(failure: Failure | None) -> list[str]:
    texts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = failure
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        texts.append(str(current))
        texts.append(repr(current))
        nested = getattr(current, "cause", None)
        if isinstance(nested, BaseException):
            current = nested
            continue
        current = current.__cause__ or current.__context__
    if failure is not None:
        texts.append(json.dumps(failure.to_json(), ensure_ascii=False, default=str))
    return texts


def _walk_tree(node: SummaryTreeNode) -> list[str]:
    texts = _failure_texts(node.failure)
    texts.append(json.dumps(node.to_json(), ensure_ascii=False, default=str))
    for child in node.children:
        texts.extend(_walk_tree(child))
    return texts


def _evidence(result: RunResult, artifact_root: Path, caplog: pytest.LogCaptureFixture) -> bytes:
    """Concatenate every surface #65 names, plus the tree's own rendering."""
    parts: list[bytes] = [
        _artifact_bytes(artifact_root),
        "\n".join(record.getMessage() for record in caplog.records).encode(),
        json.dumps(result.run_data, ensure_ascii=False, default=str).encode(),
        json.dumps(dict(result.env), ensure_ascii=False).encode(),
        json.dumps(result.to_json(), ensure_ascii=False, default=str).encode(),
    ]
    parts.extend(text.encode() for text in _failure_texts(result.failure))
    parts.extend(text.encode() for text in _walk_tree(result.summary_tree))
    return b"\n".join(parts)


# --- the suite under test ---------------------------------------------------------


class _Seen:
    """What the claims and cleanup read from ctx, so the test can prove it was real."""

    def __init__(self) -> None:
        self.values: list[object] = []


def _suite(seen: _Seen, *, fail: bool) -> LitmusTest:
    class Suite(LitmusTest):
        redact_ctx = ("sid", r"api_.*")

        async def configure(self, utils: TestUtils) -> dict[str, Any]:
            utils.logger.info("logging in")
            return {
                "sid": _SID,
                "api_token": _TOKEN,
                "api_handle": _Handle(),
                "sid_expiry_s": 300,
                "domain": "bench.example",
            }

        def build_suite(self) -> LitmusTest:
            return self.sequence(
                "claims",
                [
                    self.test("blocklist", self._blocklist),
                    self.test("login", self._login),
                ],
            )

        async def _blocklist(self, utils: TestUtils) -> Verdict:
            seen.values.append(utils.ctx["sid"])
            utils.logger.info("session %s carries %s", utils.ctx["sid"], utils.ctx["api_token"])
            utils.logger.debug("posture", extra={"session": utils.ctx["sid"]})
            await utils.capture_artifact(
                Artifact(name="posture", type="survey", data={"session": utils.ctx["sid"]})
            )
            return PASS({"note": f"sid {utils.ctx['sid']} ok"})

        async def _login(self, utils: TestUtils) -> Verdict:
            if fail:
                raise TestFailure(
                    f"FTL refused session {utils.ctx['sid']}",
                    "200",
                    utils.ctx["sid"],
                    evidence={"token_seen": utils.ctx["api_token"]},
                )
            return PASS

        async def cleanup(self, utils: TestUtils) -> None:
            seen.values.append(utils.ctx.get("sid"))
            utils.logger.info("ending session %s", utils.ctx.get("sid"))

    return Suite(name="pihole")


async def _run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, *, fail: bool
) -> tuple[RunResult, bytes, _Seen]:
    seen = _Seen()
    with caplog.at_level(logging.DEBUG, logger="litmus"):
        result = await _suite(seen, fail=fail).run(env={"PHYSICAL_LOCATION": "bench-154"})
    return result, _evidence(result, tmp_path / "artifacts", caplog), seen


# --- declaration --------------------------------------------------------------------


class TestDeclaration:
    def test_a_bare_string_is_refused_at_class_definition(self) -> None:
        with pytest.raises(TypeError, match=r"Suite\.redact_ctx must be a sequence"):

            class Suite(LitmusTest):  # pyright: ignore[reportUnusedClass]
                redact_ctx = "sid"

                def build_suite(self) -> LitmusTest:
                    return self.test("t", self._t)

                async def _t(self, utils: TestUtils) -> Verdict:
                    return PASS

    def test_a_non_string_entry_is_refused(self) -> None:
        with pytest.raises(TypeError, match=r"entries must be non-empty str, not int"):
            compile_redact_ctx("Suite", ("sid", 3))
        with pytest.raises(TypeError, match=r"entries must be non-empty str, not str"):
            compile_redact_ctx("Suite", ("sid", ""))
        with pytest.raises(TypeError, match=r"entries must be non-empty str, not Pattern"):
            compile_redact_ctx("Suite", (re.compile(b"sid"),))

    def test_a_compiled_pattern_is_taken_as_written(self) -> None:
        patterns = compile_redact_ctx("Suite", (re.compile("pin", re.IGNORECASE),))
        assert key_is_redacted("PIN", patterns)
        assert not key_is_redacted("pinned", patterns)

    def test_an_invalid_regular_expression_is_refused_with_the_escape_hint(self) -> None:
        with pytest.raises(
            TypeError, match=r"'key\[' is not a valid regular expression.*re\.escape"
        ):
            compile_redact_ctx("Suite", ["key["])

    def test_sets_and_lists_are_accepted(self) -> None:
        assert [p.pattern for p in compile_redact_ctx("Suite", ["a", "b"])] == ["a", "b"]
        assert {p.pattern for p in compile_redact_ctx("Suite", {"a"})} == {"a"}
        assert compile_redact_ctx("Suite", ()) == ()

    def test_a_subclass_adds_to_its_parent_declaration_and_never_replaces_it(self) -> None:
        class Parent(LitmusTest):
            redact_ctx = ("sid",)

            def build_suite(self) -> LitmusTest:
                return self.test("t", self._t)

            async def _t(self, utils: TestUtils) -> Verdict:
                return PASS

        class Child(Parent):
            pass

        class Override(Parent):
            redact_ctx = ("token",)

        assert [p.pattern for p in Child._redact_ctx_patterns] == ["sid"]  # pyright: ignore[reportPrivateUsage]
        assert [p.pattern for p in Override._redact_ctx_patterns] == ["sid", "token"]  # pyright: ignore[reportPrivateUsage]
        assert LitmusTest._redact_ctx_patterns == ()  # pyright: ignore[reportPrivateUsage]


class TestKeyMatching:
    def test_an_exact_name_matches_only_itself(self) -> None:
        patterns = compile_redact_ctx("Suite", ("sid",))
        assert key_is_redacted("sid", patterns)
        assert not key_is_redacted("sid_expiry_s", patterns)
        assert not key_is_redacted("xsid", patterns)

    def test_a_regular_expression_matches_the_whole_key(self) -> None:
        patterns = compile_redact_ctx("Suite", (r"api_.*",))
        assert key_is_redacted("api_token", patterns)
        assert key_is_redacted("api_", patterns)
        assert not key_is_redacted("xapi_token", patterns)
        assert not key_is_redacted("api", patterns)

    def test_an_escaped_name_with_metacharacters_matches_exactly(self) -> None:
        patterns = compile_redact_ctx("Suite", (re.escape("key[0]"),))
        assert key_is_redacted("key[0]", patterns)
        assert not key_is_redacted("key0", patterns)

    def test_a_literal_with_metacharacters_still_matches_itself(self) -> None:
        patterns = compile_redact_ctx("Suite", ("ip.v4",))
        assert key_is_redacted("ip.v4", patterns)


# --- the registry and the ctx view ------------------------------------------------


class TestRedactions:
    def test_only_non_empty_strings_become_needles(self) -> None:
        redactions = Redactions()
        redactions.register(300)
        redactions.register("")
        redactions.register(None)
        redactions.register(_Handle())
        assert not redactions
        redactions.register("secret")
        redactions.register("secret")
        assert redactions
        assert redactions.mask("a secret here") == f"a {REDACTED} here"

    def test_the_longer_secret_is_masked_whole(self) -> None:
        redactions = Redactions()
        redactions.register("abc")
        redactions.register("abcdef")
        assert redactions.mask("x abcdef y abc z") == f"x {REDACTED} y {REDACTED} z"

    def test_mask_json_reaches_nested_strings_and_leaves_the_rest(self) -> None:
        redactions = Redactions()
        redactions.register("s3cret")
        payload = {"a": "s3cret", "b": ["s3cret", 1, None], "c": {"d": ("x s3cret",)}, "e": 2.5}
        assert redactions.mask_json(payload) == {
            "a": REDACTED,
            "b": [REDACTED, 1, None],
            "c": {"d": [f"x {REDACTED}"]},
            "e": 2.5,
        }
        untouched = {"a": "plain"}
        assert Redactions().mask_json(untouched) is untouched


class TestCtxView:
    def _context(self, seed: dict[str, Any]) -> tuple[RunContext, Redactions]:
        redactions = Redactions()
        ctx = RunContext(owner="pihole", redactions=redactions)
        ctx._declare(compile_redact_ctx("Suite", ("sid", r"api_.*")))  # pyright: ignore[reportPrivateUsage]
        ctx._merge(seed)  # pyright: ignore[reportPrivateUsage]
        return ctx, redactions

    def test_a_redacted_key_is_shown_as_redacted_whatever_it_holds(self) -> None:
        ctx, _ = self._context(
            {"sid": _SID, "api_handle": _Handle(), "api_n": 7, "sid_expiry_s": 300, "d": "x"}
        )
        view = snapshot_run_data(ctx, redacted=ctx.redacted)
        assert view == {
            "measurements": [],
            "sid": REDACTED,
            "api_handle": REDACTED,
            "api_n": REDACTED,
            "sid_expiry_s": 300,
            "d": "x",
        }
        assert ctx["sid"] == _SID

    def test_a_merge_registers_every_string_under_a_redacted_key(self) -> None:
        ctx, redactions = self._context({"sid": _SID, "api_n": 7, "domain": "bench.example"})
        assert redactions.mask(f"{_SID} bench.example 7") == f"{REDACTED} bench.example 7"
        ctx["api_late"] = _TOKEN
        ctx.update(sid2=_TOKEN + "x")
        ctx.update({"api_more": "m0re"})
        assert redactions.mask(f"{_TOKEN} m0re {_TOKEN}x") == f"{REDACTED} {REDACTED} {REDACTED}x"

    def test_a_context_without_a_declaration_redacts_nothing(self) -> None:
        ctx = RunContext({"sid": _SID}, owner="plain", redactions=Redactions())
        assert not ctx.redacted("sid")
        assert snapshot_run_data(ctx, redacted=ctx.redacted)["sid"] == _SID
        assert snapshot_run_data(ctx)["sid"] == _SID


# --- the surfaces -----------------------------------------------------------------


class TestScopedLoggerMasking:
    def test_a_record_is_masked_before_the_process_logger_sees_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        redactions = Redactions()
        redactions.register(_SID)
        logger = ScopedLogger(logging.getLogger("litmus"), ("pihole",), redactions=redactions)
        with caplog.at_level(logging.DEBUG, logger="litmus"):
            logger.info("session %s opened", _SID)
            logger.info("plain %d", 3)
            logger.info("tagged", extra={"session": _SID, "n": 1})
        messages = [record.getMessage() for record in caplog.records]
        assert messages == [f"session {REDACTED} opened", "plain 3", "tagged"]
        assert getattr(caplog.records[2], "session", None) == REDACTED
        assert caplog.records[1].args == (3,), "an untouched record keeps its arguments"

    def test_without_redactions_the_record_passes_through_untouched(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        logger = ScopedLogger(logging.getLogger("litmus"), ("pihole",))
        with caplog.at_level(logging.DEBUG, logger="litmus"):
            logger.info("session %s opened", _SID)
        assert caplog.records[0].getMessage() == f"session {_SID} opened"
        assert caplog.records[0].args == (_SID,)


class TestFailureMasking:
    def test_a_recorded_failure_masks_its_message_comparison_and_evidence(self) -> None:
        redactions = Redactions()
        redactions.register(_SID)
        failure = TestFailure(f"refused {_SID}", "200", _SID, evidence={"seen": [_SID]})
        assert _SID in str(failure)
        boundary = ChildBoundary("login", redactions=redactions)

        async def raise_it() -> object:
            raise failure

        outcome = asyncio.run(boundary.run(Slot.TEST_METHOD, raise_it()))
        assert outcome.state is State.FAIL
        assert outcome.failure is failure
        assert str(failure) == f"refused {REDACTED}"
        assert _SID not in repr(failure)
        assert failure.to_json() == {
            "name": "TestFailure",
            "fail_message": f"refused {REDACTED}",
            "state": "Fail",
            "expected": "200",
            "received": REDACTED,
            "evidence": {"seen": [REDACTED]},
        }
        assert outcome.evidence == {"seen": [REDACTED]}

    def test_the_chain_beneath_a_recorded_failure_is_masked_too(self) -> None:
        redactions = Redactions()
        redactions.register(_SID)
        inner = TestFailure(f"inner {_SID}")
        outer = ContractViolation("login", f"outer {_SID}")
        outer.__cause__ = inner
        boundary = ChildBoundary("login", redactions=redactions)

        async def raise_it() -> object:
            raise outer

        asyncio.run(boundary.run(Slot.TEST_METHOD, raise_it()))
        assert _SID not in str(outer)
        assert _SID not in str(inner)

    def test_an_unbound_failure_renders_as_written(self) -> None:
        failure = TestFailure(f"refused {_SID}")
        assert str(failure) == f"refused {_SID}"
        assert failure.to_json()["fail_message"] == f"refused {_SID}"


class TestRecordContainment:
    async def test_a_passing_run_publishes_neither_secret(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        result, haystack, seen = await _run(tmp_path, caplog, fail=False)

        assert result.state is State.PASS
        assert seen.values == [_SID, _SID], "the claims and cleanup read the real value"
        assert _SID.encode() not in haystack
        assert _TOKEN.encode() not in haystack
        assert result.run_data["sid"] == REDACTED
        assert result.run_data["api_token"] == REDACTED
        assert result.run_data["api_handle"] == REDACTED
        assert result.run_data["sid_expiry_s"] == 300
        assert result.run_data["domain"] == "bench.example"
        assert REDACTED.encode() in haystack

    async def test_a_failing_run_masks_the_failure_on_every_surface(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        result, haystack, _ = await _run(tmp_path, caplog, fail=True)

        assert result.state is State.FAIL
        assert _SID.encode() not in haystack
        assert _TOKEN.encode() not in haystack
        claims = result.summary_tree.children[0]
        login = next(child for child in claims.children if child.name == "login")
        assert login.failure is not None
        assert str(login.failure) == f"FTL refused session {REDACTED}"
        assert login.failure.to_json()["received"] == REDACTED
        assert login.evidence == {"token_seen": REDACTED}
        blocklist = next(child for child in claims.children if child.name == "blocklist")
        assert blocklist.evidence == {"note": f"sid {REDACTED} ok"}

    async def test_the_run_directory_carries_the_masked_text_not_the_secret(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        await _run(tmp_path, caplog, fail=False)

        root = tmp_path / "artifacts"
        timelines = [path.read_text() for path in root.rglob("timeline.jsonl")]
        assert timelines, "the run wrote a timeline"
        joined = "\n".join(timelines)
        assert f"session {REDACTED} carries {REDACTED}" in joined
        assert f"ending session {REDACTED}" in joined
        artifacts = [path.read_text() for path in root.rglob("*.artifact")]
        assert artifacts
        assert all(REDACTED in text for text in artifacts)
        result_json = json.loads(next(root.rglob("result.json")).read_text())
        assert result_json["run_data"]["sid"] == REDACTED

    @pytest.mark.parametrize("write", ["setdefault", "ior"])
    async def test_a_secret_inserted_by_setdefault_or_ior_is_masked_run_wide(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, write: str
    ) -> None:
        # ``dict.setdefault`` and ``dict.__ior__`` insert at C level without passing
        # through ``__setitem__``; each override must register on its own, or the ctx
        # view says REDACTED while the secret sits in the timeline and the failure.
        class Suite(LitmusTest):
            redact_ctx = ("sid",)

            async def configure(self, utils: TestUtils) -> dict[str, Any]:
                # ``|=`` on the property would rebind it; the write goes to the mapping.
                ctx = utils.ctx
                assert isinstance(ctx, RunContext)
                if write == "setdefault":
                    ctx.setdefault("sid", _SID)
                else:
                    ctx |= {"sid": _SID}
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("login", self._login)

            async def _login(self, utils: TestUtils) -> Verdict:
                utils.logger.info("session %s", utils.ctx["sid"])
                raise TestFailure(
                    f"FTL refused session {utils.ctx['sid']}", "200", utils.ctx["sid"]
                )

        with caplog.at_level(logging.DEBUG, logger="litmus"):
            result = await Suite(name="pihole").run(env={"PHYSICAL_LOCATION": "bench-154"})
        haystack = _evidence(result, tmp_path / "artifacts", caplog)

        assert result.state is State.FAIL
        assert result.run_data["sid"] == REDACTED
        assert _SID.encode() not in haystack
        login = (
            result.summary_tree.children[0] if result.summary_tree.children else result.summary_tree
        )
        assert login.failure is not None
        assert str(login.failure) == f"FTL refused session {REDACTED}"
        assert login.failure.to_json()["received"] == REDACTED

    async def test_a_nested_suite_secret_is_masked_run_wide(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Child(LitmusTest):
            redact_ctx = ("child_secret",)

            async def configure(self, utils: TestUtils) -> dict[str, Any]:
                return {"child_secret": _TOKEN}

            def build_suite(self) -> LitmusTest:
                return self.test("leaf", self._leaf)

            async def _leaf(self, utils: TestUtils) -> Verdict:
                utils.logger.info("child holds %s", utils.ctx["child_secret"])
                return PASS

        class Parent(LitmusTest):
            redact_ctx = ("parent_secret",)

            async def configure(self, utils: TestUtils) -> dict[str, Any]:
                return {"parent_secret": _SID}

            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    "steps", [Child(name="child"), self.test("after", self._after)]
                )

            async def _after(self, utils: TestUtils) -> Verdict:
                # The child's secret is not in this ctx, but it is in the record's
                # registry: a secret is secret wherever it appears.
                utils.logger.info("parent saw %s and %s", utils.ctx["parent_secret"], _TOKEN)
                return PASS

        with caplog.at_level(logging.DEBUG, logger="litmus"):
            result = await Parent(name="rack").run(env={"PHYSICAL_LOCATION": "bench-154"})
        haystack = _evidence(result, tmp_path / "artifacts", caplog)

        assert result.state is State.PASS
        assert _SID.encode() not in haystack
        assert _TOKEN.encode() not in haystack
        assert result.run_data["parent_secret"] == REDACTED
