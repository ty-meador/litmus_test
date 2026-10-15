"""Pi-hole surrogate: the official container plus an sshd, the shipped example's real target.

``examples/pi_hole_server.py`` drives a Pi-hole over SSH (``pihole status``, ``pihole
-v``, ``systemctl``, ``dmesg``, log tails). The official image has no sshd, so the
bench builds one layer on top: ``openssh-server`` and a root password, both lab-only
and bound to 127.0.0.1. The same container also serves Pi-hole's own REST API on
port 80, which is the ``HttpInterface`` DUT. Two contracts, one device.

A container has no systemd and a restricted ``dmesg``; the example's claims that
touch those will fail *honestly* here. That is a finding about the surrogate, and
it is left visible rather than mocked around.

    up | down | status | check
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from tools.bench._common import cache_dir, report, require_tool, run, wait_for_port

from litmus.device_api import DeviceRequest, HttpInterface
from litmus.ssh import SshInterface

IMAGE = "litmus-bench/pihole-sshd"
CONTAINER = "litmus-bench-pihole"
HOST = "127.0.0.1"
SSH_PORT = int(os.environ.get("LITMUS_BENCH_PIHOLE_SSH_PORT", "2223"))
HTTP_PORT = int(os.environ.get("LITMUS_BENCH_PIHOLE_HTTP_PORT", "8080"))
USER = "root"
PASSWORD = "bench"

_DOCKERFILE = """\
FROM pihole/pihole:latest
RUN apk add --no-cache openssh-server \\
 && ssh-keygen -A \\
 && echo 'root:bench' | chpasswd \\
 && sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \\
 && printf '#!/bin/sh\\n/usr/sbin/sshd\\nexec start.sh "$@"\\n' > /bench-entry.sh \\
 && chmod +x /bench-entry.sh
ENTRYPOINT ["/bench-entry.sh"]
"""


def up() -> int:
    """Build the sshd layer, start the container, wait for sshd and FTL's web server."""
    docker = require_tool("docker")
    context = cache_dir() / "pihole"
    context.mkdir(exist_ok=True)
    (context / "Dockerfile").write_text(_DOCKERFILE)
    run([docker, "build", "-q", "-t", IMAGE, str(context)], capture=True)
    run([docker, "rm", "-f", CONTAINER], check=False, capture=True)
    run(
        [
            docker,
            "run",
            "-d",
            "--name",
            CONTAINER,
            "-p",
            f"{HOST}:{SSH_PORT}:22",
            "-p",
            f"{HOST}:{HTTP_PORT}:80",
            "-e",
            "TZ=UTC",
            "-e",
            f"FTLCONF_webserver_api_password={PASSWORD}",
            IMAGE,
        ],
        capture=True,
    )
    if not wait_for_port(HOST, SSH_PORT, timeout=30.0):
        sys.exit("sshd in the pihole container did not come up")
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        if asyncio.run(_api_answers()):
            print(
                f"pihole up: ssh {HOST}:{SSH_PORT} ({USER}/{PASSWORD}), "
                f"api http://{HOST}:{HTTP_PORT}"
            )
            return 0
        time.sleep(1.0)
    sys.exit(
        "pihole-FTL's web server did not answer within 90s; see `docker logs litmus-bench-pihole`"
    )


async def _api_answers() -> bool:
    backend = HttpInterface(base_url=f"http://{HOST}:{HTTP_PORT}", request_timeout=3.0)
    try:
        await backend.connect()
        response = await backend.request(DeviceRequest(method="GET", path="/api/auth"))
    except Exception:
        return False
    finally:
        await backend.disconnect()
    return response.status in (200, 401)


def down() -> int:
    """Remove the container."""
    run([require_tool("docker"), "rm", "-f", CONTAINER], check=False, capture=True)
    print("pihole container removed")
    return 0


def status() -> int:
    """Running or not."""
    out = run([require_tool("docker"), "ps", "-q", "-f", f"name={CONTAINER}"], capture=True).stdout
    state = "up" if out.strip() else "down"
    print(f"pihole {state}; ssh {HOST}:{SSH_PORT}, api http://{HOST}:{HTTP_PORT}")
    return 0 if out.strip() else 1


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""


async def check() -> int:
    """Both contracts: SSH to the pihole CLI, HTTP to the FTL API."""
    failures = 0
    shell = SshInterface(
        host=HOST, port=SSH_PORT, username=USER, password=PASSWORD, skip_host_verification=True
    )
    await shell.connect()
    print(shell.connect_provenance)
    result = await shell.exec("pihole -v")
    failures += report(
        "`pihole -v` over ssh",
        result.exit_code == 0 and "Core" in result.stdout,
        _first_line(result.stdout) or _first_line(result.stderr),
    )
    result = await shell.exec("pihole status")
    failures += report(
        "`pihole status` reports FTL",
        result.exit_code == 0 and "FTL" in result.stdout,
        result.stdout.strip().replace("\n", " | ")[:120],
    )
    await shell.disconnect()

    api = HttpInterface(base_url=f"http://{HOST}:{HTTP_PORT}", request_timeout=5.0)
    await api.connect()
    response = await api.request(
        DeviceRequest(
            method="POST",
            path="/api/auth",
            payload=json.dumps({"password": PASSWORD}).encode(),
            headers={"Content-Type": "application/json"},
        )
    )
    sid = ""
    if response.status == 200:
        sid = str(json.loads(response.body).get("session", {}).get("sid", ""))
    failures += report(
        "POST /api/auth issues a session",
        response.status == 200 and bool(sid),
        f"status={response.status}",
    )
    response = await api.request(
        DeviceRequest(method="GET", path="/api/info/version", headers={"sid": sid})
    )
    failures += report(
        "GET /api/info/version with the session",
        response.status == 200 and b"version" in response.body,
        f"status={response.status} bytes={len(response.body)}",
    )
    response = await api.request(DeviceRequest(method="GET", path="/api/info/version"))
    failures += report(
        "the same GET without a session is 401", response.status == 401, f"status={response.status}"
    )
    await api.disconnect()
    return failures


def main(argv: list[str]) -> int:
    """Dispatch ``up``/``down``/``status``/``check``."""
    actions = {"up": up, "down": down, "status": status, "check": lambda: asyncio.run(check())}
    if len(argv) != 1 or argv[0] not in actions:
        sys.exit(f"usage: python -m tools.bench.pihole {{{'|'.join(actions)}}}")
    return actions[argv[0]]()


if __name__ == "__main__":
    sys.exit(1 if main(sys.argv[1:]) else 0)
