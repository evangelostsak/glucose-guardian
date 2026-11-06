import pytest
from harness import EXAMPLE, ROOT
from harness import write_config as write

from glucose_guardian.config import ConfigError, load_config


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
