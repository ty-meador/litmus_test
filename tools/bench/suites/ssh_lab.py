"""``examples/remote_device_ssh.py`` against the QEMU guest (``make bench-ssh``)."""

from __future__ import annotations

from examples.remote_device_ssh import SshDevice


class SshLab(SshDevice):
    """The shipped SSH example with host verification off; everything else is inherited."""

    def __init__(self, name: str = "ssh-lab") -> None:
        super().__init__(name=name, skip_host_verification=True)
