"""JUnit XML reporter: three terminal states, pinned, from any RunResult."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast
from xml.etree.ElementTree import Element, fromstring

import litmus
from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict
from litmus.junit import JunitXml
from litmus.lifecycle import Slot, State
from litmus.results import NodeType, RunResult, SummaryTreeNode


def _xml(result: object) -> Element:
    rendered = JunitXml().render(cast("litmus.RunResult", result))
    assert rendered.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    root = fromstring(rendered.split("\n", 1)[1])
    assert root.tag == "testsuites"
    return root


def _cases(root: Element) -> list[Element]:
    suite = root.find("testsuite")
    assert suite is not None
    return list(suite.findall("testcase"))


def _by_name(root: Element, name: str) -> Element:
    matched = [case for case in _cases(root) if case.get("name") == name]
    assert len(matched) == 1, name
    return matched[0]


class TestPublicSurface:
    def test_junit_xml_is_not_on_the_public_surface(self) -> None:
        assert not hasattr(litmus, "JunitXml")
        assert "JunitXml" not in litmus.__all__


class TestPassFailAborted:
    async def test_pass_is_a_bare_testcase(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("say hello", self._say)

            async def _say(self, _utils: TestUtils) -> Verdict:
                return PASS

        root = _xml(await Suite(name="hello").run())
        case = _by_name(root, "say hello")

        assert root.get("failures") == "0"
        assert root.get("errors") == "0"
        assert root.get("skipped") == "0"
        assert case.find("failure") is None
        assert case.find("error") is None
        assert case.find("skipped") is None
        assert case.get("classname") == "hello"

    async def test_fail_is_a_failure_element_carrying_the_taxonomy(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("firmware", self._firmware)

            async def _firmware(self, _utils: TestUtils) -> Verdict:
                raise TestFailure("gate remained closed", "open", "closed")

        root = _xml(await Suite(name="rack").run())
        case = _by_name(root, "firmware")
        failure = case.find("failure")
        assert failure is not None
        assert case.find("error") is None
        assert failure.get("type") == "TestFailure"
        assert failure.get("message") == "gate remained closed"
        payload = json.loads(failure.text or "")
        assert payload["name"] == "TestFailure"
        assert payload["fail_message"] == "gate remained closed"
        assert payload["state"] == "Fail"
        assert payload["expected"] == "open"
        assert payload["received"] == "closed"
        assert root.get("failures") == "1"
        assert root.get("errors") == "0"

    async def test_aborted_is_an_error_element_not_a_failure(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("forgot", self._forgot)

            async def _forgot(self, _utils: TestUtils) -> Verdict:
                return None  # type: ignore[return-value]

        root = _xml(await Suite(name="rack").run())
        case = _by_name(root, "forgot")
        error = case.find("error")
        assert error is not None
        assert case.find("failure") is None
        assert error.get("type") == "MissingVerdict"
        payload = json.loads(error.text or "")
        assert payload["name"] == "MissingVerdict"
        assert payload["state"] == "Aborted"
        assert payload["bug"] is True
        assert root.get("failures") == "0"
        assert root.get("errors") == "1"


class TestTreeShape:
    async def test_parallel_nodes_become_classname_leaves_become_testcases(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "claims",
                    [
                        self.test("identity", self._pass),
                        self.test("firmware", self._fail),
                    ],
                )

            async def _pass(self, _utils: TestUtils) -> Verdict:
                return PASS

            async def _fail(self, _utils: TestUtils) -> Verdict:
                raise TestFailure("gate remained closed")

        root = _xml(await Suite(name="rack").run())
        names = [case.get("name") for case in _cases(root)]
        classnames = {case.get("name"): case.get("classname") for case in _cases(root)}

        assert names == ["identity", "firmware"]
        assert classnames["identity"] == "rack/claims"
        assert classnames["firmware"] == "rack/claims"
        assert root.get("tests") == "2"
        assert root.get("failures") == "1"
        assert root.get("errors") == "0"

    async def test_unreached_sequence_steps_are_skipped(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    "ordered",
                    [
                        self.test("gate", self._fail),
                        self.test("never", self._pass),
                    ],
                )

            async def _fail(self, _utils: TestUtils) -> Verdict:
                raise TestFailure("gate remained closed")

            async def _pass(self, _utils: TestUtils) -> Verdict:
                return PASS

        root = _xml(await Suite(name="rack").run())
        skipped = _by_name(root, "never")
        failed = _by_name(root, "gate")

        assert skipped.find("skipped") is not None
        assert failed.find("failure") is not None
        assert root.get("skipped") == "1"
        assert root.get("failures") == "1"

    async def test_configure_failure_is_a_testcase_not_a_skip(self) -> None:
        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                del utils
                raise TestFailure("device missing")

            def build_suite(self) -> LitmusTest:
                return self.test("never", self._never)

            async def _never(self, _utils: TestUtils) -> Verdict:
                return PASS

        result = await Suite(name="bring-up").run()
        root = _xml(result)
        names = [case.get("name") for case in _cases(root)]

        assert result.state is State.FAIL
        assert names == ["bring-up"]
        failed = _by_name(root, "bring-up")
        failure = failed.find("failure")
        assert failure is not None
        assert json.loads(failure.text or "")["fail_message"] == "device missing"
        assert root.get("failures") == "1"
        assert root.get("skipped") == "0"


class TestArtifactPointers:
    async def test_system_out_points_at_the_timeline(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("say hello", self._say)

            async def _say(self, _utils: TestUtils) -> Verdict:
                return PASS

        result = await Suite(name="hello").run()
        root = _xml(result)
        case = _by_name(root, "say hello")
        system_out = case.find("system-out")
        assert system_out is not None
        assert system_out.text is not None
        assert system_out.text.startswith("timeline: ")
        assert system_out.text.endswith("timeline.jsonl")

    async def test_write_persists_utf8_xml(self, tmp_path: Path) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("say hello", self._say)

            async def _say(self, _utils: TestUtils) -> Verdict:
                return PASS

        result = await Suite(name="hello").run()
        path = tmp_path / "report.xml"
        JunitXml().write(result, path)
        text = path.read_text(encoding="utf-8")
        assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        parsed = fromstring(text.split("\n", 1)[1])
        assert parsed.tag == "testsuites"
        assert parsed.get("name") == "hello"


class TestAnyRunResult:
    async def test_a_parallel_node_of_mixed_states_round_trips_the_counts(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "rack",
                    [
                        self.test("pass", self._pass),
                        self.test("fail", self._fail),
                        self.test("abort", self._abort),
                    ],
                )

            async def _pass(self, _utils: TestUtils) -> Verdict:
                return PASS

            async def _fail(self, _utils: TestUtils) -> Verdict:
                raise TestFailure("nope")

            async def _abort(self, _utils: TestUtils) -> Verdict:
                return None  # type: ignore[return-value]

        root = _xml(await Suite(name="station").run())
        assert root.get("tests") == "3"
        assert root.get("failures") == "1"
        assert root.get("errors") == "1"
        assert root.get("skipped") == "0"
        suite = root.find("testsuite")
        assert suite is not None
        assert suite.get("tests") == "3"
        assert suite.get("failures") == "1"
        assert suite.get("errors") == "1"


class TestTimeoutProjection:
    async def test_abort_after_is_an_error_with_timeout_pointer(self) -> None:
        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name, abort_after=0.03)

            def build_suite(self) -> LitmusTest:
                return self.test("stall", self._stall)

            async def _stall(self, _utils: TestUtils) -> Verdict:
                await asyncio.Event().wait()
                return PASS

        result = await asyncio.wait_for(Suite(name="timed").run(), 1.0)
        root = _xml(result)
        case = _by_name(root, "stall")
        error = case.find("error")
        system_out = case.find("system-out")
        assert result.state is State.ABORTED
        assert error is not None
        assert case.find("failure") is None
        assert error.get("type") == "Aborted"
        assert error.text is not None
        timeout = json.loads(error.text)
        assert timeout["declared"] == "abort_after"
        assert system_out is not None
        assert system_out.text is not None
        assert "timeout: abort_after" in system_out.text
        assert root.get("errors") == "1"

    async def test_fail_after_keeps_failure_and_names_the_clock(self) -> None:
        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name, fail_after=0.03)

            def build_suite(self) -> LitmusTest:
                return self.test("stall", self._stall)

            async def _stall(self, _utils: TestUtils) -> Verdict:
                await asyncio.Event().wait()
                return PASS

        result = await asyncio.wait_for(Suite(name="timed").run(), 1.0)
        root = _xml(result)
        case = _by_name(root, "stall")
        failure = case.find("failure")
        system_out = case.find("system-out")
        assert result.state is State.FAIL
        assert failure is not None
        assert case.find("error") is None
        assert system_out is not None
        assert system_out.text is not None
        assert "timeout: fail_after" in system_out.text


class TestConstructedTrees:
    def test_aborted_without_taxonomy_or_log_is_still_an_error(self) -> None:
        leaf = SummaryTreeNode(name="odd", type=NodeType.TEST, state=State.ABORTED)
        result = RunResult(
            slot=Slot.TEST_METHOD,
            node="odd",
            state=State.ABORTED,
            ancestor_names=["odd"],
            bread_crumbs="odd",
            summary_tree=leaf,
            type=NodeType.TEST,
        )
        root = _xml(result)
        case = _by_name(root, "odd")
        error = case.find("error")
        assert error is not None
        assert error.get("type") == "Aborted"
        assert error.text is None
        assert case.find("system-out") is None
        assert case.find("failure") is None

    def test_composite_that_settled_before_children_ran_is_a_testcase(self) -> None:
        pending = SummaryTreeNode(name="never", type=NodeType.TEST, state=None)
        tree = SummaryTreeNode(
            name="rack",
            type=NodeType.SUITE,
            state=State.FAIL,
            failure=TestFailure("bring-up died"),
            children=(pending,),
        )
        result = RunResult(
            slot=Slot.CONFIGURE,
            node="rack",
            state=State.FAIL,
            failure=TestFailure("bring-up died"),
            ancestor_names=["rack"],
            bread_crumbs="rack",
            summary_tree=tree,
            type=NodeType.SUITE,
        )
        root = _xml(result)
        names = [case.get("name") for case in _cases(root)]
        assert names == ["rack", "never"]
        assert _by_name(root, "rack").find("failure") is not None
        assert _by_name(root, "never").find("skipped") is not None
        assert root.get("failures") == "1"
        assert root.get("skipped") == "1"
