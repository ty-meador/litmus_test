"""ArpResolver: MAC normalisation, table parse, cache, ping, reacquire."""

from __future__ import annotations

import asyncio
import gc
import logging
from typing import Any

import pytest

from litmus import PASS, ArpResolutionFailure, ArpResolver, TestFailure, TestUtils
from litmus.executor import ExecResult, LocalExec
from litmus.utils import ScopedLogger

_ARP_TABLE = """\
IP address       HW type     Flags       HW address            Mask     Device
10.0.0.50        0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0
10.0.0.1         0x1         0x2         00:11:22:33:44:55     *        eth0
"""
_PING_OK = (
    "PING 10.0.0.50 (10.0.0.50) 56(84) bytes of data.\n"
    "--- 10.0.0.50 ping statistics ---\n"
    "1 packets transmitted, 1 received, 0% packet loss, time 0ms\n"
)
_PING_LOSS = (
    "PING 10.0.0.50 (10.0.0.50) 56(84) bytes of data.\n"
    "--- 10.0.0.50 ping statistics ---\n"
    "1 packets transmitted, 0 received, 100% packet loss, time 0ms\n"
)
_PING_DUPES = (
    "PING 10.0.0.50 (10.0.0.50) 56(84) bytes of data.\n"
    "64 bytes from 10.0.0.50: icmp_seq=1 ttl=64 time=0.3 ms\n"
    "64 bytes from 10.0.0.50: icmp_seq=1 ttl=64 time=0.4 ms (DUP!)\n"
    "--- 10.0.0.50 ping statistics ---\n"
    "1 packets transmitted, 2 received, +1 duplicates, 0% packet loss, time 0ms\n"
)


def _ok(stdout: str) -> ExecResult:
    return ExecResult(stdout=stdout, stderr="", exit_code=0)


class _ScriptedExec:
    def __init__(self, results: dict[str, ExecResult]) -> None:
        self.results = results
        self.commands: list[str] = []

    async def __call__(self, command: str) -> ExecResult:
        self.commands.append(command)
        try:
            return self.results[command]
        except KeyError:
            return ExecResult(stdout="", stderr=f"unscripted: {command}", exit_code=1)


class TestPublicSurface:
    def test_resolver_and_failure_are_exported(self) -> None:
        import litmus

        assert litmus.ArpResolver is ArpResolver
        assert "ArpResolver" in litmus.__all__
        assert litmus.ArpResolutionFailure is ArpResolutionFailure
        assert "ArpResolutionFailure" in litmus.__all__


def _resolver(exec_fn: _ScriptedExec, *, mac: str = "AA-BB-CC-DD-EE-FF") -> ArpResolver:
    return ArpResolver(
        mac=mac,
        exec_fn=exec_fn,
        logger=ScopedLogger(logging.getLogger("litmus"), ()),
        cache_key="local",
    )


class TestMacNormalisation:
    def test_colons_dashes_and_bare_hex_normalise(self) -> None:
        assert ArpResolver(mac="AA-BB-CC-DD-EE-FF").mac == "aa:bb:cc:dd:ee:ff"
        assert ArpResolver(mac="aa:bb:cc:dd:ee:ff").mac == "aa:bb:cc:dd:ee:ff"
        assert ArpResolver(mac="AABBCCDDEEFF").mac == "aa:bb:cc:dd:ee:ff"

    def test_rejects_the_wrong_number_of_hex_digits(self) -> None:
        with pytest.raises(ValueError, match="12 hex digits"):
            ArpResolver(mac="aa:bb:cc")

    def test_rejects_a_non_positive_ping_count(self) -> None:
        with pytest.raises(ValueError, match="ping_count"):
            ArpResolver(mac="aa:bb:cc:dd:ee:ff", ping_count=0)


class TestResolve:
    async def test_reads_proc_net_arp_and_returns_the_ip(self) -> None:
        exec_fn = _ScriptedExec({"cat /proc/net/arp": _ok(_ARP_TABLE)})
        resolved = await _resolver(exec_fn).resolve()

        assert resolved == {"ip": "10.0.0.50"}
        assert exec_fn.commands == ["cat /proc/net/arp"]

    async def test_skips_truncated_proc_net_arp_rows(self) -> None:
        table = (
            "IP address       HW type     Flags       HW address"
            "            Mask     Device\n"
            "garbage\n"
            "10.0.0.50        0x1         0x2         aa:bb:cc:dd:ee:ff"
            "     *        eth0\n"
        )
        exec_fn = _ScriptedExec({"cat /proc/net/arp": _ok(table)})
        assert await _resolver(exec_fn).resolve() == {"ip": "10.0.0.50"}

    async def test_second_resolve_reuses_a_fresh_cache(self) -> None:
        exec_fn = _ScriptedExec({"cat /proc/net/arp": _ok(_ARP_TABLE)})
        resolver = _resolver(exec_fn)
        await resolver.resolve()
        await resolver.resolve()

        assert exec_fn.commands == ["cat /proc/net/arp"]

    async def test_missing_mac_is_arp_resolution_failure_not_test_failure(self) -> None:
        exec_fn = _ScriptedExec({"cat /proc/net/arp": _ok(_ARP_TABLE)})
        resolver = _resolver(exec_fn, mac="de:ad:be:ef:00:01")

        with pytest.raises(ArpResolutionFailure) as raised:
            await resolver.resolve()
        assert not isinstance(raised.value, TestFailure)
        assert raised.value.mac == "de:ad:be:ef:00:01"
        assert raised.value.host == "local"

    async def test_unreadable_table_is_arp_resolution_failure(self) -> None:
        exec_fn = _ScriptedExec(
            {"cat /proc/net/arp": ExecResult(stdout="", stderr="nope", exit_code=1)}
        )

        with pytest.raises(ArpResolutionFailure, match="not readable"):
            await _resolver(exec_fn).resolve()

    async def test_unscoped_resolve_is_a_runtime_error(self) -> None:
        with pytest.raises(RuntimeError, match="get_arp_resolver"):
            await ArpResolver(mac="aa:bb:cc:dd:ee:ff").resolve()

    async def test_distinct_cache_keys_do_not_share_a_table(self) -> None:
        local = _ScriptedExec({"cat /proc/net/arp": _ok(_ARP_TABLE)})
        via = _ScriptedExec(
            {
                "cat /proc/net/arp": _ok(
                    "IP address       HW type     Flags       HW address"
                    "            Mask     Device\n"
                    "192.168.1.8      0x1         0x2         aa:bb:cc:dd:ee:ff"
                    "     *        br0\n"
                )
            }
        )
        local_resolver = ArpResolver(
            mac="aa:bb:cc:dd:ee:ff",
            exec_fn=local,
            cache_key="local",
        )
        via_resolver = ArpResolver(
            mac="aa:bb:cc:dd:ee:ff",
            exec_fn=via,
            cache_key="via 10.0.0.1",
        )

        assert await local_resolver.resolve() == {"ip": "10.0.0.50"}
        assert await via_resolver.resolve() == {"ip": "192.168.1.8"}
        assert local.commands == ["cat /proc/net/arp"]
        assert via.commands == ["cat /proc/net/arp"]


class TestPingAndReacquire:
    async def test_ping_ok_returns_the_pass_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def ping_run(self: LocalExec, command: str) -> ExecResult:
            del self
            assert command == "ping -c 1 10.0.0.50"
            return _ok(_PING_OK)

        monkeypatch.setattr(LocalExec, "run", ping_run)
        resolver = _resolver(_ScriptedExec({"cat /proc/net/arp": _ok(_ARP_TABLE)}))
        utils = TestUtils(ctx={"ip": "10.0.0.50"})
        assert await resolver.ping(utils) is PASS

    async def test_ping_duplicate_replies_are_success(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def ping_run(self: LocalExec, _command: str) -> ExecResult:
            del self
            return _ok(_PING_DUPES)

        monkeypatch.setattr(LocalExec, "run", ping_run)
        resolver = _resolver(_ScriptedExec({"cat /proc/net/arp": _ok(_ARP_TABLE)}))
        utils = TestUtils(ctx={"ip": "10.0.0.50"})
        assert await resolver.ping(utils) is PASS

    async def test_ping_no_response_is_test_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def ping_run(self: LocalExec, _command: str) -> ExecResult:
            del self
            return _ok(_PING_LOSS)

        monkeypatch.setattr(LocalExec, "run", ping_run)
        resolver = _resolver(_ScriptedExec({"cat /proc/net/arp": _ok(_ARP_TABLE)}))
        utils = TestUtils(ctx={"ip": "10.0.0.50"})
        with pytest.raises(TestFailure, match="no ping response"):
            await resolver.ping(utils)

    async def test_ping_partial_loss_is_test_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def ping_run(self: LocalExec, _command: str) -> ExecResult:
            del self
            return _ok(
                "--- 10.0.0.50 ping statistics ---\n"
                "2 packets transmitted, 1 received, 50% packet loss, time 0ms\n"
            )

        monkeypatch.setattr(LocalExec, "run", ping_run)
        resolver = ArpResolver(
            mac="aa:bb:cc:dd:ee:ff",
            ping_count=2,
            exec_fn=_ScriptedExec({"cat /proc/net/arp": _ok(_ARP_TABLE)}),
            cache_key="local",
        )
        utils = TestUtils(ctx={"ip": "10.0.0.50"})
        with pytest.raises(TestFailure, match="packet loss"):
            await resolver.ping(utils)

    async def test_ping_without_a_results_line_is_test_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def ping_run(self: LocalExec, _command: str) -> ExecResult:
            del self
            return _ok("ping: socket: Operation not permitted\n")

        monkeypatch.setattr(LocalExec, "run", ping_run)
        resolver = _resolver(_ScriptedExec({"cat /proc/net/arp": _ok(_ARP_TABLE)}))
        utils = TestUtils(ctx={"ip": "10.0.0.50"})
        with pytest.raises(TestFailure, match="expected results line"):
            await resolver.ping(utils)

    async def test_ping_without_a_resolved_ip_is_arp_resolution_failure(self) -> None:
        resolver = _resolver(_ScriptedExec({"cat /proc/net/arp": _ok(_ARP_TABLE)}))
        with pytest.raises(ArpResolutionFailure, match="resolve"):
            await resolver.ping(TestUtils())

    async def test_reacquire_returns_the_new_ip_and_leaves_ctx_alone(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = _ok(_ARP_TABLE)
        second = _ok(
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "10.0.0.99        0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0\n"
        )
        exec_fn = _ScriptedExec({"cat /proc/net/arp": first})
        resolver = _resolver(exec_fn)
        await resolver.resolve()
        exec_fn.results["cat /proc/net/arp"] = second

        async def child_run(self: LocalExec, command: str) -> ExecResult:
            del self
            return await exec_fn(command)

        monkeypatch.setattr(LocalExec, "run", child_run)
        utils = TestUtils(ctx={"ip": "10.0.0.50"})
        ip = await resolver.reacquire(utils)

        assert ip == "10.0.0.99"
        # Ctx is read-only after configure (issue #118): the address that moved lives
        # in the resolver's table, and ping dials it from there.
        assert utils.ctx["ip"] == "10.0.0.50"
        assert exec_fn.commands == ["cat /proc/net/arp", "cat /proc/net/arp"]

        pinged: list[str] = []

        async def ping_run(self: LocalExec, command: str) -> ExecResult:
            del self
            pinged.append(command)
            return _ok(_PING_OK)

        monkeypatch.setattr(LocalExec, "run", ping_run)
        # A fresh node whose ctx still says 10.0.0.50: the table wins.
        later = TestUtils(ctx={"ip": "10.0.0.50"})
        assert await resolver.ping(later) is PASS
        assert pinged == ["ping -c 1 10.0.0.99"]

    async def test_reacquire_missing_mac_is_arp_resolution_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        exec_fn = _ScriptedExec({"cat /proc/net/arp": _ok(_ARP_TABLE)})
        resolver = _resolver(exec_fn)
        await resolver.resolve()
        exec_fn.results["cat /proc/net/arp"] = _ok(
            "IP address       HW type     Flags       HW address            Mask     Device\n"
        )

        async def child_run(self: LocalExec, command: str) -> ExecResult:
            del self
            return await exec_fn(command)

        monkeypatch.setattr(LocalExec, "run", child_run)
        utils = TestUtils(ctx={"ip": "10.0.0.50"})
        with pytest.raises(ArpResolutionFailure, match="reacquire failed"):
            await resolver.reacquire(utils)

    async def test_in_flight_refresh_is_shared(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def slow_exec(_command: str) -> ExecResult:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return _ok(_ARP_TABLE)

        first = ArpResolver(
            mac="aa:bb:cc:dd:ee:ff",
            exec_fn=slow_exec,
            cache_key="local",
        )
        second = ArpResolver(
            mac="aa:bb:cc:dd:ee:ff",
            exec_fn=slow_exec,
            cache_key="local",
        )
        first_task = asyncio.create_task(first.resolve())
        await started.wait()
        second_task = asyncio.create_task(second.resolve())
        await asyncio.sleep(0)
        release.set()
        assert await first_task == {"ip": "10.0.0.50"}
        assert await second_task == {"ip": "10.0.0.50"}
        assert calls == 1

    async def test_cancelled_refresh_does_not_strand_a_sibling(self) -> None:
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0

        async def gated_exec(_command: str) -> ExecResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                await release_first.wait()
            return _ok(_ARP_TABLE)

        first = ArpResolver(
            mac="aa:bb:cc:dd:ee:ff",
            exec_fn=gated_exec,
            cache_key="shared",
        )
        second = ArpResolver(
            mac="aa:bb:cc:dd:ee:ff",
            exec_fn=gated_exec,
            cache_key="shared",
        )
        first_task = asyncio.create_task(first.resolve())
        await first_entered.wait()
        second_task = asyncio.create_task(second.resolve())
        await asyncio.sleep(0)
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        release_first.set()
        assert await asyncio.wait_for(second_task, timeout=1) == {"ip": "10.0.0.50"}
        assert calls == 2

    async def test_failed_table_read_does_not_leak_an_unretrieved_future(self) -> None:
        loop = asyncio.get_running_loop()
        leaked: list[dict[str, Any]] = []

        def handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
            leaked.append(context)

        previous = loop.get_exception_handler()
        loop.set_exception_handler(handler)
        try:
            exec_fn = _ScriptedExec(
                {"cat /proc/net/arp": ExecResult(stdout="", stderr="nope", exit_code=1)}
            )
            with pytest.raises(ArpResolutionFailure, match="not readable"):
                await _resolver(exec_fn).resolve()
            gc.collect()
            await asyncio.sleep(0)
            assert leaked == []
        finally:
            loop.set_exception_handler(previous)
