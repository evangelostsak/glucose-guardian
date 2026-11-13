# Glucose Guardian

Open-source supplementary CGM display and nighttime alarm prototype for Dexcom G7
users. This is not a certified medical device. Keep official Dexcom and Omnipod
alert systems independent, enabled, and unchanged.

The target is a Raspberry Pi 3 Model B running Debian 13, an Arduino Uno over USB
serial, and a 5-inch 800×480 HDMI IPS display. External 12 V siren and bed shaker
require dedicated driver circuitry and backup power, which have not been selected.

## Development

The root `Makefile` is the entry point. `make` lists every target, where it runs
(laptop or Pi) and the variables it reads. The laptop needs Python 3.11 or newer and
GNU make.

```sh
make setup
make setup-firmware
make check
make simulate
```

| Target | What it does |
|---|---|
| `make setup` | Creates `.venv` and installs the backend with its test and lint tools |
| `make setup-firmware` | Installs the pinned PlatformIO (6.1.18) into `.venv` |
| `make lint`, `make typecheck`, `make test` | Ruff, strict mypy and pytest on the backend |
| `make firmware-build`, `make firmware-test` | Compiles both Uno images into `bench/out` (no upload) and runs the native firmware tests |
| `make check` | Everything CI runs |
| `make simulate` | Runs the simulated runtime from `CONFIG` (default `config/example.toml`) until Ctrl+C |
| `make simulator` | Runs the synthetic Dexcom Share server on `127.0.0.1:8765` |
| `make display-preview` | Renders synthetic dashboard PNGs into `build/display-preview` (needs `fonts-dejavu-core`) |

The service logs alarm and fault changes, never glucose values, and drives no outputs.
With `[display] kind = "snapshot"` it also writes a display snapshot each tick.
`python -m glucose_guardian.screen --config <file> --png <file>` renders that
snapshot as the 800×480 bedside dashboard; it has no network access, no credentials
and, until M5 PR B, no physical screen output.
`make simulate` withholds systemd credentials, so it can never reach Dexcom. The
service reads Dexcom Share only with `provider = "dexcom_share"` and systemd-provided
credentials (see [docs/DEXCOM_SHARE.md](docs/DEXCOM_SHARE.md)). It opens a serial port
only when `[transport] kind = "serial"` is configured. `python -m glucose_guardian`
prints four synthetic readings and exits. `config/local.*` is Git-ignored. All example
thresholds are synthetic test values, not clinical recommendations.

On the Raspberry Pi, the bench and Dexcom targets run in the tree that `make pi-sync
PI_USER=... PI_HOST=...` copies to `~/glucose-guardian`. See
[docs/BENCH.md](docs/BENCH.md) and [docs/DEXCOM_SHARE.md](docs/DEXCOM_SHARE.md).
These two targets ask for a typed confirmation and never run as part of another
target:

- `make firmware-flash`, the only target that writes firmware;
- `make dexcom-check`, the only target that contacts Dexcom.

CI runs `make lint typecheck test` on Python 3.11 and 3.13 and `make firmware-build
firmware-test`. CI runs no hardware test and never contacts Dexcom.

## Layout

- `backend/`: Python package: alarm policy, runtime, scenarios, Dexcom Share
  acquisition with a synthetic simulator, display/transport boundaries and doubles,
  the display snapshot, screen model, Pillow renderer and display process, and pytest
  tests.
- `firmware/arduino-uno/`: PlatformIO firmware: protocol v1, link supervision,
  button classification and watchdog; no pins assigned and outputs disabled.
- `config/example.toml`, `config/example-dexcom-share.toml`: secret-free example
  settings (simulated and Dexcom Share acquisition).
- `Makefile`: root entry point for development, CI, firmware, bench, Pi and Dexcom
  targets (`make help`).
- `bench/`: Makefile for the USB-only Uno bench (build, deploy, flash, run, collect);
  see [docs/BENCH.md](docs/BENCH.md).
- `deployment/systemd/`: inactive, uninstalled service template; installation is
  planned with M5.
- `docs/`: [architecture](docs/ARCHITECTURE.md), [serial protocol](docs/PROTOCOL.md),
  [safety](docs/SAFETY.md), [electrical interface](docs/HARDWARE.md), and
  [Dexcom Share](docs/DEXCOM_SHARE.md).

The M3 firmware and the USB serial link passed the owner-run bench validation on a
Raspberry Pi 3B and a genuine Uno R3, including the watchdog test. The production
firmware was then restored and verified (see
[docs/BENCH.md](docs/BENCH.md#validation-record)). The read-only Dexcom Share
connectivity check passed on the Pi on 2026-11-13. Apart from that check, only the
bench harness has run on the Pi; the service has not been deployed. Nothing is
connected to the Uno's pins, so the siren, bed shaker, button, display and the alarm
system as a whole are not validated. Before hardware use, implement and validate
button wiring, actuator drivers, the GUI (M5,
[plan](docs/plans/2026-11-13-m5-hdmi-display.md)), continuous live Dexcom acquisition
and systemd supervision. No real credentials or patient data belong in this
repository.

## License

Glucose Guardian is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later version
(`GPL-3.0-or-later`). It is distributed WITHOUT ANY WARRANTY; without even the
implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
[LICENSE](LICENSE).
