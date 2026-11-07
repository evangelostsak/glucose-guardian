# Serial protocol v1

Pi ↔ Arduino Uno over USB serial at 115200 baud, 8N1. Implemented by
`backend/src/glucose_guardian/protocol.py` and `link.py` on the Pi and
`firmware/arduino-uno/lib/guardian/` on the Uno. Both test suites assert the same
frame vectors byte for byte.

## Framing

A frame is printable ASCII (0x20–0x7E) ending in a single LF:

```
<body>*<CRC>\n
```

- `CRC` is CRC-16/CCITT-FALSE (polynomial 0x1021, initial 0xFFFF, no reflection, no
  final XOR) over the body bytes, as four uppercase hex digits. Check value:
  `crc16("123456789") = 0x29B1`.
- A frame, LF included, is at most 64 bytes. Longer input is discarded up to the next
  LF and counted as oversize. Readers keep at most 63 pending bytes; the Pi also reads
  at most 1024 bytes per tick.
- Fields are separated by one space. Hex fields are fixed width and uppercase.
- The first field is the magic and version, `GG1`. A frame with a valid checksum whose
  first field starts with `GG` but is not `GG1` is a version mismatch.

Rejected input is counted by reason (oversize, encoding, missing checksum, checksum,
version, malformed, outdated, duplicate) and **never refreshes link liveness**.

## Pi → Uno: state frame

```
GG1 S <session:4> <seq:4> <glucose:1> <fault:1>*CCCC
GG1 S 7F3A 0001 U 1*03EE
```

| Field | Meaning |
|---|---|
| `session` | Random 16-bit value chosen when the Pi process starts |
| `seq` | Per-session sequence, wraps from FFFF to 0000 |
| `glucose` | Audible glucose alarm: `L` low, `U` urgent low, `H` high, `-` none (normal, unknown or acknowledged) |
| `fault` | `1` if the system-fault channel is audible, else `0` |

The Pi sends one state frame per supervisory tick, from the alarm loop itself. The
state frame is the heartbeat: if the alarm loop stops, heartbeats stop. Frames carry
only what should sound; the Uno makes no glucose decisions.

## Uno → Pi: report frame

The Uno answers every accepted state frame with one report:

```
GG1 R <boot:4> <seq:4> <ack_session:4> <ack_seq:4> <cause:1> <flags:2> <short:2> <long:2>*CCCC
GG1 R 00A1 0002 7F3A 0001 E 00 03 01*505C
```

| Field | Meaning |
|---|---|
| `boot` | Boot counter, incremented in EEPROM at every Uno start |
| `seq` | Per-boot report sequence, wraps |
| `ack_session`, `ack_seq` | The state frame being answered |
| `cause` | Reset cause: `P` power-on, `W` watchdog, `B` brown-out, `E` external (including DTR), `U` unknown |
| `flags` | Bit 0: button stuck. Other bits must be zero |
| `short`, `long` | Cumulative short and long presses this boot, modulo 256 |

## Acceptance rules

**Uno, for state frames**
- A frame is new if its session differs from the last accepted session, or its
  sequence is newer by serial-number arithmetic (`0 < (seq - last) mod 65536 < 32768`).
- Duplicates, replays and older frames are dropped without a reply and do not refresh
  liveness.
- A session change is accepted at once, so a restarted Pi resynchronizes immediately.

**Pi, for report frames**
- `ack_session` must be this process's session and `ack_seq` one of the last 16 state
  frames sent; otherwise the report is outdated.
- Within one boot, `seq` must be newer than the last accepted report; otherwise it is a
  duplicate.
- Liveness is the send time of the newest acknowledged state frame, so the Pi
  measures round-trip age, not receive time. A late ack of an older frame never
  reduces liveness.

## Transmit failures

The Pi writes a state frame only when the port reports room (zero-timeout `select`)
and never retries within a tick. pyserial's non-blocking `write()` returns a partial
count without raising, and spins on `EAGAIN` when the buffer is completely full, so
the transport never calls it on a full buffer.

A write that is zero-length, partial or raises counts as a failed transmission:

1. The frame's sequence number never enters the acknowledgement window, so no reply
   can refresh liveness for it.
2. Unsent output is discarded (`TCOFLUSH`), so neither the truncated tail nor older
   queued frames reach the Uno later. This also keeps `close()` from waiting for the
   buffer to drain.
3. The port is closed and reopened with the reconnect backoff. Reopening resets the
   Uno via DTR, clearing any partial line it holds.

If no reset occurs, the Uno appends the next frame to the truncated one, the combined
line fails its checksum, and it is rejected without refreshing liveness. The frame
after it is accepted.

## Connection, reset and reconnection

| Situation | Pi behavior |
|---|---|
| First accepted report after the port opens | Initial connection; button counters become the baseline. A `W` or `B` cause flags an unexpected reset. |
| New `boot` while connected | Unexpected reset: `controller_reset` fault latched until a long press; presses since the reboot are counted |
| Port error, unplug or hangup | Port closed and reopened with exponential backoff from `reconnect_initial_s` to `reconnect_max_s`; the backoff resets only after a connection has lasted `reconnect_max_s`; the next report is an initial connection |
| No accepted report for longer than `heartbeat_timeout_s` | `communication` fault |

Opening the port asserts DTR, which resets the Uno; that reset arrives as an initial
connection and raises no fault. The stock Uno bootloader (Optiboot) clears the reset
flags before the sketch runs, so `cause` may read `U`; the boot counter detects resets
regardless of cause. Bench validation must confirm what the installed bootloader
reports.

## Button events

The Uno debounces the button (50 ms) and classifies each press on release:

| Hold time | Event | Link up | Link lost |
|---|---|---|---|
| < 2 s | short | `short` counter +1: Pi silences the glucose channel | ignored |
| 2 s to < 30 s | long | `long` counter +1: Pi silences the fault channel | Uno silences its own link-lost alarm for 60 s |
| ≥ 30 s | none | stuck flag set until release | stuck flag set until release |

Counters, not single events, make presses robust to a lost report: the next accepted
report carries the total. Presses made while the link is lost are not counted.

## Uno timing

All values are provisional until bench validation.

| Constant | Value | Effect |
|---|---|---|
| Startup grace | 10 s | Link-lost is not raised until 10 s after boot without any state frame |
| Heartbeat timeout | 5 s | Link-lost when no new state frame for more than 5 s; latched until the next accepted frame, so a `millis()` wrap cannot clear it |
| Local silence | 60 s | A long press while link-lost silences it once; further presses do not extend it; it re-arms automatically while link-lost persists and is cancelled when the link returns |
| Debounce, long press, stuck | 50 ms, 2 s, 30 s | Button classification |
| Hardware watchdog | 2 s | Fed once per `loop()`; a hung loop resets the Uno |

The watchdog-test build (`uno-watchdog-test`, never for deployment) additionally
prints `GGTEST WATCHDOG-HANG FIRMWARE NOT FOR USE` lines, which the Pi rejects as
malformed, and hangs 20 s after boot. See [BENCH.md](BENCH.md).

While link-lost, the Uno demands its link-lost alarm (`fault = true`, `glucose = -`)
independently of the Pi. Hardware outputs are not implemented; the demand is computed
and tested but drives no pin.
