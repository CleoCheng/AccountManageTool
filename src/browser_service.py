import asyncio
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
import websockets
from src.proxy_service import _is_local_port_open, activate_proxy_node


GPU_PROFILES = [
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)", "RTX 4070"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)", "RTX 4060 Ti"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)", "RTX 3080"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)", "RTX 3070"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4080 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)", "RTX 4080S"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)", "RTX 3060 Ti"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)", "RTX 4070S"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3090 Direct3D11 vs_5_0 ps_5_0, D3D11)", "RTX 3090"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 7800 XT Direct3D11 vs_5_0 ps_5_0, D3D11)", "RX 7800 XT"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6800 XT Direct3D11 vs_5_0 ps_5_0, D3D11)", "RX 6800 XT"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 7900 GRE Direct3D11 vs_5_0 ps_5_0, D3D11)", "RX 7900 GRE"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Arc(TM) A770 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)", "Arc A770"),
]

CPU_OPTIONS = [8, 12, 16, 20, 24]
MEMORY_OPTIONS = [8, 16, 32]
SCREEN_OPTIONS = [
    (1920, 1080),
    (2560, 1440),
    (1920, 1200),
    (2560, 1600),
    (3440, 1440),
    (3840, 2160),
]
CHROME_VERSIONS = [
    "131.0.6778.86",
    "131.0.6778.140",
    "132.0.6834.84",
    "132.0.6834.111",
    "133.0.6943.54",
    "133.0.6943.99",
    "134.0.6998.45",
]


DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_LOCALE = "en-US"
DEFAULT_LANGUAGES = ["en-US", "en"]
DEFAULT_SIDECAR_PROXY_PORT = 17899

_US_EAST_KEYWORDS = (
    "new york",
    "ashburn",
    "virginia",
    "miami",
    "atlanta",
    "boston",
    "ewr",
    "iad",
    "纽约",
    "美东",
)
_US_GENERIC_KEYWORDS = (
    "united states",
    "america",
    "美国",
    "洛杉矶",
    "圣何塞",
    "西雅图",
    "硅谷",
    "usby",
    "miyaip",
    "los angeles",
    "san jose",
    "california",
    "seattle",
    "dallas",
    "chicago",
)
_US_REGION_RE = re.compile(
    r"(?:^|[\s|_\-\[\(])us(?:by|\b|[\s|_\-\[\]\)\d])", re.IGNORECASE
)
_EAST_ABBREV_RE = re.compile(
    r"(?:^|[\s|_\-\[\(])(?:ny|nj|ewr|iad)(?:[\s|_\-\[\]\)\d]|$)", re.IGNORECASE
)


def _match_any_keyword(haystack: str, keywords: tuple[str, ...]) -> bool:
    return any(k in haystack for k in keywords)


def resolve_browser_env_config(fp: dict, account: dict | None = None) -> dict:
    """Resolve the spoofed timezone/locale for a browser profile.

    The proxy exit node (and ``reg_region``) decide the timezone so the local
    clock matches the exit IP. US matching runs before JP/SG on purpose: relay
    names such as ``USbyJPTT | MiyaIP-2 HY2`` describe a JP hop but a US exit.

    Every account type (including ``域名邮箱``) resolves a real profile so Chrome
    never leaks the host's ``Asia/Shanghai`` clock or ``zh-CN`` UI language.
    """
    fp = fp if isinstance(fp, dict) else {}
    account = account if isinstance(account, dict) else {}

    proxy_node = str(fp.get("proxy_node") or "")
    proxy = str(fp.get("proxy") or "")
    reg_region = str(account.get("reg_region") or "")
    haystack = " ".join((proxy_node, proxy, reg_region)).lower()
    proxy_port = 0
    m_port = re.search(r":(\d{2,5})(?:\D|$)", proxy)
    if m_port:
        try:
            proxy_port = int(m_port.group(1))
        except ValueError:
            proxy_port = 0

    explicit_tz = str(fp.get("timezone") or "").strip()
    explicit_locale = str(fp.get("locale") or "").strip()

    timezone = ""
    locale = ""
    languages: list[str] = []

    # US FIRST so USbyJP* relays never resolve to Asia/Tokyo.
    if _match_any_keyword(haystack, _US_EAST_KEYWORDS) or _EAST_ABBREV_RE.search(haystack):
        timezone = "America/New_York"
        locale = "en-US"
        languages = ["en-US", "en"]
    elif (
        _match_any_keyword(haystack, _US_GENERIC_KEYWORDS)
        or "🇺🇸" in proxy_node
        or "🇺🇸" in proxy
        or _US_REGION_RE.search(haystack)
        or proxy_port == 7897
    ):
        timezone = DEFAULT_TIMEZONE
        locale = DEFAULT_LOCALE
        languages = list(DEFAULT_LANGUAGES)
    elif (
        _match_any_keyword(haystack, ("japan", "tokyo", "osaka", "日本", "东京", "大阪"))
        or "🇯🇵" in proxy_node
        or re.search(r"\bjp\b", haystack)
    ):
        timezone = "Asia/Tokyo"
        locale = "en-US"
        languages = ["en-US", "en"]
    elif (
        _match_any_keyword(haystack, ("singapore", "新加坡", "狮城"))
        or "🇸🇬" in proxy_node
        or re.search(r"\bsg\b", haystack)
    ):
        timezone = "Asia/Singapore"
        locale = "en-US"
        languages = ["en-US", "en"]
    elif (
        _match_any_keyword(haystack, ("taiwan", "台湾", "台北", "新北"))
        or "🇹🇼" in proxy_node
        or re.search(r"\btw\b", haystack)
    ):
        timezone = "Asia/Taipei"
        locale = "zh-TW"
        languages = ["zh-TW", "en-US", "en"]
    elif (
        _match_any_keyword(haystack, ("korea", "seoul", "韩国", "首尔"))
        or "🇰🇷" in proxy_node
        or re.search(r"\bkr\b", haystack)
    ):
        timezone = "Asia/Seoul"
        locale = "en-US"
        languages = ["en-US", "en"]
    elif (
        _match_any_keyword(
            haystack,
            ("united kingdom", "britain", "england", "london", "英国", "伦敦"),
        )
        or "🇬🇧" in proxy_node
        or re.search(r"\b(?:uk|gb)\b", haystack)
    ):
        timezone = "Europe/London"
        locale = "en-GB"
        languages = ["en-GB", "en-US", "en"]
    elif (
        _match_any_keyword(haystack, ("germany", "frankfurt", "德国", "法兰克福"))
        or "🇩🇪" in proxy_node
        or re.search(r"\bde\b", haystack)
    ):
        timezone = "Europe/Berlin"
        locale = "en-US"
        languages = ["en-US", "en"]
    elif (
        _match_any_keyword(haystack, ("canada", "toronto", "vancouver", "加拿大", "多伦多"))
        or "🇨🇦" in proxy_node
        or re.search(r"\bca\b", haystack)
    ):
        timezone = "America/Toronto"
        locale = "en-US"
        languages = ["en-US", "en"]
    elif (
        _match_any_keyword(haystack, ("hong kong", "hongkong", "香港"))
        or "🇭🇰" in proxy_node
        or re.search(r"\bhk\b", haystack)
    ):
        timezone = "Asia/Hong_Kong"
        locale = "en-US"
        languages = ["en-US", "en"]

    # Explicit overrides win over region inference.
    if explicit_tz:
        timezone = explicit_tz
    if explicit_locale:
        locale = explicit_locale

    if not timezone or not locale:
        # Claude and every proxied account default to a US West Coast profile;
        # an unproxied account with no region hint also gets en-US defaults so
        # the extension always has a coherent locale to spoof.
        timezone = timezone or DEFAULT_TIMEZONE
        locale = locale or DEFAULT_LOCALE
        if not languages:
            languages = list(DEFAULT_LANGUAGES)

    if not languages:
        languages = [locale, "en"] if locale != "en" else [locale]

    deduped: list[str] = []
    for lang in languages:
        if lang and lang not in deduped:
            deduped.append(lang)
    if locale and locale not in deduped:
        deduped.insert(0, locale)
    languages = deduped

    accept_languages = ",".join(languages)
    return {
        "timezone": timezone,
        "locale": locale,
        "accept_languages": accept_languages,
        "languages": languages,
        "enabled": True,
    }


_GITHUB_USERNAME_RE = re.compile(r"[^A-Za-z0-9]")


def suggest_github_username(email: str) -> str:
    """Derive a deterministic, GitHub-valid username from a mailbox address.

    The local part is reduced to its alphanumeric characters, truncated to 16
    chars, and combined with a deterministic 4-digit suffix from
    ``sha256(email)`` so sibling mailboxes stay distinct. A leading ``u`` keeps
    the login letter-first, and an empty local part falls back to ``user``.
    The result always fits GitHub's 3-20 char window.
    """
    raw = str(email or "").strip()
    local = raw.split("@", 1)[0]
    cleaned = _GITHUB_USERNAME_RE.sub("", local)[:16]
    if not cleaned:
        cleaned = "user"
    if cleaned[0].isdigit():
        cleaned = ("u" + cleaned)[:16]
    digest = hashlib.sha256(raw.lower().encode("utf-8")).digest()
    suffix = int.from_bytes(digest[:2], "big") % 10000
    return f"{cleaned}{suffix:04d}"


def generate_fingerprint_config(
    account_id: int,
    account_email: str,
    proxy: str = "",
    proxy_node: str = "",
    node_locked: bool = False,
    salt: str = "",
    timezone: str = "",
    locale: str = "",
    accept_languages: str = "",
) -> dict:
    seed_str = f"KeyVault-FP-{account_id}-{account_email.strip().lower()}-{salt}"
    digest = hashlib.sha256(seed_str.encode("utf-8")).digest()

    gpu_idx = int.from_bytes(digest[0:2], "big") % len(GPU_PROFILES)
    cpu_idx = digest[2] % len(CPU_OPTIONS)
    mem_idx = digest[3] % len(MEMORY_OPTIONS)
    scr_idx = digest[4] % len(SCREEN_OPTIONS)
    ver_idx = digest[5] % len(CHROME_VERSIONS)

    vendor, renderer, gpu_short = GPU_PROFILES[gpu_idx]
    cpu_cores = CPU_OPTIONS[cpu_idx]
    memory_gb = MEMORY_OPTIONS[mem_idx]
    scr_w, scr_h = SCREEN_OPTIONS[scr_idx]
    chrome_ver = CHROME_VERSIONS[ver_idx]

    canvas_seed = (int.from_bytes(digest[8:12], "big") % 900000) + 100001
    audio_seed = (int.from_bytes(digest[12:16], "big") % 900000) + 100001

    ua = (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_ver} Safari/537.36"
    )

    fp = {
        "profile_code": f"FP-{int(account_id):03d}",
        "webgl_vendor": vendor,
        "webgl_renderer": renderer,
        "gpu_short": gpu_short,
        "cpu_cores": cpu_cores,
        "memory_gb": memory_gb,
        "screen_width": scr_w,
        "screen_height": scr_h,
        "screen": f"{scr_w}x{scr_h}",
        "chrome_version": chrome_ver,
        "user_agent": ua,
        "canvas_seed": canvas_seed,
        "audio_seed": audio_seed,
        "proxy": (proxy or "").strip(),
        "proxy_node": (proxy_node or "").strip(),
        "node_locked": bool(node_locked),
    }
    # Explicit timezone/locale overrides are only persisted when supplied, so
    # existing fingerprints stay byte-for-byte compatible.
    if timezone:
        fp["timezone"] = str(timezone).strip()
    if locale:
        fp["locale"] = str(locale).strip()
    if accept_languages:
        fp["accept_languages"] = str(accept_languages).strip()
    return fp


def _build_autologin_js(fp: dict, account: dict) -> str:
    account_type = (account.get("account_type") or "ChatGPT").strip()
    is_domain_email = account_type == "域名邮箱"
    auth_payload = json.dumps(
        {
            "accountType": account_type,
            "isDomainEmail": is_domain_email,
            "mailUrl": "https://mail.ggb6.com/",
            "account": (account.get("account") or "").strip(),
            "maskedAccount": (account.get("masked_account") or account.get("account") or "").strip(),
            "password": (account.get("password") or "").strip(),
            "suggestedUsername": suggest_github_username(
                (account.get("account") or "").strip()
            ),
            "recoveryEmail": (account.get("recovery_email") or "").strip(),
            "recoveryPassword": str(
                account.get("recovery_password")
                or (account.get("custom_fields") or {}).get("recovery_password")
                or ""
            ).strip(),
            "secretKey": (account.get("secret_key") or "").strip(),
            "initialSecretKey": (account.get("initial_secret_key") or "").strip(),
            "twofaModified": bool(account.get("twofa_modified")),
            "profileCode": fp.get("profile_code", "FP-000"),
            "proxyNode": (fp.get("proxy_node") or "").strip(),
        },
        ensure_ascii=False,
    )

    return f"""(() => {{
  const AUTH = {auth_payload};

  // --- Pure synchronous RFC 6238 TOTP (SHA-1 HMAC + Base32) ---
  function base32ToBytes(b32) {{
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
    const cleaned = String(b32 || '').toUpperCase().replace(/[^A-Z2-7]/g, '');
    if (!cleaned) return new Uint8Array(0);
    let bits = 0, value = 0;
    const out = [];
    for (let i = 0; i < cleaned.length; i++) {{
      const idx = alphabet.indexOf(cleaned[i]);
      if (idx === -1) continue;
      value = (value << 5) | idx;
      bits += 5;
      if (bits >= 8) {{
        out.push((value >>> (bits - 8)) & 0xff);
        bits -= 8;
      }}
    }}
    return new Uint8Array(out);
  }}

  function sha1Bytes(msgBytes) {{
    function rotl(n, s) {{ return ((n << s) | (n >>> (32 - s))) >>> 0; }}
    const ml = msgBytes.length;
    const bitLenHi = Math.floor((ml * 8) / 0x100000000);
    const bitLenLo = (ml * 8) >>> 0;
    const padLen = ((ml + 8) >> 6 << 6) + 64;
    const buf = new Uint8Array(padLen);
    buf.set(msgBytes);
    buf[ml] = 0x80;
    const dv = new DataView(buf.buffer);
    dv.setUint32(padLen - 8, bitLenHi, false);
    dv.setUint32(padLen - 4, bitLenLo, false);

    let h0 = 0x67452301, h1 = 0xefcdab89, h2 = 0x98badcfe, h3 = 0x10325476, h4 = 0xc3d2e1f0;
    const w = new Uint32Array(80);

    for (let off = 0; off < padLen; off += 64) {{
      for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4, false);
      for (let i = 16; i < 80; i++) w[i] = rotl(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
      let a = h0, b = h1, c = h2, d = h3, e = h4;
      for (let i = 0; i < 80; i++) {{
        let f, k;
        if (i < 20) {{ f = (b & c) | (~b & d); k = 0x5a827999; }}
        else if (i < 40) {{ f = b ^ c ^ d; k = 0x6ed9eba1; }}
        else if (i < 60) {{ f = (b & c) | (b & d) | (c & d); k = 0x8f1bbcdc; }}
        else {{ f = b ^ c ^ d; k = 0xca62c1d6; }}
        const temp = (rotl(a, 5) + f + e + k + w[i]) >>> 0;
        e = d; d = c; c = rotl(b, 30); b = a; a = temp;
      }}
      h0 = (h0 + a) >>> 0;
      h1 = (h1 + b) >>> 0;
      h2 = (h2 + c) >>> 0;
      h3 = (h3 + d) >>> 0;
      h4 = (h4 + e) >>> 0;
    }}
    const out = new Uint8Array(20);
    const outDv = new DataView(out.buffer);
    outDv.setUint32(0, h0, false);
    outDv.setUint32(4, h1, false);
    outDv.setUint32(8, h2, false);
    outDv.setUint32(12, h3, false);
    outDv.setUint32(16, h4, false);
    return out;
  }}

  function hmacSha1(keyBytes, msgBytes) {{
    let key = keyBytes;
    if (key.length > 64) key = sha1Bytes(key);
    const ipad = new Uint8Array(64);
    const opad = new Uint8Array(64);
    for (let i = 0; i < 64; i++) {{
      const b = i < key.length ? key[i] : 0;
      ipad[i] = b ^ 0x36;
      opad[i] = b ^ 0x5c;
    }}
    const innerInput = new Uint8Array(64 + msgBytes.length);
    innerInput.set(ipad, 0);
    innerInput.set(msgBytes, 64);
    const innerHash = sha1Bytes(innerInput);

    const outerInput = new Uint8Array(64 + 20);
    outerInput.set(opad, 0);
    outerInput.set(innerHash, 64);
    return sha1Bytes(outerInput);
  }}

  function computeTotp(secret, nowMs) {{
    const key = base32ToBytes(secret);
    if (!key.length) return '';
    const epochSec = Math.floor((nowMs || Date.now()) / 1000);
    const counter = Math.floor(epochSec / 30);
    const msg = new Uint8Array(8);
    const dv = new DataView(msg.buffer);
    dv.setUint32(0, Math.floor(counter / 0x100000000), false);
    dv.setUint32(4, counter >>> 0, false);
    const digest = hmacSha1(key, msg);
    const offset = digest[19] & 0x0f;
    const binCode =
      ((digest[offset] & 0x7f) << 24) |
      ((digest[offset + 1] & 0xff) << 16) |
      ((digest[offset + 2] & 0xff) << 8) |
      (digest[offset + 3] & 0xff);
    return String(binCode % 1000000).padStart(6, '0');
  }}

  if (typeof globalThis !== 'undefined') {{
    globalThis.__kvComputeTotp = computeTotp;
  }}
  if (typeof document === 'undefined' || typeof window === 'undefined') return;
  try {{
    if (window !== window.top) return;
  }} catch (_) {{
    return;
  }}
  if (!/^https?:$/i.test(location.protocol || '')) return;
  if (window.__kv_autologin_active) {{
    if (typeof window.__kv_ensure_mounted === 'function') {{
      try {{ window.__kv_ensure_mounted(); }} catch (_) {{}}
    }}
    return;
  }}
  window.__kv_autologin_active = true;

  function safeGetSession(k) {{
    try {{ return sessionStorage.getItem(k); }} catch (_) {{ return null; }}
  }}
  function safeSetSession(k, v) {{
    try {{ sessionStorage.setItem(k, v); }} catch (_) {{}}
  }}
  function safeResetStepCooldowns() {{
    try {{
      const keys = [];
      for (let i = 0; i < sessionStorage.length; i++) {{
        const k = sessionStorage.key(i);
        if (k && k.startsWith('kv_step_')) keys.push(k);
      }}
      keys.forEach((k) => sessionStorage.removeItem(k));
    }} catch (_) {{}}
  }}

  // --- DOM Helpers for React / Google Wiz ---
  // Default silent (manual click-to-fill) to avoid synthetic auto-submit anti-bot signals.
  let autoEnabled = safeGetSession('kv_autologin_enabled') === '1';
  let capsuleHidden = safeGetSession('kv_capsule_hidden') === '1';
  let statusText = autoEnabled
    ? '⚡ 正在检测登录表单...'
    : '🛡️ 静默防风控模式（点下方按钮填入 · Alt+K 隐藏）';
  let statusUiUpdater = null;
  let lastFocusedInput = null;
  let hotkeyBound = false;

  document.addEventListener('focusin', (e) => {{
    if (e.target && e.target.tagName === 'INPUT') {{
      lastFocusedInput = e.target;
    }}
  }}, true);

  function setStatus(msg) {{
    statusText = msg;
    if (statusUiUpdater) statusUiUpdater();
  }}

  function isVisible(el) {{
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 8 && rect.height > 8;
  }}

  function findFirstVisible(selectors) {{
    for (const sel of selectors) {{
      const list = document.querySelectorAll(sel);
      for (const el of list) {{
        if (isVisible(el) && !el.disabled && !el.readOnly) return el;
      }}
    }}
    return null;
  }}

  function findFirstVisibleEmpty(selectors) {{
    for (const sel of selectors) {{
      const list = document.querySelectorAll(sel);
      for (const el of list) {{
        if (isVisible(el) && !el.disabled && !el.readOnly && !el.value) return el;
      }}
    }}
    return null;
  }}

  function setNativeInputValue(input, val) {{
    if (!input || val === undefined || val === null) return false;
    input.focus();
    const proto = window.HTMLInputElement ? window.HTMLInputElement.prototype : null;
    const desc = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
    if (desc && desc.set) {{
      desc.set.call(input, String(val));
    }} else {{
      input.value = String(val);
    }}
    input.dispatchEvent(new KeyboardEvent('keydown', {{ bubbles: true, key: 'a' }}));
    try {{
      input.dispatchEvent(new InputEvent('input', {{ bubbles: true, cancelable: true, inputType: 'insertText', data: String(val) }}));
    }} catch (_) {{
      input.dispatchEvent(new Event('input', {{ bubbles: true }}));
    }}
    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
    input.dispatchEvent(new KeyboardEvent('keyup', {{ bubbles: true, key: 'a' }}));
    return true;
  }}

  function clickButton(btn) {{
    if (!btn) return false;
    try {{
      btn.focus();
      btn.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true, cancelable: true }}));
      btn.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true, cancelable: true }}));
      btn.click();
      return true;
    }} catch (_) {{
      return false;
    }}
  }}

  function canRunStep(stepKey, cooldownMs = 4200) {{
    const key = 'kv_step_' + stepKey;
    const last = parseInt(safeGetSession(key) || '0', 10);
    const now = Date.now();
    if (now - last < cooldownMs) return false;
    safeSetSession(key, String(now));
    return true;
  }}

  function resetStepCooldowns() {{
    safeResetStepCooldowns();
  }}

  function findOpenAISubmitButton(contextInput) {{
    const blockedWords = /google|microsoft|apple|phone|手机|电话|passkey|通行密钥|forgot|忘记|reset|sign up|注册|免费注册|back|返回|resend|重新发送|try another|其他方式|条款|隐私/i;
    const continueWords = /^(continue|继续|next|下一步|verify|验证|submit|提交|log in|登录)$/i;

    // 1. Always prioritize submit button inside the input's own <form>
    const form = contextInput && contextInput.closest ? contextInput.closest('form') : null;
    if (form) {{
      const formBtns = Array.from(form.querySelectorAll('button[type="submit"], button[name="intent"], button[name="action"], button'));
      for (const btn of formBtns) {{
        if (!isVisible(btn) || btn.disabled) continue;
        const txt = (btn.innerText || btn.textContent || '').trim();
        if (blockedWords.test(txt)) continue;
        if (btn.type === 'submit' && txt.length > 0) return btn;
        if (continueWords.test(txt)) return btn;
      }}
      for (const btn of formBtns) {{
        if (!isVisible(btn) || btn.disabled) continue;
        const txt = (btn.innerText || btn.textContent || '').trim();
        if (!blockedWords.test(txt) && btn.type === 'submit') return btn;
      }}
    }}

    // 2. Next prioritize explicit submit buttons across the page with "Continue / 继续"
    const submitBtns = Array.from(document.querySelectorAll('button[type="submit"], button[name="intent"], button[name="action"], button[data-dd-action-name="Continue"]'));
    for (const btn of submitBtns) {{
      if (!isVisible(btn) || btn.disabled) continue;
      const txt = (btn.innerText || btn.textContent || '').trim();
      if (blockedWords.test(txt)) continue;
      if (continueWords.test(txt)) return btn;
    }}

    // 3. Fallback to any visible button with "Continue / 继续 / 下一步" (excluding top header "登录" button if type="button")
    const allBtns = Array.from(document.querySelectorAll('button'));
    for (const btn of allBtns) {{
      if (!isVisible(btn) || btn.disabled) continue;
      const txt = (btn.innerText || btn.textContent || '').trim();
      if (blockedWords.test(txt)) continue;
      if (/^(continue|继续|next|下一步|verify|验证|submit|提交)$/i.test(txt)) return btn;
    }}
    return null;
  }}

  function fillOtpCodeOnPage(code) {{
    if (!code) return false;
    // Check if there are 6 individual 1-digit inputs
    const singleDigitBoxes = Array.from(document.querySelectorAll('input[maxlength="1"], input[data-index]')).filter(
      (el) => isVisible(el) && !el.disabled && !el.readOnly && el.type !== 'password' && el.type !== 'email'
    );
    if (singleDigitBoxes.length >= 6) {{
      for (let i = 0; i < 6; i++) {{
        setNativeInputValue(singleDigitBoxes[i], code[i]);
      }}
      return true;
    }}

    const otpInput = findFirstVisible([
      'input#totpPin',
      'input[name="totpPin"]',
      'input[autocomplete="one-time-code"]',
      'input[name="code"]',
      'input[name="otp"]',
      'input[id*="code"]',
      'input[id*="totp"]',
      'input[inputmode="numeric"]',
      'input[type="tel"]',
    ]);
    if (otpInput) {{
      return setNativeInputValue(otpInput, code);
    }}
    return false;
  }}

  function getActiveTotpSecret() {{
    return AUTH.secretKey || AUTH.initialSecretKey || '';
  }}

  function scheduleTotpFillAndSubmit(submitFinder) {{
    const secret = getActiveTotpSecret();
    if (!secret) {{
      setStatus('⚠️ 此账号未配置 2FA 密钥');
      return;
    }}
    const nowSec = Math.floor(Date.now() / 1000);
    const rem = 30 - (nowSec % 30);
    const delayMs = rem <= 2 ? (rem + 0.5) * 1000 : 250;
    if (rem <= 2) {{
      setStatus('⏳ 当前 2FA 即将刷新，等待新验证码...');
    }}
    setTimeout(() => {{
      const freshCode = computeTotp(secret, Date.now());
      if (fillOtpCodeOnPage(freshCode)) {{
        setStatus('⚡ 已填入 2FA 验证码 (' + freshCode + ')，正在提交...');
        setTimeout(() => {{
          const btn = submitFinder();
          if (btn) clickButton(btn);
        }}, 420);
      }}
    }}, delayMs);
  }}

  // --- Platform Specific Auto-Login Flows ---
  function runChatGPTFlow(force = false) {{
    const host = location.hostname.toLowerCase();

    // 1. Check for OpenAI 2FA / OTP step first
    const otpInput = findFirstVisible([
      'input[autocomplete="one-time-code"]',
      'input[name="code"]',
      'input[name="otp"]',
      'input[id*="code"]',
    ]);
    if (otpInput && otpInput.type !== 'email' && otpInput.type !== 'password') {{
      const pageText = (document.body ? document.body.innerText : '') || '';
      const isEmailInboxCode = /check your inbox|查看您的收件箱|sent a code to your email|已向您的电子邮件发送/i.test(pageText)
        && !/authenticator|身份验证器|one-time|一次性|two-step|两步验证/i.test(pageText);
      if (isEmailInboxCode && !force) {{
        setStatus('📧 检测到邮箱验证码步骤，请查收邮箱或手动点击[填2FA]');
        return;
      }}
      if (getActiveTotpSecret() && (force || canRunStep('openai_totp', 4200))) {{
        scheduleTotpFillAndSubmit(() => findOpenAISubmitButton(otpInput));
        return;
      }}
    }}

    // 2. Check for OpenAI Password step
    const pwdInput = findFirstVisible([
      'input[type="password"][name="current-password"]',
      'input[type="password"][name="password"]',
      'input[type="password"]#password',
      'input[type="password"][autocomplete*="current-password"]',
      'input[type="password"]',
    ]);
    const emailInput = findFirstVisible([
      'input#mobile-auth-email',
      'input[name="login_hint"][type="email"]',
      'input[name="email"]:not([type="hidden"])',
      'input#email-input',
      'input[name="username"]:not([type="hidden"])',
      'input[type="email"]',
      'input[autocomplete="email"]',
      'input[autocomplete="username"]',
    ]);

    if (pwdInput && AUTH.password) {{
      if (force || canRunStep('openai_pwd', 4000)) {{
        if (emailInput && !emailInput.value && AUTH.account) {{
          setNativeInputValue(emailInput, AUTH.account);
        }}
        setNativeInputValue(pwdInput, AUTH.password);
        setStatus('⚡ 已自动填入密码，正在登录...');
        setTimeout(() => {{
          const btn = findOpenAISubmitButton(pwdInput);
          if (btn) clickButton(btn);
        }}, 400);
      }}
      return;
    }}

    // 3. Check for OpenAI / ChatGPT Email step (works both on auth.openai.com and inline modal on chatgpt.com)
    if (emailInput && AUTH.account) {{
      if (force || canRunStep('openai_email', 4000)) {{
        setNativeInputValue(emailInput, AUTH.account);
        setStatus('⚡ 已自动填入账号邮箱，正在点击继续...');
        setTimeout(() => {{
          const btn = findOpenAISubmitButton(emailInput);
          if (btn) clickButton(btn);
        }}, 400);
      }}
      return;
    }}

    // 4. On chatgpt.com when no email/password/OTP input is visible yet
    if (host.includes('chatgpt.com')) {{
      const loginBtn = findFirstVisible([
        'button[data-testid="login-button"]',
        'a[data-testid="login-button"]',
        'button[data-testid="welcome-login-button"]',
      ]) || Array.from(document.querySelectorAll('button, a')).find((el) => {{
        if (!isVisible(el)) return false;
        const t = (el.innerText || el.textContent || '').trim();
        return /^(log in|登录)$/i.test(t);
      }});

      if (!loginBtn) {{
        setStatus('✅ 已成功登录 ChatGPT 主页');
        autoMinimizeCapsule();
        return;
      }}

      if (force || canRunStep('chatgpt_landing_login', 4500)) {{
        setStatus('⚡ 检测到未登录，正在点击「登录」按钮...');
        setTimeout(() => clickButton(loginBtn), 450);
        return;
      }}
    }}
  }}

  function findGoogleNextButton(primarySelector) {{
    if (primarySelector) {{
      const el = document.querySelector(primarySelector + ' button') || document.querySelector(primarySelector);
      if (el && isVisible(el)) return el;
    }}
    const candidates = Array.from(document.querySelectorAll('button[jsname="LgbsSe"], #identifierNext button, #passwordNext button, #totpNext button, button[type="submit"], button'));
    for (const btn of candidates) {{
      if (!isVisible(btn) || btn.disabled) continue;
      const txt = (btn.innerText || btn.textContent || '').trim();
      if (/^(next|下一步|继续|continue|verify|验证|确认|confirm)$/i.test(txt)) return btn;
    }}
    return null;
  }}

  function runGoogleFlow(force = false) {{
    const host = location.hostname.toLowerCase();
    const path = location.pathname.toLowerCase();

    if (host.includes('myaccount.google.com') && !path.includes('signin')) {{
      setStatus('✅ 已成功登录 Google 账号主页');
      autoMinimizeCapsule();
      return;
    }}

    // 0. Account Chooser screen (if present)
    if (AUTH.account) {{
      const chooserRow = document.querySelector(`div[data-identifier="${{CSS.escape(AUTH.account)}}"]`);
      if (chooserRow && isVisible(chooserRow) && (force || canRunStep('google_chooser', 4200))) {{
        setStatus('⚡ 已在列表中选择当前 Google 账号...');
        setTimeout(() => clickButton(chooserRow), 350);
        return;
      }}
    }}

    // 1. Google 2FA TOTP step
    const totpInput = findFirstVisible([
      'input#totpPin',
      'input[name="totpPin"]',
      'input[type="tel"][id*="totp"]',
      'input[autocomplete="one-time-code"]',
    ]);
    if (totpInput && getActiveTotpSecret()) {{
      if (force || canRunStep('google_totp', 4800)) {{
        scheduleTotpFillAndSubmit(() => findGoogleNextButton('#totpNext'));
      }}
      return;
    }}

    // 2. Google Recovery Email Verification step
    const recInput = findFirstVisible([
      'input#knowledge-preregistered-email-response',
      'input[name="knowledgePreregisteredEmailResponse"]',
    ]);
    if (recInput && AUTH.recoveryEmail) {{
      if (force || canRunStep('google_recovery_email', 4500)) {{
        setNativeInputValue(recInput, AUTH.recoveryEmail);
        setStatus('⚡ 已自动填入辅助邮箱，正在验证...');
        setTimeout(() => {{
          const btn = findGoogleNextButton(null);
          if (btn) clickButton(btn);
        }}, 450);
      }}
      return;
    }}

    // Also check if Google is showing the "Choose verification method" list with "Confirm your recovery email" (data-challengetype="12") or Authenticator (data-challengetype="6")
    const totpChallengeOption = document.querySelector('div[data-challengetype="6"], li div[data-challengetype="6"]');
    if (totpChallengeOption && isVisible(totpChallengeOption) && getActiveTotpSecret() && (force || canRunStep('google_pick_totp', 4200))) {{
      setStatus('⚡ 自动选择 Google 身份验证器 2FA 验证...');
      setTimeout(() => clickButton(totpChallengeOption), 400);
      return;
    }}
    const recChallengeOption = document.querySelector('div[data-challengetype="12"], li div[data-challengetype="12"]');
    if (recChallengeOption && isVisible(recChallengeOption) && AUTH.recoveryEmail && (force || canRunStep('google_pick_rec', 4200))) {{
      setStatus('⚡ 自动选择确认辅助邮箱验证...');
      setTimeout(() => clickButton(recChallengeOption), 400);
      return;
    }}

    // 3. Google Password step
    const pwdInput = findFirstVisible([
      'input[type="password"][name="Passwd"]',
      'input[type="password"][name="password"]',
      '#password input[type="password"]',
    ]);
    if (pwdInput && AUTH.password) {{
      if (force || canRunStep('google_pwd', 4500)) {{
        setNativeInputValue(pwdInput, AUTH.password);
        setStatus('⚡ 已自动填入 Google 密码，正在下一步...');
        setTimeout(() => {{
          const btn = findGoogleNextButton('#passwordNext');
          if (btn) clickButton(btn);
        }}, 450);
      }}
      return;
    }}

    // 4. Google Identifier (Email) step
    const emailInput = findFirstVisible([
      'input#identifierId',
      'input[type="email"][name="identifier"]',
    ]);
    if (emailInput && AUTH.account) {{
      if (force || canRunStep('google_email', 4500)) {{
        setNativeInputValue(emailInput, AUTH.account);
        setStatus('⚡ 已自动填入 Google 邮箱，正在下一步...');
        setTimeout(() => {{
          const btn = findGoogleNextButton('#identifierNext');
          if (btn) clickButton(btn);
        }}, 450);
      }}
      return;
    }}
  }}

  function runClaudeFlow(force = false) {{
    const host = location.hostname.toLowerCase();
    const path = location.pathname.toLowerCase();

    // Already inside a Claude workspace: the composer only renders when signed in.
    const composer = document.querySelector(
      'div[contenteditable="true"], [data-testid="chat-input"], [data-testid="chat-input-container"]'
    );
    const emailInput = findFirstVisible([
      'input[name="email"]',
      'input[type="email"]',
      'input#email',
      'input[autocomplete="email"]',
    ]);
    const loggedInPath = path.startsWith('/new') || path.startsWith('/chat/');
    if (composer && isVisible(composer) && !emailInput && (loggedInPath || !document.querySelector('form'))) {{
      setStatus('✅ 已成功登录 Claude');
      autoMinimizeCapsule();
      return;
    }}

    if (emailInput && AUTH.account) {{
      if (force || canRunStep('claude_email', 4500)) {{
        setNativeInputValue(emailInput, AUTH.account);
        setStatus('⚡ 已自动填入 Claude 登录邮箱，请完成人机验证/邮箱验证码或点击 Continue 继续');
      }}
      return;
    }}

    if (host.includes('claude.ai') && !loggedInPath && !composer) {{
      if (force || canRunStep('claude_landing', 5000)) {{
        const loginBtn = findFirstVisible([
          'a[href*="/login"]',
          'button[data-testid="login-button"]',
        ]) || Array.from(document.querySelectorAll('a, button')).find((el) => {{
          if (!isVisible(el)) return false;
          const t = (el.innerText || el.textContent || '').trim();
          return /^(log in|sign in|登录)$/i.test(t);
        }});
        if (loginBtn) {{
          setStatus('⚡ 检测到未登录 Claude，正在打开登录页...');
          setTimeout(() => clickButton(loginBtn), 400);
          return;
        }}
      }}
    }}

    if (emailInput && !AUTH.account) {{
      setStatus('⚠️ 此账号未配置登录邮箱');
    }}
  }}

  function runRoundcubeMailFlow(force = false) {{
    const host = location.hostname.toLowerCase();
    const userEl = document.querySelector('input#rcmloginuser, input[name="_user"]');
    const pwdEl = document.querySelector('input#rcmloginpwd, input[name="_pass"]');
    const isMailHost = host.includes('mail.ggb6.com');
    const hasLoginForm = !!document.querySelector('form#login-form');

    if (!isMailHost && !hasLoginForm && !userEl) return;

    // Already logged in: Roundcube marks the body with task-mail / renders the mailbox layout
    const loggedIn = (document.body && document.body.classList.contains('task-mail'))
      || (!!document.querySelector('#layout-content') && !document.querySelector('#login-form'));
    if (loggedIn) {{
      setStatus('✅ 已成功登录 GGB6 邮箱收件箱');
      autoMinimizeCapsule();
      return;
    }}

    if (userEl && pwdEl && AUTH.account && AUTH.password) {{
      if (force || canRunStep('roundcube_login', 4000)) {{
        setNativeInputValue(userEl, AUTH.account);
        setNativeInputValue(pwdEl, AUTH.password);
        setStatus('⚡ 已自动填入邮箱账号与密码，正在登录...');
        setTimeout(() => {{
          const btn = findFirstVisible(['button#rcmloginsubmit', 'button[type="submit"]', 'form#login-form button']);
          if (btn) clickButton(btn);
          else {{
            const form = document.querySelector('form#login-form');
            if (form) form.submit();
          }}
        }}, 350);
      }}
      return;
    }}

    if (isMailHost) {{
      if (!AUTH.account || !AUTH.password) {{
        setStatus('⚠️ 此账号未配置邮箱账号或密码');
      }} else if (!userEl && !loggedIn) {{
        setStatus('⏳ 正在等待 GGB6 邮箱登录表单加载...');
      }}
    }}
  }}

  function runManualFormFill() {{
    // Strictly user-initiated (button click). Never scheduled automatically:
    // GitHub's DataDome/Arkose checks must see a real human action.
    const host = location.hostname.toLowerCase();
    const path = (location.pathname || '/').replace(/\\/+$/, '') || '/';
    if (host.includes('mail.ggb6.com')) {{
      runRoundcubeMailFlow(true);
      return;
    }}
    if (!host.includes('github.com')) {{
      setStatus('⚠️ 当前页面不支持一键填表，请打开 GitHub 或网页邮箱');
      return;
    }}

    if (path === '/' || path === '') {{
      // Warm first-party transition: fill the hero email box on github.com and
      // submit its "Sign up for GitHub" form so /signup is reached with a
      // normal top-level navigation instead of a cold direct request.
      const heroInput = findFirstVisible([
        'input#hero_user_email',
        'form[action*="/signup"] input[type="email"]',
        'input[name="user_email"]'
      ]);
      if (!heroInput) {{
        setStatus('⚠️ 未找到 GitHub 首页邮箱输入框');
        return;
      }}
      setNativeInputValue(heroInput, AUTH.account);
      try {{
        heroInput.style.outline = '2px solid #22c55e';
        heroInput.style.outlineOffset = '1px';
      }} catch (_) {{}}
      const heroForm = heroInput.closest('form[action*="/signup"]')
        || document.querySelector('form[action*="/signup"]');
      setStatus('⚡ 已填入 GitHub 首页邮箱，正在前往注册页...');
      setTimeout(() => {{
        try {{
          if (heroForm && typeof heroForm.requestSubmit === 'function') {{
            heroForm.requestSubmit();
          }} else if (heroForm) {{
            heroForm.submit();
          }} else {{
            location.href = 'https://github.com/signup?user_email=' + encodeURIComponent(AUTH.account);
          }}
        }} catch (_) {{
          location.href = 'https://github.com/signup?user_email=' + encodeURIComponent(AUTH.account);
        }}
      }}, 320);
      return;
    }}

    if (path === '/signup') {{
      // Fill only the currently visible EMPTY fields. The final "Create
      // account" button is deliberately never clicked: the human completes
      // the captcha/verification step.
      const emailInput = findFirstVisibleEmpty([
        'input#email',
        'input[name="user[email]"]',
        'input[type="email"]'
      ]);
      const pwdInput = findFirstVisibleEmpty([
        'input#password',
        'input[name="user[password]"]',
        'input[type="password"]'
      ]);
      const loginInput = findFirstVisibleEmpty([
        'input#login',
        'input[name="user[login]"]'
      ]);
      let filled = 0;
      if (emailInput && setNativeInputValue(emailInput, AUTH.account)) filled++;
      if (pwdInput && setNativeInputValue(pwdInput, AUTH.password)) filled++;
      if (loginInput && setNativeInputValue(loginInput, AUTH.suggestedUsername)) filled++;
      setStatus(filled
        ? '✅ 已填入 GitHub 注册表单（' + filled + ' 项），请手动完成验证后提交'
        : '⚠️ 注册表单未找到可填写的空字段');
      return;
    }}

    setStatus('⚠️ 请在 GitHub 首页或 /signup 页面使用一键填表');
  }}

  function runAutoLoginTick(force = false) {{
    if (!autoEnabled && !force) return;
    if (force) resetStepCooldowns();
    const host = location.hostname.toLowerCase();
    if (host.includes('google.com')) {{
      runGoogleFlow(force);
    }} else if (host.includes('chatgpt.com') || host.includes('openai.com')) {{
      runChatGPTFlow(force);
    }} else if (host.includes('claude.ai') || host.includes('anthropic.com')) {{
      runClaudeFlow(force);
    }} else if (host.includes('mail.ggb6.com') || document.querySelector('#rcmloginuser')) {{
      runRoundcubeMailFlow(force);
    }}
  }}

  // --- Closed Shadow DOM Floating Auto-Login Capsule ---
  let minimized = false;
  let minimizeTimer = null;
  let domainPanelKey = '';

  function autoMinimizeCapsule() {{
    if (minimizeTimer) return;
    minimizeTimer = setTimeout(() => {{
      minimized = true;
      if (statusUiUpdater) statusUiUpdater();
    }}, 2200);
  }}

  function bindToggleHotkey() {{
    if (hotkeyBound) return;
    hotkeyBound = true;
    document.addEventListener('keydown', (e) => {{
      if (e.altKey && (e.key === 'k' || e.key === 'K' || e.code === 'KeyK')) {{
        e.preventDefault();
        capsuleHidden = !capsuleHidden;
        safeSetSession('kv_capsule_hidden', capsuleHidden ? '1' : '0');
        const existing = document.getElementById('__kv_autologin_host');
        if (capsuleHidden) {{
          if (existing) existing.remove();
        }} else {{
          mountCapsule();
        }}
      }}
    }}, true);
  }}

  function mountCapsule() {{
    bindToggleHotkey();
    const existingHost = document.getElementById('__kv_autologin_host');
    if (capsuleHidden) {{
      if (existingHost) existingHost.remove();
      return;
    }}
    const rootTarget = document.documentElement || document.body;
    if (!rootTarget || existingHost) return;
    const hostEl = document.createElement('div');
    hostEl.id = '__kv_autologin_host';
    hostEl.style.cssText = 'all: initial; position: fixed; top: 14px; right: 14px; z-index: 2147483647; font-family: system-ui, -apple-system, "Segoe UI", sans-serif;';
    const shadow = hostEl.attachShadow({{ mode: 'closed' }});

    const style = document.createElement('style');
    style.textContent = `
      .kv-box {{
        background: rgba(15, 23, 42, 0.94);
        backdrop-filter: blur(10px);
        color: #f8fafc;
        border: 1px solid rgba(56, 189, 248, 0.35);
        border-radius: 12px;
        padding: 10px 12px;
        width: 270px;
        box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45);
        font-size: 12px;
        line-height: 1.4;
        user-select: none;
      }}
      .kv-box.minimized {{
        width: auto;
        padding: 6px 11px;
        border-radius: 999px;
        cursor: pointer;
        display: flex;
        align-items: center;
        gap: 6px;
      }}
      .kv-head {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 6px;
        padding-bottom: 5px;
        border-bottom: 1px solid rgba(148, 163, 184, 0.18);
      }}
      .kv-title {{
        font-weight: 700;
        color: #38bdf8;
        font-size: 12px;
        display: flex;
        align-items: center;
        gap: 5px;
      }}
      .kv-actions-top {{
        display: flex;
        align-items: center;
        gap: 5px;
      }}
      .kv-mini-btn {{
        background: rgba(255,255,255,0.08);
        color: #cbd5e1;
        border: 1px solid rgba(255,255,255,0.14);
        border-radius: 5px;
        padding: 2px 6px;
        font-size: 11px;
        cursor: pointer;
      }}
      .kv-mini-btn.active {{
        background: rgba(16, 185, 129, 0.22);
        color: #34d399;
        border-color: rgba(16, 185, 129, 0.45);
      }}
      .kv-row {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 5px;
        color: #cbd5e1;
        font-size: 11.5px;
      }}
      .kv-totp {{
        font-family: ui-monospace, Consolas, monospace;
        font-weight: 700;
        color: #34d399;
        background: rgba(16, 185, 129, 0.14);
        padding: 1px 6px;
        border-radius: 5px;
        cursor: pointer;
      }}
      .kv-status {{
        background: rgba(30, 41, 59, 0.9);
        color: #e2e8f0;
        padding: 5px 8px;
        border-radius: 6px;
        font-size: 11px;
        margin: 6px 0;
        border-left: 3px solid #38bdf8;
      }}
      .kv-btns {{
        display: flex;
        flex-wrap: wrap;
        gap: 5px;
        margin-top: 6px;
      }}
      .kv-btn {{
        flex: 1 1 auto;
        background: rgba(56, 189, 248, 0.15);
        color: #bae6fd;
        border: 1px solid rgba(56, 189, 248, 0.32);
        border-radius: 6px;
        padding: 4px 7px;
        font-size: 11px;
        font-weight: 600;
        cursor: pointer;
        text-align: center;
      }}
      .kv-btn:hover {{
        background: rgba(56, 189, 248, 0.28);
        color: #fff;
      }}
      .kv-btn.primary {{
        background: #0284c7;
        color: #fff;
        border-color: #38bdf8;
      }}
      .kv-field {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 6px;
        background: rgba(30, 41, 59, 0.85);
        border: 1px solid rgba(148, 163, 184, 0.18);
        border-radius: 6px;
        padding: 4px 6px;
        margin-bottom: 5px;
      }}
      .kv-field-label {{
        color: #94a3b8;
        font-size: 10px;
        min-width: 34px;
      }}
      .kv-field-value {{
        flex: 1 1 auto;
        color: #e2e8f0;
        font-size: 11px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        user-select: text;
      }}
      .kv-copy-btn {{
        flex: 0 0 auto;
        background: rgba(56, 189, 248, 0.16);
        color: #7dd3fc;
        border: 1px solid rgba(56, 189, 248, 0.35);
        border-radius: 5px;
        padding: 2px 6px;
        font-size: 10px;
        cursor: pointer;
      }}
      .kv-copy-btn:hover {{
        background: rgba(56, 189, 248, 0.3);
        color: #fff;
      }}
      .kv-nav {{
        display: flex;
        flex-direction: column;
        gap: 4px;
        margin-top: 5px;
      }}
      .kv-nav a {{
        display: block;
        background: rgba(16, 185, 129, 0.14);
        color: #6ee7b7;
        border: 1px solid rgba(16, 185, 129, 0.35);
        border-radius: 6px;
        padding: 4px 7px;
        font-size: 11px;
        font-weight: 600;
        text-align: center;
        text-decoration: none;
      }}
      .kv-nav a:hover {{
        background: rgba(16, 185, 129, 0.28);
        color: #fff;
      }}
    `;

    const container = document.createElement('div');
    container.className = 'kv-box';
    shadow.appendChild(style);
    shadow.appendChild(container);
    rootTarget.appendChild(hostEl);

    function fillIntoFocusedOrTarget(val, label, fallbackSelectors) {{
      if (!val) {{
        setStatus('⚠️ 此账号无' + label + '数据');
        return;
      }}
      let target = (lastFocusedInput && isVisible(lastFocusedInput)) ? lastFocusedInput : null;
      if (!target && fallbackSelectors) target = findFirstVisible(fallbackSelectors);
      if (target) {{
        setNativeInputValue(target, val);
        setStatus('✅ 已填入' + label);
      }} else {{
        navigator.clipboard?.writeText(val).catch(() => {{}});
        setStatus('📋 未找到输入框，已复制' + label + '到剪贴板');
      }}
    }}

    function mkEl(tag, className, text) {{
      const el = document.createElement(tag);
      if (className) el.className = className;
      if (text !== undefined && text !== null) el.textContent = String(text);
      return el;
    }}

    function copyText(val, label) {{
      if (!val) {{
        setStatus('⚠️ 此账号无' + label + '数据');
        return;
      }}
      const done = () => setStatus('📋 已复制' + label);
      try {{
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          navigator.clipboard.writeText(val).then(done).catch(() => {{
            copyViaTextarea(val);
            done();
          }});
          return;
        }}
      }} catch (_) {{}}
      copyViaTextarea(val);
      done();
    }}

    function copyViaTextarea(val) {{
      try {{
        const ta = document.createElement('textarea');
        ta.value = val;
        ta.style.cssText = 'position:fixed;top:-1000px;left:-1000px;opacity:0;';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }} catch (_) {{}}
    }}

    function buildDomainEmailPanel() {{
      const panel = document.createElement('div');
      const fields = [
        ['邮箱', AUTH.account],
        ['密码', AUTH.password],
        ['GitHub名', AUTH.suggestedUsername],
      ];
      for (const [label, value] of fields) {{
        const field = mkEl('div', 'kv-field');
        const labelEl = mkEl('span', 'kv-field-label', label);
        const valueEl = mkEl('span', 'kv-field-value', value || '—');
        valueEl.title = value || '';
        const copyBtn = mkEl('button', 'kv-copy-btn', '复制');
        copyBtn.onclick = (e) => {{
          e.stopPropagation();
          copyText(value, label);
        }};
        field.append(labelEl, valueEl, copyBtn);
        panel.appendChild(field);
      }}

      const nav = mkEl('div', 'kv-nav');
      const links = [
        ['打开 GitHub 首页', 'https://github.com/'],
        ['打开 GitHub 注册', 'https://github.com/signup'],
        ['打开 WorkBuddy', 'https://www.workbuddy.ai/'],
        ['打开网页邮箱', AUTH.mailUrl || 'https://mail.ggb6.com/'],
      ];
      for (const [text, href] of links) {{
        const link = mkEl('a', '', text);
        link.href = href;
        link.target = '_blank';
        // ``noopener`` only: keep the Referer so GitHub sees the same warm
        // first-party navigation chain a human would produce.
        link.rel = 'noopener';
        link.onclick = (e) => e.stopPropagation();
        nav.appendChild(link);
      }}
      panel.appendChild(nav);

      const mailBtns = mkEl('div', 'kv-btns');
      const mailLoginBtn = mkEl('button', 'kv-btn primary', '⚡ 自动登录邮箱');
      mailLoginBtn.onclick = (e) => {{
        e.stopPropagation();
        runRoundcubeMailFlow(true);
      }};
      const fillPageBtn = mkEl('button', 'kv-btn', '⚡ 填当前页表单');
      fillPageBtn.title = '仅在点击时执行：邮箱页自动登录；GitHub 首页/注册页填入邮箱、密码与建议用户名';
      fillPageBtn.onclick = (e) => {{
        e.stopPropagation();
        runManualFormFill();
      }};
      mailBtns.append(mailLoginBtn, fillPageBtn);
      panel.appendChild(mailBtns);
      return panel;
    }}

    function render() {{
      try {{
        const secret = getActiveTotpSecret();
        const nowSec = Math.floor(Date.now() / 1000);
        const rem = 30 - (nowSec % 30);
        const code = secret ? computeTotp(secret, Date.now()) : '';
        const fmtCode = code ? code.slice(0, 3) + ' ' + code.slice(3) : '无 2FA';

        if (minimized) {{
          container.className = 'kv-box minimized';
          domainPanelKey = '';
          const s1 = mkEl('span', '', '🔐 ' + AUTH.profileCode);
          s1.style.cssText = 'color:#38bdf8;font-weight:700;';
          const s2 = mkEl('span', '', fmtCode);
          s2.style.cssText = 'color:#34d399;font-family:monospace;';
          container.replaceChildren(s1, s2);
          container.onclick = () => {{
            minimized = false;
            render();
          }};
          return;
        }}

        container.onclick = null;
        container.className = 'kv-box';

        // Header
        const head = mkEl('div', 'kv-head');
        const title = mkEl('span', 'kv-title', '🔐 KeyVault · ' + AUTH.profileCode);
        const actionsTop = mkEl('div', 'kv-actions-top');
        const toggleBtn = mkEl('button', 'kv-mini-btn' + (autoEnabled ? ' active' : ''), autoEnabled ? '自动: 开' : '自动: 关');
        toggleBtn.onclick = (e) => {{
          e.stopPropagation();
          autoEnabled = !autoEnabled;
          safeSetSession('kv_autologin_enabled', autoEnabled ? '1' : '0');
          setStatus(autoEnabled ? '⚡ 已开启自动登录检测' : '🛡️ 静默防风控模式（点下方按钮填入 · Alt+K 隐藏）');
          if (autoEnabled) runAutoLoginTick(true);
        }};
        const minBtn = mkEl('button', 'kv-mini-btn', '—');
        minBtn.title = '收起为小胶囊';
        minBtn.onclick = (e) => {{
          e.stopPropagation();
          minimized = true;
          render();
        }};
        const hideBtn = mkEl('button', 'kv-mini-btn', '×');
        hideBtn.title = '暂时隐藏 (按 Alt+K 恢复显示)';
        hideBtn.onclick = (e) => {{
          e.stopPropagation();
          capsuleHidden = true;
          safeSetSession('kv_capsule_hidden', '1');
          hostEl.remove();
        }};
        actionsTop.append(toggleBtn, minBtn, hideBtn);
        head.append(title, actionsTop);

        // 域名邮箱 accounts get a credential + quick-navigation helper panel.
        // Reuse the existing nodes so the 1s refresh does not break text selection.
        if (AUTH.isDomainEmail) {{
          const panelKey = AUTH.account + '\\n' + AUTH.password;
          if (domainPanelKey !== panelKey) {{
            domainPanelKey = panelKey;
            container.replaceChildren(head, buildDomainEmailPanel(), mkEl('div', 'kv-status', statusText));
          }} else {{
            const statusNode = container.querySelector('.kv-status');
            if (statusNode) statusNode.textContent = statusText;
          }}
          return;
        }}
        domainPanelKey = '';

        // Account & TOTP row
        const row = mkEl('div', 'kv-row');
        const accSpan = mkEl('span', '', AUTH.maskedAccount || AUTH.account);
        accSpan.title = AUTH.account;
        accSpan.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:145px;';
        const totpSpan = mkEl('span', 'kv-totp', fmtCode + ' (' + rem + 's)');
        totpSpan.title = '点击复制 2FA';
        totpSpan.onclick = (e) => {{
          e.stopPropagation();
          if (code) {{
            navigator.clipboard?.writeText(code).catch(() => {{}});
            setStatus('📋 已复制 2FA 验证码: ' + code);
          }}
        }};
        row.append(accSpan, totpSpan);

        // Status
        const statusEl = mkEl('div', 'kv-status', statusText);

        // Buttons
        const btns = mkEl('div', 'kv-btns');
        const runNowBtn = mkEl('button', 'kv-btn primary', '⚡ 自动登录');
        runNowBtn.onclick = (e) => {{
          e.stopPropagation();
          runAutoLoginTick(true);
        }};
        const fillUserBtn = mkEl('button', 'kv-btn', '填账号');
        fillUserBtn.onclick = (e) => {{
          e.stopPropagation();
          fillIntoFocusedOrTarget(AUTH.account, '账号', [
            'input#identifierId', 'input[name="identifier"]', 'input[name="email"]',
            'input#email-input', 'input[name="username"]', 'input[type="email"]'
          ]);
        }};
        const fillPwdBtn = mkEl('button', 'kv-btn', '填密码');
        fillPwdBtn.onclick = (e) => {{
          e.stopPropagation();
          fillIntoFocusedOrTarget(AUTH.password, '密码', [
            'input[type="password"][name="Passwd"]', 'input[type="password"][name="password"]', 'input[type="password"]'
          ]);
        }};
        const fill2faBtn = mkEl('button', 'kv-btn', '填2FA');
        fill2faBtn.onclick = (e) => {{
          e.stopPropagation();
          const c = computeTotp(secret, Date.now());
          if (!fillOtpCodeOnPage(c)) {{
            fillIntoFocusedOrTarget(c, '2FA 验证码', ['input[type="tel"]', 'input[type="text"]']);
          }} else {{
            setStatus('✅ 已填入 2FA 验证码: ' + c);
          }}
        }};
        btns.append(runNowBtn, fillUserBtn, fillPwdBtn, fill2faBtn);

        if (AUTH.initialSecretKey && AUTH.initialSecretKey !== AUTH.secretKey) {{
          const fillInit2faBtn = mkEl('button', 'kv-btn', '填原2FA');
          fillInit2faBtn.onclick = (e) => {{
            e.stopPropagation();
            const c = computeTotp(AUTH.initialSecretKey, Date.now());
            if (!fillOtpCodeOnPage(c)) {{
              fillIntoFocusedOrTarget(c, '初始 2FA 验证码', ['input[type="tel"]', 'input[type="text"]']);
            }} else {{
              setStatus('✅ 已填入初始 2FA 验证码: ' + c);
            }}
          }};
          btns.appendChild(fillInit2faBtn);
        }}

        if (AUTH.recoveryEmail) {{
          const fillRecBtn = mkEl('button', 'kv-btn', '填辅助邮箱');
          fillRecBtn.onclick = (e) => {{
            e.stopPropagation();
            fillIntoFocusedOrTarget(AUTH.recoveryEmail, '辅助邮箱', [
              'input#knowledge-preregistered-email-response', 'input[name="knowledgePreregisteredEmailResponse"]', 'input[type="email"]'
            ]);
          }};
          btns.appendChild(fillRecBtn);
        }}

        if (AUTH.recoveryPassword) {{
          const fillRecPwdBtn = mkEl('button', 'kv-btn', '填辅密');
          fillRecPwdBtn.onclick = (e) => {{
            e.stopPropagation();
            fillIntoFocusedOrTarget(AUTH.recoveryPassword, '辅助邮箱密码', ['input[type="password"]']);
          }};
          btns.appendChild(fillRecPwdBtn);
        }}

        const genPwdBtn = mkEl('button', 'kv-btn', '🎲 强密');
        genPwdBtn.title = '生成 16 位高强度随机密码并复制到剪贴板';
        genPwdBtn.onclick = (e) => {{
          e.stopPropagation();
          const pool = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*_-+=';
          const rnd = new Uint32Array(16);
          crypto.getRandomValues(rnd);
          let out = '';
          for (let i = 0; i < 16; i++) out += pool[rnd[i] % pool.length];
          copyText(out, '强密码 ' + out);
          const pwdInputs = Array.from(document.querySelectorAll('input[type="password"]')).filter(isVisible);
          const active = document.activeElement;
          const target = (active && active.tagName === 'INPUT') ? active : (pwdInputs[pwdInputs.length - 1] || null);
          if (target) {{
            setNativeInputValue(target, out);
            setStatus('🎲 已生成强密并填入 & 复制: ' + out);
          }}
        }};
        btns.appendChild(genPwdBtn);

        const nav = mkEl('div', 'kv-nav');
        const rescueLinks = AUTH.accountType === 'Google'
          ? [
              ['安全中心', 'https://myaccount.google.com/security'],
              ['受限诊断', 'https://myaccount.google.com/restrictions'],
              ['TOS申诉', 'https://forms.gle/hGzM9MEUv2azZsrb9'],
              ['IP检测', 'https://ipinfo.io/json'],
            ]
          : AUTH.accountType === 'Claude'
            ? [
                ['Console', 'https://console.anthropic.com/'],
                ['IP检测', 'https://ipinfo.io/json'],
              ]
            : [
                ['API平台', 'https://platform.openai.com/api-keys'],
                ['IP检测', 'https://ipinfo.io/json'],
              ];
        for (const [text, href] of rescueLinks) {{
          const link = mkEl('a', '', text);
          link.href = href;
          link.target = '_blank';
          link.rel = 'noopener';
          link.onclick = (e) => e.stopPropagation();
          nav.appendChild(link);
        }}

        container.replaceChildren(head, row, statusEl, btns, nav);
      }} catch (_) {{}}
    }}

    statusUiUpdater = render;
    render();
    setInterval(render, 1000);
  }}

  function init() {{
    window.__kv_ensure_mounted = () => {{
      try {{ mountCapsule(); }} catch (_) {{}}
      try {{ runAutoLoginTick(false); }} catch (_) {{}}
    }};
    window.__kv_ensure_mounted();
    setInterval(window.__kv_ensure_mounted, 900);
  }}

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', init);
  }} else {{
    init();
  }}
}})();
"""


def _build_extension_files(ext_dir: Path, fp: dict, account_or_label: dict | str) -> None:
    ext_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(account_or_label, dict):
        account_dict = account_or_label
        account_label = account_dict.get("masked_account") or account_dict.get("account", "")
    else:
        account_dict = {"account": str(account_or_label), "masked_account": str(account_or_label)}
        account_label = str(account_or_label)

    manifest = {
        "manifest_version": 3,
        "name": f"KeyVault Fingerprint & AutoLogin ({fp.get('profile_code', 'FP')})",
        "version": "1.1.0",
        "description": f"Isolated hardware fingerprint & auto-login paired to {account_label}",
        "content_scripts": [
            {
                "matches": ["<all_urls>"],
                # Never inject the fingerprint spoof into third-party captcha
                # frames: DataDome/Arkose read their own pristine <canvas>.
                "exclude_matches": [
                    "*://*.captcha-delivery.com/*",
                    "*://*.arkoselabs.com/*",
                    "*://*.funcaptcha.com/*",
                    "*://*.challenges.cloudflare.com/*",
                ],
                "js": ["inject.js"],
                "run_at": "document_start",
                "world": "MAIN",
                "all_frames": True,
            },
            {
                "matches": [
                    "https://chatgpt.com/*",
                    "https://*.openai.com/*",
                    "https://accounts.google.com/*",
                    "https://myaccount.google.com/*",
                    "https://github.com/*",
                    "https://www.workbuddy.ai/*",
                    "https://mail.ggb6.com/*",
                    "https://claude.ai/*",
                    "https://*.claude.ai/*",
                    "https://accounts.anthropic.com/*",
                ],
                "js": ["autologin.js"],
                "run_at": "document_idle",
                "all_frames": False,
            },
        ],
    }
    (ext_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    env_cfg = resolve_browser_env_config(fp, account_dict)
    cfg_json = json.dumps(
        {
            "profileCode": fp.get("profile_code", "FP-000"),
            "vendor": fp.get("webgl_vendor", "Google Inc. (NVIDIA)"),
            "renderer": fp.get("webgl_renderer", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
            "cpu": int(fp.get("cpu_cores", 12)),
            "memory": int(fp.get("memory_gb", 16)),
            "screenW": int(fp.get("screen_width", 1920)),
            "screenH": int(fp.get("screen_height", 1080)),
            "canvasSeed": int(fp.get("canvas_seed", 123456)),
            "audioSeed": int(fp.get("audio_seed", 654321)),
            "timeZone": env_cfg.get("timezone", DEFAULT_TIMEZONE),
            "locale": env_cfg.get("locale", DEFAULT_LOCALE),
            "languages": env_cfg.get("languages", list(DEFAULT_LANGUAGES)),
            "enabled": bool(env_cfg.get("enabled", True)),
            # 域名邮箱 keeps every heavy fingerprint metric native so GitHub's
            # slider captcha sees genuine hardware and untouched canvas pixels.
            "isDomainEmail": (account_dict.get("account_type") or "").strip() == "域名邮箱",
        },
        ensure_ascii=False,
    )
    (ext_dir / "env_config.json").write_text(
        json.dumps(
            {
                "timeZone": env_cfg.get("timezone", ""),
                "locale": env_cfg.get("locale", ""),
                "languages": env_cfg.get("languages", []),
                "enabled": bool(env_cfg.get("enabled", False)),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    native_cloak_js = r"""
  // --- Native cloaking: every patched function must look and print native ---
  const __kvNativeMap = new WeakMap();
  const __kvOrigFnToString = Function.prototype.toString;
  function __kvMarkNative(fn, name) {
    if (typeof fn !== 'function') return fn;
    try {
      Object.defineProperty(fn, 'name', { value: String(name || ''), configurable: true });
    } catch (_) {}
    __kvNativeMap.set(fn, 'function ' + String(name || '') + '() { [native code] }');
    return fn;
  }
  try {
    const patchedFnToString = function toString() {
      if (typeof this === 'function' && __kvNativeMap.has(this)) {
        return __kvNativeMap.get(this);
      }
      return __kvOrigFnToString.call(this);
    };
    __kvMarkNative(patchedFnToString, 'toString');
    Function.prototype.toString = patchedFnToString;
  } catch (_) {}

  // Define a getter on the prototype (never on the instance) whenever the
  // prototype really is in the instance's chain; fall back to the instance
  // only in minimal environments such as Node.js test harnesses.
  function defineProtoGetter(proto, fallbackObj, prop, getter) {
    __kvMarkNative(getter, 'get ' + prop);
    try {
      if (proto && (typeof fallbackObj === 'undefined' || (!Object.prototype.hasOwnProperty.call(fallbackObj, prop) && proto.isPrototypeOf(fallbackObj)))) {
        Object.defineProperty(proto, prop, { get: getter, configurable: true });
        return;
      }
      if (fallbackObj) {
        Object.defineProperty(fallbackObj, prop, { get: getter, configurable: true });
      }
    } catch (_) {}
  }
"""

    tz_spoof_js = r"""
  // --- Timezone & language anti-leak spoofing (runs before any page JS) ---
  try {
    if (CFG.enabled && CFG.timeZone) {
      const OrigDateTimeFormat = Intl.DateTimeFormat;
      const TZ = CFG.timeZone;
      const LOCALE = CFG.locale || 'en-US';
      const LANGS = Object.freeze(
        (CFG.languages && CFG.languages.length) ? CFG.languages.slice() : [LOCALE]
      );

      function getTargetOffsetMinutes(dateInput) {
        const d = dateInput instanceof Date ? dateInput : new Date(dateInput);
        if (isNaN(d.getTime())) return NaN;
        try {
          const parts = new OrigDateTimeFormat('en-US', {
            timeZone: TZ,
            timeZoneName: 'longOffset',
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hourCycle: 'h23'
          }).formatToParts(d);
          let tzName = '';
          for (let i = 0; i < parts.length; i++) {
            if (parts[i].type === 'timeZoneName') tzName = parts[i].value;
          }
          if (!tzName || /^(GMT|UTC)$/i.test(tzName)) return 0;
          const m = /GMT([+-])(\d{1,2})(?::?(\d{2}))?/i.exec(tzName);
          if (!m) return 0;
          const sign = m[1] === '-' ? -1 : 1;
          const hours = parseInt(m[2], 10) || 0;
          const mins = parseInt(m[3] || '0', 10) || 0;
          // JS getTimezoneOffset() returns the negated UTC offset.
          return -(sign * (hours * 60 + mins));
        } catch (_) {
          return 0;
        }
      }

      function formatParts(date, options) {
        const opts = Object.assign({ timeZone: TZ }, options || {});
        const parts = new OrigDateTimeFormat('en-US', opts).formatToParts(date);
        const map = {};
        for (let i = 0; i < parts.length; i++) {
          map[parts[i].type] = parts[i].value;
        }
        return map;
      }

      function buildTimezoneSuffix(date) {
        const offset = getTargetOffsetMinutes(date);
        if (isNaN(offset)) return '';
        const total = -offset;
        const sign = total < 0 ? '-' : '+';
        const abs = Math.abs(total);
        const hh = String(Math.floor(abs / 60)).padStart(2, '0');
        const mm = String(abs % 60).padStart(2, '0');
        return 'GMT' + sign + hh + mm;
      }

      function buildLongTimezoneName(date) {
        try {
          const map = formatParts(date, { timeZoneName: 'long' });
          return map.timeZoneName || '';
        } catch (_) {
          return '';
        }
      }

      // --- Navigator.language / Navigator.languages ---
      // Getter lives on Navigator.prototype so Object.getOwnPropertyNames(
      // navigator) stays []. The instance is only touched when the prototype
      // chain is unavailable (minimal Node.js test harnesses).
      const navProto = (typeof Navigator !== 'undefined' && Navigator.prototype)
        ? Navigator.prototype
        : null;
      if (typeof navigator !== 'undefined') {
        defineProtoGetter(navProto, navigator, 'language', function() { return LOCALE; });
        defineProtoGetter(navProto, navigator, 'languages', function() { return LANGS; });
      }

      // --- Intl.DateTimeFormat (new + callable forms) ---
      function makeDefaultOptions(options) {
        const opts = Object.assign({}, options || {});
        if (!opts.timeZone) opts.timeZone = TZ;
        return opts;
      }

      const PatchedDateTimeFormat = function(locales, options) {
        const loc = locales === undefined ? LOCALE : locales;
        const opts = makeDefaultOptions(options);
        const inst = new OrigDateTimeFormat(loc, opts);
        try {
          const origResolved = inst.resolvedOptions.bind(inst);
          inst.resolvedOptions = __kvMarkNative(function() {
            const ro = origResolved();
            try {
              if (!(options && options.timeZone)) ro.timeZone = TZ;
              if (locales === undefined) ro.locale = LOCALE;
            } catch (_) {}
            return ro;
          }, 'resolvedOptions');
        } catch (_) {}
        return inst;
      };
      PatchedDateTimeFormat.prototype = OrigDateTimeFormat.prototype;
      if (typeof OrigDateTimeFormat.supportedLocalesOf === 'function') {
        PatchedDateTimeFormat.supportedLocalesOf = __kvMarkNative(function(locales) {
          return OrigDateTimeFormat.supportedLocalesOf(
            locales === undefined ? LOCALE : locales
          );
        }, 'supportedLocalesOf');
      }
      __kvMarkNative(PatchedDateTimeFormat, 'DateTimeFormat');
      Intl.DateTimeFormat = PatchedDateTimeFormat;

      // --- Other Intl formatters default to the spoofed locale ---
      const otherFormatters = [
        'NumberFormat',
        'RelativeTimeFormat',
        'PluralRules',
        'Collator',
        'ListFormat'
      ];
      for (let i = 0; i < otherFormatters.length; i++) {
        try {
          const name = otherFormatters[i];
          const Orig = Intl[name];
          if (typeof Orig !== 'function') continue;
          const Patched = function(locales, options) {
            return new Orig(locales === undefined ? LOCALE : locales, options);
          };
          Patched.prototype = Orig.prototype;
          if (typeof Orig.supportedLocalesOf === 'function') {
            Patched.supportedLocalesOf = __kvMarkNative(function(locales) {
              return Orig.supportedLocalesOf(locales === undefined ? LOCALE : locales);
            }, 'supportedLocalesOf');
          }
          __kvMarkNative(Patched, name);
          Intl[name] = Patched;
        } catch (_) {}
      }

      // --- Date.prototype ---
      const origGetTime = Date.prototype.getTime;
      const origGetTimezoneOffset = Date.prototype.getTimezoneOffset;
      const origToString = Date.prototype.toString;
      const origToDateString = Date.prototype.toDateString;
      const origToTimeString = Date.prototype.toTimeString;
      const origToLocaleString = Date.prototype.toLocaleString;
      const origToLocaleDateString = Date.prototype.toLocaleDateString;
      const origToLocaleTimeString = Date.prototype.toLocaleTimeString;

      Date.prototype.getTimezoneOffset = __kvMarkNative(function() {
        if (isNaN(origGetTime.call(this))) return NaN;
        return getTargetOffsetMinutes(this);
      }, 'getTimezoneOffset');

      Date.prototype.toDateString = __kvMarkNative(function() {
        if (isNaN(origGetTime.call(this))) return origToDateString.call(this);
        try {
          const map = formatParts(this, {
            weekday: 'short',
            month: 'short',
            day: '2-digit',
            year: 'numeric'
          });
          return map.weekday + ' ' + map.month + ' ' + map.day + ' ' + map.year;
        } catch (_) {
          return origToDateString.call(this);
        }
      }, 'toDateString');

      Date.prototype.toTimeString = __kvMarkNative(function() {
        if (isNaN(origGetTime.call(this))) return origToTimeString.call(this);
        try {
          const map = formatParts(this, {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hourCycle: 'h23'
          });
          const longName = buildLongTimezoneName(this);
          const suffix = longName ? ' (' + longName + ')' : '';
          return map.hour + ':' + map.minute + ':' + map.second + ' ' +
            buildTimezoneSuffix(this) + suffix;
        } catch (_) {
          return origToTimeString.call(this);
        }
      }, 'toTimeString');

      Date.prototype.toString = __kvMarkNative(function() {
        if (isNaN(origGetTime.call(this))) return origToString.call(this);
        try {
          return this.toDateString() + ' ' + this.toTimeString();
        } catch (_) {
          return origToString.call(this);
        }
      }, 'toString');

      function patchedToLocale(orig, locales, options) {
        const loc = locales === undefined ? LOCALE : locales;
        const opts = Object.assign({}, options || {});
        if (!opts.timeZone) opts.timeZone = TZ;
        return orig.call(this, loc, opts);
      }

      Date.prototype.toLocaleString = __kvMarkNative(function(locales, options) {
        return patchedToLocale.call(this, origToLocaleString, locales, options);
      }, 'toLocaleString');
      Date.prototype.toLocaleDateString = __kvMarkNative(function(locales, options) {
        return patchedToLocale.call(this, origToLocaleDateString, locales, options);
      }, 'toLocaleDateString');
      Date.prototype.toLocaleTimeString = __kvMarkNative(function(locales, options) {
        return patchedToLocale.call(this, origToLocaleTimeString, locales, options);
      }, 'toLocaleTimeString');
    }
  } catch (_) {}
"""
    inject_js = f"""(() => {{
  const CFG = {cfg_json};
{native_cloak_js}
{tz_spoof_js}
  // --- Hardware / screen / WebGL / Canvas / Audio spoofing ---
  // ``域名邮箱`` and the GitHub / captcha delivery origins keep every heavy
  // metric native: DataDome's slider reads real hardware and pristine
  // <canvas> pixels, while timezone & language spoofing stays active.
  try {{
    const hostNow = (typeof location !== 'undefined' && location.hostname)
      ? String(location.hostname).toLowerCase()
      : '';
    const skipHeavyFp = Boolean(CFG.isDomainEmail) ||
      /(?:^|\\.)(?:github\\.com|captcha-delivery\\.com|arkoselabs\\.com|funcaptcha\\.com)$/i.test(hostNow);

    const navProto = (typeof Navigator !== 'undefined' && Navigator.prototype)
      ? Navigator.prototype
      : null;

    // webdriver: only tamper when the engine actually exposes true. A normal
    // Chrome launch already reports false, so leave it completely untouched.
    if (typeof navigator !== 'undefined' && navigator.webdriver !== false) {{
      defineProtoGetter(navProto, navigator, 'webdriver', function() {{ return false; }});
    }}

    if (!skipHeavyFp) {{
      if (typeof navigator !== 'undefined') {{
        defineProtoGetter(navProto, navigator, 'hardwareConcurrency', function() {{ return CFG.cpu; }});
        defineProtoGetter(navProto, navigator, 'deviceMemory', function() {{ return CFG.memory; }});
      }}

      const screenProto = (typeof Screen !== 'undefined' && Screen.prototype)
        ? Screen.prototype
        : null;
      const screenObj = (typeof window !== 'undefined' && window.screen) ? window.screen : undefined;
      if (screenProto || screenObj) {{
        defineProtoGetter(screenProto, screenObj, 'width', function() {{ return CFG.screenW; }});
        defineProtoGetter(screenProto, screenObj, 'height', function() {{ return CFG.screenH; }});
        defineProtoGetter(screenProto, screenObj, 'availWidth', function() {{ return CFG.screenW; }});
        defineProtoGetter(screenProto, screenObj, 'availHeight', function() {{ return CFG.screenH - 40; }});
      }}

      const patchWebGL = (proto) => {{
        if (!proto || !proto.getParameter) return;
        const origGetParameter = proto.getParameter;
        proto.getParameter = __kvMarkNative(function(param) {{
          if (param === 37445) return CFG.vendor;
          if (param === 37446) return CFG.renderer;
          return origGetParameter.apply(this, arguments);
        }}, 'getParameter');
      }};
      if (typeof WebGLRenderingContext !== 'undefined') patchWebGL(WebGLRenderingContext.prototype);
      if (typeof WebGL2RenderingContext !== 'undefined') patchWebGL(WebGL2RenderingContext.prototype);

      if (typeof CanvasRenderingContext2D !== 'undefined' && CanvasRenderingContext2D.prototype.getImageData) {{
        const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = __kvMarkNative(function() {{
          const imgData = origGetImageData.apply(this, arguments);
          if (imgData && imgData.data && imgData.data.length >= 16) {{
            const delta = (CFG.canvasSeed % 3) + 1;
            const idx = (CFG.canvasSeed % 12);
            imgData.data[idx] = (imgData.data[idx] + delta) & 255;
          }}
          return imgData;
        }}, 'getImageData');

        if (typeof HTMLCanvasElement !== 'undefined' && HTMLCanvasElement.prototype.toDataURL) {{
          const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
          // Deterministic AND non-mutating: the pixel noise is applied to a
          // throwaway clone, so the page's own <canvas> stays byte-identical
          // and repeated toDataURL() calls always return the same string.
          HTMLCanvasElement.prototype.toDataURL = __kvMarkNative(function() {{
            try {{
              if (this.width > 0 && this.height > 0 && typeof document !== 'undefined') {{
                const clone = document.createElement('canvas');
                clone.width = this.width;
                clone.height = this.height;
                const srcCtx = this.getContext('2d');
                const dstCtx = clone.getContext('2d');
                if (srcCtx && dstCtx) {{
                  dstCtx.drawImage(this, 0, 0);
                  const px = origGetImageData.call(dstCtx, 0, 0, clone.width, clone.height);
                  if (px && px.data && px.data.length >= 16) {{
                    const delta = (CFG.canvasSeed % 3) + 1;
                    const idx = (CFG.canvasSeed % 12);
                    px.data[idx] = (px.data[idx] + delta) & 255;
                    dstCtx.putImageData(px, 0, 0);
                  }}
                  return origToDataURL.apply(clone, arguments);
                }}
              }}
            }} catch (_) {{}}
            return origToDataURL.apply(this, arguments);
          }}, 'toDataURL');
        }}
      }}

      if (typeof AudioBuffer !== 'undefined' && AudioBuffer.prototype.getChannelData) {{
        const origGetChannelData = AudioBuffer.prototype.getChannelData;
        AudioBuffer.prototype.getChannelData = __kvMarkNative(function() {{
          const data = origGetChannelData.apply(this, arguments);
          if (data && data.length > 10) {{
            const offset = ((CFG.audioSeed % 97) + 1) * 1e-7;
            data[0] = data[0] + offset;
          }}
          return data;
        }}, 'getChannelData');
      }}
    }}
  }} catch (_) {{}}
}})();
"""
    (ext_dir / "inject.js").write_text(inject_js, encoding="utf-8")
    (ext_dir / "autologin.js").write_text(_build_autologin_js(fp, account_dict), encoding="utf-8")


def _write_chromium_preferences(profile_dir: Path, env_cfg: dict) -> None:
    """Merge the spoofed locale, bookmark bar visibility, and WebRTC policy into ``Default/Preferences``.

    Existing user preferences (extensions, profile data, flags) are preserved.
    """
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_path = default_dir / "Preferences"
    prefs: dict = {}
    if prefs_path.is_file():
        try:
            loaded = json.loads(prefs_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prefs = loaded
        except Exception:
            prefs = {}
    languages = env_cfg.get("languages") or []
    accept_languages = ",".join(languages)
    intl = prefs.get("intl")
    if not isinstance(intl, dict):
        intl = {}
    intl["accept_languages"] = accept_languages
    intl["selected_languages"] = accept_languages
    prefs["intl"] = intl
    spellcheck = prefs.get("spellcheck")
    if not isinstance(spellcheck, dict):
        spellcheck = {}
    spellcheck["dictionaries"] = [env_cfg.get("locale", DEFAULT_LOCALE)]
    prefs["spellcheck"] = spellcheck

    bbar = prefs.get("bookmark_bar")
    if not isinstance(bbar, dict):
        bbar = {}
    bbar["show_on_all_tabs"] = True
    prefs["bookmark_bar"] = bbar

    webrtc = prefs.get("webrtc")
    if not isinstance(webrtc, dict):
        webrtc = {}
    webrtc["ip_handling_policy"] = "disable_non_proxied_udp"
    prefs["webrtc"] = webrtc

    prefs_path.write_text(
        json.dumps(prefs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _get_platform_bookmarks(account_type: str) -> list[tuple[str, str]]:
    atype = (account_type or "").strip().lower()
    if atype == "google":
        return [
            ("🔐 Google 安全设置", "https://myaccount.google.com/security"),
            ("📧 辅助恢复设置", "https://g.co/recover"),
            ("✉️ Gmail 收件箱", "https://mail.google.com/"),
            ("☁️ GCP Console", "https://console.cloud.google.com/"),
            ("🔗 第三方授权连接", "https://myaccount.google.com/connections"),
            ("✅ 账号身份验证", "https://g.co/verifyaccount"),
            ("📱 登录设备管理", "https://www.google.com/device"),
            ("⚖️ 服务限制诊断", "https://myaccount.google.com/restrictions"),
            ("🌍 地区归属变更", "https://policies.google.com/country-association-form"),
            ("🪪 年龄验证", "https://myaccount.google.com/age-verification"),
            ("🆘 TOS 封禁申诉表", "https://forms.gle/hGzM9MEUv2azZsrb9"),
            ("🌐 出口 IP 检测", "https://ipinfo.io/json"),
            ("🛡️ WebRTC 泄漏检测", "https://browserleaks.com/webrtc"),
        ]
    if atype == "chatgpt":
        return [
            ("🤖 ChatGPT Web", "https://chatgpt.com/"),
            ("🔑 OpenAI API Keys", "https://platform.openai.com/api-keys"),
            ("📊 OpenAI 用量统计", "https://platform.openai.com/usage"),
            ("🌐 出口 IP 检测", "https://ipinfo.io/json"),
            ("🛡️ WebRTC 泄漏检测", "https://browserleaks.com/webrtc"),
        ]
    if atype == "claude":
        return [
            ("✴️ Claude Web", "https://claude.ai/"),
            ("🔑 Anthropic Console", "https://console.anthropic.com/"),
            ("🌐 出口 IP 检测", "https://ipinfo.io/json"),
            ("🛡️ WebRTC 泄漏检测", "https://browserleaks.com/webrtc"),
        ]
    return [
        ("🌐 出口 IP 检测", "https://ipinfo.io/json"),
        ("🛡️ WebRTC 泄漏检测", "https://browserleaks.com/webrtc"),
        ("🔐 Google 安全设置", "https://myaccount.google.com/security"),
    ]


def _seed_profile_bookmarks(profile_dir: Path, account_type: str) -> None:
    """Seed or upgrade KeyVault maintenance bookmarks inside ``Default/Bookmarks`` without removing user bookmarks."""
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    bm_path = default_dir / "Bookmarks"

    ts = "13350000000000000"
    doc = None
    if bm_path.is_file():
        try:
            loaded = json.loads(bm_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("roots"), dict):
                doc = loaded
        except Exception:
            doc = None

    if doc is None:
        doc = {
            "checksum": "",
            "roots": {
                "bookmark_bar": {
                    "children": [],
                    "date_added": ts,
                    "date_last_used": "0",
                    "date_modified": ts,
                    "guid": "0bc5d13f-2cba-5d74-951f-3f233fe6c908",
                    "id": "1",
                    "name": "Bookmarks bar",
                    "type": "folder",
                },
                "other": {
                    "children": [],
                    "date_added": ts,
                    "date_last_used": "0",
                    "date_modified": ts,
                    "guid": "82b081ec-3dd3-529c-8475-ab6c344590dd",
                    "id": "2",
                    "name": "Other bookmarks",
                    "type": "folder",
                },
                "synced": {
                    "children": [],
                    "date_added": ts,
                    "date_last_used": "0",
                    "date_modified": ts,
                    "guid": "4cf2e351-0e85-532b-bb37-df045d8b8d80",
                    "id": "3",
                    "name": "Mobile bookmarks",
                    "type": "folder",
                },
            },
            "version": 1,
        }

    bar = doc["roots"].setdefault("bookmark_bar", {"children": [], "id": "1", "name": "Bookmarks bar", "type": "folder"})
    children = bar.get("children")
    if not isinstance(children, list):
        children = []

    folder_name = "KeyVault 维护助手"
    kept = [c for c in children if not (isinstance(c, dict) and c.get("name") == folder_name)]
    bm_nodes = []
    for idx, (title, url) in enumerate(_get_platform_bookmarks(account_type), start=1):
        bm_nodes.append({
            "date_added": ts,
            "date_last_used": "0",
            "id": str(9000 + idx),
            "name": title,
            "type": "url",
            "url": url,
        })
    folder_node = {
        "children": bm_nodes,
        "date_added": ts,
        "date_last_used": "0",
        "date_modified": ts,
        "id": "9000",
        "name": folder_name,
        "type": "folder",
    }
    bar["children"] = [folder_node, *kept]
    bm_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def reset_rejected_login_session(data_dir: str | Path, account_id: int) -> dict:
    """Delete stale ``DevToolsActivePort`` / lock files after Google/OAuth rejected session."""
    profile_dir = Path(data_dir) / "browsers" / f"acc_{int(account_id)}"
    if not profile_dir.exists():
        return {"ok": True, "removed": [], "message": "尚无浏览器配置目录"}
    removed = []
    for fname in ("DevToolsActivePort", "SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = profile_dir / fname
        if p.exists():
            try:
                p.unlink()
                removed.append(fname)
            except Exception:
                pass
    return {"ok": True, "removed": removed}


def prepare_profile_dir(data_dir: str | Path, account: dict) -> Path:
    base_data = Path(data_dir)
    acc_id = int(account["id"])
    profile_dir = base_data / "browsers" / f"acc_{acc_id}"
    ext_dir = profile_dir / "fp_ext"
    profile_dir.mkdir(parents=True, exist_ok=True)

    fp = account.get("fp_config") or generate_fingerprint_config(acc_id, account.get("account", ""))
    _build_extension_files(ext_dir, fp, account)
    # Every account type (域名邮箱 included) gets the matched locale persisted
    # into ``Default/Preferences`` so Chrome's first paint is not zh-CN.
    env_cfg = resolve_browser_env_config(fp, account)
    if env_cfg.get("enabled"):
        _write_chromium_preferences(profile_dir, env_cfg)
    _seed_profile_bookmarks(profile_dir, str(account.get("account_type") or ""))
    return profile_dir


def find_chromium_executable() -> str | None:
    import shutil
    import sys

    env_override = os.environ.get("KEYVAULT_BROWSER_PATH", "").strip()
    if env_override and os.path.exists(env_override):
        return env_override

    candidates: list[str] = []
    if sys.platform == "win32":
        candidates.extend(
            [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Chromium\Application\chrome.exe"),
            ]
        )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ]
        )

    for path in candidates:
        if path and os.path.exists(path):
            return path

    for bin_name in (
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "msedge",
        "microsoft-edge",
        "brave-browser",
        "chromium",
        "chromium-browser",
    ):
        found = shutil.which(bin_name)
        if found:
            return found

    return None


def get_target_url(account_type: str) -> str:
    normalized = (account_type or "").strip().lower()
    if normalized == "google":
        return "https://accounts.google.com/"
    if normalized == "域名邮箱":
        return "https://mail.ggb6.com/"
    if normalized in ("claude", "anthropic"):
        return "https://claude.ai/"
    return "https://chatgpt.com/"


def get_debug_port(account_id: int) -> int:
    import socket

    base_port = 29000 + (int(account_id) % 5000)
    # Check if base_port is free or already our Chrome CDP port
    try:
        with socket.create_connection(("127.0.0.1", base_port), timeout=0.15):
            # Port is listening; check if it's already a Chromium DevTools endpoint
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{base_port}/json/version", timeout=0.3) as resp:
                    if resp.status == 200:
                        return base_port
            except Exception:
                pass
            # Occupied by a non-CDP process: find a free ephemeral port
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                return int(s.getsockname()[1])
    except OSError:
        return base_port


def _append_proxy_isolation_args(args: list[str], proxy: str) -> None:
    """Append the network-isolation flags shared by every proxied launch mode.

    ``--proxy-server`` binds the whole browser to the sidecar endpoint (or a local
    auth relay when upstream credentials are present), ``--proxy-bypass-list`` keeps
    OAuth loopbacks local, ``disable_non_proxied_udp`` stops WebRTC from opening
    direct UDP sockets toward the local ISP, ``--disable-quic`` prevents HTTP/3
    (UDP 443) from bypassing the HTTP proxy, and ``--host-resolver-rules`` forces
    remote DNS inside the tunnel while keeping loopback hostnames local.
    """
    if not proxy:
        return
    eff_proxy = proxy.strip()
    if "@" in eff_proxy or ("://" not in eff_proxy and len(eff_proxy.split(":")) == 4):
        from src.maintenance_service import resolve_proxy_for_chromium_and_urllib

        resolved = resolve_proxy_for_chromium_and_urllib(eff_proxy)
        if resolved.get("chromium_proxy"):
            eff_proxy = resolved["chromium_proxy"]

    args.append(f"--proxy-server={eff_proxy}")
    args.append("--proxy-bypass-list=<-loopback>;localhost;127.0.0.1;[::1]")
    webrtc_flag = "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
    if webrtc_flag not in args:
        args.append(webrtc_flag)
    args.append("--disable-quic")
    args.append(
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1 , EXCLUDE localhost"
    )


def build_browser_launch_args(
    account: dict,
    profile_dir: str | Path,
    exe_path: str | None = None,
    target_url: str = "",
    pure_native: bool = False,
    enable_cdp: bool = False,
) -> list[str]:
    exe = exe_path or find_chromium_executable() or "chrome.exe"
    p_dir = Path(profile_dir).resolve()
    ext_dir = (p_dir / "fp_ext").resolve()
    acc_id = int(account.get("id") or 1)
    debug_port = get_debug_port(acc_id)

    fp = account.get("fp_config") or generate_fingerprint_config(acc_id, account.get("account", ""))
    proxy = (fp.get("proxy") or "").strip()
    eff_target_url = (target_url or "").strip() or get_target_url(account.get("account_type", "ChatGPT"))

    is_domain_email = (account.get("account_type") or "").strip() == "域名邮箱"
    env_cfg = resolve_browser_env_config(fp, account)

    if pure_native:
        args = [
            exe,
            f"--user-data-dir={p_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-save-password-bubble",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ]
        _append_proxy_isolation_args(args, proxy)
        if env_cfg and env_cfg.get("enabled"):
            args.append(f"--lang={env_cfg['locale']}")
            args.append(f"--accept-lang={','.join(env_cfg['languages'])}")
        args.append(eff_target_url)
        return args

    # Unified Zero-CDP Fingerprint Engine:
    # All platforms launch without --remote-debugging-port or --remote-allow-origins
    # by default so Google BotGuard / Cloudflare / DataDome / Arkose never detect an
    # attached V8 inspector, while fp_ext keeps hardware/timezone spoofing and manual
    # capsule assistance active.
    args = [
        exe,
        f"--user-data-dir={p_dir}",
        f"--load-extension={ext_dir}",
    ]
    if enable_cdp and not is_domain_email:
        args.append(f"--remote-debugging-port={debug_port}")
        args.append("--remote-allow-origins=*")
    args.extend([
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-save-password-bubble",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-domain-reliability",
        "--disk-cache-size=52428800",
        "--media-cache-size=20971520",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-features=WebRtcHideLocalIpsWithMdns,OptimizationHints,Translate,DisableLoadExtensionCommandLineSwitch",
    ])
    _append_proxy_isolation_args(args, proxy)
    if env_cfg and env_cfg.get("enabled"):
        # Chromium only reads these on first launch; keep them before the URL
        # so ``args[-1] == target_url`` stays true for callers and tests.
        args.append(f"--lang={env_cfg['locale']}")
        args.append(f"--accept-lang={','.join(env_cfg['languages'])}")
    args.append(eff_target_url)
    return args


async def _attach_cdp_page_target(
    ws_url: str,
    inject_js: str,
    autologin_js: str,
    env_cfg: dict | None = None,
) -> None:
    try:
        async with websockets.connect(ws_url, max_size=None, open_timeout=5) as ws:
            msg_id = 1
            # Emulation overrides are applied before any script runs so Blink
            # frames and Workers inherit the US timezone/locale natively.
            emulation_payloads: list[dict] = []
            if isinstance(env_cfg, dict) and env_cfg.get("timeZone"):
                msg_id += 1
                emulation_payloads.append(
                    {
                        "id": msg_id,
                        "method": "Emulation.setTimezoneOverride",
                        "params": {"timezoneId": env_cfg["timeZone"]},
                    }
                )
            if isinstance(env_cfg, dict) and env_cfg.get("locale"):
                msg_id += 1
                emulation_payloads.append(
                    {
                        "id": msg_id,
                        "method": "Emulation.setLocaleOverride",
                        "params": {"locale": env_cfg["locale"]},
                    }
                )
            for payload in emulation_payloads:
                await ws.send(json.dumps(payload))
            msg_id += 1
            # Never call Runtime.enable so Cloudflare Turnstile & Google BotGuard cannot detect V8 inspector attachment
            for payload in (
                {"id": msg_id, "method": "Page.enable"},
                {"id": msg_id + 1, "method": "Page.addScriptToEvaluateOnNewDocument", "params": {"source": inject_js}},
                {"id": msg_id + 2, "method": "Page.addScriptToEvaluateOnNewDocument", "params": {"source": autologin_js}},
                {"id": msg_id + 3, "method": "Runtime.evaluate", "params": {"expression": inject_js}},
                {"id": msg_id + 4, "method": "Runtime.evaluate", "params": {"expression": autologin_js}},
            ):
                await ws.send(json.dumps(payload))
            msg_id += 10

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.2)
                    evt = json.loads(raw)
                    method = evt.get("method", "")
                    if method in (
                        "Page.domContentLoaded",
                        "Page.loadEventFired",
                        "Page.frameNavigated",
                    ):
                        msg_id += 1
                        await ws.send(json.dumps({"id": msg_id, "method": "Runtime.evaluate", "params": {"expression": inject_js}}))
                        msg_id += 1
                        await ws.send(json.dumps({"id": msg_id, "method": "Runtime.evaluate", "params": {"expression": autologin_js}}))
                except asyncio.TimeoutError:
                    msg_id += 1
                    await ws.send(json.dumps({"id": msg_id, "method": "Runtime.evaluate", "params": {"expression": autologin_js}}))
    except Exception:
        pass


async def _cdp_port_monitor(
    debug_port: int,
    inject_js: str,
    autologin_js: str,
    max_Wait_sec: float = 20.0,
    env_cfg: dict | None = None,
) -> None:
    url = f"http://127.0.0.1:{debug_port}/json"
    attached_tasks: dict[str, asyncio.Task] = {}
    start_ts = time.time()
    connected_once = False
    consecutive_failures = 0

    while True:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                targets = json.loads(resp.read().decode("utf-8", errors="ignore"))
            connected_once = True
            consecutive_failures = 0

            for t in targets:
                if t.get("type") != "page":
                    continue
                t_url = t.get("url") or ""
                if t_url.startswith(("devtools://", "chrome-extension://", "chrome://", "edge://")):
                    continue
                tid = t.get("id")
                ws_url = t.get("webSocketDebuggerUrl")
                if tid and ws_url:
                    existing = attached_tasks.get(tid)
                    if existing is None or existing.done():
                        attached_tasks[tid] = asyncio.create_task(
                            _attach_cdp_page_target(
                                ws_url, inject_js, autologin_js, env_cfg=env_cfg
                            )
                        )
            await asyncio.sleep(0.8)
        except Exception:
            consecutive_failures += 1
            if not connected_once:
                if time.time() - start_ts > max_Wait_sec:
                    break
                await asyncio.sleep(0.25)
            else:
                if consecutive_failures >= 4:
                    break
                await asyncio.sleep(0.8)

    for task in attached_tasks.values():
        if not task.done():
            task.cancel()


def _start_cdp_injector(debug_port: int, profile_dir: Path) -> None:
    ext_dir = Path(profile_dir) / "fp_ext"
    inject_path = ext_dir / "inject.js"
    autologin_path = ext_dir / "autologin.js"
    if not inject_path.exists() or not autologin_path.exists():
        return
    inject_js = inject_path.read_text(encoding="utf-8")
    autologin_js = autologin_path.read_text(encoding="utf-8")
    env_cfg: dict | None = None
    env_cfg_path = ext_dir / "env_config.json"
    if env_cfg_path.is_file():
        try:
            loaded = json.loads(env_cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("enabled"):
                env_cfg = loaded
        except Exception:
            env_cfg = None

    def _runner():
        try:
            asyncio.run(
                _cdp_port_monitor(
                    debug_port, inject_js, autologin_js, env_cfg=env_cfg
                )
            )
        except Exception:
            pass

    t = threading.Thread(target=_runner, name=f"kv-cdp-{debug_port}", daemon=True)
    t.start()


def launch_isolated_browser(
    data_dir: str | Path,
    account: dict,
    target_url: str = "",
    pure_native: bool = False,
    enable_cdp: bool = False,
) -> dict:
    exe = find_chromium_executable()
    if not exe:
        return {"ok": False, "error": "未检测到 Chrome 或 Edge 浏览器"}

    fp = dict(account.get("fp_config") or {})
    proxy_url = (fp.get("proxy") or "").strip()
    proxy_node = (fp.get("proxy_node") or "").strip()
    if not fp:
        fp = generate_fingerprint_config(int(account.get("id") or 1), account.get("account", ""))
        proxy_url = (fp.get("proxy") or "").strip()
        proxy_node = (fp.get("proxy_node") or "").strip()
    account = {**account, "fp_config": fp}
    account_type = (account.get("account_type") or "ChatGPT").strip()
    is_domain_email = account_type == "域名邮箱"
    is_claude = account_type == "Claude"

    node_switch_res = {"ok": True, "switched": False}
    if is_claude:
        guard = _ensure_claude_proxy(
            account,
            proxy_url=proxy_url,
            proxy_node=proxy_node,
        )
        if not guard.get("ok"):
            return {"ok": False, "error": guard.get("error", "Claude 账号代理校验失败")}
        proxy_url = str(guard.get("proxy_url") or "").strip()
        proxy_node = str(guard.get("proxy_node") or proxy_node).strip()
        node_switch_res = guard.get("node_switch_res", node_switch_res)
        if proxy_url != (fp.get("proxy") or "").strip() or proxy_node != (
            fp.get("proxy_node") or ""
        ).strip():
            fp = {**fp, "proxy": proxy_url, "proxy_node": proxy_node}
            account = {**account, "fp_config": fp}
    elif proxy_node:
        node_switch_res = activate_proxy_node(
            account.get("account_type", "ChatGPT"),
            proxy_url,
            proxy_node,
        )
        if node_switch_res.get("ok") and str(node_switch_res.get("proxy_url") or "").strip():
            proxy_url = str(node_switch_res["proxy_url"]).strip()
            fp = {**fp, "proxy": proxy_url}
            account = {**account, "fp_config": fp}
    elif ":17899" in proxy_url:
        # The sidecar port may have moved to a fallback port (or the kernel may
        # not be running yet); always launch against the live sidecar URL.
        from src.sidecar_proxy import get_sidecar_proxy_manager

        started = get_sidecar_proxy_manager().ensure_started()
        if started.get("ok") and str(started.get("proxy_url") or "").strip():
            proxy_url = str(started["proxy_url"]).strip()
            fp = {**fp, "proxy": proxy_url}
            account = {**account, "fp_config": fp}

    profile_dir = prepare_profile_dir(data_dir, account)
    if target_url or pure_native or enable_cdp:
        args = build_browser_launch_args(
            account,
            profile_dir,
            exe_path=exe,
            target_url=target_url,
            pure_native=bool(pure_native),
            enable_cdp=bool(enable_cdp),
        )
    else:
        args = build_browser_launch_args(account, profile_dir, exe_path=exe)
    debug_port = get_debug_port(int(account.get("id") or 1))

    launch_env = os.environ.copy()
    env_cfg = resolve_browser_env_config(fp, account)
    if env_cfg.get("enabled"):
        launch_env["TZ"] = env_cfg["timezone"]
        launch_env["LANG"] = f"{env_cfg['locale'].replace('-', '_')}.UTF-8"
        launch_env["LC_ALL"] = launch_env["LANG"]

    try:
        subprocess.Popen(
            args,
            cwd=str(profile_dir),
            close_fds=True,
            env=launch_env,
        )
        # Unified Zero-CDP mode: skip WebSocket CDP injector unless enable_cdp=True
        # is explicitly requested, eliminating anti-bot V8 inspector detection.
        if enable_cdp and not is_domain_email and not pure_native:
            _start_cdp_injector(debug_port, profile_dir)
        return {
            "ok": True,
            "profile_dir": str(profile_dir),
            "fp_config": fp,
            "node_switched": node_switch_res.get("switched", False),
            "engine": "Chrome" if "chrome" in exe.lower() else "Edge",
            "pure_native": bool(pure_native),
            "zero_cdp": not bool(enable_cdp),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _proxy_port_of(proxy_url: str) -> int:
    m = re.search(r":(\d{2,5})(?:\D|$)", proxy_url or "")
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:
        return 0


def _ensure_claude_proxy(account: dict, *, proxy_url: str, proxy_node: str) -> dict:
    """Guarantee a live proxy for Claude before launching Chromium.

    Anthropic blocks direct datacenter/residential IPs and HK/CN/RU exits, so a
    Claude profile must never fall back to a direct connection. The guard
    activates the best US node on the sidecar, falls back to a live local
    Clash Verge listener (7897 ``AI-US``), and otherwise refuses to launch.
    """
    from src.sidecar_proxy import SIDECAR_GROUP_NAME, get_sidecar_proxy_manager

    result: dict = {
        "ok": True,
        "proxy_url": proxy_url,
        "proxy_node": proxy_node,
        "node_switch_res": {"ok": True, "switched": False},
    }
    sidecar_port = _proxy_port_of(proxy_url) if proxy_url else 0
    is_sidecar = bool(proxy_url) and (
        sidecar_port == DEFAULT_SIDECAR_PROXY_PORT
        or 17900 <= sidecar_port <= 17931
        or ":17899" in proxy_url
    )
    needs_sidecar = not proxy_url or is_sidecar

    if needs_sidecar:
        try:
            env = get_sidecar_proxy_manager().describe_environment("Claude")
        except Exception:
            env = {}
        nodes = [n for n in (env.get("nodes") or []) if isinstance(n, dict)]
        best_node = str(nodes[0].get("name") or "").strip() if nodes else ""
        ports = [p for p in (env.get("ports") or []) if isinstance(p, dict)]
        sidecar_entry = next(
            (
                p
                for p in ports
                if p.get("bound_group") == SIDECAR_GROUP_NAME and p.get("url")
            ),
            None,
        )
        sidecar_url = (
            str(sidecar_entry["url"])
            if sidecar_entry
            else f"http://127.0.0.1:{DEFAULT_SIDECAR_PROXY_PORT}"
        )

        target_node = proxy_node or best_node
        if target_node:
            switch = activate_proxy_node(
                "Claude",
                proxy_url or sidecar_url,
                target_node,
            )
            if switch.get("ok") and str(switch.get("proxy_url") or "").strip():
                result["proxy_url"] = str(switch["proxy_url"]).strip()
                result["proxy_node"] = target_node
                result["node_switch_res"] = switch
                return result

        # Sidecar is unavailable: fall back to any live local Clash Verge
        # listener (e.g. the fixed AI-US port 7897) before giving up. A
        # configured sidecar URL that failed to start is never trusted.
        if not proxy_url:
            for entry in ports:
                try:
                    listener_port = int(entry.get("port") or 0)
                except (TypeError, ValueError):
                    listener_port = 0
                if not listener_port:
                    continue
                if _is_local_port_open(listener_port):
                    result["proxy_url"] = str(
                        entry.get("url") or f"http://127.0.0.1:{listener_port}"
                    )
                    return result

        if not result["proxy_url"]:
            return {
                "ok": False,
                "error": (
                    "Claude 账号禁止无代理直连启动，请先配置或绑定美国代理节点/端口 "
                    "(如 127.0.0.1:17899 或 127.0.0.1:7897)"
                ),
            }
        if not _is_local_port_open(sidecar_port):
            return {
                "ok": False,
                "error": (
                    f"Claude 独立代理端口 127.0.0.1:{sidecar_port} 未启动且无可用美国节点，"
                    "已阻止直连启动，请检查订阅节点或 Clash Verge 代理"
                ),
            }
        return result

    # A user-specified node on a live sidecar must be activated as well.
    if proxy_node:
        switch = activate_proxy_node("Claude", proxy_url, proxy_node)
        if switch.get("ok") and str(switch.get("proxy_url") or "").strip():
            result["proxy_url"] = str(switch["proxy_url"]).strip()
            result["node_switch_res"] = switch

    port = _proxy_port_of(result["proxy_url"])
    is_loopback = "127.0.0.1" in result["proxy_url"] or "localhost" in result["proxy_url"]
    if is_loopback and port and port != DEFAULT_SIDECAR_PROXY_PORT:
        if not _is_local_port_open(port):
            return {
                "ok": False,
                "error": (
                    f"Claude 代理端口 127.0.0.1:{port} 未在监听，"
                    "请先启动 Clash Verge 或绑定有效的代理节点后再启动浏览器"
                ),
            }
    return result
