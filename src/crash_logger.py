"""Lightweight, thread-safe crash logging for the windowed (pythonw) build.

Nothing in this module is allowed to raise: logging failures are swallowed so a
broken log path can never turn into a second crash.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

DEFAULT_MAX_BYTES = 2 * 1024 * 1024

_log_path: Path | None = None
_winforms_handlers_installed: bool = False
_lock = threading.Lock()


def configure_crash_log(log_path: Path) -> Path:
    """Remember ``log_path`` and make sure its parent directory exists."""
    path = Path(log_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    global _log_path
    with _lock:
        _log_path = path
    return path


def _rotate_if_needed(path: Path, incoming_bytes: int) -> None:
    try:
        if path.exists() and path.stat().st_size + incoming_bytes > DEFAULT_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


def log_exception(
    source: str,
    exc_info=None,
    *,
    extra: str = "",
    log_path: Path | None = None,
) -> None:
    """Append a crash block (timestamp / thread / source / traceback) to the log.

    Never raises: write or formatting failures are silently ignored.
    """
    try:
        path = Path(log_path) if log_path is not None else _log_path
        if path is None:
            return

        if exc_info is None:
            exc_info = sys.exc_info()
        exc_type, exc_value, exc_tb = exc_info or (None, None, None)
        if exc_type is None and exc_value is None and exc_tb is None:
            trace_text = "(no active exception)\n"
        else:
            try:
                trace_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            except Exception:
                trace_text = f"{getattr(exc_type, '__name__', exc_type)}: {exc_value}\n"

        thread = threading.current_thread()
        timestamp = datetime.now().isoformat(sep=" ", timespec="seconds")
        lines = [f"[{timestamp}] [{thread.name}/{thread.ident}] {source}"]
        if extra:
            lines.append(str(extra))
        lines.append(trace_text.rstrip("\n"))
        block = "\n".join(lines) + "\n\n"
        encoded = block.encode("utf-8", errors="replace")

        with _lock:
            _rotate_if_needed(path, len(encoded))
            with path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(block)
    except Exception:
        pass


def install_crash_handlers(log_path: Path) -> Path:
    """Install ``sys.excepthook`` / ``threading.excepthook`` crash logging."""
    path = configure_crash_log(log_path)

    previous_excepthook = sys.excepthook

    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            previous_excepthook(exc_type, exc_value, exc_tb)
            return
        log_exception("sys.excepthook", (exc_type, exc_value, exc_tb), log_path=path)
        try:
            previous_excepthook(exc_type, exc_value, exc_tb)
        except Exception:
            pass

    sys.excepthook = _excepthook

    if hasattr(threading, "excepthook"):
        previous_thread_hook = threading.excepthook

        def _thread_excepthook(args):
            try:
                thread_name = getattr(getattr(args, "thread", None), "name", "")
                log_exception(
                    f"threading.excepthook:{thread_name}",
                    (args.exc_type, args.exc_value, args.exc_traceback),
                    log_path=path,
                )
            except Exception:
                pass
            try:
                previous_thread_hook(args)
            except Exception:
                pass

        threading.excepthook = _thread_excepthook

    return path


def install_winforms_exception_handlers(log_path: Path | None = None) -> None:
    """Route WinForms / .NET unhandled exceptions into ``crash.log``.

    Silently does nothing when pythonnet or WinForms is unavailable (e.g. in
    headless test environments). Safe to call repeatedly (idempotent).
    """
    global _winforms_handlers_installed
    if sys.platform != "win32":
        return
    if log_path is not None:
        path = configure_crash_log(Path(log_path))
    else:
        path = _log_path
    if path is None:
        return

    with _lock:
        if _winforms_handlers_installed:
            return

    try:
        import clr
        try:
            clr.AddReference("System.Windows.Forms")
        except Exception:
            pass
        from System import AppDomain
        from System.Windows.Forms import Application, UnhandledExceptionMode
    except Exception:
        return

    try:
        try:
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException)
        except Exception:
            # Windows already created: the mode can no longer be changed.
            pass

        def _record(source: str, exc) -> None:
            try:
                to_string = getattr(exc, "ToString", None)
                detail = str(to_string()) if callable(to_string) else str(exc or "")
                log_exception(
                    source,
                    (type(exc) if exc is not None else None, exc, getattr(exc, "__traceback__", None)),
                    extra=detail,
                    log_path=None,
                )
            except Exception:
                pass

        def _on_thread_exception(_sender, args):
            _record("winforms.Application.ThreadException", getattr(args, "Exception", None))

        def _on_unhandled_exception(_sender, args):
            _record(
                "winforms.AppDomain.UnhandledException",
                getattr(args, "ExceptionObject", None),
            )

        with _lock:
            if _winforms_handlers_installed:
                return
            Application.ThreadException += _on_thread_exception
            AppDomain.CurrentDomain.UnhandledException += _on_unhandled_exception
            _winforms_handlers_installed = True
    except Exception:
        pass
