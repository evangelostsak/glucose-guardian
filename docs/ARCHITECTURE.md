# Architecture

The Pi owns acquisition, timestamp validation, deterministic glucose policy, HDMI
display, and USB transport. The Uno independently supervises the Pi's state frames
and demands its own link-lost alarm when they stop. systemd supervision of the Pi
process is part of M5 (decision D1, approved, in the
[M5 plan](plans/2026-11-13-m5-hdmi-display.md)); the supplied unit is an uninstalled
template.

| Module | Responsibility |
|---|---|
| `providers.py` | Immutable `Reading` (value, UTC time and the trend sent with it) and the `GlucoseProvider` boundary (`read(now) -> Reading \| None`) |
| `validation.py` | Reading value and timestamp checks |
| `alarms.py` | Pure alarm policy; no I/O |
| `worker.py` | Bounded background jobs with hard timeouts |
| `clock.py` | Wall-clock synchronization check |
| `config.py` | Strict TOML configuration loading |
| `simulation.py` | Deterministic synthetic scenarios and their provider |
| `display.py` | Display frame model and its data source, log display, fan-out display, in-memory display double |
| `snapshot.py` | Display snapshot schema 1: atomic writer (`SnapshotDisplay`) and strict reader |
| `screen_model.py` | Pure classification of a snapshot into what the bedside screen shows |
| `screen_render.py` | Pillow renderer of a screen model into an 800×480 image |
| `screen.py` | Display process: snapshot, screen model, renderer, image output, systemd notifications |
| `notify.py` | systemd notifications over the Unix datagram socket in `$NOTIFY_SOCKET` |
| `preview.py` | Synthetic dashboard previews (`make display-preview`) |
| `transport.py` | Transport boundary (`receive`, `send`) and simulated transport double |
| `protocol.py` | Serial protocol v1 framing, checksums and validation; no I/O |
| `link.py` | Pi-side link state: liveness, replay rejection, reset detection, button counters |
| `serial_transport.py` | pyserial transport with bounded reads and reconnect backoff |
| `runtime.py` | Fixed-rate supervisory loop wiring the above |
| `service.py` | Runtime entry point with simulated or Dexcom Share acquisition |
| `dexcom_share.py` | Dexcom Share protocol: endpoints, request bodies, strict parsing, error classes; no I/O |
| `share_client.py` | `urllib` HTTPS poster, login state machine (`ShareSession`), `DexcomShareProvider` |
| `share_state.py` | Persisted login cooldown, rate limit and block latch; `status`/`unblock`/`path` commands on `--state-file` or `--config`; no secrets |
| `credentials.py` | Publisher credentials from `$CREDENTIALS_DIRECTORY`; `python -m glucose_guardian.credentials` validates them without printing values |
| `share_simulator.py` | Local synthetic Dexcom Share server (tests, soak runs) |
| `share_check.py` | Read-only connectivity check that prints structure, never values |

`alarms.py` takes the latest reading, UTC time, monotonic time, acquisition status,
clock synchronization status and heartbeat age, all as required arguments. Every call
re-evaluates data age and heartbeat age, so the fixed-rate runtime detects faults
independently of when readings arrive. Dexcom Share acquisition is described below
and in [DEXCOM_SHARE.md](DEXCOM_SHARE.md). The display software is described under
*Display* below; it has no physical output yet. The Uno
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
any error, timeout or other output means untrusted. With `provider = "dexcom_share"`
the service uses this check; the simulated provider takes clock status from the
scenario. It has not yet been run on Debian 13 on the Pi.

### Adapter failures and crashes

Display and transport exceptions are caught, counted (`display_errors`,
`transport_errors`) and logged by exception type only; alarm evaluation continues. A
failing `receive()` counts as an infinite link age, raising a communication
fault (`LINK_DOWN`). Provider and clock-check exceptions become worker failures. Any other exception,
such as an alarm-engine defect, propagates out of `run()`: the service logs it and
exits with status 1 for systemd to restart (not installed yet). The Uno heartbeat watchdog covers
the gap.

### Restart

State is not persisted. A restarted runtime begins with unknown glucose and a
missing-data fault, forgets any acknowledgement, and re-alarms. It also forgets a
retained alarm level: after a restart during stale data, glucose is unknown (with the
missing-data fault sounding) rather than the last urgent low. Persisting the retained
level is an open item, scheduled before physical alarm outputs (M6).

## Configuration

`load_config(path)` reads TOML strictly: unknown keys and missing keys are errors that
name the file and key, numbers must be finite and positive (booleans and strings are
rejected), and `[alarm]` builds a validated `Policy`.

| Table | Keys |
|---|---|
| top level | `provider`: `"simulated"` (needs `[simulation]`) or `"dexcom_share"` (needs `[dexcom]`) |
| `[alarm]` | the ten `Policy` fields |
| `[runtime]` | `tick_s`, `fetch_interval_s`, `fetch_timeout_s`, `clock_check_interval_s`, `clock_check_timeout_s` |
| `[simulation]` | `scenario` (a name from `SCENARIOS`) |
| `[dexcom]` | `server` (`us`, `ous`, `jp`, `simulator` with `simulator_port`), `login_min_interval_s` (at least 300), `rate_limit_initial_s`, `rate_limit_max_s`, `request_timeout_s`, `state_file` (absolute) |
| `[transport]` | `kind = "simulated"` alone, or `kind = "serial"` with `device`, `reconnect_initial_s`, `reconnect_max_s` |
| `[display]` | `kind = "log"` alone, or `kind = "snapshot"` with `snapshot_file`, `snapshot_max_age_s`, `refresh_s`, `last_known_cutoff_s`, `framebuffer` and `font_file` (paths absolute) |
| `[hardware]` | optional; `outputs_enabled` may only be `false` |

The only default is the fail-safe one: a missing `[hardware]` table means outputs
disabled. Cross-checks: `tick_s < heartbeat_timeout_s`, `fetch_timeout_s <=
fetch_interval_s`, `fetch_interval_s < max_age_s`, `clock_check_timeout_s <=
clock_check_interval_s`, `reconnect_initial_s <= reconnect_max_s`,
`rate_limit_initial_s <= rate_limit_max_s`, `3 * request_timeout_s <= fetch_timeout_s`,
`snapshot_max_age_s > tick_s`, `refresh_s < snapshot_max_age_s`,
`last_known_cutoff_s > max_age_s`.
The file holds no credentials; unknown keys such as `password` are rejected, and Dexcom
credentials come from systemd credentials.

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

`python -m glucose_guardian.service --config <file>` runs the configured scenario, or
Dexcom Share acquisition with the systemd clock check, in real time with daemon-thread
workers, the configured display and the configured transport. With `dexcom_share`,
missing credentials or a missing state directory stop the service at startup with exit
status 2, and so does a missing snapshot directory. A
serial transport gets a random 16-bit session per process. It
stops on SIGTERM or SIGINT and restores the previous signal handlers. Both the runtime
and the display process use `shutdown.stop_on_signals()`: the handler only writes one
byte to a non-blocking, close-on-exec self-pipe, and the loop's `wait(timeout_s) -> bool`
polls its read end, so a handler never takes a lock. Exit status: 0
stopped, 1 crashed, 2 configuration error. Logs contain levels and fault names, never
glucose values. Stopping the service stops heartbeats, which the Uno will treat as a
communication fault; a planned stop is not silent.

## Display (M5 PR A)

The runtime and the screen are separate processes, connected by one file. The design,
its decisions and the hardware steps still to come are in the
[M5 plan](plans/2026-11-13-m5-hdmi-display.md).

- **Frame:** `build_frame` adds the last valid reading's time and trend, the data
  source (`simulated` or `dexcom_share`), the provider status (`None` exactly for a
  simulated source) and whether outputs are enabled. The runtime builds the frame
  inside its display guard, so a frame or display error is counted and alarm
  evaluation continues.
- **Fan-out:** with `kind = "snapshot"` the service renders every frame to
  `LogDisplay` and `SnapshotDisplay` through `FanOutDisplay`, which calls both and
  raises the first failure only afterwards.
- **Snapshot:** `snapshot.py` writes schema 1 as compact JSON every tick:
  - a new partial file with mode 0640, then an atomic rename;
  - no `fsync`, because the file belongs on tmpfs (`/run`) and the rename is atomic
    for readers;
  - at most 4096 bytes.

  The reader accepts only a regular, non-symlinked file with exactly the schema's
  fields, strict types, known values, a UTC reading time and consistent fields. Any
  deviation rejects the whole snapshot.
- **Screen model:** `screen_model.classify` turns a snapshot, or a missing or invalid
  one, into one of four states:
  - `no_data`: no usable snapshot, one older than `snapshot_max_age_s` or written in
    the future by monotonic time, or one from another source or outputs setting;
  - `live`: the engine marked the reading current and its age, recomputed as
    `data_age_s + (now − written)` on the monotonic clock, is within `max_age_s`;
  - `last_known`: an older value in grey, labelled with its age, until
    `last_known_cutoff_s`;
  - `no_current_data`: no number, with the reason.

  Colors and labels come from the engine's `GlucoseLevel`; the display adds no
  threshold. The cutoff is a display setting and never reaches the alarm engine.
- **Renderer:** `screen_render.Renderer` draws the model on a black 800×480 image:
  - DejaVu Sans Bold, shrunk to fit and shortened rather than clipped;
  - trend arrows drawn as polygons;
  - the connection line, faults and silences, a clock, "DISPLAY ONLY: NO ALARM
    OUTPUT" and "SIMULATED DATA".
- **Display process:** `python -m glucose_guardian.screen --config <file> --png <file>`
  reads the snapshot every `refresh_s` and redraws only when the model changes. It
  sends `READY=1`, `WATCHDOG=1` and `STOPPING=1` when `NOTIFY_SOCKET` is set.
  - A render or output error ends it with status 1.
  - `--blank` writes a "Display restarting" frame.
  - It imports neither the Share client, the credential loader, the login state nor
    any network client module. A test checks this in a fresh interpreter.
- **Output:** a PNG file only. The framebuffer or DRM/KMS output follows in PR B,
  after the hardware smoke test.

## Dexcom Share acquisition

`DexcomShareProvider` implements `GlucoseProvider` on the read-only, undocumented Share
API. The full findings, assumptions and operator procedures are in
[DEXCOM_SHARE.md](DEXCOM_SHARE.md). The client authenticates the publisher account,
because the API has no follower login. A read-only connectivity check against Dexcom
passed on the Pi on 2026-11-13.

**Each fetch** runs in the existing `BoundedWorker`, so at most one request is ever in
flight. A fetch makes at most three HTTP requests:

- the first: authenticate, login, read;
- after a session error: read, login, read.

Each request:

- has its own `request_timeout_s` deadline and a 64 KiB body cap;
- verifies TLS against the system CA store;
- refuses redirects and ignores environment proxies;
- is never retried.

**Read-only allowlist.** Before anything is sent, `ShareSession._call` passes every
request through `dexcom_share.check_request`. The request must meet all of these:

- **Server:** one of the three Dexcom Share hosts in `SERVERS`, over HTTPS, or the
  loopback simulator `http://127.0.0.1:<port>`.
- **Endpoint:** exactly `General/AuthenticatePublisherAccount`,
  `General/LoginPublisherAccountById` or `Publisher/ReadPublisherLatestGlucoseValues`.
- **Query:** none for the two login steps. For the read, exactly `sessionId` (a UUID),
  `minutes` (1 to 1440) and `maxCount` (1 to 288), in that order.
- **Body:** a JSON object with exactly the documented keys and string values. The read
  body is `{}`.

Anything else raises `ShareRequestRefused` (a protocol error) and sends nothing.
`urllib_poster` only sends POST, has no method or header parameter, and refuses every
redirect. Only `share_client.py` imports network client modules (`service.py` imports
`ssl` only to build the TLS context), and a test enforces this.

**Deadline enforcement.** `urllib_poster` installs its own HTTP and HTTPS connection
classes, so the deadline covers more than each single socket operation:

- **Connect:** each resolved address gets only the remaining budget, so several
  unanswered addresses share one deadline.
- **After connect:** a timer shuts the socket down when the deadline passes (on a
  `dup()` of it, so the TLS socket is reached too). This ends a blocked or slow-drip
  TLS handshake, header read or body read at the deadline, however the peer paces
  its bytes.
- **Reported failure:** a request that reaches its deadline fails with "request
  deadline exceeded", even if bytes arrived meanwhile, so a body cut short by the
  shutdown is never accepted.
- **Declared length:** a body shorter than its `Content-Length` is a transport error.
  `http.client` returns it silently otherwise.
- **Timer threads:** the timer is cancelled and joined when the request ends, so no
  thread or socket outlives it.

DNS resolution (`getaddrinfo`) cannot be interrupted. A slow resolver delays the
request by its own timeout, and the request then fails at once because the deadline
has passed. The `BoundedWorker` still holds at most one acquisition job, so a slow
resolver delays acquisition but starts no new thread.

**Login state** (`share_state.py`), persisted in `state_file`:

- **Cooldown:** at least `login_min_interval_s` between login attempts, written
  *before* each attempt and measured on `CLOCK_BOOTTIME`. That write also sets
  `login_pending`. The flag is cleared only once the attempt's outcome has been
  recorded: success, a transport, server or protocol error, a rate limit, or a block.
  A state file still showing `login_pending` at load means an outcome was lost, for
  example to a crash or a failed write. Automatic login then stays blocked
  (`LoginOutcomeUnknown`) until an operator unblocks.
- **Rate limiting:** HTTP 429 backs off exponentially, honoring `Retry-After` in
  seconds. The backoff is cleared only by a read whose body passes `parse_entries`,
  never by a bare HTTP 200 or a login.
- **Latch:** rejected credentials, a lockout, a malformed login argument or a nil
  identifier block automatic login until an operator runs `share_state ... unblock`.
- **Restarts:** a restart within the same boot keeps the cooldown, and a reboot
  restarts it from service start.
- **Damaged or unwritable file:** an unreadable state file blocks login; an
  unwritable one refuses every login, because the attempt record must be written
  before any request. If a later write fails, the running process still keeps the
  block or backoff in memory. After a restart:
  - an unrecorded login outcome blocks, as above;
  - an unrecorded backoff from a read is replaced by the persisted login interval,
    because a restarted service has no session and must log in first.
- **Writes:** mode 0600 (also forced on a leftover partial file), `fsync`, an atomic
  rename and a directory `fsync`. A failed write removes the partial file and leaves
  the previous state intact.

**Parsing:**

- an entry must have `WT`, `ST`, `DT`, `Value` and `Trend`; additional fields are
  ignored and never read;
- `WT` (UTC) is the reading time, and the receipt time is never substituted;
- `Value` is an integer from 39 (LOW) to 401 (HIGH), classified by the unchanged
  thresholds;
- the newest entry by timestamp wins;
- identical duplicates collapse, and conflicting ones fail the whole response;
- older or repeated readings are not returned again, so their age grows into
  `missing_data`.

Every failure raises a `ShareError` and becomes the runtime's `acquisition` fault.
`DexcomShareProvider.status` tells a blocked login apart from network, server,
protocol, rate-limit and deferred-login states; the display shows it. Each `Reading`
carries the trend of its own entry, so a rejected newer entry can never lend its trend
to the value shown (M5 finding F1); no alarm uses the trend. Logs and exceptions carry
only error classes, HTTP statuses
and Share error codes: never credentials, identifiers, URLs, server messages or values.

## Serial transport

`SerialTransport` uses pyserial only to move bytes (non-blocking, exclusive open,
115200 baud); framing, checksums, link state and reconnection are this package's
code. Each `receive()` reads at most 1024 bytes into a 63-byte line buffer. Each
`send()` writes one state frame through `PyserialPort`, which returns 0 instead of
calling pyserial when a zero-timeout `select` reports no room; pyserial would
otherwise spin on a full buffer. A frame's sequence enters the acknowledgement window
only after a complete write. A zero, partial or failed write, an open failure or an
I/O error discards unsent output, closes the port and schedules a retry with
exponential backoff between `reconnect_initial_s` and `reconnect_max_s`. A successful
open starts a new initial connection but does not reset the backoff: it returns to
`reconnect_initial_s` only when a connection fails after staying up for at least
`reconnect_max_s`. Repeated short-lived connections therefore keep backing off, which
also bounds DTR resets of the Uno and its EEPROM boot-counter writes. See "Transmit failures"
in [PROTOCOL.md](PROTOCOL.md). Tests exercise the real pyserial
path over pseudo-terminals against a Python Uno emulator.

## Firmware

`lib/guardian/` contains the protocol parser and formatter, `PiLink` (startup grace,
heartbeat timeout, replay rejection, local silence), `Button` (debounce,
short/long classification, stuck detection), reset-cause decoding, and the
`Controller` that combines them into replies and an alarm `Demand`. It is C++11
without Arduino headers or the STL and has native Unity tests (`pio test -e native`).
`src/main.cpp` captures reset flags in `.init3`, reads and clears the
watchdog-interrupt marker in `.noinit` RAM, enables the two-stage hardware watchdog
(1 s interrupt that writes the marker, then 1 s reset), increments the EEPROM boot
counter, moves serial bytes, and computes the demand
without driving any pin. The button input is fixed at "released" until a pin is
assigned after wiring review.
