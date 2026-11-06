import subprocess

import pytest

from glucose_guardian.clock import COMMAND, systemd_clock_synchronized


def completed(stdout: str, returncode: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(COMMAND), returncode, stdout, "")


@pytest.mark.parametrize("result,expected", [
    (completed("yes\n", 0), True),
    (completed("no\n", 0), False),
    (completed("yes\n", 1), False),
    (completed("", 0), False),
])
def test_output_parsing(result, expected):
    assert systemd_clock_synchronized(lambda *args, **kwargs: result, 5) is expected


@pytest.mark.parametrize("error", [
    FileNotFoundError("timedatectl"),
    PermissionError("denied"),
    subprocess.TimeoutExpired(list(COMMAND), 5),
])
def test_check_errors_mean_untrusted(error):
    def run(*args, **kwargs):
        raise error

    assert systemd_clock_synchronized(run, 5) is False


def test_command_is_bounded_and_unchecked():
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return completed("yes\n", 0)

    systemd_clock_synchronized(run, 2.5)
    assert calls == [((list(COMMAND),), {"capture_output": True, "text": True,
                                         "timeout": 2.5, "check": False})]
