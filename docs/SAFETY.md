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

The alarm engine is not yet connected to a runtime, display or output; the runtime
that supplies acquisition, clock and heartbeat status arrives in M2 and M3.

| Condition | Detected by | Indication | Status |
|---|---|---|---|
| Urgent low, low or high glucose | Fresh valid reading crossing a threshold | Glucose alarm | Engine implemented |
| Reading below 40 or above 400 mg/dL | Normal threshold classification | Urgent low or high glucose alarm (with the configured thresholds) | Engine implemented |
| No reading, or reading older than `max_age_s` | Validation | `missing_data` fault; unresolved glucose alarm kept, normal becomes unknown | Engine implemented |
| Non-numeric, non-finite or non-positive value; naive timestamp; timestamp more than 60 s in the future | Validation | `invalid_reading` fault | Engine implemented |
| Dexcom or network fetch failure | Runtime reports `acquisition_ok = false` | `acquisition` fault; latest reading used until stale | Engine implemented; runtime M2, adapter M4 |
| Pi clock not synchronized (no RTC on the Pi 3B) | Runtime reports `clock_synchronized = false` | `clock_unsynchronized` fault; readings not evaluated | Engine implemented; runtime M2 |
| Uno heartbeat lost, seen from the Pi | Heartbeat age above `heartbeat_timeout_s` | `communication` fault | Engine implemented; transport M3 |
| Pi process crash or hang | Uno heartbeat timeout | Uno system fault | Firmware detects it internally; no output until M7 |
| Uno failure or USB loss | Not yet | None | Pi-side Uno liveness in M3 |
| Total power loss | Not possible | None | Requires backup power (hardware review) |

Unverified risks include driver failure, USB reset, power loss, loud sound,
heating, short circuits, enclosure and cable hazards, and backup battery failure.
Electrical design, user-selected alarm policy, acoustic patterns and backup power
need review and bench validation before operation. Software tests do not establish
medical suitability or electrical safety. Never commit credentials, tokens, logs
with patient readings, or real patient glucose data.
