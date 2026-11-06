"""Wall-clock trust check. An unknown or failed check means the clock is untrusted."""
import subprocess
from collections.abc import Callable

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
COMMAND = ("timedatectl", "show", "--property=NTPSynchronized", "--value")


def systemd_clock_synchronized(run: Runner, timeout_s: float) -> bool:
    try:
        result = run(list(COMMAND), capture_output=True, text=True, timeout=timeout_s,
                     check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "yes"
