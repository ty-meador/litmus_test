"""A suite with one seeded violation per rule; ``tests/test_check_*`` pin the lines.

Keep the seeds one per rule and keep the line numbers stable: each rule's CLI test names
the exact ``line:col`` it expects, which is what proves the fixture is caught end to
end rather than merely somewhere.
"""

from typing import Any

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict


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
        claims = [self.case("temperature", self._temperature)]  # seed: litmus/pure-build-suite
        return self.sequence(self.name, claims)

    async def _temperature(self, _utils: TestUtils) -> Verdict:
        if self._limit_c < 0:
            raise TestFailure("negative limit", ">= 0", str(self._limit_c))
        return PASS  # seed: litmus/gated-pass

    async def _snapshot(self, _utils: TestUtils) -> object:
        return {"reading_c": self._reading_c}

    def _annotation_seed(self, value) -> None:  # type: ignore  # noqa: ANN001
        del value  # seed: litmus/unannotated-interface
