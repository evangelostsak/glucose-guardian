# Architecture

The Pi owns acquisition, timestamp validation, deterministic glucose policy, HDMI
display, and USB transport. The Uno independently watches Pi heartbeats and will
request a distinct system fault on startup or heartbeat loss. A future systemd
runtime must supervise the Pi process. The supplied unit is only a template and
references a service module that is deliberately not implemented yet.

`providers.py` defines immutable readings and a provider boundary. The simulator
cycles synthetic values using an injected UTC clock. `validation.py` checks a
reading's value and timestamp. `alarms.py` holds the alarm policy. It takes the
latest reading, UTC time, monotonic time, acquisition status, clock synchronization
status and heartbeat age, all as required arguments, and performs no I/O. Every call
re-evaluates data age and heartbeat age, so a fixed-rate runtime loop (M2) detects
faults independently of when readings arrive. `display.py` and `transport.py` define
future adapters. There is no Dexcom adapter.

## Reading validation

A reading is invalid if its value is not a finite positive number (booleans and
strings are rejected), its timestamp is naive or missing, or it lies more than
`max_future_skew_s` in the future. It is stale when older than `max_age_s`. Both
bounds are inclusive. Values outside the G7 display range (below 40 or above
400 mg/dL) are not special-cased; they classify through the normal thresholds. A
naive `now` is a programming error and raises `ValueError`.

## Glucose levels

Glucose has five levels: urgent low, low, normal, high and unknown. A fresh valid
reading enters urgent low at or below `urgent_low_mg_dl`, low at or below
`low_mg_dl`, and high at or above `high_mg_dl`. Leaving a level requires clearing
its threshold by `hysteresis_mg_dl`: urgent low becomes low only above
`urgent_low_mg_dl + hysteresis_mg_dl`, low becomes normal only above
`low_mg_dl + hysteresis_mg_dl`, and high becomes normal only below
`high_mg_dl - hysteresis_mg_dl`.

Without a fresh valid reading (none, stale, invalid, or clock unsynchronized), an
active urgent low, low or high level is retained as unresolved, and normal becomes
unknown. `State.glucose_current` tells the display whether the level comes from a
fresh reading. Startup is unknown glucose with a missing-data fault.

## System faults

Faults are a set, so concurrent faults are all reported:

| Fault | Raised when |
|---|---|
| `acquisition` | the runtime reports that the last fetch failed |
| `missing_data` | there is no reading, or it is older than `max_age_s` |
| `invalid_reading` | the latest reading fails validation |
| `clock_unsynchronized` | the wall clock is not trusted; readings are then not evaluated |
| `communication` | the Uno heartbeat age exceeds `heartbeat_timeout_s` or is not a finite non-negative number |

Faults are recomputed from the inputs on every update, so a normal glucose reading
cannot clear one. Hardware faults reported by the Uno will join this set in M3.

## Acknowledgement

Glucose alarms and system faults are acknowledged separately with
`acknowledge_glucose` and `acknowledge_faults`; neither silences the other.
Acknowledging a quiet channel does nothing. Each acknowledgement lasts
`acknowledgement_s` of monotonic time, which the policy caps at 15 minutes
(`MAX_ACKNOWLEDGEMENT_S`). After expiry an unresolved condition sounds again.

A glucose acknowledgement ends early when the level becomes more severe or changes
direction, or when the reading moves `rearm_mg_dl` beyond the value at
acknowledgement (lower for urgent low and low, higher for high). Improving from
urgent low to low stays silent. If the acknowledgement was given while data was not
fresh, the first fresh reading becomes the baseline. Returning to normal ends the
event, so the next one alarms.

A fault acknowledgement covers only the faults active when it was given and shrinks
as they clear. Any fault outside that set, including one that cleared and returned,
sounds immediately; an empty fault set ends the event. How the single physical
button maps to the two channels is decided with the serial protocol (M3).

## Policy values

All policy values are required and validated when `Policy` is constructed: finite and
positive; `urgent_low < low < high`; `urgent_low + hysteresis <= low`;
`low + hysteresis < high - hysteresis`; `acknowledgement_s <= 900`. No clinical
values are embedded in code. `config/example.toml` and the tests use synthetic
values. Personal values belong in the git-ignored `config/local.toml` once
configuration loading exists (M2).

## Serial protocol

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
