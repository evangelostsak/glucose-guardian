# Safety and limitations

Glucose Guardian is an experimental supplementary display and alarm system, not a
certified medical device or a basis for treatment decisions. It cannot guarantee
waking a user or delivering an alarm. Keep official Dexcom and Omnipod alerts
independent and unchanged. This prototype is not ready for unattended use.

Dexcom Share depends on network access and remote services. A plausible glucose
number may be stale. Validate UTC timestamps before classifying glucose and show
data age and explicit fault status in the GUI planned for M5
([plan](plans/2026-11-13-m5-hdmi-display.md)). A network or hardware failure
must have a distinct indication rather than presenting stale data as normal.

Acknowledgement is temporary and never permanently disables an alert. A short press
silences only the glucose channel and a 2-second hold silences only system faults,
each for its own configured duration capped at 900 s. Repeated presses never extend a
silence, and any escalation, further `rearm_mg_dl` worsening, or new or returning fault
sounds immediately (see [ARCHITECTURE.md](ARCHITECTURE.md)). When the Pi is
unavailable, the Uno demands its own link-lost alarm; a hold silences it for at most
60 s, presses during that window do not extend it, and it re-arms automatically while
the link stays lost. The firmware has no button pin and drives no output yet.

## Failure modes

The runtime connects the alarm engine to bounded acquisition, a clock check, a
display frame and either a transport double or the serial transport (M3). The Dexcom
Share adapter (M4) has been tested against a synthetic simulator, and one read-only
connectivity check against Dexcom passed on 2026-11-13; the service has not run
continuously against Dexcom. There is no GUI and no physical output. "Simulated" below means verified with synthetic scenarios; "PC-tested" means verified
with pseudo-terminals, a Python Uno emulator, and native firmware unit tests.

| Condition | Detected by | Indication | Status |
|---|---|---|---|
| Urgent low, low or high glucose | Fresh valid reading crossing a threshold | Glucose alarm | Simulated |
| Reading below 40 or above 400 mg/dL | Normal threshold classification | Urgent low or high glucose alarm (with the configured thresholds) | Engine implemented |
| No reading, or reading older than `max_age_s` | Validation | `missing_data` fault; unresolved glucose alarm kept and shown as last known with its age; normal becomes unknown | Simulated |
| Non-numeric, non-finite or non-positive value; naive timestamp; timestamp more than 60 s in the future | Validation | `invalid_reading` fault | Simulated |
| Fetch error or exception | Acquisition worker | `acquisition` fault; latest reading used until stale | Simulated; Dexcom Share adapter tested against the simulator |
| Dexcom Share unreachable: DNS, refused or reset connection, TLS failure, request deadline | `ShareTransportError` | `acquisition` fault, then `missing_data` after `max_age_s`; recovers on the next successful fetch | Simulator and TLS tests; not run against Dexcom |
| Dexcom Share slow or stalled: unanswered connect, slow-drip TLS handshake, headers or body, body shorter than its `Content-Length` | Per-request deadline enforced on the socket; declared-length check | `ShareTransportError` at `request_timeout_s`; a cut-short body is never accepted. DNS resolution cannot be interrupted, so a slow resolver delays the request; it then fails at once | Loopback slow-drip, backlog and resolver tests; not run against Dexcom |
| Dexcom Share HTTP error, HTML error page, redirect, oversized or malformed body, missing or malformed required entry field, unknown trend, value outside 39 to 401, conflicting duplicates | `ShareServerError`, `ShareProtocolError`, `ShareTransportError` | `acquisition` fault; never treated as a reading; no login latch; an HTTP 200 with such a body does not clear a rate-limit backoff. Additional entry fields are ignored | Simulator tests; the normal response shape matched in the 2026-11-13 live check; error responses unverified |
| Request to any Share operation other than authenticate, login or read, to any other host, or with an unexpected query or body (programming error or misconfiguration) | `check_request` allowlist before sending | `ShareRequestRefused`, an `acquisition` fault; nothing is sent. Only POST is possible and redirects are refused | Unit, wire-level and mutation tests |
| Dexcom Share session expired | `SessionIdNotFound` or `SessionNotValid` | One login and read if the 300 s login interval allows; otherwise `acquisition` fault until it does | Simulator tests |
| Rejected credentials or account lockout (`AccountPasswordInvalid`, `SSO_AuthenticateMaxAttemptsExceeded`, rejected login, nil identifier) | Login response | Automatic login blocked and persisted across restarts; status `auth_blocked`; `acquisition` and then `missing_data` faults; operator must fix and unblock | Simulator tests |
| Dexcom Share rate limit (HTTP 429) | Response status | Exponential backoff (120 s to 1800 s, `Retry-After` honored); `acquisition` fault while backing off | Simulator tests; values provisional |
| Service crash loop or restart | Persisted login state | At most one login attempt per 300 s within a boot; after a reboot the first login waits 300 s | Unit and simulator tests |
| Login outcome not recorded: crash, stop or power loss during a login, or the block or outcome write fails | `login_pending` still set in the state file at load | Automatic login blocked (`LoginOutcomeUnknown`) until an operator checks and unblocks, so a credential rejection that was never written cannot be retried after a restart; status `auth_blocked` with `acquisition` and then `missing_data` faults | Unit and simulator tests with injected write failures |
| HTTP 429 backoff from a read cannot be written | Write error logged | The running process keeps the backoff; after a restart the persisted 300 s login interval bounds the first request instead | Unit tests with injected write failures |
| Dexcom Share returns the same, an older or no reading (phone not uploading) | Provider keeps the newest reading | Reading ages into `missing_data` after `max_age_s` | Simulator tests |
| Dexcom LOW or HIGH | Values 39 and 401 (assumed encoding) | Urgent low or high through the unchanged thresholds; another encoding becomes an `acquisition` fault | Assumption; the 2026-11-13 live check had no reading outside 40 to 400 |
| Fetch blocked or slower than `fetch_timeout_s` | Acquisition worker hard timeout | `acquisition` fault; supervisor keeps ticking; one thread at most | Simulated |
| Pi clock not synchronized (no RTC on the Pi 3B), or the check fails or times out | Clock-check worker | `clock_unsynchronized` fault; readings not evaluated | Simulated; systemd check unit-tested, not yet run on the Pi |
| Uno silent, unplugged, or answering with corrupted, outdated, duplicate or wrong-version frames | Round-trip link age above `heartbeat_timeout_s` | `communication` fault; port reopened with backoff | PC-tested; on the USB bench (owner-reported 2026-11-12): Uno rejection of invalid frames, recovery after a heartbeat pause, unplug detection, reconnect and backoff passed |
| Pi serial write zero-length, partial or failing (output buffer full, device stalled) | Byte count checked on every write; writability checked before writing | Frame not counted as sent; output discarded; port reopened with backoff; `communication` fault if the link stays down | PC-tested |
| Uno resets while connected (watchdog, brown-out, power glitch, reset button) | New boot counter, or a watchdog or brown-out cause at connection | `controller_reset` fault latched until a long press | DTR and watchdog resets detected on the USB bench (owner-reported 2026-11-12: 5 of 5 repeated resets, both watchdog hang modes). With Optiboot 4.4 only `W` (watchdog-interrupt marker) or `U` is reported; brown-out, power glitch and reset button not exercised |
| Button held 30 s or more | Uno stuck detection | `button_stuck` fault; the stuck press acknowledges nothing | PC-tested; no button pin yet |
| Display or transport send failure | Runtime adapter guard | Counted and logged; alarm evaluation continues | Simulated |
| Alarm-engine defect | Exception out of the loop | Service exits with status 1; heartbeats stop | Simulated; systemd restart not installed (proposed for M5) |
| Pi process crash, hang or planned stop; alarm loop stalled | No new state frame on the Uno for more than 5 s (10 s grace after boot) | Uno link-lost demand, independent of the Pi | PC-tested; not observable on the bench (no output until M6) |
| Uno firmware hang | Two-stage hardware watchdog (1 s interrupt, then 1 s reset) resets the Uno after about 2 s | `controller_reset` on the Pi (new boot counter) | Passed on the USB bench (owner-reported 2026-11-12): hangs with interrupts enabled and disabled both reset, 2.243 s between replies |
| Total power loss | Not possible | None | Requires backup power (hardware review) |

Acknowledging a stale glucose alarm with a short press silences only the glucose
channel; the missing-data fault keeps sounding and the display prompts "Hold 2 s to
silence". A hold silences the faults for `fault_acknowledgement_s` and clears neither
the fault nor the retained glucose level.

A restart is fail-safe but lossy: acknowledgements are forgotten and the system
re-alarms, but a retained urgent low during stale data becomes unknown glucose with a
missing-data fault, and the display shows no value until fresh data arrives.

Unverified risks include driver failure, USB reset, power loss, loud sound,
heating, short circuits, enclosure and cable hazards, and backup battery failure.
Electrical design, user-selected alarm policy, acoustic patterns and backup power
need review and bench validation before operation. Software tests do not establish
medical suitability or electrical safety. Never commit credentials, tokens, logs
with patient readings, or real patient glucose data.
