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

Approved as starting values on 2026-11-06. The watchdog was confirmed on the USB bench
on 2026-11-12 (owner-reported). The other values act only on the button and the Uno's
link-lost alarm, which the bench cannot observe yet; they remain provisional.

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
- The stock Uno bootloader, Optiboot 4.4, clears MCUSR before the sketch runs. The
  bench board's boot section matches the stock Arduino image: SHA-256
  `e36d971b54b3336178813bf16cddf2658866367874587f7fc6c560fb629fbc74`, owner-reported
  from the bootloader check. This is confirmed in the Arduino core's
  `optiboot.c` and its compiled listing (`in r24, MCUSR`, `out MCUSR, r1`, then
  `ldi r24, 0` in `appStart`). Every reset therefore reaches the sketch with empty
  flags. The firmware reports `W` from a watchdog-interrupt marker in `.noinit` RAM;
  any other reset reports `U`. The boot counter detects every reset. Owner-reported
  bench results, 2026-11-12:
  - before the marker: two watchdog resets detected, cause `U`, `U`;
  - with it (report `watchdog-2026-11-12T101301Z`): `U` after the interrupts-disabled
    hang and `W` after the interrupts-enabled hang, both reset.
- After an external reset (DTR or the reset button), Optiboot 4.4 itself leaves
  through a watchdog reset. Its 1 s timeout, or 16 ms after a programming session,
  starts the sketch. Reset flags, even if a bootloader preserved them, would therefore
  show a watchdog reset after every DTR reset. The marker does not, because only the
  sketch's interrupt writes it.
- The Uno is the only audible path. If it is powered solely by the Pi's USB port, a
  Pi power failure silences both (audit H5); the power topology is not yet decided.

## USB-only bench validation status

The M3 firmware and serial link passed the USB-only bench on a Raspberry Pi 3B (Debian
13) and a genuine Uno R3 (USB `2341:0043`, Optiboot 4.4) with nothing connected to any
Uno pin. The results are owner-reported, and the reports stay on the Pi; the procedure
and the full record are in [BENCH.md](BENCH.md#validation-record).

1. **Serial link (passed 2026-11-07 and 2026-11-12):** frames, replies, rejection of
   invalid frames, a heartbeat pause, and unplug and reconnect with backoff.
2. **Watchdog recovery (passed 2026-11-12):** a deliberately hung `loop()` (test
   build) reset the Uno with interrupts enabled and with interrupts disabled. The boot
   counter incremented and the Pi flagged an unexpected reset.
3. **Reset cause (passed 2026-11-12):** `W` after the interrupts-enabled hang, `U`
   after the interrupts-disabled hang. The cause after DTR resets is expected to be
   `U` but was not itemized in the reported results.
4. **Traffic (passed 2026-11-12):** 500 back-to-back exchanges and 600 runtime ticks.
   Static RAM is 390 of 2048 bytes; stack headroom was not measured.
5. **Not separately exercised:** a write stalled mid-transfer. In particular, `close()`
   must not wait out the USB-serial driver's closing delay (`cdc_acm` closing wait);
   the interactive unplug test passed.

Outstanding physical checks, which need components, wiring review and approval:

- the Uno's link-lost alarm output;
- buzzer (siren) and bed-shaker electrical integration;
- the physical button;
- power-loss resilience and backup power;
- the Waveshare 800×480 display.
