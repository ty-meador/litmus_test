"""A Pi-hole as a gateway: SSH hop on the parent, REST API and DNS claims behind it.

The real-world shape: the appliance is reached over SSH, its management API over HTTP,
and the thing it is *for* (answering DNS) is judged from inside it with ``dig``. The
parent owns the hop; the child owns the API session and a blocklist entry it adds in
``configure()`` and removes in ``cleanup()``. The child's ``utils.exec`` is routed
through the parent's hop by being its child; it holds no connection code.

Environment: ``PIHOLE_HOST`` / ``PIHOLE_USER`` / optional ``PIHOLE_PASSWORD`` /
``PIHOLE_SSH_PORT`` for the hop; ``PIHOLE_API_URL`` / ``PIHOLE_API_PASSWORD`` for FTL's
API. The bench container (``make bench-pihole``) sets all six in the Makefile.

Run it: ``make bench-suite-pihole``.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any

from litmus import (
    PASS,
    DeviceApi,
    DeviceApiOperationFailure,
    DeviceRequest,
    LitmusTest,
    Phase,
    TestFailure,
    TestUtils,
    Verdict,
    evidence,
)

_BLOCKED_ANSWERS = frozenset({"0.0.0.0", "::"})
_QUERY_TIME = re.compile(r";; Query time: (\d+) msec")


class PiHole(LitmusTest):
    """The appliance: an SSH hop every claim and every composed child runs through."""

    def __init__(
        self,
        *,
        name: str = "pihole",
        behind: LitmusTest | None = None,
        skip_host_verification: bool = True,
    ) -> None:
        """Capture what sits behind the hop; the hop itself is resolved in ``configure``.

        Args:
            name: Node name used by logs, artifacts, and failures.
            behind: The suite that runs through this hop. ``None`` composes a
                `BlocklistRoundTrip`, which is what ``litmus run`` gets.
            skip_host_verification: Lab-only ``known_hosts=None``; the bench container
                sits on a loopback port no ``known_hosts`` entry covers.
        """
        super().__init__(name=name)
        self._behind = behind if behind is not None else BlocklistRoundTrip(name="blocklist")
        self._skip_host_verification = skip_host_verification
        self.use_survey(
            self._ftl_tail, phases=[phase for phase in Phase if phase >= Phase.POST_CONFIGURE]
        )

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Open the hop and record the Pi-hole build as provenance."""
        host = utils.env.require("PIHOLE_HOST")
        try:
            await utils.use_shell(
                host=host,
                username=utils.env.require("PIHOLE_USER"),
                port=utils.env.port("PIHOLE_SSH_PORT"),
                password=utils.env.text("PIHOLE_PASSWORD"),
                skip_host_verification=self._skip_host_verification,
            )
        except Exception as error:
            raise TestFailure(f"SSH connect to {host} failed: {error}") from error
        versions = await utils.exec("pihole -v")
        return {"versions": versions.stdout.strip().splitlines()}

    def build_suite(self) -> LitmusTest:
        """FTL must be listening before anything behind the hop is worth asking."""
        return self.sequence(
            self.name,
            [
                self.test("ftl-listening", self._ftl_listening, fail_after=30.0),
                self._behind,
            ],
        )

    @evidence("status_lines")
    async def _ftl_listening(self, utils: TestUtils) -> Verdict:
        """``pihole status`` says FTL is listening and blocking is on."""
        result = await utils.exec("pihole status")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        listening = any("listening" in line for line in lines)
        enabled = any("blocking is enabled" in line.lower() for line in lines)
        if result.exit_code == 0 and listening and enabled:
            return PASS({"status_lines": lines})
        raise TestFailure(
            "FTL is not serving",
            "listening and blocking enabled",
            "; ".join(lines) or result.stderr.strip() or f"exit {result.exit_code}",
        )

    async def _ftl_tail(self, utils: TestUtils) -> dict[str, str] | None:
        """The FTL log tail at each boundary once there is a hop to read it through."""
        if utils.exec_route == "local":
            return None
        result = await utils.exec("tail -n 30 /var/log/pihole/FTL.log")
        return {"ftl_log": result.stdout.strip() or result.stderr.strip() or "(empty)"}


class BlocklistRoundTrip(LitmusTest):
    """Add a domain to the deny list over the API and watch DNS refuse it."""

    def __init__(
        self,
        *,
        name: str = "blocklist",
        domain: str = "blocked.litmus.bench",
        probe: str = "pi.hole",
        query_latency_limit_ms: float = 100.0,
        log_timeout_s: float = 15.0,
    ) -> None:
        """Capture the names this round trip uses and the limits it judges.

        Args:
            name: Node name used by logs, artifacts, and failures.
            domain: Added to the exact deny list in ``configure``, removed in ``cleanup``.
            probe: A name Pi-hole answers locally, proving the resolver works at all.
            query_latency_limit_ms: Ceiling on ``dig``'s reported query time.
            log_timeout_s: How long the query-log claim waits for FTL to show the query.
        """
        super().__init__(name=name)
        self._domain = domain
        self._probe = probe
        self._query_latency_limit_ms = query_latency_limit_ms
        self._log_timeout_s = log_timeout_s

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Open the API session, log in, and put the domain on the deny list."""
        api = await utils.get_http(
            base_url=utils.env.require("PIHOLE_API_URL"), request_timeout=10.0
        )
        password = utils.env.require("PIHOLE_API_PASSWORD")
        try:
            login = await api.request(
                DeviceRequest(
                    method="POST",
                    path="/api/auth",
                    payload=json.dumps({"password": password}).encode(),
                    headers={"Content-Type": "application/json"},
                )
            )
        except DeviceApiOperationFailure as error:
            raise TestFailure(f"FTL API unreachable: {error}") from error
        if login.status != 200:
            raise TestFailure("FTL API refused the login", 200, login.status)
        sid = str(json.loads(login.body).get("session", {}).get("sid", ""))
        if not sid:
            raise TestFailure("FTL API login carried no session id", "sid", login.body.decode())
        try:
            added = await api.request(
                DeviceRequest(
                    method="POST",
                    path="/api/domains/deny/exact",
                    payload=json.dumps(
                        {"domain": self._domain, "comment": "litmus bench"}
                    ).encode(),
                    headers={"Content-Type": "application/json", "sid": sid},
                )
            )
        except DeviceApiOperationFailure as error:
            raise TestFailure(f"FTL API dropped the deny-list add: {error}") from error
        if added.status not in (200, 201):
            raise TestFailure("could not add the deny-list entry", "201", added.status)
        utils.logger.info("deny list now carries %s", self._domain)
        return {
            "api": api,
            "sid": sid,
            "domain": self._domain,
            "probe": self._probe,
            "query_latency_limit_ms": self._query_latency_limit_ms,
            "log_timeout_s": self._log_timeout_s,
        }

    async def cleanup(self, utils: TestUtils) -> None:
        """Take the entry back off and end the session; a Pi-hole left dirty is a bug.

        Every read is ``.get()``: cleanup also runs after a configure Fail, and then
        ctx holds none of these keys (issue #152). ``configure()`` returns them only
        once the entry is on the list, so a missing key means there is nothing to take
        off. A login whose deny-list add then failed leaves its session to FTL's own
        timeout, because the ``sid`` never reached ctx; #163 asks for a way to carry it.
        """
        api: DeviceApi | None = utils.ctx.get("api")
        sid: str | None = utils.ctx.get("sid")
        domain: str | None = utils.ctx.get("domain")
        if api is None or sid is None or domain is None:
            utils.logger.info("configure did not settle the deny-list entry; nothing to undo")
            return
        headers = {"sid": sid}
        removed = await _settle(
            api,
            DeviceRequest(
                method="DELETE",
                path=f"/api/domains/deny/exact/{domain}",
                headers=headers,
            ),
        )
        utils.logger.info("deny-list entry removed: %s", removed)
        ended = await _settle(
            api, DeviceRequest(method="DELETE", path="/api/auth", headers=headers)
        )
        utils.logger.info("API session ended: %s", ended)

    def build_suite(self) -> LitmusTest:
        """Resolver works, then the block is visible on the wire, then in the log."""
        return self.sequence(
            self.name,
            [
                self.test("blocking-enabled", self._blocking_enabled, fail_after=15.0),
                self.test("resolver-answers", self._resolver_answers, fail_after=15.0),
                self.test("denied-domain-blocked", self._denied_domain, fail_after=15.0),
                self.test("query-logged", self._query_logged, fail_after=30.0),
            ],
        )

    @evidence("blocking")
    async def _blocking_enabled(self, utils: TestUtils) -> Verdict:
        """The API reports blocking enabled."""
        api: DeviceApi = utils.ctx["api"]
        try:
            response = await api.request(
                DeviceRequest(
                    method="GET", path="/api/dns/blocking", headers={"sid": utils.ctx["sid"]}
                )
            )
        except DeviceApiOperationFailure as error:
            raise TestFailure(f"API call failed: {error}") from error
        blocking = (
            str(json.loads(response.body).get("blocking", "")) if response.status == 200 else ""
        )
        if blocking == "enabled":
            return PASS({"blocking": blocking})
        raise TestFailure(
            "blocking is not enabled", "enabled", blocking or f"status {response.status}"
        )

    @evidence("queried", "answer", "query_time_ms")
    async def _resolver_answers(self, utils: TestUtils) -> Verdict:
        """A locally served name resolves to an address, inside the latency limit."""
        probe: str = utils.ctx["probe"]
        answer, query_ms, raw = await self._dig(utils, probe)
        reading = utils.measure(
            "probe_query_time_ms",
            query_ms,
            unit="ms",
            limits=(None, utils.ctx["query_latency_limit_ms"]),
        )
        try:
            ipaddress.ip_address(answer)
        except ValueError as error:
            raise TestFailure(f"{probe} did not resolve", "an address", raw) from error
        if reading.in_limits:
            return PASS({"queried": probe, "answer": answer, "query_time_ms": query_ms})
        raise TestFailure("resolver too slow", f"<= {reading.limits} ms", f"{query_ms} ms")

    @evidence("queried", "answer")
    async def _denied_domain(self, utils: TestUtils) -> Verdict:
        """The deny-listed domain answers with the block address."""
        domain: str = utils.ctx["domain"]
        answer, query_ms, raw = await self._dig(utils, domain)
        utils.measure("blocked_query_time_ms", query_ms, unit="ms")
        if answer in _BLOCKED_ANSWERS:
            return PASS({"queried": domain, "answer": answer})
        raise TestFailure(f"{domain} was not blocked", "0.0.0.0", raw)

    @evidence("status", "entries")
    async def _query_logged(self, utils: TestUtils) -> Verdict:
        """FTL's query log shows the blocked query with a blocked status."""
        api: DeviceApi = utils.ctx["api"]
        domain: str = utils.ctx["domain"]
        seen: list[dict[str, Any]] = []

        async def logged() -> bool:
            try:
                response = await api.request(
                    DeviceRequest(
                        method="GET",
                        path=f"/api/queries?domain={domain}&length=10",
                        headers={"sid": utils.ctx["sid"]},
                    )
                )
            except DeviceApiOperationFailure as error:
                raise TestFailure(f"API call failed: {error}") from error
            if response.status != 200:
                return False
            seen[:] = [
                q for q in json.loads(response.body).get("queries", []) if q.get("domain") == domain
            ]
            utils.logger.info("query log holds %d entries for %s", len(seen), domain)
            return bool(seen)

        await utils.wait_for(
            logged,
            looking_for=f"{domain} in the query log",
            interval=1.0,
            timeout_seconds=utils.ctx["log_timeout_s"],
        )
        statuses = sorted({str(q.get("status", "")) for q in seen})
        if any("DENYLIST" in status or "GRAVITY" in status for status in statuses):
            return PASS({"status": statuses, "entries": len(seen)})
        raise TestFailure("query logged but not as blocked", "DENYLIST", ", ".join(statuses))

    async def _dig(self, utils: TestUtils, name: str) -> tuple[str, int, str]:
        """``dig`` through the parent's hop: first answer, reported query time, raw text."""
        result = await utils.exec(f"dig +time=2 +tries=1 +stats {name} @127.0.0.1")
        answers = [
            line.split()[-1]
            for line in result.stdout.splitlines()
            if line and not line.startswith(";") and "\tIN\t" in line.replace("    ", "\t")
        ]
        found = _QUERY_TIME.search(result.stdout)
        query_ms = int(found.group(1)) if found else -1
        answer = answers[0] if answers else ""
        return answer, query_ms, result.stdout.strip() or result.stderr.strip()


async def _settle(api: DeviceApi, request: DeviceRequest) -> str:
    """One request whose outcome is a log line, never a verdict: status, or why not."""
    try:
        response = await api.request(request)
    except DeviceApiOperationFailure as error:
        return f"unreachable ({error})"
    return f"status {response.status}"
