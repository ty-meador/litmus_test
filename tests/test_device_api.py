"""Device API contract: mock, HTTP, and CoAP on the same bar (issue #119)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Protocol

import pytest

from litmus import (
    PASS,
    CoapCode,
    DeviceApi,
    DeviceApiOperationFailure,
    DeviceRequest,
    DeviceResponse,
    LitmusTest,
    SummaryTreeNode,
    TestFailure,
    TestUtils,
    Verdict,
)
from litmus.device_api import (
    CoapInterface,
    DeviceApiInterface,
    HttpInterface,
    MockDeviceApi,
)
from litmus.lifecycle import State
from litmus.utils import node_runtime


class _Router(Protocol):
    def __call__(
        self, method: str, path: str, body: bytes, headers: dict[str, str]
    ) -> tuple[int, bytes, dict[str, str]]: ...


def _echo_router(
    method: str, path: str, body: bytes, headers: dict[str, str]
) -> tuple[int, bytes, dict[str, str]]:
    del method, headers
    if path == "/missing":
        return 404, b"gone", {}
    if path == "/redirect-cross":
        return 302, b"", {"Location": "http://example.invalid/elsewhere"}
    if path == "/echo":
        return 200, body, {}
    return 200, path.encode(), {}


async def _http_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    router: _Router,
    *,
    slow_path: str | None = None,
) -> None:
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            method, path, _rest = line.decode("iso-8859-1").split(" ", 2)
            path = path.split("?", 1)[0]
            headers: dict[str, str] = {}
            content_length = 0
            while True:
                raw = await reader.readline()
                if raw in {b"\r\n", b"\n", b""}:
                    break
                name, _, value = raw.decode("iso-8859-1").partition(":")
                headers[name.strip().lower()] = value.strip()
                if name.strip().lower() == "content-length":
                    content_length = int(value.strip())
            body = await reader.readexactly(content_length) if content_length else b""
            if slow_path is not None and path == slow_path:
                await asyncio.sleep(0.2)
            status, out, extra = router(method, path, body, headers)
            extra_lines = "".join(f"{key}: {value}\r\n" for key, value in extra.items())
            writer.write(
                f"HTTP/1.1 {status} X\r\nContent-Length: {len(out)}\r\n{extra_lines}\r\n".encode(
                    "ascii"
                )
                + out
            )
            await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


@pytest.fixture
async def mock_api() -> AsyncIterator[MockDeviceApi]:
    backend = MockDeviceApi()
    await backend.connect()
    try:
        yield backend
    finally:
        await backend.disconnect()


@pytest.fixture
async def http_api() -> AsyncIterator[HttpInterface]:
    server = await asyncio.start_server(
        lambda reader, writer: _http_client(reader, writer, _echo_router),
        "127.0.0.1",
        0,
    )
    port = server.sockets[0].getsockname()[1]
    backend = HttpInterface(base_url=f"http://127.0.0.1:{port}")
    await backend.connect()
    try:
        yield backend
    finally:
        await backend.disconnect()
        server.close()
        await server.wait_closed()


_COAP_URI_PATH = 11
_COAP_BLOCK2 = 23
_COAP_BLOCK1 = 27
_COAP_CONTENT = 69  # 2.05
_COAP_CHANGED = 68  # 2.04
_COAP_CONTINUE = 95  # 2.31
_COAP_NOT_FOUND = 132  # 4.04
_COAP_INCOMPLETE = 136  # 4.08
_COAP_TOO_LARGE = 141  # 4.13
_BIG_BODY = bytes(range(256)) * 12  # 3072 bytes: three 1024-byte blocks
type _BlockTuple = tuple[int, bool, int]
type _Seen = tuple[str, _BlockTuple | None, _BlockTuple | None, int]


class _CoapServer(asyncio.DatagramProtocol):
    """Loopback CoAP with RFC 7959 block-wise, written apart from the client codec.

    ``/missing`` is 4.04, ``/echo`` is 2.04 with the request body, ``/big`` is
    2.05 with ``_BIG_BODY``, anything else is 2.05 with the path. Bodies past
    ``szx`` go out as Block2; Block1 requests are spooled and, when ``refuse``
    is set, answered 4.13 at the first block. ``misnumber`` answers Block2
    continuations with the wrong block, the way a confused server would.
    """

    def __init__(self, *, szx: int = 6, refuse: bool = False, misnumber: bool = False) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.szx = szx
        self.refuse = refuse
        self.misnumber = misnumber
        # (path, block1, block2, payload length) per datagram, in arrival order.
        self.seen: list[_Seen] = []
        self._spool: dict[tuple[object, int, str], bytearray] = {}
        self._cache: dict[tuple[object, int, str], tuple[int, bytes]] = {}

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.transport is None or len(data) < 4:
            return
        code, token, options, payload = _coap_parse(data)
        path = "/" + "/".join(v.decode() for n, v in options if n == _COAP_URI_PATH)
        block1 = _block(options, _COAP_BLOCK1)
        block2 = _block(options, _COAP_BLOCK2)
        self.seen.append((path, block1, block2, len(payload)))
        key = (addr, code, path)
        if block1 is not None:
            num, more, szx = block1
            if self.refuse:
                self._reply(data, token, addr, _COAP_TOO_LARGE, b"")
                return
            if num == 0:
                self._spool[key] = bytearray()
            spool = self._spool.get(key)
            if spool is None or num * (1 << (szx + 4)) != len(spool):
                self._reply(data, token, addr, _COAP_INCOMPLETE, b"")
                return
            spool += payload
            if more:
                ack = _block_value(num, True, min(szx, self.szx))
                self._reply(data, token, addr, _COAP_CONTINUE, b"", (_COAP_BLOCK1, ack))
                return
            payload = bytes(self._spool.pop(key))
        if block2 is not None and block2[0] > 0:
            cached = self._cache.get(key)
            if cached is None:
                self._reply(data, token, addr, _COAP_INCOMPLETE, b"")
                return
            status, full = cached
        else:
            status, full = self._render(path, payload)
            self._cache[key] = (status, full)
        szx = self.szx if block2 is None else min(self.szx, block2[2])
        size = 1 << (szx + 4)
        if len(full) <= size:
            self._reply(data, token, addr, status, full)
            return
        num = 0 if block2 is None else block2[0]
        if self.misnumber and num > 0:
            num += 1
        chunk = full[num * size : (num + 1) * size]
        more = (num + 1) * size < len(full)
        self._reply(data, token, addr, status, chunk, (_COAP_BLOCK2, _block_value(num, more, szx)))

    @staticmethod
    def _render(path: str, payload: bytes) -> tuple[int, bytes]:
        if path == "/missing":
            return _COAP_NOT_FOUND, b""
        if path == "/echo":
            return _COAP_CHANGED, payload
        if path == "/big":
            return _COAP_CONTENT, _BIG_BODY
        return _COAP_CONTENT, path.encode()

    def _reply(
        self,
        request: bytes,
        token: bytes,
        addr: tuple[str, int],
        status: int,
        body: bytes,
        option: tuple[int, bytes] | None = None,
    ) -> None:
        assert self.transport is not None
        header = bytes([(1 << 6) | (2 << 4) | len(token), status, request[2], request[3]])
        reply = bytearray(header + token)
        if option is not None:
            number, value = option
            reply.append((13 << 4) | len(value))
            reply.append(number - 13)
            reply += value
        if body:
            reply += b"\xff" + body
        self.transport.sendto(bytes(reply), addr)


def _block_value(num: int, more: bool, szx: int) -> bytes:
    value = (num << 4) | (int(more) << 3) | szx
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


def _block(options: list[tuple[int, bytes]], number: int) -> tuple[int, bool, int] | None:
    for option, raw in options:
        if option == number:
            value = int.from_bytes(raw, "big")
            return value >> 4, bool(value & 8), value & 7
    return None


def _coap_parse(datagram: bytes) -> tuple[int, bytes, list[tuple[int, bytes]], bytes]:
    """Code, token, options, payload, parsed the long way for the loopback server."""
    tkl = datagram[0] & 0x0F
    code = datagram[1]
    token = datagram[4 : 4 + tkl]
    rest = datagram[4 + tkl :]
    options: list[tuple[int, bytes]] = []
    last = 0
    while rest and rest[0] != 0xFF:
        delta = (rest[0] >> 4) & 0x0F
        length = rest[0] & 0x0F
        rest = rest[1:]
        if delta == 13:
            delta = 13 + rest[0]
            rest = rest[1:]
        elif delta == 14:
            delta = 269 + int.from_bytes(rest[:2], "big")
            rest = rest[2:]
        if length == 13:
            length = 13 + rest[0]
            rest = rest[1:]
        elif length == 14:
            length = 269 + int.from_bytes(rest[:2], "big")
            rest = rest[2:]
        last += delta
        options.append((last, rest[:length]))
        rest = rest[length:]
    payload = rest[1:] if rest else b""
    return code, token, options, payload


@asynccontextmanager
async def _coap_loopback(**kwargs: Any) -> AsyncGenerator[tuple[CoapInterface, _CoapServer]]:
    loop = asyncio.get_running_loop()
    transport, server = await loop.create_datagram_endpoint(
        lambda: _CoapServer(**kwargs), local_addr=("127.0.0.1", 0)
    )
    port = transport.get_extra_info("sockname")[1]
    backend = CoapInterface(base_url=f"coap://127.0.0.1:{port}", request_timeout=5.0)
    await backend.connect()
    try:
        yield backend, server
    finally:
        await backend.disconnect()
        transport.close()


@pytest.fixture
async def coap_api() -> AsyncIterator[CoapInterface]:
    loop = asyncio.get_running_loop()
    transport, _protocol = await loop.create_datagram_endpoint(
        _CoapServer, local_addr=("127.0.0.1", 0)
    )
    port = transport.get_extra_info("sockname")[1]
    backend = CoapInterface(base_url=f"coap://127.0.0.1:{port}")
    await backend.connect()
    try:
        yield backend
    finally:
        await backend.disconnect()
        transport.close()


@pytest.fixture(params=["mock", "http", "coap"])
async def api(
    request: pytest.FixtureRequest,
    mock_api: MockDeviceApi,
    http_api: HttpInterface,
    coap_api: CoapInterface,
) -> DeviceApiInterface:
    mapping: dict[str, DeviceApiInterface] = {
        "mock": mock_api,
        "http": http_api,
        "coap": coap_api,
    }
    backend = mapping[str(request.param)]
    if request.param == "mock":
        mock_api.script("GET", "/whoami", DeviceResponse(status=200, body=b"/whoami"))
        mock_api.script("GET", "/missing", DeviceResponse(status=404, body=b"gone"))
        mock_api.script("POST", "/echo", DeviceResponse(status=200, body=b"hi"))
    return backend


class TestCoapCode:
    """Issue #156: the code on a CoAP response is the dotted string, not the byte."""

    def test_named_codes_match_the_wire_byte(self) -> None:
        assert CoapCode.CONTENT == "2.05" == CoapCode.from_wire(2 << 5 | 5)
        assert CoapCode.CHANGED == "2.04" == CoapCode.from_wire(2 << 5 | 4)
        assert CoapCode.NOT_FOUND == "4.04" == CoapCode.from_wire(4 << 5 | 4)
        assert CoapCode.from_wire(4 << 5 | 13) == "4.13"
        assert CoapCode.from_wire(0) == "0.00"

    @pytest.mark.parametrize("code", [True, False, -1, 256, 1.5, "69"])
    def test_a_wire_code_outside_a_byte_is_refused(self, code: object) -> None:
        with pytest.raises(ValueError, match="0-255"):
            CoapCode.from_wire(code)  # type: ignore[arg-type]

    def test_a_coap_status_is_evidence_as_itself(self) -> None:
        response = DeviceResponse(status=CoapCode.CONTENT, body=b"hello")

        assert response.status == "2.05"
        assert response.status != 69


class TestPublicSurface:
    def test_backends_are_not_on_the_public_surface(self) -> None:
        import litmus

        for name in ("MockDeviceApi", "HttpInterface", "CoapInterface", "DeviceApiInterface"):
            assert not hasattr(litmus, name)
            assert name not in litmus.__all__

    def test_author_names_are_public(self) -> None:
        import litmus

        assert litmus.DeviceApi is DeviceApi
        assert "DeviceRequest" in litmus.__all__
        assert "DeviceApiOperationFailure" in litmus.__all__


class TestJsonRequest:
    @pytest.mark.parametrize(
        "value", [{"ready": True, "label": "温度"}, [1, None], False, 0, "", None]
    )
    def test_json_encodes_every_json_value(self, value: Any) -> None:
        request = DeviceRequest(method="POST", path="/echo", json=value)

        assert request.payload is not None
        assert json.loads(request.payload) == value
        assert request.headers == {"Content-Type": "application/json"}

    def test_omitting_json_preserves_the_raw_payload(self) -> None:
        request = DeviceRequest("PUT", "/echo", b"raw")

        assert request.payload == b"raw"
        assert request.headers == {}

    @pytest.mark.parametrize("payload", [b"", b"raw"])
    def test_payload_and_json_are_mutually_exclusive(self, payload: bytes) -> None:
        with pytest.raises(ValueError, match=r"payload.*json"):
            DeviceRequest("POST", "/echo", payload=payload, json=None)

    def test_json_does_not_mutate_the_headers_or_keep_the_input_object(self) -> None:
        headers = {"sid": "secret"}
        value = {"ready": True}
        request = DeviceRequest("POST", "/echo", headers=headers, json=value)
        headers["sid"] = "changed"
        value["ready"] = False

        assert headers == {"sid": "changed"}
        assert request.headers == {"sid": "secret", "Content-Type": "application/json"}
        assert json.loads(request.payload or b"") == {"ready": True}
        with pytest.raises(TypeError):
            request.headers["other"] = "value"  # type: ignore[index]

    def test_explicit_content_type_is_preserved_case_insensitively(self) -> None:
        request = DeviceRequest(
            "POST", "/echo", json={}, headers={"content-type": "application/vnd.dut+json"}
        )

        assert request.headers == {"content-type": "application/vnd.dut+json"}

    @pytest.mark.parametrize("value", [object(), {"bad": object()}])
    def test_non_serializable_json_is_refused_at_construction(self, value: Any) -> None:
        with pytest.raises(TypeError, match="not JSON serializable"):
            DeviceRequest("POST", "/echo", json=value)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_json_is_refused_at_construction(self, value: float) -> None:
        with pytest.raises(ValueError, match="Out of range float"):
            DeviceRequest("POST", "/echo", json={"reading": value})


class TestResponseParsing:
    @pytest.mark.parametrize(
        "value", [{"ready": True, "label": "温度"}, [1, None], False, 0, "", None]
    )
    def test_json_and_parse_decode_without_changing_the_response(self, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        response = DeviceResponse(400, body)

        assert response.json() == value
        assert response.parse() == value
        assert response.parse(response_type="json") == value
        assert response.status == 400
        assert response.body == body

    @pytest.mark.parametrize("body", [b"", b"not json", b"{", b"\xff"])
    def test_bad_json_raises_a_decode_error_and_keeps_the_bytes(self, body: bytes) -> None:
        response = DeviceResponse(200, body)

        with pytest.raises((json.JSONDecodeError, UnicodeDecodeError)):
            response.json()
        assert response.body == body

    def test_yaml_uses_the_selected_parser(self) -> None:
        response = DeviceResponse(200, "ready: true\nlabel: 温度\n".encode())

        assert response.parse(response_type="yaml") == {"ready": True, "label": "温度"}

    def test_yaml_does_not_construct_python_objects(self) -> None:
        import yaml

        response = DeviceResponse(200, b"!!python/object/apply:builtins.list []")

        with pytest.raises(yaml.YAMLError):
            response.parse(response_type="yaml")

    def test_yaml_extra_is_required_only_when_yaml_is_selected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "yaml", None)
        response = DeviceResponse(200, b'{"ready": true}')

        assert response.json() == {"ready": True}
        with pytest.raises(ImportError, match=r"litmus\[yaml\]"):
            response.parse(response_type="yaml")

    def test_unknown_response_type_is_refused(self) -> None:
        with pytest.raises(ValueError, match="response_type"):
            DeviceResponse(200, b"{}").parse(response_type="toml")  # type: ignore[arg-type]


class TestSharedContract:
    async def test_json_body_round_trip(self, api: DeviceApiInterface) -> None:
        request = DeviceRequest("POST", "/echo", json={"ready": True, "label": "温度"})
        if isinstance(api, MockDeviceApi):
            api.script("POST", "/echo", DeviceResponse(200, request.payload or b""))

        response = await api.request(request)

        assert response.json() == {"ready": True, "label": "温度"}
        assert response.body == request.payload
        assert len(api.attempts) == 1

    async def test_status_200_is_data(self, api: DeviceApiInterface) -> None:
        response = await api.request(DeviceRequest(method="GET", path="/whoami"))

        assert response.status in {200, CoapCode.CONTENT}
        assert response.body in {b"/whoami", b"whoami"} or (
            isinstance(api, MockDeviceApi) and response.body == b"/whoami"
        )

    async def test_client_error_status_is_data_not_an_exception(
        self, api: DeviceApiInterface
    ) -> None:
        response = await api.request(DeviceRequest(method="GET", path="/missing"))

        assert response.status in {404, CoapCode.NOT_FOUND}

    async def test_payload_must_be_bytes(self, api: DeviceApiInterface) -> None:
        with pytest.raises(TypeError, match="payload must be bytes"):
            await api.request(DeviceRequest(method="POST", path="/echo", payload="hi"))  # type: ignore[arg-type]

    async def test_absolute_url_path_is_refused(self, api: DeviceApiInterface) -> None:
        with pytest.raises(ValueError, match="relative"):
            await api.request(DeviceRequest(method="GET", path="http://evil.example/x"))

    async def test_boolean_retries_are_refused(self, api: DeviceApiInterface) -> None:
        with pytest.raises(TypeError, match="retries"):
            await api.request(DeviceRequest(method="GET", path="/whoami", retries=True))  # type: ignore[arg-type]

    async def test_post_retry_refused_unless_opted_in(self, api: DeviceApiInterface) -> None:
        with pytest.raises(ValueError, match="retry_non_idempotent"):
            await api.request(DeviceRequest(method="POST", path="/echo", payload=b"hi", retries=1))

    async def test_retries_default_to_zero(self, api: DeviceApiInterface) -> None:
        await api.request(DeviceRequest(method="GET", path="/whoami"))

        assert len(api.attempts) == 1

    async def test_disconnected_request_raises_connection_error(
        self, api: DeviceApiInterface
    ) -> None:
        await api.disconnect()

        with pytest.raises(ConnectionError, match="not connected"):
            await api.request(DeviceRequest(method="GET", path="/whoami"))

    async def test_concurrent_requests_on_one_handle_all_complete(
        self, api: DeviceApiInterface
    ) -> None:
        """Mock, HTTP, and CoAP alike: five callers at once, one at a time on the wire."""
        baseline = await api.request(DeviceRequest(method="GET", path="/whoami"))
        before = len(api.attempts)
        responses = await asyncio.gather(
            *(api.request(DeviceRequest(method="GET", path="/whoami")) for _ in range(5))
        )
        assert [(r.status, r.body) for r in responses] == [(baseline.status, baseline.body)] * 5
        assert api.attempts[before:] == ("0:GET /whoami",) * 5
        assert api.queued == 0


class TestMockHalt:
    async def test_cancel_fails_a_parked_request_and_is_idempotent(self) -> None:
        backend = MockDeviceApi()
        backend.park("GET", "/slow")
        await backend.connect()
        task = asyncio.create_task(backend.request(DeviceRequest(method="GET", path="/slow")))
        deadline = asyncio.get_running_loop().time() + 2
        while not backend.has_in_flight:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0)

        await backend.cancel_in_flight()
        await backend.cancel_in_flight()

        with pytest.raises(DeviceApiOperationFailure, match="closed"):
            await task
        assert backend.connected is False
        assert backend.cancel_count == 1
        assert backend.close_confirmed is True

    async def test_operational_retry_is_evidenced(self) -> None:
        backend = MockDeviceApi()

        async def fail_once(
            method: str, path: str, payload: bytes | None, headers: object
        ) -> DeviceResponse:
            del payload, headers
            if len(backend.attempts) == 1:
                raise DeviceApiOperationFailure(
                    "flaky",
                    handle=backend.handle_label,
                    operation="request",
                    method=method,
                    path=path,
                )
            return DeviceResponse(status=200, body=b"ok")

        backend._exchange = fail_once  # type: ignore[method-assign]
        await backend.connect()
        response = await backend.request(
            DeviceRequest(method="GET", path="/flaky", retries=1, retry_delay=0.01)
        )

        assert response.body == b"ok"
        assert backend.attempts == ("0:GET /flaky", "1:GET /flaky")


def _walk(node: SummaryTreeNode) -> Iterator[SummaryTreeNode]:
    yield node
    for child in node.children:
        yield from _walk(child)


async def _until(condition: Callable[[], bool]) -> None:
    deadline = asyncio.get_running_loop().time() + 2
    while not condition():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0)


class TestOneTurnAtATime:
    """Concurrent requests on one handle run one at a time, in call order (F13)."""

    @staticmethod
    def _gate_exchange(backend: MockDeviceApi, gates: dict[str, asyncio.Event]) -> None:
        async def gated(
            method: str, path: str, payload: bytes | None, headers: object
        ) -> DeviceResponse:
            del method, payload, headers
            await gates.setdefault(path, asyncio.Event()).wait()
            return DeviceResponse(status=200, body=path.encode())

        backend._exchange = gated  # type: ignore[method-assign]

    async def test_concurrent_requests_queue_and_complete_in_call_order(self) -> None:
        backend = MockDeviceApi()
        gates: dict[str, asyncio.Event] = {}
        self._gate_exchange(backend, gates)
        await backend.connect()
        try:
            tasks = [
                asyncio.create_task(backend.request(DeviceRequest(method="GET", path=f"/{i}")))
                for i in range(5)
            ]
            await _until(lambda: backend.queued == 4)
            # Only the first is on the wire; the rest wait their turn, unsent.
            assert backend.attempts == ("0:GET /0",)
            for i in range(5):
                gates.setdefault(f"/{i}", asyncio.Event()).set()
            responses = await asyncio.gather(*tasks)
        finally:
            await backend.disconnect()

        assert [response.body for response in responses] == [f"/{i}".encode() for i in range(5)]
        assert backend.attempts == tuple(f"0:GET /{i}" for i in range(5))
        assert backend.queued == 0

    async def test_cancel_fails_the_queued_waiters_without_sending_them(self) -> None:
        backend = MockDeviceApi()
        backend.park("GET", "/slow")
        await backend.connect()
        running = asyncio.create_task(backend.request(DeviceRequest(method="GET", path="/slow")))
        waiters = [
            asyncio.create_task(backend.request(DeviceRequest(method="GET", path=f"/queued-{i}")))
            for i in range(2)
        ]
        await _until(lambda: backend.has_in_flight and backend.queued == 2)

        await backend.cancel_in_flight()

        with pytest.raises(DeviceApiOperationFailure, match="closed"):
            await running
        for waiter in waiters:
            with pytest.raises(
                DeviceApiOperationFailure, match=r"cancelled or closed.*queued"
            ) as raised:
                await waiter
            assert raised.value.operation == "request"
            assert raised.value.method == "GET"
        # The waiters never reached the wire: the record shows one attempt, not three.
        assert backend.attempts == ("0:GET /slow",)
        assert backend.queued == 0
        assert backend.connected is False

    async def test_disconnect_fails_the_queued_waiters_the_same_way(self) -> None:
        backend = MockDeviceApi()
        backend.park("GET", "/slow")
        await backend.connect()
        running = asyncio.create_task(backend.request(DeviceRequest(method="GET", path="/slow")))
        waiter = asyncio.create_task(backend.request(DeviceRequest(method="GET", path="/queued")))
        await _until(lambda: backend.has_in_flight and backend.queued == 1)

        await backend.disconnect()

        with pytest.raises(DeviceApiOperationFailure, match="closed"):
            await running
        with pytest.raises(DeviceApiOperationFailure, match="queued"):
            await waiter
        assert backend.attempts == ("0:GET /slow",)

    async def test_a_queued_request_ceiling_covers_its_transfer_not_the_wait(self) -> None:
        backend = MockDeviceApi()
        gates: dict[str, asyncio.Event] = {}
        self._gate_exchange(backend, gates)
        await backend.connect()
        try:
            first = asyncio.create_task(backend.request(DeviceRequest(method="GET", path="/first")))
            second = asyncio.create_task(
                backend.request(DeviceRequest(method="GET", path="/second", timeout=0.05))
            )
            await _until(lambda: backend.queued == 1)
            # Four ceilings spent waiting behind a sibling: not the DUT being slow.
            await asyncio.sleep(0.2)
            assert not second.done()
            gates.setdefault("/first", asyncio.Event()).set()
            assert (await first).body == b"/first"
            # Its own transfer, never released, is what the ceiling bounds.
            with pytest.raises(DeviceApiOperationFailure, match=r"exceeded 0\.05s ceiling"):
                await second
        finally:
            await backend.disconnect()

    async def test_retries_hold_the_turn_as_one_request(self) -> None:
        backend = MockDeviceApi()
        gates: dict[str, asyncio.Event] = {}
        self._gate_exchange(backend, gates)
        flaky_calls = 0
        gated = backend._exchange  # pyright: ignore[reportPrivateUsage]

        async def flaky_then_gated(
            method: str, path: str, payload: bytes | None, headers: Mapping[str, str]
        ) -> DeviceResponse:
            nonlocal flaky_calls
            if path == "/flaky" and flaky_calls == 0:
                flaky_calls += 1
                raise DeviceApiOperationFailure(
                    "flaky",
                    handle=backend.handle_label,
                    operation="request",
                    method=method,
                    path=path,
                )
            return await gated(method, path, payload, headers)

        backend._exchange = flaky_then_gated  # type: ignore[method-assign]
        await backend.connect()
        try:
            flaky = asyncio.create_task(
                backend.request(
                    DeviceRequest(method="GET", path="/flaky", retries=1, retry_delay=0.05)
                )
            )
            other = asyncio.create_task(backend.request(DeviceRequest(method="GET", path="/other")))
            await _until(lambda: backend.queued == 1)
            gates.setdefault("/flaky", asyncio.Event()).set()
            gates.setdefault("/other", asyncio.Event()).set()
            await asyncio.gather(flaky, other)
        finally:
            await backend.disconnect()
        # The retry ran before the sibling's request: one logical request, one turn.
        assert backend.attempts == ("0:GET /flaky", "1:GET /flaky", "0:GET /other")


class TestHttpSpecific:
    async def test_cross_host_redirect_is_data(self) -> None:
        server = await asyncio.start_server(
            lambda reader, writer: _http_client(reader, writer, _echo_router),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        backend = HttpInterface(base_url=f"http://127.0.0.1:{port}", follow_redirects=True)
        await backend.connect()
        try:
            response = await backend.request(DeviceRequest(method="GET", path="/redirect-cross"))
        finally:
            await backend.disconnect()
            server.close()
            await server.wait_closed()

        assert response.status == 302
        assert "example.invalid" in (response.headers.get("Location") or "")

    async def test_echo_payload_round_trip(self, http_api: HttpInterface) -> None:
        response = await http_api.request(
            DeviceRequest(method="POST", path="/echo", payload=b"hello")
        )

        assert response.status == 200
        assert response.body == b"hello"

    async def test_json_bytes_and_content_type_reach_the_server_without_body_logging(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="litmus")
        received: list[tuple[bytes, dict[str, str]]] = []

        def router(
            method: str, path: str, body: bytes, headers: dict[str, str]
        ) -> tuple[int, bytes, dict[str, str]]:
            del method, path
            received.append((body, headers))
            return 400, body, {"Content-Type": "application/json"}

        server = await asyncio.start_server(
            lambda reader, writer: _http_client(reader, writer, router), "127.0.0.1", 0
        )
        port = server.sockets[0].getsockname()[1]
        backend = HttpInterface(base_url=f"http://127.0.0.1:{port}")
        await backend.connect()
        try:
            response = await backend.request(
                DeviceRequest("POST", "/echo", json={"password": "body-secret"})
            )
        finally:
            await backend.disconnect()
            server.close()
            await server.wait_closed()

        assert response.status == 400
        assert response.json() == {"password": "body-secret"}
        assert len(received) == 1
        assert json.loads(received[0][0]) == {"password": "body-secret"}
        assert received[0][1]["content-type"] == "application/json"
        assert "body-secret" not in caplog.text

    async def test_host_header_is_not_duplicated(self, http_api: HttpInterface) -> None:
        response = await http_api.request(
            DeviceRequest(method="GET", path="/whoami", headers={"Host": "evil"})
        )
        assert response.status == 200

    async def test_response_without_content_length(self) -> None:
        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read(64)
            writer.write(b"HTTP/1.1 204 X\r\n\r\n")
            await writer.drain()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        backend = HttpInterface(base_url=f"http://127.0.0.1:{port}")
        await backend.connect()
        try:
            response = await backend.request(DeviceRequest(method="GET", path="/empty"))
            assert response.status == 204
            assert response.body == b""
        finally:
            await backend.disconnect()
            server.close()
            await server.wait_closed()

    async def test_same_origin_absolute_redirect_without_path(self) -> None:
        port_box: dict[str, int] = {}

        def router(
            method: str, path: str, body: bytes, headers: dict[str, str]
        ) -> tuple[int, bytes, dict[str, str]]:
            del method, body, headers
            if path == "/go":
                return 302, b"", {"Location": f"http://127.0.0.1:{port_box['port']}"}
            return 200, b"root", {}

        server = await asyncio.start_server(
            lambda reader, writer: _http_client(reader, writer, router),
            "127.0.0.1",
            0,
        )
        port_box["port"] = server.sockets[0].getsockname()[1]
        backend = HttpInterface(
            base_url=f"http://127.0.0.1:{port_box['port']}", follow_redirects=True
        )
        await backend.connect()
        try:
            response = await backend.request(DeviceRequest(method="GET", path="/go"))
            assert response.status == 200
            assert response.body == b"root"
        finally:
            await backend.disconnect()
            server.close()
            await server.wait_closed()

    async def test_timeout_is_operational(self) -> None:
        server = await asyncio.start_server(
            lambda reader, writer: _http_client(reader, writer, _echo_router, slow_path="/slow"),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        backend = HttpInterface(base_url=f"http://127.0.0.1:{port}", request_timeout=0.05)
        await backend.connect()
        try:
            with pytest.raises(DeviceApiOperationFailure, match="ceiling"):
                await backend.request(DeviceRequest(method="GET", path="/slow"))
        finally:
            await backend.disconnect()
            server.close()
            await server.wait_closed()

    async def test_header_values_are_withheld_unless_opted_in(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="litmus")
        server = await asyncio.start_server(
            lambda reader, writer: _http_client(reader, writer, _echo_router),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        backend = HttpInterface(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer super-secret-token"},
        )
        await backend.connect()
        try:
            await backend.request(
                DeviceRequest(
                    method="GET",
                    path="/whoami",
                    headers={
                        "X-Device-Token": "dut-bearer-xyz",
                        "X-Auth-Token": "another-secret",
                        "Api-Key": "key-should-not-leak",
                        "X-Station": "west-bench",
                        "Accept": "application/json",
                    },
                    evidence_headers=frozenset({"Accept", "X-Station"}),
                )
            )
        finally:
            await backend.disconnect()
            server.close()
            await server.wait_closed()
        combined = "\n".join(record.getMessage() for record in caplog.records)
        for secret in (
            "super-secret-token",
            "dut-bearer-xyz",
            "another-secret",
            "key-should-not-leak",
        ):
            assert secret not in combined
        assert "application/json" in combined
        assert "west-bench" in combined
        assert "<redacted>" in combined

    async def test_evidence_headers_rejects_a_bare_str(self, mock_api: MockDeviceApi) -> None:
        with pytest.raises(TypeError, match="set of header names"):
            await mock_api.request(
                DeviceRequest(
                    method="GET",
                    path="/whoami",
                    evidence_headers="Accept",  # type: ignore[arg-type]
                )
            )
        with pytest.raises(TypeError, match="names must be str"):
            await mock_api.request(
                DeviceRequest(
                    method="GET",
                    path="/whoami",
                    evidence_headers=frozenset({1}),  # type: ignore[arg-type]
                )
            )


class TestGetHttpLifecycle:
    async def test_suite_get_http_closes_on_cleanup(self) -> None:
        holder: dict[str, DeviceApi] = {}
        servers: list[asyncio.Server] = []

        class Probe(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("whoami", self._whoami)

            async def configure(self, utils: TestUtils) -> dict[str, DeviceApi]:
                server = await asyncio.start_server(
                    lambda reader, writer: _http_client(reader, writer, _echo_router),
                    "127.0.0.1",
                    0,
                )
                servers.append(server)
                port = server.sockets[0].getsockname()[1]
                api = await utils.get_http(
                    base_url=f"http://127.0.0.1:{port}",
                    token="lab-token",
                    headers={"X-Station": "west"},
                )
                holder["api"] = api
                return {"api": api}

            async def _whoami(self, utils: TestUtils) -> Verdict:
                api = utils.ctx["api"]
                assert isinstance(api, DeviceApi)
                response = await api.request(DeviceRequest(method="GET", path="/whoami"))
                if response.status != 200:
                    raise TestFailure("whoami rejected", 200, response.status)
                return PASS({"status": response.status})

        try:
            result = await Probe(name="dut").run()
        finally:
            for server in servers:
                server.close()
                await server.wait_closed()

        assert result.state is State.PASS
        with pytest.raises(ConnectionError, match="not connected"):
            await holder["api"].request(DeviceRequest(method="GET", path="/whoami"))

    async def test_parallel_leaves_share_one_handle_from_configure(self) -> None:
        """The F13 shape: one session in configure(), five independent echo leaves."""
        servers: list[asyncio.Server] = []
        sizes = (1, 64, 512, 1024, 1500)

        class Echoes(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "exchange",
                    [self.test(f"echo-{size}b", self._echo, size=size) for size in sizes],
                )

            async def configure(self, utils: TestUtils) -> dict[str, DeviceApi]:
                server = await asyncio.start_server(
                    lambda reader, writer: _http_client(reader, writer, _echo_router),
                    "127.0.0.1",
                    0,
                )
                servers.append(server)
                port = server.sockets[0].getsockname()[1]
                return {"api": await utils.get_http(base_url=f"http://127.0.0.1:{port}")}

            async def _echo(self, utils: TestUtils, *, size: int) -> Verdict:
                api: DeviceApi = utils.ctx["api"]
                payload = bytes(i & 0xFF for i in range(size))
                try:
                    response = await api.request(
                        DeviceRequest(method="PUT", path="/echo", payload=payload)
                    )
                except DeviceApiOperationFailure as error:
                    raise TestFailure(f"echo failed: {error}") from error
                if response.body != payload:
                    raise TestFailure("echo not intact", size, len(response.body))
                return PASS({"size": size})

        try:
            result = await Echoes(name="dut").run()
        finally:
            for server in servers:
                server.close()
                await server.wait_closed()

        assert result.state is State.PASS, result.failure
        leaves = {
            node.name: node.state
            for node in _walk(result.summary_tree)
            if node.name.startswith("echo-")
        }
        assert leaves == {f"echo-{size}b": State.PASS for size in sizes}


class TestSchemeIdentity:
    def test_http_rejects_coap_scheme(self) -> None:
        with pytest.raises(ValueError, match="http or https"):
            HttpInterface(base_url="coap://127.0.0.1:5683")

    def test_coap_rejects_http_scheme(self) -> None:
        with pytest.raises(ValueError, match="must be coap"):
            CoapInterface(base_url="http://127.0.0.1")


class TestValidationAndHalt:
    async def test_connect_is_idempotent_and_exposes_identity(self) -> None:
        backend = MockDeviceApi(base_url="http://user:secret@dut.example:8080")
        await backend.connect()
        await backend.connect()
        try:
            assert backend.scheme == "http"
            assert "secret" not in backend.handle_label
            assert backend.base_url.endswith("8080") or "dut.example" in backend.base_url
            assert DeviceApi(backend).scheme == "http"
            assert DeviceApi(backend).base_url == backend.base_url
        finally:
            await backend.disconnect()

    async def test_fail_next_connect_then_recovers(self) -> None:
        backend = MockDeviceApi()
        backend.fail_next_connect(ConnectionError("down"))
        with pytest.raises(ConnectionError, match="down"):
            await backend.connect()
        await backend.connect()
        assert backend.connect_count == 2
        await backend.disconnect()

    async def test_cancel_while_disconnected_is_a_no_op(self) -> None:
        backend = MockDeviceApi()
        await backend.cancel_in_flight()
        assert backend.connected is False

    async def test_request_re_entered_from_its_own_exchange_is_refused(self) -> None:
        """The one refusal left on a busy handle: re-entrancy, which would deadlock."""
        backend = MockDeviceApi()
        original = backend._exchange  # pyright: ignore[reportPrivateUsage]

        async def re_enter(
            method: str, path: str, payload: bytes | None, headers: object
        ) -> DeviceResponse:
            del method, path, payload, headers
            return await backend.request(DeviceRequest(method="GET", path="/inner"))

        backend._exchange = re_enter  # type: ignore[method-assign]
        await backend.connect()
        try:
            with pytest.raises(ValueError, match=r"one in-flight request per handle: .*re-entered"):
                await backend.request(DeviceRequest(method="GET", path="/outer"))
            # The refusal released the turn: the handle is still open and usable.
            backend._exchange = original  # type: ignore[method-assign]
            assert backend.connected is True
            assert backend.queued == 0
            response = await backend.request(DeviceRequest(method="GET", path="/after"))
            assert response.status == 200
        finally:
            await backend.disconnect()

    async def test_negative_retries_and_bad_method_are_refused(self) -> None:
        backend = MockDeviceApi()
        await backend.connect()
        try:
            with pytest.raises(ValueError, match=">= 0"):
                await backend.request(DeviceRequest(method="GET", path="/whoami", retries=-1))
            with pytest.raises(ValueError, match="one of"):
                await backend.request(DeviceRequest(method="PATCH", path="/whoami"))  # type: ignore[arg-type]
            with pytest.raises(TypeError, match="path must be a str"):
                await backend.request(DeviceRequest(method="GET", path=1))  # type: ignore[arg-type]
            with pytest.raises(ValueError, match="positive finite"):
                await backend.request(
                    DeviceRequest(method="GET", path="/whoami", timeout=float("inf"))
                )
        finally:
            await backend.disconnect()

    async def test_same_origin_redirect_is_followed(self) -> None:
        def router(
            method: str, path: str, body: bytes, headers: dict[str, str]
        ) -> tuple[int, bytes, dict[str, str]]:
            del method, body, headers
            if path == "/go":
                return 302, b"", {"Location": "/whoami"}
            return 200, b"landed", {}

        server = await asyncio.start_server(
            lambda reader, writer: _http_client(reader, writer, router),
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        backend = HttpInterface(base_url=f"http://127.0.0.1:{port}", follow_redirects=True)
        await backend.connect()
        try:
            response = await backend.request(DeviceRequest(method="GET", path="/go"))
            assert response.status == 200
            assert response.body == b"landed"
        finally:
            await backend.disconnect()
            server.close()
            await server.wait_closed()

    async def test_type_errors_on_timeout_method_and_origin(self) -> None:
        backend = MockDeviceApi()
        await backend.connect()
        try:
            with pytest.raises(TypeError, match="finite number"):
                await backend.request(DeviceRequest(method="GET", path="/whoami", timeout="1"))  # type: ignore[arg-type]
            with pytest.raises(TypeError, match="method must be a str"):
                await backend.request(DeviceRequest(method=1, path="/whoami"))  # type: ignore[arg-type]
        finally:
            await backend.disconnect()
        with pytest.raises(ValueError, match="scheme and host"):
            HttpInterface(base_url="not-a-url")

    async def test_long_coap_path_and_get_coap(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            _CoapServer, local_addr=("127.0.0.1", 0)
        )
        port = transport.get_extra_info("sockname")[1]
        backend = CoapInterface(base_url=f"coap://127.0.0.1:{port}")
        await backend.connect()
        try:
            response = await backend.request(DeviceRequest(method="GET", path="/abcdefghijklmn"))
            assert response.status == CoapCode.CONTENT
        finally:
            await backend.disconnect()
            transport.close()

    async def test_get_coap_opens_and_requests(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            _CoapServer, local_addr=("127.0.0.1", 0)
        )
        port = transport.get_extra_info("sockname")[1]
        utils = TestUtils()
        try:
            api = await utils.get_coap(base_url=f"coap://127.0.0.1:{port}")
            response = await api.request(DeviceRequest(method="PUT", path="/a/b", payload=b"xy"))
            assert response.status == CoapCode.CONTENT
        finally:
            await node_runtime(utils).cancel_opened()
            transport.close()


class TestCoapBlockwise:
    """RFC 7959: a body past one block is still one request, one response."""

    async def test_a_large_put_round_trips_whole(self) -> None:
        payload = bytes(range(256)) * 10  # 2560 bytes: three Block1 blocks at 1024
        async with _coap_loopback() as (api, server):
            response = await api.request(DeviceRequest(method="PUT", path="/echo", payload=payload))
            assert response.status == CoapCode.CHANGED
            assert response.body == payload
            block1s = [b1 for _p, b1, _b2, _n in server.seen if b1 is not None]
            assert block1s == [(0, True, 6), (1, True, 6), (2, False, 6)]
            block2s = [b2 for _p, _b1, b2, _n in server.seen if b2 is not None]
            assert block2s == [(1, False, 6), (2, False, 6)]
            # Continuations carry no body and no Block1: that is how the
            # reference stack keys the transfer.
            assert [n for _p, _b1, b2, n in server.seen if b2 is not None] == [0, 0]

    async def test_a_large_get_is_assembled_from_block2(self) -> None:
        async with _coap_loopback() as (api, server):
            response = await api.request(DeviceRequest(method="GET", path="/big"))
            assert response.status == CoapCode.CONTENT
            assert response.body == _BIG_BODY
            assert len(server.seen) == 3

    async def test_the_server_can_shrink_the_block_size(self) -> None:
        payload = b"x" * 1500
        async with _coap_loopback(szx=5) as (api, server):
            response = await api.request(DeviceRequest(method="PUT", path="/echo", payload=payload))
            assert response.status == CoapCode.CHANGED
            assert response.body == payload
            block1s = [b1 for _p, b1, _b2, _n in server.seen if b1 is not None]
            # 1024 at SZX 6, then the server's 512: block 2 of 512 is byte 1024.
            assert block1s == [(0, True, 6), (2, False, 5)]
            block2s = [b2 for _p, _b1, b2, _n in server.seen if b2 is not None]
            assert block2s == [(1, False, 5), (2, False, 5)]

    async def test_a_body_that_fits_one_block_never_uses_block1(self) -> None:
        payload = b"y" * 1024
        async with _coap_loopback() as (api, server):
            response = await api.request(DeviceRequest(method="PUT", path="/echo", payload=payload))
            assert response.body == payload
            assert server.seen == [("/echo", None, None, 1024)]

    async def test_a_refusal_mid_block1_is_the_response(self) -> None:
        async with _coap_loopback(refuse=True) as (api, server):
            response = await api.request(
                DeviceRequest(method="PUT", path="/echo", payload=b"z" * 2000)
            )
            assert response.status == CoapCode.from_wire(_COAP_TOO_LARGE)
            assert response.body == b""
            assert len(server.seen) == 1

    async def test_a_block2_out_of_sequence_is_an_operation_failure(self) -> None:
        async with _coap_loopback(misnumber=True) as (api, _server):
            with pytest.raises(DeviceApiOperationFailure, match="lost its sequence at byte 1024"):
                await api.request(DeviceRequest(method="GET", path="/big"))
