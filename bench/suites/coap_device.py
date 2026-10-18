"""A CoAP device through ``utils.get_coap`` (``make bench-suite-coap``).

The device is the bench's ``aiocoap`` container: ``/hello`` answers GET, ``/echo``
returns what PUT sent. The suite opens the session in ``configure()``, keeps the
handle in ctx, and makes three kinds of claim: the device identifies itself, a missing
resource is 4.04 (a status is data, not an exception), and payloads of several sizes
survive the round trip whole, one leaf per size so each size is its own verdict and
its own latency measurement. The echo leaves are independent claims and run as a
``parallel`` on the one session: the handle serialises their requests in call order,
so each measured latency includes the wait behind its siblings. The latency limit is
set with that in mind; a claim about the device alone would take one handle per leaf.
Sizes past 1024 bytes need block-wise transfer, which is exactly the kind of edge the
pressure test exists to find (and did: bench/FRICTION.md, F15).

Environment: ``COAP_URL`` (``coap://127.0.0.1:5683`` at the bench).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from litmus import (
    PASS,
    DeviceApi,
    DeviceApiOperationFailure,
    DeviceRequest,
    LitmusTest,
    TestFailure,
    TestUtils,
    Verdict,
    evidence,
)

# CoAP codes as the transport reports them: class << 5 | detail.
_CONTENT = 2 << 5 | 5
_CHANGED = 2 << 5 | 4
_NOT_FOUND = 4 << 5 | 4


class CoapDevice(LitmusTest):
    """Identify the device, probe a missing resource, and echo payloads of each size."""

    def __init__(
        self,
        *,
        name: str = "coap",
        payload_sizes: Sequence[int] = (1, 64, 512, 1024, 1500),
        latency_limit_ms: float = 250.0,
        request_timeout_s: float = 5.0,
    ) -> None:
        """Capture the sizes and the latency limit; the URL comes from the run.

        Args:
            name: Node name used by logs, artifacts, and failures.
            payload_sizes: One ``echo`` leaf per size, in bytes.
            latency_limit_ms: Ceiling on a request's round trip.
            request_timeout_s: Per-request transport timeout.
        """
        super().__init__(name=name)
        self._payload_sizes = tuple(payload_sizes)
        self._latency_limit_ms = latency_limit_ms
        self._request_timeout_s = request_timeout_s

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Open the CoAP session the claims share."""
        url = utils.env.require("COAP_URL")
        try:
            api = await utils.get_coap(base_url=url, request_timeout=self._request_timeout_s)
        except DeviceApiOperationFailure as error:
            raise TestFailure(f"could not open a CoAP session to {url}: {error}") from error
        return {"api": api, "latency_limit_ms": self._latency_limit_ms}

    def build_suite(self) -> LitmusTest:
        """Identity first, then the independent exchanges together.

        The exchanges are independent claims, so they are a `parallel`. They share the
        session `configure()` opened; the handle queues their requests in call order
        rather than colliding (bench/FRICTION.md, F13 — the first composition of this
        suite died with one request per handle and was flattened to a sequence).
        """
        return self.sequence(
            self.name,
            [
                self.test("hello", self._hello, fail_after=10.0),
                self.parallel(
                    "exchange",
                    [
                        self.test("missing-resource", self._missing_resource, fail_after=10.0),
                        *[
                            self.test(f"echo-{size}b", self._echo, size=size, fail_after=15.0)
                            for size in self._payload_sizes
                        ],
                    ],
                ),
            ],
        )

    @evidence("code", "body", "latency_ms")
    async def _hello(self, utils: TestUtils) -> Verdict:
        """GET /hello is 2.05 with the device's greeting, inside the latency limit."""
        response, latency_ms = await self._timed(utils, DeviceRequest(method="GET", path="/hello"))
        reading = utils.measure(
            "hello_latency_ms", latency_ms, unit="ms", limits=(None, utils.ctx["latency_limit_ms"])
        )
        body = response.body.decode(errors="replace")
        if response.status == _CONTENT and "litmus" in body and reading.in_limits:
            return PASS({"code": response.status, "body": body, "latency_ms": latency_ms})
        raise TestFailure(
            "device did not greet",
            f"2.05 mentioning litmus within {reading.limits} ms",
            f"code {response.status}, {body!r}, {latency_ms} ms",
        )

    @evidence("code")
    async def _missing_resource(self, utils: TestUtils) -> Verdict:
        """GET on a resource the device does not serve is 4.04, as a status, not an error."""
        response, _latency = await self._timed(utils, DeviceRequest(method="GET", path="/missing"))
        if response.status == _NOT_FOUND:
            return PASS({"code": response.status})
        raise TestFailure("missing resource not reported", "4.04", f"code {response.status}")

    @evidence("size", "code", "intact", "latency_ms")
    async def _echo(self, utils: TestUtils, *, size: int) -> Verdict:
        """PUT /echo returns the payload byte for byte."""
        payload = bytes(i & 0xFF for i in range(size))
        response, latency_ms = await self._timed(
            utils, DeviceRequest(method="PUT", path="/echo", payload=payload)
        )
        reading = utils.measure(
            "echo_latency_ms", latency_ms, unit="ms", limits=(None, utils.ctx["latency_limit_ms"])
        )
        intact = response.body == payload
        if response.status == _CHANGED and intact and reading.in_limits:
            return PASS(
                {"size": size, "code": response.status, "intact": intact, "latency_ms": latency_ms}
            )
        raise TestFailure(
            f"{size}-byte echo did not come back whole",
            f"2.04, {size} bytes intact, within {reading.limits} ms",
            f"code {response.status}, {len(response.body)} bytes, intact={intact}, {latency_ms} ms",
        )

    async def _timed(self, utils: TestUtils, request: DeviceRequest) -> tuple[Any, float]:
        """One request on the shared session with its round trip in milliseconds.

        The clock starts before ``request`` takes its turn on the handle, so under the
        ``parallel`` this is the leaf's wait plus the device's answer.
        """
        api: DeviceApi = utils.ctx["api"]
        started = time.monotonic()
        try:
            response = await api.request(request)
        except DeviceApiOperationFailure as error:
            raise TestFailure(f"{request.method} {request.path} failed: {error}") from error
        return response, round((time.monotonic() - started) * 1000.0, 1)
