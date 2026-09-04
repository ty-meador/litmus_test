# Inspector JSON Contract

The Inspector TUI is outside the Python port's current scope. Its input contract is
not. Every completed node produces a `RunResult`, and `RunResult.to_json()` returns a
plain Python object that `json.dumps()` accepts without an encoder. The root result is
self-contained: consumers do not need the live `LitmusTest` objects to reconstruct the
run tree.

## Version 1 result

| Field | JSON type | Meaning |
|---|---|---|
| `format_version` | integer | Schema version; currently `1`. |
| `state` | string | One terminal state: `Pass`, `Fail`, or `Aborted`. |
| `failure` | object or null | JSON-safe framework failure, including nested causes. |
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

## Summary tree node

Every `summary_tree` node contains exactly these version-1 fields:

| Field | JSON type | Meaning |
|---|---|---|
| `name` | string | Stable node name. |
| `type` | string | The same node-type discriminator used by `RunResult`. |
| `state` | string or null | Terminal state, or null when a declared sequence step did not run. |
| `failure` | object or null | JSON-safe failure for this exact node. |
| `children` | array | Ordered child nodes. Groups retain declaration order after settlement. |

A null tree state means pending/unreached; it is not a fourth terminal state. A stopped
sequence retains its unexecuted descendants with null states, so the root snapshot
still describes the full declared tree.

## Failure shape

Failures use their framework `to_json()` methods. `TestFailure` and its subclasses
retain the failure name, message, state, comparisons, and bug metadata. An
`ExternalIntegrationFailure` retains its hook and JSON-safe cause. Arbitrary exception
objects and live values degrade to descriptive JSON-safe forms rather than making
serialization fail and masking the evidence.

Integrators should use `ancestor_names` as the durable node identity and
`bread_crumbs` only for display. Names may contain `/`; the array is therefore the
canonical representation.
