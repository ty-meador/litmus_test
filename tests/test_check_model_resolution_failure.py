"""``litmus/model-resolution-failure``: a resolver or device-API miss is a verdict, not a bug.

``ArpResolutionFailure`` and ``DeviceApiOperationFailure`` are not ``TestFailure``.
Uncaught, the taxonomy files them as developer bugs; the suite must model the miss.
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.model_resolution_failure import ModelResolutionFailureRule

CODE = "litmus/model-resolution-failure"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(ModelResolutionFailureRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestModelResolutionFailureFire:
    def test_unguarded_resolve_names_the_exception(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    arp = utils.get_arp_resolver(mac=self._mac)
                    resolved = await arp.resolve()
                    return {**resolved, "arp": arp}
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(5, 20, CODE)]
        assert "ArpResolutionFailure" in violations[0].message
        assert "TestFailure" in violations[0].message

    def test_reacquire_and_request_are_covered(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _after_reboot(self, utils: TestUtils) -> Verdict:
                    ip = await utils.ctx["arp"].reacquire(utils)
                    response = await utils.ctx["api"].request(DeviceRequest(method="GET", path="/"))
                    if ip and response.status == 200:
                        return PASS
                    raise TestFailure("no", "200", str(response.status))
        """
        violations = _check(source)

        assert [v.line for v in violations] == [4, 5]
        assert "ArpResolutionFailure" in violations[0].message
        assert "DeviceApiOperationFailure" in violations[1].message

    def test_a_try_whose_handler_does_not_raise_test_failure_is_not_a_guard(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    try:
                        resolved = await arp.resolve()
                    except ArpResolutionFailure:
                        resolved = {"ip": ""}
                    return resolved
        """
        assert _lines(source) == [5]

    def test_a_call_in_the_handler_or_finally_is_not_in_the_guarded_body(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    try:
                        first = await arp.resolve()
                    except ArpResolutionFailure as error:
                        second = await arp.resolve()
                        raise TestFailure("gone", error.mac, error.host) from error
                    return first
        """
        assert _lines(source) == [7]


class TestModelResolutionFailureStaySilent:
    def test_the_documented_shape_is_clean(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    arp = utils.get_arp_resolver(mac=self._mac)
                    try:
                        resolved = await arp.resolve()
                    except ArpResolutionFailure as error:
                        raise TestFailure(
                            f"{error.mac} did not appear on the network",
                            error.mac,
                            error.host,
                        ) from error
                    return {**resolved, "arp": arp}
        """
        assert _lines(source) == []

    def test_the_raise_may_sit_anywhere_in_the_handler(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def _whoami(self, utils: TestUtils) -> Verdict:
                    try:
                        response = await utils.ctx["api"].request(request)
                    except DeviceApiOperationFailure as error:
                        utils.logger.warning("transfer failed: %s", error)
                        if error.retryable:
                            raise litmus.TestFailure("retry later", "a reply", "none") from error
                        raise TestFailure("transfer failed", "a reply", repr(error)) from error
                    if response.status == 200:
                        return PASS
                    raise TestFailure("rejected", "200", str(response.status))
        """
        assert _lines(source) == []

    def test_an_unawaited_call_is_left_for_the_caller_to_guard(self) -> None:
        source = """
            class Suite(LitmusTest):
                def _lookup(self, arp: ArpResolver) -> Awaitable[dict[str, str]]:
                    return arp.resolve()
        """
        assert _lines(source) == []

    def test_other_classes_are_ignored(self) -> None:
        source = """
            class Helper:
                async def go(self, arp: ArpResolver) -> dict[str, str]:
                    return await arp.resolve()
        """
        assert _lines(source) == []


class TestModelResolutionFailureAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:124:20: {CODE} ")
        assert lines[-1] == "Found 1 violation."
