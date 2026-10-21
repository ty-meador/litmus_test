# Developer-Framework Contracts

The contracts below define the mutual obligations between the developer and the framework.
These are non-negotiable invariants. Any API change must preserve every contract listed here.

In the Python port, tier-1 enforcement of these contracts lives in the child-boundary
wrapper (`src/litmus/lifecycle.py`, `ChildBoundary`): every call the framework makes into
suite code passes through it, and per-slot policy (`SLOT_POLICIES`) is declared once and
read nowhere else. Nothing that protects hardware or verdict integrity may live only in
static analysis.

---

## Lifecycle Contract
**Developer obligation:** Implement `configure()`, `build_suite()`, and `cleanup()` with the correct intent.
- `configure()` — put the test in a known state and return what the tests will read. Readiness gates and polling live here. The return is a plain dict (or a local `TypedDict` under pyright strict; the framework annotation is `Mapping[str, Any]`) whose entries become `utils.ctx` for test methods, hooks, `cleanup()`, and all descendant tests. Ctx does not change after that: a `[]` read of a key `configure()` did not provide, and any write once it has returned, is a `ContractViolation` naming the test and the key, not a hardware Fail. `.get()` and `in` read an optional key (issues #112, #118).
- `build_suite()` — pure table-of-contents composition. Return a `LitmusTest` built from `self.test()` / `self.sequence()` / `self.parallel()`. Test methods (private class methods passed to `self.test()`) carry the actual verdict logic — return `PASS`, or `PASS(evidence)` with the readings, to pass; raise `TestFailure` (optionally with `evidence=`) to fail intentionally.
- `cleanup()` — restore state. Must not throw (errors are swallowed and logged). It reads ctx and does not write it; a value it must restore was captured in `configure()`.

**Framework obligation:** Execute configure → build_suite → cleanup in order, guaranteed.
- If `configure()` raises a `TestFailure`: cleanup runs, test methods never run, the test lands in `Fail`. The verdict means "the device did not reach a testable state" — whether the underlying cause is hardware or infrastructure is **not knowable from code** and is the human triager's call.
- If `configure()` raises anything else: wrapped as "THIS IS A BUG IN <name>" (`ContractViolation`) — configure must signal verdicts via `TestFailure`.
- If a test method raises a plain exception: wrapped as "THIS IS A BUG IN <name>" — not a hardware failure.
- A cancellation already delivered to suite code outranks any return value or different
  exception produced after a pathological catch. The child boundary restores the
  cancellation before classifying that settlement, so an aborted run cannot forge a
  hardware or bug verdict.
- `cleanup()` always runs regardless of any prior outcome — after configure failure and after abort. Framework finalization shields it from caller cancellation within its own bounded timeout; repeated cancellation cannot abandon that wait, and the original cancellation is re-raised after cleanup settles or exhausts the evidence budget.
- The evidence budget is 30 seconds, granted **per node**, not per run. Each node's cleanup draws its own allowance, so a deep tree tearing down after an abort can spend up to N × 30 seconds before the abort reaches the operator — plan teardown depth with that ceiling in mind. A cleanup that exhausts its allowance is a `ContractViolation` charged to that node.
- Lifecycle execution order is enforced at runtime by the child boundary — the type system is not the enforcer.

---

## Terminal State Contract
**Framework obligation:** Every run of a `LitmusTest` resolves to exactly one of three peer terminal states. The states are distinct categories with distinct recovery protocols — the framework does not collapse them. There is no hidden fourth: a call that settles without producing a terminal state (a successful `configure`, a completed `cleanup`) reports no state at all rather than inventing one.

1. **Pass** — a test method returned a `Verdict`: the bare `PASS`, or `PASS(evidence)` carrying its readings. The check is `isinstance(result, Verdict)`; a `Verdict` is obtained only by importing `PASS` or calling it, so nothing a suite constructs can forge one. A pass without its declared evidence is not a pass: when the method carries `@evidence(...)`, a `PASS` that does not carry every declared key is a `ContractViolation` (developer-bug Fail), never a green run (issue #118).
2. **Fail** — a test method raised a `TestFailure` (or subclass): the DUT produced a verdict and the verdict was negative. OR the developer didn't model a failure mode: a plain exception → BUG-signaled Fail.
3. **Aborted** — no verdict was produced. Peer to Pass/Fail, **NOT** a flavor of failure. Three causes:
   - **Harness-level (integration):** a `before_each`/`after_each` integration hook threw; the error is wrapped as `ExternalIntegrationFailure` and the run halts before a verdict could be produced.
   - **Harness-level (operator):** SIGINT or SIGTERM arrived while a run was live; the failure is `OperatorInterrupt` so the Floor Technician reads that the operator stopped the rack. The same run-abort path fires as a hook throw: Aborted everywhere, transports hard-closed, `RunResult` returned.
   - **Node-local:** a test method returned anything other than the `PASS` sentinel — `None` fall-through or an explicit wrong value (`MissingVerdict`). No verdict exists, so the framework refuses to forge Fail (or Pass) on its behalf. Siblings are unaffected. *(Deliberate deviation from the TypeScript original, which folded this into the bug/Fail path.)*

**Developer obligation:** Recognize the taxonomy. A `TestFailure` means "the DUT failed a check"; an `ExternalIntegrationFailure` means "the harness around the test broke"; an `OperatorInterrupt` means "the operator stopped the rack." Never conflate them — the Floor Technician's recovery protocol depends on knowing which. A `KeyboardInterrupt` raised from suite code is not an operator signal: the child boundary still passes it through unrecorded.

**Framework obligation:** When a descendant's hook throws, the `ExternalIntegrationFailure` propagates up through every ancestor class-intact. Sequences and parallel nodes do not rewrap it. Every ancestor's state cascades to Aborted.

---

## Time Budget Contract
**Framework obligation:** Any node may declare a time budget by naming the terminal
state it produces on expiry: `fail_after=` (seconds) or `abort_after=` (seconds).
Exactly one name, or neither. Both on one node is a `ContractViolation` at composition
time. A node with neither has no deadline of its own and is bounded only by its
ancestors. Durations must be positive, finite numbers; booleans, zero, negatives,
infinities, NaN, and non-numbers are rejected before execution.

- The pair is part of the `LitmusTest` fractal atom. It is accepted by authored-node
  construction, `self.test()`, `self.parallel()`, `self.sequence()`, and root `run()`.
  Every node is a suite, so every placement reads the same way: this suite completes
  within the budget or the named state is raised. Construction and `run()` are two
  ways to declare the root's one budget, not a way to attach two clocks to one node.
- The window opens when the node enters `configure` and closes when its test method, or
  its last child, settles. Leaves, parallel nodes, and sequences open the equivalent
  window at their test/child boundary. Cleanup runs after the cut on its own
  finalization budget. Root-owned `before_each` and `after_each` integration hooks are
  outside every declared node window; time spent in them cannot spend an ancestor's
  hardware-verdict budget.
- A budget bounds the whole subtree. Every declared clock remains active while its
  subtree does test work. Descendants inherit all ancestor bounds and may only tighten
  them. The clock that fires first decides the state; a descendant's longer declaration
  can never loosen an ancestor's remaining time. A `wait_for` timeout longer than the
  effective enclosing budget never fires—the budget wins. Concurrent descendants are
  separate active lineages: one sibling entering an excluded hook or cleanup cannot
  pause an ancestor clock while another sibling is still doing timed work. The clock
  stalls only while no descendant lineage is inside its timed window.
- Expiry settles every descendant still running with the winning clock's declared
  state. `fail_after` renders Fail with a structured `TestFailure` naming the budget.
  `abort_after` renders node-local Aborted without forging a failure verdict. Descendants
  already settled retain their verdicts, and a composite follows the ordinary terminal
  aggregation rule: any Aborted outranks Fail.
- Deadline Aborted is node-local. It never fires the shared run halt and never cascades
  beyond the bounded subtree. Siblings outside that subtree are untouched under the
  Group Isolation Contract. Expiry is never classified as a BUG.
- Expiry cancels the coroutine and hard-cancels at the resource every transport the cut
  node itself opened. An inherited route is never in that owned set and is not touched;
  it remains the parent's, available to the parent and to every sibling behind it.
- A parent whose `configure()` opens the hop owns its own configure window. If that
  window expires, the Fail or Aborted state and its timeout evidence are the parent's,
  and the children it never composed render as the Lifecycle Contract says for a parent
  that never reached a testable state.
- Every cut node carries structured `timeout` evidence with `reason="timeout"`, the
  declared name, the declared budget in seconds, active elapsed seconds, and the
  root-to-node path of the clock that fired. This evidence appears on `RunResult`, in
  every affected `summary_tree` node, and in their JSON forms.

**Developer obligation:** Choose the name for what “took too long” means at that step.
`fail_after` says the hardware did not do its job in time; `abort_after` says the
harness gave up and no verdict exists. Never swallow `asyncio.CancelledError`; doing so
would obstruct every deadline declared above the await.

**Consequence:** Fail at a deadline sends the Floor Technician to the hardware. Aborted
at a deadline sends them to the harness. The composition names that distinction, and
the record identifies the exact clock and budget without requiring the suite source.

---

## Failure Taxonomy Contract
**Framework obligation:** Every failure must be traceable to one of the following root causes, made obvious by signal:
1. **DUT failed a check** — `TestFailure` from a test method or from `configure()`. State = Fail. A `TestFailure` from `configure()` carries an extra ambiguity: the device did not reach a testable state, but whether the cause is hardware or infrastructure cannot be determined from code. The framework surfaces the `looking_for` / failure message; the human triager decides.
2. **Hardware lying** — survey artifacts expose mismatch between claimed and actual state.
3. **Test method bug** — a plain exception from a test method or from `configure()` → all failure modes modeled by the developer must raise `TestFailure`. Anything else raised is a bug. Full stop. A missing verdict (non-PASS return) is likewise a contract violation, rendered as node-local Aborted because no verdict exists to report.
4. **External integration failure** — `ExternalIntegrationFailure` from a `before_each`/`after_each` hook. State = Aborted.
5. **Operator interrupt** — `OperatorInterrupt` from SIGINT/SIGTERM during a live `run()`. State = Aborted. The failure names the operator and the signal.

**Developer obligation:** Use the right signal for the right category.
- Raise `TestFailure` for any verdict — from a test method or from `configure()` — never a plain exception.
- Use survey hooks to capture device-reported state at phase boundaries.
- Use `configure()` for environment/readiness gating. A timeout there is a `TestFailure` whose cause (hardware vs. infrastructure) is for the human to determine — the framework will not pretend to know.
- Let the framework raise `ExternalIntegrationFailure` — developers do not construct or raise it directly. It is raised automatically when a `before_each`/`after_each` hook throws anything.

---

## Evidence Contract
**Framework obligation:** Every run produces scoped, structured evidence automatically.
- Logging is scoped to the current leaf test.
- Survey and audit hooks share the same automatic artifact contract: every non-`None`
  return is faithfully recorded as an Artifact, including empty dicts, lists, and
  strings. Python resolves an explicit `return None`, a delegated call that returns
  `None`, and a fall-through to the same runtime value, so `None` writes only an
  informational log entry and no automatic artifact. When `None` itself is meaningful
  evidence, the SE must make that intent explicit with
  `await utils.capture_artifact(Artifact(..., data=None))` inside the hook.
  - Once a hook returns a non-`None` observation, its automatic artifact publication is
    shielded from shutdown within its own 30-second evidence budget. Repeated
    cancellation cannot orphan an accepted observation after the hardware was read.
  - Survey dispatch points are the six members of `Phase`, in order: `pre-configure`,
    `post-configure`, `pre-test`, `post-test`, `pre-cleanup`, and `post-cleanup`,
    limited to the phases the lifecycle reaches. The artifact name identifies the
    dispatch point and registered-hook index; audit names identify both the hook and
    interval dispatch. Both use artifact type `survey`, keeping one Inspector evidence
    contract.
  - `use_survey(fn, phases=...)` schedules a survey at a subset of those points; the
    default is all six, and an empty selection is a `ValueError` at registration. A
    boundary the survey is not scheduled at writes one debug line and no artifact, so
    the run directory holds one artifact per boundary the survey was meant to observe.
  - Order is not reach. The phases are totally ordered, but after a failed `configure`
    the engine skips `post-configure`, `pre-test`, and `post-test` and still fires
    `pre-cleanup` and `post-cleanup`, with `utils.exec` on the test host if `use_shell`
    never returned. A survey that reads the device therefore guards on
    `utils.exec_route`, not on the phase; the phase says when, the route says where.
- Log artifacts are co-located with their node.
- **Evidence travels with the verdict** (issue #118). A leaf's readings leave its test method as `PASS(evidence)` or `TestFailure(..., evidence=...)`. The child boundary validates the payload entry by entry before it is recorded: a value that is not JSON, a key that shadows a ctx entry or the framework-owned `measurements` slot, or a key `@evidence` declared that `PASS` did not carry is a `ContractViolation` naming the leaf and the key. Nothing is stripped silently. A failure is not held to the declaration; it records what it was handed.
- A leaf's `RunResult.run_data` is the JSON-safe view of its ctx plus the evidence its verdict carried, flat. A non-leaf node's `run_data` is the ctx view alone. Every `summary_tree` node carries `evidence` (what the leaf's verdict carried, or null) and `evidence_declared` (the `@evidence` declaration, or null); a declared key absent from `evidence` is a reading that was not taken, shown by name.
- All serializable values (primitives, plain dicts, lists) `configure()` returned appear in `run_data`, except under a key the suite's `redact_ctx` claims (issue #154): that key appears with the value `<REDACTED>`, whatever it holds. The claims read the real value from `utils.ctx`; the record does not carry it.
  - Functions, class instances, and live handles (e.g. SSH connections) are stripped from the ctx view; they are the objects a run works through, not evidence.
  - Context annotations do not bypass this filter. A `TypedDict` is an ordinary dict at runtime, and each value still has to satisfy the same serialization rules.
  - Filtering is all-or-nothing for each top-level ctx entry. A nested instance, handle,
    function, cycle, non-string mapping key, or non-finite float strips that complete
    entry; the framework never emits partial evidence or silently coerces it.
  - `run_data` is a detached snapshot taken when the node settles. An aborted leaf
    contributes no evidence; readings in hand when a time budget fires are lost by
    design, and the scoped log and point-in-time artifacts remain for in-flight
    observation.
  - `run_data["measurements"]` is framework-owned: the readings `utils.measure`
    recorded on this node, in call order, written when the node settles. An authored
    ctx entry with that name cannot populate or replace the slot, and a `run_data`
    override cannot drop it. Aggregation, SPC, and Cpk are the consumer's, not the
    record's.
- **Measurement Contract** (issue #86, `docs/measurements.md`). A limit is evidence,
  never a verdict: `utils.measure` records `in_limits` and touches neither the node's
  state nor its failure. A `None` value is admitted as a reading not taken (issue
  #159): recorded with `value: null`, never inside its window, so a claim gated on the
  window fails through its own gate. The framework refuses, as a `ContractViolation`
  naming the node and the measurement: an empty name, a `bool`, `NaN`, or infinite
  value, a non-text unit, or a window that bounds nothing or runs backwards; the same name
  recorded twice in one node; a call from outside `configure()` or the test method
  (`cleanup`, surveys, audits, hooks, or after the node settled); and `litmus.measure`
  where no node's slot is executing. The record is sealed on every terminal state,
  Aborted included, and published on the node's `run_data` and on every `summary_tree`
  node (`null` when the node never ran). A measurement does not satisfy an `@evidence`
  declaration.
- Every `RunResult` includes `ancestor_names: list[str]` (path from root) and `bread_crumbs: str` (joined for display), plus a `summary_tree` snapshot of the node's subtree state at completion. Downstream integrators can reconstruct the full Inspector tree from the root's RunResult alone.
- `TestFailure`, `ExternalIntegrationFailure`, and `OperatorInterrupt` all implement `to_json()`. Serializing a `RunResult` produces clean JSON for downstream integrators without any custom serializer.
- **Provenance.** Every executed node records one `exec routing: <where> (<why>)` line — `where` is `local` or `via <chain>`, `why` names the call that put it there (`declared via use_shell`, `inherited from parent`, `declared via use_shell(inherit=False)`). A node that never declared a route renders `where` alone. The line is emitted after `configure` returns, since `use_shell` lives there and the route is not final until it does.
- Every transport the router opens records its provenance lines as it comes up. SSH renders `ssh connect: <user>@<host>:<port> auth=<label> via=<chain>`; `via=direct` means the connection dials out from the test host itself. When host-key verification is skipped, SSH records a second line, `ssh host-key verification skipped: <host>:<port>`, so the Floor Technician can see that verification was off without opening the suite.
- Via chains are rendered **in full and test-host-first** (`bastion -> edge -> dut`), never summarized to the last hop.
- The credential path is named exactly — `key-file(/path)`, `agent(/socket)`, `password`, `key`, `none` — and the credential's *value* never appears. What is logged is what was dialed: the resolution order is declared once, in `SshInterface._auth_choice`, and both the connect options and the provenance line read it. A password, private-key bytes, and the contents of a key file never reach the scoped log, `run_data`, the env snapshot, `RunResult.to_json()`, or a failure repr.
- **A suite's own secret is held to the same standard** (issue #154). `redact_ctx` on the suite class names the ctx keys that are not for the record, exactly or as regular expressions matched against the whole key; a subclass adds to its bases' entries and never replaces them. A string under a redacted key is masked as `<REDACTED>` wherever it appears, run-wide: every scoped-log record, including one the author writes by hand; a failure's `str()`, `repr()`, and `to_json()`, down its cause chain, once the child boundary has recorded it; evidence on a verdict; captured artifacts; `run_data`; and therefore `RunResult.to_json()` and every file in the run directory. Only a string is a needle; a number or a handle under a redacted key is shown as `<REDACTED>` in the ctx view and nothing more. The env snapshot stays verbatim (issue #53): a secret that is also in the environment is kept off the record with `env_deny`. Masking begins when the value enters ctx, by any write path (`configure()`'s return, `[]=`, `update`, `setdefault`, `|=`), so a value logged inside `configure()` before it enters ctx is the author's to keep quiet. Masking is plain substring replacement, so a very short secret (a two-character token, a numeric sid such as `200`) also masks unrelated text that happens to contain it; that fails closed and is accepted.
- **Consequence:** the Floor Technician reconstructs the run's topology and its auth decisions from the artifacts alone, without opening the suite source.

**Developer obligation:** Instrument appropriately.
- Use `utils.logger` (never `print`) so evidence is captured in artifacts.
- Use `self.use_survey()` for periodic state snapshots; use
  `await utils.capture_artifact(artifact)` for point-in-time captures. `Artifact` is a
  write-free value on the public surface: the utility method is the only framework API
  that persists it, and it always emits the chronological pointer after publication.
- Return structured data from `use_survey` / `use_audit` hooks to capture it as an artifact.
- Hand readings back with the verdict: `PASS({...})` or `TestFailure(..., evidence={...})`. Declare with `@evidence(...)` what a pass must carry, beside the method that keeps the promise.

---

## Temporal Backbone Contract
**Framework obligation:** Every full `run()` creates one timestamped run directory
beneath the configured artifact root before the root lifecycle begins. Point-in-time
artifacts and every per-node timeline live beneath that directory, so a run's evidence
is self-contained and cannot overwrite a neighboring run.

- Every executed node that owns a discrete log streams the same records into two files:
  `timeline.log`, the human line view, and `timeline.jsonl`, the versioned Inspector
  record. Both files are flushed after every entry; an interrupted process therefore
  preserves every complete entry already accepted by the logger.
- The JSON Lines record carries one complete object per physical line with
  `format_version`, UTC `timestamp`, root-to-leaf `scope`, `level`, and `message`.
  Artifact-pointer records also carry `artifact_name`, `artifact_path`,
  `artifact_type`, and `artifact_timestamp`.
- A logging node opens its files lazily on its first record and closes them when that
  node reaches its terminal boundary. A declared node that never executes opens
  nothing.
- Authored suites, sequences, and parallel nodes own a log by default. Leaves roll up
  to the nearest owning ancestor. Sequences, parallel nodes, and composed suites may opt
  into the same roll-up with `discrete_log_file=False`; a root still owns the run log.
- `LitmusTest.log_path` exposes the owning node's absolute `timeline.jsonl` path after
  execution. It remains `None` for pending nodes and nodes whose records roll up.
- Every `summary_tree` node carries the same link as `log_path`, rendered as a POSIX
  path relative to the run directory, so the structural record reaches the temporal
  record without the live objects. It is `null` for pending nodes and nodes whose
  records roll up; the nearest ancestor with a link holds their chronology.
- On Pass, Fail, and Aborted, root `run()` atomically publishes the root
  `RunResult.to_json()` as `result.json` at the run-directory root before returning.
  The root timeline's final entry points to that file; a failed publication cannot
  expose a partial replacement or a pointer to evidence that was not published.

The persisted chronology and root result are framework-owned. Developer code continues
to use only `utils.logger` and `utils.capture_artifact()`; it never opens or edits
timeline or result files.

**Developer obligation:** Do not write directly into the run directory or depend on its
timestamped directory name. Treat `log_path` and captured artifact paths as opaque
evidence references.

The private `_run_lifecycle()` entry point is not a full run and therefore does not
create a timestamped run directory. Framework self-tests that invoke it directly may
leave point-in-time artifacts at the configured base root; authored suites use `run()`.

---

## Audit Contract
**Framework obligation:** When `self.use_audit(interval=..., fn=...)` is called in the constructor, the framework runs `fn` concurrently with the test methods on the specified interval.
- Multiple audit hooks are supported — each runs on its own interval.
- Every audit interval is validated during construction as positive and finite. An
  invalid registration cannot survive long enough to touch hardware.
- Audit start/stop is framework-controlled, not caller-controlled: the loop begins immediately before the first test method runs and ends immediately after the last test method returns. No delayed start, no manual trigger. The lifecycle contract (test in runnable state before `configure()` returns) guarantees the audit's preconditions are met by start.
- If the audit `fn` raises a `TestFailure`, the running test method is immediately cancelled and the test fails with the audit's failure.
- Audit errors are **not swallowed** — unlike survey hooks, a throwing audit is a real failure. Survey and cleanup errors are swallowed to protect the hardware verdict; `ExternalIntegrationFailure` is exempt from swallowing everywhere — swallowing it would kill the halt signal.
- Audit follows the same artifact contract as survey (see Evidence Contract above).
- Cancellation is cooperative and asyncio-native. The run-level abort (`RunAbort` in `lifecycle.py`) fires when an integration hook aborts the run; a throwing audit cancels the in-flight test method; test-method completion stops the audit loop. Suite code must let `asyncio.CancelledError` propagate — swallowing it breaks the wind-down.
- The read-only `utils.signal` combines the run-level integration halt with the nearest
  authored audit scope. Audit scope is inherited only by that node's composed suite: an
  audit failure cancels its own in-flight work and produces Fail without cancelling an
  independent parallel sibling. If an audit verdict and suite completion occur in the same
  event-loop turn, the audit verdict wins; evidence is not discarded by timing luck.
- Every started descendant cancelled by an audit still seals its result and dispatches
  its paired `after_each`. An unsettled descendant carries the audit's `TestFailure`
  class-intact; a descendant already dispatching `after_each` retains the verdict that
  hook received. Audit cancellation never becomes a run-wide integration halt.
- `utils.exec` threads cancellation to the underlying child process. When a cancellation fires, in-flight shell commands are killed (SIGTERM) immediately — no zombie processes outlive the test.

**Developer obligation:**
- Call `self.use_audit()` in the constructor with both `interval` and `fn` — both are required parameters.
- Yield to the event loop (`await`) inside any long-running loop in a test method so cancellation can be delivered, and never suppress `asyncio.CancelledError`.
- Use `use_audit` for active monitoring that can fail the test (e.g. thermal runaway, watchdog). Use `use_survey` for passive state snapshots at phase boundaries that never affect the verdict.
- Raise `TestFailure` from the audit `fn` to fail the test. Return data to continue — it is always recorded as evidence.

---

## Integration Hook Contract
**Framework obligation:** `before_each` and `after_each` are optional integration hooks registered at the root `run()` call. The framework threads them down to every executed descendant and dispatches them around every node's lifecycle.

- `before_each` fires immediately before a node's lifecycle begins.
- `after_each` fires immediately after a node's lifecycle completes, receiving the node's `RunResult`.
- **Pair invariant:** `after_each` fires EVEN IF `before_each` threw. An integrator writing a "started" row in `before_each` always gets a "finished" row in `after_each` — no orphan records in the external system.
- Throw from either hook → framework wraps as `ExternalIntegrationFailure` → the shared run-abort fires → every in-flight framework→user await across the tree unwinds at its own framework boundary → every in-flight transport is closed via `cancel_in_flight()` → enclosing node transitions to Aborted. The integrator's own error class arrives intact as the failure's cause. An operator SIGINT/SIGTERM during a live run takes this same halt path, with `OperatorInterrupt` as the reason.
- Hooks receive a deliberately narrow context (`BeforeEachContext` / `AfterEachContext`): node name, ancestor chain, breadcrumbs, scoped logger, env snapshot. NOT `exec`, `get_shell`, or `capture_artifact`. Integration hooks are for external coordination, not for exercising the hardware.
- Unpaired registration (only `before_each` or only `after_each`) emits a loud warning at run start unless `allow_unpaired_hooks=True` is passed. The pairing enforces the started/finished symmetry.

**Developer obligation:** Do not register `before_each`/`after_each` inside your `LitmusTest` subclasses. These hooks are the integrator's contract with the outside world, not part of the test's own lifecycle. Use `use_survey` / `use_audit` for per-test observation; use `before_each` / `after_each` only when composing Litmus into a larger harness.

---

## In-Flight Termination Contract
**Framework obligation:** When the run-level abort fires (an integration hook throws anywhere in the tree, or the operator sends SIGINT/SIGTERM to a live run), every framework-managed transport is closed HARD immediately — not at the next polling boundary. Cancelling the coroutine is not enough; the framework cancels the RESOURCE.

The operator-signal handler is installed by `run()`/`main()` only while that run is live, on the running loop, and removed on the way out so a library caller never leaks it. The first signal fires `RunAbort` with an `OperatorInterrupt` and hard-closes transports; a second signal inside the finalization window exits the process immediately.

- `SshInterface` destroys its underlying asyncssh connection, closing every open channel and failing every pending `exec` stream at the socket level.
- Recursively propagates through `via` chains — a jump-host tunnel and everything tunneled through it collapse together.
- `RemoteInterface` subclasses expose `cancel_in_flight()` for any transport type that needs aggressive close semantics.
- Long-running user operations (firmware flashes, long-polling requests, multi-step SSH scripts) in flight when the abort fires receive a dead resource, causing their own awaitables to fail and settle harmlessly in the event loop.

**Consequence:** A 30-minute firmware flash in progress when `before_each` throws does NOT continue to mutate the hardware. This is load-bearing for manufacturing test integrity — the halt signal means "stop touching the hardware," and the framework enforces that at the transport layer.

---

## Group Isolation Contract
**Framework obligation:** `TestGroup` runs its children concurrently under `asyncio.TaskGroup`, and the child boundary guarantees the TaskGroup semantics are correct: Fail and node-local Aborted are *returned* by the boundary, never raised — one child's failure cannot escape into the TaskGroup and cancel healthy siblings. Only a harness-level halt propagates, and the TaskGroup then cancelling everything is exactly right, because that is what Aborted means.

---

## Utils Contract
**Framework obligation:** `TestUtils` is the single, fully-scoped utility surface passed to every lifecycle method.

- `utils.exec` never throws. It returns an `ExecResult(stdout, stderr, exit_code, truncated, captured_bytes)` — a non-zero exit code is data for the test method to judge, not an exception. Signal-killed, aborted, and closed-transport commands settle as `exit_code=-1`. A `ConnectionError` from `RemoteInterface.exec` is absorbed here; the protocol stays loud, the author-facing surface stays total. `CancelledError` still propagates.
- Stdout and stderr together are bounded by one named `output_budget` (default 4 MiB), applied identically by the local executor and `SshInterface`. The budget is configured at the run seed (`LitmusTest.run(output_budget=...)` / `TestUtils(output_budget=...)`) or at `LocalExec` / `SshInterface` construction — never as a process-global. Exceeding it terminates the command the same way abort does (SIGTERM the host process group; SIGTERM then close that SSH channel, session stays up) and settles with the bytes actually received, `truncated=True`, and `captured_bytes` equal to what was kept. Truncation is evidence, never an exception. The scoped log names it (`exec output truncated: captured N bytes (budget B)`). Streaming reads preserve interrupted output: decode happens only at settlement, with replacement for a cut in the middle of a UTF-8 sequence.

**Developer obligation:** Use only `utils.*` inside lifecycle methods — never import framework internals directly into test logic.

---

## Exec Routing Contract
**Framework obligation:** `use_shell` is the only writer of the node's exec router. `get_shell` may open extra endpoints and may read `inherit` to pick a tunnel; it never assigns the router or the propagate flag.

- Four `use_shell` shapes: connection (`host` + `username`), connection with explicit `via`, policy-only `inherit=False` (force local), and bare `use_shell()` (return the active handle).
- Explicit `via` always wins. `inherit=False` on a connection form skips the parent's hop.
- **Declared in `configure()`, nowhere else.** A declaring `use_shell` (`host`, `via`, `inherit=False`, `propagate=False`) is admitted only while this node's own `configure()` is running; from a test method, `cleanup`, a survey, an audit, or a helper they call, it raises `ContractViolation` before the router is touched. The `exec routing:` line is written when `configure` returns and the FT reads it as the truth for the node's whole run; a route moved later would make that line a lie for every command after the move. The bare `use_shell()` reads the active handle and may be called from any slot.
- **A second declaration.** `configure()` may declare more than once, since no command has yet run under the route being replaced: open a jump host, then route through it. The last declaration is the route; every shell opened on the way stays `opened` on this node and is not torn down early.
- **Ownership.** The node whose `configure()` opened a shell, through `use_shell` or `get_shell`, owns it. The framework closes it at that node's cleanup (after the post-cleanup survey, before the hop it tunnels through), never earlier and never from another node.
- **Hooks and audits observe the route; they do not set it.** They read `utils.exec_route` and exec through what `configure()` committed to. A survey runs at phases before `configure()` and after `cleanup()`, and an audit runs concurrently with the test method, so neither has a moment at which one route could be declared once. `get_shell` stays open to them: an auxiliary endpoint changes nothing the routing line states.
- `propagate=False` on `use_shell` stops this node's router from flowing to children; they start local. `propagate` on `get_shell` is a documented no-op.
- **The parent owns the route.** A route is owned by the node whose `configure()` declared it and is inherited by every node that node composes, down the tree, until a descendant declares `inherit=False` (or the owner declared `propagate=False`). A device class never takes a routing argument: a gateway is a parent whose `configure()` opens the hop and whose `build_suite()` composes the devices behind it, and those devices are routed by being its children. The `LitmusTest` constructor has no `via=`; the only `via` is the tunnel argument to `use_shell` / `get_shell`, declared inside `configure()` by the node that owns the route (issue #137).
- Children inherit the route through run params (`RouterRunParams`), never through ctx. ctx is instance-local domain data, not infrastructure.
- Routed tunnels are borrowed; the router owns hop lifecycle. Every hop the router resolves — an explicit `via=` Shell or an inherited route — is already live, so the connector threads it through the transport's borrowed `tunnel=` channel, never the owned `via=` one. The opened interface must not disconnect, cancel, or collapse its hop. What a node opened is recorded on the node (`opened`), stays connected through the node's post-cleanup survey, and closes after that survey and before the hop it tunnels through — the parent's hop closes at the same point of the parent's own lifecycle, after every child is done with it (issue #115), so a survey that execs at post-cleanup reads the device, not a closed transport; a hard abort on the hop kills everything tunneled through it at the socket (In-Flight Termination Contract). A dead hop at open time is an error, not something the router silently brings up.

- Local exec and every SSH session this router opens share the run's `output_budget` (see the Utils Contract). A fat command is truncated and killed at the resource; it does not take the rest of the rack with it.
- A transport's address is fixed while it is live. `connect(host, port)` re-points an addressed transport only through a closed handle; asking a live one for a different address is refused, never silently replaced. After a flash or DHCP churn the flow is `disconnect()`, reacquire, `connect(new_ip)`. There is no self-healing: a socket the framework killed stays dead (In-Flight Termination Contract), and an unexpected drop reaches the author as a `ConnectionError` on the next `exec`, not as a quiet redial.
- `utils.exec_route` states where `utils.exec` lands at the moment it is read: `local` for the test host, otherwise the endpoint of the hop the route attaches to (the last name in the `exec routing:` chain). It is derived from the router, never stored, so it is right in a survey at every phase, including the pre-configure dispatch before `use_shell` has run and the pre-cleanup dispatch after a `configure` that failed before it.
- The router logs what it opens and where every node's exec lands (see the Evidence Contract). A transport that hides its hop hides the topology: `endpoint`, `hop`, and `route_label` on `RemoteInterface` are how a transport states its place in the chain, and `provenance_lines` (starting with `connect_provenance`) are the lines the router records for it.

**Developer obligation:** Declare in `configure()`, and only there, where this node's `utils.exec` lands when it should be somewhere other than the test host; a test method, survey, or audit that needs a different endpoint reads `use_shell()` or opens one with `get_shell`. Do not stuff SSH handles into ctx and expect routing to notice.

---

## Device API Contract
**Framework obligation:** `DeviceApiInterface` is a contract, not an `exec`
subclass ([HIL ruling 1](hil/index.md#the-three-rulings)). The guarantees live
in the signed [Device API design note](hil/device-api.md); this page does not
restate them.

`DeviceRequest(..., json=value)` is the JSON body convenience (issue #155): it
serializes once to UTF-8 `payload` bytes and supplies `Content-Type: application/json`
unless the request already declares a content type, matched case-insensitively.
`json=None` sends JSON null; omitting `json` leaves the raw-payload API unchanged.
Combining `json` with a non-`None` `payload`, even empty bytes, is a `ValueError` before
any transfer. Non-serializable values and non-finite numbers are refused at construction.
The request never mutates the caller's headers or retains its JSON object.

`DeviceResponse.json()` decodes the returned bytes. `parse(response_type="json")`
is the format-selecting form; `response_type="yaml"` uses `yaml.safe_load` from the
optional `litmus[yaml]` extra. Unknown formats raise `ValueError`, a missing YAML
dependency raises `ImportError`, and parser errors propagate without a fallback.
Parsing is explicit, independent of response status and headers; it preserves `body`
and `status`, makes no request, and supplies no verdict. A malformed body remains a
completed transfer for the suite to judge. Body contents never enter attempt logs.

**Developer obligation:** Implement `DeviceApiInterface` as that note specifies.

---

## Composability Contract
**Framework obligation:** Any `LitmusTest` or subclass thereof can be composed indefinitely in any shape the SE deems appropriate.

**Developer obligation:** Leaves must be self-contained — declare their own readiness preconditions in `configure()`, not in the parent sequence.
