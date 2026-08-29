"""Context ownership across independently composable LitmusTest instances."""

import asyncio

from litmus import PASS, LitmusTest, Verdict
from litmus.lifecycle import State


class StubUtils:
    def __init__(self, **ctx: object) -> None:
        self.ctx: dict[str, object] = dict(ctx)


class TestContextLocality:
    async def test_configure_context_stays_with_its_authored_instance(self) -> None:
        child_seen: list[dict[str, object]] = []
        parent_cleanup_seen: list[dict[str, object]] = []

        class Child(LitmusTest):
            async def configure(self, utils: StubUtils) -> dict[str, object]:
                child_seen.append(dict(utils.ctx))
                return {"child": "configured"}

            def build_suite(self) -> LitmusTest:
                return self.case("child claim", self._claim)

            async def _claim(self, utils: StubUtils) -> Verdict:
                child_seen.append(dict(utils.ctx))
                return PASS

        class Parent(LitmusTest):
            async def configure(self, utils: StubUtils) -> dict[str, object]:
                return {"parent": "configured"}

            def build_suite(self) -> LitmusTest:
                return Child(name="child")

            async def cleanup(self, utils: StubUtils) -> None:
                parent_cleanup_seen.append(dict(utils.ctx))

        utils = StubUtils(station="west")

        outcome = await Parent(name="parent").main(utils)

        assert outcome.state is State.PASS
        assert utils.ctx == {"station": "west", "parent": "configured"}
        assert parent_cleanup_seen == [{"station": "west", "parent": "configured"}]
        assert child_seen == [{}, {"child": "configured"}]

    async def test_sibling_contexts_never_cross(self) -> None:
        left_configured = asyncio.Event()
        right_configured = asyncio.Event()
        seen: dict[str, dict[str, object]] = {}

        class Child(LitmusTest):
            def __init__(
                self,
                *,
                name: str,
                own_key: str,
                configured: asyncio.Event,
                peer_configured: asyncio.Event,
            ) -> None:
                super().__init__(name=name)
                self.own_key = own_key
                self.configured = configured
                self.peer_configured = peer_configured

            async def configure(self, utils: StubUtils) -> dict[str, object]:
                self.configured.set()
                return {self.own_key: True}

            def build_suite(self) -> LitmusTest:
                return self.case(f"{self.name} claim", self._claim)

            async def _claim(self, utils: StubUtils) -> Verdict:
                await self.peer_configured.wait()
                seen[self.name] = dict(utils.ctx)
                return PASS

        class Parent(LitmusTest):
            async def configure(self, utils: StubUtils) -> dict[str, object]:
                return {"parent": True}

            def build_suite(self) -> LitmusTest:
                return self.group(
                    "siblings",
                    [
                        Child(
                            name="left",
                            own_key="left",
                            configured=left_configured,
                            peer_configured=right_configured,
                        ),
                        Child(
                            name="right",
                            own_key="right",
                            configured=right_configured,
                            peer_configured=left_configured,
                        ),
                    ],
                )

        outcome = await Parent(name="parent").main(StubUtils(injected=True))

        assert outcome.state is State.PASS
        assert seen == {"left": {"left": True}, "right": {"right": True}}

    async def test_child_observes_the_same_context_nested_or_standalone(self) -> None:
        seen: list[dict[str, object]] = []

        class Child(LitmusTest):
            async def configure(self, utils: StubUtils) -> dict[str, object]:
                seen.append(dict(utils.ctx))
                return {"serial": "SN-0042"}

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: StubUtils) -> Verdict:
                seen.append(dict(utils.ctx))
                return PASS

        class Parent(LitmusTest):
            async def configure(self, utils: StubUtils) -> dict[str, object]:
                return {"parent-only": True}

            def build_suite(self) -> LitmusTest:
                return Child(name="nested child")

        nested = await Parent(name="parent").main(StubUtils())
        standalone = await Child(name="standalone child").main(StubUtils())

        assert nested.state is State.PASS
        assert standalone.state is State.PASS
        assert seen == [{}, {"serial": "SN-0042"}, {}, {"serial": "SN-0042"}]
