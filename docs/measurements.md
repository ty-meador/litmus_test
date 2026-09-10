# Parametric Measurements

Design note for #85, signed off by Ty on the issue 2026-09-05 and implemented under
#86. This page owns the protocol; `docs/inspector-json.md` owns the record's place in
`result.json`.

A test method already hands its readings back with the verdict (`PASS(evidence)`,
`docs/idioms.md`). That record answers the Floor Technician's question: did the unit
meet the claim. It does not answer the process engineer's question: where inside the
window did this unit land, and where are the last five hundred landing. That question
needs a number with a unit and limits, in a shape a statistical consumer can read
without the suite source. `utils.measure` records that number, in one call, without
touching the verdict.

The rule this note is built on: **a limit is evidence, never a verdict.** A
measurement outside its limits is recorded as such, beside the verdict; the verdict
stays whatever the gated `PASS` says. A `Pass` keeps meaning exactly what it has always
meant, and the framework forges nothing.

## The call

`utils.measure` is the documented home. It is a method on `TestUtils`, the scoped
service object every lifecycle method already receives, so a measurement is scoped to
its node the way a log line is.

```python
def measure(
    self,
    name: str,
    value: int | float,
    *,
    unit: str | None = None,
    limits: tuple[float | None, float | None] | None = None,
) -> Measurement: ...
```

It is synchronous. It records and returns; it performs no I/O, so there is nothing to
await and no place for a cancellation to land between the reading and the record.

**`name`** — a non-empty `str`, unique within the node. The name is the identity the
downstream consumer keys on, together with the node path (see
[The export contract](#the-export-contract)): for the life of the suite it names the
same quantity in the same unit. Use the same spelling discipline as evidence keys
(`output_voltage_v`, `boot_time_s`). Recording the same name twice in one node is a
`ContractViolation` naming the node and the name; the second reading is not silently
appended, because a consumer that sees two `output_voltage_v` rows for one node cannot
tell a retry from a bug.

**`value`** — an `int` or a finite `float`. `bool` is refused (it is an `int` to
Python and a category to a process engineer), as are `NaN` and the infinities, for the
same reason `run_data` refuses them: a JSON consumer cannot carry them. A textual
observation (`ftl_state: "running"`) is evidence on the verdict, not a measurement;
SPC and Cpk are defined on numbers, and this protocol does not pretend otherwise.

**`unit`** — a free string the developer chooses (`"V"`, `"ms"`, `"°C"`), or `None`
for a dimensionless count. The framework validates nothing about it and never converts;
it is a label the consumer reads back. That is why the name must not change meaning:
a name that was volts in one release and millivolts in the next breaks every chart
built on it.

**`limits`** — `None`, or a `(low, high)` tuple, inclusive at both ends, with `None`
for an open side: `(4.75, 5.25)`, `(None, 250.0)`, `(0, None)`. A tuple is enough: it
reads as the window it is, and it puts no new type into a public signature. Both sides
`None`, a bound that is not a finite number, or `low > high` is a `ContractViolation`
at the call — a limit that bounds nothing is a bug in the suite, not a wide window.
With `limits=None` the measurement is recorded for trend and `in_limits` is `null`.

**The return** is a frozen `Measurement` record: `name`, `value`, `unit`, `limits`,
`in_limits`, `timestamp`, exactly what was recorded. It exists so the developer can gate
on it in plain sight:

```python
@evidence("output_voltage_v")
async def _check_rail(self, utils: TestUtils) -> Verdict:
    v = await self._read_rail(utils, utils.ctx["ip"])
    rail = utils.measure("output_voltage_v", v, unit="V", limits=(4.75, 5.25))
    if rail.in_limits:
        return PASS({"output_voltage_v": v})
    raise TestFailure(
        "5V rail out of window", "4.75..5.25 V", f"{v} V", evidence={"output_voltage_v": v}
    )
```

The framework evaluated the limit once, the developer chose what to do about it, and
`litmus/gated-pass` can see the predicate beside the `PASS`. Whether an out-of-window
reading is a `TestFailure`, a warning to trend, or a reason to re-seat the probe and
read again is the suite author's decision, taken in the suite.

### Where it may be called

`configure` and the test method. Those are the two slots that produce the node's
record: `configure` provides ctx, the test method produces the verdict, and both receive
the node's own `TestUtils`. A bring-up reading (ambient at start, supply voltage before
the DUT is powered) belongs to `configure`; a reading under test belongs to the test
method. A composite node (`sequence`, `parallel`) has a `configure` and may measure
there.

Not from `cleanup`, which restores state and produces no evidence
(`litmus/no-verdict-in-cleanup` is the same rule from the verdict's side). Not from
surveys or audits: they already have an artifact channel for what they observe, and an
audit's samples are a time series with its own semantics, which this protocol does not
define. Not from integration hooks, which are deliberately denied `utils`. A call from
any of those, or after the node has settled, is a `ContractViolation` naming the node,
in the pattern of `ContextImmutable`: the record is sealed at the same moment
`run_data` is snapshotted, and nothing writes into a sealed record.

### The package-root alias

Ty's condition on the approval: `from litmus import measure` and `litmus.measure(...)`
must both work. They do, with the same signature and the same rules, and `utils.measure`
remains the home the documentation teaches. The alias is convenience, not a second
mechanism.

The alias has to find the *current* node's `TestUtils` without a global, because a
`parallel` node runs its children concurrently in one process and a global would hand
one child's reading to another. The mechanism is a `contextvars.ContextVar` that the
framework binds around each slot that admits measurements, at the seam in
`src/litmus/base.py` where the child boundary is handed `fn(utils)` for `configure` and
the test method: `set` before the boundary runs the slot, `reset` in `finally`. A
`ContextVar` is task-local, and asyncio copies the context into every task it creates,
so each child of a `parallel` node sees its own binding and a `via` device sees its own
inside its own slots. `litmus.measure` reads the variable and delegates to that
`TestUtils.measure`; when nothing is bound — module scope, a hook, `cleanup`, a helper
called from outside a run — it raises `ContractViolation` saying that `measure` was
called outside a running node and naming `utils.measure` inside `configure` or a test
method as the fix. The self-tests hold the alias to the method: every guard above fires
identically through either spelling.

A `TestUtils` is not otherwise reachable from the package root; the variable is private
to the framework, is never exported, and is read by nothing else.

## The recorded shape

Each call appends one object to the node's `measurements` array, in call order:

```json
{
  "name": "output_voltage_v",
  "value": 5.02,
  "unit": "V",
  "limits": { "low": 4.75, "high": 5.25 },
  "in_limits": true,
  "timestamp": "2026-09-06T21:14:03.118402Z"
}
```

| Field | JSON type | Meaning |
|---|---|---|
| `name` | string | The measurement's identity within the node; unique in the array. |
| `value` | number | The reading, exactly as passed. |
| `unit` | string or null | The developer's label. Never converted. |
| `limits` | object or null | `{ "low": number or null, "high": number or null }`, inclusive; null when the call declared none. |
| `in_limits` | boolean or null | `low <= value <= high` with an open side always satisfied; null when `limits` is null. Evidence, not a verdict. |
| `timestamp` | string | When the reading was recorded, UTC ending in `Z`, the same form the timeline uses. |

The node path, the run, and the station are not repeated in every entry. They are
properties of the node and the run directory the array sits in, and repeating them
would invite a consumer to trust the copy over the tree.

**In two lines:** `measurements` is an array, one object per `measure` call in call
order, each `{name, value, unit, limits: {low, high} | null, in_limits: bool | null,
timestamp}`. It appears on the node's `run_data` and on the node's `summary_tree`
entry; node path comes from the tree, run id from the run directory.

### Where it appears in `result.json`

Two places, additive within result `format_version` 1 — the number does not bump.

1. **`run_data["measurements"]`** on the node's own `RunResult`, the slot reserved
   since Phase 2, now filled. The existing guards stay: an authored ctx entry named
   `measurements` cannot populate or replace it, and an evidence key of that name is
   still an `EvidenceViolation`.
2. **`measurements` on every `summary_tree` node**, beside `evidence` and
   `evidence_declared`. This placement is load-bearing, not decoration. `result.json`
   is the *root* `RunResult.to_json()`, and its `run_data` is the root's ctx view; a
   leaf's `run_data` reaches only that leaf's `RunResult`, which the root record does
   not carry. Without the tree field a leaf's measurements would never reach the file
   the Floor Technician and the SPC consumer open. The precedent is `evidence` on the
   tree node (#118) and `log_path` before it (#106): a per-node fact is published on the
   per-node tree entry.

   `measurements` on a tree node is an array on every node that ran (empty when it
   measured nothing) and `null` on a node that never ran, matching `state`.

Adding a field to a framework-owned object keeps the version under rule 1 of the
versioning policy in `docs/inspector-json.md`; filling a slot the record already
declares open is likewise additive. The self-tests close every framework-owned object
against the schema, so the schema amendment lands in the same MR that first emits a
measurement, as rule 5 requires. The amendment to `docs/schema/result-v1.schema.json`
is a new `$defs/measurement` and two references to it:

```json
"measurement": {
  "type": "object",
  "required": ["name", "value", "unit", "limits", "in_limits", "timestamp"],
  "properties": {
    "name": { "type": "string", "minLength": 1 },
    "value": { "type": "number" },
    "unit": { "type": ["string", "null"] },
    "limits": {
      "anyOf": [
        {
          "type": "object",
          "required": ["low", "high"],
          "properties": {
            "low": { "type": ["number", "null"] },
            "high": { "type": ["number", "null"] }
          },
          "additionalProperties": false
        },
        { "type": "null" }
      ]
    },
    "in_limits": { "type": ["boolean", "null"] },
    "timestamp": {
      "type": "string",
      "format": "date-time",
      "description": "Timezone-aware UTC timestamp ending in 'Z', matching the timeline's own timestamp field."
    }
  },
  "additionalProperties": true,
  "description": "One utils.measure call. in_limits is evidence: it never decides the node's state."
}
```

`result-v1.schema.json` has no shared `utcTimestamp` `$def` of its own — that definition
lives in `docs/schema/timeline-v1.schema.json`, and the two schemas are not cross-
referenced. `measurement.timestamp` therefore states the same `date-time` shape inline
rather than inventing a cross-file `$ref` the published-schema policy does not use
elsewhere.

`run_data.properties.measurements` gains `"items": { "$ref": "#/$defs/measurement" }`
and drops the "empty in version 1" wording; `summaryTreeNode` gains
`"measurements": { "anyOf": [{ "type": "array", "items": { "$ref":
"#/$defs/measurement" } }, { "type": "null" }] }` and adds it to `required`. The
`measurement` object stays open, like every framework-owned object, so a field added
later within version 1 is accepted by a consumer validating with this file.

## Evidence, never a verdict

The framework evaluates the limit and writes `in_limits`. It does not raise, does not
change `state`, does not add a failure, and does not warn the boundary. The three
terminal states keep their meanings (`docs/contracts.md`, Terminal State Contract):
`Pass` means a test method returned a `Verdict` through a gate the developer wrote,
and an `in_limits: false` beside a `Pass` means that gate did not include this limit.
That can be right — a trend-only measurement, a limit still being characterised — and
it can be a suite defect; either way it is the suite author's decision, visible in the
record, and not something the framework second-guesses.

Because a measurement is independent of the verdict, it is sealed whatever the state.
An aborted leaf contributes no *evidence*, since evidence travels with a verdict that
was never produced; the measurements it recorded before the cut were timestamped and
held by the framework at the moment of the call, and they are published with the node.
The consumer reads them beside `state` and decides what an Aborted node's numbers are
worth to a capability index. Feeding them in blind is the consumer's mistake, not a
gap in the record.

### What the Floor Technician reads

In `result.json`, walk `summary_tree` to the claim and read its `measurements` array.
`docs/floor-technician.md` gains one row for it: *The numbers this claim recorded, with
their windows. An entry with `in_limits: false` is a reading outside its window. Read
`state` beside it: it is not a Fail unless `state` says Fail.*

| `state` | `in_limits` on an entry | What it means | What you do |
|---|---|---|---|
| `Fail` | `false` | The claim named that window in its check and the unit missed it. `evidence` on the same node usually carries the same reading. | Hardware. Follow the station protocol for the failure name; the entry tells you by how much. |
| `Pass` | `false` | The claim recorded the number for trend and did not gate on it, or the window is not yet a pass criterion. | Ship on the verdict. Do not scrap the unit on this line alone; route the reading to the process engineer. |
| `Pass` or `Fail` | `null` | Recorded without a window. | Trend only. Nothing to act on here. |
| `Aborted` | anything | The number was taken before the run stopped; no verdict exists. | Follow the Aborted protocol for the failure. The measurement is context, not a category. |

In the timeline, every call writes one scoped line so the chronology carries the number
next to what the suite was doing when it took it:

```
measurement: output_voltage_v = 5.02 V limits=[4.75, 5.25] in_limits=true
measurement: output_voltage_v = 5.31 V limits=[4.75, 5.25] in_limits=false
```

An in-window or unlimited reading logs at `info`; an out-of-window reading logs at
`warning`, so it stands out in `timeline.log` without the FT filtering for it. The line
is a log entry and nothing more: it is not the record (the array is), and it does not
change the node's state.

### Relationship to `@evidence` and `PASS(evidence)`

A measurement is not an evidence key. The two records answer different questions, are
validated by different contracts, and live in different fields:

- `evidence` is what the verdict carried, validated at the child boundary against the
  `@evidence` declaration and the reserved keys, and `null` when there was no verdict.
  Its shape is the developer's mapping.
- `measurements` is what the node recorded, validated at each call, sealed with the
  node whatever its state. Its shape is fixed by this note.

Recording a measurement therefore does not satisfy an `@evidence` declaration, and a
declared key absent from `PASS` is still `EvidenceViolation.not_carried` no matter how
many measurements share its name. A developer who wants a reading held to the
declaration names it in both places, as the rail example above does: `utils.measure`
for the process engineer, `PASS({...})` for the claim. That is one extra name in a dict
literal, and it keeps each record honest on its own terms. The alternative — merging
measurements into `evidence` automatically — was considered and rejected: it would make
the boundary's evidence check depend on calls made earlier in `configure`, would let a
measurement shadow a ctx key by another route, and would give the SPC consumer two
shapes for one number.

The one interaction that exists is the reserved name. `measurements` is not a legal
evidence key and not a ctx key that reaches `run_data`; both guards predate this note
and stay.

## The export contract

The downstream consumer is an SPC or Cpk tool reading a directory of run directories,
or the after-each integrator forwarding each `RunResult` to a database. What it needs,
and where each part lives in the record, is fixed here so the consumer can be written
against this page and the schema alone:

| The consumer needs | Where it is | Stability |
|---|---|---|
| A stable name | `measurements[].name`, unique within the node | The developer's promise; a rename is a new series. |
| A unit | `measurements[].unit` | A label, never converted. Changing it under a name breaks the series. |
| A numeric value | `measurements[].value` | Always a JSON number; never a string, boolean, or non-finite. |
| The limits it was judged against | `measurements[].limits.low` / `.high`, or null | Inclusive; an open side is null. |
| Whether it was inside | `measurements[].in_limits` | Evidence; the consumer does not recompute the verdict from it. |
| A timestamp | `measurements[].timestamp` | UTC, `Z`-terminated, same form as the timeline. Depends on the station clock rule in `docs/floor-technician.md`. |
| The node path | The walk from the root of `summary_tree` to the node; `ancestor_names` on a per-node `RunResult` | Durable identity. Names may contain `/`; do not parse `bread_crumbs`. |
| The run id | The run directory name, `LITMUS_ARTIFACT_ROOT/<timestamp>[.n]`, published by the root timeline's `result` pointer as the directory of `artifact_path` | One directory per `run()`, collision-safe; `result.json` does not repeat it because a consumer has already opened the directory to read the file. |
| The station | `physical_location` on the root | `PHYSICAL_LOCATION`, or `unknown`. |
| The node's verdict | `state` on the same tree node | The consumer keys on it before it aggregates. |

A series is `(station, node path, name)`. Everything the consumer aggregates over is
in `result.json` or in the directory name; it never needs the suite source, the live
objects, or the timeline, though the timeline carries the same numbers in order for a
human.

## Tiers

The tier-1 guards live in the framework, in `TestUtils.measure` and the package-root
alias, and every one of them is a `ContractViolation` (developer bug, `bug: true`,
`banner: CONTRACT VIOLATION`) naming the node and, where there is one, the measurement:

| Guard | Fires when |
|---|---|
| Duplicate name | The node already recorded that name. |
| Not numeric | `value` is a `bool`, a `str`, `NaN`, an infinity, or anything that is not `int`/`float`. |
| Invalid limits | Both sides `None`, a non-finite bound, or `low > high`. |
| Outside a node | `litmus.measure` with nothing bound, or either spelling from `cleanup`, a survey, an audit, a hook, or after the node settled. |

They are raised by a `ContractViolation` subclass in `src/litmus/errors.py` that is
not exported, on the precedent of `ContextInsufficient` and `ContextImmutable`: the FT
reads the banner, `node`, and a `measurement` field naming the reading; the developer
follows the class name from the log. Nothing protecting the record lives only in tier 3.

Tier-3 rules for `litmus-check`, each mirroring a guard above so the finding reaches
the developer's CI before it reaches a station log, are worth adding once #86 has
landed and the call shape is in examples:

| Rule | Flags | Mirrors |
|---|---|---|
| `litmus/measurement-name-unique` | Two `measure(...)` calls with the same literal name inside one `configure` or test method. | Tier 1: duplicate name. |
| `litmus/measurement-in-node` | A `measure(...)` call, either spelling, inside `cleanup`, a survey or audit function, a `before_each`/`after_each`, or at module scope. | Tier 1: outside a node. |

Neither is part of #86; each is its own issue when raised, per `docs/litmus-check.md`
(*Adding a rule*). The `-> Verdict` and `litmus/gated-pass` rules already cover the
gate a developer writes on `.in_limits`.

## What #86 delivers

The acceptance criteria this note fixes for the implementation issue:

1. `TestUtils.measure` with the signature above, synchronous, returning a frozen
   `Measurement`; `measure` and `Measurement` added to `litmus.__all__`, the reference
   table in `docs/api-stability.md` (rows in order), `docs/api.md`, and `CHANGELOG.md`
   together, as `docs/api-stability.md` (*Changing the surface*) requires. Two names,
   not three: the violation class stays private.
2. `litmus.measure` at the package root, bound through a `ContextVar` around
   `configure` and the test method, `ContractViolation` when nothing is bound; the
   self-tests exercise every guard through both spellings and prove a `parallel` node's
   children do not see each other's binding.
3. The four tier-1 guards above, each a `ContractViolation` naming node and
   measurement, with a self-test per guard.
4. The recorded shape, exactly as the table above states it, in call order, sealed when
   the node settles, on every state including Aborted.
5. `run_data["measurements"]` filled on the node's `RunResult`; `measurements` on every
   `summary_tree` node (`null` when the node never ran); both under result
   `format_version` 1, unbumped.
6. `docs/schema/result-v1.schema.json` amended as above in the same MR;
   `tests/test_inspector_schema.py` still closes every framework-owned object.
7. The timeline line, `info` in window and `warning` out of it.
8. Documentation reconciled in the same MR, replacing every "reserved and empty"
   sentence: the `run_data` row and the "Version 1 does not define measurement
   entries" paragraph in `docs/inspector-json.md` plus a `measurements` row in its
   summary-tree table; the `run_data` and summary-tree rows in
   `docs/floor-technician.md` and the table in *What the Floor Technician reads*
   above; the reserved-slot bullets in the Evidence Contract of `docs/contracts.md`
   and a *Measurement Contract* stating the guards; the evidence section of
   `docs/idioms.md` and a short *Measurements* idiom; the `TestUtils` row of the
   reference table. This page then drops its Status header and becomes the owning
   doc.
9. The pi-hole dogfood (`examples/pi_hole_server.py`) records at least one real
   measurement with limits and gates on `.in_limits` in plain sight; `litmus-check
   examples` stays clean.
10. Coverage floors hold or rise; `make check` green locally.

Out of scope for #86, each a future issue if wanted: audit-sample time series,
aggregation of any kind, unit conversion, a `run_id` field on the root record, and the
two tier-3 rules.
