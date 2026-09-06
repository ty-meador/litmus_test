# Changelog

All notable changes to litmus are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version numbers follow
the policy in `docs/api-stability.md`. Before 1.0.0 nothing is promised; the trail is
kept so that it exists before the promise does.

## Unreleased

### Added

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

### Changed

- A shell a node opened with `use_shell` / `get_shell` now stays connected through that
  node's post-cleanup survey and closes after it, before the via lease is released. It
  used to close inside cleanup, so a post-cleanup survey on a self-owned hop settled
  `remote interface is not connected` and recorded an empty artifact while the same
  survey on an inherited via hop read the device (issue #115).
- The five locally executable examples run through `litmus run`: their constructor
  parameters carry the lesson's values as defaults, and the standalone wrapper classes
  and `__main__` blocks are gone (issue #73). The SSH-backed examples keep theirs
  until their targets move into `configure()`.
- A `[]` read of a `utils.ctx` key that `configure()` did not provide raises
  `ContractViolation` ("context insufficient") naming the test and the key, instead of a
  bare `KeyError`; `get`, `in`, and `setdefault` are unchanged. `TestUtils(ctx=...)` now
  copies the seed into the run's context rather than adopting the dict (issue #112).
- Composition vocabulary settled as `self.test`, `self.sequence`, `self.parallel`;
  `self.case` and `self.group` removed outright, and `litmus-check` gained
  `litmus/retired-names` to point at the replacement (issue #110).
- Per-node time budgets: `fail_after` and `abort_after` on any node, subtree-bound and
  tighten-only (issues #59, #60).

### Removed

- `self.case` and `self.group` (issue #110). Pre-1.0: no deprecation period.
