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
  `make check` before every push — it is the CI gate and runs verify-only.
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
- Lifecycle context has one runtime shape: a mutable `dict[str, Any]`. Accordingly,
  the framework annotation for `configure()` is `dict[str, Any]`; a suite may narrow
  that return type to a local `TypedDict` when pyright precision is useful. A future
  generic `TestUtils[CtxT]` may carry that same annotation as opt-in sugar, but it must
  still expose the framework-owned dict at runtime. Do not introduce a context
  dataclass or require every suite to parameterize `LitmusTest`.
- Public packages ship `py.typed`.

## 3. Shape of the code

- **OOP only.** Logic lives in classes as instance methods (private `_method` for
  internals). No floating module-level functions, no arrow-style lambda wrappers where a
  method suffices. Module level holds classes, constants, and true singletons (e.g. the
  `PASS` sentinel).
- **No nested ternaries.** Ever. Use a default assignment followed by `if`/`elif`.
  A single flat ternary is fine; `x if a else (y if b else z)` is not.
- **Async-first.** All I/O is `async`. Blocking or native calls enter only through the
  framework-owned executor boundary (`asyncio.to_thread` at the router) — never inline in
  a coroutine. ruff's `ASYNC` rules catch the common cases; treat them as errors.
- **Immutability bias.** Follow the framework's own immutable-self idiom in framework
  code too: set attributes in `__init__`, treat instances as frozen afterwards unless the
  class explicitly models mutable state.
- Prefer `pathlib.Path` over `os.path` (`PTH` rules). Prefer f-strings. Line length 100.

## 4. Naming

- Standard **PEP 8** naming — nothing custom. (pep8-naming `N` rules enforce.)
- Names say what a thing is for the reader who arrives cold: no abbreviations that only
  make sense mid-session, no `mgr`/`util2`/`tmp` survivors.

## 5. Docstrings and comments

- Every public class and method gets a docstring stating its **contract** (what it
  guarantees, what it raises), not a paraphrase of its name.
- Comments state constraints the code can't show ("shielded: a second cancel here would
  orphan hardware state"). No narration, no changelog comments.

## 6. Tests

- **Test-first is the methodology of this port** — the legacy framework shipped with
  zero self-tests; we do not repeat that. New behavior lands with its tests in the same
  commit.
- Tests live in `tests/`, mirror the `src/litmus/` module layout, and are written as
  classes (`class TestX:` with test methods), consistent with §3.
- Async tests are plain `async def` methods — pytest-asyncio auto mode picks them up.
- `make check` green is the definition of done. No skipped/xfail tests without a linked
  reason.

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
- Commits: imperative subject line, ≤72 chars, body explains *why* when non-obvious.
  Run `make check` before pushing — it is the gate, and an MR that fails it is not merged.
- Never commit: `.venv/`, caches, artifacts, secrets, real hostnames/credentials from
  test racks. `uv.lock` **is** committed.
