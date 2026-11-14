"""Display process: renders the runtime's snapshot. No network, Dexcom client or credentials."""
import argparse
import contextlib
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from PIL import Image

from .config import Config, ConfigError, DisplaySettings, load_config
from .display import Source
from .notify import Notifier, notifier_from_environment
from .screen_model import ScreenModel, ScreenSettings, ScreenState, classify, restarting
from .screen_render import Renderer
from .shutdown import stop_on_signals
from .snapshot import Snapshot, SnapshotProblem, read_snapshot

log = logging.getLogger("glucose_guardian.screen")

EXIT_STOPPED = 0
EXIT_CRASHED = 1
EXIT_CONFIG = 2


class Sink(Protocol):
    def show(self, image: Image.Image) -> None: ...


class PngSink:
    def __init__(self, path: Path) -> None:
        self._path = path

    def show(self, image: Image.Image) -> None:
        partial = self._path.with_name(self._path.name + ".partial")
        try:
            image.save(partial, format="PNG")
            os.replace(partial, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                partial.unlink()
            raise


def screen_settings(config: Config, display: DisplaySettings) -> ScreenSettings:
    return ScreenSettings(
        snapshot_max_age_s=display.snapshot_max_age_s,
        last_known_cutoff_s=display.last_known_cutoff_s,
        source=Source(config.provider), outputs_enabled=config.outputs_enabled)


def load(path: Path) -> Snapshot | SnapshotProblem:
    try:
        return read_snapshot(path)
    except SnapshotProblem as problem:
        return problem


def run(*, snapshot_file: Path, settings: ScreenSettings, refresh_s: float,
        renderer: Renderer, sink: Sink, notifier: Notifier | None,
        monotonic: Callable[[], float], now_local: Callable[[], datetime],
        wait: Callable[[float], bool]) -> None:
    shown: ScreenModel | None = None
    while True:
        model = classify(load(snapshot_file), settings=settings, now_monotonic=monotonic(),
                         now_local=now_local())
        if model != shown:
            if shown is None or model.state is not shown.state:
                detail = f" ({model.detail})" if model.state is ScreenState.NO_DATA else ""
                log.info("screen state %s%s", model.state.value, detail)
            sink.show(renderer.render(model).image)
            if notifier is not None and shown is None:
                notifier.notify("READY=1")
            shown = model
        if notifier is not None:
            notifier.notify("WATCHDOG=1")
        if wait(refresh_s):
            break
    if notifier is not None:
        notifier.notify("STOPPING=1")


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m glucose_guardian.screen")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--png", required=True, type=Path, help="PNG file that receives each frame")
    parser.add_argument("--blank", action="store_true",
                        help="show 'Display restarting' once, then exit")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if config.display is None:
            raise ConfigError(f"{args.config}: [display] kind must be \"snapshot\" for the "
                              "display process")
        renderer = Renderer(config.display.font_file)
    except ConfigError as error:
        log.error("%s", error)
        return EXIT_CONFIG
    except OSError as error:
        log.error("[display] font_file cannot be loaded: %s", type(error).__name__)
        return EXIT_CONFIG
    sink = PngSink(args.png)
    if args.blank:
        sink.show(renderer.render(restarting()).image)
        return EXIT_STOPPED
    display = config.display
    try:
        with stop_on_signals() as stop:
            run(snapshot_file=display.snapshot_file, settings=screen_settings(config, display),
                refresh_s=display.refresh_s, renderer=renderer, sink=sink,
                notifier=notifier_from_environment(os.environ), monotonic=time.monotonic,
                now_local=lambda: datetime.now().astimezone(), wait=stop.wait)
    except Exception:
        log.exception("display crashed")
        return EXIT_CRASHED
    log.info("display stopped")
    return EXIT_STOPPED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
