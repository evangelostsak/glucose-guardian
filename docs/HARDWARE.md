# Hardware and electrical interface

Confirmed: Raspberry Pi 3 Model B / Debian 13; Arduino Uno over USB serial;
5-inch HDMI IPS LCD at 800×480; external 12 V siren; external 12 V bed shaker;
physical acknowledgement button. Load switches and backup power remain unselected.

The Pi connects to the display over HDMI and the Uno over USB. The Uno will send
logic-level control signals to reviewed external driver circuits. Neither actuator
may be powered from a GPIO pin. No GPIO pin assignment, actuator current, driver
rating, button wiring, or power budget is approved by this document.

Before wiring, record measured actuator startup/steady currents and duty cycles,
switch and protection ratings, inductive transient suppression where applicable,
fusing, grounding/isolation, logic compatibility, USB backfeed prevention,
connectors, strain relief, enclosure, and power-domain behavior. Select and verify
backup power for required runtime and safe charging. Verify startup/reset default
states and distinguish the system-fault pattern from glucose alarms.

Button pull configuration, active level and disconnect handling must be designed and
tested once wiring is selected. Firmware currently assigns no pins: the button input
is fixed at "released", and the alarm demand is computed but cannot energize a siren
or bed shaker. Do not connect unreviewed 12 V loads to the Uno or Pi.

## Provisional firmware timing

Approved as starting values on 2026-11-06; all require bench validation.

| Constant | Value |
|---|---|
| Button debounce | 50 ms |
| Long press | 2 s |
| Stuck button | 30 s |
| Hardware watchdog | 2 × 1 s, interrupt then reset (`WDTO_1S` with `WDIE`) |
| Startup grace before link-lost | 10 s |
| Heartbeat timeout | 5 s |
| Local link-lost silence | 60 s, not extendable |

## Board behavior to account for

- Opening the USB serial port asserts DTR, which resets the Uno. The protocol treats
  this as an initial connection, and the 10 s grace keeps the Uno quiet while it
  reboots.
- The Uno increments a 16-bit boot counter in EEPROM address 0 at every start (one
  `eeprom_update_word` per boot, well inside the EEPROM's rated write endurance).
- The stock Uno bootloader, Optiboot 4.4 (version marker read on the bench board),
  clears MCUSR before the sketch runs. This is confirmed in the Arduino core's
  `optiboot.c` and its compiled listing (`in r24, MCUSR`, `out MCUSR, r1`, then
  `ldi r24, 0` in `appStart`). Every reset therefore reaches the sketch with empty
  flags. The firmware reports `W` from a watchdog-interrupt marker in `.noinit` RAM;
  any other reset reports `U`. The boot counter detects every reset. Owner-reported
  bench result before this marker (2026-11-12): two watchdog resets detected, cause
  `U`, `U`.
- After an external reset (DTR or the reset button), Optiboot 4.4 itself leaves
  through a watchdog reset. Its 1 s timeout, or 16 ms after a programming session,
  starts the sketch. Reset flags, even if a bootloader preserved them, would therefore
  show a watchdog reset after every DTR reset. The marker does not, because only the
  sketch's interrupt writes it.
- The Uno is the only audible path. If it is powered solely by the Pi's USB port, a
  Pi power failure silences both (audit H5); the power topology is not yet decided.

## Bench validation checklist (requires separate approval)

The production firmware passed the USB bench (owner-reported 2026-11-07). The
procedure and tooling are in
[BENCH.md](BENCH.md). Before outputs exist, a bench session with nothing connected to
any output should confirm:

1. The serial link to the Pi transport: frames, replies and reconnection after unplug.
2. A deliberately hung `loop()` (test build) resets the Uno in about 2 s, with
   interrupts enabled and with interrupts disabled. The boot counter increments and
   the Pi raises `controller_reset`.
3. The reset cause: `W` after an interrupts-enabled hang, `U` after an
   interrupts-disabled hang and after DTR resets.
4. RAM and stack headroom (static RAM is 390 of 2048 bytes) and loop timing under
   continuous traffic.
5. With the Uno stalled or unplugged mid-write, an incomplete write leads to discard,
   close and reconnect without blocking the Pi loop. In particular, `close()` must not
   wait out the USB-serial driver's closing delay (`cdc_acm` closing wait).
