"""Tier 1 of the context contract: a ``[]`` read of an unprovided key is a bug (issue #112).

``configure()`` returns every key a test method, hook, or ``cleanup()`` reads by
subscript. The framework's context object turns any other ``[]`` read into a
``ContractViolation`` that names the test and the key. ``get`` and ``in`` are the author
declaring a key optional and behave as on any ``dict``.
"""

import json

import pytest

from litmus import PASS, ContractViolation, LitmusTest, State, TestFailure, TestUtils, Verdict
from litmus.errors import ContextInsufficient
from litmus.results import SummaryTreeNode


def _leaf(tree: SummaryTreeNode, index: int = 0) -> SummaryTreeNode:
    return tree.children[index]


class TestMissingKeyIsAContractViolation:
    async def test_test_method_read_lands_as_a_bug_naming_the_test_and_the_key(self) -> None:
        cleaned: list[str] = []

        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                return {"present": 1}

            def build_suite(self) -> LitmusTest:
                return self.test("rail", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                if utils.ctx["absent"]:
                    return PASS
                raise TestFailure("rail down", "up", "down")

            async def cleanup(self, utils: TestUtils) -> None:
                cleaned.append("cleanup")

        result = await Suite(name="psu").run()

        assert result.state is State.FAIL
        failure = _leaf(result.summary_tree).failure
        assert isinstance(failure, ContextInsufficient)
        assert isinstance(failure, ContractViolation)
        assert failure.is_bug
        assert failure.node == "rail"
        assert failure.key == "absent"
        assert failure.owner == "psu"
        assert failure.fail_message == (
            "CONTRACT VIOLATION IN rail: context insufficient: configure() did not provide "
            "'absent' (context owned by 'psu')"
        )
        assert failure.to_json()["key"] == "absent"
        assert cleaned == ["cleanup"]

    async def test_optional_reads_never_raise(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("rail", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                seen = (
                    utils.ctx.get("absent"),
                    "absent" in utils.ctx,
                    utils.ctx.setdefault("samples", []),
                )
                if seen == (None, False, []):
                    return PASS
                raise TestFailure("optional reads changed", "(None, False, [])", repr(seen))

        result = await Suite(name="psu").run()

        assert result.state is State.PASS

    async def test_a_suite_except_keyerror_cannot_swallow_it(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("rail", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                try:
                    value = utils.ctx["absent"]
                except KeyError:
                    value = "defaulted"
                if value:
                    return PASS
                raise TestFailure("no value", "value", "none")

        result = await Suite(name="psu").run()

        assert result.state is State.FAIL
        assert isinstance(_leaf(result.summary_tree).failure, ContextInsufficient)

    async def test_configure_reading_an_unprovided_key_is_its_own_bug(self) -> None:
        calls: list[str] = []

        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                return {"rail": utils.ctx["rail"]}

            def build_suite(self) -> LitmusTest:
                raise AssertionError("configure failure must skip the suite")

            async def cleanup(self, utils: TestUtils) -> None:
                calls.append("cleanup")

        result = await Suite(name="psu").main(TestUtils())

        assert result.state is State.FAIL
        assert isinstance(result.failure, ContextInsufficient)
        assert result.failure.node == "psu"
        assert result.failure.key == "rail"
        assert result.failure.fail_message == (
            "CONTRACT VIOLATION IN psu: context insufficient: configure() did not provide 'rail'"
        )
        assert calls == ["cleanup"]

    async def test_composed_child_reads_its_own_context_not_the_parents(self) -> None:
        class Child(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("child claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                if utils.ctx["shared"]:
                    return PASS
                raise TestFailure("no", "shared", "none")

        class Parent(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                return {"shared": True}

            def build_suite(self) -> LitmusTest:
                return Child(name="child")

        result = await Parent(name="parent").run()

        assert result.state is State.FAIL
        failure = _leaf(_leaf(result.summary_tree)).failure
        assert isinstance(failure, ContextInsufficient)
        assert failure.node == "child claim"
        assert failure.owner == "child"
        assert "(context owned by 'child')" in failure.fail_message

    async def test_cleanup_read_is_logged_and_leaves_the_verdict_alone(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("rail", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                utils.ctx["shell"]

        with caplog.at_level("ERROR", logger="litmus"):
            result = await Suite(name="psu").run()

        assert result.state is State.PASS
        assert any(
            "context insufficient: configure() did not provide 'shell'" in record.getMessage()
            for record in caplog.records
        )


class TestTheContextIsStillADict:
    async def test_ctx_is_a_dict_that_snapshots_and_serializes(self) -> None:
        seen: dict[str, object] = {}

        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                return {"rail": "on"}

            def build_suite(self) -> LitmusTest:
                return self.test("rail", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                utils.ctx["reading"] = 12.1
                seen["is_dict"] = dict in type(utils.ctx).__mro__
                seen["json"] = json.dumps(utils.ctx, sort_keys=True)
                seen["copy"] = dict(utils.ctx)
                return PASS

        result = await Suite(name="psu").run()

        assert result.state is State.PASS
        assert seen["is_dict"] is True
        assert seen["json"] == '{"rail": "on", "reading": 12.1}'
        assert seen["copy"] == {"rail": "on", "reading": 12.1}
        assert result.run_data["rail"] == "on"
        assert result.run_data["reading"] == 12.1

    def test_a_seed_dict_is_copied_into_the_run_context(self) -> None:
        seed = {"station": "west"}

        utils = TestUtils(ctx=seed)

        assert utils.ctx == {"station": "west"}
        assert utils.ctx is not seed
        with pytest.raises(ContractViolation, match="did not provide 'absent'"):
            utils.ctx["absent"]
