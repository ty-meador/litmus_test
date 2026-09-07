# Litmus Inspector — target specification

This is the first deliverable of [#89](https://gitlab.com/Tyler-Meador/litmus_test/-/issues/89).
It carries the legacy Floor Technician guide into the Python port, adjusted for the
signed decisions on [#88](https://gitlab.com/Tyler-Meador/litmus_test/-/issues/88),
[#106](https://gitlab.com/Tyler-Meador/litmus_test/-/issues/106), and
[#107](https://gitlab.com/Tyler-Meador/litmus_test/-/issues/107).
It specifies the terminal UI to build; **the Python Inspector is not shipped yet**.
The [Floor Technician guide](floor-technician.md) remains the station page for reading
today's evidence. The controls below describe the target, not commands available today.
The [review rulings on !119](https://gitlab.com/Tyler-Meador/litmus_test/-/merge_requests/119#note_3796094775)
set the implementation order: ship Inspect and Failures first. Run is a separate
increment behind its own decision and does not gate saved-evidence inspection.

The internal timeline reader (`litmus.inspector_timeline.TimelineReader`) is implemented
as a foundation for the log pane. It asynchronously reads a selected timeline path,
validates each version-1 object against the packaged published schema, and returns
records and line-numbered diagnostics in physical order. Additive fields and recorded
scopes and pointers survive unchanged. A complete final JSON object is readable without
a trailing newline; an unfinished final object or UTF-8 sequence produces a truncation
diagnostic. Malformed complete records, duplicate keys, non-finite numbers, and
unsupported versions are explicit diagnostics, and later valid records remain readable.
Missing or unreadable logs produce a file-level unavailable diagnostic. These are reader
observations, never test verdicts. The reader does not open artifact pointers or resolve
tree ownership; screens, root containment and relocation, and the entry point remain
later increments under #89.

The Inspector serves the person at the station when a test stops the line. Its test of
success is concrete: find what happened, categorize it, and begin the station's recovery
protocol within sixty seconds, without opening suite source.

## What the Inspector reads

Inspect mode replays saved evidence. Moving the cursor, paging a log, opening an artifact,
or comparing two captures never touches the DUT and never executes suite code.

The input is a run directory containing `result.json`, its per-node `timeline.jsonl`
files, and the point-in-time artifacts referenced by those timelines. The structural tree
comes entirely from `summary_tree`; the temporal view comes entirely from the linked
JSON Lines records. A saved 70-DUT rack must be inspectable without live `LitmusTest`
objects, source discovery, an SSH connection, or the test station that produced it.

The [Inspector JSON contract](inspector-json.md) owns the field definitions and version
policy. This page owns their presentation. It introduces no new result fields or
verdict rules. The result and each timeline record have independent `format_version`
values; select and validate each decoder separately, accept additive fields, and report
unsupported versions explicitly rather than guessing their meaning.

## Opening a run

The entry point opens a selected `result.json` and treats its containing directory as
the run root. The legacy convenience of opening the most recent run is a target; the
Python command spelling and discovery rule remain to be settled under #89. The legacy
`npm run inspect` command and `summary-tree.artifact` input are not Python interfaces.

If no result is available, explain that there is no saved tree to inspect. Do not start
a test to manufacture one. Absence of `result.json` is not a verdict: an invocation may
have failed before a run, or a process may have ended before result publication. Preserve
any surviving timeline evidence for diagnosis.

## The screen layout

The terminal has a tab bar, a tree or failure list above, and the highlighted node's
timeline below. This illustration shows the target arrangement, not a captured run:

```text
  [ Inspect ]   Failures
  ---------------------------------------------------------
  rack  Aborted
    server-01  Pass
    server-02  Fail
      thermal check  Fail  -> temperature 87 C exceeds 75 C
    server-03  Aborted  -> operator interrupted the run
    firmware check  Pending
  up/down navigate | left/right jump failures | Tab switch
  ---------------------------------------------------------
  thermal check -> timeline owned by server-02
    #  Time       Level  Message
   37  14:02:11Z  info   starting thermal ramp
   38  14:02:45Z  info   survey captured [1]
   39  14:03:02Z  error  temperature 87 C exceeds 75 C
  n next | p previous | f errors | 1-9 artifact | q quit
```

The log updates on cursor movement. Enter is never required to read it. A delayed load
for the previous selection must not overwrite the currently highlighted node's view.
Show the selected node and, when different, the ancestor that owns its timeline.

Tree order is declaration order, including in parallel compositions. A parent marked
Fail may summarize a failing child; keep the descendants visible so the technician can
reach the actual claim. Use the root-to-node array of names as the path, not a split of
the display breadcrumbs: names may themselves contain `/`.

## The tabs

### Inspect — the default

Display every declared node, with its recorded state and failure message. Color and
symbols help scanning, but the state must remain legible as text.

| State | Presentation and meaning |
|---|---|
| `Pass` | Green success mark. The framework accepted an explicit verdict and any required evidence. A string `"PASS"` is not a passing verdict. |
| `Fail` | Red failure mark. Read the failure object: this can be a modeled `TestFailure` or a software contract violation. |
| `Aborted` | Distinct, named state. No verdict was produced; the cause may be integration, operator interrupt, missing verdict, or an abort budget. |
| `null` | Dim Pending / unreached row. This declared step never ran; it is not a fourth terminal state. |

Left and right arrows move backward and forward through nodes needing attention,
skipping Pass and pending rows. Preserve the legacy implementation's inclusion of both
Fail and Aborted in that navigation, while keeping their labels and recovery meanings
distinct. At either end, leave the cursor where it is; do not wrap unexpectedly.

Show the selected leaf's `evidence` beside its claim and failure. For every name in
`evidence_declared` absent from `evidence`, show **not taken** by name. An absent reading
is neither zero nor a successful measurement. Do not present the reserved empty
`run_data["measurements"]` slot as an implemented measurement or SPC feature.

### Run — a separate, decision-gated increment

If Run is approved and implemented, retain the legacy deliberate Enter interaction:
move to a node and press Enter to request execution. Switching tabs or highlighting a
node must never run it. Inspect and Failures ship without this capability; a saved
evidence directory alone never makes Run available.

**Ancestor-configure replay is prohibited.** The legacy `↑ctx` behavior does not port,
including behind a flag. The Python
[instance-local context idiom](idioms.md#immutable-self-convention) keeps a device
self-contained when it is removed from a rack and run at the bench. Replaying a parent's
configure to make that device runnable would introduce the hidden dependency this
contract prevents. The framework continues to own configure and cleanup.

**The Run entry point still requires its own shared decision under #89.** The operator
must supply a runnable suite through a sanctioned entry point; the Inspector holds an
adapter to it. Never derive a runnable suite from `result.json`, artifact contents, or
source discovered beside the run directory. Version-1 results carry no suite factory
or constructor arguments. This specification establishes the boundary, not a new
execution API.

During an eventual live run, Tab remains disabled as in the legacy UI. Quitting a saved
inspection is harmless; quitting live hardware execution is a different operation. Its
implementation must use the existing
[termination contract](contracts.md#in-flight-termination-contract), preserve bounded
cleanup and evidence, and expose operator interruption as Aborted. The legacy promise
that Ctrl-C always exits immediately with nothing at stake does not apply to a live run.

### Failures

Preserve the flat list for finding failures buried inside a large rack, with up/down
selection and the selected node's log below. This is a navigation projection of the
same recorded tree, not a second verdict calculation. Retain the legacy list's
navigation controls, but make Failures available during saved inspection without a
runnable suite. Both Fail and Aborted nodes remain reachable through Inspect's arrow
navigation as well. Run's separate decision does not gate either saved-evidence view.

## The main workflow: diagnosing a failure

1. **Find the node needing attention.** Use right/left to move forward/backward without
   scrolling through passed leaves. Read the recorded state before choosing a recovery.
2. **Read the failure message and comparison.** Show `fail_message`, and `expected` /
   `received` when present. Preserve bug banners, nested causes, hook names, and operator
   signals; a plain-language message alone does not establish hardware fault.
3. **Read the timeline already below the tree.** `n` advances a page, `p` returns a
   page, and `f` toggles the errors-only filter. Log severity does not replace the verdict.
4. **Open a referenced artifact.** Press its displayed digit, `1`–`9`. Survey captures
   can show whether the device's reported health agrees with what actually happened.
5. **Choose the recovery protocol.** Read the failure's cause and the available evidence.
   The human makes the final determination; uncertainty is a reason to escalate.

The five categories follow the current
[Failure Taxonomy Contract](contracts.md#failure-taxonomy-contract), which supersedes
the legacy guide's separate, automatically assigned infrastructure category:

| Category | Evidence to read | Recovery direction |
|---|---|---|
| DUT failed a check | Modeled `TestFailure`, comparisons, and captured readings. A configure failure means the device did not reach a testable state; hardware versus infrastructure remains a human determination. | Follow the station protocol for the established cause. |
| Hardware lying | Survey evidence contradicts a claimed healthy state or the apparent verdict. | Preserve the discrepancy and seek deeper inspection, even after Pass. |
| Test method bug | Contract-violation banner, `bug`, or `MissingVerdict`. The latter is Aborted; thrown bugs are Fail. A framework-contract banner identifies the framework maintainer as the owner. | Escalate the software defect; do not condemn the unit from this signal. |
| External integration failure | `ExternalIntegrationFailure`, its hook, and its cause. | Restore the harness or coordination service before retrying. |
| Operator interrupt | `OperatorInterrupt`, `operator: true`, and its signal. | The rack was stopped deliberately; resume only under the station's procedure. |

Time-budget evidence supplements this taxonomy. Display the winning `timeout.clock`,
declaration, budget, and elapsed time. `fail_after` produces Fail; `abort_after` produces
Aborted and may have no failure object at all. Do not relabel the latter as a hook
failure just because both are Aborted. The
[station recovery table](floor-technician.md#sixty-second-recovery-protocol) gives the
full reading procedure.

## The timeline and its owner

Resolve a node's non-null `log_path` relative to the selected run directory. A terminal
node with a null link rolled its records into the nearest ancestor with a link; indicate
that ancestor with the legacy cyan owner arrow. Keep each entry's `scope` visible so
sibling records in the shared timeline cannot be mistaken for this leaf's readings.

A pending node has no chronology anywhere. Show **not run**, not its parent's timeline
as though the pending node produced evidence. A missing file for a node that did run
is a separate, explicit **log unavailable** condition. Neither condition changes state.

Read JSON Lines incrementally, preserving file order and every complete valid record.
For an interrupted final line, display the complete preceding records and a visible
truncation marker; do not fill in the unfinished object. Report malformed complete
records or unsupported versions as decoding errors rather than silently dropping them.
An errors-only filter must not hide these reader diagnostics.

## Artifacts — the evidence behind the verdict

Number structured artifact-pointer entries in the loaded timeline and show `[N]` on the
corresponding row. Digits `1`–`9` open the matching numbered capture in a modal. Take
the link from the pointer fields, not from a path-looking substring in `message`.
Retain numbering when paging or filtering so a digit never silently changes its target.

The modal groups captures by `artifact_type` and labels each with its name and timestamp.
Show the persisted content, including an explicit null observation; nothing in the
viewer recomputes a verdict. The root `run-result` pointer opens `result.json`, whose
shape differs from a point-in-time artifact envelope. The envelope's version must also
be checked when its decoder is implemented, with its published schema as required by
the [JSON version policy](inspector-json.md#published-schemas).

An unavailable or unreadable capture produces a visible diagnostic containing the
reference, while the log and tree remain usable. The saved files are never edited.
Version 1 stores absolute `artifact_path` pointers, whereas tree log links are relative.
The reader resolves captures under the selected run root. For a moved run, try
re-anchoring the recorded path's run-root-relative tail beneath the opened run root
before declaring the capture unavailable. Preserve that full relative path: two nodes
may capture different evidence with the same filename. Never search for or substitute
a file merely because its basename matches.

Re-anchoring requires an established original run root. If the reader cannot establish
that mapping, or the mapped capture cannot be read under the opened root, show
**artifact unavailable** with the recorded reference; do not guess a root or silently
fall back to another run's evidence. The reader implementation must demonstrate that
mapping and its unavailable cases in tests before claiming moved-run support.

This is a reader rule. Saved pointers remain unchanged and absolute at version 1.
Changing their stored representation to relative paths is a record-contract change
under the [version policy](inspector-json.md#published-schemas), requiring Ty's decision
before an implementation MR.

### Reading and comparing artifacts

| Key | Single-artifact view | Diff view |
|---|---|---|
| `[` / `]` | Previous / next artifact type. | Change the left artifact within the current type. |
| `←` / `→` | Previous / next artifact within the current type. | Change the right artifact within the current type. |
| `n` / `p` | Next / previous content page. | Page both sides together. |
| `d` | Enter diff when the type has at least two artifacts. | Return to the single-artifact view. |
| `b` | Close the modal and return to the log. | Close the modal and return to the log. |

Diff shows two captures of the same type side by side, highlighting changed lines and
dimming unchanged lines. Preserve the legacy guide's intended swap when the two selected
indices collide, so a two-capture comparison can reverse sides. This is a display of
recorded differences, not an automatic diagnosis of hardware truth.

## Key bindings quick reference

| Context | Key | Action |
|---|---|---|
| Saved inspection, including a modal | `q` / `Ctrl-C` | Quit without changing evidence or touching hardware. |
| Main view | `Tab` | Cycle Inspect / Failures; include Run only after its separate approved implementation. Disabled during a live run. |
| Inspect tree | `↑` / `↓` | Move through the tree. |
| Inspect tree | `←` / `→` | Previous / next Fail or Aborted node; skip passed and pending rows. |
| Run tree, when enabled | `↑` / `↓`, then `Enter` | Select a node, then deliberately request execution. |
| Failures list | `↑` / `↓` | Move through the list and load the selected node's log. |
| Log pane, no modal | `n` / `p` | Next / previous page. |
| Log pane, no modal | `f` | Toggle errors-only filtering. |
| Log pane, no modal | `1`–`9` | Open the artifact carrying that number. |
| Artifact modal | `b`, `[`, `]`, `←`, `→`, `n`, `p`, `d` | Use the artifact controls above; the modal owns these keys until it closes. |

## Things that need careful reading

- **An ancestor owns the log.** The owner arrow explains roll-up; it says nothing about
  context inheritance. The legacy `↑ctx` ancestor-configure replay is prohibited.
- **An error appears in a passing test.** Survey errors are logged but do not decide
  the verdict. Read the state and the surrounding capture, not just the line's color.
- **Many nodes are Aborted.** An integration halt or operator interruption can affect
  the whole in-flight tree. A missing verdict or an abort budget is node-local. Read
  the cause instead of inferring a rack-wide hardware failure from repeated labels.
- **The record ends abruptly.** Retain the accepted chronology and mark the missing
  tail. Neither incomplete evidence nor missing declared readings may become Pass.

## Implementation acceptance

The screen implementation follows this document in a later increment. Its view model
must be testable independently of the terminal and driven by a recorded 70-DUT rack:

- Reconstruct tree order, peer states, pending descendants, failure causes, timeout
  evidence, and missing declared readings from version-1 records alone.
- Navigate failures and load the selected node's own or rolled-up timeline immediately,
  with correct owner and scope; never show stale content after a cursor change.
- Open numbered pointers, group artifacts by type, and compare captures using the
  specified keys, including the two-artifact index collision.
- Preserve complete lines under truncation and distinguish unavailable evidence,
  malformed records, unsupported versions, and nodes that never ran.
- Make Inspect and Failures usable from saved evidence without source or hardware;
  neither waits for Run. Settle the saved-view command and discovery rule under #89.
- Verify moved-run resolution preserves the complete run-relative path and remains
  under the opened root. Exercise captures with identical basenames in different nodes,
  an unestablished root mapping, and unavailable files; preserve the recorded reference
  in every unavailable diagnostic and never substitute another capture.
- Keep Run behind its own decision and increment. Its adapter accepts only an
  operator-supplied suite through a sanctioned entry point, never reconstruction from
  evidence or neighboring source. Ancestor-configure replay stays prohibited. Any live
  execution must preserve bounded cleanup, evidence, and operator-interrupt Aborted.

If the message, chronology, and captures do not establish a recovery category, keep the
run directory and escalate with the selected node's path. The Inspector must make that
uncertainty visible. Evidence earns a conclusion; the screen cannot supply one for it.

## Migration references

The behavioral reference is the read-only `litmus-test-legacy/docs/inspector.md`, with
navigation checked against `src/inspector/InspectTab.mts`, `FailuresTab.mts`,
`DebugView.mts`, and `ArtifactModal.mts` in that repository. The guide's workflow,
split panes, optional Run, failure navigation, modal, and diff controls are retained.
Python record names, the five current categories, instance-local ctx, explicit evidence,
and interruption semantics follow this port's contracts. No TypeScript runtime or
language-server behavior is required to specify this saved-evidence viewer.
