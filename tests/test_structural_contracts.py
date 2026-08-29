"""Import-time structural enforcement for authored LitmusTest subclasses."""

import pytest

from litmus import PASS, ContractViolation, LitmusTest, Verdict


def _suite(self: LitmusTest) -> LitmusTest:
    return self.case("claim", self._claim)  # type: ignore[attr-defined]


async def _replacement(self: LitmusTest, utils: object) -> Verdict:
    return PASS


class TestSealedInternals:
    @pytest.mark.parametrize(
        "method_name",
        [
            "__delattr__",
            "__init_subclass__",
            "__setattr__",
            "case",
            "main",
            "run",
            "_run_lifecycle",
            "_configure_and_merge_context",
            "_run_cleanup",
            "_safe_cleanup",
            "_safe_survey",
        ],
    )
    def test_framework_owned_methods_reject_overrides(self, method_name: str) -> None:
        namespace: dict[str, object] = {
            "build_suite": _suite,
            method_name: _replacement,
        }

        with pytest.raises(TypeError, match=rf"{method_name}.*framework-owned"):
            type(f"Overrides_{method_name}", (LitmusTest,), namespace)


class TestCoroutineContracts:
    @pytest.mark.parametrize("method_name", ["configure", "cleanup"])
    def test_lifecycle_methods_must_be_coroutines(self, method_name: str) -> None:
        def synchronous(self: LitmusTest, utils: object) -> object:
            return {}

        namespace: dict[str, object] = {
            "build_suite": _suite,
            method_name: synchronous,
        }

        with pytest.raises(TypeError, match=rf"{method_name}.*async def"):
            type(f"Synchronous_{method_name}", (LitmusTest,), namespace)

    def test_verdict_annotated_test_method_must_be_a_coroutine(self) -> None:
        with pytest.raises(TypeError, match=r"_claim.*async def"):

            class SynchronousClaim(LitmusTest):  # pyright: ignore[reportUnusedClass]
                def build_suite(self) -> LitmusTest:
                    return self.case(
                        "claim",
                        self._claim,  # pyright: ignore[reportArgumentType]
                    )

                def _claim(self, utils: object) -> Verdict:
                    return PASS

    def test_deferred_verdict_annotation_is_also_enforced(self) -> None:
        def synchronous(self: LitmusTest, utils: object) -> object:
            return PASS

        synchronous.__annotations__["return"] = "Verdict"
        namespace: dict[str, object] = {
            "build_suite": _suite,
            "_claim": synchronous,
        }

        with pytest.raises(TypeError, match=r"_claim.*async def"):
            type("SynchronousClaim", (LitmusTest,), namespace)

    def test_sync_helper_without_a_verdict_contract_remains_valid(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            def _label(self) -> str:
                return "claim"

            async def _claim(self, utils: object) -> Verdict:
                assert self._label() == "claim"
                return PASS

        assert Suite(name="rack").name == "rack"

    def test_specialization_cannot_drop_an_inherited_async_verdict_contract(self) -> None:
        class BaseSuite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        with pytest.raises(TypeError, match=r"_claim.*async def"):

            class SynchronousSpecialization(  # pyright: ignore[reportUnusedClass]
                BaseSuite
            ):
                def _claim(  # pyright: ignore[reportIncompatibleMethodOverride]
                    self, utils: object
                ) -> object:
                    return PASS


class TestBuildSuiteContract:
    def test_build_suite_must_be_implemented_by_an_authored_lineage(self) -> None:
        with pytest.raises(TypeError, match=r"build_suite.*must be overridden"):

            class MissingSuite(LitmusTest):  # pyright: ignore[reportUnusedClass]
                pass

    def test_specialization_may_inherit_an_authored_build_suite(self) -> None:
        class BaseSuite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        class SpecializedSuite(BaseSuite):
            async def _claim(self, utils: object) -> Verdict:
                return PASS

        assert SpecializedSuite(name="specialized").name == "specialized"

    def test_build_suite_must_be_a_synchronous_method(self) -> None:
        with pytest.raises(TypeError, match=r"build_suite.*def, not async def"):

            class AsyncSuite(LitmusTest):  # pyright: ignore[reportUnusedClass]
                async def build_suite(  # pyright: ignore[reportIncompatibleMethodOverride]
                    self,
                ) -> LitmusTest:
                    return self

    def test_build_suite_must_be_a_method(self) -> None:
        with pytest.raises(TypeError, match=r"build_suite.*defined with def"):
            type("DataSuite", (LitmusTest,), {"build_suite": object()})


class TestHookRegistrationShape:
    def test_audit_requires_interval_and_function_during_construction(self) -> None:
        class MissingInterval(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_audit(  # pyright: ignore[reportCallIssue]
                    fn=self._audit
                )

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _audit(self, utils: object) -> object:
                return None

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        with pytest.raises(TypeError, match="interval"):
            MissingInterval(name="rack")

    def test_registered_hook_functions_must_be_coroutines(self) -> None:
        class SynchronousAudit(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_audit(interval=1.0, fn=self._audit)  # type: ignore[arg-type]

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            def _audit(self, utils: object) -> object:
                return None

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        with pytest.raises(TypeError, match=r"audit.*async def"):
            SynchronousAudit(name="rack")

    def test_complete_hooks_register_during_construction(self) -> None:
        class Suite(LitmusTest):
            def __init__(self, *, name: str) -> None:
                super().__init__(name=name)
                self.use_survey(self._survey)
                self.use_audit(interval=1.0, fn=self._audit)

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _survey(self, utils: object) -> object:
                return None

            async def _audit(self, utils: object) -> object:
                return None

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        assert Suite(name="rack").name == "rack"

    def test_hooks_cannot_be_registered_after_construction(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _survey(self, utils: object) -> object:
                return None

            async def _claim(self, utils: object) -> Verdict:
                return PASS

        suite = Suite(name="rack")

        with pytest.raises(ContractViolation, match="during construction"):
            suite.use_survey(suite._survey)  # pyright: ignore[reportPrivateUsage]
