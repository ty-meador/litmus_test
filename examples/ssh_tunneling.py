"""SSH tunneling: `get_shell(..., via=jump)` reaches a host only the jump can see.

`get_shell` never writes the exec router — this node's `utils.exec` stays local.
Both handles are opened in `configure` so the framework disconnects them when
the test phase ends. No `cleanup()` override is needed.

A constructor argument wins when given (self-tests pass the loopback sshd). A
`None` is resolved from `utils.env`: `JUMP_HOST` / `JUMP_USER` / `TARGET_HOST` /
`TARGET_USER`, optional `JUMP_PASSWORD` / `JUMP_PORT` / `TARGET_PASSWORD` /
`TARGET_PORT` (ports default 22). A target still missing is a `TestFailure`
from `configure()` naming the variable to set.

A station run puts `JUMP_HOST`, `JUMP_USER`, `TARGET_HOST`, and `TARGET_USER`
in front of `litmus run examples/ssh_tunneling.py`.
"""

from typing import Any

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


class TunneledDevice(LitmusTest):
    """Reach a DUT through a jump host without rebinding this node's `utils.exec`."""

    def __init__(
        self,
        *,
        name: str,
        jump_host: str | None = None,
        jump_username: str | None = None,
        target_host: str | None = None,
        target_username: str | None = None,
        jump_password: str | None = None,
        jump_port: int | None = None,
        target_password: str | None = None,
        target_port: int | None = None,
        skip_host_verification: bool = False,
    ) -> None:
        """Capture the jump and the DUT this leaf will tunnel through.

        Args:
            name: Node name used by logs, artifacts, and failures.
            jump_host: Jump-host hostname or address. `None` reads `JUMP_HOST`.
            jump_username: Login name on the jump host. `None` reads `JUMP_USER`.
            target_host: DUT hostname or address, as the jump host sees it.
                `None` reads `TARGET_HOST`.
            target_username: Login name on the DUT. `None` reads `TARGET_USER`.
            jump_password: Jump-host password auth, when used. `None` reads
                `JUMP_PASSWORD`.
            jump_port: Jump-host SSH port. `None` reads `JUMP_PORT`, then 22.
            target_password: DUT password auth, when used. `None` reads
                `TARGET_PASSWORD`.
            target_port: DUT SSH port. `None` reads `TARGET_PORT`, then 22.
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
        jump_host = self._jump_host or _require_env(utils, "JUMP_HOST")
        jump_username = self._jump_username or _require_env(utils, "JUMP_USER")
        target_host = self._target_host or _require_env(utils, "TARGET_HOST")
        target_username = self._target_username or _require_env(utils, "TARGET_USER")
        jump_password = (
            self._jump_password
            if self._jump_password is not None
            else _env_text(utils, "JUMP_PASSWORD")
        )
        target_password = (
            self._target_password
            if self._target_password is not None
            else _env_text(utils, "TARGET_PASSWORD")
        )
        jump_port = (
            self._jump_port if self._jump_port is not None else _env_port(utils, "JUMP_PORT")
        )
        target_port = (
            self._target_port if self._target_port is not None else _env_port(utils, "TARGET_PORT")
        )
        try:
            jump = await utils.get_shell(
                host=jump_host,
                username=jump_username,
                port=jump_port,
                password=jump_password,
                skip_host_verification=self._skip_host_verification,
            )
            target = await utils.get_shell(
                host=target_host,
                username=target_username,
                port=target_port,
                password=target_password,
                skip_host_verification=self._skip_host_verification,
                via=jump,
            )
        except Exception as error:
            raise TestFailure(f"SSH tunnel failed: {error}") from error
        return {"target": target}

    def build_suite(self) -> LitmusTest:
        """Build one leaf that reads the DUT kernel through the tunneled handle."""
        return self.test("kernel-version", self._check_kernel)

    async def _check_kernel(self, utils: TestUtils) -> Verdict:
        result = await utils.ctx["target"].exec("uname -r")
        kernel = result.stdout.strip()
        if kernel:
            utils.logger.info("kernel: %s", kernel)
            return PASS
        raise TestFailure("uname -r returned empty", "kernel version string", "(empty)")
