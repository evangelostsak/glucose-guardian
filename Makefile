SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
BACKEND := $(ROOT)/backend
FIRMWARE := $(ROOT)/firmware/arduino-uno

PYTHON := python3
VENV := $(ROOT)/.venv
CONFIG := $(ROOT)/config/example.toml
PI_HOST :=
PI_USER :=
PI_DIR := glucose-guardian
SERIAL_PORT :=
VARIANT :=
CONFIRM :=
CREDENTIAL :=
SIMULATOR_PORT := 8765
SHARE_SCENARIO := normal
DEXCOM_CONFIG := $(ROOT)/config/local.dexcom.toml
STATE_DIR := $(HOME)/glucose-guardian-state
CREDENTIALS_DIR := /etc/glucose-guardian/credentials
PLATFORMIO_VERSION := 6.1.18

VENV_DIR := $(abspath $(VENV))
VPY := $(VENV_DIR)/bin/python
CONFIG_FILE := $(abspath $(CONFIG))
DEXCOM_CONFIG_FILE := $(abspath $(DEXCOM_CONFIG))
CREDENTIAL_USERNAME := $(CREDENTIALS_DIR)/dexcom-username.cred
CREDENTIAL_PASSWORD := $(CREDENTIALS_DIR)/dexcom-password.cred
PIO_PATH := PATH="$(VENV_DIR)/bin:$$PATH"
PI_TARGET = $(PI_USER)@$(PI_HOST)
BENCH_ARGS = -C $(ROOT)/bench VENV=$(VENV_DIR) PYTHON=$(VPY)
SYNC_SOURCES := $(addprefix $(ROOT)/,Makefile README.md backend bench config deployment docs)
SYNC_EXCLUDES := --exclude .venv --exclude __pycache__ --exclude '*.egg-info' \
	--exclude .pytest_cache --exclude .mypy_cache --exclude .ruff_cache \
	--exclude 'config/local.*' --exclude bench/reports --exclude 'bench/bench-diagnostics-*.tar.gz'

require = $(if $(strip $($(1))),,$(error $(1) is required, for example $(1)=$(2)))
need_tool = command -v $(1) >/dev/null || { echo "$(1) not found: $(2)" >&2; exit 2; }
need_venv = test -x $(VPY) || { echo "no virtual environment at $(VENV_DIR): run make setup (laptop) or make bench-setup (Pi)" >&2; exit 2; }
need_module = $(VPY) -c 'import $(1)' 2>/dev/null || { echo "$(1) is not installed in $(VENV_DIR): run make setup" >&2; exit 2; }
need_pio = $(PIO_PATH) command -v pio >/dev/null || { echo "pio not found in $(VENV_DIR)/bin or on PATH: run make setup-firmware" >&2; exit 2; }
need_tty = test -t 0 || { echo "$(1) needs an interactive terminal; nothing was done" >&2; exit 2; }
confirm = read -r -p '$(1) ' answer && test "$$answer" = '$(2)' || { echo "cancelled: nothing was done" >&2; exit 1; }
STATE_PATH = $(VPY) -m glucose_guardian.share_state --config $(DEXCOM_CONFIG_FILE) path
need_dexcom_config = test -f $(DEXCOM_CONFIG_FILE) || { echo "$(DEXCOM_CONFIG_FILE) not found: run make dexcom-config" >&2; exit 2; }
check_state_dir = $(if $(filter-out file,$(origin STATE_DIR)),state=$$($(STATE_PATH)) || exit 2; \
	[ "$$(dirname "$$state")" = '$(abspath $(STATE_DIR))' ] || { echo "STATE_DIR=$(abspath $(STATE_DIR)) does not match $(DEXCOM_CONFIG_FILE) (state in $$(dirname "$$state")); the state file always comes from DEXCOM_CONFIG" >&2; exit 2; },true)
need_credentials = sudo test -f $(CREDENTIAL_USERNAME) && sudo test -f $(CREDENTIAL_PASSWORD) || { echo "credentials missing in $(CREDENTIALS_DIR): run make dexcom-credentials" >&2; exit 2; }
validate_credentials = sudo systemd-run --pipe --wait --uid=$$(id -u) --gid=$$(id -g) \
	-p LoadCredentialEncrypted=dexcom-username:$(1) -p LoadCredentialEncrypted=dexcom-password:$(2) \
	$(VPY) -m glucose_guardian.credentials
check_pi = [[ '$(PI_USER)' =~ ^[A-Za-z0-9._-]+$$ && '$(PI_HOST)' =~ ^[A-Za-z0-9._-]+$$ ]] || { echo "PI_USER and PI_HOST may contain only letters, digits, '.', '_' and '-'" >&2; exit 2; }; \
	[[ '$(PI_DIR)' =~ ^[A-Za-z0-9_][A-Za-z0-9._/-]*$$ && '$(PI_DIR)' != *..* ]] || { echo "PI_DIR must be a plain directory under the Pi user's home, not '$(PI_DIR)'" >&2; exit 2; }

.PHONY: help setup setup-firmware lint typecheck test check \
	firmware-build firmware-test firmware-flash \
	bench-setup bench-discover bench-bootloader bench-run bench-run-interactive bench-watchdog bench-collect \
	simulator simulate display-preview \
	status pi-sync pi-setup pi-status \
	dexcom-config dexcom-credentials dexcom-credentials-check dexcom-credentials-rotate \
	dexcom-status dexcom-check \
	clean

##@ Help
help: ## Show the targets, where each runs, and the variables
	@echo "Glucose Guardian. [laptop]: development machine. [Pi]: Raspberry Pi with the Uno on USB."
	@awk 'BEGIN { FS = ":.*## " } /^##@ / { printf "\n%s\n", substr($$0, 5) } /^[a-z][a-z0-9-]*:.*## / { printf "  %-26s %s\n", $$1, $$2 }' $(ROOT)/Makefile
	@printf '\nVariables (current value; override on the command line)\n'
	@printf '  %-16s %s\n' \
		PYTHON '$(PYTHON)' VENV '$(VENV_DIR)' CONFIG '$(CONFIG_FILE)' \
		PI_USER '$(if $(PI_USER),$(PI_USER),(not set; required by pi-*))' \
		PI_HOST '$(if $(PI_HOST),$(PI_HOST),(not set; required by pi-*))' \
		PI_DIR '$(PI_DIR) (relative to the Pi user'"'"'s home)' \
		SERIAL_PORT '$(if $(SERIAL_PORT),$(SERIAL_PORT),(not set; a /dev/serial/by-id/... link; required by bench and flash targets))' \
		VARIANT '$(if $(VARIANT),$(VARIANT),(not set; production or watchdog-test; required by firmware-flash))' \
		SIMULATOR_PORT '$(SIMULATOR_PORT)' SHARE_SCENARIO '$(SHARE_SCENARIO)' \
		DEXCOM_CONFIG '$(DEXCOM_CONFIG_FILE)' \
		STATE_DIR '$(abspath $(STATE_DIR)) (only for creating DEXCOM_CONFIG; the state file always comes from it)' \
		CREDENTIALS_DIR '$(CREDENTIALS_DIR)' \
		CREDENTIAL '$(if $(CREDENTIAL),$(CREDENTIAL),(not set; username or password; required by dexcom-credentials-rotate))'

##@ Development [laptop]
$(VPY):
	@$(call need_tool,$(PYTHON),install Python 3.11 or newer or pass PYTHON=/path/to/python3)
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "$(PYTHON) is older than Python 3.11" >&2; exit 2; }
	$(PYTHON) -m venv $(VENV_DIR)
	$(VPY) -m pip install --upgrade pip

setup: $(VPY) ## Create VENV and install the backend with its test and lint tools
	$(VPY) -m pip install -e '$(BACKEND)[test,lint]'

setup-firmware: $(VPY) ## Install the pinned PlatformIO into VENV
	$(VPY) -m pip install 'platformio==$(PLATFORMIO_VERSION)'

lint: ## Ruff on the backend sources and tests
	@$(need_venv)
	@$(call need_module,ruff)
	cd $(BACKEND) && $(VPY) -m ruff check src tests

typecheck: ## Strict mypy on the backend sources
	@$(need_venv)
	@$(call need_module,mypy)
	cd $(BACKEND) && $(VPY) -m mypy src

test: ## Backend tests (synthetic data only; never contacts Dexcom)
	@$(need_venv)
	@$(call need_module,pytest)
	cd $(BACKEND) && $(VPY) -m pytest

check: lint typecheck test firmware-build firmware-test ## Everything CI runs

##@ Firmware
firmware-build: ## [laptop] Compile both images and stage them with SHA256SUMS in bench/out (never uploads)
	@$(need_pio)
	$(PIO_PATH) $(MAKE) -C $(ROOT)/bench build

firmware-test: ## [laptop] Firmware unit tests on the host (PlatformIO native env)
	@$(need_pio)
	$(PIO_PATH) pio test --project-dir $(FIRMWARE) -e native

firmware-flash: ## [Pi] Write a staged image: SERIAL_PORT=... VARIANT=production|watchdog-test (typed confirmation)
	$(call require,SERIAL_PORT,/dev/serial/by-id/...)
	$(call require,VARIANT,production)
	@case '$(VARIANT)' in production|watchdog-test) ;; *) echo "VARIANT must be production or watchdog-test" >&2; exit 2 ;; esac
	@$(call need_tty,firmware-flash)
	@test -f $(ROOT)/bench/out/SHA256SUMS || { echo "no staged images in bench/out: run make firmware-build on the laptop, then make pi-sync" >&2; exit 2; }
	@echo "This writes the $(VARIANT) image from bench/out to $(SERIAL_PORT)."
	@if [ '$(VARIANT)' = watchdog-test ]; then echo "The watchdog-test image hangs on purpose and must never stay installed. The bench also needs CONFIRM=watchdog-test and a fresh make bench-bootloader."; fi
	@$(call confirm,Type "flash $(VARIANT)" to continue:,flash $(VARIANT))
	$(MAKE) $(BENCH_ARGS) flash DEVICE=$(SERIAL_PORT) VARIANT=$(VARIANT) CONFIRM=$(CONFIRM)

##@ Bench [Pi] (USB-only Uno validation; see docs/BENCH.md)
bench-setup: ## Install avrdude, usbutils and python3-venv, create VENV, install the backend (sudo apt-get)
	$(MAKE) $(BENCH_ARGS) setup

bench-discover: ## List USB serial devices with serial numbers redacted
	@$(need_venv)
	$(MAKE) $(BENCH_ARGS) discover

bench-bootloader: ## Read-only bootloader check and authorization record: SERIAL_PORT=...
	$(call require,SERIAL_PORT,/dev/serial/by-id/...)
	@$(need_venv)
	$(MAKE) $(BENCH_ARGS) bootloader DEVICE=$(SERIAL_PORT)

bench-run: ## Automated bench suite against the installed firmware: SERIAL_PORT=...
	$(call require,SERIAL_PORT,/dev/serial/by-id/...)
	@$(need_venv)
	$(MAKE) $(BENCH_ARGS) run DEVICE=$(SERIAL_PORT)

bench-run-interactive: ## Bench suite with cable unplug and replug steps: SERIAL_PORT=...
	$(call require,SERIAL_PORT,/dev/serial/by-id/...)
	@$(need_venv)
	$(MAKE) $(BENCH_ARGS) run-interactive DEVICE=$(SERIAL_PORT)

bench-watchdog: ## Watchdog test; refuses unless the last flash record is a verified watchdog-test flash: SERIAL_PORT=...
	$(call require,SERIAL_PORT,/dev/serial/by-id/...)
	@$(need_venv)
	$(MAKE) $(BENCH_ARGS) watchdog DEVICE=$(SERIAL_PORT)

bench-collect: ## Pack bench reports and host diagnostics into a tarball in bench/
	$(MAKE) $(BENCH_ARGS) collect

##@ Simulation [laptop] (synthetic data; never contacts Dexcom)
simulator: ## Synthetic Dexcom Share server on 127.0.0.1:SIMULATOR_PORT with SHARE_SCENARIO; Ctrl+C stops
	@$(need_venv)
	$(VPY) -m glucose_guardian.share_simulator --port $(SIMULATOR_PORT) --scenario $(SHARE_SCENARIO)

simulate: ## Run the service with CONFIG until Ctrl+C; outputs stay off and systemd credentials are withheld
	@$(need_venv)
	@test -f $(CONFIG_FILE) || { echo "$(CONFIG_FILE) not found" >&2; exit 2; }
	env -u CREDENTIALS_DIRECTORY $(VPY) -m glucose_guardian.service --config $(CONFIG_FILE)

display-preview: ## Render synthetic dashboard PNGs with CONFIG's [display] font into build/display-preview
	@$(need_venv)
	@test -f $(CONFIG_FILE) || { echo "$(CONFIG_FILE) not found" >&2; exit 2; }
	$(VPY) -m glucose_guardian.preview --config $(CONFIG_FILE) --out $(ROOT)/build/display-preview

##@ Raspberry Pi
status: ## [Pi or laptop] Read-only host report: OS, Python, clock sync, venv, tools, staged images, Dexcom files
	@if [ -r /etc/os-release ]; then . /etc/os-release; echo "os: $$PRETTY_NAME"; else echo "os: unknown"; fi
	@echo "kernel: $$(uname -srm)"
	@echo "python3: $$(python3 --version 2>&1 || echo missing)"
	@echo "clock synchronized: $$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
	@if [ -x $(VPY) ]; then echo "venv: $(VENV_DIR)"; else echo "venv: missing (make setup or make bench-setup)"; fi
	@for tool in make rsync avrdude systemd-run systemd-creds systemd-ask-password; do echo "$$tool: $$(command -v $$tool || echo missing)"; done
	@if [ -f $(ROOT)/bench/out/SHA256SUMS ]; then echo "staged images: build $$(cat $(ROOT)/bench/out/BUILD-COMMIT)"; else echo "staged images: none"; fi
	@if [ -f $(DEXCOM_CONFIG_FILE) ]; then echo "dexcom config: $(DEXCOM_CONFIG_FILE)"; else echo "dexcom config: missing (make dexcom-config)"; fi
	@if [ -x $(VPY) ] && [ -f $(DEXCOM_CONFIG_FILE) ] && state=$$($(STATE_PATH) 2>/dev/null); then \
		if [ -f "$$state" ]; then echo "dexcom state: $$state"; else echo "dexcom state: none yet ($$state)"; fi; \
	else echo "dexcom state: unknown (needs the venv and a loadable DEXCOM_CONFIG)"; fi
	@if ! sudo -n true 2>/dev/null; then echo "dexcom credentials: not checked (sudo needs a password)"; \
	elif sudo -n test -f $(CREDENTIAL_USERNAME) && sudo -n test -f $(CREDENTIAL_PASSWORD); then echo "dexcom credentials: provisioned in $(CREDENTIALS_DIR)"; \
	else echo "dexcom credentials: missing (make dexcom-credentials)"; fi

pi-sync: ## [laptop] Copy Makefile, backend, bench (with staged images), config, deployment and docs to the Pi
	$(call require,PI_USER,pi)
	$(call require,PI_HOST,raspberrypi.local)
	@$(check_pi)
	@$(call need_tool,rsync,install rsync)
	@$(call need_tool,ssh,install an OpenSSH client)
	ssh $(PI_TARGET) mkdir -p $(PI_DIR)
	rsync -a --delete $(SYNC_EXCLUDES) $(SYNC_SOURCES) $(PI_TARGET):$(PI_DIR)/

pi-setup: ## [laptop] Run make bench-setup on the Pi over SSH (asks for the Pi user's sudo password)
	$(call require,PI_USER,pi)
	$(call require,PI_HOST,raspberrypi.local)
	@$(check_pi)
	@$(call need_tool,ssh,install an OpenSSH client)
	ssh -t $(PI_TARGET) 'cd $(PI_DIR) && make bench-setup'

pi-status: ## [laptop] Run make status on the Pi over SSH (read-only)
	$(call require,PI_USER,pi)
	$(call require,PI_HOST,raspberrypi.local)
	@$(check_pi)
	@$(call need_tool,ssh,install an OpenSSH client)
	ssh $(PI_TARGET) 'cd $(PI_DIR) && make status'

##@ Dexcom Share [Pi] (read-only; see docs/DEXCOM_SHARE.md)
dexcom-config: ## Create DEXCOM_CONFIG with its state in STATE_DIR (0700); an existing config is kept and must match an explicit STATE_DIR
	@$(need_venv)
	@if [ -e $(DEXCOM_CONFIG_FILE) ]; then echo "$(DEXCOM_CONFIG_FILE) exists; kept"; \
	else install -d -m 0700 $(abspath $(STATE_DIR)); \
		sed 's|/var/lib/glucose-guardian|$(abspath $(STATE_DIR))|' $(ROOT)/config/example-dexcom-share.toml > $(DEXCOM_CONFIG_FILE); \
		echo "wrote $(DEXCOM_CONFIG_FILE)"; fi
	@$(check_state_dir)
	@state=$$($(STATE_PATH)); install -d -m 0700 "$$(dirname "$$state")"; echo "state file: $$state"

dexcom-credentials: ## Encrypt the publisher username and password with systemd-creds from a hidden prompt; existing files are kept
	@$(call need_tool,systemd-creds,needs systemd 250 or newer)
	@$(call need_tool,systemd-ask-password,needs systemd)
	@$(call need_tty,dexcom-credentials)
	sudo -v
	sudo install -d -m 0700 $(CREDENTIALS_DIR)
	@for name in username password; do \
		file=$(CREDENTIALS_DIR)/dexcom-$$name.cred; \
		if sudo test -f "$$file"; then echo "$$file exists; kept (replace it with make dexcom-credentials-rotate CREDENTIAL=$$name)"; continue; fi; \
		if systemd-ask-password -n "Dexcom Share $$name:" | sudo systemd-creds encrypt --name=dexcom-$$name - "$$file.new"; then \
			sudo chmod 0600 "$$file.new"; sudo mv "$$file.new" "$$file"; echo "$$file written"; \
		else sudo rm -f "$$file.new"; echo "encrypting dexcom-$$name failed; nothing was kept" >&2; exit 1; fi; \
	done

dexcom-credentials-check: ## Decrypt both credentials through systemd and check their format; never contacts Dexcom
	@$(need_venv)
	@$(call need_tool,systemd-run,needs systemd)
	sudo -v
	@$(need_credentials)
	$(call validate_credentials,$(CREDENTIAL_USERNAME),$(CREDENTIAL_PASSWORD))

dexcom-credentials-rotate: ## Replace CREDENTIAL=username|password: typed twice, encrypted, validated, then swapped in (typed confirmation)
	$(call require,CREDENTIAL,password)
	@case '$(CREDENTIAL)' in username|password) ;; *) echo "CREDENTIAL must be username or password" >&2; exit 2 ;; esac
	@$(need_venv)
	@$(call need_tool,systemd-creds,needs systemd 250 or newer)
	@$(call need_tool,systemd-ask-password,needs systemd)
	@$(call need_tool,systemd-run,needs systemd)
	@$(call need_tty,dexcom-credentials-rotate)
	sudo -v
	@$(need_credentials)
	@echo "This replaces $(CREDENTIALS_DIR)/dexcom-$(CREDENTIAL).cred. The current file stays in place until the new"
	@echo "value has been typed twice, encrypted and validated through systemd. Nothing is sent to Dexcom."
	@$(call confirm,Type "replace dexcom-$(CREDENTIAL)" to continue:,replace dexcom-$(CREDENTIAL))
	@current=$(CREDENTIALS_DIR)/dexcom-$(CREDENTIAL).cred; new="$$current.new"; \
	trap 'sudo rm -f "$$new"' EXIT; \
	first=$$(systemd-ask-password -n "New Dexcom Share $(CREDENTIAL):"); \
	second=$$(systemd-ask-password -n "New Dexcom Share $(CREDENTIAL) again:"); \
	if [ "$$first" != "$$second" ]; then echo "the two entries differ; dexcom-$(CREDENTIAL) is unchanged" >&2; exit 1; fi; \
	unset second; \
	printf '%s' "$$first" | sudo systemd-creds encrypt --name=dexcom-$(CREDENTIAL) - "$$new"; \
	unset first; \
	sudo chmod 0600 "$$new"; \
	if [ '$(CREDENTIAL)' = username ]; then user="$$new"; pass=$(CREDENTIAL_PASSWORD); else user=$(CREDENTIAL_USERNAME); pass="$$new"; fi; \
	$(call validate_credentials,"$$user","$$pass") \
		|| { echo "validation failed; dexcom-$(CREDENTIAL) is unchanged" >&2; exit 1; }; \
	sudo mv -f "$$new" "$$current"; \
	echo "dexcom-$(CREDENTIAL) replaced. If make dexcom-status shows a block, follow 'Recovering from a blocked login' in docs/DEXCOM_SHARE.md."

dexcom-status: ## Show the login state from the state file named in DEXCOM_CONFIG; never contacts Dexcom
	@$(need_venv)
	@$(need_dexcom_config)
	@$(check_state_dir)
	$(VPY) -m glucose_guardian.share_state --config $(DEXCOM_CONFIG_FILE) status

dexcom-check: ## ONE read-only connectivity check (authenticate, login, read) after typed confirmation; never retried
	@$(need_venv)
	@$(need_dexcom_config)
	@$(check_state_dir)
	@$(call need_tool,systemd-run,needs systemd)
	@$(call need_tty,dexcom-check)
	sudo -v
	@$(need_credentials)
	@$(VPY) -m glucose_guardian.share_state --config $(DEXCOM_CONFIG_FILE) status
	@echo "This sends ONE authenticate, ONE login and ONE read request to Dexcom Share."
	@echo "It only reads; it changes nothing on the account and is never retried."
	@echo "A rejected login counts toward Dexcom's lockout and blocks automatic login."
	@$(call confirm,Type "contact dexcom" to send these requests:,contact dexcom)
	sudo systemd-run --pipe --wait --uid=$$(id -u) --gid=$$(id -g) \
		-p LoadCredentialEncrypted=dexcom-username:$(CREDENTIAL_USERNAME) \
		-p LoadCredentialEncrypted=dexcom-password:$(CREDENTIAL_PASSWORD) \
		-p WorkingDirectory=$(ROOT) \
		$(VPY) -m glucose_guardian.share_check --config $(DEXCOM_CONFIG_FILE) \
		|| { echo "check failed: do not retry; send the output above for analysis" >&2; exit 1; }

##@ Maintenance
clean: ## Remove caches and firmware build output; keeps VENV, config/local.*, bench/out, bench/reports and Dexcom state
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache $(BACKEND)/src/*.egg-info $(FIRMWARE)/.pio
	find $(BACKEND) -type d -name __pycache__ -prune -exec rm -rf {} +
