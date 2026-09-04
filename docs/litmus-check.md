# litmus-check — Tier 3 Static Analysis

`litmus-check` is the third tier of the enforcement stack described in
[overview.md](overview.md#the-three-tier-enforcement-stack). It reads authored suite
files as Python ASTs and reports departures from the declarative idiom.

## Skippable by design

| Tier | Mechanism | Guarantees | Skippable? |
|---|---|---|---|
| 1 | Runtime IoC wrapping | Verdict contract, failure taxonomy, lifecycle order, immutable-self freeze | Never |
| 2 | `__init_subclass__` at import | Structural contract: sealed internals, lifecycle signatures, hook shape | Never |
| 3 | `litmus-check` | Declarative syntax: pure `build_suite`, gated `PASS`, immutable-self idiom, constructor-only hooks | **Yes** |

**Nothing that protects hardware or verdict integrity lives only here.** Every rule
below has a tier-1 or tier-2 counterpart that holds whether or not the linter ever
runs. A suite that skips `litmus-check` loses early, readable feedback at edit time;
it does not lose a guard. That is the whole reason this tier may be skipped and the
other two may not.

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

Run `litmus-check --list-rules` for the catalogue shipped in the installed version.
Each rule is documented in its module under `tools/litmus_check/rules/`.

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
needs — enclosing function, method, and class, and a static answer to "is this class a
`LitmusTest`" — so a rule is only its own idiom.

A rule's `code` is a contract with suite authors' `--ignore` flags; rename it only with
a note in the changelog of the MR that does so.

## What this tier is not

- It is not a type checker. Suites are held to the same annotation standard as the
  framework (STANDARDS.md §2), and pyright strict is how that is enforced.
- It is not the language server. The CLI landed first; a pylsp adapter over the same
  registry is the planned follow-on.
- It is not a guard. See the table above.
