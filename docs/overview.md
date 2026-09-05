---
name: litmus project overview
description: Comprehensive synthesis of project purpose, domain, architecture, and design philosophy
type: project
---

# litmus — Project Overview

**Philosophy:** "The act of running a Litmus Test is the act of producing irrefutable evidence."

"Happy path" scripts work 99% of the time but cause 100% of the line stoppages because they fail ambiguously. Litmus forces the SE to handle the "Sad Path" (Infra vs. Hardware vs. Integration) at design time.

## What It Is

An opinionated manufacturing test framework built in Python (asyncio). It seeks to implement empathetic architecture by addressing SE → tech communication issues upstream via a pit-of-success model.

This is the Python port of the TypeScript original (`../litmus-test-legacy/`, read-only reference). Parity with the original is explicitly not a goal; the contracts are carried forward, the enforcement is stronger.

## How it does that

Litmus gives the SEs a framework that allows them to focus on getting the test right and logging meaningful evidence. It does this by reducing cognitive load on the SE as much as it possibly can. The SE declares their intent and Litmus collects and organizes structured evidence. Much like a real litmus test, **the act of executing the test is the act of producing evidence. full stop.** The SEs capture meaningful information, and the litmus inspector guarantees that a tech is able to turn that data into information at 2am.

## The Litmus Success Metric

A Floor Technician (the "FT" of the source docstrings) can accurately categorize a failure and initiate the correct recovery protocol within 60 seconds of opening the Inspector, without needing to Slack the Software Engineer.

## The Problem It Solves

AI hyperscaler server manufacturing (Google, Meta, AWS, etc.) requires rigorous, evidence-rich testing at multiple assembly stages. Current pain: test software engineers manually maintain IPN mapping files, manually look up BOM details, manually add each new customer part number to test. This scales with product velocity and introduces silent error risk.

## The Core Purpose

> "We are giving developers the tools they need to give floor operators the tools THEY need,
> and doing so without additional cognitive load on the developer."

Two-level service contract:
1. **Framework → Developer**: lifecycle guarantees, automatic evidence collection, structured failure signals — developer focuses on test logic, not plumbing.
2. **Developer → Floor Technician**: clear names, hardware-domain failure messages, scoped artifacts in the inspector tree — tech makes confident judgment calls without escalating to a software engineer.

## The Primary Beneficiary: Floor Technician

#### "Floor technicians simultaneously have the biggest impact on the success of our business, and the least control over how it operates"

Typically early-career technology enthusiast. Passionate, capable, but many are operating at the edge of their knowledge. They are the first person to see failures and to make failure category judgment calls. They are the experts on how to handle any given failure mode. The inspector is their primary tool — and also a containment layer preventing them from going places they shouldn't.

## The Atom

**`LitmusTest` is the irreducible unit of the manufacturing test problem domain.**

- Does not know or care what the DUT is
- Does not know what stage it's running at
- Does not know what surrounds it in the composition
- Only knows: `configure` → `build_suite` → `cleanup`

A rack-level L11 test is not a different kind of thing — it is more atoms composed together. Leaf cases, sequences, groups, and full hardware devices are all instances of `LitmusTest`; the framework is fractal. A `LitmusTest` is simultaneously a claim about hardware and a procedure for verifying that claim: **evidence at scale N−1 becomes a verdict at scale N.** The DUT tree and the test tree are one structure.

## The Chemistry Mental Model

| Level | Framework Construct |
|---|---|
| Atom | `LitmusTest` (leaf case via `self.case()`) |
| Molecule | `TestSequence` (ordered, bails on failure), `TestGroup` (concurrent, collects all) |
| Organelle | `LitmusTest` subclass (encapsulates state + lifecycle, produces atoms via `build_suite()`) |
| Organism | Full rack/system-level suite |
| Emulsion | L10+JBOG/JBOD — two molecules behaving as unified system under test |

**Key property**: No level needs to know about the levels above it. Composition is the only coupling mechanism and the framework is contractually obligated to provide it.

## The Three Terminal States

Every run of a `LitmusTest` resolves to one of three peer states — no silent exits, no hidden fourth category:

1. **Pass** — the test method returned the `PASS` sentinel explicitly. Cannot accidentally pass: the framework checks `result is PASS` (identity, not equality), so a string, a truthy value, or a parsed config cannot forge it. It can only come from `from litmus import PASS`.
2. **Fail** — the test method raised a `TestFailure`. Structured articulation of what the hardware did wrong, or (for a plain exception) a loud BUG signal that the developer didn't model this failure mode.
3. **Aborted** — no verdict was produced. Two causes:
   - **Harness-level:** an external integration hook (`before_each`/`after_each` on the run params) threw, halting the run before a verdict could be reached.
   - **Node-local:** the test method returned anything other than the `PASS` sentinel — a `None` fall-through or an explicit wrong value (the string `"PASS"` included). No verdict exists, so the framework refuses to forge Fail (or Pass) on its behalf. Siblings are unaffected.

   **Peer state to Pass/Fail, NOT a flavor of failure.**

> The node-local Aborted cause is a deliberate deviation from the TypeScript original, which folded a missing verdict into the bug/Fail path. The port's position: a bug that *threw* produced information (Fail + BUG signal); a method that *returned wrong* produced nothing, and reporting nothing as a verdict would itself be a quiet lie.

The tech's recovery protocol differs for each. A Fail says "check the hardware." An Aborted says "check the harness — the shop-floor DB, the coordinator, the integration layer — or the test method that produced no verdict — not the unit." Collapsing these into one category is exactly what the framework refuses to do.

A two-layer bug-guard exists alongside the three terminal states:
- `ContractViolation` — developer contract violation, raised at the lifecycle layer
- `FrameworkContractViolation` — framework internal contract violated (should be unreachable, indicates a bug in the framework itself)

## Failure Taxonomy

Root causes the framework must make obvious:
1. **DUT failed a check** — `TestFailure` from a test method or from `configure()` → state `Fail`. A `TestFailure` from `configure()` means "the device did not reach a testable state"; whether hardware or infrastructure is the cause is **not knowable from code** — the human triager decides.
2. **Hardware lying** — survey artifacts expose mismatch (claimed success vs actual state)
3. **Test case bug** — plain exception from a test method or from `configure()` → BUG signal; a missing verdict → node-local Aborted; also: a test that passes bad hardware
4. **Integration failure** — `ExternalIntegrationFailure` from `before_each`/`after_each` aborts the run. The test may be fine; the harness around it broke (e.g. shopfloor DB unreachable, network congestion).
5. **Operator interrupt** — `OperatorInterrupt` from SIGINT/SIGTERM during a live run. The Floor Technician reads that the operator stopped the rack, not that the harness or the DUT failed.

**A human must always make the final determination.** The framework presents evidence clearly enough that the tech can make the call without a software engineer's help.

## The Temporal Backbone

The log artifact is the authoritative chronological record of a test run. Two complementary views:
- **Structural** (inspector tree): what ran, what passed, what failed, how tests compose
- **Temporal** (log backbone): what happened, in what order, what state at each moment

`await utils.capture_artifact(artifact)` stitches artifacts onto the timeline via
structured pointer log entries. The public `Artifact` value has no file-writing method,
so the framework cannot persist evidence without also recording that pointer.

Every full run creates one timestamped directory beneath the configured artifact root.
Logging nodes stream a human `timeline.log` and a versioned Inspector
`timeline.jsonl`; inline cases roll into their nearest owning ancestor. Both views are
flushed entry by entry, preserving the accepted chronology through interruption.
When the root reaches Pass, Fail, or Aborted, the framework atomically writes its
versioned `RunResult` to `result.json` at that run-directory root. The root timeline's
final entry points to the file, so the structural and temporal views remain joined.

**Timestamp integrity is a system requirement.** NTP sync on the test VM is not optional. The log backbone is used as a legal instrument in customer disputes ("did failures start before your 2pm firmware push?").

## Integration Hooks: The External-World Contract

`before_each` and `after_each` on the run params are how Litmus composes with the world outside the test framework — shop-floor databases, lab coordinators, audit pipelines, any system that needs a "started" signal before each test and a "finished" signal after.

- Registered once at the root `run()` call, fire around every executed test in the tree (fractal dispatch)
- Throw from either hook → run halts → enclosing node transitions to `Aborted`
- **Pair invariant**: `after_each` fires even when `before_each` threw, so "started" and "finished" records stay balanced in the external system. No orphan rows.
- Hooks receive a narrow context (node name, ancestor chain, scoped logger, env snapshot) — NOT `utils.exec` or `get_shell`. The interface encodes "integration hooks don't touch the hardware."

When a hook throws, the required end state is for the framework to close every in-flight transport across the whole tree immediately (SSH connections destroyed, subprocesses killed). A 30-minute firmware flash in flight when the integration layer halts must NOT continue to mutate the hardware. This is load-bearing for manufacturing test integrity.

### The Pit of Success Model

The correct pattern must be the path of least resistance — enforced by API shape, not documentation.

Litmus is deliberately designed so that the _right_ way to write a test is also the _easiest_ way. The framework uses runtime enforcement, import-time structural checks, sensible defaults, and loud signals to guide developers toward honest, evidence-rich tests without requiring them to constantly consult the docs.

Here are some of the ways Litmus implements this:

- **No pass by default** — test methods must explicitly return the `PASS` sentinel. Reaching the end of the method without doing so is a missing verdict (node-local Aborted); raising a plain exception is a developer contract violation. Even internal implementations like `TestSequence` explicitly model the passing condition as (all tests ran AND all tests passed) — it does not silently pass if nothing threw.
- **Setup is constrained to `configure()`** — All infrastructure wiring, readiness checks, and context creation must happen in `configure()`. The framework merges the return value into `utils.ctx` for the rest of the lifecycle and all descendants. This prevents hidden state coupling and makes the data flow explicit.
- **Unmodeled failure modes are loud** — Raising anything other than a `TestFailure` (or subclass) from a test method produces a clear "CONTRACT VIOLATION" message with a BUG signal. The framework refuses to let an unhandled error silently become a pass.
- **`use_shell()` auto-wires execution routing** — Calling `utils.use_shell()` in `configure()` automatically rebinds `utils.exec` to route through the SSH connection. Child tests and descendants inherit the routing with zero extra code. The SE never has to manually thread SSH interfaces.
- **Composition takes bound methods directly** — `self.case("name", self._my_method)` works with a plain bound method reference. No wrapping, no binding ceremony.
- **Sensible defaults reduce boilerplate** — `self.case("name", self._my_method)` defaults to `discrete_log_file=False`, so its logs automatically roll up into the parent. This gives you **one log per logical abstraction instead of one log per screw**.
- **Survey and audit hooks belong in the constructor** — Registering them only in `__init__` makes them part of the test's stable identity (visible on the class and consistent across runs).
- **`wait_for()` requires intent** — The `looking_for` parameter is mandatory and becomes the primary diagnostic string when a timeout occurs. This nudges developers to write human-readable failure messages.
- **Import-time structural enforcement** — a tier the TypeScript original never had. `__init_subclass__` on `LitmusTest` verifies at class *definition* (import time, before any hardware is touched) that the subclass is structurally sound: required lifecycle methods present and async, sealed internals (`main`, `run`, `_run_lifecycle`, the `_safe_*` family) not overridden. A malformed test refuses to import; it can never reach the floor.

These constraints are not restrictions — they are guardrails that make it difficult to write ambiguous or incomplete tests while making the correct patterns feel natural.

## Why Litmus Rejects "Pass by Default"

Most test frameworks treat "the test reached the end without throwing an error" as a passing result. This is the default mental model the industry has used for decades.

**Litmus rejects it outright.**

#### The Quiet Lie

When a test passes by default, it has only proven one thing:

> It did not encounter any of the failure modes the author happened to think of and write explicit checks for.

That is not evidence that the system works. It is a quiet lie — one that accumulates silently until it causes a line stoppage. Litmus provides the guardrails that make it difficult to accidentally ship a test that only _appears_ complete.

### Cases Where Explicit Pass Modeling Feels Awkward

Here are the situations where forcing an explicit `PASS` can feel clunky if you are coming from a pass-by-default mentality. In every case below, the job is the same: make the pass condition a readable check the next developer can find and understand. `return PASS` on its own is syntax, not modeling — the conditional that gates it is where your intent lives, and where `litmus-check` (the static-analysis tier) will insist it goes.

| Situation | Why It Feels Awkward | How to Model the Pass Condition |
|---|---|---|
| **Long-running / soak tests** | Success = "it didn't crash for 8 hours" | Track error count or health samples across the run. `if errors == 0 and samples_collected >= expected: return PASS` — the conditional names what "healthy for 8 hours" actually means. |
| **Event-driven / callback tests** | Success depends on receiving a callback within time | `if await wait_for(lambda: callback_fired, looking_for=...): return PASS` — the wait returning truthy IS the success condition, gated explicitly. |
| **Performance / load tests** | Success = "p99 latency < 50ms and throughput > 10k req/s" | Collect metrics, then: `if metrics.p99 < 50 and metrics.throughput > 10_000: return PASS` — **both** thresholds named in code, not just in your head. |
| **Fuzzing / property-based tests** | Success is probabilistic ("no counterexample found in 10,000 runs") | `if iterations_completed == 10_000 and not counterexamples: return PASS` — **both halves matter**: you ran the full budget AND found nothing. |
| **Best-effort / smoke tests** | "It's okay if some optional checks fail" | Define the threshold as a variable then `if critical_checks_passed: return PASS`. The next reader sees the threshold immediately. |
| **Non-deterministic external systems** | Success depends on flaky third-party behavior | After retries: `if last_attempt_succeeded: return PASS`. The retry loop models the flake tolerance; the final conditional models the verdict. |

#### Why This Matters to Floor Technicians

Floor Technicians are the primary consumers of everything you produce. They are the ones who will open the Inspector at 2am when a station is down. They need to answer three questions in under 60 seconds:

1. What actually happened?
2. Is this a hardware problem, a test problem, or an infrastructure problem?
3. What should I do next?

Pass-by-default tests make all three questions harder. The tech is left guessing at your intent, debugging ambiguous results, and sometimes having to escalate because the evidence is unclear.

Litmus is built so that the evidence a tech needs is produced automatically and presented clearly — without requiring them to read your source code or understand your design intent.

#### What Litmus Demands

Litmus does not merely prefer explicit passes — it **rejects** implicit ones at every tier of the enforcement stack:

| Tier | Mechanism | Enforces |
|---|---|---|
| 1 | Runtime IoC wrapping (the child-boundary in `lifecycle.py`) | Verdict contract, failure taxonomy, lifecycle order — can never be bypassed |
| 2 | Import-time structural checks (`__init_subclass__`) | Subclass shape, sealed internals — fires before hardware is touched |
| 3 | Static analysis (`litmus-check`) | Declarative syntax: pure `build_suite`, gated `PASS`, immutable-self idiom |

**Rule: nothing that protects hardware or verdict integrity may live only in tier 3.**

- In the runtime contract: test methods must return the `PASS` sentinel explicitly. Reaching the end without doing so produces no verdict — the node renders Aborted, never Pass.
- In internal implementations: even when all conditions are met (as in `TestSequence`), the framework still performs an explicit check and refuses to silently pass.
- At the static-analysis level: returning `PASS` outside of an `if` condition is surfaced as a lint error — the framework wants you to model your success condition deliberately.

This is more work up front. It forces you to think about what "success" actually looks like with the same rigor you apply to failure modes.

In return, you get tests that mean what they say, clear failure categorization, and automatic structured evidence that a floor technician can trust.
