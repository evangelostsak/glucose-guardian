# Safety and limitations

Glucose Guardian is an experimental supplementary display and alarm system, not a
certified medical device or a basis for treatment decisions. It cannot guarantee
waking a user or delivering an alarm. Keep official Dexcom and Omnipod alerts
independent and unchanged. This prototype is not ready for unattended use.

Dexcom Share depends on network access and remote services. A plausible glucose
number may be stale. Validate UTC timestamps before classifying glucose and show
data age and explicit fault status in the future GUI. A network or hardware failure
must have a distinct indication rather than presenting stale data as normal.

Acknowledgement is temporary and never permanently disables an alert. A short press
silences only the glucose channel and a 2-second hold silences only system faults,
each for its own configured duration capped at 900 s. Repeated presses never extend a
silence, and any escalation, further `rearm_mg_dl` worsening, or new or returning fault
sounds immediately (see [ARCHITECTURE.md](ARCHITECTURE.md)). When the Pi is
unavailable, the Uno demands its own link-lost alarm; a hold silences it for at most
60 s, presses during that window do not extend it, and it re-arms automatically while
the link stays lost. The firmware has no button pin and drives no output yet.

## Failure modes

The runtime connects the alarm engine to bounded acquisition, a clock check, a
display frame and either a transport double or the serial transport (M3). There is
still no Dexcom adapter, GUI or physical output, and no hardware has been flashed.
"Simulated" below means verified with synthetic scenarios; "PC-tested" means verified
with pseudo-terminals, a Python Uno emulator, and native firmware unit tests.

| Condition | Detected by | Indication | Status |
|---|---|---|---|
| Urgent low, low or high glucose | Fresh valid reading crossing a threshold | Glucose alarm | Simulated |
| Reading below 40 or above 400 mg/dL | Normal threshold classification | Urgent low or high glucose alarm (with the configured thresholds) | Engine implemented |
| No reading, or reading older than `max_age_s` | Validation | `missing_data` fault; unresolved glucose alarm kept and shown as last known with its age; normal becomes unknown | Simulated |
| Non-numeric, non-finite or non-positive value; naive timestamp; timestamp more than 60 s in the future | Validation | `invalid_reading` fault | Simulated |
| Fetch error or exception | Acquisition worker | `acquisition` fault; latest reading used until stale | Simulated; Dexcom adapter M4 |
| Fetch blocked or slower than `fetch_timeout_s` | Acquisition worker hard timeout | `acquisition` fault; supervisor keeps ticking; one thread at most | Simulated |
| Pi clock not synchronized (no RTC on the Pi 3B), or the check fails or times out | Clock-check worker | `clock_unsynchronized` fault; readings not evaluated | Simulated; systemd check unit-tested, not yet run on the Pi |
| Uno silent, unplugged, or answering with corrupted, outdated, duplicate or wrong-version frames | Round-trip link age above `heartbeat_timeout_s` | `communication` fault; port reopened with backoff | PC-tested; on the USB bench (owner-reported 2026-11-12): Uno rejection of invalid frames, recovery after a heartbeat pause, unplug detection, reconnect and backoff passed |
| Pi serial write zero-length, partial or failing (output buffer full, device stalled) | Byte count checked on every write; writability checked before writing | Frame not counted as sent; output discarded; port reopened with backoff; `communication` fault if the link stays down | PC-tested |
| Uno resets while connected (watchdog, brown-out, power glitch, reset button) | New boot counter, or a watchdog or brown-out cause at connection | `controller_reset` fault latched until a long press | DTR and watchdog resets detected on the USB bench (owner-reported 2026-11-12: 5 of 5 repeated resets, both watchdog hang modes). With Optiboot 4.4 only `W` (watchdog-interrupt marker) or `U` is reported; brown-out, power glitch and reset button not exercised |
| Button held 30 s or more | Uno stuck detection | `button_stuck` fault; the stuck press acknowledges nothing | PC-tested; no button pin yet |
| Display or transport send failure | Runtime adapter guard | Counted and logged; alarm evaluation continues | Simulated |
| Alarm-engine defect | Exception out of the loop | Service exits with status 1; heartbeats stop | Simulated; systemd restart M6 |
| Pi process crash, hang or planned stop; alarm loop stalled | No new state frame on the Uno for more than 5 s (10 s grace after boot) | Uno link-lost demand, independent of the Pi | PC-tested; not observable on the bench (no output until M7) |
| Uno firmware hang | Two-stage hardware watchdog (1 s interrupt, then 1 s reset) resets the Uno after about 2 s | `controller_reset` on the Pi (new boot counter) | Passed on the USB bench (owner-reported 2026-11-12): hangs with interrupts enabled and disabled both reset, 2.243 s between replies |
| Total power loss | Not possible | None | Requires backup power (hardware review) |

Acknowledging a stale glucose alarm with a short press silences only the glucose
channel; the missing-data fault keeps sounding and the display prompts "Hold 2 s to
silence". A hold silences the faults for `fault_acknowledgement_s` and clears neither
the fault nor the retained glucose level.

A restart is fail-safe but lossy: acknowledgements are forgotten and the system
re-alarms, but a retained urgent low during stale data becomes unknown glucose with a
missing-data fault, and the display shows no value until fresh data arrives.

Unverified risks include driver failure, USB reset, power loss, loud sound,
heating, short circuits, enclosure and cable hazards, and backup battery failure.
Electrical design, user-selected alarm policy, acoustic patterns and backup power
need review and bench validation before operation. Software tests do not establish
medical suitability or electrical safety. Never commit credentials, tokens, logs
with patient readings, or real patient glucose data.
