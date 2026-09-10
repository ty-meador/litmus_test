# Changelog

All notable changes to litmus are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version numbers follow
the policy in `docs/api-stability.md`. Before 1.0.0 nothing is promised; the trail is
kept so that it exists before the promise does. Add a notable change in the MR that
delivers it; at release, move the completed entries into a dated version section and
leave a fresh Unreleased section above it. Use the
[release MR checklist](.gitlab/merge_request_templates/Release.md) to make the cut.

## Unreleased

The initial Python migration is unreleased. The six foundation phases below summarize
merged behavior using today's API names, with links to the MRs that established it;
they are not six published versions. Subsequent entries include the hardening and
refinement merged through [!108](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/108)
on 2026-09-06. The TypeScript repositories remain behavioral references, not a promise
of feature parity: the Python Inspector is still separate work.

### Added

- Internal Inspector artifact navigation (issue #89): opens the requested numbered
  capture, groups by recorded type in first-appearance order, and preserves chronology
  order within each type. Comparison targets stay distinct; a collision swaps the
  two sides, including for a two-capture group. Targets retain their original pointer
  and decoding kind for the existing contained reader. A moved 70-DUT proof reads
  captures and the root result without suite execution or evidence changes. Modal
  loading, content paging/diff rendering, terminal screens, latest-run discovery, and
  the entry point remain later work.
- Device API (CoAP/HTTP) implementation (issue #119, signed contract #97):
  `DeviceApi`, `DeviceRequest`, `DeviceResponse`, and
  `DeviceApiOperationFailure` on the public surface; `TestUtils.get_http` and
  `get_coap` open a session. HTTP 4xx/5xx is data. Retries default to zero and
  each attempt is evidenced. Declared methods are GET, PUT, POST and DELETE;
  PATCH is refused at the call site as undeclared (signed note #97, decision 7).
  Abort closes the session. Mock, HTTP/1.1, and
  CoAP CON/ACK backends share one contract suite. Attempt logs withhold every
  header value unless the author opts that name into `evidence_headers` — a
  secret-name allowlist cannot keep credentials out of artifacts. Core install
  unchanged (stdlib asyncio).
- Device API (CoAP/HTTP) signed interface design note (issue #97): typed
  request/response, status-as-data, retries as evidence, closed-session
  abort, deny-by-default request-header values (`evidence_headers`
  opt-in; CoAP names are registered option names, not numbers),
  response option values never recorded by the framework, and a
  refusal to treat HTTP as `exec` with a URL. Design only;
  no Device API implementation or public Python name is introduced.
  Signed by Ty in !130 on 2026-09-09; recorded on issue #97. PATCH stays
  undeclared and refused at the call site; declaring it is a later note.
- `docs/hil/i2c-spi.md`: the signed design note for I2C and SPI transaction
  contracts — atomic transfers, I2C NACK/arbitration, SPI chip-select and
  mode, and a released bus as the abort safe-state. Design only; no code
  change. Signed by Ty in !129 on 2026-09-09; recorded on issue #96.
- `docs/hil/can.md`: the signed design note for a CAN bus interface contract —
  typed frames, immutable acceptance filters, bus-off as an operational
  condition, and silence as the abort safe-state. Design only; no code
  change. Signed by Ty in !128 on 2026-09-09; recorded on issue #95.
- Internal Inspector timeline presentation (issue #89): tail-first paging, errors-only
  filtering that keeps decoding and availability diagnostics, and stable capture
  numbers across the complete loaded chronology. Paging, filtering, and resizing never
  change a number's target or edit the saved records. A moved 70-DUT proof resolves
  numbered captures through the existing contained reader without suite execution.
  Artifact target navigation is now implemented as described above. Modal loading,
  content paging/diff rendering, terminal screens, latest-run discovery, and the entry
  point remain later work.
- Internal Inspector saved-run loader (issue #89): establishes an original run root
  from one final root-scoped result publication, then relocates captures through the
  existing contained reader. Missing, damaged, ambiguous, or misplaced publication
  evidence preserves the tree and chronology with an explicit unavailable mapping.
  Operator selections share the tree reader's resolution and diagnostic boundary;
  a pending root cannot supply chronology or relocation from a damaged recorded link.
  Disagreement between result and tree roots names the result defect explicitly.
  Tested against a moved 70-DUT run without suite execution or evidence changes;
  screens, latest-run discovery, and the entry point remain later increments.
- Internal Inspector selection model (issue #89): Inspect and Failures navigation
  loads the selected node's contained timeline, clearing the previous chronology
  before I/O. Each load has its own identity, so delayed completion cannot replace
  a newer selection's evidence. Pending nodes render not run; missing logs remain
  explicitly unavailable. Tested from a moved 70-DUT result without suite execution;
  screens and the entry point remain later increments.
- Internal Inspector timeline containment (issue #89): reads recorded run-relative
  log paths beneath the opened run, retains the complete path after relocation, and
  reports unavailable references without reading another run's chronology. Timeline
  and capture readers now share checked, cached packaged schemas. Screens and the
  entry point remain separate increments.
- Internal Inspector saved-tree model (issue #89): schema-validated result loading,
  declaration-order rows, shared Fail/Aborted navigation, and nearest-ancestor log
  ownership. Pending nodes never borrow chronology, duplicate names retain distinct
  indices, and missing declared readings remain named. Operator selections resolve
  symlinks and `..` before establishing the run root, without relaxing recorded-reference
  containment. Tested from a moved 70-DUT result without live suite execution;
  terminal screens remain a later increment.
- Internal Inspector capture reader (issue #89): reads schema-validated artifact
  envelopes and root results beneath the opened run, preserves full relative paths
  under an explicit relocation mapping, and reports unavailable evidence without
  substituting captures from the original run. Publishes the existing version-1
  artifact envelope schema; shared strict JSON parsing keeps both Inspector readers
  consistent. Screens and the entry point remain later work.
- `litmus[pylsp]` extra: a `python-lsp-server` plugin over the `litmus_check`
  registry. Every `--list-rules` finding is a diagnostic on the open buffer;
  `litmus/gated-pass` wraps an unconditional `return PASS` (derived from
  contiguous, call-free guards, otherwise an `if False:` scaffold that cannot
  pass) and `litmus/method-reference` lifts a trivial lambda to the method
  reference. Core install unchanged (issue #78).
- PWM interface design note (issue #94), signed by Ty in
  [!125](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/125#note_3800005090)
  on 2026-09-08: representable frequency/duty transfers, bounded linear duty
  ramps, exclusive channel ownership, and disabled-output termination with
  explicit unconfirmed outcomes. Design only; no PWM implementation or public
  Python name is introduced.
- Optional context typing: `LitmusTest[CtxT]` and `TestUtils[CtxT]` carry a suite's own
  `TypedDict` into `utils.ctx`, so a misspelled key is a pyright error at the author's
  desk instead of a contract-violation Fail on the floor (issue #75). Opt-in and
  defaulted: the
  bare names still mean `Mapping[str, Any]`, composition and `via` stay shape-agnostic,
  and the runtime context object is unchanged.

- Internal Inspector timeline reader (issue #89): preserves valid JSON Lines in file
  order with explicit truncation, decoding, version, and availability diagnostics.
  Validates against the published timeline schema packaged with the installation;
  `jsonschema` is now a runtime dependency. This is the log-pane reading boundary,
  not a shipped terminal UI or an execution adapter.
- Inspector target specification review rulings (issue #89): Inspect and Failures
  ship independently of a separately approved Run adapter; ancestor-configure replay
  and suite reconstruction from saved evidence are prohibited. Relocated artifacts
  preserve their full run-relative path, with explicit unavailable diagnostics instead
  of basename substitution. Specification only; the Inspector remains unimplemented.
- `make mutants`: a `mutmut` mutation audit over the kernel of trust (`verdict.py`,
  `base.py`, `lifecycle.py`), configured in `[tool.mutmut]` and kept out of `make check`.
  STANDARDS §6 records the standing rule: every surviving mutant gets a killing test or
  a written equivalence reason in the MR that ran the audit. The first audit closed its
  survivors with tests on deadline-cancellation ownership, deferred and parameterized
  `Verdict` annotations, and the node named by composition-time violations (issue #69).
- `litmus/shell-in-configure`, the tier-3 rule that flags `utils.use_shell(...)` with
  arguments outside `configure()`, seeded in the fixture and listed in the rule table
  with its tier-1 counterpart (issue #76).
- `docs/hil/serial.md`: the design note for a serial/UART console transport as a
  `RemoteInterface` peer — prompt detection, timeouts, line discipline,
  exit-status recovery via a sentinel, output bounding, safe-state on abort, and
  the `via`-hop composition that reaches a remote console over SSH. Design only;
  no code change. Awaiting Ty's sign-off on the note before an implementation MR
  opens (issue #91).
- `docs/hil/index.md` and `docs/hil/gpio.md`: the HIL interface design-note index
  and the GPIO interface design note (issue #93), fixing a pin
  direction/level/debounce contract, abort safe-state (every declared output
  pin driven to its idle level), and a pin-settle error taxonomy. Debounce
  names a stability window and an acquisition deadline separately; a node
  deadline idles only the pins its own subtree opened, never the whole bench;
  `PinSettleFailure` is a `LitmusError`, not a `TestFailure`; and `PinLevel`
  carries `UNAVAILABLE` for a controller that could not be reached. Signed by
  Ty on 2026-09-07. Design-only; no code and no public-surface change.
- `RunEnv`, the type of `utils.env`: still a read-only mapping, now with `require`,
  `text`, and `port` readers that Fail the run naming the bench variable to set. The
  SSH-backed examples use them in place of per-file helper functions.
- **Phase 0 — Skeleton and kernel of trust.** Python 3.14 with uv, ruff, pyright
  strict, and pytest; the identity-checked `PASS` sentinel, structured failure taxonomy,
  and runtime child boundary. A non-PASS return is node-local Aborted, never an implicit
  pass. The Python overview, contracts, and idioms carry the migration's requirements
  ([!1](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/1),
  [!5](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/5),
  [!9](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/9)).
- **Phase 1 — The test atom.** `LitmusTest` owns configure → build_suite → cleanup,
  freezes authored instance state after construction, and checks subclass structure
  before execution. Bound methods compose as tests, sequences, and parallel nodes;
  `asyncio.TaskGroup` preserves sibling verdict isolation. Context stays local to its
  authored instance, and cleanup survives abort and repeated cancellation
  ([!13](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/13),
  [!17](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/17),
  [!19](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/19),
  [!21](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/21),
  [!23](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/23),
  [!24](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/24),
  [!25](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/25)).
- **Phase 2 — Utilities and evidence.** Per-node `TestUtils` and scoped logging,
  `wait_for(looking_for=...)`, passive surveys, active audits, and point-in-time
  artifacts with chronological pointers. Detached `run_data` and recursive
  `summary_tree` snapshots preserve evidence without live suite objects. The polling
  example demonstrates the complete trail
  ([!33](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/33),
  [!34](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/34),
  [!37](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/37),
  [!38](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/38),
  [!41](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/41),
  [!42](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/42),
  [!65](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/65)).
- **Phase 3 — Transports and routing.** Framework-managed local execution and
  asyncssh connections, borrowed SSH tunnels, inherited routes, and via-device
  lifecycles. Routing and authentication provenance identify where a command ran;
  `utils.exec` returns command outcomes as data while cancellation propagates.
  Remote-device and tunneling examples exercise the authoring surface
  ([!12](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/12),
  [!14](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/14),
  [!20](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/20),
  [!22](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/22),
  [!28](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/28),
  [!36](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/36),
  [!39](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/39),
  [!44](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/44),
  [!64](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/64)).
- **Phase 4 — Integration hooks and run abort.** Root `before_each` and `after_each`
  dispatch around every executed node through narrow contexts. Hook failure preserves
  the pair, propagates `ExternalIntegrationFailure` class-intact, and hard-closes
  in-flight transports across the tree. Tests cover all main/audit/halt race orderings
  and a transport command stopped during abort
  ([!46](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/46),
  [!48](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/48),
  [!49](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/49),
  [!63](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/63),
  [!69](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/69)).
- **Phase 5 — Rack validation and static analysis.** A 70-DUT mock rack checks
  concurrency, isolation, and abort behavior. The `litmus-check` registry and CLI add
  gated-PASS, pure-composition, immutable-self, constructor-hook, and interface
  annotation rules as defense in depth over the runtime contracts. The pi-hole
  dogfood suite carries the end-to-end authoring example
  ([!55](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/55),
  [!56](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/56),
  [!57](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/57),
  [!58](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/58),
  [!59](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/59),
  [!66](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/66),
  [!71](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/71),
  [!72](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/72)).
- Durable per-run `timeline.log` and versioned `timeline.jsonl`, per-node log links
  in the summary tree, and atomic root `result.json` publication for Pass, Fail,
  and Aborted. The final root timeline entry points to the published result
  ([!77](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/77),
  [!79](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/79),
  [!86](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/86)).
- SIGINT/SIGTERM produce an operator-named Aborted result and hard-close transports;
  a second signal during finalization exits immediately. Combined stdout/stderr
  capture has a per-run byte budget and records truncation as evidence
  ([!84](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/84),
  [!85](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/85)).
- Verification gates: repeated full-suite soak, ratcheting branch coverage floors,
  locked-dependency audit and refresh tooling, and nightly soak/rack reporting.
  Transport fault injection, shared transport contracts, the abort-phase matrix,
  and generated composition properties strengthen the self-tests. The nightly
  reporting implementation does not itself establish fourteen green nights
  ([!45](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/45),
  [!74](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/74),
  [!75](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/75),
  [!83](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/83),
  [!87](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/87),
  [!88](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/88),
  [!93](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/93),
  [!102](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/102)).
- Additional language-server rule migrations: `litmus/raise-test-failure`,
  `litmus/no-verdict-in-cleanup`, and `litmus/method-reference`; runnable network
  resolution, hardware lifecycle, and computed run-data examples
  ([!90](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/90),
  [!91](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/91)).
- GPL-2.0-only license and notice
  ([!78](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/78)).
- Release MR checklist with validation evidence, unresolved distribution prerequisites,
  changelog cut, and the handoff to Zavala for merge and release review (issue #101).
- Docs site: `make docs` builds `docs/` with mkdocs and an API reference from
  the public surface's docstrings. The gate fails on a broken cross-reference.
  A version tag publishes the site to GitLab Pages
  (`https://tyler-meador.gitlab.io/litmus_test/`); the Floor Technician guide
  there is the URL that belongs on a station label (issue #82,
  [!108](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/108)).
- `litmus new SuiteName`: writes a gated-PASS suite and a matching self-test stub
  (`SuiteName.py` / `test_suite_name.py` derived from the class name). The generated
  suite has an annotated constructor, `configure` returning a narrowed ctx, a pure
  `build_suite`, one gated test method, and `cleanup`. `--directory DIR` selects the
  destination; `--force` overwrites. The files pass `litmus-check`, ruff, and pyright
  strict unmodified. Teaching examples are verified against that core shape rather
  than regenerated from it. `docs/litmus-new.md` (issue #79).
- `docs/floor-technician.md`, the station page: how to read `result.json` without
  reading suite source, the Fail / Aborted / bug-signal recovery protocol, and
  every Inspector JSON field explained from the Floor Technician's side. Linked
  from the README and `docs/overview.md`; published on the docs site
  (issue #83, issue #82).
- `litmus run <path-or-module>[::SuiteClass]`, the runner: loads a suite file or
  module, selects one `LitmusTest` subclass, runs it, and exits 0 Pass, 1 Fail,
  2 Aborted, 3 usage error. `--artifact-root DIR` overrides `LITMUS_ARTIFACT_ROOT`,
  `--json` prints the `format_version` 1 result record, `--junit PATH` writes the
  JUnit XML report. The exit contract and the record are public; `docs/litmus-run.md`
  (issue #73).
- `docs/api-stability.md`: the public surface is `litmus.__all__`, semantic versioning
  from 1.0.0, the deprecation procedure, and a reference row for every public name.
  `tests/test_public_surface.py` holds the code to the document (issue #100).
- `litmus.__version__`, the installed distribution's version.
- Exported from the package root after the `__all__` audit: `JsonObject`, `Shell`,
  `State`, `NodeType`, `Slot`, `TimeoutEvidence`, `RunSignal`, `ScopedLogger`. Each was
  already reachable from a public object or named by the documentation; none is new
  behaviour. `RemoteInterface`, `SshInterface`, and `JunitXml` stay off the surface on
  purpose; the policy says why.
- `docs/schema/result-v1.schema.json` and `docs/schema/timeline-v1.schema.json`, the
  published Inspector record schemas, with the versioning policy in
  `docs/inspector-json.md` (issue #92).
- `litmus.junit.JunitXml`, the JUnit XML projection of a `RunResult` (issue #87).
- `Phase`, the ordered `StrEnum` of the six survey dispatch points, and
  `use_survey(fn, phases=...)` to schedule a survey at a subset of them; the default
  is all six and an empty selection is a `ValueError` at registration. A skipped
  boundary writes one debug line and no artifact. `TestUtils.exec_route` states where
  `utils.exec` lands when it is read: `local`, or the endpoint of the hop the route
  attaches to. The phase is the schedule and the route is the guard, because after a
  failed `configure` the engine still fires `pre-cleanup` and `post-cleanup` with exec
  on the test host (issue #116).
- `litmus-check` rule `litmus/ctx-provenance`: every `utils.ctx["k"]` read in a test
  method, hook, `configure`, or `cleanup` is provided by `configure()`'s return or
  written earlier in the same method; a sibling leaf's write does not count (issue #112).
- `docs/measurements.md`: the design note for the parametric measurements protocol —
  `utils.measure(name, value, *, unit=None, limits=None)`, the `measurements` array the
  reserved `run_data` slot will carry, the evidence-not-verdict rule for limits, and the
  export contract the downstream SPC/Cpk consumer needs. Barrelled at the package root
  as `litmus.measure` per Ty's condition on the approval; `utils.measure` stays the
  documented home. No code changes yet — `#86` implements the note (issue #85).

### Changed

- `use_shell` declares this node's exec route only while `configure()` is running:
  a declaring call (`host`, `via`, `inherit=False`, `propagate=False`) from a test
  method, `cleanup`, a survey, or an audit raises `ContractViolation` before the
  router is touched, so the `exec routing:` line written after `configure` is the
  route every command of the node ran under. The bare `use_shell()` read and
  `get_shell` are unchanged. The Exec Routing Contract now states the window, what a
  second declaration inside `configure` means, and who owns an opened shell
  (issue #76).
- The nightly soak draws and records a fresh Hypothesis seed for each pytest run,
  extending generated composition coverage while retaining a replay command in every
  run log. The ordinary gate and eight-run race soak keep their fixed seed
  (follow-up to [!102](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/102)).
- The pi-hole dogfood example reads the Pi-hole v6 log layout (`FTL.log` and
  `webserver.log` under `/var/log/pihole/`), schedules its survey from
  `post-configure` onward behind an `exec_route` guard so no tail runs on the test
  host, carries each claim's reading on the verdict, and Fails on an unreadable
  kernel log instead of passing on its empty output. Every example under `examples/`
  now carries teaching comments that give the reasoning behind each pattern.
- **Evidence travels with the verdict (issue #118).** `PASS` is callable: `return PASS({...})`
  carries a leaf's readings, `TestFailure(..., evidence=...)` carries them on the fail
  path, and `@evidence(...)` declares what a pass must carry; a pass without its declared
  evidence is a `ContractViolation`, never a green run. `utils.ctx` is read-only once
  `configure()` returns, so inputs come in through configure and readings go out with
  the verdict. A leaf's `run_data` is the ctx view plus its evidence; every summary-tree
  node carries `evidence` and `evidence_declared` (additive under format version 1).
  `Verdict` is a public class, `EvidenceViolation` names the three evidence breaks, the
  snapshot helpers moved from `litmus.evidence` to `litmus.run_data`, and `litmus-check`
  gained `litmus/evidence-fulfilled` while `litmus/ctx-provenance` now flags every ctx
  write. `ArpResolver.reacquire` returns the new address instead of writing ctx.

- A node halted by an integration hook now finishes its abort — hard close, paired
  `after_each`, sealed Aborted result — even when a parallel sibling's cancellation lands
  mid-close. The started row an integrator wrote in `before_each` was being left open when
  the throwing node had a live sibling; the sequence shapes under test never raced one
  (issue #117).
- The Inspector `type` discriminator speaks the composition vocabulary: `suite`, `test`,
  `parallel`, `sequence` replace `test-suite`, `test-case`, `test-group`,
  `test-sequence`, and the `NodeType` members are `SUITE`, `TEST`, `PARALLEL`,
  `SEQUENCE`. Changed in place under result `format_version` 1 by ruling: no consumer
  exists before 1.0.0, so no version roll (issue #111).
- A shell a node opened with `use_shell` / `get_shell` now stays connected through that
  node's post-cleanup survey and closes after it, before the via lease is released. It
  used to close inside cleanup, so a post-cleanup survey on a self-owned hop settled
  `remote interface is not connected` and recorded an empty artifact while the same
  survey on an inherited via hop read the device (issue #115).
- The five locally executable examples run through `litmus run`: their constructor
  parameters carry the lesson's values as defaults, and the standalone wrapper classes
  and `__main__` blocks are gone (issue #73). The SSH-backed examples initially kept
  theirs; those targets now resolve from `utils.env` in `configure()`, and the
  SSH examples also use `litmus run`. Missing target configuration produces a
  modeled Fail naming the variable to set (issue #113,
  [!105](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/105)).
- A `[]` read of a `utils.ctx` key that `configure()` did not provide raises
  `ContractViolation` ("context insufficient") naming the test and the key, instead of a
  bare `KeyError`; `get`, `in`, and `setdefault` are unchanged. `TestUtils(ctx=...)` now
  copies the seed into the run's context rather than adopting the dict (issue #112).
- Composition vocabulary settled as `self.test`, `self.sequence`, `self.parallel`;
  `self.case` and `self.group` removed outright, and `litmus-check` gained
  `litmus/retired-names` to point at the replacement (issue #110).
- Per-node time budgets: `fail_after` and `abort_after` on any node, subtree-bound and
  tighten-only (issues #59, #60,
  [!81](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/81)).
- `configure()` accepts a `Mapping[str, Any]` return annotation so suite-local
  `TypedDict` overrides type-check; runtime context remains a mutable dict
  ([!68](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/68)).
- Author-facing framework errors name the violated contract and the concrete fix,
  including missing verdicts, malformed composition, and finalization timeouts
  (issue #80, [!103](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/103)).

### Security

- Optional `env_allow` / `env_deny` filtering of the published environment snapshot;
  SSH credential containment tests pin that password and private-key values do not
  escape into logs, results, or other evidence
  ([!51](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/51),
  [!82](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/82)).

### Removed

- `self.case` and `self.group` (issue #110). Pre-1.0: no deprecation period.
