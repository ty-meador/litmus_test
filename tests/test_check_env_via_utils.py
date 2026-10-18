"""``litmus/env-via-utils``: the environment is read through ``utils.env``.

``utils.env`` is the snapshot ``RunResult`` records and a self-test injects. A read
through ``os`` is outside both, so the run cannot be reproduced from its evidence.
"""

import io
from pathlib import Path
from textwrap import dedent

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.env_via_utils import EnvViaUtilsRule

CODE = "litmus/env-via-utils"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(EnvViaUtilsRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestEnvViaUtilsFire:
    def test_os_environ_subscript_names_utils_env(self) -> None:
        source = """
            import os

            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"host": os.environ["HOST"]}
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(6, 25, CODE)]
        assert "os.environ" in violations[0].message
        assert "utils.env" in violations[0].message

    def test_environ_get_and_getenv_are_both_caught(self) -> None:
        source = """
            import os

            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    host = os.environ.get("HOST", "")
                    user = os.getenv("USER")
                    return {"host": host, "user": user}
        """
        assert _lines(source) == [6, 7]

    def test_bare_imports_and_aliases_are_caught(self) -> None:
        source = """
            import os as operating_system
            from os import environ, getenv as read_env

            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    a = operating_system.environ["A"]
                    b = environ["B"]
                    c = read_env("C")
                    return {"a": a, "b": b, "c": c}
        """
        assert _lines(source) == [7, 8, 9]

    def test_membership_test_is_a_read(self) -> None:
        source = """
            import os

            class Suite(LitmusTest):
                async def _leaf(self, utils: TestUtils) -> Verdict:
                    if "DEBUG" in os.environ:
                        return PASS
                    raise TestFailure("no", "yes", "no")
        """
        assert _lines(source) == [6]


class TestEnvViaUtilsStaySilent:
    def test_utils_env_is_the_idiom(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    host = utils.env.require("HOST")
                    port = utils.env.get("PORT", "22")
                    return {"host": host, "port": port}
        """
        assert _lines(source) == []

    def test_reads_outside_a_suite_class_are_the_harness_business(self) -> None:
        source = """
            import os

            ROOT = os.environ.get("LITMUS_ARTIFACT_ROOT")

            def main() -> None:
                print(os.getenv("STATION"))

            class Plain:
                def where(self) -> str:
                    return os.environ["HERE"]
        """
        assert _lines(source) == []

    def test_an_environ_attribute_on_another_object_is_not_os(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"snapshot": dict(utils.environ)}
        """
        assert _lines(source) == []


class TestEnvViaUtilsAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_clean_fixture_is_not(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:106:15: {CODE} ")
        assert lines[-1] == "Found 1 violation."
