# Development log

## 2026-11-07 — Device-bound bootloader authorization

### Changes and decisions

- Review finding: the watchdog-test interlock relied on a plain marker file,
  `bench/reports/bootloader-ok`, that was not tied to any board, so a check on one
  Uno could authorize flashing another.
- New `glucose_guardian.bench.authorization`. `make bootloader` now runs the read-only
  avrdude dump itself, between two identity resolutions. It writes
  `bench/reports/bootloader-authorization.json` with:
  - the `/dev/serial/by-id` link (serial redacted), VID:PID, USB port path, kernel
    device number and a digest of the serial number;
  - the Optiboot classification, version, marker and boot-section digest;
  - the host boot id, UTC and `CLOCK_BOOTTIME` verification times;
  - the build commit and `SHA256SUMS` digest, and the harness and avrdude versions;
  - an integrity digest.
- `make flash VARIANT=watchdog-test` keeps `CONFIRM=watchdog-test` and replaces the
  marker test with `authorize-watchdog`. It rejects a record that is missing,
  unreadable, malformed, modified, of another schema, from an earlier host boot, older
  than 900 s or dated in the future, for another link, USB id, port, device number or
  serial, for another build, or without Optiboot 4 or later.
- Decision: every record is bound to the current USB connection (port path and device
  number), not only records for boards without a serial number. Any reconnect
  therefore needs a new check. This closes cloned-serial mix-ups, and the check takes
  seconds and only reads.
- `DEVICE` must be a `/dev/serial/by-id` link for `bootloader` and the watchdog-test
  flash; tty names are refused. Boards sharing VID:PID and serial number (or both
  lacking one) are refused while both are attached.
- The `bootloader-version` command is replaced by `bootloader` and
  `authorize-watchdog`. The Makefile's avrdude arguments were reordered to match the
  harness read command; a test keeps them equal.
- Validity window 900 s: re-checking is cheap, and the window bounds an undetected ISP
  bootloader replacement during a session. It is a bench procedure value, not a
  hardware limit.

### Hardware and protocol record

- No firmware, protocol, GPIO, actuator or alarm-policy change. Production hex
  unchanged (sha256 `4b36ab28…`, identical to `main`).
- Documented in `docs/BENCH.md`: opening the port resets the Uno through DTR. After
  that reset the production sketch increments its EEPROM boot counter. The bootloader
  read itself issues no erase, write, verify, fuse or EEPROM operation.

### Validation

- Python 3.11.18 and 3.13.18: ruff clean, mypy strict clean (23 files), pytest 331
  passed. That includes 44 new authorization tests (replacing the 5 `bootloader-version`
  cases) covering:
  - valid authorization;
  - the exact read-only avrdude command;
  - failed classification, avrdude failure and a connection change during the read;
  - another board, another serial and a reconnect (with and without a serial number);
  - age, future dating, host reboot and build change;
  - modified, re-signed but invalid, and malformed records;
  - tty names, dangling links, non-USB ports and ambiguous identities;
  - a Makefile run with a fake avrdude, showing avrdude never runs a memory operation
    without `CONFIRM` and a valid authorization.
- Mutation check: removing any of 16 safeguards (13 in the module, 3 in the Makefile)
  fails the suite.
- Firmware: `pio run -e uno` and `-e uno-watchdog-test` succeed; `pio test -e native`
  23 of 23.
- **No hardware validation yet.** Nothing has run against a physical Uno.

### Unresolved issues

- The integrity digest detects edits, not deliberate forgery by a local user.
- A board swap between the authorization check and avrdude opening the port, or an
  ISP bootloader replacement without a reconnect, is not detected.

## 2026-11-06 — USB-only Uno bench tooling

### Changes and decisions

- New `glucose_guardian.bench` package:
  - `discovery` lists USB serial devices with Uno VID:PID recognition and serial-number
    redaction;
  - `probe` runs bounded real-time exchanges through the production encoder and `Link`;
  - `checks` holds the suite, the watchdog test and the `HARDWARE` profile;
  - `report` writes JSON and Markdown.
- Checks report PASS, FAIL or NOT TESTED, with evidence `physical-usb`, `simulated` or
  `flash-record`. Reports against non-USB targets carry a "not hardware validation"
  banner.
- Unexpected and repeated resets are driven by DTR pulses on hardware. USB
  disconnect, backoff and reconnect use the production `SerialTransport` and need an
  interactive run with physical cable access.
- The Uno's link-lost alarm is reported as NOT TESTED: nothing is observable without
  outputs. A diagnostic report flag is proposed for separate approval.
- Watchdog test: separate PlatformIO env `uno-watchdog-test` (`-DGUARDIAN_WATCHDOG_TEST`)
  prints an identifying banner and hangs 20 s after boot. The production hex is
  byte-identical to `main` (sha256 `4b36ab28…`). Flashing it requires
  `CONFIRM=watchdog-test` and a passed read-only bootloader check (Optiboot ≥ 4),
  because older bootloaders can reset-loop after a watchdog reset.
- `bench/Makefile` covers build, deploy (rsync), setup (Debian packages and a venv),
  discover, bootloader, flash (checksum-verified, flash record), run,
  run-interactive, watchdog, restore and collect.
- The test emulator `FakeUno` now follows the firmware's acceptance rules (newer
  frames per session, a session change accepted at once) and can emit unsolicited
  lines.
- CI also builds the watchdog-test image.

### Hardware and protocol record

- No GPIO assignment, actuator activation, alarm-policy or protocol change. The
  watchdog-test banner is documented in `docs/PROTOCOL.md`.

### Validation

- Python 3.11.18 and 3.13.18: ruff clean, mypy strict clean (22 files), pytest 292
  passed. The 20 bench tests passed 10 consecutive runs.
- Firmware: `pio run -e uno` and `-e uno-watchdog-test` succeed; `pio test -e native`
  23 of 23.
- `make build` produced both images; Makefile guards verified for missing variables,
  missing `CONFIRM`, a missing bootloader check, unknown variants and a tampered image.
- **No hardware validation yet.** Nothing has run against a physical Uno.

### Unresolved issues

- Hardware execution on the Pi is pending (flashing needs owner approval).
- Uno link-lost alarm observability needs a protocol diagnostic or outputs.

## 2026-11-06 — M3 review fix: backoff across short-lived connections

### Changes and decisions

- Review finding (PR #5): every successful open reset the reconnect backoff, so a
  device that opens but fails immediately was reopened every `reconnect_initial_s`,
  resetting the Uno via DTR and writing its EEPROM boot counter each time.
- The backoff now resets only when a connection fails after staying up for at least
  `reconnect_max_s`; shorter connections keep doubling towards the cap. No new setting:
  a connection that outlives the longest retry interval counts as stable.

### Hardware and protocol record

- Reconnect behavior updated in `docs/PROTOCOL.md` and `docs/ARCHITECTURE.md`. With the
  example backoff (1 s to 30 s), a persistently failing device is reopened at most about
  twice a minute.

### Validation

- Python 3.11.18 and 3.13.18: ruff clean, mypy strict clean, pytest 272 passed.
- New regression tests: repeated successful opens each followed by an immediate write
  or read failure retry at 0, 1, 3, 7, 11, 15 s (initial 1 s, cap 4 s); a connection
  that lasted the cap resets the backoff and doubling restarts at 12, 14, 18 s.
- Mutation check: restoring reset-on-open, never resetting, and a zero stability
  threshold each fail the suite.
- Firmware unchanged.

## 2026-11-06 — M3 review fix: incomplete serial writes

### Changes and decisions

- Review finding (PR #5): `SerialTransport.send()` ignored the byte count returned by
  pyserial's non-blocking `write()`. Reproduced against pyserial 3.5 on a pseudo-terminal:
  after 820 complete frames a write returned 12 of 26 bytes without raising.
- A second defect found while reproducing: with a completely full buffer, pyserial's
  `write()` retries `EAGAIN` in a loop and never returns, which would have frozen the
  supervisory loop.
- New `PyserialPort` adapter: a zero-timeout `select` guard returns 0 instead of
  calling pyserial when there is no room; `discard_output()` maps `termios.error` to
  `SerialException`. The transport depends on a small `Port` protocol.
- Any zero, partial or failed write is a failed transmission: counted
  (`incomplete_writes` / `failed_writes`), unsent output discarded, port closed,
  reconnect with backoff. A write timeout now also reconnects instead of only being
  counted.
- `Link.next_seq()` only allocates; `Link.sent()` records the send time after a
  complete write, so a failed frame can never be acknowledged into liveness.
- Alarm policy, acknowledgement semantics, protocol format and firmware behavior
  unchanged; the firmware gained a test for a truncated frame joined with the next one.

### Hardware and protocol record

- Pi transmit-failure handling added to `docs/PROTOCOL.md`; the bench checklist gained
  a stalled-device close check.

### Validation

- Python 3.11.18 and 3.13.18: ruff clean, mypy strict clean, pytest 269 passed. The
  serial transport tests passed 20 consecutive runs.
- Firmware: `pio test -e native` 23 of 23; `pio run -e uno` unchanged at 382 B RAM and
  3534 B flash.
- Mutation check: eight deliberate defects in the fix each fail the suite. Removing the
  `select` guard initially went undetected, because the transport test disconnects on
  the first partial write before the spin can occur; a direct adapter test now
  catches it in 2 s.

### Unresolved issues

- Whether `close()` on a stalled real USB-serial device returns promptly after the
  output discard needs bench validation.
- Persistent incomplete writes cause a reconnect, and therefore a DTR reset and EEPROM
  boot-counter write, per backoff interval.

## 2026-11-06 — M3 serial protocol v1

### Changes and decisions

- Owner decisions:
  - separate `glucose_acknowledgement_s` and `fault_acknowledgement_s`, each capped
    at 900 s;
  - a short press silences glucose only, and the display shows "Hold 2 s to silence"
    when only faults sound;
  - the Uno's standalone link-lost silence lasts at most 60 s, re-arms automatically
    and cannot be extended;
  - timings: 50 ms debounce, 2 s long press, 30 s stuck, 2 s watchdog, 10 s grace;
  - pyserial for the byte transport;
  - PC-only: no pins, outputs or flashing.
- Superseded M2 decision: a press no longer silences both channels.
- Protocol v1 (`docs/PROTOCOL.md`, new): CRC-16/CCITT-FALSE ASCII frames of at most
  64 bytes with `GG1` version, Pi session and sequence, and Uno boot counter and
  sequence. The state frame is the heartbeat. Button presses travel as cumulative
  counters so a lost report loses no press.
- Pi: new `protocol.py` (pure framing and validation), `link.py` (pure link state:
  ack window of 16, round-trip liveness, duplicate and replay rejection, reset
  detection, counter deltas) and `serial_transport.py` (pyserial, bounded reads,
  non-blocking writes, exponential backoff). The `AlarmTransport` protocol became
  `receive()` / `send(state, monotonic_s)`.
- Alarm engine: `update()` takes the required `controller_faults` argument; new faults
  `controller_reset` (latched by the runtime until a long press, the one fault a press
  clears because it is an event) and `button_stuck`.
- Firmware: `lib/guardian/` (protocol, `PiLink`, `Button`, reset cause, `Controller`)
  in C++11 without Arduino headers or the STL, with 22 native Unity tests sharing frame
  vectors with Python. `main.cpp` captures reset flags in `.init3`, enables the 2 s
  watchdog, increments an EEPROM boot counter and computes the alarm demand without
  driving pins. A stuck press never acknowledges anything; long presses are
  classified on release.
- A classification bug was found by the cross-language tests and fixed on both sides:
  a body of just `GG1` had been reported as a version mismatch instead of malformed.
- Configuration: required `[transport]` table (`kind = "simulated"`, or `"serial"`
  with `device` and reconnect timing). `pyserial==3.5` became the first runtime
  dependency; `types-pyserial` joined the lint extra.
- CI: the firmware job builds `-e uno` and runs `pio test -e native`.

### Hardware and protocol record

- Serial protocol replaced: the initial scaffold's one-character `H`/`N`/`G`/`F` bench
  protocol is gone; v1 is specified in `docs/PROTOCOL.md`.
- Provisional firmware timing recorded in `docs/HARDWARE.md`. No GPIO assignment; the
  button input is fixed at released. EEPROM address 0 holds the boot counter.
- Opening the port resets the Uno via DTR; the stock Optiboot bootloader may hide the
  reset cause, so resets are detected by the boot counter.

### Validation

- Python 3.11.18 and 3.13.18: ruff clean, mypy strict clean (16 files), pytest 257
  passed.
- Firmware: `pio test -e native` 22 of 22 passed with `-Wall -Wextra -Werror
  -pedantic`; `pio run -e uno` succeeded at 382 B RAM and 3534 B flash.
- Mutation check: 18 Python and 12 firmware deliberate defects each made the
  suite fail.
- Not validated: anything on a physical Uno (PC-only by owner decision).

### Unresolved issues

- Bench validation of the watchdog reset, reset causes, serial timing and DTR
  behavior (needs approval).
- The heartbeat timeout is defined separately on the Pi (`heartbeat_timeout_s`) and
  the Uno (5 s constant).
- Restart persistence of the retained alarm level (M6).

## 2026-11-06 — M2 simulated end-to-end runtime

### Changes and decisions

- New modules: `worker.py` (bounded jobs), `clock.py` (timedatectl check), `config.py`
  (strict TOML), `simulation.py` (scenarios), `runtime.py` (supervisory loop),
  `service.py` (simulated entry point). `display.py` gained the `Frame` model,
  `LogDisplay` and `MemoryDisplay`; `transport.py` gained `take_button_presses()` and
  `SimulatedTransport`; `GlucoseProvider.read` may return `None` (no new reading).
- Workers use daemon threads with one job in flight. Threads cannot be killed, so a
  timed-out job keeps its slot until it returns and its late result is discarded;
  this bounds threads to one per worker and lets the process exit with a hung fetch.
  `ThreadPoolExecutor` was rejected because its threads are joined at interpreter
  exit, so a hung fetch would block shutdown.
- The clock check runs in its own bounded worker so a slow `timedatectl` cannot
  stall a tick. Unknown, failed or timed-out checks mean untrusted; the clock starts
  untrusted. `acquisition_ok` starts true because nothing has failed yet and missing
  data is already a fault.
- Display and transport exceptions are counted and survived so alarm evaluation
  continues; a heartbeat query failure counts as infinite age. Engine defects
  propagate and the service exits with status 1.
- Configuration: every value is required; the only default is the fail-safe one (a
  missing `[hardware]` table means outputs disabled, and `true` is rejected). Runtime
  cross-checks keep the tick below the heartbeat timeout and fetches inside the data
  age limit. `provider` accepts only `"simulated"`.
- Logs contain levels and fault names, never glucose values.
- Owner M3 decision: a short button press acknowledges both audible channels
  independently. Implemented in the runtime via `take_button_presses()`; debounce and
  the serial event protocol stay in M3.
- The M1 alarm policy is unchanged; no defect required a correction.

### Hardware and protocol record

- No changes to hardware assumptions, GPIO mappings or the serial protocol. The
  transport boundary gained button-press intake; the simulated transport opens no
  serial port. Stopping the service stops heartbeats, which the Uno will treat as a
  communication fault.

### Validation

- Python 3.11.18 and 3.13.18: ruff clean, mypy strict clean (13 files), pytest 156
  passed. The thread and subprocess tests passed 30 consecutive runs.
- Mutation check: thirteen deliberate defects each made the suite fail. One mutation
  (clock trusted before the first check) initially survived and gained a test.
- Service smoke runs (`normal`, `missing_data`, `clock_failure`): correct state logs,
  exit 0 on SIGTERM, exit 2 on a missing config. With five fetches and five clock
  checks per second, RSS held at about 21 MB with one live thread over 160 ticks.
- `pio run`: success, firmware unchanged.

### Unresolved issues

- Restart forgets a retained alarm level during stale data (M6 persistence).
- The `timedatectl` check is unit-tested only; it must be validated on Debian 13 on
  the Pi.
- Button debounce and event protocol, Uno-reported faults, heartbeat as a state frame
  (M3).

## 2026-11-06 — M1 alarm policy v2

### Changes and decisions

- Owner decisions: levels urgent low, low and high (no urgent high); separate glucose and
  fault acknowledgement; improvement while silenced stays quiet; further worsening
  re-arms after a configurable `rearm_mg_dl`; hysteresis with a required margin;
  15-minute hard acknowledgement cap; 60 s future-timestamp tolerance; no special
  handling outside the G7 40–400 mg/dL display range.
- Design confirmations: the re-arm amount applies to every glucose alarm (falling for
  lows, rising for high); one `acknowledgement_s` serves both channels; an unresolved
  alarm level keeps sounding while data is not fresh.
- New `validation.py` separates value and timestamp checks from alarm policy.
  Non-numeric and boolean values are now invalid instead of crashing or classifying.
- `alarms.py` rewritten: `GlucoseLevel` (renamed `CRITICAL` to `URGENT_LOW`, added
  `HIGH` and `UNKNOWN`), `Fault` set with the new `CLOCK` and `MISSING_DATA`
  (replacing `STALE`), and per-channel acknowledgement records with expiry, re-arm
  baseline and shrinking fault set.
- `Policy` fields renamed with units (`urgent_low_mg_dl`, `low_mg_dl`, `high_mg_dl`,
  `hysteresis_mg_dl`, `rearm_mg_dl`, `max_future_skew_s`), all required, with ordering,
  band-overlap and 15-minute cap validation.
- `update()` takes every input as a required keyword: `acquisition_ok` no longer
  defaults to true, and the new `clock_synchronized` is required. A naive `now` raises.
- `State` reports `glucose`, `glucose_current`, `faults`, per-channel silenced flags,
  and `glucose_audible` / `fault_audible`.
- `config/example.toml` updated with synthetic values for the new fields; a test keeps
  it a valid `Policy`.
- ARCHITECTURE.md rewritten for validation, levels, faults, acknowledgement and policy;
  SAFETY.md gained a failure-mode table.

### Hardware and protocol record

- Alarm behavior changed as above. No changes to hardware assumptions, GPIO mappings
  or the serial protocol. The button-to-channel mapping is deferred to M3.

### Validation

- Python 3.11.18 and 3.13.18: ruff clean, mypy strict clean (7 files), pytest 82 passed.
- Mutation check: ten deliberate defects (no 15-minute cap, acknowledgement covering
  every level, no re-arm, fault set not shrinking, stale normal kept, no hysteresis,
  zero future skew, communication masking other faults, no high level, clock ignored)
  each made the suite fail.
- `pio run`: success, 191 B RAM, 1644 B flash (firmware unchanged).

### Unresolved issues

- The engine is not yet driven by a runtime; M2 supplies the loop, configuration
  loading and clock check.
- Button-to-channel mapping and Uno-reported hardware faults (M3).

## 2026-11-06 — M0 housekeeping

### Changes and decisions

- Licensed the project `GPL-3.0-or-later` (owner's choice): canonical GPL-3.0 text in
  `LICENSE`, the "or any later version" notice in README, and `license =
  "GPL-3.0-or-later"` in `pyproject.toml`. The SPDX field needs setuptools 77 or newer,
  so the build requirement rose from `>=68` to `>=77`.
- Added `CLAUDE.md`, which imports AGENTS.md so engineering rules have one source.
- Renamed `docs/architecture.md`, `docs/safety.md` and `docs/hardware.md` to
  `ARCHITECTURE.md`, `SAFETY.md` and `HARDWARE.md` with `git mv`; README links and the
  audit's link to the hardware document updated (link target only).
- CI: the Python job is a 3.11 and 3.13 matrix running ruff, strict mypy and pytest from
  `backend/`. Lint tools are pinned in a `lint` extra (ruff 0.17.4, mypy 2.4.1).
- Ruff selects E, F, W, I, B with line length 100, so the two existing 89–90 character
  lines in `alarms.py` need no rewrapping.
- Code changes are lint-only: import grouping in three modules and a `-> None` annotation
  on `Policy.__post_init__`. No behavior change.
- Owner kept status checks unrequired on the protected `main`.

### Hardware and protocol record

- No changes to hardware assumptions, GPIO mappings, alarm behavior or the serial protocol.

### Validation

- GitHub Actions passed on `4318378` (runs 42221247588, 42222916647, 42223329610); the
  earlier "still queued" status is resolved.
- Python 3.11.18 and 3.13.18, fresh environments with `.[test,lint]`: ruff clean, mypy
  strict clean (6 files), pytest 20 passed.
- `yamllint` on `ci.yml`: clean.
- Wheel builds: setuptools 76.1.0 rejects the SPDX `license` string; 77.0.1 and 77.0.3
  emit `License-Expression: GPL-3.0-or-later` (PyPI has no 77.0.0).
- `pio run`: success, 191 B RAM, 1644 B flash (firmware unchanged).

### Unresolved issues

- M1 owner decisions listed in audit section 7.
- CI still triggers on both push and pull_request, and actions are pinned by tag (audit L5).

## 2026-11-06 — Repository audit

### Changes and decisions

- Audited the Codex scaffold at commit `a4cf086`.
- Added [audits/2026-11-06-initial-audit.md](audits/2026-11-06-initial-audit.md): inventory,
  architecture assessment, 2 critical, 7 high, 10 medium and 6 low findings, roadmap M0–M7.
- No source, firmware, test, CI or configuration changes. Recommended next work is M0
  housekeeping then M1 alarm policy v2, pending owner approval and threshold decisions.

### Hardware and protocol record

- No changes to hardware assumptions, GPIO mappings, alarm behavior or the serial protocol.
- Audit flags for later review: Uno power topology (single audible annunciator), latching
  `G`/`F` frames, DTR auto-reset on port open, and the absence of an AVR watchdog.

### Validation

- pytest: 20 passed on Python 3.14.4 (pytest 9.0.2) and on Python 3.11.18.
- PlatformIO 6.1.18 / atmelavr 5.1.0: Uno build succeeded; 191 bytes RAM, 1644 bytes flash.
- `mypy --strict`: 1 error (missing return annotation). `ruff`: 5 style findings.
- GitHub Actions run 42221247588 still queued; remote CI has not completed.

### Unresolved issues

- Critical C1 (hyperglycemia and implausible values evaluate as normal) and C2 (unbounded
  acknowledgement window) are open.
- Owner decisions needed before M1: hyperglycemia thresholds, plausibility bounds, maximum
  acknowledgement window, acknowledgement semantics, license.

## 2026-11-06 — Initial scaffold and development handoff

### Changes and decisions

- Added README, engineering instructions, architecture, safety, and electrical-interface documentation.
- Added Python packaging, deterministic simulated provider, alarm state machine, and display/transport interfaces.
- Kept UTC timestamp validation ahead of glucose evaluation and monotonic time for acknowledgement and communication timeouts.
- Separated glucose severity from system faults; startup is an acquisition fault. Invalid or stale data retains the last glucose alarm.
- Acknowledgement expires and resets when glucose severity or fault changes.
- Added PlatformIO Uno scaffold, CI for Python and firmware, inactive systemd template, secret-free example configuration, and ignore rules.
- Applied the AGENTS.md handoff protocol with this log and PROJECT_STATUS.md.

### Hardware and protocol record

- Confirmed target remains Pi 3 Model B / Debian 13, USB-connected Uno, 800×480 HDMI LCD, external 12 V siren and bed shaker, and acknowledgement button.
- No GPIO assignments or electrical ratings introduced. Drivers and backup power remain unselected; actuator outputs remain disabled.
- Experimental serial protocol: 115200 baud, single-character LF-terminated H/N/G/F frames; H receives HEARTBEAT. Startup or more than five seconds without a heartbeat requests communication fault internally. No physical fault signal is implemented.
- No hardware deployment, real Dexcom integration, credentials, or patient data added.

### Validation

- Editable Python package installation succeeded during initial setup.
- Latest pytest run: 20 passed on Python 3.14.4 with pytest 9.2.0.
- Latest PlatformIO run: Uno build succeeded with atmelavr 5.1.0 and PlatformIO 6.1.18; 191 bytes RAM, 1644 bytes flash.
- `git diff --check` passed before handoff documentation was added; repeated before commit.
- The execution environment emitted stream-fd permission warnings; both test/build commands exited successfully. No test failures.
- CI is configured for Python 3.11; remote CI execution has not been observed.

### Unresolved issues

- Runtime/config loading, GUI, Pi serial adapter, real acquisition, button input, and actuator drivers are not implemented.
- The service unit is a template referencing an absent service module; it must not be installed yet.
- The bench protocol needs versioning, corruption/reconnect handling, state acknowledgement, and hardware-in-loop validation.
- Thresholds are synthetic test values; clinical policy and electrical design require review before operational use.
