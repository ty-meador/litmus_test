"""The self-test suite may not write evidence into the checkout (#57).

``tests/conftest.py`` guards this two ways: the working directory and
``LITMUS_ARTIFACT_ROOT`` both point at ``tmp_path`` for every test, and a
before/after snapshot of the checkout's ``artifacts/`` fails the test that leaks.
"""

from collections.abc import Callable
from pathlib import Path

from litmus import PASS, Artifact, LitmusTest, TestUtils, Verdict
from litmus.lifecycle import State

CHECKOUT_ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"


class SurveyingSuite(LitmusTest):
    """One case, one captured artifact, no explicit artifact root anywhere."""

    def build_suite(self) -> LitmusTest:
        return self.case("snapshot", self._snapshot)

    async def _snapshot(self, utils: TestUtils) -> Verdict:
        await utils.capture_artifact(Artifact(name="snapshot", type="survey", data={"ok": True}))
        return PASS


class TestEvidenceContainment:
    async def test_explicit_env_without_artifact_root_lands_in_tmp_path(
        self,
        tmp_path: Path,
        checkout_evidence: Callable[[], frozenset[Path]],
    ) -> None:
        """An explicit ``env=`` bypasses the environment guard; the chdir guard holds."""
        before = checkout_evidence()

        result = await SurveyingSuite(name="rack").run(env={"PHYSICAL_LOCATION": "bench-7"})

        assert result.state is State.PASS
        written = [path for path in (tmp_path / "artifacts").rglob("*") if path.is_file()]
        assert len([path for path in written if path.suffix == ".artifact"]) == 1
        assert len([path for path in written if path.name == "timeline.log"]) == 1
        assert len([path for path in written if path.name == "timeline.jsonl"]) == 1
        assert checkout_evidence() == before

    def test_guard_snapshot_sees_a_file_written_into_the_checkout(
        self,
        checkout_evidence: Callable[[], frozenset[Path]],
    ) -> None:
        """The detection half: a file appearing under the checkout's ``artifacts/`` is caught."""
        before = checkout_evidence()
        created_dir = not CHECKOUT_ARTIFACTS.exists()
        probe = CHECKOUT_ARTIFACTS / "containment-probe.artifact"
        CHECKOUT_ARTIFACTS.mkdir(exist_ok=True)
        probe.write_text("probe")
        try:
            assert checkout_evidence() - before == {probe}
        finally:
            probe.unlink()
            if created_dir:
                CHECKOUT_ARTIFACTS.rmdir()
