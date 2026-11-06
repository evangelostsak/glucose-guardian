# Project status

Updated: 2026-11-06

## Current implementation

`main` holds the initial scaffold and repository audit (PR #1), M0 housekeeping
(PR #2) and M1 alarm policy v2 (PR #3). CI runs ruff, strict mypy and pytest on
Python 3.11 and 3.13 plus the Uno build. `main` is protected and requires one
approving review; status checks are deliberately not required.

M1 provides urgent low, low, normal, high and unknown glucose levels with hysteresis,
concurrent system faults, and separate glucose and fault acknowledgement capped at
15 minutes.

M2 simulated end-to-end runtime is complete on `feat/m2-simulated-runtime`:

- Fixed-rate supervisory loop on monotonic deadlines; alarm evaluation continues while
  acquisition is delayed, blocked or failing.
- Bounded workers for acquisition and the clock check: one job in flight, hard
  timeout, a hung job costs one daemon thread and never blocks the loop or exit.
- Strict TOML configuration (`config/local.toml`, git-ignored): every value required,
  unknown keys rejected, outputs locked off.
- Clock check via `timedatectl`; unknown or failed means untrusted.
- Eleven deterministic scenarios covering normal, hypoglycemia, hyperglycemia, stale,
  missing and invalid data, acquisition errors and timeouts, clock failure,
  communication loss, and simultaneous faults.
- Display frame model (with "last known" and data age), log display, in-memory display
  and simulated transport doubles.
- `python -m glucose_guardian.service --config <file>` runs a scenario in real time
  with clean SIGTERM shutdown and exit codes 0/1/2.
- Owner M3 decision recorded and wired: a button press acknowledges both audible
  channels independently.
- 156 tests, including integration tests for startup, shutdown, crashes, restarts,
  simultaneous faults, acknowledgement expiry and recovery.

The M1 alarm policy is unchanged. This resolves audit findings M4 and the runtime half
of H3 (fault detection no longer depends on reading arrival). Uno firmware compiles
and tracks heartbeat loss internally, but drives no pins and has no button input.

## Next milestone

M3 serial protocol v1 and firmware refactor, outputs still disabled: versioned state
frames with sequence number and checksum sent from the supervisory loop (completing
H3), Pi-side Uno liveness, a host-testable firmware parser, the AVR watchdog, startup
grace and DTR reset handling, the button event protocol with debounce, and
Uno-reported hardware faults. It requires owner approval to start.

## Known blockers and gaps

- A restart forgets a retained alarm level during stale data (glucose becomes unknown
  while the missing-data fault sounds). Persisting it is an open M6 item.
- The systemd clock check has not been run on the Pi; the simulated service takes
  clock status from the scenario.
- Electrical drivers, backup power, GPIO mappings, button wiring and Uno power topology
  are unselected.
- Physical outputs and button handling require reviewed wiring and explicit approval.
- Real Dexcom access is intentionally deferred; no credentials are configured.
- GUI, Pi serial transport and systemd installation are absent.
- Remaining audit findings: H3 heartbeat half, H6, M1, M7 (M3); H5 (hardware review);
  M6 (M6); M9 (M5/M6); L3 to L6.
- Hardware-in-loop validation remains outstanding.

No deployment has occurred. Official Dexcom and Omnipod alerts remain independent
and unchanged. This is a supplementary prototype, not a certified medical device.
