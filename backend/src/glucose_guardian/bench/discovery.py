"""USB serial device discovery. Serial numbers are redacted in all output."""
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from serial.tools.list_ports_common import ListPortInfo

BY_ID_DIR = Path("/dev/serial/by-id")
KNOWN_UNO_IDS = {
    (0x2341, 0x0043): "Arduino Uno R3",
    (0x2341, 0x0001): "Arduino Uno",
    (0x2A03, 0x0043): "Arduino Uno (arduino.org)",
    (0x1A86, 0x7523): "CH340 USB-serial (Uno clone)",
}


@dataclass(frozen=True)
class Candidate:
    device: str
    by_id: str | None
    usb_id: str | None
    description: str
    serial: str | None
    recognized: bool


def redact(text: str, secret: str | None) -> str:
    if not secret or len(secret) <= 4:
        return text
    return text.replace(secret, "…" + secret[-4:])


def _by_id_links(by_id_dir: Path) -> dict[str, str]:
    if not by_id_dir.is_dir():
        return {}
    return {os.path.realpath(link): str(link) for link in sorted(by_id_dir.iterdir())}


def _candidate(port: ListPortInfo, links: dict[str, str]) -> Candidate:
    usb = port.vid is not None and port.pid is not None
    key = (port.vid, port.pid) if usb else None
    serial = port.serial_number
    by_id = links.get(os.path.realpath(port.device))
    return Candidate(
        device=port.device,
        by_id=redact(by_id, serial) if by_id else None,
        usb_id=f"{port.vid:04X}:{port.pid:04X}" if usb else None,
        description=KNOWN_UNO_IDS.get(key, port.description) if key else port.description,
        serial=redact(serial, serial) if serial else None,
        recognized=key in KNOWN_UNO_IDS,
    )


def candidates(ports: Iterable[ListPortInfo], by_id_dir: Path) -> list[Candidate]:
    links = _by_id_links(by_id_dir)
    return [_candidate(port, links) for port in ports if port.vid is not None]


def describe(device: str, ports: Iterable[ListPortInfo], by_id_dir: Path) -> Candidate | None:
    real = os.path.realpath(device)
    links = _by_id_links(by_id_dir)
    for port in ports:
        if os.path.realpath(port.device) == real:
            return _candidate(port, links)
    return None
