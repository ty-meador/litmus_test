"""MAC-to-IP resolution scoped to the node's current exec route.

Suite authors do not construct `ArpResolver`. `TestUtils.get_arp_resolver` hands
one back already bound to this node's `exec`, logger, and routing label. ARP
table reads go through `utils.exec`, so they land on the same host as the rest
of this node's commands — the test host, or a via hop.

The cache is keyed by that routing label alone. Two Gateway nodes that share a
host but differ in user therefore share one table; that is accepted. Devices
behind different gateways keep independent ARP state; devices sharing a hop
share one table read.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Final

from litmus.errors import ArpResolutionFailure, TestFailure
from litmus.executor import ExecResult
from litmus.verdict import PASS, Verdict

if TYPE_CHECKING:
    from litmus.utils import ScopedLogger, TestUtils

type ExecFn = Callable[[str], Awaitable[ExecResult]]

_HEX_DIGITS: Final[str] = "0123456789abcdef"
_ARP_COMMAND: Final[str] = "cat /proc/net/arp"
_MAX_AGE_SECONDS: Final[float] = 600.0
_LEADING_INT: Final[re.Pattern[str]] = re.compile(r"(\d+)")


def _normalize_mac(mac: str) -> str:
    hex_digits = "".join(character for character in mac.lower() if character in _HEX_DIGITS)
    if len(hex_digits) != 12:
        raise ValueError(f"MAC address must contain 12 hex digits, got {mac!r}")
    return ":".join(hex_digits[index : index + 2] for index in range(0, 12, 2))


def _leading_int(text: str) -> int | None:
    match = _LEADING_INT.match(text.strip())
    if match is None:
        return None
    return int(match.group(1))


@dataclass(frozen=True, slots=True)
class _ArpEntry:
    ip: str
    mac: str
    device: str


def _empty_table() -> list[_ArpEntry]:
    return []


@dataclass(slots=True)
class _ArpCache:
    table: list[_ArpEntry] = field(default_factory=_empty_table)
    last_update: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _cache_is_fresh(cache: _ArpCache, *, force: bool, now: float) -> bool:
    return not force and cache.last_update != 0.0 and (now - cache.last_update) < _MAX_AGE_SECONDS


def _parse_proc_arp(raw: str) -> list[_ArpEntry]:
    """Parse `/proc/net/arp` into rows. The header line is skipped."""
    entries: list[_ArpEntry] = []
    lines = raw.splitlines()
    for line in lines[1:]:
        columns = line.split()
        if len(columns) < 6:
            continue
        entries.append(_ArpEntry(ip=columns[0], mac=columns[3].lower(), device=columns[5]))
    return entries


class ArpResolver:
    """Resolve a MAC address to an IP through the node's current exec route.

    `resolve()` returns a plain dict suitable for spreading into `configure()`'s
    return value. `ping(utils)` is a gated reachability claim. `reacquire(utils)`
    forces a fresh table read after a reboot, flash, or DHCP churn and mutates
    `utils.ctx` in place.

    Raises:
        ArpResolutionFailure: The MAC is missing from the table, or `/proc/net/arp`
            is unreadable. This is not a `TestFailure`. Uncaught it is a developer
            bug — the suite did not model the failure mode. Catch it and re-raise
            as `TestFailure` to make "the device did not appear on the network" a
            verdict. Hardware versus infrastructure remains the triager's call.
        RuntimeError: `resolve()` was called on an instance that was not created
            through `utils.get_arp_resolver()`.
        ValueError: `mac` does not contain 12 hex digits, or `ping_count` < 1.
    """

    _caches: ClassVar[dict[str, _ArpCache]] = {}

    def __init__(
        self,
        *,
        mac: str,
        ping_count: int = 1,
        exec_fn: ExecFn | None = None,
        logger: ScopedLogger | None = None,
        cache_key: str | None = None,
    ) -> None:
        """Bind a MAC. Authors go through `utils.get_arp_resolver()`, not this.

        Args:
            mac: Device MAC in any usual form (colons, dashes, or bare hex).
            ping_count: ICMP packets per `ping()`. Must be at least 1.
            exec_fn: Route used to read the ARP table. Injected by
                `get_arp_resolver`.
            logger: Scoped logger for the resolve/reacquire lines.
            cache_key: Routing label identifying which host's table this is.
        """
        if ping_count < 1:
            raise ValueError("ping_count must be at least 1")
        self._mac = _normalize_mac(mac)
        self._ping_count = ping_count
        self._exec = exec_fn
        self._logger = logger
        self._cache_key = cache_key

    @classmethod
    def reset_caches(cls) -> None:
        """Drop every ARP table cache. Self-tests call this between cases."""
        cls._caches.clear()

    @property
    def mac(self) -> str:
        """The normalised colon-separated lowercase MAC this resolver owns."""
        return self._mac

    async def resolve(self) -> dict[str, str]:
        """Refresh a stale cache and return `{"ip": ...}` for this MAC.

        Zero-arg: uses the exec and cache key `get_arp_resolver` bound. Raises
        if this instance was constructed without that scope.
        """
        exec_fn, cache_key = self._require_scope()
        await self._refresh(exec_fn, cache_key)
        ip = self._ip_from_cache(cache_key)
        if ip is None:
            raise ArpResolutionFailure(
                f"{self._mac} not found in ARP table on {cache_key}",
                mac=self._mac,
                host=cache_key,
            )
        self._log("resolved %s -> %s", self._mac, ip)
        return {"ip": ip}

    async def reacquire(self, utils: TestUtils) -> str:
        """Force a fresh ARP read and write the new IP into `utils.ctx`.

        Call from a test method after anything that may have changed the IP.
        The cache key stays the resolver's — that is the host whose table holds
        this MAC — while the command runs through the child's `utils.exec`.
        """
        cache_key = self._cache_key if self._cache_key is not None else "local"
        await self._refresh(utils.exec, cache_key, force=True)
        ip = self._ip_from_cache(cache_key)
        if ip is None:
            raise ArpResolutionFailure(
                f"reacquire failed — {self._mac} not found in ARP table after "
                f"forced refresh on {cache_key}",
                mac=self._mac,
                host=cache_key,
            )
        utils.logger.info("reacquired %s -> %s", self._mac, ip)
        utils.ctx["ip"] = ip
        return ip

    async def ping(self, utils: TestUtils) -> Verdict:
        """Reachability claim against the IP currently in `utils.ctx`.

        Uses the child's exec and logger. Packet loss and a missing stats line
        are `TestFailure`. Duplicate replies (`received > transmitted`) still
        count as success — the host answered. A missing IP (resolve never ran)
        is `ArpResolutionFailure`.
        """
        ip = utils.ctx.get("ip")
        if not isinstance(ip, str) or not ip:
            raise ArpResolutionFailure(
                "No IP address resolved — was resolve() called?",
                mac=self._mac,
                host="unknown",
            )
        result = await utils.exec(f"ping -c {self._ping_count} {ip}")
        expected = f"{self._ping_count} packets transmitted"
        for line in result.stdout.splitlines():
            if expected not in line:
                continue
            parts = [part.strip() for part in line.split(",")]
            transmitted = _leading_int(parts[0]) if parts else None
            received = _leading_int(parts[1]) if len(parts) > 1 else None
            if transmitted is None or received is None:
                break
            if received == 0:
                raise TestFailure(
                    "no ping response received",
                    f"{transmitted} packets",
                    "0 packets",
                )
            if received < transmitted:
                lost_percent = round((1 - received / transmitted) * 100)
                raise TestFailure("packet loss detected", "0%", f"{lost_percent}%")
            utils.logger.info("ping ok — %s/%s packets received", received, transmitted)
            return PASS
        raise TestFailure(
            "ping output did not contain expected results line",
            expected,
            result.stdout,
        )

    def _require_scope(self) -> tuple[ExecFn, str]:
        if self._exec is None or self._cache_key is None:
            raise RuntimeError(
                "ArpResolver: use utils.get_arp_resolver() to create scoped instances"
            )
        return self._exec, self._cache_key

    def _log(self, message: str, *args: object) -> None:
        if self._logger is not None:
            self._logger.info(message, *args)

    @classmethod
    def _get_cache(cls, cache_key: str) -> _ArpCache:
        cache = cls._caches.get(cache_key)
        if cache is None:
            cache = _ArpCache()
            cls._caches[cache_key] = cache
        return cache

    def _ip_from_cache(self, cache_key: str) -> str | None:
        for entry in self._get_cache(cache_key).table:
            if entry.mac == self._mac:
                return entry.ip
        return None

    async def _refresh(self, exec_fn: ExecFn, cache_key: str, force: bool = False) -> None:
        cache = self._get_cache(cache_key)
        if _cache_is_fresh(cache, force=force, now=time.monotonic()):
            return
        async with cache.lock:
            if _cache_is_fresh(cache, force=force, now=time.monotonic()):
                return
            result = await exec_fn(_ARP_COMMAND)
            if result.exit_code != 0 or not result.stdout.strip():
                raise ArpResolutionFailure(
                    f"/proc/net/arp is not readable on {cache_key}",
                    mac=self._mac,
                    host=cache_key,
                )
            cache.table = _parse_proc_arp(result.stdout)
            cache.last_update = time.monotonic()
