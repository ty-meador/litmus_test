# Bench

The bench is a sibling of `examples/` and models the expected real-world use of
`litmus`: a developer's rack, composed from the shipped tools, run against devices
(Ty's ruling of 2026-10-15 on #139). It is not part of the framework. Nothing here
is reached by `make check`; nothing here is shipped.

The surrogates in this directory are virtual devices, one per Stage 0 rung of
`virtual_hardware_options.md`. Every target prints `[PASS]`/`[FAIL]` lines and
exits non-zero on any `[FAIL]`.

| Target | Device | Contract exercised | Needs |
|---|---|---|---|
| `make bench-serial` | socat null-modem pty pair, self-test console on the far end | Serial: open by tty path, exec, sentinel, cancel | `socat` |
| `make bench-ssh` | Debian 13 cloud image under QEMU/KVM | SSH: connect, exec, status/stderr, disconnect; then `examples/remote_device_ssh.py` | `qemu-system-x86`, `xorriso`, kvm group |
| `make bench-pihole` | official Pi-hole container + sshd | SSH to the `pihole` CLI, HTTP to the FTL API; then `examples/pi_hole_server.py` | Docker |
| `make bench-coap` | `aiocoap` server container | Device API over CoAP: GET, PUT, 4.04 | Docker |
| `make bench-can` | `vxcan0`<->`vxcan1` pair | SocketCAN send/receive, no self-echo (no backend yet) | root setup |
| `make bench-i2c` | `i2c-stub` at 0x50 | SMBus write then read-back through `/dev/i2c-N` | root setup, `i2c-tools` |
| `make bench-gpio` | `gpio-sim` chip, 8 lines | GPIO v2 ioctls through `gpiod`: input follows a pull, output reaches the sim, edge events | root setup, `bench` dependency group |

`make bench-root-setup` runs `sudo sh bench/root_setup.sh` once per boot: it
loads `vcan`/`vxcan`/`i2c-stub`/`gpio-sim`, creates the links and the gpio-sim chip,
and opens the stub's `/dev/i2c-N`, the chip's `/dev/gpiochipN` and its sysfs `pull`
knobs to you. It is the only step that needs root. `make bench-down` stops
the guest and both containers. Images, keys, overlays and generated container
contexts live in `~/.cache/litmus-bench/` (`LITMUS_BENCH_CACHE` to move it).

Host packages: `apt install socat can-utils i2c-tools xorriso qemu-system-x86`.
The GPIO bench needs the python `gpiod` bindings, which `uv run --group bench`
builds from source (gcc); `apt install gpiod` adds `gpiodetect`/`gpioget` for poking
at the chip by hand but the bench does not use them.

## Where each one is honest, and where it is not

- **Serial.** The console is the same `FakeConsole` the self-tests use; the new
  truth is the tty node opened by path through pyserial. A pty drops BREAK, so the
  cancel path is Ctrl-C, exactly as it is through an SSH hop. Timing is not serial
  timing.
- **SSH.** Real sshd on a real Debian kernel. The guest user has a password only so
  the shipped example, which resolves credentials from the environment, can log in;
  `check` uses the bench key.
- **Pi-hole.** Real `pihole` CLI and FTL, but a container: no systemd, restricted
  `dmesg`. The example's claims that touch those fail here and should.
- **CoAP.** The reference Python server, not a device. It proves the encoder and
  the exchange, not a vendor's edge cases.
- **CAN / I2C.** Kernel paths a SocketCAN or `/dev/i2c` backend would use unchanged.
  Parked until those backends exist in `src/`.
- **GPIO.** The chip is a real `/dev/gpiochipN` answering the same v2 ioctls a
  SoC header would; the stimulus is sysfs, not a wire. Pull and value are instant,
  so nothing here says anything about debounce or slew. Chip creation and the node's
  permissions are root-only, which is why the chip is born in the root setup rather
  than the bench. **PWM** has no rung: nothing on this host can witness a waveform.

## The suites

`suites/` is the pressure test proper: suites written the way a developer writes
against a rack, aimed at the surrogates above. `make bench-up` brings every device
up (idempotent); each target below assumes its device is. The API friction met while
writing them is logged in `FRICTION.md`, which is the other deliverable of #139.

| Target | Suite | What it models |
|---|---|---|
| `make bench-suite-serial` | `serial_console.py` | A console opened by tty path: prompt sync, exit status as data, a 4 KiB reply, cancel of a wedged command. The transport has no route today, so the suite owns the handle. |
| `make bench-suite-guest` | `guest_acceptance.py` | Board bring-up acceptance over a class-owned SSH hop: `wait_for` systemd to settle, a journal survey at every boundary, a load audit while claims run, one leaf per unit, measurements with limits (disk, memory, entropy, clock skew). |
| `make bench-suite-pihole` | `pihole_rack.py::PiHole` | The gateway shape: SSH hop on the parent, a child that logs into FTL's REST API, puts a domain on the deny list in `configure()`, watches `dig` behind the hop answer `0.0.0.0`, finds the query in FTL's log, and takes the entry back off in `cleanup()`. |
| `make bench-suite-coap` | `coap_device.py` | A device API over CoAP: identity, 4.04 as a status, one echo leaf per payload size up to past the block-wise boundary, a latency measurement on each. |
| `make bench-suite-gpio` | `gpio_lines.py` | A header through `gpiod` inside the claims, because `utils.get_gpio` is not built: input follows drive, output is witnessed, edge events with a latency measurement. |
| `make bench-rack` | `rack.py` | The four networked/serial devices under one `parallel` owner: one record for the rack. |
| `make bench-soak` | `rack.py` x `RACK_RUNS` | The rack many times over, every run's record and log under `.soak/bench/`; where a leaked task or session shows up. A settled Fail does not stop the soak; the target is red if any run was not Pass. |

As of 2026-10-16 the guest, Pi-hole, and GPIO suites are green. The serial suite is
red at `configure()` and the CoAP suite is red on its 1500-byte echo, both on
findings against the framework (`FRICTION.md` F1 and F15), so the rack and the soak
are red until those are ruled on. That is the bench doing its job, not a bench bug.

`ssh_lab.py` and `pihole_lab.py` are the earlier wrappers that run the shipped
examples unchanged except for `skip_host_verification`; `make bench-ssh` and
`make bench-pihole` still use them, so the examples themselves stay proven against
a real sshd and a real FTL.
