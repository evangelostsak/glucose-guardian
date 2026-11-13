# M5 HDMI display: design and implementation plan

Status:

- Proposed on 2026-11-13 for owner approval. No M5 code exists.
- Display only. No siren, buzzer, bed shaker, GPIO output, firmware change, physical
  acknowledgement change or new escalation logic.
- The decisions needed before implementation are in section 9.

## 1. Goal and boundaries

A fullscreen bedside dashboard on the Waveshare 5-inch HDMI LCD (800×480) attached to
the Raspberry Pi 3B (Debian 13). It shows:

- the glucose value in mg/dL, large enough to read from bed;
- the Dexcom trend arrow;
- a color-coded glucose status, taken from the alarm engine's level;
- the reading's time and age;
- data freshness, with outdated data marked as such;
- the Dexcom Share connection status;
- explicit screens for unavailable, invalid and outdated readings.

It starts at boot through systemd, recovers from display-process crashes and hangs on
its own, and needs no keyboard, mouse or desktop.

Invariants:

- **One Dexcom client.** Only the existing runtime service talks to Dexcom, through
  the existing `ShareSession`. The display process has no network access, no
  credentials and no access to the login state file.
- **No reclassification.** Colors and labels come from the alarm engine's
  `GlucoseLevel`. The display adds no threshold.
- **Never an old reading as current.** A value is drawn as current only when the alarm
  engine marked it current and its age, recomputed by the display, is within
  `max_age_s`.
- **Unchanged:** alarm policy, thresholds, acknowledgement, fault set, GG1 protocol,
  firmware, the read-only request allowlist, the login interval, the rate-limit
  backoff and the login latch.

## 2. Repository inspection (2026-11-13)

| Component | Today | Use in M5 |
|---|---|---|
| `runtime.Runtime.tick` | Renders `build_frame(state, last_valid, now)` through the `Display` protocol every tick (`tick_s = 1` in both example configurations). Display exceptions are counted in `display_errors` and never stop alarm evaluation | Loop unchanged; a new `Display` implementation publishes each frame to the display process |
| `display.Frame`, `build_frame` | Level, last valid value, `last_known`, data age, faults, audible and silenced flags, the "Hold 2 s to silence" prompt | Reused and extended (F4) |
| `alarms.State`, `GlucoseLevel`, `Fault` | Five levels, seven faults, `glucose_current` | Source of the screen's colors and labels |
| `DexcomShareProvider.status` | Nine `ShareStatus` values, including `auth_blocked`, `login_deferred` and `rate_limited` | Connection line (section 3.4) |
| `DexcomShareProvider.latest_trend` | Trend of the newest accepted entry; read only by one test | Replaced (F1) |
| `config.load_config` | Strict TOML: unknown or missing keys are errors | Extended with a required `[display]` table |
| `service.py` | Builds the provider, the transport and `LogDisplay`; the only process holding the Share session and the credentials | Adds the snapshot display next to `LogDisplay` |
| `simulation.SCENARIOS` | Eleven deterministic scenarios | Screen previews and end-to-end tests |
| `deployment/systemd/glucose-guardian.service` | Uninstallable template: `Restart=on-failure`, no watchdog (audit finding M6) | Made installable if D1 is approved |
| `test_share_readonly.py` | Only `share_client.py`, and `service.py` for `ssl`, may import network modules | Extended: display modules import neither network modules nor the Share client |
| Dependencies | `pyserial==3.5` only; no GUI toolkit | One new pinned dependency (D2) |

Findings:

- **F1. The trend can belong to a different reading than the one shown.**
  `DexcomShareProvider._accept` sets `latest_trend` for every newer entry. The runtime
  keeps an entry as `_last_valid` only when the alarm engine finds it current. A newer
  entry that is already stale (after an outage, for example) or more than
  `max_future_skew_s` in the future therefore updates `latest_trend`, while the screen
  would still show the previous value. Nothing outside a test reads `latest_trend`
  today, so there is no visible effect yet. M5 moves the trend into `Reading`, so it
  travels with its value, and removes `latest_trend`. The alarm engine ignores the
  trend.
- **F2. The provider status lags a hung fetch.** `status` changes only when a fetch
  completes. A fetch past `fetch_timeout_s` raises the `acquisition` fault while
  `status` keeps its previous value, possibly `ok`. The connection line combines both
  (section 3.4).
- **F3. A second Dexcom session is not an option.** A display process with its own
  `ShareSession` would double the logins under the 300 s interval and race the
  runtime on the state file. The display gets its data from the runtime.
- **F4. `Frame` lacks** the reading's timestamp, its trend, the data source (simulated
  or Dexcom) and whether outputs are enabled.
- **F5. The display depends on a supervised runtime.** Without systemd supervision, a
  runtime crash leaves the screen on "not running" until someone restarts the
  runtime. Audit finding M6 (restart policy and watchdog) is therefore a prerequisite
  (D1).
- **F6. Platform limits (audit finding M9).** 1 GB of RAM rules out a browser kiosk.
  Screen blanking and night brightness need explicit handling.

## 3. Design

### 3.1 Processes and data flow

```text
glucose-guardian.service           existing runtime, the only Dexcom client
  Runtime.tick -> build_frame -> FanOutDisplay -> LogDisplay
                                              -> SnapshotDisplay -> /run/glucose-guardian/display.json

glucose-guardian-display.service   new: no network, no credentials, no Share state
  read snapshot -> screen_model(snapshot, now) -> render 800x480 image -> framebuffer
```

Two processes:

- **Isolation:** a crash, hang or memory leak in the renderer cannot stop alarm
  evaluation or the heartbeat to the Uno.
- **Least privilege:** the display runs as its own user with `PrivateNetwork=yes` and
  no credentials.
- **Honest failure:** a separate display can show that the runtime has stopped. An
  in-process renderer would stop with it.

A single snapshot file in `/run`:

- is written atomically every tick (temporary file, then rename), so a reader never
  sees a partial snapshot;
- lives on tmpfs, so live values never reach the SD card and disappear at reboot;
- is removed by systemd (`RuntimeDirectory=`) when the runtime stops, which the
  display shows as "not running";
- is read from scratch on every refresh, so a restarted display shows the current
  state at once.

`FanOutDisplay` calls every display and raises the first error only after all have
been called, so a failing snapshot write cannot stop the log display. The runtime
counts the error as before.

### 3.2 Rendering stack

| Option | Assessment |
|---|---|
| Browser kiosk | Excluded: audit finding M9, 1 GB of RAM, needs a compositor |
| Tkinter | Needs an X server and a graphical session started by systemd, `python3-tk` on the Pi and Xvfb in CI. X brings its own blanking and power management |
| pygame (SDL2 on KMS/DRM) | Needs no X and handles modesetting, but it is a game library with audio and input subsystems. The PyPI wheels bundle their own SDL, whose KMS/DRM support on the Pi is unverified; Debian's `python3-pygame` would need a virtual environment that sees system packages. Fallback if the framebuffer check (H2) fails |
| **Pillow and the Linux framebuffer** | **Recommended.** One pinned dependency. Rendering is a pure function to an image, testable pixel by pixel in CI without a display. The output step writes the image to `/dev/fb0`, the KMS driver's framebuffer emulation, in the pixel format read from sysfs. No X or Wayland |

The screen content changes at most once per refresh, so double buffering and vsync do
not matter. The display redraws only when the screen model changes: value, age in
minutes, status or clock minute.

### 3.3 Snapshot schema 1

| Field | Type | Source |
|---|---|---|
| `schema` | `1` | constant |
| `written_monotonic_s` | number | the runtime's monotonic clock. `CLOCK_MONOTONIC` is system-wide on Linux, so both processes read the same clock |
| `source` | `"dexcom_share"` or `"simulated"` | configuration |
| `source_status` | a `ShareStatus` value, or `null` when simulated | `DexcomShareProvider.status` |
| `outputs_enabled` | bool, `false` throughout M5 | `[hardware]` |
| `max_age_s` | number | policy |
| `glucose` | a `GlucoseLevel` value | `State.glucose` |
| `glucose_current` | bool | `State.glucose_current` |
| `mg_dl` | number or `null` | last valid reading |
| `reading_utc` | ISO 8601 UTC or `null` | last valid reading's timestamp |
| `data_age_s` | number or `null` | `Frame.data_age_s` at write time |
| `trend` | a `Trend` value or `null` | last valid reading (F1) |
| `faults` | list of `Fault` values | `State.faults` |
| `glucose_audible`, `fault_audible`, `glucose_silenced`, `faults_silenced` | bool | `State` |
| `prompt` | string or `null` | `Frame.prompt` |

Rules:

- **Writer:** mode 0640, readable by the display user through its group. The schema
  bounds the size.
- **Reader:** reads at most a fixed number of bytes. A wrong schema number, a missing,
  extra or ill-typed field, or a parse error rejects the whole snapshot. No field of a
  rejected snapshot is used.
- **Privacy:** neither process logs glucose values. Development previews use
  synthetic scenarios only.

### 3.4 Screen states

Every refresh classifies the snapshot. The first matching row wins:

| # | Condition | Glucose area | Trend arrow |
|---|---|---|---|
| 1 | Snapshot missing, unreadable or rejected; older than `snapshot_max_age_s` by monotonic time; or written in the future | Full screen "NO DATA", with "Glucose Guardian is not running" or "display data invalid"; no value | none |
| 2 | `glucose_current`, and the age recomputed by the display is at most `max_age_s` | Value in the level color, "mg/dL", age and reading time | shown |
| 3 | Not current, and a last valid reading exists | Value in grey, smaller, marked "LAST KNOWN", with its age shown prominently | none |
| 4 | No valid reading since start | "– – –" with the reason, such as "waiting for the first reading" | none |

- **The required cases:** unavailable data is state 4, or state 1 when the runtime is
  down; an outdated reading is state 3; an invalid reading shows the previous valid
  value in state 3, or state 4, with the reason line.
- **Recomputed age:** `data_age_s + (now_monotonic − written_monotonic_s)`, so a
  frozen snapshot cannot freeze the age. The reading time is shown in local time.
- **Level band:** shows the engine's level. A retained urgent low, low or high during
  stale data keeps its color and is labelled "unresolved: no current data"; the number
  itself is grey (state 3). `unknown` is grey.
- **Reason line,** from the faults: `clock_unsynchronized` gives "clock not
  synchronized: readings not evaluated", `invalid_reading` "latest reading invalid",
  `missing_data` "no current reading".
- **Connection line,** from `source_status` and the `acquisition` fault:

  | `source_status` | `acquisition` fault | Shown |
  |---|---|---|
  | `ok` | no | "Dexcom: connected" |
  | `ok` | yes | "Dexcom: no response" (a hung fetch, F2) |
  | `starting` | either | "Dexcom: connecting" |
  | `login_deferred` | either | "Dexcom: waiting to log in" (up to 300 s after a reboot) |
  | `network_error` | either | "Dexcom: no connection" |
  | `server_error` | either | "Dexcom: server error" |
  | `protocol_error` | either | "Dexcom: unexpected response" |
  | `rate_limited` | either | "Dexcom: rate limited, waiting" |
  | `auth_blocked` | either | "Dexcom: login blocked, operator action needed", in red |
  | `state_error` | either | "Dexcom: login state unavailable" |

- **Faults and silences:** every active fault by a short label, the silenced channels,
  and the existing "Hold 2 s to silence" prompt.
- **Permanent markers:**
  - "SIMULATED DATA" whenever `source` is `simulated`;
  - "DISPLAY ONLY: NO ALARM OUTPUT" while `outputs_enabled` is false, which is all of
    M5;
  - the local time (HH:MM) in a corner, so a frozen screen shows a wrong time.
- **Enforced by tests:** a number in a level color, and a trend arrow, appear only in
  state 2.

### 3.5 Layout (800×480)

```text
+------------------------------------------------------------------------------+
| Dexcom: connected                                                     03:14  |
|                                                                              |
|                                                                              |
|                      123                                --->                 |
|                                                                              |
|                                                                              |
| NORMAL                                         mg/dL  4 min ago (03:10)      |
| DISPLAY ONLY: NO ALARM OUTPUT                                                |
+------------------------------------------------------------------------------+
```

`123` is a synthetic placeholder.

- **Size:** about 0.14 mm per pixel, from the nominal 5-inch diagonal, so a digit
  200 pixels tall is about 27 mm. Text shrinks to fit its box, so any font and any
  value fit.
- **Night use:** black background, so only text emits light. The panel's backlight is
  not software-controlled over HDMI; brightness is checked on the device (H9).
- **Colors (proposal, D5):** urgent low white on red, low red, normal green, high
  amber, unknown and last known grey. Every color comes with its text label, so color
  is never the only cue.
- **Trend:** drawn as polygons, one arrow or two for `DoubleUp` and `DoubleDown`, so
  no font glyph is needed. `None`, `NotComputable` and `RateOutOfRange` draw no arrow.
- **Device limits:** 39 and 401 are Dexcom's LOW and HIGH (an M4 assumption, still
  unconfirmed on Share). They are shown as "<40" and ">400" (D5).

### 3.6 systemd

New unit `glucose-guardian-display.service`:

| Directive | Reason |
|---|---|
| `Type=notify`, `WatchdogSec=` | Ready after the first frame. The loop pings every refresh, so a hang is killed and restarted |
| `Restart=always`, `RestartSec=`, `StartLimitIntervalSec=0` | Restart after any exit and never give up: a display that stops retrying leaves a blank or frozen screen |
| `ExecStopPost=` running the display with `--blank` | After any stop, crash or watchdog kill, overwrite the last frame with "Display restarting", so a stale value never stays on the panel |
| Own `User=`; `SupplementaryGroups=` `video` and the runtime's group | Framebuffer access and read-only snapshot access |
| `PrivateNetwork=yes`, no `LoadCredential*` | No network, no credentials |
| `ProtectSystem=strict`, `ProtectHome=yes`, `NoNewPrivileges=yes`, `PrivateTmp=yes`, `DevicePolicy=closed` with `DeviceAllow=` for the framebuffer and its console | Least privilege |
| `TTYPath=` the first console, `Conflicts=getty@tty1.service` | The display owns the console and switches it to graphics mode, so no login prompt or kernel text draws over the dashboard |
| `Nice=` | Rendering never competes with the runtime's tick |
| No `Requires=` on the runtime; `WantedBy=multi-user.target` | Starts at boot without a desktop, and runs, saying so, when the runtime does not |

Runtime unit changes (D1): `Restart=always`, `Type=notify` with a watchdog ping from
the tick loop, and `RuntimeDirectory=glucose-guardian` for the snapshot. `WatchdogSec`,
`RestartSec`, `refresh_s` and `snapshot_max_age_s` stay provisional until measured on
the Pi (D7, section 6).

### 3.7 Configuration

A required `[display]` table in the runtime configuration. The display process reads
the same file, so the snapshot path has a single source.

| Key | Use |
|---|---|
| `kind` | `"log"` (today's behavior) or `"snapshot"` |
| `snapshot_file` | absolute path under `/run`; required with `kind = "snapshot"` |
| `snapshot_max_age_s` | snapshot age at which the screen shows "NO DATA"; more than `tick_s` |
| `refresh_s` | display loop period; less than `snapshot_max_age_s` |
| `framebuffer` | framebuffer device |
| `font_file` | TrueType font (D4) |

Because the table is required, an existing configuration, including the Pi's
`config/local.dexcom.toml`, stops with a configuration error naming it until the table
is added. The examples and `make dexcom-config` will include it.

## 4. Failure handling

| Failure | Detected by | Screen | Recovery | Alarm runtime |
|---|---|---|---|---|
| Display process crash | systemd | "Display restarting" (`ExecStopPost`), then the dashboard | `Restart=always` | Unaffected |
| Display loop hang | Missing watchdog ping | As above, after `WatchdogSec` | Watchdog kill and restart | Unaffected |
| Rendering error | Exception in the loop; no ping after a failed render | Last good frame until the watchdog fires | Restart | Unaffected |
| Runtime crash or stop | Snapshot removed, or older than `snapshot_max_age_s` | "NO DATA: Glucose Guardian is not running" | Runtime unit restart (D1) | The Uno's link-lost demand (audible from M6) |
| Runtime loop stalled, process alive | Snapshot ages | As above | Runtime watchdog (D1) | As above |
| Snapshot corrupt or of another schema | Strict reader | "NO DATA: display data invalid" | Next valid snapshot | Unaffected |
| Dexcom outage, rate limit, deferred or blocked login | `source_status` and the `acquisition` fault | Connection line; the value ages into "LAST KNOWN" | Existing M4 behavior; a blocked login needs the operator | Existing faults |
| Outdated reading | Engine (`missing_data`) and the recomputed age | "LAST KNOWN" in grey with its age | Next fresh reading | Existing |
| Invalid reading | Engine (`invalid_reading`) | Previous valid value as "LAST KNOWN", or "– – –", with the reason line | Next valid reading | Existing |
| Clock not synchronized | Engine (`clock_unsynchronized`) | Reason line; no current value | NTP | Existing |
| Framebuffer missing or HDMI unplugged | Open or write error | none | Restart loop every `RestartSec` | Unaffected |
| Whole Pi hangs or loses power | Nothing on the Pi | Frozen or dark panel; the frozen clock shows it | Power cycle | Uno link-lost demand (M6); backup power (M7) |

Residual risk: a kernel or GPU hang freezes the last frame, value included. Until the
Uno's audible link-lost alarm exists (M6), the on-screen clock is the only cue.

## 5. Testing

All CI tests are offline, use synthetic data and need no display hardware.

- **Screen model (pure):**
  - a table over the four states and their precedence;
  - transitions: live to last known and back, runtime down and up, `auth_blocked` to
    `ok`;
  - boundaries: age exactly `max_age_s` and just over, snapshot age exactly
    `snapshot_max_age_s` and just over;
  - failures: missing, truncated, oversized, wrong-schema and ill-typed snapshots, a
    snapshot written in the future, negative ages;
  - recovery after each failure.
- **Invariants:** across every scenario and state, a level-colored number and a trend
  arrow appear only in state 2; "SIMULATED DATA" appears exactly when the source is
  simulated.
- **Snapshot:** writer round trip, atomic replacement, mode and size bound; the reader
  rejects each malformed field.
- **Trend move (F1):** alarm engine results are identical with and without trends; a
  stale or future-dated newest entry never pairs its trend with the previous value.
- **Renderer:** every state renders at 800×480 without error; text stays inside its
  box for the widest strings; sampled pixels have the expected colors; arrows point
  the right way.
- **Framebuffer output:** against a temporary file and a fake sysfs directory, for 16-
  and 32-bit pixel formats and padded line lengths.
- **systemd notification:** against a temporary datagram socket.
- **End to end:** the real service runs each simulated scenario with the snapshot in a
  temporary directory, and the display loop writes to an image sink.
  - Killing the runtime shows "NO DATA" within `snapshot_max_age_s + refresh_s`, and
    restarting it recovers.
  - Killing the display leaves the runtime's tick and error counts unchanged.
- **Static:**
  - display modules import no network module, `share_client`, `credentials` or
    `share_state`;
  - the display unit has `PrivateNetwork=yes` and `Restart=always`, and no
    `LoadCredential*`;
  - `systemd-analyze verify` where available.
- **Preview:** `make display-preview` writes a PNG per state and scenario for review
  in the pull request, with synthetic values only.
- **Regression:** `make check` passes; the M4 read-only, login-protection and alarm
  tests are unchanged; the production firmware hash stays `bde6b740…`.

## 6. Hardware-dependent validation (waits for the screen)

Owner-run on the Pi, after explicit approval to deploy. Photos and reports must not
show real glucose values; use the simulated configuration for photos.

1. **H1, panel:** the exact Waveshare model and revision; the modes it reports over
   HDMI; whether 800×480 is used without configuration. If not, follow Waveshare's
   documentation for that revision and record the boot configuration change.
2. **H2, framebuffer:** `/dev/fb0` exists at 800×480 with a supported pixel format;
   the picture is upright with correct colors. If not, the pygame fallback applies
   (D2).
3. **H3, console:** no login prompt, cursor or kernel message ever draws over the
   dashboard, also across reboots.
4. **H4, blanking:** the screen never blanks during an overnight soak of at least
   8 hours.
5. **H5, boot:** the dashboard appears after boot without keyboard or mouse; record
   the time to the first frame.
6. **H6, recovery:** `kill -9` and `SIGSTOP` of the display process, and a stop and
   start of the runtime; record the recovery times.
7. **H7, cabling:** HDMI unplug and replug, and a panel power cycle; the picture
   returns without intervention.
8. **H8, readability:** owner judgement at the real bedside distance, in darkness and
   daylight, and from the side.
9. **H9, brightness:** night brightness with the panel's own control, and whether it
   is acceptable.
10. **H10, resources:** CPU, memory and temperature during the soak. The runtime's
    `overruns` counter stays at 0, and no under-voltage is reported with the panel
    powered as installed (audit finding M9).
11. **H11, touch:** if the panel has touch input, touching does nothing; no touch
    driver is installed.
12. **H12, colors:** the level colors are distinguishable on this panel.

The soak against Dexcom Share with outputs disabled, still open from the M4 plan, runs
as part of H4 and H10.

## 7. Acceptance criteria

1. The four screen states and every connection status render as specified, and the
   tests in section 5 pass in CI on Python 3.11 and 3.13.
2. No test, scenario or failure shows a level-colored number or a trend arrow unless
   the engine marked the reading current and the recomputed age is within
   `max_age_s`.
3. A runtime stop shows "NO DATA" within `snapshot_max_age_s + refresh_s`.
4. A display crash or hang never changes the runtime's tick, fault or error counts,
   and the display comes back by itself.
5. The display process has no network, no credentials and no Share state access,
   enforced by tests and by the unit.
6. Alarm policy, thresholds, acknowledgement, GG1, firmware and the M4 protections are
   unchanged, and `make check` passes.
7. On the Pi (section 6): boot to the dashboard without interaction, no blanking or
   console text overnight, recovery in H6 and H7, zero runtime overruns, and
   readability confirmed by the owner.

## 8. Implementation plan

Two pull requests. Each step is a small increment with its own tests.

PR A, offline (CI only, nothing on the Pi):

1. **Trend with its reading:** `Reading.trend`, set by `DexcomShareProvider` and the
   scenarios; `latest_trend` removed. Done when the F1 tests pass and the alarm tests
   are unchanged.
2. **Frame extension:** the reading time, trend, source and outputs flag in `Frame`
   and `build_frame`.
3. **Source status:** the service passes `DexcomShareProvider.status`, or "simulated",
   to the display layer.
4. **Snapshot:** schema 1, `SnapshotDisplay`, `FanOutDisplay`, the strict reader and
   the `[display]` table, with the examples updated.
5. **Screen model:** the pure classification of section 3.4 with its test tables.
6. **Renderer:** Pillow layout, fit-to-box text, arrows and colors;
   `make display-preview`.
7. **Display process:** `python -m glucose_guardian.screen` with the refresh loop,
   redraw on change, image sink, systemd notification, `--blank` and signal handling;
   end-to-end tests with the simulated service.

PR B, Pi integration:

8. **Framebuffer output** and console graphics mode, tested against fakes.
9. **systemd units:** the display unit and the runtime unit changes (D1), with static
   unit tests. Installation is documented in a new `docs/DISPLAY.md` and stays a
   manual, approved step.
10. **Documentation:** ARCHITECTURE, the SAFETY failure table, PROJECT_STATUS and
    DEVELOPMENT_LOG.
11. **Pi validation (section 6),** after explicit approval to deploy, recorded in
    `docs/DISPLAY.md`.

## 9. Decisions needed

| # | Decision | Recommendation |
|---|---|---|
| D1 | Supervise the runtime in M5: an installable unit with `Restart=always`, a watchdog and the snapshot directory (audit finding M6). Persisting the retained alarm level stays before M6 outputs | Yes: without a supervised runtime the display has nothing reliable to show |
| D2 | Rendering stack and the new runtime dependency | Pillow, pinned exactly, with the framebuffer; pygame with SDL on KMS/DRM if H2 fails |
| D3 | Pi image: 32- or 64-bit, Lite or desktop | Report `uname -m` (shown by `make status`) and whether a desktop starts. A running desktop must be disabled so it does not own the display |
| D4 | Font | DejaVu Sans Bold from Debian's `fonts-dejavu-core`, its path in `[display]` |
| D5 | Colors, labels, and "<40" and ">400" for 39 and 401 | As proposed in section 3.5 |
| D6 | Last known value: shown with its age until fresh data arrives, or hidden after a cutoff | Shown, grey and labelled, with its age. A cutoff would be a new clinical-style value for the owner to set |
| D7 | `snapshot_max_age_s`, `refresh_s`, `WatchdogSec`, `RestartSec` | Provisionally 5 s (the `heartbeat_timeout_s` after which the Uno declares the Pi lost), 1 s (the runtime's `tick_s`), 10 s and 2 s; confirmed against measured render times on the Pi |
| D8 | Transport during the M5 soak | Serial, with the Uno on USB running the production firmware, which drives no output; the soak then also exercises GG1. Simulated is the alternative |
| D9 | Install location, users and state file | `/opt/glucose-guardian`, users `glucose-guardian` and `glucose-guardian-display`. The live check used `~/glucose-guardian-state/dexcom-share-state.json`; afterwards the check and the service must share one state file so the 300 s login interval spans both. Recommended: the service's `/var/lib/glucose-guardian/dexcom-share-state.json`, with `DEXCOM_CONFIG` pointing at the installed configuration |
| D10 | One or two pull requests | Two, as in section 8 |

## 10. Out of scope

Siren, buzzer, bed shaker, GPIO outputs, firmware changes, physical acknowledgement,
new escalation logic, touch input, trend-based alerts, persisting readings, persisting
the retained alarm level (before M6 outputs), backup power (M7), any Omnipod
interaction and any write to Dexcom.
