import itertools
import math
from dataclasses import replace

import pytest
from harness import EXAMPLE

from glucose_guardian.config import load_config
from glucose_guardian.dexcom_share import Trend
from glucose_guardian.preview import models
from glucose_guardian.screen_model import ScreenModel, ScreenState, Tone, restarting
from glucose_guardian.screen_render import (
    ARROW,
    ARROW_TIP,
    ARROWS,
    COLORS,
    HEIGHT,
    MAGENTA,
    RED,
    SIDE_BOX,
    SIMULATED_BOX,
    TREND_BOX,
    VALUE_BOX,
    WHITE,
    WIDTH,
    Renderer,
    arrow_polygons,
)

DISPLAY = load_config(EXAMPLE).display
assert DISPLAY is not None
FONT = DISPLAY.font_file
MODELS = dict(models(DISPLAY.last_known_cutoff_s, DISPLAY.snapshot_max_age_s))


@pytest.fixture(scope="module")
def renderer():
    assert FONT.is_file(), f"{FONT} is missing: install fonts-dejavu-core"
    return Renderer(FONT)


def inside(inner, outer) -> bool:
    return (outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2]
            and inner[3] <= outer[3])


def overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def pixels(image, box):
    left, top, right, bottom = (int(edge) for edge in box)
    return [image.getpixel((x, y)) for x in range(left, right) for y in range(top, bottom)]


def colored(image, box, hole=(0, 0, 0, 0)) -> int:
    left, top, right, bottom = box
    return sum(1 for x in range(left, right) for y in range(top, bottom)
               if not (hole[0] <= x < hole[2] and hole[1] <= y < hole[3])
               and image.getpixel((x, y)) != (0, 0, 0))


@pytest.mark.parametrize("name", sorted(MODELS))
def test_every_preview_renders_inside_its_layout(renderer, name):
    rendering = renderer.render(MODELS[name])
    assert rendering.image.size == (WIDTH, HEIGHT) and rendering.image.mode == "RGB"
    for placed in rendering.placed:
        assert inside(placed.ink, placed.box), placed
        assert inside(placed.box, (0, 0, WIDTH, HEIGHT)), placed
    boxes = [placed for placed in rendering.placed if placed.name != "value_panel"]
    for first, second in itertools.combinations(boxes, 2):
        assert not overlap(first.box, second.box), (first.name, second.name)


@pytest.mark.parametrize("name", sorted(MODELS))
def test_no_number_or_arrow_outside_live(renderer, name):
    model = MODELS[name]
    rendering = renderer.render(model)
    names = {placed.name for placed in rendering.placed}
    if model.state is not ScreenState.LIVE:
        assert "trend" not in names
    if model.state is ScreenState.LAST_KNOWN:
        assert colored(rendering.image, TREND_BOX, hole=SIDE_BOX) == 0
    assert ("tag" in names) == (model.state is ScreenState.LAST_KNOWN)
    if model.state is ScreenState.LAST_KNOWN:
        tag = next(placed for placed in rendering.placed if placed.name == "tag")
        value = next(placed for placed in rendering.placed if placed.name == "value")
        assert model.tag.startswith("LAST KNOWN") and tag.ink[3] <= value.ink[1]
    if model.state in (ScreenState.NO_DATA, ScreenState.NO_CURRENT_DATA):
        assert "value" not in names


def test_live_value_uses_the_level_color(renderer):
    rendering = renderer.render(MODELS["live-normal-flat"])
    value = next(placed for placed in rendering.placed if placed.name == "value")
    assert COLORS[Tone.NORMAL] in pixels(rendering.image, value.ink)


def test_last_known_value_is_achromatic(renderer):
    for name in ("last-known", "last-known-urgent-low-unresolved"):
        rendering = renderer.render(MODELS[name])
        value = next(placed for placed in rendering.placed if placed.name == "value")
        shades = pixels(rendering.image, value.ink)
        assert all(r == g == b for r, g, b in shades)
        assert any(pixel != (0, 0, 0) for pixel in shades)


def test_urgent_low_is_white_on_red(renderer):
    rendering = renderer.render(MODELS["live-urgent-low-single-down"])
    assert rendering.image.getpixel((VALUE_BOX[0] + 1, VALUE_BOX[1] + 1)) == RED
    value = next(placed for placed in rendering.placed if placed.name == "value")
    assert WHITE in pixels(rendering.image, value.ink)


def test_device_limits_render_as_ranges(renderer):
    assert MODELS["live-device-low"].value == "<40"
    assert MODELS["live-device-high"].value == ">400"
    for name in ("live-device-low", "live-device-high"):
        names = {placed.name for placed in renderer.render(MODELS[name]).placed}
        assert {"value", "trend"} <= names


def test_markers(renderer):
    simulated = renderer.render(MODELS["simulated-live"])
    corner = (SIMULATED_BOX[0] + 1, SIMULATED_BOX[1] + 1)
    assert simulated.image.getpixel(corner) == MAGENTA
    assert {"simulated", "display_only"} <= {placed.name for placed in simulated.placed}
    live = renderer.render(MODELS["live-normal-flat"])
    assert live.image.getpixel(corner) == (0, 0, 0)
    assert "simulated" not in {placed.name for placed in live.placed}
    blank = renderer.render(restarting())
    assert {placed.name for placed in blank.placed} == {"headline", "detail"}


def test_long_texts_shrink_or_truncate_but_never_overflow(renderer):
    model: ScreenModel = replace(
        MODELS["last-known"], reason="x" * 400, connection="Dexcom: " + "y" * 300,
        alerts=tuple(f"Alert number {index}" for index in range(30)))
    rendering = renderer.render(model)
    for placed in rendering.placed:
        assert inside(placed.ink, placed.box), placed
    assert {"reason", "connection", "alerts"} <= {placed.name for placed in rendering.placed}


def unit(angle_deg: float, x: float, y: float, box=TREND_BOX, count: int = 1):
    width = (box[2] - box[0]) / count
    radius = min(width, box[3] - box[1]) / 2
    angle = math.radians(angle_deg)
    center_x, center_y = box[0] + width / 2, (box[1] + box[3]) / 2
    return (round(center_x + radius * (x * math.cos(angle) - y * math.sin(angle))),
            round(center_y - radius * (x * math.sin(angle) + y * math.cos(angle))))


@pytest.mark.parametrize("trend", list(ARROWS))
def test_arrows_point_in_the_trend_direction(renderer, trend):
    angle, count = ARROWS[trend]
    polygons = arrow_polygons(trend, TREND_BOX)
    assert len(polygons) == count
    for polygon in polygons:
        assert len(polygon) == len(ARROW)
        assert inside((min(x for x, _ in polygon), min(y for _, y in polygon),
                       max(x for x, _ in polygon), max(y for _, y in polygon)), TREND_BOX)
        tip_x, tip_y = polygon[ARROW_TIP]
        tail_x = (polygon[0][0] + polygon[-1][0]) / 2
        tail_y = (polygon[0][1] + polygon[-1][1]) / 2
        direction = math.degrees(math.atan2(tail_y - tip_y, tip_x - tail_x))
        assert math.isclose(direction, angle, abs_tol=1e-6)
    model = replace(MODELS["live-normal-flat"], trend=trend)
    image = renderer.render(model).image
    green = COLORS[Tone.NORMAL]
    head = unit(angle, 0.35, 0.3, count=count)
    tail_side = unit(angle, -0.35, 0.3, count=count)
    assert image.getpixel(head) == green
    assert image.getpixel(tail_side) == (0, 0, 0)


@pytest.mark.parametrize("trend", [None, Trend.NONE, Trend.NOT_COMPUTABLE,
                                   Trend.RATE_OUT_OF_RANGE])
def test_no_arrow_without_a_direction(renderer, trend):
    assert arrow_polygons(trend, TREND_BOX) == ()
    image = renderer.render(replace(MODELS["live-normal-flat"], trend=trend)).image
    assert colored(image, TREND_BOX) == 0


def test_renderer_refuses_a_missing_or_invalid_font(tmp_path):
    with pytest.raises(FileNotFoundError):
        Renderer(tmp_path / "absent.ttf")
    bogus = tmp_path / "bogus.ttf"
    bogus.write_bytes(b"not a font")
    with pytest.raises(OSError):
        Renderer(bogus)
