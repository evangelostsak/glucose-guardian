# M4 Dexcom Share adapter: implementation plan

Status:

- Proposed on 2026-11-07.
- Approved by the owner on 2026-11-12, with the decisions in section 8.
- Implemented on `feat/m4-dexcom-share`. It was tested with synthetic data only; no
  live Dexcom request has been made.
- The operator procedures are in [DEXCOM_SHARE.md](../DEXCOM_SHARE.md).

## 1. Goal and boundaries

- Read the latest CGM value from Dexcom Share and feed it to the existing alarm engine
  as a `Reading`, behind `provider = "dexcom_share"`. `config/example.toml` keeps
  `provider = "simulated"`; `config/example-dexcom-share.toml` shows the Dexcom
  settings.
- Glucose Guardian stays a supplementary bedside alert and display device. It only
  reads from Share. It never writes to Dexcom, changes Share or follower settings, or
  touches the Omnipod 5 or its controller. The official Dexcom G7 and Omnipod 5 alerts
  stay independent and unchanged.
- The adapter also avoids indirect effects on the official system, chiefly locking the
  patient's Dexcom account through repeated failed logins (section 4).
- No change to the alarm policy, thresholds, acknowledgement, fault set, GG1 protocol,
  firmware or outputs. No deployment.

## 2. How Share works: verified against current clients (2026-11-12)

The sources were read on 2026-11-12:

- `pydexcom` (`gagebenne/pydexcom`, `main`): `const.py`, `dexcom.py`, `errors.py`,
  `glucose_reading.py`, `README.md`;
- `nightscout-connect` (`lib/sources/dexcomshare.js`, `main`);
- `share2nightscout-bridge` (`index.js`, `README.md`, deprecated);
- Dexcom's official API reference for the LOW and HIGH values.

| Item | Finding | Source |
|---|---|---|
| Hosts | `share2.dexcom.com` (US), `shareous1.dexcom.com` (outside the US), `share.dexcom.jp` (Japan), each with `/ShareWebServices/Services/` | pydexcom `const.py`; nightscout-connect `_known_servers` |
| Application id | `d89443d2-327c-4a6f-89e5-496bbb0317db` (US and outside the US), `d8665ade-9673-4e27-9ff6-92db4ce13d13` (Japan) | pydexcom `const.py`; both Nightscout clients |
| Login | POST `General/AuthenticatePublisherAccount` with `accountName`, `password`, `applicationId` returns an account id. POST `General/LoginPublisherAccountById` with `accountId`, `password`, `applicationId` returns a session id | pydexcom `dexcom.py`; both Nightscout clients |
| Read | POST `Publisher/ReadPublisherLatestGlucoseValues` with query `sessionId`, `minutes` (at most 1440) and `maxCount` (at most 288), body `{}` | pydexcom (Nightscout spells it `sessionID`) |
| Entry | `WT`, `ST`, `DT` as `Date(<epoch ms>)`, with `DT` carrying a `±HHMM` offset; integer `Value`; `Trend` as a name. Older responses used numbers 0 to 9 | pydexcom reads `DT`; Nightscout reads `WT` |
| Errors | `SessionIdNotFound` and `SessionNotValid` mean the session expired. `AccountPasswordInvalid`, `SSO_AuthenticateMaxAttemptsExceeded`, and `SSO_InternalError` with "Cannot Authenticate by AccountName/AccountId" mean a failed login. `InvalidArgument` means bad input. The all-zero UUID is "likely error" | pydexcom `_handle_error_code`, `DEFAULT_UUID` |
| Session renewal | pydexcom renews once on a session error. nightscout-connect refreshes after about 23 h 50 min. Neither applies a timeout | pydexcom; nightscout-connect |
| LOW and HIGH | Dexcom's official API uses 39 for below 40 and 401 for above 400. Nightscout shows 39 as LOW and 401 as HIGH for `share2` devices (PR #7207). Neither Share client special-cases them | developer.dexcom.com v2/v3; cgm-remote-monitor PR #7207 |

**Account type.** The API authenticates the **publisher** account, the patient's own
Dexcom account that uploads the data:

- pydexcom documents its `username` argument as "username for the Dexcom Share user,
  *not follower*". Its FAQ says: "Use the same credentials used to login to the Dexcom
  mobile application publishing the glucose readings."
- The Nightscout bridge README says it "logs in to Dexcom Share as the data publisher".

Neither client has a follower login, so a dedicated follower account for the Pi is not
supported by this API, and none was created. Share must be enabled with at least one
follower; the owner has two, and they stay untouched.

**Alternatives considered:**

- Dexcom's official developer API (OAuth) serves delayed, retrospective data, so it
  cannot drive a real-time alert.
- Nightscout would add a service outside this project.

## 3. Components (as implemented)

| Module | Responsibility |
|---|---|
| `dexcom_share.py` | Pure protocol: endpoints, request bodies, strict parsing, error classes and display statuses |
| `share_state.py` | Persisted login state (cooldown, rate limit, block latch) and the `status`/`unblock` operator commands; holds no secrets |
| `credentials.py` | Reads `dexcom-username` and `dexcom-password` from `$CREDENTIALS_DIRECTORY` |
| `share_client.py` | `urllib` HTTPS poster, `ShareSession` login state machine, `DexcomShareProvider` |
| `share_simulator.py` | Local synthetic Share server for tests and soak runs |
| `share_check.py` | Read-only connectivity check that prints structure and timing, never values |
| `config.py`, `service.py` | Strict `[dexcom]` table; provider wiring with the systemd clock check |

Changes from the proposal:

- The provider lives in `share_client.py`, not `providers.py`, so `providers.py`
  imports no network code.
- At most three requests per fetch, because the account id is cached:
  - first fetch: authenticate, login, read;
  - renewal: read, login, read.
- The added persisted state and the connectivity check (sections 4 and 9).

## 4. Authentication and session

As proposed, with these approved values:

- **Login interval:** at least 300 s between login attempts. This is a hard floor in
  the configuration.
- **Latch:** `AccountPasswordInvalid`, `SSO_AuthenticateMaxAttemptsExceeded`,
  `SSO_InternalError` with a rejected-login message, `InvalidArgument` during login, or
  an all-zero identifier blocks automatic login. It stays blocked until an operator runs
  `python -m glucose_guardian.share_state --state-file <path> unblock`.
- **HTTP 429:** exponential backoff from `rate_limit_initial_s` to `rate_limit_max_s`.
  A `Retry-After` in seconds is honored up to the maximum. A rate-limited login still
  counts as a login attempt.

**Restart safety.** A state file, written with mode 0600, `fsync` and an atomic
rename, holds:

- the time of the last login attempt;
- the rate-limit backoff and its end;
- the block reason.

It holds no credentials, account id or session id. Each attempt is persisted *before*
the request is sent, so a crash during login still counts. Over restarts:

- **Same boot:** the cooldown continues on `CLOCK_BOOTTIME`, unaffected by wall-clock
  changes, so a crash-looping service logs in at most once per 300 s.
- **After a reboot:** the cooldown restarts from service start. The Pi 3B has no
  real-time clock, so wall time cannot be trusted at boot. This costs up to 300 s of
  startup delay after a reboot; the startup missing-data fault is active then anyway.
- **Unreadable state file:** automatic login is blocked until an operator unblocks it.
- **Unwritable state file:** every login is refused, before any request is sent.

## 5. Readings and freshness

As proposed, plus:

- **Window:** each read asks for `minutes = ceil(max_age_s / 60)` (10) and
  `maxCount = minutes / 5 + 1` (3).
- **Timestamp:** `WT` (UTC epoch milliseconds) is the reading time, as in Nightscout.
  `ST` and `DT` must parse but are not used; the connectivity check reports their
  offsets from `WT`. The receipt time is never used.
- **Strict entries:** exactly `WT`, `ST`, `DT`, `Value`, `Trend`. `Value` must be an
  integer from 39 to 401, which takes in LOW 39 and HIGH 401. Unknown keys, trends or
  value encodings fail the fetch as a protocol error, which shows as an acquisition
  fault. 39 and 401 reach the engine unchanged and classify through the existing
  thresholds.
- **Choosing the reading:** the newest entry by timestamp wins regardless of order.
  Identical duplicates collapse; entries with the same timestamp and different values
  fail the fetch. A reading older than, or equal to, the previous one is not returned
  again, so its age keeps growing toward `missing_data`.
- **Trend:** parsed into `Trend` and kept as `DexcomShareProvider.latest_trend` for M5.
  No alarm uses it.
- **Display status:** `DexcomShareProvider.status` tells a blocked login
  (`auth_blocked`) apart from network, server, protocol, rate-limit and deferred-login
  states, for the M5 display.

## 6. Simulated data source

As proposed. `share_simulator.py`:

- binds to `127.0.0.1` and accepts only the synthetic credentials;
- serves generated values with injectable faults;
- has a command-line mode for soak runs (`--scenario normal|stale|empty|server-error|
  rate-limit|invalid-password|lockout|hang`).

With `server = "simulator"`, the client uses plain HTTP to that loopback port. Every
other server is HTTPS.

## 7. Fixtures and tests

Synthetic only. The protocol tests use hand-written entries in the documented shape.
The client, runtime, service and check tests run the real `urllib` client against the
simulator over loopback HTTP; one test uses a self-signed TLS server. Results are in
`DEVELOPMENT_LOG.md`.

## 8. Owner decisions (approved 2026-11-12)

1. **Location and region:** Germany, `ous`.
2. **Credentials:** stored on the Pi as systemd encrypted credentials. The SD-card
   limitations are documented in [DEXCOM_SHARE.md](../DEXCOM_SHARE.md).
3. **Login limits:** at least 300 s between login attempts. Invalid credentials and
   lockout responses stop automatic login.
4. **Cadence and freshness:** a fetch every 60 s, readings valid for at most 600 s.
5. **Out-of-range values:** 39 and 401 are accepted per the findings in section 2;
   confirmation on a live response is pending.
6. **HTTP client:** standard-library `urllib`.
7. **Blocked login:** distinguishable from a network outage, through
   `DexcomShareProvider.status`, for the M5 display.
8. **Undocumented API:** the read-only use of Share is accepted.
9. **Rate limiting:** the starting values `rate_limit_initial_s = 120` and
   `rate_limit_max_s = 1800` are provisional and were not part of the approval.

## 9. Phases

1. Protocol, fixtures and parser tests: done.
2. Session client, simulator and client tests: done.
3. Provider, configuration, credentials, systemd clock wiring and runtime tests: done.
4. Documentation: done. See ARCHITECTURE, SAFETY, DEXCOM_SHARE, PROJECT_STATUS and
   DEVELOPMENT_LOG.
5. **Pending owner approval:** a read-only connectivity check with the owner's
   credentials (`share_check`, procedure in DEXCOM_SHARE.md). After that, a soak
   against the simulator and against Share, with outputs still disabled.

## 10. Out of scope and risks

Out of scope: the display (M5), deployment (M6), outputs (M7), anything involving the
Omnipod 5, writing to Dexcom, trend-based alerts and persisting readings.

Risks:

- **API change:** Share can change or block access without notice. That surfaces as
  acquisition faults and stale data, never as a normal reading.
- **Account lockout:** limited by the login interval, the latch and the persisted
  cooldown.
- **Phone dependency:** Share has data only while the G7 app uploads. The official
  alerts do not depend on Share.
- **Credentials on the Pi:** see DEXCOM_SHARE.md.
- **Clock:** freshness depends on a synchronized clock, which the systemd clock check
  enforces.
