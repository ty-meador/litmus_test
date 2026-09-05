"""MAC-to-IP resolution with `ArpResolver`, locally and through a via hop.

The DUT code is identical in both placements: `get_arp_resolver` in
`configure`, then ping. An `ArpResolutionFailure` is caught and re-raised as
`TestFailure` so "the device did not appear on the network" is a verdict, not a
bug. Hardware versus infrastructure stays the triager's call.

The via hop is a `Gateway` whose `use_shell` the framework owns. Pass it as
`via=` at construction; the DUT never mentions SSH.

A standalone run needs `DUT_MAC` on the local ARP table. Add `GATEWAY_HOST` and
`GATEWAY_USER` to resolve through a hop instead.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, cast

from litmus import (
    PASS,
    ArpResolutionFailure,
    ArpResolver,
    LitmusTest,
    TestFailure,
    TestUtils,
    Verdict,
)


class Gateway(LitmusTest):
    """Class-owned SSH hop that a DUT can borrow as `via`."""

    def __init__(
        self,
        *,
        name: str,
        host: str,
        username: str,
        password: str | None = None,
        port: int = 22,
        skip_host_verification: bool = False,
    ) -> None:
        """Capture the hop this gateway will own for the rest of the run.

        Args:
            name: Node name used by logs, artifacts, and failures.
            host: SSH peer hostname or address.
            username: Login name on that peer.
            password: Password auth, when used.
            port: SSH port. Defaults to 22.
            skip_host_verification: Lab-only `known_hosts=None`. Default verifies.
        """
        super().__init__(name=name)
        self._host = host
        self._username = username
        self._password = password
        self._port = port
        self._skip_host_verification = skip_host_verification

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Open the hop. A refused dial is Fail, not a contract banner."""
        try:
            await utils.use_shell(
                host=self._host,
                username=self._username,
                port=self._port,
                password=self._password,
                skip_host_verification=self._skip_host_verification,
            )
        except Exception as error:
            raise TestFailure(f"SSH connect failed: {error}") from error
        utils.logger.info("gateway hop open: %s", self._host)
        return {}

    def build_suite(self) -> LitmusTest:
        """Confirm the hop executes; unused when this node is only a via."""
        return self.case("gateway-up", self._gateway_up)

    async def _gateway_up(self, utils: TestUtils) -> Verdict:
        result = await utils.exec("true")
        if result.exit_code == 0:
            return PASS
        raise TestFailure("gateway hop is not executing", 0, result.exit_code)


class DutDevice(LitmusTest):
    """Resolve a MAC in `configure` and claim reachability plus firmware.

    The same class runs locally or behind a `Gateway` passed as `via`. `configure`
    does not mention SSH either way.
    """

    def __init__(
        self,
        *,
        name: str,
        mac: str,
        firmware_version: str = "2.4.1",
        via: LitmusTest | None = None,
    ) -> None:
        """Capture the MAC this device is found by.

        Args:
            name: Node name used by logs, artifacts, and failures.
            mac: Device MAC in any usual form (colons, dashes, or bare hex).
            firmware_version: Substring the firmware claim requires in stdout.
            via: Optional gateway. The framework brings it up and routes exec.
        """
        super().__init__(name=name, via=via)
        self._mac = mac
        self._firmware_version = firmware_version

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Resolve the MAC and return the IP plus the resolver into ctx."""
        arp = utils.get_arp_resolver(mac=self._mac)
        try:
            resolved = await arp.resolve()
        except ArpResolutionFailure as error:
            raise TestFailure(
                f"{error.mac} did not appear on the network",
                error.mac,
                error.host,
            ) from error
        return {**resolved, "arp": arp}

    def build_suite(self) -> LitmusTest:
        """Reachability first: firmware is meaningless if the device is gone."""
        return self.sequence(
            self.name,
            [
                self.case("reachability", self._reachability),
                self.case("firmware-version", self._firmware_version_check),
            ],
        )

    async def _reachability(self, utils: TestUtils) -> Verdict:
        arp = cast(ArpResolver, utils.ctx["arp"])
        return await arp.ping(utils)

    async def _firmware_version_check(self, utils: TestUtils) -> Verdict:
        ip = str(utils.ctx["ip"])
        result = await utils.exec(f"curl -sf http://{ip}/api/version")
        version = result.stdout.strip()
        if self._firmware_version in version:
            utils.logger.info("firmware version: %s", version)
            return PASS
        raise TestFailure(
            "firmware version mismatch",
            self._firmware_version,
            version if version else "(empty)",
        )


class NetworkedDeviceExample:
    """Run the local or via-routed ARP suite as a standalone program."""

    @staticmethod
    async def run() -> None:
        """Execute through the framework-owned lifecycle.

        Requires ``DUT_MAC``. Optional ``GATEWAY_HOST`` and ``GATEWAY_USER``
        (with optional ``GATEWAY_PASSWORD`` / ``GATEWAY_PORT``) resolve through
        a hop instead of the test host's ARP table.
        """
        mac = os.environ.get("DUT_MAC")
        if mac is None:
            raise LookupError("DUT_MAC must be set to run this example")
        gateway_host = os.environ.get("GATEWAY_HOST")
        gateway_user = os.environ.get("GATEWAY_USER")
        via: LitmusTest | None = None
        if gateway_host is not None or gateway_user is not None:
            if gateway_host is None or gateway_user is None:
                raise LookupError("GATEWAY_HOST and GATEWAY_USER must both be set")
            via = Gateway(
                name="gateway",
                host=gateway_host,
                username=gateway_user,
                password=os.environ.get("GATEWAY_PASSWORD"),
                port=int(os.environ.get("GATEWAY_PORT", "22")),
            )
        await DutDevice(name="networked device", mac=mac, via=via).run()


if __name__ == "__main__":
    asyncio.run(NetworkedDeviceExample.run())
