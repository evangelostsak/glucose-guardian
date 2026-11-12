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
- The watchdog-test firmware hangs on purpose. It is flashed only with an explicit
  `CONFIRM` and a bootloader authorization recorded for the same board connection, host
  boot and bench build (see [Bootloader authorization](#bootloader-authorization)), and
  production firmware is restored afterwards.

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
boot and every 5 s, and hangs 20 s after boot so the two-stage hardware watchdog
resets it. On an even boot counter it hangs with interrupts enabled, so the watchdog
interrupt records a `W` marker. On an odd one it hangs with interrupts disabled, so the
interrupt cannot run and only the second stage resets it. The two hang cycles of
`make watchdog` cover both.

This test-only code is excluded from the production build at compile time. The
production image contains neither the banner nor the deliberate hang. Production and
watchdog-test images are different binaries with different SHA-256 hashes (see
[Validation record](#validation-record)). A production `run` that sees the banner
fails `firmware.identity` and stops.

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
| Flash production firmware | `make flash DEVICE=... VARIANT=production` | **writes flash** |
| Automatic bench suite | `make run DEVICE=...` | DTR resets only |
| Suite plus cable replug steps | `make run-interactive DEVICE=...` | you unplug and replug |
| Check the bootloader (read-only flash dump) | `make bootloader DEVICE=...` | DTR reset, reads flash |
| Flash watchdog-test firmware | `make flash DEVICE=... VARIANT=watchdog-test CONFIRM=watchdog-test` | **writes flash** |
| Watchdog test | `make watchdog DEVICE=...` | deliberate hangs |
| Restore production firmware | `make restore DEVICE=...` | **writes flash** |
| Collect diagnostics | `make collect` | no |

Use the `/dev/serial/by-id/...` path shown by `make discover` as `DEVICE`; `bootloader`,
`flash` and `watchdog` refuse any other path. `flash` verifies `SHA256SUMS` before
writing, deletes the previous `last-flash.json`, lets avrdude verify after writing, and
then writes a flash record (see [Flash records](#flash-records-and-the-watchdog-test))
that `make run` reports as `firmware.upload`. Run `make bootloader` immediately before
the watchdog-test flash and `make watchdog` immediately after it: both records expire
after 15 minutes and on any reconnect.

Opening the serial port resets the Uno through DTR, whether the harness or avrdude opens
it: the USB-serial chip pulses the reset line, the bootloader runs for about a second,
then the installed sketch starts. The production sketch increments its EEPROM boot
counter on every start, including these.

## Bootloader authorization

`make bootloader DEVICE=/dev/serial/by-id/...`:

1. Resolves the link through sysfs to its USB device: VID:PID, USB serial number if the
   board has one, USB port path and the kernel device number. It refuses a tty name
   such as `/dev/ttyACM0`, a link to nothing, a non-USB port, and a board whose VID:PID
   and serial number (or absence of one) another attached USB device shares.
2. Runs exactly `avrdude -p atmega328p -c arduino -b 115200 -P DEVICE -U
   flash:r:reports/flash-dump.bin:r`, with a 60 s timeout.
3. Resolves the device again and refuses if anything changed during the read.
4. Accepts only a 32768-byte dump whose Optiboot version marker (major at `0x7FFF`,
   minor at `0x7FFE`) is 4 or later and not `0xFF`. avrdude omits trailing erased
   bytes, so a bootloader without the marker yields a shorter dump.
5. Deletes the dump and writes `reports/bootloader-authorization.json`.

An earlier record is deleted first, so a failed check leaves no authorization.

The record holds:

| Field | Content |
|---|---|
| `schema` | `glucose-guardian/bootloader-authorization/1` |
| `verified_utc`, `verified_boottime_s` | Verification time: UTC for people, `CLOCK_BOOTTIME` for the age check |
| `host_boot_id` | The Pi's `/proc/sys/kernel/random/boot_id` |
| `device` | `/dev/serial/by-id` link (serial redacted), USB VID:PID, USB port path, kernel device number, SHA-256 of boot id and serial number (`null` without one) |
| `bootloader` | `optiboot`, version, marker bytes, SHA-256 of the 512-byte boot section, dump size |
| `build` | `out/BUILD-COMMIT` and the SHA-256 of `out/SHA256SUMS` |
| `tooling` | Harness version, avrdude version (`null` if unknown) |
| `integrity_sha256` | SHA-256 of all other fields |

`make flash VARIANT=watchdog-test CONFIRM=watchdog-test` checks `CONFIRM`, then accepts
the record only if all of these hold; otherwise it stops before avrdude runs:

- it is readable JSON with exactly these fields, its integrity digest matches, and the
  schema is supported;
- it is from the current host boot and between 0 and 900 s old;
- the link, VID:PID, port path, device number and serial digest equal the selected
  device's current ones;
- the build commit and `SHA256SUMS` digest equal the deployed build's;
- the recorded marker still shows Optiboot 4 or later.

Limits:

- **Every record is bound to one USB connection**, with or without a serial number:
  the kernel assigns a new device number on every reconnect. Reconnecting the board,
  rebooting the Pi, deploying a new build or waiting more than 15 minutes requires a
  new `make bootloader`. Without a serial number (common on CH340 clones) the
  connection is the only identity; neither case proves that a reconnected board is
  the board that was checked.
- The integrity digest detects edits and corruption. It is not a signature: a local
  user who recomputes it can forge a record. The record guards against mistakes, such
  as the wrong board, an old record or a changed build.
- A bootloader replaced over ISP without a USB reconnect is not detected; the
  15-minute limit bounds that window. A board swapped in the milliseconds between the
  check and avrdude opening the port is not detected either; a reconnect takes longer.
- Serial numbers appear only redacted or as a digest.

The read is read-only: the command has a single flash read operation and no erase
(`-e`), write, verify or EEPROM operation, and a test pins the exact command line.
avrdude erases only on request or before writing flash, and fuses can be changed only
over ISP, never through the bootloader. The one write that follows is the sketch's own
boot counter update after the DTR reset, described above.

## Flash records and the watchdog test

`make flash` deletes `reports/last-flash.json` before avrdude runs. Afterwards the harness
writes `reports/flash-<UTC time>.json`, which `make flash` copies to `last-flash.json`.
The record holds:

- schema `glucose-guardian/flash-record/1`;
- the variant, the image file name, the image's SHA-256 and the avrdude exit code;
- the UTC stamp, the host boot id and a `CLOCK_BOOTTIME` time;
- the same device and build fields as the bootloader authorization;
- an integrity digest.

The harness writes a record for a failed flash too. If writing the record fails, or the
flash is interrupted, `last-flash.json` stays absent.

`make watchdog` runs `watchdog.flash_record` first and opens the serial port only if it
passes. The check reads `last-flash.json` itself; it does not trust anything `make
flash` checked. It refuses unless all of these hold:

- the record is readable JSON with exactly these fields, its integrity digest matches,
  and the schema is supported;
- the variant is `watchdog-test` and the image is `watchdog-test-DO-NOT-DEPLOY.hex`;
- the avrdude exit code is the integer 0 (avrdude verifies after writing);
- the recorded SHA-256 equals this build's `SHA256SUMS` entry, and the image file on disk
  still has that hash;
- the record is from the current host boot and between 0 and 900 s old;
- the link, VID:PID, port path, device number and serial digest equal the selected
  device's current ones;
- the build commit and `SHA256SUMS` digest equal the deployed build's.

On refusal the report shows `watchdog.flash_record` FAIL with the reason, the three
watchdog checks are NOT TESTED, and `make watchdog` fails. A flash from another tool,
such as the Arduino IDE, updates no record. The watchdog test still identifies the
running firmware by its banner, so production or other firmware fails
`watchdog.identity`.

Records written before this schema (plain `variant`, `hex`, `sha256`,
`avrdude_exit_code`, `utc`) are refused by `make watchdog`; `make run` still reads them
for `firmware.upload`.

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
| `watchdog.flash_record` | Watchdog test only, before the port is opened: the last flash record verifies (see [Flash records](#flash-records-and-the-watchdog-test)) |
| `watchdog.identity` | Watchdog test only: banner seen and GG1 reports received |
| `watchdog.reset` | Two hang cycles, one with interrupts enabled and one disabled: boot counter +1, reply gap 1.5–4.0 s, unexpected reset flagged |
| `watchdog.cause` | `W` after the interrupts-enabled hang (even boot), `U` after the interrupts-disabled hang (odd boot) |
| `hardware.*` | Always NOT TESTED: outputs, button, power and backup, display |

## Reading the results

- **PASS / FAIL** apply only to checks that ran. **NOT TESTED** means the check could
  not run in this setup; the reason is in the report.
- `evidence: physical-usb` marks results measured on the real Uno; `simulated` marks
  emulator runs; `flash-record` marks the upload record.
- **Reset cause:** Optiboot 4.4 clears the reset flags before the sketch runs, so
  `W` comes only from the firmware's watchdog-interrupt marker (see
  [PROTOCOL.md](PROTOCOL.md)). Possible failures:
  - `U` after the interrupts-enabled hang: the marker was not written or did not
    survive the reset.
  - `W` after the interrupts-disabled hang: something other than the watchdog
    interrupt wrote the marker.
  - A `watchdog.reset` failure on the interrupts-disabled cycle: the second watchdog
    stage did not reset the Uno. That is a recovery failure; restore production
    firmware and report it.
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
`report.md`, `report.json` and `run.log`. `make bootloader` writes
`bench/reports/bootloader-authorization.json`; `make flash` writes
`bench/reports/flash-<UTC time>.json`, `.log` and `last-flash.json`. `make collect`
adds kernel USB messages (serial numbers removed), `lsusb`, OS details and the build
checksums, and packs everything into `bench/bench-diagnostics-<UTC time>.tar.gz`.

## Validation record

These results are owner-reported from the physical bench; the reports stay on the Pi
under `bench/reports/` and are not in the repository. They cover the M3 firmware and
serial link over USB only, not the alarm system (see
[HARDWARE.md](HARDWARE.md#usb-only-bench-validation-status)).

The bench:

- a Raspberry Pi 3B with Debian 13, and a genuine Arduino Uno R3 (ATmega328P, USB
  `2341:0043`) with nothing connected to any pin;
- stock Optiboot 4.4, boot section SHA-256
  `e36d971b54b3336178813bf16cddf2658866367874587f7fc6c560fb629fbc74`.

| Date | Run | Firmware | Verdict |
|---|---|---|---|
| 2026-11-07 | Bootloader check | — | PASS (Optiboot 4.4) |
| 2026-11-07 | Automated and interactive suites | earlier production image `4b36ab28…` (original 2 s reset-only watchdog) | PASS (reported as 14 PASS, 0 FAIL, 5 NOT TESTED) |
| 2026-11-12 | Watchdog test, bench build `48a6c65` | watchdog-test image with the original 2 s reset-only watchdog | FAIL on `watchdog.cause` only: `U`, `U`. `watchdog.reset` passed (2 of 2 resets); led to the reset-cause fix |
| 2026-11-12 | `watchdog-2026-11-12T101301Z`, build `475deaa` | watchdog-test image | PASS |
| 2026-11-12 | `run-2026-11-12T101456Z`, build `475deaa` | production `bde6b740…` | PASS |
| 2026-11-12 | `run-2026-11-12T102024Z` (interactive), build `475deaa` | production `bde6b740…` | PASS |

The watchdog changed between these runs:

- **Up to `48a6c65`:** a 2 s reset-only watchdog (`WDTO_2S`).
- **From the reset-cause fix (validated at `475deaa`):** two 1 s stages, an interrupt
  that records the `W` marker, then a reset.

The `48a6c65` run failed reset-cause reporting, not watchdog recovery: both of its
hangs reset the Uno, but Optiboot 4.4 had cleared the reset flags, so both reported `U`.

Watchdog test `watchdog-2026-11-12T101301Z`:

- `watchdog.flash_record` and `watchdog.identity`: PASS;
- `watchdog.reset`: PASS, 2 of 2 resets, gaps 2.243 s and 2.243 s, both the
  interrupts-disabled and the interrupts-enabled hang;
- `watchdog.cause`: PASS, observed `U`, `W`, expected `U`, `W`.

Production firmware runs:

| Check | `run-…101456Z` | `run-…102024Z` (interactive) |
|---|---|---|
| `gg1.exchange` | PASS: 50 of 50, no anomalies, p95 20.2 ms | PASS: 50 of 50, no anomalies, p95 20.21 ms |
| `gg1.session_change` | PASS | not itemized |
| `gg1.rejection` | PASS | not itemized |
| `link.heartbeat_pause` | PASS | not itemized |
| `reset.unexpected` | PASS | not itemized |
| `reset.repeated` | PASS: 5 of 5, longest recovery 0.732 s | PASS: 5 of 5, longest recovery 0.731 s |
| `traffic.burst` | PASS: 500 of 500, 43.9 exchanges/s | PASS: 500 of 500 |
| `traffic.sustained` | PASS: 600 ticks, max lateness 4.04 ms, no communication fault after warm-up, CPU 1.3% | PASS: 600 ticks, max lateness 3.98 ms, no communication fault after warm-up, CPU 1.3% |
| `usb.disconnect`, `usb.reconnect` | NOT TESTED (noninteractive) | PASS |
| `usb.backoff` | NOT TESTED (noninteractive) | PASS: delays 0.801, 2.003, 4.005, 8.008 s against 1, 2, 4, 8 s (tolerance 0.7 s) |
| `link.uno_alarm`, `hardware.*` | NOT TESTED (by design) | NOT TESTED (by design) |

"Not itemized" means the owner's summary did not list the check. A PASS verdict means
no check failed. Checks missing from the table were not itemized in either run:
`usb.enumeration`, `firmware.upload`, `firmware.identity` and `gg1.handshake`.

Firmware images:

- **Production** `bde6b74088c52eb5483af3cfc2a3a6ef437fb9bdbeec7d7ba3d9d2d4442450db`.
  It was restored, verified and is installed on the bench Uno. A local rebuild of
  `475deaa` gives the same hash.
- **Watchdog-test image:** a local rebuild of `475deaa` gives
  `9e194d48d60f233d480c11c7908d0bec15ba53932fd8ccb4335a511920f54317`. On the bench,
  `watchdog.flash_record` passed, so the flashed image matched the deployed build's
  `SHA256SUMS`.
