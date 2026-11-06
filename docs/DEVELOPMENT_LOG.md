# Development log

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
