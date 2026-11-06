# Architecture

The Pi owns acquisition, timestamp validation, deterministic glucose policy, HDMI
display, and USB transport. The Uno independently watches Pi heartbeats and will
request a distinct system fault on startup or heartbeat loss. A future systemd
runtime must supervise the Pi process. The supplied unit is only a template and
references a service module that is deliberately not implemented yet.

`providers.py` defines immutable readings and a provider boundary. The simulator
cycles synthetic values using an injected UTC clock. `alarms.py` takes a reading,
UTC time, monotonic time, acquisition status, and heartbeat age. It performs no I/O.
`display.py` and `transport.py` define future adapters. There is no Dexcom adapter.

Readings with naive or future timestamps, nonfinite or nonpositive values are
invalid. Readings older than the configured age are stale. Until valid data
arrives, the last glucose alarm is retained. Acquisition, stale/invalid data, and
communication faults are separate from glucose severity. A single fault field
prioritizes communication failure; multi-fault reporting is future work. Startup begins in acquisition fault until a validated reading arrives.

Acknowledgement is bounded by monotonic time. A changed glucose severity or fault
clears it immediately; an unchanged critical event sounds again at expiry. Recovery
clears the event so a later event can alarm. Policy values must be explicit and
reviewed; no clinical defaults are embedded in the alarm engine. Hysteresis and
repeat-reading policy require further design and validation.

The experimental Uno protocol uses LF-terminated one-character frames at 115200
baud: H heartbeat (reply HEARTBEAT), N clear requested alarms, G request glucose
alarm, F request system fault. Malformed frames do not refresh heartbeat. Startup
and more than five seconds without H request communication fault. This five-second
bench constant is not an approved operating limit. GPIO outputs are absent.

Before end-to-end operation, define versioning, state framing, acknowledgements,
reconnect behavior, Pi-side Arduino liveness detection, corruption handling, button
events and fault patterns. The current protocol is a compilation scaffold, not an
operational safety interface. USB loss, process crash, boot, counter rollover,
partial messages and recovery need hardware-in-loop validation.
