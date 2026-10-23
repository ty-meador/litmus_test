# Design note: PWM interface contract

Status: SIGNED — Ty approved this note in !125 on 2026-09-08; recorded on #94

PWM controls a repeating output through its frequency and duty cycle. This note
states the transfer, ramp, timing, and disabled-output obligations that make
that control fit Litmus's existing lifecycle. The [three HIL rulings](index.md#the-three-rulings)
govern it. [Ty's approval of the actual note](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/125#note_3800005090)
signs the obligations below. Implementation requires its own approved issue
and Zavala's review under [API stability](../api-stability.md).

All names below (`PwmInterface`, `PwmChannel`, `PwmOperationFailure`,
`MockPwm`, and the method names) are provisional. This note adds no public
Python name and changes no stored record. The legacy framework and language
server supply no PWM behavior to carry forward; the migration context is the
preserved lifecycle and evidence contracts, with a signed hardware contract
for a later implementation.

## The PWM Transfer Contract

**Framework obligation:** Present PWM as its own typed interface under HIL
ruling 1. `PwmInterface` is not an `exec` subclass and does not change where
`utils.exec` routes. Each named channel has a declared output polarity,
frequency range, duty-cycle resolution, and disabled-output behavior before
it can be enabled.

- Frequency is in hertz, positive and finite. Duty cycle is the fraction of
  the period spent in the declared active polarity, from `0.0` through `1.0`
  inclusive. Reject booleans, non-numbers, non-finite values, out-of-range
  values, and unsupported frequency/duty pairs at runtime before any write.
  A fraction and a percentage are not interchangeable: `50` is invalid.
- A provisional `set_output(channel, frequency_hz=..., duty_cycle=...)`
  deliberately enables or updates the output. It validates the complete pair
  before applying either member. The backend must describe its supported
  pairs and update behavior, including shared timing resources; it cannot
  silently clamp, round, or momentarily apply an undeclared combination.
  A value the hardware cannot represent is refused, with the supported
  resolution named, so the author can request a different value deliberately.
- `disable(channel)` stops output generation. Neither a zero duty cycle nor
  a lost controller connection counts as confirmation that output is disabled.
  The inactive electrical behavior of a disabled channel is part of its
  declaration; disabled does not claim that an attached load has lost power.
- Each operation records the requested setting and whether the controller
  confirmed applying it. An acknowledgement is evidence of controller state,
  not an independent measurement of the waveform at the DUT. A failure or
  timeout leaves the unconfirmed setting explicitly unknown, never cached as
  the last successful setting.
- One operation owns a channel at a time. A second write or ramp is refused
  while one is active; it is not queued to surprise the next lifecycle phase.
  Framework termination takes precedence over this admission rule. A backend
  whose shared timer would let one channel's update or disable alter another
  owner's channel must refuse the conflicting acquisition before touching
  either owner's output. It cannot sacrifice Group Isolation to offer more channels.

**Developer obligation:** Declare the channel, polarity, supported settings,
and disabled behavior in `configure()`. Obtain its framework-managed handle
through a provisional `utils.get_pwm(...)`; return the handle and static
inputs through configure's ctx. Keep `self` immutable, `build_suite()` pure,
and later ctx reads read-only. Judge the DUT with actual observations, rather
than treating a successful output request as a passing test.

**Consequence:** The Floor Technician can distinguish the stimulus requested,
the controller's acknowledgement, and the DUT's response. A rejected request
cannot quietly become a different stimulus in a record that claims otherwise.

## The PWM Ramp and Timing Contract

**Framework obligation:** A provisional
`ramp(channel, target_duty_cycle=..., duration=...)` changes duty at a fixed
frequency. The channel must already be enabled with a confirmed starting
setting. Duration is positive finite seconds, with booleans refused by the
same numeric rule as transfer; target duty follows the transfer validation
above. A frequency ramp is outside this proposal.

- The requested trajectory is linear from the confirmed starting duty to the
  target over the declared duration. The backend states its update interval,
  duty resolution, and worst-case scheduling deviation. Quantized intermediate
  steps must remain within those declared bounds, progress monotonically, and
  reach the representable target; they must not overshoot. Before starting,
  refuse a trajectory that cannot meet the backend's declared envelope.
- Ramp duration and backend-operation deadline are separate quantities. The
  former describes the requested change; the latter bounds how long the
  controller may take to confirm it. As in the signed GPIO
  [stability-window/acquisition-deadline distinction](gpio.md#the-gpio-interface-contract),
  one number cannot describe both the work and the allowance to finish it.
  The resolved ramp completion deadline must exceed its duration and include
  the backend's declared scheduling and acknowledgement allowances. Both
  values appear in evidence before the ramp starts.
- Ordinary output updates, disable operations, and bring-up also have positive
  finite backend-declared latency ceilings. An operation timeout names that
  operation and its ceiling. It does not manufacture the `timeout` evidence
  reserved for the winning node clock in the
  [Time Budget Contract](../contracts.md#time-budget-contract).
- The node's `fail_after` or `abort_after` bounds an awaited ramp like any
  other work. A longer ramp or backend deadline cannot extend that clock.
  Cancellation interrupts the resource's ramp; it does not wait for the target
  duty or a graceful ramp-down before disabling output.
- A ramp settles only when its completion is confirmed or a failure/cut has
  been recorded. An operational failure stops further ramp writes and attempts
  bounded disable of that channel before settlement; failed confirmation stays
  explicit. It cannot leave a detached task changing the output after it
  settles. Blocking SDK work uses the existing framework executor
  boundary. A late native completion must not re-enable or rewrite an output
  after the framework has disabled it.

**Developer obligation:** Choose a supported trajectory and name the node
budget for what expiry means to this test. Do not create a suite-owned timing
loop, suppress `CancelledError`, or use an output ramp as a substitute for
measuring the DUT's response. Catch operational failures and model their
meaning at the test boundary.

**Consequence:** A completed ramp means the declared controller trajectory
completed within its stated envelope. An interrupted ramp carries the point
last confirmed and an explicit interruption, not a fabricated endpoint. The
record keeps a slow controller distinct from a node that spent its budget.

## The PWM Safe-State Contract

**Framework obligation:** The safe state is output disabled, enforced at the
resource by the existing termination sweep. This specializes the signed GPIO
[ownership and cut rules](gpio.md#the-gpio-interface-contract); it introduces
no PWM halt signal, event loop, lifecycle runner, or integration hook.

- Register ownership as soon as acquisition can affect a channel, so partial
  `configure()` failure cannot orphan an output. Bring-up establishes and
  confirms disabled state before admitting the first enable operation.
  Failure to establish it refuses use and records the unconfirmed state.
- An integration-hook failure or operator interrupt disables every output
  owned by every open PWM interface. A node deadline disables only resources
  opened by the bounded subtree, under both `fail_after` (Fail) and
  `abort_after` (node-local Aborted). An inherited controller or borrowed hop
  remains its owner's resource; unrelated siblings are untouched.
- Normal teardown disables owned outputs as well. The framework retains its
  existing order: bounded authored cleanup and post-cleanup survey, then
  release of owned resources before their borrowed transport. On a hard cut,
  disable starts immediately through resource termination; it never waits for
  authored cleanup to remember the channel.
- `cancel_in_flight()` is idempotent and refuses further output work when the
  cut begins. It cancels pending ramps at the controller, attempts the disable
  under the declared backend bound, and records whether disable was confirmed.
  A repeated call cannot restart a ramp or perform an enable. A closed handle
  remains closed; there is no automatic reconnect or replay of its last duty.
- Loss of access can prevent confirmation. As the signed GPIO note requires,
  the scoped timeline records that fact with the channel and operation named.
  An unconfirmed disable never leaves a success line behind or rewrites the
  original terminal state. A backend unable to stop autonomous output through
  the existing resource-termination boundary is a finding for Ty before it
  can be admitted; closing its control socket alone does not satisfy this note.

**Developer obligation:** Select a fixture for which the declared disabled
output is the intended safe state. Declare shared controller resources
honestly. Keep restoration in `cleanup()` but rely on framework ownership for
termination; never manually sequence another node's configure or cleanup.

**Consequence:** Ending the test stops its output work and records what could
be confirmed. A technician is never asked to infer electrical safety from a
disconnected handle or from Pass, Fail, or Aborted alone.

## Lifecycle, hooks, and failure vocabulary

The [HIL lifecycle ruling](index.md#the-three-rulings) applies unchanged.
Framework bookkeeping owns connection flags, acquisition, release, and halt
dispatch. `before_each`/`after_each` keep their pair invariant and their narrow
context without hardware access. Constructor-registered surveys and audits
remain the observation hooks: survey errors are logged and swallowed, audit
errors fail their own scope, and `ExternalIntegrationFailure` is never
swallowed. PWM transfer methods return operational results, never `PASS`.

The proposed `PwmOperationFailure` derives from `LitmusError`, not
`TestFailure`, for the same reason GPIO's `PinSettleFailure` does: failure to
apply a stimulus does not identify the DUT as the cause. It names the channel,
operation, requested setting, last confirmed setting if known, and any
backend ceiling that expired. Unknown is represented explicitly, never as
zero duty or disabled output.

| Situation | Signal and existing classification |
|---|---|
| Invalid setting, unsupported trajectory, or conflicting channel use | `TypeError` for a wrong type, `ValueError` for an invalid value or unsupported request; rejected before writes. Escaping configure/test code becomes a `ContractViolation`: Fail with the `CONTRACT VIOLATION` banner and `bug: true`. |
| Controller unavailable during bring-up or an output request; backend operation exceeds its ceiling | `PwmOperationFailure`; the author handles the condition and may raise a modeled `TestFailure`. An unmodeled escape through configure/test code is a `ContractViolation` Fail, not an automatic hardware verdict. No silent retry or assumed setting. |
| Independent observation shows the DUT missed its declared requirement | Author raises `TestFailure(..., evidence=...)`; Fail under the existing verdict contract. |
| Disable cannot be confirmed during finalization | Scoped evidence names the unconfirmed channel; the established terminal state is preserved, as in GPIO's safe-state sweep. |
| Integration hook throws or the operator interrupts | Framework-owned `ExternalIntegrationFailure` or `OperatorInterrupt`; run-level Aborted and the full resource sweep. |
| Node budget expires during transfer or ramp | The winning clock supplies its existing structured evidence: `fail_after` produces Fail, `abort_after` node-local Aborted. Only the owned resources within the cut are disabled. |
| Test returns without a verdict | Existing `MissingVerdict`, node-local Aborted. A completed PWM operation cannot supply the missing verdict. |

This is one proposed operational exception under the existing root, not a new
terminal state. Cancellation still propagates and outranks an attempted late
return or substituted error at the child boundary.

## Evidence and backend acceptance

Configuration logs name the channel, owner, polarity, disabled behavior,
supported resolution, and timing envelope. Transfers and ramps use the same
scoped logger to distinguish requested, confirmed, interrupted, and
unconfirmed outcomes. The safe-state entry names its cause and confirmation
before evidence closes. `timeline.log` and `timeline.jsonl` remain the owning
node's two views; `result.json` retains the existing state and failure shape.

Readings that establish a verdict travel as JSON-safe `PASS(evidence)` or
`TestFailure(..., evidence=...)`, with `@evidence(...)` when a pass owes those
keys. Commanded frequency and duty are stimulus settings, not measured
frequency and duty. Actual parametric observations follow the signed
[#85 measurement design](../measurements.md), whose implementation remains
#86; this note neither fills the reserved `measurements` slot nor redefines
units, limits, or verdict evaluation. In-flight observations use the scoped
log or `capture_artifact`, since a cancelled leaf has no verdict evidence.

`MockPwm` and a real backend must face the same contract suite, following the
`RemoteInterface`/`MockTransport` precedent without inheriting command
semantics. Required proofs include boundary/unsupported values before writes,
atomic pair updates, exclusive channel ownership and shared-timer refusal,
representable ramps within declared bounds, partial acquisition failure,
normal teardown, and interruption at every ramp boundary. Parked operations
must prove repeated cancellation cannot allow a late enable, node cuts leave
an independent sibling unchanged, and unavailable disable produces only
unconfirmed evidence. Real-backend tests must verify output behavior through
independent observation; mock acknowledgements cannot establish it.

## Signed decisions recorded on #94

Ty approved these obligations with the note in !125 on 2026-09-08. The signature
is also recorded on [#94](https://gitlab.com/Tyler-Meador/litmus_test/-/issues/94):

1. Explicit frequency/duty pairs with refusal of unrepresentable requests,
   rather than implicit rounding; fixed polarity and declared disabled behavior.
2. Linear duty-only ramps at fixed frequency, with a backend-declared timing
   envelope and a separate completion deadline; no detached ramp after settlement.
3. Exclusive channel operations and refusal of independent ownership when
   shared hardware cannot preserve node-local isolation and disable semantics.
4. Operational write/bring-up failures as a modeled `LitmusError` condition,
   with unconfirmed finalization recorded under the signed GPIO rule. GPIO's
   typed `UNAVAILABLE` read is not silently extended into a PWM write result.

The lifecycle, verdict, and evidence contracts above are constraints on that
decision. If a required backend cannot satisfy them, the finding belongs on
#94 with the affected guarantee named. It does not earn an alternate lifecycle
or an exception enforced only by a linter.
