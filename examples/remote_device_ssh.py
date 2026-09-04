"""Class-owned SSH: `use_shell` in configure routes every descendant's `utils.exec`.

The device is then eligible as a `via` target — the framework owns disconnect.
No `cleanup()` override is needed, and the shell does not belong in ctx.
"""

import asyncio
import os
from typing import Any

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict


class SshDevice(LitmusTest):
    """Share one SSH hop across this device's cases by declaring it in configure."""

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
        """Capture the hop this device will own for the rest of the run.

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

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Open the class-owned hop. A refused dial is Fail, not a contract banner."""
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
        utils.logger.info("SSH connected: %s", self._host)
        return {}

    def build_suite(self) -> LitmusTest:
        """Check uptime and root disk in order; later claims need the hop live."""
        return self.sequence(
            self.name,
            [
                self.case("uptime", self._check_uptime),
                self.case("disk-space", self._check_disk_space),
            ],
        )

    async def _check_uptime(self, utils: TestUtils) -> Verdict:
        result = await utils.exec("uptime -p")
        uptime = result.stdout.strip()
        if uptime:
            utils.logger.info("uptime: %s", uptime)
            return PASS
        raise TestFailure("uptime returned empty", "uptime string", "(empty)")

    async def _check_disk_space(self, utils: TestUtils) -> Verdict:
        result = await utils.exec("df -h /")
        usage = result.stdout.strip()
        if usage and "100%" not in usage:
            utils.logger.info("disk usage:\n%s", usage)
            return PASS
        if "100%" in usage:
            raise TestFailure("root filesystem full", "< 100%", "100%")
        raise TestFailure("df returned empty", "disk usage table", "(empty)")


class RemoteDeviceSshExample:
    """Run the class-owned SSH suite as a standalone program."""

    @staticmethod
    async def run() -> None:
        """Execute through the framework-owned lifecycle.

        Requires ``SSH_HOST`` and ``SSH_USER``. Optional ``SSH_PASSWORD`` and
        ``SSH_PORT`` (default 22).
        """
        host = os.environ.get("SSH_HOST")
        username = os.environ.get("SSH_USER")
        if host is None or username is None:
            raise LookupError("SSH_HOST and SSH_USER must be set to run this example")
        await SshDevice(
            name="ssh device",
            host=host,
            username=username,
            password=os.environ.get("SSH_PASSWORD"),
            port=int(os.environ.get("SSH_PORT", "22")),
        ).run()


if __name__ == "__main__":
    asyncio.run(RemoteDeviceSshExample.run())
