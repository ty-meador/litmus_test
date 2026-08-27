# litmus

React-for-hardware HIL testing framework — Python port. Rack-scale (~70 DUT) mass
production testing on asyncio structured concurrency, with the verdict contract enforced
at runtime.

## Getting started

```sh
make install   # uv sync + git hooks
make fix       # auto-format + auto-fix lint
make check     # CI gate: format check, lint, pyright strict, pytest
```

Read `STANDARDS.md` before committing. The port design lives in the workspace plan
(`../PLAN.md`); the TypeScript original is the `../litmus-test-legacy/` design reference,
and its language server is `../litmus-language-server-legacy/`. Both are read-only —
parity with them is explicitly not a goal. `PLAN.md` section 0 maps the workspace.
