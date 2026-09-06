"""Generated composition trees checked against a pure terminal-state model (#64)."""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Literal, cast

from hypothesis import example, given, settings
from hypothesis import strategies as st

from litmus import (
    PASS,
    AfterEachContext,
    BeforeEachContext,
    ContractViolation,
    ExternalIntegrationFailure,
    LitmusTest,
    MissingVerdict,
    RunResult,
    TestFailure,
    TestUtils,
    Verdict,
)
from litmus.lifecycle import State
from litmus.results import NodeType, SummaryTreeNode

type _Path = tuple[str, ...]
type _Kind = Literal["test", "sequence", "parallel"]
type _Verdict = Literal["pass", "fail", "bug", "missing", "hook"]

_OUTCOMES: tuple[_Verdict, ...] = ("pass", "fail", "bug", "missing")
_TYPES = {
    "test": NodeType.TEST,
    "sequence": NodeType.SEQUENCE,
    "parallel": NodeType.PARALLEL,
}
_STATES = {
    "pass": State.PASS,
    "fail": State.FAIL,
    "bug": State.FAIL,
    "missing": State.ABORTED,
    "hook": State.ABORTED,
}
_SEVERITY = {State.PASS: 0, State.FAIL: 1, State.ABORTED: 2}
_PROPERTY = settings(max_examples=60, database=None, deadline=None)


@dataclass(frozen=True)
class _Tree:
    kind: _Kind = "test"
    verdict: _Verdict = "pass"
    children: tuple[_Tree, ...] = ()


@dataclass(frozen=True)
class _Expected:
    state: State | None
    reported_leaf: _Path | None = None


def _trees(depth: int = 3, outcomes: tuple[_Verdict, ...] = _OUTCOMES) -> st.SearchStrategy[_Tree]:
    leaf = st.builds(_Tree, verdict=st.sampled_from(outcomes))
    if depth == 0:
        return leaf
    return leaf | st.builds(
        _Tree,
        kind=st.sampled_from(("sequence", "parallel")),
        children=st.lists(_trees(depth - 1, outcomes), min_size=1, max_size=3).map(tuple),
    )


def _walk(tree: _Tree, path: _Path = ("root",)) -> Iterator[tuple[_Path, _Tree]]:
    yield path, tree
    for index, child in enumerate(tree.children):
        yield from _walk(child, (*path, f"node-{index}"))


def _mark_hook(tree: _Tree, target: _Path, path: _Path = ("root",)) -> _Tree:
    if path == target:
        return replace(tree, verdict="hook")
    return replace(
        tree,
        children=tuple(
            _mark_hook(child, target, (*path, f"node-{index}"))
            for index, child in enumerate(tree.children)
        ),
    )


@st.composite
def _hook_trees(draw: st.DrawFn) -> _Tree:
    # One reachable hook owns the halt, so expected propagation does not depend
    # on which of several concurrent failures the event loop happens to deliver.
    tree = draw(_trees(outcomes=("pass",)))
    leaves = [path for path, spec in _walk(tree) if spec.kind == "test"]
    return _mark_hook(tree, draw(st.sampled_from(leaves)))


def _fold(tree: _Tree, path: _Path = ("root",)) -> dict[_Path, _Expected]:
    """Evaluate declarations alone; no lifecycle objects or observed results enter here."""
    if tree.kind == "test":
        state = _STATES[tree.verdict]
        return {path: _Expected(state, None if state is State.PASS else path)}
    folded: dict[_Path, _Expected] = {}
    children: list[_Expected] = []
    for index, child in enumerate(tree.children):
        child_path = (*path, f"node-{index}")
        if tree.kind == "sequence" and children and children[-1].state is not State.PASS:
            folded.update((p, _Expected(None)) for p, _ in _walk(child, child_path))
        else:
            subtree = _fold(child, child_path)
            folded.update(subtree)
            children.append(subtree[child_path])
    folded[path] = max(children, key=lambda child: _SEVERITY[cast(State, child.state)])
    return folded


class _Execution:
    """Mutable observations owned by one generated run, never by a pytest fixture."""

    def __init__(self, hook_path: _Path | None = None, hook_phase: str = "before") -> None:
        self.hook_path = hook_path
        self.hook_phase = hook_phase
        self.cause = RuntimeError("generated shop-floor integration failure")
        self.started: list[_Path] = []
        self.results: dict[_Path, RunResult] = {}
        self.calls: list[_Path] = []

    async def before(self, ctx: BeforeEachContext) -> None:
        """Record starts and inject one selected integration failure."""
        path = tuple(ctx.ancestor_names)
        self.started.append(path)
        if path == self.hook_path and self.hook_phase == "before":
            raise self.cause

    async def after(self, ctx: AfterEachContext) -> None:
        """Capture each result at the public integration boundary."""
        path = tuple(ctx.ancestor_names)
        assert path not in self.results
        self.results[path] = ctx.result
        if path == self.hook_path and self.hook_phase == "after":
            raise self.cause

    def compile(self, tree: _Tree, owner: LitmusTest, path: _Path = ("root",)) -> LitmusTest:
        """Build a fresh framework tree from the independent declaration."""
        if tree.kind == "test":

            async def claim(utils: TestUtils) -> Verdict:
                self.calls.append(path)
                if tree.verdict == "pass" or (
                    tree.verdict == "hook" and self.hook_phase == "after"
                ):
                    return PASS
                if tree.verdict == "fail":
                    raise TestFailure("generated hardware mismatch", expected=1, received=0)
                if tree.verdict == "bug":
                    raise ValueError("generated unmodeled exception")
                if tree.verdict == "missing":
                    return cast("Verdict", None)
                raise AssertionError("an aborted hook must prevent its test method")

            return owner.test(path[-1], claim)
        children = [
            self.compile(child, owner, (*path, f"node-{index}"))
            for index, child in enumerate(tree.children)
        ]
        factory = owner.sequence if tree.kind == "sequence" else owner.parallel
        return factory(path[-1], children, discrete_log_file=False)


def _summaries(
    node: SummaryTreeNode,
    path: _Path = (),
) -> dict[_Path, SummaryTreeNode]:
    path = (*path, node.name)
    nodes: dict[_Path, SummaryTreeNode] = {path: node}
    for child in node.children:
        nodes.update(_summaries(child, path))
    return nodes


def _assert_evidence(tree: _Tree, result: RunResult, execution: _Execution) -> None:
    declarations = dict(_walk(tree))
    summaries = _summaries(result.summary_tree)
    assert summaries.keys() == declarations.keys()
    assert len(summaries) == len(declarations)
    assert len(execution.started) == len(set(execution.started))
    assert set(execution.started) == execution.results.keys(), (
        execution.started,
        list(execution.results),
    )
    for path, summary in summaries.items():
        assert summary.type is _TYPES[declarations[path].kind]
        if summary.state is None:
            assert path not in execution.results
            assert summary.failure is None
            assert summary.log_path is None
            continue
        recorded = execution.results[path]
        assert recorded.ancestor_names == list(path)
        assert recorded.bread_crumbs == "/".join(path)
        if path == execution.hook_path and execution.hook_phase == "after":
            # The hook receives the verdict it subsequently invalidates.
            assert recorded.state is State.PASS
            assert recorded.failure is None
            assert summary.state is State.ABORTED
        else:
            assert recorded.summary_tree == summary
            assert recorded.state is summary.state
        payload = json.loads(json.dumps(recorded.to_json()))
        assert payload["ancestor_names"] == list(path)
        assert payload["bread_crumbs"] == "/".join(path)
        assert payload["summary_tree"] == recorded.summary_tree.to_json()


class _LocalContextSuite(LitmusTest):
    cleanup_owners: ClassVar[dict[tuple[str, ...], str]] = {}

    def __init__(self, tree: _Tree, path: _Path, contexts: dict[_Path, Mapping[str, Any]]) -> None:
        super().__init__(name=path[-1], discrete_log_file=False)
        self.tree = tree
        self.path = path
        self.contexts = contexts

    async def configure(self, utils: TestUtils) -> dict[str, object]:
        assert utils.ctx == {}
        self.contexts[self.path] = utils.ctx
        return {"owner": "/".join(self.path), "visited": []}

    def build_suite(self) -> LitmusTest:
        if self.tree.kind == "test":
            return self.test("claim", self._claim)
        children = [
            _LocalContextSuite(child, (*self.path, f"node-{index}"), self.contexts)
            for index, child in enumerate(self.tree.children)
        ]
        factory = self.sequence if self.tree.kind == "sequence" else self.parallel
        return factory("children", children, discrete_log_file=False)

    async def _claim(self, utils: TestUtils) -> Verdict:
        assert utils.ctx is self.contexts[self.path]
        assert utils.ctx["owner"] == "/".join(self.path)
        utils.ctx["visited"].append("claim")
        if utils.ctx["visited"] == ["claim"]:
            return PASS
        raise TestFailure("generated claim executed more than once")

    async def cleanup(self, utils: TestUtils) -> None:
        # Cleanup exceptions are swallowed by contract; inspect these values outside
        # the lifecycle so a failed assertion cannot be mistaken for a passing test.
        self.cleanup_owners[self.path] = str(utils.ctx["owner"])


class TestCompositionProperties:
    @_PROPERTY
    @given(tree=_trees())
    @example(tree=_Tree(verdict="pass"))
    @example(tree=_Tree(verdict="fail"))
    @example(tree=_Tree(verdict="bug"))
    @example(tree=_Tree(verdict="missing"))
    @example(tree=_Tree("sequence", children=(_Tree(verdict="fail"), _Tree())))
    @example(
        tree=_Tree(
            "parallel",
            children=(
                _Tree(verdict="fail"),
                _Tree(verdict="missing"),
                _Tree(verdict="bug"),
                _Tree(),
            ),
        )
    )
    async def test_terminal_algebra_and_evidence(self, tree: _Tree) -> None:
        expected = _fold(tree)
        execution = _Execution()
        node = execution.compile(tree, LitmusTest(name="author"))
        result = await node.run(before_each=execution.before, after_each=execution.after)

        _assert_evidence(tree, result, execution)
        summaries = _summaries(result.summary_tree)
        assert {p: n.state for p, n in summaries.items()} == {
            p: value.state for p, value in expected.items()
        }
        executed_leaves = {
            path
            for path, spec in _walk(tree)
            if spec.kind == "test" and expected[path].state is not None
        }
        assert set(execution.calls) == executed_leaves
        assert len(execution.calls) == len(executed_leaves)
        for path, value in expected.items():
            failure = summaries[path].failure
            if value.reported_leaf is None:
                assert failure is None
                continue
            assert failure is summaries[value.reported_leaf].failure
            verdict = dict(_walk(tree))[value.reported_leaf].verdict
            if verdict == "fail":
                assert type(failure) is TestFailure
                assert not failure.is_bug
            elif verdict == "bug":
                assert isinstance(failure, ContractViolation)
                assert isinstance(failure.cause, ValueError)
                assert failure.is_bug
            else:
                assert isinstance(failure, MissingVerdict)

    @_PROPERTY
    @given(tree=_hook_trees(), phase=st.sampled_from(("before", "after")))
    @example(tree=_Tree("parallel", children=(_Tree(verdict="hook"), _Tree())), phase="before")
    @example(tree=_Tree("sequence", children=(_Tree(verdict="hook"), _Tree())), phase="after")
    async def test_hook_abort_reaches_every_ancestor(self, tree: _Tree, phase: str) -> None:
        hook_path = next(path for path, spec in _walk(tree) if spec.verdict == "hook")
        execution = _Execution(hook_path, phase)
        node = execution.compile(tree, LitmusTest(name="author"))
        result = await node.run(before_each=execution.before, after_each=execution.after)

        _assert_evidence(tree, result, execution)
        assert result.state is _fold(tree)[("root",)].state is State.ABORTED
        assert isinstance(result.failure, ExternalIntegrationFailure)
        assert result.failure.cause is execution.cause
        assert (hook_path in execution.calls) == (phase == "after")
        summaries = _summaries(result.summary_tree)
        for depth in range(1, len(hook_path) + 1):
            ancestor = summaries[hook_path[:depth]]
            assert ancestor.state is State.ABORTED
            assert ancestor.failure is result.failure
        for summary in summaries.values():
            assert summary.state in (None, State.PASS, State.ABORTED)
            if summary.state is State.ABORTED:
                assert summary.failure is result.failure

    @_PROPERTY
    @given(tree=_trees(outcomes=("pass",)))
    async def test_context_stays_with_each_authored_instance(self, tree: _Tree) -> None:
        contexts: dict[_Path, Mapping[str, Any]] = {}
        _LocalContextSuite.cleanup_owners.clear()
        result = await _LocalContextSuite(tree, ("root",), contexts).run()

        assert result.state is State.PASS
        assert contexts.keys() == dict(_walk(tree)).keys()
        assert len({id(ctx) for ctx in contexts.values()}) == len(contexts)
        for path, spec in _walk(tree):
            assert contexts[path] == {
                "owner": "/".join(path),
                "visited": ["claim"] if spec.kind == "test" else [],
            }
            assert _LocalContextSuite.cleanup_owners[path] == "/".join(path)
        # Inline nodes share their authored owner's dict; siblings and descendants
        # must not share even a nested mutable value across that instance boundary.
        contexts[("root",)]["visited"].append("after run")
        for path, ctx in contexts.items():
            if path != ("root",):
                assert "after run" not in ctx["visited"]
        assert "after run" not in cast(list[str], result.run_data["visited"])
