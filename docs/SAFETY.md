# Safety and limitations

Glucose Guardian is an experimental supplementary display and alarm system, not a
certified medical device or a basis for treatment decisions. It cannot guarantee
waking a user or delivering an alarm. Keep official Dexcom and Omnipod alerts
independent and unchanged. This prototype is not ready for unattended use.

Dexcom Share depends on network access and remote services. A plausible glucose
number may be stale. Validate UTC timestamps before classifying glucose and show
data age and explicit fault status in the future GUI. A network or hardware failure
must have a distinct indication rather than presenting stale data as normal.

Acknowledgement is temporary and never permanently disables an alert. The alarm
engine caps every acknowledgement at 15 minutes, acknowledges glucose alarms and
system faults separately, and re-sounds immediately on escalation, on a further
`rearm_mg_dl` worsening, or on any new or returning fault (see
[ARCHITECTURE.md](ARCHITECTURE.md)). Future firmware must implement equivalent
bounds for the physical button and keep communication-fault handling independent.
The current firmware has no button input or physical alarm output.

## Failure modes

The simulated runtime (M2) connects the alarm engine to bounded acquisition, a clock
check, a display frame and a transport double. There is still no Dexcom adapter,
serial transport, GUI or physical output, so "simulated" below means verified with
synthetic scenarios only.

| Condition | Detected by | Indication | Status |
|---|---|---|---|
| Urgent low, low or high glucose | Fresh valid reading crossing a threshold | Glucose alarm | Simulated |
| Reading below 40 or above 400 mg/dL | Normal threshold classification | Urgent low or high glucose alarm (with the configured thresholds) | Engine implemented |
| No reading, or reading older than `max_age_s` | Validation | `missing_data` fault; unresolved glucose alarm kept and shown as last known with its age; normal becomes unknown | Simulated |
| Non-numeric, non-finite or non-positive value; naive timestamp; timestamp more than 60 s in the future | Validation | `invalid_reading` fault | Simulated |
| Fetch error or exception | Acquisition worker | `acquisition` fault; latest reading used until stale | Simulated; Dexcom adapter M4 |
| Fetch blocked or slower than `fetch_timeout_s` | Acquisition worker hard timeout | `acquisition` fault; supervisor keeps ticking; one thread at most | Simulated |
| Pi clock not synchronized (no RTC on the Pi 3B), or the check fails or times out | Clock-check worker | `clock_unsynchronized` fault; readings not evaluated | Simulated; systemd check unit-tested, not yet run on the Pi |
| Uno heartbeat lost, or heartbeat query fails, seen from the Pi | Heartbeat age above `heartbeat_timeout_s` | `communication` fault | Simulated; serial transport M3 |
| Display or transport send failure | Runtime adapter guard | Counted and logged; alarm evaluation continues | Simulated |
| Alarm-engine defect | Exception out of the loop | Service exits with status 1; heartbeats stop | Simulated; systemd restart M6 |
| Pi process crash, hang or planned stop | Uno heartbeat timeout | Uno system fault | Firmware detects it internally; no output until M7 |
| Uno failure or USB loss | Not yet | None | Pi-side Uno liveness in M3 |
| Total power loss | Not possible | None | Requires backup power (hardware review) |

Acknowledging a stale glucose alarm silences only the glucose channel; the
missing-data fault keeps sounding until it is acknowledged separately. A button press
acknowledges both audible channels independently for at most 15 minutes and clears
neither the fault nor the retained glucose level.

A restart is fail-safe but lossy: acknowledgements are forgotten and the system
re-alarms, but a retained urgent low during stale data becomes unknown glucose with a
missing-data fault, and the display shows no value until fresh data arrives.

Unverified risks include driver failure, USB reset, power loss, loud sound,
heating, short circuits, enclosure and cable hazards, and backup battery failure.
Electrical design, user-selected alarm policy, acoustic patterns and backup power
need review and bench validation before operation. Software tests do not establish
medical suitability or electrical safety. Never commit credentials, tokens, logs
with patient readings, or real patient glucose data.
