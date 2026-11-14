import ast
from pathlib import Path

from harness import write_config
from PIL import Image

from glucose_guardian import preview
from glucose_guardian.dexcom_share import ShareStatus
from glucose_guardian.screen_model import ScreenState
from glucose_guardian.screen_render import HEIGHT, WIDTH

SOURCE = Path(__file__).resolve().parents[1] / "src" / "glucose_guardian" / "preview.py"


def test_previews_cover_every_state_and_connection_status(tmp_path):
    out = tmp_path / "previews"
    assert preview.main(["--config", str(write_config(tmp_path)), "--out", str(out)]) == 0
    files = sorted(out.iterdir())
    listing = preview.models(1800, 5)
    assert len(files) == len(listing)
    for path in files:
        with Image.open(path) as image:
            assert image.size == (WIDTH, HEIGHT)
    states = {model.state for _, model in listing}
    assert states == set(ScreenState)
    names = {name for name, _ in listing}
    for status in ShareStatus:
        assert f"connection-{status.value.replace('_', '-')}" in names
    assert {"connection-no-response", "simulated-live", "live-device-low",
            "live-device-high", "display-restarting"} <= names
    assert {model.connection for _, model in listing if model.connection} >= {
        "Dexcom: login blocked, operator action needed", "Dexcom: no response",
        "Source: simulation"}


def test_previews_use_synthetic_values_only():
    tree = ast.parse(SOURCE.read_text())
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                for alias in node.names}
    assert not imported & {"read_snapshot", "load", "SnapshotDisplay", "write_snapshot"}
    values = {model.value for _, model in preview.models(1800, 5) if model.value}
    assert values == {"123", "48", "64", "262", "141", "168", "<40", ">400"}


def test_previews_need_snapshot_display_settings(tmp_path, caplog):
    text = write_config(tmp_path).read_text()
    config = tmp_path / "log.toml"
    config.write_text(text[:text.index("[display]")] + '[display]\nkind = "log"\n'
                      + text[text.index("[hardware]"):])
    assert preview.main(["--config", str(config), "--out", str(tmp_path / "out")]) == 2
    assert 'kind must be "snapshot"' in caplog.text
