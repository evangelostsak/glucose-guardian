# M5 HDMI display: design and implementation plan

Status:

- Proposed on 2026-11-13 in PR #9.
- Design review on 2026-11-13: the owner decisions are recorded in section 9. The
  review changed the last-known cutoff (D6) and made the framebuffer output
  conditional on a smoke test (G1). It also added the login-state migration
  (section 3.8) and the account and permission model (section 3.6).
- PR A (steps 1 to 7, offline) was implemented on 2026-11-13: section 11 records it,
  with its deviations. PR B has not started; its prerequisites are in section 12.
- Display only. No siren, buzzer, bed shaker, GPIO output, firmware change, physical
  acknowledgement change or new escalation logic.

## 1. Goal and boundaries

A fullscreen bedside dashboard on the Waveshare 5-inch HDMI LCD (800×480) attached to
the Raspberry Pi 3B (Debian 13, aarch64). It shows:

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
  credentials and no access to the login state file (section 3.6).
- **No reclassification.** Colors and labels come from the alarm engine's
  `GlucoseLevel`. The display adds no threshold.
- **Never an old reading as current.** A value is drawn as current only when the alarm
  engine marked it current and its age, recomputed by the display, is within
  `max_age_s`. A last known value is hidden once it is older than
  `last_known_cutoff_s`.
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
| `share_state.AuthStateStore` | Login state file: cooldown on `CLOCK_BOOTTIME` with the boot id, rate-limit backoff, login latch, `login_pending` | Untouched; moved once to the service's state directory (section 3.8) |
| `config.load_config` | Strict TOML: unknown or missing keys are errors | Extended with a required `[display]` table |
| `service.py` | Builds the provider, the transport and `LogDisplay`; the only process holding the Share session and the credentials | Adds the snapshot display next to `LogDisplay` |
| `simulation.SCENARIOS` | Eleven deterministic scenarios | Screen previews and end-to-end tests |
| `deployment/systemd/glucose-guardian.service` | Uninstallable template: `Restart=on-failure`, no watchdog (audit finding M6) | Made installable (D1) |
| `test_share_readonly.py` | Only `share_client.py`, and `service.py` for `ssl`, may import network modules | Extended: display modules import neither network modules nor the Share client |
| Dependencies | `pyserial==3.5` only; no GUI toolkit | Pillow, pinned exactly (D2) |

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
  runtime crash leaves the screen on "NO DATA" until someone restarts the
  runtime. Audit finding M6 (restart policy and watchdog) is therefore a prerequisite
  (D1).
- **F6. Platform limits (audit finding M9).** 1 GB of RAM rules out a browser kiosk.
  Screen blanking and night brightness need explicit handling.
- **F7. The login state file has a single writer by design.** `AuthStateStore` reads
  the file once when the process starts, keeps the state in memory and replaces the
  file on every change. Two processes on one file, such as the service and a
  connectivity check, would each miss the other's login attempt, and the later write
  would erase the earlier one. This cannot happen today because the service is not
  installed. Section 3.8 makes the check and the service mutually exclusive.
- **F8. A missing state file means no history.** `AuthStateStore` treats a missing
  file as a fresh state: no cooldown, no block and no pending login. Deleting or
  recreating the file would therefore allow an immediate login and forget a block.

## 3. Design

### 3.1 Processes and data flow

```text
glucose-guardian.service           existing runtime, the only Dexcom client
  Runtime.tick -> build_frame -> FanOutDisplay -> LogDisplay
                                              -> SnapshotDisplay -> /run/glucose-guardian/display.json

glucose-guardian-display.service   new: no network, no credentials, no Share state
  read snapshot -> screen_model(snapshot, now) -> Pillow 800x480 image -> output backend
```

Two processes:

- **Isolation:** a crash, hang or memory leak in the renderer cannot stop alarm
  evaluation or the heartbeat to the Uno.
- **Least privilege:** the display runs as its own user, without network and
  credentials, and can only read the snapshot (section 3.6).
- **Honest failure:** a separate display can show that its data stopped arriving,
  without claiming to know why. An in-process renderer would stop with it.

A single snapshot file in `/run`:

- is written atomically every tick (temporary file, then rename), so a reader never
  sees a partial snapshot;
- lives on tmpfs, so live values never reach the SD card and disappear at reboot;
- is removed by systemd (`RuntimeDirectory=`) when the runtime stops, which the
  display shows as "NO DATA";
- is read from scratch on every refresh, so a restarted display shows the current
  state at once.

`FanOutDisplay` calls every display and raises the first error only after all have
been called, so a failing snapshot write cannot stop the log display. The runtime
counts the error as before.

### 3.2 Rendering and output (D2)

**Renderer: Pillow, approved.** Rendering is a pure function from the screen model to
an 800×480 image, testable pixel by pixel in CI without a display. Only the output
step that puts the image on the panel depends on the hardware.

Options considered for the whole stack:

| Option | Assessment |
|---|---|
| Browser kiosk | Excluded: audit finding M9, 1 GB of RAM, needs a compositor |
| Tkinter | Needs an X server and a graphical session started by systemd, `python3-tk` on the Pi and Xvfb in CI. X brings its own blanking and power management |
| pygame (SDL2 on KMS/DRM) | Needs no X and handles modesetting, but it is a game library with audio and input subsystems. The PyPI wheels bundle their own SDL, whose KMS/DRM support on the Pi is unverified; Debian's `python3-pygame` would need a virtual environment that sees system packages |
| Pillow and the Linux framebuffer | Chosen: one pinned dependency, pure rendering, and an output step that writes the image to `/dev/fb0` in the pixel format read from sysfs. No X or Wayland |

**Output backend: `/dev/fb0`, conditional on smoke test G1.** The framebuffer is
adopted only after G1 passes on the real Waveshare panel and Pi.

KMS/DRM compatibility risk. On Debian 13 the Pi 3B's display is driven by the KMS
driver; `/dev/fb0` exists only through the kernel's DRM framebuffer emulation, a
legacy interface. The risks:

- the running kernel may provide no emulated framebuffer, or not for the HDMI output;
- the framebuffer's size follows the mode chosen at boot. A panel that reports an
  unexpected preferred mode, or is powered after the Pi, can leave it at a size other
  than 800×480;
- a process that holds DRM master, such as a desktop compositor (D3: desktop or
  headless is pending), owns the scanout, and framebuffer writes may never appear;
- the pixel format and line length can differ from the expected 16 or 32 bits without
  padding;
- the text console draws on the same framebuffer unless the console is switched to
  graphics mode, which must work for the unprivileged display user;
- later kernels may change or drop the emulation.

Fallbacks, evaluated with the same G1 procedure in this order if G1 fails:

1. pygame with SDL2 on KMS/DRM, which takes DRM master itself and needs access to
   `/dev/dri/card*`. The Pillow image is handed to it unchanged.
2. A direct DRM/KMS output (dumb buffer and modeset), with the most control and the
   most code.

The screen model and the renderer stay the same; only the output step changes. The
owner approves the backend from the G1 record before PR B continues (section 8).

**Smoke test G1** (owner-run on the Pi with the panel attached, after approval; no
runtime and no Dexcom contact):

1. Record the image facts: `uname -m`, the kernel version, and whether a desktop or
   compositor starts (D3).
2. Record the panel facts: the HDMI connector's status and modes under
   `/sys/class/drm`, and `name`, `virtual_size`, `bits_per_pixel` and `stride` under
   `/sys/class/graphics/fb0`.
3. Run the display's test-pattern mode as the display user with the planned sandbox.
   The pattern has a full-frame border, orientation marks, color bars in the level
   colors and a full-size sample digit.
4. Pass when:
   - the pattern fills the panel, upright and with correct colors;
   - no console text or cursor appears;
   - the image stays unchanged for 10 minutes;
   - it reappears after a restart of the test.
5. Record the result in `docs/DISPLAY.md`; on failure, evaluate the fallbacks.

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

- **Writer:** creates the temporary file with mode 0640, as `write_state` forces 0600,
  then renames it (ownership in section 3.6). The schema bounds the size.
- **Reader:** reads at most a fixed number of bytes. A wrong schema number, a missing,
  extra or ill-typed field, or a parse error rejects the whole snapshot. No field of a
  rejected snapshot is used.
- **Privacy:** neither process logs glucose values. Development previews use
  synthetic scenarios only.

### 3.4 Screen states

Every refresh classifies the snapshot. The first matching row wins:

| # | Condition | Glucose area | Trend arrow |
|---|---|---|---|
| 1 | Snapshot missing, unreadable or rejected; older than `snapshot_max_age_s` by monotonic time; or written in the future | Full screen "NO DATA", with "No recent display data" (missing or expired; the display cannot tell whether the runtime stopped, stalled or lost its snapshot directory) or "display data invalid"; no value | none |
| 2 | `glucose_current`, and the age recomputed by the display is at most `max_age_s` | Value in the level color, "mg/dL", age and reading time | shown |
| 3 | Not current, and the last valid reading's recomputed age is at most `last_known_cutoff_s` | Value in grey, smaller, marked "LAST KNOWN", with its age shown prominently | none |
| 4 | Not current, and no valid reading since start, or the last one is older than `last_known_cutoff_s` | "NO CURRENT DATA" with the reason; no number. "Last reading N min ago" when a reading exists | none |

- **The required cases:**
  - unavailable data is state 4, or state 1 when the runtime is down;
  - an outdated reading is state 3 until the cutoff, then state 4;
  - an invalid reading shows the reason line in state 3 or 4.
- **Recomputed age:** `data_age_s + (now_monotonic − written_monotonic_s)`, so a
  frozen snapshot cannot freeze the age. The reading time is shown in local time.
- **Last-known cutoff (D6):** `last_known_cutoff_s`, provisionally 1800 s (30
  minutes), is a display setting applied only to the recomputed age of the last valid
  reading.
  - Neither the runtime nor the alarm engine reads it.
  - `max_age_s`, the thresholds, the retained level, the faults and acknowledgement
    are unchanged.
- **Level band:** shows the engine's level in every state except 1. A retained urgent
  low, low or high during stale data keeps its color, labelled "unresolved: no current
  data", also after the cutoff. The number itself is grey (state 3) or hidden
  (state 4). `unknown` is grey.
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
  state 2; no number appears in states 1 and 4.

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
- **Font (D4):** DejaVu Sans Bold from Debian's `fonts-dejavu-core`, its path in
  `[display]`.
- **Night use:** black background, so only text emits light. The panel's backlight is
  not software-controlled over HDMI; brightness is checked on the device (H9).
- **Colors (D5, approved provisionally):** urgent low white on red, low red, normal
  green, high amber, unknown and last known grey. Every color comes with its text
  label, so color is never the only cue.
- **Trend:** drawn as polygons, one arrow or two for `DoubleUp` and `DoubleDown`, so
  no font glyph is needed. `None`, `NotComputable` and `RateOutOfRange` draw no arrow.
- **Device limits (D5, provisional):** 39 and 401 are shown as "<40" and ">400". That
  they mean Dexcom's LOW and HIGH on Share is an M4 assumption, not yet live-verified.

### 3.6 Accounts, permissions and systemd

Two system accounts, each without a login shell or home directory:

| Account | Primary group | Supplementary groups | Role |
|---|---|---|---|
| `glucose-guardian` | `glucose-guardian` | none in M5; `dialout` only once the real Uno is used (D8) | Runtime: the only Dexcom client |
| `glucose-guardian-display` | `glucose-guardian-display` | `glucose-guardian` (snapshot and configuration, read only), `video` (framebuffer) | Display |

Membership in `glucose-guardian` grants the display only what that group can read:
the configuration and the snapshot. The state directory, the state file and the
credentials are not group-readable, and the display unit also makes them inaccessible.

| Path | Owner and group | Mode | Runtime | Display |
|---|---|---|---|---|
| `/opt/glucose-guardian` (code and virtual environment) | `root:root` | directories 0755, files 0644 | read | read |
| `/etc/glucose-guardian/config.toml` | `root:glucose-guardian` | 0640 | read | read |
| `/etc/glucose-guardian/credentials/` and its `.cred` files | `root:root` | 0700, files 0600 | only through systemd (`LoadCredentialEncrypted=`) | none; `InaccessiblePaths=` |
| `$CREDENTIALS_DIRECTORY` (decrypted at run time) | managed by systemd for the runtime unit only | private | read | none |
| `/var/lib/glucose-guardian/` (`StateDirectory=`) | `glucose-guardian:glucose-guardian` | 0700 | read and write | none; `InaccessiblePaths=` |
| `/var/lib/glucose-guardian/dexcom-share-state.json` | `glucose-guardian:glucose-guardian` | 0600 | read and write | none |
| `/run/glucose-guardian/` (`RuntimeDirectory=`) | `glucose-guardian:glucose-guardian` | 0750 | read and write | read and traverse; `ReadOnlyPaths=` |
| `/run/glucose-guardian/display.json` | `glucose-guardian:glucose-guardian` | 0640 | writes (atomic rename) | read only |
| `/dev/fb0` | `root:video` | 0660 | none | read and write |

Runtime unit `glucose-guardian.service` (D1):

| Directive | Reason |
|---|---|
| `User=glucose-guardian`, `Group=glucose-guardian`, `UMask=0027` | Own account; nothing it creates is world-readable |
| `StateDirectory=glucose-guardian`, `StateDirectoryMode=0700` | Login state, readable only by the runtime |
| `RuntimeDirectory=glucose-guardian`, `RuntimeDirectoryMode=0750`, `RuntimeDirectoryPreserve=no` | Snapshot directory readable by the group; removed on stop, which the display shows as "NO DATA" |
| `LoadCredentialEncrypted=` for both credentials | Only this unit receives the credentials |
| `Type=notify`, `WatchdogSec=`, `Restart=always`, `RestartSec=` | Supervision (audit finding M6): the tick loop pings the watchdog |
| `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes` | Least privilege; the runtime keeps network access for Dexcom |

New unit `glucose-guardian-display.service`:

| Directive | Reason |
|---|---|
| `User=glucose-guardian-display`, `Group=glucose-guardian-display`, `SupplementaryGroups=glucose-guardian video` | Own account; snapshot, configuration and framebuffer access only |
| `ReadOnlyPaths=/run/glucose-guardian` | Snapshot access is read only, even if a mode were wrong |
| `InaccessiblePaths=/var/lib/glucose-guardian /etc/glucose-guardian/credentials` | No login-state or credential access, even if a mode were wrong |
| No `LoadCredential*` | No credentials |
| `PrivateNetwork=yes`, `IPAddressDeny=any`, `RestrictAddressFamilies=AF_UNIX` | No network; Unix sockets only, for the systemd notifications |
| `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, `NoNewPrivileges=yes` | Least privilege |
| `DevicePolicy=closed`, `DeviceAllow=` for `/dev/fb0` and `/dev/tty1` | Only the framebuffer and its console |
| `TTYPath=/dev/tty1`, `StandardInput=tty`, `Conflicts=getty@tty1.service` | The display owns the console and switches it to graphics mode, so no login prompt or kernel text draws over the dashboard (verified in G1) |
| `Type=notify`, `WatchdogSec=` | Ready after the first frame. The loop pings every refresh, so a hang is killed and restarted |
| `Restart=always`, `RestartSec=`, `StartLimitIntervalSec=0` | Restart after any exit and never give up: a display that stops retrying leaves a blank or frozen screen |
| `ExecStopPost=` running the display with `--blank` | After a stop, crash or watchdog kill, try to overwrite the last frame with "Display restarting" |
| `Nice=` | Rendering never competes with the runtime's tick |
| `After=glucose-guardian.service`, no `Requires=`; `WantedBy=multi-user.target` | Starts at boot without a desktop, and runs, saying so, when the runtime does not |

**Limits of `ExecStopPost=` and the watchdog.** Both need a working kernel, systemd and
output path. They cannot guarantee that a frozen image disappears:

- when the kernel hangs;
- when the GPU firmware, the KMS driver or the framebuffer emulation stops showing new
  content or blocks writes;
- when `ExecStopPost=` itself fails or times out, for example because the framebuffer
  is gone;
- when the HDMI link or the panel's own controller freezes;
- when the Pi loses power while the panel stays powered, depending on what the panel
  then shows (H7).

In these cases the last frame, including a glucose value, can stay on the panel
indefinitely. Only the on-screen clock shows it, and only to someone who notices.
After M6, the Uno's audible link-lost alarm covers a stalled runtime or a dead Pi, but
not a display-only freeze while the runtime works.

`WatchdogSec`, `RestartSec`, `refresh_s` and `snapshot_max_age_s` are provisional
(D7) until measured on the Pi.

### 3.7 Configuration

A required `[display]` table in the runtime configuration. The display process reads
the same file, so the snapshot path has a single source.

| Key | Use | Provisional value (D6, D7) |
|---|---|---|
| `kind` | `"log"` (today's behavior) or `"snapshot"` | `"snapshot"` on the Pi |
| `snapshot_file` | absolute path under `/run`; required with `kind = "snapshot"` | `/run/glucose-guardian/display.json` |
| `snapshot_max_age_s` | snapshot age at which the screen shows "NO DATA"; more than `tick_s` | 5 |
| `refresh_s` | display loop period; less than `snapshot_max_age_s` | 1 |
| `last_known_cutoff_s` | age after which a last known value is hidden; more than `max_age_s`, so an outdated reading is shown as last known before it disappears; read only by the display | 1800 |
| `framebuffer` | framebuffer device | `/dev/fb0`, after G1 |
| `font_file` | TrueType font (D4) | DejaVu Sans Bold |

Because the table is required, an existing configuration, including the Pi's
`config/local.dexcom.toml`, stops with a configuration error naming it until the table
is added. The examples and `make dexcom-config` will include it.

### 3.8 Login state migration (D9)

The connectivity check created the login state on the Pi as the Pi user, in the file
named by `config/local.dexcom.toml`. It moves once to the service's state directory:

| | Now | After installation |
|---|---|---|
| File | `~/glucose-guardian-state/dexcom-share-state.json` | `/var/lib/glucose-guardian/dexcom-share-state.json` |
| Owner, mode | Pi user, 0600; directory 0700 | `glucose-guardian`, 0600; directory 0700 |
| Named by | `config/local.dexcom.toml` | the installed configuration |
| Used by | `make dexcom-check` and `make dexcom-status`, run as the Pi user | the service, and those targets run as `glucose-guardian` |

The file holds the cooldown (on `CLOCK_BOOTTIME`, with the boot id), the rate-limit
backoff, the login latch and `login_pending`. Rules:

- **Never reset (F8).** No step deletes, recreates, edits or truncates the file, and
  no step runs `unblock`. A failure is never resolved by starting with a fresh file.
- **Exact copy.** The contents are copied byte for byte and the mode stays 0600. Only
  the owner changes, to the service user that has to read and write it; a file the
  service cannot read blocks login (`StateUnreadable`), which fails closed.
- **One live file.** After the move, no configuration names the old path, and the
  check and the service resolve the same `state_file` from the installed
  configuration.
- **One writer at a time (F7).** The check never runs while the service is active, and
  the service is not started while a check runs. The cross-process lock in
  section 12.2 enforces this for every process. A Makefile guard alone would not
  cover a direct Python invocation.
- **No Dexcom contact.** The migration sends nothing.

Procedure, owner-run during the approved installation; PR B documents the exact
commands in `docs/DISPLAY.md` and `DEXCOM_SHARE.md`:

1. **Preconditions:** the service is installed but neither enabled nor started, and no
   check is running. Save the output of `make dexcom-status` for the current
   configuration; it contains no secrets.
2. **Refusals:**
   - stop if the target file already exists: never overwrite;
   - stop and investigate if the source is missing or unreadable: never create a
     target in its place.
3. **Copy:** copy the source into the state directory under a temporary name, with
   owner `glucose-guardian`, group `glucose-guardian` and mode 0600 set at creation,
   and flush it to disk. Then rename it to the final name in the same directory,
   refusing to overwrite.
4. **Verify:**
   - the target is byte-identical to the source;
   - the owner, group and mode are as in the table, and the directory is 0700;
   - `share_state --state-file <target> status`, run as `glucose-guardian`, prints the
     same `blocked`, last login attempt and rate-limit backoff as step 1.
5. **Retire the old path:**
   - rename the source in place to `dexcom-share-state.json.migrated-<UTC time>`,
     kept as evidence until the owner removes it;
   - rename `config/local.dexcom.toml` likewise, so nothing can name the old path.
6. **Switch the commands:** from now on, `make dexcom-status` and `make dexcom-check`
   use the installed configuration and run as `glucose-guardian`. `dexcom-check`
   refuses while `glucose-guardian.service` is active, and any target refuses a
   configuration whose `state_file` differs from the service's (PR B, step 10).
7. **Start:** only then enable and start the service.
   - Within the same boot the cooldown continues exactly, because the boot id
     matches.
   - After a reboot, the existing rule applies: the first login waits 300 s from
     service start.

If any step fails, do not start the service. Until step 5 the source is untouched, so
the state is never lost.

## 4. Failure handling

| Failure | Detected by | Screen | Recovery | Alarm runtime |
|---|---|---|---|---|
| Display process crash | systemd | "Display restarting" (`ExecStopPost`, best effort), then the dashboard | `Restart=always` | Unaffected |
| Display loop hang | Missing watchdog ping | As above, after `WatchdogSec` | Watchdog kill and restart | Unaffected |
| Rendering error | Exception in the loop; no ping after a failed render | Last good frame until the watchdog fires | Restart | Unaffected |
| Runtime crash or stop | Snapshot removed, or older than `snapshot_max_age_s` | "NO DATA: No recent display data" | Runtime unit restart (D1) | The Uno's link-lost demand (audible from M6) |
| Runtime loop stalled, process alive | Snapshot ages | As above | Runtime watchdog (D1) | As above |
| Snapshot corrupt or of another schema | Strict reader | "NO DATA: display data invalid" | Next valid snapshot | Unaffected |
| Dexcom outage, rate limit, deferred or blocked login | `source_status` and the `acquisition` fault | Connection line; the value turns "LAST KNOWN", then "NO CURRENT DATA" after the cutoff | Existing M4 behavior; a blocked login needs the operator | Existing faults |
| Outdated reading | Engine (`missing_data`) and the recomputed age | "LAST KNOWN" in grey with its age; "NO CURRENT DATA" after `last_known_cutoff_s` | Next fresh reading | Existing |
| Invalid reading | Engine (`invalid_reading`) | Previous valid value as "LAST KNOWN", or "NO CURRENT DATA", with the reason line | Next valid reading | Existing |
| Clock not synchronized | Engine (`clock_unsynchronized`) | Reason line; no current value | NTP | Existing |
| Framebuffer missing or HDMI unplugged | Open or write error | none | Restart loop every `RestartSec` | Unaffected |
| Kernel, GPU, KMS or framebuffer driver, HDMI or panel freeze | Nothing automatic | The last frame can stay, value included; the frozen clock shows it | Power cycle | Runs on if the kernel lives |
| Whole Pi hangs or loses power | Nothing on the Pi | Frozen or dark panel, depending on the panel (H7) | Power cycle | Uno link-lost demand (M6); backup power (M7) |

Residual risk: no software in M5 can guarantee that a frozen image disappears
(section 3.6). Until the Uno's audible link-lost alarm exists (M6), the on-screen
clock is the only cue, and it covers no display-only freeze afterwards either.

## 5. Testing

All CI tests are offline, use synthetic data and need no display hardware.

- **Screen model (pure):**
  - a table over the four states and their precedence;
  - transitions: live, last known, no current data, and back to live; runtime down and
    up; `auth_blocked` to `ok`;
  - boundaries: age exactly `max_age_s` and just over, exactly `last_known_cutoff_s`
    and just over, snapshot age exactly `snapshot_max_age_s` and just over;
  - failures: missing, truncated, oversized, wrong-schema and ill-typed snapshots, a
    snapshot written in the future, negative ages;
  - recovery after each failure.
- **Invariants:** across every scenario and state:
  - a level-colored number and a trend arrow appear only in state 2;
  - no number appears in states 1 and 4;
  - "SIMULATED DATA" appears exactly when the source is simulated.
- **Cutoff isolation:** alarm engine results are identical for any
  `last_known_cutoff_s`; the runtime and `alarms.py` never read it.
- **Snapshot:** writer round trip, atomic replacement, mode 0640 and size bound; the
  reader rejects each malformed field.
- **Trend move (F1):** alarm engine results are identical with and without trends; a
  stale or future-dated newest entry never pairs its trend with the previous value.
- **Renderer:** every state renders at 800×480 without error; text stays inside its
  box for the widest strings; sampled pixels have the expected colors; arrows point
  the right way.
- **Output:** the framebuffer step against a temporary file and a fake sysfs
  directory, for 16- and 32-bit pixel formats and padded line lengths; the test
  pattern.
- **systemd notification:** against a temporary datagram socket.
- **End to end:** the real service runs each simulated scenario with the snapshot in a
  temporary directory, and the display loop writes to an image sink.
  - Killing the runtime shows "NO DATA" within `snapshot_max_age_s + refresh_s`, and
    restarting it recovers.
  - Killing the display leaves the runtime's tick and error counts unchanged.
- **Static:**
  - display modules import no network module, `share_client`, `credentials` or
    `share_state`;
  - the display unit sets the account, group, `ReadOnlyPaths=`, `InaccessiblePaths=`,
    `PrivateNetwork=yes`, `IPAddressDeny=any`, `RestrictAddressFamilies=AF_UNIX` and
    `Restart=always`, and no `LoadCredential*`;
  - the runtime unit sets `RuntimeDirectoryMode=0750`, `StateDirectoryMode=0700` and
    `UMask=0027`;
  - `systemd-analyze verify` where available.
- **Makefile (fakes):** `dexcom-check` refuses while the service is active, and refuses
  a configuration whose `state_file` differs from the service's; no target deletes,
  recreates or unblocks the state file.
- **Preview:** `make display-preview` writes a PNG per state and scenario for review
  in the pull request, with synthetic values only.
- **Regression:** `make check` passes; the M4 read-only, login-protection and alarm
  tests are unchanged; the production firmware hash stays `bde6b740…`.

## 6. Hardware-dependent validation (waits for the screen)

Owner-run on the Pi, after explicit approval to deploy. Photos and reports must not
show real glucose values; use the simulated configuration for photos. G1
(section 3.2) comes first and decides the output backend.

1. **H1, panel:** the exact Waveshare model and revision; the modes it reports over
   HDMI; whether 800×480 is used without configuration. If not, follow Waveshare's
   documentation for that revision and record the boot configuration change.
2. **H2, output backend:** G1 passed, or an approved fallback passed the same
   procedure.
3. **H3, console:** no login prompt, cursor or kernel message ever draws over the
   dashboard, also across reboots.
4. **H4, blanking:** the screen never blanks during an overnight soak of at least
   8 hours.
5. **H5, boot:** the dashboard appears after boot without keyboard or mouse; record
   the time to the first frame.
6. **H6, recovery:** `kill -9` and `SIGSTOP` of the display process, and a stop and
   start of the runtime; record the recovery times and whether "Display restarting"
   appeared.
7. **H7, cabling and power:** HDMI unplug and replug, a panel power cycle, and what the
   panel shows when the Pi stops sending a signal or loses power: the last image, a
   no-signal screen or black.
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
13. **H13, permissions:** as the display user, reading the state file or the
    credential directory fails, opening a network connection fails, and writing the
    snapshot fails; reading the snapshot succeeds.
14. **H14, state migration:** the section 3.8 verification passed, recorded without
    secrets.

The soak runs with the simulated transport (D8) and Dexcom Share, with outputs
disabled. It covers H4 and H10 and the soak still open from the M4 plan. A soak with
the real Uno follows later as a separate, approved step.

Open hardware questions:

- Desktop or headless image (D3): headless as of 2026-11-13 (section 12.1). A
  compositor started later would own the display.
- The Waveshare model and revision, its reported modes, and any boot configuration it
  needs.
- Whether the running kernel provides an 800×480 framebuffer for the HDMI output, and
  in which pixel format. `/dev/fb0` was absent on 2026-11-13 with no display
  connected; whether it appears with the panel attached is unknown.
- Whether the display user can switch the console to graphics mode through `TTYPath=`.
- What the panel shows when the HDMI signal stops or the Pi loses power.
- The panel's power source, and whether it causes under-voltage.
- Whether the panel has a touch controller, and how its backlight is adjusted.
- Whether the picture appears when the panel is powered after the Pi has booted.

## 7. Acceptance criteria

1. The four screen states and every connection status render as specified, and the
   tests in section 5 pass in CI on Python 3.11 and 3.13.
2. No test, scenario or failure shows a level-colored number or a trend arrow unless
   the engine marked the reading current and the recomputed age is within
   `max_age_s`.
3. A last known value older than `last_known_cutoff_s` is never shown as a number;
   the screen shows "NO CURRENT DATA". Alarm engine results do not depend on the
   cutoff.
4. A runtime stop shows "NO DATA" within `snapshot_max_age_s + refresh_s`.
5. A display crash or hang never changes the runtime's tick, fault or error counts,
   and the display comes back by itself. A frozen image after a kernel, driver or
   hardware failure is a documented residual risk, not a passed criterion.
6. The display process has read-only snapshot access and no credentials, login-state
   access or network: enforced by the units, tested statically, and confirmed on the
   Pi (H13).
7. The output backend is `/dev/fb0` only if G1 passed; otherwise an owner-approved
   fallback that passed the same procedure.
8. The login state was migrated by section 3.8: byte-identical, mode 0600, owned by
   `glucose-guardian`, identical status lines, one live state file. Nothing reset or
   unblocked it, and the check refuses while the service runs.
9. Alarm policy, thresholds, acknowledgement, GG1, firmware and the M4 protections are
   unchanged, and `make check` passes.
10. On the Pi (section 6): boot to the dashboard without interaction, no blanking or
    console text overnight, recovery in H6 and H7, zero runtime overruns, and
    readability confirmed by the owner.

## 8. Implementation plan

Two pull requests (D10). Each step is a small increment with its own tests.

PR A, offline (CI only, nothing on the Pi), implemented on 2026-11-13 (section 11):

1. **Trend with its reading:** `Reading.trend`, set by `DexcomShareProvider` and the
   scenarios; `latest_trend` removed. Done when the F1 tests pass and the alarm tests
   are unchanged.
2. **Frame extension:** the reading time, trend, source and outputs flag in `Frame`
   and `build_frame`.
3. **Source status:** the service passes `DexcomShareProvider.status`, or "simulated",
   to the display layer.
4. **Snapshot:** schema 1, `SnapshotDisplay` (mode 0640), `FanOutDisplay`, the strict
   reader and the `[display]` table including `last_known_cutoff_s`, with the
   examples updated.
5. **Screen model:** the classification of section 3.4, including the cutoff, with
   its test tables.
6. **Renderer:** Pillow layout, fit-to-box text, arrows and colors;
   `make display-preview`.
7. **Display process:** `python -m glucose_guardian.screen` with the refresh loop,
   redraw on change, image sink, systemd notification, `--blank` and signal handling;
   end-to-end tests with the simulated service.

PR B, Pi integration, after the prerequisites in section 12:

8. **Framebuffer output and test pattern,** with console graphics mode, tested against
   fakes.
9. **Gate G1:** the smoke test on the Pi (section 3.2), owner-run after approval. If it
   fails, evaluate the fallbacks; the owner approves the backend before step 10.
10. **systemd units and accounts:** the units, accounts and permissions of
    section 3.6, with static tests. Makefile: `dexcom-status` and `dexcom-check` use
    the installed configuration as `glucose-guardian`; `dexcom-check` refuses while
    the service is active, and any target refuses a mismatched `state_file`.
11. **Installation and migration procedure:** a new `docs/DISPLAY.md` with the exact
    commands, including section 3.8, and `DEXCOM_SHARE.md` updated. Installation
    stays a manual, approved step.
12. **Documentation:** ARCHITECTURE, the SAFETY failure table (including the
    frozen-image residual risk), PROJECT_STATUS and DEVELOPMENT_LOG.
13. **Pi validation (section 6),** after explicit approval to deploy. First the
    simulated-transport soak, then, separately approved, the real Uno. Recorded in
    `docs/DISPLAY.md`.

## 9. Owner decisions (2026-11-13)

| # | Decision | Owner decision | Effect on this plan |
|---|---|---|---|
| D1 | Runtime supervision in M5 | Approved | Runtime unit in section 3.6; persisting the retained alarm level stays before M6 outputs |
| D2 | Rendering stack | Pillow approved; the framebuffer backend is conditional | G1 decides the output backend; fallbacks in section 3.2 |
| D3 | Pi image | Debian 13 aarch64 confirmed; desktop or headless pending. Update 2026-11-13: headless (`multi-user.target`, no display manager active; section 12.1) | Recorded again in G1; a desktop must stay disabled, or the fallback must take DRM master |
| D4 | Font | Approved: DejaVu Sans Bold (`fonts-dejavu-core`) | Section 3.5 |
| D5 | Colors, labels, "<40" and ">400" | Approved provisionally; the LOW and HIGH encoding is not yet live-verified | Revisit when a live LOW or HIGH confirms the encoding |
| D6 | Last known value | Modified: a configurable display cutoff, provisionally 30 minutes; after it, the number is hidden and the screen shows "NO CURRENT DATA". No alarm engine change | `last_known_cutoff_s` (sections 3.4 and 3.7) |
| D7 | Timing | Provisional values approved: `snapshot_max_age_s` 5 s, `refresh_s` 1 s, `WatchdogSec` 10 s, `RestartSec` 2 s | Confirmed against measurements on the Pi |
| D8 | Transport | Simulated transport first; the real Uno later under controlled conditions | Simulated soak first; a later, separately approved soak with the Uno running the production firmware, outputs disabled, attended by the owner |
| D9 | Install location, users, state file | Approved conditionally, with a safe state migration | Sections 3.6 and 3.8 |
| D10 | Number of pull requests | Two approved | Section 8 |

Still open:

- the G1 result and, if needed, the fallback choice; `/dev/fb0` was absent on
  2026-11-13 with no display connected (section 12.1);
- the cross-process lock of section 12.2;
- the hardware questions in section 6.

## 10. Out of scope

Siren, buzzer, bed shaker, GPIO outputs, firmware changes, physical acknowledgement,
new escalation logic, touch input, trend-based alerts, persisting readings, persisting
the retained alarm level (before M6 outputs), backup power (M7), any Omnipod
interaction and any write to Dexcom.

## 11. PR A implementation record (2026-11-13)

Steps 1 to 7 of section 8, offline. Nothing was deployed, flashed or sent to Dexcom;
no real configuration or state file was touched.

| Module | Change |
|---|---|
| `providers.py` | `Reading.trend` (`Trend` or `None`), a required field |
| `simulation.py` | Every segment has a trend; scenario readings carry it |
| `share_client.py` | `DexcomShareProvider` returns the entry's trend inside the `Reading`; `latest_trend` removed |
| `display.py` | `Source`; `Frame` gains `reading_time`, `trend`, `source`, `source_status`, `outputs_enabled`; `build_frame` refuses a Dexcom source without a status and a simulated source with one; `FanOutDisplay` |
| `runtime.py` | New required arguments `source`, `source_status` and `outputs_enabled`. The frame is built inside the display guard, so a frame error counts as a display error |
| `config.py` | Required `[display]` table; `Config.display` and `Config.outputs_enabled` |
| `snapshot.py` | New: schema 1, atomic writer, strict reader, `SnapshotDisplay` |
| `service.py` | `build_display`; `Acquisition` dataclass with the source and its status callable |
| `screen_model.py` | New: the pure classifier of section 3.4 |
| `screen_render.py` | New: the Pillow renderer |
| `screen.py` | New: the display process |
| `notify.py` | New: systemd notifications over a Unix datagram socket |
| `preview.py` | New: synthetic previews for `make display-preview` |
| `bench/checks.py` | Passes the new runtime arguments |

Implementation details:

- **Trend (F1):** the trend travels inside the `Reading` it arrived with. A stale or
  future-dated newer entry can no longer pair its trend with the value shown, because
  the frame takes the value, time and trend from the same last valid reading. `Trend`
  stays in `dexcom_share.py`; `providers.py` imports it.
- **Snapshot writer:**
  - removes a leftover partial file and creates a new one with `O_EXCL`, mode 0640
    (also forced with `fchmod`);
  - writes, then renames over the target; the rename replaces a symlink rather than
    following it;
  - does not `fsync`: the file lives on tmpfs, and the rename alone is atomic for
    readers;
  - `encode` parses its own output before writing, so the writer can never publish a
    snapshot that the reader would reject.
- **Snapshot reader:**
  - opens with `O_NOFOLLOW` and `O_NONBLOCK`, accepts regular files only, and reads at
    most 4096 bytes;
  - rejects duplicate keys, `NaN` and `Infinity`;
  - requires the exact field set, strict types (a boolean is never a number), known
    enum values and a UTC ISO 8601 time;
  - checks consistency: the value, time and age are present together; a trend or a
    current level needs a reading; the status is null exactly for a simulated source.
- **Screen model:** as in section 3.4, with the additions listed under Deviations.
  Ages use minute granularity, so the screen redraws at most once a minute unless
  something changes.
- **Renderer:**
  - fixed, disjoint layout boxes;
  - text shrinks by binary search down to 12 px, then is shortened with an ellipsis;
  - the alerts line drops trailing items with "+N more";
  - arrows are polygons rotated per trend;
  - the font is loaded once from its file.
- **Display process:** `python -m glucose_guardian.screen --config <file> --png <file>
  [--blank]`.
  - It sends `READY=1` after the first frame, `WATCHDOG=1` after each completed loop
    and `STOPPING=1` on SIGTERM or SIGINT.
  - A render or output error ends the process with status 1 before the next watchdog
    ping.
  - A missing, invalid or expired snapshot is a screen state, never an error.
  - It logs state changes, never values.
  - `--blank` writes the "Display restarting" frame once.
- **Notifications:** `notify.py` uses only `AF_UNIX` datagram sockets (path or
  abstract `NOTIFY_SOCKET`). The network-import test lists it with `socket`, and a
  further test confirms that it uses only Unix sockets.
- **Previews:** `make display-preview` classifies 30 synthetic snapshots through the
  real classifier and renders them with the real renderer. It also renders the
  "Display restarting" frame, writing 31 PNGs to `build/display-preview/` (Git-ignored).
- **Configuration:**
  - `config/example.toml` writes its development snapshot to
    `/tmp/glucose-guardian-display.json`;
  - `config/example-dexcom-share.toml` uses `/run/glucose-guardian/display.json`;
  - both name DejaVu Sans Bold and `/dev/fb0`. The `framebuffer` key is validated but
    unused until PR B.
- **Dependency:** `pillow==13.0.0` (wheels for Python 3.11 to 3.13; aarch64 wheel for
  Python 3.13 confirmed). CI installs `fonts-dejavu-core`.

Deviations from the approved design:

1. **Stricter snapshot acceptance:** a snapshot whose source or outputs flag differs
   from the display's own configuration is shown as "NO DATA: display data invalid".
2. **LAST KNOWN layout:** after reviewing the previews, the age moved into the tag
   ("LAST KNOWN · 12 min ago", bright text), and the unit and reading time into the
   right column. The retained alarm level got the full width of its row, where it had
   been shrunk to small text.
3. **Connection colors:** `ok` and `starting` muted, the other error statuses amber
   (warning), `auth_blocked` red. The plan specified only the red.
4. **Simulated source:** the connection line reads "Source: simulation".
5. **Device limits:** values at or below 39 show as "<40" and at or above 401 as
   ">400". This matters only for simulated values, because the Share parser accepts
   only 39 to 401.
6. **"Display restarting" frame:** no clock and no markers, so a stuck restarting
   screen cannot show a plausible time.

Review correction (PR #10, 2026-11-13):

- A missing or expired snapshot showed "Glucose Guardian is not running". The display
  cannot know that: the runtime may have stopped, stalled, crashed or lost its
  snapshot directory, or may still be starting. Both cases now show "NO DATA: No
  recent display data".
- The NO DATA state, the suppressed value and trend, and the thresholds
  (`snapshot_max_age_s`, written-in-the-future) are unchanged.
- The preview case was renamed `no-data-no-recent-data`.
- The end-to-end test helper now reads process output on a thread with a real
  deadline. Before, a missing log line blocked the test instead of failing it.

Verification:

- **Python 3.11.18 and 3.13.18:** ruff clean, mypy strict clean (36 files), pytest
  1032 passed after the review correction (1025 before it, 761 before PR A).
- **Native firmware tests:** 27 of 27; firmware unchanged.
- **Mutation check:** 19 of 19 mutations of the new safety rules fail the tests. A first
  run found one survivor: drawing any value in the live layout hid the LAST KNOWN
  label. A test now requires the label.
- **Development machine:** about 3 ms per rendered frame and 76 MiB peak memory for
  the preview run. The Pi figures are measured in H10.

## 12. PR B prerequisites

### 12.1 Raspberry Pi inspection (owner-reported, 2026-11-13)

| Check | Result |
|---|---|
| `systemctl get-default` | `multi-user.target` |
| `systemctl is-active display-manager` | `inactive` |
| `/dev/dri/card0`, `/dev/dri/renderD128` | present |
| `/dev/fb0` | not present |
| Waveshare display | still packaged, not connected or identified |

- **The Pi boots headless, and no display manager runs.** This answers the
  desktop-or-headless part of D3 for the current image.
- **The DRM devices exist, but no legacy framebuffer does.** It must not be assumed
  that `/dev/fb0` appears when HDMI is connected. G1 first establishes whether the
  kernel provides framebuffer emulation for the panel. If it does not, the KMS/DRM
  fallbacks of section 3.2 apply, which the present DRM devices make viable.
- **The PR A architecture stays unchanged:** only the output step depends on this.

### 12.2 Cross-process lock for the Dexcom login state (mandatory)

Required before the service and the connectivity check share a state file, and before
continuous live monitoring starts. It changes M4 code (`share_state.py`) and needs its
own review.

- **Problem (F7):** `AuthStateStore` loads the state once at startup and replaces the
  file on each change. Two processes can therefore overwrite each other's cooldown,
  rate-limit backoff or block. A Makefile guard does not stop a direct
  `python -m glucose_guardian.share_check`.
- **Lock:** an exclusive, non-blocking `flock` (`LOCK_EX | LOCK_NB`) on a dedicated
  lock file next to the state file (`<state_file>.lock`, mode 0600, opened with
  `O_CLOEXEC` so no child process inherits it).
- **Who takes it:**
  - every process that builds an `AuthStateStore` takes the same lock: the runtime,
    `share_check` and `share_state unblock`;
  - `share_state status` and `path` only read, and need no lock; the atomic rename
    gives them a consistent file.
- **Lifetime:** the lock is taken before the state file is read and held until the
  process exits. It therefore covers the whole time the state is held in memory, not
  only single writes.
- **Fail closed:**
  - a process that cannot take the lock refuses before it reads the state or sends
    any request;
  - `share_check` and `unblock` exit with a message that another process holds the
    login state;
  - the runtime exits with status 2, and systemd retries after `RestartSec`;
  - failing to open the lock file blocks login exactly like an unreadable state file.
- **Crashes and stale locks:** a `flock` lock belongs to the open file and is released
  by the kernel when the holder exits for any reason, including SIGKILL, a crash or
  the OOM killer; a power loss ends the boot.
  - There are no PID files, timeouts or manual cleanup.
  - The lock file is never deleted: deleting it while held would let another process
    lock a new file.
- **Kept:** every existing fail-closed rule (unreadable state, `login_pending`, the
  login latch). The Makefile refusal while the service runs stays as an early,
  friendly check.
- **Tests (PR B):**
  - a second process refuses while the first holds the lock;
  - a SIGKILLed holder releases it;
  - `unblock` refuses while the service runs;
  - no code path removes the lock file.

### 12.3 Configuration on the Pi

`[display]` is now required. The Pi's existing `config/local.dexcom.toml` lacks it, so
once this code reaches the Pi, the following stop with a configuration error that
names `[display]` until the `[display]` block from
`config/example-dexcom-share.toml` is added:

- `make dexcom-status`;
- `make dexcom-check`;
- the Dexcom state line of `make status`.

Nothing was changed on the Pi.

### 12.4 Other PR B items

- **Packages:** install `fonts-dejavu-core` on the Pi. Without the font the display
  process refuses to start (status 2).
- **G1, then the output backend:** step 9, then the approved backend.
- **Runtime unit:** the runtime sends `WATCHDOG=1` from its tick loop through
  `notify.py`.
- **Remaining PR B work:** the units, accounts and permissions of section 3.6, the
  Makefile changes of step 10, the state migration of section 3.8 and the Pi validation
  of section 6.
