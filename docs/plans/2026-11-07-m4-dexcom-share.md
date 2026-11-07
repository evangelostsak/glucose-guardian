# M4 Dexcom Share adapter: implementation plan

Status: proposed on 2026-11-07, not approved. No M4 code exists. Implementation starts
only after the watchdog changes on `feat/bench-harness` are reviewed and this plan,
including the owner decisions in section 8, is approved.

## 1. Goal and boundaries

- Read the latest CGM value from Dexcom Share and feed it to the existing alarm engine
  as a `Reading`, behind `provider = "dexcom_share"`. The example configuration keeps
  `provider = "simulated"`.
- Glucose Guardian stays a supplementary bedside alert and display device. It only
  reads from Share. It never writes to Dexcom, changes Share or follower settings, or
  touches the Omnipod 5 or its controller. The official Dexcom G7 and Omnipod 5 alerts
  stay independent and unchanged.
- The adapter must also avoid indirect effects on the official system, chiefly locking
  the patient's Dexcom account through repeated failed logins (section 4).
- No change to the alarm policy, fault set, protocol, firmware or outputs. No
  deployment.

## 2. How Share works (to verify before implementation)

Share is undocumented. The details below come from the open-source client `pydexcom`
(MIT) and the Nightscout Share bridge. They are re-verified against the current
`pydexcom` source at the start of implementation, and anything that differs is
recorded in this plan before code is written.

| Item | Behavior |
|---|---|
| Hosts | One per region: United States, outside the United States, Japan |
| Login | Two POSTs: `General/AuthenticatePublisherAccount` (user name, password, application id) returns an account id; `General/LoginPublisherAccountById` (account id, password, application id) returns a session id |
| Read | POST `Publisher/ReadPublisherLatestGlucoseValues` with `sessionId`, `minutes` and `maxCount` query parameters; returns a JSON list, newest first |
| Entry | `WT` and `ST` as `Date(<epoch ms>)`, `DT` with a local offset, `Value` in mg/dL, `Trend` as a name such as `Flat` or `SingleDown` |
| Errors | HTTP 500 with a JSON `Code`, such as `SessionNotValid`, `SessionIdNotFound`, `AccountPasswordInvalid`, `SSO_AuthenticateMaxAttemptsExceeded`, `InvalidArgument` |
| Account | The patient's own (publisher) Dexcom account, with Share enabled and at least one follower, according to the `pydexcom` documentation |
| Data path | Readings reach Share only while the G7 app on the phone uploads them |

Alternatives considered:

- Dexcom's official developer API (OAuth) serves delayed, retrospective data, so it
  cannot drive a real-time alert.
- Nightscout would add a service outside this project.

## 3. Components

| Module | Responsibility |
|---|---|
| `dexcom_share.py` | Pure protocol: request construction, response parsing, error classification; no I/O |
| `share_client.py` | HTTPS through stdlib `urllib`; the session state machine of section 4 |
| `credentials.py` | Reads `dexcom-username` and `dexcom-password` from `$CREDENTIALS_DIRECTORY` |
| `providers.py` | `DexcomShareProvider.read(now) -> Reading \| None` behind the existing `GlucoseProvider` |
| `share_simulator.py` | The simulated data source of section 6 |
| `config.py` | Strict `[dexcom]` table |

HTTP behavior:

- HTTPS only, with certificate verification against the system CA store.
- Connect and read timeouts below `fetch_timeout_s`.
- Redirects refused and response bodies capped (proposed: 64 KiB).
- No retries inside a call except the single re-login of section 4.

The existing `BoundedWorker` stays the outer bound: a hung call is reported as
`TimedOut` and costs one daemon thread.

Credentials come from systemd (`LoadCredentialEncrypted=`). A missing variable or file
stops the service at startup with a message naming it. The Uno's independent heartbeat
alarm then covers the missing service.

## 4. Authentication and session

| State | Event | Action |
|---|---|---|
| No session | fetch, login allowed | Log in (two requests), then read |
| No session | fetch, within the login interval | Fail the fetch without contacting Share |
| Session | read succeeds | Return the reading |
| Session | `SessionNotValid` or `SessionIdNotFound` | Drop the session, log in once if allowed, read once |
| Any | `AccountPasswordInvalid` or `SSO_AuthenticateMaxAttemptsExceeded` | Latch "login blocked": every fetch fails without contacting Share until the service restarts |
| Any | all-zero account or session id | Treat as a failed login |

- Login attempts are limited to one per `login_min_interval_s`, measured on the
  monotonic clock, whatever the fetch cadence. The latch and the limit exist because
  failed logins can lock the patient's account.
- Account and session ids are bearer secrets. They stay in memory and never appear in
  logs, exceptions, reports or files. The session id travels in the query string, as
  Share requires, so no URL is ever logged.
- Logs carry the error class, the HTTP status and the Share error code only: no user
  names, values or reading timestamps.

## 5. Readings and freshness

- Each read requests `maxCount=1` and a `minutes` window covering `max_age_s`, rounded
  up to whole minutes.
- An empty list returns `None`. The runtime keeps the last reading, and the engine
  raises `missing_data` once it is older than `max_age_s`.
- `WT` becomes the reading timestamp: UTC epoch milliseconds, made timezone-aware. `DT`
  (local display time) is ignored. The adapter never substitutes the arrival time.
- `Value` must be an integer; booleans and strings are rejected. It goes unchanged to
  the existing validation and thresholds.
- How Share encodes out-of-range readings (the G7 shows LOW below 40 and HIGH above
  400 mg/dL) is unknown. Until confirmed, nothing is special-cased (audit decision 2).
- `Trend` is checked against the known names and then dropped; display is M5.
- Freshness needs no new logic. The engine already validates timestamps first: age
  against synchronized UTC, `max_future_skew_s`, and no evaluation while the clock is
  unsynchronized. Repeated identical readings simply keep ageing.
- `provider = "dexcom_share"` requires the systemd clock check, not the scenario clock;
  M4 wires it into the real runtime.
- Malformed responses (bad JSON, missing fields, a bad `WT`) are acquisition failures,
  not invalid readings.

Network and service failures:

| Condition | Adapter | Engine result |
|---|---|---|
| DNS failure, refused or unreachable connection, TLS failure | raises | `acquisition` fault; the last reading ages into `missing_data` |
| Socket timeout | raises | `acquisition` fault |
| Hung call past `fetch_timeout_s` | worker `TimedOut` | `acquisition` fault |
| HTTP 5xx or 429, non-JSON body, oversized body, redirect | raises | `acquisition` fault |
| Session expired | one re-login and read | normal if they succeed |
| Login blocked (latched) | raises without network access | `acquisition` fault until restart |
| Phone not uploading | returns the same or no reading | `missing_data` after `max_age_s` |
| Reading in the future beyond skew | returns it | `invalid_reading` |
| Clock unsynchronized | returns readings | `clock_unsynchronized`, readings not evaluated |

Each later fetch is a fresh attempt at the configured interval. Recovery takes the
first successful read with no extra state.

## 6. Simulated data source

- `share_simulator.py` is a stdlib `http.server` bound to `127.0.0.1`. It implements
  the three endpoints with synthetic data, accepts only the synthetic test credentials,
  and generates every value.
- It is driven by the existing `SCENARIOS` segments, plus Share-specific faults:
  - session expiry, an invalid password and the lockout code;
  - HTTP 500 and 429, malformed and oversized bodies, redirects;
  - slow and hung responses, an empty list;
  - out-of-range encodings, once confirmed.
- It serves the integration tests, which run the real `urllib` client against the
  loopback server. With `server = "simulator"` it also allows an end-to-end soak on the
  Pi without Dexcom access: plain HTTP to `127.0.0.1` only, with synthetic credentials.

## 7. Fixtures and tests

Fixtures are hand-written synthetic JSON in `backend/tests/fixtures/dexcom_share/`,
shaped like the documented responses:

- account and session ids, an all-zero id;
- one value per trend name, an empty list, several values;
- each error code, an HTML 500 page, truncated JSON;
- a string value, a boolean value, a missing `WT`, a malformed `WT`, a future `WT`;
- values 39, 40, 400 and 401.

No real response is recorded. The real shape is confirmed on the Pi with an approved
read-only command that prints field names, types and data age, never values.

Tests:

- **Parser:** every fixture, accepted or rejected for the right reason.
- **Requests:** URLs, bodies, and credentials absent from URLs.
- **Session state machine,** with a fake monotonic clock: login, re-login, the login
  interval, the latch and restart.
- **Client against the simulator:** timeouts, the body cap, a refused redirect, and
  HTTPS required for real servers.
- **Runtime integration** with the provider, the worker and the engine: normal, stale
  data, missing data, outage, hang, session expiry, a blocked login, and recovery. These
  are the transition, stale-data, failure and recovery tests AGENTS.md requires for
  acquisition paths.
- **Configuration:** strict `[dexcom]` keys, the simulator port allowed only with
  `server = "simulator"`, credential loader errors.
- **Secrets:** no credential, account id or session id in logs, exceptions or reports.

## 8. Owner decisions

1. **Region:** `us`, `ous` or `jp`.
2. **Credential exposure:** the Pi holds the patient's Dexcom account credentials as
   systemd encrypted credentials. The Pi 3B has no TPM, so the encryption key lives on
   the same SD card. That stops casual copying of the credential file but not someone
   holding the card. Is that acceptable?
3. **Login limits:** proposed at least 300 s between logins, and a latch after
   `AccountPasswordInvalid` or `SSO_AuthenticateMaxAttemptsExceeded` until restart.
4. **Fetch cadence and staleness:** `fetch_interval_s` (example 60 s) and `max_age_s`
   (example 600 s, about two missed 5-minute readings) are the owner's operational
   values.
5. **Out-of-range encoding:** confirm it from the `pydexcom` and Nightscout handling,
   or leave it unconfirmed and keep relying on the normal thresholds.
6. **HTTP client:** stdlib `urllib` (recommended: no new dependency, explicit timeouts,
   no hidden retries) or `pydexcom` with `requests`.
7. **Display reason:** should a blocked login be distinguishable from a network outage
   on the display? The fault stays `acquisition`; only the M5 display text would
   differ.
8. **Undocumented API:** Share needs an application id, and open-source clients use
   the one from Dexcom's own app. Do you accept using an undocumented API, given
   Dexcom's terms?

## 9. Phases

On `feat/m4-dexcom-share`, one commit per file, one PR at the end:

1. Protocol module, fixtures and parser tests.
2. Session client, simulator and client tests.
3. Provider, configuration, credentials, systemd clock wiring and runtime integration
   tests.
4. Documentation: ARCHITECTURE (acquisition), SAFETY (failure-mode rows from section 5),
   PROJECT_STATUS, DEVELOPMENT_LOG.
5. With approval, on the Pi: a read-only connectivity and shape check with the owner's
   credentials, then a soak against the simulator and against Share with outputs still
   disabled. It records data age and error counts only.

## 10. Out of scope and risks

Out of scope: the display (M5), deployment (M6), outputs (M7), anything involving the
Omnipod 5, writing to Dexcom, trend-based alerts and persisting readings.

Risks:

- **API change:** Share can change or block access without notice. That surfaces as
  acquisition faults and stale data, never as a normal reading.
- **Account lockout:** limited by the login interval and the latch.
- **Phone dependency:** Share has data only while the G7 app uploads. The official
  alerts do not depend on Share.
- **Credentials on the Pi:** decision 2.
- **Clock:** freshness depends on a synchronized clock, which the existing clock check
  already enforces.
