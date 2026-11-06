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

Button pull configuration, debounce, stuck-button behavior, acknowledgement limits,
and disconnect handling must be designed and tested once wiring is selected.
Firmware currently assigns no pins and cannot energize a siren or bed shaker.
Do not connect unreviewed 12 V loads to the Uno or Pi.
