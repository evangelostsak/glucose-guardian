# Project status

Updated: 2026-11-06

## Current implementation

Initial development scaffold is complete on `feat/initial-project-structure`.
Python packaging, synthetic acquisition, timestamp validation, deterministic alarm
policy, bounded acknowledgement, and 20 automated tests are implemented.
Display and transport are interfaces only. Uno firmware compiles and tracks
heartbeat loss internally, but drives no pins and has no button input.
CI covers pytest and Uno compilation. Safety, architecture, hardware, example
configuration, and an inactive systemd template are documented.

A repository audit was completed on 2026-11-06:
[audits/2026-11-06-initial-audit.md](audits/2026-11-06-initial-audit.md). It found two
critical defects in the alarm engine: hyperglycemia and implausible values evaluate as
normal (C1), and the acknowledgement window has no upper bound (C2).

## Next milestone

Proposed by the audit, awaiting owner approval:

1. M0 housekeeping: `CLAUDE.md`, rename `architecture.md`/`safety.md` to uppercase,
   license, scaffold PR, branch protection, CI on Python 3.11 and 3.13 with lint and types.
2. M1 alarm policy v2: hyperglycemia and plausibility bounds, explicit unknown glucose,
   concurrent faults, per-channel bounded acknowledgement, clock-validity fault.

The simulated end-to-end runtime previously proposed here becomes M2, because it would
otherwise build on the current alarm state model. M1 is blocked on owner decisions listed
in section 7 of the audit.

## Known blockers and gaps

- Electrical drivers, backup power, GPIO mappings, and button wiring are unselected.
- Physical outputs and button handling require reviewed wiring and explicit approval.
- Real Dexcom access is intentionally deferred; no credentials are configured.
- GUI, persistent service module, and Pi serial transport are absent.
- Operational alarm policy and hardware-in-loop validation remain outstanding.
- Local tests pass and firmware compiles; the first GitHub Actions run (42221247588)
  was still queued on 2026-11-06 and no remote run has completed.
- Alarm engine critical defects C1 and C2 are open (see audit).

No deployment has occurred. Official Dexcom and Omnipod alerts remain independent
and unchanged. This is a supplementary prototype, not a certified medical device.
