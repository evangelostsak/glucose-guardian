import os

import pytest

from glucose_guardian.credentials import (
    PASSWORD_FILE,
    USERNAME_FILE,
    CredentialsError,
    load_credentials,
)

USERNAME = "synthetic-publisher"
PASSWORD = "synthetic pass phrase"


def write(directory, name: str, content: bytes, mode: int) -> None:
    path = directory / name
    path.unlink(missing_ok=True)
    path.write_bytes(content)
    os.chmod(path, mode)


@pytest.fixture
def directory(tmp_path):
    write(tmp_path, USERNAME_FILE, USERNAME.encode() + b"\n", 0o400)
    write(tmp_path, PASSWORD_FILE, PASSWORD.encode() + b"\n", 0o400)
    return tmp_path


def test_loads_credentials_and_strips_one_line_ending(directory):
    credentials = load_credentials(str(directory))
    assert (credentials.username, credentials.password) == (USERNAME, PASSWORD)
    write(directory, PASSWORD_FILE, PASSWORD.encode() + b"\r\n", 0o440)
    assert load_credentials(str(directory)).password == PASSWORD


@pytest.mark.parametrize("value", [None, ""])
def test_missing_directory_names_the_systemd_variable(value):
    with pytest.raises(CredentialsError, match="CREDENTIALS_DIRECTORY is not set"):
        load_credentials(value)


@pytest.mark.parametrize("name,content,mode,message", [
    (PASSWORD_FILE, None, None, "is missing"),
    (PASSWORD_FILE, b"", 0o400, "1 to 256 bytes"),
    (PASSWORD_FILE, b"x" * 257, 0o400, "1 to 256 bytes"),
    (PASSWORD_FILE, b"\n", 0o400, "one non-empty line"),
    (PASSWORD_FILE, b"two\nlines\n", 0o400, "one non-empty line"),
    (PASSWORD_FILE, b"nul\x00byte", 0o400, "one non-empty line"),
    (PASSWORD_FILE, b"\xff\xfe", 0o400, "not readable UTF-8"),
    (PASSWORD_FILE, PASSWORD.encode(), 0o404, "other users"),
    (USERNAME_FILE, USERNAME.encode(), 0o602, "other users"),
])
def test_unsafe_credentials_are_refused(directory, name, content, mode, message):
    path = directory / name
    path.unlink()
    if content is not None:
        write(directory, name, content, mode)
    with pytest.raises(CredentialsError, match=message):
        load_credentials(str(directory))


def test_credential_that_is_not_a_file_is_refused(directory):
    (directory / USERNAME_FILE).unlink()
    (directory / USERNAME_FILE).mkdir(mode=0o700)
    with pytest.raises(CredentialsError, match="not a regular file"):
        load_credentials(str(directory))


def test_values_never_appear_in_text(directory):
    credentials = load_credentials(str(directory))
    for text in (repr(credentials), str(credentials), f"{credentials}"):
        assert USERNAME not in text and PASSWORD not in text
    os.chmod(directory / PASSWORD_FILE, 0o404)
    with pytest.raises(CredentialsError) as error:
        load_credentials(str(directory))
    assert PASSWORD not in str(error.value) and USERNAME not in str(error.value)
