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
.venv/bin/python -m pip install -e './backend[test,lint]'
cd backend
../.venv/bin/python -m ruff check src tests
../.venv/bin/python -m mypy src
../.venv/bin/python -m pytest
../.venv/bin/python -m glucose_guardian
cp ../config/example.toml ../config/local.toml
../.venv/bin/python -m glucose_guardian.service --config ../config/local.toml
```

The demo prints four synthetic readings and exits. The service runs the simulated
runtime with the scenario named in `[simulation]` until SIGTERM or Ctrl+C; it logs
alarm and fault changes, never glucose values. Neither accesses Dexcom, opens serial
ports or drives outputs. `config/local.toml` is git-ignored. All example thresholds
are synthetic test values, not clinical recommendations.

```sh
python3 -m pip install platformio==6.1.18
pio run --project-dir firmware/arduino-uno
```

CI runs ruff, mypy and pytest on Python 3.11 and 3.13, and compiles Uno firmware.
No hardware test is implied.

## Layout

- `backend/`: Python package: alarm policy, simulated runtime, scenarios,
  display/transport boundaries and doubles, and pytest tests.
- `firmware/arduino-uno/`: PlatformIO bench protocol and heartbeat detection;
  outputs disabled and button handling deferred.
- `config/example.toml`: secret-free example settings.
- `deployment/systemd/`: inactive, uninstalled service template (M6).
- `docs/`: [architecture](docs/ARCHITECTURE.md), [safety](docs/SAFETY.md),
  and [electrical interface](docs/HARDWARE.md).

Before hardware use, implement and validate the serial adapter, physical button,
actuator drivers, GUI, Dexcom acquisition, and systemd supervision. No real credentials or patient data belong in this repository. Nothing
in this scaffold has been deployed to a Raspberry Pi.

## License

Glucose Guardian is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later version
(`GPL-3.0-or-later`). It is distributed WITHOUT ANY WARRANTY; without even the
implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
[LICENSE](LICENSE).
