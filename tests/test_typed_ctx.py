# A `pyright: ignore` that suppresses nothing would make the misspelled-key claims in
# this file vacuous, so here an unnecessary ignore is itself an error.
# pyright: reportUnnecessaryTypeIgnoreComment=true
"""Opt-in typed context: ``LitmusTest[CtxT]`` narrows ``utils.ctx`` for the author (#75).

Nothing about the run changes. The context object is always the framework's
``RunContext``, a ``dict[str, Any]`` the framework owns. The parameter exists so that a
suite which already writes its context shape down as a ``TypedDict`` gets that shape
back in its own methods, and the developer's editor rejects a misspelled key while it is
still a typo — before the Floor Technician meets it as an ``Aborted`` run.

Half of this file is checked by pyright rather than by pytest: the ``assert_type`` calls
and the ignore comments are the assertions, and ``make check`` is what runs them.
"""

from collections.abc import Mapping
from typing import Any, TypedDict, assert_type

from litmus import PASS, LitmusTest, State, TestUtils, Verdict
from litmus.utils import RunContext


class PowerCtx(TypedDict):
    """The context a power suite promises its own methods."""

    rail: str
    volts: float


class TypedPowerSuite(LitmusTest[PowerCtx]):
    """A suite that names its context shape once and is held to it everywhere."""

    async def configure(self, utils: TestUtils[PowerCtx]) -> PowerCtx:
        return {"rail": "3v3", "volts": 3.3}

    def build_suite(self) -> LitmusTest:
        return self.test("rail", self._rail_is_up)

    async def _rail_is_up(self, utils: TestUtils[PowerCtx]) -> Verdict:
        # The point of the parameter: these are `str` and `float` to the author's
        # editor, not `Any`, so arithmetic and string work on them is checked.
        assert_type(utils.ctx["rail"], str)
        assert_type(utils.ctx["volts"], float)
        return PASS

    async def cleanup(self, utils: TestUtils[PowerCtx]) -> None:
        assert_type(utils.ctx["rail"], str)


class UntypedPowerSuite(LitmusTest):
    """The same suite without the parameter: what every suite written before #75 is."""

    async def configure(self, utils: TestUtils) -> Mapping[str, Any]:
        return {"rail": "3v3", "volts": 3.3}

    def build_suite(self) -> LitmusTest:
        return self.test("rail", self._rail_is_up)

    async def _rail_is_up(self, utils: TestUtils) -> Verdict:
        # Unchanged from before the parameter existed: the context is open, and reading
        # a key the suite never promised is caught at runtime by #112, not by pyright.
        assert_type(utils.ctx, Mapping[str, Any])
        return PASS


class TestTheParameterIsOptional:
    """Acceptance: an un-parameterized suite behaves exactly as it did before #75."""

    async def test_an_unparameterized_suite_runs_unchanged(self) -> None:
        result = await UntypedPowerSuite(name="psu").run()

        assert result.state is State.PASS
        assert result.run_data["rail"] == "3v3"

    def test_the_bare_class_is_the_defaulted_class(self) -> None:
        # PEP 696 default: writing `TestUtils` and writing `TestUtils[Mapping[str, Any]]`
        # name the same thing, which is why no existing annotation had to change.
        assert_type(TestUtils().ctx, Mapping[str, Any])


class TestTheParameterNarrowsForTheAuthor:
    """Acceptance: a parameterized suite gets its own shape back, under pyright strict."""

    async def test_a_parameterized_suite_runs_and_reports_the_same_way(self) -> None:
        result = await TypedPowerSuite(name="psu").run()

        assert result.state is State.PASS
        # run_data is the context snapshot; the parameter is a static claim only, so
        # filtering and reporting see exactly what an untyped suite would produce.
        assert result.run_data == {"measurements": [], "rail": "3v3", "volts": 3.3}

    async def test_the_runtime_context_is_still_the_framework_s_own_object(self) -> None:
        seen: list[Mapping[str, Any]] = []

        class Observed(TypedPowerSuite):
            async def _rail_is_up(self, utils: TestUtils[PowerCtx]) -> Verdict:
                seen.append(utils.ctx)
                return PASS

        result = await Observed(name="psu").run()

        assert result.state is State.PASS
        assert isinstance(seen[0], RunContext)


class TestAMisspelledKeyIsRejectedStatically:
    """The whole point of the parameter: the typo never reaches a run.

    Each ignore below marks a real pyright error. The file-level
    ``reportUnnecessaryTypeIgnoreComment`` override at the top turns a stale ignore into
    a failure, so these stay honest as the framework changes.
    """

    async def test_reading_an_undeclared_key_is_a_type_error(self) -> None:
        class Misspelled(TypedPowerSuite):
            async def _rail_is_up(self, utils: TestUtils[PowerCtx]) -> Verdict:
                _ = utils.ctx["volt"]  # pyright: ignore[reportGeneralTypeIssues, reportUnknownVariableType]
                return PASS

        # It still runs — the parameter is a claim to the editor, not a runtime guard.
        # #112 is what stops the read at runtime, and it reports the key by name.
        result = await Misspelled(name="psu").run()

        assert result.state is State.FAIL

    def test_returning_the_wrong_shape_from_configure_is_a_type_error(self) -> None:
        class WrongConfigure(TypedPowerSuite):
            async def configure(self, utils: TestUtils[PowerCtx]) -> PowerCtx:
                return {"rail": "3v3"}  # pyright: ignore[reportReturnType]

        assert issubclass(WrongConfigure, LitmusTest)
