# JUnit XML reporter

Every CI system reads JUnit XML. `litmus.junit.JunitXml` maps a `RunResult` summary
tree onto that schema so a litmus run shows up beside every other suite with no
adapter. The Floor Technician's taxonomy survives the mapping: a red build says
hardware, harness, or bug — not just red.

The runner (`litmus run --junit PATH`, issue #73) will call `JunitXml.write`. Until
that CLI exists, harness adapters import `litmus.junit` directly, off the promised
surface (`docs/api-stability.md`).

This is a *projection* of the Inspector JSON contract, not a second source of truth.
`RunResult.to_json()` remains the evidence record. JUnit is what CI already knows
how to paint.

## Mapping of the three terminal states

There is no fourth terminal state. A declared node that did not run (`state` is
`null` on the summary tree — a later step in a failed sequence) is `skipped`, which
is a JUnit status, not a litmus state.

| Litmus | JUnit element | What the FT still sees |
|---|---|---|
| `Pass` | `testcase` with neither `failure` nor `error` | A green row. |
| `Fail` | `failure` | Taxonomy class on `@type`, `fail_message` on `@message`, full `to_json()` payload in the body. `bug: true` means broken code, not broken hardware. |
| `Aborted` | `error` | Same payload shape. Aborted is **not** a flavor of Fail; CI must not count it as `failures`. |
| unreached (`state` null) | `skipped` | The node was declared and never ran. |

`@type` is the taxonomy class name (`TestFailure`, `ContractViolation`,
`MissingVerdict`, `ExternalIntegrationFailure`, `OperatorInterrupt`). The body is
the same JSON object `RunResult.to_json()` already publishes under `failure`, so a
CI log and the Inspector agree.

## Tree → testcases

- Every **leaf** (a summary-tree node with no children) is one `testcase`.
- Groups, sequences, and suites become `classname`: the root-to-parent path, joined
  with `/`. `ancestor_names` is still the durable identity in the Inspector JSON;
  classname is display.
- A **composite that settled before any child ran** (configure failed, abort during
  bring-up) is itself a `testcase`. Otherwise CI would paint skip — or nothing — for
  a Fail. Declared children that exist on the tree and never ran are `skipped`.

Counts on `testsuites` / `testsuite` (`tests`, `failures`, `errors`, `skipped`) are
the counts of emitted testcases, not of composite nodes.

## Artifacts

Point-in-time evidence stays in the run directory. JUnit gets pointers, not copies:

- `system-out` contains `timeline: <log_path>` when the node (or the nearest ancestor
  that owns a log) has a `timeline.jsonl` relative to the run directory.
- A timeout that determined the state adds a `timeout: …` line with the same fields
  as the Inspector timeout object.

Consumers resolve `log_path` against the run directory that holds `result.json`.

## Document shape

Jenkins-compatible JUnit (the schema GitLab CI, Jenkins, and pytest-junit share):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<testsuites name="rack" children="3" failures="1" errors="1" skipped="1">
  <testsuite name="rack" children="3" failures="1" errors="1" skipped="1">
    <testcase name="identity" classname="rack/claims"/>
    <testcase name="firmware" classname="rack/claims">
      <failure type="TestFailure" message="gate remained closed">{taxonomy JSON}</failure>
      <system-out>timeline: rack/timeline.jsonl</system-out>
    </testcase>
    <testcase name="never reached" classname="rack/ordered claims">
      <skipped/>
    </testcase>
  </testsuite>
</testsuites>
```

No `time` attribute is emitted: `RunResult` does not carry per-node duration, and
inventing `0` would lie.
