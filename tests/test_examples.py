"""Executable Phase 1 examples and the terminal states they promise readers."""

from types import SimpleNamespace
from typing import cast

from examples.hello_world import HelloWorld
from examples.test_composition import GroupedDevice, SequencedDevice

from litmus import TestFailure
from litmus.lifecycle import Outcome, State


class TestHelloWorldExample:
    """Pin the minimum authored suite to an explicit Pass verdict."""

    async def test_hello_world_passes(self) -> None:
        outcome = await HelloWorld(name="hello world").main(SimpleNamespace(ctx={}))

        assert outcome.state is State.PASS


class TestCompositionExample:
    """Pin the legacy composition lesson to modern sequence and group semantics."""

    async def test_sequence_stops_at_first_dependent_failure(self) -> None:
        outcome = await SequencedDevice(
            name="startup checks",
            service_active=False,
            firmware_version="2.4.1",
            application_pid=412,
        ).main(SimpleNamespace(ctx={}))
        children = cast("tuple[Outcome, ...]", outcome.value)

        assert outcome.state is State.FAIL
        assert [child.node for child in children] == ["boot-check"]
        assert isinstance(outcome.failure, TestFailure)
        assert outcome.failure.expected == "active"
        assert outcome.failure.received == "inactive"

    async def test_group_collects_all_independent_results(self) -> None:
        outcome = await GroupedDevice(
            name="sensor checks",
            temperature_c=92,
            voltage_v=3.3,
            fan_rpm=800,
        ).main(SimpleNamespace(ctx={}))
        children = cast("tuple[Outcome, ...]", outcome.value)

        assert outcome.state is State.FAIL
        assert [child.node for child in children] == [
            "temperature",
            "voltage",
            "fan-speed",
        ]
        assert [child.state for child in children] == [
            State.FAIL,
            State.PASS,
            State.FAIL,
        ]

    async def test_healthy_compositions_pass(self) -> None:
        sequence = await SequencedDevice(
            name="startup checks",
            service_active=True,
            firmware_version="release-2.4.1",
            application_pid=412,
        ).main(SimpleNamespace(ctx={}))
        group = await GroupedDevice(
            name="sensor checks",
            temperature_c=42,
            voltage_v=3.3,
            fan_rpm=1800,
        ).main(SimpleNamespace(ctx={}))

        assert sequence.state is State.PASS
        assert group.state is State.PASS
