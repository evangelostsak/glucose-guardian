# USB-only Uno bench validation

A reproducible procedure for validating the M3 serial link on a real Arduino Uno,
connected to the Raspberry Pi over USB **with nothing connected to any Uno pin**. It
needs no siren, shaker, button, driver or display.

## Safety rules

- Supplementary prototype, not a medical device. Never disable or change Dexcom G7 or
  Omnipod 5 alarms; this bench does not touch them.
- No physical outputs: the firmware assigns no pins and drives nothing.
- No glucose data: the harness sends a fixed synthetic "normal, nothing audible" state.
- Results against a pseudo-terminal or emulator are **simulated** and are never
  hardware validation. Every report records `physical: true` only for a USB device.
- The watchdog-test firmware hangs on purpose. It is flashed only after the bootloader
  check passes and with an explicit `CONFIRM`, and production firmware is restored
  afterwards.

## Components

| Piece | Location |
|---|---|
| Harness | `backend/src/glucose_guardian/bench/` (`python -m glucose_guardian.bench`) |
| Workflow | `bench/Makefile` |
| Production image | PlatformIO env `uno` → `bench/out/production.hex` |
| Watchdog-test image | PlatformIO env `uno-watchdog-test` → `bench/out/watchdog-test-DO-NOT-DEPLOY.hex` |

The harness reuses the production `protocol.py` encoder and decoder, the Pi-side
`Link` rules, `SerialTransport` and `Runtime`, so the bench exercises the code that
will run on the Pi. Every wait has a deadline. Reports redact USB serial numbers
and contain no host names, user names or glucose values.

The watchdog-test image is the production firmware plus code behind
`GUARDIAN_WATCHDOG_TEST`. It prints `GGTEST WATCHDOG-HANG FIRMWARE NOT FOR USE` at
boot and every 5 s, and hangs 20 s after boot so the 2 s hardware watchdog resets it.
The production image is byte-identical with and without this code. A production `run`
that sees the banner fails `firmware.identity` and stops.

## Workflow

On the laptop (PlatformIO 6.1.18 installed):

```sh
make -C bench build
make -C bench deploy PI=user@raspberrypi.local
```

On the Pi, in `~/glucose-guardian-bench/bench`:

| Step | Command | Touches the Uno |
|---|---|---|
| Install dependencies, create venv | `make setup` | no |
| List USB serial devices | `make discover` | no |
| Check the bootloader (read-only flash dump) | `make bootloader DEVICE=...` | reads only |
| Flash production firmware | `make flash DEVICE=... VARIANT=production` | **writes flash** |
| Automatic bench suite | `make run DEVICE=...` | DTR resets only |
| Suite plus cable replug steps | `make run-interactive DEVICE=...` | you unplug and replug |
| Flash watchdog-test firmware | `make flash DEVICE=... VARIANT=watchdog-test CONFIRM=watchdog-test` | **writes flash** |
| Watchdog test | `make watchdog DEVICE=...` | deliberate hangs |
| Restore production firmware | `make restore DEVICE=...` | **writes flash** |
| Collect diagnostics | `make collect` | no |

Use the `/dev/serial/by-id/...` path shown by `make discover` as `DEVICE`. `flash`
verifies `SHA256SUMS` before writing, lets avrdude verify after writing, and stores a
flash record that `make run` reports as `firmware.upload`.

## Checks

Values come from the `HARDWARE` profile in
`backend/src/glucose_guardian/bench/checks.py`. They are provisional
bench criteria, not clinical or electrical limits.

| Check | Pass criterion |
|---|---|
| `usb.enumeration` | Device exists and enumerates as USB serial; Uno VID:PID recognized or reported |
| `firmware.upload` | Last flash record: production image, avrdude exit 0 (NOT TESTED without a record) |
| `firmware.identity` | GG1 reports and no watchdog-test banner |
| `gg1.handshake` | First valid report within 5 s of opening the port (includes the DTR reset) |
| `gg1.exchange` | 50 exchanges every 0.2 s; each answered within 0.5 s; CRC, session, sequence, constant boot, report sequence +1; latency statistics reported |
| `gg1.session_change` | A new Pi session is answered at once |
| `gg1.rejection` | Duplicate, older, bad-checksum, wrong-version and 100-byte frames get no reply within 0.6 s; the next valid frame does |
| `link.heartbeat_pause` | After 7 s without frames (> 5 s Uno timeout), replies resume with the same boot |
| `link.uno_alarm` | Always NOT TESTED: the Uno's link-lost alarm is not observable without outputs |
| `reset.unexpected` | DTR reset: boot counter +1, recovery within 5 s, and the production `Link` flags an unexpected reset |
| `reset.repeated` | 5 DTR resets, each +1 and recovered within 5 s |
| `traffic.burst` | 500 back-to-back exchanges all answered; throughput and latency reported |
| `traffic.sustained` | 120 s of the production `Runtime` at a 0.2 s tick over the real link: no overruns, tick lateness ≤ 100 ms, no communication fault after a 5 s warm-up, no write failures or disconnects; CPU and RSS reported |
| `usb.disconnect` | Interactive: unplugging is detected as a disconnect |
| `usb.backoff` | Interactive: during 20 s unplugged, open attempts follow 1, 2, 4, 8 s (tolerance 0.7 s) |
| `usb.reconnect` | Interactive: after replugging, a new initial connection without an unexpected-reset flag |
| `watchdog.identity` | Watchdog test only: banner seen and GG1 reports received |
| `watchdog.reset` | Two hang cycles: boot counter +1, reply gap 1.5–4.0 s, unexpected reset flagged |
| `watchdog.cause` | Reports after the watchdog reset carry cause `W` |
| `hardware.*` | Always NOT TESTED: outputs, button, power and backup, display |

## Reading the results

- **PASS / FAIL** apply only to checks that ran. **NOT TESTED** means the check could
  not run in this setup; the reason is in the report.
- `evidence: physical-usb` marks results measured on the real Uno; `simulated` marks
  emulator runs; `flash-record` marks the upload record.
- **Reset cause `U`:** the stock Uno bootloader (Optiboot) clears the reset flags
  before the sketch runs. A `watchdog.cause` FAIL with `U` while `watchdog.reset`
  passes means resets are still detected by the boot counter, but the cause is
  unavailable on this board.
- **Rejects at startup:** opening the port resets the Uno into its bootloader for
  about a second. Frames sent then can make the bootloader answer with STK500 bytes,
  which the Pi counts as rejected lines. Retries absorb this; a non-zero
  `rejected_before_first_reply` in `gg1.handshake` documents it.
- **Uno link-lost alarm:** the Uno sends nothing while the Pi is silent and has no
  outputs, so its link-lost demand cannot be observed. Verifying it needs either output
  integration (M7) or a protocol diagnostic, such as a report flag "link was lost since
  the last accepted frame"; that would be a protocol change for separate approval.

## Outputs

`make run` and `make watchdog` write `bench/reports/<command>-<UTC time>/` containing
`report.md`, `report.json` and `run.log`. `make collect` adds kernel USB messages (serial
numbers removed), `lsusb`, OS details and the build checksums, and packs everything into
`bench/bench-diagnostics-<UTC time>.tar.gz`.
