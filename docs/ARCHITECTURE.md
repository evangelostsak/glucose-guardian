# Architecture

The Pi owns acquisition, timestamp validation, deterministic glucose policy, HDMI
display, and USB transport. The Uno independently watches Pi heartbeats and will
request a distinct system fault on startup or heartbeat loss. systemd will supervise
the Pi process (M6); the supplied unit is an uninstalled template.

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
| `transport.py` | Transport boundary and simulated transport double |
| `runtime.py` | Fixed-rate supervisory loop wiring the above |
| `service.py` | Simulated runtime entry point |

`alarms.py` takes the latest reading, UTC time, monotonic time, acquisition status,
clock synchronization status and heartbeat age, all as required arguments. Every call
re-evaluates data age and heartbeat age, so the fixed-rate runtime detects faults
independently of when readings arrive. There is no Dexcom adapter, serial adapter or
GUI yet.

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
sounds immediately; an empty fault set ends the event.

A short press of the single physical button acknowledges both currently audible
channels independently (owner decision for M3): it calls `acknowledge_glucose` and
`acknowledge_faults`, each of which only acts on an audible channel. A press never
clears an active fault, never extends past the 15-minute cap, and never silences a
later new or escalating event. Debounce and the button event protocol belong to M3.
In M2 presses arrive through `AlarmTransport.take_button_presses()`.

## Policy values

All policy values are required and validated when `Policy` is constructed: finite and
positive; `urgent_low < low < high`; `urgent_low + hysteresis <= low`;
`low + hysteresis < high - hysteresis`; `acknowledgement_s <= 900`. No clinical
values are embedded in code. `config/example.toml` and the tests use synthetic
values; personal values belong in the git-ignored `config/local.toml`.

## Runtime

`Runtime.tick()` runs once per `tick_s` and never blocks on I/O:

1. Take button presses from the transport and acknowledge both audible channels.
2. Poll the acquisition worker and the clock-check worker.
3. Evaluate the alarm engine with the latest reading, acquisition status, clock
   status and the transport's heartbeat age.
4. Send the state to the transport and render a display frame.

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
failing heartbeat query counts as an infinite heartbeat age, raising a communication
fault. Provider and clock-check exceptions become worker failures. Any other exception,
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
| `[alarm]` | the nine `Policy` fields |
| `[runtime]` | `tick_s`, `fetch_interval_s`, `fetch_timeout_s`, `clock_check_interval_s`, `clock_check_timeout_s` |
| `[simulation]` | `scenario` (a name from `SCENARIOS`) |
| `[hardware]` | optional; `outputs_enabled` may only be `false` |

The only default is the fail-safe one: a missing `[hardware]` table means outputs
disabled. Cross-checks: `tick_s < heartbeat_timeout_s`, `fetch_timeout_s <=
fetch_interval_s`, `fetch_interval_s < max_age_s`, `clock_check_timeout_s <=
clock_check_interval_s`. The file holds no credentials; unknown keys such as
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
real time with daemon-thread workers, a log display and the simulated transport. It
stops on SIGTERM or SIGINT and restores the previous signal handlers. Exit status: 0
stopped, 1 crashed, 2 configuration error. Logs contain levels and fault names, never
glucose values. Stopping the service stops heartbeats, which the Uno will treat as a
communication fault; a planned stop is not silent.

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
