import ctypes
from ctypes import wintypes
import os
import sys
import time
from pathlib import Path


HWND_MESSAGE = -3
HWND_TOPMOST = -1
GW_OWNER = 4
GWLP_HWNDPARENT = -8
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
MONITOR_DEFAULTTONEAREST = 2

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("monitor", wintypes.RECT),
        ("work_area", wintypes.RECT),
        ("flags", wintypes.DWORD),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("U", _INPUT_UNION),
    ]


def _toolbar_rect(
    browser: wintypes.RECT,
    work: wintypes.RECT,
    width: int,
    height: int,
    dpi: int,
    *,
    fullscreen: bool = False,
) -> tuple[int, int, int, int] | None:
    """Dock the HUD inside the browser window's top content edge.

    The strip sits horizontally centred inside the browser client area and
    vertically just below Chrome's tab strip + address bar + bookmarks bar
    (``show_on_all_tabs``), i.e. at the very top of the web content viewport
    (or near the top edge when F11 fullscreen hides browser chrome).
    """
    scale = max(1.0, float(dpi or 96) / 96.0)
    left = max(browser.left, work.left)
    right = min(browser.right, work.right)
    top = max(browser.top, work.top)
    bottom = min(browser.bottom, work.bottom)
    margin_x = max(8, round(12 * scale))
    if right <= left + 40 or bottom <= top + height or width <= 0 or height <= 0:
        return None

    eff_width = min(width, max(220, right - left - margin_x))
    x = left + (right - left - eff_width) // 2
    x = max(work.left + 4, min(x, max(work.left + 4, work.right - eff_width - 4)))

    header_inset = round(116 * scale)
    edge_inset = round(6 * scale)
    if fullscreen:
        y = browser.top + edge_inset
    elif bottom - top >= header_inset + height + round(72 * scale):
        y = browser.top + header_inset
    else:
        # Very short window: tuck the strip against the browser's top edge.
        y = browser.top + edge_inset
    min_y = min(bottom - height - edge_inset, work.bottom - height - 4)
    max_y = max(work.top + 4, top + edge_inset)
    if min_y < max_y:
        return None
    y = min(max(y, max_y), min_y)
    return x, y, eff_width, height


class BrowserWindows:
    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("浏览器附着工具仅支持 Windows。")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.own_pid = os.getpid()
        hwnd = wintypes.HWND
        win_bool = wintypes.BOOL
        str_ptr = wintypes.LPCWSTR
        rect_ptr = ctypes.POINTER(wintypes.RECT)
        for name, restype, argtypes in (
            ("FindWindowExW", hwnd, (hwnd, hwnd, str_ptr, str_ptr)),
            ("GetWindowThreadProcessId", wintypes.DWORD, (hwnd, ctypes.POINTER(wintypes.DWORD))),
            ("EnumWindows", win_bool, (self._enum_callback, wintypes.LPARAM)),
            ("GetWindow", hwnd, (hwnd, wintypes.UINT)),
            ("GetClassNameW", ctypes.c_int, (hwnd, wintypes.LPWSTR, ctypes.c_int)),
            ("IsWindowVisible", win_bool, (hwnd,)),
            ("IsIconic", win_bool, (hwnd,)),
            ("IsChild", win_bool, (hwnd, hwnd)),
            ("GetForegroundWindow", hwnd, ()),
            ("SetForegroundWindow", win_bool, (hwnd,)),
            ("ShowWindow", win_bool, (hwnd, ctypes.c_int)),
            ("GetWindowRect", win_bool, (hwnd, rect_ptr)),
            ("MonitorFromWindow", wintypes.HMONITOR, (hwnd, wintypes.DWORD)),
            ("GetMonitorInfoW", win_bool, (wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo))),
            ("GetWindowLongW", wintypes.LONG, (hwnd, ctypes.c_int)),
            ("SetWindowLongW", wintypes.LONG, (hwnd, ctypes.c_int, wintypes.LONG)),
            ("SetWindowPos", win_bool, (hwnd, hwnd, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)),
            ("MapVirtualKeyW", wintypes.UINT, (wintypes.UINT, wintypes.UINT)),
            ("SendInput", wintypes.UINT, (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)),
        ):
            func = getattr(self._user32, name)
            func.restype = restype
            func.argtypes = argtypes

        self._get_dpi = getattr(self._user32, "GetDpiForWindow", None)
        if self._get_dpi is not None:
            self._get_dpi.restype = wintypes.UINT
            self._get_dpi.argtypes = (hwnd,)
        set_window_long = "SetWindowLongPtrW" if ctypes.sizeof(hwnd) == 8 else "SetWindowLongW"
        self._set_window_long_ptr = getattr(self._user32, set_window_long)
        self._set_window_long_ptr.restype = ctypes.c_ssize_t
        self._set_window_long_ptr.argtypes = (hwnd, ctypes.c_int, ctypes.c_ssize_t)

    def _process_id(self, hwnd: int) -> int:
        if not hwnd:
            return 0
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def process_id(self, hwnd: int) -> int:
        return self._process_id(hwnd)

    def windows_for_profile(self, profile_dir: Path) -> list[int]:
        # Chromium uses an exact profile path as the title of its message-only
        # singleton window, even when a second launch hands off to that process.
        singleton = self._user32.FindWindowExW(
            HWND_MESSAGE, None, "Chrome_MessageWindow", os.path.abspath(profile_dir)
        )
        if not singleton:
            return []
        pid = self._process_id(singleton)
        if not pid:
            return []

        windows: list[int] = []
        class_name = ctypes.create_unicode_buffer(64)

        @self._enum_callback
        def collect(hwnd, _):
            if (
                self._process_id(hwnd) == pid
                and self._user32.IsWindowVisible(hwnd)
                and not self._user32.GetWindow(hwnd, GW_OWNER)
                and self._user32.GetClassNameW(hwnd, class_name, len(class_name))
                and class_name.value.startswith("Chrome_WidgetWin_")
            ):
                windows.append(hwnd)
            return True

        self._user32.EnumWindows(collect, 0)
        return windows

    def is_chromium_window(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        class_name = ctypes.create_unicode_buffer(64)
        length = self._user32.GetClassNameW(hwnd, class_name, len(class_name))
        return length > 0 and class_name.value.startswith("Chrome_WidgetWin_")

    def is_child_or_self(self, parent_hwnd: int, target_hwnd: int) -> bool:
        if not parent_hwnd or not target_hwnd:
            return False
        return parent_hwnd == target_hwnd or bool(self._user32.IsChild(parent_hwnd, target_hwnd))

    def focus_window(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        if self._user32.IsIconic(hwnd):
            self._user32.ShowWindow(hwnd, SW_RESTORE)
        return bool(self._user32.SetForegroundWindow(hwnd))

    def send_ctrl_v_paste(self) -> None:
        """Send genuine hardware scan-code Ctrl+V paste events (zero VK_PACKET flags)."""
        ctrl_scan = self._user32.MapVirtualKeyW(VK_CONTROL, 0)
        v_scan = self._user32.MapVirtualKeyW(VK_V, 0)
        cb_size = ctypes.sizeof(_INPUT)

        down = (_INPUT * 2)()
        down[0].type = INPUT_KEYBOARD
        down[0].U.ki = _KEYBDINPUT(wVk=VK_CONTROL, wScan=ctrl_scan, dwFlags=0, time=0, dwExtraInfo=0)
        down[1].type = INPUT_KEYBOARD
        down[1].U.ki = _KEYBDINPUT(wVk=VK_V, wScan=v_scan, dwFlags=0, time=0, dwExtraInfo=0)
        self._user32.SendInput(2, down, cb_size)

        time.sleep(0.026)

        up = (_INPUT * 2)()
        up[0].type = INPUT_KEYBOARD
        up[0].U.ki = _KEYBDINPUT(wVk=VK_V, wScan=v_scan, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)
        up[1].type = INPUT_KEYBOARD
        up[1].U.ki = _KEYBDINPUT(wVk=VK_CONTROL, wScan=ctrl_scan, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)
        self._user32.SendInput(2, up, cb_size)

    def foreground(self) -> int:
        return self._user32.GetForegroundWindow() or 0

    def placement(self, hwnd: int, width: int, height: int) -> tuple[int, int, int, int] | None:
        if not self._user32.IsWindowVisible(hwnd) or self._user32.IsIconic(hwnd):
            return None
        browser = wintypes.RECT()
        if not self._user32.GetWindowRect(hwnd, ctypes.byref(browser)):
            return None
        monitor = self._user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        info = _MonitorInfo(size=ctypes.sizeof(_MonitorInfo))
        if not monitor or not self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        dpi = (self._get_dpi(hwnd) if self._get_dpi is not None else 96) or 96
        is_fullscreen = (
            browser.left == info.monitor.left
            and browser.top == info.monitor.top
            and browser.right == info.monitor.right
            and browser.bottom == info.monitor.bottom
        )
        bounds = info.monitor if is_fullscreen else info.work_area
        return _toolbar_rect(browser, bounds, width, height, dpi, fullscreen=is_fullscreen)

    def prepare_toolbar(self, hwnd: int) -> None:
        ctypes.set_last_error(0)
        original = self._user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not original and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        style = original | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        ctypes.set_last_error(0)
        previous = self._user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        if not previous and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        # WinForms gives a tool window a hidden surrogate owner when ShowInTaskbar is false.
        # Keep this toolbar independent of all other KeyVault windows.
        ctypes.set_last_error(0)
        previous_owner = self._set_window_long_ptr(hwnd, GWLP_HWNDPARENT, 0)
        if not previous_owner and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        self._user32.SetWindowPos(
            hwnd, None, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )

    def place_toolbar(self, hwnd: int, rect: tuple[int, int, int, int]) -> None:
        if not self._user32.SetWindowPos(hwnd, HWND_TOPMOST, *rect, SWP_NOACTIVATE | SWP_SHOWWINDOW):
            raise ctypes.WinError(ctypes.get_last_error())

    def move_toolbar(self, hwnd: int, x: int, y: int) -> None:
        if not self._user32.SetWindowPos(
            hwnd, HWND_TOPMOST, int(x), int(y), 0, 0,
            SWP_NOACTIVATE | SWP_NOSIZE | SWP_SHOWWINDOW,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def hide_toolbar(self, hwnd: int) -> None:
        self._user32.SetWindowPos(
            hwnd, None, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_HIDEWINDOW,
        )

    def is_topmost(self, hwnd: int) -> bool:
        """Return whether ``hwnd`` currently carries the Win32 ``WS_EX_TOPMOST`` bit."""
        if not hwnd:
            return False
        return bool(self._user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)

    def is_owned_by(self, owner_hwnd: int, target_hwnd: int) -> bool:
        """Return whether ``target_hwnd`` is owned by ``owner_hwnd`` (e.g. a Chrome prompt)."""
        if not owner_hwnd or not target_hwnd:
            return False
        cur = target_hwnd
        for _ in range(4):
            cur = self._user32.GetWindow(cur, GW_OWNER)
            if not cur:
                return False
            if cur == owner_hwnd:
                return True
        return False


def is_window_topmost(hwnd: int) -> bool:
    """Return whether ``hwnd`` has Win32 ``WS_EX_TOPMOST`` set (non-Windows safe)."""
    if sys.platform != "win32" or not hwnd:
        return True
    user32 = ctypes.WinDLL("user32", use_last_error=False)
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    return bool(user32.GetWindowLongW(int(hwnd), GWL_EXSTYLE) & WS_EX_TOPMOST)
