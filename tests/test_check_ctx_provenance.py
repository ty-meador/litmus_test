"""``litmus/ctx-provenance``: every ``utils.ctx["k"]`` read is provided by ``configure()``.

Strict by ruling (issue #112): a read is provided by ``configure()``'s returned dict or
``TypedDict``, or by a write earlier in the same method. A sibling method's write does
not count. Reads through ``.get()`` and ``in`` are the author declaring a key optional.
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.ctx_provenance import CtxProvenanceRule

CODE = "litmus/ctx-provenance"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(CtxProvenanceRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestCtxProvenanceFires:
    def test_read_of_a_key_configure_never_returns(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"present": 1}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["absent"] > 0:
                        return PASS
                    raise TestFailure("no", "> 0", "0")
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(6, 12, CODE)]
        assert '"absent" is read here but configure() does not provide it' in violations[0].message
        assert "read it with .get() if it is optional" in violations[0].message

    def test_class_without_configure_provides_nothing(self) -> None:
        source = """
            class Suite(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", self._claim)
                async def _claim(self, utils):
                    return utils.ctx["ip"]
        """
        assert _lines(source) == [6]

    def test_sibling_write_does_not_count_and_is_named(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {}
                async def _first(self, utils: TestUtils) -> Verdict:
                    utils.ctx["ip"] = "10.0.0.1"
                    return PASS
                async def _second(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["ip"]:
                        return PASS
                    raise TestFailure("no", "ip", "none")
        """
        violations = _check(source)

        assert [v.line for v in violations] == [9]
        assert "_first() writes it, but a method may not rely on a sibling's write" in (
            violations[0].message
        )

    def test_read_before_the_same_method_writes_it(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    utils.ctx["n"] = utils.ctx["n"] + 1
                    utils.ctx["m"] += 1
                    return PASS
        """
        assert _lines(source) == [6, 7]

    def test_cleanup_and_hooks_and_configure_are_readers(self) -> None:
        source = """
            class Suite(LitmusTest):
                def __init__(self, *, name: str) -> None:
                    super().__init__(name=name)
                    self.use_survey(self._snapshot)
                    self.use_audit(1.0, fn=self._watch)
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    rail = utils.ctx["rail"]
                    return {"rail": rail}
                async def cleanup(self, utils: TestUtils) -> None:
                    utils.ctx["shell"].close()
                async def _snapshot(self, utils: TestUtils) -> object:
                    return utils.ctx["temperature_c"]
                async def _watch(self, utils: TestUtils) -> None:
                    utils.ctx["attempts"]
        """
        assert _lines(source) == [8, 11, 13, 15]

    def test_alias_through_cast_is_still_ctx(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"rail": "on"}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    ctx = cast("_Device", utils.ctx)
                    plain = utils.ctx
                    if ctx["rail"] == "on" and plain["missing"]:
                        return PASS
                    raise TestFailure("no", "on", "off")
        """
        violations = _check(source)

        assert [(v.line, v.column) for v in violations] == [(8, 36)]
        assert '"missing"' in violations[0].message

    def test_inherited_configure_from_a_same_module_base(self) -> None:
        source = """
            class Base(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"serial": "SN-1"}
            class Suite(Base):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["serial"] and utils.ctx["firmware"]:
                        return PASS
                    raise TestFailure("no", "fw", "none")
        """
        violations = _check(source)

        assert [v.line for v in violations] == [7]
        assert '"firmware"' in violations[0].message


class TestCtxProvenanceStaysQuiet:
    def test_provided_by_dict_literal_typed_dict_and_local_dict(self) -> None:
        source = """
            class _Ctx(TypedDict):
                rail: str
                temperature_c: float
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> _Ctx:
                    built = {"rail": "on"}
                    built["temperature_c"] = 40.0
                    built.update(attempts=0)
                    return built
                async def _claim(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["rail"] and utils.ctx["temperature_c"] and utils.ctx["attempts"]:
                        return PASS
                    raise TestFailure("no", "on", "off")
        """
        assert _lines(source) == []

    def test_dict_call_and_cast_return_are_readable(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    if utils.env.get("FAST"):
                        return dict(mode="fast")
                    return cast(dict[str, Any], {"mode": "slow"})
                async def _claim(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["mode"]:
                        return PASS
                    raise TestFailure("no", "mode", "none")
        """
        assert _lines(source) == []

    def test_same_method_write_before_read_and_optional_reads(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    utils.ctx["reading"] = 1.0
                    utils.ctx.update(attempts=1)
                    utils.ctx.setdefault("samples", [])
                    optional = utils.ctx.get("optional")
                    present = "flag" in utils.ctx
                    if utils.ctx["reading"] and utils.ctx["attempts"] and utils.ctx["samples"]:
                        return PASS
                    raise TestFailure("no", str(optional), str(present))
        """
        assert _lines(source) == []

    def test_unreadable_configure_return_silences_the_class(self) -> None:
        spread = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    resolved = await utils.get_arp_resolver().resolve("aa:bb")
                    return {**resolved, "arp": True}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["ip"]
        """
        computed = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {self.own_key: True}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["ip"]
        """
        helper = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return self._build()
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["ip"]
        """
        foreign_base = """
            class Suite(Imported):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["ip"]
        """
        for source in (spread, computed, helper):
            assert _lines(source) == [], source
        assert _lines(foreign_base) == []

    def test_dynamic_keys_wildcard_updates_and_non_suite_classes(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    name = "x"
                    utils.ctx.update(await self._probe())
                    if utils.ctx[name] and utils.ctx["anything"]:
                        return PASS
                    raise TestFailure("no", "x", "none")
            class Helper:
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["ip"]
        """
        assert _lines(source) == []

    def test_methods_without_a_utils_parameter_are_not_readers(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {}
                def build_suite(self) -> LitmusTest:
                    return self.test("x", self._claim)
                @property
                def run_data(self) -> JsonObject:
                    return {**super().run_data, "extra": self._ctx["x"]}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return PASS
        """
        assert _lines(source) == []


class TestCtxProvenanceEdges:
    """The branches a real suite rarely takes, pinned so the rule stays honest."""

    def test_members_that_are_not_readers_are_skipped(self) -> None:
        source = """
            class Suite(LitmusTest):
                \"\"\"A docstring and a class attribute are not methods.\"\"\"
                limit_c = 70.0
                def __init__(self, *, name: str) -> None:
                    super().__init__(name=name)
                def build_suite(self) -> LitmusTest:
                    return self.test("x", self._other)
                async def cleanup(self) -> None:
                    return None
                def _helper() -> None:
                    return None
                async def _claim(self, utils: TestUtils) -> "Verdict":
                    count: int
                    name = "k"
                    utils.ctx[name] = 1
                    utils.ctx.setdefault(name, [])
                    utils.ctx.update(**extra)
                    utils.ctx.update({"literal": 1})
                    if utils.ctx["literal"]:
                        return PASS
                    raise TestFailure("no", "1", "0")
        """
        assert _lines(source) == []

    def test_configure_edge_returns(self) -> None:
        bare_return = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    if utils.env.get("SKIP"):
                        return
                    return {"rail": "on"}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["rail"]
        """
        assert _lines(bare_return) == []
        local_edges = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    pending: dict[str, Any]
                    other = {}
                    other["ignored"] = 1
                    first, second = pair
                    first["ignored"] = 1
                    first.update(ignored=2)
                    built = {"rail": "on"}
                    built[utils.env["KEY"]] = 1
                    return built
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["anything"]
        """
        assert _lines(local_edges) == []
        wildcard_update = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    built = {"rail": "on"}
                    built.update(await self._probe())
                    return built
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["anything"]
        """
        assert _lines(wildcard_update) == []
        non_string_key = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {1: "one"}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["anything"]
        """
        assert _lines(non_string_key) == []
        dict_call_positional = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return dict(await self._probe())
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["anything"]
        """
        assert _lines(dict_call_positional) == []
        dict_call_splat = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return dict(**await self._probe())
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["anything"]
        """
        assert _lines(dict_call_splat) == []

    def test_typed_dict_inheritance_and_string_annotation(self) -> None:
        source = """
            class _Base(TypedDict):
                rail: str
            class _Ctx(_Base):
                temperature_c: float
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> "_Ctx":
                    return {}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["rail"] and utils.ctx["temperature_c"]:
                        return PASS
                    raise TestFailure("no", "on", "off")
        """
        assert _lines(source) == []
        not_a_typed_dict = """
            class _Plain:
                rail: str
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> _Plain:
                    return {}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["rail"]
        """
        assert _lines(not_a_typed_dict) == [8]
        imported_annotation = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> Imported:
                    return {}
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["rail"]
        """
        assert _lines(imported_annotation) == [6]

    def test_unresolvable_base_silences_a_derived_suite(self) -> None:
        source = """
            class Base(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"serial": "SN-1"}
            class Suite(Base, Mixin):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["firmware"]
            class Grandchild(Suite):
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return utils.ctx["firmware"]
        """
        assert _lines(source) == []

    def test_registrar_calls_outside_a_method_are_ignored(self) -> None:
        source = """
            class Suite(LitmusTest):
                hooks = register(use_survey)
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {}
                def build_suite(*args) -> LitmusTest:
                    return args[0].test("x")
                async def _snapshot(self, utils: TestUtils) -> object:
                    return utils.ctx.get("x")
        """
        assert _lines(source) == []


class TestCtxProvenanceAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:49:12: {CODE} ")
        assert lines[-1] == "Found 1 violation."
