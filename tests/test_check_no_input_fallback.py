"""``litmus/no-input-fallback``: one source per ``configure()`` value, identity or discovery.

Ruled on issue #142 and shipped in 1.0.0 by the ruling on issue #144 (issue #147). A
``self`` attribute and a discovery utility may not stand as alternatives for one value
inside ``configure()``: not through ``or``, not through a conditional expression, and not
through a local name bound from each by a different assignment. Identity fed *into*
discovery and identity read from ``utils.env`` are the blessed shapes and stay quiet.
"""

import io
from pathlib import Path
from textwrap import dedent

import pytest

from litmus_check import SuiteModule, Violation
from litmus_check.cli import EXIT_VIOLATIONS, LitmusCheckCommand
from litmus_check.rules.no_input_fallback import NoInputFallbackRule

CODE = "litmus/no-input-fallback"
TAIL = "the node names what it needs"


def _check(source: str) -> list[Violation]:
    module = SuiteModule(Path("suite.py"), dedent(source))
    return sorted(NoInputFallbackRule().check(module))


def _lines(source: str) -> list[int]:
    return [violation.line for violation in _check(source)]


class TestNoInputFallbackFiresOnChoices:
    def test_the_idiom_example_inline_in_the_returned_dict(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"ip": self._ip or await utils.get_arp_resolver(mac=self._mac).resolve()}
        """
        violations = _check(source)

        assert [(v.line, v.column, v.code) for v in violations] == [(4, 23, CODE)]
        assert violations[0].message.startswith("self._ip falls back to utils.get_arp_resolver(): ")
        assert TAIL in violations[0].message
        assert "issue #142" in violations[0].message

    def test_either_order_and_a_conditional_expression(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    shell = await utils.get_shell(host="bench") or self._shell
                    ip = self._ip if self._ip else await utils.get_arp_resolver(mac=m).resolve()
                    http = (await utils.get_http(base_url=self._url)) if self._url else self._client
                    return {"shell": shell, "ip": ip, "http": http}
        """
        violations = _check(source)

        assert [v.line for v in violations] == [4, 5, 6]
        assert violations[0].message.startswith("self._shell falls back to utils.get_shell()")
        assert violations[1].message.startswith("self._ip falls back to utils.get_arp_resolver()")
        assert violations[2].message.startswith("self._client falls back to utils.get_http()")

    @pytest.mark.parametrize(
        "utility",
        [
            "get_arp_resolver",
            "use_shell",
            "get_shell",
            "use_serial",
            "get_serial",
            "get_http",
            "get_coap",
        ],
    )
    def test_every_discovery_utility_is_known(self, utility: str) -> None:
        source = f"""
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {{"value": self._value or await utils.{utility}()}}
        """
        violations = _check(source)

        assert [v.line for v in violations] == [4]
        assert f"utils.{utility}()" in violations[0].message

    def test_receiver_and_utils_are_read_from_the_signature(self) -> None:
        source = """
            class Suite(LitmusTest):
                def configure(this, u: TestUtils) -> dict[str, Any]:
                    return {"shell": this._shell or u.get_shell()}
        """
        violations = _check(source)

        assert [v.line for v in violations] == [4]
        assert violations[0].message.startswith("this._shell falls back to u.get_shell()")

    def test_a_chain_off_the_discovery_result_and_through_local_names(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    resolved = await utils.get_arp_resolver(mac=self._mac).resolve()
                    fallback = resolved["ip"]
                    ip = self._ip or fallback
                    host = (await utils.get_shell(host="x")).host
                    return {"ip": ip, "host": self._cfg.host or host}
        """
        violations = _check(source)

        assert [v.line for v in violations] == [6, 8]
        assert violations[0].message.startswith("self._ip falls back to utils.get_arp_resolver()")
        assert violations[1].message.startswith("self._cfg falls back to utils.get_shell()")

    def test_a_walrus_is_reported_once(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"api": (api := self._api or await utils.get_coap(host=self._host))}
        """
        assert _lines(source) == [4]


class TestNoInputFallbackFiresOnBindings:
    def test_if_else_branches_bind_the_same_name(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    if self._ip is None:
                        ip = (await utils.get_arp_resolver(mac=self._mac).resolve())["ip"]
                    else:
                        ip = self._ip
                    return {"ip": ip}
        """
        violations = _check(source)

        assert [(v.line, v.column) for v in violations] == [(7, 13)]
        assert violations[0].message.startswith(
            '"ip" is bound from self._ip and from utils.get_arp_resolver(): '
        )
        assert TAIL in violations[0].message

    def test_try_except_and_a_none_check_reassignment(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    try:
                        shell = await utils.get_shell(host=self._host)
                    except OSError:
                        shell = self._shell
                    ip: str | None = self._ip
                    if ip is None:
                        ip = (await utils.get_arp_resolver(mac=self._mac).resolve())["ip"]
                    return {"shell": shell, "ip": ip}
        """
        violations = _check(source)

        assert [v.line for v in violations] == [7, 10]
        assert (
            '"shell" is bound from self._shell and from utils.get_shell()' in violations[0].message
        )
        assert (
            '"ip" is bound from self._ip and from utils.get_arp_resolver()' in violations[1].message
        )

    def test_a_self_referencing_rebind_is_reported_once_as_the_choice(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    ip = (await utils.get_arp_resolver(mac=self._mac).resolve())["ip"]
                    ip = ip or self._ip
                    return {"ip": ip}
        """
        violations = _check(source)

        assert [v.line for v in violations] == [5]
        assert "falls back to" in violations[0].message

    def test_an_alias_of_a_reported_name_is_not_reported_again(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    ip = self._ip
                    ip = (await utils.get_arp_resolver(mac=self._mac).resolve())["ip"]
                    address = ip
                    return {"ip": address}
        """
        assert _lines(source) == [5]


class TestNoInputFallbackStaysQuiet:
    def test_identity_fed_into_discovery(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    ip = self._ip
                    shell = await utils.get_shell(host=ip, username=self._user)
                    resolver = utils.get_arp_resolver(mac=self._mac)
                    return {"shell": shell, "ip": (await resolver.resolve())["ip"]}
        """
        assert _lines(source) == []

    def test_identity_from_the_environment_is_the_declared_input(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    mac = self._mac or utils.env.require("DUT_MAC")
                    host = self._host if self._host else utils.env["DUT_HOST"]
                    return {"mac": mac, "host": host, "ctx": self._x or utils.ctx["x"]}
        """
        assert _lines(source) == []

    def test_two_identities_two_discoveries_and_a_helper_call(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    ip = self._ip or self._default_ip
                    shell = await utils.get_shell() or await utils.get_http()
                    probed = self._probe() or await utils.get_shell()
                    api = self._api and await utils.get_coap()
                    bare = self._shell or utils.get_shell
                    other = self._ip or await resolver.resolve()
                    return {"ip": ip, "shell": shell, "p": probed, "a": api, "b": bare, "o": other}
        """
        assert _lines(source) == []

    def test_the_same_shapes_outside_configure(self) -> None:
        source = """
            class Suite(LitmusTest):
                def __init__(self, *, name: str, ip: str | None = None) -> None:
                    super().__init__(name=name)
                    self._ip = ip or self._default
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    async def resolve() -> str:
                        return self._ip or (await utils.get_arp_resolver(mac=m).resolve())["ip"]
                    return {"ip": await resolve()}
                async def cleanup(self, utils: TestUtils) -> None:
                    shell = self._shell or await utils.get_shell()
                    await shell.close()
                async def _claim(self, utils: TestUtils) -> Verdict:
                    if self._ip or await utils.get_shell():
                        return PASS
                    raise TestFailure("no", "ip", "none")
            class Helper:
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    return {"ip": self._ip or await utils.get_shell()}
        """
        assert _lines(source) == []

    def test_configure_without_the_two_parameter_shape_or_absent(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self) -> dict[str, Any]:
                    return {"ip": self._ip or await utils.get_shell()}
            class Other(LitmusTest):
                def build_suite(self) -> LitmusTest:
                    return self.test("x", self._claim)
        """
        assert _lines(source) == []

    def test_unrelated_and_same_kind_rebindings(self) -> None:
        source = """
            class Suite(LitmusTest):
                async def configure(self, utils: TestUtils) -> dict[str, Any]:
                    ip = self._ip
                    ip = self._ip.strip()
                    shell = await utils.get_shell(host=ip)
                    shell = await utils.get_http(base_url=ip)
                    a, b = self._ip, await utils.get_coap()
                    pending: str
                    return {"ip": ip, "shell": shell, "a": a, "b": b}
        """
        assert _lines(source) == []


class TestNoInputFallbackAtTheCommandLine:
    def test_seeded_fixture_is_caught_and_examples_are_clean(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "litmus_check"
        out = io.StringIO()

        status = LitmusCheckCommand(stdout=out, stderr=io.StringIO()).run(
            ["--select", CODE, str(fixtures)]
        )

        assert status == EXIT_VIOLATIONS
        lines = out.getvalue().splitlines()
        assert lines[0].startswith(f"{fixtures / 'seeded_suite.py'}:85:14: {CODE} ")
        assert lines[-1] == "Found 1 violation."
