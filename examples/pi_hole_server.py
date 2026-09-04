"""Pi-hole dogfood suite: class-owned SSH, surveys, and a fail-fast health sequence.

Port of the legacy `pi-hole-server` suite. Credentials and the hop are identity
captured at construction; `configure` opens the shell and records parsed versions
so the claims stay pure. Phase-boundary surveys tail the service logs through
whatever route is active.

The legacy `_dmesgClean` returned PASS before reading stdout. This port keeps the
claim the method was written to make: empty emerg/alert/crit output is Pass.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, ClassVar

from litmus import PASS, LitmusTest, RunResult, TestFailure, TestUtils, Verdict

_LOG_TAILS: tuple[tuple[str, str], ...] = (
    ("ftl", "/opt/pihole/ftl.log"),
    ("pihole", "/var/log/pihole/pihole.log"),
    ("gravity", "/var/log/pihole/pihole_updateGravity.log"),
)


class PiHoleServer(LitmusTest):
    """Claim a pi-hole is reachable, running, and not screaming in the kernel log."""

    _SERVICE_PATTERNS: ClassVar[dict[str, re.Pattern[str]]] = {
        "pihole": re.compile(
            r"(pi-hole|core).*(v\d+\.\d+\.\d+).*latest.*(v\d+\.\d+\.\d+)",
            re.IGNORECASE,
        ),
        "web": re.compile(
            r"(web).*(v\d+\.\d+\.\d+).*latest.*(v\d+\.\d+\.\d+)",
            re.IGNORECASE,
        ),
        "ftl": re.compile(
            r"(ftl).*(v\d+\.\d+\.\d+).*latest.*(v\d+\.\d+\.\d+)",
            re.IGNORECASE,
        ),
    }

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
        """Capture the hop this pi-hole owns for the rest of the run.

        Args:
            name: Node name used by logs, artifacts, and failures.
            host: SSH peer hostname or address.
            username: Login name on that peer.
            password: Password auth, when used. `None` falls through to the
                transport's remaining credential paths (key, agent).
            port: SSH port. Defaults to 22.
            skip_host_verification: Lab-only `known_hosts=None`. Default verifies.
        """
        super().__init__(name=name)
        self._host = host
        self._username = username
        self._password = password
        self._port = port
        self._skip_host_verification = skip_host_verification
        self.use_survey(self._survey)

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Open the class-owned hop and publish parsed `pihole -v` into ctx."""
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
        result = await utils.exec("pihole -v")
        return {"versions": self._parse_versions(result.stdout)}

    def build_suite(self) -> LitmusTest:
        """Sanity, then FTL, then independent kernel and status claims."""
        return self.sequence(
            f"{self.name}-main-sequence",
            [
                self.case("sanity-check", self._sanity_check),
                self.case("pihole-running", self._pihole_is_running),
                self.group(
                    "health-checks",
                    [
                        self.case("dmesg-clean", self._dmesg_clean),
                        self.case("status-checks", self._status_check),
                    ],
                ),
            ],
        )

    async def _survey(self, utils: TestUtils) -> dict[str, str]:
        """Tail the three pi-hole logs that an FT reads when a claim fails."""
        keys = tuple(key for key, _path in _LOG_TAILS)
        results = await asyncio.gather(
            *(utils.exec(f"tail -n 50 {path}") for _key, path in _LOG_TAILS)
        )
        return {
            key: (result.stdout.strip() or "(empty)")
            for key, result in zip(keys, results, strict=True)
        }

    async def _sanity_check(self, utils: TestUtils) -> Verdict:
        result = await utils.exec("uptime")
        utils.logger.info("Uptime: %s", result.stdout.strip())
        if result.stdout:
            return PASS
        raise TestFailure("uptime returned no output!", "uptime output", result.stdout)

    async def _pihole_is_running(self, utils: TestUtils) -> Verdict:
        result = await utils.exec("systemctl status pihole-FTL")
        if "active (running)" in result.stdout:
            return PASS
        raise TestFailure(
            "Pi-Hole FTL service not active",
            "systemctl output containing 'active (running)'",
            result.stdout,
        )

    async def _dmesg_clean(self, utils: TestUtils) -> Verdict:
        result = await utils.exec("dmesg --level=emerg,alert,crit")
        if not result.stdout.strip():
            return PASS
        raise TestFailure(
            "Critical kernel errors detected",
            "dmesg to have no emerg, alert, or crit entries",
            result.stdout,
        )

    async def _status_check(self, utils: TestUtils) -> Verdict:
        result = await utils.exec("pihole status")
        lines = [line for line in result.stdout.split("\n") if line.strip()]
        fails = [line for line in lines if "✓" not in line]
        if lines and not fails:
            return PASS
        raise TestFailure(
            "One or more status checks failed",
            "All lines containing '✓'",
            fails if fails else "No output returned",
        )

    def _parse_versions(self, stdout: str) -> dict[str, dict[str, str]]:
        found: dict[str, dict[str, str]] = {}
        for line in stdout.split("\n"):
            for service, pattern in self._SERVICE_PATTERNS.items():
                if service in found:
                    continue
                match = pattern.search(line)
                if match is not None:
                    found[service] = {"current": match.group(2), "latest": match.group(3)}
        return found


class PiHoleServerExample:
    """Run the pi-hole dogfood suite as a standalone program."""

    @staticmethod
    async def run() -> RunResult:
        """Execute through the framework-owned lifecycle.

        Requires ``TARGET_ADDRESS`` and ``TARGET_USER``. Optional
        ``TARGET_PASSWORD`` and ``TARGET_PORT`` (default 22).
        """
        host = os.environ.get("TARGET_ADDRESS")
        username = os.environ.get("TARGET_USER")
        if host is None or username is None:
            raise LookupError("TARGET_ADDRESS and TARGET_USER must be set to run this example")
        return await PiHoleServer(
            name="PiHoleServer",
            host=host,
            username=username,
            password=os.environ.get("TARGET_PASSWORD"),
            port=int(os.environ.get("TARGET_PORT", "22")),
        ).run()


if __name__ == "__main__":
    asyncio.run(PiHoleServerExample.run())
