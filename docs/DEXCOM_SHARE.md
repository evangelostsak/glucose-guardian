# Dexcom Share acquisition

Glucose Guardian can read the latest CGM value from Dexcom Share
(`provider = "dexcom_share"`). This is a supplementary, read-only use of an undocumented
API. The official Dexcom G7 and Omnipod 5 alerts stay independent and unchanged.
Nothing here writes to Dexcom, changes Share or followers, or touches the Omnipod 5.

Status on 2026-11-12:

- Implemented and tested against a local synthetic simulator only.
- **No live Dexcom request has been made.**
- Live connectivity needs the owner-approved check at the end of this document.

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

- HTTPS through `urllib` with certificate verification against the system CA store.
- Bounds on every request:
  - a per-request deadline;
  - a 64 KiB body cap, enforced with and without `Content-Length`;
  - redirects refused;
  - environment proxies ignored;
  - no automatic retries.
- Login: authenticate, then log in, then reuse the session. A session error renews it
  once, if the login interval allows.
- Login protection, kept across restarts in the state file:
  - at least 300 s between login attempts, persisted *before* each attempt;
  - a latch on rejected credentials or lockout;
  - backoff on HTTP 429.
- Strict parsing of each entry:
  - exactly `WT`, `ST`, `DT`, `Value` and `Trend`;
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

These need verification against a real response. The connectivity check reports each
one without printing glucose values.

| Assumption | Basis | If wrong |
|---|---|---|
| Endpoints, application id, request bodies | pydexcom and Nightscout agree | Login or read fails: `acquisition` fault, or the latch if Dexcom answers with a credential error |
| Query parameter spelled `sessionId` | pydexcom (Nightscout uses `sessionID`) | Reads fail with a session or argument error: `acquisition` fault |
| Entry keys are exactly `WT`, `ST`, `DT`, `Value`, `Trend` | pydexcom and Nightscout parsing | Every read is rejected: `acquisition` fault, then `missing_data` |
| `WT` is the reading time; `ST` and `DT` agree with it | Nightscout uses `WT`; pydexcom uses `DT` | The reading time could be wrong: the check prints the offsets |
| `Trend` is a name, or a legacy number from 0 to 9 | pydexcom says names replaced numbers | Reads are rejected: `acquisition` fault |
| LOW is 39 and HIGH is 401 | Dexcom's official API and Nightscout; not confirmed for Share | Another encoding is rejected: `acquisition` fault instead of a glucose alarm |
| Errors are HTTP errors with a JSON `Code` | pydexcom | Unknown errors are server errors and never latch |
| Sessions last about 24 h | nightscout-connect | Any lifetime works; renewal is limited by the login interval |
| HTTP 429 may carry `Retry-After` in seconds | HTTP standard | Without it, exponential backoff applies |
| `User-Agent: glucose-guardian/<version>` is accepted | Not documented | Requests rejected: `acquisition` fault |

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

The configuration never contains credentials; unknown keys such as `password` are
rejected.

## Provisioning credentials

Credentials are read only from `$CREDENTIALS_DIRECTORY`, which systemd provides from
`LoadCredentialEncrypted=`. Each credential file holds a single line. A file readable
by other users is refused. This procedure is for an approved installation (M6); nothing
here deploys the service.

1. Encrypt the credentials as root, typing them at the prompt so they stay out of shell
   history:

   ```sh
   sudo install -d -m 0700 /etc/glucose-guardian/credentials
   systemd-ask-password -n "Dexcom Share username:" | sudo systemd-creds encrypt --name=dexcom-username - /etc/glucose-guardian/credentials/dexcom-username.cred
   systemd-ask-password -n "Dexcom Share password:" | sudo systemd-creds encrypt --name=dexcom-password - /etc/glucose-guardian/credentials/dexcom-password.cred
   sudo chmod 0600 /etc/glucose-guardian/credentials/*.cred
   ```

2. Reference them in the unit, as `deployment/systemd/glucose-guardian.service` shows:
   `LoadCredentialEncrypted=` for both names, `StateDirectory=glucose-guardian` and
   `StateDirectoryMode=0700`.

At run time systemd decrypts both files into a private, read-only directory that only
the service user can read. The plaintext never touches the SD card.

## Rotating credentials

1. Change the password through Dexcom (the G7 app or Dexcom's account site), never
   from the Pi.
2. Stop the service, re-encrypt the changed credential (step 1 above), check the state
   with `python -m glucose_guardian.share_state --state-file <state_file> status`, and
   start the service.
3. If the service ran with the old password after the change, it has latched
   `AccountPasswordInvalid` and stopped logging in. Follow the recovery below.

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
2. Stop the service, run `python -m glucose_guardian.share_state --state-file
   <state_file> unblock`, then start the service. The login interval still applies
   after unblocking.
3. Never unblock repeatedly without fixing the cause: every attempt counts toward
   Dexcom's lockout.

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
- **The state file** contains no secrets: only timestamps, the backoff and the block
  reason.

## Read-only connectivity check (owner approval required)

This is the first live Dexcom contact. Run it only with explicit approval, once, and
not within 5 minutes of another login attempt; the persisted interval enforces this
anyway.

`python -m glucose_guardian.share_check` makes one session request (authenticate,
login) and one read.

- **It prints:** the entry count, each entry's keys and JSON types, the age of `WT`,
  the `ST` and `DT` offsets from `WT`, the `DT` time-zone offset, whether `Value` is an
  integer from 39 to 401, whether `Trend` is recognized, and whether the parser accepts
  the response.
- **It never prints:** glucose values, trends, credentials or identifiers.

It uses the same state file, login interval and latch as the service.

1. On the laptop, from the reviewed branch, copy the code to the Pi:

   ```sh
   rsync -a --exclude .venv --exclude __pycache__ --exclude '*.egg-info' backend config <user>@<pi-host>:glucose-guardian-m4/
   ```

2. On the Pi, install it into a virtual environment:

   ```sh
   cd ~/glucose-guardian-m4
   python3 -m venv .venv
   .venv/bin/python -m pip install -e backend
   ```

3. Encrypt the credentials as in step 1 of *Provisioning credentials*.

4. Create a state directory and a local configuration (`config/local.*` is ignored by
   Git):

   ```sh
   install -d -m 0700 ~/glucose-guardian-state
   sed "s|/var/lib/glucose-guardian|$HOME/glucose-guardian-state|" config/example-dexcom-share.toml > config/local.dexcom.toml
   ```

5. Run the check once:

   ```sh
   sudo systemd-run --pipe --wait --uid="$(id -u)" --gid="$(id -g)" \
     -p LoadCredentialEncrypted=dexcom-username:/etc/glucose-guardian/credentials/dexcom-username.cred \
     -p LoadCredentialEncrypted=dexcom-password:/etc/glucose-guardian/credentials/dexcom-password.cred \
     -p WorkingDirectory="$PWD" \
     "$PWD/.venv/bin/python" -m glucose_guardian.share_check --config "$PWD/config/local.dexcom.toml"
   ```

6. Send back the complete output; it contains no values or secrets. Expected for a
   healthy account:
   - `Dexcom Share responded`;
   - `entries:` 1 to 3, each with keys `['DT', 'ST', 'Trend', 'Value', 'WT']`;
   - a `WT age` of a few minutes;
   - small `ST minus WT` and `DT minus WT` offsets;
   - `DT offset +0100` or `+0200`;
   - `parser: accepted`.
7. **On failure, do not retry.**
   - `auth_blocked`: follow the recovery above.
   - Anything else: send the output for analysis.

This check does not confirm the LOW and HIGH encodings, which need a reading outside
40 to 400. If such a reading occurs, a check run then shows whether `Value` stays in
the accepted range.
