"""The import-cycle gate: red on a runtime cycle, green with a report on an annotation-only one.

Seeded packages are written to a temporary directory so the contract is pinned against
a graph the test controls, the way ``tools/coverage_floor.py`` is exercised against a
report it writes itself; the real package is checked once at the end so the gate's
verdict on ``main`` is part of the suite.
"""

from __future__ import annotations

import io
from pathlib import Path

from tools.import_cycles import EXIT_CLEAN, EXIT_CYCLE, PACKAGE, ImportGraph, main

GUARDED_IMPORT = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pkg.{} import X\n"


def _seed(root: Path, name: str, modules: dict[str, str]) -> Path:
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for module, source in modules.items():
        (package / f"{module}.py").write_text(source, encoding="utf-8")
    return package


class TestImportGraphEdges:
    def test_every_import_spelling_is_an_edge(self, tmp_path: Path) -> None:
        package = _seed(
            tmp_path,
            "pkg",
            {
                "a": "from pkg.b import thing\n",
                "b": "import pkg.c\n",
                "c": "from pkg import d\n",
                "d": "from .e import thing\n",
                "e": "from . import f\n",
                "f": "import os\nfrom other.pkg import a\n",
            },
        )
        graph = ImportGraph.from_package(package)

        assert graph.modules == ("__init__", "a", "b", "c", "d", "e", "f")
        assert graph.runtime_cycles() == ()
        assert graph.annotation_cycles() == ()

    def test_type_checking_guard_bare_or_qualified_is_an_annotation_edge(
        self, tmp_path: Path
    ) -> None:
        package = _seed(
            tmp_path,
            "pkg",
            {
                "a": GUARDED_IMPORT.format("b"),
                "b": "import typing\nif typing.TYPE_CHECKING:\n    from pkg.a import A\n",
            },
        )
        graph = ImportGraph.from_package(package)

        assert graph.runtime_cycles() == ()
        assert graph.annotation_cycles() == (("a", "b"),)

    def test_imports_inside_functions_classes_and_try_blocks_still_count(
        self, tmp_path: Path
    ) -> None:
        package = _seed(
            tmp_path,
            "pkg",
            {
                "a": "def f():\n    from pkg.b import B\n",
                "b": "class C:\n    import pkg.c\n",
                "c": "try:\n    from pkg.d import D\nexcept ImportError:\n    import pkg.a\n",
                "d": "with open('x') as fh:\n    from pkg.a import A\n",
            },
        )
        graph = ImportGraph.from_package(package)

        assert graph.runtime_cycles() == (("a", "b", "c", "d"),)

    def test_else_branch_of_the_guard_is_runtime(self, tmp_path: Path) -> None:
        package = _seed(
            tmp_path,
            "pkg",
            {
                "a": (
                    "from typing import TYPE_CHECKING\n"
                    "if TYPE_CHECKING:\n    pass\nelse:\n    from pkg.b import B\n"
                ),
                "b": "from pkg.a import A\n",
            },
        )

        assert ImportGraph.from_package(package).runtime_cycles() == (("a", "b"),)


class TestImportGraphVerdict:
    def test_runtime_cycle_is_red_and_named(self, tmp_path: Path) -> None:
        package = _seed(
            tmp_path,
            "pkg",
            {
                "a": "from pkg.b import B\n",
                "b": "from pkg.c import C\n",
                "c": "from pkg.a import A\n",
                "lone": "import os\n",
            },
        )
        out = io.StringIO()

        status = ImportGraph.from_package(package).report(out)

        assert status == EXIT_CYCLE
        lines = out.getvalue().splitlines()
        assert lines[0] == "import cycles: 5 modules, 1 runtime, 0 annotation-only"
        assert lines[1] == "  RUNTIME CYCLE among: a, b, c"
        assert lines[-1].startswith("runtime import cycle: break it")

    def test_reaching_a_root_reexport_is_an_edge_to_the_package_root(self, tmp_path: Path) -> None:
        package = _seed(tmp_path, "pkg", {"a": "from pkg import PUBLIC_NAME\n"})
        (package / "__init__.py").write_text("from pkg.a import A\n", encoding="utf-8")

        assert ImportGraph.from_package(package).runtime_cycles() == (("__init__", "a"),)

    def test_self_import_is_a_runtime_cycle(self, tmp_path: Path) -> None:
        package = _seed(tmp_path, "pkg", {"a": "import pkg.a\n"})

        assert ImportGraph.from_package(package).runtime_cycles() == (("a",),)

    def test_annotation_only_cycle_is_green_and_listed(self, tmp_path: Path) -> None:
        package = _seed(
            tmp_path,
            "pkg",
            {
                "a": "from pkg.b import B\n",
                "b": GUARDED_IMPORT.format("a"),
            },
        )
        out = io.StringIO()

        status = ImportGraph.from_package(package).report(out)

        assert status == EXIT_CLEAN
        assert out.getvalue().splitlines() == [
            "import cycles: 3 modules, 0 runtime, 1 annotation-only",
            "  annotation-only, permitted (STANDARDS §3): a, b",
        ]

    def test_a_runtime_cycle_is_not_double_counted_as_annotation(self, tmp_path: Path) -> None:
        package = _seed(
            tmp_path,
            "pkg",
            {
                "a": "from pkg.b import B\nfrom typing import TYPE_CHECKING\n",
                "b": "from pkg.a import A\n",
            },
        )
        graph = ImportGraph.from_package(package)

        assert graph.runtime_cycles() == (("a", "b"),)
        assert graph.annotation_cycles() == ()


class TestImportCyclesAtTheCommandLine:
    def test_main_takes_the_package_path(self, tmp_path: Path) -> None:
        package = _seed(
            tmp_path, "pkg", {"a": "from pkg.b import B\n", "b": "from pkg.a import A\n"}
        )

        assert main([str(package)]) == EXIT_CYCLE

    def test_the_shipped_package_has_no_runtime_cycle(self) -> None:
        graph = ImportGraph.from_package(PACKAGE)

        assert graph.runtime_cycles() == ()
        # The one annotation-only cycle recorded on #120; breaking it is out of scope.
        assert graph.annotation_cycles() == (("arp", "measurement", "utils"),)
