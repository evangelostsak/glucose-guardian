import pytest
from harness import EXAMPLE, ROOT
from harness import write_config as write

from glucose_guardian.config import ConfigError, load_config


def test_example_config_loads():
    config = load_config(EXAMPLE)
    assert (config.provider, config.scenario) == ("simulated", "normal")
    assert config.policy.acknowledgement_s == 60
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
    (("acknowledgement_s = 60", "acknowledgement_s = 901"), "[alarm] acknowledgement_s"),
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
