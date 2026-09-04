"""SSH tunneling: `get_shell(..., via=jump)` reaches a host only the jump can see.

`get_shell` never writes the exec router — this node's `utils.exec` stays local.
Both handles are opened in `configure` so the framework disconnects them when
the test phase ends. No `cleanup()` override is needed.
"""

import asyncio
import os
from typing import Any

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict


class TunneledDevice(LitmusTest):
    """Reach a DUT through a jump host without rebinding this node's `utils.exec`."""

    def __init__(
        self,
        *,
        name: str,
        jump_host: str,
        jump_username: str,
        target_host: str,
        target_username: str,
        jump_password: str | None = None,
        jump_port: int = 22,
        target_password: str | None = None,
        target_port: int = 22,
        skip_host_verification: bool = False,
    ) -> None:
        """Capture the jump and the DUT this case will tunnel through.

        Args:
            name: Node name used by logs, artifacts, and failures.
            jump_host: Jump-host hostname or address.
            jump_username: Login name on the jump host.
            target_host: DUT hostname or address, as the jump host sees it.
            target_username: Login name on the DUT.
            jump_password: Jump-host password auth, when used.
            jump_port: Jump-host SSH port. Defaults to 22.
            target_password: DUT password auth, when used.
            target_port: DUT SSH port. Defaults to 22.
            skip_host_verification: Lab-only `known_hosts=None`. Default verifies.
        """
        super().__init__(name=name)
        self._jump_host = jump_host
        self._jump_username = jump_username
        self._jump_password = jump_password
        self._jump_port = jump_port
        self._target_host = target_host
        self._target_username = target_username
        self._target_password = target_password
        self._target_port = target_port
        self._skip_host_verification = skip_host_verification

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Open the jump, then the DUT through it. A refused dial is Fail."""
        try:
            jump = await utils.get_shell(
                host=self._jump_host,
                username=self._jump_username,
                port=self._jump_port,
                password=self._jump_password,
                skip_host_verification=self._skip_host_verification,
            )
            target = await utils.get_shell(
                host=self._target_host,
                username=self._target_username,
                port=self._target_port,
                password=self._target_password,
                skip_host_verification=self._skip_host_verification,
                via=jump,
            )
        except Exception as error:
            raise TestFailure(f"SSH tunnel failed: {error}") from error
        return {"target": target}

    def build_suite(self) -> LitmusTest:
        """Build one leaf that reads the DUT kernel through the tunneled handle."""
        return self.case("kernel-version", self._check_kernel)

    async def _check_kernel(self, utils: TestUtils) -> Verdict:
        result = await utils.ctx["target"].exec("uname -r")
        kernel = result.stdout.strip()
        if kernel:
            utils.logger.info("kernel: %s", kernel)
            return PASS
        raise TestFailure("uname -r returned empty", "kernel version string", "(empty)")


class SshTunnelingExample:
    """Run the jump-host suite as a standalone program."""

    @staticmethod
    async def run() -> None:
        """Execute through the framework-owned lifecycle.

        Requires ``JUMP_HOST``, ``JUMP_USER``, ``TARGET_HOST``, and ``TARGET_USER``.
        Optional ``JUMP_PASSWORD``, ``JUMP_PORT``, ``TARGET_PASSWORD``, ``TARGET_PORT``.
        """
        jump_host = os.environ.get("JUMP_HOST")
        jump_username = os.environ.get("JUMP_USER")
        target_host = os.environ.get("TARGET_HOST")
        target_username = os.environ.get("TARGET_USER")
        if (
            jump_host is None
            or jump_username is None
            or target_host is None
            or target_username is None
        ):
            raise LookupError(
                "JUMP_HOST, JUMP_USER, TARGET_HOST, and TARGET_USER must be set to run this example"
            )
        await TunneledDevice(
            name="tunneled device",
            jump_host=jump_host,
            jump_username=jump_username,
            jump_password=os.environ.get("JUMP_PASSWORD"),
            jump_port=int(os.environ.get("JUMP_PORT", "22")),
            target_host=target_host,
            target_username=target_username,
            target_password=os.environ.get("TARGET_PASSWORD"),
            target_port=int(os.environ.get("TARGET_PORT", "22")),
        ).run()


if __name__ == "__main__":
    asyncio.run(SshTunnelingExample.run())
