# litmus run — the runner

`litmus run` is the one command the developer documents and the Floor Technician
types. It loads a suite, runs it through the sealed lifecycle, and exits with a code
the station script branches on without parsing output.

```sh
uv run litmus run examples/hello_world.py
uv run litmus run examples/test_composition.py::ParallelDevice
uv run litmus run station.suites.psu --artifact-root /var/lib/litmus/psu-0042
uv run litmus run suite.py --json > result.json
uv run litmus run suite.py --junit report.xml
```

## What is public

Two things are promised (`docs/api-stability.md`): the **exit status contract** below,
and the **`--json` record**, which is the same `format_version` 1 result the framework
writes as `result.json`, governed by the policy in `docs/inspector-json.md`. The
Python module behind the command is private.

## Exit status

| Status | Meaning |
|---|---|
| `0` | The root settled **Pass**. |
| `1` | The root settled **Fail**. |
| `2` | The root settled **Aborted**. |
| `3` | **Usage error.** The invocation was wrong, or the suite could not be loaded, selected, or constructed. No run took place, no evidence was written. |

There is no fourth terminal state and no fourth run-related code. A station script
treats `0` as ship, `1` as reject, `2` as re-run after the operator looks, and `3` as
a suite-authoring or configuration fault that never reached hardware.

```sh
uv run litmus run suites/psu.py --artifact-root "$RUN_DIR"
case $? in
  0) echo ship ;;
  1) echo reject ;;
  2) echo "aborted; see $RUN_DIR" ;;
  *) echo "suite could not run" >&2 ;;
esac
```

## Selecting the suite

`TARGET` is `<path-or-module>[::SuiteClass]`.

- A source ending in `.py`, or naming an existing file, is **loaded from disk**,
  standalone. Anything else is **imported by dotted name** through the normal import
  path (`examples.hello_world`, `station.suites.psu`).
- Without `::SuiteClass`, the module must **define exactly one** `LitmusTest`
  subclass. A module that defines none, or more than one, is a usage error whose
  message lists what it found.
- With `::SuiteClass`, the named attribute may be **any** `LitmusTest` subclass bound
  in the module, imported or defined. A station module may therefore re-export the
  suite it runs.

The runner constructs the selected class as `SuiteClass(name=SuiteClass.__name__)`.
Every other `__init__` parameter must carry a default; a constructor that needs more
is a usage error naming the parameter. A suite that reads its target from the
environment does so in `configure()` through `utils.env`, where the value is
published as evidence in the same motion (`docs/idioms.md`, TestUtils Injection).

## Evidence

- **`--artifact-root DIR`** sets the evidence root for this run and overrides
  `LITMUS_ARTIFACT_ROOT`. Without it, the variable applies, then `./artifacts`.
  The run directory, the per-node timelines, and `result.json` land beneath it as
  `docs/inspector-json.md` describes.
- **`--json`** prints the `format_version` 1 result record to **stdout** and nothing
  else goes there, so `> result.json` captures exactly the record.
- **`--junit PATH`** also writes the run as JUnit XML through `litmus.junit.JunitXml`
  (`docs/junit-xml.md`). The runner owns the reporter; a harness that imports the
  module directly does so off the promised surface. A `PATH` that cannot be written
  is an error line on stderr; the exit status stays the verdict's, because the run
  settled and `result.json` under the artifact root is the record.
- The **verdict line** and the **run log** go to **stderr**:
  `litmus run: <node>: <state>` with the failure class and message when there is one.
  The log is the scoped logger at `INFO`, the same lines the per-node `timeline.log`
  records, so the person at the station watches the run as it happens. Python
  warnings, including `DeprecationWarning`, are routed through the same log.

## Operator interrupt

`SIGINT` or `SIGTERM` during a run is an operator run-abort (`docs/contracts.md`,
Operator Interrupt): transports close hard, every unsettled node records the
operator's name, `result.json` is still written, and the exit status is `2`.
