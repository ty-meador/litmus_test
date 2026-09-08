# Design note: CAN bus interface contract

Status: DRAFT — awaiting Ty's signature on #95

A CAN frame is not a command string. This note proposes the transfer, filter,
bus-off, timing, and silent-bus obligations that make a controller fit Litmus's
existing lifecycle. The [three HIL rulings](index.md#the-three-rulings) govern
it. Approval to draft #95 does not sign the obligations below; no CAN
implementation may precede Ty's signature on this note.

All names below (`CanInterface`, `CanFrame`, `CanFilter`, `CanBusOff`,
`CanOperationFailure`, `MockCan`, and the method names) are provisional. This
note adds no public Python name and changes no stored record. The legacy
framework and language server supply no CAN behavior to carry forward; the
migration context is the preserved lifecycle and evidence contracts, with a
new hardware contract for Ty to judge.

## Why not an `exec` subclass

A CAN frame has an arbitration identifier, an extended-id flag, a remote flag,
and a payload of bytes. None of that is a shell command. Forcing frames
through `exec` would mean either shelling out to `cansend`/`candump` (lossy,
racy, and blind to bus-off) or smuggling a controller inside a
`RemoteInterface` subclass that answers `_exec` with a fake dialect — exactly
the parallel-lifecycle-in-disguise [ruling 1](index.md#the-three-rulings)
forbids. A frame that has been stringified has already lost the identifier
width, the ACK, and the error counters. `CanInterface` is its own ABC,
structurally parallel to `RemoteInterface` (`src/litmus/remote.py`) and to the
signed [`GpioInterface`](gpio.md): it owns `connect` / `disconnect` /
`cancel_in_flight` in the same shape, and adds frame-shaped operations in
place of `exec`.

## The CAN Frame Transfer Contract

**Framework obligation:** Present CAN as its own typed interface under HIL
ruling 1. `CanInterface` is not an `exec` subclass and does not change where
`utils.exec` routes. A handle is bound to one controller, one bitrate (or
bitrate pair, for a backend that also speaks CAN FD), and a declared
payload ceiling before it can send or receive.

- A provisional `CanFrame` carries `arbitration_id` (non-negative int),
  `extended` (11-bit vs 29-bit), `rtr` (remote transmission request), and
  `data` (`bytes`). Classic CAN admits 0–8 data bytes. A CAN-FD backend
  declares a larger ceiling (up to 64) and a bitrate switch; it does not
  earn a second interface or a second lifecycle. Reject a negative id, an
  id that does not fit the declared width, a payload longer than the
  ceiling, a non-`bytes` payload, and a remote frame that also carries
  data, at the call site, before the controller is touched.
- `send(frame)` places one data or remote frame on the wire and waits for
  the on-bus ACK bit. That ACK is evidence that *a* receiver on the bus
  acknowledged the frame. It is not evidence that the DUT parsed it, acted
  on it, or is even the node that ACKed. A send that the controller cannot
  represent is refused; a send the bus does not ACK within the backend's
  declared transmit ceiling is an operational failure, not a forged
  success. The interface does not retry.
- `receive(deadline=...)` returns the next frame that passes this handle's
  declared filters, or fails if none arrives before the deadline. The
  deadline is positive and finite seconds; booleans and non-finite values
  are refused. There is no GPIO-style stability window: a CAN frame is
  atomic. A timeout does not invent a frame, does not return empty `data`
  with a sentinel id, and does not raise `TestFailure` — see the failure
  vocabulary. Software matching on top of hardware filters is allowed;
  dropping a frame the hardware already accepted is not a protocol error.
- One in-flight send and one in-flight receive per handle. A second `send`
  or `receive` is refused while one is active; it is not queued to surprise
  the next lifecycle phase. Framework termination takes precedence over
  this admission rule.
- Each operation records the requested frame (id, flags, payload length;
  payload bytes when they are JSON-safe and within evidence bounds) and
  whether the controller confirmed the outcome. An unconfirmed send never
  becomes the last-successful frame in a cache the next test method reads
  as truth.

**Developer obligation:** Declare the controller, bitrate, payload ceiling,
and filters in `configure()`. Obtain the framework-managed handle through a
provisional `utils.get_can(...)`; return the handle and static inputs through
configure's ctx. Keep `self` immutable, `build_suite()` pure, and later ctx
reads read-only. Judge the DUT with frames actually received (or with an
independent observation), never by treating a successful `send` ACK as a
passing device.

**Consequence:** The Floor Technician can tell the frame the suite asked to
put on the wire, whether the bus ACKed it, and what the DUT sent back. A
missing ACK cannot quietly become a delivered command in a record that
claims otherwise.

## The CAN Filter Contract

**Framework obligation:** Acceptance filters are part of the handle's
identity, declared before the first `receive`, not discovered by dumping the
bus into the test method.

- A provisional `CanFilter` names an arbitration-id base, a mask, and
  whether it matches standard, extended, or both. Hardware filters are
  programmed at bring-up. A backend that cannot install the declared set
  refuses the handle before any traffic is observed, naming the filters it
  cannot honour.
- Filters are immutable for the handle's lifetime. Changing the mask
  mid-run is the CAN instance of flipping a GPIO pin's direction live:
  `TypeError` at the call site. A suite that needs a different window opens
  a different declared handle.
- An open handle whose filters would accept every frame on a production
  bus is legal but must be explicit — a wildcard filter is a declaration,
  not a default. The default with no filters declared is "receive
  nothing," so an author who forgot to say what they are listening for
  does not silently subscribe to the whole network.

**Developer obligation:** State the filters that belong to this node. Do
not compensate for a missing filter by busy-looping `receive` and
discarding ids in suite code; that turns the test method into a sniffer
and hides the contract from the Floor Technician.

**Consequence:** The record shows which ids this node was allowed to see.
A pass that depended on a frame the handle was not declared to accept is
not reconstructable from the artifacts.

## The CAN Bus-Off and Recovery Contract

**Framework obligation:** Bus-off is a controller state, not a DUT verdict.
When the transmit error counter crosses the bus-off threshold the
controller stops driving the bus. That event is reported as an operational
failure (`CanBusOff`, a `LitmusError`) naming the handle and the last
confirmed error counters if the backend can read them.

- Automatic recovery that resumes transmitting during the same run is
  refused. A controller whose driver auto-recovers in the background would
  put frames on the wire after the suite, or the abort path, has stopped
  sending — the CAN instance of a detached PWM ramp. Bring-up may enable
  a backend's recovery *into silent/stopped*, never into active TX, unless
  the suite calls a provisional `recover()` deliberately.
- `recover()` is a new operation, not an implicit retry of the failed
  `send`. It is refused while a send or receive is in flight, refused after
  `cancel_in_flight` has started, and records whether the controller
  re-entered the declared active state. Failure to recover is the same
  operational class, not a `TestFailure`.
- Error-passive and warning thresholds, when the backend exposes them, are
  logged as evidence of controller health. They do not change control
  flow. Only bus-off (unable to transmit) is a failure of `send`.

**Developer obligation:** Catch `CanBusOff` and decide what it means. A
DUT that storms the bus, a terminator that fell off, and a second tester
fighting this node all produce the same controller state. The framework
must not choose among those. Do not treat recover-and-retry inside the
test method as a substitute for naming the failure.

**Consequence:** Bus-off cannot masquerade as a green run, and it cannot
masquerade as a DUT Fail the author never wrote. The Floor Technician sees
a controller that stopped, not a verdict someone else invented.

## The CAN Safe-State and Timing Contract

**Framework obligation:** The safe state is **silence** — no further frames
originating from this handle — enforced at the resource by the existing
termination sweep. This specializes the signed GPIO
[ownership and cut rules](gpio.md#the-gpio-interface-contract) and the signed
PWM [disabled-output termination](pwm.md#the-pwm-safe-state-contract). It
introduces no CAN halt signal, event loop, lifecycle runner, or integration
hook, and it does not invent a shutdown frame.

- Register ownership as soon as acquisition can put a frame on the wire, so
  partial `configure()` failure cannot orphan a transmitter. Bring-up
  establishes and confirms the declared silent/stopped state before
  admitting the first `send`.
- An integration-hook failure or operator interrupt silences every open
  CAN handle. A node deadline silences only resources opened by the
  bounded subtree, under both `fail_after` (Fail) and `abort_after`
  (node-local Aborted). An inherited controller or borrowed hop remains
  its owner's resource; unrelated siblings are untouched.
- `cancel_in_flight()` is idempotent. It aborts a pending send or receive
  at the controller, attempts to enter the declared silent state under the
  backend's latency ceiling, and records whether silence was confirmed. A
  repeated call cannot resume TX or replay the last frame. A closed handle
  remains closed; there is no automatic reconnect.
- The framework does not transmit a "safe" or "shutdown" frame on abort.
  A DUT that needs a specific last frame gets it from authored
  `cleanup()`, which the hard cut does not wait for. Inventing a frame at
  abort would be a stimulus the suite never declared, written into a
  record the technician might trust.
- Transmit ACK ceiling, receive deadline, and bring-up/silence ceiling are
  each positive finite backend-declared bounds, separate from the node's
  `fail_after` / `abort_after`. An operation timeout names that operation
  and its ceiling. It does not manufacture the `timeout` evidence reserved
  for the winning node clock in the
  [Time Budget Contract](../contracts.md#time-budget-contract). A longer
  receive wait cannot extend the node clock.
- A backend whose shared adapter would let one node's silence or filter
  change starve another owner's handle must refuse the conflicting
  acquisition before either owner sends. It cannot sacrifice Group
  Isolation to offer more nodes on one stick.

**Developer obligation:** Select a fixture for which "this node stops
transmitting" is the intended safe state. Declare shared adapters honestly.
Keep any required shutdown frame in `cleanup()`; rely on framework
ownership for termination. Never manually sequence another node's
configure or cleanup.

**Consequence:** Ending the test stops this node's frames and records what
could be confirmed. A technician is never asked to infer bus safety from a
disconnected handle, from Pass/Fail/Aborted alone, or from a shutdown
frame nobody authored.

## Lifecycle and hook ownership: unchanged

The [HIL lifecycle ruling](index.md#the-three-rulings) applies unchanged.
Concretely:

- Bring-up happens the same way `use_shell` brings up SSH: a `configure()`
  call obtains a `CanInterface` handle (provisionally `utils.get_can(...)`,
  mirroring `get_shell` / `get_gpio` / `get_pwm`), and the framework's
  existing bookkeeping tracks what this node opened versus what it
  inherited.
- Teardown order is the framework's: `cancel_in_flight` on every opened
  `CanInterface` fires as part of the same In-Flight Termination Contract
  sweep that already closes `RemoteInterface` transports and silences
  signed GPIO/PWM resources when `RunAbort` fires.
- `use_survey` / `use_audit` remain the only hooks a suite registers to
  observe the bus; a periodic audit that receives a watchdog frame and
  raises `TestFailure` on an unexpected payload is the existing audit
  contract, not a new hook kind.
- `before_each` / `after_each` remain the integrator's narrow context and
  still cannot touch hardware — a CAN controller is exactly the kind of
  thing those hooks are barred from reaching.

Hardware that needs the controller to run its own concurrent RX loop
outside survey/audit, or that cannot honor a hard `cancel_in_flight`, is a
finding for Ty — not a reason to give CAN its own lifecycle.

## Failure vocabulary

No new terminal state is introduced, and no new exception root.
`CanOperationFailure` and `CanBusOff` derive from `LitmusError`, not
`TestFailure`, for the same reason GPIO's `PinSettleFailure` and PWM's
`PwmOperationFailure` do: failure to put a frame on the wire, or a
controller that has gone bus-off, does not identify the DUT as the cause.

| Situation | Raised as | Terminal state |
|---|---|---|
| Invalid frame, filter, deadline, or conflicting in-flight use | `TypeError` / `ValueError` at the call site | `ContractViolation` (Fail, `bug: true`) if it escapes |
| Bus does not ACK a send within the transmit ceiling; receive deadline expires with no matching frame; silence cannot be confirmed at bring-up | `CanOperationFailure` (provisional `LitmusError`) naming the handle, operation, requested frame or filter, and ceiling | Fail when the author catches it and states the verdict; BUG-signaled Fail when it escapes |
| Controller enters bus-off | `CanBusOff` (provisional `LitmusError`) | As above. Never an automatic DUT Fail |
| Backend cannot reach the adapter at bring-up | `ConnectionError` from the interface's own `connect` | Absorbed the way `RemoteInterface` connect failures are absorbed today |
| Silence cannot be confirmed during the safe-state sweep | No exception; the run is already ending | Terminal state unchanged; timeline records the unconfirmed silence, naming the handle. Never a success line |
| Integration hook throws while frames are in flight | Framework raises `ExternalIntegrationFailure` | Aborted; run-level halt silences every open `CanInterface` |
| Operator SIGINT/SIGTERM mid-run | Framework raises `OperatorInterrupt` | Aborted; same run-level sweep |
| A node's time budget expires while it holds a handle | Framework settles the node per its declared budget | `fail_after` → Fail, `abort_after` → node-local Aborted; either way only that subtree's handles are silenced |
| Test returns without a verdict | Existing `MissingVerdict` | Node-local Aborted. A completed `send` cannot supply the missing verdict |

Unknown is represented explicitly. A timed-out `receive` is not an empty
frame. An unconfirmed silence is not a quiet bus.

## Mock and real backends: one contract, two implementations

The pattern is the one `RemoteInterface` / `MockTransport` / `SshInterface`
already proves, and that signed GPIO/PWM reuse: `CanInterface` is the ABC;
a real backend (SocketCAN, a USB-CAN adapter, a vendor SDK) and a
`MockCan` test double both implement the same
`connect` / `_send` / `_receive` / `_cancel_in_flight` methods. Self-tests
exercise both against a shared contract suite so a bug in either backend
is caught by the same assertions.

Required proofs: refuse unrepresentable frames before TX; exclusive
in-flight send/receive; immutable filters; wildcard-vs-declared receive;
ACK timeout; receive deadline with no invented frame; bus-off stops TX and
does not auto-resume; `recover()` is explicit; partial `configure()`
cannot orphan a transmitter; node cuts leave an independent sibling
transmitting; repeated `cancel_in_flight` cannot replay a frame; unconfirmed
silence produces only unconfirmed evidence. `MockCan` schedules
`park`-style holds analogous to `MockTransport.park`, so lifecycle tests
can abort mid-ACK without hardware. A suite author never constructs either
backend directly: `utils.get_can(...)` resolves to whichever backend the
run was configured with.

Real-backend tests must verify frames through independent observation
(a second SocketCAN interface, a logger); mock ACKs cannot establish that
a DUT saw the payload.

## Evidence

Frames that matter to the verdict travel with it exactly as the Evidence
Contract already requires: `PASS({"response_id": frame.arbitration_id})` or
`TestFailure(..., evidence={"response_id": ...})`, declared up front with
`@evidence(...)` when the method promises to carry it. Commanded frames are
stimulus, not measurements. A payload that is numeric in the problem domain
(RPM on a J1939 PGN, a voltage packed in two bytes) follows whatever
protocol issue #85 settled in `docs/measurements.md`, referenced there
rather than restated here. This note neither fills the reserved
`measurements` slot nor decodes anyone's application protocol.

Every handle declaration — bitrate, payload ceiling, filters, silent-state
— is logged through `utils.logger` at configuration time, the same way
`ExecRouting` logs a connection's provenance. Sends, receives, bus-off,
recover, and silence attempts use that scoped logger to distinguish
requested, ACKed, received, interrupted, and unconfirmed outcomes.

## What the Floor Technician sees

When a handle is silenced, the node's `timeline.jsonl` (and its
`timeline.log` twin) carries a log entry at the scope of the node that
owned it, naming the handle and whether silence was confirmed, immediately
before that node's terminal entry — the same temporal-backbone placement
the In-Flight Termination Contract already gives a hard-closed SSH
transport and that signed GPIO/PWM reuse for idle/disable. The terminal
entry is not always Aborted: a run-level halt and an `abort_after` expiry
both settle Aborted, a `fail_after` expiry settles Fail, and the silence
sweep is owed under every one of them.

`result.json`'s `summary_tree` shows whichever state applies; there is no
CAN-specific field, because a safe-state action is evidence of what the
framework did to protect the bus, not a verdict of its own. A technician
reading the timeline sees why the run ended and — from the log line —
that this node stopped transmitting. Where the sweep could not confirm
silence, the timeline says exactly that, naming the handle. An unconfirmed
silence never leaves a success line behind.

## Decisions for Ty on #95

The draft asks for a ruling on these concrete obligations before
implementation:

1. Frames as a typed value (id, extended, rtr, payload bytes), not an
   `exec` string; Classic CAN 0–8 bytes, CAN-FD as a backend-declared
   ceiling under the same contract, not a fork.
2. `send` confirms the on-bus ACK bit only; a DUT response is a later
   `receive`. No silent retry.
3. Filters declared in `configure()`, immutable for the handle's lifetime;
   the no-filter default is receive-nothing, not subscribe-everything.
4. Bus-off is a `LitmusError` operational condition; no automatic TX
   resume during the run; `recover()` is an explicit later operation.
5. Abort safe-state is silence, not a framework-invented shutdown frame.
   Authored `cleanup()` may send a last frame; the hard cut does not wait
   for it and does not replace it.
6. Shared adapters refuse conflicting ownership when one node's silence
   would starve another, same Group Isolation posture as signed PWM.

The lifecycle, verdict, and evidence contracts above are constraints on
that decision. If a required backend cannot satisfy them, the finding
belongs on #95 with the affected guarantee named. It does not earn an
alternate lifecycle or an exception enforced only by a linter.
