import sys
from pathlib import Path
from types import SimpleNamespace

from src.api import AccountApi
from src.login_tools_panel import LoginToolsPanel
import src.login_tools_panel as panel_module


def test_browser_launch_registers_only_successful_account_launch(tmp_path: Path, monkeypatch):
    api = AccountApi(tmp_path)
    chat = api.save_account({"account": "chat@example.test", "password": "synthetic"})["account"]
    google = api.save_account({"account_type": "Google", "account": "google@example.test"})["account"]
    assert api.show_login_tools(chat["id"])["ok"] is False

    shown = []
    api._show_login_tools = lambda account_id, profile, *, launching=False: shown.append((account_id, profile, launching))
    chat_dir = tmp_path / "browsers" / f"acc_{chat['id']}"
    google_dir = tmp_path / "browsers" / f"acc_{google['id']}"
    launch = {"ok": True, "profile_dir": str(chat_dir)}
    monkeypatch.setattr("src.browser_service.launch_isolated_browser", lambda *args, **kwargs: launch)
    assert api.launch_browser(chat["id"])["ok"] is True
    assert api.launch_browser(chat["id"], target_url="https://example.test")["ok"] is True
    launch["ok"] = False
    assert api.launch_browser(chat["id"])["ok"] is False
    assert api.show_login_tools(google["id"])["ok"] is True
    assert api.show_login_tools(-1)["ok"] is False
    assert shown == [
        (chat["id"], chat_dir, True),
        (chat["id"], chat_dir, True),
        (google["id"], google_dir, False),
    ]


def test_tools_track_browser_handles_and_dispose_on_close(monkeypatch, tmp_path):
    account_a, account_b = tmp_path / "acc_1", tmp_path / "acc_2"
    opened = {account_a: [10, 11], account_b: [20]}
    active = 10
    positioned = {}
    copy = {}
    clipboard = []
    minimized = set()
    monkeypatch.setitem(sys.modules, "System.Windows.Forms", SimpleNamespace(
        Clipboard=SimpleNamespace(SetDataObject=lambda value, *_args: clipboard.append(value)),
    ))

    class FakeWindows:
        def windows_for_profile(self, profile):
            return opened[profile]

        def foreground(self):
            return active

        def prepare_toolbar(self, handle):
            pass

        def placement(self, browser, width, height):
            return None if browser in minimized else (browser, 0, width, height)

        def place_toolbar(self, handle, placement):
            positioned[handle] = placement

        def hide_toolbar(self, handle):
            positioned.pop(handle, None)

    class FakeStrip:
        def __init__(self, label, callback, close):
            self.size = (600, 40)
            self.handle = len(copy) + 100
            self.disposed = False
            self.form = SimpleNamespace(Dispose=self.dispose)
            copy[self.handle] = callback

        def dispose(self):
            self.disposed = True

        def set_account(self, label, has_password):
            self.label = label

        def set_code(self, code, remaining):
            pass

        def set_message(self, message):
            pass

    def account(account_id):
        return {"account": f"account-{account_id}", "password": "synthetic", "secret_key": ""}

    monkeypatch.setattr(panel_module, "LoginToolsStrip", FakeStrip)
    panel = LoginToolsPanel(None, account, lambda: 0)
    panel._windows = FakeWindows()
    panel._sessions = {
        1: panel_module._BrowserSession(account_a, 0),
        2: panel_module._BrowserSession(account_b, 0),
    }
    panel._tick()
    first = panel._sessions[1].strips[10]
    assert first.label == "account-1"
    assert set(positioned) == {first.handle}
    copy[first.handle]("account")
    assert clipboard == ["account-1"]

    active = 20
    panel._tick()
    second = panel._sessions[2].strips[20]
    assert second.label == "account-2"
    assert set(positioned) == {second.handle}
    copy[first.handle]("account")
    copy[second.handle]("account")
    assert clipboard == ["account-1", "account-2"]
    minimized.add(20)
    panel._tick()
    assert positioned == {} and not second.disposed

    active = 11
    opened[account_a] = [11]
    panel._tick()
    assert first.disposed
    third = panel._sessions[1].strips[11]
    assert third.label == "account-1" and set(positioned) == {third.handle}

    active = 99
    panel._tick()
    assert positioned == {}
    opened[account_b] = []
    panel._tick()
    assert second.disposed and 2 not in panel._sessions
    panel.close()
    assert third.disposed and not panel._sessions


def test_browser_window_rect_and_winforms_hud_strip_smoke():
    import sys
    from ctypes import wintypes
    from src.browser_window import _toolbar_rect

    work = wintypes.RECT(0, 0, 1920, 1040)

    def assert_inside(browser, rect):
        x, y, w, h = rect
        assert browser.left <= x and x + w <= browser.right, (browser, rect)
        assert browser.top <= y and y + h <= browser.bottom, (browser, rect)
        assert work.left <= x and x + w <= work.right, (browser, rect)
        assert work.top <= y and y + h <= work.bottom, (browser, rect)

    # Normal window: HUD docks inside the browser's top content edge, below the
    # Chrome tab strip + address bar + bookmark bar, horizontally centred.
    b_normal = wintypes.RECT(100, 120, 1100, 900)
    rect_inside = _toolbar_rect(b_normal, work, 800, 40, 96)
    assert rect_inside == (200, 236, 800, 40)
    assert rect_inside[1] == b_normal.top + 116
    assert_inside(b_normal, rect_inside)
    # The strip is horizontally centred within the browser window.
    assert abs((rect_inside[0] - b_normal.left) - (b_normal.right - rect_inside[0] - rect_inside[2])) <= 1

    # Maximized window: identical interior-top docking (never over the caption).
    b_max = wintypes.RECT(0, 0, 1920, 1040)
    rect_max = _toolbar_rect(b_max, work, 800, 40, 96)
    assert rect_max == (560, 116, 800, 40)
    assert rect_max[1] == b_max.top + 116
    assert_inside(b_max, rect_max)

    # DPI scaling keeps the same 116px logical header inset.
    rect_hidpi = _toolbar_rect(b_normal, work, 800, 40, 144)
    assert rect_hidpi is not None
    assert rect_hidpi[1] == b_normal.top + round(116 * 144 / 96)
    assert_inside(b_normal, rect_hidpi)

    # Unusually short browser: fall back to the browser's top edge, still inside.
    b_short = wintypes.RECT(100, 300, 1100, 430)
    rect_short = _toolbar_rect(b_short, work, 800, 40, 96)
    assert rect_short == (200, 306, 800, 40)
    assert_inside(b_short, rect_short)

    # Too short to fit the HUD at all: decline instead of drawing off-window.
    assert _toolbar_rect(wintypes.RECT(100, 1000, 1100, 1035), work, 800, 40, 96) is None

    # A narrow browser clamps the HUD width and stays inside the work area.
    b_narrow = wintypes.RECT(100, 120, 360, 900)
    rect_narrow = _toolbar_rect(b_narrow, work, 800, 40, 96)
    assert rect_narrow is not None
    assert rect_narrow[2] <= 260 and rect_narrow[2] >= 220
    assert_inside(b_narrow, rect_narrow)

    if sys.platform == "win32":
        import clr
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        from System.Windows.Forms import Button, CheckBox, ContextMenuStrip, Label, ToolStripSeparator
        from src.login_tools_strip import HudQuickCredentialForm, LoginToolsStrip

        def assert_gdi_safe(text: str, context: str):
            assert "️" not in text, (context, text)
            assert not any(ord(ch) > 0xFFFF for ch in text), (context, text)

        def walk_control_texts(control, seen=None):
            if seen is None:
                seen = set()
            if id(control) in seen:
                return []
            seen.add(id(control))
            texts = []
            if isinstance(control, (Button, Label, CheckBox)):
                texts.append((type(control).__name__, control.Text or ""))
            for child in control.Controls:
                texts.extend(walk_control_texts(child, seen))
            return texts

        account = {
            "id": 1,
            "account_type": "Google",
            "account": "hud_smoke@gmail.com",
            "password": "pw",
            "secret_key": "JBSWY3DPEHPK3PXP",
            "recovery_email": "rec@example.com",
            "fp_config": {"timezone": "America/Los_Angeles", "locale": "en-US"},
        }
        opened_urls = []
        strip = LoginToolsStrip(
            "hud_smoke@gmail.com",
            lambda _f: None,
            lambda: None,
            account=account,
            actions={"open_url": opened_urls.append},
        )
        try:
            assert strip.handle > 0
            w_exp, h_exp = strip.size
            assert w_exp >= 860 and h_exp >= 40
            assert w_exp <= 1060

            # Expanded HUD text (badge/buttons/status) is 100% GDI-safe.
            for kind, text in walk_control_texts(strip.form):
                assert_gdi_safe(text, kind)

            strip.set_code("123456", 20)
            assert strip._btn_2fa.Text == "2FA 123 456 (20s)"
            assert_gdi_safe(strip._btn_2fa.Text, "2FA pill")

            # Emoji variation selectors from the geo warning are cleaned away.
            strip.set_geo_status("⚠️ 时区差(代-8/本+8)", tooltip_text="⚠️ tz warn", is_warning=True)
            assert strip._status_lbl.Text == "⚠ 时区差(代-8/本+8)"
            assert_gdi_safe(strip._status_lbl.Text, "geo status")
            strip.set_message("🛡️ 已复制")
            assert_gdi_safe(strip._status_lbl.Text, "message")

            # Every quick-link menu caption stays tofu-free, and both payment /
            # subscription links survive with their exact URLs.
            captured = {}

            class _CaptureMenu(ContextMenuStrip):
                def Show(self, *_args, **_kwargs):
                    captured["menu"] = self

            strip._show_links_menu(strip._btn_gov, _CaptureMenu, ToolStripSeparator)
            menu = captured["menu"]
            try:
                menu_texts = [item.Text for item in menu.Items if item.Text]
                assert menu_texts, "quick-links menu must contain captions"
                for text in menu_texts:
                    assert_gdi_safe(text, "menu item")
                assert any("关闭支付资料" in t for t in menu_texts)
                assert any("取消Pro订阅" in t for t in menu_texts)

                for item in menu.Items:
                    if "关闭支付资料" in (item.Text or ""):
                        item.PerformClick()
                    if "取消Pro订阅" in (item.Text or ""):
                        item.PerformClick()
                assert "https://payments.google.com/gp/w/home/reauthprompt?rtrp=/home/settings" in opened_urls
                assert "https://myaccount.google.com/deleteservices" in opened_urls
            finally:
                menu.Dispose()

            strip.set_account("chat@example.test", True, {**account, "account_type": "ChatGPT"})
            strip._show_links_menu(strip._btn_gov, _CaptureMenu, ToolStripSeparator)
            menu = captured["menu"]
            try:
                menu_texts = [item.Text for item in menu.Items if item.Text]
                assert any("ChatGPT 对话主页" in text for text in menu_texts)
                assert not any("Google" in text or "关闭支付资料" in text for text in menu_texts)
            finally:
                menu.Dispose()
                strip.set_account(account["account"], True, account)

            strip.set_collapsed(True)
            w_min, h_min = strip.size
            assert w_min < w_exp and h_min < h_exp
            assert w_min <= 260 and h_min <= 40
            for kind, text in walk_control_texts(strip.form):
                assert_gdi_safe(text, kind)
            strip.set_collapsed(False)
            assert strip.size == (w_exp, h_exp)
        finally:
            strip.dispose()

        saved_calls = []
        qc = HudQuickCredentialForm(
            account,
            (500, 120),
            lambda: 0,
            lambda _v: None,
            lambda acc_id, payload: saved_calls.append((acc_id, payload)) or {"ok": True, "account": {**account, **payload}},
            get_top_recovery_fn=lambda: [{"email": "top@example.com", "count": 3}],
        )
        try:
            assert qc.handle > 0
            for kind, text in walk_control_texts(qc.form):
                assert_gdi_safe(text, f"quick-cred {kind}")
            assert "验证码:" in qc._lbl_live_totp.Text
            assert_gdi_safe(qc._lbl_live_totp.Text, "live totp")
            # Google account without confirmation checkbox blocks saving credential changes
            qc._txt_pwd.Text = "NewStrong#2026"
            qc._save_all()
            assert saved_calls == []
            assert "确认" in qc._lbl_feedback.Text
            qc._chk_google_confirmed.Checked = True
            qc._save_all()
            assert len(saved_calls) == 1
            assert saved_calls[0][1]["password"] == "NewStrong#2026"
            assert saved_calls[0][1]["google_confirmed"] is True
        finally:
            qc.form.Dispose()


def test_smart_fill_focuses_browser_on_caller_thread_and_pastes_via_sendinput(monkeypatch, tmp_path):
    import threading
    import time
    from types import SimpleNamespace

    events = []
    caller_thread_id = threading.get_ident()
    clipboard = []
    monkeypatch.setitem(sys.modules, "System.Windows.Forms", SimpleNamespace(
        Clipboard=SimpleNamespace(SetDataObject=lambda val, *_args: clipboard.append(val)),
    ))
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda fn: fn))

    class StubWindows:
        def __init__(self):
            self.fg = 999  # non-chromium foreground (e.g. QuickCredential popup)

        def foreground(self):
            return self.fg

        def is_chromium_window(self, hwnd):
            return hwnd == 77

        def focus_window(self, hwnd):
            events.append(("focus", hwnd, threading.get_ident() == caller_thread_id))
            self.fg = hwnd
            return True

        def send_ctrl_v_paste(self):
            events.append(("paste", self.fg))

    messages = []
    stub_strip = SimpleNamespace(
        form=SimpleNamespace(IsDisposed=False, InvokeRequired=False),
        set_message=lambda msg, **_kw: messages.append(msg),
        set_account=lambda *_a, **_kw: None,
        secret="",
        last_account_check=0.0,
    )
    panel = LoginToolsPanel(
        None,
        lambda aid: {"id": aid, "account": "fill@gmail.com", "password": "pw", "secret_key": ""},
        lambda: 0,
    )
    panel._windows = StubWindows()
    sess = panel_module._BrowserSession(tmp_path / "acc_1", 0)
    sess.strips[77] = stub_strip
    panel._smart_fill_field_async(1, sess, 77, "account")
    deadline = time.monotonic() + 2.0
    while ("paste", 77) not in events and time.monotonic() < deadline:
        time.sleep(0.02)

    assert clipboard == ["fill@gmail.com"]
    assert ("focus", 77, True) in events
    assert ("paste", 77) in events
    assert any("已填入邮箱" in m for m in messages)


def test_async_webview_notifier_runs_off_caller_thread_and_coalesces():
    import threading
    import time

    from main import make_async_webview_notifier

    caller_thread = threading.get_ident()
    caller_released = threading.Event()
    calls = []

    class BlockingWindow:
        def evaluate_js(self, script):
            # Simulate EdgeChrome.evaluate_js waiting on the UI-thread
            # SynchronizationContext: it cannot finish until the caller
            # (UI) thread is free again.
            calls.append((script, threading.get_ident()))
            assert caller_released.wait(2.0), "caller thread never returned"
            return True

    notifier = make_async_webview_notifier(BlockingWindow())

    started = time.monotonic()
    notifier({"id": 1})
    notifier({"id": 1})
    notifier({"id": 1})
    elapsed = time.monotonic() - started
    # The notifier must never block the calling (WinForms UI) thread.
    assert elapsed < 0.5

    caller_released.set()
    deadline = time.monotonic() + 3.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.02)
    assert calls, "evaluate_js was never called"
    assert all(tid != caller_thread for _script, tid in calls)
    assert calls[0][0] == "window.__kvOnAccountUpdated && window.__kvOnAccountUpdated()"

    # Repeated notifications coalesce instead of spawning one worker each.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        workers = [t for t in threading.enumerate() if t.name == "kv-webview-notify"]
        if not workers:
            break
        time.sleep(0.02)
    assert not [t for t in threading.enumerate() if t.name == "kv-webview-notify"]
    # First call + exactly one coalesced re-run, not one worker per notify().
    assert len(calls) == 2

    closed_calls = []

    class CountingWindow:
        def evaluate_js(self, script):
            closed_calls.append(script)

    closing_notifier = make_async_webview_notifier(CountingWindow(), is_closing=lambda: True)
    closing_notifier({"id": 2})
    time.sleep(0.15)
    assert closed_calls == []


def test_notify_account_updated_marshals_off_thread_and_updates_secret_immediately(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda fn: fn))

    begin_invoke_calls = []

    class CrossThreadForm:
        IsDisposed = False
        InvokeRequired = True

        def BeginInvoke(self, action):
            # Real WinForms queues the delegate to the UI thread. Defer it so
            # the test can prove the strip is NOT touched on the caller thread.
            begin_invoke_calls.append(action)

    class DisposedForm:
        IsDisposed = True
        InvokeRequired = False

        def BeginInvoke(self, action):  # pragma: no cover - must never run
            raise AssertionError("disposed strip must be skipped")

    class StubStrip:
        def __init__(self, form):
            self.form = form
            self.secret = ""
            self.last_account_check = 0.0
            self.codes = []
            self.labels = []

        def set_account(self, label, has_password, account=None):
            self.labels.append((label, has_password))

        def set_code(self, code, remaining):
            self.codes.append((code, remaining))

    fresh_form = CrossThreadForm()
    fresh_strip = StubStrip(fresh_form)
    disposed_strip = StubStrip(DisposedForm())

    panel = LoginToolsPanel(None, lambda _aid: None, lambda: 0)
    session = panel_module._BrowserSession(Path("C:/tmp/acc_1"), 0)
    session.strips[10] = fresh_strip
    session.strips[11] = disposed_strip
    panel._sessions = {1: session}

    updated = {
        "id": 1,
        "account": "hud@example.test",
        "password": "pw",
        "secret_key": "JBSWY3DPEHPK3PXP",
    }
    panel.notify_account_updated(updated)

    # Marshalled via BeginInvoke (never a blocking Invoke / direct mutation).
    assert len(begin_invoke_calls) == 1
    assert fresh_strip.secret == "" and fresh_strip.labels == [] and fresh_strip.codes == []
    assert disposed_strip.labels == [] and disposed_strip.codes == []

    # Running the queued delegate applies the update on the UI thread.
    begin_invoke_calls[0]()
    assert fresh_strip.secret == "JBSWY3DPEHPK3PXP"
    assert fresh_strip.last_account_check > 0
    assert fresh_strip.labels == [("hud@example.test", True)]
    assert len(fresh_strip.codes) == 1 and len(fresh_strip.codes[0][0]) == 6
    assert disposed_strip.labels == [] and disposed_strip.codes == []


def test_hud_quick_update_2fa_notifies_once_and_does_not_deadlock(tmp_path):
    import threading
    import time

    from main import make_async_webview_notifier

    api = AccountApi(tmp_path)
    created = api.save_account({
        "account_type": "Google",
        "account": "hud_2fa@example.test",
        "password": "OldPass#2026",
        "secret_key": "JBSWY3DPEHPK3PXP",
    })
    assert created["ok"]
    account = created["account"]

    evaluated = []

    class FakeWindow:
        def evaluate_js(self, script):
            evaluated.append(script)
            return True

    class StubStrip:
        def __init__(self):
            self.form = SimpleNamespace(IsDisposed=False, InvokeRequired=False)
            self.secret = ""
            self.last_account_check = 0.0
            self.codes = []
            self.labels = []

        def set_account(self, label, has_password, acc=None):
            self.labels.append(label)

        def set_code(self, code, remaining):
            self.codes.append(code)

    panel = LoginToolsPanel(
        FakeWindow(),
        api.db.get_account,
        lambda: 0,
        api=api,
        on_account_updated=make_async_webview_notifier(FakeWindow()),
    )
    api._login_tools_panel = panel

    session = panel_module._BrowserSession(tmp_path / "acc_1", 0)
    strip = StubStrip()
    session.strips[10] = strip
    panel._sessions[account["id"]] = session

    quick_update = panel._build_strip_actions(account["id"], session, 10)["quick_update"]

    started = time.monotonic()
    res = quick_update(account["id"], {
        "secret_key": "GEZDGNBVGY3TQOJQ",
        "twofa_modified": 1,
        "google_confirmed": True,
        "reason": "hud_bind_2fa",
    })
    elapsed = time.monotonic() - started
    assert res["ok"] is True
    assert elapsed < 1.0

    stored = api.db.get_account(account["id"])
    assert stored["secret_key"] == "GEZDGNBVGY3TQOJQ"
    history = api.get_credential_history(account["id"])["history"]
    assert len(history) == 1
    assert history[0]["old_secret_key"] == "JBSWY3DPEHPK3PXP"
    assert history[0]["reason"] == "hud_bind_2fa"

    deadline = time.monotonic() + 3.0
    while not evaluated and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(evaluated) == 1
    assert strip.secret == "GEZDGNBVGY3TQOJQ"
    assert strip.codes and len(strip.codes[0]) == 6

    # Let any coalesced follow-up worker settle before asserting the count.
    time.sleep(0.2)
    assert len(evaluated) == 1

    if sys.platform == "win32":
        _assert_hud_quick_form_2fa_flow(api, stored)


def _assert_hud_quick_form_2fa_flow(api: AccountApi, account: dict) -> None:
    import clr

    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    from src.login_tools_strip import HudQuickCredentialForm

    saved_calls = []

    def save_credentials(acc_id: int, payload: dict) -> dict:
        res = api.quick_update_credentials(acc_id, payload)
        saved_calls.append((acc_id, payload))
        return res

    form = HudQuickCredentialForm(
        account,
        (500, 120),
        lambda: 0,
        lambda _value: None,
        save_credentials,
        on_saved_fn=lambda _updated, _msg: None,
    )
    try:
        # Invalid Base32 must be rejected locally without hitting the API.
        form._chk_google_confirmed.Checked = True
        form._txt_2fa.Text = "123456"
        form._save_all()
        assert saved_calls == []
        assert "2FA 密钥格式无效" in form._lbl_feedback.Text

        # Valid Base32 saves, normalizes the echo and refreshes the preview.
        form._txt_2fa.Text = "jbsw y3dp ehpk 3pxp"
        form._save_all()
        assert len(saved_calls) == 1
        assert saved_calls[0][1]["secret_key"] == "JBSWY3DPEHPK3PXP"
        assert saved_calls[0][1]["twofa_modified"] == 1
        assert saved_calls[0][1]["google_confirmed"] is True
        assert form._txt_2fa.Text == "JBSWY3DPEHPK3PXP"
        assert "验证码:" in form._lbl_live_totp.Text
        assert form._btn_save.Enabled is True

        # Re-entrancy guard: a second save while one is in flight is ignored.
        calls_before = len(saved_calls)
        form._saving = True
        form._save_all()
        form._saving = False
        assert len(saved_calls) == calls_before
    finally:
        form.form.Dispose()


def test_crash_logger_writes_and_rotates(tmp_path, monkeypatch):
    from src import crash_logger

    monkeypatch.setattr(crash_logger, "_log_path", None)
    log_path = tmp_path / "crash.log"
    crash_logger.configure_crash_log(log_path)
    try:
        raise RuntimeError("synthetic crash for logger test")
    except RuntimeError:
        crash_logger.log_exception("tests.synthetic", extra="extra-context")

    content = log_path.read_text(encoding="utf-8")
    assert "tests.synthetic" in content
    assert "RuntimeError: synthetic crash for logger test" in content
    assert "extra-context" in content
    assert "Traceback (most recent call last)" in content

    # Oversized files rotate to crash.log.1 instead of growing forever.
    log_path.write_text("x" * (crash_logger.DEFAULT_MAX_BYTES + 16), encoding="utf-8")
    try:
        raise ValueError("rotate me")
    except ValueError:
        crash_logger.log_exception("tests.rotate")

    rotated = tmp_path / "crash.log.1"
    assert rotated.exists()
    assert rotated.stat().st_size > crash_logger.DEFAULT_MAX_BYTES
    fresh = log_path.read_text(encoding="utf-8")
    assert "tests.rotate" in fresh and "rotate me" in fresh
    assert fresh.count("tests.rotate") == 1

    # Logging to an unwritable location must never raise.
    crash_logger.log_exception("tests.missing", log_path=tmp_path / "no_such_dir" / "crash.log")


def test_quick_cred_rejects_garbage_2fa_fullscreen_rect_and_active_lock_keeps_session(tmp_path: Path, monkeypatch):
    from ctypes import wintypes
    from src.browser_window import _toolbar_rect

    # P1-2: Fullscreen browser docks near top edge (6px) rather than 116px
    mon = wintypes.RECT(0, 0, 1920, 1080)
    fs_rect = _toolbar_rect(mon, mon, 900, 44, 96, fullscreen=True)
    assert fs_rect == (510, 6, 900, 44)

    # P1-3: Active profile lock keeps session alive across a transient 0-handle frame
    prof = tmp_path / "acc_locked"
    prof.mkdir()
    (prof / "SingletonLock").write_text("1", encoding="utf-8")

    class _ZeroWin:
        def windows_for_profile(self, _p):
            return []

        def foreground(self):
            return 0

    panel = LoginToolsPanel(None, lambda _aid: None, lambda: 0)
    panel._windows = _ZeroWin()
    panel._sessions[7] = panel_module._BrowserSession(prof, 0)
    panel._tick()
    assert 7 in panel._sessions
    (prof / "SingletonLock").unlink()
    panel._tick()
    assert 7 not in panel._sessions

    if sys.platform == "win32":
        from src.login_tools_strip import HudQuickCredentialForm, LoginToolsStrip

        saved_calls = []
        acc = {
            "id": 9,
            "account_type": "ChatGPT",
            "account": "very.long.email.address.for.overlap.regression.check@enterprise.example.com",
            "password": "pw",
            "secret_key": "JBSWY3DPEHPK3PXP",
        }
        qc = HudQuickCredentialForm(
            acc,
            (400, 120),
            lambda: 0,
            lambda _v: None,
            lambda aid, payload: saved_calls.append((aid, payload)) or {"ok": True, "account": {**acc, **payload}},
        )
        try:
            # P0-3: Non-empty garbage 2FA must never normalize to "" and overwrite the stored secret
            qc._txt_2fa.Text = "###!!!"
            qc._save_all()
            assert saved_calls == []
            assert "格式无效" in qc._lbl_feedback.Text

            # Unsaved edit is preserved across background updates; unmodified box syncs live
            qc.update_account({**acc, "secret_key": "NB2W45DFOIZA===="})
            assert qc._txt_2fa.Text == "###!!!"
            qc._txt_2fa.Text = "NB2W45DFOIZA===="
            qc.update_account({**acc, "secret_key": "MFRGGZDFMZTWQ2LK"})
            assert qc._txt_2fa.Text == "MFRGGZDFMZTWQ2LK"
        finally:
            qc.form.Dispose()

        strip = LoginToolsStrip("long.google.account@gmail.com", lambda _f: None, lambda: None, account=acc)
        try:
            strip.set_code("123456", 19)
            gov_right = int(strip._btn_gov.Left) + int(strip._btn_gov.Width)
            # right_panel sits right of main_flow with >= 8px clean visual separator
            assert int(strip._right_panel.Left) <= gov_right or int(strip._right_panel.Padding.Left) >= 8
            status_screen_left = int(strip._right_panel.Left) + int(strip._status_lbl.Left)
            assert status_screen_left >= gov_right + 6
        finally:
            strip.dispose()


def test_hud_strip_applies_real_ws_ex_topmost_and_keeps_owned_browser_dialogs(tmp_path: Path, monkeypatch):
    prof = tmp_path / "acc_topmost"
    prof.mkdir()
    events = []
    active_fg = 101

    class _FakeWin:
        def windows_for_profile(self, _p):
            return [100]

        def foreground(self):
            return active_fg

        def is_owned_by(self, owner_hwnd, target_hwnd):
            return owner_hwnd == 100 and target_hwnd == 101

        def prepare_toolbar(self, handle):
            events.append(("prepare", handle))

        def placement(self, browser, width, height):
            return (200, 180, width, height)

        def place_toolbar(self, handle, rect):
            events.append(("place", handle, rect))

        def move_toolbar(self, handle, x, y):
            events.append(("move", handle, x, y))

        def hide_toolbar(self, handle):
            events.append(("hide", handle))

    class _StripStub:
        def __init__(self, label, cb, close, **_kwargs):
            self.size = (880, 44)
            self.handle = 555
            self.form = SimpleNamespace(Dispose=lambda: None)
            self.user_dragged = False
            self.dragged_pos = None

        def ensure_visible_topmost(self, bounds=None):
            events.append(("ensure", self.handle, bounds))

        def set_account(self, *_args):
            pass

        def set_code(self, *_args):
            pass

        def dispose(self):
            pass

    monkeypatch.setattr(panel_module, "LoginToolsStrip", _StripStub)
    panel = LoginToolsPanel(None, lambda aid: {"id": aid, "account": "g@gmail.com", "password": "pw", "secret_key": ""}, lambda: 0)
    panel._windows = _FakeWin()
    panel._sessions[1] = panel_module._BrowserSession(prof, 0)
    panel._tick()

    assert events == [
        ("prepare", 555),
        ("ensure", 555, (200, 180, 880, 44)),
        ("place", 555, (200, 180, 880, 44)),
    ]

    events.clear()
    strip = panel._sessions[1].strips[100]
    strip.user_dragged = True
    strip.dragged_pos = (310, 220)
    panel._tick()
    assert events == [
        ("ensure", 555, None),
        ("move", 555, 310, 220),
    ]

    if sys.platform == "win32":
        from System.Windows.Forms import Application
        from src.browser_window import BrowserWindows, is_window_topmost
        from src.login_tools_strip import LoginToolsStrip

        real_strip = LoginToolsStrip("topmost_real@gmail.com", lambda _f: None, lambda: None)
        bw = BrowserWindows()
        rect = (160, 140, real_strip.size[0], real_strip.size[1])
        try:
            bw.prepare_toolbar(real_strip.handle)
            real_strip.ensure_visible_topmost(rect)
            bw.place_toolbar(real_strip.handle, rect)
            Application.DoEvents()
            assert real_strip.form.Visible is True
            assert real_strip.form.TopMost is True
            assert is_window_topmost(real_strip.handle) is True

            bw.hide_toolbar(real_strip.handle)
            Application.DoEvents()
            real_strip.ensure_visible_topmost(rect)
            bw.place_toolbar(real_strip.handle, rect)
            Application.DoEvents()
            assert is_window_topmost(real_strip.handle) is True
        finally:
            real_strip.dispose()
