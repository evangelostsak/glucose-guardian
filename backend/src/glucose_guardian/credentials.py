"""Dexcom Share publisher credentials from a systemd credentials directory. Never printed."""
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

USERNAME_FILE = "dexcom-username"
PASSWORD_FILE = "dexcom-password"
MAX_BYTES = 256
FORBIDDEN = frozenset("\r\n\x00")
EXIT_OK = 0
EXIT_FAILED = 1


class CredentialsError(Exception):
    pass


@dataclass(frozen=True, repr=False)
class Credentials:
    username: str
    password: str

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"


def _read(directory: Path, name: str) -> str:
    path = directory / name
    try:
        info = path.stat()
    except FileNotFoundError:
        raise CredentialsError(f"credential {name} is missing") from None
    except OSError as error:
        raise CredentialsError(f"credential {name} is not readable: "
                               f"{type(error).__name__}") from None
    if not stat.S_ISREG(info.st_mode):
        raise CredentialsError(f"credential {name} is not a regular file")
    if info.st_mode & 0o007:
        raise CredentialsError(f"credential {name} must not be accessible to other users")
    if not 0 < info.st_size <= MAX_BYTES:
        raise CredentialsError(f"credential {name} must hold 1 to {MAX_BYTES} bytes")
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CredentialsError(f"credential {name} is not readable UTF-8: "
                               f"{type(error).__name__}") from None
    value = text.removesuffix("\n").removesuffix("\r")
    if not value or FORBIDDEN & set(value):
        raise CredentialsError(f"credential {name} must be one non-empty line")
    return value


def load_credentials(directory: str | None) -> Credentials:
    if not directory:
        raise CredentialsError(
            "CREDENTIALS_DIRECTORY is not set: provide dexcom-username and dexcom-password "
            "through systemd LoadCredentialEncrypted= (see docs/DEXCOM_SHARE.md)")
    return Credentials(_read(Path(directory), USERNAME_FILE),
                       _read(Path(directory), PASSWORD_FILE))


def main() -> int:
    try:
        load_credentials(os.environ.get("CREDENTIALS_DIRECTORY"))
    except CredentialsError as error:
        print(f"credentials invalid: {error}", file=sys.stderr)
        return EXIT_FAILED
    print("credentials: both decrypt and are well-formed (Dexcom was not contacted)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
