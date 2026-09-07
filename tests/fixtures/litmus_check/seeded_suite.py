"""A suite with one seeded violation per rule; ``tests/test_check_*`` pin the lines.

Keep the seeds one per rule and keep the line numbers stable: each rule's CLI test names
the exact ``line:col`` it expects, which is what proves the fixture is caught end to
end rather than merely somewhere.
"""

from typing import Any

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict, evidence


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
