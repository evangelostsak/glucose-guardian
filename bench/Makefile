SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.ONESHELL:

ROOT := $(abspath ..)
FIRMWARE := $(ROOT)/firmware/arduino-uno
OUT := out
REPORTS := reports
VENV := .venv
PYTHON := $(VENV)/bin/python
REMOTE_DIR := glucose-guardian-bench
PRODUCTION_HEX := $(OUT)/production.hex
WATCHDOG_HEX := $(OUT)/watchdog-test-DO-NOT-DEPLOY.hex
AUTHORIZATION := $(REPORTS)/bootloader-authorization.json
AVRDUDE = avrdude -p atmega328p -c arduino -b 115200 -P $(DEVICE)

require = $(if $($(1)),,$(error $(1) is required: $(2)))

.PHONY: help build deploy setup discover bootloader flash restore run run-interactive watchdog collect

help:
	@echo "Laptop: make build | make deploy PI=user@host"
	@echo "Pi:     make setup | make discover | make flash DEVICE=... VARIANT=production"
	@echo "        make run DEVICE=... | make run-interactive DEVICE=..."
	@echo "        make bootloader DEVICE=/dev/serial/by-id/..."
	@echo "        make flash DEVICE=... VARIANT=watchdog-test CONFIRM=watchdog-test"
	@echo "        make watchdog DEVICE=... | make restore DEVICE=... | make collect"

build:
	pio run -d $(FIRMWARE) -e uno -e uno-watchdog-test
	mkdir -p $(OUT)
	cp $(FIRMWARE)/.pio/build/uno/firmware.hex $(PRODUCTION_HEX)
	cp $(FIRMWARE)/.pio/build/uno-watchdog-test/firmware.hex $(WATCHDOG_HEX)
	cd $(OUT)
	sha256sum production.hex watchdog-test-DO-NOT-DEPLOY.hex > SHA256SUMS
	git -C $(ROOT) describe --always --dirty > BUILD-COMMIT
	cat SHA256SUMS BUILD-COMMIT

deploy:
	$(call require,PI,PI=user@raspberrypi.local)
	test -f $(PRODUCTION_HEX) || { echo "run make build first" >&2; exit 2; }
	ssh $(PI) mkdir -p $(REMOTE_DIR)
	rsync -a --delete --exclude .venv --exclude __pycache__ --exclude '*.egg-info' \
	  --exclude .pytest_cache --exclude reports $(ROOT)/backend $(ROOT)/bench $(PI):$(REMOTE_DIR)/

setup:
	sudo apt-get update
	sudo apt-get install -y python3-venv avrdude usbutils
	python3 -m venv $(VENV)
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e $(ROOT)/backend
	id -nG | tr ' ' '\n' | grep -qx dialout || \
	  echo "WARNING: run 'sudo usermod -aG dialout $$USER', then log out and back in"

discover:
	$(PYTHON) -m glucose_guardian.bench discover

bootloader:
	$(call require,DEVICE,DEVICE=/dev/serial/by-id/...)
	$(PYTHON) -m glucose_guardian.bench bootloader --device $(DEVICE) --build-dir $(OUT) \
	  --record $(AUTHORIZATION)

flash:
	$(call require,DEVICE,DEVICE=/dev/serial/by-id/...)
	$(call require,VARIANT,VARIANT=production or VARIANT=watchdog-test)
	case "$(VARIANT)" in
	  production) hex=$(PRODUCTION_HEX) ;;
	  watchdog-test)
	    [ "$(CONFIRM)" = watchdog-test ] || { echo "watchdog-test needs CONFIRM=watchdog-test" >&2; exit 2; }
	    $(PYTHON) -m glucose_guardian.bench authorize-watchdog --device $(DEVICE) \
	      --build-dir $(OUT) --record $(AUTHORIZATION)
	    hex=$(WATCHDOG_HEX) ;;
	  *) echo "unknown VARIANT $(VARIANT)" >&2; exit 2 ;;
	esac
	(cd $(OUT) && sha256sum --check --ignore-missing SHA256SUMS)
	mkdir -p $(REPORTS)
	stamp=$$(date -u +%Y%m%dT%H%M%SZ)
	set +e
	$(AVRDUDE) -D -U flash:w:$$hex:i 2>&1 | tee $(REPORTS)/flash-$$stamp.log
	code=$${PIPESTATUS[0]}
	set -e
	printf '{"variant": "%s", "hex": "%s", "sha256": "%s", "avrdude_exit_code": %d, "utc": "%s"}\n' \
	  "$(VARIANT)" "$$(basename $$hex)" "$$(sha256sum $$hex | cut -d' ' -f1)" "$$code" "$$stamp" \
	  > $(REPORTS)/flash-$$stamp.json
	cp $(REPORTS)/flash-$$stamp.json $(REPORTS)/last-flash.json
	echo "flash record: $(REPORTS)/flash-$$stamp.json (avrdude exit $$code)"
	exit $$code

restore:
	$(call require,DEVICE,DEVICE=/dev/serial/by-id/...)
	$(MAKE) flash DEVICE=$(DEVICE) VARIANT=production

run:
	$(call require,DEVICE,DEVICE=/dev/serial/by-id/...)
	$(PYTHON) -m glucose_guardian.bench run --device $(DEVICE) --report-dir $(REPORTS) \
	  --flash-record $(REPORTS)/last-flash.json

run-interactive:
	$(call require,DEVICE,DEVICE=/dev/serial/by-id/...)
	$(PYTHON) -m glucose_guardian.bench run --device $(DEVICE) --report-dir $(REPORTS) \
	  --flash-record $(REPORTS)/last-flash.json --interactive

watchdog:
	$(call require,DEVICE,DEVICE=/dev/serial/by-id/...)
	$(PYTHON) -m glucose_guardian.bench watchdog --device $(DEVICE) --report-dir $(REPORTS)

collect:
	stamp=$$(date -u +%Y%m%dT%H%M%SZ)
	dir=$(REPORTS)/diagnostics-$$stamp
	mkdir -p $$dir
	uname -srmv > $$dir/uname.txt
	cat /etc/os-release > $$dir/os-release.txt
	(lsusb || echo "lsusb unavailable") > $$dir/lsusb.txt
	(sudo -n dmesg 2>/dev/null || echo "dmesg unavailable without sudo") \
	  | grep -iE 'usb|tty|cdc_acm|ch34' | grep -viE 'serialnumber' | tail -n 300 \
	  > $$dir/dmesg-usb.txt || true
	(avrdude -? 2>&1 | head -n 3 || true) > $$dir/avrdude-version.txt
	cp $(OUT)/SHA256SUMS $(OUT)/BUILD-COMMIT $$dir/ 2>/dev/null || true
	tar -czf bench-diagnostics-$$stamp.tar.gz $(REPORTS)
	echo "bench-diagnostics-$$stamp.tar.gz"
