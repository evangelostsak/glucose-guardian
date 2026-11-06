# Project status

Updated: 2026-11-06

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
  exponential backoff).
- **Firmware rework** into a testable C++ library:
  - startup grace (10 s) and heartbeat timeout (5 s), latched so a `millis()` wrap
    cannot clear it;
  - a link-lost alarm demanded independently of the Pi, with a 60 s non-extendable,
    auto-re-arming local silence;
  - button debounce (50 ms), long press (2 s) and stuck detection (30 s);
  - a 2 s hardware watchdog, an EEPROM boot counter and reset-cause capture.
  No pins are assigned and no outputs are driven.
- **Tests:** 269 Python tests (including real pyserial over pseudo-terminals against a
  Python Uno emulator) and 23 native firmware tests, with shared frame vectors.

This resolves audit findings H3, H6, M1, M7, L3 and L4.

## Next milestone

- **Bench validation (needs separate approval):** flash an Uno with nothing on its
  outputs and work through the checklist in [HARDWARE.md](HARDWARE.md).
- **M4 Dexcom Share adapter,** following the roadmap: behind a configuration flag,
  tested with synthetic recorded responses, with credentials held in systemd
  credentials outside the repository. It requires owner approval to start.

## Known blockers and gaps

- No Uno has been flashed: watchdog resets, reset-cause reporting with the installed
  bootloader, and real serial timing are unverified.
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
