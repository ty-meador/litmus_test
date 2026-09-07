"""The smallest complete Litmus suite: one authored claim that passes.

Concept: a suite is a class, a claim is a method, and a verdict is explicit.

Three things make a `LitmusTest` runnable, and this file has exactly those three:

- `__init__` captures what the suite is about. Nothing is observed here.
- `build_suite` declares the tree of claims. Here it is a single leaf.
- A test method returns `PASS` or raises `TestFailure`. Nothing else counts as a
  verdict; a method that falls off the end is a framework Abort, not a pass.

Even the smallest claim names its pass condition. `return PASS` is syntax; the `if`
that gates it is the modeling, and it is what the next reader looks for first. The
framework's tier-3 linter (`litmus-check`) flags an ungated `PASS` for that reason.

Run it: `litmus run examples/hello_world.py`.
"""

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict


class HelloWorld(LitmusTest):
    """Demonstrate the minimum composition needed for a passing suite."""

    def __init__(self, *, name: str, greeting: str = "hello world") -> None:
        """Capture the greeting this suite claims it can say.

        Keyword-only arguments are the convention for every suite: a call site that
        reads `HelloWorld(name="hello", greeting="hi")` documents itself, and adding a
        parameter later cannot silently shift an existing positional one.
        """
        super().__init__(name=name)
        # Instance state is frozen after construction. Anything a claim needs to know
        # is either captured here or provided by `configure()`; a test method that
        # tries to set an attribute at run time is a contract violation.
        self._greeting = greeting

    def build_suite(self) -> LitmusTest:
        """Build one leaf whose explicit verdict is Pass.

        `self.test(name, method)` is the whole composition vocabulary at this size.
        The name is what the Floor Technician sees in the tree and the artifacts, so
        it is written for them, not for the code.
        """
        return self.test("say hello", self._say_hello)

    async def _say_hello(self, utils: TestUtils) -> Verdict:
        # The `if` is the claim: "there is a greeting to say". The log line is the
        # observation, written through `utils.logger` so it lands in this node's
        # scoped timeline rather than in whatever the process's root logger does.
        if self._greeting:
            utils.logger.info(self._greeting)
            return PASS
        # `TestFailure(message, expected, received)` is the fail path. The three
        # positional fields are the FT's first read of any failure, so even a toy
        # claim fills them in.
        raise TestFailure("nothing to say", "a greeting", "(empty)")
