# HIL interface design notes

Phase 9 gives each hardware-in-the-loop interface its own contract, written to the
same diligence as `docs/contracts.md`, and signed by Ty before any implementation
begins. This page states the three standing rulings that govern every note below
once, so each note can cite them instead of repeating them.

## The three rulings

1. **New contracts, not `exec` subclasses.** `exec` is a command contract: a string
   goes in, an `ExecResult` comes out, and a non-zero exit is data for the test
   method to judge. A GPIO pin level, a PWM duty cycle, a CAN frame, an I2C/SPI
   transaction, and a CoAP/HTTP resource are not commands — none of them fit the
   shape of "run this string and collect stdout/stderr/exit_code." Coercing one
   into `exec` would either lose information (a frame is not text) or invite the
   test author to shell out to a protocol library instead of calling a typed
   method. Each interface below gets its own contract, in the same shape as
   `docs/contracts.md`: a named contract, the framework's obligation, the
   developer's obligation, and the consequence of getting it wrong.
2. **The framework owns lifecycle and hooks, unforked.** Connected-flag
   ownership, the idempotent `cancel_in_flight` halt, `before_each`/`after_each`
   dispatch and the pair invariant, `use_survey`/`use_audit` asymmetry (survey
   swallows, audit does not), and the In-Flight Termination Contract belong to
   `src/litmus/lifecycle.py`, `src/litmus/hooks.py`, and the transport base in
   `src/litmus/remote.py`. A new interface contract governs transfer, error
   taxonomy, timing, and safe-state for its own protocol — it does not reopen how
   the framework sequences `configure` → `build_suite` → `cleanup`, how a hook
   pair fires, or how a hard abort reaches a resource. Hardware whose behavior
   cannot be expressed under those existing contracts is a finding to bring back
   to Ty, not a license to grow a parallel lifecycle for one interface.
3. **Ty signs the design note; Zavala signs the implementation.** Each note below
   is `DRAFT` until Ty records his signature on its issue. No implementation MR
   may open, and no reference implementation (#98) may build against a shape,
   until the note it depends on carries that signature. An implementation may
   not outrun its approved note.

## The notes

| Interface | Note | Issue | Status |
|---|---|---|---|
| GPIO | [gpio.md](gpio.md) | #93 | DRAFT |
| PWM | `pwm.md` | #94 | DRAFT — lands in its own MR |
| CAN bus | `can.md` | #95 | DRAFT — lands in its own MR |
| I2C / SPI | `i2c-spi.md` | #96 | DRAFT — lands in its own MR |
| Device API (CoAP/HTTP) | `device-api.md` | #97 | DRAFT — lands in its own MR |
| Serial (UART) | [serial.md](serial.md) | #91 | DRAFT — landed in its own MR |

Each of the four notes not yet present links only once its own MR merges: each
of #94-#97 opens as an independent, single-file MR carrying just its own note
(so `mkdocs --strict` passes standalone on that branch), and this page is not
touched again to turn its filename into a link until that file exists on
`main` — otherwise this page itself would carry a broken link on every branch
but the one that adds the target.

Serial/UART is Cayde's vertical (ruled by Ty as both a local peer of SSH and the
same transport reached through a `via` hop). It is listed here for navigation
only — its note lives at `docs/hil/serial.md`, drafted and maintained in its own
MR, and is not one of the five notes this page introduces.

## What every note holds in common

- A `Status: DRAFT — awaiting Ty's signature on #N` header. The note is a
  proposal until that line changes.
- Provisional interface and type names, clearly marked as provisional. Naming is
  not the thing being signed off; the obligations are. A name may still change
  between sign-off and implementation without reopening the ruling, provided the
  obligations it names do not move.
- A statement that lifecycle and hook ownership are unchanged, made concrete for
  that hardware — not a restatement of ruling 2 in the abstract, but what it
  means for *this* protocol's bring-up, teardown, and abort path.
- A failure-vocabulary mapping onto the taxonomy already in `docs/contracts.md`
  and `src/litmus/errors.py` (`TestFailure`, `ContractViolation`,
  `ExternalIntegrationFailure`, `OperatorInterrupt`, `MissingVerdict`). No note
  invents a new terminal state or a new exception root; Aborted and Fail keep
  their existing meanings.
- A description of how a mock backend and a real backend both satisfy the same
  contract — the pattern `RemoteInterface`/`MockTransport`/`SshInterface`
  already establishes for `exec`, carried over to the new protocol.
- A statement of how evidence is recorded, tied to the Evidence Contract
  (`PASS(evidence)`, `@evidence`) and, where the interface produces a measurable
  quantity, a reference to the parametric-measurements protocol under design in
  issue #85 rather than a restatement of it.
- A description of what the Floor Technician sees in the timeline when the
  framework takes a safe-state action on that interface.

None of these notes changes `litmus.__all__`. Every name in them is provisional
until an implementation issue is raised and signed off in its own right, per
`docs/api-stability.md`.
