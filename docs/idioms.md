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
After `__init__` runs, `self` is read-only. All runtime state flows through configure's return value into `utils.ctx`. This eliminates hidden state coupling and makes the data flow explicit.

**Rules:**
- `__init__` sets read-only config (MAC, credentials, site, etc.)
- `configure()` returns runtime state as a plain dict — the framework merges it into `utils.ctx`
- test methods, `cleanup()`, and all child test bodies read state from `utils.ctx`, not from `self`; every key they read with `[]` is one `configure()` returned, and a key it did not provide is read with `.get()` or not at all — a missing `[]` read is a `ContractViolation`, not a `KeyError` (issue #112)
- No `self.field = value` after construction — if you need to update state mid-test, mutate `utils.ctx` in place (e.g. `utils.ctx.update(ip=new_ip)`)

**Why:** Mutable instance state creates hidden coupling between lifecycle phases. When state flows through ctx, the data path is explicit and traceable. The Floor Technician benefits because `run_data` captures ctx automatically — structured evidence appears in `RunResult` with zero extra work.

**Enforcement:** This is a tier-1 runtime guard, not merely a lint convention.
`LitmusTest` freezes only after the most-derived `__init__` returns, so subclasses may
finish declaring their identity after `super().__init__()`. Ordinary assignment and
deletion then raise `AttributeError`. Framework-owned lifecycle and evidence transitions
use `object.__setattr__` directly; suite code receives no framework escape-hatch method.
The static no-self-mutation rule remains defense in depth.

**Scope note:** ctx is strictly instance-local. A parent's ctx never flows into composed children — composition is the only coupling mechanism, and ctx inheritance would smuggle hidden state across it.

## Via-Routing — Framework-Managed Lifecycle

Any Testable whose `configure()` calls `utils.use_shell()` can serve as a via target.
Pass it as `via=` at construction. The framework brings the via device up before the
consumer's pre-configure survey, routes `utils.exec` through that hop for configure,
surveys, tests, and audits, and tears the via down after the consumer's post-cleanup
survey. A survey or in-flight audit that execs therefore measures the hop, not the
test host. Authors never call `run_configure_only()` or `run_cleanup_only()` by hand.

```python
gateway = Gateway(name="gateway")
target = MyDevice(name="target", mac="aa:bb:cc:dd:ee:ff", via=gateway)
```

The via device's ctx stays instance-local. Cross-instance data still moves through
constructor params. The hop itself is borrowed: a consumer `get_shell()` tunnels
through it without owning it, so cancelling that tunnel leaves the gateway up until
the framework tears the via device down. A via shared by more than one consumer
stays up until the last consumer finishes — the first teardown does not drop the
hop out from under a sibling. A declared via that never opens a shell is a
`FrameworkContractViolation`, not local exec under Pass.

## run_data — Automatic Structured Evidence
The default `run_data` returns a detached snapshot of all serializable values from
`utils.ctx`. JSON primitives (with finite numbers), plain dicts with string keys, and
lists survive; class instances (`SshInterface`, etc.), functions, live handles,
non-finite floats, and cycles are stripped automatically.

Stripping is deterministic and all-or-nothing at each top-level ctx entry. If a list or
dict contains one invalid nested value, that complete entry is absent from `run_data`.
Litmus does not remove only the handle and leave a plausible-looking partial record.

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

Override `run_data` only when you need computed or filtered output:

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

Every default snapshot includes an empty `run_data["measurements"]` list reserved for
parametric measurements. It is framework-owned: a ctx entry with that key does not
populate or replace it. Measurement limits, units, aggregation, SPC, and Cpk require a
later shared design. The empty slot preserves that boundary without pretending those
semantics exist today.

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

A deadline closes only transports opened by the cut node. An inherited route and a
borrowed `via` hop remain their owner's resources; this is what keeps a timed-out DUT
from taking down a healthy sibling behind the same gateway.

A `LitmusTest` used as `via=` may declare the same budget pair. Its clock begins when
that via enters `configure`; if bring-up expires, the consumer adopts its state and
timeout evidence, then the framework runs the via's cleanup and releases any borrowed
upstream hop.

## Pass/Fail/Aborted Contract
- A test method returning the `PASS` sentinel → test passes (state `Pass`). The check is `result is PASS` — identity, not equality. The string `"PASS"` does not pass.
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
`configure()` returns a plain dict; the framework merges it into `utils.ctx` for test methods, survey hooks, `cleanup()`, and all descendant leaves. No instance fields are needed for configure-phase data.

The return value is the contract with the test methods: it names every key they read with `[]`. Seed no placeholders — a reading a test method has not taken yet is absent, not `0.0` — and do not rely on a sibling leaf's write, which is order under `sequence` and a race under `parallel`. `litmus-check` enforces this statically as `litmus/ctx-provenance`; at run time a `[]` read of a missing key is a `ContractViolation` naming the test and the key, while `.get()` and `in` read an optional key (issue #112).

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

Generic `TestUtils[CtxT]` typing may later carry the same suite-local type through
`utils.ctx`, but remains opt-in sugar. The framework stores a dict either way, and
`run_data` still strips class instances, functions, and live handles value by value.

```python
class MyDevice(LitmusTest):
    # configure ALWAYS returns the context your test provides to its
    # lifecycle methods and children. This tells future devs exactly what
    # context your test requires and what it supplies — important, for
    # example, for ssh chaining, which requires an "ssh" entry in ctx.
    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        username = utils.env.get("TARGET_USER")
        password = utils.env.get("TARGET_PASSWORD")
        address = utils.env.get("TARGET_ADDRESS")
        # Raise TestFailure to signal a verdict from configure — the test lands
        # in Fail without the suite ever running. The verdict means "did not
        # reach a testable state"; whether the cause is config/infra or DUT is
        # the human's call from logs and survey artifacts. Always model as many
        # failure modes as possible so they surface as verdicts rather than bugs.
        if not username:
            raise TestFailure("TARGET_USER env var not set")
        if not password:
            raise TestFailure("TARGET_PASSWORD env var not set")
        if not address:
            raise TestFailure("TARGET_ADDRESS env var not set")
        shell = await utils.use_shell(host=address, username=username, password=password)
        # use_shell wires utils.exec to route through SSH automatically
        return {"ssh": shell.ssh, "token": await self._authenticate()}

    # pass or fail, cleanup is GUARANTEED to run at the end of the lifecycle.
    # Use it to close any opened connections, put the device into a neutral state, etc.
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

## Via-Routing — Framework-Managed Lifecycle
Any `LitmusTest` whose `configure()` returns `{"ssh": SshInterface}` (or whose context otherwise carries an SSH interface) can serve as a via target. Pass it as the `via` param in `TestParams` — the framework automatically brings up the via device before `configure()`, merges its ctx (including the SSH interface), and scopes `utils.exec` to route through the tunnel. Cleanup runs automatically after the consuming device's cleanup.

```python
# Wiring — pass the gateway as `via`:
gateway = Gateway(name="gateway")
target = MyDevice(name="target", mac="aa:bb:cc:dd:ee:ff", via=gateway)
```

The SE never runs the via device's configure or cleanup manually. The framework owns the via device's lifecycle.

## Composability
A device-level test can be dropped into a rack-level test without modification. "Push state up" to higher-level components; trust the framework lifecycle.

## Environment Variables
Always use `utils.env` inside test code. Never `os.environ` directly. The env snapshot is stored in every `RunResult` for post-run auditing — filtered by the run's `env_allow` or `env_deny` list when one was declared, verbatim otherwise (see `docs/inspector-json.md`).

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
| Device behind a jump host or OOB gateway | `LitmusTest` with `via` param |
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

---

## ArpResolver — MAC to IP

`ArpResolver` is a composable utility for resolving a device's IP from its MAC. Create one via `utils.get_arp_resolver()` in `configure()`, call `resolve()`, and return both the resolved data and the resolver into ctx. Test methods read the IP and the resolver from `utils.ctx`. Call `arp.ping(utils)` for reachability. Call `arp.reacquire(utils)` after anything that may change the IP (reboot, firmware flash, DHCP churn).

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

## ArpResolver with Via-Routing

When the test host cannot reach the target network directly, the SE code is identical to the local case. Pass the gateway as `via` in `TestParams` and the framework handles everything — the SE never touches SSH.

```python
# configure() — no SSH awareness, identical to local ArpResolver usage:
arp = utils.get_arp_resolver(mac=self._mac)
resolved = await arp.resolve()
return {**resolved, "arp": arp}

# Wiring:
gateway = Gateway(name="gateway")
target = MyDevice(name="target", mac="aa:bb:cc:dd:ee:ff", via=gateway)
```

See `examples/networked_device.py`. The DUT class is the same one used locally.

---

## SSH Device — Class-Owned Connection

Extend `LitmusTest` when one SSH connection must be shared across all of the device's leaves. Open in `configure()` via `utils.use_shell()` and let the framework route `utils.exec` through it automatically.

Returning `{"ssh": ...}` from `configure()` also makes the device eligible as a `via` target. Pass it as `via` in `TestParams` — the framework manages the lifecycle and scopes `utils.exec` automatically.

`use_shell` vs `get_shell`: `use_shell` answers "where does `utils.exec` go" — it rebinds the routing for this test and its descendants. `get_shell` hands you an additional interface without touching the routing.

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

## wait_for Patterns

Where `wait_for()` is called determines what the timeout *means*, not what gets raised — both raise `TestFailure`:

- **In `configure()`** — timeout becomes a `TestFailure` that halts the lifecycle. The test lands in Fail; the suite never runs. The verdict means "the device did not reach a testable state." Whether the cause is hardware or infrastructure is **not knowable from code** — the human triager makes that call from logs and survey artifacts.
- **In a test method** — timeout becomes a `TestFailure` that is the verdict itself. The device was reachable but failed to reach the expected state.

`looking_for` is required and must be plain language — it becomes the Floor Technician's primary diagnostic string when a wait times out.

The runnable [`examples/wait_for_patterns.py`](https://gitlab.com/Tyler-Meador/litmus_test/-/blob/main/examples/wait_for_patterns.py) ports
both placements from the legacy example. It also shows how polling observations flow
through scoped logs, phase-boundary surveys, point-in-time artifacts, `run_data`, and the
root `summary_tree` without adding another evidence API.

---

## run_data — Structured Evidence

Configure's return values flow into ctx and appear in `RunResult.run_data` automatically. Class instances and non-serializable values are stripped. Override `run_data` only when you need computed or filtered output.

See `examples/run_data_evidence.py`.
