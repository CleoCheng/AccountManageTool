import ctypes
from ctypes import wintypes
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
import webview
from src.api import AccountApi
from src.crash_logger import install_crash_handlers, install_winforms_exception_handlers, log_exception
from src.login_tools_panel import LoginToolsPanel
from src.url_dispatcher import (
    enable_windows_url_interception,
    extract_url_from_args,
    start_pipe_server,
    try_forward_to_running_instance,
)
from src.window_state import remember_window

APP_ID = "KeyVault.AccountManager.Desktop.1"


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def set_windows_app_icon(window_title: str, icon_path: Path) -> None:
    if sys.platform != "win32" or not icon_path.exists():
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.SendMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.SendMessageW.restype = wintypes.LPARAM

        hwnd = user32.FindWindowW(None, window_title)
        if not hwnd:
            return
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1

        hicon_big = user32.LoadImageW(None, str(icon_path), IMAGE_ICON, 64, 64, LR_LOADFROMFILE)
        hicon_small = user32.LoadImageW(None, str(icon_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if hicon_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, int(hicon_big))
        if hicon_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, int(hicon_small))
    except Exception:
        pass


def make_async_webview_notifier(
    window, *, is_closing: Callable[[], bool] | None = None
) -> Callable[[dict], None]:
    """Return a notifier that refreshes the Web UI off the caller's thread.

    ``EdgeChrome.evaluate_js`` blocks on a semaphore released through the
    WinForms UI thread's ``SynchronizationContext``. Calling it synchronously
    from a WinForms UI-thread event handler therefore deadlocks forever
    (Windows reports ``AppHangB1``). The returned callback only flips a flag
    and lets a single daemon worker perform ``evaluate_js``.
    """
    script = "window.__kvOnAccountUpdated && window.__kvOnAccountUpdated()"
    closing = is_closing or (lambda: False)
    lock = threading.Lock()
    state = {"pending": False, "running": False}

    def _worker() -> None:
        while True:
            if closing():
                with lock:
                    state["pending"] = False
                    state["running"] = False
                return
            try:
                window.evaluate_js(script)
            except Exception:
                if not closing():
                    log_exception("webview.evaluate_js")
            with lock:
                if not state["pending"] or closing():
                    state["pending"] = False
                    state["running"] = False
                    return
                state["pending"] = False

    def notify(_acc: dict | None = None) -> None:
        with lock:
            if closing():
                return
            if state["running"]:
                state["pending"] = True
                return
            state["running"] = True
        threading.Thread(target=_worker, name="kv-webview-notify", daemon=True).start()

    return notify


def main() -> None:
    startup_url = extract_url_from_args(sys.argv[1:])
    if startup_url and try_forward_to_running_instance(startup_url):
        return

    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass

    base_dir = get_base_dir()
    data_dir = base_dir / "data"
    runtime_dir = data_dir / "runtime"
    web_dir = base_dir / "web"
    icon_path = web_dir / "icon.ico"

    data_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    crash_log_path = data_dir / "crash.log"
    install_crash_handlers(crash_log_path)

    api = AccountApi(data_dir=data_dir, auto_sync_account_data=True, base_dir=base_dir)
    closing_event = threading.Event()
    pipe_stop = None
    login_tools = None

    try:
        if "--verify-init" in sys.argv:
            dash = api.get_dashboard()
            print(f"OK: data_dir={data_dir} total_accounts={dash['stats']['total']}")
            return

        settings = api._ensure_settings()
        if sys.platform == "win32" and settings.get("url_interception_enabled"):
            enable_windows_url_interception(base_dir=base_dir)

        title = "KeyVault · 全平台账号与凭据管家"
        index_html = web_dir / "index.html"
        window = webview.create_window(
            title=title,
            url=str(index_html),
            js_api=api,
            width=1180,
            height=780,
            min_size=(860, 560),
            background_color="#0b1120",
            text_select=True,
        )

        on_hud_account_updated = make_async_webview_notifier(window, is_closing=closing_event.is_set)

        def _install_winforms_handlers():
            try:
                install_winforms_exception_handlers(crash_log_path)
            except Exception:
                pass

        # Install WinForms hooks before window creation settles so CatchException takes effect.
        _install_winforms_handlers()
        window.events.shown += lambda: set_windows_app_icon(title, icon_path)
        window.events.shown += _install_winforms_handlers
        remember_window(window, data_dir / "window_state.json")
        login_tools = LoginToolsPanel(
            window,
            api.db.get_account,
            lambda: api.db.totp_offset_seconds,
            api=api,
            on_account_updated=on_hud_account_updated,
        )
        api._show_login_tools = login_tools.track
        api._login_tools_panel = login_tools

        pipe_stop = start_pipe_server(lambda url: api.handle_dispatched_url(url))

        if startup_url:
            def _dispatch_startup():
                time.sleep(0.9)
                api.handle_dispatched_url(startup_url)

            window.events.shown += lambda: threading.Thread(target=_dispatch_startup, daemon=True).start()

        def _on_closing():
            closing_event.set()
            if pipe_stop is not None:
                pipe_stop.set()
            if login_tools is not None:
                login_tools.close()

        window.events.closing += _on_closing

        webview.start(
            private_mode=False,
            storage_path=str(runtime_dir),
            debug=False,
        )
    finally:
        closing_event.set()
        if pipe_stop is not None:
            pipe_stop.set()
        api._shutdown()


if __name__ == "__main__":
    main()
