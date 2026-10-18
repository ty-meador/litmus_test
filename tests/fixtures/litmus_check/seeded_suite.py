"""A suite with one seeded violation per rule; ``tests/test_check_*`` pin the lines.

Keep the seeds one per rule, each tripping only its own rule, and the line numbers stable.
"""

import os
from typing import Any

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict, evidence, measure
from litmus.errors import ArpResolutionFailure


class SeededBlade(LitmusTest):
    """One departure from each idiom, deliberately."""

    def __init__(self, *, name: str, limit_c: float) -> None:
        super().__init__(name=name)
        self._limit_c = limit_c

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        self._reading_c = 40.0  # seed: litmus/immutable-self
        self.use_survey(self._snapshot)  # seed: litmus/constructor-hooks
        return {}

    def build_suite(self) -> LitmusTest:
        claims = [self.test("temperature", lambda u: self._temperature(u))]  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]  # two seeds
        return self.group(self.name, claims)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]  # seed: litmus/retired-names

    async def _temperature(self, _utils: TestUtils) -> Verdict:
        if self._limit_c < 0:
            raise TestFailure("negative limit", ">= 0", str(self._limit_c))
        return PASS  # seed: litmus/gated-pass

    async def _snapshot(self, _utils: TestUtils) -> object:
        return {"reading_c": self._reading_c}

    def _annotation_seed(self, value) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]  # noqa: ANN001
        del value  # seed: litmus/unannotated-interface

    async def _plain_error(self, _utils: TestUtils) -> Verdict:
        raise ValueError("out of range")  # seed: litmus/raise-test-failure

    async def cleanup(self, utils: TestUtils) -> None:
        if self._limit_c:
            return PASS  # pyright: ignore[reportReturnType]  # seed: litmus/no-verdict-in-cleanup
        return None

    async def _unprovided(self, utils: TestUtils) -> Verdict:
        if utils.ctx["rail_v"] > 0:  # seed: litmus/ctx-provenance
            return PASS
        raise TestFailure("rail down", "> 0 V", "0 V")

    @evidence("reading_c")
    async def _unfulfilled(self, utils: TestUtils) -> Verdict:
        if self._limit_c:
            return PASS  # seed: litmus/evidence-fulfilled
        raise TestFailure("no limit", "> 0", "0")

    async def _retarget(self, utils: TestUtils) -> object:
        await utils.use_shell(inherit=False)  # seed: litmus/shell-in-configure
        return None

    async def _twice(self, utils: TestUtils) -> Verdict:
        utils.measure("rail_v", 3.3)
        utils.measure("rail_v", 3.31)  # seed: litmus/measurement-name-unique
        if self._limit_c:
            return PASS
        raise TestFailure("no limit", "> 0", "0")


async def before_each(_context: object) -> None:
    measure("hook_ms", 1.0)  # seed: litmus/measurement-in-node


class SeededGateway(LitmusTest):
    """The one seed that needs a constructor argument beside a discovery call."""

    def __init__(self, *, name: str, mac: str, ip: str | None = None) -> None:
        super().__init__(name=name)
        self._mac = mac
        self._ip = ip

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        try:  # the guarded resolve keeps model-resolution-failure quiet; the next line is the seed
            ip = self._ip or (await utils.get_arp_resolver(mac=self._mac).resolve())["ip"]
        except ArpResolutionFailure as error:  # seed above: litmus/no-input-fallback
            raise TestFailure("no address", "an IP", error.mac) from error
        return {"ip": ip}

    def build_suite(self) -> LitmusTest:
        return self.test("reachable", self._reachable)

    async def _reachable(self, utils: TestUtils) -> Verdict:
        if utils.ctx["ip"]:
            return PASS
        raise TestFailure("no address", "an IP", "none")


class SeededSeams(LitmusTest):
    """One departure from each seam idiom; nothing here trips a rule seeded above."""

    def __init__(self, *, name: str) -> None:
        super().__init__(name=name)

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        mac = os.environ["DUT_MAC"]  # seed: litmus/env-via-utils
        print(mac)  # seed: litmus/logger-not-print
        return {"mac": mac}

    def build_suite(self) -> LitmusTest:
        return self.test("reachable", self._reachable)

    async def _reachable(self, utils: TestUtils) -> Verdict:
        try:
            result = await utils.exec("true")
        except BaseException:  # seed: litmus/no-swallowed-cancel
            raise TestFailure("exec interrupted", "exit 0", "cancelled") from None
        if result.exit_code == 0:
            return PASS
        raise TestFailure("unreachable", "exit 0", str(result.exit_code))

    async def _resolve(self, utils: TestUtils) -> Verdict:
        arp = utils.get_arp_resolver(mac=utils.ctx["mac"])
        resolved = await arp.resolve()  # seed: litmus/model-resolution-failure
        if resolved["ip"]:
            return PASS
        raise TestFailure("no address", "an IP", "none")
