# Design note: I2C/SPI interface contract

Status: DRAFT — awaiting Ty's signature on #96

I2C and SPI carry register transactions, not command strings. This note
proposes the transfer, addressing, arbitration, timing, and released-bus
obligations that make both buses fit Litmus's existing lifecycle. They are
two interfaces under one contract shape, not one type with a protocol flag.
The [three HIL rulings](index.md#the-three-rulings) govern both. Approval to
draft #96 does not sign the obligations below; no I2C or SPI implementation
may precede Ty's signature on this note.

All names below (`I2cInterface`, `SpiInterface`, `I2cAddress`, `SpiMode`,
`BusArbitrationFailure`, `BusOperationFailure`, `MockI2c`, `MockSpi`, and
the method names) are provisional. This note adds no public Python name and
changes no stored record. The legacy framework and language server supply no
I2C/SPI behavior to carry forward; the migration context is the preserved
lifecycle and evidence contracts, with a new hardware contract for Ty to
judge.

## Why not an `exec` subclass

A register read is an addressed transaction: a 7- or 10-bit I2C address and
a register pointer, or a SPI chip-select plus a command byte, then a
bounded payload. None of that is a shell command. Forcing it through `exec`
would mean shelling out to `i2cget`/`spidev` tools (lossy, racy, and blind
to a stuck-low SDA) or smuggling a controller inside a `RemoteInterface`
subclass that answers `_exec` with a fake dialect — exactly the
parallel-lifecycle-in-disguise [ruling 1](index.md#the-three-rulings)
forbids. `I2cInterface` and `SpiInterface` are their own ABCs,
structurally parallel to `RemoteInterface` (`src/litmus/remote.py`) and to
the signed [`GpioInterface`](gpio.md) / [`PwmInterface`](pwm.md): each owns
`connect` / `disconnect` / `cancel_in_flight` in the same shape, and adds
transaction-shaped operations in place of `exec`.

They are not subclasses of each other. An I2C NACK is not a SPI 0xFF. A
shared "bus transfer" ABC that pretended otherwise would lose the
arbitration and chip-select contracts this note exists to name.

## The Bus Transaction Contract

**Framework obligation:** Present each bus as its own typed interface under
HIL ruling 1. Neither is an `exec` subclass; neither changes where
`utils.exec` routes. A handle is bound to one adapter, one protocol, and a
declared timing envelope before it can transfer.

- A **transaction** is atomic at the wire: I2C from START through STOP; SPI
  for the whole time chip-select is asserted. The interface does not expose
  a pile of unbound `read()` / `write()` calls that another node, or an
  abort, can interleave mid-address.
- One in-flight transaction per handle. A second transfer is refused while
  one is active; it is not queued to surprise the next lifecycle phase.
  Framework termination takes precedence over this admission rule.
- Each operation records the requested address or chip-select, the
  direction, the payload length, and whether the controller confirmed the
  outcome. An unconfirmed transfer never becomes the last-successful value
  in a cache the next test method reads as truth. Unknown is explicit.
- Payload is `bytes`. Reject a non-`bytes` payload, a negative length, and
  a length above the backend-declared ceiling, at the call site, before the
  adapter is touched. Booleans are not integers.

**Developer obligation:** Declare the adapter, protocol parameters, and
every device this node will address in `configure()`. Obtain the
framework-managed handle through a provisional `utils.get_i2c(...)` or
`utils.get_spi(...)`; return the handle and static inputs through
configure's ctx. Keep `self` immutable, `build_suite()` pure, and later ctx
reads read-only. Judge the DUT with bytes actually returned (or with an
independent observation), never by treating a successful ACK or a full-duplex
clock-out as a passing device.

**Consequence:** The Floor Technician can tell which device was addressed,
what was written, what came back, and whether the transaction completed.
A NACK or a short read cannot quietly become a register value in a record
that claims otherwise.

## The I2C Addressing and Arbitration Contract

**Framework obligation:** I2C identity is an address, not a path.

- A provisional `I2cAddress` carries the 7-bit or 10-bit address and the
  width. Reject an address that does not fit the declared width. 8-bit
  "address plus R/W" encodings are refused: the R/W bit is the transfer's,
  not the handle's.
- Provisional `write(address, data)`, `read(address, n)`, and
  `write_read(address, tx, n)` each emit one START–STOP transaction.
  `write_read` is a repeated-START, not two transactions the abort path
  could split. There is no "hold the bus across calls" mode.
- **NACK on address** (no device ACKed) and **NACK on data** (device ACKed
  the address then rejected a byte) are distinct operational failures,
  named in the error. Neither is a `TestFailure`. The interface does not
  retry, does not clock out dummy bytes after a NACK, and does not return
  a truncated payload as success.
- **Arbitration loss** is an operational failure (`BusArbitrationFailure`).
  Another master won the bus. The interface stops, does not retry, and
  does not invent the bytes it did not finish sending. A backend whose
  driver auto-retries arbitration in the background is refused at bring-up:
  that is the I2C instance of a detached PWM ramp.
- Clock stretching is the slave's right within the backend-declared stretch
  ceiling. Exceeding that ceiling is an operational timeout of the
  transaction, not a node time-budget expiry.

**Developer obligation:** Name every address this node will talk to at
configure time when the set is known. Catch address-NACK, data-NACK, and
arbitration loss separately if they mean different things on this bench
(missing DUT vs. register reject vs. a second tester). Do not busy-loop a
NACK into existence of a device.

**Consequence:** A missing part, a device that refused a register, and a
bus fight are three records, not one "I2C failed" blob.

## The SPI Chip-Select and Mode Contract

**Framework obligation:** SPI identity is a chip-select plus a mode, not an
address.

- A provisional `SpiDevice` declares the chip-select line, `SpiMode`
  (CPOL/CPHA 0–3), bit order, and bits-per-word. Mode is fixed for the
  handle's lifetime; flipping CPOL live is the SPI instance of flipping a
  GPIO pin's direction.
- Provisional `transfer(tx) -> rx` asserts chip-select, clocks `tx`,
  returns `rx` of the same length, and deasserts chip-select. Full-duplex
  is the default shape. A backend that can only half-duplex still returns
  `bytes` of that length; it does not return `None` for the unobserved
  side.
- There is no SPI NACK. MISO idle (typically 0xFF or 0x00) is **data**.
  The framework must not interpret a repeating fill byte as "device
  missing." Absence of a device, if the bench can know it, is a GPIO-level
  or power-level observation, not a SPI protocol error.
- Chip-select is owned for the transaction. The interface does not leave
  CS asserted across calls so an author can "optimize" two transfers. A
  multi-message device that requires CS held uses one `transfer` covering
  the whole sequence, or it is a finding for Ty.

**Developer obligation:** Declare mode, bit order, and chip-select in
`configure()`. Do not decode 0xFF into "unplugged" inside the interface
call; if that fill is a fault, the test method says so with a
`TestFailure` naming the expected register contents.

**Consequence:** A disconnected MISO cannot be laundered into a protocol
exception the Floor Technician would treat like an I2C NACK.

## The Released-Bus and Timing Contract

**Framework obligation:** The safe state is a **released bus** — I2C lines
idle after a STOP, SPI chip-select deasserted and clock at the mode's idle
level — enforced at the resource by the existing termination sweep. This
specializes the signed GPIO
[ownership and cut rules](gpio.md#the-gpio-interface-contract) and the
signed PWM
[disabled-output termination](pwm.md#the-pwm-safe-state-contract). It
introduces no bus halt signal, event loop, lifecycle runner, or
integration hook.

- Register ownership as soon as acquisition can drive a line, so partial
  `configure()` failure cannot leave SDA held low or CS asserted. Bring-up
  establishes and confirms the released state before admitting the first
  transaction.
- An integration-hook failure or operator interrupt releases every open
  I2C and SPI handle. A node deadline releases only resources opened by
  the bounded subtree, under both `fail_after` (Fail) and `abort_after`
  (node-local Aborted). An inherited adapter remains its owner's resource;
  unrelated siblings are untouched.
- `cancel_in_flight()` is idempotent. It aborts the in-flight transaction
  at the controller (I2C: STOP if a START is open; SPI: deassert CS),
  attempts the released state under the backend's latency ceiling, and
  records whether release was confirmed. A repeated call cannot start a
  new transaction or replay the last write. A closed handle remains
  closed.
- The framework does not invent a "safe" register write on abort (clearing
  an output register, putting a DAC to zero). A DUT that needs a specific
  last write gets it from authored `cleanup()`, which the hard cut does
  not wait for. Inventing a write at abort would be a stimulus the suite
  never declared.
- Transaction ceiling (including I2C stretch bound) and bring-up/release
  ceiling are each positive finite backend-declared bounds, separate from
  the node's `fail_after` / `abort_after`. An operation timeout names that
  operation and its ceiling. It does not manufacture the `timeout`
  evidence reserved for the winning node clock in the
  [Time Budget Contract](../contracts.md#time-budget-contract).
- A backend whose shared adapter would let one node's STOP or CS-deassert
  starve another owner's in-flight transaction must refuse the conflicting
  acquisition before either owner transfers. It cannot sacrifice Group
  Isolation to offer more nodes on one bus.

**Developer obligation:** Select a fixture for which "this node releases
the bus" is the intended safe state. Declare shared adapters honestly.
Keep any required shutdown write in `cleanup()`; rely on framework
ownership for termination.

**Consequence:** Ending the test stops this node's transactions and records
what could be confirmed. A technician is never asked to infer that SDA is
free, or that CS is high, from Pass/Fail/Aborted alone.

## Lifecycle and hook ownership: unchanged

The [HIL lifecycle ruling](index.md#the-three-rulings) applies unchanged.
Concretely:

- Bring-up happens the same way `use_shell` brings up SSH: `configure()`
  obtains a handle (provisionally `utils.get_i2c(...)` / `utils.get_spi(...)`,
  mirroring `get_shell` / `get_gpio` / `get_pwm`), and the framework's
  bookkeeping tracks what this node opened versus what it inherited.
- Teardown order is the framework's: `cancel_in_flight` fires as part of
  the same In-Flight Termination Contract sweep that already closes
  `RemoteInterface` transports and silences signed GPIO/PWM resources.
- `use_survey` / `use_audit` remain the observation hooks. A periodic
  audit that reads a status register and raises `TestFailure` on an
  unexpected value is the existing audit contract.
- `before_each` / `after_each` remain the integrator's narrow context and
  still cannot touch hardware.

Hardware that needs the controller to run its own concurrent transfer loop
outside survey/audit, or that cannot honor a hard `cancel_in_flight`
(cannot issue STOP, cannot deassert CS), is a finding for Ty — not a
reason to give either bus its own lifecycle.

## Failure vocabulary

No new terminal state is introduced, and no new exception root.
`BusOperationFailure` and `BusArbitrationFailure` derive from
`LitmusError`, not `TestFailure`, for the same reason GPIO's
`PinSettleFailure` and PWM's `PwmOperationFailure` do: failure to complete
a transaction does not identify the DUT as the cause.

| Situation | Raised as | Terminal state |
|---|---|---|
| Invalid address, mode, payload, or conflicting in-flight use | `TypeError` / `ValueError` at the call site | `ContractViolation` (Fail, `bug: true`) if it escapes |
| I2C NACK on address or data; transaction exceeds its ceiling (including stretch); SPI transfer exceeds its ceiling; release cannot be confirmed at bring-up | `BusOperationFailure` (provisional `LitmusError`) naming the handle, operation, address or CS, and ceiling | Fail when the author catches it and states the verdict; BUG-signaled Fail when it escapes |
| I2C arbitration loss | `BusArbitrationFailure` (provisional `LitmusError`) | As above. Never an automatic DUT Fail |
| SPI MISO fill (0xFF / 0x00) | No exception: those bytes are the transfer result | Whatever verdict the author writes. The interface does not guess "unplugged" |
| Backend cannot reach the adapter at bring-up | `ConnectionError` from the interface's own `connect` | Absorbed the way `RemoteInterface` connect failures are absorbed today |
| Release cannot be confirmed during the safe-state sweep | No exception; the run is already ending | Terminal state unchanged; timeline records the unconfirmed release, naming the handle. Never a success line |
| Integration hook throws mid-transaction | Framework raises `ExternalIntegrationFailure` | Aborted; run-level halt releases every open handle |
| Operator SIGINT/SIGTERM mid-run | Framework raises `OperatorInterrupt` | Aborted; same run-level sweep |
| A node's time budget expires while it holds a handle | Framework settles the node per its declared budget | `fail_after` → Fail, `abort_after` → node-local Aborted; either way only that subtree's handles are released |
| Test returns without a verdict | Existing `MissingVerdict` | Node-local Aborted. A completed transfer cannot supply the missing verdict |

Unknown is represented explicitly. A NACK is not a zero payload. An
unconfirmed STOP is not a free bus.

## Mock and real backends: one contract, two implementations per bus

The pattern is the one `RemoteInterface` / `MockTransport` / `SshInterface`
already proves, and that signed GPIO/PWM reuse: each ABC has a real
backend (`smbus2` / `spidev` / a vendor SDK) and a mock (`MockI2c`,
`MockSpi`). Self-tests exercise both against a shared contract suite.

Required proofs, I2C: refuse bad addresses before START; one transaction
in flight; address-NACK vs data-NACK named separately; arbitration loss
stops and does not auto-retry; `write_read` is one repeated-START;
partial `configure()` cannot leave SDA held; node cuts leave a sibling's
transaction untouched; repeated `cancel_in_flight` issues at most one STOP
and cannot replay a write; unconfirmed release produces only unconfirmed
evidence.

Required proofs, SPI: mode immutable; `transfer` length(tx) == length(rx);
CS asserted only for that call; fill bytes are data; CS deasserted on
abort even mid-clock; exclusive ownership of a shared adapter.

`MockI2c` / `MockSpi` schedule `park`-style holds analogous to
`MockTransport.park`. A suite author never constructs either backend
directly: `utils.get_i2c` / `utils.get_spi` resolve to whichever backend
the run was configured with. Real-backend tests must verify transactions
through independent observation (a second master, a logic analyzer);
mock ACKs cannot establish that a DUT saw the register write.

## Evidence

Register values that matter to the verdict travel with it exactly as the
Evidence Contract already requires: `PASS({"whoami": value})` or
`TestFailure(..., evidence={"whoami": value})`, declared with
`@evidence(...)` when the method promises to carry it. Commanded writes
are stimulus, not measurements. A value that is parametric in the problem
domain (a temperature register, a DAC code) follows whatever protocol
issue #85 settled in `docs/measurements.md`, referenced there rather than
restated here. This note neither fills the reserved `measurements` slot
nor decodes anyone's application protocol.

Every handle declaration — adapter, addresses or chip-selects, I2C width,
SPI mode, ceilings — is logged through `utils.logger` at configuration
time. Transfers, NACKs, arbitration loss, and release attempts use that
scoped logger to distinguish requested, confirmed, interrupted, and
unconfirmed outcomes.

## What the Floor Technician sees

When a handle is released, the node's `timeline.jsonl` (and its
`timeline.log` twin) carries a log entry at the scope of the node that
owned it, naming the handle and whether release was confirmed, immediately
before that node's terminal entry — the same temporal-backbone placement
the In-Flight Termination Contract already gives a hard-closed SSH
transport and that signed GPIO/PWM reuse for idle/disable. The terminal
entry is not always Aborted: a run-level halt and an `abort_after` expiry
both settle Aborted, a `fail_after` expiry settles Fail, and the release
sweep is owed under every one of them.

`result.json`'s `summary_tree` shows whichever state applies; there is no
I2C- or SPI-specific field, because a safe-state action is evidence of
what the framework did to protect the bus, not a verdict of its own. A
technician reading the timeline sees why the run ended and — from the log
line — that this node released the bus.
Where the sweep could not confirm release (SDA still low, CS still
asserted), the timeline says exactly that, naming the handle. An
unconfirmed release never leaves a success line behind. That is the
load-bearing line: a technician reaching into a rack trusts a record that
says the bus is free.

## Decisions for Ty on #96

The draft asks for a ruling on these concrete obligations before
implementation:

1. Two interfaces, one contract shape. I2C and SPI are not one type. A
   NACK is not a 0xFF fill byte.
2. Transactions are atomic (I2C START–STOP, including `write_read` as
   repeated-START; SPI CS held only for one `transfer`). No unbound
   read/write that another node can interleave.
3. Address-NACK, data-NACK, and arbitration loss are named operational
   `LitmusError`s; no automatic retry.
4. SPI MISO fill is data. Device-missing is not a SPI protocol error.
5. Abort safe-state is a released bus (STOP / CS deassert), not a
   framework-invented register write. Authored `cleanup()` may write a
   last value; the hard cut does not wait for it and does not replace it.
6. Shared adapters refuse conflicting ownership when one node's release
   would starve another, same Group Isolation posture as signed PWM.

The lifecycle, verdict, and evidence contracts above are constraints on
that decision. If a required backend cannot satisfy them, the finding
belongs on #96 with the affected guarantee named. It does not earn an
alternate lifecycle or an exception enforced only by a linter.
