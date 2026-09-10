"""Device API (CoAP/HTTP) — a resource-request contract, not an exec subclass.

Suite authors do not construct a backend. ``TestUtils.get_http`` and
``TestUtils.get_coap`` open a session and hand back ``DeviceApi``. Self-tests
import this module directly. Not every name here is public: the ABC and the
three backends stay off ``litmus.__all__``, the way ``RemoteInterface`` does.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import ssl
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, cast, final
from urllib.parse import urljoin, urlsplit

from litmus.errors import DeviceApiOperationFailure

LOGGER = logging.getLogger(__name__)

type DeviceMethod = Literal["GET", "PUT", "POST", "DELETE"]

_METHODS: Final[frozenset[str]] = frozenset({"GET", "PUT", "POST", "DELETE"})
_NON_IDEMPOTENT: Final[frozenset[str]] = frozenset({"POST"})
_HTTP_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
_COAP_SCHEMES: Final[frozenset[str]] = frozenset({"coap"})
_DEFAULT_PORTS: Final[dict[str, int]] = {
    "http": 80,
    "https": 443,
    "coap": 5683,
}

_COAP_CODES: Final[dict[str, int]] = {"GET": 1, "POST": 2, "PUT": 3, "DELETE": 4}
_COAP_URI_PATH = 11
_COAP_CON = 0
_COAP_VER = 1


def _require_int(name: str, value: object) -> int:
    """Reject bools (they subclass int) and every non-int."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, not {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def _require_finite_seconds(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a finite number of seconds, not {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive finite number of seconds")
    return number


def _require_method(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"method must be a str, not {type(value).__name__}")
    method = value.upper()
    if method not in _METHODS:
        raise ValueError(f"method must be one of {sorted(_METHODS)}, not {value!r}")
    return method


def _require_path(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"path must be a str, not {type(value).__name__}")
    if "://" in value or not value.startswith("/"):
        raise ValueError("path must be relative to the declared base URL and start with '/'")
    return value


def _require_payload(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, bytes):
        raise TypeError("payload must be bytes; encoding is the author's")
    return value


def _freeze_headers(headers: Mapping[str, str] | None) -> Mapping[str, str]:
    if headers is None:
        return MappingProxyType({})
    frozen: dict[str, str] = {}
    for key, item in headers.items():
        if type(key) is not str or type(item) is not str:  # pragma: no cover
            raise TypeError("header names and values must be str")
        frozen[key] = item
    return MappingProxyType(frozen)


def _evidence_header_names(names: object) -> frozenset[str]:
    """Lowercased names whose values the author opted into the attempt log."""
    if isinstance(names, str) or not isinstance(names, Iterable):
        raise TypeError("evidence_headers must be a set of header names, not a single str")
    raw_items = list(cast(Iterable[object], names))
    frozen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, str):
            raise TypeError("evidence_headers names must be str")
        frozen.add(item.lower())
    return frozenset(frozen)


def _redact_headers(headers: Mapping[str, str], *, reveal: frozenset[str]) -> dict[str, str]:
    """Keep header names; withhold values unless the author opted that name in.

    A secret-name allowlist cannot deliver "credentials never reach evidence":
    ``X-Device-Token``, ``Api-Key``, and the next DUT's favorite all miss it.
    Names still tell the Floor Technician that Authorization was sent.
    """
    return {key: value if key.lower() in reveal else "<redacted>" for key, value in headers.items()}


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    auth = "user:<redacted>@"
    return parts._replace(netloc=f"{auth}{host}").geturl()


@final
@dataclass(frozen=True, slots=True)
class DeviceRequest:
    """One resource operation: method, path, optional payload, optional headers.

    Path is relative to the handle's base URL. Payload is the bytes actually
    sent — a dict is not a JSON body. ``retries`` counts extra application-level
    attempts after the first; the default is zero. Attempt logs withhold every
    header value unless the name is in ``evidence_headers``.
    """

    method: DeviceMethod
    path: str
    payload: bytes | None = None
    headers: Mapping[str, str] = MappingProxyType({})
    retries: int = 0
    retry_delay: float = 0.05
    retry_non_idempotent: bool = False
    timeout: float | None = None
    evidence_headers: frozenset[str] = frozenset()


@final
@dataclass(frozen=True, slots=True)
class DeviceResponse:
    """The bytes and status a transfer actually returned.

    HTTP 4xx/5xx and CoAP 4.xx/5.xx are fields here, not exceptions. ``headers``
    carries HTTP headers or CoAP options as strings. ``body`` is the payload
    bytes; JSON parsing is the author's.
    """

    status: int
    body: bytes
    headers: Mapping[str, str] = MappingProxyType({})


class DeviceApiInterface(ABC):
    """Lifecycle and request halt for a CoAP or HTTP session.

    ``connect`` / ``disconnect`` own the connected flag. Subclasses implement
    ``_connect``, ``_disconnect``, and ``_exchange``. ``request`` owns retries,
    in-flight admission, and the closed-session cut.
    """

    def __init__(
        self,
        *,
        scheme: str,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        verify: bool = True,
        follow_redirects: bool = False,
        request_timeout: float = 20.0,
        logger: logging.Logger | logging.LoggerAdapter[logging.Logger] | None = None,
    ) -> None:
        self._scheme = scheme
        self._base_url = base_url.rstrip("/")
        self._headers = _freeze_headers(headers)
        self._verify = verify
        self._follow_redirects = follow_redirects
        self._request_timeout = _require_finite_seconds("request_timeout", request_timeout)
        self._logger = LOGGER if logger is None else logger
        self._connected = False
        self._in_flight = False
        self._close_confirmed = False
        self._attempts: list[str] = []

    @property
    def scheme(self) -> str:
        """The scheme this handle was bound to. Immutable for the session."""
        return self._scheme

    @property
    def base_url(self) -> str:
        """Declared origin, credentials stripped from the logged form."""
        return self._base_url

    @property
    def handle_label(self) -> str:
        """What the timeline names this session."""
        return f"{self._scheme} {_redact_url(self._base_url)}"

    @property
    def connected(self) -> bool:
        """True if the session is open and ``request`` is allowed to proceed."""
        return self._connected

    @property
    def attempts(self) -> tuple[str, ...]:
        """Application-level attempts this handle has issued, in order."""
        return tuple(self._attempts)

    @property
    def close_confirmed(self) -> bool:
        """True if the last close/cancel observed a dead session."""
        return self._close_confirmed

    @final
    async def connect(self) -> None:
        """Open the session. Idempotent if already connected."""
        if self._connected:
            return
        await self._connect()
        self._connected = True
        self._close_confirmed = False

    @final
    async def disconnect(self) -> None:
        """Close the session. Safe when it was never opened."""
        try:
            await self._disconnect()
            self._close_confirmed = True
        except Exception:  # pragma: no cover - close is best-effort
            self._close_confirmed = False
            raise
        finally:
            self._connected = False

    @final
    async def cancel_in_flight(self) -> None:
        """Kill the in-flight request and the retry loop. Idempotent."""
        if not self._connected:
            return
        try:
            await self._cancel_in_flight()
            self._close_confirmed = True
        except Exception:  # pragma: no cover - close is best-effort
            self._close_confirmed = False
            raise
        finally:
            self._connected = False
            self._in_flight = False

    @final
    async def request(self, req: DeviceRequest) -> DeviceResponse:
        """Perform one application-level request, with declared retries.

        A well-formed response of any status is data and ends the loop. An
        operational failure retries only when ``req.retries`` allows it.
        """
        if not self._connected:
            raise ConnectionError("device API session is not connected")
        method = _require_method(req.method)
        path = _require_path(req.path)
        payload = _require_payload(req.payload)
        retries = _require_int("retries", req.retries)
        headers = _freeze_headers(dict(self._headers) | dict(req.headers))
        timeout = (
            self._request_timeout
            if req.timeout is None
            else _require_finite_seconds("timeout", req.timeout)
        )
        if retries and method in _NON_IDEMPOTENT and not req.retry_non_idempotent:
            # Author-contract, same class as the in-flight refusal below: the
            # types are fine, the usage is not. ValueError for both, not TypeError.
            raise ValueError(f"{method} refuses application retry unless retry_non_idempotent=True")
        delay = 0.0 if retries == 0 else _require_finite_seconds("retry_delay", req.retry_delay)
        reveal = _evidence_header_names(req.evidence_headers)
        last_error: DeviceApiOperationFailure | None = None
        for attempt in range(retries + 1):
            if not self._connected:  # pragma: no cover - cancel races the retry loop
                break
            if self._in_flight:
                raise ValueError("one in-flight request per handle; wait or cancel")
            if attempt > 0:
                self._logger.warning(
                    "device-api retry %s/%s %s %s — waiting %ss",
                    attempt,
                    retries,
                    method,
                    path,
                    delay,
                )
                await asyncio.sleep(delay)
                if not self._connected:  # pragma: no cover - cancel during delay
                    break
            self._in_flight = True
            self._attempts.append(f"{attempt}:{method} {path}")
            self._logger.info(
                "device-api attempt %s %s %s headers=%s payload_len=%s",
                attempt,
                method,
                path,
                _redact_headers(headers, reveal=reveal),
                0 if payload is None else len(payload),
            )
            try:
                response = await asyncio.wait_for(
                    self._exchange(method, path, payload, headers),
                    timeout=timeout,
                )
            except TimeoutError:
                last_error = DeviceApiOperationFailure(
                    f"device-api {method} {path} exceeded {timeout:g}s ceiling",
                    handle=self.handle_label,
                    operation="request",
                    method=method,
                    path=path,
                    ceiling=timeout,
                )
            except DeviceApiOperationFailure as error:
                last_error = error
            except (ConnectionError, OSError) as error:
                last_error = DeviceApiOperationFailure(
                    f"device-api {method} {path} failed: {error}",
                    handle=self.handle_label,
                    operation="request",
                    method=method,
                    path=path,
                    ceiling=timeout,
                )
            else:
                self._logger.info(
                    "device-api responded %s %s status=%s body_len=%s",
                    method,
                    path,
                    response.status,
                    len(response.body),
                )
                return response
            finally:
                self._in_flight = False
        if last_error is not None:
            raise last_error
        raise DeviceApiOperationFailure(  # pragma: no cover - cancel with no attempt
            f"device-api {method} {path} cancelled before a response",
            handle=self.handle_label,
            operation="request",
            method=method,
            path=path,
            ceiling=timeout,
        )

    @abstractmethod
    async def _connect(self) -> None:
        """Open the protocol-specific session. Called by ``connect``."""

    @abstractmethod
    async def _disconnect(self) -> None:
        """Close the protocol-specific session. Safe when never opened."""

    @abstractmethod
    async def _exchange(
        self,
        method: str,
        path: str,
        payload: bytes | None,
        headers: Mapping[str, str],
    ) -> DeviceResponse:
        """One on-the-wire attempt. Called by ``request`` while in-flight."""

    async def _cancel_in_flight(self) -> None:
        """Protocol-specific hard close. Default is ``_disconnect``."""
        await self._disconnect()


@final
class DeviceApi:
    """Author-facing handle from ``get_http`` / ``get_coap``.

    Suite authors do not construct this. ``request`` returns a typed response;
    HTTP 4xx is data, not an exception. The framework owns connect, retry, and
    the closed-session halt.
    """

    __slots__ = ("_backend",)

    def __init__(self, backend: DeviceApiInterface) -> None:
        """Wrap an already-connected backend. Framework and self-tests only."""
        self._backend = backend

    async def request(self, req: DeviceRequest) -> DeviceResponse:
        """Perform ``req`` on this session. See :class:`DeviceRequest`."""
        return await self._backend.request(req)

    @property
    def scheme(self) -> str:
        """Scheme this handle was bound to."""
        return self._backend.scheme

    @property
    def base_url(self) -> str:
        """Declared origin."""
        return self._backend.base_url


@final
class MockDeviceApi(DeviceApiInterface):
    """In-process Device API so contract tests never need a server.

    ``script`` registers a response for ``METHOD path``. Unscripted paths
    return status 200 and an empty body. ``park`` holds that path until
    ``cancel_in_flight``.
    """

    def __init__(
        self,
        *,
        scheme: str = "http",
        base_url: str = "http://mock.invalid",
        headers: Mapping[str, str] | None = None,
        verify: bool = True,
        follow_redirects: bool = False,
        request_timeout: float = 20.0,
        logger: logging.Logger | logging.LoggerAdapter[logging.Logger] | None = None,
    ) -> None:
        super().__init__(
            scheme=scheme,
            base_url=base_url,
            headers=headers,
            verify=verify,
            follow_redirects=follow_redirects,
            request_timeout=request_timeout,
            logger=logger,
        )
        self._scripts: dict[str, DeviceResponse] = {}
        self._parked: set[str] = set()
        self._blocked: set[asyncio.Future[DeviceResponse]] = set()
        self._connect_error: BaseException | None = None
        self._connect_count = 0
        self._cancel_count = 0

    def script(self, method: str, path: str, response: DeviceResponse) -> None:
        """Make ``request(DeviceRequest(method, path))`` return ``response``."""
        self._scripts[f"{method.upper()} {path}"] = response

    def park(self, method: str, path: str) -> None:
        """Hold that request until the session is cancelled or closed."""
        self._parked.add(f"{method.upper()} {path}")

    def fail_next_connect(self, error: BaseException) -> None:
        """Raise ``error`` from the next ``connect``, then recover."""
        self._connect_error = error

    @property
    def connect_count(self) -> int:
        """How many times ``_connect`` ran, including failed attempts."""
        return self._connect_count

    @property
    def cancel_count(self) -> int:
        """How many times ``cancel_in_flight`` actually tore the session down."""
        return self._cancel_count

    @property
    def has_in_flight(self) -> bool:
        """True if a parked request is waiting on this handle."""
        return bool(self._blocked)

    async def _connect(self) -> None:
        self._connect_count += 1
        if self._connect_error is not None:
            error = self._connect_error
            self._connect_error = None
            raise error

    async def _disconnect(self) -> None:
        self._fail_blocked("device API session closed")

    async def _cancel_in_flight(self) -> None:
        self._cancel_count += 1
        await self._disconnect()

    async def _exchange(
        self,
        method: str,
        path: str,
        payload: bytes | None,
        headers: Mapping[str, str],
    ) -> DeviceResponse:
        del payload, headers
        key = f"{method} {path}"
        if key in self._parked:
            blocked: asyncio.Future[DeviceResponse] = asyncio.get_running_loop().create_future()
            self._blocked.add(blocked)
            try:
                return await blocked
            finally:
                self._blocked.discard(blocked)
        return self._scripts.get(key, DeviceResponse(status=200, body=b""))

    def _fail_blocked(self, message: str) -> None:
        for blocked in tuple(self._blocked):
            if not blocked.done():
                blocked.set_exception(ConnectionError(message))


def _split_origin(url: str) -> tuple[str, str, int, str]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname
    if not scheme or not host:
        raise ValueError(f"base_url must include a scheme and host, not {url!r}")
    port = parts.port if parts.port is not None else _DEFAULT_PORTS.get(scheme, 0)
    if port <= 0:  # pragma: no cover - unknown scheme already rejected
        raise ValueError(f"base_url has no default port for scheme {scheme!r}")
    return scheme, host, port, parts.path or ""


@final
class HttpInterface(DeviceApiInterface):
    """HTTP/1.1 backend. One TCP (or TLS) session, cancelled at the socket."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        verify: bool = True,
        follow_redirects: bool = False,
        request_timeout: float = 20.0,
        logger: logging.Logger | logging.LoggerAdapter[logging.Logger] | None = None,
    ) -> None:
        scheme, host, port, prefix = _split_origin(base_url)
        if scheme not in _HTTP_SCHEMES:
            raise ValueError(f"HttpInterface scheme must be http or https, not {scheme!r}")
        super().__init__(
            scheme=scheme,
            base_url=base_url,
            headers=headers,
            verify=verify,
            follow_redirects=follow_redirects,
            request_timeout=request_timeout,
            logger=logger,
        )
        self._host = host
        self._port = port
        self._prefix = prefix.rstrip("/")
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def _connect(self) -> None:
        ssl_ctx: ssl.SSLContext | None = None
        if self._scheme == "https":  # pragma: no cover - loopback contract tests speak http
            ssl_ctx = ssl.create_default_context()
            if not self._verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                self._logger.warning(
                    "device-api: TLS verification skipped for %s (lab-only)",
                    self.handle_label,
                )
        self._reader, self._writer = await asyncio.open_connection(
            self._host,
            self._port,
            ssl=ssl_ctx,
            server_hostname=self._host if ssl_ctx is not None else None,
        )

    async def _disconnect(self) -> None:
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def _exchange(
        self,
        method: str,
        path: str,
        payload: bytes | None,
        headers: Mapping[str, str],
    ) -> DeviceResponse:
        hops = 0
        current_path = path
        while True:
            response = await self._round_trip(method, current_path, payload, headers)
            location = _header(response.headers, "location")
            if (
                self._follow_redirects
                and location is not None
                and 300 <= response.status < 400
                and hops < 5
            ):
                hops += 1
                current_path = self._redirect_path(location)
                if current_path is None:
                    return response
                continue
            return response

    def _redirect_path(self, location: str) -> str | None:
        """Same-origin path to follow, or None when the Location is another host."""
        if location.startswith("/"):
            return location
        joined = urljoin(self._base_url + "/", location)
        scheme, host, port, path = _split_origin(joined)
        if (scheme, host, port) != (self._scheme, self._host, self._port):
            return None
        return path or "/"

    async def _round_trip(
        self,
        method: str,
        path: str,
        payload: bytes | None,
        headers: Mapping[str, str],
    ) -> DeviceResponse:
        reader = self._reader
        writer = self._writer
        if reader is None or writer is None:  # pragma: no cover
            raise ConnectionError("device API session is not connected")
        target = f"{self._prefix}{path}" if self._prefix else path
        body = payload or b""
        lines = [
            f"{method} {target} HTTP/1.1",
            f"Host: {self._host}",
            "Connection: keep-alive",
            f"Content-Length: {len(body)}",
        ]
        for key, value in headers.items():
            if key.lower() in {"host", "content-length", "connection"}:
                continue
            lines.append(f"{key}: {value}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body
        writer.write(request)
        await writer.drain()
        status_line = await reader.readline()
        if not status_line:  # pragma: no cover - peer hangup
            raise ConnectionError("HTTP peer closed the session")
        parts = status_line.decode("iso-8859-1").split()
        if len(parts) < 2 or not parts[1].isdigit():  # pragma: no cover
            raise DeviceApiOperationFailure(
                f"HTTP peer sent an unreadable status line: {status_line!r}",
                handle=self.handle_label,
                operation="request",
                method=method,
                path=path,
            )
        status = int(parts[1])
        response_headers: dict[str, str] = {}
        while True:
            header_line = await reader.readline()
            if header_line in {b"\r\n", b"\n", b""}:
                break
            raw = header_line.decode("iso-8859-1")
            name, _, value = raw.partition(":")
            response_headers[name.strip()] = value.strip()
        length_text = _header(response_headers, "content-length")
        if length_text is not None:
            body_out = await reader.readexactly(int(length_text))
        else:
            body_out = b""
        return DeviceResponse(
            status=status,
            body=body_out,
            headers=MappingProxyType(response_headers),
        )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _coap_encode(
    method: str, path: str, payload: bytes | None, message_id: int, token: bytes
) -> bytes:
    code = _COAP_CODES[method]
    header = bytes(
        [
            (_COAP_VER << 6) | (_COAP_CON << 4) | len(token),
            code,
            (message_id >> 8) & 0xFF,
            message_id & 0xFF,
        ]
    )
    options = bytearray()
    last = 0
    segments = [segment for segment in path.split("/") if segment]
    for segment in segments:
        raw = segment.encode("utf-8")
        delta = _COAP_URI_PATH - last
        last = _COAP_URI_PATH
        delta_nibble, delta_ext = _coap_nibble(delta)
        length_nibble, length_ext = _coap_nibble(len(raw))
        options.append((delta_nibble << 4) | length_nibble)
        options.extend(delta_ext)
        options.extend(length_ext)
        options.extend(raw)
    if payload:
        return header + token + bytes(options) + b"\xff" + payload
    return header + token + bytes(options)


def _coap_nibble(value: int) -> tuple[int, bytes]:
    if value < 13:
        return value, b""
    if value < 269:
        return 13, bytes([value - 13])
    raise ValueError("CoAP option value is too large for this backend")  # pragma: no cover


def _coap_decode(datagram: bytes) -> tuple[int, int, bytes, bytes]:
    if len(datagram) < 4:  # pragma: no cover - wire garbage
        raise ValueError("CoAP datagram is truncated")
    tkl = datagram[0] & 0x0F
    code = datagram[1]
    token = datagram[4 : 4 + tkl]
    rest = datagram[4 + tkl :]
    payload = b""
    while rest:
        if rest[0] == 0xFF:
            payload = rest[1:]
            break
        nibble_len = rest[0] & 0x0F
        rest = rest[1:]
        if nibble_len == 13:  # pragma: no cover - long options
            nibble_len = 13 + rest[0]
            rest = rest[1:]
        rest = rest[nibble_len:]
    return code, tkl, token, payload


@final
class CoapInterface(DeviceApiInterface):
    """CoAP CON/ACK over UDP. Cancel closes the datagram endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        verify: bool = True,
        follow_redirects: bool = False,
        request_timeout: float = 20.0,
        logger: logging.Logger | logging.LoggerAdapter[logging.Logger] | None = None,
    ) -> None:
        scheme, host, port, _prefix = _split_origin(base_url)
        if scheme not in _COAP_SCHEMES:
            raise ValueError(
                f"CoapInterface scheme must be coap, not {scheme!r}; "
                "coaps/coaps+tcp are a later backend"
            )
        super().__init__(
            scheme=scheme,
            base_url=base_url,
            headers=headers,
            verify=verify,
            follow_redirects=follow_redirects,
            request_timeout=request_timeout,
            logger=logger,
        )
        self._host = host
        self._port = port
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _CoapProtocol | None = None
        self._message_id = 0

    async def _connect(self) -> None:
        loop = asyncio.get_running_loop()
        protocol = _CoapProtocol()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            remote_addr=(self._host, self._port),
        )
        self._transport = transport
        self._protocol = protocol

    async def _disconnect(self) -> None:
        protocol = self._protocol
        transport = self._transport
        self._protocol = None
        self._transport = None
        if protocol is not None:
            protocol.fail("device API session closed")
        if transport is not None:
            transport.close()

    async def _exchange(
        self,
        method: str,
        path: str,
        payload: bytes | None,
        headers: Mapping[str, str],
    ) -> DeviceResponse:
        del headers
        transport = self._transport
        protocol = self._protocol
        if transport is None or protocol is None:  # pragma: no cover
            raise ConnectionError("device API session is not connected")
        self._message_id = (self._message_id + 1) & 0xFFFF
        token = bytes([self._message_id & 0xFF])
        datagram = _coap_encode(method, path, payload, self._message_id, token)
        waiter = protocol.expect(token)
        transport.sendto(datagram)
        code, body = await waiter
        return DeviceResponse(status=code, body=body, headers=MappingProxyType({}))


class _CoapProtocol(asyncio.DatagramProtocol):
    """Collect one CON reply at a time for ``CoapInterface``."""

    def __init__(self) -> None:
        self._waiters: dict[bytes, asyncio.Future[tuple[int, bytes]]] = {}

    def expect(self, token: bytes) -> asyncio.Future[tuple[int, bytes]]:
        waiter: asyncio.Future[tuple[int, bytes]] = asyncio.get_running_loop().create_future()
        self._waiters[token] = waiter
        return waiter

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        del addr
        try:
            code, _tkl, token, payload = _coap_decode(data)
        except ValueError:  # pragma: no cover - truncated peer
            return
        waiter = self._waiters.pop(token, None)
        if waiter is not None and not waiter.done():
            waiter.set_result((code, payload))

    def fail(self, message: str) -> None:
        for waiter in tuple(self._waiters.values()):
            if not waiter.done():  # pragma: no cover - already settled
                waiter.set_exception(ConnectionError(message))
        self._waiters.clear()

    def connection_lost(self, exc: Exception | None) -> None:
        del exc
        self.fail("device API session closed")
