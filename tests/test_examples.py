"""Executable examples and the terminal states and evidence they promise readers."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, cast

import pytest
from examples.hardware_device_lifecycle import PoweredDevice
from examples.hello_world import HelloWorld
from examples.networked_device import DutDevice, Gateway
from examples.pi_hole_server import PiHoleServer
from examples.remote_device_ssh import SshDevice
from examples.run_data_evidence import PowerSupply
from examples.ssh_tunneling import TunneledDevice
from examples.test_composition import ParallelDevice, SequencedDevice
from examples.wait_for_patterns import ConfigureGatedDevice, MainAssertionDevice
from tests.loopback_sshd import LoopbackSshd

from litmus import AfterEachContext, ExecResult, RunResult, TestFailure, TestUtils
from litmus.executor import LocalExec
from litmus.lifecycle import Outcome, State
from litmus.mock import MockTransport
from litmus.remote import RemoteInterface
from litmus.routing import ShellOpenRequest, SshConnector

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


def _artifact_payloads(root: Path) -> dict[str, Any]:
    """Load the point-in-time evidence after the asynchronous run settles."""
    return {
        artifact["name"]: artifact
        for path in root.rglob("*.artifact")
        if isinstance((artifact := json.loads(path.read_text())), dict)
    }


class TestHelloWorldExample:
    """Pin the minimum authored suite to an explicit Pass verdict."""

    async def test_hello_world_passes(self) -> None:
        outcome = await HelloWorld(name="hello world").run()

        assert outcome.state is State.PASS


class TestCompositionExample:
    """Pin the legacy composition lesson to modern sequence and parallel semantics."""

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

    async def test_parallel_collects_all_independent_results(self) -> None:
        outcome = await ParallelDevice(
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
        parallel = await ParallelDevice(
            name="sensor checks",
            temperature_c=42,
            voltage_v=3.3,
            fan_rpm=1800,
        ).run()

        assert sequence.state is State.PASS
        assert parallel.state is State.PASS


def _detach_log_paths(tree: dict[str, Any]) -> dict[str, str | None]:
    """Detach additive evidence fields so the established tree shape stays legible."""
    links: dict[str, str | None] = {}
    pending = [tree]
    while pending:
        node = pending.pop()
        links[node["name"]] = node.pop("log_path")
        node.pop("timeout")
        pending.extend(node["children"])
    return links


def _assert_links_resolve(
    artifact_root: Path, links: dict[str, str | None], owners: set[str]
) -> None:
    """Every owning node links to a real timeline beneath the single run directory."""
    (run_root,) = [entry for entry in artifact_root.iterdir() if entry.is_dir()]
    assert {name for name, link in links.items() if link is not None} == owners
    for name in owners:
        link = links[name]
        assert link is not None
        assert (run_root / link).name == "timeline.jsonl"
        assert (run_root / link).is_file()


class TestWaitForPatternsExample:
    """Pin both polling classifications and the evidence visible to an Inspector."""

    async def test_configure_gate_emits_a_reconstructable_artifact_trail(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger="litmus"):
            result = await ConfigureGatedDevice(
                name="polling device",
                service_states=("inactive", "activating", "active"),
                health_response="ok: service healthy",
                interval=0.001,
                timeout_seconds=0.1,
            ).run(
                env={
                    "LITMUS_ARTIFACT_ROOT": str(tmp_path),
                    "PHYSICAL_LOCATION": "bench-7",
                }
            )

        assert result.state is State.PASS
        # The authored node's record is the ctx view; the reading travels with the
        # leaf's verdict (issue #118).
        assert result.run_data == {
            "measurements": [],
            "service_state": "active",
            "service_poll_attempts": 3,
        }

        payload = json.loads(json.dumps(result.to_json()))
        assert payload["ancestor_names"] == ["polling device"]
        assert payload["bread_crumbs"] == "polling device"
        assert payload["physical_location"] == "bench-7"
        links = _detach_log_paths(payload["summary_tree"])
        _assert_links_resolve(tmp_path, links, owners={"polling device"})
        assert payload["summary_tree"] == {
            "name": "polling device",
            "type": "suite",
            "state": "Pass",
            "failure": None,
            "evidence": None,
            "evidence_declared": None,
            "measurements": [],
            "children": [
                {
                    "name": "service-health",
                    "type": "test",
                    "state": "Pass",
                    "failure": None,
                    "evidence": {"health_response": "ok: service healthy"},
                    "evidence_declared": ["health_response"],
                    "measurements": [],
                    "children": [],
                }
            ],
        }

        artifacts = _artifact_payloads(tmp_path)
        assert set(artifacts) == {
            "pre-configure-1",
            "post-configure-1",
            "pre-test-1",
            "post-test-1",
            "pre-cleanup-1",
            "post-cleanup-1",
            "service-ready",
            "service-health",
        }
        assert artifacts["service-ready"]["data"] == {"attempts": 3, "state": "active"}
        assert artifacts["service-health"]["data"] == {"response": "ok: service healthy"}
        assert artifacts["pre-configure-1"]["data"]["service_state"] == "not-configured"
        assert artifacts["post-cleanup-1"]["data"] == {
            "service_state": "active",
            "service_poll_attempts": 3,
        }

        poll_records = [
            record for record in caplog.records if "service readiness poll" in record.getMessage()
        ]
        assert len(poll_records) == 3
        assert {getattr(record, "litmus_scope", ()) for record in poll_records} == {
            ("polling device",)
        }
        pointer_records = [
            record
            for record in caplog.records
            if record.getMessage().startswith("artifact captured")
        ]
        assert len(pointer_records) == len(artifacts)
        assert ("polling device", "service-health") in {
            getattr(record, "litmus_scope", ()) for record in pointer_records
        }

    async def test_test_method_polling_produces_the_hardware_verdict(self) -> None:
        result = await MainAssertionDevice(
            name="stress device",
            stress_states=(False, False, True),
            interval=0.001,
            timeout_seconds=0.1,
        ).run()

        assert result.state is State.PASS
        leaf = result.summary_tree.children[0]
        assert leaf.evidence == {"stress_complete": True, "stress_poll_attempts": 3}
        assert leaf.evidence_declared == ("stress_complete", "stress_poll_attempts")
        assert result.run_data == {
            "measurements": [],
            "poll_interval_s": 0.001,
            "poll_timeout_s": 0.1,
        }


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

    async def test_missing_host_from_env_fails_in_configure(self, tmp_path: Path) -> None:
        result = await SshDevice(name="ssh device").run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.FAIL
        assert isinstance(result.failure, TestFailure)
        assert "did not reach a testable state" in result.failure.fail_message
        assert "SSH_HOST" in result.failure.fail_message

    async def test_garbage_port_from_env_fails_in_configure(self, tmp_path: Path) -> None:
        result = await SshDevice(name="ssh device").run(
            env={
                "LITMUS_ARTIFACT_ROOT": str(tmp_path),
                "SSH_HOST": "127.0.0.1",
                "SSH_USER": "root",
                "SSH_PORT": "nope",
            }
        )

        assert result.state is State.FAIL
        assert isinstance(result.failure, TestFailure)
        assert "did not reach a testable state" in result.failure.fail_message
        assert "SSH_PORT" in result.failure.fail_message


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


_PIHOLE_VERSIONS = """\
Pi-hole version is v5.18.2 (Latest: v5.18.3)
web version is v5.21.1 (Latest: v5.21.1)
FTL version is v5.25.2 (Latest: v5.25.2)
"""
_PIHOLE_STATUS = """\
  [✓] DNS service is running
  [✓] Pi-hole blocking is enabled
"""
_PIHOLE_SECRET = "pihole-secret"
_TAIL_COMMANDS = (
    "tail -n 50 /var/log/pihole/FTL.log",
    "tail -n 50 /var/log/pihole/pihole.log",
    "tail -n 50 /var/log/pihole/pihole_updateGravity.log",
    "tail -n 50 /var/log/pihole/webserver.log",
)
_PIHOLE_STATUS_LINES = ["  [✓] DNS service is running", "  [✓] Pi-hole blocking is enabled"]


def _ok(stdout: str = "") -> ExecResult:
    return ExecResult(stdout=stdout, stderr="", exit_code=0)


def _script_healthy_pihole(transport: MockTransport) -> None:
    transport.script("pihole -v", _ok(_PIHOLE_VERSIONS))
    transport.script("uptime", _ok(" 10:04:11 up 4 days,  3:12"))
    transport.script("systemctl status pihole-FTL", _ok("     Active: active (running)"))
    transport.script("dmesg --level=emerg,alert,crit", _ok(""))
    transport.script("pihole status", _ok(_PIHOLE_STATUS))
    for command in _TAIL_COMMANDS:
        transport.script(command, _ok(""))


def _patch_ssh_connector(monkeypatch: pytest.MonkeyPatch, transport: MockTransport) -> None:
    """Node views construct a fresh ExecRouting; the seed's connector is not inherited."""

    async def open_mock(_self: SshConnector, request: ShellOpenRequest) -> RemoteInterface:
        del request
        await transport.connect()
        return transport

    monkeypatch.setattr(SshConnector, "open", open_mock)


async def _run_pihole(
    transport: MockTransport,
    monkeypatch: pytest.MonkeyPatch,
    *,
    artifact_root: Path,
) -> RunResult:
    _patch_ssh_connector(monkeypatch, transport)
    env: dict[str, str] = {
        "PHYSICAL_LOCATION": "bench-pihole",
        "LITMUS_ARTIFACT_ROOT": str(artifact_root),
    }
    utils = TestUtils(env=env)
    return await PiHoleServer(
        name="PiHoleServer",
        host="pihole.lab",
        username="root",
        password=_PIHOLE_SECRET,
    ).main(utils)


class TestPiHoleServerExample:
    """Dogfood suite: hop, surveys, versions, and the fail-fast health sequence."""

    async def test_healthy_run_emits_versions_tree_and_survey_trail(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport = MockTransport()
        _script_healthy_pihole(transport)

        with caplog.at_level(logging.INFO, logger="litmus"):
            result = await _run_pihole(transport, monkeypatch, artifact_root=tmp_path)

        assert result.state is State.PASS
        assert transport.connected is False
        assert transport.connect_count == 1
        assert transport.disconnect_count >= 1
        assert result.run_data["versions"] == {
            "pihole": {"current": "v5.18.2", "latest": "v5.18.3"},
            "web": {"current": "v5.21.1", "latest": "v5.21.1"},
            "ftl": {"current": "v5.25.2", "latest": "v5.25.2"},
        }
        payload = json.loads(json.dumps(result.to_json()))
        links = _detach_log_paths(payload["summary_tree"])
        _assert_links_resolve(
            tmp_path,
            links,
            owners={"PiHoleServer", "PiHoleServer-main-sequence", "health-checks"},
        )
        # The sanity claim's round trip is the suite's one measurement. Its value and
        # timestamp come from the clock, so they are checked by shape here and the
        # literal below compares the rest of the tree exactly.
        sanity = payload["summary_tree"]["children"][0]["children"][0]
        [round_trip] = sanity.pop("measurements")
        assert round_trip["name"] == "ssh_round_trip_ms"
        assert round_trip["unit"] == "ms"
        assert round_trip["limits"] == {"low": None, "high": 2000.0}
        assert round_trip["in_limits"] is True
        assert 0.0 <= round_trip["value"] <= 2000.0
        assert round_trip["timestamp"].endswith("Z")
        assert payload["summary_tree"] == {
            "name": "PiHoleServer",
            "type": "suite",
            "state": "Pass",
            "failure": None,
            "evidence": None,
            "evidence_declared": None,
            "measurements": [],
            "children": [
                {
                    "name": "PiHoleServer-main-sequence",
                    "type": "sequence",
                    "state": "Pass",
                    "failure": None,
                    "evidence": None,
                    "evidence_declared": None,
                    "measurements": [],
                    "children": [
                        {
                            "name": "sanity-check",
                            "type": "test",
                            "state": "Pass",
                            "failure": None,
                            "evidence": {"uptime": "10:04:11 up 4 days,  3:12"},
                            "evidence_declared": ["uptime"],
                            "children": [],
                        },
                        {
                            "name": "pihole-running",
                            "type": "test",
                            "state": "Pass",
                            "failure": None,
                            "evidence": {"ftl_state": "Active: active (running)"},
                            "evidence_declared": ["ftl_state"],
                            "measurements": [],
                            "children": [],
                        },
                        {
                            "name": "health-checks",
                            "type": "parallel",
                            "state": "Pass",
                            "failure": None,
                            "evidence": None,
                            "evidence_declared": None,
                            "measurements": [],
                            "children": [
                                {
                                    "name": "dmesg-clean",
                                    "type": "test",
                                    "state": "Pass",
                                    "failure": None,
                                    "evidence": {"kernel_critical_lines": []},
                                    "evidence_declared": ["kernel_critical_lines"],
                                    "measurements": [],
                                    "children": [],
                                },
                                {
                                    "name": "status-checks",
                                    "type": "test",
                                    "state": "Pass",
                                    "failure": None,
                                    "evidence": {"status_lines": _PIHOLE_STATUS_LINES},
                                    "evidence_declared": ["status_lines"],
                                    "measurements": [],
                                    "children": [],
                                },
                            ],
                        },
                    ],
                }
            ],
        }
        artifacts = _artifact_payloads(tmp_path)
        # The survey is scheduled from post-configure onward: no pre-configure tail
        # ran on the test host, and no pre-configure artifact claims to be pi-hole
        # evidence (#116).
        assert "pre-configure-1" not in artifacts
        assert set(artifacts) >= {
            "post-configure-1",
            "pre-test-1",
            "post-test-1",
            "pre-cleanup-1",
            "post-cleanup-1",
        }
        assert artifacts["post-configure-1"]["data"] == {
            "ftl": "(empty)",
            "pihole": "(empty)",
            "gravity": "(empty)",
            "web-server": "(empty)",
        }
        # The hop served the post-cleanup survey: five boundaries after configure,
        # four tails each, and the transport closed only after the last one (#115).
        assert transport.commands.count(_TAIL_COMMANDS[0]) == 5
        assert set(transport.commands[-len(_TAIL_COMMANDS) :]) == set(_TAIL_COMMANDS)
        for secret_line in caplog.records:
            assert _PIHOLE_SECRET not in secret_line.getMessage()
        hop_commands = set(transport.commands)
        assert {
            "pihole -v",
            "uptime",
            "systemctl status pihole-FTL",
            "dmesg --level=emerg,alert,crit",
            "pihole status",
            *_TAIL_COMMANDS,
        }.issubset(hop_commands)

    async def test_inactive_ftl_fails_the_sequence_before_health_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport = MockTransport()
        _script_healthy_pihole(transport)
        transport.script(
            "systemctl status pihole-FTL",
            _ok("     Active: inactive (dead)"),
        )

        result = await _run_pihole(transport, monkeypatch, artifact_root=tmp_path)

        assert result.state is State.FAIL
        assert isinstance(result.failure, TestFailure)
        assert result.failure.fail_message == "Pi-Hole FTL service not active"
        sequence = result.summary_tree.children[0]
        sanity, ftl, health = sequence.children
        assert sanity.state is State.PASS
        assert ftl.state is State.FAIL
        assert health.state is None
        assert "dmesg --level=emerg,alert,crit" not in transport.commands
        assert "pihole status" not in transport.commands

    async def test_critical_dmesg_is_a_hardware_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport = MockTransport()
        _script_healthy_pihole(transport)
        transport.script(
            "dmesg --level=emerg,alert,crit",
            _ok(" miller-raid: disk 2 unrecoverable"),
        )

        result = await _run_pihole(transport, monkeypatch, artifact_root=tmp_path)

        assert result.state is State.FAIL
        assert isinstance(result.failure, TestFailure)
        assert result.failure.fail_message == "Critical kernel errors detected"
        health = result.summary_tree.children[0].children[2]
        dmesg, status = health.children
        assert dmesg.state is State.FAIL
        assert status.state is State.PASS
        assert result.failure.evidence == {
            "kernel_critical_lines": [" miller-raid: disk 2 unrecoverable"]
        }

    async def test_unreadable_dmesg_is_not_a_clean_kernel_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unprivileged login gets empty stdout from dmesg, exactly like a clean log."""
        transport = MockTransport()
        _script_healthy_pihole(transport)
        transport.script(
            "dmesg --level=emerg,alert,crit",
            ExecResult(
                stdout="",
                stderr="dmesg: read kernel buffer failed: Operation not permitted",
                exit_code=1,
            ),
        )

        result = await _run_pihole(transport, monkeypatch, artifact_root=tmp_path)

        assert result.state is State.FAIL
        assert isinstance(result.failure, TestFailure)
        assert result.failure.fail_message == "dmesg could not read the kernel log"
        assert (
            result.failure.received
            == "exit 1: dmesg: read kernel buffer failed: Operation not permitted"
        )
        assert result.failure.evidence is None

    async def test_status_lines_without_a_checkmark_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport = MockTransport()
        _script_healthy_pihole(transport)
        transport.script("pihole status", _ok("  [✗] DNS service is NOT running\n"))

        result = await _run_pihole(transport, monkeypatch, artifact_root=tmp_path)

        assert result.state is State.FAIL
        assert isinstance(result.failure, TestFailure)
        assert result.failure.fail_message == "One or more status checks failed"
        assert result.failure.received == ["  [✗] DNS service is NOT running"]
        assert result.failure.evidence == {"status_lines": ["  [✗] DNS service is NOT running"]}

    async def test_refused_connect_is_fail_not_a_contract_banner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transport = MockTransport()
        transport.fail_next_connect(ConnectionError("peer unreachable"))

        result = await _run_pihole(transport, monkeypatch, artifact_root=tmp_path)

        assert result.state is State.FAIL
        assert isinstance(result.failure, TestFailure)
        assert "SSH connect failed" in result.failure.fail_message
        assert "peer unreachable" in result.failure.fail_message
        assert transport.commands == ()


_ARP_TABLE = """\
IP address       HW type     Flags       HW address            Mask     Device
10.0.0.50        0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0
"""
_PING_OK = (
    "PING 10.0.0.50 (10.0.0.50) 56(84) bytes of data.\n"
    "--- 10.0.0.50 ping statistics ---\n"
    "1 packets transmitted, 1 received, 0% packet loss, time 0ms\n"
)
_FIRMWARE = "2.4.1\n"


def _script_arp_dut(transport: MockTransport) -> None:
    transport.script("true", _ok())
    transport.script("cat /proc/net/arp", _ok(_ARP_TABLE))
    transport.script("ping -c 1 10.0.0.50", _ok(_PING_OK))
    transport.script("curl -sf http://10.0.0.50/api/version", _ok(_FIRMWARE))


async def _scripted_local_exec(self: LocalExec, command: str) -> ExecResult:
    del self
    scripts = {
        "cat /proc/net/arp": _ok(_ARP_TABLE),
        "ping -c 1 10.0.0.50": _ok(_PING_OK),
        "curl -sf http://10.0.0.50/api/version": _ok(_FIRMWARE),
    }
    return scripts.get(command, ExecResult(stdout="", stderr=f"unscripted: {command}", exit_code=1))


class TestNetworkedDeviceExample:
    """ArpResolver in configure, locally and behind a `Gateway` that owns the hop."""

    async def test_local_resolve_ping_and_firmware(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(LocalExec, "run", _scripted_local_exec)
        result = await DutDevice(name="networked device", mac="aa:bb:cc:dd:ee:ff").run(
            env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)}
        )

        assert result.state is State.PASS
        assert result.run_data["ip"] == "10.0.0.50"
        assert "arp" not in result.run_data
        assert result.summary_tree.children[0].children[0].name == "reachability"
        assert result.summary_tree.children[0].children[1].name == "firmware-version"

    async def test_mac_from_env_resolves_locally(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(LocalExec, "run", _scripted_local_exec)
        result = await DutDevice(name="networked device").run(
            env={"LITMUS_ARTIFACT_ROOT": str(tmp_path), "DUT_MAC": "aa:bb:cc:dd:ee:ff"}
        )

        assert result.state is State.PASS
        assert result.run_data["ip"] == "10.0.0.50"

    async def test_missing_mac_from_env_fails_in_configure(self, tmp_path: Path) -> None:
        result = await DutDevice(name="networked device").run(
            env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)}
        )

        assert result.state is State.FAIL
        assert isinstance(result.failure, TestFailure)
        assert "did not reach a testable state" in result.failure.fail_message
        assert "DUT_MAC" in result.failure.fail_message

    async def test_half_set_gateway_env_fails_naming_the_missing_var(self, tmp_path: Path) -> None:
        result = await Gateway(name="gateway").run(
            env={
                "LITMUS_ARTIFACT_ROOT": str(tmp_path),
                "DUT_MAC": "aa:bb:cc:dd:ee:ff",
                "GATEWAY_HOST": "10.0.0.1",
            }
        )

        assert result.state is State.FAIL
        assert isinstance(result.failure, TestFailure)
        assert "did not reach a testable state" in result.failure.fail_message
        assert "GATEWAY_USER" in result.failure.fail_message

    async def test_garbage_gateway_port_from_env_fails_in_configure(self, tmp_path: Path) -> None:
        result = await Gateway(name="gateway").run(
            env={
                "LITMUS_ARTIFACT_ROOT": str(tmp_path),
                "DUT_MAC": "aa:bb:cc:dd:ee:ff",
                "GATEWAY_HOST": "10.0.0.1",
                "GATEWAY_USER": "ops",
                "GATEWAY_PORT": "nope",
            }
        )

        assert result.state is State.FAIL
        assert isinstance(result.failure, TestFailure)
        assert "did not reach a testable state" in result.failure.fail_message
        assert "GATEWAY_PORT" in result.failure.fail_message

    async def test_missing_mac_in_configure_is_fail_not_a_bug(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def empty_arp(self: LocalExec, command: str) -> ExecResult:
            del self
            if command == "cat /proc/net/arp":
                return _ok(
                    "IP address       HW type     Flags       HW address"
                    "            Mask     Device\n"
                )
            return ExecResult(stdout="", stderr=f"unscripted: {command}", exit_code=1)

        monkeypatch.setattr(LocalExec, "run", empty_arp)
        result = await DutDevice(name="missing dut", mac="aa:bb:cc:dd:ee:ff").run(
            env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)}
        )

        assert result.state is State.FAIL
        assert isinstance(result.failure, TestFailure)
        assert "did not appear on the network" in result.failure.fail_message
        assert result.failure.is_bug is False

    async def test_the_gateway_composes_the_dut_and_arp_resolves_through_the_hop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport = MockTransport()
        _script_arp_dut(transport)
        _patch_ssh_connector(monkeypatch, transport)
        result = await Gateway(
            name="gateway",
            host="10.0.0.1",
            username="ops",
            password="gateway-secret",
            behind=DutDevice(name="networked device", mac="aa:bb:cc:dd:ee:ff"),
        ).run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.PASS
        assert transport.commands[0] == "true"
        assert "cat /proc/net/arp" in transport.commands
        assert "ping -c 1 10.0.0.50" in transport.commands
        assert "curl -sf http://10.0.0.50/api/version" in transport.commands
        assert transport.connect_count == 1
        assert transport.connected is False
        dut = result.summary_tree.children[0].children[1]
        assert dut.name == "networked device"
        assert dut.state is State.PASS

    async def test_gateway_from_env_runs_the_same_dut_through_the_hop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport = MockTransport()
        _script_arp_dut(transport)
        _patch_ssh_connector(monkeypatch, transport)
        result = await Gateway(name="gateway").run(
            env={
                "LITMUS_ARTIFACT_ROOT": str(tmp_path),
                "DUT_MAC": "aa:bb:cc:dd:ee:ff",
                "GATEWAY_HOST": "10.0.0.1",
                "GATEWAY_USER": "ops",
            }
        )

        assert result.state is State.PASS
        assert "cat /proc/net/arp" in transport.commands
        assert "ping -c 1 10.0.0.50" in transport.commands
        assert transport.connected is False

    async def test_an_unreachable_gateway_fails_and_the_dut_is_never_asked(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport = MockTransport()
        transport.fail_next_connect(ConnectionError("peer unreachable"))
        _patch_ssh_connector(monkeypatch, transport)
        result = await Gateway(
            name="gateway",
            host="10.0.0.1",
            username="ops",
            behind=DutDevice(name="networked device", mac="aa:bb:cc:dd:ee:ff"),
        ).run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.FAIL
        assert isinstance(result.failure, TestFailure)
        assert "SSH connect failed" in result.failure.fail_message
        assert transport.commands == ()
        assert result.summary_tree.children == ()


class TestHardwareDeviceLifecycleExample:
    """Survey and audit around rail, enumeration, and cleanup."""

    async def test_rail_up_enumerates_and_cleanup_drops_the_rail(self, tmp_path: Path) -> None:
        result = await PoweredDevice(
            name="powered device",
            rail_states=("off", "ramping", "on"),
            temperatures=(41.0, 42.5),
            enumerated=True,
            interval=0.001,
            timeout_seconds=0.1,
            audit_interval=0.001,
            claim_seconds=0.02,
        ).run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.PASS
        # Handles are not JSON, so the ctx view carries only the limit; the claim's
        # reading rides on its verdict and the rail state on the survey artifacts.
        assert result.run_data == {"measurements": [], "limit_c": 85.0}
        leaf = result.summary_tree.children[0]
        assert leaf.evidence == {"enumerated": True}
        assert leaf.evidence_declared == ("enumerated",)
        artifacts = _artifact_payloads(tmp_path)
        assert artifacts["pre-configure-1"]["data"]["rail"] == "unknown"
        assert artifacts["post-configure-1"]["data"]["rail"] == "on"
        assert artifacts["post-test-1"]["data"]["rail"] == "on"
        assert artifacts["post-cleanup-1"]["data"]["rail"] == "off"

    async def test_over_temperature_audit_fails_the_suite(self, tmp_path: Path) -> None:
        result = await PoweredDevice(
            name="hot device",
            rail_states=("on",),
            temperatures=(96.0,),
            enumerated=True,
            limit_c=85.0,
            interval=0.001,
            timeout_seconds=0.1,
            audit_interval=0.001,
            claim_seconds=0.03,
        ).run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert result.state is State.FAIL
        assert isinstance(result.failure, TestFailure)
        assert "temperature over limit" in result.failure.fail_message


class TestRunDataEvidenceExample:
    """Limits come from configure; each reading travels with its leaf's verdict."""

    async def test_in_spec_run_carries_each_reading_on_its_leaf(self) -> None:
        leaves: dict[str, RunResult] = {}

        async def after_each(hook: AfterEachContext) -> None:
            leaves[hook.name] = hook.result

        suite = PowerSupply(name="power supply", output_voltage_v=12.1, temperature_c=48.0)
        result = await suite.run(after_each=after_each, allow_unpaired_hooks=True)

        assert result.state is State.PASS
        limits = {"voltage_low_v": 11.5, "voltage_high_v": 12.5, "temp_limit_c": 70.0}
        assert result.run_data == {"measurements": [], **limits}
        assert suite.run_data == {"measurements": [], **limits}
        # The leaf's record is the ctx view plus its reading: the limit sits beside it.
        assert leaves["output-voltage"].run_data == {
            "measurements": [],
            **limits,
            "output_voltage_v": 12.1,
        }
        assert leaves["temperature"].run_data == {
            "measurements": [],
            **limits,
            "temperature_c": 48.0,
        }
        voltage, temperature = result.summary_tree.children[0].children
        assert voltage.evidence == {"output_voltage_v": 12.1}
        assert voltage.evidence_declared == ("output_voltage_v",)
        assert temperature.evidence == {"temperature_c": 48.0}
        assert temperature.evidence_declared == ("temperature_c",)

    async def test_configure_provides_limits_and_seeds_no_reading(self) -> None:
        suite = PowerSupply(name="power supply", output_voltage_v=12.1, temperature_c=48.0)

        provided = await suite.configure(TestUtils())

        assert set(provided) == {"voltage_low_v", "voltage_high_v", "temp_limit_c"}

    async def test_out_of_spec_voltage_fails_and_carries_the_reading(self) -> None:
        result = await PowerSupply(
            name="power supply",
            output_voltage_v=10.0,
            temperature_c=48.0,
        ).run()

        assert result.state is State.FAIL
        voltage = result.summary_tree.children[0].children[0]
        assert voltage.state is State.FAIL
        assert isinstance(voltage.failure, TestFailure)
        assert voltage.failure.evidence == {"output_voltage_v": 10.0}
        assert voltage.evidence == {"output_voltage_v": 10.0}
        assert "output_voltage_v" not in result.run_data
