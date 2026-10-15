# Bench surrogates

Virtual devices for the bench suites, one per Stage 0 rung of
`virtual_hardware_options.md`. Nothing here is reached by `make check`; nothing
here is shipped. Every target prints `[PASS]`/`[FAIL]` lines and exits non-zero
on any `[FAIL]`.

| Target | Device | Contract exercised | Needs |
|---|---|---|---|
| `make bench-serial` | socat null-modem pty pair, self-test console on the far end | Serial: open by tty path, exec, sentinel, cancel | `socat` |
| `make bench-ssh` | Debian 13 cloud image under QEMU/KVM | SSH: connect, exec, status/stderr, disconnect; then `examples/remote_device_ssh.py` | `qemu-system-x86`, `xorriso`, kvm group |
| `make bench-pihole` | official Pi-hole container + sshd | SSH to the `pihole` CLI, HTTP to the FTL API; then `examples/pi_hole_server.py` | Docker |
| `make bench-coap` | `aiocoap` server container | Device API over CoAP: GET, PUT, 4.04 | Docker |
| `make bench-can` | `vxcan0`<->`vxcan1` pair | SocketCAN send/receive, no self-echo (no backend yet) | root setup |
| `make bench-i2c` | `i2c-stub` at 0x50 | SMBus write then read-back through `/dev/i2c-N` | root setup, `i2c-tools` |

`make bench-root-setup` runs `sudo sh tools/bench/root_setup.sh` once per boot: it
loads `vcan`/`vxcan`/`i2c-stub`/`gpio-sim`, creates the links, and opens the stub's
`/dev/i2c-N` to you. It is the only step that needs root. `make bench-down` stops
the guest and both containers. Images, keys, overlays and generated container
contexts live in `~/.cache/litmus-bench/` (`LITMUS_BENCH_CACHE` to move it).

Host packages: `apt install socat can-utils i2c-tools xorriso qemu-system-x86`.

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
- **GPIO.** `gpio-sim` is loaded by the root setup; the chip and stimulus helper
  are not written yet. **PWM** has no rung: nothing on this host can witness a
  waveform.

The wrappers in `suites/` run the shipped examples with `skip_host_verification`
because the targets are on loopback ports no `known_hosts` entry covers. That is
the only difference from running the examples by hand.
