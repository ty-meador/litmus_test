"""Credential containment: nothing secret reaches the evidence surface (#65).

`test_ssh.py` already pins that a password never lands in the asyncssh options
and that provenance names the credential *path*, never its value. This module
runs full suites against the loopback sshd and sweeps every evidence surface
the Floor Technician (or a customer) might be handed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import stat
from pathlib import Path
from typing import Any

import pytest
from tests.loopback_sshd import LoopbackSshd

from litmus import (
    PASS,
    Artifact,
    Failure,
    LitmusTest,
    RunResult,
    SummaryTreeNode,
    TestFailure,
    TestUtils,
    Verdict,
)
from litmus.lifecycle import State

_PASSWORD = "litmus-65-pw-e8c1f4b2a90d"
_AUTH_PATH = re.compile(rb"auth=(?:key-file|agent)\([^)]*\)")


def _strip_named_auth_paths(haystack: bytes) -> bytes:
    """Drop provenance's named credential *paths*. The path is not the secret."""
    return _AUTH_PATH.sub(b"auth=<path>", haystack)


def _artifact_bytes(root: Path) -> bytes:
    chunks: list[bytes] = []
    if not root.exists():
        return b""
    for path in sorted(root.rglob("*")):
        if path.is_file():
            chunks.append(path.read_bytes())
    return b"\n".join(chunks)


def _failure_texts(failure: Failure | None) -> list[str]:
    texts: list[str] = []
    seen: set[int] = set()

    def walk_exception(error: BaseException | None) -> None:
        current = error
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            texts.append(str(current))
            texts.append(repr(current))
            nested = getattr(current, "cause", None)
            if isinstance(nested, BaseException):
                current = nested
                continue
            current = current.__cause__ or current.__context__

    walk_exception(failure)
    if failure is not None:
        texts.append(json.dumps(failure.to_json(), ensure_ascii=False, default=str))
    return texts


def _walk_tree(node: SummaryTreeNode) -> list[str]:
    texts = _failure_texts(node.failure)
    for child in node.children:
        texts.extend(_walk_tree(child))
    return texts


def _evidence(
    result: RunResult,
    artifact_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> bytes:
    """Concatenate every surface #65 names into one haystack."""
    parts: list[bytes] = [
        _artifact_bytes(artifact_root),
        "\n".join(record.getMessage() for record in caplog.records).encode(),
        json.dumps(result.run_data, ensure_ascii=False, default=str).encode(),
        json.dumps(dict(result.env), ensure_ascii=False).encode(),
        json.dumps(result.to_json(), ensure_ascii=False, default=str).encode(),
    ]
    for text in _failure_texts(result.failure):
        parts.append(text.encode())
    for text in _walk_tree(result.summary_tree):
        parts.append(text.encode())
    return b"\n".join(parts)


def _assert_absent(haystack: bytes, secret: bytes | str, *, label: str) -> None:
    needle = secret.encode() if isinstance(secret, str) else secret
    if not needle:
        return
    if needle in haystack:
        raise AssertionError(f"{label} leaked into the evidence surface")


def _key_needles(key_bytes: bytes) -> tuple[bytes, ...]:
    text = key_bytes.decode()
    lines = [
        line.strip().encode()
        for line in text.splitlines()
        if line.strip() and not line.startswith("-----") and len(line.strip()) >= 16
    ]
    return (key_bytes, b"BEGIN OPENSSH PRIVATE KEY", *lines)


def _suite(
    *,
    port: int,
    username: str,
    fail: bool = False,
    password: str | None = None,
    private_key: str | bytes | None = None,
    private_key_path: str | Path | None = None,
    agent: str | None = None,
) -> LitmusTest:
    """One routed case against a loopback sshd. Credentials stay off ctx."""

    class Suite(LitmusTest):
        async def configure(self, utils: TestUtils) -> dict[str, Any]:
            await utils.use_shell(
                host="127.0.0.1",
                port=port,
                username=username,
                password=password,
                private_key=private_key,
                private_key_path=private_key_path,
                agent=agent,
                skip_host_verification=True,
            )
            return {"station": "bench-65"}

        def build_suite(self) -> LitmusTest:
            return self.case("claim", self._claim)

        async def _claim(self, utils: TestUtils) -> Verdict:
            result = await utils.exec("hello")
            await utils.capture_artifact(
                Artifact(name="posture", type="survey", data={"ok": result.exit_code == 0})
            )
            if fail:
                raise TestFailure("DUT did not POST")
            if result.exit_code != 0:
                raise TestFailure("loopback exec failed", 0, result.exit_code)
            return PASS

    return Suite(name="dut")


async def _run_suite(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    peer: LoopbackSshd,
    *,
    fail: bool = False,
    password: str | None = None,
    private_key: str | bytes | None = None,
    private_key_path: str | Path | None = None,
    agent: str | None = None,
) -> tuple[RunResult, bytes]:
    suite = _suite(
        port=peer.port,
        username=peer.username,
        fail=fail,
        password=password,
        private_key=private_key,
        private_key_path=private_key_path,
        agent=agent,
    )
    with caplog.at_level("DEBUG", logger="litmus"):
        result = await suite.run(env={"PHYSICAL_LOCATION": "bench-65"})
    haystack = _evidence(result, tmp_path / "artifacts", caplog)
    return result, haystack


class TestPasswordContainment:
    async def test_a_password_run_never_publishes_the_secret(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        peer = await LoopbackSshd.start(username="ops", password=_PASSWORD)
        try:
            result, haystack = await _run_suite(tmp_path, caplog, peer, password=_PASSWORD)
        finally:
            await peer.aclose()

        assert result.state is State.PASS
        _assert_absent(haystack, _PASSWORD, label="password")
        assert b"ssh host-key verification skipped: 127.0.0.1:" in haystack

    async def test_a_failed_run_still_withholds_the_password(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        peer = await LoopbackSshd.start(username="ops", password=_PASSWORD)
        try:
            result, haystack = await _run_suite(
                tmp_path, caplog, peer, password=_PASSWORD, fail=True
            )
        finally:
            await peer.aclose()

        assert result.state is State.FAIL
        assert result.failure is not None
        _assert_absent(haystack, _PASSWORD, label="password")

    async def test_a_rejected_password_is_absent_from_the_failure_tree(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        peer = await LoopbackSshd.start(username="ops", password="not-the-secret")
        try:
            result, haystack = await _run_suite(tmp_path, caplog, peer, password=_PASSWORD)
        finally:
            await peer.aclose()

        assert result.state is State.FAIL
        _assert_absent(haystack, _PASSWORD, label="password")


class TestKeyContainment:
    async def test_in_memory_private_key_bytes_never_reach_evidence(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client_key = LoopbackSshd.ed25519()
        key_bytes = client_key.export_private_key("openssh")
        peer = await LoopbackSshd.start(username="ops", password=None, authorized_key=client_key)
        try:
            result, haystack = await _run_suite(tmp_path, caplog, peer, private_key=key_bytes)
        finally:
            await peer.aclose()

        assert result.state is State.PASS
        for needle in _key_needles(key_bytes):
            _assert_absent(haystack, needle, label="in-memory private key")

    async def test_key_file_contents_never_reach_evidence(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client_key = LoopbackSshd.ed25519()
        key_bytes = client_key.export_private_key("openssh")
        key_file = tmp_path / "id_ed25519"
        key_file.write_bytes(key_bytes)
        key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
        peer = await LoopbackSshd.start(username="ops", password=None, authorized_key=client_key)
        try:
            result, haystack = await _run_suite(tmp_path, caplog, peer, private_key_path=key_file)
        finally:
            await peer.aclose()

        assert result.state is State.PASS
        for needle in _key_needles(key_bytes):
            _assert_absent(haystack, needle, label="key-file contents")


class TestAgentContainment:
    async def test_agent_socket_path_stays_off_every_non_provenance_surface(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client_key = LoopbackSshd.ed25519()
        key_file = tmp_path / "id_ed25519"
        key_file.write_bytes(client_key.export_private_key("openssh"))
        key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

        agent = await asyncio.create_subprocess_exec(
            "ssh-agent",
            "-s",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await agent.communicate()
        if agent.returncode != 0:
            pytest.skip(f"ssh-agent unavailable: {stderr.decode()}")
        sock, pid = LoopbackSshd.parse_agent(stdout.decode())
        try:
            added = await asyncio.create_subprocess_exec(
                "ssh-add",
                str(key_file),
                env={**os.environ, "SSH_AUTH_SOCK": sock, "SSH_AGENT_PID": str(pid)},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _add_out, add_err = await added.communicate()
            if added.returncode != 0:
                pytest.skip(f"ssh-add failed: {add_err.decode()}")

            peer = await LoopbackSshd.start(
                username="ops", password=None, authorized_key=client_key
            )
            try:
                result, haystack = await _run_suite(tmp_path, caplog, peer, agent=sock)
            finally:
                await peer.aclose()
        finally:
            os.kill(pid, signal.SIGTERM)

        assert result.state is State.PASS
        _assert_absent(
            _strip_named_auth_paths(haystack),
            sock,
            label="agent socket path",
        )
        for needle in _key_needles(key_file.read_bytes()):
            _assert_absent(haystack, needle, label="agent-held private key")


class TestHostKeyProvenance:
    async def test_skip_host_verification_names_the_host_in_the_scoped_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        peer = await LoopbackSshd.start(username="ops", password=_PASSWORD)
        port = peer.port
        try:
            result, haystack = await _run_suite(tmp_path, caplog, peer, password=_PASSWORD)
        finally:
            await peer.aclose()

        assert result.state is State.PASS
        expected = f"ssh host-key verification skipped: 127.0.0.1:{port}".encode()
        assert expected in haystack
        _assert_absent(haystack, _PASSWORD, label="password")
