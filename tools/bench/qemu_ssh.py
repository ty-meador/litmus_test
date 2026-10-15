"""SSH surrogate: Debian 13 genericcloud amd64 under QEMU with KVM.

An honest OS peer: a real sshd, real PAM, a real ``uname``, on a kernel that is not
the host's. The image is Debian's own cloud build, verified against its SHA512SUMS
and cached once under ``~/.cache/litmus-bench/``; every boot runs on a throwaway
qcow2 overlay so the base stays pristine. A cloud-init seed ISO (``xorriso``) creates
the ``bench`` user with a bench-only ed25519 key for ``check`` and a password for the
shipped example, which resolves credentials from the environment and has no key path.

    up      fetch, seed, boot (daemonized), wait for sshd; idempotent
    down    stop the guest
    status  pid and port
    check   SshInterface contract: connect, exec, status, provenance

Needs ``qemu-system-x86_64``, ``xorriso``, ``ssh-keygen`` and ``/dev/kvm`` (kvm group).
Forwarded port: ``LITMUS_BENCH_SSH_PORT`` (default 2222) on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import sys
import time
import urllib.request
from pathlib import Path

from tools.bench._common import cache_dir, pid_from, report, require_tool, run

from litmus.ssh import SshInterface

IMAGE_BASE = "https://cloud.debian.org/images/cloud/trixie/latest/"
IMAGE_NAME = "debian-13-genericcloud-amd64.qcow2"
USER = "bench"
PASSWORD = "bench"
HOST = "127.0.0.1"
PORT = int(os.environ.get("LITMUS_BENCH_SSH_PORT", "2222"))
MEMORY_MIB = "1024"
BOOT_TIMEOUT = 240.0


def _paths() -> dict[str, Path]:
    root = cache_dir()
    return {
        "image": root / IMAGE_NAME,
        "overlay": root / "ssh-guest.qcow2",
        "key": root / "bench_ed25519",
        "seed_dir": root / "seed",
        "seed": root / "seed.iso",
        "pid": root / "ssh-guest.pid",
        "console": root / "ssh-guest-console.log",
    }


def ensure_image(image: Path) -> None:
    """Fetch Debian's cloud image once, verified against the published SHA512SUMS."""
    if image.exists():
        return
    sums = urllib.request.urlopen(IMAGE_BASE + "SHA512SUMS", timeout=60).read().decode()
    expected = next(line.split()[0] for line in sums.splitlines() if line.endswith(IMAGE_NAME))
    print(f"fetching {IMAGE_NAME} (sha512 {expected[:16]}...)")
    digest = hashlib.sha512()
    partial = image.with_suffix(".part")
    with (
        urllib.request.urlopen(IMAGE_BASE + IMAGE_NAME, timeout=120) as response,
        partial.open("wb") as out,
    ):
        while chunk := response.read(1 << 20):
            out.write(chunk)
            digest.update(chunk)
    if digest.hexdigest() != expected:
        partial.unlink()
        sys.exit("image checksum mismatch; not keeping it")
    partial.rename(image)


def ensure_key(key: Path) -> str:
    """A bench-only ed25519 keypair; returns the public key line."""
    if not key.exists():
        run(
            [
                require_tool("ssh-keygen"),
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "litmus-bench",
                "-f",
                str(key),
            ]
        )
    return key.with_suffix(".pub").read_text().strip()


def ensure_seed(seed_dir: Path, seed: Path, pubkey: str) -> None:
    """Write the NoCloud seed (user-data, meta-data) and pack it as a ``cidata`` ISO."""
    seed_dir.mkdir(exist_ok=True)
    (seed_dir / "meta-data").write_text("instance-id: litmus-bench\nlocal-hostname: litmus-dut\n")
    (seed_dir / "user-data").write_text(
        "#cloud-config\n"
        "ssh_pwauth: true\n"
        "users:\n"
        f"  - name: {USER}\n"
        "    shell: /bin/bash\n"
        "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        "    lock_passwd: false\n"
        f"    plain_text_passwd: {PASSWORD}\n"
        "    ssh_authorized_keys:\n"
        f"      - {pubkey}\n"
    )
    run(
        [
            require_tool("xorriso"),
            "-as",
            "mkisofs",
            "-quiet",
            "-output",
            str(seed),
            "-volid",
            "cidata",
            "-joliet",
            "-rock",
            str(seed_dir / "user-data"),
            str(seed_dir / "meta-data"),
        ]
    )


def up() -> int:
    """Boot the guest if it is not already up, then wait for sshd to answer."""
    paths = _paths()
    if pid_from(paths["pid"]) is not None:
        print(f"guest already up (pid {pid_from(paths['pid'])}), ssh on {HOST}:{PORT}")
        return 0
    qemu = require_tool("qemu-system-x86_64")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        sys.exit("/dev/kvm is not writable: add yourself to the kvm group and log in again")
    ensure_image(paths["image"])
    ensure_seed(paths["seed_dir"], paths["seed"], ensure_key(paths["key"]))
    paths["overlay"].unlink(missing_ok=True)
    run(
        [
            require_tool("qemu-img"),
            "create",
            "-q",
            "-f",
            "qcow2",
            "-b",
            str(paths["image"]),
            "-F",
            "qcow2",
            str(paths["overlay"]),
        ]
    )
    run(
        [
            qemu,
            "-machine",
            "q35,accel=kvm",
            "-cpu",
            "host",
            "-smp",
            "2",
            "-m",
            MEMORY_MIB,
            "-display",
            "none",
            "-daemonize",
            "-pidfile",
            str(paths["pid"]),
            "-serial",
            f"file:{paths['console']}",
            "-drive",
            f"if=virtio,format=qcow2,file={paths['overlay']}",
            "-drive",
            f"if=virtio,format=raw,readonly=on,file={paths['seed']}",
            "-nic",
            f"user,model=virtio-net-pci,hostfwd=tcp:{HOST}:{PORT}-:22",
        ]
    )
    started = time.monotonic()
    print(f"guest booting (pid {pid_from(paths['pid'])}); console in {paths['console']}")
    deadline = started + BOOT_TIMEOUT
    while time.monotonic() < deadline:
        if pid_from(paths["pid"]) is None:
            sys.exit(f"qemu exited during boot; see {paths['console']}")
        if asyncio.run(_ssh_ready(paths["key"])):
            print(f"sshd answered after {time.monotonic() - started:.0f}s on {HOST}:{PORT}")
            return 0
        time.sleep(2.0)
    sys.exit(f"sshd did not answer within {BOOT_TIMEOUT:.0f}s; see {paths['console']}")


async def _ssh_ready(key: Path) -> bool:
    transport = _transport(key)
    try:
        await asyncio.wait_for(transport.connect(), timeout=5.0)
    except Exception:
        return False
    await transport.disconnect()
    return True


def _transport(key: Path) -> SshInterface:
    return SshInterface(
        host=HOST, port=PORT, username=USER, private_key_path=key, skip_host_verification=True
    )


def down() -> int:
    """Stop the guest; the overlay is discarded on the next ``up``."""
    paths = _paths()
    pid = pid_from(paths["pid"])
    if pid is None:
        print("guest is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if pid_from(paths["pid"]) is None:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    paths["pid"].unlink(missing_ok=True)
    print(f"guest stopped (pid {pid})")
    return 0


def status() -> int:
    """One line: pid and forwarded port."""
    pid = pid_from(_paths()["pid"])
    print(f"guest {'up, pid ' + str(pid) if pid else 'down'}; ssh {HOST}:{PORT} user {USER}")
    return 0 if pid else 1


async def check() -> int:
    """The SSH transport contract against the real sshd."""
    transport = _transport(_paths()["key"])
    await transport.connect()
    for line in transport.provenance_lines:
        print(line)
    failures = 0
    result = await transport.exec("uname -sr")
    failures += report(
        "exec on a real kernel",
        result.exit_code == 0 and result.stdout.startswith("Linux"),
        result.stdout.strip(),
    )
    result = await transport.exec(". /etc/os-release && echo $PRETTY_NAME")
    failures += report(
        "the peer is the Debian cloud image", "Debian" in result.stdout, result.stdout.strip()
    )
    result = await transport.exec("sh -c 'echo to-stderr >&2; exit 3'")
    failures += report(
        "status and stderr survive the channel",
        result.exit_code == 3 and "to-stderr" in result.stderr,
        f"exit={result.exit_code} stderr={result.stderr.strip()!r}",
    )
    await transport.disconnect()
    failures += report("disconnect closes the handle", not transport.connected, "")
    return failures


def main(argv: list[str]) -> int:
    """Dispatch ``up``/``down``/``status``/``check``."""
    actions = {"up": up, "down": down, "status": status, "check": lambda: asyncio.run(check())}
    if len(argv) != 1 or argv[0] not in actions:
        sys.exit(f"usage: python -m tools.bench.qemu_ssh {{{'|'.join(actions)}}}")
    return actions[argv[0]]()


if __name__ == "__main__":
    sys.exit(1 if main(sys.argv[1:]) else 0)
