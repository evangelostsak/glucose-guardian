# Development log

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
