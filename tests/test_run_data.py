"""Automatic structured evidence captured from node-local runtime context."""

import json
from collections.abc import Mapping
from typing import Any, cast

from litmus import PASS, LitmusTest, TestUtils, Verdict
from litmus.evidence import snapshot_run_data


class _Handle:
    """Representative live framework or device handle that must never serialize."""


class _DictInstance(dict[str, object]):
    """A dict-shaped class instance is not a plain dict."""


class _ListInstance(list[object]):
    """A list-shaped class instance is not a plain list."""


class TestRunDataFiltering:
    def test_snapshot_keeps_complete_json_values_and_strips_instances(self) -> None:
        ctx: dict[str, object] = {
            "none": None,
            "boolean": True,
            "integer": 3,
            "number": 12.25,
            "text": "ready",
            "nested": {"samples": [1, 2.5, None, {"valid": False}]},
            "handle": _Handle(),
            "function": lambda: None,
            "tuple": (1, 2),
            "dict_instance": _DictInstance(valid=True),
            "list_instance": _ListInstance([1, 2]),
        }

        snapshot = snapshot_run_data(ctx)

        assert snapshot == {
            "measurements": [],
            "none": None,
            "boolean": True,
            "integer": 3,
            "number": 12.25,
            "text": "ready",
            "nested": {"samples": [1, 2.5, None, {"valid": False}]},
        }
        assert json.loads(json.dumps(snapshot, allow_nan=False)) == snapshot

    def test_invalid_nested_value_strips_its_complete_top_level_entry(self) -> None:
        cycle: list[object] = []
        cycle.append(cycle)
        ctx: dict[str, object] = {
            "valid": {"reading": 4.2},
            "nested_handle": {"reading": 4.2, "transport": _Handle()},
            "nested_function": [1, lambda: None],
            "nested_bad_key": {1: "coercion would change the evidence"},
            "cycle": cycle,
            "nan": float("nan"),
            "positive_infinity": float("inf"),
            "negative_infinity": float("-inf"),
        }

        assert snapshot_run_data(ctx) == {
            "measurements": [],
            "valid": {"reading": 4.2},
        }

    def test_non_string_ctx_key_is_stripped_instead_of_coerced(self) -> None:
        untyped_ctx: dict[object, object] = {"valid": 1, 2: "invalid key"}

        snapshot = snapshot_run_data(cast(Mapping[str, object], untyped_ctx))

        assert snapshot == {"measurements": [], "valid": 1}

    def test_measurements_is_a_framework_owned_reserved_slot(self) -> None:
        ctx: dict[str, object] = {
            "measurements": [{"name": "voltage", "value": 12.1}],
            "serial": "SN-0042",
        }

        assert snapshot_run_data(ctx) == {
            "measurements": [],
            "serial": "SN-0042",
        }


class TestLitmusTestRunData:
    async def test_run_data_captures_configure_and_test_method_context(self) -> None:
        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, Any]:
                return {"serial": "SN-0042", "transport": _Handle()}

            def build_suite(self) -> LitmusTest:
                return self.case("sample", self._sample)

            async def _sample(self, utils: TestUtils) -> Verdict:
                utils.ctx["voltage"] = 12.1
                return PASS

        suite = Suite(name="power-supply")

        await suite.main()

        assert suite.run_data == {
            "measurements": [],
            "serial": "SN-0042",
            "voltage": 12.1,
        }

    async def test_run_data_is_a_detached_snapshot_of_node_local_context(self) -> None:
        samples = [1]
        seed = TestUtils(ctx={"evidence": {"samples": samples}})

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                return PASS

        suite = Suite(name="rack")
        await suite.main(seed)

        first_snapshot = suite.run_data
        samples.append(2)

        assert first_snapshot == {"measurements": [], "evidence": {"samples": [1]}}
        assert suite.run_data == {
            "measurements": [],
            "evidence": {"samples": [1, 2]},
        }

    async def test_composed_authored_nodes_capture_only_their_own_context(self) -> None:
        class Child(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, Any]:
                return {"child": "local"}

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                return PASS

        child = Child(name="child")

        class Parent(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, Any]:
                return {"parent": "local"}

            def build_suite(self) -> LitmusTest:
                return self.sequence("children", [child])

        parent = Parent(name="parent")

        await parent.main()

        assert parent.run_data == {"measurements": [], "parent": "local"}
        assert child.run_data == {"measurements": [], "child": "local"}
