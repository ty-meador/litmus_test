# Design note: Device API (CoAP/HTTP) interface contract

Status: DRAFT — awaiting Ty's signature on #97

A CoAP or HTTP resource is not a command string. This note proposes the
request and response shape, retry-as-evidence, timing, error taxonomy, and
closed-session obligations that make a device API fit Litmus's existing
lifecycle. The [three HIL rulings](index.md#the-three-rulings) govern it.
Approval to draft #97 does not sign the obligations below; no Device API
implementation may precede Ty's signature on this note.

This is the interface most likely to be waved through as "just `exec` with a
URL." It is not. An HTTP call needs its own contract, error taxonomy, and
safe-state, same as a pin or a bus.

All names below (`DeviceApiInterface`, `HttpInterface`, `CoapInterface`,
`DeviceRequest`, `DeviceResponse`, `DeviceApiOperationFailure`,
`MockDeviceApi`, and the method names) are provisional. This note adds no
public Python name and changes no stored record. The legacy
`CoapInterface` is a design reference only — see below. The migration
context is the preserved lifecycle and evidence contracts, with a new
hardware contract for Ty to judge.

## Why not an `exec` subclass

`exec` is a command contract: a string goes in, an `ExecResult` comes out,
and a non-zero exit is data for the test method to judge. A device-API
call is a typed resource operation: method, path, payload bytes, status,
headers or CoAP options, and a body. None of that is a shell command.

Forcing it through `exec` would mean either shelling out to `curl` /
`wget` / the `coap` CLI (lossy, racy, and blind to in-flight cancellation)
or smuggling an HTTP client inside a `RemoteInterface` subclass that
answers `_exec` with a fake dialect — exactly the
parallel-lifecycle-in-disguise [ruling 1](index.md#the-three-rulings)
forbids. A URL that has been stringified into `curl -X POST ...` has
already lost the status as a first-class value, the header set, whether
the body was truncated, and whether the client retried. `DeviceApiInterface`
is its own ABC, structurally parallel to `RemoteInterface`
(`src/litmus/remote.py`) and to the signed [`GpioInterface`](gpio.md) /
[`PwmInterface`](pwm.md): it owns `connect` / `disconnect` /
`cancel_in_flight` in the same shape, and adds request-shaped operations
in place of `exec`.

The legacy TypeScript `CoapInterface` wrapped the `coap` CLI through
`utils.exec` because the UDP-only `node-coap` package could not speak
`coaps+tcp://` (RFC 8323). That was a toolchain constraint, not a
contract. This port does not carry the CLI wrapper forward. A backend
that can only shell out to `curl` or `coap` is a finding for Ty, not the
shape of the interface.

## The Device Request Contract

**Framework obligation:** Present CoAP and HTTP as one resource-request
contract under HIL ruling 1. `DeviceApiInterface` is not an `exec`
subclass and does not change where `utils.exec` routes. A handle is bound
to one scheme, one base URL, and a declared timing envelope before it can
request.

- HTTP and CoAP share the request/response shape (method, path, payload,
  status, body). They are not one handle type. An `HttpInterface` cannot
  speak CoAP; a `CoapInterface` cannot follow HTTP redirects. Scheme is
  identity, fixed for the handle's lifetime: `http` / `https` for HTTP,
  `coap` / `coaps` / `coaps+tcp` for CoAP. Flipping the scheme live is the
  Device API instance of flipping a GPIO pin's direction.
- A provisional `DeviceRequest` carries `method` (GET, PUT, POST, DELETE;
  CoAP may add the methods its backend declares), `path` (appended to the
  declared base URL, never a second origin), `payload` (`bytes` or absent),
  and protocol options (HTTP headers, CoAP options). Reject a non-`bytes`
  payload, a path that is an absolute URL, a method the backend did not
  declare, and a header or option name that is not a string, at the call
  site, before the adapter is touched. Booleans are not integers; a `dict`
  is not a JSON body. Encoding is the author's, recorded as the bytes
  actually sent.
- A provisional `DeviceResponse` carries `status` (HTTP status or CoAP
  code), protocol options (headers / CoAP options), and `body` (`bytes`).
  JSON parsing is a convenience on top of those bytes, not the contract:
  a body that is not JSON is still a successful transfer of bytes. The
  interface does not invent an empty body, a 200, or a CoAP 2.05 to stand
  in for a missing response.
- **Status is data.** HTTP 4xx / 5xx and CoAP 4.xx / 5.xx are fields on
  the response, the way a non-zero `exit_code` is a field on `ExecResult`.
  The interface does not raise `TestFailure` because a resource returned
  404, and it does not raise because a resource returned 500. The test
  method judges the status. Transport failure, protocol violation, and
  timeout are the operational exceptions; a well-formed error response is
  not one of them.
- `request(req) -> DeviceResponse` performs one application-level attempt
  (see retries below). One in-flight request per handle. A second
  `request` is refused while one is active; it is not queued to surprise
  the next lifecycle phase. Framework termination takes precedence over
  this admission rule.
- Each operation records the requested method, path, payload length,
  whether the adapter confirmed a response, the status if one arrived, and
  the body length. An unconfirmed request never becomes the last-successful
  response in a cache the next test method reads as truth. Unknown is
  explicit.

**Developer obligation:** Declare the scheme, base URL, and protocol
parameters in `configure()`. Obtain the framework-managed handle through
a provisional `utils.get_http(...)` or `utils.get_coap(...)`; return the
handle and static inputs through configure's ctx. Keep `self` immutable,
`build_suite()` pure, and later ctx reads read-only. Judge the DUT with
the status and body actually returned (or with an independent
observation), never by treating a 2xx as a passing device.

**Consequence:** The Floor Technician can tell which resource was
addressed, what was sent, what status came back, and whether the transfer
completed. A 404 cannot quietly become a missing payload in a record that
claims the call succeeded, and a 200 cannot quietly become a verdict.

## The Retry-as-Evidence Contract

**Framework obligation:** A retry is stimulus the DUT saw. Hiding it lies
to the Floor Technician.

- Application-level retries default to **zero**. A handle that retries
  without a declaration is refused at the call site. Each declared
  attempt — including the first — is logged through the scoped logger
  with attempt index, outcome (response status, operational failure, or
  cancelled), and delay before the next attempt when one is scheduled.
- The retry budget is a count of application-level attempts, not a
  duration. Delay between attempts is positive finite seconds, declared
  with the count. Booleans and non-finite values are refused. The node's
  `fail_after` / `abort_after` still bounds the whole sequence; a retry
  loop cannot extend the node clock.
- Non-idempotent methods (POST, and any other method the backend declares
  non-idempotent) refuse application retry unless the author sets an
  explicit `retry_non_idempotent=True` on that call. GET / PUT / DELETE
  may retry when a count is declared. The interface does not guess
  idempotency from the path.
- Protocol-level reliability is not an application retry. HTTP/TCP
  retransmits and CoAP CON retransmits belong to the backend's declared
  request ceiling. CON retransmits that actually go on the wire are
  counted in the attempt's evidence (the DUT saw them) but they do not
  consume the application retry budget. A backend whose CON loop cannot
  be bounded by the request ceiling, or cannot be cut by
  `cancel_in_flight`, is a finding for Ty.
- `cancel_in_flight` stops the in-flight attempt *and* the retry loop.
  A cancelled retry does not start the next attempt. The record names how
  many attempts ran and that the remainder were not started.

**Developer obligation:** Leave retries at zero unless this bench has a
named reason to repeat a request. When retries are declared, treat the
attempt log as part of the evidence that will be read after a Fail —
especially for POST. Do not wrap `request()` in a suite-owned retry loop
to dodge the declaration; that is the same lie with worse artifacts.

**Consequence:** The timeline shows every request the DUT actually
received from this handle. A pass that depended on the third try cannot
be reconstructed as a single clean GET.

## The Closed-Session and Timing Contract

**Framework obligation:** The safe state is a **closed session** — no
further requests originating from this handle — enforced at the resource
by the existing termination sweep. This specializes the signed GPIO
[ownership and cut rules](gpio.md#the-gpio-interface-contract) and the
signed PWM
[disabled-output termination](pwm.md#the-pwm-safe-state-contract). It
introduces no Device API halt signal, event loop, lifecycle runner, or
integration hook, and it does not invent a shutdown request.

- Register ownership as soon as acquisition can put a request on the
  wire, so partial `configure()` failure cannot orphan a session.
  Bring-up establishes and confirms the session (TLS handshake, CoAP
  identity) before admitting the first `request`.
- An integration-hook failure or operator interrupt closes every open
  Device API handle. A node deadline closes only resources opened by the
  bounded subtree, under both `fail_after` (Fail) and `abort_after`
  (node-local Aborted). An inherited adapter or borrowed hop remains its
  owner's resource; unrelated siblings are untouched.
- `cancel_in_flight()` is idempotent. It aborts the in-flight request at
  the socket (HTTP: cancel the stream and close the connection; CoAP:
  stop CON retransmits; a RST or connection-close is the protocol cancel,
  analogous to I2C STOP, not a resource write), attempts the closed
  state under the backend's latency ceiling, and records whether close
  was confirmed. A repeated call cannot start a new request or replay the
  last one. A closed handle remains closed; there is no automatic
  reconnect.
- The framework does not transmit a "safe" PUT, DELETE, or CoAP action
  on abort. A DUT that needs a specific last request gets it from
  authored `cleanup()`, which the hard cut does not wait for. Inventing
  a `/shutdown` write at abort would be a stimulus the suite never
  declared, written into a record the technician might trust.
- Request ceiling (including protocol-level CON/TCP reliability), retry
  delay, bring-up/close ceiling, and the node's `fail_after` /
  `abort_after` are separate clocks. An operation timeout names that
  operation and its ceiling. It does not manufacture the `timeout`
  evidence reserved for the winning node clock in the
  [Time Budget Contract](../contracts.md#time-budget-contract). A long
  poll cannot extend the node clock.
- A backend whose shared session would let one node's close starve
  another owner's in-flight request must refuse the conflicting
  acquisition before either owner requests. It cannot sacrifice Group
  Isolation to offer more nodes on one socket.

**Developer obligation:** Select a fixture for which "this node stops
talking" is the intended safe state. Declare shared sessions honestly.
Keep any required shutdown request in `cleanup()`; rely on framework
ownership for termination. Never manually sequence another node's
configure or cleanup.

**Consequence:** Ending the test stops this node's requests and records
what could be confirmed. A technician is never asked to infer that a
session is idle from Pass/Fail/Aborted alone, or from a DELETE nobody
authored.

## Redirects, credentials, and the legacy action helper

**Framework obligation:**

- HTTP redirects are a declared policy, default **do not follow**.
  Same-origin follow is an opt-in on the handle. Cross-host follow is
  refused: a 302 to a second origin is data on the response, not a
  silent second request, and it must not carry the original credentials.
  A backend that auto-follows across hosts is refused at bring-up.
- Authentication material (Bearer tokens, basic passwords, client
  certificates, CoAP DTLS PSKs) is declared in `configure()` and never
  reaches evidence. Provenance names the credential *path* or slot, never
  the value — the same containment SSH already proves. Response bodies
  still go to evidence when the author puts them there; secrets in a
  body are the author's leak to stop, not a framework rewrite of the
  payload.
- TLS / DTLS verification is on by default. Skipping it is a lab-only
  declaration analogous to SSH `skip_host_verification`, logged as such
  at bring-up.
- The legacy `performAction` helper (PUT an action id, poll until a
  K2-specific status set) is **application protocol**. It is not part of
  this contract. Authors who need a long-running device action compose
  `request()` with the existing `wait_for`. Porting that helper into
  `DeviceApiInterface` would bake one vendor's action DSL into the
  framework the way decoding a J1939 PGN would bake an application
  protocol into CAN.

**Developer obligation:** Do not follow redirects in suite code to dodge
the handle policy. Do not log tokens. Do not ask the framework for a
`perform_action` sugar that hides the poll.

**Consequence:** A redirect cannot launder a second origin into a record
that still names the declared base URL. A K2 action poll is visible as
the requests it actually made.

## Lifecycle and hook ownership: unchanged

The [HIL lifecycle ruling](index.md#the-three-rulings) applies unchanged.
Concretely:

- Bring-up happens the same way `use_shell` brings up SSH: `configure()`
  obtains a handle (provisionally `utils.get_http(...)` /
  `utils.get_coap(...)`, mirroring `get_shell` / `get_gpio` / `get_pwm`),
  and the framework's bookkeeping tracks what this node opened versus
  what it inherited.
- Teardown order is the framework's: `cancel_in_flight` fires as part of
  the same In-Flight Termination Contract sweep that already closes
  `RemoteInterface` transports and silences signed GPIO/PWM resources.
- `use_survey` / `use_audit` remain the observation hooks. A periodic
  audit that GETs a health resource and raises `TestFailure` on an
  unexpected status or body is the existing audit contract.
- `before_each` / `after_each` remain the integrator's narrow context and
  still cannot touch hardware.

Hardware that needs the client to run its own concurrent receive loop
outside survey/audit, that cannot honor a hard `cancel_in_flight` (a
request that keeps running on the device after the socket is cut is the
device's problem; a client that cannot cut the socket is a finding), or
that requires the CLI-wrapper shape the legacy `CoapInterface` used, is
a finding for Ty — not a reason to give Device API its own lifecycle.

## Failure vocabulary

No new terminal state is introduced, and no new exception root.
`DeviceApiOperationFailure` derives from `LitmusError`, not
`TestFailure`, for the same reason GPIO's `PinSettleFailure` and PWM's
`PwmOperationFailure` do: failure to complete a transfer does not
identify the DUT as the cause.

| Situation | Raised as | Terminal state |
|---|---|---|
| Invalid method, path, payload, retry declaration, or conflicting in-flight use | `TypeError` / `ValueError` at the call site | `ContractViolation` (Fail, `bug: true`) if it escapes |
| HTTP 4xx / 5xx or CoAP 4.xx / 5.xx on a well-formed response | No exception: status and body are the result | Whatever verdict the author writes. The interface does not guess Fail |
| Connection refused, TLS/DTLS handshake failure, protocol violation, request exceeds its ceiling, close cannot be confirmed at bring-up | `DeviceApiOperationFailure` (provisional `LitmusError`) naming the handle, operation, method, path, and ceiling | Fail when the author catches it and states the verdict; BUG-signaled Fail when it escapes |
| Backend cannot reach the adapter at bring-up | `ConnectionError` from the interface's own `connect` | Absorbed the way `RemoteInterface` connect failures are absorbed today |
| Close cannot be confirmed during the safe-state sweep | No exception; the run is already ending | Terminal state unchanged; timeline records the unconfirmed close, naming the handle. Never a success line |
| Integration hook throws mid-request | Framework raises `ExternalIntegrationFailure` | Aborted; run-level halt closes every open handle |
| Operator SIGINT/SIGTERM mid-run | Framework raises `OperatorInterrupt` | Aborted; same run-level sweep |
| A node's time budget expires while it holds a handle | Framework settles the node per its declared budget | `fail_after` → Fail, `abort_after` → node-local Aborted; either way only that subtree's handles are closed |
| Test returns without a verdict | Existing `MissingVerdict` | Node-local Aborted. A completed `request` cannot supply the missing verdict |

Unknown is represented explicitly. A timed-out request is not a 200 with
an empty body. An unconfirmed close is not an idle session.

## Mock and real backends: one contract, two implementations

The pattern is the one `RemoteInterface` / `MockTransport` / `SshInterface`
already proves, and that signed GPIO/PWM reuse: `DeviceApiInterface` is
the ABC; `HttpInterface` and `CoapInterface` are real backends; 
`MockDeviceApi` is the test double. Self-tests exercise mock and real
against a shared contract suite so a bug in either backend is caught by
the same assertions.

Required proofs: refuse a non-`bytes` payload and an absolute-URL path
before the adapter is touched; one in-flight request; status-as-data
(4xx is a response, not an exception); retries default to zero and each
declared attempt is evidenced; non-idempotent retry refused unless opted
in; `cancel_in_flight` stops the retry loop; redirects do not follow
cross-host; credentials never appear in evidence; partial `configure()`
cannot orphan a session; node cuts leave a sibling's in-flight request
untouched; repeated `cancel_in_flight` cannot replay a POST; unconfirmed
close produces only unconfirmed evidence. `MockDeviceApi` schedules
`park`-style holds analogous to `MockTransport.park`, so lifecycle tests
can abort mid-request without a server. A suite author never constructs
either backend directly: `utils.get_http` / `utils.get_coap` resolve to
whichever backend the run was configured with.

Real-backend tests must verify transfers through independent observation
(a second client, a capturing proxy); mock statuses cannot establish
that a DUT saw the body.

Core install stays dependency-light. HTTP and CoAP clients land as
optional extras on the implementation issue; this note does not pick the
library.

## Evidence

Responses that matter to the verdict travel with it exactly as the
Evidence Contract already requires: `PASS({"status": 200, "whoami": ...})`
or `TestFailure(..., evidence={"status": 404})`, declared with
`@evidence(...)` when the method promises to carry it. Commanded
requests are stimulus, not measurements. A body field that is parametric
in the problem domain (a temperature in a JSON object, a firmware
version) follows whatever protocol issue #85 settled in
`docs/measurements.md`, referenced there rather than restated here. This
note neither fills the reserved `measurements` slot nor decodes anyone's
application protocol.

Every handle declaration — scheme, base URL, retry policy, redirect
policy, verification posture, ceilings — is logged through `utils.logger`
at configuration time. Requests, retries, operational failures, and
close attempts use that scoped logger to distinguish requested,
responded, interrupted, and unconfirmed outcomes. Attempt index and
status belong in that log even when the body does not.

## What the Floor Technician sees

When a handle is closed, the node's `timeline.jsonl` (and its
`timeline.log` twin) carries a log entry at the scope of the node that
owned it, naming the handle and whether close was confirmed, immediately
before that node's terminal entry — the same temporal-backbone placement
the In-Flight Termination Contract already gives a hard-closed SSH
transport and that signed GPIO/PWM reuse for idle/disable. The terminal
entry is not always Aborted: a run-level halt and an `abort_after` expiry
both settle Aborted, a `fail_after` expiry settles Fail, and the close
sweep is owed under every one of them.

`result.json`'s `summary_tree` shows whichever state applies; there is no
Device API-specific field, because a safe-state action is evidence of
what the framework did to protect the session, not a verdict of its own.
A technician reading the timeline sees why the run ended and — from the
log line — that this node stopped requesting. Where the sweep could not
confirm close, the timeline says exactly that, naming the handle. An
unconfirmed close never leaves a success line behind. Retry lines, when
present, sit with the request that produced them, so a Fail that landed
on the third attempt cannot be read as a single call.

## Decisions for Ty on #97

The draft asks for a ruling on these concrete obligations before
implementation:

1. One resource-request contract, two backends. HTTP and CoAP share
   method/path/payload/status/body. They are not one handle type, and
   they are not `exec` with a URL. Status (including 4xx/5xx) is data.
2. Typed `DeviceRequest` / `DeviceResponse`. Payload is `bytes`. Path is
   relative to the declared base URL. JSON parsing is convenience, not
   the contract.
3. Retries are evidence. Default is zero. Each attempt is logged.
   Non-idempotent retry is refused unless opted in. Abort cancels the
   in-flight attempt and the remainder of the loop. CoAP CON retransmits
   are protocol-level, bounded by the request ceiling, and counted in
   that attempt's evidence.
4. Legacy `CoapInterface` is reference only. The CLI-through-`exec`
   wrapper is the trap this note closes. Legacy `performAction` is
   application protocol and stays out of the framework; authors compose
   `request()` with `wait_for`.
5. Abort safe-state is a closed session (socket cancel / CON stop /
   connection-close), not a framework-invented shutdown request.
   Authored `cleanup()` may send a last request; the hard cut does not
   wait for it and does not replace it.
6. Redirects default to do-not-follow; cross-host follow is refused.
   Shared sessions refuse conflicting ownership, same Group Isolation
   posture as signed PWM. Credentials never reach evidence.

The lifecycle, verdict, and evidence contracts above are constraints on
that decision. If a required backend cannot satisfy them, the finding
belongs on #97 with the affected guarantee named. It does not earn an
alternate lifecycle or an exception enforced only by a linter.
