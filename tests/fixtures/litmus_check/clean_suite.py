"""A suite that follows every ``litmus_check`` idiom; the CLI must find nothing here."""

from typing import Any

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict


class CleanBlade(LitmusTest):
    """Every idiom observed: gated PASS, pure build_suite, frozen self, constructor hooks."""

    def __init__(self, *, name: str, limit_c: float) -> None:
        super().__init__(name=name)
        self._limit_c = limit_c
        self.use_survey(self._snapshot)
        self.use_audit(interval=5.0, fn=self._thermals)

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        return {"reading_c": 40.0}

    def build_suite(self) -> LitmusTest:
        return self.sequence(
            self.name,
            [
                self.test("temperature", self._temperature),
                self.test(f"limit {self._limit_c:g}", self._limit_declared),
            ],
        )

    async def _temperature(self, utils: TestUtils) -> Verdict:
        if utils.ctx["reading_c"] <= self._limit_c:
            return PASS
        raise TestFailure("over limit", f"<= {self._limit_c:g}", str(utils.ctx["reading_c"]))

    async def _limit_declared(self, utils: TestUtils) -> Verdict:
        if self._limit_c > 0:
            return PASS
        raise TestFailure("no limit", "> 0", str(self._limit_c))

    async def _snapshot(self, utils: TestUtils) -> object:
        return {"reading_c": utils.ctx["reading_c"]}

    async def _thermals(self, utils: TestUtils) -> object:
        return {"limit_c": self._limit_c}
