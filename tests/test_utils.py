"""Per-node TestUtils ownership and scoped evidence logging."""

import ast
from pathlib import Path
from typing import Protocol, cast

import pytest

import litmus
from litmus import PASS, LitmusTest, TestUtils, Verdict
from litmus.lifecycle import State


class _ScopedLogRecord(Protocol):
    litmus_node: str
    litmus_scope: tuple[str, ...]
    measurement: int


class TestTestUtilsPublicSurface:
    def test_test_utils_is_the_exported_lifecycle_surface(self) -> None:
        assert litmus.TestUtils is TestUtils
        assert "TestUtils" in litmus.__all__

        utils = TestUtils()

        assert callable(utils.exec)
        assert callable(utils.wait_for)
        assert isinstance(utils.ctx, dict)
        assert utils.logger.scope == ()
        assert not utils.signal.fired


class TestTestUtilsInjection:
    async def test_run_constructs_the_root_utility_seed(self) -> None:
        received: list[TestUtils] = []

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                received.append(utils)
                return PASS

        outcome = await Suite(name="rack").run(env={"STATION": "west"})

        assert outcome.state is State.PASS
        assert received[0].env["STATION"] == "west"
        assert received[0].logger.scope == ("rack", "claim")

    async def test_every_node_receives_its_own_scoped_utils(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        seen: dict[str, TestUtils] = {}

        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                seen["configure"] = utils
                return {"serial": "SN-0042"}

            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    "claims",
                    [
                        self.case("first", self._first),
                        self.case("second", self._second),
                    ],
                )

            async def _first(self, utils: TestUtils) -> Verdict:
                seen["first"] = utils
                utils.logger.info("first observation", extra={"measurement": 1})
                return PASS

            async def _second(self, utils: TestUtils) -> Verdict:
                seen["second"] = utils
                utils.logger.info("second observation")
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                seen["cleanup"] = utils

        source_env = {"STATION": "west"}
        seed = TestUtils(ctx={"injected": True}, env=source_env)
        source_env["STATION"] = "changed after snapshot"

        with caplog.at_level("INFO", logger="litmus"):
            outcome = await Suite(name="rack").main(seed)

        assert outcome.state is State.PASS
        assert seen["configure"] is seen["cleanup"]
        assert len({id(utils) for utils in seen.values()}) == 3
        assert all(utils.ctx is seed.ctx for utils in seen.values())
        assert seed.ctx == {"injected": True, "serial": "SN-0042"}
        assert seen["configure"].logger.scope == ("rack",)
        assert seen["first"].logger.scope == ("rack", "claims", "first")
        assert seen["second"].logger.scope == ("rack", "claims", "second")
        assert all(utils.env["STATION"] == "west" for utils in seen.values())
        assert all(not utils.signal.fired for utils in seen.values())

        records = [
            cast("_ScopedLogRecord", record)
            for record in caplog.records
            if "observation" in record.message
        ]
        assert [record.litmus_node for record in records] == ["first", "second"]
        assert [record.litmus_scope for record in records] == [
            ("rack", "claims", "first"),
            ("rack", "claims", "second"),
        ]
        assert records[0].measurement == 1

        with pytest.raises(TypeError):
            cast("dict[str, str]", seen["configure"].env)["STATION"] = "mutated"


class TestFrameworkLoggingDiscipline:
    def test_framework_code_never_calls_print(self) -> None:
        source_root = Path(__file__).parents[1] / "src" / "litmus"
        violations: list[str] = []

        for source_path in source_root.rglob("*.py"):
            tree = ast.parse(source_path.read_text(), filename=str(source_path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"
                ):
                    relative = source_path.relative_to(source_root)
                    violations.append(f"{relative}:{node.lineno}")

        assert violations == []
