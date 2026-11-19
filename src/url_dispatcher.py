"""Windows URL dispatcher & Named Pipe single-instance interceptor.

Intercepts external Google / Antigravity / Claude / ChatGPT auth and
verification URLs from desktop apps (e.g. Antigravity's [Verify], Claude Code,
or Electron ``shell.openExternal`` calls) and routes them straight into the
active isolated browser profile before Edge/default browser can consume a
one-time token. All unrelated HTTP/HTTPS URLs are forwarded transparently to
the user's native Edge/Chrome browser.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path


PIPE_NAME = r"\\.\pipe\KeyVault_UrlDispatch_Pipe_v1"
MAX_PIPE_MESSAGE_BYTES = 64 * 1024
_EDGE_PROG_ID = "MSEdgeHTM"
_CHROME_PROG_ID = "ChromeHTML"
_BACKUP_REG_PATH = r"Software\KeyVault\UrlDispatcherBackup"
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_k32_lock = threading.Lock()
_k32_instance = None


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


def _get_kernel32():
    global _k32_instance
    with _k32_lock:
        if _k32_instance is not None:
            return _k32_instance
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        k32.WaitNamedPipeW.restype = wintypes.BOOL
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.WriteFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        k32.WriteFile.restype = wintypes.BOOL
        k32.CreateNamedPipeW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        k32.CreateNamedPipeW.restype = wintypes.HANDLE
        k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        k32.ConnectNamedPipe.restype = wintypes.BOOL
        k32.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        k32.ReadFile.restype = wintypes.BOOL
        k32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        k32.DisconnectNamedPipe.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.SetEvent.argtypes = [wintypes.HANDLE]
        k32.SetEvent.restype = wintypes.BOOL
        k32.ResetEvent.argtypes = [wintypes.HANDLE]
        k32.ResetEvent.restype = wintypes.BOOL
        k32.WaitForMultipleObjects.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        k32.WaitForMultipleObjects.restype = wintypes.DWORD
        k32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        ]
        k32.GetOverlappedResult.restype = wintypes.BOOL
        k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        k32.CancelIoEx.restype = wintypes.BOOL
        _k32_instance = k32
        return k32


def _is_valid_handle(handle) -> bool:
    return bool(handle) and handle != _INVALID_HANDLE_VALUE and handle != -1
_AUTH_URL_RE = re.compile(
    r"accounts\.google\.com|myaccount\.google\.com|g\.co/|google\.com/device|"
    r"continue\?plt=|webapproval|cloudcode|gemini-code-assist|policies\.google\.com|"
    r"forms\.gle|aistudio\.google\.com|makersuite\.google\.com|idx\.google\.com|"
    r"claude\.ai|anthropic\.com|console\.anthropic\.com|platform\.claude\.com|"
    r"auth\.anthropic\.com|claude\.com|"
    r"chatgpt\.com|auth\.openai\.com|auth0\.openai\.com|platform\.openai\.com|chat\.openai\.com",
    re.IGNORECASE,
)
_CLAUDE_URL_RE = re.compile(r"claude\.ai|anthropic\.com|claude\.com", re.IGNORECASE)
_CHATGPT_URL_RE = re.compile(r"chatgpt\.com|openai\.com", re.IGNORECASE)


def _is_http_url(text: str | None) -> bool:
    if not text or not text.strip():
        return False
    low = text.strip().lower()
    return low.startswith("http://") or low.startswith("https://")


def extract_url_from_args(argv: list[str] | None) -> str | None:
    """Extract an HTTP(S) URL passed via ``--url=<u >``, ``--url <u>``, or bare arg."""
    if not argv:
        return None
    i = 0
    while i < len(argv):
        arg = (argv[i] or "").strip()
        if arg.lower().startswith("--url="):
            val = arg[6:].strip("\"'")
            if _is_http_url(val):
                return val
        elif arg.lower() == "--url" and i + 1 < len(argv):
            nxt = (argv[i + 1] or "").strip("\"'")
            if _is_http_url(nxt):
                return nxt
            i += 1
        elif _is_http_url(arg.strip("\"'")):
            return arg.strip("\"'")
        i += 1
    return None


def is_target_auth_url(url: str | None) -> bool:
    """Return True if ``url`` belongs to Google/Antigravity, Claude, or ChatGPT auth."""
    if not url or not url.strip():
        return False
    return bool(_AUTH_URL_RE.search(url))


def detect_url_platform(url: str | None) -> str:
    """Classify an intercepted URL into ``Claude``, ``ChatGPT``, or ``Google``."""
    if not url:
        return "Google"
    if _CLAUDE_URL_RE.search(url):
        return "Claude"
    if _CHATGPT_URL_RE.search(url):
        return "ChatGPT"
    return "Google"


def try_forward_to_running_instance(url: str, timeout_ms: int = 800) -> bool:
    """Forward ``url`` to a running KeyVault Named Pipe server if available."""
    if sys.platform != "win32" or not url:
        return False
    clean_url = url.replace("\r", "").replace("\n", "").strip()
    if not _is_http_url(clean_url):
        return False
    payload = (clean_url + "\n").encode("utf-8")
    if len(payload) > MAX_PIPE_MESSAGE_BYTES:
        return False
    try:
        k32 = _get_kernel32()
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        ERROR_FILE_NOT_FOUND = 2
        ERROR_PIPE_BUSY = 231
        deadline = time.monotonic() + max(0.05, float(timeout_ms) / 1000.0)
        handle = None
        while True:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            if k32.WaitNamedPipeW(PIPE_NAME, remaining_ms):
                candidate = k32.CreateFileW(
                    PIPE_NAME,
                    GENERIC_WRITE,
                    0,
                    None,
                    OPEN_EXISTING,
                    0,
                    None,
                )
                if _is_valid_handle(candidate):
                    handle = candidate
                    break
                err = ctypes.get_last_error()
            else:
                err = ctypes.get_last_error()
            if time.monotonic() >= deadline or err not in (ERROR_FILE_NOT_FOUND, ERROR_PIPE_BUSY):
                return False
            time.sleep(0.015)

        try:
            buf = ctypes.create_string_buffer(payload, len(payload))
            written = wintypes.DWORD(0)
            ok = k32.WriteFile(
                handle,
                ctypes.cast(buf, ctypes.c_void_p),
                len(payload),
                ctypes.byref(written),
                None,
            )
            return bool(ok and written.value == len(payload))
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False


class _PipeStopEvent(threading.Event):
    """Stop event that also signals a Win32 event handle so overlapped waits wake immediately."""

    def __init__(self, win_event_handle):
        super().__init__()
        self._lock = threading.Lock()
        self._win_event = win_event_handle

    def set(self) -> None:
        super().set()
        with self._lock:
            if _is_valid_handle(self._win_event):
                try:
                    _get_kernel32().SetEvent(self._win_event)
                except Exception:
                    pass

    def close_win_event(self) -> None:
        with self._lock:
            if _is_valid_handle(self._win_event):
                try:
                    _get_kernel32().CloseHandle(self._win_event)
                except Exception:
                    pass
                self._win_event = None


def start_pipe_server(on_url_callback) -> threading.Event:
    """Start background Windows Named Pipe server to receive forwarded URLs."""
    if sys.platform != "win32":
        return threading.Event()

    k32 = _get_kernel32()
    stop_win_event = k32.CreateEventW(None, True, False, None)
    stop_event = _PipeStopEvent(stop_win_event)
    pipe_name = PIPE_NAME
    cb_queue: queue.Queue[str | None] = queue.Queue(maxsize=64)

    def _dispatch_worker() -> None:
        while True:
            item = cb_queue.get()
            if item is None:
                cb_queue.task_done()
                break
            try:
                on_url_callback(item)
            except Exception:
                pass
            finally:
                cb_queue.task_done()

    for idx in range(2):
        threading.Thread(
            target=_dispatch_worker,
            name=f"kv-url-cb-{idx}",
            daemon=True,
        ).start()

    def _serve() -> None:
        PIPE_ACCESS_INBOUND = 0x00000001
        FILE_FLAG_OVERLAPPED = 0x40000000
        PIPE_TYPE_BYTE = 0x00000000
        PIPE_WAIT = 0x00000000
        PIPE_UNLIMITED_INSTANCES = 255
        ERROR_PIPE_CONNECTED = 535
        ERROR_NO_DATA = 232
        ERROR_IO_PENDING = 997
        WAIT_OBJECT_0 = 0
        INFINITE = 0xFFFFFFFF

        def _create_pipe():
            return k32.CreateNamedPipeW(
                pipe_name,
                PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED,
                PIPE_TYPE_BYTE | PIPE_WAIT,
                PIPE_UNLIMITED_INSTANCES,
                4096,
                4096,
                500,
                None,
            )

        io_event = k32.CreateEventW(None, True, False, None)
        if not _is_valid_handle(io_event):
            stop_event.close_win_event()
            return

        wait_handles = (wintypes.HANDLE * 2)(stop_win_event, io_event)
        pipe = None
        try:
            while not stop_event.is_set():
                if not _is_valid_handle(pipe):
                    pipe = _create_pipe()
                if not _is_valid_handle(pipe):
                    stop_event.wait(0.25)
                    continue
                next_pipe = None
                try:
                    k32.ResetEvent(io_event)
                    ov_conn = _OVERLAPPED(hEvent=io_event)
                    connected = k32.ConnectNamedPipe(pipe, ctypes.byref(ov_conn))
                    if not connected:
                        err = ctypes.get_last_error()
                        if err == ERROR_IO_PENDING:
                            wait_res = k32.WaitForMultipleObjects(2, wait_handles, False, INFINITE)
                            if wait_res == WAIT_OBJECT_0 or stop_event.is_set():
                                k32.CancelIoEx(pipe, ctypes.byref(ov_conn))
                                dummy = wintypes.DWORD(0)
                                k32.GetOverlappedResult(pipe, ctypes.byref(ov_conn), ctypes.byref(dummy), True)
                                break
                            if wait_res != WAIT_OBJECT_0 + 1:
                                continue
                            dummy = wintypes.DWORD(0)
                            if not k32.GetOverlappedResult(pipe, ctypes.byref(ov_conn), ctypes.byref(dummy), False):
                                if ctypes.get_last_error() not in (ERROR_PIPE_CONNECTED, ERROR_NO_DATA):
                                    continue
                        elif err not in (ERROR_PIPE_CONNECTED, ERROR_NO_DATA):
                            continue

                    if stop_event.is_set():
                        break

                    # Pre-create the next listening instance before reading/closing
                    # so incoming bursts never hit a zero-instance gap.
                    next_pipe = _create_pipe()

                    chunks: list[bytes] = []
                    total_bytes = 0
                    buf = ctypes.create_string_buffer(4096)
                    buf_ptr = ctypes.cast(buf, ctypes.c_void_p)
                    while total_bytes < MAX_PIPE_MESSAGE_BYTES and not stop_event.is_set():
                        k32.ResetEvent(io_event)
                        ov_read = _OVERLAPPED(hEvent=io_event)
                        bytes_read = wintypes.DWORD(0)
                        ok = k32.ReadFile(pipe, buf_ptr, 4096, ctypes.byref(bytes_read), ctypes.byref(ov_read))
                        if not ok:
                            err = ctypes.get_last_error()
                            if err == ERROR_IO_PENDING:
                                wait_res = k32.WaitForMultipleObjects(2, wait_handles, False, 5000)
                                if wait_res != WAIT_OBJECT_0 + 1:
                                    k32.CancelIoEx(pipe, ctypes.byref(ov_read))
                                    dummy = wintypes.DWORD(0)
                                    k32.GetOverlappedResult(pipe, ctypes.byref(ov_read), ctypes.byref(dummy), True)
                                    break
                                if not k32.GetOverlappedResult(
                                    pipe, ctypes.byref(ov_read), ctypes.byref(bytes_read), False
                                ):
                                    break
                            else:
                                break
                        if bytes_read.value <= 0:
                            break
                        take = min(int(bytes_read.value), MAX_PIPE_MESSAGE_BYTES - total_bytes)
                        chunks.append(buf.raw[:take])
                        total_bytes += take

                    text = b"".join(chunks).decode("utf-8", errors="replace").strip()
                    if text:
                        for line in text.splitlines():
                            candidate = line.strip()
                            if _is_http_url(candidate):
                                try:
                                    cb_queue.put_nowait(candidate)
                                except queue.Full:
                                    pass
                except Exception:
                    pass
                finally:
                    try:
                        k32.DisconnectNamedPipe(pipe)
                    except Exception:
                        pass
                    try:
                        k32.CloseHandle(pipe)
                    except Exception:
                        pass
                    pipe = next_pipe
        finally:
            if _is_valid_handle(pipe):
                try:
                    k32.CloseHandle(pipe)
                except Exception:
                    pass
            for _ in range(2):
                try:
                    cb_queue.put_nowait(None)
                except queue.Full:
                    pass
            try:
                k32.CloseHandle(io_event)
            except Exception:
                pass
            stop_event.close_win_event()

    threading.Thread(target=_serve, name="kv-url-pipe", daemon=True).start()
    return stop_event


def _get_user_choice_prog_id(scheme: str) -> str:
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        path = rf"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\{scheme}\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            val, _ = winreg.QueryValueEx(key, "ProgId")
            return str(val or "").strip()
    except Exception:
        return ""


def _read_hkcu_command(prog_id: str) -> str:
    if sys.platform != "win32" or not prog_id:
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\{prog_id}\shell\open\command",
        ) as key:
            val, _ = winreg.QueryValueEx(key, None)
            return str(val or "")
    except Exception:
        return ""


def _is_keyvault_cmd(cmd: str | None) -> bool:
    low = (cmd or "").lower()
    return bool(low and ("keyvault" in low or "main.py" in low))


def _hkcu_command_exists(prog_id: str) -> tuple[bool, str]:
    if sys.platform != "win32" or not prog_id:
        return False, ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\{prog_id}\shell\open\command",
        ) as key:
            try:
                val, _ = winreg.QueryValueEx(key, None)
            except FileNotFoundError:
                return True, ""
            return True, str(val or "")
    except Exception:
        return False, ""


def is_windows_url_interception_enabled() -> bool:
    """Return True if HKCU URL association currently routes through KeyVault."""
    if sys.platform != "win32":
        return False
    https_prog = _get_user_choice_prog_id("https")
    http_prog = _get_user_choice_prog_id("http")
    for pid in (_EDGE_PROG_ID, _CHROME_PROG_ID, https_prog, http_prog):
        if pid and pid.lower() == "keyvaulturl":
            cmd = _read_hkcu_command("KeyVaultURL")
            if _is_keyvault_cmd(cmd):
                return True
            continue
        cmd = _read_hkcu_command(pid)
        if _is_keyvault_cmd(cmd):
            return True
    return False


def _resolve_dispatch_command(base_dir: Path | None = None) -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --url "%1"'
    root = Path(base_dir).resolve() if base_dir else Path(__file__).resolve().parent.parent
    launcher = root / "KeyVault.exe"
    if launcher.is_file():
        return f'"{launcher}" --url "%1"'
    py_exe = Path(sys.executable)
    pyw = py_exe.with_name("pythonw.exe")
    runner = str(pyw if pyw.is_file() else py_exe)
    main_py = root / "main.py"
    return f'"{runner}" "{main_py}" --url "%1"'


def _backup_hkcu_command(winreg, prog_id: str) -> None:
    backup_subkey = rf"{_BACKUP_REG_PATH}\{prog_id}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, backup_subkey):
            return
    except OSError:
        pass
    existed, cur_cmd = _hkcu_command_exists(prog_id)
    if existed and _is_keyvault_cmd(cur_cmd):
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, backup_subkey) as bkey:
        winreg.SetValueEx(bkey, "Existed", 0, winreg.REG_DWORD, 1 if existed else 0)
        winreg.SetValueEx(bkey, "OriginalCommand", 0, winreg.REG_SZ, cur_cmd if existed else "")


def _list_backed_up_prog_ids(winreg) -> set[str]:
    found: set[str] = set()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _BACKUP_REG_PATH) as key:
            idx = 0
            while True:
                try:
                    found.add(winreg.EnumKey(key, idx))
                    idx += 1
                except OSError:
                    break
    except OSError:
        pass
    return found


def _restore_or_clear_hkcu_command(winreg, prog_id: str) -> None:
    restored = False
    backup_subkey = rf"{_BACKUP_REG_PATH}\{prog_id}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, backup_subkey) as bkey:
            existed, _ = winreg.QueryValueEx(bkey, "Existed")
            orig_cmd, _ = winreg.QueryValueEx(bkey, "OriginalCommand")
            if int(existed or 0) == 1 and orig_cmd and not _is_keyvault_cmd(str(orig_cmd)):
                with winreg.CreateKey(
                    winreg.HKEY_CURRENT_USER,
                    rf"Software\Classes\{prog_id}\shell\open\command",
                ) as cmd_key:
                    winreg.SetValueEx(cmd_key, None, 0, winreg.REG_SZ, str(orig_cmd))
                restored = True
    except OSError:
        pass
    if not restored:
        _delete_reg_tree(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\{prog_id}\shell\open\command",
        )


def _delete_reg_value(root_key, subkey: str, value_name: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(root_key, subkey, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)
    except OSError:
        pass


def enable_windows_url_interception(
    *,
    base_dir: Path | None = None,
    dispatch_cmd: str | None = None,
) -> bool:
    """Enable HKCU user-mode URL interception for Edge/Chrome/UserChoice ProgIDs."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        cmd = dispatch_cmd or _resolve_dispatch_command(base_dir)
        prog_ids = {_EDGE_PROG_ID, _CHROME_PROG_ID}
        for scheme in ("https", "http"):
            pid = _get_user_choice_prog_id(scheme)
            if pid and pid.lower() != "keyvaulturl":
                prog_ids.add(pid)

        for prog_id in prog_ids:
            _backup_hkcu_command(winreg, prog_id)
            with winreg.CreateKey(
                winreg.HKEY_CURRENT_USER,
                rf"Software\Classes\{prog_id}\shell\open\command",
            ) as key:
                winreg.SetValueEx(key, None, 0, winreg.REG_SZ, cmd)

        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Classes\KeyVaultURL\shell\open\command",
        ) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, cmd)

        client_key = r"Software\Clients\StartMenuInternet\KeyVault"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, client_key) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "KeyVault URL Dispatcher")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{client_key}\shell\open\command") as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, cmd)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{client_key}\Capabilities") as key:
            winreg.SetValueEx(key, "ApplicationName", 0, winreg.REG_SZ, "KeyVault")
            winreg.SetValueEx(
                key,
                "ApplicationDescription",
                0,
                winreg.REG_SZ,
                "KeyVault Fingerprint & Auth URL Dispatcher",
            )
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, rf"{client_key}\Capabilities\URLAssociations"
        ) as key:
            winreg.SetValueEx(key, "http", 0, winreg.REG_SZ, "KeyVaultURL")
            winreg.SetValueEx(key, "https", 0, winreg.REG_SZ, "KeyVaultURL")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\RegisteredApplications") as key:
            winreg.SetValueEx(key, "KeyVault", 0, winreg.REG_SZ, rf"{client_key}\Capabilities")
        return True
    except Exception:
        return False


def _delete_reg_tree(root_key, subkey: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(root_key, subkey, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                    _delete_reg_tree(root_key, rf"{subkey}\{child}")
                except OSError:
                    break
        winreg.DeleteKey(root_key, subkey)
    except OSError:
        pass


def disable_windows_url_interception() -> bool:
    """Remove HKCU URL interception overrides, restoring native system browser handling."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        prog_ids = {_EDGE_PROG_ID, _CHROME_PROG_ID}
        prog_ids.update(_list_backed_up_prog_ids(winreg))
        for scheme in ("https", "http"):
            pid = _get_user_choice_prog_id(scheme)
            if pid and pid.lower() != "keyvaulturl":
                prog_ids.add(pid)

        for prog_id in prog_ids:
            _restore_or_clear_hkcu_command(winreg, prog_id)

        _delete_reg_tree(winreg.HKEY_CURRENT_USER, r"Software\Classes\KeyVaultURL")
        _delete_reg_tree(winreg.HKEY_CURRENT_USER, r"Software\Clients\StartMenuInternet\KeyVault")
        _delete_reg_value(winreg.HKEY_CURRENT_USER, r"Software\RegisteredApplications", "KeyVault")
        _delete_reg_tree(winreg.HKEY_CURRENT_USER, _BACKUP_REG_PATH)
        return True
    except Exception:
        return False


def forward_to_original_browser(url: str) -> bool:
    """Open a non-auth URL directly in system Edge/Chrome without re-entering KeyVault."""
    if not _is_http_url(url):
        return False
    try:
        from src.browser_service import detect_chrome_path, detect_edge_path

        active_prog = _get_user_choice_prog_id("https") or _get_user_choice_prog_id("http") or ""
        prefer_chrome = active_prog.lower().startswith("chrome")
        chrome_path = detect_chrome_path()
        edge_path = detect_edge_path()
        candidates = (chrome_path, edge_path) if prefer_chrome else (edge_path, chrome_path)
        for exe in candidates:
            if exe and os.path.exists(exe):
                subprocess.Popen([exe, url], close_fds=True)
                return True
    except Exception:
        pass
    if is_windows_url_interception_enabled():
        return False
    try:
        import webbrowser

        return bool(webbrowser.open(url))
    except Exception:
        return False
