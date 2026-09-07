"""Class-owned SSH: `use_shell` in configure routes every descendant's `utils.exec`.

Concept: the suite declares where its commands run, and the framework owns the
connection.

`utils.use_shell(...)` in `configure()` rebinds `utils.exec` for this node and its
descendants to a remote peer. After that line, every claim's `utils.exec("...")`
runs on the device, and no claim contains connection code. Three consequences follow,
and each one is a deliberate design choice rather than a convenience:

- **No `cleanup()` override.** The framework opened the hop, so the framework
  closes it, after the last survey has fired. A suite that closed the hop itself
  would race its own post-cleanup survey.
- **The shell is not in ctx.** Ctx is for values the claims read; the route is
  carried by the framework. Putting the handle in ctx would invite a claim to bypass
  the route, and would put a non-serializable object in the record for nothing.
- **The device is eligible as a `via`.** Because the hop is class-owned, another
  suite can pass this one as `via=` and inherit the route. See
  `networked_device.py` for the other side of that arrangement.

A constructor argument wins when given (the self-tests pass the loopback sshd). A
`None` is resolved from `utils.env`: `SSH_HOST` / `SSH_USER`, optional
`SSH_PASSWORD` / `SSH_PORT` (default 22). A target still missing is a `TestFailure`
from `configure()` naming the variable to set.

Run it: `SSH_HOST=dut SSH_USER=root litmus run examples/remote_device_ssh.py`.
"""

from typing import Any

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict

# --- Environment helpers -----------------------------------------------------------
#
# Repeated in each SSH-backed example so every file is a complete reference on its
# own. They read `utils.env` (injectable by self-tests) rather than `os.environ`, and
# they Fail with "did not reach a testable state" plus the variable name so a bench
# mistake reads as an instruction, not a traceback.


def _env_text(utils: TestUtils, name: str) -> str | None:
    """Return a trimmed env value, treating blank the same as unset."""
    value = utils.env.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _require_env(utils: TestUtils, name: str) -> str:
    """Return the env value or Fail the run with the variable to set."""
    value = _env_text(utils, name)
    if value is None:
        raise TestFailure(f"did not reach a testable state: {name} env var not set; set {name}")
    return value


def _env_port(utils: TestUtils, name: str) -> int:
    """Return the env port, defaulting to 22, and reject values a dial would choke on."""
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


class SshDevice(LitmusTest):
    """Share one SSH hop across this device's claims by declaring it in configure."""

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

        Nothing is dialed in the constructor. A refused connection belongs to the
        run, where it becomes a Fail with a message, not to construction, where it
        would be a traceback before any record exists.

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
        # `is not None`, not `or`: an explicit empty password is a choice, and only
        # an absent one should fall through to the environment.
        password = (
            self._password if self._password is not None else _env_text(utils, "SSH_PASSWORD")
        )
        port = self._port if self._port is not None else _env_port(utils, "SSH_PORT")
        try:
            # This is the line the whole example is about. From here on,
            # `utils.exec` in this node and its descendants runs on `host`.
            await utils.use_shell(
                host=host,
                username=username,
                port=port,
                password=password,
                skip_host_verification=self._skip_host_verification,
            )
        except Exception as error:
            # Catching broadly is deliberate: the transport can raise any of several
            # exception types for "could not connect", and all of them mean the same
            # thing to the run: the device is not testable. The original is chained
            # so the full cause is still in the log.
            raise TestFailure(f"SSH connect failed: {error}") from error
        utils.logger.info("SSH connected: %s", host)
        # Nothing to provide: the claims need the route, and the framework carries
        # the route. An empty mapping is the honest ctx.
        return {}

    def build_suite(self) -> LitmusTest:
        """Check uptime and root disk in order; later claims need the hop live.

        A sequence, because a failed uptime means exec is not reaching the device,
        and a disk reading taken in that state would be noise.
        """
        return self.sequence(
            self.name,
            [
                self.test("uptime", self._check_uptime),
                self.test("disk-space", self._check_disk_space),
            ],
        )

    async def _check_uptime(self, utils: TestUtils) -> Verdict:
        # Plain `utils.exec`, no host, no handle: the route declared in configure
        # decides where this runs.
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
        # Two distinct fail reasons get two distinct messages. "full" and "no output"
        # send the FT to different places, and one combined message would not.
        if "100%" in usage:
            raise TestFailure("root filesystem full", "< 100%", "100%")
        raise TestFailure("df returned empty", "disk usage table", "(empty)")
