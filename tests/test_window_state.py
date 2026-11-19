from pathlib import Path
from types import SimpleNamespace

from src import window_state
from src.window_state import WindowPlacement


class Handlers(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self


def test_keeps_secondary_monitor_and_recovers_removed_monitor():
    primary = (0, 40, 1920, 1040)
    secondary = (-1600, 0, 1600, 900)
    saved = WindowPlacement(-1420, 90, 980, 650, maximized=True)
    minimum = (860, 560)

    assert window_state._fit_placement(saved, [primary, secondary], minimum) == saved
    assert window_state._fit_placement(saved, [primary], minimum) == WindowPlacement(470, 235, 980, 650, True)
    smaller = (-1280, 50, 1280, 650)
    oversized = WindowPlacement(-1450, 0, 1700, 900)
    assert window_state._fit_placement(oversized, [primary, smaller], minimum) == WindowPlacement(-1280, 50, 1280, 650)


def test_bad_window_state_and_failed_replace_leave_defaults_or_previous_layout(tmp_path, monkeypatch):
    path = tmp_path / "window_state.json"
    assert window_state._load_placement(path) is None
    path.write_text('{"x":true,"y":20,"width":980,"height":650}', encoding="utf-8")
    assert window_state._load_placement(path) is None
    path.write_text("{", encoding="utf-8")
    assert window_state._load_placement(path) is None

    saved = WindowPlacement(-800, 145, 900, 600)
    window_state._save_placement(path, saved)
    assert window_state._load_placement(path) == saved

    def fail_replace(source: Path, destination: Path):
        raise PermissionError("test denied replace")

    monkeypatch.setattr(window_state.os, "replace", fail_replace)
    window_state._save_placement(path, WindowPlacement(30, 40, 1110, 650))
    assert window_state._load_placement(path) == saved
    assert list(tmp_path.iterdir()) == [path]


def test_close_saves_restore_bounds_and_registers_next_start(tmp_path, monkeypatch):
    monkeypatch.setattr(window_state, "sys", SimpleNamespace(platform="win32"))
    path = tmp_path / "window_state.json"
    calls = []
    monkeypatch.setattr(window_state, "_restore_winforms", lambda window, placement: calls.append(placement))
    normal = SimpleNamespace(X=180, Y=140, Width=980, Height=660)
    form = SimpleNamespace(WindowState="Maximized", RestoreBounds=normal)
    window = SimpleNamespace(native=form, events=SimpleNamespace(closing=Handlers(), before_show=Handlers()))

    window_state.remember_window(window, path)
    assert window.events.before_show == []
    window.events.closing[0]()
    maximized = WindowPlacement(180, 140, 980, 660, True)
    assert window_state._load_placement(path) == maximized

    restarted = SimpleNamespace(native=form, events=SimpleNamespace(closing=Handlers(), before_show=Handlers()))
    window_state.remember_window(restarted, path)
    restarted.events.before_show[0]()
    assert calls == [maximized]

    form.WindowState = "Minimized"
    restarted.events.closing[0]()
    assert window_state._load_placement(path) == WindowPlacement(180, 140, 980, 660)


def test_legacy_winforms_uses_initial_geometry_and_form_on_close(tmp_path, monkeypatch):
    monkeypatch.setattr(window_state, "sys", SimpleNamespace(platform="win32"))
    user32 = SimpleNamespace(SetProcessDPIAware=lambda: True)
    monkeypatch.setattr(window_state, "ctypes", SimpleNamespace(windll=SimpleNamespace(user32=user32)))
    work_area = SimpleNamespace(X=-1600, Y=0, Width=1600, Height=900)
    monitor = SimpleNamespace(frame=work_area)
    monkeypatch.setattr(window_state, "webview", SimpleNamespace(screens=[monitor]))

    path = tmp_path / "window_state.json"
    saved = WindowPlacement(-1520, 55, 980, 660)
    window_state._save_placement(path, saved)
    bounds = SimpleNamespace(X=-1500, Y=70, Width=1050, Height=690)
    form = SimpleNamespace(WindowState="Normal", Bounds=bounds, RestoreBounds=bounds)
    backend = SimpleNamespace(BrowserView=SimpleNamespace(instances={"master": form}))
    window = SimpleNamespace(uid="master", gui=backend, min_size=(860, 560), events=SimpleNamespace(closing=Handlers()))

    window_state.remember_window(window, path)
    assert (window.initial_x, window.initial_y, window.initial_width, window.initial_height) == (-1520, 55, 980, 660)
    window.events.closing[0]()
    assert window_state._load_placement(path) == WindowPlacement(-1500, 70, 1050, 690)
