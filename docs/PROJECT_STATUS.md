# Project status

Updated: 2026-11-12

## Current implementation

`main` holds the initial scaffold and repository audit (PR #1), M0 housekeeping
(PR #2), M1 alarm policy v2 (PR #3) and M2 simulated end-to-end runtime (PR #4). CI
runs ruff, strict mypy and pytest on Python 3.11 and 3.13 plus the Uno build. `main` is
protected and requires one approving review; status checks are deliberately not
required.

- **M1:** urgent low, low, normal, high and unknown glucose levels with hysteresis,
  concurrent system faults, and separate glucose and fault acknowledgement.
- **M2:** a fixed-rate supervisory loop with bounded acquisition and clock-check
  workers, strict TOML configuration, eleven deterministic scenarios, display and
  transport doubles, and a simulated service entry point.

M3 serial protocol v1 is complete on `feat/m3-serial-protocol`, PC-only:

- **Protocol v1** ([PROTOCOL.md](PROTOCOL.md)): CRC-16 framed ASCII lines of at most
  64 bytes with version, session and sequence numbers. The Pi's state frame, sent from
  the supervisory loop each tick, is the heartbeat; the Uno answers each new frame with
  a report carrying a boot counter, reset cause, stuck flag and press counters.
- **Liveness:** round-trip link age from the newest acknowledged state frame.
  Corrupted, outdated, duplicate, replayed and wrong-version frames never refresh it.
  Sequence numbers wrap at 16 bits on both sides.
- **Resets:** an initial connection is distinguished from an unexpected Uno reset,
  which latches a `controller_reset` fault until a long press.
- **Button (owner decision):** a short press silences only the glucose channel; a 2 s
  hold silences only system faults; the display shows "Hold 2 s to silence" when only
  faults are sounding. `glucose_acknowledgement_s` and `fault_acknowledgement_s` are
  separate settings, each capped at 900 s.
- **Pi serial transport** on pyserial 3.5: bounded reads (1024 bytes per tick, 63-byte
  lines), writes guarded so a full buffer never blocks, and every zero, partial or
  failed write treated as a failed transmission (discard output, close, reconnect with
  exponential backoff, kept across short-lived connections).
- **Firmware rework** into a testable C++ library:
  - startup grace (10 s) and heartbeat timeout (5 s), latched so a `millis()` wrap
    cannot clear it;
  - a link-lost alarm demanded independently of the Pi, with a 60 s non-extendable,
    auto-re-arming local silence;
  - button debounce (50 ms), long press (2 s) and stuck detection (30 s);
  - a 2 s hardware watchdog, an EEPROM boot counter and reset-cause capture.
  No pins are assigned and no outputs are driven.
- **Tests:** 272 Python tests (including real pyserial over pseudo-terminals against a
  Python Uno emulator) and 23 native firmware tests, with shared frame vectors.

This resolves audit findings H3, H6, M1, M7, L3 and L4.

USB-only bench tooling is complete on `feat/bench-harness` ([BENCH.md](BENCH.md)):

- `python -m glucose_guardian.bench` (`discover`, `run`, `watchdog`, `bootloader`,
  `authorize-watchdog`, `flash-record`) built on the production protocol, link, transport and runtime
  code, with JSON and Markdown reports (PASS / FAIL / NOT TESTED, physical or
  simulated evidence, redacted serial numbers).
- A separate `uno-watchdog-test` firmware build that announces itself and hangs on
  purpose; the production image is byte-identical to `main`.
- `bench/Makefile` for build, deploy, setup, discovery, read-only bootloader check,
  checksum-verified flashing, runs, restore and diagnostics collection.
- Watchdog-test flashing needs `CONFIRM=watchdog-test` and a bootloader authorization
  record bound to the board's current USB connection, the host boot and the deployed
  build, at most 15 minutes old; anything missing, stale, mismatched, modified or
  malformed stops it before avrdude runs.
- The watchdog test opens the serial port only after it has verified the last flash
  record itself:
  - the watchdog-test image hash matches the build's `SHA256SUMS` and the file on disk;
  - avrdude exited 0;
  - the record is for the same USB connection, host boot and build, and at most
    15 minutes old;
  - the record is intact.
  Each flash deletes the previous record before avrdude runs.

Hardware bench results, owner-reported on 2026-11-07 (Raspberry Pi 3B, Debian 13,
genuine Uno R3). The reports stay on the Pi and are not in the repository:

- the bootloader check passed: ATmega328P with Optiboot 4.4;
- the production firmware flashed and verified (3,534 bytes) and is installed;
- the automated and interactive USB suites passed, including disconnect, reconnect and
  backoff: 14 PASS, 0 FAIL, 5 NOT TESTED (hardware-dependent).

Watchdog test, owner-reported on 2026-11-12 (bench build `48a6c65`):
`watchdog.flash_record`, `watchdog.identity` and `watchdog.reset` passed (2 of 2
resets, gaps 2.243 s); `watchdog.cause` failed with `U`, `U`. Production firmware was
restored and verified.

Cause: Optiboot 4.4 clears MCUSR before the sketch starts. The firmware's capture was
correct but read zero. The proposed fix, on `feat/bench-harness` and not yet on
hardware:

- a two-stage watchdog (1 s interrupt, then 1 s reset, about 2 s to reset as before);
- the first-stage interrupt writes a checked marker in `.noinit` RAM, and the next
  boot reports `W` from it;
- the watchdog-test build alternates interrupts-enabled and interrupts-disabled hangs,
  so one run verifies `W` and recovery without the interrupt. The latter reports `U`.

The production image changes (sha256 `bde6b740…`).

## Next milestone

- **Review of the reset-cause fix,** then a rebuild, a deploy and a new watchdog test
  on the Uno, and production firmware restored with the new image. Flashing and
  running each need approval.
- **M4 Dexcom Share adapter:** an implementation plan is proposed for review on its own
  branch, `docs/m4-dexcom-share-plan`. Implementation starts only after the watchdog
  changes are reviewed and the plan is approved.

## Known blockers and gaps

- The two-stage watchdog and the `W` marker are unverified on hardware; the reset
  itself was verified with the earlier single-stage watchdog.
- With Optiboot 4.4, power-on, brown-out and external resets all report `U`. A
  watchdog reset while the Pi is disconnected is lost from `cause` when the Pi
  reconnects, because opening the port resets the Uno again.
- A restart forgets a retained alarm level during stale data (glucose becomes unknown
  while the missing-data fault sounds). Persisting it is an open M6 item.
- The systemd clock check has not been run on the Pi; the simulated service takes
  clock status from the scenario.
- Electrical drivers, backup power, GPIO mappings, button wiring and Uno power topology
  are unselected.
- Physical outputs and button input require reviewed wiring and explicit approval.
- Real Dexcom access is intentionally deferred; no credentials are configured.
- GUI and systemd installation are absent.
- Remaining audit findings: H5 (hardware review); M6 (M6); M9 (M5/M6); L5, and L6's
  heartbeat timeout defined separately on the Pi and the Uno.

No deployment has occurred. Official Dexcom and Omnipod alerts remain independent
and unchanged. This is a supplementary prototype, not a certified medical device.
