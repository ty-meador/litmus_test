# Floor Technician guide — reading a result without reading source

This is the station page. You opened a run because something stopped the line.
You do not need the suite source, a Slack thread, or a software engineer to
categorize what happened. You need `result.json`, the scoped log next to it, and
the recovery protocol below.

The Inspector TUI is not in this port yet. Until it is, you read the same two
records the Inspector will: the structural tree in `result.json`, and the
temporal log at each node's `log_path`. Every field below is the field in
`docs/inspector-json.md`, explained from this side of the bench.

**Clock rule.** A station whose system clock is not on NTP cannot produce
trustworthy evidence. Do not execute tests on it. The log is used as chronology
in customer disputes.

## Where to look

A completed run always writes a timestamped directory under
`LITMUS_ARTIFACT_ROOT` (or `./artifacts` if that variable was not set). Inside
that directory:

| You want | File |
|---|---|
| The verdict and the tree | `result.json` at the run-directory root |
| What happened, in order, for a node | that node's `timeline.jsonl` (the Inspector record) and `timeline.log` (the same events as readable lines) |
| A captured screenshot, dump, or similar | the path in an artifact-pointer log line |
| A CI-flavored projection of the same tree | the JUnit XML the runner wrote with `--junit`, if anyone asked for one. It is not a second record. |

`result.json` is present for Pass, Fail, **and** Aborted. Absence of the file
means the invocation never reached a run (usage error, exit 3) — there is no
hardware story to read.

The last entry in the root node's timeline points at `result.json` with artifact
name `result` and type `run-result`. You do not have to guess the run directory
name from the clock.

## Sixty-second recovery protocol

Open `result.json`. Read `state` at the root, then `failure`. That pair is the
category. Then follow the protocol. A human still makes the final call; the
framework's job is to make the call obvious.

| Root `state` | What `failure` looks like | Category | What you do |
|---|---|---|---|
| `Pass` | `failure` is `null` | The claim held | Ship. You are done. |
| `Fail` | `name` is `TestFailure` (or a suite-authored subclass), `bug` absent or `false` | **Hardware.** The unit did not meet a check the suite named. | Pull the unit. Follow the station protocol for that failure name. The `fail_message` is written in your language; `expected` / `received` are the comparison when there is one. |
| `Fail` | `banner` is `CONTRACT VIOLATION`, or `bug` is `true` | **Author.** The suite broke its contract. This is not the unit. | Do not scrap the unit on this evidence. Escalate to the suite author. The message names the node and the fix. |
| `Fail` | `banner` is `FRAMEWORK CONTRACT VIOLATION` | **Framework.** Should be unreachable. | Escalate to the framework maintainers with the full run directory. Not the unit, not the suite author. |
| `Aborted` | `name` is `ExternalIntegrationFailure` | **Integration / harness.** The shop-floor DB, the coordinator, the hook around the suite. | Fix the harness. Re-run. The unit was not judged. |
| `Aborted` | `name` is `MissingVerdict`, or `bug` is `true` with `state` `Aborted` | **Author.** The test method returned nothing the framework will accept as a verdict. | Escalate to the suite author. Do not treat this as a hardware Fail. |
| `Aborted` | `name` is `OperatorInterrupt`, `operator` is `true` | **Operator.** Someone sent SIGINT or SIGTERM. | The rack was stopped on purpose. Not a Fail, not a harness outage. Re-run when the line is ready. `signal` names which one. |
| `Aborted` | `timeout.declared` is `abort_after` | **Budget.** The node's abort clock fired. | The framework refused to forge a Fail. Read `timeout.clock` for which node declared the budget, then decide with the log whether the unit hung or the budget was tight. |

A `TestFailure` raised from `configure()` means **the device did not reach a
testable state**. Hardware vs infrastructure is **not knowable from the file**.
You decide that, using the message, the survey artifacts, and what you can see
on the bench.

Do not collapse Fail and Aborted. A Fail says check the hardware. An Aborted
says no verdict was produced — check the harness, the author, or the operator,
not the unit, unless you have other evidence.

## How to find the node that matters

The root object is one node. The tree of everything that ran is
`summary_tree`. Walk `children` in declaration order.

Use **`ancestor_names`** as the node's identity. It is the root-to-this path as
an array of names. Names may contain `/`, so the array is the durable form.

Use **`bread_crumbs`** only to display that path. It is exactly
`ancestor_names` joined with `/`. Do not parse it back into names.

A node whose `summary_tree` `state` is `null` never ran (a sequence stopped
before it). That is not a fourth terminal state. It has no log and no failure.
The declared tree is still complete, so you can see what was skipped.

## Every field, from this side of the bench

### The root result (`result.json`)

| Field | What you do with it |
|---|---|
| `format_version` | Read this first. Version `1` is this page. A number you do not know is not a result you can guess at — refuse it and get a decoder that names the versions it accepts. |
| `state` | The root verdict: `Pass`, `Fail`, or `Aborted`. This is the first word of the recovery protocol. |
| `failure` | Why this node is not Pass, or `null`. Shape below. |
| `timeout` | `null` unless a time budget determined or contributed to this state. Shape below. |
| `test_case` | Stable name of this node. The root's name is usually the suite class name. |
| `type` | The node's shape in the suite: `suite`, `test`, `parallel`, or `sequence`. A `test` is one claim. A `sequence` stops at the first failure. A `parallel` runs children together and keeps the worst state. |
| `ancestor_names` | Identity. Root-to-this, inclusive. |
| `bread_crumbs` | Display path only. |
| `summary_tree` | The whole declared tree, rooted here. This is the structural view. |
| `physical_location` | Station / slot name from `PHYSICAL_LOCATION`, or `unknown` if that variable was not in the run environment. |
| `env` | The run environment as the run declared it. Secrets in an unfiltered environment are in this object verbatim — that is an operator obligation, not a framework rewrite. With `environment=DEBUG` the allow/deny list is ignored and a warning is in the log. |
| `run_data` | Suite-authored evidence snapshot. Version 1 always has a `measurements` array; it is reserved and empty until the measurements protocol lands. Do not invent SPC meaning from an empty array. Other keys are the suite's. |

### A summary-tree node

| Field | What you do with it |
|---|---|
| `name` | This node's name. Combine with parent names via `ancestor_names` on the matching `RunResult` when you have one; in the tree, the path is the walk from the root. |
| `type` | Same discriminator as the root: `suite`, `test`, `parallel`, `sequence`. |
| `state` | Terminal state, or `null` if this step did not run. |
| `failure` | Failure for **this** node, or `null`. A parent can be Fail because a child is; look at the child that actually failed. |
| `timeout` | Time-budget evidence for **this** node, or `null`. |
| `log_path` | POSIX path of this node's `timeline.jsonl`, relative to the run directory. `null` means the records rolled up into an ancestor, or the node never ran. Resolve it against the run directory you opened. The root always has one. |
| `children` | Ordered children. Groups keep declaration order after settlement. |

### The failure object

Failures keep their framework name so you can tell the cases apart. Unknown
fields are additive; read what you know.

| What you see | Meaning |
|---|---|
| `name` | The failure class: `TestFailure`, a suite subclass (`DimmFailure`, …), `ContractViolation`, `FrameworkContractViolation`, `ExternalIntegrationFailure`, `MissingVerdict`, `OperatorInterrupt`, `ArpResolutionFailure`. |
| `fail_message` | The sentence you read out loud. Hardware copy for a `TestFailure`; a banner plus a fix for a contract break; "operator interrupted…" for a Ctrl-C. |
| `state` | `Fail` or `Aborted`, matching the taxonomy. |
| `expected` / `received` | Present only when the suite supplied a comparison. Omitted, not nulled, when the failure is self-evident. `received` is what the suite observed. |
| `banner` | `CONTRACT VIOLATION` (author) or `FRAMEWORK CONTRACT VIOLATION` (maintainers). |
| `bug` | `true` means broken software, not broken hardware. |
| `node` | Which node broke the contract. |
| `cause` | Nested failure or a `{name, message}` for a plain exception. Follow it; do not stop at the wrapper. |
| `hook` | `before_each` or `after_each` on an `ExternalIntegrationFailure`. That is the harness hook that threw. |
| `operator` | `true` on `OperatorInterrupt`. The operator stopped the rack. |
| `signal` | `SIGINT`, `SIGTERM`, or `signal N`. |
| `returned` | What a `MissingVerdict` node actually returned instead of PASS. |
| `key` / `owner` | On a context-insufficient contract break: the ctx key that was never published, and which `configure()` owed it. |
| `mac` / `host` | On `ArpResolutionFailure`: the MAC that was not in the table, and which host was asked. |

Live handles (an open SSH session, a dict with a weird key) degrade to a
string rather than making the file unreadable. If `received` looks like
`<SshInterface host=dut-12>`, that is the degraded form, not a missing value.

### The timeout object

`timeout` is `null` when no deadline contributed. Otherwise:

| Field | What you do with it |
|---|---|
| `reason` | Always `timeout`. This object exists because a clock fired. |
| `declared` | Which clock won: `fail_after` or `abort_after`. |
| `budget_seconds` | How long that clock was willing to wait. |
| `elapsed_seconds` | How long the node had actually been active when it fired. |
| `clock` | Root-to-declaring-node path of the winning clock. The budget may have been declared on a parent. |

`fail_after` also puts a hardware-facing `TestFailure` in `failure`.
`abort_after` may leave `failure` null — the timeout record **is** the reason
the node is Aborted. Do not report those two the same way.

### The timeline (`timeline.jsonl`)

One complete JSON object per line. You may read it while a run is still live;
keep every complete line after an interrupt. `timeline.log` is the same events
as escaped human lines.

| Field | What you do with it |
|---|---|
| `format_version` | Timeline schema version; currently `1`. Independent of the result's version. |
| `timestamp` | Timezone-aware UTC, ending in `Z`. This is the chronology. |
| `scope` | Root-to-emitting-node path. Same idea as `ancestor_names`. |
| `level` | Logging level (`info`, `warning`, `error`, …). |
| `message` | The line. Newlines stay escaped inside the JSON. |
| `artifact_name` | Present only on pointer lines. The captured artifact's name, or `result` for the root result. |
| `artifact_path` | Absolute path of that file. Open it. |
| `artifact_type` | What kind of capture, or `run-result` for `result.json`. |
| `artifact_timestamp` | When that evidence was taken, UTC. |

Ordinary log lines omit the four `artifact_*` fields rather than setting them
to null. A pointer line has all four.

Pending nodes create no files. Leaves roll their records into the nearest
ancestor that owns a log. If a node has a terminal `state` and `log_path` is
null, read the nearest ancestor that has a `log_path`.

## Operator interrupt, in the file

A Ctrl-C or a SIGTERM during a live run is **not** a Fail and **not** a harness
outage.

In `result.json`:

- root `state` is `Aborted`
- `failure.name` is `OperatorInterrupt`
- `failure.operator` is `true`
- `failure.signal` is `SIGINT` or `SIGTERM`
- `failure.fail_message` contains `operator`

In the timeline: the same story in order, including any artifacts flushed
before the signal. The run directory is still the record; `result.json` is
still written.

If `operator` is missing or false, it was not the operator. Do not guess.

## Two records, two version numbers

`result.json` and `timeline.jsonl` each carry their own `format_version`. They
move independently. The tree points at the log through `log_path`.

Within version 1, new fields may appear. Ignore fields you do not know; do not
fail the read. A changed meaning, a renamed field, or a moved file would bump
the number. From 1.0.0 that bump is a contract change. Before 1.0.0 the record
may still change in place under version 1.

The published schemas live next to this guide:

- `docs/schema/result-v1.schema.json`
- `docs/schema/timeline-v1.schema.json`

You do not have to validate against them on the station. They exist so a
decoder can refuse a version it was not written for, loudly.

## What this page is not

It is not the authoring guide (`docs/idioms.md`, `docs/contracts.md`). It is
not the runner's exit-code table (`docs/litmus-run.md`) — though exit `0`/`1`/`2`
match Pass/Fail/Aborted, and exit `3` means there is no `result.json` because
no run happened. It is not the Inspector TUI; when that lands, it will consume
exactly these files.
