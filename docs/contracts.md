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
- `configure()` — put the test in a known state. Readiness gates and polling live here. Its framework return annotation is `dict[str, Any]`; a suite may narrow it to a local `TypedDict`. The returned value is always an ordinary dict whose entries merge into `utils.ctx` for test methods, cleanup, and all descendant tests.
- `build_suite()` — pure table-of-contents composition. Return a `LitmusTest` built from `self.case()` / `self.sequence()` / `self.group()`. Test methods (private class methods passed to `self.case()`) carry the actual verdict logic — return the `PASS` sentinel to pass, raise `TestFailure` to fail intentionally.
- `cleanup()` — restore state. Must not throw (errors are swallowed and logged).

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

1. **Pass** — a test method returned the `PASS` sentinel. The check is `result is PASS` — identity, not equality. Nothing else can forge it.
2. **Fail** — a test method raised a `TestFailure` (or subclass): the DUT produced a verdict and the verdict was negative. OR the developer didn't model a failure mode: a plain exception → BUG-signaled Fail.
3. **Aborted** — no verdict was produced. Peer to Pass/Fail, **NOT** a flavor of failure. Two causes:
   - **Harness-level:** a `before_each`/`after_each` integration hook threw; the error is wrapped as `ExternalIntegrationFailure` and the run halts before a verdict could be produced.
   - **Node-local:** a test method returned anything other than the `PASS` sentinel — `None` fall-through or an explicit wrong value (`MissingVerdict`). No verdict exists, so the framework refuses to forge Fail (or Pass) on its behalf. Siblings are unaffected. *(Deliberate deviation from the TypeScript original, which folded this into the bug/Fail path.)*

**Developer obligation:** Recognize the taxonomy. A `TestFailure` means "the DUT failed a check"; an `ExternalIntegrationFailure` means "the harness around the test broke." Never conflate them — the Floor Technician's recovery protocol depends on knowing which.

**Framework obligation:** When a descendant's hook throws, the `ExternalIntegrationFailure` propagates up through every ancestor class-intact. Sequences and groups do not rewrap it. Every ancestor's state cascades to Aborted.

---

## Failure Taxonomy Contract
**Framework obligation:** Every failure must be traceable to one of the following root causes, made obvious by signal:
1. **DUT failed a check** — `TestFailure` from a test method or from `configure()`. State = Fail. A `TestFailure` from `configure()` carries an extra ambiguity: the device did not reach a testable state, but whether the cause is hardware or infrastructure cannot be determined from code. The framework surfaces the `looking_for` / failure message; the human triager decides.
2. **Hardware lying** — survey artifacts expose mismatch between claimed and actual state.
3. **Test case bug** — a plain exception from a test method or from `configure()` → all failure modes modeled by the developer must raise `TestFailure`. Anything else raised is a bug. Full stop. A missing verdict (non-PASS return) is likewise a contract violation, rendered as node-local Aborted because no verdict exists to report.
4. **External integration failure** — `ExternalIntegrationFailure` from a `before_each`/`after_each` hook. State = Aborted.

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
  - Survey dispatch points are `pre-configure`, `post-configure`, `pre-test`,
    `post-test`, `pre-cleanup`, and `post-cleanup`, limited to the phases the lifecycle
    reaches. The artifact name identifies the dispatch point and registered-hook index;
    audit names identify both the hook and interval dispatch. Both use artifact type
    `survey`, keeping one Inspector evidence contract.
- Log artifacts are co-located with their test case.
- All serializable values (primitives, plain dicts, lists) added to `ctx` are automatically added to `run_data`.
  - Functions, class instances, and live handles (e.g. SSH connections) are stripped.
  - Context annotations do not bypass this filter. A `TypedDict` is an ordinary dict at runtime, and each value still has to satisfy the same serialization rules.
  - Filtering is all-or-nothing for each top-level ctx entry. A nested instance, handle,
    function, cycle, non-string mapping key, or non-finite float strips that complete
    entry; the framework never emits partial evidence or silently coerces it.
  - `run_data` is a detached snapshot taken after cleanup. Later ctx mutation cannot
    rewrite evidence already captured in a `RunResult`; serializable cleanup evidence
    is included.
  - `run_data["measurements"]` is a framework-owned empty list reserved for future
    parametric measurements. An authored ctx entry with that name cannot populate or
    replace the slot. Limit evaluation, aggregation, SPC, and Cpk are deliberately out
    of scope; the reserved slot must not be mistaken for an implemented statistical
    process-control model.
- Every `RunResult` includes `ancestor_names: list[str]` (path from root) and `bread_crumbs: str` (joined for display), plus a `summary_tree` snapshot of the node's subtree state at completion. Downstream integrators can reconstruct the full Inspector tree from the root's RunResult alone.
- `TestFailure` and `ExternalIntegrationFailure` both implement `to_json()`. Serializing a `RunResult` produces clean JSON for downstream integrators without any custom serializer.
- **Provenance.** Every executed node records one `exec routing: <where> (<why>)` line — `where` is `local` or `via <chain>`, `why` names the call that put it there (`declared via use_shell`, `inherited from parent`, `declared via use_shell(inherit=False)`). A node that never declared a route renders `where` alone. The line is emitted after `configure` returns, since `use_shell` lives there and the route is not final until it does.
- Every transport the router opens records one line as it comes up. SSH renders `ssh connect: <user>@<host>:<port> auth=<label> via=<chain>`; `via=direct` means the connection dials out from the test host itself.
- Via chains are rendered **in full and test-host-first** (`bastion -> edge -> dut`), never summarized to the last hop.
- The credential path is named exactly — `key-file(/path)`, `agent(/socket)`, `password`, `key`, `none` — and the credential's *value* never appears. What is logged is what was dialed: the resolution order is declared once, in `SshInterface._auth_choice`, and both the connect options and the provenance line read it.
- **Consequence:** the Floor Technician reconstructs the run's topology and its auth decisions from the artifacts alone, without opening the suite source.

**Developer obligation:** Instrument appropriately.
- Use `utils.logger` (never `print`) so evidence is captured in artifacts.
- Use `self.use_survey()` for periodic state snapshots; use
  `await utils.capture_artifact(artifact)` for point-in-time captures. `Artifact` is a
  write-free value on the public surface: the utility method is the only framework API
  that persists it, and it always emits the chronological pointer after publication.
- Return structured data from `use_survey` / `use_audit` hooks to capture it as an artifact.
- Populate `ctx` with structured evidence during lifecycle methods so that values appear in `run_data` automatically.

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
  independent group sibling. If an audit verdict and suite completion occur in the same
  event-loop turn, the audit verdict wins; evidence is not discarded by timing luck.
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
- Throw from either hook → framework wraps as `ExternalIntegrationFailure` → the shared run-abort fires → every in-flight framework→user await across the tree unwinds at its own framework boundary → every in-flight transport is closed via `cancel_in_flight()` → enclosing node transitions to Aborted. The integrator's own error class arrives intact as the failure's cause.
- Hooks receive a deliberately narrow context (`BeforeEachContext` / `AfterEachContext`): node name, ancestor chain, breadcrumbs, scoped logger, env snapshot. NOT `exec`, `get_shell`, or `capture_artifact`. Integration hooks are for external coordination, not for exercising the hardware.
- Unpaired registration (only `before_each` or only `after_each`) emits a loud warning at run start unless `allow_unpaired_hooks=True` is passed. The pairing enforces the started/finished symmetry.

**Developer obligation:** Do not register `before_each`/`after_each` inside your `LitmusTest` subclasses. These hooks are the integrator's contract with the outside world, not part of the test's own lifecycle. Use `use_survey` / `use_audit` for per-test observation; use `before_each` / `after_each` only when composing Litmus into a larger harness.

---

## In-Flight Termination Contract
**Framework obligation:** When the run-level abort fires (integration hook throws anywhere in the tree), every framework-managed transport is closed HARD immediately — not at the next polling boundary. Cancelling the coroutine is not enough; the framework cancels the RESOURCE.

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

- `utils.exec` never throws. It returns an `ExecResult(stdout, stderr, exit_code)` — a non-zero exit code is data for the test method to judge, not an exception.

**Developer obligation:** Use only `utils.*` inside lifecycle methods — never import framework internals directly into test logic.

---

## Exec Routing Contract
**Framework obligation:** `use_shell` is the only writer of the node's exec router. `get_shell` may open extra endpoints and may read `inherit` to pick a tunnel; it never assigns the router or the propagate flag.

- Four `use_shell` shapes: connection (`host` + `username`), connection with explicit `via`, policy-only `inherit=False` (force local), and bare `use_shell()` (return the active handle).
- Explicit `via` always wins. `inherit=False` on a connection form skips the parent's hop.
- `propagate=False` on `use_shell` stops this node's router from flowing to children; they start local. `propagate` on `get_shell` is a documented no-op.
- Children inherit the route through run params (`RouterRunParams`), never through ctx. ctx is instance-local domain data, not infrastructure.
- Routed tunnels are borrowed; the router owns hop lifecycle. Every hop the router resolves — an explicit `via=` Shell or an inherited route — is already live, so the connector threads it through the transport's borrowed `tunnel=` channel, never the owned `via=` one. The opened interface must not disconnect, cancel, or collapse its hop. What a node opened is recorded on the node (`opened`) and closes before the hop it tunnels through; a hard abort on the hop kills everything tunneled through it at the socket (In-Flight Termination Contract). A dead hop at open time is an error, not something the router silently brings up.

- A transport's address is fixed while it is live. `connect(host, port)` re-points an addressed transport only through a closed handle; asking a live one for a different address is refused, never silently replaced. After a flash or DHCP churn the flow is `disconnect()`, reacquire, `connect(new_ip)`. There is no self-healing: a socket the framework killed stays dead (In-Flight Termination Contract), and an unexpected drop reaches the author as a `ConnectionError` on the next `exec`, not as a quiet redial.
- The router logs what it opens and where every node's exec lands (see the Evidence Contract). A transport that hides its hop hides the topology: `endpoint`, `hop`, and `route_label` on `RemoteInterface` are how a transport states its place in the chain, and `connect_provenance` is the line the router records for it.

**Developer obligation:** Call `use_shell` when this node's `utils.exec` should land somewhere other than the test host. Call `get_shell` for auxiliary endpoints. Do not stuff SSH handles into ctx and expect routing to notice.

---

## Composability Contract
**Framework obligation:** Any `LitmusTest` or subclass thereof can be composed indefinitely in any shape the SE deems appropriate.

**Developer obligation:** Test cases must be self-contained — declare their own readiness preconditions in `configure()`, not in the parent sequence.
