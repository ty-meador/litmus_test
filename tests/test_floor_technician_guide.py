"""The Floor Technician guide explains every Inspector JSON field from the FT side.

Issue #83: every field table in ``docs/inspector-json.md`` is accounted for in
``docs/floor-technician.md``, so the station page cannot drift from the contract.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "inspector-json.md"
GUIDE = ROOT / "docs" / "floor-technician.md"
README = ROOT / "README.md"

_FIELD_ROW = re.compile(r"^\| `([^`]+)` \|", re.MULTILINE)


def _table_fields(markdown: str) -> list[str]:
    return [match.group(1) for match in _FIELD_ROW.finditer(markdown)]


class TestFloorTechnicianGuide:
    def test_guide_exists(self) -> None:
        assert GUIDE.is_file()

    def test_readme_links_the_guide(self) -> None:
        readme = README.read_text(encoding="utf-8")
        assert "docs/floor-technician.md" in readme
        assert "Floor Technician" in readme

    def test_overview_points_at_the_guide(self) -> None:
        overview = (ROOT / "docs" / "overview.md").read_text(encoding="utf-8")
        assert "docs/floor-technician.md" in overview

    def test_every_inspector_json_field_is_named_in_the_guide(self) -> None:
        contract_fields = _table_fields(CONTRACT.read_text(encoding="utf-8"))
        guide = GUIDE.read_text(encoding="utf-8")
        missing = [field for field in contract_fields if f"`{field}`" not in guide]
        assert contract_fields, "inspector-json.md grew no field tables"
        assert missing == []

    def test_recovery_protocol_names_the_three_states_and_the_bug_signal(self) -> None:
        guide = GUIDE.read_text(encoding="utf-8")
        for token in (
            "`Pass`",
            "`Fail`",
            "`Aborted`",
            "`TestFailure`",
            "`ExternalIntegrationFailure`",
            "`MissingVerdict`",
            "`OperatorInterrupt`",
            "`CONTRACT VIOLATION`",
            "operator",
            "hardware",
        ):
            assert token in guide, token
