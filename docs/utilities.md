# Utilities

## Logging and the timeline

- `utils.logger` is scoped per node. Every entry is captured in artifacts.
- `discrete_log_file=True` (the default on class-based tests) gives the node its own log file; `False` rolls entries into the parent logger. Inline `self.test()` bodies default to `False`.
- Every full run creates a timestamped directory under the configured artifact root. A logging owner streams both `timeline.log` for humans and versioned `timeline.jsonl` for the Inspector, flushing both on every entry. Treat `LitmusTest.log_path` as the authoritative path; directory names are not an API.
- While a durable timeline is attached, `utils.logger.isEnabledFor(...)` is true at every level so developer-side guards cannot suppress the chronology. Process logging handlers still apply their own level policy.

### Point-in-time artifacts

Construct an immutable `Artifact` and pass it to `utils.capture_artifact()`. The method persists the JSON artifact under `LITMUS_ARTIFACT_ROOT`, then emits a structured pointer through the same scoped logger that records the surrounding test events. `Artifact` deliberately has no `write()` method: a file without its pointer would be absent from the chronology.

```python
from litmus import Artifact

snapshot = Artifact(
    name="pre-flash", type="hardware-survey", data={"firmware": "2.4.1", "healthy": True}
)
artifact_path = await utils.capture_artifact(snapshot)
```

Names and types accept letters, numbers, dashes, and underscores. Payloads must be JSON serializable. Capture is async, never overwrites, and returns the absolute path recorded in the pointer entry.

## RunResult navigation

Every `RunResult` carries the node's position in the tree:

- `ancestor_names: list[str]`, root-to-this inclusive. Use for programmatic aggregation (DB keys, leaf identity).
- `bread_crumbs: str`, `"/".join(ancestor_names)` precomputed. Use for display, log lines, error messages.
- `summary_tree: SummaryTreeNode`, a snapshot of this node's subtree at completion, so an integrator can reconstruct the full Inspector tree from the root's `RunResult` alone.

The versioned payload is defined in [`inspector-json.md`](inspector-json.md). A full `run()` also persists the root payload as `result.json` in the run's artifact directory. Suites consume the returned `RunResult` when they need the live value; they never write the canonical Inspector file themselves.

## ArpResolver: MAC to IP

Create one via `utils.get_arp_resolver()` in `configure()`, call `resolve()`, and return both the resolved data and the resolver into ctx. Test methods read the IP and the resolver from `utils.ctx`. `arp.ping(utils)` checks reachability. `arp.reacquire(utils)` after anything that may change the IP (reboot, flash, DHCP churn) returns the new address and the resolver remembers it, so a later `ping` dials the address it last resolved while ctx stays as `configure()` left it. Carry the new address as evidence on the verdict if the Floor Technician should see it.

`ArpResolutionFailure` is not a `TestFailure`. Catch it in `configure` and re-raise as `TestFailure` so a missing ARP entry is a verdict. Uncaught, it is a developer bug: the suite did not model the failure mode.

```python
arp = utils.get_arp_resolver(mac=self._mac)
try:
    resolved = await arp.resolve()
except ArpResolutionFailure as error:
    raise TestFailure(
        f"{error.mac} did not appear on the network", error.mac, error.host
    ) from error
return {**resolved, "arp": arp}
```

The resolver is scoped to the node's current exec route and its cache is keyed by the routing label, so a device composed under a gateway resolves through the hop with no change to the device's code. `litmus run ...::DutDevice` reads the test host's ARP table; `litmus run ...::Gateway` runs the same class through the hop. See `examples/networked_device.py`.

## SSH: `use_shell` and `get_shell`

`use_shell` answers "where does `utils.exec` go": it rebinds the routing for this node and its descendants. `get_shell` hands you an additional interface without touching the routing. Both are framework-managed; no `cleanup()` override is needed, and the shell is disconnected after the post-cleanup survey.

Open the device's shell in `configure()` with `use_shell` when one connection must be shared across all of the device's leaves. See `examples/remote_device_ssh.py`. The pi-hole dogfood suite (`examples/pi_hole_server.py`) is the same class-owned hop plus phase-boundary surveys that tail service logs for the Floor Technician.

### Tunneling

Pass the `Shell` an earlier `get_shell()` returned as the `via` argument to the next one to tunnel the new connection through it.

```python
jump = await utils.get_shell(host=jump_host, username=jump_user)
target = await utils.get_shell(host=target_host, username=target_user, via=jump)
```

See `examples/ssh_tunneling.py`.

## Serial console: `use_serial` and `get_serial`

`use_serial(device=...)` is `use_shell(host=...)` for a console reached by its device path: it opens the port, binds `utils.exec` to it for this node and its descendants, and the framework closes the port after the post-cleanup survey and from the abort sweep. `get_serial(device=...)` hands you an additional console without touching the routing. Both return a `Shell`; `use_serial()` with no arguments reads the active route from any slot, and the declaring form is admitted only in `configure()`, under the same `ContractViolation` as `use_shell`. No `cleanup()` override is needed and no transport is imported.

```python
async def configure(self, utils: TestUtils) -> dict[str, Any]:
    await utils.use_serial(
        device=utils.env.require("SERIAL_DEVICE"),
        baud=utils.env.int("SERIAL_BAUD", default=115200),
    )
    return {}
```

The line settings are the transport's: `prompt` (a regex; `None` is a bare POSIX prompt), `login_prompt` / `password_prompt` with `username` / `password` for a console that logs in, `login_timeout` for connect, `prompt_timeout` for how long one `exec` waits for the prompt before settling `exit_code == -1` with the text so far as evidence. A console's `stderr` is transport-generated only (`"prompt timeout"`, `"transport closed"`); everything the DUT writes lands in `stdout`. The contract is `docs/hil/serial.md`.

A console behind a hop (a console server, a Pi with a USB-UART) is the same call: pass an SSH `Shell` as `via`, or declare `use_shell(host=...)` on the parent and let the console inherit the route. The device path then names a node on the hop box and the bytes bridge through it (`bridge_command` overrides the default `stty` + `cat` bridge). A serial console accepts no other kind of hop; passing one is a `TypeError` naming the call. The policy-only shapes (`inherit=False`, `propagate=False`) belong to `use_shell`. `pyserial-asyncio` is the `litmus[serial]` extra; a local open without it raises `ModuleNotFoundError` naming the install.

## Device API: CoAP and HTTP

A device API is not `exec` with a URL. Open a session from `configure()` with `utils.get_http` or `utils.get_coap`, then `request` a typed `DeviceRequest`. The response's `status` is data; HTTP 4xx/5xx is not an exception. Catch `DeviceApiOperationFailure` when the transfer itself did not complete (timeout, refused connection) and model it as a `TestFailure`.

Retries default to zero and each declared attempt is logged. POST refuses retry unless `retry_non_idempotent=True`. Attempt logs keep header names and withhold every value unless the author lists that name in `evidence_headers`. Abort closes the session; it does not invent a shutdown PUT.

```python
api = await utils.get_http(base_url="https://dut.local")
response = await api.request(DeviceRequest(method="GET", path="/whoami"))
if response.status != 200:
    raise TestFailure("whoami rejected", 200, response.status)
return PASS({"status": response.status})
```

The backends stay off the public surface, the way `SshInterface` does.

## `wait_for`

`looking_for` is required and must be plain language: it is the Floor Technician's primary diagnostic string when a wait times out. Both placements raise `TestFailure`; where the call sits decides what the timeout means, and that is a lifecycle question answered on the idioms page. The call returns the predicate's truthy value (`True` stays `True`), so a claim that needs the observation returns it from the predicate instead of writing into a closure. The runnable [`examples/wait_for_patterns.py`](https://gitlab.com/Tyler-Meador/litmus_test/-/blob/main/examples/wait_for_patterns.py) shows both placements and how polling observations flow through scoped logs, surveys, artifacts, `run_data`, and the root `summary_tree` without another evidence API.

## `utils.exec`

Never throws. Returns `stdout`, `stderr`, and `exit_code`; output is bounded and truncation is recorded as evidence. Where it lands is the parent's decision (idioms, root 4).
