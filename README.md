# litmus

React-for-hardware HIL testing framework — Python port. Rack-scale (~70 DUT) mass
production testing on asyncio structured concurrency, with the verdict contract enforced
at runtime.

## Getting started

```sh
make install   # uv sync + git hooks
make fix       # auto-format + auto-fix lint
make check     # CI gate: format check, lint, pyright strict, pytest, litmus-check
```

`litmus run` is the runner (`docs/litmus-run.md`): it loads a suite file or module,
runs the one `LitmusTest` subclass it defines (or the one named after `::`), and exits
0 for Pass, 1 for Fail, 2 for Aborted, 3 when the suite could not be run at all. The
locally executable examples progress from one explicit claim, through composition, to
polling, lifecycle hooks, and the evidence surface:

```sh
uv run litmus run examples/hello_world.py
uv run litmus run examples/test_composition.py::SequencedDevice
uv run litmus run examples/test_composition.py::ParallelDevice   # exits 1 by design
uv run litmus run examples/wait_for_patterns.py::ConfigureGatedDevice
uv run litmus run examples/wait_for_patterns.py::MainAssertionDevice
uv run litmus run examples/hardware_device_lifecycle.py
uv run litmus run examples/run_data_evidence.py
```

`--artifact-root DIR` points the run's evidence somewhere specific, `--json` prints the
`format_version` 1 result record to stdout, and `--junit PATH` writes the JUnit XML
projection beside it.

The pi-hole dogfood suite is the end-to-end proof against a real hop. It needs
`TARGET_ADDRESS` and `TARGET_USER`; `TARGET_PASSWORD` and `TARGET_PORT` are optional:

```sh
uv run python examples/pi_hole_server.py
```

`wait_for_patterns.py` demonstrates both readiness polling in `configure()` and verdict
polling in a test method. `hardware_device_lifecycle.py` registers survey and audit
hooks around a rail that comes up, is watched, and is dropped in cleanup.
`run_data_evidence.py` shows ctx flowing into `run_data` and a computed `within_spec`
override. `networked_device.py` is the MAC-to-IP lesson (`utils.get_arp_resolver` in
`configure`, locally or through a `via` hop) and needs `DUT_MAC` on the ARP table;
`GATEWAY_HOST` and `GATEWAY_USER` resolve through a hop instead.

Read `STANDARDS.md` before committing. `docs/` carries the framework documentation —
`overview.md` (purpose, terminal states, pit of success), `idioms.md` (authoring
patterns), `contracts.md` (the non-negotiable developer↔framework invariants),
`inspector-json.md` (the versioned downstream result contract, with its JSON Schemas
under `docs/schema/`), `junit-xml.md` (the
JUnit projection of that result for CI), translated from the legacy docs and updated
to the port's semantics, `litmus-run.md` (the runner and its exit status contract),
`litmus-check.md` (the skippable tier-3 linter over authored suites), and
`api-stability.md` (the public surface, semantic versioning from 1.0.0, and the
deprecation procedure). `CHANGELOG.md` at the root records every change
to that surface.

## Deployment requirement

Every test VM must synchronize its system clock with NTP before Litmus runs. The scoped
log is the authoritative chronology for a run, including structured pointers to captured
artifacts, and may be used to establish event order in customer disputes. A station with
an unsynchronized clock cannot produce trustworthy evidence and must not execute tests.

Set `LITMUS_ARTIFACT_ROOT` to the run-specific evidence directory. Point-in-time evidence
is an immutable `Artifact` value and has no public file-writing method; persist it only
with `await utils.capture_artifact(artifact)`, which writes the file and then appends its
pointer to the scoped log timeline.

The port design lives in the workspace plan
(`../PLAN.md`); the TypeScript original is the `../litmus-test-legacy/` design reference,
and its language server is `../litmus-language-server-legacy/`. Both are read-only —
parity with them is explicitly not a goal. `PLAN.md` section 0 maps the workspace.

## License

Copyright 2026 Tyler Meador. Litmus is free software, released under the GNU General
Public License, version 2 (`GPL-2.0-only`). The full text is in `LICENSE`; the copyright
notice is in `NOTICE`. There is no warranty; see the license for the terms.
