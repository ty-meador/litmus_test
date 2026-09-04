"""Enforce the per-file coverage floors recorded in pyproject.toml. Not shipped.

``make check`` runs the suite under branch coverage. This script reads the JSON
report and enforces the global floor (``[tool.coverage.report] fail_under``) and the
per-file floors in ``[tool.litmus.coverage.floors]``, which coverage.py cannot do on
its own. Measurements are rounded to the report's precision before the comparison,
the same rule coverage.py applies to ``fail_under``, so 91.46 meets a floor of 91.5
and a rounding edge cannot flap the gate. Floors are ratchet-only: below the floor
is red; comfortably above it prints a hint to raise the floor in the same change
(STANDARDS.md section 6).
"""

from __future__ import annotations

import json
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
REPORT = ROOT / ".coverage" / "coverage.json"
PRECISION = 1
RATCHET_HINT_MARGIN = 1.0


class CoverageFloors:
    """The recorded floors, and the verdict on one coverage report against them.

    ``enforce`` prints one line per floored file plus the total and returns the
    process exit code: 0 when every floor is met, 1 when any measurement rounds below
    its floor or a floored file is missing from the report (a renamed kernel file
    must move its floor too).
    """

    def __init__(self, *, total_floor: float, file_floors: Mapping[str, float]) -> None:
        self._total_floor = total_floor
        self._file_floors = dict(file_floors)

    @classmethod
    def from_pyproject(cls, pyproject: Path = PYPROJECT) -> CoverageFloors:
        """Read ``fail_under`` and ``[tool.litmus.coverage.floors]`` from pyproject."""
        with pyproject.open("rb") as handle:
            tool = tomllib.load(handle)["tool"]
        total_floor = float(tool["coverage"]["report"]["fail_under"])
        raw_floors: Mapping[str, object] = tool["litmus"]["coverage"]["floors"]
        file_floors = {path: float(_number(floor)) for path, floor in raw_floors.items()}
        return cls(total_floor=total_floor, file_floors=file_floors)

    def enforce(self, report: Path = REPORT, out: TextIO = sys.stdout) -> int:
        """Compare the report to the floors; print the table and any ratchet hints."""
        with report.open("rb") as handle:
            payload = json.load(handle)
        files: Mapping[str, Mapping[str, Mapping[str, float]]] = payload["files"]
        measured_by_path = {
            Path(path).as_posix(): float(entry["summary"]["percent_covered"])
            for path, entry in files.items()
        }
        red = False
        for path, floor in sorted(self._file_floors.items()):
            measured = measured_by_path.get(path)
            if measured is None:
                out.write(f"RED   {path}: floored but absent from the coverage report\n")
                red = True
                continue
            red = self._report(path, measured, floor, out) or red
        total = float(payload["totals"]["percent_covered"])
        red = self._report("TOTAL (fail_under)", total, self._total_floor, out) or red
        if red:
            out.write("coverage floor not met; floors only move up (STANDARDS.md section 6)\n")
            return 1
        return 0

    def _report(self, label: str, measured: float, floor: float, out: TextIO) -> bool:
        """Print one verdict line (and a ratchet hint); return True when it is red."""
        rounded = round(measured, PRECISION)
        status = "ok   "
        if rounded < floor:
            status = "RED  "
        out.write(f"{status} {label}: {rounded:.{PRECISION}f}% (floor {floor:.{PRECISION}f}%)\n")
        if rounded - floor >= RATCHET_HINT_MARGIN:
            out.write(f"      ratchet: raise the floor for {label} to {rounded:.{PRECISION}f}\n")
        return rounded < floor


def _number(value: object) -> float:
    """Accept the int or float a TOML floor may be written as; reject anything else."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"coverage floor must be a number, got {value!r}"
        raise TypeError(msg)
    return float(value)


if __name__ == "__main__":
    raise SystemExit(CoverageFloors.from_pyproject().enforce())
