import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from src.browser_service import _profile_has_active_lock
from src.browser_window import BrowserWindows
from src.crash_logger import log_exception
from src.login_tools_strip import LoginToolsStrip
from src.totp_service import generate_totp, normalize_secret
from src.window_state import _winforms_window


@dataclass
class _BrowserSession:
    profile_dir: Path
    startup_deadline: float
    runtime_proxy: str = ""
    strips: dict[int, LoginToolsStrip] = field(default_factory=dict)
    dismissed: set[int] = field(default_factory=set)
    geo_checked: bool = False


class LoginToolsPanel:
    def __init__(
        self,
        window,
        get_account: Callable[[int], dict | None],
        get_totp_offset: Callable[[], int],
        *,
        api=None,
        on_account_updated: Callable[[dict], None] | None = None,
    ):
        self._window = window
        self._get_account = get_account
        self._get_totp_offset = get_totp_offset
        self._api = api
        self._on_account_updated = on_account_updated
        self._windows = None
        self._timer = None
        self._sessions: dict[int, _BrowserSession] = {}
        self._sessions_lock = threading.RLock()
        self._last_active_account_id: int | None = None
        self._typing_lock = threading.Lock()

    def track(
        self,
        account_id: int,
        profile_dir: Path,
        *,
        launching: bool = False,
        runtime_proxy: str = "",
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError("浏览器登录工具仅支持 Windows。")
        owner = _winforms_window(self._window)
        if owner is None or owner.IsDisposed or not owner.IsHandleCreated:
            raise RuntimeError("无法打开浏览器登录工具。")
        from System import Action

        profile_dir = profile_dir.resolve()

        def register():
            with self._sessions_lock:
                if self._windows is None:
                    self._windows = BrowserWindows()
                windows = self._windows.windows_for_profile(profile_dir)
                if not launching and not windows:
                    raise RuntimeError("请先从 KeyVault 启动这个账号的浏览器。")
                session = self._sessions.setdefault(
                    account_id, _BrowserSession(profile_dir, 0, runtime_proxy=runtime_proxy)
                )
                if runtime_proxy:
                    session.runtime_proxy = runtime_proxy
                session.dismissed.clear()
                session.startup_deadline = time.monotonic() + 12 if launching else 0
                self._last_active_account_id = int(account_id)
            self._start_timer()
            self._tick()

        if owner.InvokeRequired:
            owner.Invoke(Action(register))
        else:
            register()

    def get_active_or_last_account_id(self) -> int | None:
        """Return the account ID whose isolated browser is focused or most recently active."""
        with self._sessions_lock:
            snapshot: list[tuple[int, Path, list[tuple[int, int]]]] = []
            for account_id, session in self._sessions.items():
                strip_handles: list[tuple[int, int]] = []
                for strip in list(session.strips.values()):
                    try:
                        strip_handles.append(
                            (int(getattr(strip, "handle", 0) or 0), int(getattr(strip, "quick_cred_handle", 0) or 0))
                        )
                    except Exception:
                        pass
                snapshot.append((int(account_id), session.profile_dir, strip_handles))
            last_id = self._last_active_account_id
            windows = self._windows

        if windows is not None and snapshot:
            fg = windows.foreground()
            for account_id, profile_dir, strip_handles in snapshot:
                handles = set(windows.windows_for_profile(profile_dir))
                if fg in handles:
                    return int(account_id)
                for s_hwnd, q_hwnd in strip_handles:
                    if (s_hwnd and fg == s_hwnd) or (q_hwnd and fg == q_hwnd):
                        return int(account_id)
        if last_id is not None and any(aid == last_id for aid, _, _ in snapshot):
            return int(last_id)
        if snapshot:
            return int(snapshot[-1][0])
        return None

    def notify_account_updated(self, updated: dict) -> None:
        if not updated:
            return
        acc_id = int(updated.get("id") or 0)
        with self._sessions_lock:
            session = self._sessions.get(acc_id)
            strips = list(session.strips.values()) if session else []
        if strips:
            for strip in strips:
                try:
                    form = getattr(strip, "form", None)
                    if form is not None and getattr(form, "IsDisposed", False):
                        continue

                    def _apply(s=strip):
                        f = getattr(s, "form", None)
                        if f is not None and getattr(f, "IsDisposed", False):
                            return
                        self._refresh_account(s, updated)
                        if hasattr(s, "set_code"):
                            code = self._current_totp(getattr(s, "secret", ""))
                            s.set_code(code["code"], code["remaining"])

                    if form is not None and getattr(form, "InvokeRequired", False) and hasattr(form, "BeginInvoke"):
                        from System import Action

                        form.BeginInvoke(Action(_apply))
                    else:
                        _apply()
                except Exception:
                    log_exception("LoginToolsPanel.notify_account_updated.strip")
        if callable(self._on_account_updated):
            try:
                self._on_account_updated(updated)
            except Exception:
                log_exception("LoginToolsPanel.notify_account_updated.callback")

    def _start_timer(self):
        if self._timer is not None:
            return
        from System.Windows.Forms import Timer

        self._timer = Timer()
        self._timer.Interval = 150

        def _on_tick(_sender, _event):
            try:
                self._tick()
            except Exception:
                log_exception("LoginToolsPanel._tick")

        self._timer.Tick += _on_tick
        self._timer.Start()

    def _tick(self) -> None:
        foreground = self._windows.foreground()
        is_child_or_self = getattr(self._windows, "is_child_or_self", None)
        with self._sessions_lock:
            items = list(self._sessions.items())
        for account_id, session in items:
            handles = set(self._windows.windows_for_profile(session.profile_dir))
            session.dismissed.intersection_update(handles)
            with self._sessions_lock:
                for browser in session.strips.keys() - handles:
                    self._remove_strip(session, browser)
                if not handles:
                    if time.monotonic() >= session.startup_deadline and not _profile_has_active_lock(
                        session.profile_dir
                    ):
                        self._sessions.pop(account_id, None)
                    continue
                session.startup_deadline = 0
                strip_items = list(session.strips.items())

            active_browser = foreground if foreground in handles else 0
            if not active_browser and foreground:
                is_owned_by = getattr(self._windows, "is_owned_by", None)
                if callable(is_owned_by):
                    for b_hwnd in handles:
                        if is_owned_by(b_hwnd, foreground):
                            active_browser = b_hwnd
                            break
            if not active_browser:
                for b_hwnd, strip in strip_items:
                    if getattr(strip, "is_menu_open", False):
                        active_browser = b_hwnd
                        break
            if not active_browser and callable(is_child_or_self):
                for b_hwnd, strip in strip_items:
                    q_hwnd = getattr(strip, "quick_cred_handle", 0)
                    s_hwnd = getattr(strip, "handle", 0)
                    if (s_hwnd and is_child_or_self(s_hwnd, foreground)) or (
                        q_hwnd and is_child_or_self(q_hwnd, foreground)
                    ):
                        active_browser = b_hwnd
                        break

            for browser, strip in strip_items:
                if browser != active_browser:
                    self._windows.hide_toolbar(strip.handle)
                    if hasattr(strip, "hide_popups"):
                        strip.hide_popups()

            if active_browser and active_browser not in session.dismissed:
                with self._sessions_lock:
                    self._last_active_account_id = int(account_id)
                self._update_visible_strip(account_id, session, active_browser)
        with self._sessions_lock:
            has_sessions = bool(self._sessions)
        if not has_sessions:
            self._stop_timer()

    def _update_visible_strip(self, account_id: int, session: _BrowserSession, browser: int) -> None:
        strip = session.strips.get(browser)
        if strip is None:
            account = self._get_account(account_id)
            if account is None:
                session.dismissed.add(browser)
                return
            actions = self._build_strip_actions(account_id, session, browser)
            try:
                strip = LoginToolsStrip(
                    account["account"],
                    lambda field: self._copy(account_id, session, browser, field),
                    lambda: self._dismiss(session, browser),
                    account=account,
                    actions=actions,
                )
            except TypeError:
                # Test stubs like FakeStrip take only the 3 positional args.
                strip = LoginToolsStrip(
                    account["account"],
                    lambda field: self._copy(account_id, session, browser, field),
                    lambda: self._dismiss(session, browser),
                )
            try:
                self._windows.prepare_toolbar(strip.handle)
            except OSError:
                strip.form.Dispose()
                session.dismissed.add(browser)
                return
            self._refresh_account(strip, account)
            with self._sessions_lock:
                session.strips[browser] = strip
            if not session.geo_checked:
                session.geo_checked = True
                self._start_geo_check_async(account_id, session, strip)
        elif time.monotonic() - getattr(strip, "last_account_check", 0.0) >= 1:
            account = self._get_account(account_id)
            if account is None:
                self._dismiss(session, browser)
                return
            self._refresh_account(strip, account)

        if hasattr(strip, "restore_popups"):
            strip.restore_popups()

        code = self._current_totp(getattr(strip, "secret", ""))
        strip.set_code(code["code"], code["remaining"])

        if getattr(strip, "user_dragged", False) and getattr(strip, "dragged_pos", None):
            move_fn = getattr(self._windows, "move_toolbar", None)
            if callable(move_fn):
                try:
                    ensure = getattr(strip, "ensure_visible_topmost", None)
                    if callable(ensure):
                        ensure()
                    move_fn(strip.handle, *strip.dragged_pos)
                    return
                except OSError:
                    self._remove_strip(session, browser)
                    return

        placement = self._windows.placement(browser, *strip.size)
        if placement is None:
            self._windows.hide_toolbar(strip.handle)
            if hasattr(strip, "hide_popups"):
                strip.hide_popups()
            return
        try:
            ensure = getattr(strip, "ensure_visible_topmost", None)
            if callable(ensure):
                ensure(placement)
            self._windows.place_toolbar(strip.handle, placement)
        except OSError:
            self._remove_strip(session, browser)

    def _build_strip_actions(self, account_id: int, session: _BrowserSession, browser: int) -> dict:
        def get_offset():
            return self._get_totp_offset()

        def get_top_recovery():
            if self._api is not None:
                try:
                    return self._api.get_top_recovery_emails(6).get("emails") or []
                except Exception:
                    pass
            return []

        def quick_update(acc_id: int, payload: dict) -> dict:
            if self._api is None:
                return {"ok": False, "error": "主服务未连接"}
            res = self._api.quick_update_credentials(acc_id, payload)
            # ``AccountApi.quick_update_credentials`` already notifies this panel;
            # only notify here when the API is not wired back to us.
            if (
                res.get("ok")
                and res.get("account")
                and getattr(self._api, "_login_tools_panel", None) is not self
            ):
                self.notify_account_updated(res["account"])
            return res

        def open_url(url: str) -> None:
            if self._api is None:
                return
            threading.Thread(
                target=lambda: self._api.launch_browser(account_id, target_url=url),
                daemon=True,
            ).start()

        def paste_value(value: str) -> None:
            self._paste_value_into_browser_async(session, browser, value)

        def smart_fill(field: str) -> None:
            self._smart_fill_field_async(account_id, session, browser, field)

        def start_oauth() -> None:
            strip = session.strips.get(browser)
            if self._api is None:
                return
            if strip:
                strip.set_message("正在发起反重力扫码授权...")

            def _run():
                res = self._api.start_builtin_oauth(account_id, launch_isolated=True, pure_native=True)
                if strip and not strip.form.IsDisposed:
                    msg = "✓ 请在打开的页面点击允许授权" if res.get("ok") else f"⚠ {res.get('error') or '发起失败'}"
                    self._marshal_strip_message(strip, msg, warning=not res.get("ok"))

            threading.Thread(target=_run, daemon=True).start()

        def inject_rt() -> None:
            strip = session.strips.get(browser)
            if self._api is None:
                return

            def _run():
                res = self._api.inject_rt_to_antigravity(account_id)
                if strip and not strip.form.IsDisposed:
                    msg = "✓ RT 已注入反重力！请重启客户端" if res.get("ok") else f"⚠ {res.get('error') or '注入失败'}"
                    self._marshal_strip_message(strip, msg, warning=not res.get("ok"))

            threading.Thread(target=_run, daemon=True).start()

        def probe_verify() -> None:
            strip = session.strips.get(browser)
            if self._api is None:
                return
            if strip:
                strip.set_message("正在拉取实时 Google 验证页...")

            def _run():
                res = self._api.probe_live_validation(account_id)
                url = str(res.get("target_url") or "").strip()
                if url:
                    self._api.launch_browser(account_id, target_url=url)
                if strip and not strip.form.IsDisposed:
                    msg = "✓ 已打开实时验证页" if url else (res.get("message") or "✓ 无需验证")
                    self._marshal_strip_message(strip, msg, warning=not res.get("ok"))

            threading.Thread(target=_run, daemon=True).start()

        def reset_session() -> None:
            strip = session.strips.get(browser)
            if self._api is None:
                return

            def _run():
                res = self._api.reset_rejected_session(account_id)
                if res.get("ok"):
                    self._api.launch_browser(account_id, target_url="https://accounts.google.com/ServiceLogin")
                if strip and not strip.form.IsDisposed:
                    msg = "✓ 已清理异常会话并重载登录页" if res.get("ok") else f"⚠ {res.get('error') or '清理失败'}"
                    self._marshal_strip_message(strip, msg, warning=not res.get("ok"))

            threading.Thread(target=_run, daemon=True).start()

        def clear_ag_cache() -> None:
            strip = session.strips.get(browser)
            if self._api is None:
                return
            res = self._api.clear_antigravity_cache()
            if strip:
                strip.set_message(
                    "✓ 反重力缓存已清除" if res.get("ok") else f"⚠ {res.get('error') or '清理失败'}",
                    is_warning=not res.get("ok"),
                )

        def is_url_intercept_enabled() -> bool:
            if self._api is None:
                return False
            return bool(self._api.get_url_interception_status().get("enabled"))

        def toggle_url_intercept() -> None:
            strip = session.strips.get(browser)
            if self._api is None:
                return
            cur = is_url_intercept_enabled()
            res = self._api.toggle_url_interception(not cur)
            if strip:
                if res.get("ok"):
                    strip.set_message(
                        "✓ 外链接管已开启" if res.get("enabled") else "已关闭外链接管"
                    )
                else:
                    strip.set_message(f"⚠ {res.get('error') or '切换失败'}", is_warning=True)

        def copy_persona() -> None:
            from System.Windows.Forms import Clipboard

            strip = session.strips.get(browser)
            acc = self._get_account(account_id)
            if not acc:
                return
            lines = ["=== 谷歌/养号注册资料 ===", f"邮箱: {acc.get('account', '')}", f"密码: {acc.get('password', '')}"]
            if acc.get("secret_key"):
                lines.append(f"2FA密钥: {acc['secret_key']}")
            if acc.get("recovery_email"):
                lines.append(f"辅助邮箱: {acc['recovery_email']}")
            if acc.get("reg_region"):
                lines.append(f"地区: {acc['reg_region']}")
            if acc.get("note"):
                lines.append(f"备注与资料: {acc['note']}")
            try:
                Clipboard.SetDataObject("\n".join(lines), True, 4, 50)
                if strip:
                    strip.set_message("✓ 已复制注册资料到剪贴板")
            except Exception:
                if strip:
                    strip.set_message("⚠ 复制失败", is_warning=True)

        def bind_2fa_clipboard() -> None:
            from System.Windows.Forms import Clipboard

            strip = session.strips.get(browser)
            if self._api is None:
                return
            raw = ""
            try:
                if Clipboard.ContainsText():
                    raw = Clipboard.GetText() or ""
            except Exception:
                raw = ""
            cleaned = normalize_secret(raw)
            if not cleaned:
                if strip:
                    strip.set_message("⚠ 剪贴板中未识别到有效 Base32 2FA", is_warning=True)
                return
            preview = generate_totp(cleaned, offset_seconds=get_offset())
            if not preview.get("valid"):
                if strip:
                    strip.set_message("⚠ 剪贴板 2FA 格式无效", is_warning=True)
                return
            acc = self._get_account(account_id) or {}
            is_google = (acc.get("account_type") or "").strip().lower() == "google"
            payload = {"secret_key": cleaned, "twofa_modified": 1, "reason": "hud_bind_2fa"}
            if is_google:
                payload["google_confirmed"] = True
            res = quick_update(account_id, payload)
            if strip:
                if res.get("ok"):
                    strip.set_message("✓ 2FA 已绑定，实时码已生效")
                else:
                    strip.set_message(f"⚠ {res.get('error') or '绑定失败'}", is_warning=True)

        return {
            "get_totp_offset": get_offset,
            "get_top_recovery": get_top_recovery,
            "quick_update": quick_update,
            "open_url": open_url,
            "paste_value": paste_value,
            "smart_fill": smart_fill,
            "start_oauth": start_oauth,
            "inject_rt": inject_rt,
            "probe_verify": probe_verify,
            "reset_session": reset_session,
            "clear_ag_cache": clear_ag_cache,
            "is_url_intercept_enabled": is_url_intercept_enabled,
            "toggle_url_intercept": toggle_url_intercept,
            "copy_persona": copy_persona,
            "bind_2fa_clipboard": bind_2fa_clipboard,
        }

    def _marshal_strip_message(self, strip: LoginToolsStrip, msg: str, *, warning: bool = False) -> None:
        from System import Action

        if strip.form.IsDisposed:
            return
        try:
            if strip.form.InvokeRequired:
                strip.form.BeginInvoke(Action(lambda: strip.set_message(msg, is_warning=warning)))
            else:
                strip.set_message(msg, is_warning=warning)
        except Exception:
            pass

    def _start_geo_check_async(self, account_id: int, session: _BrowserSession, strip: LoginToolsStrip) -> None:
        if self._api is None:
            return
        try:
            settings = self._api._ensure_settings()
            if settings.get("auto_probe_geo_on_launch") is False:
                return
        except Exception:
            pass

        def _worker():
            from System import Action
            from src.maintenance_service import probe_proxy_geo

            acc = self._get_account(account_id)
            if not acc:
                return
            fp = acc.get("fp_config") or {}
            proxy = session.runtime_proxy or str(fp.get("proxy") or "").strip()
            geo = probe_proxy_geo(proxy, timeout=6.0)
            if not geo.get("ok") or strip.form.IsDisposed:
                return
            try:
                updated = self._api.db.update_account_fp_geo_silent(account_id, geo, auto_apply_tz=True)
                if updated and callable(self._on_account_updated):
                    self._on_account_updated(updated)
            except Exception:
                pass

            cc = geo.get("country_code") or ""
            ip = geo.get("ip") or ""
            tz = geo.get("timezone") or ""
            p_off = geo.get("proxy_offset_hours")
            h_off = geo.get("host_offset_hours")
            if geo.get("tz_mismatch") and p_off is not None and h_off is not None:
                p_str = f"{p_off:+g}"
                h_str = f"{h_off:+g}"
                short_txt = f"⚠️ 时区差(代{p_str}/本{h_str})"
                tip = (
                    f"⚠️ 时区偏差提醒：\n"
                    f"• 代理出口: {cc} {ip} ({tz}, UTC{p_str})\n"
                    f"• 本机系统: UTC{h_str}\n"
                    f"Windows 原生 Chrome 网页会读取宿主机时区；高敏养号建议将系统时区切至代理地区或使用同区节点。"
                )
                is_warn = True
            else:
                short_txt = f"🌐 {cc} {ip}".strip()
                tip = f"出口 IP: {ip} ({cc}) | 时区: {tz}"
                is_warn = False

            def _apply():
                if strip.form.IsDisposed:
                    return
                strip.set_geo_status(short_txt, tooltip_text=tip, is_warning=is_warn)

            try:
                if strip.form.InvokeRequired:
                    strip.form.BeginInvoke(Action(_apply))
                else:
                    _apply()
            except Exception:
                pass

        threading.Thread(target=_worker, name=f"kv-hud-geo-{account_id}", daemon=True).start()

    def _smart_fill_field_async(
        self,
        account_id: int,
        session: _BrowserSession,
        browser: int,
        field: str,
    ) -> None:
        from System.Windows.Forms import Clipboard

        strip = session.strips.get(browser)
        if strip is None:
            return
        account = self._get_account(account_id)
        if account is None:
            self._dismiss(session, browser)
            return
        self._refresh_account(strip, account)

        labels = {
            "account": "邮箱",
            "password": "密码",
            "totp": "2FA码",
            "recovery": "辅助邮箱",
        }
        label = labels.get(field, field)

        if field == "totp":
            secret = normalize_secret(account.get("secret_key") or "")
            if not secret:
                strip.set_message("⚠ 未录入2FA", is_warning=True)
                return
            code_info = self._current_totp(secret)
            if int(code_info.get("remaining") or 0) <= 2:
                strip.set_message("⏳ 等待新周期 2FA...")

                def _wait_and_paste():
                    time.sleep(min(2.3, float(code_info["remaining"]) + 0.15))
                    fresh = self._current_totp(secret)
                    val = fresh.get("code") or ""
                    if val:
                        self._copy_and_paste_bg(strip, session, browser, val, label)

                threading.Thread(target=_wait_and_paste, daemon=True).start()
                return
            value = code_info.get("code") or ""
        elif field == "recovery":
            value = str(account.get("recovery_email") or "").strip()
        else:
            value = str(account.get(field) or "")

        if not value:
            strip.set_message(f"⚠ 未录入{label}", is_warning=True)
            return

        try:
            Clipboard.SetDataObject(value, True, 4, 50)
        except Exception:
            strip.set_message("⚠ 剪贴板写入失败", is_warning=True)
            return

        need_delay = self._focus_browser_on_ui_thread(browser)
        threading.Thread(
            target=lambda: self._paste_only_bg(strip, session, browser, label, delay_for_focus=need_delay),
            daemon=True,
        ).start()

    def _focus_browser_on_ui_thread(self, browser: int, *, force_focus: bool = False) -> bool:
        """Transfer foreground focus from the active UI thread so Windows allows ``SetForegroundWindow``."""
        is_chromium = getattr(self._windows, "is_chromium_window", None)
        focus_fn = getattr(self._windows, "focus_window", None)
        fg = self._windows.foreground()
        if force_focus or (callable(is_chromium) and not is_chromium(fg)):
            if callable(focus_fn):
                focus_fn(browser)
                return True
        return False

    def _copy_and_paste_bg(
        self, strip: LoginToolsStrip, session: _BrowserSession, browser: int, value: str, label: str
    ) -> None:
        from System import Action
        from System.Windows.Forms import Clipboard

        focused = {"needed": False}

        def _set_clip_and_focus():
            try:
                Clipboard.SetDataObject(value, True, 4, 50)
            except Exception:
                pass
            focused["needed"] = self._focus_browser_on_ui_thread(browser)

        try:
            if strip.form.InvokeRequired:
                strip.form.Invoke(Action(_set_clip_and_focus))
            else:
                _set_clip_and_focus()
        except Exception:
            pass
        self._paste_only_bg(strip, session, browser, label, delay_for_focus=focused["needed"])

    def _paste_value_into_browser_async(
        self, session: _BrowserSession, browser: int, value: str
    ) -> None:
        from System.Windows.Forms import Clipboard

        if not value:
            return
        try:
            Clipboard.SetDataObject(value, True, 4, 50)
        except Exception:
            return
        strip = session.strips.get(browser)
        need_delay = self._focus_browser_on_ui_thread(browser, force_focus=True)
        threading.Thread(
            target=lambda: self._paste_only_bg(strip, session, browser, "内容", delay_for_focus=need_delay),
            daemon=True,
        ).start()

    def _paste_only_bg(
        self,
        strip: LoginToolsStrip | None,
        session: _BrowserSession,
        browser: int,
        label: str,
        *,
        delay_for_focus: bool = False,
    ) -> None:
        if not self._typing_lock.acquire(blocking=False):
            return
        try:
            is_chromium = getattr(self._windows, "is_chromium_window", None)
            paste_fn = getattr(self._windows, "send_ctrl_v_paste", None)
            if delay_for_focus:
                time.sleep(0.095)
            fg = self._windows.foreground()
            if callable(is_chromium) and is_chromium(fg) and callable(paste_fn):
                time.sleep(0.035)
                paste_fn()
                if strip:
                    self._marshal_strip_message(strip, f"✓ 已填入{label}")
            else:
                if strip:
                    self._marshal_strip_message(strip, f"✓ 已复制{label}")
        finally:
            self._typing_lock.release()

    def _copy(self, account_id: int, session: _BrowserSession, browser: int, field: str) -> None:
        from System.Windows.Forms import Clipboard

        strip = session.strips.get(browser)
        if strip is None or browser not in self._windows.windows_for_profile(session.profile_dir):
            return
        if self._windows.foreground() != browser:
            return
        try:
            account = self._get_account(account_id)
            if account is None:
                self._dismiss(session, browser)
                return
            self._refresh_account(strip, account)
            value = self._current_totp(getattr(strip, "secret", ""))["code"] if field == "totp" else account[field]
            if value:
                Clipboard.SetDataObject(value, True, 4, 50)
                strip.set_message("已复制")
            else:
                strip.set_message("无可复制内容")
        except Exception:
            strip.set_message("复制失败")

    def _current_totp(self, secret: str) -> dict:
        return generate_totp(secret, offset_seconds=self._get_totp_offset())

    def _refresh_account(self, strip: LoginToolsStrip, account: dict) -> None:
        try:
            strip.set_account(account["account"], bool(account.get("password")), account)
        except TypeError:
            strip.set_account(account["account"], bool(account.get("password")))
        strip.secret = account.get("secret_key", "")
        strip.last_account_check = time.monotonic()

    def _dismiss(self, session: _BrowserSession, browser: int) -> None:
        with self._sessions_lock:
            session.dismissed.add(browser)
            self._remove_strip(session, browser)

    def _remove_strip(self, session: _BrowserSession, browser: int) -> None:
        strip = session.strips.pop(browser, None)
        if strip is not None:
            self._windows.hide_toolbar(strip.handle)
            if hasattr(strip, "dispose"):
                strip.dispose()
            else:
                strip.form.Dispose()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.Dispose()
            self._timer = None

    def close(self) -> None:
        def _do_close():
            self._stop_timer()
            with self._sessions_lock:
                for session in list(self._sessions.values()):
                    for browser in list(session.strips):
                        self._remove_strip(session, browser)
                self._sessions.clear()
                self._last_active_account_id = None

        owner = _winforms_window(self._window) if self._window is not None else None
        if owner is not None and not getattr(owner, "IsDisposed", True) and getattr(owner, "InvokeRequired", False):
            try:
                from System import Action

                owner.Invoke(Action(_do_close))
                return
            except Exception:
                pass
        _do_close()
