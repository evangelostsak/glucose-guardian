"""Finite simulation demo; deliberately not a production service."""
from datetime import datetime, timezone

from .providers import SimulatedProvider


def main() -> None:
    provider = SimulatedProvider()
    for _ in range(4):
        print(provider.read(datetime.now(timezone.utc)))


if __name__ == "__main__":
    main()
