"""The public LitmusTest atom and its framework-owned execution path."""

import pytest

import litmus
from litmus import PASS, ContractViolation, LitmusTest, TestUtils, Verdict
from litmus.lifecycle import State


class TestLitmusTestPublicSurface:
    def test_litmus_test_is_the_single_public_node_class(self) -> None:
        assert litmus.LitmusTest is LitmusTest
        assert "LitmusTest" in litmus.__all__
        assert not hasattr(litmus, "_Case")

    def test_main_cannot_be_overridden(self) -> None:
        with pytest.raises(TypeError, match=r"main.*framework-owned"):

            class OverridesMain(LitmusTest):  # pyright: ignore[reportUnusedClass]
                async def main(  # pyright: ignore[reportIncompatibleMethodOverride]
                    self, utils: TestUtils
                ) -> Verdict:
                    return PASS


class TestLitmusTestImmutability:
    def test_subclass_constructor_can_finish_before_instance_freezes(self) -> None:
        class Device(LitmusTest):
            def __init__(self, *, name: str, serial: str) -> None:
                super().__init__(name=name)
                self.serial = serial

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        device = Device(name="dut", serial="abc123")

        assert device.name == "dut"
        assert device.serial == "abc123"
        with pytest.raises(AttributeError, match="immutable after construction"):
            device.serial = "changed"
        with pytest.raises(AttributeError, match="immutable after construction"):
            del device.serial

    def test_framework_has_an_explicit_internal_escape_hatch(self) -> None:
        test = LitmusTest(name="evidence")

        object.__setattr__(test, "_has_run", True)

        assert test._has_run is True  # pyright: ignore[reportPrivateUsage]


class TestLitmusTestMain:
    async def test_bound_verdict_method_reaches_main_only_through_case(self) -> None:
        calls: list[object] = []

        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.calls = calls

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: object) -> Verdict:
                self.calls.append(utils)
                return PASS

        marker = TestUtils()

        outcome = await Suite(name="rack").main(marker)

        assert outcome.state is State.PASS
        assert len(calls) == 1
        assert calls[0] is not marker
        assert isinstance(calls[0], TestUtils)
        assert calls[0].ctx is marker.ctx

    async def test_instance_cannot_run_twice(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        suite = Suite(name="rack")

        outcome = await suite.main(TestUtils())

        assert outcome.state is State.PASS
        with pytest.raises(ContractViolation, match="may run only once"):
            await suite.main(TestUtils())
