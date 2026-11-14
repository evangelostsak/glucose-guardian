"""systemd service notifications (sd_notify) over the Unix datagram socket in $NOTIFY_SOCKET."""
import socket
from collections.abc import Mapping


class Notifier:
    def __init__(self, address: str) -> None:
        if address.startswith("@"):
            self._address = "\0" + address[1:]
        elif address.startswith("/"):
            self._address = address
        else:
            raise ValueError("NOTIFY_SOCKET must be an absolute path or an abstract socket name")

    def notify(self, message: str) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
            sock.connect(self._address)
            sock.sendall(message.encode())


def notifier_from_environment(environment: Mapping[str, str]) -> Notifier | None:
    address = environment.get("NOTIFY_SOCKET")
    return None if address is None else Notifier(address)
