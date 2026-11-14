"""Pillow renderer: draws a screen model as an 800x480 image. No glucose classification here."""
import io
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .dexcom_share import Trend
from .screen_model import ScreenModel, ScreenState, Tone

WIDTH = 800
HEIGHT = 480
MIN_TEXT_PX = 12
ELLIPSIS = "…"
SEPARATOR = " · "

Box = tuple[int, int, int, int]
Point = tuple[float, float]

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
RED = (225, 30, 30)
MAGENTA = (150, 0, 150)
COLORS = {
    Tone.TEXT: (235, 235, 235),
    Tone.MUTED: (150, 150, 150),
    Tone.GREY: (120, 120, 120),
    Tone.URGENT_LOW: RED,
    Tone.LOW: RED,
    Tone.NORMAL: (40, 200, 90),
    Tone.HIGH: (255, 176, 0),
    Tone.WARNING: (255, 176, 0),
    Tone.ALERT: RED,
}

CONNECTION_BOX: Box = (20, 8, 640, 48)
CLOCK_BOX: Box = (660, 8, 780, 48)
VALUE_BOX: Box = (20, 60, 560, 316)
TAG_BOX: Box = (20, 64, 560, 108)
LAST_VALUE_BOX: Box = (20, 116, 560, 312)
TREND_BOX: Box = (580, 84, 780, 292)
HEADLINE_BOX: Box = (20, 92, 780, 206)
DETAIL_BOX: Box = (20, 216, 780, 266)
SIDE_BOX: Box = (580, 176, 780, 212)
LEVEL_BOX: Box = (20, 324, 400, 366)
LEVEL_WIDE_BOX: Box = (20, 324, 780, 366)
AGE_BOX: Box = (410, 324, 780, 366)
REASON_BOX: Box = (20, 372, 780, 400)
ALERTS_BOX: Box = (20, 404, 780, 430)
DISPLAY_ONLY_BOX: Box = (20, 440, 470, 472)
SIMULATED_BOX: Box = (490, 436, 780, 476)

ARROW: tuple[Point, ...] = ((-0.9, -0.2), (0.15, -0.2), (0.15, -0.55), (0.9, 0.0),
                            (0.15, 0.55), (0.15, 0.2), (-0.9, 0.2))
ARROW_TIP = 3
ARROWS = {
    Trend.DOUBLE_UP: (90.0, 2),
    Trend.SINGLE_UP: (90.0, 1),
    Trend.FORTY_FIVE_UP: (45.0, 1),
    Trend.FLAT: (0.0, 1),
    Trend.FORTY_FIVE_DOWN: (-45.0, 1),
    Trend.SINGLE_DOWN: (-90.0, 1),
    Trend.DOUBLE_DOWN: (-90.0, 2),
}


class Align(str, Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


@dataclass(frozen=True)
class Placed:
    name: str
    box: Box
    ink: Box


@dataclass(frozen=True)
class Rendering:
    image: Image.Image
    placed: tuple[Placed, ...]


def arrow_polygons(trend: Trend | None, box: Box) -> tuple[tuple[Point, ...], ...]:
    if trend is None or trend not in ARROWS:
        return ()
    angle_deg, count = ARROWS[trend]
    left, top, right, bottom = box
    width = (right - left) / count
    radius = min(width, bottom - top) / 2
    angle = math.radians(angle_deg)
    cos, sin = math.cos(angle), math.sin(angle)
    polygons = []
    for index in range(count):
        center_x = left + width * (index + 0.5)
        center_y = (top + bottom) / 2
        polygons.append(tuple(
            (center_x + radius * (x * cos - y * sin), center_y - radius * (x * sin + y * cos))
            for x, y in ARROW))
    return tuple(polygons)


class Renderer:
    def __init__(self, font_file: Path) -> None:
        self._font_data = font_file.read_bytes()
        self._fonts: dict[int, ImageFont.FreeTypeFont] = {}
        self._font(MIN_TEXT_PX)

    def render(self, model: ScreenModel) -> Rendering:
        image = Image.new("RGB", (WIDTH, HEIGHT), BLACK)
        draw = ImageDraw.Draw(image)
        placed: list[Placed] = []

        def text(name: str, value: str | None, box: Box, color: tuple[int, int, int],
                 max_px: int, align: Align) -> None:
            if value is not None:
                placed.append(self._text(draw, name, value, box, color, max_px, align))

        text("connection", model.connection, CONNECTION_BOX, COLORS[model.connection_tone], 34,
             Align.LEFT)
        text("clock", model.clock, CLOCK_BOX, COLORS[Tone.TEXT], 40, Align.RIGHT)
        if model.state is ScreenState.LIVE and model.value is not None:
            color = COLORS[model.value_tone]
            if model.value_tone is Tone.URGENT_LOW:
                draw.rectangle(VALUE_BOX, fill=RED)
                placed.append(Placed("value_panel", VALUE_BOX, VALUE_BOX))
                color = WHITE
            text("value", model.value, _inset(VALUE_BOX, 12), color, 260, Align.CENTER)
            for polygon in arrow_polygons(model.trend, TREND_BOX):
                draw.polygon(polygon, fill=COLORS[model.value_tone])
            if arrow_polygons(model.trend, TREND_BOX):
                placed.append(Placed("trend", TREND_BOX, TREND_BOX))
            text("level", model.level, LEVEL_BOX, COLORS[model.level_tone], 40, Align.LEFT)
            text("age", model.age, AGE_BOX, COLORS[model.value_tone], 34, Align.RIGHT)
        else:
            text("tag", model.tag, TAG_BOX, COLORS[Tone.TEXT], 40, Align.LEFT)
            text("value", model.value, LAST_VALUE_BOX, COLORS[model.value_tone], 150,
                 Align.CENTER)
            text("age", model.age, SIDE_BOX, COLORS[Tone.GREY], 30, Align.CENTER)
            text("headline", model.headline, HEADLINE_BOX, COLORS[Tone.TEXT], 96, Align.CENTER)
            text("detail", model.detail, DETAIL_BOX, COLORS[Tone.MUTED], 40, Align.CENTER)
            text("level", model.level, LEVEL_WIDE_BOX, COLORS[model.level_tone], 34, Align.LEFT)
        text("reason", model.reason, REASON_BOX, COLORS[Tone.TEXT], 28, Align.LEFT)
        if model.alerts:
            placed.append(self._alerts(draw, model.alerts))
        if model.display_only:
            text("display_only", "DISPLAY ONLY: NO ALARM OUTPUT", DISPLAY_ONLY_BOX,
                 COLORS[Tone.MUTED], 26, Align.LEFT)
        if model.simulated:
            draw.rectangle(SIMULATED_BOX, fill=MAGENTA)
            placed.append(self._text(draw, "simulated", "SIMULATED DATA",
                                     _inset(SIMULATED_BOX, 6), WHITE, 28, Align.CENTER))
        return Rendering(image, tuple(placed))

    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        if size not in self._fonts:
            self._fonts[size] = ImageFont.truetype(io.BytesIO(self._font_data), size)
        return self._fonts[size]

    def _fit(self, value: str, box: Box, max_px: int) -> ImageFont.FreeTypeFont | None:
        width, height = box[2] - box[0], box[3] - box[1]
        low, high = MIN_TEXT_PX, max_px
        found = None
        while low <= high:
            size = (low + high) // 2
            font = self._font(size)
            left, top, right, bottom = font.getbbox(value)
            if right - left <= width and bottom - top <= height:
                found, low = font, size + 1
            else:
                high = size - 1
        return found

    def _text(self, draw: ImageDraw.ImageDraw, name: str, value: str, box: Box,
              color: tuple[int, int, int], max_px: int, align: Align) -> Placed:
        shown = value
        font = self._fit(shown, box, max_px)
        while font is None:
            if len(shown) <= 1:
                raise ValueError(f"{name} cannot fit its box at {MIN_TEXT_PX} px")
            shown = shown[:-2] + ELLIPSIS if shown.endswith(ELLIPSIS) else shown[:-1] + ELLIPSIS
            font = self._fit(shown, box, max_px)
        left, top, right, bottom = (int(edge) for edge in font.getbbox(shown))
        width, height = right - left, bottom - top
        if align is Align.LEFT:
            x = box[0] - left
        elif align is Align.RIGHT:
            x = box[2] - width - left
        else:
            x = box[0] + (box[2] - box[0] - width) // 2 - left
        y = box[1] + (box[3] - box[1] - height) // 2 - top
        draw.text((x, y), shown, font=font, fill=color)
        return Placed(name, box, (x + left, y + top, x + right, y + bottom))

    def _alerts(self, draw: ImageDraw.ImageDraw, alerts: tuple[str, ...]) -> Placed:
        for count in range(len(alerts), 0, -1):
            shown = SEPARATOR.join(alerts[:count])
            if count < len(alerts):
                shown += f"{SEPARATOR}+{len(alerts) - count} more"
            if self._fit(shown, ALERTS_BOX, 24) is not None:
                return self._text(draw, "alerts", shown, ALERTS_BOX, COLORS[Tone.WARNING], 24,
                                  Align.LEFT)
        return self._text(draw, "alerts", f"{len(alerts)} alerts", ALERTS_BOX,
                          COLORS[Tone.WARNING], 24, Align.LEFT)


def _inset(box: Box, margin: int) -> Box:
    return (box[0] + margin, box[1] + margin, box[2] - margin, box[3] - margin)
