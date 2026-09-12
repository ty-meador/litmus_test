# litmus — Coding Standards

Rules for everyone (human or agent) committing to this repo. The design itself lives in
the port plan (`../PLAN.md` in the workspace); this doc is only about how we write code.
Where a rule is enforceable by tooling, the tooling config in `pyproject.toml` is the
source of truth and this doc is the explanation.

## 1. Toolchain

- **Python 3.14** (pinned in `.python-version`), managed by **uv**. Never `pip install`
  into the venv by hand; edit `pyproject.toml` and run `uv sync`.
- **ruff** formats and lints. **pyright strict** type-checks. **pytest + pytest-asyncio**
  (auto mode) test.
- One entry point: `make`. `make install` once, `make fix` while working,
  `make check` before every push — it is the gate and runs verify-only. **There is no
  CI** (Ty's ruling of 2026-09-10, recorded on #104): the bench run is the gate,
  permanently. Every MR states "green locally" on its head, the reviewer runs
  `make check` again on that head before merging, and nothing arms auto-merge on a
  pipeline.
- **The locked set is audited, not trusted.** `make check` ends with `make audit`:
  `pip-audit` over every pin in `uv.lock`, red on any published vulnerability. It needs
  the network; there is no offline pass. A finding is fixed by upgrading the pin; if an
  advisory truly does not apply, `--ignore-vuln` goes in the Makefile with the issue that
  says why. `make deps-update` (`tools/dependency_update.py`, run weekly at the bench
  with `LITMUS_BOT_TOKEN` exported) refreshes `uv.lock` and opens the update MR itself;
  `make deps-plan` prints the plan and touches nothing.
- **The package is built and smoke-installed by the gate.** `make check` runs
  `make smoke`: `uv build` into `dist/`, then the wheel installed into a clean venv under
  `.smoke/` where `litmus-check --list-rules` and `litmus run examples/hello_world.py`
  must start. A wheel that cannot build, or that imports from `src/` instead of itself,
  is red before it is tagged, so the developer's `pip install` never delivers a package
  that cannot import on the floor. Publishing waits on the name decision (#9) and is a
  bench step of the release checklist, not a pipeline (#138). The docs site is not
  hosted either: `make docs` in the gate proves it builds, GitLab renders `docs/` on
  `main`, and `make docs-release` commits the build under `docs/site/` on the release
  branch, never on `main` (#138).
- The pre-commit hook (installed by `make install`) auto-formats and auto-fixes staged
  files. Don't bypass it with `--no-verify`.

## 2. Type hints — every interface, no exceptions

Every function and method signature, constructor, class attribute, and protocol is fully
annotated: parameters and return types, `-> None` included. Enforced by ruff's `ANN`
rules and pyright strict — an unannotated def fails `make check`.

- Use latest-3.x syntax: `list[str]`, `X | None`, PEP 695 generics (`class Foo[T]:`).
  `ruff`'s `UP` rules push you there automatically.
- `Any` is allowed only where the contract genuinely is "any" (e.g. `dict[str, Any]`
  ctx). Never use it to silence pyright.
- Lifecycle context has one runtime shape: a mutable `dict[str, Any]` (`utils.ctx`).
  The framework annotation for `configure()` is `Mapping[str, Any]` so that a suite may
  narrow that return type to a local `TypedDict` under pyright strict; returning a plain
  `dict[str, Any]` remains the default. The framework copies the return into the dict
  it owns, so the one runtime shape is `utils.ctx`, not the return value. The generic
  `LitmusTest[CtxT]` / `TestUtils[CtxT]` carries that same annotation into the test
  methods as opt-in sugar (issue #75); both parameters default to `Mapping[str, Any]`,
  so a bare `TestUtils` annotation means exactly what it always did, and the object
  exposed at runtime is still the framework-owned dict. Composition — `test`,
  `sequence`, `parallel` — stays shape-agnostic. Do not introduce a context
  dataclass or require every suite to parameterize `LitmusTest`.
- Public packages ship `py.typed`.

## 3. Shape of the code

- **OOP at the surface.** Public logic lives in classes as instance methods (private
  `_method` for internals). No arrow-style lambda wrappers where a method suffices.
  Module level holds classes, constants, true singletons (e.g. the `PASS` sentinel),
  and stateless helper functions — a pure predicate does not earn a class-as-namespace.
  A module-level function is one of three things, and its name says which:
  - **On the public surface by ruling.** A decorator or registrar the developer calls
    by name (`evidence`, #118; `measure`, #86; `cli.main`) sits in `litmus.__all__`
    because a signed ruling put it there. Nothing reaches the surface as a function
    without one; the ruling, not this list, is the authority.
  - **Package-internal, read by a sibling module.** A name outside `litmus.__all__` is
    package-internal. When a sibling imports it, it keeps a plain name: a
    `from litmus.x import _helper` line silences the type checker to say what the
    plain name already says.
  - **Private to its module.** A helper only its own module calls is `_helper`, so the
    reader knows without a search that nothing else depends on it.
- **No nested ternaries.** Ever. Use a default assignment followed by `if`/`elif`.
  A single flat ternary is fine; `x if a else (y if b else z)` is not.
- **Async-first.** All I/O is `async`. Blocking or native calls enter only through the
  framework-owned executor boundary (`asyncio.to_thread` at the router) — never inline in
  a coroutine. ruff's `ASYNC` rules catch the common cases; treat them as errors.
- **Immutability bias.** Follow the framework's own immutable-self idiom in framework
  code too: set attributes in `__init__`, treat instances as frozen afterwards unless the
  class explicitly models mutable state.
- Prefer `pathlib.Path` over `os.path` (`PTH` rules). Prefer f-strings. Line length 100.
- **No runtime import cycles.** A cycle that exists only under `if TYPE_CHECKING:` is
  permitted: those imports never execute and exist so an annotation can name a sibling's
  class. A cycle among the imports that run is not: the module that loses the race sees
  a half-initialised sibling and fails far from the cause. `tools/import_cycles.py`
  refuses the second kind in `make check` and lists the first so it stays visible.

## 4. Naming

- Standard **PEP 8** naming — nothing custom. (pep8-naming `N` rules enforce.)
- Names say what a thing is for the reader who arrives cold: no abbreviations that only
  make sense mid-session, no `mgr`/`util2`/`tmp` survivors.

## 5. Docstrings and comments — written for the next reader

The reader this section serves is the one who arrives cold, cannot ask the author, and
has to decide whether the code in front of them is right. The framework is going public;
that reader is soon a stranger.

- Every public class and method gets a docstring stating its **contract** (what it
  guarantees, what it raises), not a paraphrase of its name.
- Docstrings open with a one-line summary. Parameter documentation, when needed, follows
  as an indented Google-style `Args:` block after a blank line — `Args:` never stands in
  for the summary line.
- Every module under `src/` opens with a docstring naming what the module owns and where
  it sits in the framework. The next reader arrives at a file, not at a class.
- **Why over what.** A comment says why the code is shaped this way and what would go
  wrong otherwise — the constraint, the trade-off, the failure it prevents ("shielded: a
  second cancel here would orphan hardware state"). A comment that restates the next
  line is noise and comes out on review. No changelog comments; git carries the history.
- `examples/` is the one place narration is welcome: the examples exist to teach, and a
  walkthrough serves that. Even there the reasoning leads and the narration carries it.
- Tests document by name: the method name states the contract it pins (§6). A comment
  carries the why only where the name cannot — the race being reproduced, the boundary
  being probed on purpose.
- **The review test.** A reader who did not write the change must be able to state, from
  the diff alone, what each changed unit is for and why it is shaped that way. Where the
  reviewer has to reconstruct that intent from the code, the intent goes into the code —
  not into the MR thread, and not into a promise to comment later. An MR that fails this
  test is held with revision requested, whatever else it carries.
- **What the gate checks.** ruff's `D` rules (Google convention) run in `make check`:
  every public module, class, function, and method under `src/` and `tools/` carries a
  docstring, shaped as above. `pyproject.toml` is the source of truth for the exact set;
  the exemptions there say why each exists (magic methods, `__init__`, the D1 coverage
  rules for test modules, `tests/` and the stub `litmus new` writes). The gate checks presence and shape; whether the words state the
  contract is still the reviewer's call.

## 6. Tests

- **Test-first is the methodology of this port** — the legacy framework shipped with
  zero self-tests; we do not repeat that. New behavior lands with its tests in the same
  commit.
- Tests live in `tests/`, mirror the `src/litmus/` module layout, and are written as
  classes (`class TestX:` with test methods), consistent with §3.
- Async tests are plain `async def` methods — pytest-asyncio auto mode picks them up.
- `make check` green is the definition of done. No skipped/xfail tests without a linked
  reason.
- **Coverage is a floor that only rises.** The gate runs the suite under branch coverage.
  The global floor is `[tool.coverage.report] fail_under`; the per-file floors on the
  kernel of trust (`verdict.py`, `base.py`, `lifecycle.py`, `executor.py`) are
  `[tool.litmus.coverage.floors]`, enforced by `tools/coverage_floor.py`. Measured below
  a floor is red. When a change lifts coverage, raise the floor to the new measurement in
  the same MR — the gate prints the hint. A floor never moves down: an MR that lowers one
  is not merged, whatever else it carries. The report lives under `.coverage/`, ignored
  by git and outside the evidence guard's `artifacts/`.
- **One green run proves nothing about a timing race.** A change that touches asyncio
  timing, cancellation, hook scheduling, or transport teardown is attested with
  `make soak` (the full suite eight times over; `SOAK_RUNS=n` overrides), not a single
  pass — a race can fail 7 of 8 on one machine while another wins it every time. The
  reviewer runs the soak again on the MR head before merging such a change; there is
  no second machine unless a person runs one. The nightly soak is a bench run of the
  full suite and the rack suite many times over on `main`
  (`tools/nightly_soak.py`: `make nightly` runs and prints, `make nightly-record`
  also records the night on issue #68 with `LITMUS_BOT_TOKEN` exported); a red night
  names the failing case and run in #general before an MR meets it. The `nightly-soak`
  job in `.gitlab-ci.yml` is the same driver behind a pipeline schedule that stays
  switched off (Ty, 2026-09-11: no pipeline minutes to spend on it); a night counts
  only once it is posted, whichever machine ran it. Until a schedule runs again, the
  Phase 6 exit is one recorded bench soak green on the release candidate head.
- **Generated coverage must remain replayable.** `make check` and `make soak` retain
  the fixed Hypothesis seed in `pyproject.toml`. The nightly driver (`make nightly`)
  draws a fresh seed for every pytest invocation and writes the exact replay command
  to stdout and the first line of each `.soak/*.log` before starting it. To reproduce
  that generated coverage, check out the recorded revision, sync its lockfile, and run
  the logged command. A seed reproduces generated inputs, not event-loop scheduling.
- **Coverage proves a line ran, not that a test would notice it breaking.** `make
  mutants` attacks `verdict.py`, `base.py`, and `lifecycle.py` — the child boundary
  that turns a suite's return into a non-forgeable verdict — with `mutmut`
  (`[tool.mutmut]` in `pyproject.toml`), flipping one operator, condition, or literal
  at a time and re-running the scoped test files. A surviving mutant means some
  behavior in the kernel of trust has no test watching it; each one is closed with a
  killing test or, where the mutation is behaviorally identical to the original
  (dead code, an unreachable branch), recorded as equivalent with its reasoning in the
  MR that ran the audit. `make mutants` is an audit, not a gate: it is not part of
  `make check` — mutation runs are slow and its job is to find gaps for a human to
  close, not to fail the gate.

## 7. Workflow

- **`main` is the trunk, and it is locked by design** — `push = No one`, merge restricted
  to maintainers, force-push disabled. The only way in is branch, then merge request.
- Branch from `main` per task, named for the work (`phase-0-verdict`, `fix-abort-shield`,
  …). No personal prefixes. The branch is deleted when its MR merges.
- Every change reaches `main` through an MR — no exceptions, and no direct pushes. Link
  the issue the branch closes so the board stays honest about what is done.
- **Merge authority rests with Commander Zavala.** He reviews each MR against `PLAN.md`
  (the design) and this document (the code shape), and makes the call on what enters
  `main`. Open the MR and leave it for that review; do not merge your own.
- Design reviews apply the [Golden Rule](docs/idioms.md#the-golden-rule): when authoring
  feels awkward, identify the idiom or problem-domain boundary being fought before proposing
  a new framework feature.
- A change that adds, removes, renames, or re-types a name in `litmus.__all__`, weakens
  a contract in `docs/contracts.md`, or bumps a record `format_version` is a change to a
  promise. It is raised as a `needs-sign-off` issue for Ty before its MR opens, and the MR
  updates `__all__`, the reference table in `docs/api-stability.md`, the documenting
  section, and `CHANGELOG.md` together; from 1.0.0 a removal takes the deprecation path
  first. `tests/test_public_surface.py` refuses an MR that updates one without the others.
- Commits: imperative subject line, ≤72 chars, body explains *why* when non-obvious.
  Run `make check` before pushing — it is the gate, and an MR that fails it is not merged.
- Never commit: `.venv/`, caches, artifacts, secrets, real hostnames/credentials from
  test racks. `uv.lock` **is** committed.
