# API Stability

1.0.0 is a promise. This document states what the promise covers, how version numbers
carry it, how a name leaves the surface, and where every change is announced. The
developer reads it to know which names are safe to build a station on and how much
warning a change carries. Suites written for 1.0 keep running on every 1.x, so the
Floor Technician's stations do not break on an upgrade.

The self-tests hold the framework to this document (`tests/test_public_surface.py`):
the reference table below and `litmus.__all__` must name the same set, every example
and every documented code block imports only that set from the package root, and
`litmus.__version__` reports the installed distribution.

## The public surface

**`litmus.__all__` is the public surface. Everything else is private.**

- A name is public if and only if it appears in `litmus.__all__`. Import it from
  `litmus`; the module a name is defined in is an implementation detail and may move
  without notice. `from litmus import LitmusTest` is a promise; `from litmus.base
  import LitmusTest` is not.
- The documented attributes, methods, parameters, and raised exceptions of a public
  name are part of the promise. "Documented" means named in this document, in
  `docs/contracts.md`, `docs/idioms.md`, `docs/inspector-json.md`,
  `docs/junit-xml.md`, or `docs/litmus-check.md`, or in the name's own docstring.
- A public object has no attribute of a private type. Every value a suite can read
  from a public object carries a public type, so the reader can follow every name back
  to its meaning. `ScopedLogger` is promised as its `logging.LoggerAdapter` base plus
  the documented `scope` and `bread_crumbs` additions.
- `litmus.__version__` is public: the installed distribution's version string.
- The invariants in `docs/contracts.md` are promises in their own right. A change that
  weakens one is a breaking change even when no name moves.
- The records the framework emits (`result.json`, `timeline.jsonl`, the point-in-time
  artifact envelope) are promised through their published JSON Schemas and
  `format_version`, under the policy in `docs/inspector-json.md`.
- Three environment variables are public configuration, and the framework reads no
  others: `LITMUS_ARTIFACT_ROOT`, the artifact root when `TestUtils` is given none;
  `environment=DEBUG`, which publishes every variable despite an `env_allow` or
  `env_deny` list and logs a warning saying so (`docs/inspector-json.md`); and
  `SSH_AUTH_SOCK`, the SSH agent `get_shell` and `use_shell` fall back to when no
  credential is given. The run environment as a whole is published as evidence under
  `env_allow` / `env_deny`, not read as configuration.
- The `litmus run` command line is public in two respects: its exit status contract
  (`0` Pass, `1` Fail, `2` Aborted, `3` usage error) and its `--json` record, which is
  the `result.json` record under the same `format_version` policy
  (`docs/litmus-run.md`). The `litmus.cli` module behind it is not.
- The `litmus new` command line is public in two respects: the files it writes (a
  gated-PASS suite and a matching self-test stub) and its exit status (`0` wrote the
  files, `3` usage error). The `litmus.scaffold` module behind it is not
  (`docs/litmus-new.md`).
- The `litmus inspect` command line is public in three respects: its selection rule
  (PATH is a run directory or its `result.json`; no PATH opens the most recently
  modified `result.json` under `LITMUS_ARTIFACT_ROOT`, default `artifacts/`), its
  exit status (`0` the Inspector was closed, `3` nothing to inspect, not a saved
  version-1 run, or a `--suite` that cannot be loaded), and the rule that the Re-Run
  tab executes only the suite the run recorded as `suite_target` or the one `--suite`
  names, never one derived from evidence. The screen behind it,
  `litmus.inspector_app`, and the re-run adapter, `litmus.inspector_rerun`, are not
  (`docs/inspector.md`).
- The `litmus-check` command line is public in two respects: its exit status contract
  and the rule codes it ships (`docs/litmus-check.md`). The Python API of the
  `litmus_check` package is not.

Private, with no promise attached:

- Any name beginning with an underscore, at any depth.
- Every submodule path. `litmus.lifecycle`, `litmus.routing`, `litmus.executor`,
  `litmus.timeline`, `litmus.mock`, and the rest are the framework's organs; the
  self-tests import from them, suites do not.
- Annotation-only aliases that appear in signatures but are not exported
  (`WaitPredicate`, `HaltReason`, `ExecFn`). They are documented by their expansion.
- Directory and file names beneath the artifact root. `LitmusTest.log_path` and the
  `result` pointer in the timeline are the API; the layout that produces them is not.
- The transports, `RemoteInterface`, `SshInterface`, and `SerialInterface`. A suite
  author never constructs an SSH transport: `TestUtils.use_shell` and
  `TestUtils.get_shell` open the connection and hand back a `Shell`. Serial is
  constructed directly until a `use_shell` shape exists for a device path. Transport
  authors and the self-tests import `litmus.remote`, `litmus.ssh`, and `litmus.serial`
  directly; a transport contract for third parties is a design note of its own
  (issues #91, #93–#97).
- The Device API backends, `DeviceApiInterface`, `HttpInterface`, `CoapInterface`,
  and `MockDeviceApi`. A suite author never constructs one: `TestUtils.get_http`
  and `TestUtils.get_coap` open the session and hand back a `DeviceApi`. Self-tests
  import `litmus.device_api` directly.
- `litmus.junit.JunitXml`. The runner owns the reporter: `litmus run --junit PATH`
  is the promised way to a JUnit report. A harness adapter that imports
  `litmus.junit` directly does so without the promise, as `docs/junit-xml.md` says.
- The self-test fixtures, the loopback sshd, and `MockTransport`. A transport double
  for suite authors is a feature to be designed, not a side door into the test kit.

## Version numbers

The distribution version follows [Semantic Versioning 2.0.0](https://semver.org):
`MAJOR.MINOR.PATCH`.

**Before 1.0.0 (`0.y.z`)** nothing is promised. A name may be renamed or removed in any
release, without a deprecation period; the composition vocabulary ruling on issue #110
(`case` and `group` removed outright, a `litmus-check` rule naming the replacement) is
the precedent. Every such change still lands in `CHANGELOG.md` under the release that
makes it, so the trail exists before the promise does.

**From 1.0.0** a release bumps exactly one component:

| Component | Bumped when | Examples |
|---|---|---|
| **MAJOR** | A promise is broken. | A name leaves `__all__`; a documented parameter, attribute, or return type changes incompatibly; a contract in `docs/contracts.md` weakens; a `format_version` bumps; `requires-python` rises; a `litmus-check` rule code is removed or renamed. |
| **MINOR** | The surface grows or a deprecation is announced. | A new name in `__all__`; a new optional keyword parameter; a new field on a record within its `format_version`; a new `litmus-check` rule; a name marked deprecated. |
| **PATCH** | Behaviour is corrected without changing the surface. | A bug fix that brings behaviour in line with a documented contract; a dependency bump that changes nothing a suite can observe; documentation. |

Two consequences the developer can rely on:

- **A fix is not a break.** When a documented contract and the code disagree, the
  document is the promise and the code is the bug. Correcting it is a PATCH even if a
  suite depended on the wrong behaviour.
- **A `format_version` bump is a MAJOR.** The Inspector and the harness that read a
  station's records are part of the station. A new record version that an installed
  consumer refuses is a break, whatever the Python surface did.

Dependencies are private. `asyncssh` and anything else the framework imports may change
major version in a MINOR or PATCH release as long as nothing a suite can observe
changes; a dependency that becomes observable (a type in a public signature) becomes
part of the promise at that moment and is documented as such.

## Deprecation

A public name is never removed in the MAJOR line that introduced it. It is deprecated
first, and removed no sooner than the next MAJOR release and no sooner than two MINOR
releases after the deprecation was announced. Both conditions hold at once.

Announcing a deprecation is one MR that does all of the following:

1. **The old spelling keeps working**, with the documented behaviour, for the whole
   deprecation period.
2. **Calling it emits `DeprecationWarning`** through `warnings.warn` with
   `stacklevel=2`, so the warning points at the suite's line, not the framework's. The
   message names the replacement and the release that will remove the old spelling.
3. **The reference table marks it** with `Deprecated in x.y, removed in z.0` and the
   replacement, and the section that documents the name says the same.
4. **`CHANGELOG.md` records it** under *Deprecated* for the release that announces it,
   and under *Removed* for the release that removes it.
5. **`litmus-check` gains a rule** that flags the deprecated spelling and names the
   replacement, in the pattern of `litmus/retired-names`, so the finding reaches the
   developer's CI before it reaches a station log.

The developer sees the warning in the self-tests of their own suite (pytest surfaces
`DeprecationWarning` by default) and in `litmus-check` output; `litmus run` routes
Python warnings through the run log so the station's console carries the notice as
well.

A removal at the next MAJOR deletes the code and the rule, moves the table row to the
changelog's *Removed* list, and stops there: a removed name is not reused for something
else within the following MAJOR line.

## The changelog

`CHANGELOG.md` at the repository root follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/): an *Unreleased* section at
the top, one section per release below it, entries grouped under *Added*, *Changed*,
*Deprecated*, *Removed*, *Fixed*, and *Security*.

Every MR that changes a public name, a documented contract, a record format, or a
`litmus-check` rule adds its line to *Unreleased* in the same MR. A release renames
*Unreleased* to the version and date and opens a fresh *Unreleased* above it. The
changelog is the announcement; the GitLab issue is the ruling behind it.

## Changing the surface

A change to the public surface is a change to a promise, and the workflow in
`STANDARDS.md` section 7 applies: it is raised as an issue for Ty's sign-off before
its MR opens, stating how the change helps the developer help the Floor Technician.
The MR then updates `litmus.__all__`, the reference table below, the section that
documents the name, and `CHANGELOG.md` together. The public-surface self-test refuses
an MR that updates one without the others.

## Reference

Every name in `litmus.__all__`, what it is, and where its contract is written. *Kind*
is checked against the object by the self-tests.

| Name | Kind | What it is | Described in |
|---|---|---|---|
| `PASS` | constant | The verdict. `return PASS` passes with no evidence; `return PASS({...})` passes and carries the readings that prove it. | `docs/overview.md` (Why Litmus Rejects "Pass by Default"), `docs/idioms.md` (Evidence Travels With the Verdict), `docs/contracts.md` (Terminal State Contract) |
| `AfterEachContext` | class | The narrow integration-hook context after a node settles: `BeforeEachContext` plus `result`. | `docs/idioms.md` (Integration Hooks), `docs/contracts.md` (Integration Hook Contract) |
| `AfterEachHook` | type alias | `Callable[[AfterEachContext], Awaitable[None]]`; the `after_each` parameter of `LitmusTest.run`. | `docs/idioms.md` (Integration Hooks) |
| `ArpResolutionFailure` | class | `TestFailure` raised when the resolver cannot map a MAC address to an IP. | `docs/idioms.md` (ArpResolver — MAC to IP) |
| `ArpResolver` | class | Resolve a MAC address to an IP through the node's current exec route; `mac`, `resolve`, `reacquire`, `ping`. | `docs/idioms.md` (ArpResolver — MAC to IP, ArpResolver with Via-Routing) |
| `Artifact` | class | Immutable point-in-time evidence (`name`, `type`, `data`, `timestamp`) for `TestUtils.capture_artifact`. | `docs/idioms.md` (Point-in-Time Artifacts), `docs/inspector-json.md` |
| `BeforeEachContext` | class | The narrow integration-hook context before a node runs: `name`, `ancestor_names`, `bread_crumbs`, `logger`, `env`. | `docs/idioms.md` (Integration Hooks), `docs/contracts.md` (Integration Hook Contract) |
| `BeforeEachHook` | type alias | `Callable[[BeforeEachContext], Awaitable[None]]`; the `before_each` parameter of `LitmusTest.run`. | `docs/idioms.md` (Integration Hooks) |
| `ContractViolation` | class | A developer bug: suite code did something the verdict contract forbids. Records as Fail with `bug: true`. | `docs/overview.md` (Failure Taxonomy), `docs/contracts.md` (Failure Taxonomy Contract) |
| `DeviceApi` | class | The session handle returned by `TestUtils.get_http` and `TestUtils.get_coap`; `request`, `scheme`, `base_url`. | `docs/idioms.md` (Device API — CoAP/HTTP), signed `docs/hil/device-api.md` |
| `DeviceApiOperationFailure` | class | `LitmusError` raised when a device-API transfer does not complete. Not a DUT verdict; HTTP 4xx/5xx is data on `DeviceResponse`. | `docs/idioms.md` (Device API — CoAP/HTTP), signed `docs/hil/device-api.md` |
| `DeviceRequest` | class | One resource operation: `method`, `path`, `payload` bytes, `headers`, retry declaration, `evidence_headers`. | `docs/idioms.md` (Device API — CoAP/HTTP), signed `docs/hil/device-api.md` |
| `DeviceResponse` | class | What a transfer returned: `status`, `body` bytes, `headers`. Status is data. | `docs/idioms.md` (Device API — CoAP/HTTP), signed `docs/hil/device-api.md` |
| `EvidenceViolation` | class | `ContractViolation` raised by the child boundary when a verdict's evidence is not JSON, shadows a ctx key, or lacks a key `@evidence` declared; `key`. | `docs/idioms.md` (Evidence Travels With the Verdict), `docs/contracts.md` (Evidence Contract) |
| `ExecResult` | class | What a command produced: `stdout`, `stderr`, `exit_code`, `truncated`, `captured_bytes`. `exec` never throws. | `docs/contracts.md` (Utils Contract) |
| `ExternalIntegrationFailure` | class | A harness-level halt: an integration hook threw. Aborts the run. | `docs/overview.md` (Failure Taxonomy), `docs/contracts.md` (Integration Hook Contract) |
| `Failure` | type alias | The union of `TestFailure`, `ExternalIntegrationFailure`, `OperatorInterrupt`, and `MissingVerdict`; the type of `RunResult.failure`. | `docs/contracts.md` (Failure Taxonomy Contract) |
| `FrameworkContractViolation` | class | A framework bug: the second layer of the guard, unreachable by design. Exported so a log line can be followed. | `docs/contracts.md` (Failure Taxonomy Contract) |
| `JsonObject` | type alias | `dict[str, JsonValue]`; the shape of `run_data`, of a node's `evidence`, and of `Artifact.data`. | `docs/idioms.md` (run_data — Automatic Structured Evidence) |
| `LitmusError` | class | Root of the framework's exception taxonomy. | `docs/contracts.md` (Failure Taxonomy Contract) |
| `LitmusTest` | class | The atom: a claim about hardware and the procedure that produces its evidence. `configure`, `build_suite`, `cleanup`, `run`, `main`, `test`, `sequence`, `parallel`, `use_survey`, `use_audit`, `name`, `log_path`, `run_data`. Optionally parameterized as `LitmusTest[CtxT]` to type the suite's own context; the bare name keeps today's meaning. | `docs/overview.md` (The Atom), `docs/idioms.md`, `docs/contracts.md` (Lifecycle Contract) |
| `Measurement` | class | One `utils.measure` call as recorded: `name`, `value`, `unit`, `limits`, `in_limits`, `timestamp`. Frozen; `in_limits` is evidence, never a verdict. | `docs/measurements.md` (The call) |
| `MissingVerdict` | class | No verdict was produced: the test method returned something other than `PASS`. | `docs/overview.md` (Failure Taxonomy), `docs/contracts.md` (Terminal State Contract) |
| `NodeType` | class | `StrEnum` discriminator on `RunResult.type` and `SummaryTreeNode.type`: `SUITE`, `TEST`, `PARALLEL`, `SEQUENCE`; the wire values are fixed by the result schema. | `docs/inspector-json.md` (Version 1 result) |
| `OperatorInterrupt` | class | A harness-level halt: the operator interrupted the run. Aborts the run. | `docs/overview.md` (Failure Taxonomy), `docs/contracts.md` (Failure Taxonomy Contract) |
| `Phase` | class | Ordered `StrEnum` of the six survey dispatch points, `PRE_CONFIGURE` through `POST_CLEANUP`; the `phases=` selection of `use_survey` and the artifact-name prefix. | `docs/idioms.md` (Constructor-Only Hooks), `docs/contracts.md` (Evidence Contract) |
| `RunEnv` | class | The run environment on `TestUtils.env`: a read-only `Mapping[str, str]` plus `text`, `require`, and `port`, readers that Fail the run naming the bench variable to set. | `docs/idioms.md` (TestUtils Injection), `docs/litmus-run.md` (Selecting the suite) |
| `RunResult` | class | Self-contained evidence for one settled node: `state`, `failure`, `timeout`, `ancestor_names`, `bread_crumbs`, `summary_tree`, `env`, `run_data`, `to_json`. | `docs/idioms.md` (RunResult Navigation), `docs/inspector-json.md`, `docs/contracts.md` (Evidence Contract) |
| `RunSignal` | class | Read-only view of the halt signals affecting a node: `fired`, `reason`. `TestUtils.signal`. | `docs/idioms.md` (TestUtils Injection) |
| `ScopedLogger` | class | The per-node logger on `TestUtils.logger` and the hook contexts; a `logging.LoggerAdapter` with `scope` and `bread_crumbs`. | `docs/idioms.md` (Logging), `docs/contracts.md` (Evidence Contract) |
| `Shell` | class | The scoped exec handle returned by `TestUtils.use_shell` and `TestUtils.get_shell`; the `via` argument to both. | `docs/idioms.md` (SSH Device, SSH Tunneling), `docs/contracts.md` (Exec Routing Contract) |
| `Slot` | class | `StrEnum` of the lifecycle positions the framework calls suite code from; `RunResult.slot`. | `docs/inspector-json.md` (Version 1 result), `docs/contracts.md` (Lifecycle Contract) |
| `State` | class | `StrEnum` of the three terminal states, `PASS`, `FAIL`, `ABORTED`; `RunResult.state`. There is no fourth. | `docs/overview.md` (The Three Terminal States), `docs/contracts.md` (Terminal State Contract) |
| `SummaryTreeNode` | class | Plain recursive snapshot of the run tree on `RunResult.summary_tree`; `evidence`, `evidence_declared`, `to_json`. | `docs/inspector-json.md` (Summary tree) |
| `TestFailure` | class | A verdict of Fail: the hardware did not meet the spec. The exception a test method raises; `evidence=` carries the readings taken before it failed. | `docs/overview.md` (Failure Taxonomy), `docs/contracts.md` (Failure Taxonomy Contract) |
| `TestUtils` | class | The scoped service object handed to every lifecycle method: `logger`, `ctx`, `env`, `signal`, `artifact_root`, `exec_route`, `exec`, `wait_for`, `capture_artifact`, `get_arp_resolver`, `use_shell`, `get_shell`, `get_http`, `get_coap`, `measure`. Optionally parameterized as `TestUtils[CtxT]` so `ctx` returns the suite's declared shape. | `docs/idioms.md` (TestUtils Injection), `docs/contracts.md` (Utils Contract) |
| `TimeoutEvidence` | class | Structured evidence when a time budget cut a subtree: `reason`, `declared`, `budget_seconds`, `elapsed_seconds`, `clock`; `RunResult.timeout`. | `docs/idioms.md` (Time Budgets), `docs/contracts.md` (Time Budget Contract) |
| `Verdict` | class | The return type of a test method: `PASS` or `PASS(evidence)`; `evidence`. Generic in the evidence shape, so `-> Verdict[MyEvidence]` lets pyright check the payload. | `docs/overview.md` (Why Litmus Rejects "Pass by Default"), `docs/idioms.md` (Evidence Travels With the Verdict), `docs/litmus-check.md` (`litmus/unannotated-interface`) |
| `evidence` | function | Decorator naming the keys a test method's `PASS` must carry: `@evidence("k", ...)` or `@evidence(MyTypedDict)`. Static at class definition. | `docs/idioms.md` (Evidence Travels With the Verdict), `docs/litmus-check.md` (`litmus/evidence-fulfilled`) |
| `measure` | function | Package-root spelling of `utils.measure`, resolved to the node whose `configure()` or test method is running and refused outside one. `utils.measure` is the documented home. | `docs/measurements.md` (The package-root alias) |
