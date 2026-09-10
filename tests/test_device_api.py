"""Device API contract: mock, HTTP, and CoAP on the same bar (issue #119)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Protocol

import pytest

from litmus import (
    PASS,
    DeviceApi,
    DeviceApiOperationFailure,
    DeviceRequest,
    DeviceResponse,
    LitmusTest,
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


class _CoapServer(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.transport is None:
            return
        if len(data) < 4:
            return
        tkl = data[0] & 0x0F
        token = data[4 : 4 + tkl]
        path = _coap_path(data)
        status = 132 if path == "/missing" else 69
        body = b"" if path == "/missing" else path.encode()
        tkl_nibble = tkl
        header = bytes(
            [
                (1 << 6) | (2 << 4) | tkl_nibble,
                status,
                data[2],
                data[3],
            ]
        )
        reply = header + token
        if body:
            reply += b"\xff" + body
        self.transport.sendto(reply, addr)


def _coap_path(datagram: bytes) -> str:
    """Best-effort Uri-Path join for the loopback server."""
    tkl = datagram[0] & 0x0F
    rest = datagram[4 + tkl :]
    segments: list[str] = []
    last = 0
    while rest and rest[0] != 0xFF:
        delta = (rest[0] >> 4) & 0x0F
        length = rest[0] & 0x0F
        rest = rest[1:]
        if delta == 13:
            delta = 13 + rest[0]
            rest = rest[1:]
        if length == 13:
            length = 13 + rest[0]
            rest = rest[1:]
        option = last + delta
        last = option
        value = rest[:length]
        rest = rest[length:]
        if option == 11:
            segments.append(value.decode("utf-8"))
    return "/" + "/".join(segments) if segments else "/"


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


class TestSharedContract:
    async def test_status_200_is_data(self, api: DeviceApiInterface) -> None:
        response = await api.request(DeviceRequest(method="GET", path="/whoami"))

        assert response.status in {200, 69}
        assert response.body in {b"/whoami", b"whoami"} or (
            isinstance(api, MockDeviceApi) and response.body == b"/whoami"
        )

    async def test_client_error_status_is_data_not_an_exception(
        self, api: DeviceApiInterface
    ) -> None:
        response = await api.request(DeviceRequest(method="GET", path="/missing"))

        assert response.status in {404, 132}

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

    async def test_second_request_while_parked_is_refused(self) -> None:
        backend = MockDeviceApi()
        backend.park("GET", "/slow")
        await backend.connect()
        first = asyncio.create_task(backend.request(DeviceRequest(method="GET", path="/slow")))
        deadline = asyncio.get_running_loop().time() + 2
        while not backend.has_in_flight:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0)
        with pytest.raises(ValueError, match="one in-flight"):
            await backend.request(DeviceRequest(method="GET", path="/whoami"))
        await backend.cancel_in_flight()
        with contextlib.suppress(DeviceApiOperationFailure, ConnectionError):
            await first

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
            assert response.status == 69
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
            assert response.status == 69
        finally:
            await node_runtime(utils).cancel_opened()
            transport.close()
