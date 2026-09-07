"""Topology-aware exec routing.

`use_shell` answers "where does `utils.exec` go". `get_shell` answers "what other
endpoints does this test touch" and never writes the router. Children inherit a
route through `RouterRunParams`, never through ctx.

Suite authors do not import this module. `TestUtils` owns one `ExecRouting` per node;
self-tests import the policy machinery directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, final

from litmus.executor import DEFAULT_EXEC_OUTPUT_BUDGET, ExecResult
from litmus.remote import RemoteInterface
from litmus.ssh import SshInterface

type ExecFn = Callable[[str], Awaitable[ExecResult]]
type TransportOpened = Callable[[RemoteInterface], None]
type RouterProvenance = Literal["local", "inherited", "declared", "isolated"]

LOGGER = logging.getLogger("litmus")

_ROUTING_REASONS: dict[RouterProvenance, str] = {
    "declared": "declared via use_shell",
    "inherited": "inherited from parent",
    "isolated": "declared via use_shell(inherit=False)",
}
"""Why the active route is where it is, in the words of the call that put it there.
A node that never declared one has no reason to give, so `"local"` is deliberately
absent and renders bare."""


@final
class Shell:
    """A scoped exec handle.

    `ssh` is unset when the handle wraps a local route. Passing a local Shell as
    `via` is a direct connection from the test host — standalone behavior.
    """

    __slots__ = ("_exec", "_ssh")

    def __init__(self, exec_fn: ExecFn, ssh: RemoteInterface | None) -> None:
        """Wrap an already-resolved exec function and optional hop."""
        self._exec = exec_fn
        self._ssh = ssh

    async def exec(self, command: str) -> ExecResult:
        """Run `command` on this handle's route. Never raises for the exit status.

        A closed or aborted transport settles as `exit_code == -1`, same as
        `TestUtils.exec`. `RemoteInterface.exec` still raises `ConnectionError`.
        """
        try:
            return await self._exec(command)
        except ConnectionError as error:
            return ExecResult.settled("", str(error), None)

    @property
    def ssh(self) -> RemoteInterface | None:
        """The hop this handle tunnels through, or `None` when the route is local."""
        return self._ssh


@final
@dataclass(frozen=True, slots=True)
class ShellOpenRequest:
    """What a connector needs to open a connection-form shell."""

    host: str
    username: str
    port: int = 22
    password: str | None = None
    private_key: str | bytes | None = None
    private_key_path: str | Path | None = None
    passphrase: str | None = None
    agent: str | None = None
    skip_host_verification: bool = False
    via: RemoteInterface | None = None


class ShellConnector(Protocol):
    """Opens a remote for connection-form `use_shell` / `get_shell`."""

    async def open(self, request: ShellOpenRequest) -> RemoteInterface:
        """Connect and return the hop. The caller owns disconnect."""
        ...


@final
class SshConnector:
    """Default connector: asyncssh `SshInterface`, connected before return.

    Every hop `_resolve_tunnel` can supply is already live — an inherited
    router's hop was brought up by the parent's `use_shell`, and an explicit
    `via=jump` Shell wraps a connected remote. The opened interface therefore
    *borrows* the hop through the transport's `tunnel=` channel; it never
    claims ownership via `via=`, and teardown of the hop stays with the router.

    `output_budget` is the same named ceiling `LocalExec` enforces, so a run's
    local and remote exec share one bound.
    """

    def __init__(self, *, output_budget: int = DEFAULT_EXEC_OUTPUT_BUDGET) -> None:
        self._output_budget = output_budget

    async def open(self, request: ShellOpenRequest) -> RemoteInterface:
        """Reject a non-SSH or dead via; open an `SshInterface` borrowing the hop."""
        via = request.via
        if via is not None and not isinstance(via, SshInterface):
            msg = "SshConnector via must be an SshInterface; pass an SshInterface hop"
            raise TypeError(msg)
        if via is not None and not via.connected:
            msg = (
                "routed via hop must be live at open time; "
                "bring the hop up before passing it as via"
            )
            raise ConnectionError(msg)
        ssh = SshInterface(
            host=request.host,
            username=request.username,
            port=request.port,
            password=request.password,
            private_key=request.private_key,
            private_key_path=request.private_key_path,
            passphrase=request.passphrase,
            agent=request.agent,
            skip_host_verification=request.skip_host_verification,
            tunnel=via,
            output_budget=self._output_budget,
        )
        await ssh.connect()
        return ssh


@final
class ExecRouter:
    """The mapping from this node to where `exec` runs.

    Every router is either local or attached to one remote. Topology is declared
    by `use_shell`; there is no ctx-scan path.
    """

    __slots__ = ("_exec", "_label", "_ssh")

    def __init__(self, label: str, ssh: RemoteInterface | None, exec_fn: ExecFn) -> None:
        """Create a resolved route. Callers use `local` / `for_ssh`."""
        self._label = label
        self._ssh = ssh
        self._exec = exec_fn

    @property
    def label(self) -> str:
        """Human-readable hop: `"local"` or `"via <host>"`."""
        return self._label

    @property
    def ssh(self) -> RemoteInterface | None:
        """The remote this router sends commands through, or `None` when local."""
        return self._ssh

    async def exec(self, command: str) -> ExecResult:
        """Run `command` on this route. Never raises for the command's exit status."""
        return await self._exec(command)

    @classmethod
    def local(cls, local_exec: ExecFn) -> ExecRouter:
        """Commands run on the test host. Default before any topology is declared."""
        return cls("local", None, local_exec)

    @classmethod
    def for_ssh(cls, ssh: RemoteInterface) -> ExecRouter:
        """Commands run through `ssh`. Label names the hop this route attaches to."""
        return cls(f"via {ssh.endpoint}", ssh, ssh.exec)


@final
class RouterRunParams:
    """The inherit slice of a child's run params.

    Underscore-router in the TypeScript original. Children receive a route this
    way so ctx never becomes infrastructure.
    """

    __slots__ = ("_router",)

    def __init__(self, router: ExecRouter | None = None) -> None:
        """Attach an inherited router, or omit to leave the child local."""
        self._router = router

    @property
    def router(self) -> ExecRouter | None:
        """The route a child should start with. `None` means local exec."""
        return self._router


def require_connection_pair(host: str | None, username: str | None) -> None:
    """Refuse a half-declared connection before any slot or router logic runs.

    Shared by ``ExecRouting`` and ``TestUtils`` so a malformed call is a ``TypeError``
    naming the missing argument wherever it is made, ahead of the configure-only guard.
    """
    if host is not None and username is None:
        msg = "use_shell connection form requires username; pass username= together with host="
        raise TypeError(msg)
    if host is None and username is not None:
        msg = "use_shell username requires host; pass host= together with username="
        raise TypeError(msg)


@final
class ExecRouting:
    """Per-node exec policy. The only writer of the router is `use_shell`.

    `get_shell` may open extra endpoints and may *read* `inherit` to pick a
    tunnel. It never assigns `_router` or `_propagate`.
    """

    __slots__ = (
        "_connector",
        "_local_exec",
        "_logger",
        "_on_open",
        "_opened",
        "_propagate",
        "_provenance",
        "_router",
    )

    def __init__(
        self,
        local_exec: ExecFn,
        *,
        inherited: ExecRouter | None = None,
        connector: ShellConnector | None = None,
        logger: logging.Logger | logging.LoggerAdapter[logging.Logger] | None = None,
        on_open: TransportOpened | None = None,
        output_budget: int = DEFAULT_EXEC_OUTPUT_BUDGET,
    ) -> None:
        """Start local, or already inherited from a parent's `RouterRunParams`.

        Args:
            local_exec: Where `exec` lands before any topology is declared.
            inherited: A parent's route, handed down through `RouterRunParams`.
            connector: Opens connection-form shells. Defaults to real SSH.
            logger: Where connect provenance is recorded. A node's scoped adapter
                keeps the line in that node's evidence trail.
            on_open: Run-level observer for every successfully opened transport.
            output_budget: Ceiling applied to SSH sessions this node opens. The
                local executor is constructed with the same number by `TestUtils`.
        """
        self._local_exec = local_exec
        self._logger = logger if logger is not None else LOGGER
        self._on_open = on_open
        self._router = inherited
        self._propagate = True
        if inherited is not None:
            self._provenance: RouterProvenance = "inherited"
        else:
            self._provenance = "local"
        if connector is not None:
            self._connector: ShellConnector = connector
        else:
            self._connector = SshConnector(output_budget=output_budget)
        self._opened: list[RemoteInterface] = []

    @property
    def exec(self) -> ExecFn:
        """Where `utils.exec` currently goes on this node."""
        router = self._router
        if router is None:
            return self._local_exec
        return router.exec

    @property
    def router(self) -> ExecRouter | None:
        """The active route, or `None` when this node has never declared one."""
        return self._router

    @property
    def provenance(self) -> RouterProvenance:
        """Why the active route is where it is. For the later provenance log line."""
        return self._provenance

    @property
    def opened(self) -> tuple[RemoteInterface, ...]:
        """Remotes this node opened. Inherited hops are not in this tuple."""
        return tuple(self._opened)

    def child_run_params(self) -> RouterRunParams:
        """What a composed child receives. `propagate=False` yields a local child."""
        if not self._propagate:
            return RouterRunParams()
        return RouterRunParams(self._router)

    def apply_run_params(self, params: RouterRunParams) -> None:
        """Install an inherited router. A missing router is not a wipe."""
        router = params.router
        if router is None:
            return
        self._router = router
        self._provenance = "inherited"

    async def disconnect_opened(self) -> None:
        """Close remotes this node opened. Inherited hops are not in that set.

        A borrowed tunnel dies here; the hop it borrowed stays with its owner.
        """
        opened = list(reversed(self._opened))
        self._opened.clear()
        for remote in opened:
            with contextlib.suppress(Exception):
                await remote.disconnect()

    async def cancel_opened(self) -> None:
        """Hard-close only remotes this node opened, never its inherited hop."""
        opened = list(reversed(self._opened))
        self._opened.clear()
        await asyncio.gather(*(self._cancel_opened_remote(remote) for remote in opened))

    async def _cancel_opened_remote(self, remote: RemoteInterface) -> None:
        with contextlib.suppress(Exception):
            await remote.cancel_in_flight()

    async def use_shell(
        self,
        *,
        host: str | None = None,
        username: str | None = None,
        port: int = 22,
        password: str | None = None,
        private_key: str | bytes | None = None,
        private_key_path: str | Path | None = None,
        passphrase: str | None = None,
        agent: str | None = None,
        skip_host_verification: bool = False,
        via: Shell | None = None,
        inherit: bool = True,
        propagate: bool = True,
    ) -> Shell:
        """Declare this node's exec policy and optionally open a primary shell.

        Four call shapes:
          - `use_shell(host=..., username=...)` — open SSH and bind exec to it.
          - `use_shell(host=..., username=..., via=jump)` — explicit tunnel wins.
          - `use_shell(inherit=False)` — force local exec, no new shell.
          - `use_shell()` — return the active Shell (inherited or local).

        `propagate=False` stops this node's router from flowing to children.
        """
        require_connection_pair(host, username)
        self._propagate = propagate
        if host is None:
            if not inherit:
                self._router = ExecRouter.local(self._local_exec)
                self._provenance = "isolated"
            return self._active_shell()
        if username is None:
            msg = "use_shell connection form requires username; pass username= together with host="
            raise TypeError(msg)
        shell = await self.get_shell(
            host=host,
            username=username,
            port=port,
            password=password,
            private_key=private_key,
            private_key_path=private_key_path,
            passphrase=passphrase,
            agent=agent,
            skip_host_verification=skip_host_verification,
            via=via,
            inherit=inherit,
            propagate=propagate,
        )
        if shell.ssh is None:
            msg = "use_shell connection form did not yield a remote interface"
            raise RuntimeError(msg)
        self._router = ExecRouter.for_ssh(shell.ssh)
        self._provenance = "declared"
        return shell

    async def get_shell(
        self,
        *,
        host: str,
        username: str,
        port: int = 22,
        password: str | None = None,
        private_key: str | bytes | None = None,
        private_key_path: str | Path | None = None,
        passphrase: str | None = None,
        agent: str | None = None,
        skip_host_verification: bool = False,
        via: Shell | None = None,
        inherit: bool = True,
        propagate: bool = True,
    ) -> Shell:
        """Open an extra endpoint. Does not write the router or propagate flag.

        `inherit` picks the auto-tunnel default when `via` is omitted.
        `propagate` is a documented no-op: this method does not touch it.
        """
        del propagate
        tunnel = self._resolve_tunnel(via=via, inherit=inherit)
        request = ShellOpenRequest(
            host=host,
            username=username,
            port=port,
            password=password,
            private_key=private_key,
            private_key_path=private_key_path,
            passphrase=passphrase,
            agent=agent,
            skip_host_verification=skip_host_verification,
            via=tunnel,
        )
        remote = await self._connector.open(request)
        self._opened.append(remote)
        if self._on_open is not None:
            self._on_open(remote)
        # Logged on the way up, not on the way in: what is recorded is a connection
        # that exists, with the credential path the transport actually selected.
        for line in remote.provenance_lines:
            self._logger.info("%s", line)
        return Shell(remote.exec, remote)

    def routing_line(self) -> str:
        """Where this node's `exec` lands, and why it lands there.

        The via chain is rendered in full rather than summarized to its last hop: the
        FT reconstructs the path from the test host to the DUT out of this line alone.
        """
        router = self._router
        where = "local"
        if router is not None and router.ssh is not None:
            where = f"via {router.ssh.route_label}"
        reason = _ROUTING_REASONS.get(self._provenance)
        if reason is None:
            return where
        return f"{where} ({reason})"

    def _active_shell(self) -> Shell:
        router = self._router
        if router is None:
            return Shell(self._local_exec, None)
        return Shell(router.exec, router.ssh)

    def _resolve_tunnel(self, *, via: Shell | None, inherit: bool) -> RemoteInterface | None:
        if via is not None:
            return via.ssh
        if not inherit:
            return None
        router = self._router
        if router is None:
            return None
        return router.ssh
