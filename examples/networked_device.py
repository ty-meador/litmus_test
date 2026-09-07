"""MAC-to-IP resolution with `ArpResolver`, locally and through a via hop.

Concept: a device under test (DUT) is found by its MAC, and the code that finds it
does not care which host runs the lookup.

A bench usually knows a device's MAC (it is printed on the label) and not its IP
(DHCP hands one out). `utils.get_arp_resolver(mac=...)` turns the one into the
other by reading the ARP table of whatever host `utils.exec` currently lands on.
That "whatever host" is the whole lesson:

- Run locally, the resolver reads the test host's table. The DUT has to be on the
  same segment as the developer's machine.
- Run behind a `Gateway` passed as `via=`, the resolver reads the gateway's table,
  because the framework has routed this node's `utils.exec` through the gateway's
  hop. The DUT class is byte-for-byte the same; it never mentions SSH.

Two failure modes are kept apart on purpose. `ArpResolutionFailure` means the
device did not appear on the network; it is caught in `configure` and re-raised as
`TestFailure`, so the run Fails with the MAC and the host that was searched. A bug
in the resolver would surface as an Abort instead. Hardware versus infrastructure
stays the triager's call, and the terminal state tells them which question to ask.

Under `litmus run`, `configure()` reads `DUT_MAC` from `utils.env` and, when
`GATEWAY_HOST` and `GATEWAY_USER` are set together, opens that hop itself so ARP
resolves through it. Half-set gateway variables Fail naming the missing one.

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


class Gateway(LitmusTest):
    """Class-owned SSH hop that a DUT can borrow as `via`.

    A via node is an ordinary `LitmusTest`. What makes it usable as a hop is that
    its `configure` calls `use_shell`, so the framework owns a route through it
    that descendants inherit. Its own claim is a smoke check; when it is only
    serving as someone's via, that claim is never built.
    """

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
        # `utils.env`, never `os.environ`: the run's own view, injectable by the self-tests and
        # published on the record. `require` Fails naming the variable to set; `port` defaults
        # to 22 and rejects a typo; `text` treats blank as unset.
        host = self._host or utils.env.require("GATEWAY_HOST")
        username = self._username or utils.env.require("GATEWAY_USER")
        password = (
            self._password if self._password is not None else utils.env.text("GATEWAY_PASSWORD")
        )
        port = self._port if self._port is not None else utils.env.port("GATEWAY_PORT")
        try:
            await utils.use_shell(
                host=host,
                username=username,
                port=port,
                password=password,
                skip_host_verification=self._skip_host_verification,
            )
        except Exception as error:
            # An unreachable gateway is a bench state, not a suite defect. Wrapping
            # it keeps the exit status at Fail and the transport's reason in the
            # failure record where the FT reads it.
            raise TestFailure(f"SSH connect failed: {error}") from error
        utils.logger.info("gateway hop open: %s", host)
        # Nothing for descendants to read: the route is the product of this
        # configure, and the framework carries it, not ctx.
        return {}

    def build_suite(self) -> LitmusTest:
        """Confirm the hop executes; unused when this node is only a via."""
        return self.test("gateway-up", self._gateway_up)

    async def _gateway_up(self, utils: TestUtils) -> Verdict:
        # `true` is the cheapest command that proves exec reaches the peer. The exit
        # code is the reading here because `true` has no output to judge.
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
        # `via=` goes to the base class: the framework configures the gateway
        # first, routes this node's exec through it, and tears it down after this
        # node's last survey. The DUT has no code for any of that.
        super().__init__(name=name, via=via)
        self._mac = mac
        self._firmware_version = firmware_version
        # Remembered so `configure` knows whether to consult the GATEWAY_* env
        # fallback. A constructor via always wins; the env is for `litmus run`.
        self._via_from_constructor = via

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Resolve the MAC and return the IP plus the resolver into ctx.

        Resolution happens in `configure` rather than in a claim because "the
        device is on the network" is the precondition of every claim, not one of
        them. A device that never appears is a Fail from configure, and no claim
        is asked to ping an address that does not exist.
        """
        mac = self._mac or utils.env.require("DUT_MAC")
        if self._via_from_constructor is None:
            gateway_host = utils.env.text("GATEWAY_HOST")
            gateway_user = utils.env.text("GATEWAY_USER")
            # The pair is all-or-nothing. A host without a user would dial as the
            # process's login and fail with a message about credentials; naming the
            # unset variable instead points the operator at the actual gap.
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
                # Opening the hop here does the same thing a `Gateway` via would:
                # from this line on, `utils.exec` (and therefore the ARP lookup)
                # runs on the gateway.
                await utils.use_shell(
                    host=gateway_host,
                    username=gateway_user,
                    password=utils.env.text("GATEWAY_PASSWORD"),
                    port=utils.env.port("GATEWAY_PORT"),
                )
        arp = utils.get_arp_resolver(mac=mac)
        try:
            resolved = await arp.resolve()
        except ArpResolutionFailure as error:
            # The resolver's failure carries the MAC it searched for and the host
            # whose table it read; both go into expected/received so the FT can see
            # at a glance which segment was searched.
            raise TestFailure(
                f"{error.mac} did not appear on the network",
                error.mac,
                error.host,
            ) from error
        # `resolved` is the JSON-safe reading (`ip`, and whatever else the resolver
        # learned); it lands on `run_data`. The resolver itself goes into ctx as a
        # handle for the reachability claim and is stripped from the record.
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
        # `arp.ping` is a verdict-returning helper: it pings the resolved address
        # through the current route and returns PASS or raises TestFailure itself.
        # The cast tells pyright what `configure` put under the key.
        arp = cast(ArpResolver, utils.ctx["arp"])
        return await arp.ping(utils)

    async def _firmware_version_check(self, utils: TestUtils) -> Verdict:
        ip = str(utils.ctx["ip"])
        # `curl -sf`: silent, and a non-2xx status is an empty stdout rather than an
        # HTML error page that might happen to contain the version string.
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
