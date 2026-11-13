# Project status

Updated: 2026-11-12

## Current implementation

`main` holds the initial scaffold and repository audit (PR #1), M0 housekeeping
(PR #2), M1 alarm policy v2 (PR #3), M2 simulated end-to-end runtime (PR #4), M3
serial protocol v1 (PR #5), and the USB bench tooling with its hardware validation
(PR #6). CI
runs ruff, strict mypy and pytest on Python 3.11 and 3.13 plus the Uno build. `main` is
protected and requires one approving review; status checks are deliberately not
required.

- **M1:** urgent low, low, normal, high and unknown glucose levels with hysteresis,
  concurrent system faults, and separate glucose and fault acknowledgement.
- **M2:** a fixed-rate supervisory loop with bounded acquisition and clock-check
  workers, strict TOML configuration, eleven deterministic scenarios, display and
  transport doubles, and a simulated service entry point.

M3 serial protocol v1 (merged in PR #5; hardware bench validation below):

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
  - a hardware watchdog (two 1 s stages since the reset-cause fix below), an EEPROM
    boot counter and reset-cause reporting.
  No pins are assigned and no outputs are driven.
- **Tests at merge:** 272 Python tests (including real pyserial over pseudo-terminals against a
  Python Uno emulator) and 23 native firmware tests, with shared frame vectors.

This resolves audit findings H3, H6, M1, M7, L3 and L4.

USB-only bench tooling (merged in PR #6, [BENCH.md](BENCH.md)):

- `python -m glucose_guardian.bench` (`discover`, `run`, `watchdog`, `bootloader`,
  `authorize-watchdog`, `flash-record`) built on the production protocol, link, transport and runtime
  code, with JSON and Markdown reports (PASS / FAIL / NOT TESTED, physical or
  simulated evidence, redacted serial numbers).
- A separate `uno-watchdog-test` firmware build that announces itself and hangs on
  purpose, alternating interrupts-enabled and interrupts-disabled hangs. Its test code
  is compiled out of the production image.
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

Optiboot 4.4 reset-cause workaround (PR #6):

- Optiboot 4.4 clears MCUSR before the sketch starts, so reset flags are always empty.
- The firmware runs a two-stage watchdog (1 s interrupt, then 1 s reset). The
  interrupt writes a checked `.noinit` RAM marker, and the next boot reports `W` from
  it.
- Every other reset reports `U`, including a hang with interrupts disabled.

**M3 hardware bench validation is complete** for the firmware and the serial link over
USB. The results are owner-reported and the reports stay on the Pi; the full record is
in [BENCH.md](BENCH.md#validation-record). The bench: a Raspberry Pi 3B with Debian 13,
and a genuine Uno R3 (`2341:0043`) with stock Optiboot 4.4, boot section `e36d971b…`.

- **2026-11-07:** bootloader check PASS; automated and interactive suites PASS with
  the earlier production image.
- **2026-11-12, build `475deaa`:**
  - watchdog test PASS: both hang modes reset, gaps 2.243 s, causes `U` and `W` as
    expected;
  - production firmware `bde6b740…` restored, verified and installed;
  - noninteractive and interactive production runs PASS: exchanges 50 of 50 with p95
    about 20 ms; 5 of 5 resets, longest recovery 0.732 s; bursts 500 of 500; 600
    sustained ticks with at most 4.04 ms lateness; unplug, reconnect and backoff
    passed in the interactive run.
- An earlier watchdog run at `48a6c65` failed `watchdog.cause` (`U`, `U`), which led to
  the workaround.

The physical alarm system is not validated. NOT TESTED on this bench by design: the
Uno's link-lost alarm output, the siren and bed shaker, the button, power-loss
resilience and the display.

M4 Dexcom Share acquisition is implemented on `feat/m4-dexcom-share` and tested with
synthetic data only. No live Dexcom request has been made. See
[DEXCOM_SHARE.md](DEXCOM_SHARE.md) and the updated
[plan](plans/2026-11-07-m4-dexcom-share.md).

- **Account:** read-only access through the publisher account. Current pydexcom and
  Nightscout clients show the API has no follower login. No account, invitation or
  follower was created or changed.
- **Client:** HTTPS through stdlib `urllib` with certificate verification, a
  per-request deadline enforced on the socket (connect, TLS handshake, headers and
  body; DNS resolution excepted), a 64 KiB body cap, a declared-length check, refused
  redirects, ignored proxies and no hidden retries.
- **Login protection,** persisted without secrets across restarts:
  - at least 300 s between login attempts;
  - backoff on HTTP 429, cleared only by a read that passes the parser;
  - a latch on rejected credentials or lockout, cleared only by an operator;
  - a latch when a login's outcome was never recorded (crash, stop or failed write),
    so a lost rejection is not retried after a restart;
  - a 300 s wait for the first login after a reboot.
- **Parsing:** required entry keys with additional fields ignored, the `WT` reading
  time, values from 39 (LOW) to 401
  (HIGH) through the unchanged thresholds, and newest-wins handling of duplicates and
  out-of-order entries. The trend is kept for M5; `DexcomShareProvider.status` tells a
  blocked login apart from an outage.
- **Supporting tools:** a synthetic Share simulator, a read-only connectivity check
  (`share_check`) and `share_state status/unblock`.
- **Configuration:** owner decisions `ous`, 60 s polling, 600 s maximum age, 300 s
  login interval; the rate-limit values (120 s to 1800 s) are provisional.
- **Tests:** 212 M4 tests, 595 in total. No alarm policy, threshold, GG1, firmware or
  output change.
- **Review follow-up (2026-11-13):** the first external review of PR #7, at
  `bfb4582d`, requested changes. All five findings are resolved and the
  resolutions are recorded in section 11 of the plan. Testing them uncovered a sixth
  defect, now fixed: a body shorter than its `Content-Length` used to be accepted.

## Next milestone

- **Second external review of M4** (PR #7, `feat/m4-dexcom-share`).
- **The owner-approved read-only connectivity check** with the publisher credentials
  ([DEXCOM_SHARE.md](DEXCOM_SHARE.md)). It confirms the endpoints, the response shape,
  `WT` and `DT` agreement, and the trend format. Then a soak with outputs disabled.
- **M5 display:** data age, faults and the Share status, including blocked
  authentication.

## Known blockers and gaps

- Outstanding physical checks, each needing components, wiring review and approval:
  - the Uno link-lost alarm output;
  - buzzer and bed-shaker electrical integration;
  - the physical button;
  - power-loss resilience;
  - the Waveshare 800×480 display.
- With Optiboot 4.4, power-on, brown-out and external resets all report `U`. A
  watchdog reset while the Pi is disconnected is lost from `cause` when the Pi
  reconnects, because opening the port resets the Uno again.
- A restart forgets a retained alarm level during stale data (glucose becomes unknown
  while the missing-data fault sounds). Persisting it is an open M6 item.
- The systemd clock check has not been run on the Pi; the Dexcom provider depends on
  it.
- Dexcom Share is undocumented and has not been contacted. The response shape, the LOW
  and HIGH encoding, the session lifetime and the rate-limit behavior are assumptions
  until the connectivity check and real LOW or HIGH events confirm them.
- The publisher credentials on the Pi are protected only by the host key on the same
  SD card (no TPM).
- A service stop, crash or power loss during the few seconds of a Dexcom login blocks
  automatic login (`LoginOutcomeUnknown`) until an operator unblocks. This is a
  deliberate fail-closed choice.
- DNS resolution cannot be interrupted by the request deadline; a slow resolver
  delays a fetch by its own timeout.
- Electrical drivers, backup power, GPIO mappings, button wiring and Uno power topology
  are unselected.
- Physical outputs and button input require reviewed wiring and explicit approval.
- GUI and systemd installation are absent.
- Remaining audit findings: H5 (hardware review); M6 (M6); M9 (M5/M6); L5, and L6's
  heartbeat timeout defined separately on the Pi and the Uno.

No deployment has occurred. Official Dexcom and Omnipod alerts remain independent
and unchanged. This is a supplementary prototype, not a certified medical device.
