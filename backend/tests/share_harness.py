import ssl
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness import ROOT

from glucose_guardian.credentials import Credentials
from glucose_guardian.dexcom_share import APPLICATION_ID
from glucose_guardian.share_client import (
    MAX_RESPONSE_BYTES,
    DexcomShareProvider,
    ShareServer,
    ShareSession,
    urllib_poster,
)
from glucose_guardian.share_simulator import (
    SYNTHETIC_PASSWORD,
    SYNTHETIC_USERNAME,
    ShareSimulator,
    SyntheticReading,
)
from glucose_guardian.share_state import AuthStateStore

START = datetime(2026, 1, 1, tzinfo=UTC)
CREDENTIALS = Credentials(SYNTHETIC_USERNAME, SYNTHETIC_PASSWORD)
DEXCOM_EXAMPLE = ROOT / "config" / "example-dexcom-share.toml"
LOGIN_INTERVAL_S = 300.0
RATE_LIMIT_INITIAL_S = 120.0
RATE_LIMIT_MAX_S = 1800.0


class ShareClock:
    def __init__(self) -> None:
        self.boottime = 1000.0
        self.wall = START

    def advance(self, seconds: float) -> None:
        self.boottime += seconds
        self.wall += timedelta(seconds=seconds)


def synthetic(mg_dl: int, at: datetime, trend: str) -> SyntheticReading:
    return SyntheticReading(mg_dl, at, trend)


def make_store(path: Path, clock: ShareClock, boot_id: str) -> AuthStateStore:
    return AuthStateStore(path=path, boot_id=boot_id, boottime=lambda: clock.boottime,
                          utc=lambda: clock.wall, login_min_interval_s=LOGIN_INTERVAL_S,
                          rate_limit_initial_s=RATE_LIMIT_INITIAL_S,
                          rate_limit_max_s=RATE_LIMIT_MAX_S)


def make_session(simulator: ShareSimulator, store: AuthStateStore, clock: ShareClock, *,
                 request_timeout_s: float, credentials: Credentials) -> ShareSession:
    post = urllib_poster(context=ssl.create_default_context(),
                         user_agent="glucose-guardian-tests", max_bytes=MAX_RESPONSE_BYTES,
                         monotonic=time.monotonic)
    return ShareSession(server=ShareServer(simulator.base_url, APPLICATION_ID),
                        credentials=credentials, post=post, store=store,
                        request_timeout_s=request_timeout_s, boottime=lambda: clock.boottime)


def make_provider(simulator: ShareSimulator, state_file: Path, clock: ShareClock, *,
                  boot_id: str, request_timeout_s: float) -> DexcomShareProvider:
    store = make_store(state_file, clock, boot_id)
    session = make_session(simulator, store, clock, request_timeout_s=request_timeout_s,
                           credentials=CREDENTIALS)
    return DexcomShareProvider(session, max_age_s=600)
