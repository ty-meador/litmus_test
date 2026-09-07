"""Pi-hole dogfood suite: class-owned SSH, scheduled surveys, and a fail-fast sequence.

This is the example that runs against a real machine the maintainers own, so it is
where the framework's rules meet a device that does not read the docs. It teaches
four things:

1.  **A class-owned hop.** `configure()` calls `utils.use_shell(...)` once, and from
    then on every `utils.exec` in this node and its descendants lands on the pi-hole.
    No claim mentions SSH. The framework closes the hop when the lifecycle ends, so
    there is no `cleanup()` override and the shell never sits in ctx.

2.  **Surveys that know when there is a device to survey.** The log tails are
    scheduled from `post-configure` onward and guarded by `utils.exec_route`. Without
    both halves, the `pre-configure` tail would run on the developer's laptop and file
    that machine's `/var/log` as pi-hole evidence. See `docs/idioms.md`, "Scheduling a
    survey".

3.  **Evidence on the verdict.** Each claim hands back the reading it judged:
    `PASS({...})` when the reading is in spec, `TestFailure(..., evidence={...})` when
    it is not. `@evidence` names what a pass must carry, so a claim that forgets is a
    contract violation rather than a quiet green.

4.  **Fail-fast composition.** Sanity, then the FTL service, then two independent
    health claims in parallel. If FTL is down, the kernel log and status checks are
    never asked; a `sequence` stops at the first failure and the tree records the
    unreached claims as pending, not passed.

Targets come from constructor arguments when given (the self-tests pass a scripted
hop) and from `utils.env` otherwise: `TARGET_ADDRESS` / `TARGET_USER`, with optional
`TARGET_PASSWORD` / `TARGET_PORT` (default 22). A missing variable is a `TestFailure`
from `configure()` that names the variable to set. That is a Fail, not an Abort: the
device did not reach a testable state, and the exit status should say so.

The legacy `_dmesgClean` returned PASS before reading stdout. This port keeps the claim
the method was written to make (no emerg/alert/crit lines) and adds the check the
legacy suite lacked: an unreadable kernel log is not a clean one.

Run it: `TARGET_ADDRESS=pihole TARGET_USER=root litmus run examples/pi_hole_server.py`.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, ClassVar

from litmus import PASS, LitmusTest, Phase, TestFailure, TestUtils, Verdict, evidence

# --- Environment helpers -----------------------------------------------------------
#
# These read `utils.env`, never `os.environ`. The framework hands each run its own
# environment view so a self-test can inject a target without touching the process,
# and so the run record can say where a value came from.
#
# The failure wording is deliberate. "did not reach a testable state" is the phrase
# the Floor Technician (FT) is taught to read as "the bench is wrong, not the device",
# and naming the variable turns the failure into an instruction.


def _env_text(utils: TestUtils, name: str) -> str | None:
    """Return a trimmed env value, treating blank the same as unset.

    A shell script that exports `TARGET_PASSWORD=""` means "no password", not "the
    empty password". Collapsing blank to `None` keeps that intent.
    """
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
    """Return the env port, defaulting to 22, and reject values a dial would choke on.

    Validating here rather than letting the SSH library fail keeps the message about
    the bench variable, which is what the operator can fix.
    """
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


# --- What the survey reads ---------------------------------------------------------
#
# Pi-hole v6 keeps every log under `/var/log/pihole/`: FTL moved there from
# `/opt/pihole/ftl.log`, and the embedded web server gained its own file. The names on
# the left are the keys in the survey artifact, chosen to match how an FT refers to
# the services ("what did FTL say?"), not the file names.
_LOG_TAILS: tuple[tuple[str, str], ...] = (
    ("ftl", "/var/log/pihole/FTL.log"),
    ("pihole", "/var/log/pihole/pihole.log"),
    ("gravity", "/var/log/pihole/pihole_updateGravity.log"),
    ("web-server", "/var/log/pihole/webserver.log"),
)


class PiHoleServer(LitmusTest):
    """Claim a pi-hole is reachable, running, and not screaming in the kernel log."""

    # `pihole -v` prints one line per component, "Core version is v6.0.4 (Latest:
    # v6.0.4)" on v6 and "Pi-hole version is v5.18.2 (Latest: v5.18.3)" on v5. One
    # pattern per component, case-insensitive, accepts both generations; group 2 is
    # the installed version and group 3 the latest published one.
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

        Nothing is dialed here. The constructor records intent; `configure()` acts on
        it, because a connection failure belongs to the run (a Fail with evidence),
        not to object construction (a traceback with none).

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
        # Surveys are registered in the constructor because they are part of the
        # suite's identity: the same boundaries are observed on every run. The phase
        # list starts at `post-configure` because before `configure()` returns there
        # is no hop, and a tail at `pre-configure` would read the test host. The
        # schedule alone is not enough (see `_survey`); it is the first of two guards.
        self.use_survey(
            self._survey,
            phases=[phase for phase in Phase if phase >= Phase.POST_CONFIGURE],
        )

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Open the class-owned hop and provide the parsed `pihole -v` versions.

        Everything the claims need to know before they run happens here, and ctx is
        read-only once this returns. The versions go into ctx rather than onto a
        claim because they are provenance, not a pass/fail question: an out-of-date
        pi-hole is still a healthy one, but the FT wants to know which build produced
        this record. Ctx lands on the suite's `run_data`, so they are on every run.
        """
        host = self._host or _require_env(utils, "TARGET_ADDRESS")
        username = self._username or _require_env(utils, "TARGET_USER")
        # `is not None` rather than `or`: an explicit empty password from the
        # constructor is a choice; only an absent one falls through to the env.
        password = (
            self._password if self._password is not None else _env_text(utils, "TARGET_PASSWORD")
        )
        port = self._port if self._port is not None else _env_port(utils, "TARGET_PORT")
        try:
            # `use_shell` rebinds `utils.exec` for this node and its descendants. The
            # framework owns the handle from here: it closes it after the last survey,
            # which is why there is no `cleanup()` in this class.
            await utils.use_shell(
                host=host,
                username=username,
                port=port,
                password=password,
                skip_host_verification=self._skip_host_verification,
            )
        except Exception as error:
            # A refused dial is a Fail (the device is not testable), never an Abort
            # (the suite is broken). Wrapping keeps the exit status honest and puts
            # the transport's message where the FT reads failures.
            raise TestFailure(f"SSH connect failed: {error}") from error
        result = await utils.exec("pihole -v")
        return {"versions": self._parse_versions(result.stdout)}

    def build_suite(self) -> LitmusTest:
        """Sanity, then FTL, then independent kernel and status claims.

        The order is the dependency graph. A dead FTL makes `pihole status` and the
        kernel log irrelevant to the question "is this pi-hole serving?", so those
        two sit behind it in a sequence. They do not depend on each other, so they
        run in parallel and both are recorded when both fail.
        """
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

    async def _survey(self, utils: TestUtils) -> dict[str, str] | None:
        """Tail the pi-hole logs an FT reads when a claim fails.

        The phase schedule says when the survey fires; the route check says whether
        anything is on the other end. They differ when `configure()` fails before
        `use_shell` returns: the engine skips the test-phase boundaries but still
        fires `pre-cleanup` and `post-cleanup`, and at those two `utils.exec` is the
        test host. Returning `None` there writes a log line and no artifact, which is
        the truthful record. A survey error never changes the verdict, so this hook
        does not need to defend against a missing log file; it records what `tail`
        said so the absence is itself evidence.
        """
        if utils.exec_route == "local":
            return None
        keys = tuple(key for key, _path in _LOG_TAILS)
        # The tails are independent reads of one host; gathering them keeps the
        # boundary short so the survey does not stretch the phase it observes.
        results = await asyncio.gather(
            *(utils.exec(f"tail -n 50 {path}") for _key, path in _LOG_TAILS)
        )
        return {
            key: (result.stdout.strip() or result.stderr.strip() or "(empty)")
            for key, result in zip(keys, results, strict=True)
        }

    @evidence("uptime")
    async def _sanity_check(self, utils: TestUtils) -> Verdict:
        """Prove the hop executes at all before asking anything of the service.

        The reading is the `uptime` line itself. Carrying it on the verdict means the
        run record shows how long the box had been up when it was judged, which is the
        first thing an FT asks about a flaky pi-hole.
        """
        result = await utils.exec("uptime")
        uptime = result.stdout.strip()
        utils.logger.info("Uptime: %s", uptime)
        if uptime:
            return PASS({"uptime": uptime})
        raise TestFailure("uptime returned no output!", "uptime output", result.stdout)

    @evidence("ftl_state")
    async def _pihole_is_running(self, utils: TestUtils) -> Verdict:
        """Claim the FTL resolver service is active.

        `systemctl status` exits non-zero for an inactive unit, but the exit code is
        not the reading; the `Active:` line is, and it is what the evidence carries.
        A Fail here stops the sequence: the two health claims are meaningless against
        a resolver that is not running.
        """
        result = await utils.exec("systemctl status pihole-FTL")
        state = next(
            (line.strip() for line in result.stdout.splitlines() if "Active:" in line),
            result.stdout.strip(),
        )
        if "active (running)" in result.stdout:
            return PASS({"ftl_state": state})
        raise TestFailure(
            "Pi-Hole FTL service not active",
            "systemctl output containing 'active (running)'",
            result.stdout,
            evidence={"ftl_state": state},
        )

    @evidence("kernel_critical_lines")
    async def _dmesg_clean(self, utils: TestUtils) -> Verdict:
        """Claim the kernel has logged nothing at emerg, alert, or crit.

        This is the claim where "no output" is ambiguous. A clean log prints nothing;
        so does `dmesg` when the login cannot read the kernel buffer, except that it
        also exits non-zero and writes to stderr. The legacy suite passed on both. The
        exit code is checked first so the second case is a Fail that names the cause,
        and the pass path carries the (empty) list so the record shows the log was
        actually read.
        """
        result = await utils.exec("dmesg --level=emerg,alert,crit")
        if result.exit_code != 0:
            raise TestFailure(
                "dmesg could not read the kernel log",
                "exit 0",
                f"exit {result.exit_code}: {result.stderr.strip() or '(no stderr)'}",
            )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            return PASS({"kernel_critical_lines": lines})
        raise TestFailure(
            "Critical kernel errors detected",
            "dmesg to have no emerg, alert, or crit entries",
            result.stdout,
            evidence={"kernel_critical_lines": lines},
        )

    @evidence("status_lines")
    async def _status_check(self, utils: TestUtils) -> Verdict:
        """Claim every line of `pihole status` carries a check mark.

        Pi-hole prints one line per sub-check and marks each with `[✓]` or `[✗]`.
        Requiring the mark on every non-blank line, rather than searching for `✗`,
        means a new sub-check in a future release cannot pass by being unrecognized.
        The lines travel with the verdict either way; on a Fail, `received` narrows
        to the offending ones so the FT does not have to diff.
        """
        result = await utils.exec("pihole status")
        lines = [line for line in result.stdout.split("\n") if line.strip()]
        fails = [line for line in lines if "✓" not in line]
        if lines and not fails:
            return PASS({"status_lines": lines})
        raise TestFailure(
            "One or more status checks failed",
            "All lines containing '✓'",
            fails if fails else "No output returned",
            evidence={"status_lines": lines},
        )

    def _parse_versions(self, stdout: str) -> dict[str, dict[str, str]]:
        """Reduce `pihole -v` to `{component: {current, latest}}`.

        The first matching line per component wins, so a repeated banner cannot
        overwrite a parsed value. Only JSON-safe types go in, because the result is
        bound for ctx and the run record strips anything else.
        """
        found: dict[str, dict[str, str]] = {}
        for line in stdout.split("\n"):
            for service, pattern in self._SERVICE_PATTERNS.items():
                if service in found:
                    continue
                match = pattern.search(line)
                if match is not None:
                    found[service] = {"current": match.group(2), "latest": match.group(3)}
        return found
