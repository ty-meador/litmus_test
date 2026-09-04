"""Executable examples and the terminal states they promise readers."""

from __future__ import annotations

import re
from typing import cast

import pytest
from examples.hello_world import HelloWorld
from examples.remote_device_ssh import SshDevice
from examples.ssh_tunneling import TunneledDevice
from examples.test_composition import GroupedDevice, SequencedDevice
from tests.loopback_sshd import LoopbackSshd

from litmus import TestFailure
from litmus.lifecycle import Outcome, State

# PLAN §5.6 / docs/contracts.md Evidence Contract — the legacy provenance shape.
# Python words (`use_shell`) replace TS `useShell`; the line grammar is the same.
_SSH_CONNECT_SHAPE = re.compile(
    r"^ssh connect: [^@\s]+@[^:\s]+:\d+ "
    r"auth=(?:none|password|key|key-file\([^)]+\)|agent\([^)]+\)) "
    r"via=.+$"
)
_EXEC_ROUTING_SHAPE = re.compile(
    r"^exec routing: (?:local|via .+?)"
    r"(?: \((?:declared via use_shell(?:\(inherit=False\))?|inherited from parent)\))?$"
)


def _provenance_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(("ssh connect:", "exec routing:"))
    ]


def _assert_legacy_shape(lines: list[str], *, secrets: tuple[str, ...]) -> None:
    """Every provenance line matches the documented grammar; credentials never appear."""
    assert lines, "expected ssh connect: / exec routing: lines"
    for line in lines:
        if line.startswith("ssh connect:"):
            assert _SSH_CONNECT_SHAPE.fullmatch(line), line
        else:
            assert _EXEC_ROUTING_SHAPE.fullmatch(line), line
        for secret in secrets:
            assert secret not in line


class TestHelloWorldExample:
    """Pin the minimum authored suite to an explicit Pass verdict."""

    async def test_hello_world_passes(self) -> None:
        outcome = await HelloWorld(name="hello world").run()

        assert outcome.state is State.PASS


class TestCompositionExample:
    """Pin the legacy composition lesson to modern sequence and group semantics."""

    async def test_sequence_stops_at_first_dependent_failure(self) -> None:
        outcome = await SequencedDevice(
            name="startup checks",
            service_active=False,
            firmware_version="2.4.1",
            application_pid=412,
        ).run()
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
        ).run()
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
        ).run()
        group = await GroupedDevice(
            name="sensor checks",
            temperature_c=42,
            voltage_v=3.3,
            fan_rpm=1800,
        ).run()

        assert sequence.state is State.PASS
        assert group.state is State.PASS


class TestRemoteDeviceSshExample:
    """Class-owned `use_shell` routes exec, and the artifact names that hop."""

    async def test_loopback_run_emits_declared_then_inherited_routing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        peer = await LoopbackSshd.start(username="root", password="ssh-secret")
        port = peer.port
        try:
            with caplog.at_level("INFO", logger="litmus"):
                outcome = await SshDevice(
                    name="ssh device",
                    host="127.0.0.1",
                    port=port,
                    username="root",
                    password="ssh-secret",
                    skip_host_verification=True,
                ).run()
        finally:
            await peer.aclose()

        assert outcome.state is State.PASS
        lines = _provenance_lines(caplog)
        _assert_legacy_shape(lines, secrets=("ssh-secret",))
        assert lines == [
            f"ssh connect: root@127.0.0.1:{port} auth=password via=direct",
            "exec routing: via 127.0.0.1 (declared via use_shell)",
            "exec routing: via 127.0.0.1 (inherited from parent)",
            "exec routing: via 127.0.0.1 (inherited from parent)",
            "exec routing: via 127.0.0.1 (inherited from parent)",
        ]


class TestSshTunnelingExample:
    """`get_shell(..., via=jump)` records the chain and leaves `utils.exec` local."""

    async def test_loopback_run_emits_direct_then_tunneled_connect(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        jump_peer = await LoopbackSshd.start(username="ops", password="jump-secret")
        dut_peer = await LoopbackSshd.start(username="root", password="dut-secret")
        jump_port = jump_peer.port
        dut_port = dut_peer.port
        try:
            with caplog.at_level("INFO", logger="litmus"):
                outcome = await TunneledDevice(
                    name="tunneled device",
                    jump_host="127.0.0.1",
                    jump_port=jump_port,
                    jump_username="ops",
                    jump_password="jump-secret",
                    target_host="127.0.0.1",
                    target_port=dut_port,
                    target_username="root",
                    target_password="dut-secret",
                    skip_host_verification=True,
                ).run()
        finally:
            await dut_peer.aclose()
            await jump_peer.aclose()

        assert outcome.state is State.PASS
        lines = _provenance_lines(caplog)
        _assert_legacy_shape(lines, secrets=("jump-secret", "dut-secret"))
        assert lines == [
            f"ssh connect: ops@127.0.0.1:{jump_port} auth=password via=direct",
            f"ssh connect: root@127.0.0.1:{dut_port} auth=password via=127.0.0.1",
            "exec routing: local",
            "exec routing: local",
        ]
