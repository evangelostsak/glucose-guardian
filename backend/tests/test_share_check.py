import json
import os
import re
from datetime import timedelta

import pytest
from harness import EXAMPLE
from share_harness import DEXCOM_EXAMPLE, START, synthetic

from glucose_guardian import share_check
from glucose_guardian.credentials import PASSWORD_FILE, USERNAME_FILE
from glucose_guardian.share_simulator import (
    SYNTHETIC_PASSWORD,
    SYNTHETIC_USERNAME,
    share_entry,
    static,
)

VALUE = 187


@pytest.fixture
def environment(tmp_path, simulator, monkeypatch):
    directory = tmp_path / "credentials"
    directory.mkdir(mode=0o700)
    for name, value in ((USERNAME_FILE, SYNTHETIC_USERNAME), (PASSWORD_FILE, SYNTHETIC_PASSWORD)):
        (directory / name).write_text(value + "\n")
        os.chmod(directory / name, 0o400)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(directory))
    text = DEXCOM_EXAMPLE.read_text().replace(
        'server = "ous"', f'server = "simulator"\nsimulator_port = {simulator.port}').replace(
        "/var/lib/glucose-guardian", str(tmp_path))
    config = tmp_path / "check.toml"
    config.write_text(text)
    simulator.source = static([synthetic(VALUE, START - timedelta(seconds=90), "SingleUp")])
    return config


def test_check_reports_structure_without_values_or_secrets(environment, simulator, capsys):
    assert share_check.main(["--config", str(environment)]) == share_check.EXIT_OK
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "parser: accepted" in output
    assert "keys ['DT', 'ST', 'Trend', 'Value', 'WT']" in output
    assert "Value is an integer from 39 to 401: yes" in output
    assert "DT minus WT +0 s" in output and "DT offset +0000" in output
    assert re.search(rf"\b{VALUE}\b", output) is None
    for secret in ("SingleUp", SYNTHETIC_USERNAME, SYNTHETIC_PASSWORD):
        assert secret not in output
    assert (simulator.requests["authenticate"], simulator.requests["read"]) == (1, 1)


def test_check_respects_the_persisted_login_interval(environment, simulator, capsys):
    assert share_check.main(["--config", str(environment)]) == share_check.EXIT_OK
    assert share_check.main(["--config", str(environment)]) == share_check.EXIT_FAILED
    assert "login_deferred" in capsys.readouterr().err
    assert simulator.requests["authenticate"] == 1


def test_check_respects_the_block_latch(environment, simulator, capsys):
    simulator.password = "rotated-elsewhere"
    assert share_check.main(["--config", str(environment)]) == share_check.EXIT_FAILED
    assert share_check.main(["--config", str(environment)]) == share_check.EXIT_FAILED
    error = capsys.readouterr().err
    assert "auth_blocked" in error and "AccountPasswordInvalid" in error
    assert simulator.requests["authenticate"] == 1


def test_check_describes_unexpected_shapes_without_values(environment, simulator, capsys):
    simulator.raw_entries = [share_entry(synthetic(VALUE, START, "Flat")) | {"Extra": 1},
                             {"WT": "Date(1)", "Value": 38, "Trend": "Sideways"}]
    assert share_check.main(["--config", str(environment)]) == share_check.EXIT_FAILED
    output = capsys.readouterr().out
    assert "key set differs" in output
    assert "Value is an integer from 39 to 401: no" in output
    assert "Trend recognized: no" in output and "parser: rejected" in output
    assert re.search(rf"\b({VALUE}|38)\b", output) is None


def test_describe_handles_non_list_and_non_json():
    assert share_check.describe(b"{}", 3, START) == (["response: JSON dict, not a list"], False)
    assert share_check.describe(b"<html>", 3, START) == (["response: not JSON"], False)
    lines, accepted = share_check.describe(json.dumps(["x"]).encode(), 3, START)
    assert "entry 0: JSON str, not an object" in lines and not accepted


def test_check_refuses_a_simulated_provider_config(capsys):
    assert share_check.main(["--config", str(EXAMPLE)]) == share_check.EXIT_CONFIG
    assert "provider must be dexcom_share" in capsys.readouterr().err
