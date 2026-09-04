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
- test methods, `cleanup()`, and all child test bodies read state from `utils.ctx`, not from `self`
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
from litmus.evidence import JsonObject


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

## Composition Factories: `self.case`, `self.sequence`, `self.group`

`LitmusTest` exposes three instance factories for building the suite:

- `self.case(name, test_fn)` — a leaf test case. Defaults to `discrete_log_file=False` so entries roll up to the parent artifact.
- `self.sequence(name, [tests])` — serial execution, bails on first failure.
- `self.group(name, [tests])` — concurrent execution via `asyncio.TaskGroup`, collects all failures.

**Private methods pass as references directly — Python bound methods carry `self` for free.**

```python
def build_suite(self) -> LitmusTest:
    return self.sequence(
        self.name,
        [
            self.case("check reachable", self._check_reachable),
            self.case("check firmware", self._check_firmware),
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

## Integration Hooks: `before_each` / `after_each` on the Run Params

For composing Litmus into a larger harness (shop-floor DB, lab coordinator, audit pipeline), pass `before_each` and `after_each` to the root `run()` call:

```python
await root.run(
    before_each=lambda ctx: shopfloor_db.record_started(
        test=ctx.bread_crumbs,
        station=ctx.env.get("STATION_ID"),
        at=datetime.now(UTC),
    ),
    after_each=lambda ctx: shopfloor_db.record_finished(
        test=ctx.bread_crumbs,
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
`configure()` returns a plain dict; the framework merges it into `utils.ctx` for test methods, survey hooks, `cleanup()`, and all descendant test cases. No instance fields are needed for configure-phase data.

The broad framework annotation is `dict[str, Any]`. When a suite benefits from precise
key types, define a local `TypedDict` and use it as the covariant return annotation.
This is static information only: a `TypedDict` is still an ordinary mutable dict at
runtime. A context dataclass is deliberately out of contract because it would fight
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
        pass  # ssh auto-disconnected by framework

    def build_suite(self) -> LitmusTest:
        return self.sequence(
            self.name,
            [
                self.case("survey", self._capture_survey),
                self.case("reachable", self._check_reachable),
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
full root-to-leaf path. Inline cases, sequences, and groups built by one authored
instance share that instance's `utils.ctx` dict, while a composed `LitmusTest` subclass
starts with a fresh context. The environment snapshot is immutable and shared across
the run; the halt signal is read-only from suite code.

## Logging
- `utils.logger` is scoped per test case — never `print()`, so evidence is captured in artifacts
- Artifacts captured automatically
- `discrete_log_file=False` rolls log entries into the parent logger. `discrete_log_file=True` (the default on class-based tests) gives the node its own log file. Inline `self.case()` bodies default to `False`.

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

## TestSequence vs TestGroup

**`self.sequence()`** — serial, stops on the first failure.
Use when later tests depend on earlier ones. The Floor Technician sees exactly where the chain broke and nothing beyond it.

**`self.group()`** — concurrent via `asyncio.TaskGroup`, collects all failures.
Use when tests are fully independent. The Floor Technician sees everything broken at once. One child's Fail never cancels its siblings — Fail is returned, not raised, at the child boundary; only a harness-level abort cancels the group, and then cancelling everything is exactly what Aborted means.

---

## ArpResolver — MAC to IP

`ArpResolver` is a composable utility for resolving a device's IP from its MAC. Create one via `utils.get_arp_resolver()` in `configure()`, call `resolve()`, and return both the resolved data and the resolver into ctx. Access ctx fields via `self.ctx` inside class methods. Call `arp.ping(utils)` for reachability. Call `arp.reacquire(utils)` after anything that may change the IP (reboot, firmware flash, DHCP churn).

```python
# In configure():
arp = utils.get_arp_resolver(mac=self._mac)
resolved = await arp.resolve()
return {**resolved, "arp": arp}

# In build_suite():
self.case("reachability", lambda utils: self.ctx["arp"].ping(utils))
```

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

---

## SSH Device — Class-Owned Connection

Extend `LitmusTest` when one SSH connection must be shared across all of the device's test cases. Open in `configure()` via `utils.use_shell()` and let the framework route `utils.exec` through it automatically.

Returning `{"ssh": ...}` from `configure()` also makes the device eligible as a `via` target. Pass it as `via` in `TestParams` — the framework manages the lifecycle and scopes `utils.exec` automatically.

`use_shell` vs `get_shell`: `use_shell` answers "where does `utils.exec` go" — it rebinds the routing for this test and its descendants. `get_shell` hands you an additional interface without touching the routing.

See `examples/remote_device_ssh.py`.

---

## SSH Tunneling

Pass an existing `SshInterface` as the `via` argument to `utils.get_shell()` to tunnel the new connection through it. Both interfaces are framework-managed — no `cleanup()` override is needed.

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

The runnable [`examples/wait_for_patterns.py`](../examples/wait_for_patterns.py) ports
both placements from the legacy example. It also shows how polling observations flow
through scoped logs, phase-boundary surveys, point-in-time artifacts, `run_data`, and the
root `summary_tree` without adding another evidence API.

---

## run_data — Structured Evidence

Configure's return values flow into ctx and appear in `RunResult.run_data` automatically. Class instances and non-serializable values are stripped. Override `run_data` only when you need computed or filtered output.
