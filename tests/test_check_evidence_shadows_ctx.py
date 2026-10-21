"""``litmus/evidence-shadows-ctx``: an ``@evidence`` key is not a ctx key and not ``measurements``.

Bench finding F14, issue #161. Two of five bench suites hit the tier-1 refusal on their
first run because the natural name for a reading was the natural name for the limit or
target it was judged against. The rule moves that finding to the editor: a key
``@evidence`` declares that ``configure()`` is statically known to return, or that is the
framework-owned ``measurements`` slot, is flagged on the literal that declares it.
"""

import io
from pathlib import Path
from textwrap import dedent

import pytest

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_CLEAN, EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.evidence_shadows_ctx import EvidenceShadowsCtxRule

CODE = "litmus/evidence-shadows-ctx"
_BENCH_SUITES = Path(__file__).resolve().parents[1] / "bench" / "suites"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(EvidenceShadowsCtxRule().check(module))


def _found(source: str) -> list[tuple[int, int, str]]:
    return [(v.line, v.column, _named_key(v)) for v in _check(source)]


def _named_key(violation: Violation) -> str:
    assert violation.code == CODE
    return violation.message.split("'", 2)[1]


class TestEvidenceShadowsCtxFires:
    def test_a_literal_key_configure_returns_is_flagged_on_the_literal(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"kernel": "6.1.0", "limit_c": 70.0}

                @evidence("kernel", "machine")
                async def _identity(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["kernel"]:
                        return PASS({"kernel": "6.1.0", "machine": "x86_64"})
                    raise TestFailure("no")
        """
        violations = _check(source)

        assert _found(source) == [(6, 15, "kernel")]
        assert "configure() returns it as a ctx key" in violations[0].message
        assert "kernel_measured" in violations[0].message

    def test_every_clashing_key_is_its_own_violation(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return dict(domain="ads.example", probe="probe.example")

                @evidence("probe", "answer", "domain")
                async def _blocked(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["probe"]:
                        return PASS({"probe": "", "answer": "", "domain": ""})
                    raise TestFailure("no")
        """
        assert _found(source) == [(6, 15, "probe"), (6, 34, "domain")]

    def test_the_measurements_slot_is_reserved_whatever_configure_returns(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return self._discover()

                @evidence("measurements")
                async def _claim(self, utils: TestUtils) -> Verdict:
                    if utils.ctx.get("ok"):
                        return PASS({"measurements": []})
                    raise TestFailure("no")
        """
        violations = _check(source)

        assert _found(source) == [(6, 15, "measurements")]
        assert "framework-owned slot" in violations[0].message

    def test_a_typed_dict_declaration_is_flagged_on_its_field(self) -> None:
        source = """
            class _Ctx(TypedDict):
                rail_v: float

            class _Reading(TypedDict):
                rail_v: float
                ripple_mv: float

            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> _Ctx:
                    return {"rail_v": 3.3}

                @evidence(_Reading)
                async def _rail(self, utils: TestUtils) -> Verdict[_Reading]:
                    if utils.ctx["rail_v"]:
                        return PASS({"rail_v": 3.3, "ripple_mv": 1.0})
                    raise TestFailure("no")
        """
        assert _found(source) == [(6, 5, "rail_v")]

    def test_an_inherited_typed_dict_field_is_flagged_on_the_decorator(self) -> None:
        source = """
            class _Base(TypedDict):
                kernel: str

            class _Reading(_Base):
                machine: str

            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"kernel": "6.1.0"}

                @evidence(_Reading)
                async def _identity(self, utils: TestUtils) -> Verdict[_Reading]:
                    if utils.ctx["kernel"]:
                        return PASS({"kernel": "6.1.0", "machine": "x86_64"})
                    raise TestFailure("no")
        """
        assert _found(source) == [(12, 6, "kernel")]

    def test_a_subclass_without_configure_is_held_to_what_its_base_provides(self) -> None:
        source = """
            class Base(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"domain": "ads.example"}

            class Child(Base):
                @evidence("domain")
                async def _resolves(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["domain"]:
                        return PASS({"domain": "ads.example"})
                    raise TestFailure("no")
        """
        assert _found(source) == [(7, 15, "domain")]

    def test_a_local_dict_configure_builds_and_returns_counts(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    ctx = {"ip": "10.0.0.1"}
                    ctx["kernel"] = "6.1.0"
                    return ctx

                @evidence("kernel")
                async def _identity(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["kernel"]:
                        return PASS({"kernel": "6.1.0"})
                    raise TestFailure("no")
        """
        assert _found(source) == [(8, 15, "kernel")]


class TestEvidenceShadowsCtxStaysQuiet:
    def test_distinct_namespaces_are_clean_in_both_spellings(self) -> None:
        source = """
            class _Reading(TypedDict):
                kernel_running: str

            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"kernel": "6.1.0", "domain": "ads.example"}

                @evidence("kernel_running", "machine")
                async def _identity(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["kernel"]:
                        return PASS({"kernel_running": "6.1.0", "machine": "x86_64"})
                    raise TestFailure("no")

                @evidence(_Reading)
                async def _typed(self, utils: TestUtils) -> Verdict[_Reading]:
                    if utils.ctx["kernel"]:
                        return PASS({"kernel_running": "6.1.0"})
                    raise TestFailure("no")
        """
        assert _found(source) == []

    def test_an_unreadable_configure_is_left_to_tier_one(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return await self._discover(utils)

                @evidence("kernel")
                async def _identity(self, utils: TestUtils) -> Verdict:
                    if utils.ctx["kernel"]:
                        return PASS({"kernel": "6.1.0"})
                    raise TestFailure("no")
        """
        assert _found(source) == []

    def test_unreadable_declarations_undecorated_methods_and_helpers_are_ignored(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"kernel": "6.1.0"}

                @evidence(KEY)
                async def _computed(self, utils: TestUtils) -> Verdict:
                    return PASS({"kernel": "6.1.0"})

                @evidence(ImportedShape)
                async def _imported(self, utils: TestUtils) -> Verdict:
                    return PASS({"kernel": "6.1.0"})

                async def _plain(self, utils: TestUtils) -> Verdict:
                    return PASS({"kernel": "6.1.0"})

            class Helper:
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"kernel": "6.1.0"}

                @evidence("kernel")
                async def _claim(self, utils: TestUtils) -> Verdict:
                    return PASS({"kernel": "6.1.0"})
        """
        assert _found(source) == []

    def test_a_parent_suite_key_is_out_of_reach_by_design(self) -> None:
        # The leaf shares the parent's ctx at run time, where tier 1 refuses the
        # payload; a single-file analysis does not know the parent, so the rule is quiet.
        source = """
            class Parent(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"domain": "ads.example"}

            class Leaf(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {}

                @evidence("domain")
                async def _resolves(self, utils: TestUtils) -> Verdict:
                    if utils.ctx.get("domain"):
                        return PASS({"domain": "ads.example"})
                    raise TestFailure("no")
        """
        assert _found(source) == []


@pytest.mark.skipif(not _BENCH_SUITES.is_dir(), reason="bench suites are not in this checkout")
class TestEvidenceShadowsCtxAtTheBench:
    """The three findings F14 reported, reproduced by reverting the bench's renames."""

    _REVERTS: tuple[tuple[str, str, str], ...] = (
        (
            "guest_acceptance.py",
            '@evidence("kernel_running", "machine")',
            '@evidence("kernel", "machine")',
        ),
        (
            "pihole_rack.py",
            '@evidence("queried", "answer", "query_time_ms")',
            '@evidence("probe", "answer", "query_time_ms")',
        ),
        ("pihole_rack.py", '@evidence("queried", "answer")', '@evidence("domain", "answer")'),
    )

    def _bench(self, name: str, *reverts: tuple[str, str]) -> list[str]:
        source = (_BENCH_SUITES / name).read_text()
        for before, after in reverts:
            assert source.count(before) == 1, f"{name}: {before!r} is the one line to revert"
            source = source.replace(before, after)
        module = SuiteModule(Path(name), source)
        return [_named_key(v) for v in sorted(EvidenceShadowsCtxRule().check(module))]

    def test_the_renamed_bench_is_clean(self) -> None:
        for path in sorted(_BENCH_SUITES.glob("*.py")):
            assert self._bench(path.name) == [], path.name

    def test_reverting_the_renames_reproduces_all_three_findings(self) -> None:
        guest = [(b, a) for name, b, a in self._REVERTS if name == "guest_acceptance.py"]
        pihole = [(b, a) for name, b, a in self._REVERTS if name == "pihole_rack.py"]

        assert self._bench("guest_acceptance.py", *guest) == ["kernel"]
        assert self._bench("pihole_rack.py", *pihole) == ["probe", "domain"]


class TestEvidenceShadowsCtxAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:109:15: {CODE} ")
        assert lines[-1] == "Found 1 violation."

    def test_the_clean_fixture_alone_is_clean(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "litmus_check" / "clean_suite.py"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixture)]
        )

        assert status == EXIT_CLEAN
        assert out.getvalue() == "Found 0 violations.\n"
