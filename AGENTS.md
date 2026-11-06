# Engineering instructions

Use Python for the Pi and C++ for the Uno. Keep acquisition, validation, alarm policy,
display, and transport separate. Use simulated data in tests; never add credentials,
tokens, or patient data. Validate timestamps before evaluating glucose. Keep system
faults distinct from glucose alarms. Acknowledgement must expire and must not hide a
new or escalating event. Use monotonic time for timeouts and UTC for reading timestamps.

Do not invent pin assignments, electrical ratings, or clinical thresholds. Hardware
outputs stay disabled until the driver and wiring are reviewed. Preserve independent
official Dexcom and Omnipod alerts. This project is supplementary and is not a certified
medical device. Do not deploy without an explicit request.

Run `cd backend && python -m ruff check src tests && python -m mypy src && python -m pytest`
and, when available,
`cd firmware/arduino-uno && pio run`. Add transition, stale-data, failure, and recovery
tests when changing alarm behavior. Work on feature branches.

## Development handoff protocol

At the end of every implementation task:

1. Update `docs/DEVELOPMENT_LOG.md` with the changes made, architectural decisions, test results, and unresolved issues.
2. Update `docs/PROJECT_STATUS.md` with the current implementation status, next milestone, and known blockers.
3. Record any changes to hardware assumptions, GPIO mappings, alarm behavior, or communication protocols.
4. Run the relevant tests and document failures.
5. Commit changes on the current feature branch.
6. Provide a concise handoff containing the branch name, commit hash, changed files, test results, and pull request URL if available.

Never commit secrets, patient data, or actual glucose readings.

Do not deploy to physical hardware or modify alarm outputs without explicit approval.