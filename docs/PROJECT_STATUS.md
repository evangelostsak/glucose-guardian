# Project status

Updated: 2026-11-06

## Current implementation

`main` holds the initial scaffold and repository audit (PR #1) and M0 housekeeping
(PR #2): `GPL-3.0-or-later` license, `CLAUDE.md`, uppercase documentation names, and
CI running ruff, strict mypy and pytest on Python 3.11 and 3.13 plus the Uno build.
`main` is protected and requires one approving review; status checks are deliberately
not required.

M1 alarm policy v2 is complete on `feat/m1-alarm-policy-v2`, implementing the owner's
decisions of 2026-11-06:

- Glucose levels urgent low, low, normal, high and unknown, with a hysteresis margin.
  Unknown replaces a stale "normal"; an unresolved alarm level is kept while data is
  not fresh.
- Concurrent system faults: acquisition, missing data, invalid reading, clock
  unsynchronized, communication.
- Separate glucose and fault acknowledgement, capped at 15 minutes in code. Silence
  ends on escalation, on a further `rearm_mg_dl` worsening, or on a new or returning
  fault; improvement from urgent low to low stays silent.
- Readings up to 60 s in the future are accepted; validation lives in `validation.py`.
- 82 tests, including a seeded randomized invariant test.

This resolves audit findings C1, C2, H1, H2, H4, H7, M3, M5 and L2. Display and transport
are interfaces only; the engine is not yet driven by a runtime. Uno firmware compiles
and tracks heartbeat loss internally, but drives no pins and has no button input.

## Next milestone

M2 simulated end-to-end runtime: a fixed-rate supervisory loop calling the alarm engine,
an acquisition worker with hard timeouts, a scenario simulator for stale, delayed and
missing data, a stdlib `tomllib` loader for the git-ignored `config/local.toml`, a
clock-synchronization check, display and transport test doubles, and crash and
restart integration tests. It requires owner approval to start.

## Known blockers and gaps

- How the single physical button maps to the glucose and fault channels is open (M3).
- Electrical drivers, backup power, GPIO mappings, button wiring and Uno power topology
  are unselected.
- Physical outputs and button handling require reviewed wiring and explicit approval.
- Real Dexcom access is intentionally deferred; no credentials are configured.
- GUI, persistent service module, and Pi serial transport are absent.
- Remaining audit findings: H3 (M2), H5 (hardware review), H6, M1 and M7 (M3), M4 (M2),
  M6 (M6), M9 (M5/M6), L3 to L6.
- Hardware-in-loop validation remains outstanding.

No deployment has occurred. Official Dexcom and Omnipod alerts remain independent
and unchanged. This is a supplementary prototype, not a certified medical device.
