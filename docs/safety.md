# Safety and limitations

Glucose Guardian is an experimental supplementary display and alarm system, not a
certified medical device or a basis for treatment decisions. It cannot guarantee
waking a user or delivering an alarm. Keep official Dexcom and Omnipod alerts
independent and unchanged. This prototype is not ready for unattended use.

Dexcom Share depends on network access and remote services. A plausible glucose
number may be stale. Validate UTC timestamps before classifying glucose and show
data age and explicit fault status in the future GUI. A network or hardware failure
must have a distinct indication rather than presenting stale data as normal.

Acknowledgement must be temporary, clear on new/escalating events, and never
permanently disable critical alerts. Future firmware must implement equivalent
bounds for the physical button and keep communication-fault handling independent.
The current firmware has no button input or physical alarm output.

Unverified risks include driver failure, USB reset, power loss, loud sound,
heating, short circuits, enclosure and cable hazards, and backup battery failure.
Electrical design, user-selected alarm policy, acoustic patterns and backup power
need review and bench validation before operation. Software tests do not establish
medical suitability or electrical safety. Never commit credentials, tokens, logs
with patient readings, or real patient glucose data.
