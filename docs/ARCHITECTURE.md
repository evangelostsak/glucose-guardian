# Architecture

The Pi owns acquisition, timestamp validation, deterministic glucose policy, HDMI
display, and USB transport. The Uno independently supervises the Pi's state frames
and demands its own link-lost alarm when they stop. systemd will supervise the Pi
process (M6); the supplied unit is an uninstalled template.

| Module | Responsibility |
|---|---|
| `providers.py` | Immutable `Reading` and the `GlucoseProvider` boundary (`read(now) -> Reading \| None`) |
| `validation.py` | Reading value and timestamp checks |
| `alarms.py` | Pure alarm policy; no I/O |
| `worker.py` | Bounded background jobs with hard timeouts |
| `clock.py` | Wall-clock synchronization check |
| `config.py` | Strict TOML configuration loading |
| `simulation.py` | Deterministic synthetic scenarios and their provider |
| `display.py` | Display frame model, log display, in-memory display double |
| `transport.py` | Transport boundary (`receive`, `send`) and simulated transport double |
| `protocol.py` | Serial protocol v1 framing, checksums and validation; no I/O |
| `link.py` | Pi-side link state: liveness, replay rejection, reset detection, button counters |
| `serial_transport.py` | pyserial transport with bounded reads and reconnect backoff |
| `runtime.py` | Fixed-rate supervisory loop wiring the above |
| `service.py` | Simulated runtime entry point |

`alarms.py` takes the latest reading, UTC time, monotonic time, acquisition status,
clock synchronization status and heartbeat age, all as required arguments. Every call
re-evaluates data age and heartbeat age, so the fixed-rate runtime detects faults
independently of when readings arrive. There is no Dexcom adapter or GUI yet. The Uno
firmware lives in `firmware/arduino-uno/lib/guardian/` (testable C++) with Arduino
glue in `src/main.cpp`; the protocol is specified in [PROTOCOL.md](PROTOCOL.md).

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
| `communication` | the round-trip link age exceeds `heartbeat_timeout_s` or is not a finite non-negative number |
| `controller_reset` | the Uno reset unexpectedly (new boot while connected, or a watchdog or brown-out cause at connection); latched until a long press |
| `button_stuck` | the Uno reports the button held for 30 s or more |

Faults are recomputed from the inputs on every update, so a normal glucose reading
cannot clear one. `controller_reset` records an event rather than a condition, so it
is the one fault a long press clears: the press both silences the current faults and
acknowledges the reset. `update()` takes Uno-reported faults as `controller_faults`
and rejects any other fault in that argument.

## Acknowledgement

Glucose alarms and system faults are acknowledged separately with
`acknowledge_glucose` and `acknowledge_faults`; neither silences the other.
Acknowledging a quiet channel, or one already silenced, does nothing, so repeated
presses never extend a silence. A glucose acknowledgement lasts
`glucose_acknowledgement_s` and a fault acknowledgement `fault_acknowledgement_s` of
monotonic time; the policy caps both at 900 s (`MAX_ACKNOWLEDGEMENT_S`). After expiry
an unresolved condition sounds again.

A glucose acknowledgement ends early when the level becomes more severe or changes
direction, or when the reading moves `rearm_mg_dl` beyond the value at
acknowledgement (lower for urgent low and low, higher for high). Improving from
urgent low to low stays silent. If the acknowledgement was given while data was not
fresh, the first fresh reading becomes the baseline. Returning to normal ends the
event, so the next one alarms.

A fault acknowledgement covers only the faults active when it was given and shrinks
as they clear. Any fault outside that set, including one that cleared and returned,
sounds immediately; an empty fault set ends the event.

The single physical button has two gestures (owner decision for M3, superseding the
M2 "one press silences both" rule):

| Gesture | Pi action |
|---|---|
| Short press (< 2 s) | `acknowledge_glucose`; system faults stay audible |
| Long press (2 s to < 30 s) | `acknowledge_faults`; also clears a latched `controller_reset` |

The display keeps every active warning, silenced or not, and shows "Hold 2 s to
silence" (`Frame.prompt`) when faults are sounding and the glucose channel is not. No
press clears a condition fault, extends a silence, or hides a later new or escalating
event. The Uno classifies presses and reports cumulative counters; see
[PROTOCOL.md](PROTOCOL.md).

## Policy values

All policy values are required and validated when `Policy` is constructed: finite and
positive; `urgent_low < low < high`; `urgent_low + hysteresis <= low`;
`low + hysteresis < high - hysteresis`; both acknowledgement durations `<= 900`. No clinical
values are embedded in code. `config/example.toml` and the tests use synthetic
values; personal values belong in the git-ignored `config/local.toml`.

## Runtime

`Runtime.tick()` runs once per `tick_s` and never blocks on I/O:

1. `transport.receive()`: link age, short and long presses, unexpected reset, stuck
   flag. Latch `controller_reset`; short presses acknowledge glucose, long presses
   acknowledge faults and clear the reset latch.
2. Poll the acquisition worker and the clock-check worker.
3. Evaluate the alarm engine with the latest reading, acquisition status, clock
   status, link age and Uno-reported faults.
4. `transport.send(state)`: the state frame is the heartbeat. Render a display frame.

`Runtime.run(wait)` schedules ticks on monotonic deadlines (`deadline += tick_s`), so
tick duration does not accumulate drift. An overrunning tick skips the missed slots,
counts them in `counts["overruns"]`, and the next tick starts on the schedule grid.
`wait` is `threading.Event.wait` in the service, so a stop request interrupts the
sleep.

### Bounded workers

Acquisition and the clock check each run in a `BoundedWorker`. A job starts every
interval with at most one job in flight. A job still running after its hard timeout
is reported as `TimedOut` once and keeps its slot until it returns; its late result
is discarded and no further job starts meanwhile. A permanently hung fetch therefore
costs exactly one daemon thread and never blocks the supervisor or process exit.
Exceptions inside a job, and a failure to start a thread, become `Failed`.

| Worker result | Acquisition | Clock |
|---|---|---|
| `Done(reading)` | latest reading replaced; `acquisition_ok` true | `Done(True)`: synchronized |
| `Done(None)` | no new reading; latest kept and ages towards stale | `Done(False)`: unsynchronized |
| `Failed`, `TimedOut` | `acquisition_ok` false | unsynchronized |

At startup `acquisition_ok` is true (nothing has failed yet; missing data is already a
fault) and the clock is untrusted until its first check completes.

### Clock check

`systemd_clock_synchronized` runs `timedatectl show --property=NTPSynchronized
--value` with a timeout. Only exit status 0 with output `yes` counts as synchronized;
any error, timeout or other output means untrusted. The simulated service takes clock
status from the scenario instead; the systemd check is wired to the real runtime in
M4/M6 and must be validated on Debian 13 on the Pi.

### Adapter failures and crashes

Display and transport exceptions are caught, counted (`display_errors`,
`transport_errors`) and logged by exception type only; alarm evaluation continues. A
failing `receive()` counts as an infinite link age, raising a communication
fault (`LINK_DOWN`). Provider and clock-check exceptions become worker failures. Any other exception,
such as an alarm-engine defect, propagates out of `run()`: the service logs it and
exits with status 1 for systemd to restart (M6). The Uno heartbeat watchdog covers
the gap.

### Restart

State is not persisted. A restarted runtime begins with unknown glucose and a
missing-data fault, forgets any acknowledgement, and re-alarms. It also forgets a
retained alarm level: after a restart during stale data, glucose is unknown (with the
missing-data fault sounding) rather than the last urgent low. Persisting the retained
level is an open M6 item.

## Configuration

`load_config(path)` reads TOML strictly: unknown keys and missing keys are errors that
name the file and key, numbers must be finite and positive (booleans and strings are
rejected), and `[alarm]` builds a validated `Policy`.

| Table | Keys |
|---|---|
| top level | `provider` (only `"simulated"` until M4) |
| `[alarm]` | the ten `Policy` fields |
| `[runtime]` | `tick_s`, `fetch_interval_s`, `fetch_timeout_s`, `clock_check_interval_s`, `clock_check_timeout_s` |
| `[simulation]` | `scenario` (a name from `SCENARIOS`) |
| `[transport]` | `kind = "simulated"` alone, or `kind = "serial"` with `device`, `reconnect_initial_s`, `reconnect_max_s` |
| `[hardware]` | optional; `outputs_enabled` may only be `false` |

The only default is the fail-safe one: a missing `[hardware]` table means outputs
disabled. Cross-checks: `tick_s < heartbeat_timeout_s`, `fetch_timeout_s <=
fetch_interval_s`, `fetch_interval_s < max_age_s`, `clock_check_timeout_s <=
clock_check_interval_s`, `reconnect_initial_s <= reconnect_max_s`. The file holds no credentials; unknown keys such as
`password` are rejected, and Dexcom credentials will use systemd credentials (M4).

## Simulation

`SCENARIOS` holds deterministic, synthetic scenarios: `normal`, `hypoglycemia`,
`hyperglycemia`, `stale_data`, `missing_data`, `invalid_readings`,
`acquisition_timeout`, `acquisition_errors`, `clock_failure`, `communication_loss` and
`simultaneous_faults`. Each is a list of segments giving the reading value (or none),
whether reading timestamps are frozen (stale data), the fetch behavior (ok, error,
hang), clock status and heartbeat status. The last segment persists.

`ScenarioProvider` serves readings from the scenario. In the service, a hang segment
blocks the fetch thread until the segment ends. Tests drive the runtime with fake
monotonic and wall clocks and model a hang by holding the fetch job until the hang
segment ends, which keeps every scenario deterministic.

## Service

`python -m glucose_guardian.service --config <file>` runs the configured scenario in
real time with daemon-thread workers, a log display and the configured transport. A
serial transport gets a random 16-bit session per process. It
stops on SIGTERM or SIGINT and restores the previous signal handlers. Exit status: 0
stopped, 1 crashed, 2 configuration error. Logs contain levels and fault names, never
glucose values. Stopping the service stops heartbeats, which the Uno will treat as a
communication fault; a planned stop is not silent.

## Serial transport

`SerialTransport` uses pyserial only to move bytes (non-blocking, exclusive open,
115200 baud); framing, checksums, link state and reconnection are this package's
code. Each `receive()` reads at most 1024 bytes into a 63-byte line buffer. Each
`send()` writes one state frame through `PyserialPort`, which returns 0 instead of
calling pyserial when a zero-timeout `select` reports no room; pyserial would
otherwise spin on a full buffer. A frame's sequence enters the acknowledgement window
only after a complete write. A zero, partial or failed write, an open failure or an
I/O error discards unsent output, closes the port and schedules a retry with
exponential backoff between `reconnect_initial_s` and `reconnect_max_s`; a successful
open resets the backoff and starts a new initial connection. See "Transmit failures"
in [PROTOCOL.md](PROTOCOL.md). Tests exercise the real pyserial
path over pseudo-terminals against a Python Uno emulator.

## Firmware

`lib/guardian/` contains the protocol parser and formatter, `PiLink` (startup grace,
heartbeat timeout, replay rejection, local silence), `Button` (debounce,
short/long classification, stuck detection), reset-cause decoding, and the
`Controller` that combines them into replies and an alarm `Demand`. It is C++11
without Arduino headers or the STL and has native Unity tests (`pio test -e native`).
`src/main.cpp` captures reset flags in `.init3`, enables the 2 s hardware watchdog,
increments the EEPROM boot counter, moves serial bytes, and computes the demand
without driving any pin. The button input is fixed at "released" until a pin is
assigned after wiring review.
