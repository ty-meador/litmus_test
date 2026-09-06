"""Class-owned SSH: `use_shell` in configure routes every descendant's `utils.exec`.

The device is then eligible as a `via` target — the framework owns disconnect.
No `cleanup()` override is needed, and the shell does not belong in ctx.

A constructor argument wins when given (self-tests pass the loopback sshd). A
`None` is resolved from `utils.env`: `SSH_HOST` / `SSH_USER`, optional
`SSH_PASSWORD` / `SSH_PORT` (default 22). A target still missing is a
`TestFailure` from `configure()` naming the variable to set.

Run it: `SSH_HOST=dut SSH_USER=root litmus run examples/remote_device_ssh.py`.
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
        raise TestFailure(f"{name} env var not set")
    return value


def _env_port(utils: TestUtils, name: str) -> int:
    raw = _env_text(utils, name)
    if raw is None:
        return 22
    try:
        port = int(raw)
    except ValueError:
        raise TestFailure(f"{name} env var is not a valid port") from None
    if not 1 <= port <= 65535:
        raise TestFailure(f"{name} env var is not a valid port")
    return port


class SshDevice(LitmusTest):
    """Share one SSH hop across this device's cases by declaring it in configure."""

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
        """Capture the hop this device will own, or leave it for `configure()` to resolve.

        Args:
            name: Node name used by logs, artifacts, and failures.
            host: SSH peer hostname or address. `None` reads `SSH_HOST`.
            username: Login name on that peer. `None` reads `SSH_USER`.
            password: Password auth, when used. `None` falls through to
                `SSH_PASSWORD`, then the transport's remaining credential paths
                (key, agent).
            port: SSH port. `None` reads `SSH_PORT`, then 22.
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
        host = self._host or _require_env(utils, "SSH_HOST")
        username = self._username or _require_env(utils, "SSH_USER")
        password = (
            self._password if self._password is not None else _env_text(utils, "SSH_PASSWORD")
        )
        port = self._port if self._port is not None else _env_port(utils, "SSH_PORT")
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
        utils.logger.info("SSH connected: %s", host)
        return {}

    def build_suite(self) -> LitmusTest:
        """Check uptime and root disk in order; later claims need the hop live."""
        return self.sequence(
            self.name,
            [
                self.test("uptime", self._check_uptime),
                self.test("disk-space", self._check_disk_space),
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
