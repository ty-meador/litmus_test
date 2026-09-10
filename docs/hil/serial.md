# Serial/UART console transport

**Status: SIGNED — Ty, 2026-09-10** ("all approved", recorded on #91). Implementation
lands with the MR that carries this status change.

Ty ruled **both** placements on 2026-09-05: `SerialInterface` as a local peer of
`SshInterface` (console header on the litmus host), and the same transport reached
through the existing `via` hop (a console server, or a Pi with a USB-UART, driven
over SSH). One transport, two placements. This note specifies the transport's
contract once — prompt detection, timeouts, line discipline, exit-status recovery,
output bounding, safe-state on abort — and gives `via` composition its own
section, per Ty's scoping note on the issue. Serial is a transport, not a new HIL
contract: it slots in beside `SshInterface` behind `RemoteInterface`, matching
`remote.py`'s own framing ("SSH, and later serial/UART, are peers behind this
class"). Blocked on #90 (closed): the shared transport-contract suite this design
must pass before any implementation MR opens.

`SerialInterface(RemoteInterface)` opens a local device node (`/dev/ttyUSB0`, a
Windows `COM` port) via `pyserial-asyncio`; it never dials a socket. `address` is
`None` — matching `RemoteInterface`'s own note that "a UART on a local device node
has no address" — so `connect()` rejects `host`/`port` with `TypeError`, same as
any unaddressed transport. `endpoint` falls back to the device path.

```python
console = SerialInterface(device="/dev/ttyUSB0", baud=115200)
```

## Serial Prompt Contract

**Framework obligation:** A console is a byte stream, not a request/response
protocol — `exec` recognizes a command's end by the shell's prompt reappearing,
never by an EOF or a status arriving on the wire. `prompt` (a regex, default a
bare POSIX prompt) is matched against the tail of decoded output; `login_prompt` /
`password_prompt`, when configured, are answered during `connect()` before the
transport reads until `prompt` matches — a console already at a shell is
unaffected. `login_timeout` (default 10s, connect-time only) bounds that
resolution; expiry raises `ConnectionError` naming the last bytes seen,
`connect()` raising whatever `_connect` raises, matching `RemoteInterface`.
`prompt_timeout` (default 30s, overridable per `exec`) bounds how long `exec`
waits for the prompt after sending a command — the transport's own safety net, not
a substitute for `fail_after`/`abort_after`: a wedged console is a resource the
transport cannot leave parked forever even with no ancestor budget declared.
`prompt_timeout` and an ancestor's `fail_after`/`abort_after` are independent
clocks racing the same await; whichever fires first decides the state, exactly as
the Time Budget Contract already specifies. An echoed line unchanged across two
read cycles is named in the eventual timeout's message, distinguishing "wedged,
frozen" from "slow, still producing bytes" without the Floor Technician opening
the suite.

**Developer obligation:** Supply `prompt` (and `login_prompt`/`password_prompt`
where the console logs in) at construction for anything but a bare POSIX shell —
guessing a prompt at runtime is how a stuck read gets misread as a fast command.
Set `prompt_timeout` shorter than any `fail_after` wrapping this node's `exec`
calls when a wedge should surface on its own evidence line rather than only as an
ancestor's `TimeoutEvidence`.

**Consequence:** `exec` never raises for a command's own completion. Expiry
settles like a killed command — `ExecResult.settled(partial_output, "prompt
timeout", None)`, `exit_code == -1`, partial output kept as evidence — never an
exception the suite must catch. If the harness's own clock wins the race instead,
the coroutine is cancelled (`CancelledError` propagates, never swallowed) and
`cancel_in_flight` runs the Serial Abort Contract below.

## Serial Line Discipline Contract

**Framework obligation:** The wire is raw bytes with none of an SSH channel's
structure (separate stdout/stderr, a real exit status). `_exec` normalizes before
matching the prompt or handing bytes to `ExecResult.settled`: input is written as
bare `\r` (real-terminal `Enter`, since some bootloader shells ignore `\n` alone),
output CRLF is normalized to `\n`; exactly one leading echoed line matching the
command just sent is stripped before that normalization (strip-if-present — a
console with echo already off is unaffected); ANSI/VT100 escapes are stripped on
the accumulated decoded buffer, so an escape split across two reads still
resolves and a colorized prompt cannot defeat the `prompt` regex; decoding is
UTF-8 with `errors="replace"`, matching `ExecResult.settled`'s existing path for
every transport.

**Developer obligation:** Read `ExecResult.stdout` with `\n` line endings and no
local echo, the same as any other transport's result — do not special-case
serial output in suite code for line endings or echo.

**Consequence:** A suite calling `result.stdout.splitlines()` does not need to
know the transport was serial. A DUT's binary or non-UTF-8 output is evidence,
not a dead channel.

## Serial Exit-Status Contract

**Framework obligation:** A raw console has no exit-status side-channel — no
equivalent of asyncssh's `process.exit_status`. The transport appends a sentinel
to what it sends and parses it back out of the reply:

```
<command>; echo "__LITMUS_RC__=$?"
```

Once the prompt reappears, the transport scans for `__LITMUS_RC__=<digits>`,
strips that one line from `ExecResult.stdout`, and parses the digits as
`exit_code`. A sentinel that never appears — echo suppression failed, the shell
died mid-command — settles as `exit_code == -1` via `ExecResult.settled(...,
None)`, the same "no real status exists" path every transport shares, with the
raw captured text kept as evidence rather than discarded: the documented `-1`
case ("killed by a signal, or aborted before an exit status existed") with a lost
sentinel standing in for the signal-kill. Stdout and the sentinel-bearing reply
together are bounded by `litmus.executor.BoundedCapture` and
`DEFAULT_EXEC_OUTPUT_BUDGET` unmodified — the same named ceiling `LocalExec` and
`SshInterface` already share. Hitting the budget sets `truncated=True`, stops
accepting bytes, and triggers the Serial Abort Contract below (there is no
process to SIGTERM on the other end of a UART, only a wire to interrupt); the
port stays open afterward, matching "the SSH session stays up" for the
equivalent case.

**Developer obligation:** Read `ExecResult.stderr` as transport-generated
messages only (`"prompt timeout"`, `"transport closed"`) — never a slice of the
DUT's own output. A console has one combined byte stream, not separate
stdout/stderr descriptors; everything the DUT writes settles into `stdout`. A
suite ported from an SSH-backed console that greps `stderr` for DUT error text
needs to read `stdout` instead — the one place the two transports are not
drop-in interchangeable.

**Consequence:** Every `ExecResult` this transport produces is a real, bounded
value a suite can treat exactly like any other transport's result — a missing or
truncated status is evidence, never a raised exception.

## Serial Abort Contract

**Framework obligation:** `cancel_in_flight` is the load-bearing halt ("kill
every in-flight operation as hard as the protocol allows... a later `exec`
rejects"). A serial line has no socket to destroy the way
`SshInterface._abort_client` does, so the sequence is protocol-shaped: send a
`BREAK` condition (a real UART primitive) to interrupt whatever runs in the
foreground on most bootloader and kernel consoles; send `Ctrl-C` (`\x03`) for a
foreground shell command a BREAK alone misses; close the port unconditionally,
whether or not the prompt reappeared after those two — no graceful wait, matching
SSH's "graceful hangup is not used on either path" and the In-Flight Termination
Contract's "closed HARD immediately... not at the next polling boundary." This
contract does not fork that one; it names what "closed hard" means on a
protocol with no socket to abort. The connected flag clears even if the
BREAK/Ctrl-C sends themselves raise, same `try`/`finally` pattern as
`RemoteInterface.cancel_in_flight`.

**Developer obligation:** None beyond the existing `RemoteInterface` contract —
abort is framework-owned and idempotent; suite code never calls
`cancel_in_flight` itself.

**Consequence:** Any `exec` still parked when the sequence runs settles with
`stderr` naming `"transport closed"` — the same cause string
`_name_transport_death` gives SSH, so a Floor Technician's grep for a dead
resource does not need to know which transport produced it. A 30-minute console
session in progress when the run aborts does not keep mutating hardware after the
sequence completes.

## Error-taxonomy mapping

The transport raises nothing shaped like a verdict — only `ConnectionError`
(protocol-level reject, absorbed by `TestUtils.exec`/`Shell.exec` into
`exit_code == -1`) or whatever `_connect` propagates from a failed login.

- **`exec` while not connected** → `ConnectionError`, absorbed to `-1`.
- **Login/prompt never resolves at connect** → `ConnectionError` from `_connect`.
  A suite that wraps `connect()` in `configure()` and re-raises as `TestFailure`
  makes the hardware-vs-infrastructure call itself — the framework never guesses
  (Terminal State Contract: "not knowable from code... the human triager's call").
- **Prompt timeout / sentinel not found** → `ExecResult` with `exit_code == -1`,
  raw text kept as evidence; a suite treating this as a Fail raises `TestFailure`
  from the reading, not the transport.
- **`cancel_in_flight` / abort** → parked `exec` settles `-1`,
  `stderr="transport closed"` — framework-decided, never a suite choice.
- **A suite bug** (bad regex type, wrong argument) → propagates uncaught into
  `LitmusTest`'s existing bug boundary: unmodeled exception → `ContractViolation`,
  "THIS IS A BUG IN `<name>`".

Nothing here adds a new type to `litmus.errors`. The transport speaks the two
vocabularies every `RemoteInterface` already speaks — `ConnectionError` at the
protocol layer, `ExecResult` at the command layer.

## Via composition (placement 2)

The device path names a node on the *remote* box, not the litmus host, so
`SerialInterface` cannot open it as a local file descriptor the way placement 1
does. The resolution reuses the seam `RemoteInterface` already has for this:
`hop` and `endpoint` let a transport name its place in the topology independent
of how its bytes move ("an owned via hop and a borrowed tunnel answer alike").
`SerialInterface` takes a `via: SshInterface | None` constructor argument,
structurally matching `SshInterface`'s own `via` (owned only — a console has no
tunnel to borrow): when given, the "wire" is not a local pyserial handle but the
stdin/stdout of a bridging command run **through the hop's own `exec`** and kept
open as a long-lived remote process. Every contract above — prompt, timeouts,
line discipline, sentinel, abort — operates on that bytestream identically;
placement only changes where the bytes originate. `cancel_in_flight` for
placement 2 sends BREAK/Ctrl-C into the bridging process's stdin, then
hard-cancels that exec through the hop — never the hop's own
`cancel_in_flight()`, which would take down every other consumer of that SSH
connection (Exec Routing Contract: a router "must not disconnect, cancel, or
collapse its hop").

**Routing line.** `route_label`'s existing chain-walk needs no new rendering
code: placement 1 renders `exec routing: via ttyUSB0 (declared via use_shell)` —
no network in the picture; placement 2 renders `exec routing: via bastion ->
console-pi -> ttyUSB0 (declared via use_shell)` — test-host-first, exactly as any
SSH chain renders today.

**Deferred to the implementation MR.** `use_shell`'s four documented shapes all
resolve through `SshConnector`'s `host`/`username` request — there is no existing
shape for handing it an already-built `SerialInterface` (a device path and baud
rate are not a `host`/`username` pair). A fifth call shape, or a narrower
attach-a-built-transport escape hatch, touches the Exec Routing Contract and
needs its own `needs-sign-off` issue, separate from this transport. Until it
lands, a suite constructs `SerialInterface` directly, holds it in ctx, and calls
`.exec()` explicitly — fully usable standalone; only the `use_shell(host=...)`
convenience is deferred.

## Packaging

`pyserial-asyncio` is checked for GPL-2.0 compatibility at implementation time and
lands as an optional extra: `pip install litmus[serial]`. Core `litmus` gains no
new dependency and `import litmus` never imports `litmus.serial`. `SerialInterface`
stays off `litmus.__all__`, same policy as `RemoteInterface`, `SshInterface`, and
`JunitXml` today ("stay private by test-pinned ruling") — self-tests and the
transport-contract suite import the module directly.

## Conformance: the shared transport-contract suite

`tests/test_transport_contract.py` parametrizes every assertion over a
`TRANSPORTS` tuple of fixtures; "serial/UART is the next name on that list" per
the suite's own docstring. A `serial_peer` fixture joins `mock_peer` and
`ssh_peer`, backed by a **pty-based fake console**: `pty.openpty()` gives a real
master/slave pair, and a small in-process script on the slave end plays a
login-and-shell console — echoing input, answering `success`/`fail`/`block`
(parked until cancelled) the same way the other two fixtures do, and emitting the
`__LITMUS_RC__=` sentinel itself so the suite exercises real parsing rather than a
stub. This keeps the suite honest about actual line discipline (real CRLF, a real
echo) while staying fully in-process — no UART hardware required for `make
check`. `wait_in_flight` follows the existing `_wait_ssh_in_flight` shape (a short
sleep) since the parked command is a real subprocess, not a future the fixture
can poll.

Every existing assertion applies unchanged: connect/disconnect idempotence,
exec-never-throws, `cancel_in_flight` idempotence and the `"transport closed"`
cause name, connected-flag ownership. Adding this fixture and naming it in
`TRANSPORTS` is the entire integration cost the suite's docstring promises
("adding a transport is adding a fixture").
