"""MAC-to-IP resolution with `ArpResolver`, locally and through a via hop.

The DUT code is identical in both placements: `get_arp_resolver` in
`configure`, then ping. An `ArpResolutionFailure` is caught and re-raised as
`TestFailure` so "the device did not appear on the network" is a verdict, not a
bug. Hardware versus infrastructure stays the triager's call.

The via hop is a `Gateway` whose `use_shell` the framework owns. Pass it as
`via=` at construction; the DUT never mentions SSH. Under `litmus run`,
`configure()` reads `DUT_MAC` from `utils.env` and, when `GATEWAY_HOST` and
`GATEWAY_USER` are set together, opens that hop itself so ARP resolves through
it. Half-set gateway variables Fail naming the missing one.

Run it: `DUT_MAC=aa:bb:cc:dd:ee:ff litmus run examples/networked_device.py::DutDevice`.
"""

from __future__ import annotations

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


def _env_text(utils: TestUtils, name: str) -> str | None:
    value = utils.env.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _require_env(utils: TestUtils, name: str) -> str:
    value = _env_text(utils, name)
    if value is None:
        raise TestFailure(f"did not reach a testable state: {name} env var not set; set {name}")
    return value


def _env_port(utils: TestUtils, name: str) -> int:
    raw = _env_text(utils, name)
    if raw is None:
        return 22
    try:
        port = int(raw)
    except ValueError:
        raise TestFailure(
            f"did not reach a testable state: {name} env var is not a valid port; "
            f"set {name} to an integer 1-65535"
        ) from None
    if not 1 <= port <= 65535:
        raise TestFailure(
            f"did not reach a testable state: {name} env var is not a valid port; "
            f"set {name} to an integer 1-65535"
        )
    return port


class Gateway(LitmusTest):
    """Class-owned SSH hop that a DUT can borrow as `via`."""

    def __init__(
        self,
        *,
        name: str,
        host: str | None = None,
        username: str | None = None,
        password: str | None = None,
        port: int | None = None,
        skip_host_verification: bool = False,
    ) -> None:
        """Capture the hop this gateway will own, or leave it for `configure()` to resolve.

        Args:
            name: Node name used by logs, artifacts, and failures.
            host: SSH peer hostname or address. `None` reads `GATEWAY_HOST`.
            username: Login name on that peer. `None` reads `GATEWAY_USER`.
            password: Password auth, when used. `None` reads `GATEWAY_PASSWORD`.
            port: SSH port. `None` reads `GATEWAY_PORT`, then 22.
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
        host = self._host or _require_env(utils, "GATEWAY_HOST")
        username = self._username or _require_env(utils, "GATEWAY_USER")
        password = (
            self._password if self._password is not None else _env_text(utils, "GATEWAY_PASSWORD")
        )
        port = self._port if self._port is not None else _env_port(utils, "GATEWAY_PORT")
        try:
            await utils.use_shell(
                host=host,
                username=username,
                port=port,
                password=password,
                skip_host_verification=self._skip_host_verification,
            )
        except Exception as error:
            raise TestFailure(f"SSH connect failed: {error}") from error
        utils.logger.info("gateway hop open: %s", host)
        return {}

    def build_suite(self) -> LitmusTest:
        """Confirm the hop executes; unused when this node is only a via."""
        return self.test("gateway-up", self._gateway_up)

    async def _gateway_up(self, utils: TestUtils) -> Verdict:
        result = await utils.exec("true")
        if result.exit_code == 0:
            return PASS
        raise TestFailure("gateway hop is not executing", 0, result.exit_code)


class DutDevice(LitmusTest):
    """Resolve a MAC in `configure` and claim reachability plus firmware.

    The same class runs locally or behind a `Gateway` passed as `via`. An
    explicit via still owns the hop; the DUT does not mention SSH. When no via
    was passed, `GATEWAY_HOST` / `GATEWAY_USER` in the run environment open the
    hop here so ARP resolves through it.
    """

    def __init__(
        self,
        *,
        name: str,
        mac: str | None = None,
        firmware_version: str = "2.4.1",
        via: LitmusTest | None = None,
    ) -> None:
        """Capture the MAC this device is found by, or leave it for `configure()`.

        Args:
            name: Node name used by logs, artifacts, and failures.
            mac: Device MAC in any usual form (colons, dashes, or bare hex).
                `None` reads `DUT_MAC`.
            firmware_version: Substring the firmware claim requires in stdout.
            via: Optional gateway. The framework brings it up and routes exec.
        """
        super().__init__(name=name, via=via)
        self._mac = mac
        self._firmware_version = firmware_version
        self._via_from_constructor = via

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Resolve the MAC and return the IP plus the resolver into ctx."""
        mac = self._mac or _require_env(utils, "DUT_MAC")
        if self._via_from_constructor is None:
            gateway_host = _env_text(utils, "GATEWAY_HOST")
            gateway_user = _env_text(utils, "GATEWAY_USER")
            if gateway_host is not None or gateway_user is not None:
                if gateway_host is None:
                    raise TestFailure(
                        "did not reach a testable state: GATEWAY_HOST env var not set; "
                        "set GATEWAY_HOST"
                    )
                if gateway_user is None:
                    raise TestFailure(
                        "did not reach a testable state: GATEWAY_USER env var not set; "
                        "set GATEWAY_USER"
                    )
                await utils.use_shell(
                    host=gateway_host,
                    username=gateway_user,
                    password=_env_text(utils, "GATEWAY_PASSWORD"),
                    port=_env_port(utils, "GATEWAY_PORT"),
                )
        arp = utils.get_arp_resolver(mac=mac)
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
                self.test("reachability", self._reachability),
                self.test("firmware-version", self._firmware_version_check),
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
