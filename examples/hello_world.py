"""The smallest complete Litmus suite: one authored claim that passes.

Even the smallest claim names its pass condition. `return PASS` is syntax; the `if`
that gates it is the modeling, and it is what the next reader looks for first.
"""

import asyncio

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict


class HelloWorld(LitmusTest):
    """Demonstrate the minimum composition needed for a passing suite."""

    def __init__(self, *, name: str, greeting: str = "hello world") -> None:
        """Capture the greeting this suite claims it can say."""
        super().__init__(name=name)
        self._greeting = greeting

    def build_suite(self) -> LitmusTest:
        """Build one leaf whose explicit verdict is Pass."""
        return self.test("say hello", self._say_hello)

    async def _say_hello(self, utils: TestUtils) -> Verdict:
        if self._greeting:
            utils.logger.info(self._greeting)
            return PASS
        raise TestFailure("nothing to say", "a greeting", "(empty)")


class HelloWorldExample:
    """Run the hello-world suite as a standalone program."""

    @staticmethod
    async def run() -> None:
        """Execute the example through the framework-owned lifecycle."""
        await HelloWorld(name="hello world").run()


if __name__ == "__main__":
    asyncio.run(HelloWorldExample.run())
