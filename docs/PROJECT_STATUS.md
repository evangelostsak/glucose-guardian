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

## Next milestone

Implement a simulated end-to-end runtime with configuration loading, display and
transport test doubles, supervised lifecycle, and integration tests for startup,
stale data, communication loss, acknowledgement, and recovery. Specify a versioned
serial protocol before connecting runtime behavior to hardware.

## Known blockers and gaps

- Electrical drivers, backup power, GPIO mappings, and button wiring are unselected.
- Physical outputs and button handling require reviewed wiring and explicit approval.
- Real Dexcom access is intentionally deferred; no credentials are configured.
- GUI, persistent service module, and Pi serial transport are absent.
- Operational alarm policy and hardware-in-loop validation remain outstanding.
- Local tests pass and firmware compiles; remote GitHub Actions has not been run.

No deployment has occurred. Official Dexcom and Omnipod alerts remain independent
and unchanged. This is a supplementary prototype, not a certified medical device.
