"""litmus — React-for-hardware HIL testing framework.

The public surface. Suite authors need `LitmusTest`, `TestUtils`, `ExecResult`,
`Artifact`, `PASS`, `Verdict`, and `TestFailure`; the rest of the taxonomy is
exported because it appears in artifacts and logs, where a reader will follow a
name back to its meaning. Harness integrators may also import the narrow
`BeforeEachContext` and `AfterEachContext` surfaces.

The lifecycle machinery (`litmus.lifecycle`) is deliberately absent. The child
boundary, its slots, and its policies are the framework's internal organs — a suite
author never holds an instance of any of them, so they do not belong in the outward
API. The framework and its self-tests import them from `litmus.lifecycle` directly.
"""

from litmus.artifacts import Artifact
from litmus.base import LitmusTest
from litmus.errors import (
    ContractViolation,
    ExternalIntegrationFailure,
    Failure,
    FrameworkContractViolation,
    LitmusError,
    MissingVerdict,
    TestFailure,
)
from litmus.executor import ExecResult
from litmus.hooks import AfterEachContext, AfterEachHook, BeforeEachContext, BeforeEachHook
from litmus.results import RunResult, SummaryTreeNode
from litmus.utils import TestUtils
from litmus.verdict import PASS, Verdict

__all__ = [
    "PASS",
    "AfterEachContext",
    "AfterEachHook",
    "Artifact",
    "BeforeEachContext",
    "BeforeEachHook",
    "ContractViolation",
    "ExecResult",
    "ExternalIntegrationFailure",
    "Failure",
    "FrameworkContractViolation",
    "LitmusError",
    "LitmusTest",
    "MissingVerdict",
    "RunResult",
    "SummaryTreeNode",
    "TestFailure",
    "TestUtils",
    "Verdict",
]
