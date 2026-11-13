import re
from pathlib import Path

import pytest
from harness import EXAMPLE, ROOT
from harness import write_config as write
from share_harness import DEXCOM_EXAMPLE

from glucose_guardian.config import ConfigError, DexcomSettings, load_config


def test_example_config_loads():
    config = load_config(EXAMPLE)
    assert (config.provider, config.scenario) == ("simulated", "normal")
    assert config.policy.fault_acknowledgement_s == 60
    assert config.serial is None
    assert config.runtime.fetch_timeout_s == 10


def test_hardware_table_is_optional_and_outputs_stay_disabled(tmp_path):
    config = load_config(write(tmp_path, ("[hardware]\noutputs_enabled = false\n", "")))
    assert config.scenario == "normal"


@pytest.mark.parametrize("replacement,message", [
    (("low_mg_dl = 70\n", ""), "missing ['low_mg_dl']"),
    (("tick_s = 1\n", ""), "missing ['tick_s']"),
    (('scenario = "normal"', 'scenario = "normal"\npassword = "x"'), "unknown keys ['password']"),
    (('provider = "simulated"', 'provider = "simulated"\n[dexcom]\nusername = "x"'),
     "unknown keys ['dexcom']"),
    (('provider = "simulated"', 'provider = "dexcom"'), "provider must be one of"),
    (('scenario = "normal"', 'scenario = "nightmare"'), "scenario must be one of"),
    (("outputs_enabled = false", "outputs_enabled = true"), "outputs_enabled must be false"),
    (("outputs_enabled = false", 'outputs_enabled = "no"'), "outputs_enabled must be false"),
    (("fault_acknowledgement_s = 60", "fault_acknowledgement_s = 901"),
     "acknowledgement durations must not exceed 900"),
    (("low_mg_dl = 70", "low_mg_dl = 50"), "[alarm] thresholds"),
    (("tick_s = 1", "tick_s = true"), "tick_s must be a positive number"),
    (("tick_s = 1", 'tick_s = "1"'), "tick_s must be a positive number"),
    (("tick_s = 1", "tick_s = nan"), "tick_s must be a positive number"),
    (("tick_s = 1", "tick_s = -1"), "tick_s must be a positive number"),
    (("tick_s = 1", "tick_s = 5"), "tick_s must be below"),
    (("fetch_timeout_s = 10", "fetch_timeout_s = 61"), "fetch_timeout_s must not exceed"),
    (("fetch_interval_s = 60", "fetch_interval_s = 600"), "fetch_interval_s must be below"),
    (("clock_check_timeout_s = 5", "clock_check_timeout_s = 61"),
     "clock_check_timeout_s must not exceed"),
])
def test_invalid_config_names_the_problem(tmp_path, replacement, message):
    path = write(tmp_path, replacement)
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert message in str(error.value)
    assert str(path) in str(error.value)


def test_section_must_be_a_table(tmp_path):
    runtime = ("[runtime]\ntick_s = 1\nfetch_interval_s = 60\nfetch_timeout_s = 10\n"
               "clock_check_interval_s = 60\nclock_check_timeout_s = 5\n")
    path = write(tmp_path, (runtime, ""), ('provider = "simulated"',
                                           'provider = "simulated"\nruntime = 1'))
    with pytest.raises(ConfigError, match="runtime must be a table"):
        load_config(path)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_config(tmp_path / "local.toml")


def test_invalid_toml(tmp_path):
    path = tmp_path / "local.toml"
    path.write_text("provider = \n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


def test_local_config_is_git_ignored():
    assert "config/local.*" in (ROOT / ".gitignore").read_text().splitlines()


SIMULATED = '[transport]\nkind = "simulated"\n'
SERIAL = ('[transport]\nkind = "serial"\ndevice = "/dev/serial/by-id/uno"\n'
          'reconnect_initial_s = 1\nreconnect_max_s = 30\n')


def test_serial_transport_settings(tmp_path):
    config = load_config(write(tmp_path, (SIMULATED, SERIAL)))
    assert config.serial is not None
    assert (config.serial.device, config.serial.reconnect_initial_s,
            config.serial.reconnect_max_s) == ("/dev/serial/by-id/uno", 1, 30)


@pytest.mark.parametrize("transport,message", [
    ("[transport]\n", "kind must be one of"),
    ('[transport]\nkind = "usb"\n', "kind must be one of"),
    ('[transport]\nkind = "simulated"\ndevice = "/dev/x"\n', "unknown keys ['device']"),
    (SERIAL.replace('device = "/dev/serial/by-id/uno"\n', ""), "missing ['device']"),
    (SERIAL.replace("reconnect_max_s = 30\n", ""), "missing ['reconnect_max_s']"),
    (SERIAL.replace('"/dev/serial/by-id/uno"', '""'), "device must be a non-empty string"),
    (SERIAL.replace('"/dev/serial/by-id/uno"', "5"), "device must be a non-empty string"),
    (SERIAL.replace("reconnect_initial_s = 1", "reconnect_initial_s = 0"),
     "reconnect_initial_s must be a positive number"),
    (SERIAL.replace("reconnect_initial_s = 1", "reconnect_initial_s = 60"),
     "reconnect_initial_s must not exceed reconnect_max_s"),
])
def test_invalid_transport_settings(tmp_path, transport, message):
    with pytest.raises(ConfigError) as error:
        load_config(write(tmp_path, (SIMULATED, transport)))
    assert message in str(error.value)


def test_transport_table_is_required(tmp_path):
    with pytest.raises(ConfigError, match=r"missing \['transport'\]"):
        load_config(write(tmp_path, (SIMULATED, "")))


def write_dexcom(tmp_path, *replacements):
    text = DEXCOM_EXAMPLE.read_text()
    for old, new in replacements:
        assert old in text
        text = text.replace(old, new)
    path = tmp_path / "local-dexcom.toml"
    path.write_text(text)
    return path


def test_dexcom_example_config_loads():
    config = load_config(DEXCOM_EXAMPLE)
    assert (config.provider, config.scenario) == ("dexcom_share", None)
    assert config.dexcom == DexcomSettings(
        "ous", None, 300.0, 120.0, 1800.0, 5.0,
        Path("/var/lib/glucose-guardian/dexcom-share-state.json"))
    assert (config.runtime.fetch_interval_s, config.policy.max_age_s) == (60, 600)


def test_simulator_server_takes_a_loopback_port(tmp_path):
    config = load_config(write_dexcom(tmp_path, ('server = "ous"',
                                                 'server = "simulator"\nsimulator_port = 8443')))
    assert config.dexcom is not None
    assert (config.dexcom.server, config.dexcom.simulator_port) == ("simulator", 8443)


def without_dexcom_table() -> tuple[str, str]:
    text = DEXCOM_EXAMPLE.read_text()
    return text[text.index("[dexcom]"):text.index("[transport]")], ""


@pytest.mark.parametrize("replacement,message", [
    (('server = "ous"', 'server = "eu"'), "server must be one of"),
    (('server = "ous"', "server = 1"), "server must be one of"),
    (("login_min_interval_s = 300", "login_min_interval_s = 299"), "at least 300"),
    (("login_min_interval_s = 300", "login_min_interval_s = true"), "positive number"),
    (("rate_limit_initial_s = 120", "rate_limit_initial_s = 2000"),
     "must not exceed rate_limit_max_s"),
    (("request_timeout_s = 5", "request_timeout_s = 7"), "times 3 must not exceed"),
    (('state_file = "/var/lib', 'state_file = "var/lib'), "absolute path"),
    (('server = "ous"', 'server = "simulator"'), "missing ['simulator_port']"),
    (('server = "ous"', 'server = "ous"\nsimulator_port = 8443'),
     "unknown keys ['simulator_port']"),
    (('server = "ous"', 'server = "simulator"\nsimulator_port = 0'), "integer port"),
    (('server = "ous"', 'server = "simulator"\nsimulator_port = true'), "integer port"),
    (('server = "ous"', 'server = "ous"\npassword = "x"'), "unknown keys ['password']"),
    (('server = "ous"', 'server = "ous"\nusername = "x"'), "unknown keys ['username']"),
    (("[dexcom]", '[simulation]\nscenario = "normal"\n[dexcom]'),
     "unknown keys ['simulation']"),
    (without_dexcom_table(), "missing ['dexcom']"),
    (('provider = "dexcom_share"\n', ""), "missing ['provider']"),
    (('provider = "dexcom_share"', 'provider = ["dexcom_share"]'), "provider must be one of"),
])
def test_invalid_dexcom_config_names_the_problem(tmp_path, replacement, message):
    path = write_dexcom(tmp_path, replacement)
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(path)
