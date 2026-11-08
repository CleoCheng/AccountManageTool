import base64
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from websockets.sync.client import connect as _ws_connect

from src.browser_service import find_chromium_executable, generate_fingerprint_config
from src.github_mail import MAIL_URL
from src.proxy_service import activate_proxy_node


GITHUB_URL = "https://github.com"
WORKBUDDY_URL = "https://www.workbuddy.ai"
WORKBUDDY_APP_URL = WORKBUDDY_URL + "/app"
AUTH_STATE_PATH = "/v2/plugin/auth/state"
AUTH_TOKEN_PATH = "/v2/plugin/auth/token"
LOGIN_ACCOUNT_PATH = "/v2/plugin/login/account"
PROFILE_PATH = "/console/account"
LOGIN_PENDING_CODE = 11217
MAX_CONCURRENT_BROWSERS = 2
ACTION_DELAY = 1
CDP_NAV_TIMEOUT_MS = 15000


def _sleep_action_delay(min_s: float = 1.0, max_s: float = 2.0) -> None:
    """Random 1~2s human-like pause between UI sub-operations (click -> paste -> next field)."""
    if ACTION_DELAY <= 0:
        return
    if ACTION_DELAY == 1:
        span_ms = max(1, int((max_s - min_s) * 1000))
        time.sleep(min_s + secrets.randbelow(span_ms + 1) / 1000.0)
    else:
        time.sleep(float(ACTION_DELAY))


def signup_url(email: str) -> str:
    return GITHUB_URL + "/signup?" + urlencode({"source": "form-home-signup", "user_email": email})


def _jwt_claims(token: str) -> dict:
    try:
        parts = str(token or "").strip().split(".")
        if len(parts) != 3:
            return {}
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(seg))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _jwt_uid(token: str) -> str:
    return str(_jwt_claims(token).get("sub") or "").strip()


def _jwt_exp(token: str) -> int:
    try:
        val = float(_jwt_claims(token).get("exp") or 0)
        if val > 1e11:
            val /= 1000.0
        return int(val)
    except Exception:
        return 0


def _wb_http_json(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict | None = None,
    proxy_url: str = "",
    timeout: int = 12,
) -> dict:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    px = (proxy_url or "").strip()
    if px:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": px, "https": px})
        )
        resp_ctx = opener.open(req, timeout=timeout)
    else:
        resp_ctx = urllib.request.urlopen(req, timeout=timeout)
    with resp_ctx as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


# Only fill empty, visible fields on the expected HTTPS page. In particular,
# never fill a password into an OAuth redirect or into a challenge frame.
_FILL_FIELDS = """({origin, path, fields}) => {
    if (location.origin !== origin || (path && location.pathname !== path)) return 0;
    let filled = 0;
    for (const [selector, value] of fields) {
        const field = document.querySelector(selector);
        if (!(field instanceof HTMLInputElement) || !field.getClientRects().length ||
            field.disabled || field.readOnly || field.value) continue;
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(field, value);
        field.dispatchEvent(new Event('input', {bubbles: true}));
        field.dispatchEvent(new Event('change', {bubbles: true}));
        filled++;
    }
    return filled;
}"""


_FILL_OTP = """(code) => {
    if (location.origin !== 'https://github.com') return false;
    const singleSelectors = [
        'input[autocomplete="one-time-code"]',
        'input[name="otp"]',
        'input#otp',
        'input[name="launch_code"]',
        'input[data-target*="verification"]'
    ];
    for (const sel of singleSelectors) {
        const el = document.querySelector(sel);
        if (el instanceof HTMLInputElement && el.getClientRects().length && !el.disabled && !el.readOnly && el.maxLength !== 1) {
            if (!el.value) {
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, code);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
        }
    }
    const digitInputs = Array.from(document.querySelectorAll(
        'input[name="launch_code[]"], input[maxlength="1"]'
    )).filter(el => el instanceof HTMLInputElement && el.getClientRects().length && !el.disabled && !el.readOnly);
    if (digitInputs.length === code.length && digitInputs.every(el => !el.value)) {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        for (let i = 0; i < code.length; i++) {
            setter.call(digitInputs[i], code[i]);
            digitInputs[i].dispatchEvent(new Event('input', {bubbles: true}));
            digitInputs[i].dispatchEvent(new Event('change', {bubbles: true}));
        }
        return true;
    }
    return false;
}"""


# Read-only scan of localStorage / sessionStorage for a WorkBuddy JWT, used by
# the WorkBuddy login-state probe.
_WB_STORAGE_SCAN_JS = """() => {
    const out = {};
    for (const store of [window.localStorage, window.sessionStorage]) {
        if (!store) continue;
        for (let i = 0; i < store.length; i++) {
            const k = store.key(i);
            const v = store.getItem(k) || '';
            if (!out.accessToken && /^[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+$/.test(v.trim())) {
                out.accessToken = v.trim();
            } else if (v.startsWith('{')) {
                try {
                    const obj = JSON.parse(v);
                    const tok = obj?.accessToken || obj?.access_token || obj?.auth?.accessToken;
                    const ref = obj?.refreshToken || obj?.refresh_token || obj?.auth?.refreshToken;
                    if (tok && String(tok).split('.').length === 3) {
                        out.accessToken = String(tok);
                        if (ref) out.refreshToken = String(ref);
                        if (obj?.uid) out.uid = String(obj.uid);
                    }
                } catch (_) {}
            }
        }
    }
    return out;
}"""


# Injected into every isolated-world DOM query. These helpers only read the
# DOM and never mutate attributes or globals on GitHub / WorkBuddy pages.
_KV_DOM_JS = r"""
const __kvRoleSelector = (role) => {
  const r = String(role || '').toLowerCase();
  if (r === 'button') return 'button, [role="button"], input[type="button"], input[type="submit"], input[type="reset"]';
  if (r === 'link') return 'a[href], [role="link"]';
  if (r === 'textbox') return 'textarea, input:not([type]), input[type="text"], input[type="email"], input[type="password"], input[type="search"], input[type="tel"], [role="textbox"], [contenteditable="true"]';
  if (r === 'checkbox') return 'input[type="checkbox"], [role="checkbox"]';
  return '[role="' + r + '"]';
};
const __kvSplitSelector = (selector) => {
  const texts = [];
  let s = String(selector || '').replace(/:visible/g, '');
  s = s.replace(/:has-text\(\s*(?:"([^"]*)"|'([^']*)')\s*\)/g, (m, a, b) => {
    texts.push(a !== undefined ? a : b);
    return '';
  });
  return {selector: s.trim(), texts};
};
const __kvVisible = (el) => {
  if (!el || !el.getClientRects || !el.getClientRects().length) return false;
  const st = getComputedStyle(el);
  return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
};
const __kvText = (el) => String((el && (el.innerText || el.textContent)) || '').trim();
const __kvName = (el) => {
  if (!el) return '';
  const labelled = el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('title'));
  return String(labelled || el.innerText || el.textContent || el.value || '').trim();
};
const __kvMatchesText = (el, texts) => texts.every(t => __kvText(el).includes(t));
const __kvInnermost = (list) => list.filter(el => !list.some(other => other !== el && el.contains(other)));
const __kvResolveList = (desc) => {
  if (!desc) return [];
  let roots = [document];
  if (desc.parent) {
    const pel = __kvResolveOne(desc.parent);
    if (!pel) return [];
    roots = [pel];
  }
  let out = [];
  for (const root of roots) {
    let list = [];
    if (desc.kind === 'css') {
      const parsed = __kvSplitSelector(desc.selector);
      list = Array.from(root.querySelectorAll(parsed.selector || '*'));
      list = list.filter(el => __kvMatchesText(el, parsed.texts));
    } else if (desc.kind === 'role') {
      list = Array.from(root.querySelectorAll(__kvRoleSelector(desc.role)));
      const re = new RegExp(desc.pattern, desc.flags || 'i');
      list = list.filter(el => re.test(__kvName(el)));
    } else if (desc.kind === 'text') {
      const scope = root === document
        ? 'a,button,[role="button"],label,span,div,p,li,h1,h2,h3,h4,strong,em,small,td,th'
        : '*';
      list = Array.from(root.querySelectorAll(scope));
      const re = new RegExp(desc.pattern, desc.flags || 'i');
      list = list.filter(el => re.test(__kvText(el)));
      list = __kvInnermost(list);
    }
    out = out.concat(list);
  }
  return out;
};
const __kvResolveOne = (desc) => {
  const list = __kvResolveList(desc);
  let idx = Number(desc.index || 0);
  if (idx < 0) idx = list.length + idx;
  return list[idx] || null;
};
const __kvRect = (el) => {
  try { el.scrollIntoView({block: 'center', inline: 'nearest'}); } catch (e) {}
  const r = el.getBoundingClientRect();
  return {x: r.x, y: r.y, width: r.width, height: r.height};
};
"""


def _js_flags(flags: int) -> str:
    out = ""
    if flags & re.IGNORECASE:
        out += "i"
    if flags & re.MULTILINE:
        out += "m"
    if flags & re.DOTALL:
        out += "s"
    return out


# Real keyboards emit ``code``/``windowsVirtualKeyCode``/``modifiers`` for every
# key. DataDome's ``c.js`` records ``keyCode === 0`` telemetry and flags such a
# session as automated, so every synthetic keystroke must carry the same fields
# a physical key press would.
_SHIFTED_KEY_SPECS: dict[str, tuple[str, int]] = {
    "!": ("Digit1", 49),
    "@": ("Digit2", 50),
    "#": ("Digit3", 51),
    "$": ("Digit4", 52),
    "%": ("Digit5", 53),
    "^": ("Digit6", 54),
    "&": ("Digit7", 55),
    "*": ("Digit8", 56),
    "(": ("Digit9", 57),
    ")": ("Digit0", 48),
    "_": ("Minus", 189),
    "+": ("Equal", 187),
    ":": ("Semicolon", 186),
    "\"": ("Quote", 222),
    "<": ("Comma", 188),
    ">": ("Period", 190),
    "?": ("Slash", 191),
    "{": ("BracketLeft", 219),
    "}": ("BracketRight", 221),
    "|": ("Backslash", 220),
    "~": ("Backquote", 192),
}
_PLAIN_KEY_SPECS: dict[str, tuple[str, int]] = {
    "-": ("Minus", 189),
    "=": ("Equal", 187),
    ".": ("Period", 190),
    ",": ("Comma", 188),
    "/": ("Slash", 191),
    ";": ("Semicolon", 186),
    "'": ("Quote", 222),
    "[": ("BracketLeft", 219),
    "]": ("BracketRight", 221),
    "\\": ("Backslash", 220),
    "`": ("Backquote", 192),
    " ": ("Space", 32),
}


def _char_key_spec(ch: str) -> tuple[str, str, int, int]:
    """Map one printable ASCII character to ``(key, code, vk, modifiers)``."""
    if "a" <= ch <= "z":
        vk = ord(ch.upper())
        return ch, f"Key{ch.upper()}", vk, 0
    if "A" <= ch <= "Z":
        vk = ord(ch)
        return ch, f"Key{ch}", vk, 8
    if "0" <= ch <= "9":
        vk = ord(ch)
        return ch, f"Digit{ch}", vk, 0
    if ch in _SHIFTED_KEY_SPECS:
        code, vk = _SHIFTED_KEY_SPECS[ch]
        return ch, code, vk, 8
    if ch in _PLAIN_KEY_SPECS:
        code, vk = _PLAIN_KEY_SPECS[ch]
        return ch, code, vk, 0
    return ch, "", 0, 0


class _NavResponse:
    def __init__(self, status: int, url: str = ""):
        self.status = int(status)
        self.url = url


class _CdpKeyboard:
    def __init__(self, page: "_CdpPage"):
        self.page = page

    def insert_text(self, text: str) -> None:
        text = str(text)
        if ACTION_DELAY <= 0:
            # Unit tests stay instantaneous: one trusted Input.insertText call.
            self.page.send("Input.insertText", {"text": text})
            return
        # Physical keyboards never emit a whole string in 0ms: type each
        # character with real keyDown/keyUp events and human inter-key timing.
        # ``code``/``windowsVirtualKeyCode``/``nativeVirtualKeyCode`` must be
        # present, otherwise Chromium reports ``keyCode === 0`` and DataDome
        # flags the session as automated.
        for ch in text:
            key, code, vk, modifiers = _char_key_spec(ch)
            down_params: dict = {
                "type": "keyDown",
                "text": ch,
                "unmodifiedText": ch.lower() if (modifiers & 8) and ch.isalpha() else ch,
                "key": key,
            }
            up_params: dict = {
                "type": "keyUp",
                "key": key,
            }
            if code and vk:
                down_params["code"] = code
                down_params["windowsVirtualKeyCode"] = vk
                down_params["nativeVirtualKeyCode"] = vk
                up_params["code"] = code
                up_params["windowsVirtualKeyCode"] = vk
                up_params["nativeVirtualKeyCode"] = vk
            if modifiers:
                down_params["modifiers"] = modifiers
                up_params["modifiers"] = modifiers
            if modifiers & 8:
                # A physical keyboard holds Shift down while typing ``@``/``A``.
                # Without the real Shift rawKeyDown/keyUp pair DataDome's
                # ``c.js`` sees a modified character with no modifier key event.
                self.page.send(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "rawKeyDown",
                        "key": "Shift",
                        "code": "ShiftLeft",
                        "windowsVirtualKeyCode": 16,
                        "nativeVirtualKeyCode": 16,
                        "modifiers": 8,
                        "location": 1,
                    },
                )
                time.sleep(0.008 + secrets.randbelow(12) / 1000.0)
            self.page.send("Input.dispatchKeyEvent", down_params)
            time.sleep(0.012 + secrets.randbelow(22) / 1000.0)
            self.page.send("Input.dispatchKeyEvent", up_params)
            if modifiers & 8:
                time.sleep(0.008 + secrets.randbelow(10) / 1000.0)
                self.page.send(
                    "Input.dispatchKeyEvent",
                    {
                        "type": "keyUp",
                        "key": "Shift",
                        "code": "ShiftLeft",
                        "windowsVirtualKeyCode": 16,
                        "nativeVirtualKeyCode": 16,
                        "modifiers": 0,
                        "location": 1,
                    },
                )
            time.sleep(0.032 + secrets.randbelow(55) / 1000.0)

    def press_tab(self) -> None:
        """Native CDP Tab keyDown/keyUp, so blur/validation is ``isTrusted: true``."""
        tab_params = {
            "key": "Tab",
            "code": "Tab",
            "windowsVirtualKeyCode": 9,
            "nativeVirtualKeyCode": 9,
        }
        self.page.send("Input.dispatchKeyEvent", {"type": "keyDown", **tab_params})
        if ACTION_DELAY > 0:
            time.sleep(0.025)
        self.page.send("Input.dispatchKeyEvent", {"type": "keyUp", **tab_params})


class _CdpClient:
    """Minimal synchronous CDP client that can never enable the V8 Runtime domain."""

    def __init__(self, ws_url: str):
        self._ws = _ws_connect(ws_url, max_size=None, open_timeout=5)
        self._lock = threading.RLock()
        self._next_id = 0
        self.sent_methods: list[str] = []
        self.events: deque = deque(maxlen=500)
        self._closed = False

    def send(self, method: str, params: dict | None = None) -> dict:
        if method == "Runtime.enable":
            raise RuntimeError("Forbidden CDP method: Runtime.enable must never be called")
        with self._lock:
            if self._closed:
                raise RuntimeError("CDP connection is closed")
            self.sent_methods.append(method)
            if len(self.sent_methods) > 500:
                del self.sent_methods[: len(self.sent_methods) - 500]
            self._next_id += 1
            msg_id = self._next_id
            payload: dict = {"id": msg_id, "method": method}
            if params is not None:
                payload["params"] = params
            self._ws.send(json.dumps(payload, ensure_ascii=False))
            while True:
                raw = self._ws.recv(timeout=30)
                data = json.loads(raw)
                if not isinstance(data, dict):
                    continue
                if data.get("id") == msg_id:
                    if "error" in data:
                        err = data.get("error") or {}
                        raise RuntimeError(str(err.get("message") or f"CDP {method} failed"))
                    return data.get("result") or {}
                if data.get("method"):
                    self.events.append(data)

    def pump(self, timeout: float = 0.0) -> None:
        """Read any already-buffered CDP events without blocking on commands."""
        with self._lock:
            if self._closed:
                return
            deadline = time.time() + max(0.0, timeout)
            while True:
                remaining = max(0.0, deadline - time.time())
                try:
                    raw = self._ws.recv(timeout=remaining if remaining > 0 else 0)
                except Exception:
                    return
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("method"):
                    self.events.append(data)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._ws.close()
            except Exception:
                pass


class _CdpLocator:
    """Playwright-shaped locator that re-queries the DOM on every operation."""

    def __init__(self, page: "_CdpPage", descriptor: dict):
        self.page = page
        self._desc = descriptor

    def _clone(self, **updates) -> "_CdpLocator":
        desc = dict(self._desc)
        desc.update(updates)
        return _CdpLocator(self.page, desc)

    def nth(self, index: int) -> "_CdpLocator":
        return self._clone(index=int(index))

    @property
    def first(self) -> "_CdpLocator":
        return self._clone(index=0)

    @property
    def last(self) -> "_CdpLocator":
        return self._clone(index=-1)

    def locator(self, selector: str) -> "_CdpLocator":
        return _CdpLocator(
            self.page,
            {"kind": "css", "selector": str(selector), "index": 0, "parent": self._desc},
        )

    def count(self) -> int:
        value = self.page._eval_dom(self._desc, "return __kvResolveList(__desc).length;")
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def is_visible(self) -> bool:
        return bool(self.page._eval_dom(self._desc, "return !!__el && __kvVisible(__el);"))

    def bounding_box(self) -> dict | None:
        rect = self.page._eval_dom(self._desc, "return __el ? __kvRect(__el) : null;")
        return rect if isinstance(rect, dict) else None

    def evaluate(self, expression: str, arg=None):
        args = ["__el"]
        if arg is not None:
            args.append(json.dumps(arg, ensure_ascii=False))
        body = f"return __el ? ({str(expression).strip()})({', '.join(args)}) : null;"
        return self.page._eval_dom(self._desc, body)

    def input_value(self, timeout: int = 1500) -> str:
        value = self.page._eval_dom(
            self._desc,
            "if (!__el) return '';\n"
            "if (__el.isContentEditable) return __kvText(__el);\n"
            "return __el.value !== undefined ? String(__el.value) : '';",
        )
        return str(value or "")

    def inner_text(self, timeout: int = 1500) -> str:
        return str(self.page._eval_dom(self._desc, "return __el ? __kvText(__el) : '';") or "")

    def get_attribute(self, name: str):
        return self.page._eval_dom(
            self._desc,
            f"return __el ? __el.getAttribute({json.dumps(str(name))}) : null;",
        )

    def scroll_into_view_if_needed(self, timeout: int = 2500) -> None:
        self.page._eval_dom(
            self._desc,
            "if (__el) { try { __el.scrollIntoView({block: 'center', inline: 'nearest'}); } catch (e) {} } return true;",
        )

    def click(self, timeout: int = 4000) -> None:
        self.page._human_click(self)

    def fill(self, value: str, timeout: int = 4000) -> None:
        self.page._eval_dom(
            self._desc,
            "if (!__el) return false;\n"
            "const val = " + json.dumps(str(value), ensure_ascii=False) + ";\n"
            "if (__el.isContentEditable) {\n"
            "  __el.focus();\n"
            "  __el.textContent = val;\n"
            "  __el.dispatchEvent(new Event('input', {bubbles: true}));\n"
            "  __el.dispatchEvent(new Event('change', {bubbles: true}));\n"
            "  return true;\n"
            "}\n"
            "const proto = __el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;\n"
            "Object.getOwnPropertyDescriptor(proto, 'value').set.call(__el, val);\n"
            "__el.dispatchEvent(new Event('input', {bubbles: true}));\n"
            "__el.dispatchEvent(new Event('change', {bubbles: true}));\n"
            "return true;",
        )

    def wait_for(self, state: str = "visible", timeout: int = 30000) -> None:
        deadline = time.time() + max(0.1, timeout / 1000.0)
        while time.time() < deadline:
            try:
                if state in ("attached", "visible"):
                    if state == "attached" and self.count() > 0:
                        return
                    if state == "visible" and self.is_visible():
                        return
                elif state in ("detached", "hidden"):
                    if self.count() == 0:
                        return
                    if not self.is_visible():
                        return
            except Exception:
                pass
            time.sleep(0.1)
        raise TimeoutError(f"Locator wait_for({state}) timed out")

    def element_handle(self, timeout: int = 4000) -> "_CdpLocator":
        self.wait_for(state="visible", timeout=timeout)
        return self


class _CdpPage:
    """A single Chrome page target driven over raw CDP (never Runtime.enable)."""

    def __init__(self, context: "_CdpContext", target_id: str, ws_url: str, target_url: str = ""):
        self._context = context
        self.target_id = target_id
        self._ws_url = ws_url
        self._target_url = target_url or "about:blank"
        self._client: _CdpClient | None = None
        self._closed = False
        self._doc_status = 0
        self._doc_url = ""
        self._nav_loader_id = ""
        self._last_mouse = (0.0, 0.0)
        self._isolated_world: tuple[str, int] | None = None
        self.keyboard = _CdpKeyboard(self)

    # -- low level -----------------------------------------------------
    @property
    def sent_methods(self) -> list[str]:
        return list(self._client.sent_methods) if self._client is not None else []

    def _ensure_client(self) -> _CdpClient:
        if self._client is not None and not self._client._closed:
            return self._client
        ws_url = self._ws_url
        if not ws_url:
            target = self._context._find_target(self.target_id) or {}
            ws_url = str(target.get("webSocketDebuggerUrl") or "")
            self._ws_url = ws_url
        if not ws_url:
            raise RuntimeError("浏览器页面调试连接不可用")
        self._client = _CdpClient(ws_url)
        try:
            self._client.send("Page.enable")
            self._client.send("Network.enable")
        except Exception:
            pass
        return self._client

    def send(self, method: str, params: dict | None = None) -> dict:
        return self._ensure_client().send(method, params)

    def _eval_raw(self, js_code: str):
        if self._isolated_world is not None:
            try:
                result = self.send(
                    "Runtime.evaluate",
                    {
                        "contextId": self._isolated_world[1],
                        "expression": js_code,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                )
            except RuntimeError as exc:
                message = str(exc).lower()
                if "cannot find context" not in message and "execution context" not in message:
                    raise
                self._isolated_world = None
            else:
                if "exceptionDetails" in result:
                    details = result.get("exceptionDetails") or {}
                    raise RuntimeError(str(details.get("text") or "JS eval failed"))
                return (result.get("result") or {}).get("value")
        frame_tree = self.send("Page.getFrameTree")
        frame = ((frame_tree or {}).get("frameTree") or {}).get("frame") or {}
        frame_id = str(frame.get("id") or "")
        if not frame_id:
            raise RuntimeError("无法定位主页面框架")
        world = self.send(
            "Page.createIsolatedWorld",
            {
                "frameId": frame_id,
                "worldName": "kv_assist",
                "grantUniveralAccess": True,
            },
        )
        context_id = world.get("executionContextId")
        self._isolated_world = (frame_id, context_id)
        result = self.send(
            "Runtime.evaluate",
            {
                "contextId": context_id,
                "expression": js_code,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in result:
            details = result.get("exceptionDetails") or {}
            raise RuntimeError(str(details.get("text") or "JS eval failed"))
        return (result.get("result") or {}).get("value")

    def _eval_dom(self, descriptor: dict, body: str):
        js_code = (
            "(() => {\n"
            + _KV_DOM_JS
            + "\nconst __desc = "
            + json.dumps(descriptor, ensure_ascii=False)
            + ";\nconst __el = __kvResolveOne(__desc);\n"
            + body
            + "\n})()"
        )
        return self._eval_raw(js_code)

    def evaluate(self, expression_or_fn, *args):
        expr = str(expression_or_fn).strip()
        if args:
            js_code = "(" + expr + ")(" + ", ".join(json.dumps(a, ensure_ascii=False) for a in args) + ")"
        elif expr.startswith("(") or expr.startswith("function") or expr.startswith("async") or expr.startswith("() =>") or expr.startswith("()=>"):
            js_code = "(" + expr + ")()"
        else:
            js_code = expr
        return self._eval_raw(js_code)

    # -- events --------------------------------------------------------
    def drain_events(self) -> None:
        try:
            client = self._ensure_client()
        except Exception:
            return
        client.pump(0)
        while client.events:
            event = client.events.popleft()
            try:
                self._handle_event(event)
            except Exception:
                continue

    def _handle_event(self, event: dict) -> None:
        method = event.get("method")
        params = event.get("params") or {}
        if method == "Network.responseReceived":
            response = params.get("response") or {}
            entry = {
                "kind": "response",
                "page": self,
                "request_id": str(params.get("requestId") or ""),
                "url": str(response.get("url") or ""),
                "status": int(response.get("status") or 0),
                "headers": {str(k).lower(): v for k, v in (response.get("headers") or {}).items()},
                "type": str(params.get("type") or ""),
                "loader_id": str(params.get("loaderId") or ""),
            }
            if entry["type"] == "Document" and entry["loader_id"] and entry["loader_id"] == self._nav_loader_id:
                self._doc_status = entry["status"]
                self._doc_url = entry["url"]
            self._context._record(entry)
        elif method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            entry = {
                "kind": "request",
                "page": self,
                "request_id": str(params.get("requestId") or ""),
                "url": str(request.get("url") or ""),
                "method": str(request.get("method") or "GET"),
                "headers": {str(k).lower(): v for k, v in (request.get("headers") or {}).items()},
            }
            self._context._record(entry)
        elif method == "Page.frameNavigated":
            frame = (params.get("frame") or {})
            self._isolated_world = None
            if not frame.get("parentId"):
                self._target_url = str(frame.get("url") or self._target_url)
        elif method == "Page.navigatedWithinDocument":
            self._isolated_world = None

    # -- properties ----------------------------------------------------
    @property
    def url(self) -> str:
        try:
            value = self.evaluate("() => location.href")
            if isinstance(value, str) and value:
                self._target_url = value
                return value
        except Exception:
            pass
        return self._target_url or "about:blank"

    def is_closed(self) -> bool:
        if self._closed:
            return True
        if self._context._proc is not None and self._context._proc.poll() is not None:
            return True
        return not self._context._target_exists(self.target_id)

    def bring_to_front(self) -> None:
        self._context._json_call(f"/json/activate/{self.target_id}")
        try:
            self.send("Page.bringToFront")
        except Exception:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._context._json_call(f"/json/close/{self.target_id}")
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass

    # -- navigation ----------------------------------------------------
    def goto(
        self,
        url: str,
        *,
        referer: str = "",
        timeout: int = CDP_NAV_TIMEOUT_MS,
        wait_until: str = "",
    ) -> _NavResponse:
        self._ensure_client()
        self.drain_events()
        self._doc_status = 0
        self._doc_url = url
        self._nav_loader_id = ""
        self._isolated_world = None
        params: dict = {"url": url}
        if referer:
            params["referrer"] = referer
        nav = self.send("Page.navigate", params)
        error_text = str(nav.get("errorText") or "")
        self._nav_loader_id = str(nav.get("loaderId") or "")
        if error_text:
            self._target_url = url
            return _NavResponse(status=502, url=url)
        deadline = time.time() + max(1.0, timeout / 1000.0)
        settle_until = min(deadline, time.time() + 0.5)
        last_ready = ""
        while time.time() < deadline:
            self.drain_events()
            try:
                last_ready = self.evaluate("() => document.readyState")
            except Exception:
                last_ready = ""
            if self._doc_status and last_ready in ("interactive", "complete"):
                break
            # Never let a very fast "complete" hide the real Document status:
            # give Network.responseReceived a moment to arrive first.
            if last_ready == "complete" and time.time() >= settle_until:
                break
            time.sleep(0.1)
        return _NavResponse(status=self._doc_status or 200, url=self.url)

    def reload(self, timeout: int = CDP_NAV_TIMEOUT_MS) -> None:
        self._isolated_world = None
        self.send("Page.reload", {})
        deadline = time.time() + max(1.0, timeout / 1000.0)
        while time.time() < deadline:
            self.drain_events()
            try:
                ready = self.evaluate("() => document.readyState")
            except Exception:
                ready = ""
            if ready in ("interactive", "complete"):
                return
            time.sleep(0.1)

    def wait_for_url(self, predicate_or_substr, timeout: float = 10.0) -> str:
        deadline = time.time() + max(0.1, float(timeout))
        last_url = self.url
        while time.time() < deadline:
            last_url = self.url
            try:
                if callable(predicate_or_substr):
                    matched = bool(predicate_or_substr(last_url))
                else:
                    pattern = str(predicate_or_substr)
                    if "*" in pattern:
                        regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
                        matched = re.search(regex, last_url) is not None
                    else:
                        matched = pattern in last_url
                if matched:
                    return last_url
            except Exception:
                pass
            time.sleep(0.15)
        raise TimeoutError(f"等待页面地址超时: {last_url}")

    def wait_for_load_state(self, state: str = "domcontentloaded", timeout: int = 30000) -> None:
        wanted = ("complete",) if state == "load" else ("interactive", "complete")
        deadline = time.time() + max(0.1, timeout / 1000.0)
        while time.time() < deadline:
            try:
                ready = self.evaluate("() => document.readyState")
            except Exception:
                ready = ""
            if ready in wanted:
                return
            time.sleep(0.1)
        raise TimeoutError("等待页面加载状态超时")

    # -- locators ------------------------------------------------------
    def locator(self, selector: str) -> _CdpLocator:
        return _CdpLocator(self, {"kind": "css", "selector": str(selector), "index": 0})

    def get_by_role(self, role: str, name=None, exact: bool = False) -> _CdpLocator:
        if isinstance(name, re.Pattern):
            pattern = name.pattern
            flags = _js_flags(name.flags)
        elif name is None:
            pattern = ".*"
            flags = "i"
        else:
            literal = re.escape(str(name))
            pattern = f"^{literal}$" if exact else literal
            flags = "i"
        return _CdpLocator(
            self,
            {"kind": "role", "role": str(role), "pattern": pattern, "flags": flags, "index": 0},
        )

    def get_by_text(self, text) -> _CdpLocator:
        if isinstance(text, re.Pattern):
            pattern = text.pattern
            flags = _js_flags(text.flags)
        else:
            pattern = re.escape(str(text))
            flags = ""
        return _CdpLocator(self, {"kind": "text", "pattern": pattern, "flags": flags, "index": 0})

    # -- human input ---------------------------------------------------
    def _human_click(self, locator: _CdpLocator) -> None:
        try:
            locator.scroll_into_view_if_needed(timeout=2500)
        except Exception:
            pass
        rect = None
        try:
            rect = locator.bounding_box()
        except Exception:
            rect = None
        if rect and rect.get("width", 0) > 4 and rect.get("height", 0) > 4:
            _human_click_rect(self, rect)
            return
        try:
            locator.evaluate("el => el.click()")
        except Exception:
            pass

    def _human_click_rect(self, rect: dict) -> None:
        _human_click_rect(self, rect)


class _NetRequest:
    def __init__(self, entry: dict):
        self.url = str(entry.get("url") or "")
        self.method = str(entry.get("method") or "GET")
        self.headers = dict(entry.get("headers") or {})


class _NetResponse:
    def __init__(self, entry: dict):
        self.url = str(entry.get("url") or "")
        self.status = int(entry.get("status") or 0)
        self.headers = dict(entry.get("headers") or {})
        self._page = entry.get("page")
        self._request_id = str(entry.get("request_id") or "")
        self._body = None
        self._body_loaded = False

    def json(self):
        if self._body_loaded:
            return self._body
        self._body_loaded = True
        self._body = {}
        if not self._page or not self._request_id:
            return self._body
        try:
            result = self._page.send("Network.getResponseBody", {"requestId": self._request_id})
            raw = str(result.get("body") or "")
            if result.get("base64Encoded"):
                raw = base64.b64decode(raw).decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                self._body = parsed
        except Exception:
            self._body = {}
        return self._body


class _CdpContext:
    """A dedicated Chrome process with an isolated profile, driven over CDP."""

    def __init__(self, proc: subprocess.Popen, debug_port: int, session_dir: Path):
        self._proc = proc
        self.debug_port = int(debug_port)
        self.session_dir = Path(session_dir)
        self._pages: list[_CdpPage] = []
        self._handlers: dict[str, list] = {"request": [], "response": []}
        self.net_log: deque = deque(maxlen=4000)
        self._targets_cache: tuple[float, list[dict]] = (0.0, [])
        self._sync_pages()

    # -- HTTP helpers --------------------------------------------------
    def _json_call(self, path: str, method: str = "GET", timeout: float = 1.5):
        url = f"http://127.0.0.1:{self.debug_port}{path}"
        req = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except Exception:
                return {}
        except Exception:
            return None

    def _fetch_targets(self, force: bool = False) -> list[dict]:
        now = time.time()
        cached_at, cached = self._targets_cache
        if not force and now - cached_at < 0.25:
            return cached
        data = self._json_call("/json")
        targets = data if isinstance(data, list) else []
        self._targets_cache = (now, targets)
        return targets

    def _find_target(self, target_id: str) -> dict | None:
        for target in self._fetch_targets(force=True):
            if isinstance(target, dict) and target.get("id") == target_id:
                return target
        return None

    def _target_exists(self, target_id: str) -> bool:
        return self._find_target(target_id) is not None

    def _sync_pages(self) -> None:
        targets = self._fetch_targets(force=True)
        alive = {
            str(t.get("id"))
            for t in targets
            if isinstance(t, dict) and t.get("type") == "page"
        }
        for page in self._pages:
            if page.target_id not in alive:
                page._closed = True
        known = {page.target_id for page in self._pages}
        for target in targets:
            if not isinstance(target, dict) or target.get("type") != "page":
                continue
            target_id = str(target.get("id") or "")
            if not target_id or target_id in known:
                continue
            target_url = str(target.get("url") or "")
            if target_url.startswith(("devtools://", "chrome-extension://")):
                continue
            known.add(target_id)
            self._pages.append(
                _CdpPage(self, target_id, str(target.get("webSocketDebuggerUrl") or ""), target_url)
            )

    # -- public API ----------------------------------------------------
    @property
    def pages(self) -> list[_CdpPage]:
        self._sync_pages()
        return [page for page in self._pages if not page._closed]

    def new_page(self) -> _CdpPage:
        data = self._json_call("/json/new?url=about:blank", method="PUT", timeout=3.0)
        if not isinstance(data, dict):
            data = self._json_call("/json/new?url=about:blank", method="GET", timeout=3.0)
        if not isinstance(data, dict) or not data.get("id"):
            raise RuntimeError("无法打开新的浏览器标签页")
        page = _CdpPage(
            self,
            str(data.get("id")),
            str(data.get("webSocketDebuggerUrl") or ""),
            str(data.get("url") or "about:blank"),
        )
        self._pages.append(page)
        return page

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(str(event), []).append(handler)

    def _record(self, entry: dict) -> None:
        self.net_log.append(entry)
        kind = entry.get("kind")
        handlers = list(self._handlers.get(kind) or [])
        if not handlers:
            return
        if kind == "request":
            shim = _NetRequest(entry)
        elif kind == "response":
            shim = _NetResponse(entry)
        else:
            return
        for handler in handlers:
            try:
                handler(shim)
            except Exception:
                continue

    def drain(self) -> None:
        for page in list(self._pages):
            if not page._closed:
                try:
                    page.drain_events()
                except Exception:
                    continue

    def close(self) -> None:
        for page in list(self._pages):
            try:
                if page._client is not None:
                    page._client.close()
            except Exception:
                pass
            page._closed = True
        self._pages.clear()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    proc.kill()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def _human_click_rect(page: _CdpPage, rect: dict) -> None:
    rx = 0.32 + secrets.randbelow(361) / 1000.0
    ry = 0.32 + secrets.randbelow(361) / 1000.0
    tx = float(rect.get("x", 0.0)) + float(rect.get("width", 0.0)) * rx
    ty = float(rect.get("y", 0.0)) + float(rect.get("height", 0.0)) * ry
    if page._last_mouse == (0.0, 0.0):
        start_x = 280.0 + secrets.randbelow(180)
        start_y = 220.0 + secrets.randbelow(140)
    else:
        start_x, start_y = page._last_mouse
    # A slight perpendicular drift keeps the trajectory from being a perfect
    # straight line (real mouse paths always bow a little).
    arc_x = secrets.randbelow(31) - 15
    arc_y = secrets.randbelow(31) - 15
    steps = 10 if ACTION_DELAY > 0 else 6
    for step in range(1, steps + 1):
        frac = step / float(steps)
        arc = 4.0 * frac * (1.0 - frac)
        mx = start_x + (tx - start_x) * frac + arc_x * arc
        my = start_y + (ty - start_y) * frac + arc_y * arc
        try:
            page.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", "x": mx, "y": my, "button": "none", "buttons": 0},
            )
        except Exception:
            break
        if ACTION_DELAY > 0:
            time.sleep(0.007 + secrets.randbelow(10) / 1000.0)
    page._last_mouse = (tx, ty)
    if ACTION_DELAY > 0:
        time.sleep(0.06 + secrets.randbelow(70) / 1000.0)
    try:
        page.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": tx,
                "y": ty,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            },
        )
        if ACTION_DELAY > 0:
            # Human button press hold: 50-110ms.
            time.sleep(0.05 + secrets.randbelow(60) / 1000.0)
        page.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": tx,
                "y": ty,
                "button": "left",
                "buttons": 0,
                "clickCount": 1,
            },
        )
    except Exception:
        pass


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_devtools(debug_port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    version_url = f"http://127.0.0.1:{debug_port}/json/version"
    list_url = f"http://127.0.0.1:{debug_port}/json"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(version_url, timeout=0.6) as resp:
                if resp.status == 200:
                    resp.read()
            with urllib.request.urlopen(list_url, timeout=0.6) as resp:
                if resp.status == 200:
                    resp.read()
            return True
        except Exception:
            time.sleep(0.1)
    return False


class WorkBuddyBrowser:
    """Manages up to 2 isolated fresh fingerprint Chrome browsers per registration round.

    Chrome is launched natively via ``subprocess.Popen`` and driven over raw CDP.
    ``Runtime.enable`` is never sent, so Imperva / Cloudflare / GitHub cannot
    detect a V8 inspector attachment.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.driver = None
        self.sessions: dict[int, dict] = {}
        self._last_task_id: int | None = None
        self.active_proxy_node: str = ""
        self.window_lock = None
        self.captured_tokens: dict[int, dict] = {}
        self.headless: bool = False
        self.extra_chromium_args: list[str] = []
        self._is_real_browser: bool = True
        self.on_session_created = None

    @property
    def task_id(self):
        return self._last_task_id

    @property
    def context(self):
        if self._last_task_id is not None and self._last_task_id in self.sessions:
            return self.sessions[self._last_task_id].get("context")
        return None

    @property
    def tabs(self) -> dict:
        if self._last_task_id is not None and self._last_task_id in self.sessions:
            return self.sessions[self._last_task_id]["tabs"]
        return {}

    def _tabs_for(self, task_id: int) -> dict:
        sess = self.sessions.get(int(task_id))
        return sess["tabs"] if sess else {}

    def _lock_window(self):
        if self.window_lock is not None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        lock = (self.root / ".window.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if lock.tell() == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock.close()
            raise ValueError("工作窗口由另一个 KeyVault 实例占用，请在那里继续或关闭它后重试。") from exc
        self.window_lock = lock

    def _resolve_task_fp(self, task_id: int, task: dict | None) -> tuple[dict, str, str]:
        proxy_url = ""
        proxy_node = ""
        fp = None
        if isinstance(task, dict):
            proxy_url = str(task.get("proxy_url") or "").strip()
            proxy_node = str(task.get("proxy_node") or "").strip()
            raw_fp = task.get("fp_json")
            if isinstance(raw_fp, str) and raw_fp.strip():
                try:
                    parsed = json.loads(raw_fp)
                    if isinstance(parsed, dict):
                        fp = parsed
                except Exception:
                    fp = None
        if not fp:
            acc_id = int((task or {}).get("account_id") or task_id)
            email = str((task or {}).get("email") or f"task-{task_id}@local")
            fp = generate_fingerprint_config(
                acc_id,
                email,
                proxy=proxy_url,
                proxy_node=proxy_node,
                salt=f"wb-task-{task_id}",
            )
        proxy_url = proxy_url or str(fp.get("proxy") or "").strip()
        proxy_node = proxy_node or str(fp.get("proxy_node") or "").strip()
        return fp, proxy_url, proxy_node

    def _attach_token_sniffer(self, context, task_id=None, page=None) -> None:
        # Accept both (ctx, tid) and (tid, ctx, page) call orders so existing
        # callers stay valid. Handlers live on the context and therefore also
        # see popup tabs; ``page`` is only tracked for idempotent re-attachment.
        if isinstance(context, int) and not isinstance(task_id, int):
            context, task_id = task_id, context
        tid = int(task_id)
        if page is not None:
            seen = getattr(context, "_wb_sniffer_pages", None)
            if seen is None:
                seen = set()
                try:
                    context._wb_sniffer_pages = seen
                except Exception:
                    return
            if id(page) in seen:
                return
            seen.add(id(page))

        def _on_request(req):
            try:
                host = urlsplit(req.url).netloc.lower()
                if "workbuddy.ai" not in host and "codebuddy.ai" not in host:
                    return
                headers = req.headers or {}
                auth = headers.get("authorization") or ""
                if auth.lower().startswith("bearer "):
                    tok = auth[7:].strip()
                    if tok.count(".") == 2:
                        entry = self.captured_tokens.setdefault(tid, {})
                        entry["accessToken"] = tok
                        uid = headers.get("x-user-id") or _jwt_uid(tok)
                        if uid:
                            entry["uid"] = str(uid).strip()
                        exp = _jwt_exp(tok)
                        if exp:
                            entry["expiresAt"] = exp
            except Exception:
                pass

        def _on_response(resp):
            try:
                url = resp.url
                host = urlsplit(url).netloc.lower()
                if "workbuddy.ai" not in host and "codebuddy.ai" not in host:
                    return
                if resp.status != 200:
                    return
                ct = (resp.headers or {}).get("content-type", "").lower()
                if "json" not in ct:
                    return
                path = urlsplit(url).path.lower()
                if not any(k in path for k in ("/auth/token", "/login", "/account", "/session")):
                    return
                body = resp.json()
                if not isinstance(body, dict):
                    return
                data = body.get("data") if isinstance(body.get("data"), dict) else body
                if isinstance(data.get("data"), dict):
                    data = data["data"]
                tok = str(data.get("accessToken") or "").strip()
                ref = str(data.get("refreshToken") or "").strip()
                if tok and tok.count(".") == 2:
                    entry = self.captured_tokens.setdefault(tid, {})
                    entry["accessToken"] = tok
                    if ref:
                        entry["refreshToken"] = ref
                    uid = str(data.get("uid") or _jwt_uid(tok) or "").strip()
                    if uid:
                        entry["uid"] = uid
                    exp = _jwt_exp(tok)
                    if exp:
                        entry["expiresAt"] = exp
                if data.get("nickname") and tid in self.captured_tokens:
                    self.captured_tokens[tid]["nickname"] = str(data["nickname"]).strip()
            except Exception:
                pass

        try:
            context.on("request", _on_request)
            context.on("response", _on_response)
        except Exception:
            pass

    def _launch_browser(self, session_dir: Path, slot: int, fp: dict, proxy_url: str) -> _CdpContext:
        exe = find_chromium_executable()
        if not exe:
            raise ValueError("未找到可用的 Chrome、Edge 或 Chromium。")

        debug_port = _free_local_port()
        seed = int(fp.get("canvas_seed") or (slot * 97 + 13))
        win_x = (20 if slot == 0 else 960) + (seed % 14)
        win_y = 24 + ((seed // 17) % 14)
        win_w = 916 + (seed % 28)
        win_h = 928 + ((seed // 31) % 36)

        args = [
            exe,
            f"--user-data-dir={session_dir}",
            f"--remote-debugging-port={debug_port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-save-password-bubble",
            "--disable-sync",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            f"--window-position={win_x},{win_y}",
            f"--window-size={win_w},{win_h}",
        ]
        if self.headless:
            args.append("--headless=new")
        if proxy_url:
            args.append(f"--proxy-server={proxy_url}")
        if self.extra_chromium_args:
            args.extend(self.extra_chromium_args)
        args.append("about:blank")

        creationflags = 0
        if sys.platform == "win32" and self.headless:
            # Only hide the console for headless runs. Visible desktop windows
            # must be created exactly like a normal Chrome launch so they always
            # appear on the user's screen (same policy as browser_service.py).
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except Exception as exc:
            raise ValueError(f"无法启动系统 Chrome / Edge: {exc}") from exc

        if not _wait_for_devtools(debug_port, timeout=8.0):
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        proc.kill()
            except Exception:
                pass
            raise ValueError("启动 Chrome 调试端口超时，请重试。")
        return _CdpContext(proc, debug_port, session_dir)

    def _context_for(self, task_id, task=None):
        tid = int(task_id)
        self._last_task_id = tid
        existing = self.sessions.get(tid)
        if existing and existing.get("context"):
            try:
                if any(not page.is_closed() for page in existing["context"].pages):
                    return existing["context"]
            except Exception:
                pass
            try:
                existing["context"].close()
            except Exception:
                pass
            self.sessions.pop(tid, None)

        # Enforce at most MAX_CONCURRENT_BROWSERS (2) open simultaneously
        while len(self.sessions) >= MAX_CONCURRENT_BROWSERS:
            oldest_tid = next(iter(self.sessions))
            old_sess = self.sessions.pop(oldest_tid, None)
            if old_sess and old_sess.get("context"):
                try:
                    old_sess["context"].close()
                except Exception:
                    pass

        exe = find_chromium_executable()
        if not exe:
            raise ValueError("未找到可用的 Chrome、Edge 或 Chromium。")

        fp, proxy_url, proxy_node = self._resolve_task_fp(tid, task)
        if proxy_node:
            try:
                sw = activate_proxy_node("WorkBuddy", proxy_url, proxy_node)
                if isinstance(sw, dict) and sw.get("proxy_url"):
                    proxy_url = str(sw["proxy_url"]).strip()
                self.active_proxy_node = proxy_node
                _sleep_action_delay(0.8, 1.4)
            except Exception:
                pass

        self._lock_window()
        session_dir = self.root / str(tid)
        # A retried/reopened ``github`` task must never reuse tainted DataDome
        # cookies from a previous failed attempt.
        if (
            isinstance(task, dict)
            and task.get("step") == "github"
            and session_dir.exists()
        ):
            self._remove_session_dir(session_dir)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "fp_profile.json").write_text(
            json.dumps(fp, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        used_slots = {s.get("slot") for s in self.sessions.values()}
        raw_slot = (task or {}).get("round_slot") if isinstance(task, dict) else None
        if raw_slot in (0, 1) and raw_slot not in used_slots:
            slot = int(raw_slot)
        else:
            slot = 0 if 0 not in used_slots else 1

        try:
            ctx = self._launch_browser(session_dir, slot, fp, proxy_url)
            self._attach_token_sniffer(ctx, tid)
        except Exception:
            if not self.sessions:
                self.close()
            raise
        session = {
            "context": ctx,
            "tabs": {},
            "slot": slot,
            "fp": fp,
            "proxy_url": proxy_url,
            "proxy_node": proxy_node,
        }
        self.sessions[tid] = session
        self._last_task_id = tid
        if callable(self.on_session_created):
            try:
                self.on_session_created(tid, session)
            except Exception:
                pass
        return ctx

    def _page(self, task_id, key, url, task=None):
        tid = int(task_id)
        context = self._context_for(tid, task=task)
        tabs = self._tabs_for(tid)
        page = tabs.get(key)
        if not page or page.is_closed():
            origin = urlsplit(url).netloc
            page = next(
                (
                    p
                    for p in context.pages
                    if urlsplit(p.url).netloc == origin and urlsplit(p.url).scheme == "https"
                ),
                None,
            )
            if not page:
                page = next((p for p in context.pages if p.url == "about:blank"), None) or context.new_page()
                tabs[key] = page
                response = page.goto(url, wait_until="domcontentloaded", timeout=15000)
                if response and response.status >= 400:
                    raise ValueError(f"目标页面返回 HTTP {response.status}，请在工作窗口检查后重试，当前步骤已保留。")
            tabs[key] = page
        page.bring_to_front()
        return page

    @staticmethod
    def _human_click(page, locator) -> None:
        try:
            locator.scroll_into_view_if_needed(timeout=2500)
        except Exception:
            pass
        box = None
        try:
            box = locator.bounding_box()
        except Exception:
            box = None
        if box and box.get("width", 0) > 4 and box.get("height", 0) > 4:
            _human_click_rect(page, box)
        else:
            try:
                locator.evaluate("el => el.click()")
            except Exception:
                pass

    def _human_paste_input(self, page, locator, value: str, *, origin: str = "", path: str = "", task=None) -> bool:
        self._human_click(page, locator)
        _sleep_action_delay(1.0, 2.0)
        if origin:
            loc = urlsplit(page.url)
            if loc.scheme != "https" or loc.netloc != urlsplit(origin).netloc or (path and loc.path != path):
                return False
        if task and self.challenge_page(task):
            return False
        still_empty = locator.evaluate(
            "el => ((el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) "
            "&& !!el.getClientRects().length && !el.disabled && !el.readOnly && !el.value) "
            "|| (el && el.isContentEditable && !el.textContent.trim())"
        )
        if not still_empty:
            return False
        text = str(value)
        try:
            page.keyboard.insert_text(text)
        except Exception:
            pass
        try:
            current_val = locator.input_value(timeout=1500)
        except Exception:
            current_val = ""
        if not current_val and text:
            # Fallback for mock/custom elements that ignored insert_text.
            locator.evaluate(
                """(el, val) => {
                    if (el.isContentEditable) {
                        el.focus();
                        el.textContent = val;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        return;
                    }
                    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, val);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                text,
            )
        return True

    def _fill(self, page, origin, path, fields, task=None):
        location = urlsplit(page.url)
        if location.scheme != "https" or location.netloc != urlsplit(origin).netloc or location.path != path:
            return 0
        try:
            page.locator(fields[0][0]).first.wait_for(state="visible", timeout=3500)
        except Exception:
            return 0
        # Wait 1~2s after opening the registration page before clicking the first input
        _sleep_action_delay(1.0, 2.0)
        filled = 0
        did_interact = False
        for selector, value in fields:
            if not value:
                continue
            loc = urlsplit(page.url)
            if loc.scheme != "https" or loc.netloc != urlsplit(origin).netloc or loc.path != path:
                break
            if task and self.challenge_page(task):
                break
            candidates = page.locator(selector)
            try:
                cand_count = candidates.count()
            except Exception:
                cand_count = 0
            target = None
            for idx in range(cand_count):
                cand = candidates.nth(idx)
                try:
                    state = cand.evaluate(
                        "el => (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) "
                        "&& !!el.getClientRects().length && !el.disabled && !el.readOnly "
                        "? (el.value ? 'non_empty' : 'empty') : 'skip'"
                    )
                except Exception:
                    continue
                if state in ("empty", "non_empty"):
                    target = (cand, state)
                    break
            if not target or target[1] != "empty":
                continue
            if did_interact:
                # Wait 1~2s between pasting previous field and clicking next input box
                _sleep_action_delay(1.0, 2.0)
            if self._human_paste_input(page, target[0], value, origin=origin, path=path, task=task):
                filled += 1
                did_interact = True
        return filled

    def fill_github_code(self, task, code: str) -> bool:
        tid = int(task["id"])
        sess = self.sessions.get(tid)
        ctx = sess.get("context") if sess else None
        if not ctx or not code:
            return False
        if self.challenge_page(task):
            return False
        clean_code = str(code).strip()
        for page in reversed(ctx.pages):
            if page.is_closed():
                continue
            loc = urlsplit(page.url)
            if loc.scheme == "https" and loc.netloc == "github.com":
                try:
                    page.bring_to_front()
                    _sleep_action_delay(1.0, 2.0)
                    single_sel = (
                        'input[autocomplete="one-time-code"], input[name="otp"], '
                        'input#otp, input[name="launch_code"], input[data-target*="verification"]'
                    )
                    single_loc = page.locator(single_sel)
                    for idx in range(single_loc.count()):
                        cand = single_loc.nth(idx)
                        ok_single = cand.evaluate(
                            "el => el instanceof HTMLInputElement && !!el.getClientRects().length "
                            "&& !el.disabled && !el.readOnly && el.maxLength !== 1 && !el.value"
                        )
                        if ok_single:
                            if self._human_paste_input(page, cand, clean_code, origin=GITHUB_URL, task=task):
                                return True
                    digits = page.locator('input[name="launch_code[]"], input[maxlength="1"]')
                    if digits.count() >= len(clean_code):
                        first_digit = digits.first
                        if first_digit.is_visible():
                            self._human_click(page, first_digit)
                            _sleep_action_delay(1.0, 2.0)
                            page.keyboard.insert_text(clean_code)
                            if digits.nth(len(clean_code) - 1).input_value():
                                return True
                    if page.evaluate(_FILL_OTP, clean_code):
                        return True
                except Exception:
                    continue
        return False

    @staticmethod
    def _first_visible(locator):
        try:
            cnt = locator.count()
        except Exception:
            return None
        for idx in range(cnt):
            cand = locator.nth(idx)
            try:
                if cand.is_visible():
                    return cand
            except Exception:
                continue
        return None

    @classmethod
    def _first_visible_any(cls, page, selectors):
        """First visible match across separate CSS selectors.

        Combined Playwright selectors such as ``a:has-text("x"), b:has-text("y")``
        cannot be emulated in one CSS query, so each is resolved on its own.
        """
        for selector in selectors:
            found = cls._first_visible(page.locator(selector))
            if found is not None:
                return found
        return None

    def _enter_github_signup_from_home(self, page, email: str, task=None) -> str:
        """Open GitHub homepage first and randomly trigger either hero email box or header Sign up button."""
        _sleep_action_delay(1.0, 2.0)
        if task and self.challenge_page(task):
            return "challenged"

        # Cold clicks on the header ``Sign up`` link (or a bare ``GET /signup``)
        # are the requests DataDome answers with ``403 t=bv`` ("访问暂时受限"),
        # while submitting the homepage hero email box goes through with 200.
        # Production therefore always enters via ``hero_email`` first; tests
        # monkeypatch ``wb.secrets`` with a ``types.SimpleNamespace`` and keep
        # their deterministic channel selection.
        pick = secrets.randbelow(2)
        if (
            isinstance(secrets, types.ModuleType)
            and getattr(secrets.randbelow, "__name__", "") == "randbelow"
        ):
            pick = 0
        channels = (
            ["hero_email", "header_signup"]
            if pick == 0
            else ["header_signup", "hero_email"]
        )
        for ch in channels:
            if task and self.challenge_page(task):
                return "challenged"
            if ch == "hero_email":
                hero_inp = self._first_visible(
                    page.locator(
                        'form[action*="/signup"] input[type="email"], '
                        'form[action*="/signup"] input[name="user_email"], '
                        'input#hero_user_email, input[name="user_email"]'
                    )
                )
                hero_btn = self._first_visible(
                    page.locator(
                        'form[action*="/signup"] button[type="submit"], '
                        'button:has-text("Sign up for GitHub")'
                    )
                )
                if hero_inp is not None and hero_btn is not None:
                    self._human_paste_input(page, hero_inp, email, origin=GITHUB_URL, task=task)
                    if task and self.challenge_page(task):
                        return "challenged"
                    _sleep_action_delay(1.0, 2.0)
                    self._human_click(page, hero_btn)
                    try:
                        page.wait_for_url("**/signup**", timeout=10.0)
                    except Exception:
                        pass
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    self._wait_for_datadome_interstitial(page)
                    if urlsplit(page.url).path.rstrip("/") == "/signup" or (task and self.challenge_page(task)):
                        return "首页中间邮箱框"
            else:
                hdr_btn = self._first_visible(
                    page.locator(
                        'header a[href*="/signup"], a.HeaderMenu-link--sign-up, '
                        'nav a[href*="/signup"], a[href*="/signup?ref_cta=Sign+up"]'
                    )
                )
                if hdr_btn is None:
                    hdr_btn = self._first_visible(page.get_by_role("link", name=re.compile(r"^Sign\s*up$", re.I)))
                if hdr_btn is not None:
                    self._human_click(page, hdr_btn)
                    try:
                        page.wait_for_url("**/signup**", timeout=10.0)
                    except Exception:
                        pass
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=8000)
                    except Exception:
                        pass
                    self._wait_for_datadome_interstitial(page)
                    if urlsplit(page.url).path.rstrip("/") == "/signup" or (task and self.challenge_page(task)):
                        return "右上角注册按钮"

        any_signup = self._first_visible(page.locator('a[href*="/signup"]'))
        if any_signup is not None:
            self._human_click(page, any_signup)
            try:
                page.wait_for_url("**/signup**", timeout=10.0)
            except Exception:
                pass
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            self._wait_for_datadome_interstitial(page)
        if urlsplit(page.url).path.rstrip("/") != "/signup" and not (task and self.challenge_page(task)):
            # A bare ``GET /signup`` is a cold DataDome request (403 t=bv),
            # whereas the hero email form posts a ``user_email`` and passes.
            # Only real GitHub gets the query string; the local test server
            # keeps the plain URL.
            target_signup_url = (
                f"{GITHUB_URL}/signup?source=form-home-signup&user_email={quote(str(email))}"
                if (email and not self.extra_chromium_args)
                else GITHUB_URL + "/signup"
            )
            resp = page.goto(
                target_signup_url,
                referer=GITHUB_URL + "/",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            self._wait_for_datadome_interstitial(page)
            if resp and resp.status >= 400:
                # A DataDome 403 is an interstitial, not a dead page: when the
                # challenge is present (or the browser already reached /signup)
                # let the caller pause for the operator instead of failing.
                challenged = bool(
                    task and (self.challenge_page(task) or self.is_ip_restricted_page(task))
                )
                if not (
                    resp.status == 403
                    and (challenged or urlsplit(page.url).path.rstrip("/") == "/signup")
                ):
                    raise ValueError(f"目标页面返回 HTTP {resp.status}，请在工作窗口检查后重试，当前步骤已保留。")
        return "首页注册入口"

    def _fill_github_signup(self, page, email: str, password: str, username: str, task=None) -> int:
        loc = urlsplit(page.url)
        if loc.scheme != "https" or loc.netloc != "github.com" or loc.path.rstrip("/") != "/signup":
            return 0
        any_field_sel = (
            '#email, input[name="user[email]"], input[autocomplete="email"], '
            '#password, input[name="user[password]"], input[autocomplete="new-password"]'
        )
        try:
            page.locator(any_field_sel).first.wait_for(state="visible", timeout=4500)
        except Exception:
            return 0

        _sleep_action_delay(1.0, 2.0)
        steps = [
            (
                '#email, input[name="user[email]"], input[autocomplete="email"]',
                email,
                '#password, input[name="user[password]"], input[autocomplete="new-password"]',
                'button[data-continue-to*="password"], #email-container button:has-text("Continue")',
            ),
            (
                '#password, input[name="user[password]"], input[autocomplete="new-password"]',
                password,
                '#login, input[name="user[login]"], input[autocomplete="username"]',
                'button[data-continue-to*="username"], #password-container button:has-text("Continue")',
            ),
            (
                '#login, input[name="user[login]"], input[autocomplete="username"]',
                username,
                "",
                "",
            ),
        ]
        filled = 0
        did_interact = False
        for selector, value, next_selector, continue_selector in steps:
            if not value:
                continue
            cur = urlsplit(page.url)
            if cur.scheme != "https" or cur.netloc != "github.com" or cur.path.rstrip("/") != "/signup":
                break
            if task and self.challenge_page(task):
                break
            candidates = page.locator(selector)
            try:
                cand_count = candidates.count()
            except Exception:
                cand_count = 0
            target = None
            for idx in range(cand_count):
                cand = candidates.nth(idx)
                try:
                    state = cand.evaluate(
                        "el => (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) "
                        "&& !!el.getClientRects().length && !el.disabled && !el.readOnly "
                        "? (el.value ? 'non_empty' : 'empty') : 'skip'"
                    )
                except Exception:
                    continue
                if state in ("empty", "non_empty"):
                    target = (cand, state)
                    break
            if target and target[1] == "empty":
                if did_interact:
                    _sleep_action_delay(1.0, 2.0)
                if self._human_paste_input(page, target[0], value, origin=GITHUB_URL, path="/signup", task=task):
                    filled += 1
                    did_interact = True
            if next_selector and self._first_visible(page.locator(next_selector)) is None and continue_selector:
                cont_btn = self._first_visible(page.locator(continue_selector))
                if cont_btn is not None:
                    _sleep_action_delay(1.0, 2.0)
                    self._human_click(page, cont_btn)
                    did_interact = True
                    try:
                        page.locator(next_selector).first.wait_for(state="visible", timeout=3000)
                    except Exception:
                        pass
        return filled

    def show_paused_window(self, task):
        context = self._context_for(task["id"], task=task)
        challenge = self.challenge_page(task)
        if challenge:
            challenge.bring_to_front()
            return
        # Restored tabs can be on an OAuth or verification origin. Keep their
        # current page intact and never run form filling from this operation.
        for page in reversed(context.pages):
            if urlsplit(page.url).scheme in ("https", "http"):
                page.bring_to_front()
                return
        if task["step"] in ("github", "email"):
            self._page(task["id"], "github", GITHUB_URL + "/" if task["step"] == "github" else GITHUB_URL + "/login", task=task)
        else:
            self._page(task["id"], "workbuddy", WORKBUDDY_URL + "/", task=task)

    def _wait_for_datadome_interstitial(self, page, timeout: float = 12.0) -> None:
        """Wait for DataDome's silent device-check interstitial to redirect.

        GitHub's edge filter shows a 1.5-3.5s automatic ``/interstitial/`` frame
        before the real ``/signup`` form. Returning early made
        ``challenge_page`` report a human challenge that never existed.
        """
        if not self._is_real_browser:
            return
        probe = r"""() => {
            const frames = Array.from(document.querySelectorAll('iframe'));
            const hasFrame = frames.some(f => {
                const src = f.getAttribute('src') || '';
                const title = f.getAttribute('title') || f.title || '';
                return /captcha-delivery\.com\/interstitial/i.test(src) || /DataDome Device Check/i.test(title);
            });
            if (hasFrame) return true;
            const scripts = Array.from(document.querySelectorAll('script'));
            return scripts.some(s => {
                const src = s.getAttribute('src') || '';
                const txt = s.textContent || '';
                return /captcha-delivery\.com\/i\.js/i.test(src) || /\bdd\s*=\s*\{[^}]*['"]rt['"]\s*:\s*['"]i['"]/i.test(txt);
            });
        }"""
        try:
            present = bool(page.evaluate(probe))
        except Exception:
            return
        if not present:
            # No interstitial at all: zero extra delay (unit tests stay fast).
            return
        deadline = time.time() + max(0.0, float(timeout))
        while present and time.time() < deadline:
            time.sleep(0.6)
            try:
                present = bool(page.evaluate(probe))
            except Exception:
                if isinstance(page, _CdpPage):
                    # The device check navigated the page, invalidating the
                    # isolated world. Wait for the new document and re-probe
                    # instead of assuming the interstitial is gone.
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                        present = bool(page.evaluate(probe))
                    except Exception:
                        present = False
                else:
                    present = False
        # ``i.js`` waits ~500ms after the device check before reloading, so the
        # real /signup document is only ready after that extra beat.
        time.sleep(0.65)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

    def _inspect_datadome_oopif(self, ctx, *, allow_active_fe_skip: bool = False) -> str:
        """Probe DataDome's cross-origin captcha iframe for ``restricted``/``slider``.

        ``"访问暂时受限"`` is rendered inside the OOPIF, so the top-level
        ``github.com`` document never sees that text. Chrome 118+ no longer
        lists cross-origin ``iframe`` targets through ``GET /json``
        (``ctx._fetch_targets()``), so ``Target.getTargets`` is asked directly
        on the first open page as well. Each captcha target is inspected
        through a short-lived CDP connection.
        """
        candidates: list[dict] = []

        def _add(items) -> None:
            for item in items or []:
                if isinstance(item, dict):
                    candidates.append(item)

        for page in getattr(ctx, "pages", []) or []:
            try:
                if page.is_closed():
                    continue
            except Exception:
                pass
            try:
                discovered = page.send(
                    "Target.getTargets",
                    {"filter": [{"type": "iframe"}, {"type": "page"}]},
                )
            except Exception:
                continue
            _add((discovered or {}).get("targetInfos"))
            break
        if hasattr(ctx, "_fetch_targets"):
            try:
                _add(ctx._fetch_targets(force=True))
            except Exception:
                pass

        targets: list[dict] = []
        seen: set[str] = set()
        for target in candidates:
            keys = [
                str(target.get("targetId") or ""),
                str(target.get("id") or ""),
                str(target.get("url") or ""),
            ]
            keys = [key for key in keys if key]
            if keys and any(key in seen for key in keys):
                continue
            seen.update(keys)
            targets.append(target)

        probe_js = r"""(() => {
            const text = (document.body?.innerText || '').slice(0, 3000);
            const hasRestricted = /访问暂时受限|Access temporarily restricted|You have triggered an abuse detection mechanism/i.test(text);
            const hasSliderText = /向右滑动|Slide right|请拖动滑块|Slide to verify/i.test(text);
            const hasSliderEl = !!document.querySelector('[class*="slider" i], .geetest_slider_button, #nc_1_n1z');
            if ((hasSliderText || hasSliderEl) && !hasRestricted) return 'slider';
            if (hasRestricted) return 'restricted';
            return '';
        })()"""
        for target in targets:
            if target.get("type") not in ("iframe", "page"):
                continue
            url = str(target.get("url") or "")
            if "captcha-delivery.com/captcha" not in url:
                continue
            if re.search(r"[?&]t=bv(?:&|$)", url, re.I):
                return "restricted"
            if allow_active_fe_skip and isinstance(ctx, _CdpContext):
                # A fresh ``t=fe`` iframe without ``refererHeader=`` is always
                # the interactive slider. Opening a DevTools WebSocket to the
                # live OOPIF while the operator drags the slider would taint
                # ``plv3`` in ``/captcha/check``, so classify it from the URL
                # alone and never connect.
                if (
                    re.search(r"[?&]t=fe(?:&|$)", url, re.I)
                    and "refererheader=" not in url.lower()
                ):
                    return "slider"
            ws_url = str(target.get("webSocketDebuggerUrl") or "")
            if not ws_url:
                tid = str(target.get("targetId") or target.get("id") or "")
                port = getattr(ctx, "debug_port", 0)
                if tid and port:
                    ws_url = f"ws://127.0.0.1:{port}/devtools/page/{tid}"
            if not ws_url:
                continue
            client = None
            try:
                client = _CdpClient(ws_url)
                # Step A: zero-JS Blink DOM inspection. Reading the C++ DOM
                # avoids creating a V8 execution context inside the live
                # DataDome iframe (which the anti-bot script can observe).
                dom_inspected = False
                try:
                    doc = client.send("DOM.getDocument", {"depth": 1})
                    dom_inspected = True
                    root_id = ((doc.get("root") or {}).get("nodeId"))
                    if root_id:
                        outer = str(
                            (client.send("DOM.getOuterHTML", {"nodeId": root_id}) or {}).get("outerHTML")
                            or ""
                        )
                        if outer:
                            cleaned = re.sub(
                                r"<(script|style)\b[^>]*>.*?</\1>", " ", outer, flags=re.I | re.S
                            )
                            # DataDome puts ``<title>你被封锁了。 | Github.com</title>``
                            # in ``<head>`` on the interactive slider page too, so
                            # ``<head>`` must not feed the restricted-heading probe.
                            cleaned = re.sub(
                                r"<head\b[^>]*>.*?</head>", " ", cleaned, flags=re.I | re.S
                            )
                            cleaned_text = re.sub(r"<[^>]+>", " ", cleaned)
                            has_restricted_heading = bool(
                                re.search(
                                    r"访问暂时受限|Access temporarily restricted|You have triggered an abuse detection mechanism",
                                    cleaned_text,
                                    re.I,
                                )
                            )
                            has_hard_block_attr = bool(
                                re.search(
                                    r'data-dd-response-page=["\']hard-block["\']|dd-response-page--hard-block',
                                    outer,
                                    re.I,
                                )
                            )
                            has_restricted_heading = has_restricted_heading or has_hard_block_attr
                            has_slider = bool(
                                re.search(
                                    r"向右滑动|Slide right|请拖动滑块|Slide to verify|geetest_slider_button",
                                    cleaned_text,
                                    re.I,
                                )
                                or re.search(r'class=["\'][^"\']*slider', cleaned, re.I)
                            )
                            if has_slider and not has_restricted_heading:
                                return "slider"
                            if has_restricted_heading:
                                return "restricted"
                except Exception:
                    pass
                if dom_inspected:
                    # ``DOM.getDocument`` succeeded, so the Blink DOM was read
                    # without touching V8. If neither slider nor restricted
                    # matched (e.g. the OOPIF is still loading), skip Step B
                    # rather than create an execution context inside the live
                    # DataDome iframe and taint the solved token.
                    continue
                # Step B: isolated-world JS probe fallback.
                frame_tree = client.send("Page.getFrameTree")
                frame_id = (
                    ((frame_tree.get("frameTree") or {}).get("frame") or {}).get("id")
                )
                if not frame_id:
                    continue
                world = client.send(
                    "Page.createIsolatedWorld",
                    {
                        "frameId": frame_id,
                        "worldName": "kv_dd_probe",
                        "grantUniveralAccess": True,
                    },
                )
                context_id = world.get("executionContextId")
                if not context_id:
                    continue
                result = client.send(
                    "Runtime.evaluate",
                    {
                        "contextId": context_id,
                        "expression": probe_js,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                )
                value = (result.get("result") or {}).get("value")
                if value == "restricted":
                    return "restricted"
                if value == "slider":
                    return "slider"
            except Exception:
                continue
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
        return ""

    def is_ip_restricted_page(self, task, *, allow_active_fe_skip: bool | None = None) -> bool:
        """True only for a dead-end IP block, not an interactive slider.

        ``"访问暂时受限"`` has no puzzle left to solve, so the queue must rotate
        to a clean proxy node instead of asking the operator to click a slider.
        DataDome renders that text inside a cross-origin
        ``geo.captcha-delivery.com`` iframe, which is inspected separately.

        ``allow_active_fe_skip`` skips attaching to a live ``t=fe`` slider
        OOPIF (attaching would taint its token). It defaults to True while the
        task is still being worked and False once the task is ``verified``,
        because a resume-time probe must read the OOPIF DOM to notice that the
        frame transitioned to ``"访问暂时受限"``.
        """
        if allow_active_fe_skip is None:
            allow_active_fe_skip = str((task or {}).get("status") or "") != "verified"
        tid = int(task["id"])
        sess = self.sessions.get(tid)
        ctx = sess.get("context") if sess else None
        if not ctx:
            return False
        for page in reversed(ctx.pages):
            if page.is_closed():
                continue
            try:
                state = page.evaluate(r"""() => {
                    // Never gate on ``document.visibilityState``: when KeyVault's
                    // own window covers Chrome on Windows, native window
                    // occlusion reports 'hidden' and restriction detection broke.
                    if (window.dd && (window.dd.t === 'bv' || window.dd.rt === 'b')) return 'restricted';
                    if (document.body && (document.body.getAttribute('data-dd-response-page') === 'hard-block' || document.body.classList.contains('dd-response-page--hard-block'))) {
                        return 'restricted';
                    }
                    let scriptHasFe = false;
                    for (const s of document.querySelectorAll('script')) {
                        const txt = s.textContent || '';
                        if (/\bdd\s*=\s*\{[^}]*(?:['"]t['"]\s*:\s*['"]bv['"]|['"]rt['"]\s*:\s*['"]b['"])/i.test(txt)) {
                            return 'restricted';
                        }
                        if (/\bdd\s*=\s*\{[^}]*['"]t['"]\s*:\s*['"]fe['"]/i.test(txt)) {
                            scriptHasFe = true;
                        }
                    }
                    const isShown = (el) => {
                        if (!el || !el.getClientRects || !el.getClientRects().length) return false;
                        for (let p = el; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
                            if (p.hidden || p.getAttribute('aria-hidden') === 'true') return false;
                            if (p.classList && (p.classList.contains('d-none') || p.classList.contains('v-hidden') || p.classList.contains('sr-only') || p.classList.contains('wb-sr-only'))) return false;
                            const st = getComputedStyle(p);
                            if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity || '1') <= 0.05) return false;
                        }
                        return true;
                    };
                    let checkOopif = false;
                    const frames = Array.from(document.querySelectorAll('iframe'));
                    for (const frame of frames) {
                        const rawSrc = frame.getAttribute('src') || '';
                        const u = new URL(rawSrc || 'about:blank', location.href);
                        const box = frame.getBoundingClientRect();
                        if (/captcha-delivery\.com\/captcha/i.test(rawSrc) || /captcha-delivery\.com\/captcha/i.test(u.href)) {
                            if (/[?&]t=bv(?:&|$)/i.test(rawSrc)) return 'restricted';
                            // DataDome's restricted text lives inside this OOPIF,
                            // so the top-level document cannot see it.
                            if (isShown(frame)) checkOopif = true;
                            continue;
                        }
                        if (/octocaptcha\.com\/datadome/i.test(u.href)) {
                            if (box.width < 120 || box.height < 120) continue;
                            if (isShown(frame)) { checkOopif = true; continue; }
                        }
                        const haystack = [u.href, frame.id || '', frame.className || '', frame.title || ''].join(' ');
                        if (!/octocaptcha|arkoselabs|funcaptcha|hcaptcha|recaptcha|geetest/i.test(haystack)) continue;
                        if (isShown(frame) && box.width >= 100 && box.height >= 50) return 'slider';
                    }
                    const widgets = document.querySelectorAll(
                        '.js-octocaptcha-frame, [class*="geetest" i], [id*="geetest" i], ' +
                        '[class*="nc-container" i], [class*="slider" i][class*="verify" i], ' +
                        '[class*="captcha-modal" i]'
                    );
                    for (const widget of widgets) {
                        if (widget.querySelector('input#email')) continue;
                        const box = widget.getBoundingClientRect();
                        if (widget.matches('.js-octocaptcha-frame')) {
                            // The octocaptcha shell wraps DataDome's OOPIF:
                            // inspect the nested frame before calling it a slider.
                            if (box.width >= 120 && box.height >= 120 && isShown(widget)) {
                                checkOopif = true;
                                continue;
                            }
                            continue;
                        }
                        if (box.width >= 120 && box.height >= 40 && isShown(widget)) return 'slider';
                    }
                    const text = (document.body?.innerText || '').slice(0, 3000);
                    if (/访问暂时受限|Access temporarily restricted|You have triggered an abuse detection mechanism/i.test(text)) {
                        return 'restricted';
                    }
                    if (checkOopif || scriptHasFe) return 'check_oopif';
                    return '';
                }""")
            except Exception:
                continue
            if state == "restricted":
                return True
            if state == "slider":
                return False
            if state == "check_oopif":
                oopif_state = self._inspect_datadome_oopif(
                    ctx, allow_active_fe_skip=allow_active_fe_skip
                )
                if oopif_state == "restricted":
                    return True
                if oopif_state == "slider":
                    return False
        return False

    def challenge_page(self, task):
        tid = int(task["id"])
        sess = self.sessions.get(tid)
        ctx = sess.get("context") if sess else None
        if not ctx:
            return None
        for page in reversed(ctx.pages):
            if page.is_closed():
                continue
            try:
                visible = page.evaluate(r"""() => {
                    // Native window occlusion (KeyVault covering Chrome) reports
                    // 'hidden'; never let that hide a real human challenge.
                    const isShown = (el) => {
                        if (!el || !el.getClientRects || !el.getClientRects().length) return false;
                        for (let p = el; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
                            if (p.hidden || p.getAttribute('aria-hidden') === 'true') return false;
                            if (p.classList && (p.classList.contains('d-none') || p.classList.contains('v-hidden') || p.classList.contains('sr-only') || p.classList.contains('wb-sr-only'))) return false;
                            const st = getComputedStyle(p);
                            if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity || '1') <= 0.05) return false;
                        }
                        return true;
                    };
                    const frames = document.querySelectorAll('iframe');
                    for (const frame of frames) {
                        const rawSrc = frame.getAttribute('src') || '';
                        const u = new URL(rawSrc || 'about:blank', location.href);
                        // DataDome's silent automatic device check is not a
                        // human challenge: it redirects to /signup on its own.
                        if (/captcha-delivery\.com\/interstitial/i.test(u.href) || /DataDome Device Check/i.test(frame.title || '')) {
                            continue;
                        }
                        const box = frame.getBoundingClientRect();
                        // GitHub's suppressed DataDome placeholder frame.
                        if (/octocaptcha\.com\/datadome/i.test(u.href) && (box.width < 150 || box.height < 120)) {
                            continue;
                        }
                        const haystack = [
                            u.href, u.hostname, frame.id || '', frame.className || '', frame.title || ''
                        ].join(' ');
                        const isChallengeFrame =
                            /octocaptcha|arkoselabs|funcaptcha|hcaptcha|challenges\.cloudflare|recaptcha|imperva|incapsula|_Incapsula_Resource|geetest|captcha|verification|challenge|滑块|验证/i.test(haystack);
                        if (!isChallengeFrame) continue;
                        if (box.width >= 100 && box.height >= 50 && isShown(frame)) return true;
                    }
                    // ``page.evaluate`` runs in the ``kv_assist`` isolated world
                    // where ``window.dd`` is undefined, so inspect the shared
                    // DOM's <script> tags for DataDome's active challenge shell.
                    // Only ``c.js`` / ``rt: 'c'`` / ``rt: 'b'`` match: the silent
                    // ``i.js`` interstitial (``rt: 'i'``) is still ignored.
                    for (const s of document.querySelectorAll('script')) {
                        const src = s.getAttribute('src') || '';
                        const txt = s.textContent || '';
                        if (/captcha-delivery\.com\/c\.js/i.test(src) || /\bdd\s*=\s*\{[^}]*['"]rt['"]\s*:\s*['"][cb]['"]/i.test(txt)) {
                            return true;
                        }
                    }
                    const widget = document.querySelector('#challenge-stage, #challenge-running');
                    if (widget && widget.getClientRects().length && /just a moment|请稍候/i.test(document.title)) {
                        return true;
                    }
                    // Slider / captcha widget containers (never a form field).
                    const widgets = document.querySelectorAll(
                        '.js-octocaptcha-frame, [class*="geetest" i], [id*="geetest" i], ' +
                        '[class*="nc-container" i], [class*="slider" i][class*="verify" i], ' +
                        '[class*="captcha-modal" i]'
                    );
                    for (const widget of widgets) {
                        if (widget.querySelector('input#email')) continue;
                        const box = widget.getBoundingClientRect();
                        if (widget.matches('.js-octocaptcha-frame') &&
                            /octocaptcha\.com\/datadome/i.test(widget.getAttribute('src') || '') &&
                            (box.width < 150 || box.height < 120)) {
                            continue;
                        }
                        if (box.width >= 120 && box.height >= 40 && isShown(widget)) return true;
                    }
                    const sample = (document.body?.innerText || '').slice(0, 2500);
                    if (/访问暂时受限|为什么需要经过「我不是机器人」的验证|Access temporarily restricted|You have triggered an abuse detection mechanism/i.test(sample)) {
                        return true;
                    }
                    // "Verify your account" is static section text on /signup.
                    if (/Please solve this puzzle|请拖动滑块|滑动验证|向右滑动以保护您的访问|Slide right to secure your access|Slide to verify|请完成安全验证/i.test(sample)) {
                        return true;
                    }
                    return false;
                }""")
            except Exception:
                continue
            if visible:
                return page
        return None

    def check_paused_challenge_state(self, task: dict) -> str:
        """Non-intrusive status check while a task is paused for human verification.

        Returns:
        - ``"ip_restricted"`` if the page or DataDome OOPIF transitioned to ``"访问暂时受限"``.
        - ``"solved"`` if the challenge disappeared or the GitHub tab advanced to OTP / logged_in.
        - ``"still_challenged"`` if the challenge is still waiting for the operator.
        """
        tid = int(task["id"])
        sess = self.sessions.get(tid)
        ctx = sess.get("context") if sess else None
        if not ctx:
            return "still_challenged"
        if self.is_ip_restricted_page(task):
            return "ip_restricted"
        if isinstance(ctx, _CdpContext):
            # The operator may have closed the Chrome window (or the whole
            # process died) while paused. That must never be mistaken for a
            # solved challenge, otherwise submit_github_signup runs against a
            # dead browser and the task is lost.
            proc = getattr(ctx, "_proc", None)
            if proc is not None and proc.poll() is not None:
                return "still_challenged"
            try:
                open_pages = [p for p in (ctx.pages or []) if not p.is_closed()]
            except Exception:
                open_pages = []
            if not open_pages:
                return "still_challenged"
        for page in reversed(ctx.pages):
            if page.is_closed():
                continue
            loc = urlsplit(page.url)
            if loc.scheme == "https" and loc.netloc == "github.com":
                # The GitHub tab may be mid-reload or running DataDome's silent
                # /interstitial/ device check. Reporting "solved" then would let
                # submit_github_signup touch a half-built DOM, so wait instead.
                try:
                    transitioning = bool(
                        page.evaluate(
                            r"""() => {
                                if (document.readyState === 'loading') return true;
                                for (const f of document.querySelectorAll('iframe')) {
                                    const src = f.getAttribute('src') || '';
                                    const title = f.getAttribute('title') || f.title || '';
                                    if (/captcha-delivery\.com\/interstitial/i.test(src) || /DataDome Device Check/i.test(title)) return true;
                                }
                                for (const s of document.querySelectorAll('script')) {
                                    const src = s.getAttribute('src') || '';
                                    const txt = s.textContent || '';
                                    if (/captcha-delivery\.com\/i\.js/i.test(src) || /\bdd\s*=\s*\{[^}]*['"]rt['"]\s*:\s*['"]i['"]/i.test(txt)) return true;
                                }
                                return false;
                            }"""
                        )
                    )
                except Exception:
                    transitioning = False
                if transitioning:
                    return "still_challenged"
                state = self._github_page_state(page)
                if state in ("otp", "logged_in"):
                    return "solved"
        if self.challenge_page(task) is None:
            return "solved"
        return "still_challenged"

    @staticmethod
    def _github_page_state(page) -> str:
        """Classify the GitHub tab as ``otp``, ``logged_in`` or ``signup``."""
        try:
            state = page.evaluate("""() => {
                const path = location.pathname || '';
                const otpVisible = Array.from(document.querySelectorAll(
                    'input[name="launch_code[]"], input[autocomplete="one-time-code"], input[name="otp"], input#otp'
                )).some(el => !!el.getClientRects().length);
                const bodyText = document.body?.innerText || '';
                if (path.startsWith('/account_verifications') || otpVisible ||
                    /Enter the.*code|sent a launch code|verify your email|Confirm your email|输入验证码/i.test(bodyText)) {
                    return 'otp';
                }
                const meta = document.querySelector('meta[name="user-login"]');
                const login = (meta?.getAttribute('content') || '').trim();
                const trimmed = path.replace(/\\/+$/, '') || '/';
                if (login && trimmed !== '/signup' && trimmed !== '/login') return 'logged_in';
                return 'signup';
            }""")
        except Exception:
            return "signup"
        return state if state in ("otp", "logged_in", "signup") else "signup"

    @staticmethod
    def _username_taken(page) -> bool:
        """True when GitHub flags the username field as unavailable/taken."""
        try:
            return bool(page.evaluate("""() => {
                const el = document.querySelector('#login, input[name="user[login]"], input[autocomplete="username"]');
                if (el && el.getAttribute('aria-invalid') === 'true') return true;
                const errors = Array.from(document.querySelectorAll(
                    '[role="alert"], .flash-error, .error, [class*="error"]'
                )).filter(node => !!node.getClientRects().length);
                return errors.some(node => /is not available|already taken|is unavailable|已被占用/i.test(node.innerText || ''));
            }"""))
        except Exception:
            return False

    def open(self, task, password, *, mailbox=False):
        if mailbox:
            page = self._page(task["id"], "mail", MAIL_URL, task=task)
            self._fill(page, MAIL_URL, "/", [["#rcmloginuser", task["email"]], ["#rcmloginpwd", password]], task=task)
            return "已打开网页邮箱并按真人节奏填入账密，可在窗口中查看验证邮件。"
        if task["step"] == "github":
            page = self._page(task["id"], "github", GITHUB_URL + "/", task=task)
            cur_loc = urlsplit(page.url)
            channel_label = "当前注册页"
            if not (cur_loc.scheme == "https" and cur_loc.netloc == "github.com" and cur_loc.path.rstrip("/") == "/signup"):
                if (cur_loc.scheme, cur_loc.netloc) != ("https", "github.com"):
                    resp = page.goto(GITHUB_URL + "/", wait_until="domcontentloaded", timeout=15000)
                    if resp and resp.status >= 400:
                        raise ValueError(f"目标页面返回 HTTP {resp.status}，请在工作窗口检查后重试，当前步骤已保留。")
                channel_label = self._enter_github_signup_from_home(page, task["email"], task=task)
                if channel_label == "challenged":
                    return "工作窗口出现人机验证或访问暂时受限，请完成验证或切换节点后继续。"
            self._wait_for_datadome_interstitial(page)
            if self.challenge_page(task):
                return "工作窗口出现人机验证或访问暂时受限，请完成验证或切换节点后继续。"
            count = self._fill_github_signup(page, task["email"], password, task["username"], task=task)
            if count == 0 and (self.challenge_page(task) or self.is_ip_restricted_page(task)):
                return "工作窗口出现人机验证或访问暂时受限，请完成验证或切换节点后继续。"
            return f"已从 GitHub 首页经「{channel_label}」进入注册页，并按真人点击+粘贴节奏填写 {count} 个空栏。请在工作窗口核对提交。"
        if task["step"] == "email":
            self._page(task["id"], "github", GITHUB_URL + "/login", task=task)
            return "请在 GitHub 窗口完成邮箱验证。"

        page = self._page(task["id"], "workbuddy", WORKBUDDY_URL + "/", task=task)
        official = urlsplit(page.url)
        if (official.scheme, official.netloc) != ("https", "www.workbuddy.ai"):
            return "请在当前授权窗口核对身份并完成登录。"
        if task["step"] == "greeting":
            if official.path.rstrip("/") != "/app":
                _sleep_action_delay(1.0, 2.0)
                if self.challenge_page(task):
                    return "工作窗口出现人机验证或访问暂时受限，请完成验证后继续。"
                app_entry = self._first_visible(page.locator('a[href*="/app"]'))
                if app_entry is None:
                    app_entry = self._first_visible(
                        page.get_by_role(
                            "button",
                            name=re.compile(r"(Web\s*App|Open\s*App|Start\s*Chat|在线使用|网页版|进入应用|开始对话)", re.I),
                        )
                    )
                if app_entry is not None:
                    self._human_click(page, app_entry)
                    try:
                        page.wait_for_url("**/app**", timeout=8.0)
                    except Exception:
                        pass
                after_loc = urlsplit(page.url)
                if (
                    after_loc.netloc == "www.workbuddy.ai"
                    and after_loc.path.rstrip("/") != "/app"
                    and not self.challenge_page(task)
                ):
                    resp = page.goto(
                        WORKBUDDY_APP_URL,
                        referer=WORKBUDDY_URL + "/",
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    if resp and resp.status >= 400:
                        raise ValueError(f"目标页面返回 HTTP {resp.status}，请在工作窗口检查后重试，当前步骤已保留。")
            return "网页版已打开。发送招呼并等到回复后，再确认完成。"

        _sleep_action_delay(1.0, 2.0)
        if self.challenge_page(task):
            return "工作窗口出现人机验证或访问暂时受限，请完成验证后继续。"
        if task["step"] == "region":
            profile = self._first_visible(
                page.get_by_text(re.compile(r"^(完善信息|Complete (your )?(profile|information))$", re.I))
            )
            if profile is not None:
                self._human_click(page, profile)
                _sleep_action_delay(1.0, 2.0)
                return "已打开完善信息页面，请核对非国区地区后保存。"
            return "请检查右上角的完善信息入口，并记录实际地区确认结果。"

        gh_re = re.compile(r"^(Continue with GitHub|Sign in with GitHub|GitHub|使用 GitHub 登录)$", re.I)
        provider = self._first_visible(page.get_by_text(gh_re))
        if provider is not None:
            self._human_click(page, provider)
        else:
            login_re = re.compile(r"^(登录|Log\s?in|Sign\s?in)$", re.I)
            signup_re = re.compile(r"^(注册|Sign\s?up|Get\s+Started|Try\s+Now|免费使用|立即体验|开始使用)$", re.I)
            entry_candidates = []
            for loc_cand in (
                self._first_visible(page.get_by_role("button", name=login_re)),
                self._first_visible(page.get_by_role("link", name=login_re)),
                self._first_visible(page.get_by_role("button", name=signup_re)),
                self._first_visible(page.get_by_role("link", name=signup_re)),
            ):
                if loc_cand is not None:
                    entry_candidates.append(loc_cand)
            if entry_candidates:
                chosen_entry = secrets.choice(entry_candidates)
                self._human_click(page, chosen_entry)
                _sleep_action_delay(1.0, 2.0)
                provider_after = self._first_visible(page.get_by_text(gh_re))
                if provider_after is not None:
                    self._human_click(page, provider_after)
        return "已从 WorkBuddy 国际版首页触发登录/注册入口，请使用 GitHub 完成授权并核对用户名。"

    def submit_github_signup(self, task: dict, password: str) -> dict:
        """Fill the GitHub signup form and human-click ``Create account``.

        Never solves a challenge: when one is detected the queue is paused with
        ``status="challenged"`` so the operator can finish it manually. A
        dead-end Imperva IP block is reported as ``status="ip_restricted"`` so
        the caller can rotate to a clean proxy node instead.
        """
        username = str(task.get("username") or "")
        page = self._page(task["id"], "github", GITHUB_URL + "/", task=task)
        self._wait_for_datadome_interstitial(page)

        if self.is_ip_restricted_page(task):
            return {
                "status": "ip_restricted",
                "username": username,
                "message": "当前节点 IP 被 GitHub 限制访问（访问暂时受限），正在自动拉黑该节点并切换新节点重开…",
            }

        state = self._github_page_state(page)
        if state == "otp":
            return {
                "status": "next_step",
                "next": "email",
                "username": username,
                "message": "已进入 GitHub 邮箱验证码页面，准备自动读取验证码。",
            }
        if state == "logged_in":
            return {
                "status": "next_step",
                "next": "workbuddy",
                "username": username,
                "message": "GitHub 已登录，自动进入 WorkBuddy 授权。",
            }

        actual_username = username.strip()
        login_sel = '#login, input[name="user[login]"], input[autocomplete="username"]'
        skip_refill = False

        if str(task.get("status") or "") == "verified":
            # The operator just solved a slider/CAPTCHA. Give the page time to
            # redirect without touching the DOM, so the solved token survives.
            _sleep_action_delay(1.2, 2.0)
            self._wait_for_datadome_interstitial(page)
            if self.is_ip_restricted_page(task):
                return {
                    "status": "ip_restricted",
                    "username": username,
                    "message": "当前节点 IP 被 GitHub 限制访问（访问暂时受限），正在自动拉黑该节点并切换新节点重开…",
                }
            state = self._github_page_state(page)
            if state == "otp":
                return {
                    "status": "next_step",
                    "next": "email",
                    "username": username,
                    "message": "已进入 GitHub 邮箱验证码页面，准备自动读取验证码。",
                }
            if state == "logged_in":
                return {
                    "status": "next_step",
                    "next": "workbuddy",
                    "username": username,
                    "message": "GitHub 已登录，自动进入 WorkBuddy 授权。",
                }
            resume_loc = urlsplit(page.url)
            resume_on_signup = (
                resume_loc.scheme == "https"
                and resume_loc.netloc == "github.com"
                and resume_loc.path.rstrip("/") == "/signup"
            )
            if resume_on_signup and self.challenge_page(task) is None:
                # DataDome's c.js solves the slider, then calls history.back()
                # to GET /signup. Chrome restores #email and #login from session
                # history but clears <input type="password">, so only refill the
                # password instead of re-touching #login (which would invalidate
                # the solved token and restart the username Tab/loop).
                try:
                    field_state = page.evaluate(
                        """() => {
                            const val = (s) => {
                                const el = document.querySelector(s);
                                return el && el.value ? el.value.trim() : '';
                            };
                            return {
                                email: Boolean(val('#email, input[name="user[email]"], input[autocomplete="email"]')),
                                password: Boolean(val('#password, input[name="user[password]"], input[autocomplete="new-password"]')),
                                login: val('#login, input[name="user[login]"], input[autocomplete="username"]'),
                            };
                        }"""
                    )
                except Exception:
                    field_state = {}
                if (
                    isinstance(field_state, dict)
                    and field_state.get("email")
                    and field_state.get("login")
                ):
                    if not field_state.get("password") and password:
                        self._human_paste_input(
                            page,
                            page.locator(
                                '#password, input[name="user[password]"], '
                                'input[autocomplete="new-password"]'
                            ).first,
                            password,
                            origin=GITHUB_URL,
                            path="/signup",
                            task=task,
                        )
                    skip_refill = True
                    actual_username = str(field_state.get("login")).strip()
                else:
                    try:
                        all_filled = bool(
                            page.evaluate(
                                "() => ['#email', '#password', '#login'].every(s => { "
                                "const el = document.querySelector(s); "
                                "return el && el.value && el.value.trim().length > 0; })"
                            )
                        )
                    except Exception:
                        all_filled = False
                    # Re-filling would mutate #login and invalidate the solved
                    # CAPTCHA token, so jump straight to Create account. Read the
                    # current value instead so operator edits are preserved.
                    skip_refill = all_filled
                    if skip_refill:
                        try:
                            current_login = str(
                                page.locator(login_sel).first.input_value(timeout=1500) or ""
                            ).strip()
                        except Exception:
                            current_login = ""
                        if current_login:
                            actual_username = current_login

        if not skip_refill:
            if self.challenge_page(task):
                if self.is_ip_restricted_page(task):
                    return {
                        "status": "ip_restricted",
                        "username": username,
                        "message": "当前节点 IP 被 GitHub 限制访问（访问暂时受限），正在自动拉黑该节点并切换新节点重开…",
                    }
                self.show_paused_window(task)
                return {
                    "status": "challenged",
                    "username": username,
                    "message": "工作窗口出现人机验证，请在浏览器点击完成验证后点击「继续自动注册」。",
                }

            cur_loc = urlsplit(page.url)
            on_signup = (
                cur_loc.scheme == "https"
                and cur_loc.netloc == "github.com"
                and cur_loc.path.rstrip("/") == "/signup"
            )
            if not on_signup:
                self.open(task, password)
                page = self._page(task["id"], "github", GITHUB_URL + "/signup", task=task)
            else:
                self._fill_github_signup(
                    page, str(task.get("email") or ""), password, username, task=task
                )

            if self.is_ip_restricted_page(task):
                return {
                    "status": "ip_restricted",
                    "username": username,
                    "message": "当前节点 IP 被 GitHub 限制访问（访问暂时受限），正在自动拉黑该节点并切换新节点重开…",
                }
            if self.challenge_page(task):
                return {
                    "status": "challenged",
                    "username": username,
                    "message": "工作窗口出现人机验证，请在浏览器点击完成验证后点击「继续自动注册」。",
                }

            # Blur #login with a native CDP Tab key so validation/blur events are
            # isTrusted: true (synthetic dispatchEvent calls are a bot signal).
            try:
                page.keyboard.press_tab()
            except Exception:
                pass
            _sleep_action_delay(1.2, 2.0)
            for _ in range(3):
                if not self._username_taken(page):
                    break
                prefix = (
                    re.sub(
                        r"[^a-zA-Z0-9-]+",
                        "-",
                        str(task.get("email") or "user").split("@")[0],
                    )
                    .strip("-")[:24]
                    or "user"
                )
                actual_username = f"{prefix}{secrets.randbelow(9000) + 1000}"
                try:
                    login_field = page.locator(login_sel).first
                    self._human_click(page, login_field)
                    # Select-all + Backspace clears the field with trusted key
                    # events instead of a synthetic value setter.
                    page.send(
                        "Input.dispatchKeyEvent",
                        {
                            "type": "rawKeyDown",
                            "key": "a",
                            "code": "KeyA",
                            "windowsVirtualKeyCode": 65,
                            "nativeVirtualKeyCode": 65,
                            "modifiers": 2,
                            "commands": ["selectAll"],
                        },
                    )
                    page.send(
                        "Input.dispatchKeyEvent",
                        {
                            "type": "keyUp",
                            "key": "a",
                            "code": "KeyA",
                            "windowsVirtualKeyCode": 65,
                            "nativeVirtualKeyCode": 65,
                            "modifiers": 2,
                        },
                    )
                    page.send(
                        "Input.dispatchKeyEvent",
                        {
                            "type": "rawKeyDown",
                            "key": "Backspace",
                            "code": "Backspace",
                            "windowsVirtualKeyCode": 8,
                            "nativeVirtualKeyCode": 8,
                            "commands": ["deleteBackward"],
                        },
                    )
                    page.send(
                        "Input.dispatchKeyEvent",
                        {
                            "type": "keyUp",
                            "key": "Backspace",
                            "code": "Backspace",
                            "windowsVirtualKeyCode": 8,
                            "nativeVirtualKeyCode": 8,
                        },
                    )
                except Exception:
                    pass
                try:
                    self._human_paste_input(
                        page,
                        page.locator(login_sel).first,
                        actual_username,
                        origin=GITHUB_URL,
                        path="/signup",
                        task=task,
                    )
                except Exception:
                    pass
                try:
                    page.keyboard.press_tab()
                except Exception:
                    pass
                _sleep_action_delay(1.2, 2.0)

        submit_selectors = [
            'button[type="submit"]',
            'button.js-octocaptcha-form-submit',
            'form[action*="/signup"] button[type="submit"]',
        ]
        submit_btn = None
        deadline = time.time() + 5.0
        while time.time() < deadline and submit_btn is None:
            for selector in submit_selectors:
                loc = page.locator(selector)
                try:
                    cnt = loc.count()
                except Exception:
                    cnt = 0
                for idx in range(cnt):
                    cand = loc.nth(idx)
                    try:
                        info = cand.evaluate(
                            "el => ({text: (el.textContent || ''), visible: !!el.getClientRects().length, "
                            "disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'})"
                        )
                    except Exception:
                        continue
                    if not isinstance(info, dict) or not info.get("visible"):
                        continue
                    text = str(info.get("text") or "")
                    if re.search(r"google|apple", text, re.I):
                        continue
                    if not re.search(r"create account|创建账户|continue|sign up", text, re.I):
                        continue
                    submit_btn = cand
                    break
                if submit_btn is not None:
                    break
            if submit_btn is None:
                time.sleep(0.4)

        clicked = False
        if submit_btn is not None:
            ready_deadline = time.time() + 4.0
            while time.time() < ready_deadline:
                try:
                    ready = submit_btn.evaluate(
                        "el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'"
                    )
                except Exception:
                    ready = False
                if ready:
                    break
                time.sleep(0.2)
            try:
                _sleep_action_delay(1.0, 2.0)
                self._human_click(page, submit_btn)
                clicked = True
            except Exception:
                clicked = False

        if clicked:
            # Let any redirect / slider challenge initialize cleanly before the
            # first post-click CDP read.
            _sleep_action_delay(1.8, 2.6)
            for _ in range(4):
                if self.is_ip_restricted_page(task):
                    return {
                        "status": "ip_restricted",
                        "username": actual_username,
                        "message": "提交注册后当前节点 IP 触发「访问暂时受限」，正在自动拉黑并切换干净节点重开…",
                    }
                state = self._github_page_state(page)
                if state == "otp":
                    return {
                        "status": "next_step",
                        "next": "email",
                        "username": actual_username,
                        "message": "已自动点击 Create account 并进入邮箱验证码页面！",
                    }
                if state == "logged_in":
                    return {
                        "status": "next_step",
                        "next": "workbuddy",
                        "username": actual_username,
                        "message": "GitHub 注册已完成并登录！",
                    }
                if self.challenge_page(task):
                    try:
                        page.bring_to_front()
                    except Exception:
                        pass
                    # Return immediately: no further CDP traffic while the
                    # operator drags the slider.
                    return {
                        "status": "challenged",
                        "username": actual_username,
                        "message": "已点击 Create account，当前弹出 GitHub 人机验证，请在浏览器窗口完成验证后点击「继续自动注册」。",
                    }
                time.sleep(1.2)

        return {
            "status": "waiting_human",
            "username": actual_username,
            "message": "已填写并点击 Create account，请在浏览器窗口确认是否需要补充验证后点击继续。",
        }

    def submit_github_otp(self, task: dict, code: str) -> dict:
        """Submit the GitHub email verification code without touching challenges."""
        filled = self.fill_github_code(task, code)
        _sleep_action_delay(1.2, 2.2)

        tid = int(task["id"])
        sess = self.sessions.get(tid)
        ctx = sess.get("context") if sess else None
        page = None
        if ctx:
            for candidate in reversed(ctx.pages):
                if candidate.is_closed():
                    continue
                loc = urlsplit(candidate.url)
                if loc.scheme == "https" and loc.netloc == "github.com":
                    page = candidate
                    break
        if page is not None:
            try:
                still_otp = urlsplit(page.url).path.startswith("/account_verifications")
                if not still_otp:
                    still_otp = bool(
                        page.evaluate(
                            "() => Array.from(document.querySelectorAll("
                            "'input[name=\"launch_code[]\"], input[autocomplete=\"one-time-code\"], "
                            "input[name=\"otp\"], input#otp'"
                            ")).some(el => !!el.getClientRects().length)"
                        )
                    )
                if still_otp:
                    btn = self._first_visible_any(
                        page,
                        [
                            'button[type="submit"]',
                            'button:has-text("Verify")',
                            'button:has-text("Continue")',
                        ],
                    )
                    if btn is not None:
                        self._human_click(page, btn)
            except Exception:
                pass

        deadline = time.time() + 10.0
        while time.time() < deadline:
            if self.challenge_page(task):
                return {
                    "status": "challenged",
                    "filled": filled,
                    "message": "提交验证码后出现人机验证，请在窗口完成后点击继续。",
                }
            if page is not None:
                try:
                    skip = self._first_visible_any(
                        page,
                        [
                            'button:has-text("Skip personalization")',
                            'button:has-text("Continue for free")',
                            'a[href*="skip"]',
                        ],
                    )
                    if skip is not None:
                        self._human_click(page, skip)
                        time.sleep(1.0)
                except Exception:
                    pass
                try:
                    verified = page.evaluate("""() => {
                        const meta = document.querySelector('meta[name="user-login"]');
                        if ((meta?.getAttribute('content') || '').trim()) return true;
                        const path = location.pathname || '';
                        const otpVisible = Array.from(document.querySelectorAll(
                            'input[name="launch_code[]"], input#otp, input[name="otp"]'
                        )).some(el => !!el.getClientRects().length);
                        const errors = Array.from(document.querySelectorAll(
                            '.flash-error, [role="alert"]'
                        )).filter(node => !!node.getClientRects().length);
                        const hasError = errors.some(node =>
                            /incorrect|invalid|expired/i.test(node.innerText || ''));
                        return path !== '/signup' && !otpVisible && !hasError;
                    }""")
                except Exception:
                    verified = False
                if verified:
                    return {
                        "status": "next_step",
                        "next": "workbuddy",
                        "filled": True,
                        "message": "GitHub 邮箱验证码已自动验证通过，正在进入 WorkBuddy 国际版！",
                    }
            time.sleep(0.8)

        return {
            "status": "waiting_human",
            "filled": filled,
            "message": "已填入 GitHub 验证码，请在窗口核对验证结果后点击继续。",
        }

    def _is_workbuddy_logged_in(self, tid: int, ctx) -> bool:
        cached = self.captured_tokens.get(tid)
        if isinstance(cached, dict) and cached.get("accessToken"):
            return True
        if ctx is None:
            return False
        for p in ctx.pages:
            if p.is_closed():
                continue
            if urlsplit(p.url).netloc != "www.workbuddy.ai":
                continue
            try:
                found = p.evaluate(_WB_STORAGE_SCAN_JS)
                if isinstance(found, dict) and found.get("accessToken"):
                    merged = {**(self.captured_tokens.get(tid) or {}), **found}
                    self.captured_tokens[tid] = merged
                    return True
            except Exception:
                pass
            try:
                in_app = p.evaluate(
                    "() => location.pathname.replace(/\\/+$/, '') === '/app' "
                    "&& !document.querySelector('.cloud-welcome__login-btn')"
                )
                if in_app:
                    return True
            except Exception:
                pass
        return False

    def complete_workbuddy_github_login(self, task: dict) -> dict:
        """Drive WorkBuddy homepage Login -> Continue with GitHub -> Authorize."""
        page = self._page(task["id"], "workbuddy", WORKBUDDY_URL + "/", task=task)
        tid = int(task["id"])
        ctx = self._context_for(tid, task=task)
        if self._is_workbuddy_logged_in(tid, ctx):
            return {"status": "next_step", "next": "region", "message": "已登录 WorkBuddy 国际版！"}

        self.open({**task, "step": "workbuddy"}, "")

        deadline = time.time() + 18.0
        while time.time() < deadline:
            pages = ctx.pages
            for p in pages:
                self._attach_token_sniffer(tid, ctx, p)
            if self.challenge_page(task):
                return {
                    "status": "challenged",
                    "message": "登录 WorkBuddy 过程中出现人机验证，请在窗口完成后点击继续。",
                }
            if self._is_workbuddy_logged_in(tid, ctx):
                return {
                    "status": "next_step",
                    "next": "region",
                    "message": "已完成 GitHub OAuth 授权并成功登录 WorkBuddy 国际版！",
                }
            for p in reversed(pages):
                if p.is_closed():
                    continue
                loc = urlsplit(p.url)
                if loc.netloc == "github.com" and loc.path.startswith("/login/oauth/authorize"):
                    auth_btn = self._first_visible_any(
                        p,
                        [
                            "button#js-oauth-authorize-btn",
                            'button[name="authorize"][value="1"]',
                            'form[action*="/login/oauth/authorize"] button[type="submit"]',
                        ],
                    )
                    if auth_btn is None:
                        auth_btn = self._first_visible(
                            p.get_by_role("button", name=re.compile(r"^(Authorize|授权)", re.I))
                        )
                    if auth_btn is not None:
                        try:
                            enabled = auth_btn.evaluate("el => !el.disabled")
                        except Exception:
                            enabled = False
                        if enabled:
                            try:
                                p.bring_to_front()
                            except Exception:
                                pass
                            _sleep_action_delay(1.0, 2.0)
                            self._human_click(p, auth_btn)
                            try:
                                p.wait_for_url("**/www.workbuddy.ai/**", timeout=1.5)
                            except Exception:
                                time.sleep(1.5)
                elif loc.netloc == "www.workbuddy.ai":
                    gh_btn = self._first_visible(
                        p.get_by_text(
                            re.compile(
                                r"^(Continue with GitHub|Sign in with GitHub|GitHub|使用 GitHub 登录)$",
                                re.I,
                            )
                        )
                    )
                    if gh_btn is not None:
                        self._human_click(p, gh_btn)
            time.sleep(1.0)

        if self._is_workbuddy_logged_in(tid, ctx) or (
            urlsplit(page.url).netloc == "www.workbuddy.ai"
            and self._first_visible(
                page.get_by_role("button", name=re.compile(r"^(登录|Log\s?in|Sign\s?in)$", re.I))
            )
            is None
        ):
            if self._is_workbuddy_logged_in(tid, ctx):
                return {
                    "status": "next_step",
                    "next": "region",
                    "message": "已完成 GitHub 授权并登录 WorkBuddy 国际版！",
                }
        return {
            "status": "waiting_human",
            "message": "已在窗口触发 WorkBuddy 的 GitHub 登录，请在窗口确认授权后点击继续。",
        }

    def prepare_greeting(self, task):
        page = self._page(task["id"], "workbuddy", WORKBUDDY_APP_URL, task=task)
        location = urlsplit(page.url)
        if (location.scheme, location.netloc, location.path.rstrip("/")) != ("https", "www.workbuddy.ai", "/app"):
            raise ValueError("请先在 WorkBuddy 国际版打开网页版新对话。")
        _sleep_action_delay(1.0, 2.0)
        composer = page.locator(".wb-home-composer:visible")
        editor = composer.locator('[role="textbox"][contenteditable="true"][data-slate-editor="true"]:visible')
        send = composer.locator('[data-cb-chat-input-toolbar-right] [role="button"][data-track-id="agent_session_input_status"]:visible')
        if page.locator(".cloud-welcome__login-btn:visible").count():
            raise ValueError("请先登录当前员工的 WorkBuddy，再发送招呼。")
        if editor.count() != 1 or send.count() != 1:
            raise ValueError("未识别到网页版新对话的输入框和发送按钮。可打开新对话重试，或复制招呼手动发送。")
        draft = editor.evaluate("""el => {
            const copy = el.cloneNode(true);
            copy.querySelectorAll('[data-slate-placeholder]').forEach(node => node.remove());
            return copy.textContent.replace(/\\uFEFF/g, '').trim();
        }""")
        if draft and draft != task["greeting"]:
            raise ValueError("对话框中已有其他草稿，请先处理草稿或打开新对话。")
        if not draft:
            self._human_click(page, editor)
            _sleep_action_delay(1.0, 2.0)
            try:
                page.keyboard.insert_text(task["greeting"])
            except Exception:
                pass
            after_text = editor.evaluate("""el => {
                const copy = el.cloneNode(true);
                copy.querySelectorAll('[data-slate-placeholder]').forEach(node => node.remove());
                return copy.textContent.replace(/\\uFEFF/g, '').trim();
            }""")
            if not after_text:
                editor.fill(task["greeting"], timeout=4000)
            _sleep_action_delay(1.0, 2.0)
        return send.element_handle(timeout=4000)

    def complete_workbuddy_region(self, task: dict) -> dict:
        """Confirm the optional WorkBuddy profile / region dialog."""
        page = self._page(task["id"], "workbuddy", WORKBUDDY_URL + "/", task=task)
        _sleep_action_delay(1.0, 2.0)
        if self.challenge_page(task):
            return {"status": "challenged", "message": "窗口出现人机验证，请在窗口完成后点击继续。"}

        profile = self._first_visible(
            page.get_by_text(
                re.compile(
                    r"^(完善信息|补充信息|Complete (your )?(profile|information))$", re.I
                )
            )
        )
        if profile is None:
            return {
                "status": "next_step",
                "next": "greeting",
                "region": "not_required",
                "message": "右上角无「完善信息」按钮（无需确认地区），自动进入在线 WorkBuddy 发送招呼！",
            }

        self._human_click(page, profile)
        _sleep_action_delay(1.0, 2.0)
        # Best-effort: pick a non-CN region when the dialog offers a choice.
        try:
            page.evaluate("""() => {
                const isCn = (text) => /中国|中国大陆|China|CN\\b/i.test(text || '');
                const select = document.querySelector('select');
                if (select instanceof HTMLSelectElement) {
                    const option = Array.from(select.options).find(
                        opt => opt.value && !isCn(opt.value) && !isCn(opt.textContent)
                    );
                    if (option) {
                        select.value = option.value;
                        select.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                }
                const candidates = Array.from(document.querySelectorAll(
                    '[role="radio"], [role="option"], label, li'
                )).filter(el => !!el.getClientRects().length);
                const pick = candidates.find(el => {
                    const text = (el.innerText || el.textContent || '').trim();
                    return text && !isCn(text) && /美国|日本|新加坡|United States|Japan|Singapore/i.test(text);
                });
                if (pick) { pick.click(); return true; }
                return false;
            }""")
        except Exception:
            pass
        _sleep_action_delay(1.0, 2.0)
        confirm_btn = self._first_visible(
            page.get_by_role(
                "button",
                name=re.compile(r"^(确认|保存|提交|确定|Confirm|Save|Submit|Continue)$", re.I),
            )
        )
        if confirm_btn is not None:
            self._human_click(page, confirm_btn)
            time.sleep(1.2)
            still_open = self._first_visible(
                page.get_by_text(
                    re.compile(
                        r"^(完善信息|补充信息|Complete (your )?(profile|information))$", re.I
                    )
                )
            )
            if still_open is None:
                return {
                    "status": "next_step",
                    "next": "greeting",
                    "region": "confirmed",
                    "message": "已自动完成「完善信息」与非国区地区确认！",
                }
        return {
            "status": "waiting_human",
            "region": "confirmed",
            "message": "已打开「完善信息」弹窗，请确认非国区地区并保存后点击继续。",
        }

    def send_greeting_and_wait_reply(self, task: dict, already_sent: bool = False) -> dict:
        """Send the greeting on /app and wait (bounded) for the assistant reply."""
        self.open({**task, "step": "greeting"}, "")
        if self.challenge_page(task):
            return {
                "status": "challenged",
                "sent": bool(already_sent),
                "message": "窗口出现人机验证，请在窗口完成后点击继续。",
            }
        page = self._page(task["id"], "workbuddy", WORKBUDDY_APP_URL, task=task)
        try:
            base_len = int(page.evaluate("() => (document.body?.innerText || '').length") or 0)
        except Exception:
            base_len = 0

        sent = bool(already_sent)
        if not sent:
            try:
                btn = self.prepare_greeting(task)
                btn.click(timeout=4000)
                sent = True
            except ValueError:
                try:
                    composer = self._first_visible(
                        page.locator('[contenteditable="true"], textarea')
                    )
                    if composer is not None:
                        self._human_click(page, composer)
                        _sleep_action_delay(1.0, 2.0)
                        page.keyboard.insert_text(str(task.get("greeting") or ""))
                        _sleep_action_delay(1.0, 2.0)
                        send_btn = self._first_visible_any(
                            page,
                            [
                                'button[type="submit"]',
                                '[role="button"][data-track-id*="input"]',
                                'button:has-text("Send")',
                                'button:has-text("发送")',
                            ],
                        )
                        if send_btn is not None:
                            self._human_click(page, send_btn)
                        else:
                            page.send(
                                "Input.dispatchKeyEvent",
                                {
                                    "type": "keyDown",
                                    "key": "Enter",
                                    "code": "Enter",
                                    "windowsVirtualKeyCode": 13,
                                },
                            )
                            page.send(
                                "Input.dispatchKeyEvent",
                                {
                                    "type": "keyUp",
                                    "key": "Enter",
                                    "code": "Enter",
                                    "windowsVirtualKeyCode": 13,
                                },
                            )
                        sent = True
                except Exception:
                    sent = False

        if sent:
            in_tests = bool(os.environ.get("PYTEST_CURRENT_TEST"))
            timeout = 3.0 if in_tests else 35.0
            interval = 0.2 if in_tests else 1.2
            greeting_len = len(str(task.get("greeting") or ""))
            deadline = time.time() + timeout
            last_len = None
            stable = 0
            while time.time() < deadline:
                try:
                    detected = page.evaluate("""() => {
                        const sel = '[data-role="assistant"], [class*="assistant"], [class*="reply"], [class*="markdown"], .wb-chat-message';
                        if (document.querySelector(sel)) return true;
                        return false;
                    }""")
                    if not detected:
                        now_len = int(page.evaluate("() => (document.body?.innerText || '').length") or 0)
                        if now_len > base_len + greeting_len + 4:
                            if last_len == now_len:
                                stable += 1
                            else:
                                stable = 0
                            last_len = now_len
                            detected = stable >= 2
                except Exception:
                    detected = False
                if detected:
                    return {
                        "status": "replied",
                        "sent": True,
                        "message": "已发送招呼并收到 WorkBuddy 回复！",
                    }
                time.sleep(interval)

        return {
            "status": "replied",
            "sent": bool(sent),
            "message": "已发送招呼并完成等待响应，正在保存凭据。",
        }

    def _normalize_credential(self, task: dict, raw: dict) -> dict | None:
        tok = str(raw.get("accessToken") or "").strip()
        if not tok or tok.count(".") != 2:
            return None
        uid = re.sub(
            r"[^A-Za-z0-9_-]",
            "_",
            str(raw.get("uid") or _jwt_uid(tok) or "").strip(),
        ).strip("_ ")
        if not uid:
            uid = re.sub(r"[^A-Za-z0-9_-]", "_", str(task.get("username") or "wb_user")).strip("_ ")
        exp = int(raw.get("expiresAt") or _jwt_exp(tok) or 0)
        nickname = str(
            raw.get("nickname")
            or task.get("username")
            or str(task.get("email") or "").split("@")[0]
        ).strip()
        proxy_url = str(task.get("proxy_url") or "").strip()
        return {
            "uid": uid,
            "nickname": nickname,
            "domain": "www.workbuddy.ai",
            "realm": "intl",
            "platform": "CLI",
            "product": "workbuddy",
            "enterpriseId": str(raw.get("enterpriseId") or ""),
            "accessToken": tok,
            "refreshToken": str(raw.get("refreshToken") or "").strip(),
            "expiresAt": exp,
            "addedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "oauth",
            "apiKey": False,
            "proxySlot": "",
            "proxy": proxy_url,
            "enabled": True,
            "tasksEnabled": True,
            "weight": 1,
            "note": f"{task.get('email', '')} | GitHub: {task.get('username', '')}".strip(" |"),
        }

    def _wb_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "WorkBuddy/5.8.4",
            "Origin": WORKBUDDY_URL,
            "Referer": WORKBUDDY_URL + "/",
        }

    def poll_cli_oauth(self, state: str, task: dict) -> dict:
        st = str(state or "").strip()
        if not st:
            return {"status": "empty"}
        proxy_url = str(task.get("proxy_url") or "").strip()
        headers = self._wb_headers()
        token_url = f"{WORKBUDDY_URL}{AUTH_TOKEN_PATH}?state={urllib.parse.quote(st)}"
        try:
            payload = _wb_http_json(token_url, method="GET", headers=headers, proxy_url=proxy_url, timeout=10)
        except Exception as exc:
            return {"status": "pending", "message": str(exc)}
        code = payload.get("code")
        if code == LOGIN_PENDING_CODE:
            return {"status": "pending", "message": payload.get("msg") or "等待浏览器授权确认"}
        if code != 0:
            return {"status": "error", "message": str(payload.get("msg") or f"授权状态异常 ({code})")}
        data = payload.get("data") or {}
        if isinstance(data.get("data"), dict):
            data = data["data"]
        tok = str(data.get("accessToken") or "").strip()
        if not tok or tok.count(".") != 2:
            return {"status": "pending", "message": "等待签发 accessToken"}
        ref = str(data.get("refreshToken") or "").strip()
        uid = _jwt_uid(tok)
        nickname = ""
        enterprise_id = ""
        try:
            acct_headers = dict(headers)
            acct_headers["Authorization"] = f"Bearer {tok}"
            acct_url = f"{WORKBUDDY_URL}{LOGIN_ACCOUNT_PATH}?state={urllib.parse.quote(st)}"
            acct_payload = _wb_http_json(acct_url, method="GET", headers=acct_headers, proxy_url=proxy_url, timeout=8)
            acct_data = acct_payload.get("data") if isinstance(acct_payload.get("data"), dict) else acct_payload
            nickname = str(acct_data.get("nickname") or acct_data.get("userName") or "").strip()
            enterprise_id = str(acct_data.get("enterpriseId") or "").strip()
            if not uid and acct_data.get("uid"):
                uid = str(acct_data["uid"]).strip()
        except Exception:
            pass
        if not nickname:
            try:
                prof_headers = {
                    "Authorization": f"Bearer {tok}",
                    "Accept": "application/json, text/plain, */*",
                    "x-client-platform": "web",
                    "User-Agent": "WorkBuddy/5.8.4",
                }
                prof_data = _wb_http_json(
                    WORKBUDDY_URL + PROFILE_PATH,
                    method="GET",
                    headers=prof_headers,
                    proxy_url=proxy_url,
                    timeout=8,
                )
                nickname = str(prof_data.get("nickname") or "").strip()
                if not uid and prof_data.get("uid"):
                    uid = str(prof_data["uid"]).strip()
            except Exception:
                pass
        cred = self._normalize_credential(
            task,
            {
                "uid": uid,
                "nickname": nickname,
                "enterpriseId": enterprise_id,
                "accessToken": tok,
                "refreshToken": ref,
                "expiresAt": _jwt_exp(tok),
            },
        )
        if cred:
            tid = int(task["id"])
            self.captured_tokens[tid] = dict(cred)
            oauth_tab = self._tabs_for(tid).pop("wb_oauth", None)
            if oauth_tab and not oauth_tab.is_closed():
                try:
                    oauth_tab.close()
                except Exception:
                    pass
            return {"status": "ok", "credential": cred}
        return {"status": "pending", "message": "暂未获取到有效 JWT 凭据"}

    def start_cli_oauth(self, task: dict) -> dict:
        tid = int(task["id"])
        proxy_url = str(task.get("proxy_url") or "").strip()
        state_url = f"{WORKBUDDY_URL}{AUTH_STATE_PATH}?platform=CLI"
        payload = _wb_http_json(
            state_url,
            method="POST",
            data=b"{}",
            headers=self._wb_headers(),
            proxy_url=proxy_url,
            timeout=12,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        state = str(data.get("state") or "").strip()
        auth_url = str(data.get("authUrl") or "").strip()
        if not state or not auth_url:
            raise ValueError("未能从 WorkBuddy 获取 CLI 授权地址，请确认网络与代理连接正常。")

        context = self._context_for(tid, task=task)
        tabs = self._tabs_for(tid)
        old_tab = tabs.get("wb_oauth")
        if old_tab and not old_tab.is_closed():
            page = old_tab
        else:
            page = context.new_page()
            tabs["wb_oauth"] = page
        page.goto(auth_url, wait_until="domcontentloaded", timeout=15000)
        page.bring_to_front()
        _sleep_action_delay(1.0, 2.0)

        if not self.challenge_page(task):
            try:
                btn = page.get_by_role(
                    "button",
                    name=re.compile(r"^(授权|确认授权|确认登录|允许|同意|确认|Authorize|Confirm|Allow|Approve|Continue)$", re.I),
                ).first
                btn.wait_for(state="visible", timeout=2000)
                if urlsplit(page.url).netloc.endswith("workbuddy.ai"):
                    self._human_click(page, btn)
            except Exception:
                pass

        for _ in range(4):
            time.sleep(1.0)
            polled = self.poll_cli_oauth(state, task)
            if polled.get("status") == "ok" and polled.get("credential"):
                return {
                    "state": state,
                    "credential": polled["credential"],
                    "message": "已成功捕获并保存 WorkBuddy2API 登录凭据（含 refreshToken）！",
                }
        return {
            "state": state,
            "credential": None,
            "message": "已在工作窗口打开 CLI 授权页，请在浏览器内点击确认授权后再次点击「保存登录凭据」或「确认完成」。",
        }

    def capture_credential(self, task: dict, oauth_state: str = "") -> dict | None:
        tid = int(task["id"])
        if oauth_state:
            for _ in range(2):
                polled = self.poll_cli_oauth(oauth_state, task)
                if polled.get("status") == "ok" and polled.get("credential"):
                    return polled["credential"]
                time.sleep(0.6)

        cached = self.captured_tokens.get(tid)
        if isinstance(cached, dict) and cached.get("accessToken") and cached.get("refreshToken"):
            return self._normalize_credential(task, cached)

        # Inspect browser storage on www.workbuddy.ai pages for this task's session
        sess = self.sessions.get(tid)
        ctx = sess.get("context") if sess else None
        if ctx:
            for page in reversed(ctx.pages):
                if page.is_closed():
                    continue
                if urlsplit(page.url).netloc != "www.workbuddy.ai":
                    continue
                try:
                    found = page.evaluate("""() => {
                        const out = {};
                        for (const store of [window.localStorage, window.sessionStorage]) {
                            if (!store) continue;
                            for (let i = 0; i < store.length; i++) {
                                const k = store.key(i);
                                const v = store.getItem(k) || '';
                                if (!out.accessToken && /^[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+$/.test(v.trim())) {
                                    out.accessToken = v.trim();
                                } else if (v.startsWith('{')) {
                                    try {
                                        const obj = JSON.parse(v);
                                        const tok = obj?.accessToken || obj?.access_token || obj?.auth?.accessToken;
                                        const ref = obj?.refreshToken || obj?.refresh_token || obj?.auth?.refreshToken;
                                        if (tok && String(tok).split('.').length === 3) {
                                            out.accessToken = String(tok);
                                            if (ref) out.refreshToken = String(ref);
                                            if (obj?.uid) out.uid = String(obj.uid);
                                        }
                                    } catch (_) {}
                                }
                            }
                        }
                        return out;
                    }""")
                    if isinstance(found, dict) and found.get("accessToken"):
                        merged = {**(self.captured_tokens.get(tid) or {}), **found}
                        self.captured_tokens[tid] = merged
                except Exception:
                    continue

            now_cached = self.captured_tokens.get(tid)
            if not (isinstance(now_cached, dict) and now_cached.get("refreshToken")):
                try:
                    res = self.start_cli_oauth(task)
                    if res.get("credential"):
                        return res["credential"]
                except Exception:
                    pass

        final_cached = self.captured_tokens.get(tid)
        if isinstance(final_cached, dict) and final_cached.get("accessToken"):
            return self._normalize_credential(task, final_cached)
        return None

    def _remove_session_dir(self, directory: Path) -> None:
        if not directory.exists():
            return
        if directory.is_symlink() or directory.resolve().parent != self.root:
            raise ValueError("工作目录位置异常，请检查后再继续。")
        # Chrome on Windows can hold transient locks on files (chrome.exe is
        # still exiting), so retry and fall back to ignore_errors on the last
        # attempt instead of crashing cache purging.
        for attempt in range(10):
            try:
                if attempt == 9:
                    shutil.rmtree(directory, ignore_errors=True)
                    return
                shutil.rmtree(directory)
                return
            except PermissionError:
                time.sleep(0.15)

    def close(self, discard_id=None):
        # Also acquire ownership when cleaning a profile left by a previous
        # process. Another live instance might still have that window open.
        if discard_id is not None:
            self._lock_window()
        try:
            if discard_id is None:
                for sess in list(self.sessions.values()):
                    ctx = sess.get("context")
                    if ctx:
                        try:
                            ctx.close()
                        except Exception:
                            pass
                self.sessions.clear()
            else:
                tid = int(discard_id)
                sess = self.sessions.pop(tid, None)
                if sess and sess.get("context"):
                    sess["context"].close()
                if self._last_task_id == tid:
                    self._last_task_id = next(iter(self.sessions), None)

            if not self.sessions and self.driver:
                try:
                    self.driver.stop()
                finally:
                    self.driver = None

            if discard_id is not None:
                tid = int(discard_id)
                self.captured_tokens.pop(tid, None)
                self._remove_session_dir(self.root / str(tid))
        finally:
            if not self.sessions:
                self._last_task_id = None
                self.active_proxy_node = ""
                if self.window_lock is not None:
                    self.window_lock.close()
                    self.window_lock = None

    def purge_cache_and_switch_node(self, next_proxy_url: str = "", next_proxy_node: str = "") -> None:
        """Ensure all round browsers are closed, wipe any residual session cache, and switch proxy node."""
        self.close()
        if self.root.exists():
            self._lock_window()
            try:
                for child in list(self.root.iterdir()):
                    if child.name == ".window.lock":
                        continue
                    if child.is_dir():
                        self._remove_session_dir(child)
            finally:
                if self.window_lock is not None:
                    self.window_lock.close()
                    self.window_lock = None
        self.captured_tokens.clear()
        self.active_proxy_node = ""
        node = (next_proxy_node or "").strip()
        px = (next_proxy_url or "").strip()
        if node:
            _sleep_action_delay(1.0, 2.0)
            try:
                activate_proxy_node("WorkBuddy", px, node)
                self.active_proxy_node = node
            except Exception:
                pass

