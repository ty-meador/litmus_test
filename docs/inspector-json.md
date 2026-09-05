# Inspector JSON Contract

The Inspector TUI is outside the Python port's current scope. Its input contract is
not. Every completed node produces a `RunResult`, and `RunResult.to_json()` returns a
plain Python object that `json.dumps()` accepts without an encoder. The root result is
self-contained: consumers do not need the live `LitmusTest` objects to reconstruct the
run tree.

Every full `LitmusTest.run()` atomically writes the root `RunResult.to_json()` payload
to `result.json` in the timestamped run directory beneath `LITMUS_ARTIFACT_ROOT`. The
file is present for Pass, Fail, and Aborted outcomes. Its absolute path is published by
the final entry in the root node's timeline, with artifact name `result` and artifact
type `run-result`; consumers do not need to derive the run directory name.

## Version 1 result

| Field | JSON type | Meaning |
|---|---|---|
| `format_version` | integer | Schema version; currently `1`. |
| `state` | string | One terminal state: `Pass`, `Fail`, or `Aborted`. |
| `failure` | object or null | JSON-safe framework failure, including nested causes. |
| `timeout` | object or null | Structured time-budget evidence when a deadline determined or contributed to this state. |
| `test_case` | string | Stable name of this node. |
| `type` | string | `test-suite`, `test-case`, `test-group`, or `test-sequence`. |
| `ancestor_names` | array of strings | Root-to-this path, inclusive. |
| `bread_crumbs` | string | Display path, exactly `ancestor_names.join("/")`. |
| `summary_tree` | object | Recursive snapshot rooted at this node. |
| `physical_location` | string | `PHYSICAL_LOCATION`, or `unknown` when absent. |
| `env` | object | Environment snapshot taken at the start of the run, filtered per the run's allow or deny list; see below. |
| `run_data` | object | Detached structured evidence snapshot. It always contains a framework-owned `measurements` array reserved for a future parametric-measurement protocol. |

Version 1 does not define measurement entries, limits, units, aggregation, SPC, or Cpk;
the reserved `measurements` array is therefore empty. Integrators must not infer
statistical semantics from its presence.

Python-only lifecycle details such as the internal slot and raw return value are not
part of this payload. Consumers must treat unknown fields as additive and select a
decoder by `format_version`.

### The `env` snapshot is filtered by the run, not by the schema

Version 1 publishes the run environment as the run declared it. `LitmusTest.run()` and
`TestUtils` accept **either** `env_allow` **or** `env_deny` (both is a `ValueError`): a
list of regular expressions matched with `re.search` against each variable's *name*.
`TOKEN` therefore matches `GITLAB_TOKEN`; anchor as `^HOME$` for an exact name. With an
allow list only matching names are published; with a deny list matching names are
withheld. Values are never hashed or rewritten — a variable is either present verbatim
or absent.

With neither list the snapshot is verbatim: every value present at run start, including
tokens or credentials injected by CI or the shell, is serialized into the artifact.
Keeping secrets out of an unfiltered run environment is the operator's obligation.

**DEBUG override.** When the source environment carries `environment=DEBUG`, the
declared list is ignored and every variable is published. The run logs a warning —
`DEBUG mode detected, ignoring provided <allow|deny> list and including ALL environment
variables` — so the artifact's extra content is explained in its own timeline.

The filter is applied once, at the root, and every node and every `RunResult` sees the
same filtered view. `PHYSICAL_LOCATION` and `LITMUS_ARTIFACT_ROOT` are read from that
view, so an allow list must let them through to keep their effect. Decoders must not
assume any particular variable is present (ruling on issue #53, 2026-09-03).

## Version 1 timeline

Each `run()` creates a timestamped directory directly beneath
`LITMUS_ARTIFACT_ROOT`. Point-in-time artifacts are stored at that run-directory root.
Every executed node that owns a discrete log receives a child directory containing:

- `timeline.log` — a streamed human view with one escaped line per entry;
- `timeline.jsonl` — the authoritative Inspector record, with one complete JSON object
  per physical line.

Both files are flushed after each entry. Consumers may read the JSON Lines file while a
run is active and retain every complete line after interruption. The child-directory
name is framework-owned and must be treated as opaque; use `LitmusTest.log_path` rather
than reconstructing it.

Every version-1 timeline object contains these fields:

| Field | JSON type | Meaning |
|---|---|---|
| `format_version` | integer | Timeline-record schema version; currently `1`. |
| `timestamp` | string | Timezone-aware UTC timestamp ending in `Z`. |
| `scope` | array of strings | Root-to-emitting-node path, inclusive. |
| `level` | string | Lowercase Python logging level name. |
| `message` | string | Fully rendered log message. Newlines remain escaped within the JSON line. |

An artifact-pointer entry additionally contains all four fields below. They are absent
from ordinary records rather than set to null.

| Field | JSON type | Meaning |
|---|---|---|
| `artifact_name` | string | Captured artifact name, or `result` for the root result. |
| `artifact_path` | string | Absolute path of the published artifact or root `result.json`. |
| `artifact_type` | string | Captured artifact type, or `run-result` for the root result. |
| `artifact_timestamp` | string | The evidence timestamp in UTC; for the root result, its publication time. |

Authored suites, sequences, and groups own a log by default. Inline cases roll their
records into the nearest owning ancestor. A sequence, group, or composed authored suite
constructed with `discrete_log_file=False` also rolls up. Pending nodes create no files.

## Two records, two version fields

A run therefore produces two versioned records, and each carries its own
`format_version`:

| Record | Where it lives | Version field |
|---|---|---|
| Result | `result.json` at the run-directory root; the serialized root `RunResult.to_json()` and structural view rooted at `summary_tree` | `format_version` on the result object |
| Timeline | `timeline.jsonl` beneath the run directory; the temporal view | `format_version` on every JSON Lines object |

The two version numbers move independently. The summary tree links the structural view
to the temporal view through each node's `log_path` (ruling on issue #106, 2026-09-04:
the link is additive within result version 1). An Inspector selects a node in the tree,
resolves `log_path` against the run directory, and reads that node's chronology.

## Summary tree node

Every `summary_tree` node contains exactly these version-1 fields:

| Field | JSON type | Meaning |
|---|---|---|
| `name` | string | Stable node name. |
| `type` | string | The same node-type discriminator used by `RunResult`. |
| `state` | string or null | Terminal state, or null when a declared sequence step did not run. |
| `failure` | object or null | JSON-safe failure for this exact node. |
| `timeout` | object or null | Structured time-budget evidence for this exact node. |
| `log_path` | string or null | POSIX path of this node's `timeline.jsonl`, relative to the run directory. Null when the node never ran or when its records rolled up into an ancestor. |
| `children` | array | Ordered child nodes. Groups retain declaration order after settlement. |

A null tree state means pending/unreached; it is not a fourth terminal state. A stopped
sequence retains its unexecuted descendants with null states, so the root snapshot
still describes the full declared tree.

`log_path` is relative so that a run directory copied to another machine keeps every
link intact. Consumers resolve it against the run directory they opened, the same
directory that holds the point-in-time artifacts. A node with a terminal `state` and a
null `log_path` rolled its records into an ancestor; the nearest ancestor with a
non-null `log_path` holds them, and the root always carries one. A node whose `state`
is null never ran and has no chronology anywhere.

## Failure shape

Failures use their framework `to_json()` methods. `TestFailure` and its subclasses
retain the failure name, message, state, comparisons, and bug metadata. An
`ExternalIntegrationFailure` retains its hook and JSON-safe cause. Arbitrary exception
objects and live values degrade to descriptive JSON-safe forms rather than making
serialization fail and masking the evidence.

## Timeout shape

When no deadline contributed to a node's state, `timeout` is null. Otherwise it carries
these fields:

| Field | JSON type | Meaning |
|---|---|---|
| `reason` | string | Always `timeout`. |
| `declared` | string | The winning declaration: `fail_after` or `abort_after`. |
| `budget_seconds` | number | Positive finite duration declared on the winning clock. |
| `elapsed_seconds` | number | Active window time consumed when that clock fired. |
| `clock` | string | Root-to-declaring-node path of the winning clock. |

For `fail_after`, `failure` also contains the hardware-facing `TestFailure`. For
`abort_after`, `failure` may be null because the framework does not forge a verdict;
the timeout record carries the reason for the node-local Aborted state.

Integrators should use `ancestor_names` as the durable node identity and
`bread_crumbs` only for display. Names may contain `/`; the array is therefore the
canonical representation.
