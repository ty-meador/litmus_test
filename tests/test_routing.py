"""ExecRouter: use_shell writes the route, get_shell does not, children inherit via run params.

`use_serial` / `get_serial` are the same two shapes for a console by device path and
are held to the same policy here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, cast

import pytest
from tests.fake_console import open_pty_console

from litmus.executor import ExecResult
from litmus.remote import RemoteInterface
from litmus.routing import (
    ExecRouter,
    ExecRouting,
    RouterRunParams,
    SerialConnector,
    SerialOpenRequest,
    Shell,
    ShellOpenRequest,
    SshConnector,
)
from litmus.ssh import SshInterface
from litmus.utils import ScopedLogger


class _ScopedLogRecord(Protocol):
    litmus_scope: tuple[str, ...]


class _Local:
    """Records host-side exec so tests can see whether routing left the machine."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command: str) -> ExecResult:
        self.commands.append(command)
        return ExecResult(stdout=f"local:{command}", stderr="", exit_code=0)


class _NamedRemote(RemoteInterface):
    """A hop with a host name and an upstream, so labels match production SSH."""

    def __init__(self, host: str, hop: RemoteInterface | None = None) -> None:
        super().__init__()
        self.host = host
        self.commands: list[str] = []
        self._hop = hop

    @property
    def hop(self) -> RemoteInterface | None:
        return self._hop

    async def _connect(self) -> None:
        return

    async def _disconnect(self) -> None:
        return

    async def _exec(self, command: str) -> ExecResult:
        self.commands.append(command)
        return ExecResult(stdout=f"{self.host}:{command}", stderr="", exit_code=0)


class _Connector:
    """Test seam: connection-form shells never need a live sshd."""

    def __init__(self) -> None:
        self.requests: list[ShellOpenRequest] = []
        self.opened: list[_NamedRemote] = []

    async def open(self, request: ShellOpenRequest) -> RemoteInterface:
        self.requests.append(request)
        # Mirrors SshConnector: the resolved hop is threaded through as the tunnel,
        # so the opened remote reports the same chain production would.
        remote = _NamedRemote(request.host, request.via)
        await remote.connect()
        self.opened.append(remote)
        return remote


class _Console(RemoteInterface):
    """A console with a device path and the hop it bridges through, labelled like serial."""

    def __init__(self, device: str, hop: RemoteInterface | None = None) -> None:
        super().__init__()
        self.device = device
        self.commands: list[str] = []
        self._hop = hop

    @property
    def endpoint(self) -> str:
        return Path(self.device).name

    @property
    def hop(self) -> RemoteInterface | None:
        return self._hop

    async def _connect(self) -> None:
        return

    async def _disconnect(self) -> None:
        return

    async def _exec(self, command: str) -> ExecResult:
        self.commands.append(command)
        return ExecResult(stdout=f"{self.endpoint}:{command}", stderr="", exit_code=0)


class _ConsoleConnector:
    """Test seam: console-form routes never need a tty."""

    def __init__(self) -> None:
        self.requests: list[SerialOpenRequest] = []
        self.opened: list[_Console] = []

    async def open(self, request: SerialOpenRequest) -> RemoteInterface:
        self.requests.append(request)
        remote = _Console(request.device, request.via)
        await remote.connect()
        self.opened.append(remote)
        return remote


class _Routing:
    """Builds an ExecRouting with a recording local exec and mock connectors."""

    def __init__(self, *, inherited: ExecRouter | None = None) -> None:
        self.local = _Local()
        self.connector = _Connector()
        self.console_connector = _ConsoleConnector()
        self.routing = ExecRouting(
            self.local.run,
            inherited=inherited,
            connector=self.connector,
            console_connector=self.console_connector,
        )


class TestPublicSurface:
    def test_routing_is_not_on_the_public_surface(self) -> None:
        import litmus

        assert not hasattr(litmus, "ExecRouter")
        assert not hasattr(litmus, "ExecRouting")
        assert "ExecRouter" not in litmus.__all__


class TestExecRouter:
    async def test_local_routes_to_the_host_executor(self) -> None:
        local = _Local()
        router = ExecRouter.local(local.run)
        result = await router.exec("uname")
        assert router.label == "local"
        assert router.ssh is None
        assert result.stdout == "local:uname"
        assert local.commands == ["uname"]

    async def test_for_ssh_routes_through_the_remote(self) -> None:
        local = _Local()
        remote = _NamedRemote("10.0.0.1")
        await remote.connect()
        router = ExecRouter.for_ssh(remote)
        result = await router.exec("uname")
        assert router.label == "via 10.0.0.1"
        assert router.ssh is remote
        assert result.stdout == "10.0.0.1:uname"
        assert local.commands == []
        assert remote.commands == ["uname"]

    async def test_for_ssh_without_a_host_attribute_still_labels_the_hop(self) -> None:
        class _Anonymous(RemoteInterface):
            async def _connect(self) -> None:
                return

            async def _disconnect(self) -> None:
                return

            async def _exec(self, command: str) -> ExecResult:
                return ExecResult(stdout=command, stderr="", exit_code=0)

        remote = _Anonymous()
        await remote.connect()
        router = ExecRouter.for_ssh(remote)
        assert router.label == "via remote"
        assert router.ssh is remote


class TestUseShellShapes:
    async def test_connection_form_binds_exec_to_the_opened_hop(self) -> None:
        harness = _Routing()
        shell = await harness.routing.use_shell(host="10.0.0.1", username="root")
        result = await harness.routing.exec("id")
        assert shell.ssh is harness.connector.opened[0]
        assert result.stdout == "10.0.0.1:id"
        assert harness.local.commands == []
        assert harness.routing.provenance == "declared"

    async def test_explicit_via_wins_over_an_inherited_hop(self) -> None:
        parent = _Routing()
        jump = await parent.routing.use_shell(host="jump", username="root")
        child = _Routing()
        child.routing.apply_run_params(parent.routing.child_run_params())
        await child.routing.use_shell(host="target", username="root", via=jump)
        assert jump.ssh is parent.connector.opened[0]
        assert child.connector.requests[0].via is jump.ssh
        assert child.connector.requests[0].host == "target"

    async def test_policy_only_inherit_false_forces_local_exec(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="10.0.0.1", username="root")
        child = _Routing()
        child.routing.apply_run_params(parent.routing.child_run_params())
        shell = await child.routing.use_shell(inherit=False)
        result = await child.routing.exec("uname")
        assert shell.ssh is None
        assert result.stdout == "local:uname"
        assert child.local.commands == ["uname"]
        assert child.routing.provenance == "isolated"
        assert child.connector.requests == []

    async def test_bare_use_shell_returns_the_active_route_without_opening(self) -> None:
        parent = _Routing()
        declared = await parent.routing.use_shell(host="10.0.0.1", username="root")
        child = _Routing()
        child.routing.apply_run_params(parent.routing.child_run_params())
        active = await child.routing.use_shell()
        assert active.ssh is declared.ssh
        assert child.connector.requests == []
        result = await child.routing.exec("uname")
        assert result.stdout == "10.0.0.1:uname"
        assert child.routing.provenance == "inherited"


class TestGetShellDoesNotWriteRouter:
    async def test_get_shell_opens_an_endpoint_without_rebinding_exec(self) -> None:
        harness = _Routing()
        shell = await harness.routing.get_shell(host="10.0.0.9", username="root")
        routed = await harness.routing.exec("uname")
        extra = await shell.exec("id")
        assert routed.stdout == "local:uname"
        assert extra.stdout == "10.0.0.9:id"
        assert harness.routing.router is None
        assert harness.routing.provenance == "local"
        assert harness.local.commands == ["uname"]

    async def test_get_shell_ignores_propagate(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="10.0.0.1", username="root")
        await parent.routing.get_shell(host="aux", username="root", propagate=False)
        assert parent.routing.child_run_params().router is parent.routing.router

    async def test_get_shell_honors_inherit_for_the_tunnel_only(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="jump", username="root")
        child = _Routing()
        child.routing.apply_run_params(parent.routing.child_run_params())
        inherited_hop = child.routing.router.ssh if child.routing.router is not None else None
        await child.routing.get_shell(host="aux", username="root")
        await child.routing.get_shell(host="direct", username="root", inherit=False)
        assert child.connector.requests[0].via is inherited_hop
        assert child.connector.requests[1].via is None
        assert child.routing.router is parent.routing.router


class TestInheritThroughRunParams:
    async def test_children_inherit_the_router_not_ctx(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="10.0.0.1", username="root")
        decoy: dict[str, object] = {"ssh": _NamedRemote("decoy")}
        child = _Routing()
        child.routing.apply_run_params(parent.routing.child_run_params())
        result = await child.routing.exec("uname")
        assert result.stdout == "10.0.0.1:uname"
        assert child.routing.provenance == "inherited"
        assert "ssh" in decoy
        assert child.local.commands == []

    async def test_propagate_false_resets_children_to_local(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="10.0.0.1", username="root", propagate=False)
        params = parent.routing.child_run_params()
        assert params.router is None
        child = _Routing()
        child.routing.apply_run_params(params)
        result = await child.routing.exec("uname")
        assert result.stdout == "local:uname"
        assert child.routing.provenance == "local"

    async def test_apply_run_params_does_not_read_a_none_router_as_wipe(self) -> None:
        harness = _Routing()
        await harness.routing.use_shell(host="10.0.0.1", username="root")
        harness.routing.apply_run_params(RouterRunParams())
        result = await harness.routing.exec("uname")
        assert result.stdout == "10.0.0.1:uname"

    async def test_inherited_connection_form_tunnels_through_the_parent_hop(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="jump", username="root")
        child = _Routing()
        child.routing.apply_run_params(parent.routing.child_run_params())
        await child.routing.use_shell(host="target", username="root")
        assert child.connector.requests[0].via is parent.connector.opened[0]
        assert child.routing.provenance == "declared"
        result = await child.routing.exec("uname")
        assert result.stdout == "target:uname"

    async def test_inherit_false_on_connection_form_skips_the_parent_hop(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="jump", username="root")
        child = _Routing()
        child.routing.apply_run_params(parent.routing.child_run_params())
        await child.routing.use_shell(host="target", username="root", inherit=False)
        assert child.connector.requests[0].via is None


class TestUseShellValidation:
    async def test_host_without_username_is_rejected(self) -> None:
        harness = _Routing()
        with pytest.raises(TypeError, match="username"):
            await harness.routing.use_shell(host="10.0.0.1")

    async def test_username_without_host_is_rejected(self) -> None:
        harness = _Routing()
        with pytest.raises(TypeError, match="host"):
            await harness.routing.use_shell(username="root")


class TestOpenedInterfaces:
    async def test_connection_forms_record_what_this_node_opened(self) -> None:
        harness = _Routing()
        await harness.routing.get_shell(host="aux", username="root")
        await harness.routing.use_shell(host="primary", username="root")
        assert harness.routing.opened == tuple(harness.connector.opened)
        assert [remote.host for remote in harness.connector.opened] == ["aux", "primary"]

    async def test_inherited_hops_are_not_this_nodes_opens(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="jump", username="root")
        child = _Routing()
        child.routing.apply_run_params(parent.routing.child_run_params())
        await child.routing.use_shell()
        assert child.routing.opened == ()


class TestSshConnector:
    async def test_rejects_a_non_ssh_via(self) -> None:
        hop = _NamedRemote("jump")
        await hop.connect()
        request = ShellOpenRequest(host="10.0.0.2", username="root", via=hop)
        with pytest.raises(TypeError, match=r"SSH hop.*inherit=False"):
            await SshConnector().open(request)

    async def test_rejects_a_dead_via_hop(self) -> None:
        hop = SshInterface(
            host="127.0.0.1",
            username="ops",
            password="jump",
            skip_host_verification=True,
        )
        request = ShellOpenRequest(host="10.0.0.2", username="root", via=hop)
        with pytest.raises(ConnectionError, match="must be live"):
            await SshConnector().open(request)


class TestRoutingProvenance:
    """One line per node: where exec lands, and the call that put it there."""

    def test_a_node_that_never_declared_a_route_reports_the_test_host(self) -> None:
        harness = _Routing()

        assert harness.routing.routing_line() == "local"

    async def test_a_declared_route_names_the_call_that_declared_it(self) -> None:
        harness = _Routing()

        await harness.routing.use_shell(host="10.0.0.1", username="root")

        assert harness.routing.routing_line() == "via 10.0.0.1 (declared via use_shell)"

    async def test_an_inherited_route_says_where_it_came_from(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="10.0.0.1", username="root")
        child = _Routing()

        child.routing.apply_run_params(parent.routing.child_run_params())

        assert child.routing.routing_line() == "via 10.0.0.1 (inherited from parent)"

    async def test_forcing_local_exec_names_the_opt_out(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="10.0.0.1", username="root")
        child = _Routing()
        child.routing.apply_run_params(parent.routing.child_run_params())

        await child.routing.use_shell(inherit=False)

        assert child.routing.routing_line() == "local (declared via use_shell(inherit=False))"

    async def test_the_via_chain_is_rendered_whole_not_summarized(self) -> None:
        harness = _Routing()
        jump = await harness.routing.use_shell(host="bastion", username="ops")

        await harness.routing.use_shell(host="dut", username="root", via=jump)

        assert harness.routing.routing_line() == "via bastion -> dut (declared via use_shell)"


class TestConnectProvenance:
    """Every transport the router opens records its own line as it comes up."""

    async def test_opening_a_shell_records_the_endpoint_and_its_chain(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        harness = _Routing()

        with caplog.at_level("INFO", logger="litmus"):
            jump = await harness.routing.use_shell(host="bastion", username="ops")
            await harness.routing.get_shell(host="dut", username="root", via=jump)

        assert [record.getMessage() for record in caplog.records] == [
            "connect: bastion",
            "connect: bastion -> dut",
        ]

    async def test_the_line_lands_on_the_node_that_opened_the_connection(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        local = _Local()
        connector = _Connector()
        logger = ScopedLogger(logging.getLogger("litmus"), ("rack", "dut"))
        routing = ExecRouting(local.run, connector=connector, logger=logger)

        with caplog.at_level("INFO", logger="litmus"):
            await routing.use_shell(host="10.0.0.1", username="root")

        scoped = [cast("_ScopedLogRecord", record) for record in caplog.records]
        assert [record.litmus_scope for record in scoped] == [("rack", "dut")]


class TestUseSerialShapes:
    """`use_serial` is `use_shell` for a device path: two shapes, same router policy."""

    async def test_declaring_form_binds_exec_to_the_console(self) -> None:
        harness = _Routing()

        shell = await harness.routing.use_serial(device="/dev/ttyUSB0")

        result = await harness.routing.exec("uname")
        assert result.stdout == "ttyUSB0:uname"
        assert harness.local.commands == []
        assert shell.ssh is harness.console_connector.opened[0]
        assert harness.routing.provenance == "declared"
        assert harness.routing.routing_line() == "via ttyUSB0 (declared via use_serial)"

    async def test_the_request_carries_the_line_settings(self) -> None:
        harness = _Routing()

        await harness.routing.use_serial(
            device="/dev/ttyUSB0",
            baud=9600,
            prompt=r"uboot> $",
            login_prompt="login: ",
            password_prompt="Password: ",
            username="root",
            password="hunter2",
            login_timeout=3.0,
            prompt_timeout=4.0,
            bridge_command="console",
            output_budget=4096,
        )

        assert harness.console_connector.requests == [
            SerialOpenRequest(
                device="/dev/ttyUSB0",
                baud=9600,
                prompt=r"uboot> $",
                login_prompt="login: ",
                password_prompt="Password: ",
                username="root",
                password="hunter2",
                login_timeout=3.0,
                prompt_timeout=4.0,
                bridge_command="console",
                output_budget=4096,
                via=None,
            )
        ]

    async def test_bare_use_serial_returns_the_active_route_without_opening(self) -> None:
        harness = _Routing()
        primary = await harness.routing.use_shell(host="dut", username="root")

        shell = await harness.routing.use_serial()

        assert shell.ssh is primary.ssh
        assert harness.console_connector.opened == []
        assert harness.routing.routing_line() == "via dut (declared via use_shell)"

    async def test_bare_use_serial_on_a_local_node_is_the_local_shell(self) -> None:
        harness = _Routing()

        shell = await harness.routing.use_serial()

        assert shell.ssh is None
        assert (await shell.exec("uname")).stdout == "local:uname"

    @pytest.mark.parametrize(
        "kwargs", [{"inherit": False}, {"propagate": False}, {"via": Shell(_Local().run, None)}]
    )
    async def test_policy_only_shapes_belong_to_use_shell(self, kwargs: dict[str, object]) -> None:
        harness = _Routing()

        with pytest.raises(TypeError, match="use_shell"):
            await harness.routing.use_serial(**kwargs)  # type: ignore[arg-type]
        assert harness.routing.provenance == "local"

    async def test_an_inherited_ssh_route_bridges_the_console_through_the_hop(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="console-pi", username="ops")
        child = _Routing()
        child.routing.apply_run_params(parent.routing.child_run_params())

        await child.routing.use_serial(device="/dev/ttyUSB0")

        hop = parent.connector.opened[0]
        assert child.console_connector.requests[0].via is hop
        assert child.routing.routing_line() == "via console-pi -> ttyUSB0 (declared via use_serial)"

    async def test_explicit_via_wins_over_an_inherited_hop(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="inherited", username="ops")
        child = _Routing()
        child.routing.apply_run_params(parent.routing.child_run_params())
        jump = await child.routing.get_shell(host="jump", username="ops")

        await child.routing.use_serial(device="/dev/ttyUSB0", via=jump)

        assert child.console_connector.requests[0].via is jump.ssh

    async def test_inherit_false_opens_the_device_on_the_test_host(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="console-pi", username="ops")
        child = _Routing()
        child.routing.apply_run_params(parent.routing.child_run_params())

        await child.routing.use_serial(device="/dev/ttyUSB0", inherit=False)

        assert child.console_connector.requests[0].via is None
        assert child.routing.routing_line() == "via ttyUSB0 (declared via use_serial)"

    async def test_a_local_shell_as_via_is_a_direct_connection(self) -> None:
        harness = _Routing()
        local = await harness.routing.use_shell()

        await harness.routing.use_serial(device="/dev/ttyUSB0", via=local)

        assert harness.console_connector.requests[0].via is None

    async def test_propagate_false_keeps_the_console_from_children(self) -> None:
        harness = _Routing()

        await harness.routing.use_serial(device="/dev/ttyUSB0", propagate=False)

        assert harness.routing.child_run_params().router is None

    async def test_children_inherit_a_console_route(self) -> None:
        parent = _Routing()
        await parent.routing.use_serial(device="/dev/ttyUSB0")
        child = _Routing()

        child.routing.apply_run_params(parent.routing.child_run_params())

        assert child.routing.routing_line() == "via ttyUSB0 (inherited from parent)"
        assert (await child.routing.exec("uname")).stdout == "ttyUSB0:uname"


class TestGetSerialDoesNotWriteRouter:
    async def test_get_serial_opens_a_console_without_rebinding_exec(self) -> None:
        harness = _Routing()

        aux = await harness.routing.get_serial(device="/dev/ttyUSB1")

        assert (await aux.exec("uname")).stdout == "ttyUSB1:uname"
        assert (await harness.routing.exec("uname")).stdout == "local:uname"
        assert harness.routing.provenance == "local"
        assert harness.routing.routing_line() == "local"

    async def test_get_serial_honors_inherit_for_the_tunnel_only(self) -> None:
        parent = _Routing()
        await parent.routing.use_shell(host="console-pi", username="ops")
        child = _Routing()
        child.routing.apply_run_params(parent.routing.child_run_params())

        await child.routing.get_serial(device="/dev/ttyUSB1")
        await child.routing.get_serial(device="/dev/ttyUSB2", inherit=False, propagate=False)

        requests = child.console_connector.requests
        assert requests[0].via is parent.connector.opened[0]
        assert requests[1].via is None
        assert child.routing.routing_line() == "via console-pi (inherited from parent)"
        assert child.routing.child_run_params().router is not None


class TestOpenedConsoles:
    """A console this node opened is in `opened`, so both sweeps reach the port."""

    async def test_console_forms_record_what_this_node_opened(self) -> None:
        harness = _Routing()
        await harness.routing.get_serial(device="/dev/ttyUSB1")
        await harness.routing.use_serial(device="/dev/ttyUSB0")
        assert harness.routing.opened == tuple(harness.console_connector.opened)

    async def test_disconnect_opened_closes_the_console(self) -> None:
        harness = _Routing()
        shell = await harness.routing.use_serial(device="/dev/ttyUSB0")
        console = shell.ssh
        assert console is not None
        assert console.connected

        await harness.routing.disconnect_opened()

        assert console.connected is False
        assert harness.routing.opened == ()

    async def test_cancel_opened_hard_closes_the_console(self) -> None:
        harness = _Routing()
        shell = await harness.routing.use_serial(device="/dev/ttyUSB0")
        console = shell.ssh
        assert console is not None

        await harness.routing.cancel_opened()

        assert console.connected is False
        assert (await shell.exec("uname")).exit_code == -1

    async def test_the_run_level_observer_sees_the_console(self) -> None:
        seen: list[RemoteInterface] = []
        local = _Local()
        connector = _ConsoleConnector()
        routing = ExecRouting(local.run, console_connector=connector, on_open=seen.append)

        await routing.use_serial(device="/dev/ttyUSB0")

        assert seen == connector.opened


class TestSerialConnector:
    async def test_rejects_a_non_ssh_via_naming_the_call(self) -> None:
        hop = _NamedRemote("jump")
        await hop.connect()
        request = SerialOpenRequest(device="/dev/ttyUSB0", via=hop)
        with pytest.raises(TypeError, match="use_serial"):
            await SerialConnector().open(request)

    async def test_an_inherited_console_route_is_not_a_hop_to_bridge_through(self) -> None:
        parent = _Routing()
        await parent.routing.use_serial(device="/dev/ttyUSB0")
        child = ExecRouting(_Local().run)
        child.apply_run_params(parent.routing.child_run_params())

        with pytest.raises(TypeError, match="use_serial / get_serial via must be an SSH hop"):
            await child.use_serial(device="/dev/ttyUSB1")
        assert child.routing_line() == "via ttyUSB0 (inherited from parent)"

    async def test_the_refusal_names_the_inherited_hop_and_the_escape(self) -> None:
        # Issue #150: the child never passed a via; `_resolve_tunnel` handed it
        # the parent's console. The message must say which hop, and how to open
        # the device without it, the way the SSH connector's refusal does.
        parent = _Routing()
        await parent.routing.use_serial(device="/dev/ttyUSB0")
        child = ExecRouting(_Local().run)
        child.apply_run_params(parent.routing.child_run_params())

        with pytest.raises(TypeError, match=r"got ttyUSB0 \(_Console\).*inherit=False") as raised:
            await child.get_serial(device="/dev/ttyUSB1")
        assert "may have been inherited, not passed" in str(raised.value)

    async def test_rejects_a_dead_via_hop(self) -> None:
        hop = SshInterface(
            host="127.0.0.1",
            username="ops",
            password="jump",
            skip_host_verification=True,
        )
        request = SerialOpenRequest(device="/dev/ttyUSB0", via=hop)
        with pytest.raises(ConnectionError, match="must be live"):
            await SerialConnector().open(request)

    async def test_opens_a_real_console_and_logs_its_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        local = _Local()
        routing = ExecRouting(local.run)
        async with open_pty_console() as (device, _console):
            with caplog.at_level("INFO", logger="litmus"):
                shell = await routing.use_serial(device=device, prompt_timeout=5.0)
            try:
                assert (await routing.exec("uname")).stdout == "uname"
                assert (
                    routing.routing_line() == f"via {Path(device).name} (declared via use_serial)"
                )
                assert [record.getMessage() for record in caplog.records] == [
                    f"serial connect: {device} baud=115200 via=direct"
                ]
            finally:
                await routing.disconnect_opened()
        assert shell.ssh is not None
        assert shell.ssh.connected is False
