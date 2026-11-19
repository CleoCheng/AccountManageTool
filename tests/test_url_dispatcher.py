import sys
import threading
import time
from pathlib import Path

import pytest

import src.url_dispatcher as ud


def test_extract_url_and_classification():
    assert ud.extract_url_from_args(["--url=https://accounts.google.com/o/oauth2/v2/auth?a=1"]) == "https://accounts.google.com/o/oauth2/v2/auth?a=1"
    assert ud.extract_url_from_args(["--url", "https://claude.ai/login"]) == "https://claude.ai/login"
    assert ud.extract_url_from_args(['"https://chatgpt.com/auth/login"']) == "https://chatgpt.com/auth/login"
    assert ud.extract_url_from_args(["--verify-init"]) is None

    assert ud.is_target_auth_url("https://accounts.google.com/v3/signin/continue?plt=abc") is True
    assert ud.is_target_auth_url("https://claude.ai/oauth/authorize") is True
    assert ud.is_target_auth_url("https://auth.openai.com/authorize") is True
    assert ud.is_target_auth_url("https://example.com/news") is False

    assert ud.detect_url_platform("https://claude.ai/login") == "Claude"
    assert ud.detect_url_platform("https://chatgpt.com/") == "ChatGPT"
    assert ud.detect_url_platform("https://accounts.google.com/") == "Google"


def test_named_pipe_round_trip(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows Named Pipe test")

    unique_pipe = rf"\\.\pipe\KeyVault_Test_Pipe_{int(time.time() * 1000)}"
    monkeypatch.setattr(ud, "PIPE_NAME", unique_pipe)

    received: list[str] = []
    ev = threading.Event()

    def _on_url(u: str):
        received.append(u)
        ev.set()

    stop = ud.start_pipe_server(_on_url)
    try:
        time.sleep(0.15)
        ok = ud.try_forward_to_running_instance("https://accounts.google.com/v3/signin/continue?plt=xyz", timeout_ms=1500)
        assert ok is True
        assert ev.wait(timeout=2.0) is True
        assert received == ["https://accounts.google.com/v3/signin/continue?plt=xyz"]
    finally:
        stop.set()


def test_named_pipe_sanitizes_newlines_bounds_size_and_nonblocking(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows Named Pipe test")

    unique_pipe = rf"\\.\pipe\KeyVault_Test_Pipe_NB_{int(time.time() * 1000)}"
    monkeypatch.setattr(ud, "PIPE_NAME", unique_pipe)

    gate = threading.Event()
    received: list[str] = []
    second_done = threading.Event()

    def _on_url(u: str):
        received.append(u)
        if "first" in u:
            gate.wait(timeout=2.0)
        elif "second" in u:
            second_done.set()

    stop = ud.start_pipe_server(_on_url)
    try:
        time.sleep(0.15)
        # Newline injection stripped into single valid URL
        assert ud.try_forward_to_running_instance(
            "https://accounts.google.com/first\r\nhttps://evil.example/", timeout_ms=1500
        )
        # Second arrives while first callback is blocked on gate
        assert ud.try_forward_to_running_instance(
            "https://claude.ai/second", timeout_ms=1500
        )
        assert second_done.wait(timeout=2.0) is True
        # Oversized payload (> 64KB) rejected cleanly by client
        huge = "https://accounts.google.com/" + ("a" * (ud.MAX_PIPE_MESSAGE_BYTES + 100))
        assert ud.try_forward_to_running_instance(huge, timeout_ms=500) is False
    finally:
        gate.set()
        stop.set()

    assert len(received) == 2
    assert received[0] == "https://accounts.google.com/firsthttps://evil.example/"
    assert received[1] == "https://claude.ai/second"


class _FakeRegKey:
    def __init__(self, store: dict, path: str):
        self._store = store
        self._path = path

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _make_fake_winreg(monkeypatch, store: dict[str, dict]):
    import types

    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER = "HKCU"
    fake.KEY_ALL_ACCESS = 0xF003F
    fake.KEY_SET_VALUE = 0x0002
    fake.REG_SZ = 1
    fake.REG_DWORD = 4

    def _norm(p: str) -> str:
        return p.strip("\\").lower()

    def OpenKey(_root, subkey, _reserved=0, _access=0):
        np = _norm(subkey)
        if np not in store:
            raise FileNotFoundError(subkey)
        return _FakeRegKey(store, np)

    def CreateKey(_root, subkey):
        parts = _norm(subkey).split("\\")
        for i in range(1, len(parts) + 1):
            store.setdefault("\\".join(parts[:i]), {})
        return _FakeRegKey(store, "\\".join(parts))

    def QueryValueEx(key_obj, name):
        vals = store.get(key_obj._path, {})
        k = "(default)" if name is None else name
        if k not in vals:
            raise FileNotFoundError(k)
        return vals[k], 1

    def SetValueEx(key_obj, name, _res, _typ, val):
        vals = store.setdefault(key_obj._path, {})
        k = "(default)" if name is None else name
        vals[k] = val

    def EnumKey(key_obj, index):
        prefix = key_obj._path + "\\"
        children = sorted(
            {
                p[len(prefix) :].split("\\", 1)[0]
                for p in store
                if p.startswith(prefix)
            }
        )
        if index >= len(children):
            raise OSError("No more data")
        return children[index]

    def DeleteKey(_root, subkey):
        np = _norm(subkey)
        prefix = np + "\\"
        if any(p.startswith(prefix) for p in store):
            raise OSError("Subkeys exist")
        if np not in store:
            raise FileNotFoundError(subkey)
        store.pop(np, None)

    def DeleteValue(key_obj, name):
        vals = store.get(key_obj._path, {})
        k = "(default)" if name is None else name
        if k not in vals:
            raise FileNotFoundError(k)
        vals.pop(k, None)

    fake.OpenKey = OpenKey
    fake.CreateKey = CreateKey
    fake.QueryValueEx = QueryValueEx
    fake.SetValueEx = SetValueEx
    fake.EnumKey = EnumKey
    fake.DeleteKey = DeleteKey
    fake.DeleteValue = DeleteValue
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return fake


def test_url_interception_enable_disable_preserves_siblings_and_restores(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows Registry test")

    store: dict[str, dict] = {
        r"software\microsoft\windows\shell\associations\urlassociations\https\userchoice": {"ProgId": "ChromeHTML"},
        r"software\classes\chromehtml\application": {"ApplicationName": "Google Chrome"},
        r"software\classes\chromehtml\shell\open\command": {"(default)": '"C:\\Chrome\\chrome.exe" -- "%1"'},
        r"software\classes\msedgehtm\application": {"ApplicationName": "Microsoft Edge"},
    }
    _make_fake_winreg(monkeypatch, store)

    assert ud.enable_windows_url_interception(dispatch_cmd='"C:\\KeyVault\\KeyVault.exe" --url "%1"') is True
    assert ud.is_windows_url_interception_enabled() is True
    assert (
        store[r"software\classes\chromehtml\shell\open\command"]["(default)"]
        == '"C:\\KeyVault\\KeyVault.exe" --url "%1"'
    )
    assert r"software\classes\keyvaulturl\shell\open\command" in store
    assert r"software\clients\startmenuinternet\keyvault" in store

    # Re-enabling does not overwrite original backup
    assert ud.enable_windows_url_interception(dispatch_cmd='"C:\\KeyVault\\KeyVault.exe" --url "%1"') is True

    assert ud.disable_windows_url_interception() is True
    assert ud.is_windows_url_interception_enabled() is False
    # Chrome's original HKCU command is restored and Application subkeys are preserved
    assert (
        store[r"software\classes\chromehtml\shell\open\command"]["(default)"]
        == '"C:\\Chrome\\chrome.exe" -- "%1"'
    )
    assert store[r"software\classes\chromehtml\application"]["ApplicationName"] == "Google Chrome"
    assert store[r"software\classes\msedgehtm\application"]["ApplicationName"] == "Microsoft Edge"
    # KeyVault keys cleaned up completely
    assert r"software\classes\keyvaulturl" not in store
    assert r"software\clients\startmenuinternet\keyvault" not in store
    assert "KeyVault" not in store.get(r"software\registeredapplications", {})


def test_forward_to_original_browser_respects_user_choice_and_prevents_loop(monkeypatch):
    import src.browser_service as bs

    launched: list[list[str]] = []
    opened_fallback: list[str] = []
    monkeypatch.setattr(bs, "detect_chrome_path", lambda: "C:\\Fake\\chrome.exe")
    monkeypatch.setattr(bs, "detect_edge_path", lambda: "C:\\Fake\\msedge.exe")
    monkeypatch.setattr(ud.os.path, "exists", lambda p: p in {"C:\\Fake\\chrome.exe", "C:\\Fake\\msedge.exe"})
    monkeypatch.setattr(ud.subprocess, "Popen", lambda args, **_kw: launched.append(args))
    monkeypatch.setattr(ud, "_get_user_choice_prog_id", lambda scheme: "ChromeHTML" if scheme == "https" else "")

    assert ud.forward_to_original_browser("https://example.com/ok") is True
    assert launched == [["C:\\Fake\\chrome.exe", "https://example.com/ok"]]

    # When neither browser exists and interception is enabled, never invoke webbrowser.open loop
    monkeypatch.setattr(ud.os.path, "exists", lambda _p: False)
    monkeypatch.setattr(ud, "is_windows_url_interception_enabled", lambda: True)
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda u: opened_fallback.append(u) or True)
    assert ud.forward_to_original_browser("https://example.com/loop") is False
    assert opened_fallback == []


def test_api_toggle_url_interception_does_not_overwrite_settings_on_failure(monkeypatch, tmp_path):
    from src.api import AccountApi
    import src.api as api_mod

    api = AccountApi(tmp_path / "data", base_dir=tmp_path)
    api.save_settings({"url_interception_enabled": True})

    monkeypatch.setattr(api_mod, "disable_windows_url_interception", lambda: False)
    monkeypatch.setattr(api_mod, "is_windows_url_interception_enabled", lambda: True)
    res = api.toggle_url_interception(False)
    assert res["ok"] is False
    assert res["enabled"] is True
    assert api._ensure_settings()["url_interception_enabled"] is True
