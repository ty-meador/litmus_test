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

- `TestUtils.use_serial` and `TestUtils.get_serial`, `use_shell` / `get_shell` for a console
  reached by its device path (bench finding F2, issue #139). `use_serial(device=...)` in
  `configure()` opens the console and binds `utils.exec` to it for the node and its
  descendants; `use_serial()` reads the active route; `get_serial(device=...)` opens an
  auxiliary console. The port joins the node's opened set, so the post-cleanup sweep and
  the run-level abort sweep close it. Behind a hop the console borrows the router's live
  SSH session (`SerialInterface(tunnel=...)`). `litmus/shell-in-configure` and
  `litmus/no-input-fallback` recognise the new names.
- `DeviceApi` serialises requests per handle (bench finding F13, issue #139): `parallel`
  leaves sharing the session `configure()` opened queue in call order instead of dying on
  `ValueError: one in-flight request per handle`. A queued request's `timeout` bounds only
  its own transfer; one still waiting when the handle is cancelled or closed is never sent
  and fails with `DeviceApiOperationFailure`. The `ValueError` remains only for re-entrancy.
  The `ContractViolation` banner for a non-`TestFailure` exception that escapes a node now
  names the exception instead of advising `return PASS`.
- `litmus/no-input-fallback`, the tier-3 rule the #142 ruling named and Ty ruled into
  1.0.0 on issue #144 (issue #147). Inside `configure()` of a `LitmusTest`, a `self`
  attribute and a discovery utility (`get_arp_resolver`, `use_shell`, `get_shell`,
  `get_http`, `get_coap`) may not stand as alternatives for one value: an `or` chain, a
  conditional expression, or a local name bound from each by a different assignment
  (`if`/`else`, `try`/`except`, a re-assignment after a `None` check) is reported with
  the attribute and the utility named. Identity fed into discovery and identity read
  from `utils.env` are the blessed shapes and stay quiet, as are the same shapes outside
  `configure()`. The rule guards a style, not a verdict; its row in
  `docs/litmus-check.md` says so rather than inventing a runtime counterpart.
- Doctrine: the node names what it needs, the framework decides where it comes from
  (issue #142, Ty's ruling of 2026-09-12; recorded under issue #143). A constructor
  argument is identity fixed at composition time; runtime state comes from `utils` in
  `configure()` as one call with no fallback; anything a parent and a child could both
  supply belongs in `TestUtils`, which inherits it down the tree and records the
  provenance. The paragraph sits under "The Parent Owns the Route" in `docs/idioms.md`.
  The `examples/` audit the ruling asked for found no constructor-or-discovery
  fallback; the identity-or-`utils.env` read a root node needs is the run's declared
  input and stays.
- The Inspector's Re-Run tab (issue #89, Ty's ruling of 2026-09-10). Inspect stays the
  archive of the production run and executes nothing; Re-Run shows the same tree
  unhydrated, and Enter on any node executes that subtree with the code that ran in
  production, its fresh log in the pane below. Re-run evidence is scratch: a node's
  previous re-run is deleted when it is re-run again and the directory goes when the
  Inspector closes. Tab is held during a live re-run; `q` takes the operator-interrupt
  path and quits once the node settles Aborted. The suite comes from the new
  `suite_target` field `litmus run` records in `result.json` (absolute file or module,
  `::`, class) or from `litmus inspect --suite`; a suite whose declared tree differs from
  the saved one is refused by path. Behind it, `LitmusTest.run(subtree=...)` runs one
  declared node through the sealed lifecycle with every leaf outside it left pending,
  and `run(suite_target=...)` records the provenance. The Failures tab is folded into
  Inspect as `h`, which hides passing rows; the legacy tree glyphs (`├─`, `└─`, `│`) and
  the cyan owner arrow on the row whose timeline the highlighted node rolled into are
  back on both tabs.
- `SerialInterface`, a `RemoteInterface` peer for a serial/UART console (issue #91).
  A local device node via `pyserial-asyncio` (`litmus[serial]` extra; core install
  unchanged) or the same transport through an owned SSH `via` hop. Prompt
  detection, optional login sequence, CRLF/echo/ANSI line discipline, a
  `__LITMUS_RC__` exit-status sentinel, `BoundedCapture` output bounding, and
  BREAK/Ctrl-C/close abort. Off `litmus.__all__`. The shared transport-contract
  suite gains a pty-backed `serial_peer`.
- Package metadata for discovery (issue #103): keywords, trove classifiers, and project
  URLs (docs site, source, issue tracker, changelog) in `pyproject.toml`, with the README
  already the long description. The name stays as it is until #9 rules at 1.0.0.
- `make build` and `make smoke` (issue #102): the gate now builds the wheel and sdist
  with `uv build`, installs the wheel into a clean venv under `.smoke/`, and starts both
  console entry points from there. A package that cannot build or import is red before
  it is tagged. The publish job stays out until the name decision (#9) and CI return.
- `litmus-check` rule `litmus/measurement-name-unique` (issue #132, raised by the #109
  sweep): two `measure(...)` calls, either spelling, with the same literal name inside one
  `configure` or test method are reported at the second call. The tier-1 duplicate-name
  guard is unchanged; the rule moves that finding from the first run to the editor.
- `litmus-check` rule `litmus/measurement-in-node` (issue #133, raised by the #109 sweep):
  a `measure(...)` call, either spelling, inside `cleanup`, a registered survey or audit,
  a `before_each`/`after_each` hook, or outside any function is reported with the slot it
  sits in. The tier-1 outside-a-node guard is unchanged. `SuiteModule.registered_methods`
  now carries the registrar lookup the ctx-provenance rule kept privately.
- `litmus inspect [PATH]` (issue #89, Ty's ruling of 2026-09-09): opens a saved run in
  the Inspector. PATH is a run directory or its `result.json`; without PATH the most
  recently modified `result.json` under `LITMUS_ARTIFACT_ROOT` (default `artifacts/`;
  `--artifact-root DIR` overrides) opens, the legacy discovery rule carried over.
  Nothing under the root, or a selection that is not a saved version-1 run, is exit 3
  with the reader's diagnostic on stderr and nothing opened in its place; no suite code
  runs. With this the Inspector's acceptance list is on `main` in full; the Run tab
  stays behind its own decision.
- Parametric measurements (issue #86, signed note #85): `utils.measure` records a
  number with a unit and an inclusive window beside the verdict and returns a frozen
  `Measurement`; `litmus.measure` is the same call resolved to the running node. The
  node's own `run_data["measurements"]` fills, and every `summary_tree` node carries
  `measurements` (`null` when it never ran), under result `format_version` 1 unbumped;
  `docs/schema/result-v1.schema.json` gains `$defs/measurement`. A limit is evidence,
  never a verdict: `in_limits` is recorded, the state is untouched, and an
  out-of-window reading logs at `warning`. Four framework guards refuse a malformed
  reading, a duplicate name, a call outside `configure()` or the test method, and the
  alias outside a running node. The pi-hole dogfood records `ssh_round_trip_ms` with
  an upper limit and gates on it in plain sight.
- Inspector main view (issue #89): `litmus.inspector_app.InspectorApp`, a Textual
  screen over one saved run. A tab row (Inspect, Failures), the structural tree above
  with Pass, Fail, Aborted, and Pending rendered as distinct states and readings not
  taken named on the row, and the highlighted node's chronology below, loaded on
  cursor movement without Enter and labelled with its owner when the node's records
  rolled up. The guide's keys: `q` and `Ctrl-C` quit, `Tab` cycles the two tabs, arrows
  move and jump between Fail and Aborted nodes without wrapping, `n`/`p` page, `f`
  filters to errors while reader diagnostics such as a truncation marker stay in
  place. Every key handler is one model call and a redraw; the plain text of each
  redraw is kept on the app so the self-tests and a screenshot can be read back.
  `textual` becomes a runtime dependency by Ty's ruling of 2026-09-09 (MIT; the tree is
  GPL-2.0 compatible). The Run tab stays behind its own decision.
- Inspector artifact modal screen (issue #89): `litmus.inspector_app.ArtifactScreen`.
  A digit in the log pane opens the capture carrying that number; the modal owns the
  guide's artifact keys until `b` closes it (`[` `]` types or the left side, arrows
  captures or the right side, `n` `p` page both sides together, `d` compare). One
  capture renders as its lines, a comparison as two aligned columns with differing
  rows highlighted, an unreadable capture as the reader's diagnostic with the recorded
  reference. Every key is one call into the modal model.
- Internal Inspector artifact modal model (issue #89): loads the selected capture,
  or the two sides of a comparison, through the contained capture reader and pages
  the result. A capture is printed in its recorded key order; an unavailable or
  undecodable one shows the reader's diagnostic with the recorded reference and is
  never replaced by another file. A comparison aligns the two captures row by row so
  one page cursor moves both sides together; a singleton type stays single without a
  reload; a delayed read never replaces a newer selection. `tests/inspector_rack.py`
  now records the shared 70-DUT run every Inspector test opens. The main view screen
  is now implemented as described above.
- Internal Inspector artifact navigation (issue #89): opens the requested numbered
  capture, groups by recorded type in first-appearance order, and preserves chronology
  order within each type. Comparison targets stay distinct; a collision swaps the
  two sides, including for a two-capture group. Targets retain their original pointer
  and decoding kind for the existing contained reader. A moved 70-DUT proof reads
  captures and the root result without suite execution or evidence changes.
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
  Artifact target navigation, modal loading, content paging/diff rendering, and the
  main view screen are now implemented as described above.
- Internal Inspector saved-run loader (issue #89): establishes an original run root
  from one final root-scoped result publication, then relocates captures through the
  existing contained reader. Missing, damaged, ambiguous, or misplaced publication
  evidence preserves the tree and chronology with an explicit unavailable mapping.
  Operator selections share the tree reader's resolution and diagnostic boundary;
  a pending root cannot supply chronology or relocation from a damaged recorded link.
  Disagreement between result and tree roots names the result defect explicitly.
  Tested against a moved 70-DUT run without suite execution or evidence changes;
  the screens are now implemented as described above.
- Internal Inspector selection model (issue #89): Inspect and Failures navigation
  loads the selected node's contained timeline, clearing the previous chronology
  before I/O. Each load has its own identity, so delayed completion cannot replace
  a newer selection's evidence. Pending nodes render not run; missing logs remain
  explicitly unavailable. Tested from a moved 70-DUT result without suite execution;
  the screens are now implemented as described above.
- Internal Inspector timeline containment (issue #89): reads recorded run-relative
  log paths beneath the opened run, retains the complete path after relocation, and
  reports unavailable references without reading another run's chronology. Timeline
  and capture readers now share checked, cached packaged schemas. The screens are
  now implemented as described above.
- Internal Inspector saved-tree model (issue #89): schema-validated result loading,
  declaration-order rows, shared Fail/Aborted navigation, and nearest-ancestor log
  ownership. Pending nodes never borrow chronology, duplicate names retain distinct
  indices, and missing declared readings remain named. Operator selections resolve
  symlinks and `..` before establishing the run root, without relaxing recorded-reference
  containment. Tested from a moved 70-DUT result without live suite execution;
  the screens are now implemented as described above.
- Internal Inspector capture reader (issue #89): reads schema-validated artifact
  envelopes and root results beneath the opened run, preserves full relative paths
  under an explicit relocation mapping, and reports unavailable evidence without
  substituting captures from the original run. Publishes the existing version-1
  artifact envelope schema; shared strict JSON parsing keeps both Inspector readers
  consistent. The screens are now implemented as described above.
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
  the `via`-hop composition that reaches a remote console over SSH. Signed by Ty
  on 2026-09-10; implementation is `SerialInterface` (issue #91).
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

- The gate is green again from a fresh venv, and the tree agrees with the October
  changes that turned it red. `make check` and `make typecheck` run pyright with the
  `bench` dependency group, so `gpiod` resolves for `bench/gpio_sim.py` and
  `bench/suites/gpio_lines.py` on every machine rather than only one that once ran a
  GPIO target. `.gitlab-ci.yml` is gone (2026-10-11), so the dormant `nightly-soak`
  schedule named in the #138 entries below no longer exists: the docs-site test now
  asserts there is no pipeline definition, and `STANDARDS.md` §6 and
  `tools/nightly_soak.py` no longer name the schedule. The README overhaul moved the
  docs-tree and station URLs and the example runs into `docs/getting-started.md`; the
  tests that pinned them follow. `docs/utilities.md` joins the site nav. The catalogue
  tests read both seed files, `seeded_suite.py` and `seeded_seams.py`, and the
  `no-input-fallback` seed no longer also trips `litmus/model-resolution-failure`,
  whose one seed is in `seeded_seams.py`. `uv.lock` takes urllib3 2.8.0, which the
  gate's audit requires for PYSEC-2026-4175/4176/4177; it reaches the tree only through
  `pip-audit`'s `requests` in the dev group.
- The executor's SIGKILL escalation is under test (issue #145). A host child that
  ignores SIGTERM is killed once the two-second grace runs out, and a second cancel
  landing inside that grace still kills and reaps it before the cancellation goes on;
  neither branch of `LocalExec._terminate` had executed under the gate before. The
  `src/litmus/executor.py` floor rises from 92.8 to 98.9. The probe behind these tests
  found the grandchild gap fixed under #146.
- The gate is the bench run, and the tree now says so (issue #138, Ty's ruling of
  2026-09-10 on #104: no CI, permanently). `STANDARDS.md` §1 and §6, the README, and
  the Makefile no longer name a pipeline: `make check` on the MR head is the gate, the
  reviewer runs it again before merging, and nothing arms auto-merge. `.gitlab-ci.yml`
  loses the `check` and `soak` jobs and the `nightly-soak` and `dependency-update`
  schedules; only the tag-only `pages` job remains, pending Ty's ruling on where the
  docs site is hosted. Both bench tools post without a pipeline: `tools/nightly_soak.py`
  takes the head sha from `git rev-parse`, defaults its API root and project id to this
  project (`GITLAB_API_V4_URL` / `GITLAB_PROJECT_ID` override), and names the bench
  host and `.soak/` directory as the night's record where it used to name a pipeline
  URL; `tools/dependency_update.py` carries the same defaults. New targets:
  `make nightly-record` (the soak, recorded on #68), `make deps-plan`, and
  `make deps-update`.
- Nothing hosts the docs site, a release is a branch, and the nightly is dormant
  (issue #138, Ty's rulings of 2026-09-11). GitLab renders `docs/` on `main`; a
  release is cut as `release/MAJOR.MINOR.PATCH` from the validated commit, where
  `make docs-release` commits the built site under `docs/site/` (`main` never carries
  it; `use_directory_urls: false` so a clone opens it from disk, `exclude_docs` keeps
  one build out of the next), the version tag sits on that branch head, and
  `make build` plus `uv publish` are bench steps of the release checklist. The
  tag-only `pages` job is gone with the Pages URL: README, `pyproject.toml`, and
  `mkdocs.yml` point at the tree, and the station label carries the rendered
  `docs/floor-technician.md`. `.gitlab-ci.yml` keeps one job, `nightly-soak`, the
  bench driver behind a pipeline schedule that stays switched off (no pipeline
  minutes to spend); until a schedule runs again the Phase 6 exit is one recorded
  bench soak green on the release candidate head, posted to #68, in place of
  fourteen consecutive nights.
- The parent owns the route (issue #137, Ty's ruling of 2026-09-10, signed
  2026-09-11). A route is owned by the node whose `configure()` declared it with
  `use_shell` and is inherited by every node it composes until a descendant declares
  `inherit=False`; a device class never takes a routing argument. A gateway is a
  parent whose `configure()` opens the hop and whose `build_suite()` composes the
  devices behind it, and its hop closes in its own teardown after every child's. A
  gateway that cannot come up Fails in its own configure slot and the children it
  never composed stay pending. `examples/networked_device.py` is rebuilt on this
  shape: `DutDevice(name, mac, firmware_version)` has no routing surface, and
  `litmus run ...::Gateway` runs the same DUT class through the hop. The Exec
  Routing Contract, the Time Budget Contract, and the idioms say so; the #115
  ruling (owned hops outlive the post-cleanup survey) now describes the only kind
  of hop there is.
- The lifecycle seam inside the framework (issue #126, Ty's ruling of 2026-09-10,
  raised by the #109 sweep): the half of `TestUtils` that `base.py` drives, the run
  bracket, the context phases, transport teardown, timeline ownership, measurement
  sealing, and router inheritance, now lives on a package-internal `NodeRuntime` the
  lifecycle reaches through `node_runtime(utils)` by plain names; `TestUtils` holds
  one and reads its state on the author's behalf. Thirty-five `reportPrivateUsage`
  suppressions in `base.py` are gone. No public name, signature, or behavior changed.

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

### Fixed

- `SshInterface` password auth no longer scans `~/.ssh`: the password path passed
  `client_keys=[]`, which asyncssh reads as "load the default key files", so every
  connect loaded and validated whatever keys the test host held (about 0.2s for a
  4096-bit RSA key) and offered credentials the provenance line never named. It now
  passes `client_keys=None`. Found through the 70-DUT operator-interrupt self-test,
  which had hung on hosts with a large RSA key since the key appeared: the slow
  connects outran the rendezvous and the in-process sshd's `aclose()` then waited on
  sessions nobody would close. The loopback sshd now aborts live sessions on close and
  the test cancels its rack on a failed rendezvous, so a timeout reports instead of
  parking.
- Abort now reaches a grandchild that ignores SIGTERM (issue #146). `LocalExec`
  measured its two-second grace against the direct child alone; under dash a compound
  command (`a; b`, `a && b`, a pipe) keeps the shell as that child, the shell dies on
  SIGTERM at once, and a grandchild that ignores the signal kept the pipes open, so the
  abort never escalated and `run()` waited out the orphan's whole lifetime (30.01 s in
  the probe). The grace is now measured against the process group: SIGTERM at once,
  SIGKILL to every survivor when the grace expires, and the group is waited for after
  the kill for a bounded settle so an orphan zombie under a reaper that never collects
  it cannot hold a run open. Local exec only; the contract text and the
  `cancel_in_flight()` surface are unchanged.
- The Inspector's Re-Run tab no longer shows a verdict on a node above the one that
  was pressed. Re-running a single leaf runs its ancestors' lifecycle and rolls their
  state up over that one child, so a group whose other child failed in production came
  back Pass and carried the pass to the root, hiding the failure the archive still
  held. Those nodes now read **partly re-run**; their fresh chronology stays readable,
  and the production verdict stays on the Inspect tab (issue #141).

### Security

- Optional `env_allow` / `env_deny` filtering of the published environment snapshot;
  SSH credential containment tests pin that password and private-key values do not
  escape into logs, results, or other evidence
  ([!51](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/51),
  [!82](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/82)).

### Removed

- `via=` on the `LitmusTest` constructor, with the framework-owned via lifecycle
  behind it: `run_configure_only`, `run_cleanup_only`, the via lease count, and the
  adoption of a via's failure into the consumer's configure slot (issue #137). The
  only `via` left is the tunnel argument to `use_shell` / `get_shell`, declared
  inside `configure()` by the node that owns the route. Pre-1.0: no deprecation
  period; the `tests/test_via.py` suite is replaced by `tests/test_parent_route.py`.
- `self.case` and `self.group` (issue #110). Pre-1.0: no deprecation period.
