"""Board bring-up acceptance for a Linux guest over SSH (``make bench-suite-guest``).

The shape of a real acceptance suite for a freshly imaged board: one class-owned hop,
a survey that tails the journal at every boundary after the hop exists, an audit that
samples load while the claims run, a ``wait_for`` on systemd settling, and claims that
each take a reading over the hop and judge it against a limit stated in the record.

The device here is the bench's Debian guest under QEMU (``make bench-ssh``), but the
suite does not know that: it reads ``GUEST_HOST`` / ``GUEST_USER`` and optional
``GUEST_PASSWORD`` / ``GUEST_PORT`` from the run environment and would run unchanged
against a Raspberry Pi on the rack.

Run it: ``make bench-suite-guest``, or set the four variables and
``PYTHONPATH=. litmus run bench/suites/guest_acceptance.py``.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from litmus import PASS, LitmusTest, Phase, TestFailure, TestUtils, Verdict, evidence

_SETTLED_STATES = frozenset({"running", "degraded"})


class DebianGuest(LitmusTest):
    """Prove a guest booted to a usable state and is inside its resource limits."""

    def __init__(
        self,
        *,
        name: str = "guest",
        services: Sequence[str] = ("ssh", "systemd-journald", "systemd-logind"),
        load_limit: float = 4.0,
        audit_interval_s: float = 1.0,
        root_used_limit_pct: float = 90.0,
        mem_available_floor_mb: float = 128.0,
        entropy_floor_bits: int = 256,
        clock_skew_limit_s: float = 5.0,
        settle_timeout_s: float = 60.0,
        skip_host_verification: bool = True,
    ) -> None:
        """Capture the services and limits this acceptance judges against.

        Args:
            name: Node name used by logs, artifacts, and failures.
            services: systemd units that must be ``active``; one leaf per unit.
            load_limit: 1-minute load average above which the audit fails the suite.
            audit_interval_s: Seconds between load samples while claims run.
            root_used_limit_pct: Inclusive ceiling on root filesystem use.
            mem_available_floor_mb: Inclusive floor on ``MemAvailable``.
            entropy_floor_bits: Inclusive floor on the kernel entropy pool.
            clock_skew_limit_s: Guest clock may differ from the bench by this much.
            settle_timeout_s: How long ``configure`` waits for systemd to settle.
            skip_host_verification: Lab-only ``known_hosts=None``; the bench guest sits on
                a loopback port no ``known_hosts`` entry covers.
        """
        super().__init__(name=name)
        self._services = tuple(services)
        self._load_limit = load_limit
        self._root_used_limit_pct = root_used_limit_pct
        self._mem_available_floor_mb = mem_available_floor_mb
        self._entropy_floor_bits = entropy_floor_bits
        self._clock_skew_limit_s = clock_skew_limit_s
        self._settle_timeout_s = settle_timeout_s
        self._skip_host_verification = skip_host_verification
        # Hooks are identity: the same boundaries are surveyed and the same feed is
        # audited on every run. The survey starts at post-configure because before the
        # hop exists `utils.exec` is the bench itself.
        self.use_survey(
            self._journal_tail, phases=[phase for phase in Phase if phase >= Phase.POST_CONFIGURE]
        )
        self.use_audit(interval=audit_interval_s, fn=self._load_audit)

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Open the hop, wait for systemd to settle, and record the guest's identity."""
        host = utils.env.require("GUEST_HOST")
        username = utils.env.require("GUEST_USER")
        try:
            await utils.use_shell(
                host=host,
                username=username,
                port=utils.env.port("GUEST_PORT"),
                password=utils.env.text("GUEST_PASSWORD"),
                skip_host_verification=self._skip_host_verification,
            )
        except Exception as error:
            # Several transport types mean the same thing: no guest to test.
            raise TestFailure(f"SSH connect to {host} failed: {error}") from error

        async def systemd_settled() -> bool:
            state = (await utils.exec("systemctl is-system-running")).stdout.strip()
            utils.logger.info("systemd is-system-running: %s", state or "(no answer)")
            return state in _SETTLED_STATES

        # A guest still starting units is not yet the device the claims describe; a
        # guest that never settles is a Fail from configure, which `wait_for` raises.
        await utils.wait_for(
            systemd_settled,
            looking_for="systemd running or degraded",
            interval=2.0,
            timeout_seconds=self._settle_timeout_s,
        )
        kernel = (await utils.exec("uname -r")).stdout.strip()
        os_release = (await utils.exec(". /etc/os-release && echo $PRETTY_NAME")).stdout.strip()
        uptime = (await utils.exec("cut -d' ' -f1 /proc/uptime")).stdout.strip()
        try:
            utils.measure("uptime_at_configure_s", float(uptime), unit="s")
        except ValueError:
            utils.logger.warning("no uptime reading: %r", uptime)
        return {
            "kernel": kernel,
            "os": os_release,
            "load_limit": self._load_limit,
            "root_used_limit_pct": self._root_used_limit_pct,
            "mem_available_floor_mb": self._mem_available_floor_mb,
            "entropy_floor_bits": self._entropy_floor_bits,
            "clock_skew_limit_s": self._clock_skew_limit_s,
        }

    def build_suite(self) -> LitmusTest:
        """Identity, then the units, then the independent resource readings."""
        return self.sequence(
            self.name,
            [
                self.test("kernel-identity", self._kernel_identity, fail_after=15.0),
                self.parallel(
                    "services",
                    [
                        self.test(f"{unit}-active", self._unit_active, unit=unit, fail_after=15.0)
                        for unit in self._services
                    ],
                ),
                self.parallel(
                    "resources",
                    [
                        self.test("root-filesystem", self._root_filesystem, fail_after=15.0),
                        self.test("memory", self._memory, fail_after=15.0),
                        self.test("entropy", self._entropy, fail_after=15.0),
                        self.test("clock", self._clock, fail_after=15.0),
                    ],
                ),
            ],
        )

    @evidence("kernel_running", "machine")
    async def _kernel_identity(self, utils: TestUtils) -> Verdict:
        """The running kernel is the one configure recorded and is a 6.x series kernel."""
        result = await utils.exec("uname -rm")
        parts = result.stdout.split()
        kernel = parts[0] if parts else ""
        machine = parts[1] if len(parts) > 1 else ""
        if result.exit_code == 0 and kernel == utils.ctx["kernel"] and kernel.startswith("6."):
            return PASS({"kernel_running": kernel, "machine": machine})
        raise TestFailure(
            "kernel identity mismatch",
            f"6.x kernel {utils.ctx['kernel']}",
            f"exit {result.exit_code}: {result.stdout.strip() or result.stderr.strip()}",
        )

    @evidence("unit", "state")
    async def _unit_active(self, utils: TestUtils, *, unit: str) -> Verdict:
        """One systemd unit reports ``active``."""
        result = await utils.exec(f"systemctl is-active {unit}")
        state = result.stdout.strip() or result.stderr.strip()
        if state == "active":
            return PASS({"unit": unit, "state": state})
        raise TestFailure(f"unit {unit} is not active", "active", state or "(no answer)")

    @evidence("root_used_pct")
    async def _root_filesystem(self, utils: TestUtils) -> Verdict:
        """Root filesystem use is under the ceiling."""
        result = await utils.exec("df --output=pcent / | tail -n 1")
        text = result.stdout.strip().rstrip("%")
        try:
            used = float(text)
        except ValueError as error:
            raise TestFailure("df gave no percentage", "N%", result.stdout.strip()) from error
        reading = utils.measure(
            "root_used_pct", used, unit="%", limits=(None, utils.ctx["root_used_limit_pct"])
        )
        if reading.in_limits:
            return PASS({"root_used_pct": used})
        raise TestFailure("root filesystem too full", f"<= {reading.limits}", f"{used:g} %")

    @evidence("mem_available_mb")
    async def _memory(self, utils: TestUtils) -> Verdict:
        """``MemAvailable`` is above the floor."""
        result = await utils.exec("awk '/MemAvailable/ {print $2}' /proc/meminfo")
        try:
            available_mb = int(result.stdout.strip()) / 1024.0
        except ValueError as error:
            raise TestFailure("no MemAvailable reading", "kB", result.stdout.strip()) from error
        reading = utils.measure(
            "mem_available_mb",
            round(available_mb, 1),
            unit="MB",
            limits=(utils.ctx["mem_available_floor_mb"], None),
        )
        if reading.in_limits:
            return PASS({"mem_available_mb": reading.value})
        raise TestFailure("memory below floor", f">= {reading.limits}", f"{reading.value} MB")

    @evidence("entropy_bits")
    async def _entropy(self, utils: TestUtils) -> Verdict:
        """The entropy pool is seeded."""
        result = await utils.exec("cat /proc/sys/kernel/random/entropy_avail")
        try:
            bits = int(result.stdout.strip())
        except ValueError as error:
            raise TestFailure("no entropy reading", "bits", result.stdout.strip()) from error
        reading = utils.measure(
            "entropy_bits", bits, unit="bit", limits=(utils.ctx["entropy_floor_bits"], None)
        )
        if reading.in_limits:
            return PASS({"entropy_bits": bits})
        raise TestFailure("entropy pool not seeded", f">= {reading.limits}", f"{bits} bits")

    @evidence("clock_skew_s")
    async def _clock(self, utils: TestUtils) -> Verdict:
        """The guest clock agrees with the bench clock to within the limit."""
        bench_now = time.time()
        result = await utils.exec("date +%s.%N")
        try:
            skew = float(result.stdout.strip()) - bench_now
        except ValueError as error:
            raise TestFailure("no clock reading", "epoch seconds", result.stdout.strip()) from error
        limit: float = utils.ctx["clock_skew_limit_s"]
        reading = utils.measure("clock_skew_s", round(skew, 3), unit="s", limits=(-limit, limit))
        if reading.in_limits:
            return PASS({"clock_skew_s": reading.value})
        raise TestFailure("guest clock skewed", f"within {limit} s", f"{reading.value} s")

    async def _journal_tail(self, utils: TestUtils) -> dict[str, str] | None:
        """The last journal lines at each boundary, for the Floor Technician."""
        if utils.exec_route == "local":
            return None
        result = await utils.exec("sudo -n journalctl -n 20 --no-pager -o short-iso")
        return {"journal": result.stdout.strip() or result.stderr.strip() or "(empty)"}

    async def _load_audit(self, utils: TestUtils) -> dict[str, float]:
        """Sample the 1-minute load; above the limit the suite fails, mid-claim."""
        result = await utils.exec("cut -d' ' -f1 /proc/loadavg")
        try:
            load = float(result.stdout.strip())
        except ValueError:
            utils.logger.warning("no load reading: %r", result.stdout.strip())
            return {}
        limit: float = utils.ctx["load_limit"]
        if load > limit:
            raise TestFailure("guest overloaded during claims", f"<= {limit}", f"{load:g}")
        return {"load_1m": load}
