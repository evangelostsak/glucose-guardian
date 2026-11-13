# Project status

Updated: 2026-11-13

## Current implementation

`main` holds the initial scaffold and repository audit (PR #1), M0 housekeeping
(PR #2), M1 alarm policy v2 (PR #3), M2 simulated end-to-end runtime (PR #4), M3
serial protocol v1 (PR #5), the USB bench tooling with its hardware validation
(PR #6), M4 Dexcom Share acquisition (PR #7) and the root Makefile (PR #8). CI
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

**M4 Dexcom Share acquisition is complete and live-validated** (merged in PR #7). See
[DEXCOM_SHARE.md](DEXCOM_SHARE.md) and the
[plan](plans/2026-11-07-m4-dexcom-share.md), whose section 13 holds the live record.

- **Live validation (2026-11-13, owner-run on the Pi, owner-reported):**
  - environment: Raspberry Pi 3 Model B, Debian 13, Python 3.13.5; Share region
    `ous`; publisher credentials provisioned as encrypted systemd credentials;
  - `make dexcom-credentials-check`: both credentials decrypt and are well-formed;
  - `make dexcom-check`: authentication, login and one read succeeded on the first
    attempt, with no retry. The read returned two Dexcom G7 entries, each with `DT`,
    `ST`, `Trend`, `Value` and `WT`. Timestamp validation, the value-format check and
    trend recognition passed, and the parser accepted the response. The newest
    reading was 42 s old;
  - `make dexcom-status` afterwards: `blocked: no`, last login attempt (UTC)
    `2026-11-13T17:25:00.069100+00:00`, rate-limit backoff `None`;
  - no physical alarm output was activated; none exists yet;
  - not recorded anywhere: glucose values, credentials, account and session
    identifiers.
- **What the live check confirmed:** the `ous` endpoints, application id and request
  bodies, the `sessionId` spelling, the entry shape, `WT` as the time of a current
  reading, recognized trends and the accepted `User-Agent`.
- **Still unconfirmed:** the LOW and HIGH encodings, session lifetime and renewal,
  rate-limit behavior, error-response shapes, and continuous operation (the soak).

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
- **Read-only audit (2026-11-13):** `dexcom_share.check_request`, an explicit
  allowlist, is now checked before every request. It covers the three Share hosts, the
  authenticate, login and read operations, the read query and the documented bodies.
  Only POST is sent, redirects are refused, and only `share_client.py` can open network
  connections.
- **Tests:** 280 M4 tests, 663 in total. No alarm policy, threshold, GG1, firmware or
  output change.
- **Review follow-up (2026-11-13):** the first external review of PR #7, at
  `bfb4582d`, requested changes. All five findings are resolved and the
  resolutions are recorded in section 11 of the plan. Testing them uncovered a sixth
  defect, now fixed: a body shorter than its `Content-Length` used to be accepted.

**Root Makefile** (merged in PR #8). A `Makefile` at the repository root is the
entry point for development, CI, firmware, the bench, the Raspberry Pi and Dexcom
Share diagnostics. `make help` is the default target.

- **Delegation:** the bench and flash targets call `bench/Makefile`, and the Dexcom
  targets follow `DEXCOM_SHARE.md`. No application logic was duplicated.
- **Firmware:** `firmware-flash` is the only target that writes firmware. It needs
  `SERIAL_PORT`, `VARIANT` and a typed `flash <variant>`.
- **Dexcom:** `dexcom-check` is the only target that contacts Dexcom. It needs a
  terminal and a typed `contact dexcom`, and never retries. `dexcom-credentials`
  encrypts from a hidden prompt; credentials never pass through arguments, variables
  or the environment.
- **State file:** `dexcom-status`, the `dexcom-check` preflight and `share_check` read
  the path named in `DEXCOM_CONFIG`. An explicit `STATE_DIR` that disagrees is
  refused.
- **Credential rotation:** `dexcom-credentials-rotate CREDENTIAL=username|password`
  replaces a credential only after a typed confirmation, the same value entered twice,
  encryption and validation through systemd. Until then the current file stays.
  `dexcom-credentials-check` validates the provisioned pair.
- **CI:** now runs the same targets: `make setup lint typecheck test` and
  `make setup-firmware firmware-build firmware-test`.
- **Tests:** 88 Makefile tests; 761 in total.

**M5 PR A, offline display software** (2026-11-13, `feat/m5-display-pr-a`; section 11
of the [M5 plan](plans/2026-11-13-m5-hdmi-display.md)). Steps 1 to
7 of the plan, tested without display hardware:

- **Trend (finding F1 fixed):** each `Reading` carries its own trend, and
  `latest_trend` is gone. A stale or future-dated newer entry can no longer lend its
  trend to the value shown.
- **Frame and source:** frames carry the reading time, trend, data source, Dexcom
  status and outputs flag.
- **Snapshot:** the runtime writes a schema-1 JSON snapshot each tick (atomic, mode
  0640, at most 4096 bytes), next to the log display, through `FanOutDisplay`. A strict
  reader rejects any malformed snapshot.
- **Screen:** a pure classifier gives four states (LIVE, LAST KNOWN, NO CURRENT DATA
  after a 30-minute provisional cutoff, NO DATA). The Pillow renderer draws the
  800×480 dashboard.
- **Display process:** `python -m glucose_guardian.screen` writes PNG frames and sends
  systemd readiness, watchdog and stopping notifications.
- **Previews:** `make display-preview` writes 31 synthetic PNGs to
  `build/display-preview/`.
- **Configuration and dependencies:** a required `[display]` table; `pillow==13.0.0`;
  CI installs `fonts-dejavu-core`.
- **Unchanged:** alarm policy, thresholds, acknowledgement, GG1, firmware and the M4
  login protections.
- **Tests:** 1025 in total, 264 more than before. 19 of 19 mutations of the new
  safety rules are caught.

## Roadmap

Corrected on 2026-11-13 by the owner. The audit's roadmap
([2026-11-06](audits/2026-11-06-initial-audit.md#6-proposed-roadmap)) had M5 Display,
M6 Deployment hardening and M7 Hardware integration.

| Milestone | Scope | Status | Depends on |
|---|---|---|---|
| M0 to M3 | Housekeeping, alarm policy v2, simulated runtime, serial protocol v1 and firmware | Done; M3 bench-validated | |
| M4 Dexcom Share acquisition | Read-only Share client, login protection, connectivity check | Done; live-validated 2026-11-13 | |
| M5 HDMI display | Standalone bedside dashboard on the Waveshare 5-inch 800×480 display; display only | [Plan](plans/2026-11-13-m5-hdmi-display.md) approved with owner decisions (2026-11-13); PR A (offline software) implemented; PR B (Pi integration) not started | M4; a runtime supervised by systemd (D1, approved); the framebuffer smoke test G1 before the output backend is fixed; the login-state migration before the service starts (D9) |
| M6 Physical siren and alarm hardware | Siren driver and Uno alarm outputs, the button, the audible link-lost alarm, hardware-in-the-loop tests | Not started | Components purchased; wiring and electrical review, including the Uno power topology (audit finding H5); retained-level persistence; explicit approval |
| M7 Bed shaker and additional hardware | Bed-shaker driver, backup power and other peripherals | Not started | M6; components; wiring review |

Deployment hardening is no longer a milestone of its own:

- systemd supervision of the runtime is part of M5, because the display depends on it
  (D1, approved);
- persisting the retained alarm level is scheduled before physical alarm outputs
  (M6);
- the hardware review may require backup power before any audible output, which would
  move it from M7 to M6.

Older entries in [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md) keep their wording: "M6"
there means deployment hardening and "M7" hardware integration.

## Next milestone

- **M5 HDMI display:** the design and implementation plan,
  [plans/2026-11-13-m5-hdmi-display.md](plans/2026-11-13-m5-hdmi-display.md), was
  reviewed on 2026-11-13. The owner decisions are recorded in its section 9:
  - Pillow is the renderer; `/dev/fb0` becomes the output backend only after the
    smoke test G1 on the real panel;
  - a last known value is hidden after a display cutoff, provisionally 30 minutes;
  - the login state moves to the service's state directory by an exact, verified copy,
    never a reset;
  - the soak uses the simulated transport first.

  PR A, the offline software, is implemented and awaits the owner's review and commit.
  PR B needs the prerequisites in section 12 of the plan first:
  - the cross-process lock for the Dexcom login state;
  - the hardware smoke test G1, now that `/dev/fb0` was absent on the headless Pi;
  - the `[display]` table in the Pi's configuration;
  - `fonts-dejavu-core` on the Pi.

  PR B starts only with explicit approval.
- **Open from M4:**
  - the soak against Share with outputs disabled, part of the M5 Pi validation;
  - confirmation of the LOW and HIGH encodings when such a reading occurs.

## Known blockers and gaps

- Outstanding physical checks, each needing components, wiring review and approval:
  - the Uno link-lost alarm output;
  - buzzer and bed-shaker electrical integration;
  - the physical button;
  - power-loss resilience;
  - the Waveshare 800×480 display (M5: smoke test G1, then checks H1 to H14 in its
    plan). The display is still packaged.
- With Optiboot 4.4, power-on, brown-out and external resets all report `U`. A
  watchdog reset while the Pi is disconnected is lost from `cause` when the Pi
  reconnects, because opening the port resets the Uno again.
- A restart forgets a retained alarm level during stale data (glucose becomes unknown
  while the missing-data fault sounds). Persisting it is scheduled before physical
  alarm outputs (M6).
- The systemd clock check has not been run on the Pi; the Dexcom provider depends on
  it.
- Dexcom Share is undocumented. One read-only connectivity check passed on 2026-11-13.
  The LOW and HIGH encoding, the session lifetime, the rate-limit behavior and the
  error responses remain assumptions until real events confirm them.
- The connectivity check used the state file in `~/glucose-guardian-state`. When the
  service is installed, the file moves to `/var/lib/glucose-guardian` by the procedure
  in section 3.8 of the M5 plan: an exact copy with mode 0600, verified, never reset
  or unblocked. Afterwards the check and the service use that one file and never run
  at the same time.
- Pi inspection, 2026-11-13: headless (`multi-user.target`, no display manager);
  `/dev/dri/card0` and `/dev/dri/renderD128` present; `/dev/fb0` absent with no display
  connected. Whether a framebuffer appears with the panel attached is unknown; G1
  decides between `/dev/fb0` and a KMS/DRM output.
- `AuthStateStore` loads the login state once and replaces the file on each change, so
  two processes on one state file can overwrite each other's cooldown, backoff or
  block. A cross-process lock (plan section 12.2) is mandatory before the service and
  the check share a state file and before continuous live monitoring.
- `[display]` is now a required configuration table. The Pi's existing
  `config/local.dexcom.toml` lacks it, so `make dexcom-status`, `make dexcom-check` and
  the Dexcom line of `make status` stop with a configuration error until the block
  from `config/example-dexcom-share.toml` is added.
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
- The display software exists offline only: no framebuffer or DRM output, no systemd
  units and no installation yet (M5 PR B, D1 approved).
- Remaining audit findings:
  - H5, the Uno power topology: hardware review before M6;
  - M6, systemd supervision: part of M5 (D1, approved);
  - M9, Pi platform risks: the display stack, blanking and night brightness in M5,
    under-voltage observed during the M5 Pi validation, SD-card wear open;
  - L5;
  - L6, the heartbeat timeout defined separately on the Pi and the Uno.

No deployment has occurred. Official Dexcom and Omnipod alerts remain independent
and unchanged. This is a supplementary prototype, not a certified medical device.
