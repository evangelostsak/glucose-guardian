from datetime import timedelta

import pytest
from harness import NOW

from glucose_guardian.simulation import SCENARIOS, ScenarioProvider, SimulatedFetchError


def provider(name: str, elapsed_s: list[float], hangs: list[float]) -> ScenarioProvider:
    def hang(seconds: float) -> None:
        hangs.append(seconds)
        elapsed_s[0] += seconds

    return ScenarioProvider(SCENARIOS[name], start_wall=NOW, elapsed=lambda: elapsed_s[0],
                            hang=hang)


def read_at(name: str, t: float) -> object:
    return provider(name, [t], []).read(NOW + timedelta(seconds=t))


def test_segment_lookup_and_last_segment_persists():
    scenario = SCENARIOS["hypoglycemia"]
    assert [scenario.segment_at(t)[0] for t in (0, 299.9, 300, 899, 900, 10_000)] == [
        0, 0, 300, 600, 900, 900]


def test_fresh_frozen_missing_and_error_segments():
    assert read_at("normal", 5).timestamp == NOW + timedelta(seconds=5)
    assert read_at("stale_data", 1000).timestamp == NOW + timedelta(seconds=300)
    assert read_at("missing_data", 5) is None
    with pytest.raises(SimulatedFetchError):
        read_at("acquisition_errors", 400)


def test_hang_blocks_until_the_segment_ends():
    elapsed_s, hangs = [350.0], []
    reading = provider("acquisition_timeout", elapsed_s, hangs).read(NOW)
    assert hangs == [250.0] and elapsed_s == [600.0] and reading.mg_dl == 110
