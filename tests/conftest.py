"""Suite-wide fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def artifacts_stay_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the default artifact root at the test's tmp_path.

    A run that reads its environment from the process — every ``main()`` or
    ``run()`` without an explicit ``env`` — would otherwise publish survey and
    capture evidence into ``./artifacts`` of whichever checkout ran the suite.
    """
    monkeypatch.setenv("LITMUS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
