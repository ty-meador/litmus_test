"""The smallest complete Litmus suite: one authored claim that passes."""

import asyncio
from types import SimpleNamespace

from litmus import PASS, LitmusTest, Verdict


class HelloWorld(LitmusTest):
    """Demonstrate the minimum composition needed for a passing suite."""

    def build_suite(self) -> LitmusTest:
        """Build one leaf whose explicit verdict is Pass."""
        return self.case("say hello", self._say_hello)

    async def _say_hello(self, _utils: object) -> Verdict:
        return PASS


class HelloWorldExample:
    """Run the hello-world suite as a standalone program."""

    @staticmethod
    async def run() -> None:
        """Execute the example through the framework-owned lifecycle."""
        await HelloWorld(name="hello world").main(SimpleNamespace(ctx={}))


if __name__ == "__main__":
    asyncio.run(HelloWorldExample.run())
