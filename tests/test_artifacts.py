"""Point-in-time artifacts and their chronological log pointers."""

import asyncio
import json
import logging
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import pytest

import litmus
from litmus import PASS, Artifact, LitmusTest, TestUtils, Verdict
from litmus.lifecycle import State


class _ArtifactPointerRecord(Protocol):
    artifact_name: str
    artifact_path: str
    artifact_timestamp: str
    artifact_type: str
    litmus_scope: tuple[str, ...]
    message: str


class TestArtifactValue:
    def test_artifact_is_public_immutable_data_without_a_write_surface(self) -> None:
        artifact = Artifact(name="pre-flash", type="hardware-survey", data={"ready": True})

        assert litmus.Artifact is Artifact
        assert "Artifact" in litmus.__all__
        assert not hasattr(artifact, "write")
        assert not hasattr(artifact, "write_file")
        with pytest.raises(FrozenInstanceError):
            artifact.name = "changed"  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("name", "artifact_type"),
        [
            ("../escape", "survey"),
            ("rack/snapshot", "survey"),
            ("snapshot", "../../survey"),
            ("", "survey"),
            ("snapshot", ""),
        ],
    )
    def test_artifact_identity_cannot_escape_the_artifact_root(
        self,
        name: str,
        artifact_type: str,
    ) -> None:
        with pytest.raises(ValueError, match="artifact"):
            Artifact(name=name, type=artifact_type, data={})

    def test_artifact_timestamp_must_be_timezone_aware(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            Artifact(
                name="snapshot",
                type="survey",
                data={},
                timestamp=datetime(2026, 8, 30),
            )


class TestCaptureArtifact:
    async def test_capture_persists_then_logs_a_scoped_pointer(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        captured_paths: list[Path] = []
        timestamp = datetime(2026, 8, 30, 14, 5, 9, tzinfo=UTC)

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.case("snapshot", self._snapshot)

            async def _snapshot(self, utils: TestUtils) -> Verdict:
                artifact = Artifact(
                    name="pre-flash",
                    type="hardware-survey",
                    data={"firmware": "2.4.1", "healthy": True},
                    timestamp=timestamp,
                )
                captured_paths.append(await utils.capture_artifact(artifact))
                return PASS

        with caplog.at_level(logging.INFO, logger="litmus"):
            outcome = await Suite(name="rack").run(env={"LITMUS_ARTIFACT_ROOT": str(tmp_path)})

        assert outcome.state is State.PASS
        assert len(captured_paths) == 1
        artifact_path = captured_paths[0]
        assert artifact_path.is_absolute()
        assert artifact_path.parent == tmp_path
        assert artifact_path.name.startswith("pre-flash.hardware-survey.")
        assert artifact_path.suffix == ".artifact"

        payload = json.loads(artifact_path.read_text())
        assert payload == {
            "data": {"firmware": "2.4.1", "healthy": True},
            "format_version": 1,
            "name": "pre-flash",
            "timestamp": "2026-08-30T14:05:09Z",
            "type": "hardware-survey",
        }

        pointer = cast(
            "_ArtifactPointerRecord",
            next(record for record in caplog.records if hasattr(record, "artifact_path")),
        )
        assert pointer.message == (
            f"artifact captured [hardware-survey:pre-flash] -> {artifact_path}"
        )
        assert pointer.artifact_name == "pre-flash"
        assert pointer.artifact_type == "hardware-survey"
        assert pointer.artifact_path == str(artifact_path)
        assert pointer.artifact_timestamp == "2026-08-30T14:05:09Z"
        assert pointer.litmus_scope == ("rack", "snapshot")

    async def test_capture_rejects_unserializable_data_without_orphaning_a_file(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        utils = TestUtils(artifact_root=tmp_path)
        artifact = Artifact(name="bad", type="survey", data=object())

        with caplog.at_level(logging.INFO, logger="litmus"), pytest.raises(TypeError):
            await utils.capture_artifact(artifact)

        assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []
        assert not any(hasattr(record, "artifact_path") for record in caplog.records)

    async def test_concurrent_captures_never_overwrite_one_another(self, tmp_path: Path) -> None:
        utils = TestUtils(artifact_root=tmp_path)
        artifact = Artifact(name="sample", type="survey", data={"temperature": 42})

        paths = await asyncio.gather(*(utils.capture_artifact(artifact) for _ in range(8)))

        assert len(set(paths)) == 8
        assert all(path.exists() for path in paths)

    def test_explicit_artifact_root_wins_over_the_environment(self, tmp_path: Path) -> None:
        explicit = tmp_path / "explicit"
        utils = TestUtils(
            env={"LITMUS_ARTIFACT_ROOT": str(tmp_path / "environment")},
            artifact_root=explicit,
        )

        assert utils.artifact_root == explicit.resolve()
