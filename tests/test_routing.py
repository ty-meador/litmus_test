"""ExecRouter: use_shell writes the route, get_shell does not, children inherit via run params."""

from __future__ import annotations

import pytest

from litmus.executor import ExecResult
from litmus.remote import RemoteInterface
from litmus.routing import (
    ExecRouter,
    ExecRouting,
    RouterRunParams,
    ShellOpenRequest,
    SshConnector,
)


class _Local:
    """Records host-side exec so tests can see whether routing left the machine."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command: str) -> ExecResult:
        self.commands.append(command)
        return ExecResult(stdout=f"local:{command}", stderr="", exit_code=0)


class _NamedRemote(RemoteInterface):
    """A hop with a host name, so ExecRouter labels match production SSH."""

    def __init__(self, host: str) -> None:
        super().__init__()
        self.host = host
        self.commands: list[str] = []

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
        remote = _NamedRemote(request.host)
        await remote.connect()
        self.opened.append(remote)
        return remote


class _Routing:
    """Builds an ExecRouting with a recording local exec and mock connector."""

    def __init__(self, *, inherited: ExecRouter | None = None) -> None:
        self.local = _Local()
        self.connector = _Connector()
        self.routing = ExecRouting(
            self.local.run,
            inherited=inherited,
            connector=self.connector,
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
        with pytest.raises(TypeError, match="SshInterface"):
            await SshConnector().open(request)
