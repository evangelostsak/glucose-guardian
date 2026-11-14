# Dexcom Share acquisition

Glucose Guardian can read the latest CGM value from Dexcom Share
(`provider = "dexcom_share"`). This is a supplementary, read-only use of an undocumented
API. The official Dexcom G7 and Omnipod 5 alerts stay independent and unchanged.
Nothing here writes to Dexcom, changes Share or followers, or touches the Omnipod 5.

Status on 2026-11-13:

- Implemented, and tested against a local synthetic simulator.
- **Live-validated:** the owner-approved read-only connectivity check at the end of
  this document passed on the Pi on 2026-11-13, on the first attempt.
- The service has not yet run continuously against Dexcom.

## Required account

The Share API authenticates the **publisher** account: the patient's own Dexcom account
that the G7 app uses to upload readings.

- **Follower accounts:** the API offers no follower login, so a dedicated follower
  account for the Pi is not possible. Do not create one.
- **Share setup:** Share must be enabled with at least one follower. The existing
  followers stay untouched.
- **Username form:** the Dexcom user name, email address, or phone number with `+` and
  the country code.
- **Region:** `ous` for Germany.

The evidence for this is in [plans/2026-11-07-m4-dexcom-share.md](plans/2026-11-07-m4-dexcom-share.md),
section 2.

## Implemented and tested

The following is implemented and tested against the synthetic simulator:

- A read-only allowlist checked before every request:
  - the three Share hosts (or the loopback simulator);
  - only the authenticate, login and read operations;
  - no query except the read window;
  - only the documented body keys.

  Anything else is refused without being sent. Only POST is used and redirects are
  refused. No code path can change account settings, followers, sharing, alerts,
  glucose records or devices.

- HTTPS through `urllib` with certificate verification against the system CA store.
- Bounds on every request:
  - a per-request deadline that covers connecting to every resolved address, the TLS
    handshake, the headers and the body, however slowly the server sends. DNS
    resolution is the exception: it cannot be interrupted, and the request fails as
    soon as it returns late;
  - a 64 KiB body cap, enforced with and without `Content-Length`;
  - a body shorter than its `Content-Length` is rejected;
  - redirects refused;
  - environment proxies ignored;
  - no automatic retries.
- Login: authenticate, then log in, then reuse the session. A session error renews it
  once, if the login interval allows.
- Login protection, kept across restarts in the state file:
  - at least 300 s between login attempts, persisted *before* each attempt;
  - a latch on rejected credentials or lockout;
  - a latch when a login's outcome was never recorded (`LoginOutcomeUnknown`), so a
    rejection lost to a crash or a failed write is not retried after a restart;
  - backoff on HTTP 429, cleared only by a read that passes the parser, never by a
    bare HTTP 200.
- Strict parsing of each entry:
  - the required fields `WT`, `ST`, `DT`, `Value` and `Trend`; additional fields are
    ignored and never read;
  - `WT` as the UTC reading time;
  - an integer `Value` from 39 to 401;
  - a known trend.
- Readings: the newest entry by timestamp is used. Duplicates, out-of-order entries and
  older readings never replace a newer one.
- Errors, logs, reports and the state file never contain credentials, account ids,
  session ids, server messages or URLs.
- Faults: failures become the existing `acquisition` fault, stale data becomes
  `missing_data`, future timestamps become `invalid_reading`, and an unsynchronized
  clock suspends evaluation. Alarm policy and thresholds are unchanged.
- Worker limit: at most one acquisition job, and so one HTTP request, in flight. A hung
  request is reported as `TimedOut` and starts no new thread.

## Assumptions about the undocumented API

The connectivity check reports each one without printing glucose values. The last
column is the owner-reported result of the check on 2026-11-13.

| Assumption | Basis | If wrong | Live check 2026-11-13 |
|---|---|---|---|
| Endpoints, application id, request bodies | pydexcom and Nightscout agree | Login or read fails: `acquisition` fault, or the latch if Dexcom answers with a credential error | Confirmed for `ous` |
| Query parameter spelled `sessionId` | pydexcom (Nightscout uses `sessionID`) | Reads fail with a session or argument error: `acquisition` fault | Confirmed |
| Entries contain `WT`, `ST`, `DT`, `Value`, `Trend`; other fields may be added | pydexcom and Nightscout parsing | A missing field rejects every read: `acquisition` fault, then `missing_data`. Added fields are ignored | Confirmed: both entries had all five |
| `WT` is the reading time; `ST` and `DT` agree with it | Nightscout uses `WT`; pydexcom uses `DT` | The reading time could be wrong: the check prints the offsets | Timestamp checks passed; newest `WT` 42 s old |
| `Trend` is a name, or a legacy number from 0 to 9 | pydexcom says names replaced numbers | Reads are rejected: `acquisition` fault | Recognized; the check does not tell names from legacy numbers |
| LOW is 39 and HIGH is 401 | Dexcom's official API and Nightscout; not confirmed for Share | Another encoding is rejected: `acquisition` fault instead of a glucose alarm | Not exercised |
| Errors are HTTP errors with a JSON `Code` | pydexcom | Unknown errors are server errors and never latch | Not exercised |
| Sessions last about 24 h | nightscout-connect | Any lifetime works; renewal is limited by the login interval | Not exercised |
| HTTP 429 may carry `Retry-After` in seconds | HTTP standard | Without it, exponential backoff applies | Not exercised |
| `User-Agent: glucose-guardian/<version>` is accepted | Not documented | Requests rejected: `acquisition` fault | Confirmed |

## Configuration

`config/example-dexcom-share.toml` holds the approved values. Every key is required.

| Key | Value | Rule |
|---|---|---|
| `[runtime] fetch_interval_s` | 60 | Below `max_age_s` |
| `[alarm] max_age_s` | 600 | Reading age before `missing_data` |
| `[dexcom] server` | `ous` | `us`, `ous`, `jp` or `simulator`; `simulator` also needs `simulator_port` |
| `[dexcom] login_min_interval_s` | 300 | At least 300 |
| `[dexcom] rate_limit_initial_s`, `rate_limit_max_s` | 120, 1800 | Provisional; initial must not exceed max |
| `[dexcom] request_timeout_s` | 5 | Three times it must not exceed `fetch_timeout_s` (20) |
| `[dexcom] state_file` | `/var/lib/glucose-guardian/dexcom-share-state.json` | Absolute; its directory must exist |
| `[display]` | `kind = "snapshot"`, `snapshot_file = "/run/glucose-guardian/display.json"`, 5 s, 1 s, 1800 s, `/dev/fb0`, DejaVu Sans Bold | Required since M5 PR A; see ARCHITECTURE |

`[display]` is required. A configuration written before M5 PR A, such as an existing
`config/local.dexcom.toml` on the Pi, is refused with a configuration error naming
`[display]`. That stops `dexcom-status`, `dexcom-check` and the Dexcom line of
`status` until the `[display]` block from `config/example-dexcom-share.toml` is added.
The table never affects Dexcom access.

The configuration never contains credentials; unknown keys such as `password` are
rejected.

## Provisioning credentials

Credentials are read only from `$CREDENTIALS_DIRECTORY`, which systemd provides from
`LoadCredentialEncrypted=`. Each credential file holds a single line. A file readable
by other users is refused. Nothing here deploys the service; installing it is planned
with M5 and needs approval.

1. Encrypt the credentials as root, typing them at the prompt so they stay out of shell
   history:

   ```sh
   sudo install -d -m 0700 /etc/glucose-guardian/credentials
   systemd-ask-password -n "Dexcom Share username:" | sudo systemd-creds encrypt --name=dexcom-username - /etc/glucose-guardian/credentials/dexcom-username.cred
   systemd-ask-password -n "Dexcom Share password:" | sudo systemd-creds encrypt --name=dexcom-password - /etc/glucose-guardian/credentials/dexcom-password.cred
   sudo chmod 0600 /etc/glucose-guardian/credentials/*.cred
   ```

   On the Pi, `make dexcom-credentials` runs the same commands. It encrypts each file
   under a temporary name and renames it when encryption succeeds, so a cancelled
   prompt leaves nothing behind. It keeps an existing file; replace one with
   `make dexcom-credentials-rotate` (see *Rotating credentials*), never by deleting it
   first. `make dexcom-credentials-check` decrypts both files through systemd and
   checks their format without contacting Dexcom.

2. Reference them in the unit, as `deployment/systemd/glucose-guardian.service` shows:
   `LoadCredentialEncrypted=` for both names, `StateDirectory=glucose-guardian` and
   `StateDirectoryMode=0700`.

At run time systemd decrypts both files into a private, read-only directory that only
the service user can read. The plaintext never touches the SD card.

## Rotating credentials

1. Change the password through Dexcom (the G7 app or Dexcom's account site), never
   from the Pi.
2. Stop the service if it is installed. On the Pi, in `~/glucose-guardian`, replace
   the changed credential:

   ```sh
   make dexcom-credentials-rotate CREDENTIAL=password
   make dexcom-credentials-check
   make dexcom-status
   ```

   `dexcom-credentials-rotate` (`CREDENTIAL=username` for the user name) works as
   follows:

   - **Confirmation:** it asks for `replace dexcom-password` to be typed exactly.
   - **Entry:** it reads the new value twice from hidden `systemd-ask-password`
     prompts and stops if the two differ.
   - **Encryption:** it pipes the value into `systemd-creds encrypt` as a temporary
     `.cred.new` file.
   - **Validation:** it loads that file, with the other current credential, through
     `systemd-run` with `LoadCredentialEncrypted=` (the mechanism the check uses) and
     `python -m glucose_guardian.credentials`. That decrypts both and checks their
     format. It does not contact Dexcom, so a wrong but well-formed password is only
     found at the next login.
   - **Swap:** only then does it rename the temporary file over the current one.
   - **On failure:** if the entries differ, encryption fails or validation fails, the
     current file is unchanged and the temporary file is removed.
   - **Exposure:** the value never appears in arguments, the environment or logs.

3. If `dexcom-status` shows `AccountPasswordInvalid`, the old password was used after
   the change and automatic login is blocked. Follow the recovery below, then start
   the service.

## Recovering from a blocked login

The log shows `Dexcom Share automatic login blocked: <code>`, the provider status is
`auth_blocked`, and the `acquisition` fault stays active. The service sends no further
login requests, across restarts too.

1. Find the cause with `share_state ... status`:
   - `AccountPasswordInvalid` or `SSO_InternalError`: the credentials are wrong.
     Check that the official app still logs in, then re-provision.
   - `SSO_AuthenticateMaxAttemptsExceeded`: Dexcom has locked the account. Wait out
     Dexcom's lockout or reset the password through Dexcom; check that the official
     app works first.
   - `InvalidArgument` or `NilIdentifier`: the user name is malformed or unknown.
   - `StateUnreadable`: the state file was damaged.
   - `LoginOutcomeUnknown`: a login was sent but its result was never recorded. The
     service crashed, was stopped or lost power during the login, or the state file
     could not be written (look for `Dexcom Share state could not be written` in the
     log). Check that the state directory is writable and that the official app
     still logs in; the credentials may have been rejected.
2. Stop the service, run `python -m glucose_guardian.share_state --config
   config/local.dexcom.toml unblock` (or `--state-file <state_file>`), then start the
   service. The login interval still applies after unblocking.
3. Never unblock repeatedly without fixing the cause: every attempt counts toward
   Dexcom's lockout.

## Moving the state file to the installed service

The connectivity check keeps its login state in the file named by
`config/local.dexcom.toml` (`~/glucose-guardian-state`). When the service is installed
(M5), the file moves once to `/var/lib/glucose-guardian/dexcom-share-state.json`, by
the procedure in section 3.8 of the
[M5 plan](plans/2026-11-13-m5-hdmi-display.md#38-login-state-migration-d9). The exact
commands follow with the installation.

- **Never reset:** never delete, recreate, edit or unblock the file to simplify the
  move. A missing file allows an immediate login and forgets a block.
- **Exact copy:** byte for byte, mode 0600, owned by the service user, verified with
  `share_state ... status` before the service starts.
- **One file:** afterwards the check and the service use the same file, and the check
  never runs while the service is active.

## SD-card limitations

- The Raspberry Pi 3B has no TPM. `systemd-creds` therefore encrypts with the host key
  `/var/lib/systemd/credential.secret`, on the same SD card.
- **Protects against:** copying a `.cred` file to another machine, accidental
  inclusion in backups or diagnostics, and other unprivileged users on the Pi.
- **Does not protect against:** someone holding the SD card, or root on the Pi; both
  can decrypt the credentials.
- **What a leak exposes:** the publisher password opens the whole Dexcom account. Use
  a password unique to Dexcom, keep the Pi physically secure, and change the password
  through Dexcom if the card is lost.
- **The state file** contains no secrets: only timestamps, the backoff, the block
  reason and whether a login is in progress.

## Read-only connectivity check (owner approval required)

**Result on 2026-11-13:** passed on the Pi on the first attempt (owner-reported). The
record is in section 13 of the
[M4 plan](plans/2026-11-07-m4-dexcom-share.md#13-live-validation-2026-11-13) and in
[PROJECT_STATUS.md](PROJECT_STATUS.md).

Run it again only with explicit approval, for example after a credential rotation or
to confirm the LOW and HIGH encodings, and not within 5 minutes of another login
attempt; the persisted interval enforces this anyway.

`python -m glucose_guardian.share_check` makes one session request (authenticate,
login) and one read.

- **It prints:** the entry count, each entry's keys and JSON types, any missing
  required key, the number of additional keys, the age of `WT`, the `ST` and `DT`
  offsets from `WT`, the `DT` time-zone offset, whether `Value` is an integer from 39
  to 401, whether `Trend` is recognized, and whether the parser accepts the response.
- **It never prints:** glucose values, trends, the values of additional fields,
  credentials or identifiers.

It uses the same state file, login interval, latch and request allowlist as the
service, so it can send nothing but the authenticate, login and read operations.

The root `Makefile` runs every step; `make -n <target>` prints a target's exact
commands without running them.

1. On the laptop, from an up-to-date `main`, copy the tree to the Pi and install it
   there:

   ```sh
   make pi-sync PI_USER=<user> PI_HOST=<pi-host>
   make pi-setup PI_USER=<user> PI_HOST=<pi-host>
   ```

   `pi-sync` copies the repository (without `.venv`, `config/local.*` or bench reports)
   to `~/glucose-guardian`. `pi-setup` runs `make bench-setup` there over SSH. That
   installs `python3-venv`, `avrdude` and `usbutils`, creates `.venv`, installs the
   backend, and asks for the Pi user's `sudo` password.

2. On the Pi, in `~/glucose-guardian`, provision and check:

   ```sh
   make dexcom-credentials
   make dexcom-credentials-check
   make dexcom-config
   make status
   make dexcom-status
   ```

   - **`dexcom-credentials`:** encrypts both credentials, typed at a hidden prompt, as
     in *Provisioning credentials*.
   - **`dexcom-credentials-check`:** must report that both decrypt and are well-formed.
   - **`dexcom-config`:** creates `~/glucose-guardian-state` (mode 0700) and the
     Git-ignored `config/local.dexcom.toml` pointing at it.
   - **The state file:** it always comes from `DEXCOM_CONFIG`.
     - `dexcom-status`, the `dexcom-check` preflight and `share_check` all read the
       path named there.
     - An explicit `STATE_DIR` that disagrees with an existing configuration is
       refused, not silently used.
   - **`status`:** must show `clock synchronized: yes`, the virtual environment, the
     Dexcom configuration and `dexcom credentials: provisioned`.
   - **`dexcom-status`:** must show `blocked: no`, or that no state file exists yet.

3. Run the check once:

   ```sh
   make dexcom-check
   ```

   It shows the login state and says which three requests it will send. It sends
   nothing unless `contact dexcom` is typed exactly. It then runs `share_check` once
   under `sudo systemd-run --pipe --wait`, with `LoadCredentialEncrypted=` for both
   credentials, and never retries.

4. Send back the complete output; it contains no values or secrets. Expected for a
   healthy account:
   - `Dexcom Share responded`;
   - `entries:` 1 to 3, each with keys that include `DT`, `ST`, `Trend`, `Value` and
     `WT`, and no `missing required keys` line;
   - `additional keys ignored:` 0, or the count of extra fields Dexcom adds; their
     names and types are listed in the `keys` line, never their values;
   - a `WT age` of a few minutes;
   - small `ST minus WT` and `DT minus WT` offsets;
   - `DT offset +0100` or `+0200`;
   - `parser: accepted`.
5. **On failure, do not retry.**
   - `auth_blocked`: follow the recovery above.
   - Anything else: send the output for analysis.

This check does not confirm the LOW and HIGH encodings, which need a reading outside
40 to 400. If such a reading occurs, a check run then shows whether `Value` stays in
the accepted range.
