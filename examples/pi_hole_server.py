"""Pi-hole dogfood suite: class-owned SSH, surveys, and a fail-fast health sequence.

Port of the legacy `pi-hole-server` suite. A constructor argument wins when given
(self-tests pass a mock hop). A `None` is resolved from `utils.env`:
`TARGET_ADDRESS` / `TARGET_USER`, optional `TARGET_PASSWORD` / `TARGET_PORT`
(default 22). A target still missing is a `TestFailure` from `configure()`
naming the variable to set. `configure` then opens the shell and records parsed
versions so the claims stay pure. Phase-boundary surveys tail the service logs
through whatever route is active.

The legacy `_dmesgClean` returned PASS before reading stdout. This port keeps the
claim the method was written to make: empty emerg/alert/crit output is Pass.

Run it: `TARGET_ADDRESS=pihole TARGET_USER=root litmus run examples/pi_hole_server.py`.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, ClassVar

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict


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
        host: str | None = None,
        username: str | None = None,
        password: str | None = None,
        port: int | None = None,
        skip_host_verification: bool = False,
    ) -> None:
        """Capture the hop this pi-hole owns, or leave it for `configure()` to resolve.

        Args:
            name: Node name used by logs, artifacts, and failures.
            host: SSH peer hostname or address. `None` reads `TARGET_ADDRESS`.
            username: Login name on that peer. `None` reads `TARGET_USER`.
            password: Password auth, when used. `None` falls through to
                `TARGET_PASSWORD`, then the transport's remaining credential
                paths (key, agent).
            port: SSH port. `None` reads `TARGET_PORT`, then 22.
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
        host = self._host or _require_env(utils, "TARGET_ADDRESS")
        username = self._username or _require_env(utils, "TARGET_USER")
        password = (
            self._password if self._password is not None else _env_text(utils, "TARGET_PASSWORD")
        )
        port = self._port if self._port is not None else _env_port(utils, "TARGET_PORT")
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
        result = await utils.exec("pihole -v")
        return {"versions": self._parse_versions(result.stdout)}

    def build_suite(self) -> LitmusTest:
        """Sanity, then FTL, then independent kernel and status claims."""
        return self.sequence(
            f"{self.name}-main-sequence",
            [
                self.test("sanity-check", self._sanity_check),
                self.test("pihole-running", self._pihole_is_running),
                self.parallel(
                    "health-checks",
                    [
                        self.test("dmesg-clean", self._dmesg_clean),
                        self.test("status-checks", self._status_check),
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
