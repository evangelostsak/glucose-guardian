from collections.abc import Iterator
from datetime import timedelta

import pytest
from share_harness import START, ShareClock, synthetic

from glucose_guardian.share_simulator import ShareSimulator, static


@pytest.fixture
def share_clock() -> ShareClock:
    return ShareClock()


@pytest.fixture
def simulator(share_clock: ShareClock) -> Iterator[ShareSimulator]:
    server = ShareSimulator(port=0, now=lambda: share_clock.wall,
                            source=static([synthetic(110, START - timedelta(seconds=60),
                                                     "Flat")])).start()
    yield server
    server.stop()
