# Glucose Guardian

Open-source supplementary CGM display and nighttime alarm prototype for Dexcom G7
users. This is not a certified medical device. Keep official Dexcom and Omnipod
alert systems independent, enabled, and unchanged.

The target is a Raspberry Pi 3 Model B running Debian 13, an Arduino Uno over USB
serial, and a 5-inch 800×480 HDMI IPS display. External 12 V siren and bed shaker
require dedicated driver circuitry and backup power, which have not been selected.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e './backend[test]'
.venv/bin/python -m pytest backend/tests
.venv/bin/python -m glucose_guardian
```

The demo prints four synthetic readings and exits. It does not access Dexcom,
open serial ports, drive outputs, or run a persistent alarm service. The example
configuration is documentation for future runtime integration; it is not loaded.
All example thresholds are synthetic test values, not clinical recommendations.

```sh
python3 -m pip install platformio==6.1.18
pio run --project-dir firmware/arduino-uno
```

CI runs Python tests and compiles Uno firmware. No hardware test is implied.

## Layout

- `backend/`: Python package, simulated provider, deterministic alarm policy,
  display/transport boundaries, and pytest tests.
- `firmware/arduino-uno/`: PlatformIO bench protocol and heartbeat detection;
  outputs disabled and button handling deferred.
- `config/example.toml`: secret-free example settings.
- `deployment/systemd/`: inactive service template; runtime still to be built.
- `docs/`: [architecture](docs/architecture.md), [safety](docs/safety.md),
  and [electrical interface](docs/hardware.md).

Before hardware use, implement and validate the serial adapter, physical button,
actuator drivers, GUI, configuration loader, Dexcom acquisition, and supervised
runtime. No real credentials or patient data belong in this repository. Nothing
in this scaffold has been deployed to a Raspberry Pi.
