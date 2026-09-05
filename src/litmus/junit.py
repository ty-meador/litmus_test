"""JUnit XML reporter over `RunResult`.

Not on the public package surface. The runner (`litmus run --junit PATH`, #73) will
call `JunitXml.write`. Self-tests and harness adapters import this module directly.

Mapping (the three terminal states, plus unreached):
  - `Pass` → a `testcase` with neither `failure` nor `error`
  - `Fail` → `failure`; `@type` is the taxonomy class, body is `failure.to_json()`
  - `Aborted` → `error`; same taxonomy payload. Aborted is not a flavor of Fail.
  - `state is None` → `skipped` (declared, never ran — a stopped sequence step)

Leaves are testcases. Groups, sequences, and suites become `classname` (the
root-to-parent path). A composite that settled before any child ran (configure
failure, abort during bring-up) is itself a testcase so CI cannot report skip
for a Fail. Artifacts appear as `system-out` pointers (`timeline: …`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import final
from xml.etree.ElementTree import Element, SubElement, indent, tostring

from litmus.lifecycle import State
from litmus.results import RunResult, SummaryTreeNode

type _Case = tuple[str, str, SummaryTreeNode, str | None]


@final
class JunitXml:
    """Render one `RunResult` as Jenkins-compatible JUnit XML.

    The mapping is the contract. Changing which element carries Fail versus
    Aborted is a reporter bug, not a style choice.
    """

    def render(self, result: RunResult) -> str:
        """Return well-formed JUnit XML for `result`, including the XML declaration."""
        cases = _iter_cases(result.summary_tree, (), None)
        failures = sum(1 for _name, _cls, node, _log in cases if node.state is State.FAIL)
        errors = sum(1 for _name, _cls, node, _log in cases if node.state is State.ABORTED)
        skipped = sum(1 for _name, _cls, node, _log in cases if node.state is None)
        testsuites = Element(
            "testsuites",
            {
                "name": result.node,
                "tests": str(len(cases)),
                "failures": str(failures),
                "errors": str(errors),
                "skipped": str(skipped),
            },
        )
        testsuite = SubElement(
            testsuites,
            "testsuite",
            {
                "name": result.node,
                "tests": str(len(cases)),
                "failures": str(failures),
                "errors": str(errors),
                "skipped": str(skipped),
            },
        )
        for name, classname, node, log_path in cases:
            testsuite.append(_testcase(name, classname, node, log_path))
        indent(testsuites)
        body = tostring(testsuites, encoding="unicode")
        return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'

    def write(self, result: RunResult, path: Path) -> None:
        """Write the JUnit XML for `result` to `path` (UTF-8)."""
        path.write_text(self.render(result), encoding="utf-8")


def _iter_cases(
    node: SummaryTreeNode,
    ancestors: tuple[str, ...],
    nearest_log: str | None,
) -> list[_Case]:
    """Walk the summary tree into the testcases the reporter emits."""
    log_path = node.log_path if node.log_path is not None else nearest_log
    children = node.children
    ran = any(child.state is not None for child in children)
    classname = "/".join(ancestors) if ancestors else node.name
    cases: list[_Case] = []
    if not children or (node.state is not None and not ran):
        cases.append((node.name, classname, node, log_path))
    lineage = (*ancestors, node.name)
    for child in children:
        cases.extend(_iter_cases(child, lineage, log_path))
    return cases


def _testcase(
    name: str,
    classname: str,
    node: SummaryTreeNode,
    log_path: str | None,
) -> Element:
    case = Element("testcase", {"name": name, "classname": classname})
    if node.state is None:
        SubElement(case, "skipped")
    elif node.state is State.FAIL:
        case.append(_taxonomy_element("failure", node))
    elif node.state is State.ABORTED:
        case.append(_taxonomy_element("error", node))
    pointer = _artifact_pointer(node, log_path)
    if pointer is not None:
        system_out = SubElement(case, "system-out")
        system_out.text = pointer
    return case


def _taxonomy_element(tag: str, node: SummaryTreeNode) -> Element:
    failure = node.failure
    if failure is None:
        state = node.state
        assert state is not None
        element = Element(tag, {"type": state.value})
        if node.timeout is not None:
            element.text = json.dumps(node.timeout.to_json(), indent=2, sort_keys=True)
        return element
    payload = failure.to_json()
    element = Element(
        tag,
        {
            "type": str(payload["name"]),
            "message": str(payload["fail_message"]),
        },
    )
    element.text = json.dumps(payload, indent=2, sort_keys=True)
    return element


def _artifact_pointer(node: SummaryTreeNode, log_path: str | None) -> str | None:
    lines: list[str] = []
    if log_path is not None:
        lines.append(f"timeline: {log_path}")
    if node.timeout is not None:
        timeout = node.timeout
        lines.append(
            f"timeout: {timeout.declared} budget_seconds={timeout.budget_seconds} "
            f"elapsed_seconds={timeout.elapsed_seconds} clock={timeout.clock}"
        )
    if not lines:
        return None
    return "\n".join(lines)
