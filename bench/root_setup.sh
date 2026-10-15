#!/bin/sh
# Loads the kernel-side surrogates. This is the only bench step that needs root:
#   CAN   vcan0 (shared bus) and the vxcan0<->vxcan1 pair
#   I2C   i2c-stub answering at 0x50, its /dev/i2c-N opened to the bench user
#   GPIO  gpio-sim chip "litmus-bench" (8 lines), its /dev/gpiochipN and pull knobs opened to the bench user
# Idempotent. Run as: sudo sh bench/root_setup.sh
set -eu

modprobe can_raw
modprobe vcan
modprobe vxcan
ip link show vcan0 >/dev/null 2>&1 || ip link add dev vcan0 type vcan
ip link show vxcan0 >/dev/null 2>&1 || ip link add dev vxcan0 type vxcan peer name vxcan1
for dev in vcan0 vxcan0 vxcan1; do ip link set up "$dev"; done
echo "CAN: vcan0, vxcan0<->vxcan1 up"

lsmod | grep -q '^i2c_stub' || modprobe i2c-stub chip_addr=0x50
bus=""
for n in /sys/bus/i2c/devices/i2c-*/name; do
    if [ "$(cat "$n")" = "SMBus stub driver" ]; then
        bus=$(basename "$(dirname "$n")" | sed 's/^i2c-//')
    fi
done
if [ -z "$bus" ]; then echo "i2c-stub loaded but no adapter found" >&2; exit 1; fi
chmod a+rw "/dev/i2c-$bus"
echo "I2C: i2c-stub on /dev/i2c-$bus, chip 0x50"

modprobe gpio-sim
mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
sim=/sys/kernel/config/gpio-sim/litmus
[ -d "$sim" ] || mkdir "$sim"
[ -d "$sim/bank0" ] || mkdir "$sim/bank0"
if [ "$(cat "$sim/live")" != 1 ]; then
    echo 8 > "$sim/bank0/num_lines"
    echo litmus-bench > "$sim/bank0/label"
    echo 1 > "$sim/live"
fi
chip=$(cat "$sim/bank0/chip_name")
dev=$(cat "$sim/dev_name")
chmod a+rw "/dev/$chip"
for pull in "/sys/devices/platform/$dev/$chip"/sim_gpio*/pull; do chmod a+w "$pull"; done
echo "GPIO: gpio-sim chip /dev/$chip (label litmus-bench, 8 lines), pulls under /sys/devices/platform/$dev/$chip"
