"""litmus — React-for-hardware HIL testing framework.

The public surface. Suite authors need `PASS`, `Verdict`, and `TestFailure`; the rest
of the taxonomy is exported because it appears in artifacts and logs, where a reader
will follow a name back to its meaning.

The lifecycle machinery (`litmus.lifecycle`) is deliberately absent. The child
boundary, its slots, and its policies are the framework's internal organs — a suite
author never holds an instance of any of them, so they do not belong in the outward
API. The framework and its self-tests import them from `litmus.lifecycle` directly.
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
from litmus.verdict import PASS, Verdict

__all__ = [
    "PASS",
    "ContractViolation",
    "ExternalIntegrationFailure",
    "Failure",
    "FrameworkContractViolation",
    "LitmusError",
    "MissingVerdict",
    "TestFailure",
    "Verdict",
]
