"""Author-facing errors name the rule broken and the one-sentence fix (#80)."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from litmus import (
    PASS,
    ArpResolver,
    Artifact,
    ContractViolation,
    FrameworkContractViolation,
    LitmusTest,
    MissingVerdict,
    Phase,
    RunResult,
    SummaryTreeNode,
    TestUtils,
    Verdict,
)
from litmus.cli import SuiteLoader, UsageError
from litmus.errors import ContextInsufficient
from litmus.executor import ExecResult, LocalExec
from litmus.hooks import (
    AuditHook,
    BeforeEachContext,
    BeforeEachHook,
    IntegrationHooks,
    SurveyHook,
    invoke_integration_hook,
)
from litmus.lifecycle import Slot, State
from litmus.remote import RemoteInterface
from litmus.results import NodeType
from litmus.routing import ShellOpenRequest, SshConnector
from litmus.ssh import SshInterface
from litmus.timeline import RunTimeline
from litmus.utils import RunContext
from litmus_check import Rule, RuleRegistry, SuiteModule, Violation

_FCV_FIX = "report it to the framework maintainers with the full log and reproduction steps"


def _assert_names_rule_and_fix(message: str) -> None:
    """A rule clause, then a one-sentence fix, separated by ';' or ':'."""
    for separator in ("; ", ": "):
        if separator not in message:
            continue
        rule, fix = message.split(separator, 1)
        if rule.strip() and fix.strip():
            return
    raise AssertionError(f"message does not name a rule and a fix: {message!r}")


def _suite(self: LitmusTest) -> LitmusTest:
    return self.test("claim", self._claim)  # type: ignore[attr-defined]


async def _claim(self: LitmusTest, utils: object) -> Verdict:
    return PASS


def _sync_claim(self: LitmusTest, utils: object) -> Verdict:
    return PASS


class _AddressedStub(RemoteInterface):
    """Reports an address without implementing retarget — the default TypeError path."""

    @property
    def address(self) -> tuple[str, int] | None:
        return ("10.0.0.1", 22)

    async def _connect(self) -> None:
        return None

    async def _disconnect(self) -> None:
        return None

    async def _exec(self, command: str) -> ExecResult:
        raise AssertionError(command)


class TestDefaultDetails:
    def test_developer_default_names_the_fix(self) -> None:
        _assert_names_rule_and_fix(ContractViolation.DEFAULT_DETAIL)
        assert ContractViolation("rack").detail == ContractViolation.DEFAULT_DETAIL

    def test_framework_default_names_the_escalation(self) -> None:
        _assert_names_rule_and_fix(FrameworkContractViolation.DEFAULT_DETAIL)
        violation = FrameworkContractViolation("rack")
        assert violation.detail == FrameworkContractViolation.DEFAULT_DETAIL
        assert "maintainers" in violation.fail_message

    def test_a_rule_only_framework_detail_gains_the_escalation(self) -> None:
        violation = FrameworkContractViolation(
            "rack",
            detail="an internal test has no child suite",
        )
        assert violation.detail == f"an internal test has no child suite; {_FCV_FIX}"
        _assert_names_rule_and_fix(violation.detail)

    def test_an_already_escalated_detail_is_not_doubled(self) -> None:
        detail = f"an internal test has no child suite; {_FCV_FIX}"
        violation = FrameworkContractViolation("rack", detail=detail)
        assert violation.detail == detail
        assert violation.detail.count("framework maintainers") == 1


class TestImportTimeTypeErrors:
    def test_sealed_override_names_the_fix(self) -> None:
        with pytest.raises(TypeError) as raised:
            type("OverridesMain", (LitmusTest,), {"build_suite": _suite, "main": _claim})
        assert str(raised.value) == (
            "LitmusTest.main is framework-owned and cannot be overridden; "
            "rename the method or call the public API instead of replacing it"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_lifecycle_method_must_be_async(self) -> None:
        with pytest.raises(TypeError) as raised:
            type(
                "SyncConfigure",
                (LitmusTest,),
                {"build_suite": _suite, "configure": _sync_claim},
            )
        assert str(raised.value) == (
            "SyncConfigure.configure must be defined with async def; "
            "write it as async def configure(self, utils)"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_verdict_method_must_be_async(self) -> None:
        with pytest.raises(TypeError) as raised:

            class SyncClaim(LitmusTest):  # pyright: ignore[reportUnusedClass]
                def build_suite(self) -> LitmusTest:
                    return self.test(
                        "claim",
                        self._claim,  # pyright: ignore[reportArgumentType]
                    )

                def _claim(self, utils: object) -> Verdict:
                    return PASS

        assert str(raised.value) == (
            "SyncClaim._claim must be defined with async def; "
            "write it as async def _claim(self, utils)"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_build_suite_must_be_overridden(self) -> None:
        with pytest.raises(TypeError) as raised:

            class MissingSuite(LitmusTest):  # pyright: ignore[reportUnusedClass]
                pass

        assert str(raised.value) == (
            "MissingSuite.build_suite must be overridden; "
            "define def build_suite(self) -> LitmusTest on the subclass"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_build_suite_must_be_a_method(self) -> None:
        with pytest.raises(TypeError) as raised:
            type("DataSuite", (LitmusTest,), {"build_suite": object()})
        assert str(raised.value) == (
            "DataSuite.build_suite must be defined with def; assign a method, not a data attribute"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_build_suite_must_not_be_async(self) -> None:
        async def async_suite(self: LitmusTest) -> LitmusTest:
            return self

        with pytest.raises(TypeError) as raised:
            type("AsyncSuite", (LitmusTest,), {"build_suite": async_suite})
        assert str(raised.value) == (
            "AsyncSuite.build_suite must be defined with def, not async def; "
            "build_suite is a pure table of contents and must not await"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_test_rejects_a_synchronous_function(self) -> None:
        with pytest.raises(TypeError) as raised:
            LitmusTest(name="rack").test("claim", _sync_claim)  # type: ignore[arg-type]
        assert str(raised.value) == (
            f"test method {_sync_claim.__qualname__} must be defined with async def; "
            "write it as async def and return PASS or raise TestFailure"
        )
        _assert_names_rule_and_fix(str(raised.value))


class TestAuthorContractViolations:
    def test_empty_parallel_and_sequence_name_the_fix(self) -> None:
        suite = LitmusTest(name="rack")
        with pytest.raises(ContractViolation) as parallel:
            suite.parallel("parallel", [])
        with pytest.raises(ContractViolation) as sequence:
            suite.sequence("sequence", [])
        assert parallel.value.detail == (
            "a parallel node must contain at least one child; pass one or more LitmusTest instances"
        )
        assert sequence.value.detail == (
            "a sequence must contain at least one child; pass one or more LitmusTest instances"
        )
        _assert_names_rule_and_fix(parallel.value.detail)
        _assert_names_rule_and_fix(sequence.value.detail)

    def test_arguments_that_do_not_fit_name_the_fix(self) -> None:
        async def probe(_utils: object, *, cpu: int) -> Verdict:
            del cpu
            return PASS

        with pytest.raises(ContractViolation) as raised:
            LitmusTest(name="rack").test("no cpu", probe)
        assert raised.value.detail == (
            "arguments do not fit test method "
            f"{probe.__qualname__} (missing a required keyword-only argument: 'cpu'); "
            "pass arguments that match the method (utils is supplied by the framework)"
        )
        _assert_names_rule_and_fix(raised.value.detail)

    def test_hooks_after_construction_name_the_fix(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._claim)

            async def _survey(self, utils: object) -> object:
                return None

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        suite = Suite(name="rack")
        with pytest.raises(ContractViolation) as raised:
            suite.use_survey(suite._survey)  # pyright: ignore[reportPrivateUsage]
        assert raised.value.detail == (
            "hooks may be registered only during construction; "
            "call use_survey or use_audit from __init__ after super().__init__"
        )
        _assert_names_rule_and_fix(raised.value.detail)

    def test_base_build_suite_names_the_composition_fix(self) -> None:
        with pytest.raises(ContractViolation) as raised:
            LitmusTest(name="rack").build_suite()
        assert raised.value.detail == (
            "build_suite() must return a test, sequence, or parallel composition; "
            "return self.test(...), self.sequence(...), or self.parallel(...)"
        )
        _assert_names_rule_and_fix(raised.value.detail)

    def test_mutual_exclusive_deadlines_name_the_fix(self) -> None:
        with pytest.raises(ContractViolation) as raised:
            LitmusTest(name="rack", fail_after=1.0, abort_after=1.0)
        assert raised.value.detail == (
            "fail_after and abort_after are mutually exclusive; declare exactly one or neither"
        )
        _assert_names_rule_and_fix(raised.value.detail)

    def test_non_positive_deadline_names_the_fix(self) -> None:
        with pytest.raises(ContractViolation) as raised:
            LitmusTest(name="rack", fail_after=0)
        assert raised.value.detail == (
            "fail_after must be a positive finite number of seconds; "
            "pass a number of seconds greater than zero"
        )
        _assert_names_rule_and_fix(raised.value.detail)

    async def test_duplicate_root_deadline_names_the_fix(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._claim)

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        with pytest.raises(ContractViolation) as raised:
            await Suite(name="rack", fail_after=1.0).run(fail_after=2.0)
        assert raised.value.detail == (
            "a root time budget is already declared; declare exactly one "
            "fail_after or abort_after across construction and run"
        )
        _assert_names_rule_and_fix(raised.value.detail)

    async def test_a_second_run_names_the_fix(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._claim)

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        suite = Suite(name="rack")
        await suite.main(TestUtils())
        with pytest.raises(ContractViolation) as raised:
            await suite.main(TestUtils())
        assert raised.value.detail == (
            "a LitmusTest instance may run only once; construct a new instance for each run"
        )
        _assert_names_rule_and_fix(raised.value.detail)

    async def test_a_live_via_target_names_the_fix(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._claim)

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        suite = Suite(name="rack")
        await suite.run_configure_only()
        try:
            with pytest.raises(ContractViolation) as raised:
                await suite.main(TestUtils())
            assert raised.value.detail == (
                "this instance is already up as a via target; "
                "construct a separate instance to run, or tear the hop down first"
            )
            _assert_names_rule_and_fix(raised.value.detail)
        finally:
            await suite.run_cleanup_only()


class TestContextAndVerdictMessages:
    def test_missing_ctx_key_names_the_configure_fix(self) -> None:
        ctx = RunContext(owner="psu")
        with pytest.raises(ContextInsufficient) as raised:
            ctx["rail"]
        assert raised.value.detail == (
            "context insufficient: configure() did not provide 'rail'; "
            "return 'rail' from configure()"
        )
        _assert_names_rule_and_fix(raised.value.detail)

    def test_missing_ctx_key_names_a_different_owner(self) -> None:
        violation = ContextInsufficient("rail", key="absent", owner="psu")
        assert violation.detail == (
            "context insufficient: configure() did not provide 'absent' "
            "(context owned by 'psu'); return 'absent' from configure()"
        )
        _assert_names_rule_and_fix(violation.detail)

    def test_missing_verdict_names_the_return_fix(self) -> None:
        missing = MissingVerdict("rail", returned=None)
        assert missing.fail_message == (
            "CONTRACT VIOLATION IN rail: returned None instead of the PASS sentinel; "
            "return PASS or raise TestFailure"
        )
        _assert_names_rule_and_fix(missing.fail_message)


class TestHookMessages:
    def test_survey_must_be_async(self) -> None:
        def sync_survey(utils: object) -> object:
            return None

        with pytest.raises(TypeError) as raised:
            SurveyHook(fn=sync_survey)  # type: ignore[arg-type]
        assert str(raised.value) == (
            "survey hook function must be defined with async def; "
            "write it as async def survey(self, utils)"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_empty_survey_phases_name_the_fix(self) -> None:
        async def survey(utils: object) -> object:
            return None

        with pytest.raises(ValueError, match="at least one phase") as raised:
            SurveyHook(fn=survey, phases=frozenset())
        assert str(raised.value) == (
            "a survey must observe at least one phase; "
            "pass one or more Phase values, or omit phases to observe every boundary"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_an_unknown_survey_phase_names_the_fix(self) -> None:
        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_survey(self._survey, phases=["mid-test"])  # pyright: ignore[reportArgumentType]

            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._claim)

            async def _survey(self, utils: object) -> object:
                return None

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        with pytest.raises(ValueError, match="mid-test") as raised:
            Suite(name="rack")
        names = ", ".join(member.value for member in Phase)
        assert str(raised.value) == (
            f"'mid-test' is not a survey phase; pass one of {names}, "
            "or omit phases to observe every boundary"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_audit_interval_must_be_positive(self) -> None:
        async def audit(utils: object) -> object:
            return None

        with pytest.raises(ValueError, match="audit interval") as raised:
            AuditHook(interval=0, fn=audit)
        assert str(raised.value) == (
            "audit interval must be a positive finite number; "
            "pass a number of seconds greater than zero"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_integration_hook_must_be_callable(self) -> None:
        with pytest.raises(TypeError) as raised:
            IntegrationHooks(before_each=cast("BeforeEachHook", object()))
        assert str(raised.value) == "before_each hook must be callable; pass an async function"
        _assert_names_rule_and_fix(str(raised.value))

    async def test_integration_hook_must_return_an_awaitable(self) -> None:
        def sync_hook(_ctx: BeforeEachContext) -> None:
            return None

        context = BeforeEachContext(
            name="rack",
            ancestor_names=("rack",),
            bread_crumbs="rack",
            logger=logging.LoggerAdapter(logging.getLogger("litmus"), extra={}),
            env={},
        )
        with pytest.raises(TypeError) as raised:
            await invoke_integration_hook(
                "before_each",
                cast("BeforeEachHook", sync_hook),
                context,
            )
        assert str(raised.value) == (
            "before_each hook must return an awaitable; "
            "define it with async def so the call returns a coroutine"
        )
        _assert_names_rule_and_fix(str(raised.value))


class TestValueErrorMessages:
    def test_mac_normalisation_names_the_fix(self) -> None:
        with pytest.raises(ValueError, match="12 hex digits") as raised:
            ArpResolver(mac="aa:bb:cc")
        assert str(raised.value) == (
            "MAC address must contain 12 hex digits, got 'aa:bb:cc'; "
            "pass a colon, dash, or bare-hex MAC"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_ping_count_names_the_fix(self) -> None:
        with pytest.raises(ValueError, match="ping_count") as raised:
            ArpResolver(mac="aa:bb:cc:dd:ee:ff", ping_count=0)
        assert str(raised.value) == "ping_count must be at least 1; pass a positive packet count"
        _assert_names_rule_and_fix(str(raised.value))

    def test_artifact_identity_names_the_fix(self) -> None:
        with pytest.raises(ValueError, match="artifact name") as raised:
            Artifact(name="bad name", type="survey", data={})
        assert str(raised.value) == (
            "artifact name 'bad name' must use only letters, numbers, dashes, "
            "and underscores; rename it to match that alphabet"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_artifact_timestamp_names_the_fix(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware") as raised:
            Artifact(
                name="snapshot",
                type="survey",
                data={},
                timestamp=datetime(2026, 8, 30),
            )
        assert str(raised.value) == (
            "artifact timestamp must be timezone-aware; "
            "pass a datetime with tzinfo, not a naive datetime"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_output_budget_names_the_fix(self) -> None:
        with pytest.raises(ValueError, match="output_budget") as raised:
            LocalExec(output_budget=0)
        assert str(raised.value) == (
            "output_budget must be at least 1 byte; pass a positive byte ceiling"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_env_filters_name_the_fix(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive") as raised:
            TestUtils(env_allow=["HOME"], env_deny=["TOKEN"])
        assert str(raised.value) == (
            "env_allow and env_deny are mutually exclusive: "
            "publish by allow list or by deny list, never both"
        )
        _assert_names_rule_and_fix(str(raised.value))

    async def test_wait_for_interval_names_the_fix(self) -> None:
        with pytest.raises(ValueError, match="wait_for interval") as raised:
            await TestUtils().wait_for(lambda: True, looking_for="up", interval=0)
        assert str(raised.value) == (
            "wait_for interval must be greater than zero; pass a positive number of seconds"
        )
        _assert_names_rule_and_fix(str(raised.value))

    async def test_wait_for_timeout_names_the_fix(self) -> None:
        with pytest.raises(ValueError, match="wait_for timeout") as raised:
            await TestUtils().wait_for(lambda: True, looking_for="up", timeout_seconds=-1)
        assert str(raised.value) == (
            "wait_for timeout must not be negative; pass zero or a positive number of seconds"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_ssh_via_and_tunnel_name_the_fix(self) -> None:
        hop = SshInterface(host="127.0.0.1", username="ops", password="x")
        with pytest.raises(ValueError, match="via and tunnel") as raised:
            SshInterface(
                host="10.0.0.2",
                username="root",
                password="x",
                via=hop,
                tunnel=hop,
            )
        assert str(raised.value) == (
            "via and tunnel are mutually exclusive: own the hop or borrow it, never both"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_run_result_navigation_invariants_name_the_fix(self) -> None:
        tree = SummaryTreeNode(name="rack", type=NodeType.SUITE, state=State.PASS)
        with pytest.raises(ValueError, match="ancestor_names") as raised:
            RunResult(
                slot=Slot.TEST_METHOD,
                node="rack",
                state=State.PASS,
                ancestor_names=[],
                bread_crumbs="rack",
                summary_tree=tree,
                type=NodeType.SUITE,
            )
        assert str(raised.value) == (
            "ancestor_names must be a non-empty root-to-node path; "
            "pass the lineage ending in this node's name"
        )
        _assert_names_rule_and_fix(str(raised.value))

        with pytest.raises(ValueError, match="bread_crumbs") as raised:
            RunResult(
                slot=Slot.TEST_METHOD,
                node="rack",
                state=State.PASS,
                ancestor_names=["rack"],
                bread_crumbs="other",
                summary_tree=tree,
                type=NodeType.SUITE,
            )
        assert str(raised.value) == (
            "bread_crumbs must equal '/'.join(ancestor_names); "
            "set bread_crumbs to the joined ancestor_names"
        )
        _assert_names_rule_and_fix(str(raised.value))

        mismatch = SummaryTreeNode(name="other", type=NodeType.SUITE, state=State.PASS)
        with pytest.raises(ValueError, match="summary_tree") as raised:
            RunResult(
                slot=Slot.TEST_METHOD,
                node="rack",
                state=State.PASS,
                ancestor_names=["rack"],
                bread_crumbs="rack",
                summary_tree=mismatch,
                type=NodeType.SUITE,
            )
        assert str(raised.value) == (
            "summary_tree root must describe the RunResult node and state; "
            "pass a tree whose name and state match this result"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_timeline_scope_names_the_fix(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="node scope") as raised:
            RunTimeline(tmp_path).register(())
        assert str(raised.value) == (
            "a timeline owner must have a node scope; pass a non-empty (name, ...) lineage"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_duplicate_rule_code_names_the_fix(self) -> None:
        class _Once(Rule):
            code = "test/once"
            summary = "once"

            def check(self, module: SuiteModule) -> Iterable[Violation]:
                del module
                return []

        with pytest.raises(ValueError, match="duplicate rule code") as raised:
            RuleRegistry([_Once(), _Once()])
        assert str(raised.value) == "duplicate rule code: test/once; give each rule a unique code"
        _assert_names_rule_and_fix(str(raised.value))


class TestRoutingAndTransportTypeErrors:
    async def test_use_shell_host_without_username_names_the_fix(self) -> None:
        with pytest.raises(TypeError) as raised:
            await TestUtils().use_shell(host="10.0.0.1")
        assert str(raised.value) == (
            "use_shell connection form requires username; pass username= together with host="
        )
        _assert_names_rule_and_fix(str(raised.value))

    async def test_use_shell_username_without_host_names_the_fix(self) -> None:
        with pytest.raises(TypeError) as raised:
            await TestUtils().use_shell(username="root")
        assert str(raised.value) == (
            "use_shell username requires host; pass host= together with username="
        )
        _assert_names_rule_and_fix(str(raised.value))

    async def test_ssh_connector_rejects_a_non_ssh_via(self) -> None:
        hop = _AddressedStub()
        await hop.connect()
        request = ShellOpenRequest(host="10.0.0.2", username="root", via=hop)
        with pytest.raises(TypeError) as raised:
            await SshConnector().open(request)
        assert str(raised.value) == (
            "SshConnector via must be an SshInterface; pass an SshInterface hop"
        )
        _assert_names_rule_and_fix(str(raised.value))

    async def test_addressless_transport_names_the_connect_fix(self) -> None:
        class _Uart(RemoteInterface):
            async def _connect(self) -> None:
                return None

            async def _disconnect(self) -> None:
                return None

            async def _exec(self, command: str) -> ExecResult:
                raise AssertionError(command)

        with pytest.raises(TypeError) as raised:
            await _Uart().connect("10.0.0.2")
        assert str(raised.value) == (
            "_Uart has no address to connect to; call connect() without host or port"
        )
        _assert_names_rule_and_fix(str(raised.value))

    async def test_addressed_transport_without_retarget_names_the_fix(self) -> None:
        transport = _AddressedStub()
        with pytest.raises(TypeError) as raised:
            await transport.connect("10.0.0.9")
        assert str(raised.value) == (
            "_AddressedStub reports an address but cannot re-point it; "
            "override _retarget on the transport subclass, or connect() to the existing address"
        )
        _assert_names_rule_and_fix(str(raised.value))


class TestRunnerUsageErrors:
    def test_missing_file_names_the_fix(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent.py"
        with pytest.raises(UsageError) as raised:
            SuiteLoader().load(str(missing))
        assert str(raised.value) == (
            f"{missing}: no such file; give a path to an existing .py file"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_empty_module_names_the_fix(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.py"
        path.write_text("VALUE = 1\n", encoding="utf-8")
        with pytest.raises(UsageError) as raised:
            SuiteLoader().load(str(path))
        assert str(raised.value) == (
            f"{path}: defines no LitmusTest subclass; "
            f"define one in that module or name one as {path}::SuiteClass"
        )
        _assert_names_rule_and_fix(str(raised.value))

    def test_selector_without_a_source_names_the_fix(self) -> None:
        with pytest.raises(UsageError) as raised:
            SuiteLoader().load("::Passing")
        assert str(raised.value) == (
            "'::Passing': a suite file or module name must precede '::'; "
            "give a path or module name before the selector"
        )
        _assert_names_rule_and_fix(str(raised.value))
