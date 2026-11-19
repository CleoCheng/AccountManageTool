import ctypes
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

import webview


@dataclass(frozen=True)
class WindowPlacement:
    x: int
    y: int
    width: int
    height: int
    maximized: bool = False


def _load_placement(path: Path) -> WindowPlacement | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    bounds = [data.get(key) for key in ("x", "y", "width", "height")]
    if not all(type(value) is int and -(2**31) <= value < 2**31 for value in bounds):
        return None
    if bounds[2] <= 0 or bounds[3] <= 0:
        return None
    return WindowPlacement(*bounds, maximized=data.get("maximized") is True)


def _save_placement(path: Path, placement: WindowPlacement) -> None:
    temporary = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            json.dump(asdict(placement), file)
        os.replace(temporary, path)
        temporary = None
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _fit_placement(
    placement: WindowPlacement,
    work_areas: list[tuple[int, int, int, int]],
    min_size: tuple[int, int],
) -> WindowPlacement | None:
    def overlap(area: tuple[int, int, int, int]) -> int:
        x, y, width, height = area
        shared_width = min(placement.x + placement.width, x + width) - max(placement.x, x)
        shared_height = min(placement.y + placement.height, y + height) - max(placement.y, y)
        return max(0, shared_width) * max(0, shared_height)

    # The primary monitor comes first and wins if every intersection is empty.
    area = max(work_areas, key=overlap, default=None)
    if area is None:
        return None

    x, y, area_width, area_height = area
    width = min(max(placement.width, min_size[0]), area_width)
    height = min(max(placement.height, min_size[1]), area_height)
    if overlap(area):
        x = min(max(placement.x, x), x + area_width - width)
        y = min(max(placement.y, y), y + area_height - height)
    else:
        x += (area_width - width) // 2
        y += (area_height - height) // 2
    return WindowPlacement(x, y, width, height, placement.maximized)


def _rectangle(rect) -> tuple[int, int, int, int]:
    return rect.X, rect.Y, rect.Width, rect.Height


def _winforms_window(window):
    native = getattr(window, "native", None)
    if native is None:
        # pywebview 5.0–5.2 store the form on the backend instead.
        browser_view = getattr(window.gui, "BrowserView", None)
        if browser_view is not None:
            native = browser_view.instances.get(window.uid)
    return native if hasattr(native, "RestoreBounds") else None


def _restore_winforms(window, placement: WindowPlacement) -> None:
    native = _winforms_window(window)
    if native is None:
        return
    from System.Windows.Forms import FormStartPosition, FormWindowState, Screen

    monitors = sorted(Screen.AllScreens, key=lambda screen: not screen.Primary)
    fitted = _fit_placement(placement, [_rectangle(screen.WorkingArea) for screen in monitors], window.min_size)
    if fitted is None:
        return
    native.StartPosition = FormStartPosition.Manual
    native.SetBounds(fitted.x, fitted.y, fitted.width, fitted.height)
    if fitted.maximized:
        native.WindowState = FormWindowState.Maximized


def _restore_initial_geometry(window, placement: WindowPlacement) -> None:
    if sys.platform == "win32":
        # Older pywebview sets DPI awareness at GUI startup, after this screen query.
        ctypes.windll.user32.SetProcessDPIAware()
    try:
        work_areas = []
        for screen in webview.screens:
            frame = getattr(screen, "frame", None)
            if sys.platform == "win32" and hasattr(frame, "X"):
                work_areas.append(_rectangle(frame))
            else:
                work_areas.append((getattr(screen, "x", 0), getattr(screen, "y", 0), screen.width, screen.height))
        fitted = _fit_placement(placement, work_areas, window.min_size)
    except (AttributeError, RuntimeError, OSError):
        return
    if fitted is not None:
        window.initial_x, window.initial_y = fitted.x, fitted.y
        window.initial_width, window.initial_height = fitted.width, fitted.height
        window.maximized = fitted.maximized


def remember_window(window, path: Path) -> None:
    placement = _load_placement(path)
    if placement is not None:
        if sys.platform == "win32" and hasattr(window.events, "before_show"):
            window.events.before_show += lambda: _restore_winforms(window, placement)
        else:
            _restore_initial_geometry(window, placement)

    def save_on_close():
        native = _winforms_window(window) if sys.platform == "win32" else None
        if native is not None:
            state = str(native.WindowState)
            rect = native.Bounds if state == "Normal" else native.RestoreBounds
            current = WindowPlacement(*_rectangle(rect), maximized=state == "Maximized")
        elif window.events.shown.is_set():
            current = WindowPlacement(
                window.x,
                window.y,
                window.width,
                window.height,
                maximized=bool(getattr(window, "maximized", False)),
            )
        else:
            return
        if current.width > 0 and current.height > 0:
            _save_placement(path, current)

    window.events.closing += save_on_close
