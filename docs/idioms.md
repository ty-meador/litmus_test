# Framework Idioms & Patterns

## The Golden Rule
If implementing something feels awkward or "ugly", you are fighting the framework. Seek the idiomatic approach.

Treat that friction as a design-review signal, not a style preference and not immediate
evidence that the framework needs another feature. Ask:

> What established Litmus idiom is this design fighting, and which problem-domain concept
> would let composition, lifecycle boundaries, or `utils` express the intent coherently?

Resolve that question before expanding the public API. A new abstraction is warranted only
when the existing idioms cannot express a real manufacturing-test concept without obscuring
evidence, hardware ownership, or verdict integrity. Convenience alone is not sufficient.

The most important application is the React boundary in the port plan's section 1. Litmus
borrows React's component model—composition, inversion of control, hooks, and local state—not
its runtime model. Reconciliation, re-rendering, and reactivity are out of scope because a
hardware test executes once and may produce irreversible physical side effects. If a proposal
feels awkward because it tries to import that runtime model, the friction identifies a crossed
boundary, not a missing Litmus feature.

## Immutable-self Convention
After `__init__` runs, `self` is read-only. What the tests need flows through configure's return value into `utils.ctx`, and what they observed leaves with the verdict. This eliminates hidden state coupling and makes the data flow explicit.

**Rules:**
- `__init__` sets read-only config (MAC, credentials, site, etc.)
- `configure()` returns what the tests will read, as a plain dict — the framework merges it into `utils.ctx` and closes it
- test methods, `cleanup()`, and all child test bodies read from `utils.ctx`, not from `self`; every key they read with `[]` is one `configure()` returned, and a key it did not provide is read with `.get()` or not at all — a missing `[]` read is a `ContractViolation`, not a `KeyError` (issue #112)
- `cleanup()` reads every key with `.get()` or `in`, never `[]`: it also runs when `configure()` raised before it returned, and then ctx holds nothing `configure()` would have provided. A `[]` read there logs a `ContractViolation` beside the real Fail (issue #152)
- No `self.field = value` after construction, and no write to `utils.ctx` after `configure()` — a reading travels with the verdict, `PASS({...})`, and a thing that changes mid-run (an address, a connection) is an object in ctx that manages itself (issue #118)

**Why:** Mutable state, on `self` or in ctx, creates hidden coupling between lifecycle phases. When inputs flow through ctx and readings flow out with the verdict, the data path is explicit and traceable. The Floor Technician benefits because each leaf's record carries exactly what that leaf handed back, beside the limits it judged against.

**Enforcement:** This is a tier-1 runtime guard, not merely a lint convention.
`LitmusTest` freezes only after the most-derived `__init__` returns, so subclasses may
finish declaring their identity after `super().__init__()`. Ordinary assignment and
deletion then raise `AttributeError`. Framework-owned lifecycle and evidence transitions
use `object.__setattr__` directly; suite code receives no framework escape-hatch method.
The static no-self-mutation rule remains defense in depth.

**Scope note:** ctx is strictly instance-local. A parent's ctx never flows into composed children — composition is the only coupling mechanism, and ctx inheritance would smuggle hidden state across it. One leaf never reads another's reading either: what a sibling observed is on that sibling's record, not in ctx.

## Evidence Travels With the Verdict
A test method is a function from `utils.ctx` to a verdict. Its readings leave with the
verdict: `PASS({...})` on the pass path, `TestFailure(..., evidence={...})` on the fail
path. `@evidence` is the method's return signature — it names the keys a pass must
carry, beside the code that keeps the promise (issue #118).

```python
@evidence("output_voltage_v")
async def _check_voltage(self, utils: TestUtils) -> Verdict:
    v = await self._read(utils, utils.ctx["ip"])
    reading = {"output_voltage_v": v}
    if v <= utils.ctx["voltage_high_v"]:
        return PASS(reading)
    raise TestFailure(f"{v} V exceeds limit", evidence=reading)
```

- The declaration is fixed at class definition and never computed at run time. Pass
  criteria that vary by SKU or fixture are a leaf argument or a different leaf, decided
  before `build_suite` runs.
- A pass without its declared evidence is not a pass: the child boundary refuses it as a
  `ContractViolation` naming the leaf and the key. Extra keys are allowed; the
  declaration is a floor. A failure is not held to it, since a failure may fire before
  every reading was taken; it records what it is handed.
- The payload is validated entry by entry. A value that is not JSON, or a key that shadows
  a ctx entry or the framework-owned `measurements` slot, is a `ContractViolation` naming
  the key. Nothing is stripped silently.
- Ctx and evidence are one namespace, because they land on one record. A key
  `configure()` returns names the limit or the target (`kernel`, `domain`, `voltage_high_v`);
  a key `@evidence` declares names the reading judged against it (`kernel_running`,
  `queried`, `output_voltage_v`). The same word cannot do both, so pick the evidence name
  with the ctx key in view. `litmus-check` flags the clash on the declaring literal as
  `litmus/evidence-shadows-ctx` (bench finding F14, issue #161).
- A measurement is not evidence. `utils.measure` records a number beside the verdict
  (see *Measurements* below); it does not satisfy an `@evidence` declaration, so a claim
  that wants the reading in both records names it in both.
- An undecorated method may still return `PASS(payload)`; the payload is recorded
  unchecked. Declare when you want to be held to it.
- The same decorator accepts a `TypedDict`. With `-> Verdict[MyEvidence]` on the method,
  pyright checks the `PASS({...})` payload against the shape at write time; at run time
  the `TypedDict`'s required keys are the declaration.

```python
class VoltageEvidence(TypedDict):
    output_voltage_v: float


@evidence(VoltageEvidence)
async def _check_voltage(self, utils: TestUtils) -> Verdict[VoltageEvidence]: ...
```

`litmus-check` enforces the declaration statically as `litmus/evidence-fulfilled`, the one
namespace as `litmus/evidence-shadows-ctx`, and the read-only context as part of
`litmus/ctx-provenance`.

## The Parent Owns the Route

The node that opens a hop owns it, and everything composed under it inherits it. A
gateway is a parent node: its `configure()` calls `utils.use_shell(host=..., ...)`,
and its `build_suite()` composes the devices behind the hop. The framework routes the
gateway's own `utils.exec` through that hop and hands the route to every child, so a
device's configure, surveys, tests, and audits all land on the far side without the
device saying so. A device class never takes a routing argument; it is written once
and reused on every bench.

```python
class Gateway(LitmusTest):
    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        await utils.use_shell(host="10.0.0.1", username="ops")
        return {}

    def build_suite(self) -> LitmusTest:
        return MyDevice(name="target", mac="aa:bb:cc:dd:ee:ff")
```

`MyDevice` run on its own reads the test host; composed under `Gateway` it reads the
gateway. Same bytes. A child that must run locally declares `use_shell(inherit=False)`
in its own `configure()`; a parent that must not hand its route down declares
`propagate=False`. The hop stays up until the parent's own teardown, after every
child has cleaned up — a child that fails or expires loses only the transports it
opened itself, never its parent's hop. A gateway that cannot come up Fails in its
own `configure()`, and the children it never composed stay pending: the bench was
not ready, so the device was never asked.

The only `via` in the framework is the tunnel argument to `use_shell` / `get_shell`
(see SSH Tunnel below). That is the parent's tool for reaching through a jump host
inside its own `configure()`; it is never a child's burden.

### The Node Names What It Needs; the Framework Decides Where It Comes From

Ruled on issue #142 (2026-09-12). A constructor argument is identity, fixed at
composition time: a name, a MAC, the SKU-specific pass criteria. Runtime state comes
from `utils` in `configure()`, as one call with no fallback: the IP, the shell, the
tunnel, the resolver, the device API. When a parent and a child both could provide the
same thing, that thing belongs in `TestUtils`, where the framework inherits it down the
tree and records which way it went; it never travels through ctx or the constructor. A
fallback in `configure()` is the smell that says a utility is missing.

```python
# Not allowed: two sources of truth for one ctx key, a branch the suite engineer
# wrote, and a record that cannot say which path produced the value.
ip = self._ip or await utils.get_arp_resolver(mac=self._mac).resolve()
```

The framework already does this twice. The route flows down through `use_shell` and is
inherited by every composed child; the ARP resolver is scoped to the node's current
route and its cache is keyed by the routing label, so devices behind one gateway share
ARP state without the suite engineer writing anything to arrange it.

Identity reaches a root node through `utils.env` when no parent composed it: the
`DUT_MAC` a bench-run device reads is the same MAC a rack parent passes to the
constructor, and the run's environment is published on the record either way (see
Environment Variables below). That is the run's declared input, not discovery, and it
is what keeps the bench-run device and the composed one the same bytes.

## run_data — Automatic Structured Evidence
The default `run_data` returns a detached snapshot of all serializable values from
`utils.ctx`: the ctx view. JSON primitives (with finite numbers), plain dicts with string
keys, and lists survive; class instances (`SshInterface`, etc.), functions, live handles,
non-finite floats, and cycles are stripped automatically. A leaf's `RunResult.run_data` is
that view plus the evidence its verdict carried, flat, so the limit sits beside the
reading; the leaf's `summary_tree` node carries the same evidence and its declaration.
A non-leaf node's `run_data` is the ctx view alone.

Stripping is deterministic and all-or-nothing at each top-level ctx entry. If a list or
dict contains one invalid nested value, that complete entry is absent from `run_data`.
Litmus does not remove only the handle and leave a plausible-looking partial record.

A value the claims need but the record must not carry, such as a session credential
obtained in `configure()`, is named in `redact_ctx` on the suite class (issue #154).
Each entry is a ctx key, exact or as a regular expression the key matches whole. The
key appears in `run_data` as `<REDACTED>`, and the string under it is masked on every
record surface, including a log line or a `TestFailure` the author hands it to. The
record shows that the key was held; it never shows what. ctx itself is unchanged: the
claims read the real value.

```python
class BlocklistRoundTrip(LitmusTest):
    redact_ctx = ("sid", r"api_.*")

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        sid = await self._login(utils)
        return {"sid": sid, "api_token": await self._token(utils, sid), "domain": "x.test"}


# RunResult.run_data => {
#     "measurements": [],
#     "sid": "<REDACTED>",
#     "api_token": "<REDACTED>",
#     "domain": "x.test",
# }
```

```python
# configure returns data — it appears in run_data automatically
async def configure(self, utils: TestUtils) -> dict[str, Any]:
    ip = await self._resolve()
    return {"ip": ip, "hostname": "switch-01"}


# RunResult.run_data => {
#     "measurements": [],
#     "ip": "10.0.0.1",
#     "hostname": "switch-01",
# }
```

Override `run_data` only when you need computed or filtered output from the context.
An override sees the ctx view, not the readings, which are merged when the leaf settles:

```python
from litmus import JsonObject


@property
def run_data(self) -> JsonObject:
    base = super().run_data
    return {**base, "within_spec": self._check_spec(base)}
```

**Note:** non-JSON-safe objects (`datetime`, sets, arbitrary classes) are stripped by
default. Override `run_data` if you need custom serialization for these types, and
return only JSON-safe data from the override.

Every snapshot includes `run_data["measurements"]`, the list `utils.measure` fills. It
is framework-owned: a ctx entry with that key does not populate or replace it, and it
is written after any `run_data` override runs, so an override cannot drop it.

## Measurements: `utils.measure`

A reading with a unit and a window that a process engineer will chart belongs in
`utils.measure`, not only in the evidence payload. The call records and returns; the
verdict stays whatever the gated `PASS` says, because a limit is evidence, never a
verdict (`docs/measurements.md`):

```python
from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict, evidence


class Rail(LitmusTest):
    @evidence("output_voltage_v")
    async def _output_rail(self, utils: TestUtils) -> Verdict:
        volts = await self._read_rail(utils)
        rail = utils.measure("output_voltage_v", volts, unit="V", limits=(4.75, 5.25))
        if rail.in_limits:
            return PASS({"output_voltage_v": volts})
        raise TestFailure("rail outside window", "4.75-5.25 V", f"{volts} V")
```

The gate reads in plain sight, so `litmus/gated-pass` sees the predicate and the next
reader sees the criterion. Name the quantity once per node, in the unit it will always
carry; the consumer keys its series on that name. Record for trend without a window by
passing `limits=None`. Measure from `configure()` (a bring-up reading) or the test
method only; `cleanup`, surveys, audits, and hooks are refused. `from litmus import
measure` is the same call resolved to the running node when a helper does not have
`utils` in hand.

When the reading comes over a shell, parse it to `float | None` and hand `measure` the
`None` instead of wrapping the parse in a `try`: a reading not taken is recorded as
such, it is never inside its window, and the same gate fails the claim (issue #159).
Keep `NaN` out of the call; that is a bug in the suite's arithmetic, not a blank from
the device, and the framework refuses it.

## Composition Factories: `self.test`, `self.sequence`, `self.parallel`

`LitmusTest` exposes three instance factories for building the suite. The names are
the vocabulary a suite reads aloud in: a leaf is a *test*, dependent children run in
*sequence*, independent children run in *parallel*. There is exactly one spelling for
each; `case` and `group` are retired (issue #110) and `litmus/retired-names` flags them.

- `self.test(name, fn, *, fail_after=None, abort_after=None)` — the leaf: one verdict
  method, named here at composition time. Defaults to `discrete_log_file=False` so
  entries roll up to the parent artifact.
- `self.sequence(name, children, ...)` — serial execution, bails on first failure, and
  owns a discrete timeline by default.
- `self.parallel(name, children, ...)` — concurrent execution via `asyncio.TaskGroup`,
  collects all failures, and owns a discrete timeline by default. The name says so:
  nobody misses that two children share a bus.

Pass `discrete_log_file=False` to `self.sequence()` or `self.parallel()` when that
composition is too small to merit a separate Inspector timeline. Its records then roll
into the nearest owning ancestor, just as leaves do. Authored `LitmusTest`
subclasses also default to a discrete timeline and may opt into roll-up through
`super().__init__(..., discrete_log_file=False)`. The root always owns a log so every
run has an authoritative temporal backbone.

**Private methods pass as references directly — Python bound methods carry `self` for free.**

```python
def build_suite(self) -> LitmusTest:
    return self.sequence(
        self.name,
        [
            self.test("check reachable", self._check_reachable),
            self.test("check firmware", self._check_firmware),
        ],
    )


async def _check_reachable(self, utils: TestUtils) -> Verdict:
    # `self` is preserved — bound at attribute access, no wrapping needed
    result = await utils.exec("true")
    if result.exit_code == 0:
        return PASS
    raise TestFailure("device unreachable", "exit 0", str(result.exit_code))
```

Every `return PASS` sits under the predicate that earns it; `litmus-check` (the
`litmus/gated-pass` rule) flags a bare one, and the examples show the gated shape.

### Enumerate Leaves at Runtime

A leaf's name is decided where the tree is composed, not where the method is defined.
One method, many leaves: pass the value that varies straight to `self.test()` after the
method, and name each leaf in a comprehension. The framework calls
`fn(utils, *args, **kwargs)`; `utils` is always first, and the developer's arguments
follow. `fail_after` and `abort_after` are the leaf's budget, never the method's
parameters. The arguments are checked against the method's signature when the tree is
built, so a leaf that could never be called fails `build_suite` with a
`ContractViolation` before any hardware is touched.

```python
class Compute(LitmusTest):
    def __init__(self, *, name: str, cpu_count: int) -> None:
        super().__init__(name=name)
        self._cpu_count = cpu_count

    async def _cpu_stress(self, utils: TestUtils, *, cpu: int) -> Verdict: ...

    def build_suite(self) -> LitmusTest:
        return self.parallel(
            "stress",
            [
                self.test(f"cpu{i}_stress", self._cpu_stress, cpu=i, fail_after=120.0)
                for i in range(self._cpu_count)
            ],
        )
```

The comprehension maps a known population (every core, every rail, every channel) onto
leaves; it does not choose among them. A comprehension with an `if` filter decides at
build time which claims exist, and `litmus/pure-build-suite` flags it. The population
is identity, like a hook: it comes in through `__init__`, never out of `configure()`.

### `self.test()` or `configure()`?

A long operation such as a firmware flash or a provisioning step is a `self.test(...)`
when the Floor Technician should see it in the report with its own verdict and its own
budget. When it is preparation the report should not itemize, it belongs in
`configure()`, where a `TestFailure` means "the device did not reach a testable state"
(see overview.md) and the node reads Fail without a phantom leaf. Ask whether the Floor
Technician acts differently when this step fails on its own line. If so, it is a leaf.

## Time Budgets: Name the Meaning of “Too Long”

Declare a budget on the composition line that owns the work. Use `fail_after` when
missing the time limit is a negative hardware verdict, and `abort_after` when the
harness should stop waiting without claiming the hardware failed.

```python
def build_suite(self) -> LitmusTest:
    return self.parallel(
        "rack claims",
        [
            self.test("BMC ready", self._bmc_ready, fail_after=45.0),
            self.test("optional telemetry", self._telemetry, abort_after=10.0),
        ],
        fail_after=120.0,
    )


result = await rack.run(abort_after=300.0)
```

The run budget bounds the whole rack. The parallel node and its leaves may tighten that
bound; they cannot lengthen it. If the 45-second leaf is reached with only 8 seconds left on the
run clock, the run clock fires after those 8 seconds and its `abort_after` state applies.
The resulting `timeout` evidence names `reason="timeout"`, the declared budget, active
elapsed time, and the root-to-node path of the winning clock.

The timed window is configure through the test method or last child. Cleanup retains
its separate finalization allowance. Root-owned `before_each`/`after_each` hooks are
excluded from every time-budget clock, so a slow shop-floor database cannot manufacture
a hardware Fail. In a parallel node, however, a sibling still inside its timed
window keeps the shared ancestor clock moving; another sibling's hook or cleanup cannot
lend it extra time. Do not catch or suppress `asyncio.CancelledError` around awaited
work.

A deadline closes only transports opened by the cut node. An inherited route remains
the parent's resource; this is what keeps a timed-out DUT from taking down a healthy
sibling behind the same gateway.

A gateway parent may declare the same budget pair. Its clock begins when it enters
`configure`; if the hop is not open by then, the Fail or Aborted state and its timeout
evidence are the gateway's own, and the devices behind it are never asked.

## Pass/Fail/Aborted Contract
- A test method returning `PASS`, or `PASS(evidence)` with its readings → test passes (state `Pass`). The check is `isinstance(result, Verdict)`, and only `PASS` mints one. The string `"PASS"` does not pass, and a `PASS` missing a key its `@evidence` declared is a contract violation, not a pass.
- A test method raising `TestFailure` (or subclass) → test fails with structured data (state `Fail`)
- A test method raising any other exception → BUG-signaled Fail: the developer didn't model this failure mode
- A test method returning anything other than the `PASS` sentinel (including falling through with `None`) → node-local `Aborted`. No verdict was produced, so the framework refuses to forge one. Siblings are unaffected.
- An integration hook (`before_each` / `after_each`) throwing anywhere in the tree → state `Aborted` on the enclosing node AND every ancestor

`Aborted` is a peer state to `Pass` and `Fail`, not a flavor of failure. It signals that no verdict was produced — either the harness around the test broke, or the test method finished without rendering one. The Floor Technician's recovery protocol is "check the integration layer / the test method," not "check the hardware."

## Constructor-Only Hooks: `use_survey` and `use_audit`

Survey and audit hooks are registered in `__init__`, not in `configure()`. They are stackable — multiple calls register multiple independent hooks, each firing on its own schedule.

```python
class ComputeBlade(LitmusTest):
    def __init__(self, params: TestParams) -> None:
        super().__init__(params)

        # Passive snapshot at every lifecycle phase boundary
        self.use_survey(self._dmesg_snapshot)

        # Active monitoring that can fail the test
        self.use_audit(interval=5.0, fn=self._check_thermals)
```

**Why constructor-registration, not configure-registration?**
Survey and audit are part of the test's *identity* — stable across runs, visible on the class — not runtime-configured state. Registering them in the constructor makes them discoverable and keeps `configure()` focused on runtime wiring.

Survey errors are swallowed (never affect the verdict). Audit errors are real failures — a throwing audit cancels in-flight test methods immediately and fails the test.

**Scheduling a survey.** A survey fires at all six `Phase` boundaries unless the
constructor says otherwise. A survey that reads the device through `utils.exec` has no
device to read at `pre-configure`, before `use_shell` has run; its dispatch there execs
on the test host and records the developer's machine as device evidence. Say which
boundaries the survey observes, and guard the route:

```python
from litmus import LitmusTest, Phase, TestUtils


class PiHole(LitmusTest):
    def __init__(self, *, name: str) -> None:
        super().__init__(name=name)
        self.use_survey(
            self._ftl_tail,
            phases=[phase for phase in Phase if phase >= Phase.POST_CONFIGURE],
        )

    async def _ftl_tail(self, utils: TestUtils) -> object:
        if utils.exec_route == "local":
            return None  # no device on the other end of exec at this boundary
        return (await utils.exec("tail -n 50 /var/log/pihole/FTL.log")).stdout
```

Both halves are needed. The phase list is the schedule; the route check is the guard.
Order is not reach: when `configure` fails before `use_shell` returns, the engine
skips `post-configure`, `pre-test`, and `post-test` and still fires `pre-cleanup` and
`post-cleanup`, and at those two `utils.exec` is on the test host. `utils.exec_route`
answers `"local"` there and the hop's endpoint (`"10.10.10.10"`) once the route is
up, so a survey that compares against it never tails the wrong machine. An empty
`phases=` is a `ValueError` at registration, not a survey that silently never fires.

Survey artifacts are named for the lifecycle dispatch point and hook index. Audit
artifacts use the same `survey` artifact type and add the audit hook and dispatch indexes
to the name. Every non-`None` return is captured, including empty containers. Python
cannot distinguish an explicit or delegated `None` return from fall-through at runtime,
so `None` writes only an informational log entry. A hook that needs to preserve a
deliberate null observation calls `await utils.capture_artifact(Artifact(..., data=None))`
directly. Once a hook produces an observation, framework shutdown waits for its artifact
publication within the bounded evidence budget, even under repeated cancellation. Audit
intervals must be positive and finite, and the framework stops every audit loop as soon
as the authored suite settles.

See `examples/hardware_device_lifecycle.py` for survey and audit around a device
with real state transitions — rail up in `configure`, thermal audit during the claim,
rail down in `cleanup`.

## Integration Hooks: `before_each` / `after_each` on the Run Params

For composing Litmus into a larger harness (shop-floor DB, lab coordinator, audit pipeline), pass `before_each` and `after_each` to the root `run()` call:

```python
await root.run(
    before_each=lambda ctx: shopfloor_db.record_started(
        fn=ctx.bread_crumbs,
        station=ctx.env.get("STATION_ID"),
        at=datetime.now(UTC),
    ),
    after_each=lambda ctx: shopfloor_db.record_finished(
        fn=ctx.bread_crumbs,
        state=ctx.result.state,
        failure=ctx.result.failure.fail_message if ctx.result.failure else None,
        at=datetime.now(UTC),
    ),
)
```

Register once at the root — the framework fires them around every executed test in the tree. The hook context is deliberately narrow (node name, `ancestor_names`, `bread_crumbs`, `logger`, `env`) — NOT `exec` or `get_shell`. Integration hooks are for external coordination, not for exercising the hardware.

**Pair invariant:** `after_each` fires even when `before_each` threw. Register them as a pair or pass `allow_unpaired_hooks=True` to opt out of the warning.

The exception-path behavior is: throw from either hook → run halts → every enclosing node transitions to `Aborted` → every in-flight SSH session and subprocess is killed immediately. A 30-minute firmware flash mid-abort must NOT continue to mutate the hardware.

## Configure-Returns-Context Pattern
`configure()` returns a plain dict; the framework merges it into `utils.ctx` for test methods, survey hooks, `cleanup()`, and all descendant leaves, then closes it. No instance fields are needed for configure-phase data.

The return value is the contract with the test methods: it names every key they read with `[]`, and it is all they will ever find there. Seed no placeholders — a reading belongs on the verdict, not in ctx — and nothing writes ctx after configure returns. `litmus-check` enforces both statically as `litmus/ctx-provenance`; at run time a `[]` read of a missing key, or any later write, is a `ContractViolation` naming the test and the key, while `.get()` and `in` read an optional key (issues #112, #118).

The framework annotation is `Mapping[str, Any]`; a plain `dict[str, Any]` return is the
default and satisfies it. When a suite benefits from precise key types, define a local
`TypedDict` and use it as the covariant return annotation — pyright strict accepts the
override because a `TypedDict` is a `Mapping`, which `dict[str, Any]` as the base
annotation would not allow. This is static information only: a `TypedDict` is still an
ordinary mutable dict at runtime, and the framework copies it into `utils.ctx`. A context dataclass is deliberately out of contract because it would fight
dict merge semantics and require special handling in the default `run_data` path.

```python
class DeviceContext(TypedDict):
    ssh: RemoteInterface
    token: str


async def configure(self, utils: TestUtils) -> DeviceContext: ...
```

### Carrying the same type into the test methods

A `TypedDict` return tells the reader the shape, but `utils.ctx` still hands the test
methods a `Mapping[str, Any]`, so a misspelled key survives pyright and reaches the
floor as a `ContextInsufficient` contract violation — a Fail under the bug banner,
where the author meant to take a reading (issue #112). Naming the type once on the
class closes that gap: declare
`class MySuite(LitmusTest[MyCtx])` and annotate the lifecycle methods
`utils: TestUtils[MyCtx]`, and `utils.ctx["volts"]` is a `float` to the editor while
`utils.ctx["volt"]` is a type error before it is ever run (issue #75).

```python
class PowerCtx(TypedDict):
    rail: str
    volts: float


class PowerSuite(LitmusTest[PowerCtx]):
    async def configure(self, utils: TestUtils[PowerCtx]) -> PowerCtx:
        return {"rail": "3v3", "volts": 3.3}

    def build_suite(self) -> LitmusTest:
        return self.test("rail", self._rail_is_up)

    async def _rail_is_up(self, utils: TestUtils[PowerCtx]) -> Verdict:
        if utils.ctx["volts"] > 3.0:  # a float here, not Any
            return PASS
        raise TestFailure("rail low", "> 3.0V", f"{utils.ctx['volts']}V")
```

This is sugar, and it is opt-in in both directions. Writing `LitmusTest` and
`TestUtils` bare means what it always did — the parameter defaults to
`Mapping[str, Any]` — so no existing suite has to change, and a suite that adopts the
parameter is making a claim to its own reader, not to the framework. Nothing about the
run moves: the context object is the framework's `RunContext`, a `dict[str, Any]` it
owns; a key `configure()` never provided is still a runtime `ContractViolation` rather
than a static one; and `run_data` still strips class instances, functions, and live
handles value by value.

Composition stays unparameterized on purpose. `self.test()`, `self.sequence()`, and
`self.parallel()` speak of `LitmusTest` nodes of any context shape, because a rack composes suites that do not share one context — pinning them to
a single `CtxT` would make the parameter contagious rather than opt-in.

```python
class MyDevice(LitmusTest):
    # configure ALWAYS returns the context your test provides to its
    # lifecycle methods and children. This tells future devs exactly what
    # context your test requires and what it supplies — important, for
    # example, for ssh chaining, which requires an "ssh" entry in ctx.
    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        # utils.env.require raises TestFailure when the variable is unset or
        # blank: a verdict from configure — the test lands in Fail without the
        # suite ever running. The verdict means "did not reach a testable
        # state"; whether the cause is config/infra or DUT is the human's call
        # from logs and survey artifacts. Model as many failure modes as
        # possible so they surface as verdicts rather than bugs. utils.env.port
        # does the same for a port, defaulting to 22 and rejecting a typo.
        # utils.env.int and utils.env.float do it for a number: unset uses the
        # default when one was given, and a bad value Fails naming the variable
        # and the value.
        username = utils.env.require("TARGET_USER")
        password = utils.env.require("TARGET_PASSWORD")
        address = utils.env.require("TARGET_ADDRESS")
        shell = await utils.use_shell(host=address, username=username, password=password)
        # use_shell wires utils.exec to route through SSH automatically
        return {"ssh": shell.ssh, "token": await self._authenticate()}

    # pass or fail, cleanup is GUARANTEED to run at the end of the lifecycle.
    # Use it to close any opened connections, put the device into a neutral state, etc.
    # It runs after a configure Fail too, so read ctx with .get(): "token" may never
    # have been returned.
    async def cleanup(self, utils: TestUtils) -> None:
        pass  # ssh auto-disconnected by framework, after the post-cleanup survey

    def build_suite(self) -> LitmusTest:
        return self.sequence(
            self.name,
            [
                self.test("survey", self._capture_survey),
                self.test("reachable", self._check_reachable),
            ],
        )

    async def _check_reachable(self, utils: TestUtils) -> Verdict:
        # utils.exec already routed through ssh
        result = await utils.exec("true")
        if result.exit_code == 0:
            return PASS
        raise TestFailure("device unreachable", "exit 0", str(result.exit_code))
```

## Composability
A device-level test can be dropped into a rack-level test without modification. "Push state up" to higher-level components; trust the framework lifecycle.

## Environment Variables
Always use `utils.env` inside test code. Never `os.environ` directly. The env snapshot is stored in every `RunResult` for post-run auditing — filtered by the run's `env_allow` or `env_deny` list when one was declared, verbatim otherwise (see `docs/inspector-json.md`). `text`, `require`, and `port` read a string, a required string, and a TCP port. `int` and `float` read a number the same way: a bad value Fails the run naming the variable and the value, and a missing one uses the `default` when the call passed one.

## TestUtils Injection
Call `await root.run()` to execute a suite. The framework constructs `TestUtils`; suite
authors import it for lifecycle annotations, not as plumbing they must assemble.

Every node receives a distinct `TestUtils` object with a logger scoped to that node's
full root-to-leaf path. Leaves, sequences, and parallel nodes built by one authored
instance share that instance's `utils.ctx` dict, while a composed `LitmusTest` subclass
starts with a fresh context. The environment snapshot is immutable and shared across
the run; the halt signal is read-only from suite code.

## Logging
- `utils.logger` is scoped per node — never `print()`, so evidence is captured in artifacts
- Artifacts captured automatically
- `discrete_log_file=False` rolls log entries into the parent logger. `discrete_log_file=True` (the default on class-based tests) gives the node its own log file. Inline `self.test()` bodies default to `False`.
- Every full run creates a timestamped directory under the configured artifact root.
  A logging owner streams both `timeline.log` for humans and versioned
  `timeline.jsonl` for the Inspector, flushing both on every entry. Treat
  `LitmusTest.log_path` as the authoritative path; directory names are not an API.
- While a durable timeline is attached, `utils.logger.isEnabledFor(...)` is true at
  every level so developer-side guards cannot suppress the authoritative chronology.
  Process logging handlers still apply their own level policy.

### Point-in-Time Artifacts

Construct an immutable `Artifact` and pass it to `utils.capture_artifact()`. The method
persists the JSON artifact under `LITMUS_ARTIFACT_ROOT`, then emits a structured pointer
through the same scoped logger that records the surrounding test events. `Artifact`
deliberately has no `write()` or `write_file()` method: a file without its pointer would
be absent from the authoritative chronology.

```python
from litmus import Artifact

snapshot = Artifact(
    name="pre-flash",
    type="hardware-survey",
    data={"firmware": "2.4.1", "healthy": True},
)
artifact_path = await utils.capture_artifact(snapshot)
```

Artifact names and types accept letters, numbers, dashes, and underscores. Payloads must
be JSON serializable. Capture is async, never overwrites an existing artifact, and
returns the absolute path recorded in the pointer entry.

## RunResult Navigation — `ancestor_names` and `bread_crumbs`
Every `RunResult` carries the node's position in the tree:

- `ancestor_names: list[str]` — canonical form, root-to-this inclusive. Use for programmatic aggregation (DB keys, leaf identity).
- `bread_crumbs: str` — `"/".join(ancestor_names)` precomputed. Use for display, log lines, error messages.
- `summary_tree: SummaryTreeNode` — snapshot of this node's subtree at completion. Enables downstream integrators to reconstruct the full Inspector tree from the root's RunResult alone.

The versioned payload and recursive node fields are defined in
[`inspector-json.md`](inspector-json.md).

A full `run()` also persists the root payload automatically as `result.json` in that
run's artifact directory. Authored suites should consume the returned `RunResult` when
they need the live value; they never write the canonical Inspector file themselves.

## Fractal Design: Litmus exposes a single class
| Need | Use |
|---|---|
| Physical hardware component | `LitmusTest` |
| Networked device (MAC-based) | `LitmusTest` + `utils.get_arp_resolver()` in configure |
| Device reached over SSH, connection shared across tests | `LitmusTest` with configure returning `{"ssh": ...}` |
| Device behind a jump host or OOB gateway | The gateway is a `LitmusTest` whose `configure()` opens the hop and whose `build_suite()` composes the device |
| Standalone test procedure | `LitmusTest` |

---

The Python `examples/` tree is the authoring reference. A broken example means
the API changed and the docs need updating.

---

## `self.sequence()` vs `self.parallel()`

**`self.sequence()`** — serial, stops on the first failure.
Use when later tests depend on earlier ones. The Floor Technician sees exactly where the chain broke and nothing beyond it.

**`self.parallel()`** — concurrent via `asyncio.TaskGroup`, collects all failures.
Use when tests are fully independent. The Floor Technician sees everything broken at once. One child's Fail never cancels its siblings — Fail is returned, not raised, at the child boundary; only a harness-level abort cancels the parallel node, and then cancelling everything is exactly what Aborted means.

Independent claims can still share one resource. A single `DeviceApi` handle opened in `configure()` and read from ctx by every leaf serialises their requests: the leaves run concurrently, their requests go out one at a time in call order, and a latency measured around `request` includes the wait for siblings. That is correct when the claim is about the bytes; when the claim is about the DUT's own latency, give each leaf its own handle.

The same holds for a routed console. `use_serial(device=...)` in `configure()` binds `utils.exec` for the node and its descendants to one `SerialInterface`, and `parallel` leaves under it take turns on it in call order (`docs/hil/serial.md`, Serial Turn Contract). A console costs more to share than a handle does: the leaves share one shell's state, so a `cd` or an exported variable in one leaf is visible to the next. Claims that need isolation want one console each, via `get_serial(device=...)`.

---

## ArpResolver — MAC to IP

`ArpResolver` is a composable utility for resolving a device's IP from its MAC. Create one via `utils.get_arp_resolver()` in `configure()`, call `resolve()`, and return both the resolved data and the resolver into ctx. Test methods read the IP and the resolver from `utils.ctx`. Call `arp.ping(utils)` for reachability. Call `arp.reacquire(utils)` after anything that may change the IP (reboot, firmware flash, DHCP churn): it returns the new address and the resolver remembers it, so a later `ping` dials the address it last resolved while ctx stays as `configure()` left it. Carry the new address as evidence on the verdict if the Floor Technician should see it.

`ArpResolutionFailure` is not a `TestFailure`. Catch it in `configure` and re-raise as `TestFailure` so a missing ARP entry is a verdict. Uncaught, it is a developer bug — the suite did not model the failure mode.

```python
# In configure():
arp = utils.get_arp_resolver(mac=self._mac)
try:
    resolved = await arp.resolve()
except ArpResolutionFailure as error:
    raise TestFailure(
        f"{error.mac} did not appear on the network",
        error.mac,
        error.host,
    ) from error
return {**resolved, "arp": arp}

# In build_suite():
self.test("reachability", self._reachability)
```

See `examples/networked_device.py`.

---

## ArpResolver Behind a Gateway

When the test host cannot reach the target network directly, the SE code is identical to the local case. Compose the device under a gateway parent whose `configure()` opened the hop, and the framework routes the lookup — the SE never touches SSH.

```python
# DutDevice.configure() — no SSH awareness, identical to local ArpResolver usage:
arp = utils.get_arp_resolver(mac=self._mac)
resolved = await arp.resolve()
return {**resolved, "arp": arp}

# Gateway.build_suite() — the parent names what sits behind its hop:
return self.sequence(self.name, [self.test("gateway-up", self._gateway_up), DutDevice(name="dut")])
```

See `examples/networked_device.py`. `litmus run ...::DutDevice` reads the test host's ARP table; `litmus run ...::Gateway` runs the same DUT class through the hop.

---

## SSH Device — Class-Owned Connection

Extend `LitmusTest` when one SSH connection must be shared across all of the device's leaves. Open in `configure()` via `utils.use_shell()` and let the framework route `utils.exec` through it automatically.

Returning `{"ssh": ...}` from `configure()` also makes the device eligible as a `via` target. Pass it as `via` in `TestParams` — the framework manages the lifecycle and scopes `utils.exec` automatically.

`use_shell` vs `get_shell`: `use_shell` answers "where does `utils.exec` go" — it rebinds the routing for this test and its descendants. `get_shell` hands you an additional interface without touching the routing.

A console by device path is the same idiom: `await utils.use_serial(device="/dev/ttyUSB0")` in `configure()` declares it as the node's exec route and `get_serial` opens one as an auxiliary endpoint; both hand back a `Shell`, the framework closes the port after the post-cleanup survey and from the abort sweep, and no transport is imported or carried in ctx. Behind a hop, pass an SSH `Shell` as `via` or let the console inherit the parent's `use_shell` route. The policy-only shapes (`inherit=False`, `propagate=False`) stay with `use_shell`.

See `examples/remote_device_ssh.py`. The pi-hole dogfood suite
(`examples/pi_hole_server.py`) is the same class-owned hop plus phase-boundary
surveys that tail service logs for the Floor Technician.

---

## SSH Tunneling

Pass the `Shell` an earlier `utils.get_shell()` returned as the `via` argument to the next `utils.get_shell()` to tunnel the new connection through it. Both interfaces are framework-managed — no `cleanup()` override is needed.

```python
jump = await utils.get_shell(host=jump_host, username=jump_user)
target = await utils.get_shell(host=target_host, username=target_user, via=jump)
```

See `examples/ssh_tunneling.py`.

---

## Device API — CoAP/HTTP

A device API is not `exec` with a URL. Open a session from `configure()` with
`utils.get_http` or `utils.get_coap`, then `request` a typed
`DeviceRequest`. The response's `status` is data — HTTP 4xx/5xx is not an
exception, and neither is a CoAP 4.xx. On HTTP, `status` is the integer. On
CoAP it is the dotted code (`CoapCode.CONTENT` is `"2.05"`), which is what the
record stores when the claim puts `response.status` on the verdict. Catch
`DeviceApiOperationFailure` when the transfer itself did not complete (timeout,
refused connection) and model it as a `TestFailure`.

Retries default to zero. Each declared attempt is logged. POST refuses retry
unless `retry_non_idempotent=True`. Attempt logs keep header names and withhold
every value unless the author lists that name in `evidence_headers`. Abort
closes the session; it does not invent a shutdown PUT.

One handle carries one request at a time. Leaves under a `parallel` that share
the handle `configure()` opened queue on it in call order — nothing collides and
nothing is refused — so a round trip timed in the leaf includes the queue. A
request still waiting when the handle is cancelled or closed is never sent; it
fails with `DeviceApiOperationFailure`, the same class as any other transfer
that did not complete. Measure the DUT alone with one handle per leaf.

```python
api = await utils.get_http(base_url="https://dut.local")
response = await api.request(DeviceRequest(method="GET", path="/whoami"))
if response.status != 200:
    raise TestFailure("whoami rejected", 200, response.status)
return PASS({"status": response.status})
```

The backends stay off the public surface, the way `SshInterface` does.

For a JSON body, declare `json=` instead of encoding bytes and setting a header by
hand. Read it back with `.json()`, or select the parser with
`.parse(response_type="json")` / `.parse(response_type="yaml")` (YAML needs
`litmus[yaml]`). Both leave the original bytes and status available. Parser errors
propagate, so model a malformed response as `TestFailure` when it is a DUT failure
mode. Neither the transfer nor the parser chooses a verdict.

```python
response = await api.request(DeviceRequest(method="PUT", path="/settings", json={"enabled": True}))
if response.status == 200 and response.json().get("enabled") is True:
    return PASS({"enabled": True})
raise TestFailure("device did not enable", "enabled", response.status)
```

---

## wait_for Patterns

Where `wait_for()` is called determines what the timeout *means*, not what gets raised — both raise `TestFailure`:

- **In `configure()`** — timeout becomes a `TestFailure` that halts the lifecycle. The test lands in Fail; the suite never runs. The verdict means "the device did not reach a testable state." Whether the cause is hardware or infrastructure is **not knowable from code** — the human triager makes that call from logs and survey artifacts.
- **In a test method** — timeout becomes a `TestFailure` that is the verdict itself. The device was reachable but failed to reach the expected state.

`looking_for` is required and must be plain language — it becomes the Floor Technician's primary diagnostic string when a wait times out. The call returns the predicate's truthy value (`True` stays `True`); a claim that needs what was observed returns it from the predicate instead of closing over a list.

The runnable [`examples/wait_for_patterns.py`](https://gitlab.com/Tyler-Meador/litmus_test/-/blob/main/examples/wait_for_patterns.py) ports
both placements from the legacy example. It also shows how polling observations flow
through scoped logs, phase-boundary surveys, point-in-time artifacts, `run_data`, and the
root `summary_tree` without adding another evidence API.
