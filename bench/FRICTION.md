# API friction log

What it felt like to write `suites/` against the surrogates, recorded as it was met
(the second deliverable of #139: "build real tests to get a feel for the developer
API"). Each entry says what the suite wanted to say, what the framework made it say
instead, and what would close the gap. Severity is the author's guess at how often
a developer on a real rack would hit it; the ruling is Ty's.

Entries are numbered in the order they were met, not by severity (F12 was known
before the first run, so it sits last). A `fixed` line
means the bench worked around it in the suite; it does not mean the gap is closed.

## F1. `SerialInterface.connect()` never pokes the line (bug, high) — closed 2026-10-17

**Wanted:** open `/dev/ttyUSB0` on a board that booted an hour ago and get a prompt.

**Got:** `configure()` Fails with `serial shell prompt timed out ...; last bytes: ''`.
`_resolve_login` only *expects* the prompt; nothing is written first. A console that
printed its prompt before the port was opened never prints another one, and pyserial
flushes the input buffer on open anyway. The self-tests and `make bench-serial` pass
because `FakeConsole` writes its banner 0.1 s after it starts, with the comment
"wait for the slave to be claimed before writing the banner, otherwise connect sees an
empty wire": the test double was shaped to the gap. The bench's detached console
(`make bench-serial-up`) surfaced it on the first run of `serial_console.py`.

**Close it:** write `\r` after the port opens and before the first `_expect` when no
`login_prompt` is configured (and after the login exchange when one is), the way every
terminal program does; a self-test with a console that emitted its prompt *before*
the open. Until then the serial suite is red at configure and the rack carries it.
The suite's claims themselves were proven green with a just-in-time console.

**Closed:** `_resolve_login` writes `\r` once the wire is open, before the first
`_expect` (so a getty that printed `login:` an hour ago re-prompts too), and after
the shell prompt resolves it drains the wire until it has been quiet for 0.2 s
(bounded at 2 s) so the second prompt a console that was already at its prompt
answers with is not the first exec's match. A poke the wire refuses is left for
`_expect` to name as closed. Self-tests: a console that only answers input, a
getty that only answers input, and the two-prompt case. `make bench-suite-serial`
is green against the detached pair, twice over: the first green run exposed that
the detached console went deaf after the cancel claim. `FakeConsole` read Ctrl-C in
the same coroutine that was parked on `sleep`, so the interrupt could never land and
the park lasted until SIGTERM; in-process tests tore the console down right after
the cancel and never saw it. Bytes are now pumped by their own task, and a self-test
reconnects after a cancel.

## F2. No `use_serial` / `get_serial`: the console cannot be a route (gap, high)

**Wanted:** `await utils.use_serial(device=...)` in `configure()`, then `utils.exec`
in every claim, as `use_shell` lets an SSH suite do.

**Got:** `SerialInterface` is importable from `litmus.serial`, which `docs/api-stability.md`
says is not a promise, and nothing on `TestUtils` takes it. The suite constructs the
transport, carries the handle in ctx (stripped from the record as non-JSON), calls
`console.exec` instead of `utils.exec`, and closes it in `cleanup()`. The run-level
abort sweep (`NodeRuntime.register`) is framework-only, so an operator Ctrl-C does not
reach the port; the suite's own cleanup does. The survey/audit pattern that reads the
device through `utils.exec` does not work for a serial device at all.

**Close it:** `use_serial(...)` / `get_serial(...)` on `TestUtils` in the shape of
`use_shell` / `get_shell`, `SerialInterface` promoted to the promised surface.

## F3. Cleanup after a failed `configure()` sees an empty ctx (idiom, medium)

**Wanted:** `cleanup()` reads `utils.ctx["console"]` and closes it.

**Got:** when `configure()` raised, `cleanup()` still ran and the `[]` read was a
`ContractViolation` ("context insufficient"), logged beside the real Fail. The tier-3
rule `litmus/ctx-provenance` is happy because `configure()` *does* return the key; the
tier-1 guard fires because it never got to. Fixed in the suite with `ctx.get`, which the
rule documents as the escape; nothing in `docs/idioms.md` says cleanup must use it.

**Close it:** a sentence in the cleanup idiom, or skip `cleanup()` when `configure()`
did not settle (the lifecycle example suggests the latter is not the intent).

## F4. Two tier-3 rules collide on an API call in `cleanup()` (tooling, medium)

**Wanted:** `cleanup()` that `DELETE`s the deny-list entry it added.

**Got:** `litmus/model-resolution-failure` wants every awaited `request()` inside a
`try` with a handler that raises `TestFailure`; `litmus/no-verdict-in-cleanup` forbids
raising `TestFailure` in `cleanup()`. No `cleanup()` can satisfy both. Dodged with a
module-level `_settle()` helper, which the rule does not look inside.

**Close it:** the resolution rule accepts a handler that logs when the enclosing
method is `cleanup`, or a survey/audit hook.

## F5. A session credential has to ride in ctx, which is on the record (gap, medium)

**Wanted:** log in to FTL's API once in `configure()` and have every claim's request
carry the session id.

**Got:** `get_http(token=...)` is Bearer-only and headers are fixed at open, so the
`sid` that `POST /api/auth` returns cannot be attached to the open session. It goes in
ctx (the only channel to the claims), and ctx is published as `run_data`, so the record
of every Pi-hole run carries a live session id until logout. Every request also repeats
`headers={"sid": ...}` by hand.

**Close it:** `DeviceApi.set_header(name, value)` (or `headers` mutable on the handle),
and a way to mark a ctx key as not-for-the-record.

## F6. JSON is hand-rolled on both sides of `DeviceRequest` (ergonomics, low)

`payload` is `bytes`, so every JSON body is `json.dumps(...).encode()` plus a
`Content-Type` header; `DeviceResponse` has no `json()`, so every reading is
`json.loads(response.body)`. Six times in one suite. A `json=` argument on the request
and a `.json()` on the response would halve the Pi-hole child.

## F7. CoAP codes arrive as bare integers (ergonomics, low)

`GET /hello` is `69`, `PUT` is `68`, a miss is `132`: `class << 5 | detail`. The suite
defines the constants itself. Named codes (or a `(2, 5)` tuple with a `__str__` of
`2.05`) on `DeviceResponse` would make the evidence readable on the record.

## F8. `utils.env` has `text`, `require`, `port` and nothing numeric (ergonomics, low)

A baud rate or a limit from the environment is `int(utils.env.require(...))` with a
`ValueError` that is a developer-bug Fail, not a named one. `env.int(name, default=)`
and `env.float` beside `port` would carry the same "Fails naming the variable" promise.

## F9. `wait_for` has no channel for the observed value (ergonomics, low)

The query-log claim needs the entries it found, not just that it found some. The
predicate writes into a list it closes over. A predicate returning a truthy value that
`wait_for` hands back would remove the closure.

## F10. `measure()` refuses NaN with a ContractViolation (observation, low)

A blank reading over the hop (`float("")`) is a `ValueError`, and a NaN is a contract
violation naming the suite as buggy. Both are right by the measurements note; both mean
every reading taken over a shell needs its own `try` before `measure`. The guest suite
has five of them. A `measure` that accepts `None` and records "not taken" would move
the gap to the record, where the FT reads it, without lowering the NaN guard.

## F11. The gateway shape needs `::Class` on the command line (observation, low)

`pihole_rack.py` holds the gateway and the device behind it, which is the shape
`examples/networked_device.py` recommends; `litmus run bench/suites/pihole_rack.py`
refuses a module with two suites. The Makefile says `::PiHole`. A module-level
`__suite__ = PiHole` the runner honours would let the file name the default.

## F13. One in-flight request per `DeviceApi` handle, and `parallel` does not know (gap, high)

**Wanted:** `configure()` opens one CoAP session; five independent echo leaves under
a `parallel` each make one request on it.

**Got:** every leaf but the first died with `ValueError: one in-flight request per
handle; wait or cancel`, classified as a developer bug with the banner "the node
reached a state the author did not explicitly handle; return PASS or raise
TestFailure", which is the wrong advice: the leaf did neither because a `ValueError`
escaped, and the real cause is only in `failure.cause` on the record. The one-request
rule is in `docs/hil/device-api.md`; the idiom page says "open a session from
`configure()`" and "use `parallel` for independent claims" without saying the two
cannot be combined on one handle. Fixed in the suite with a `sequence`.

**Close it:** either the handle serialises requests (a lock, so `parallel` leaves
queue), or `request()` on a busy handle raises `DeviceApiOperationFailure` so the
existing rule catches it, or the idiom page says one handle per concurrent leaf. And
the banner for an escaped non-`TestFailure` should name the exception, not advise
returning `PASS`.

## F14. An evidence key may not reuse a ctx key, and only the contract page says so (idiom, medium)

Two of five suites hit `evidence shadows a ctx key or reserved slot` on their first run
(`kernel` in the guest, `probe` and `domain` in the Pi-hole child): the natural name
for a reading is the natural name for the limit or target it was judged against.
`docs/contracts.md` states the rule; `docs/idioms.md` does not, and `litmus-check` has
no rule for it although both sides are literals in the same class. Fixed by renaming
the evidence keys.

**Close it:** a tier-3 rule (`litmus/evidence-shadows-ctx`), and a line in the idioms
page next to `@evidence`.

## F15. `CoapInterface` has no block-wise transfer: a large echo is 2.04 and short (bug, high) — closed 2026-10-17

**Wanted:** `PUT /echo` with 1500 bytes comes back as 1500 bytes.

**Got:** `code 68 (2.04), 768 bytes, intact=False` with no `truncated` flag and no
error; 1024 bytes and below round-trip whole. The server is the reference stack,
which answers a body past the block size with a Block2 first block; the transport
takes it as the whole body. A device API that fetches a log or pushes a config blob
will see success codes over partial bodies.

**Close it:** RFC 7959 Block1/Block2 in `CoapInterface`, or at least a
`DeviceApiOperationFailure` when a Block2 option arrives on a response, so the suite
sees a failure rather than a short body.

**Closed:** RFC 7959 in `CoapInterface`. A body past 1024 bytes goes out as Block1
blocks; a 2.31 that names a smaller SZX shrinks the rest of the transfer, and any
other answer mid-transfer is returned as the response. A response that arrives with
Block2 is fetched to the end with Block2-only continuations (no payload, no Block1:
how aiocoap keys the transfer, on code and path) and handed over whole; a block out
of sequence is a `DeviceApiOperationFailure`. The 768 bytes were a second bug under
the first: the option decoder never consumed a delta extension byte, so any option
numbered 13 or above put the payload marker in the wrong place. The decoder now
handles 13- and 14-nibble deltas and lengths. Self-tests run against a loopback
server with its own codec; `make bench-suite-coap` is green against aiocoap at every
size including 1500.

## F12. No GPIO backend, so the suite holds blocking `gpiod` calls (known, by ruling)

`docs/hil/gpio.md` is signed; `utils.get_gpio` is not built. `gpio_lines.py` calls
`gpiod` inside the claims under `asyncio.to_thread` and relies on `with` to release
the lines. The abort sweep does not know about them. Recorded so the GPIO
implementation issue can point at a suite that already exists.
