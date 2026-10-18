# litmus-check — Tier 3 Static Analysis

`litmus-check` is the third tier of the enforcement stack described in
[overview.md](overview.md#the-three-tier-enforcement-stack). It reads authored suite
files as Python ASTs and reports departures from the declarative idiom.

## Skippable by design

| Tier | Mechanism | Guarantees | Skippable? |
|---|---|---|---|
| 1 | Runtime IoC wrapping | Verdict contract, failure taxonomy, lifecycle order, immutable-self freeze | Never |
| 2 | `__init_subclass__` at import | Structural contract: sealed internals, lifecycle signatures, hook shape | Never |
| 3 | `litmus-check` | Declarative syntax: pure `build_suite`, gated `PASS`, immutable-self idiom, constructor-only hooks, live composition names, ctx provenance, fulfilled evidence, routes declared in `configure`, measurements named once and recorded where a node runs, one source per `configure()` value | **Yes** |

**Nothing that protects hardware or verdict integrity lives only here.** Every rule
below either has a tier-1 or tier-2 counterpart that holds whether or not the linter
ever runs, or guards a style alone and says so in its row. A suite that skips
`litmus-check` loses early, readable feedback at edit time; it does not lose a guard.
That is the whole reason this tier may be skipped and the other two may not.

## Usage

```sh
uv run litmus-check suites/                 # check a directory (recursive)
uv run litmus-check suites/blade.py other/  # files and directories mix freely
uv run litmus-check --list-rules            # the rule catalogue
uv run litmus-check --select litmus/gated-pass suites/
uv run litmus-check --ignore litmus/pure-build-suite suites/
uv run litmus-check --format json suites/   # machine-readable report
python -m litmus_check suites/              # same command without the script
```

Output is one line per finding, `path:line:col: code message`, followed by a count.

### Exit status — the CI contract

| Status | Meaning |
|---|---|
| `0` | Every checked file is clean. |
| `1` | At least one rule violation. |
| `2` | A file could not be checked (missing path, unreadable, invalid Python), or the invocation was wrong (unknown rule code, no paths). |

A path that cannot be parsed is an error, never silence: a suite that did not parse
was not checked. Unknown rule codes fail the same way so a typo in a CI job cannot
quietly disable a rule. `make check` runs the tool over `examples/`, which is the
scaffolding suite authors copy from, so the examples always demonstrate the idiom.

## Rules

| Code | Idiom | Runtime counterpart (the guarantee) |
|---|---|---|
| `litmus/gated-pass` | Every `return PASS` sits inside an `if` branch or `match` arm; the pass predicate is visible where the PASS is. | Tier 1: the child boundary accepts only the `PASS` identity and refuses to forge a verdict. |
| `litmus/pure-build-suite` | `build_suite` is one `return` of composition calls: no other statements, no conditionals, filtered comprehensions, lambdas, `await`, or walrus inside; an unfiltered comprehension enumerates leaves (one per core, rail, or channel) and is allowed. | Tier 2: `__init_subclass__` requires `build_suite` with the right shape; tier 1 refuses anything but a `LitmusTest` from it. |
| `litmus/immutable-self` | `self.<attr>` is assigned or deleted only in `__init__`; runtime state returns from `configure()` into `utils.ctx`. | Tier 1: the instance freezes after the most-derived `__init__`; assignment raises `AttributeError`. |
| `litmus/constructor-hooks` | `use_survey` and `use_audit` are called only in `__init__`; hooks are identity, not runtime state. | Tier 1: both registrars raise `ContractViolation` on a frozen instance. |
| `litmus/unannotated-interface` | Functions and methods inside suite classes annotate every explicit parameter and return; methods passed to `self.test` declare `-> Verdict`. | Tier 1 for the `-> Verdict` half: the child boundary accepts only the `PASS` identity and refuses to forge a verdict. The parameter half is authoring clarity only; an annotation guards nothing at runtime. |
| `litmus/raise-test-failure` | A test method raises `TestFailure`, not a plain `ValueError` / `RuntimeError` / `Exception`. | Tier 1: the failure taxonomy classifies any non-`TestFailure` exception escaping a test method as a developer-bug Fail. |
| `litmus/method-reference` | `self.test` takes a method reference, not a `lambda` or nested def. | Tier 2: method references passed to `test` are auto-bound; the wrapper is unnecessary. |
| `litmus/retired-names` | `self.case` and `self.group` are gone; compose with `self.test`, `self.sequence`, and `self.parallel` (issue #110). | Tier 1: the attributes no longer exist, so `build_suite` raises `AttributeError` before any hardware is touched; the rule names the replacement instead. |
| `litmus/no-verdict-in-cleanup` | `cleanup` does not `return PASS` or `raise TestFailure`; it restores hardware state. | Tier 1: cleanup's return is discarded and its exceptions are swallowed. |
| `litmus/ctx-provenance` | Every `utils.ctx["k"]` read in a test method, hook, `configure`, or `cleanup` names a key that `configure()` returns (its dict literal or `TypedDict`), and ctx is never written: no subscript assignment, `update`, `setdefault`, `pop`, or `clear` anywhere in the suite. A reading travels with the verdict, and `.get()` is how an optional key is read (issues #112, #118). | Tier 1: a `[]` read of a key `configure()` did not provide, and any write after `configure()` returns, raise `ContractViolation` naming the test and the key; `get` and `in` never do. |
| `litmus/shell-in-configure` | `utils.use_shell(...)` or `utils.use_serial(...)` with arguments, the call that declares where this node's `utils.exec` lands, appears only in `configure()`; the bare `use_shell()` / `use_serial()` read and `get_shell` / `get_serial` may appear anywhere (issue #76). | Tier 1: a declaring call (`host`, `via`, `inherit=False`, `propagate=False`) from any slot but `configure` raises `ContractViolation` before the router is touched, so the `exec routing:` line written after `configure` is the route every command of the node ran under. |
| `litmus/evidence-fulfilled` | Every `return PASS({...})` in a method decorated `@evidence(...)` carries every declared key, and such a method never returns bare `PASS` (issue #118). | Tier 1: the child boundary holds every `PASS` to the declaration; a missing key is a `ContractViolation` naming the leaf and the key, a developer-bug Fail. |
| `litmus/measurement-name-unique` | Two `measure(...)` calls, either spelling, with the same literal name inside one `configure` or test method; a measurement's series is keyed on its name, so the second call would overwrite or fork it (issue #132). | Tier 1: the duplicate-name guard in `TestUtils.measure` and `litmus.measure` raises `ContractViolation` naming the node and the measurement. |
| `litmus/measurement-in-node` | A `measure(...)` call, either spelling, inside `cleanup`, a method registered with `use_survey`/`use_audit`, a `before_each`/`after_each` hook (by name, or passed under either keyword), or outside any function; a reading belongs to the node whose `configure()` or test method is running (issue #133). | Tier 1: the outside-a-node guard raises `ContractViolation` naming the node and the measurement for `litmus.measure` with nothing bound and for either spelling from `cleanup`, a survey, an audit, a hook, or after the node settled. |
| `litmus/no-input-fallback` | Inside `configure()`, a value is identity from the constructor or discovered through `utils`, never one with the other as fallback: no `self._ip or await utils.get_arp_resolver(...).resolve()`, no conditional expression choosing between them, no local name bound from a `self` attribute by one assignment and from `get_arp_resolver` / `use_shell` / `get_shell` / `use_serial` / `get_serial` / `get_http` / `get_coap` by another. Identity fed into discovery and identity read from `utils.env` stay (issues #142, #144, #147). | None, by design: a fallback is a style the ruling forbids, not a guard on hardware or a verdict. The runtime records which utility provided a value either way; the rule moves the ruling to the editor. |
| `litmus/env-via-utils` | Suite code reads the environment through `utils.env` (`utils.env["K"]`, `.get`, `.require`), never `os.environ` or `os.getenv`, under any import alias; `utils.env` is the run's recorded, injectable snapshot and `os.environ` is invisible to the evidence. | None can exist: `os.environ` is the interpreter's. The rule is the only gate, so it stays narrow and names the replacement. |
| `litmus/logger-not-print` | Suite code logs through `utils.logger`, whose entries land in the run's timeline; a builtin `print` inside a `LitmusTest` subclass reaches only the process's stdout, which the framework does not capture. | None: the framework cannot intercept the builtin without changing what `print` means everywhere, so the rule is the gate. |
| `litmus/model-resolution-failure` | An awaited `.resolve()` / `.reacquire(...)` / `.request(...)` inside a suite class sits in a `try` with a handler that raises `TestFailure`; `ArpResolutionFailure` and `DeviceApiOperationFailure` are not verdicts, and the suite models the miss instead of leaking it. | Tier 1: the failure taxonomy classifies the escaping exception as a developer-bug Fail ("THIS IS A BUG IN ..."), which is correct and unhelpful; the rule moves the fix to the editor and names the exception to catch. |
| `litmus/no-swallowed-cancel` | Inside an `async` method, a `try` whose body awaits is not guarded by a bare `except`, `except BaseException`, or `except CancelledError` that fails to re-raise with a bare `raise`; `contextlib.suppress` never names either. `asyncio.CancelledError` is the abort signal, and a handler that swallows it mints a verdict after the harness gave up. Catch `Exception` for the failure you modeled. | Tier 1: the cancellation discipline of issue #38 shields evidence collection and expects suite code to propagate; this rule is the editor-time half. |

Run `litmus-check --list-rules` for the catalogue shipped in the installed version.
Each rule's module docstring under `tools/litmus_check/rules/` states exactly what it
flags, what it leaves alone, and why.

## Adding a rule

The registry is extensible without touching the CLI:

1. Add a module under `tools/litmus_check/rules/`.
2. Define a class deriving from `litmus_check.Rule` with a stable `code`
   (`litmus/<kebab-name>`), a one-line `summary`, and a `check(module)` method that
   yields `Violation` values via `module.violation(node, code, message)`.
3. Land its tests in `tests/` alongside the others, with a seeded fixture under
   `tests/fixtures/litmus_check/` so the CLI is proven end to end.

`RuleRegistry.discover()` imports every module in the package and instantiates every
concrete `Rule` subclass it finds. `SuiteModule` carries the structural queries a rule
needs — enclosing function, method, and class, the methods a class registers by
reference (`use_survey`, `use_audit`, `test`), and a static answer to "is this class a
`LitmusTest`" — so a rule is only its own idiom.

A rule's `code` is a contract with suite authors' `--ignore` flags; rename it only with
a note in the changelog of the MR that does so.

## Editor — the pylsp adapter

The same registry the CLI runs is a `python-lsp-server` plugin. Install the extra so
the core package stays dependency-light:

```sh
uv pip install 'litmus[pylsp]'
# or, from a checkout: uv sync --extra pylsp
```

`python-lsp-server` discovers the plugin via the `pylsp` entry point. Every rule in
`litmus-check --list-rules` is published as a diagnostic on the open buffer (unsaved
contents included). Two rules offer a quick-fix:

| Code | Action |
|---|---|
| `litmus/gated-pass` | Wrap the `return PASS` in an explicit pass predicate. Preceding `if ...: raise` guards are inverted into that predicate only when they sit flush against the return in its own block (a return in a loop's or `try`'s `else:` or in `finally:` never borrows the body's guards), load no name bound at the insertion point, and contain no call. Every other shape gets `if False:` plus a `TestFailure` raise — a leftover scaffold cannot return PASS. |
| `litmus/method-reference` | Replace a trivial `lambda` (`lambda u: self._claim(u)`) with the method reference. A lambda that carries extra logic is left alone — that logic belongs in a real method. |

Disable the plugin in pylsp config with `plugins.litmus_check.enabled: false`.

## What this tier is not

- It is not a type checker. Suites are held to the same annotation standard as the
  framework (STANDARDS.md §2); `litmus/unannotated-interface` makes the interface
  explicit, and pyright strict remains the authority on whether those types agree.
- It is not a second checker. The editor plugin is the CLI's registry with an LSP
  surface; a finding in the editor is the same finding `litmus-check` would print.
- It is not a guard. See the table above.
