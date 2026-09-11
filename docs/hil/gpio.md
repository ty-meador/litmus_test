# Design note: GPIO interface contract

Status: SIGNED — Ty ruled the open questions on #93 on 2026-09-07

This note states the developer-framework contract for digital I/O pins —
direction, level, debounce, safe-state on abort, the error taxonomy for a pin
that will not settle, and timing guarantees. It is written to the diligence of
`docs/contracts.md` and governed by the three rulings in `docs/hil/index.md`.
All names below (`GpioInterface`, `GpioPin`, `PinDirection`, `PinLevel`,
`PinSettleFailure`) are provisional: they name the shape Ty signed off, not
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
  operations. `PinLevel` carries three members, not two: `HIGH`, `LOW`, and
  `UNAVAILABLE` — the last meaning the controller could not be reached for
  this read. A two-valued return would have to answer that case with a level
  nobody measured, and there is no honest choice between them.
  Setting an input pin or reading an output-only pin (on hardware
  that cannot read back its own output) is a `TypeError` at the call site, not
  a silent no-op — the same posture `RemoteInterface.exec` takes toward a
  disconnected transport, caught before it can be mistaken for a hardware
  reading.
- **Debounce.** A read on an input pin names two quantities, never one: a
  **stability window** — how long the level must hold unchanged for the
  reading to count — and an **acquisition deadline** — how long the framework
  keeps looking for such a stretch. Both are seconds, both validated positive
  and finite, and the deadline must exceed the window. One number cannot carry
  both jobs: a transition halfway through a window leaves no complete stable
  stretch before that same instant, so a single `settle` can demand an outcome
  it has also made unreachable. Given the two, the framework samples at an
  interval no coarser than a tenth of the window and returns the first level
  that holds for a full window. A pin still chattering when the deadline
  passes does not return a guessed value — it raises (see the failure
  vocabulary below). Both defaults are per-backend but always explicit in the
  resolved call. Debounce is symmetric: it is not a filter for "the level I
  expected," it is a filter for "the level that stopped moving."
- **Safe-state on abort.** Every declared output pin carries a required idle
  level, given at declaration time, not inferred from its first `set_level`
  call. Two different events drive pins to idle, and this note keeps them
  apart because the contracts do:
    - *The run-level halt* — an integration hook throw, or an operator
      SIGINT/SIGTERM — drives every output pin that every open
      `GpioInterface` holds to its declared idle level before any interface
      reports `cancel_in_flight` complete. The whole bench is coming down.
    - *A node's time budget expiring is not that halt.* Deadline Aborted is
      node-local: it never fires the shared run halt and never cascades past
      the bounded subtree (`docs/contracts.md`, Time Budget Contract). It
      drives only the pins the cut subtree itself opened; an inherited
      controller or an inherited hop is never in that owned set, and a
      sibling outside the subtree is untouched under the Group Isolation
      Contract. On a rack of thirty units, one node overrunning its clock must
      not idle the other twenty-nine.
  The sweep is owed under both budget names. `fail_after` renders Fail and
  `abort_after` renders node-local Aborted; the verdict differs, the safe
  state does not. This is the GPIO-specific instance of the In-Flight
  Termination Contract's "cancel the resource, not just the coroutine": for a
  relay driving mains power or a reset line holding a DUT in a known state,
  leaving the last-written level in place across an abort is the failure mode
  this contract exists to prevent. Input pins have no safe-state action; they
  carry no risk from being read after abort, and `cancel_in_flight` stops
  accepting reads immediately.
- **Timing guarantees.** `set_level` and `read_level` (after debounce settles)
  complete within a bounded, backend-declared latency ceiling — the interface
  states its worst-case pin-toggle latency so a suite author can reason about
  whether a protocol timing requirement (a reset pulse width, a chip-select
  setup time) is achievable at all through this backend before writing the
  test. A call that cannot complete within that ceiling is a
  backend-operation timeout — the controller gave up — and it is reported as
  the operational failure below. It is not the node's clock expiring, and the
  record must never conflate the two: a suite that declared a generous
  `fail_after` has not thereby reclassified a controller that timed out in
  milliseconds, and a node deadline firing during a pin call has not made the
  controller at fault.

**Developer obligation:**

- Declare every pin's direction and, for outputs, its idle level at
  configuration time — in `configure()`, the same place `use_shell` is called.
  A pin whose idle level is not knowable ("depends on what the last test left
  it at") is a design defect in the test, not something the framework can
  paper over.
- State both debounce quantities deliberately. The defaults are not "right for
  every signal" — a mechanical relay and a solid-state one settle on different
  timescales, and a noisy line often needs a strict window with a generous
  deadline. The author who knows the hardware states both numbers.
- Catch `PinSettleFailure` and decide what it means. It is a `LitmusError`,
  not a `TestFailure`, and that ancestry is the whole point: a pin that will
  not stop moving does not by itself say whether the DUT is faulty, the
  harness is noisy, or the probe is loose. The author models it into a verdict
  or the child boundary classifies the escape as the bug it is — a mode the
  suite never handled.
- Handle `PinLevel.UNAVAILABLE` at every read. It is a real state of the
  bench, not defensive padding: the controller was unreachable and no
  measurement exists. Compare against the member you mean
  (`if level is PinLevel.LOW:`), never against its negation —
  `if level != PinLevel.HIGH:` quietly reads an unreachable controller as a
  low line, and a technician acting on that verdict believes a wire is dead
  when nothing confirmed it.

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
| A pin never settles within its acquisition deadline | `PinSettleFailure` (provisional; a `LitmusError`, deliberately **not** a `TestFailure` — the exact posture `ArpResolutionFailure` holds, `src/litmus/errors.py`) | Fail when the author catches it and states the verdict; a `ContractViolation`-wrapped Fail (`bug: true`) when it escapes, because the suite never modeled the mode |
| A pin call exceeds the backend's declared latency ceiling | `PinSettleFailure`, naming the backend operation | As above. Never recorded as a node time-budget expiry |
| Direction/level operation misused (write to an input, read an output-only pin) | `TypeError` at the call site | `ContractViolation` (Fail, `bug: true`) — same posture as `ExecRouting`'s connection-pair `TypeError`s |
| Backend cannot reach the controller during a read | No exception: `read_level` returns `PinLevel.UNAVAILABLE` | Whatever verdict the author writes for that state. An unhandled third member is not a framework error — it is a reading the suite declined to judge |
| Backend cannot reach the controller at bring-up | `ConnectionError` from the interface's own `connect` | Absorbed the way `RemoteInterface`'s connect failures are absorbed today — not a bug, a hardware-unreachable condition for the test method to judge |
| An idle drive cannot be confirmed during the safe-state sweep | No exception; the run is already ending | The node's terminal state is unchanged, and the timeline records the failure to confirm, naming the pin. A sweep that could not verify idle never leaves a success line behind |
| Integration hook throws while pins are held mid-toggle | Framework raises `ExternalIntegrationFailure` | Aborted; the run-level halt drives every open `GpioInterface` to idle, per the In-Flight Termination Contract |
| Operator SIGINT/SIGTERM mid-run | Framework raises `OperatorInterrupt` | Aborted; same run-level sweep |
| A node's time budget expires while it holds pins | Framework settles the node per its declared budget | `fail_after` → Fail, `abort_after` → node-local Aborted; either way only the pins that subtree opened are driven to idle |

No new terminal state is introduced, and no new exception root.
`PinSettleFailure` is an operational condition rather than a verdict, which is
why it takes `LitmusError` and not `TestFailure`: "this pin will not stop
moving" does not tell anyone whether the DUT is faulty, the harness is noisy,
or the probe is loose, and the framework must not choose among those on the
author's behalf. That is the epistemic position `ArpResolutionFailure` already
occupies. `PinLevel.UNAVAILABLE` is likewise data and never control flow — a
reading the author judges, exactly as a non-zero exit code from `exec` is.

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

When a pin is driven to its idle level, the node's `timeline.jsonl` (and its
human-readable `timeline.log` twin) carries a log entry at the scope of the
node that owned the pin, naming which pin was driven and to what level,
immediately before that node's terminal entry — the same temporal-backbone
placement the In-Flight Termination Contract already gives a hard-closed SSH
transport. The terminal entry is not always Aborted: a run-level halt and an
`abort_after` expiry both settle the node Aborted, a `fail_after` expiry
settles it Fail, and the idle sweep is owed under every one of them.
`result.json`'s `summary_tree` shows whichever state applies; there is no
separate GPIO-specific field, because a safe-state action is evidence of what
the framework did to protect the hardware, not a verdict of its own. A
technician reading the timeline sees why the run ended (from `failure`, naming
the operator, the integration hook that threw, or the clock that fired) and —
from the log line — that the reset/enable/relay pin the suite declared was left
in its documented idle state, not in whatever level the test last happened to
set.

Where the sweep could not confirm a pin reached its idle level, the timeline
says exactly that, naming the pin. This is the most load-bearing line in the
note. A technician reaching into a rack trusts a record that says a relay was
de-energized, and a success line written for a drive nobody confirmed is how
someone puts a hand on a line believing it dead. An unconfirmed idle is
recorded as unconfirmed.
