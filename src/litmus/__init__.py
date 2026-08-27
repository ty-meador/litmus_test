"""litmus — React-for-hardware HIL testing framework.

The public surface. Suite authors need `PASS`, `Verdict`, and `TestFailure`; everything
else here exists for the framework's own machinery and for the tools that read a run's
artifacts.
"""

from litmus.errors import (
    ContractViolation,
    ExternalIntegrationFailure,
    Failure,
    FrameworkContractViolation,
    LitmusError,
    MissingVerdict,
    TestFailure,
)
from litmus.lifecycle import ChildBoundary, Outcome, RunAbort, Slot, SlotPolicy, State
from litmus.verdict import PASS, Verdict

__all__ = [
    "PASS",
    "ChildBoundary",
    "ContractViolation",
    "ExternalIntegrationFailure",
    "Failure",
    "FrameworkContractViolation",
    "LitmusError",
    "MissingVerdict",
    "Outcome",
    "RunAbort",
    "Slot",
    "SlotPolicy",
    "State",
    "TestFailure",
    "Verdict",
]
