"""Suite-wide fixtures."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

CHECKOUT_ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"


def _checkout_evidence() -> frozenset[Path]:
    """Every file currently under the checkout's ``artifacts/``."""
    if not CHECKOUT_ARTIFACTS.exists():
        return frozenset()
    return frozenset(path for path in CHECKOUT_ARTIFACTS.rglob("*") if path.is_file())


@pytest.fixture(autouse=True)
def evidence_stays_out_of_the_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Keep every run's evidence under ``tmp_path`` and fail the test that leaks.

    ``TestUtils`` resolves its artifact root from ``LITMUS_ARTIFACT_ROOT`` in the
    environment it was handed, then from ``./artifacts`` of the working directory.
    The variable set here reaches a run that reads the process environment; a test
    that hands ``TestUtils`` its own ``env={...}`` never sees it and falls through
    to the working directory, which under ``uv run pytest`` is the checkout. Moving
    the working directory to ``tmp_path`` closes that fallback as well.

    Prevention is not proof. The snapshot taken before the test and compared after
    it turns a leak into a red test, so ``make check`` reports it instead of
    ``git status`` after the gate.
    """
    before = _checkout_evidence()
    monkeypatch.setenv("LITMUS_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.chdir(tmp_path)
    yield
    leaked = sorted(_checkout_evidence() - before)
    assert not leaked, (
        f"{len(leaked)} evidence file(s) written into the checkout by this test; "
        f"first: {leaked[0].relative_to(CHECKOUT_ARTIFACTS.parent)}"
    )


@pytest.fixture
def checkout_evidence() -> Callable[[], frozenset[Path]]:
    """The snapshot the autouse guard compares, for tests that prove the guard."""
    return _checkout_evidence
