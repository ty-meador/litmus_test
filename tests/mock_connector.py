"""In-memory ``ShellConnector`` for self-tests. Not a pytest test module."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from litmus.remote import RemoteInterface

if TYPE_CHECKING:
    from litmus import TestUtils
    from litmus.routing import ShellOpenRequest


class MockConnector:
    """Open in-memory transports where the framework would open SSH.

    Handed one transport, it answers every host with that transport. Handed a
    mapping, it answers each declared host with its own transport and refuses an
    undeclared host with ``ConnectionError`` — the rack suite leans on that refusal
    to prove no DUT can reach a peer's transport.
    """

    def __init__(self, transports: RemoteInterface | Mapping[str, RemoteInterface]) -> None:
        self._transport: RemoteInterface | None = None
        self._hosts: Mapping[str, RemoteInterface] = {}
        if isinstance(transports, RemoteInterface):
            self._transport = transports
        else:
            self._hosts = transports

    async def open(self, request: ShellOpenRequest) -> RemoteInterface:
        """Connect the transport declared for ``request.host`` and return it."""
        transport = self._transport
        if transport is None:
            transport = self._hosts.get(request.host)
        if transport is None:
            msg = f"no mock transport declared for host {request.host!r}"
            raise ConnectionError(msg)
        await transport.connect()
        return transport


type ConnectorInstaller = Callable[
    [TestUtils, RemoteInterface | Mapping[str, RemoteInterface]], MockConnector
]
