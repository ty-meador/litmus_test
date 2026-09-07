# Design note: GPIO interface contract

Status: DRAFT — awaiting Ty's signature on #93

This note proposes the developer-framework contract for digital I/O pins —
direction, level, debounce, safe-state on abort, the error taxonomy for a pin
that will not settle, and timing guarantees. It is written to the diligence of
`docs/contracts.md` and governed by the three rulings in `docs/hil/index.md`.
All names below (`GpioInterface`, `GpioPin`, `PinDirection`, `PinLevel`,
`PinSettleFailure`) are provisional: they name the shape being signed off, not
a promised spelling.

## Why not an `exec` subclass

A GPIO pin does not run commands. "Set this pin high" and "read this pin's
level" are typed operations on a fixed set of physical lines, not strings
handed to a shell. Forcing pin control through `exec` would mean either
shelling out to `sysfs`/`libgpiod` command-line tools per call (slow, racy,
and impossible to debounce cleanly) or smuggling a pin controller inside a
`RemoteInterface` subclass that answers `_exec` with a fake shell dialect —
exactly the parallel-lifecycle-in-disguise ruling 1 forbids. `GpioInterface` is
its own ABC, structurally parallel to `RemoteInterface`
(`src/litmus/remote.py`) but not a subclass of it: it owns `connect`/
`disconnect`/`cancel_in_flight` in the same shape, and adds pin-shaped
operations in place of `exec`.

## The GPIO Interface Contract

**Framework obligation:** `GpioInterface` presents a fixed set of named pins,
each with a declared direction and idle level, and guarantees the following
regardless of backend:

- **Direction and level.** Every pin is declared `PinDirection.OUT` or
  `PinDirection.IN` before first use; direction is fixed for the pin's
  lifetime — a suite that needs the other direction opens a different
  declared pin, it does not flip one live. `set_level(pin, PinLevel.HIGH |
  PinLevel.LOW)` and `read_level(pin) -> PinLevel` are the only level
  operations. Setting an input pin or reading an output-only pin (on hardware
  that cannot read back its own output) is a `TypeError` at the call site, not
  a silent no-op — the same posture `RemoteInterface.exec` takes toward a
  disconnected transport, caught before it can be mistaken for a hardware
  reading.
- **Debounce.** `read_level` on an input pin accepts a `settle: float` (seconds,
  default per-backend but always explicit in the resolved call) naming how
  long the level must hold steady before the read returns. The framework
  samples at a fixed interval no coarser than a tenth of `settle` and returns
  the first level that holds for the full window. A pin that never stops
  chattering within `settle` does not return a guessed value — it raises
  (see error taxonomy below). Debounce is symmetric: it is not a filter for
  "the level I expected," it is a filter for "the level that stopped moving."
- **Safe-state on abort.** Every declared output pin carries a required idle
  level, given at declaration time, not inferred from its first `set_level`
  call. When the run-level abort fires — an integration hook throw, an
  operator SIGINT/SIGTERM, or a time-budget expiry on the node that owns the
  pin — every output pin this interface opened is driven to its declared idle
  level before the interface reports `cancel_in_flight` complete. This is the
  GPIO-specific instance of the In-Flight Termination Contract's "cancel the
  resource, not just the coroutine": for a relay driving mains power or a
  reset line holding a DUT in a known state, leaving the last-written level in
  place across an abort is the failure mode this contract exists to prevent.
  Input pins have no safe-state action; they carry no risk from being read
  after abort, and `cancel_in_flight` stops accepting reads immediately.
- **Timing guarantees.** `set_level` and `read_level` (after debounce settles)
  complete within a bounded, backend-declared latency ceiling — the interface
  states its worst-case pin-toggle latency so a suite author can reason about
  whether a protocol timing requirement (a reset pulse width, a chip-select
  setup time) is achievable at all through this backend before writing the
  test. A call that cannot complete within that ceiling is a timeout, reported
  through the existing Time Budget Contract if the calling node declared one,
  or as a `PinSettleFailure` if not.

**Developer obligation:**

- Declare every pin's direction and, for outputs, its idle level at
  configuration time — in `configure()`, the same place `use_shell` is called.
  A pin whose idle level is not knowable ("depends on what the last test left
  it at") is a design defect in the test, not something the framework can
  paper over.
- Use `settle` deliberately. The default is not "fast enough for every
  signal" — a mechanical relay and a solid-state one debounce on different
  timescales, and the author who knows the hardware states the number.
- Treat `PinSettleFailure` as a `TestFailure` opportunity, not a bug: catch it
  where a chattering pin is itself the hardware finding (a flaky connector), and
  let it propagate as a plain exception (becoming a `ContractViolation`) only
  where debounce failure truly indicates a test-harness bug.

**Consequence:** A relay left energized, a reset line left asserted, or a
power-enable pin left high across an aborted run is a safety and equipment-
damage risk, not merely a test artifact. Declaring idle levels up front and
enforcing them on every abort path — not just a clean `cleanup()` — is what
makes GPIO safe to use on live hardware under this framework.

## Lifecycle and hook ownership: unchanged

`GpioInterface` does not run its own event loop, does not register its own
`before_each`/`after_each`, and does not own a competing halt signal.
Concretely:

- Bring-up happens the same way `use_shell` brings up SSH: a `configure()`
  call obtains a `GpioInterface` handle (provisionally `utils.get_gpio(...)`,
  mirroring `get_shell`), and the framework's existing `ExecRouting`-style
  bookkeeping tracks what this node opened versus what it inherited.
- Teardown order is the framework's, not the interface's: `cancel_in_flight`
  on every opened `GpioInterface` fires as part of the same In-Flight
  Termination Contract sweep that already closes `RemoteInterface` transports
  when `RunAbort` fires, not on a separate GPIO-specific abort path.
- `use_survey`/`use_audit` are the only hooks a suite registers to observe pin
  state; a periodic audit that reads a watchdog pin and raises `TestFailure`
  on an unexpected level is exactly the audit contract's existing shape
  (thermal-runaway-style monitoring), not a new hook kind.
- `before_each`/`after_each` remain the integrator's narrow context
  (`BeforeEachContext`/`AfterEachContext`) and still cannot touch hardware —
  a GPIO pin is exactly the kind of thing those hooks are barred from reaching,
  same as `exec` and `get_shell` today.

Hardware that needs a GPIO controller to run its own concurrent polling loop
outside the audit/survey pattern, or that cannot honor a hard
`cancel_in_flight`, is a finding for Ty — not a reason to give GPIO its own
lifecycle.

## Failure vocabulary

| Situation | Raised as | Terminal state |
|---|---|---|
| A pin never settles within `settle` | `PinSettleFailure` (provisional; a `TestFailure` subclass, parallel to `ArpResolutionFailure`'s relationship to `TestFailure`) | Fail, if the author catches and re-raises as a verdict; a `ContractViolation`-wrapped Fail if it escapes as a plain exception |
| Direction/level operation misused (write to an input, read an output-only pin) | `TypeError` at the call site | `ContractViolation` (Fail, `bug: true`) — same posture as `ExecRouting`'s connection-pair `TypeError`s |
| Backend cannot reach the physical controller | `ConnectionError` from the interface's own `connect`/`exec`-equivalent | Absorbed the way `RemoteInterface.exec`'s `ConnectionError` is absorbed today — not a bug, a hardware-unreachable reading for the test method to judge |
| Integration hook throws while pins are held mid-toggle | Framework raises `ExternalIntegrationFailure` | Aborted; every open `GpioInterface` is driven to idle as part of the halt, per the In-Flight Termination Contract |
| Operator SIGINT/SIGTERM mid-run | Framework raises `OperatorInterrupt` | Aborted; same idle-drive sweep |

No new terminal state is introduced. `PinSettleFailure` is data (a `TestFailure`
subclass), never control flow — a flaky pin failing to settle is a hardware
finding, exactly the same posture `TestFailure` already holds for a DUT that
fails a check.

## Mock and real backends: one contract, two implementations

The pattern is the one `RemoteInterface`/`MockTransport`/`SshInterface`
already proves for `exec`: `GpioInterface` is the ABC; a real backend
(provisionally targeting `libgpiod` or a vendor SDK) and a `MockGpio` test
double both implement the same `connect`/`_set_level`/`_read_level`/
`_cancel_in_flight` methods, and self-tests exercise both against a shared
contract-suite of assertions (declare pin, set level, read level, abort mid-
toggle, verify idle drive) so a bug in either backend is caught by the same
tests. `MockGpio` schedules `park`-style holds analogous to
`MockTransport.park`, so lifecycle tests can assert that an abort arriving
mid-debounce still drives idle levels without a real controller on the bench.
A suite author never constructs either backend directly, mirroring
`use_shell`/`get_shell`: a provisional `utils.get_gpio(...)` call resolves to
whichever backend the run was configured with.

## Evidence

A pin read that matters to the verdict travels with it exactly as the Evidence
Contract already requires: `PASS({"reset_pin_level": level})` or
`TestFailure(..., evidence={"reset_pin_level": level})`, declared up front with
`@evidence(...)` when the method promises to carry it. A GPIO level itself is
not a parametric measurement (there is no unit, limit, or continuous scale to
it — it is boolean-shaped evidence), so it is recorded as a plain evidence
entry, not through the `#85` measurements protocol. Where a GPIO-adjacent
reading *is* parametric — a pulse-width or duty-cycle-like timing measured off
a pin transition — that measurement follows whatever protocol issue #85
settles on, referenced there rather than re-specified here.

Every pin declaration, direction, and idle-level assignment is logged through
`utils.logger` at configuration time, the same way `ExecRouting` logs a
connection's provenance lines — so the Floor Technician can see what idle
state was promised without opening the suite source.

## What the Floor Technician sees

When an abort drives a pin to its idle level, the node's `timeline.jsonl`
(and its human-readable `timeline.log` twin) carries a log entry at the scope
of the node that owned the pin, stating which pin was driven and to what
level, immediately before the node's terminal Aborted entry — the same
temporal-backbone placement the In-Flight Termination Contract already gives
a hard-closed SSH transport. `result.json`'s `summary_tree` for that node
shows `state: "Aborted"`; there is no separate GPIO-specific field, because
the safe-state action is evidence of what the framework did to protect the
hardware, not a verdict of its own. A technician reading the timeline sees:
the run aborted (from `failure`, naming the operator or the integration hook
that threw), and — from the log line — that the reset/enable/relay pin the
suite declared was left in its documented idle state, not in whatever level
the test last happened to set.
