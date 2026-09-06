# litmus new — the suite generator

`litmus new` writes a gated-PASS suite and a matching self-test so the author starts
from a shape that is already correct, annotated, and gated. Examples stay the
lessons; this is the scaffolding they used to be copied from.

```sh
uv run litmus new WidgetSuite
uv run litmus new PowerRail --directory suites/
uv run litmus new WidgetSuite --force
```

The command writes two files next to each other:

| File | What it is |
|---|---|
| `widget_suite.py` | One `LitmusTest` subclass named `WidgetSuite` |
| `test_widget_suite.py` | A pytest stub that constructs it from its name and asserts Pass |

## What is public

The command line is public (`docs/api-stability.md`): the class name, the two files,
`--directory`, `--force`, and the exit status (`0` wrote the files, `3` usage error).
The Python module behind it is not.

## The shape it writes

The generated suite is the single source for this shape. Every file `litmus new`
emits has:

- an annotated `__init__` whose extra parameters carry defaults, so `litmus run`
  can construct the class from its name alone
- `configure` returning a narrowed `TypedDict` ctx
- a pure `build_suite` that returns `self.test(...)`
- one test method whose `return PASS` sits inside an explicit pass predicate
- `cleanup`

A missing target after construction is `TestFailure` from `configure()`: "did not
reach a testable state". That is the ruled meaning of a failure there
(`docs/contracts.md`).

The generated files pass `litmus-check`, ruff, and pyright strict unmodified on the
day they are written. Teaching examples in `examples/` are verified against the core
of this shape (annotated `__init__`, `build_suite`, gated `return PASS`) rather than
regenerated from it — they carry lessons the template does not.

## Exit status

| Status | Meaning |
|---|---|
| `0` | Both files were written. |
| `3` | **Usage error.** The name is not a suite class name, the directory does not exist, or the files already exist and `--force` was not given. Nothing was overwritten. |

A name must be a Python identifier, not a keyword, and start with an uppercase
letter (`WidgetSuite`, not `widget_suite` and not `class`). Existing files are
refused unless `--force` is passed, so a second invocation cannot clobber a suite
the author has started editing.
