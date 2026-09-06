"""litmus — React-for-hardware HIL testing framework.

`__all__` below is the public surface, and `docs/api-stability.md` is the policy that
governs it: from 1.0.0 every name listed here is a promise, and nothing outside the
list is. Import public names from `litmus` itself; the module a name is defined in is
an implementation detail and may move.

Suite authors need `LitmusTest`, `TestUtils`, `Shell`, `ExecResult`, `Artifact`,
`JsonObject`, `PASS`, `Verdict`, `TestFailure`, and `ArpResolver`. The rest of the
failure taxonomy is exported because it appears in artifacts and logs, where a reader
will follow a name back to its meaning (`ExternalIntegrationFailure`,
`OperatorInterrupt`, `ArpResolutionFailure`). The record types (`RunResult`,
`SummaryTreeNode`, `State`, `NodeType`, `Slot`, `TimeoutEvidence`, `RunSignal`,
`ScopedLogger`) are exported because a public object has no attribute of a private
type. `Phase` names the survey dispatch points for `use_survey(phases=)`. Harness
integrators import the narrow `BeforeEachContext` and `AfterEachContext` surfaces.

The lifecycle machinery (`litmus.lifecycle`) is deliberately absent. The child
boundary, its slot policies, and the run-abort signal are the framework's internal
organs — a suite author never holds an instance of any of them, so they do not belong
in the outward API. The transports (`RemoteInterface`, `SshInterface`) and the JUnit
reporter stay off the surface for the same reason: a suite author never constructs
one; `use_shell` and `get_shell` hand back a `Shell`, and the runner (#73) will own
the reporter. The framework and its self-tests import all of these from their modules
directly.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

from litmus.arp import ArpResolver
from litmus.artifacts import Artifact
from litmus.base import LitmusTest
from litmus.errors import (
    ArpResolutionFailure,
    ContractViolation,
    ExternalIntegrationFailure,
    Failure,
    FrameworkContractViolation,
    LitmusError,
    MissingVerdict,
    OperatorInterrupt,
    TestFailure,
)
from litmus.evidence import JsonObject
from litmus.executor import ExecResult
from litmus.hooks import AfterEachContext, AfterEachHook, BeforeEachContext, BeforeEachHook
from litmus.lifecycle import Phase, Slot, State, TimeoutEvidence
from litmus.results import NodeType, RunResult, SummaryTreeNode
from litmus.routing import Shell
from litmus.utils import RunSignal, ScopedLogger, TestUtils
from litmus.verdict import PASS, Verdict

try:
    __version__ = _distribution_version("litmus")
except PackageNotFoundError:  # pragma: no cover - a source checkout that was never installed
    __version__ = "0.0.0+unknown"

__all__ = [
    "PASS",
    "AfterEachContext",
    "AfterEachHook",
    "ArpResolutionFailure",
    "ArpResolver",
    "Artifact",
    "BeforeEachContext",
    "BeforeEachHook",
    "ContractViolation",
    "ExecResult",
    "ExternalIntegrationFailure",
    "Failure",
    "FrameworkContractViolation",
    "JsonObject",
    "LitmusError",
    "LitmusTest",
    "MissingVerdict",
    "NodeType",
    "OperatorInterrupt",
    "Phase",
    "RunResult",
    "RunSignal",
    "ScopedLogger",
    "Shell",
    "Slot",
    "State",
    "SummaryTreeNode",
    "TestFailure",
    "TestUtils",
    "TimeoutEvidence",
    "Verdict",
]
