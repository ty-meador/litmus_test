"""I2C surrogate: the kernel's ``i2c-stub``, a fake SMBus device with register memory.

``modprobe i2c-stub chip_addr=0x50`` (``make bench-root-setup``) puts an adapter named
"SMBus stub driver" on the bus list with one device answering at 0x50. Writes land in
its register file and reads come back from it, through the same ``/dev/i2c-N`` node
and ``I2C_SMBUS`` ioctl a real EEPROM would use. Userspace access is built into this
kernel (``CONFIG_I2C_CHARDEV=y``). Needs ``i2c-tools`` and read/write on the node,
which the root setup grants.
"""

from __future__ import annotations

import sys
from pathlib import Path

from bench._common import report, require_tool, run

_CHIP = "0x50"
_REGISTER = "0x10"
_VALUE = "0xa5"


def stub_bus() -> int:
    """The bus number the stub adapter registered as, or exit naming the setup step."""
    for name in Path("/sys/bus/i2c/devices").glob("i2c-*/name"):
        if name.read_text().strip() == "SMBus stub driver":
            return int(name.parent.name.removeprefix("i2c-"))
    sys.exit("no i2c-stub adapter; run `make bench-root-setup` first")


def main() -> int:
    """Detect the stub, write a register, read it back."""
    i2cdetect, i2cset, i2cget = (require_tool(t) for t in ("i2cdetect", "i2cset", "i2cget"))
    bus = stub_bus()
    print(f"i2c-stub adapter: /dev/i2c-{bus}")
    failures = 0
    scan = run([i2cdetect, "-y", str(bus)], capture=True).stdout
    failures += report(
        f"chip {_CHIP} answers the bus scan", " 50 " in scan or scan.rstrip().endswith("50"), ""
    )
    run([i2cset, "-y", str(bus), _CHIP, _REGISTER, _VALUE])
    got = run([i2cget, "-y", str(bus), _CHIP, _REGISTER], capture=True).stdout.strip()
    failures += report(
        f"register {_REGISTER} reads back what was written",
        got == _VALUE,
        f"wrote {_VALUE} read {got}",
    )
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
