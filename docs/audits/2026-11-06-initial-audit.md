# Repository audit — 2026-11-06

- Audited commit: `a4cf086` on `feat/initial-project-structure` (Codex scaffold)
- Scope: all branches, documentation, backend, firmware, tests, CI, deployment template
- Constraint: audit only. No feature code was changed. Line references are valid for `a4cf086`.

Severity scale: **Critical** means an alarm could be wrongly silent or suppressed if the code is
used as-is. **High** is a structural flaw to fix before the next layer is built on it.
**Medium** is a reliability, process or test gap. **Low** is minor correctness or style.

## Summary

The scaffold is a sound foundation. The alarm policy is a pure function of injected inputs, UTC
reading time and monotonic timeout time are separated, startup is treated as a fault, the last
glucose alarm is retained while data is stale, acknowledgement expires, there are no runtime
dependencies, and the documentation does not overclaim. The alarm engine nevertheless has two
critical gaps: hyperglycemia and implausible values evaluate as normal, and the acknowledgement
window has no upper bound. Its state model must be revised before a runtime is built on it.

## 1. Implementation inventory

All work is on `feat/initial-project-structure` (1 commit). `main` contains only `.gitignore`.
No pull request exists, `main` has no branch protection, the repository is public, and there is
no license.

| Area | Status |
|---|---|
| [alarms.py](../../backend/src/glucose_guardian/alarms.py) | Pure policy engine: glucose `NORMAL`/`LOW`/`CRITICAL`, one `Fault` field, acknowledgement bounded by monotonic time |
| [providers.py](../../backend/src/glucose_guardian/providers.py) | `Reading` dataclass, provider protocol, simulator cycling fixed values stamped with the caller's clock |
| [display.py](../../backend/src/glucose_guardian/display.py), [transport.py](../../backend/src/glucose_guardian/transport.py) | Protocol definitions only |
| [\_\_main\_\_.py](../../backend/src/glucose_guardian/__main__.py) | Prints four simulated readings and exits |
| [main.cpp](../../firmware/arduino-uno/src/main.cpp) | `H`/`N`/`G`/`F` one-character frames, 5 s heartbeat timeout; no pins, outputs, button or watchdog |
| [example.toml](../../config/example.toml) | Not loaded by any code |
| [glucose-guardian.service](../../deployment/systemd/glucose-guardian.service) | Template; `ExecStart` references a module that does not exist |
| Docs | README, AGENTS.md, `architecture.md`, `safety.md`, `hardware.md`, PROJECT_STATUS, DEVELOPMENT_LOG; no `CLAUDE.md` |

## 2. Architecture assessment

The separation of policy from I/O is correct and should be kept. Three structural problems must be
resolved before the runtime milestone:

1. **Communication health is coupled to glucose acquisition.** Heartbeat age is evaluated only
   inside `AlarmMachine.update()` ([alarms.py:85](../../backend/src/glucose_guardian/alarms.py#L85)),
   which takes a glucose reading. If the runtime calls it only when a reading arrives, or a Dexcom
   request blocks, communication-fault detection stalls with acquisition.
2. **The heartbeat proves too little.** A bare `H` frame shows that a serial writer is alive, not
   that the policy loop is running. The heartbeat should be the full state frame, emitted from the
   supervisory loop each cycle, so a hung policy loop stops the heartbeat.
3. **The state model cannot express what the system needs.** Unknown glucose is encoded as
   `NORMAL`, only one fault can be represented at a time, and acknowledgement is a single flag
   covering every alarm channel.

## 3. Test and CI status

| Check | Result |
|---|---|
| `cd backend && python -m pytest`, Python 3.14.4, pytest 9.0.2 | 20 passed |
| Same suite, Python 3.11.18 (CI interpreter) | 20 passed |
| `pio run`, PlatformIO 6.1.18, atmelavr 5.1.0 | Success; RAM 191 B (9.3 %), flash 1644 B (5.1 %) |
| `mypy --strict src` | 1 error: missing `-> None` on `Policy.__post_init__` ([alarms.py:32](../../backend/src/glucose_guardian/alarms.py#L32)) |
| `ruff check` | 5 style findings (import order, `datetime.UTC` alias); no correctness findings |
| GitHub Actions run `42221247588` | Still queued at the time of writing; no remote run has completed |
| Firmware tests | None. Parser and timeout logic live inside `loop()` and cannot be tested on a host |

Findings marked *verified* were reproduced with throwaway scripts against `a4cf086`: Python
probes of `AlarmMachine`, and `main.cpp` compiled on the host against a stub `Arduino.h` with
32-bit `millis()`. See the appendix.

## 4. Security and reliability findings

### Critical

| ID | Finding | Evidence |
|---|---|---|
| C1 | **Hyperglycemia and implausible values evaluate as normal.** There is no high classification and no upper plausibility bound, so any large value yields `NORMAL` with no fault and no alarm. Hyperglycemia detection is a stated requirement. | Verified: 5000 mg/dL gives `NORMAL, NONE`. [alarms.py:81-83](../../backend/src/glucose_guardian/alarms.py#L81-L83) |
| C2 | **The acknowledgement window has no upper bound.** `Policy` accepts any finite positive `acknowledgement_s`. A unit error (60000 intended as ms means 16.7 h) silences an unchanged critical alarm, which violates "critical alarms cannot be permanently suppressed". | Verified: `acknowledgement_s=1e12` accepted. [alarms.py:32-38](../../backend/src/glucose_guardian/alarms.py#L32-L38) |

### High

| ID | Finding | Evidence |
|---|---|---|
| H1 | Unknown glucose is represented as `NORMAL`: at startup, and as the retained value during arbitrarily long stale periods. A display rendering `glucose` would show "normal" when the true state is unknown. | Verified: initial state `NORMAL/ACQUISITION`; after 2 h stale, `NORMAL/STALE`. [alarms.py:56](../../backend/src/glucose_guardian/alarms.py#L56) |
| H2 | A single acknowledgement silences a critical glucose alarm and a concurrent communication fault together. Acknowledgement ownership between the Uno button and the Pi is undefined. | Verified: `CRITICAL` + `COMMUNICATION` acknowledged gives `audible == False`. [alarms.py:47-50](../../backend/src/glucose_guardian/alarms.py#L47-L50) |
| H3 | Communication-fault detection is coupled to glucose updates, and the heartbeat does not prove policy-loop liveness (section 2). | [alarms.py:64-91](../../backend/src/glucose_guardian/alarms.py#L64-L91) |
| H4 | The wall clock is trusted without validation. The Pi 3B has no battery-backed RTC and restores an approximate time until NTP synchronises. Future tolerance is zero and there is no clock-unsynchronised fault. This fails safe, but produces nuisance faults. | Verified: a timestamp 1 ms in the future gives `INVALID`. [alarms.py:76-77](../../backend/src/glucose_guardian/alarms.py#L76-L77) |
| H5 | The Uno is the only audible annunciator. If it is powered solely from the Pi's USB port, a Pi power loss silences both controllers. If the Uno or USB link fails, the Pi has no audible fallback. | Power topology undecided ([HARDWARE.md](../HARDWARE.md)) |
| H6 | The serial protocol is edge-triggered latches, not state. `G` and `F` are set-only and `N` clears both, so one cannot be cleared alone. There is no sequence number, checksum or version, and no Pi-side check of Uno liveness. A NUL-prefixed frame is accepted. Opening the port resets the Uno (DTR), and the firmware treats startup as a communication fault, so every Pi service restart will pulse a fault alarm once outputs exist. | Verified on host harness. [main.cpp:17-43](../../firmware/arduino-uno/src/main.cpp#L17-L43) |
| H7 | Only one fault can be represented, and `COMMUNICATION` masks stale or invalid data. There is no representation or latching for hardware faults other than heartbeat loss (actuator, button, Uno-reported faults). | Verified: stale + heartbeat loss reports only `COMMUNICATION`. [alarms.py:85-87](../../backend/src/glucose_guardian/alarms.py#L85-L87) |

### Medium

| ID | Finding |
|---|---|
| M1 | No firmware tests. Parser and timeout logic should move into a module testable with `pio test -e native`. |
| M2 | CI tests Python 3.11 only, while Debian 13 ships Python 3.13. CI has no lint or type-check step, and no remote run has completed. |
| M3 | `update(..., acquisition_ok: bool = True)` defaults to "healthy" in a safety path ([alarms.py:66](../../backend/src/glucose_guardian/alarms.py#L66)). `reading is None` and `acquisition_ok=False` are two encodings of the same condition. The provider error model is undefined. |
| M4 | The simulator always stamps readings with the current time, so stale, delayed, missing, outage and error scenarios cannot be simulated end to end. |
| M5 | There is no hysteresis and no in-band escalation policy (verified). Values alternating 70/71 re-alarm on every reading. A fall from 50 to 41 while acknowledged stays silent. De-escalation from `CRITICAL` to `LOW` clears the acknowledgement and re-sounds. These require a policy decision by the owner. |
| M6 | The systemd template uses `Restart=on-failure`, so a clean exit leaves the service dead. It has no `WatchdogSec=` / `sd_notify`, so a hang is undetected. It lacks a `dialout` supplementary group for serial access and a `StateDirectory=`. |
| M7 | The Uno does not enable the AVR hardware watchdog, so a firmware hang is undetected. |
| M8 | The repository is public with no LICENSE (not legally open source, and no warranty disclaimer), `main` is unprotected, and there is no secret-scanning guard. |
| M9 | Pi platform risks are unplanned: display stack (1 GB RAM rules out a browser kiosk), screen blanking, night brightness, SD-card wear, under-voltage. |
| M10 | `docs/architecture.md` and `docs/safety.md` are lowercase while the required names are `ARCHITECTURE.md` and `SAFETY.md`. They must be renamed with `git mv`, not duplicated. `CLAUDE.md` is missing. |

### Low

| ID | Finding |
|---|---|
| L1 | One mypy error and five ruff findings (section 3). `class X(str, Enum)` can become `StrEnum` on Python 3.11+. |
| L2 | A naive `now` (a caller bug) is reported as an invalid reading instead of raising. `True` is accepted as 1 mg/dL and evaluates `CRITICAL`. A non-numeric value raises `TypeError` (all verified). |
| L3 | After about 49.7 days with no heartbeat, the 32-bit `millis()` subtraction wraps and the communication fault clears for about 5 s per wrap (verified). The timeout should latch once detected. |
| L4 | Firmware silently rejects CRLF-terminated frames and gives no negative reply to unknown commands. |
| L5 | CI triggers on both `push` and `pull_request`, so PR branches run twice. Actions are pinned by tag rather than commit SHA. No dependency caching. |
| L6 | Threshold comparisons are inclusive (`<=`, so 70 is `LOW`) and this is undocumented. The 5 s heartbeat timeout is defined independently in config and firmware. |

## 5. Missing functionality

- High-glucose classification, plausibility bounds, and an explicit unknown glucose state
- Supervisory runtime loop, configuration loader, Pi-side serial transport
- Dexcom Share adapter with timeouts, backoff, and credentials stored outside the repository
- Versioned state-frame protocol, Uno liveness on the Pi, Uno hardware watchdog, button input,
  gated outputs, distinct system-fault pattern
- GUI, logging that excludes patient data, installable systemd unit with watchdog, clock-sync check
- Hardware-in-loop test plan and a failure-mode table in the safety documentation

## 6. Proposed roadmap

| Milestone | Scope |
|---|---|
| M0 Housekeeping | `CLAUDE.md` referencing AGENTS.md, rename the two docs with `git mv`, LICENSE (owner's choice), PR for the scaffold branch, protect `main`, CI on Python 3.11 and 3.13 with ruff and mypy |
| M1 Alarm policy v2 | Resolve C1, C2, H1, H2, H4, H7, M3 and L2 in the pure engine. Table-driven transition tests plus stale-data, failure and recovery tests. Failure-mode table in the safety doc. |
| M2 Simulated end-to-end runtime | Fixed-rate supervisory loop, acquisition worker with hard timeouts, scenario simulator, stdlib `tomllib` config loader, display and transport test doubles, crash and restart integration tests |
| M3 Serial protocol v1 and firmware refactor | Versioned state frames with sequence number and checksum, Pi-side Uno liveness, host-testable firmware parser, AVR watchdog, startup grace period. Outputs remain disabled. |
| M4 Dexcom Share adapter | Behind a configuration flag; tests use synthetic recorded responses; credentials through systemd credentials outside the repository |
| M5 Display | Lightweight GUI showing data age and fault state, with a night mode |
| M6 Deployment hardening | Production unit and Pi setup documentation. Deployment only with explicit approval. |
| M7 Hardware integration | Only after components are purchased and wiring is reviewed: pin map, drivers, button, hardware-in-loop tests, backup power |

## 7. Recommended next milestone

M0, then M1. The scaffold's own status document proposed the simulated runtime next. The runtime,
protocol and display would all consume the current `State`, which carries both critical findings
and cannot represent unknown glucose or concurrent faults. Changing it later would ripple through
every layer. M1 is pure logic, fully testable with simulated data, and touches no hardware.

Owner decisions required before M1 (the code will not embed clinical values):

1. Hyperglycemia threshold or thresholds.
2. Plausibility bounds. These are a device fact rather than a clinical choice: the G7 displays
   40–400 mg/dL. How Dexcom Share encodes out-of-range values must be confirmed.
3. Maximum acknowledgement window, and whether critical alarms use a shorter one.
4. Acknowledgement semantics: per channel (glucose vs. system fault) or global; whether
   de-escalation re-sounds; whether in-band worsening re-alarms.
5. License choice and approval of the documentation rename.

The Uno power topology (H5) is needed by M3/M7 and belongs in the hardware review.

## Appendix: reproduction

Python findings, run from `backend/` with `PYTHONPATH=src`:

```python
from datetime import datetime, timedelta, timezone
from glucose_guardian.alarms import AlarmMachine, Policy
from glucose_guardian.providers import Reading

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
policy = Policy(low=70, critical=55, max_age_s=600, heartbeat_timeout_s=5, acknowledgement_s=60)

def update(m, value, tick=0, age=0.0, heartbeat=0):
    reading = Reading(value, NOW - timedelta(seconds=age))
    return m.update(reading, now=NOW, monotonic_s=tick, heartbeat_age_s=heartbeat)

print(update(AlarmMachine(policy), 5000))
print(Policy(70, 55, 600, 5, 1e12).acknowledgement_s)
print(AlarmMachine(policy).state)
m = AlarmMachine(policy); update(m, 50, heartbeat=6); m.acknowledge(0)
print(update(m, 50, tick=1, heartbeat=6).audible)
print(update(AlarmMachine(policy), 110, age=-0.001))
print(update(AlarmMachine(policy), 110, age=700, heartbeat=6).fault)
```

| Output line | Finding | Observed |
|---|---|---|
| 1 | C1 | `glucose=NORMAL, fault=NONE` |
| 2 | C2 | `1000000000000.0` (accepted, no error) |
| 3 | H1 | `glucose=NORMAL, fault=ACQUISITION` |
| 4 | H2 | `False` |
| 5 | H4 | `fault=INVALID` |
| 6 | H7 | `Fault.COMMUNICATION` |

Firmware findings (H6, L3, L4) were reproduced by compiling `main.cpp` on the host with a stub
`Arduino.h` providing a fake `Serial` and a `uint32_t millis()`. The checks covered startup
fault, heartbeat reply, NUL-prefixed `H` accepted, CRLF rejected, `N` clearing both latches,
timeout at 5001 ms, and the fault clearing after the 32-bit wrap.
