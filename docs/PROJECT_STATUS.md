# Project status

Updated: 2026-11-06

## Current implementation

The initial scaffold and the repository audit were merged to `main` in PR #1.
Python packaging, synthetic acquisition, timestamp validation, deterministic alarm
policy, bounded acknowledgement, and 20 automated tests are implemented.
Display and transport are interfaces only. Uno firmware compiles and tracks
heartbeat loss internally, but drives no pins and has no button input.
Safety, architecture, hardware, example configuration, and an inactive systemd
template are documented.

The repository audit of 2026-11-06,
[audits/2026-11-06-initial-audit.md](audits/2026-11-06-initial-audit.md), found two
critical defects in the alarm engine: hyperglycemia and implausible values evaluate as
normal (C1), and the acknowledgement window has no upper bound (C2).

M0 housekeeping is complete on `chore/m0-housekeeping`:

- Licensed `GPL-3.0-or-later`: `LICENSE`, README notice, SPDX field in `pyproject.toml`.
- `CLAUDE.md` importing AGENTS.md.
- `docs/architecture.md`, `docs/safety.md` and `docs/hardware.md` renamed to
  `ARCHITECTURE.md`, `SAFETY.md` and `HARDWARE.md`.
- CI runs ruff, mypy (strict) and pytest on Python 3.11 and 3.13, plus the Uno build.
- `main` is protected and requires one approving review; status checks are deliberately
  not required.

## Next milestone

M1 alarm policy v2: hyperglycemia and plausibility bounds, explicit unknown glucose,
concurrent faults, per-channel bounded acknowledgement, clock-validity fault, with
table-driven transition, stale-data, failure and recovery tests. The simulated
end-to-end runtime follows as M2.

## Known blockers and gaps

- M1 is blocked on owner decisions (audit section 7): hyperglycemia thresholds,
  plausibility bounds, maximum acknowledgement window, acknowledgement semantics.
- Alarm engine critical defects C1 and C2 are open.
- Electrical drivers, backup power, GPIO mappings, and button wiring are unselected.
- Physical outputs and button handling require reviewed wiring and explicit approval.
- Real Dexcom access is intentionally deferred; no credentials are configured.
- GUI, persistent service module, and Pi serial transport are absent.
- Operational alarm policy and hardware-in-loop validation remain outstanding.

No deployment has occurred. Official Dexcom and Omnipod alerts remain independent
and unchanged. This is a supplementary prototype, not a certified medical device.
