"""Scaffold smoke test — proves the package imports and the async test rig runs."""

import asyncio

import litmus


class TestScaffold:
    def test_package_imports(self) -> None:
        assert litmus.__name__ == "litmus"

    async def test_async_rig_runs(self) -> None:
        await asyncio.sleep(0)
