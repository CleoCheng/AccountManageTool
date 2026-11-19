import asyncio
import ctypes
import hashlib
import inspect
import json
import locale as py_locale
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
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


_REGION_TZ_LOCALE_TABLE: dict[tuple[str, ...], tuple[str, str]] = {
    ("us", "usa", "united states", "america", "美国"): ("America/Los_Angeles", "en-US"),
    ("uk", "gb", "united kingdom", "britain", "england", "英国"): ("Europe/London", "en-GB"),
    ("jp", "japan", "日本"): ("Asia/Tokyo", "ja-JP"),
    ("kr", "korea", "south korea", "韩国"): ("Asia/Seoul", "ko-KR"),
    ("hk", "hong kong", "hongkong", "香港"): ("Asia/Hong_Kong", "zh-HK"),
    ("tw", "taiwan", "台湾"): ("Asia/Taipei", "zh-TW"),
    ("sg", "singapore", "新加坡", "狮城"): ("Asia/Singapore", "en-SG"),
    ("de", "germany", "德国"): ("Europe/Berlin", "de-DE"),
    ("fr", "france", "法国"): ("Europe/Paris", "fr-FR"),
    ("nl", "netherlands", "荷兰"): ("Europe/Amsterdam", "nl-NL"),
    ("ca", "canada", "加拿大"): ("America/Toronto", "en-CA"),
    ("au", "australia", "澳大利亚", "澳洲"): ("Australia/Sydney", "en-AU"),
    ("in", "india", "印度"): ("Asia/Kolkata", "en-IN"),
    ("br", "brazil", "巴西"): ("America/Sao_Paulo", "pt-BR"),
    ("vn", "vietnam", "越南"): ("Asia/Ho_Chi_Minh", "vi-VN"),
    ("th", "thailand", "泰国"): ("Asia/Bangkok", "th-TH"),
    ("ph", "philippines", "菲律宾"): ("Asia/Manila", "en-PH"),
    ("my", "malaysia", "马来西亚"): ("Asia/Kuala_Lumpur", "en-MY"),
    ("id", "indonesia", "印尼", "印度尼西亚"): ("Asia/Jakarta", "id-ID"),
    ("tr", "turkey", "türkiye", "土耳其"): ("Europe/Istanbul", "tr-TR"),
    ("ru", "russia", "俄罗斯"): ("Europe/Moscow", "ru-RU"),
}


def resolve_timezone_and_locale_by_region(
    region: str | None, browser_lang: str | None = None
) -> tuple[str, str]:
    """Resolve default IANA timezone and BCP-47 locale from an account region tag."""
    norm = (region or "").strip().lower()
    tz, loc = DEFAULT_TIMEZONE, DEFAULT_LOCALE
    if norm:
        for keys, pair in _REGION_TZ_LOCALE_TABLE.items():
            if norm in keys:
                tz, loc = pair
                break
    explicit = (browser_lang or "").strip()
    if explicit and explicit.lower() not in ("default", "match_region", "system"):
        loc = explicit
    return tz, loc


def _get_system_ui_locale() -> str:
    """Return host OS UI locale (used only when user selects ``system`` language mode)."""
    if sys.platform == "win32":
        try:
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            buf = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.LCIDToLocaleName(lang_id, buf, len(buf), 0) > 0 and buf.value:
                return buf.value
        except Exception:
            pass
    try:
        loc = py_locale.getlocale()[0] or ""
        if loc:
            return loc.replace("_", "-")
    except Exception:
        pass
    return "zh-CN"


def _match_any_keyword(haystack: str, keywords: tuple[str, ...]) -> bool:
    return any(k in haystack for k in keywords)


def resolve_browser_env_config(
    fp: dict,
    account: dict | None = None,
    settings: dict | None = None,
) -> dict:
    """Resolve the browser timezone/locale/Accept-Language for a profile.

    The proxy exit node (and ``reg_region``) decide the timezone and default
    locale so HTTP headers, ``--lang``, and ``Default/Preferences`` match the
    exit IP. US matching runs before JP/SG on purpose: relay names such as
    ``USbyJPTT | MiyaIP-2 HY2`` describe a JP hop but a US exit.
    """
    fp = fp if isinstance(fp, dict) else {}
    account = account if isinstance(account, dict) else {}
    settings = settings if isinstance(settings, dict) else {}

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
    explicit_browser_lang = str(fp.get("browser_lang") or "").strip()
    global_lang_mode = str(settings.get("default_browser_lang") or "").strip()

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
        locale = "ja-JP" if (reg_region.strip() and not proxy_node.strip()) else "en-US"
        languages = ["ja-JP", "ja", "en-US", "en"] if locale == "ja-JP" else ["en-US", "en"]
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
        locale = "ko-KR" if (reg_region.strip() and not proxy_node.strip()) else "en-US"
        languages = ["ko-KR", "ko", "en-US", "en"] if locale == "ko-KR" else ["en-US", "en"]
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
        locale = "de-DE" if (reg_region.strip() and not proxy_node.strip()) else "en-US"
        languages = ["de-DE", "de", "en-US", "en"] if locale == "de-DE" else ["en-US", "en"]
    elif (
        _match_any_keyword(haystack, ("canada", "toronto", "vancouver", "加拿大", "多伦多"))
        or "🇨🇦" in proxy_node
        or re.search(r"\bca\b", haystack)
    ):
        timezone = "America/Toronto"
        locale = "en-CA" if (reg_region.strip() and not proxy_node.strip()) else "en-US"
        languages = ["en-CA", "en-US", "en"] if locale == "en-CA" else ["en-US", "en"]
    elif (
        _match_any_keyword(haystack, ("hong kong", "hongkong", "香港"))
        or "🇭🇰" in proxy_node
        or re.search(r"\bhk\b", haystack)
    ):
        timezone = "Asia/Hong_Kong"
        locale = "zh-HK" if (reg_region.strip() and not proxy_node.strip()) else "en-US"
        languages = ["zh-HK", "zh", "en-US", "en"] if locale == "zh-HK" else ["en-US", "en"]
    elif reg_region.strip():
        reg_tz, reg_loc = resolve_timezone_and_locale_by_region(reg_region)
        if reg_tz != DEFAULT_TIMEZONE or reg_loc != DEFAULT_LOCALE:
            timezone = reg_tz
            locale = reg_loc

    # Explicit overrides win over region inference.
    if explicit_tz:
        timezone = explicit_tz
    if explicit_locale:
        locale = explicit_locale
    if explicit_browser_lang and explicit_browser_lang.lower() not in ("default", "match_region"):
        locale = _get_system_ui_locale() if explicit_browser_lang.lower() == "system" else explicit_browser_lang
        languages = []
    elif not explicit_locale and global_lang_mode and global_lang_mode.lower() not in ("default", "match_region"):
        locale = _get_system_ui_locale() if global_lang_mode.lower() == "system" else global_lang_mode
        languages = []

    if not timezone or not locale:
        timezone = timezone or DEFAULT_TIMEZONE
        locale = locale or DEFAULT_LOCALE
        if not languages:
            languages = list(DEFAULT_LANGUAGES)

    if not languages:
        base_lang = locale.split("-", 1)[0]
        languages = [locale]
        if "-" in locale and base_lang:
            languages.append(base_lang)
        # Bilingual hint if user runs a non-English UI on a foreign-region account
        if reg_region.strip():
            _, reg_loc = resolve_timezone_and_locale_by_region(reg_region)
            reg_base = reg_loc.split("-", 1)[0]
            if reg_base.lower() != base_lang.lower() and reg_base.lower() != "en":
                languages.append(reg_loc)
                if "-" in reg_loc:
                    languages.append(reg_base)
        if base_lang.lower() != "en":
            languages.extend(["en-US", "en"])

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
            "launchToken": f"{int(time.time() * 1000)}_{secrets.token_hex(4)}",
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
  function safeGetLocal(k) {{
    try {{ return localStorage.getItem(k); }} catch (_) {{ return null; }}
  }}
  function safeSetLocal(k, v) {{
    try {{ localStorage.setItem(k, v); }} catch (_) {{}}
  }}
  function pruneStaleLocalLaunchStates() {{
    try {{
      const stale = [];
      for (let i = 0; i < localStorage.length; i++) {{
        const k = localStorage.key(i);
        if (k && k.indexOf('kv_launch_state_') === 0 && k !== SESSION_STATE_KEY) stale.push(k);
      }}
      stale.forEach((k) => {{ try {{ localStorage.removeItem(k); }} catch (_) {{}} }});
    }} catch (_) {{}}
  }}
  function loadCachedLaunchState() {{
    try {{
      const cached = safeGetSession(SESSION_STATE_KEY);
      if (cached) return JSON.parse(cached);
    }} catch (_) {{}}
    try {{
      const cachedLocal = safeGetLocal(SESSION_STATE_KEY);
      if (cachedLocal) return JSON.parse(cachedLocal);
    }} catch (_) {{}}
    return null;
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

  // --- Shared Cross-Origin Launch State (First-Launch One-Shot Auto-Login + Bar UI State) ---
  const LAUNCH_STORAGE_KEY = 'kv_launch_state';
  const SESSION_STATE_KEY = 'kv_launch_state_' + (AUTH.launchToken || 'default');
  let launchState = {{
    launchToken: AUTH.launchToken || '',
    autoLoginDone: false,
    loginRoundActive: false,
    stepsDone: {{}},
    lastStepAt: 0,
    minimized: false,
    hidden: false,
    pos: null
  }};
  let storageReady = false;
  let roundCheckAnchorAt = Date.now();
  let autoEnabled = false;
  let capsuleHidden = false;
  let minimized = false;
  let statusText = '⏳ 首轮登录检测中...';
  let statusUiUpdater = null;
  let lastFocusedInput = null;
  let hotkeyBound = false;
  let dropdownOpen = false;
  let navPopupBuiltKey = '';

  function persistLaunchState() {{
    try {{
      safeSetSession(SESSION_STATE_KEY, JSON.stringify(launchState));
    }} catch (_) {{}}
    // Mirror to localStorage so the docked bar survives CDP-injected pages
    // (no chrome.storage.local) and full reloads within the same origin.
    try {{
      pruneStaleLocalLaunchStates();
      safeSetLocal(SESSION_STATE_KEY, JSON.stringify(launchState));
    }} catch (_) {{}}
    try {{
      if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {{
        chrome.storage.local.set({{ [LAUNCH_STORAGE_KEY]: launchState }});
      }}
    }} catch (_) {{}}
  }}

  function finishAutoLoginRound(msg) {{
    const changed = !launchState.autoLoginDone || autoEnabled;
    launchState.autoLoginDone = true;
    launchState.loginRoundActive = false;
    autoEnabled = false;
    if (changed) {{
      persistLaunchState();
    }}
    if (msg) setStatus(msg);
    else if (statusUiUpdater) statusUiUpdater();
  }}

  function claimAutoStep(stepKey, force) {{
    if (force) {{
      launchState.loginRoundActive = true;
      launchState.lastStepAt = Date.now();
      launchState.stepsDone[stepKey] = launchState.lastStepAt;
      persistLaunchState();
      return true;
    }}
    if (launchState.stepsDone && launchState.stepsDone[stepKey]) {{
      return false;
    }}
    const now = Date.now();
    if (!launchState.stepsDone) launchState.stepsDone = {{}};
    launchState.stepsDone[stepKey] = now;
    launchState.loginRoundActive = true;
    launchState.lastStepAt = now;
    persistLaunchState();
    return true;
  }}

  function startManualAutoLoginRound() {{
    resetStepCooldowns();
    launchState.autoLoginDone = false;
    launchState.loginRoundActive = true;
    launchState.stepsDone = {{}};
    launchState.lastStepAt = Date.now();
    roundCheckAnchorAt = Date.now();
    autoEnabled = true;
    persistLaunchState();
    setStatus('⚡ 正在执行本轮自动登录...');
    runAutoLoginTick(true);
  }}

  function initLaunchState(onReady) {{
    const finalize = (saved) => {{
      if (saved && typeof saved === 'object' && saved.launchToken === (AUTH.launchToken || '')) {{
        launchState = {{
          ...launchState,
          ...saved,
          stepsDone: (saved.stepsDone && typeof saved.stepsDone === 'object') ? saved.stepsDone : {{}}
        }};
      }} else {{
        launchState = {{
          launchToken: AUTH.launchToken || '',
          autoLoginDone: false,
          loginRoundActive: false,
          stepsDone: {{}},
          lastStepAt: 0,
          minimized: false,
          hidden: false,
          pos: null
        }};
        persistLaunchState();
      }}
      autoEnabled = !launchState.autoLoginDone;
      capsuleHidden = !!launchState.hidden;
      minimized = !!launchState.minimized;
      statusText = autoEnabled
        ? '⏳ 检测登录状态中...'
        : '🛡️ 静默防风控模式（点按钮手动填入 · Alt+K 隐藏）';
      storageReady = true;
      if (typeof onReady === 'function') onReady();
    }};

    try {{
      if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {{
        chrome.storage.local.get([LAUNCH_STORAGE_KEY], (res) => {{
          let saved = res ? res[LAUNCH_STORAGE_KEY] : null;
          if (!saved) saved = loadCachedLaunchState();
          finalize(saved);
        }});
        return;
      }}
    }} catch (_) {{}}

    finalize(loadCachedLaunchState());
  }}

  document.addEventListener('focusin', (e) => {{
    if (e.target && e.target.tagName === 'INPUT') {{
      lastFocusedInput = e.target;
    }}
  }}, true);

  let statusSeq = 0;
  function setStatus(msg) {{
    statusSeq += 1;
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
        finishAutoLoginRound('📧 需邮箱验证码 · 自动登录已关闭');
        return;
      }}
      if (getActiveTotpSecret() && claimAutoStep('openai_totp', force)) {{
        scheduleTotpFillAndSubmit(() => findOpenAISubmitButton(otpInput));
        return;
      }}
      if (launchState.stepsDone && launchState.stepsDone.openai_totp && !force) {{
        if (Date.now() - (launchState.lastStepAt || 0) >= 6500) {{
          finishAutoLoginRound('✅ 本轮登录已结束 · 自动登录已关闭');
        }}
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
      if (claimAutoStep('openai_pwd', force)) {{
        if (emailInput && !emailInput.value && AUTH.account) {{
          setNativeInputValue(emailInput, AUTH.account);
        }}
        setNativeInputValue(pwdInput, AUTH.password);
        setStatus('⚡ 已自动填入密码，正在登录...');
        setTimeout(() => {{
          const btn = findOpenAISubmitButton(pwdInput);
          if (btn) clickButton(btn);
        }}, 400);
      }} else if (!force && Date.now() - (launchState.lastStepAt || 0) >= 7500) {{
        finishAutoLoginRound('✅ 本轮密码步骤已提交 · 自动登录已关闭');
      }}
      return;
    }}

    // 3. Check for OpenAI / ChatGPT Email step (works both on auth.openai.com and inline modal on chatgpt.com)
    if (emailInput && AUTH.account) {{
      if (claimAutoStep('openai_email', force)) {{
        setNativeInputValue(emailInput, AUTH.account);
        setStatus('⚡ 已自动填入账号邮箱，正在点击继续...');
        setTimeout(() => {{
          const btn = findOpenAISubmitButton(emailInput);
          if (btn) clickButton(btn);
        }}, 400);
      }} else if (!force && Date.now() - (launchState.lastStepAt || 0) >= 7500) {{
        finishAutoLoginRound('✅ 本轮邮箱步骤已结束 · 自动登录已关闭');
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
        return /^(log in|sign in|登录)$/i.test(t);
      }});

      if (loginBtn) {{
        if (claimAutoStep('chatgpt_landing_login', force)) {{
          setStatus('⚡ 检测到未登录，正在点击「登录」按钮...');
          setTimeout(() => clickButton(loginBtn), 450);
        }} else if (!force && Date.now() - (launchState.lastStepAt || 0) >= 7500) {{
          finishAutoLoginRound('✅ 本轮登录已触发 · 自动登录已关闭');
        }}
        return;
      }}

      const signedInEl = document.querySelector(
        '#prompt-textarea, [data-testid="profile-button"], button[data-testid="accounts-profile-button"], [data-testid="create-new-chat-button"], nav a[href^="/c/"]'
      );
      if (signedInEl || Date.now() - roundCheckAnchorAt >= 2600) {{
        finishAutoLoginRound(
          launchState.loginRoundActive
            ? '✅ 首轮登录完成 · 自动登录已关闭'
            : '🛡️ 已登录 ChatGPT · 自动登录已关闭'
        );
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
      finishAutoLoginRound(
        launchState.loginRoundActive
          ? '✅ 首轮登录 Google 完成 · 自动登录已关闭'
          : '🛡️ 已登录 Google · 自动登录已关闭'
      );
      return;
    }}

    // 0. Account Chooser screen (if present)
    if (AUTH.account) {{
      const chooserRow = document.querySelector(`div[data-identifier="${{CSS.escape(AUTH.account)}}"]`);
      if (chooserRow && isVisible(chooserRow)) {{
        if (claimAutoStep('google_chooser', force)) {{
          setStatus('⚡ 已在列表中选择当前 Google 账号...');
          setTimeout(() => clickButton(chooserRow), 350);
        }}
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
      if (claimAutoStep('google_totp', force)) {{
        scheduleTotpFillAndSubmit(() => findGoogleNextButton('#totpNext'));
      }} else if (!force && Date.now() - (launchState.lastStepAt || 0) >= 7500) {{
        finishAutoLoginRound('✅ 本轮 2FA 提交完成 · 自动登录已关闭');
      }}
      return;
    }}

    // 2. Google Recovery Email Verification step
    const recInput = findFirstVisible([
      'input#knowledge-preregistered-email-response',
      'input[name="knowledgePreregisteredEmailResponse"]',
    ]);
    if (recInput && AUTH.recoveryEmail) {{
      if (claimAutoStep('google_recovery_email', force)) {{
        setNativeInputValue(recInput, AUTH.recoveryEmail);
        setStatus('⚡ 已自动填入辅助邮箱，正在验证...');
        setTimeout(() => {{
          const btn = findGoogleNextButton(null);
          if (btn) clickButton(btn);
        }}, 450);
      }} else if (!force && Date.now() - (launchState.lastStepAt || 0) >= 7500) {{
        finishAutoLoginRound('✅ 本轮辅助邮箱验证已提交 · 自动登录已关闭');
      }}
      return;
    }}

    // Also check if Google is showing the "Choose verification method" list with "Confirm your recovery email" (data-challengetype="12") or Authenticator (data-challengetype="6")
    const totpChallengeOption = document.querySelector('div[data-challengetype="6"], li div[data-challengetype="6"]');
    if (totpChallengeOption && isVisible(totpChallengeOption) && getActiveTotpSecret() && claimAutoStep('google_pick_totp', force)) {{
      setStatus('⚡ 自动选择 Google 身份验证器 2FA 验证...');
      setTimeout(() => clickButton(totpChallengeOption), 400);
      return;
    }}
    const recChallengeOption = document.querySelector('div[data-challengetype="12"], li div[data-challengetype="12"]');
    if (recChallengeOption && isVisible(recChallengeOption) && AUTH.recoveryEmail && claimAutoStep('google_pick_rec', force)) {{
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
      if (claimAutoStep('google_pwd', force)) {{
        setNativeInputValue(pwdInput, AUTH.password);
        setStatus('⚡ 已自动填入 Google 密码，正在下一步...');
        setTimeout(() => {{
          const btn = findGoogleNextButton('#passwordNext');
          if (btn) clickButton(btn);
        }}, 450);
      }} else if (!force && Date.now() - (launchState.lastStepAt || 0) >= 7500) {{
        finishAutoLoginRound('✅ 本轮密码步骤已提交 · 自动登录已关闭');
      }}
      return;
    }}

    // 4. Google Identifier (Email) step
    const emailInput = findFirstVisible([
      'input#identifierId',
      'input[type="email"][name="identifier"]',
    ]);
    if (emailInput && AUTH.account) {{
      if (claimAutoStep('google_email', force)) {{
        setNativeInputValue(emailInput, AUTH.account);
        setStatus('⚡ 已自动填入 Google 邮箱，正在下一步...');
        setTimeout(() => {{
          const btn = findGoogleNextButton('#identifierNext');
          if (btn) clickButton(btn);
        }}, 450);
      }} else if (!force && Date.now() - (launchState.lastStepAt || 0) >= 7500) {{
        finishAutoLoginRound('✅ 本轮账号步骤已提交 · 自动登录已关闭');
      }}
      return;
    }}

    // Signed-in non-login Google surfaces (Gmail, Cloud Console, Account overview, etc.)
    if (!force) {{
      const elapsed = Date.now() - Math.max(roundCheckAnchorAt, launchState.lastStepAt || 0);
      const hasSignedInNav = !!document.querySelector('a[aria-label*="Google"], a[href*="SignOutOptions"]');
      if (hasSignedInNav || (!host.includes('accounts.google.com') && elapsed >= 1500) || elapsed >= 3200) {{
        finishAutoLoginRound(
          launchState.loginRoundActive
            ? '✅ 首轮登录已结束 · 自动登录已关闭'
            : '🛡️ 无需登录 · 自动登录已关闭'
        );
      }}
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
      finishAutoLoginRound('✅ 已成功登录 Claude（自动登录已关闭）');
      return;
    }}

    if (emailInput && AUTH.account) {{
      if (claimAutoStep('claude_email', force)) {{
        setNativeInputValue(emailInput, AUTH.account);
        finishAutoLoginRound('✅ 已填入 Claude 登录邮箱 · 首轮自动填充已关闭');
      }}
      return;
    }}

    if (host.includes('claude.ai') && !loggedInPath && !composer) {{
      if (claimAutoStep('claude_landing', force)) {{
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
      finishAutoLoginRound('⚠️ 此账号未配置登录邮箱 · 自动登录已关闭');
      return;
    }}

    if (!force && Date.now() - Math.max(roundCheckAnchorAt, launchState.lastStepAt || 0) >= 3200) {{
      finishAutoLoginRound('🛡️ 当前无需登录 · 自动登录已关闭');
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
      finishAutoLoginRound('✅ 已成功登录 GGB6 邮箱收件箱（自动登录已关闭）');
      return;
    }}

    if (userEl && pwdEl && AUTH.account && AUTH.password) {{
      if (claimAutoStep('roundcube_login', force)) {{
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
      }} else if (!force && Date.now() - (launchState.lastStepAt || 0) >= 6500) {{
        finishAutoLoginRound('✅ 本轮邮箱登录已提交 · 自动登录已关闭');
      }}
      return;
    }}

    if (isMailHost) {{
      if (!AUTH.account || !AUTH.password) {{
        finishAutoLoginRound('⚠️ 此账号未配置邮箱账号或密码');
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
    if (!storageReady) return;
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
    }} else if (!force) {{
      finishAutoLoginRound('🛡️ 当前页无需登录 · 自动登录已关闭');
      return;
    }}

    if (!force && autoEnabled && !launchState.autoLoginDone) {{
      const now = Date.now();
      if (launchState.loginRoundActive && launchState.lastStepAt && now - launchState.lastStepAt >= 8000) {{
        finishAutoLoginRound('✅ 本轮登录已结束 · 自动登录已关闭');
      }} else if (!launchState.loginRoundActive && now - roundCheckAnchorAt >= 4500) {{
        finishAutoLoginRound('🛡️ 无需登录 · 自动登录已关闭');
      }}
    }}
  }}

  // --- Closed Shadow DOM Top Bar & Draggable Mini Capsule ---
  let minimizeTimer = null;
  let currentUiMode = '';

  // --- In-Page Docked Top Bar: push the page down so the bar never covers content ---
  let docOrigMarginTop = null;
  let docAppliedMarginTop = '';

  function isDockedTop() {{
    return !capsuleHidden && !minimized && !launchState.pos;
  }}

  function rememberDocMarginTop() {{
    if (docOrigMarginTop !== null) return;
    const docEl = document.documentElement;
    if (!docEl) return;
    let value = '';
    let priority = '';
    try {{
      if (docEl.style && typeof docEl.style.getPropertyValue === 'function') {{
        value = docEl.style.getPropertyValue('margin-top') || '';
        priority = typeof docEl.style.getPropertyPriority === 'function'
          ? (docEl.style.getPropertyPriority('margin-top') || '')
          : '';
      }} else if (docEl.style) {{
        value = docEl.style.marginTop || '';
      }}
    }} catch (_) {{
      value = '';
      priority = '';
    }}
    docOrigMarginTop = {{ value: value, priority: priority }};
  }}

  function restoreDocMarginTop() {{
    const docEl = document.documentElement;
    if (!docEl || !docEl.style) return;
    try {{
      if (typeof docEl.style.removeProperty === 'function') {{
        if (docOrigMarginTop && docOrigMarginTop.value) {{
          docEl.style.setProperty('margin-top', docOrigMarginTop.value, docOrigMarginTop.priority || '');
        }} else {{
          docEl.style.removeProperty('margin-top');
          docEl.style.marginTop = '';
        }}
      }} else {{
        docEl.style.marginTop = (docOrigMarginTop && docOrigMarginTop.value) || '';
      }}
    }} catch (_) {{}}
    docAppliedMarginTop = '';
  }}

  function syncPageTopOffset(barHeightHint) {{
    const docEl = document.documentElement;
    if (!docEl || !docEl.style) return;
    const hostEl = document.getElementById('__kv_autologin_host');
    if (!isDockedTop() || !hostEl) {{
      restoreDocMarginTop();
      return;
    }}
    rememberDocMarginTop();
    let h = 0;
    try {{
      h = barHeightHint || (hostEl.getBoundingClientRect ? hostEl.getBoundingClientRect().height : 0);
    }} catch (_) {{ h = 0; }}
    h = Math.max(36, Math.round(h || 38));
    const px = h + 'px';
    if (docAppliedMarginTop === px) return;
    try {{
      if (typeof docEl.style.setProperty === 'function') {{
        docEl.style.setProperty('margin-top', px, 'important');
      }} else {{
        docEl.style.marginTop = px;
      }}
    }} catch (_) {{
      try {{ docEl.style.marginTop = px; }} catch (_) {{}}
    }}
    docAppliedMarginTop = px;
  }}

  function autoMinimizeCapsule() {{
    if (minimizeTimer) return;
    minimizeTimer = setTimeout(() => {{
      minimized = true;
      launchState.minimized = true;
      persistLaunchState();
      if (statusUiUpdater) statusUiUpdater();
    }}, 1800);
  }}

  function bindToggleHotkey() {{
    if (hotkeyBound) return;
    hotkeyBound = true;
    document.addEventListener('keydown', (e) => {{
      if (e.altKey && (e.key === 'k' || e.key === 'K' || e.code === 'KeyK')) {{
        e.preventDefault();
        capsuleHidden = !capsuleHidden;
        launchState.hidden = capsuleHidden;
        persistLaunchState();
        const existing = document.getElementById('__kv_autologin_host');
        if (capsuleHidden) {{
          if (existing) existing.remove();
          syncPageTopOffset();
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
      syncPageTopOffset();
      return;
    }}
    const rootTarget = document.documentElement || document.body;
    if (!rootTarget || existingHost) return;
    const hostEl = document.createElement('div');
    hostEl.id = '__kv_autologin_host';
    hostEl.style.cssText = 'all: initial; position: fixed; top: 0px; left: 0px; right: 0px; width: 100%; transform: none; z-index: 2147483647; font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;';
    const shadow = hostEl.attachShadow({{ mode: 'closed' }});

    const style = document.createElement('style');
    style.textContent = `
      * {{ box-sizing: border-box; }}
      .kv-bar {{
        position: relative;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        flex-wrap: nowrap;
        max-width: calc(100vw - 16px);
        background: rgba(11, 17, 32, 0.94);
        backdrop-filter: blur(10px);
        color: #f8fafc;
        border: 1px solid rgba(99, 102, 241, 0.45);
        border-radius: 10px;
        padding: 4px 8px;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.48);
        font-size: 11.5px;
        line-height: 1.25;
        user-select: none;
        white-space: nowrap;
      }}
      .kv-bar.minimized {{
        border-radius: 999px;
        padding: 4px 8px 4px 10px;
        background: rgba(23, 21, 58, 0.95);
        border-color: rgba(129, 140, 248, 0.65);
        cursor: grab;
        gap: 7px;
      }}
      .kv-bar.minimized:active {{
        cursor: grabbing;
      }}
      .kv-bar.docked {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        width: 100%;
        max-width: 100vw;
        min-height: 38px;
        padding: 4px 12px;
        gap: 8px;
        border-radius: 0;
        border-top: none;
        border-left: none;
        border-right: none;
        border-bottom: 1px solid rgba(99, 102, 241, 0.45);
        background: linear-gradient(90deg, rgba(11, 17, 32, 0.96), rgba(19, 25, 48, 0.96));
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.36);
      }}
      .kv-bar.floating {{
        display: inline-flex;
        width: auto;
        border-radius: 10px;
        border: 1px solid rgba(99, 102, 241, 0.45);
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.48);
      }}
      .kv-bar-left,
      .kv-bar-right {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
        flex-wrap: nowrap;
        min-width: 0;
      }}
      .kv-bar-right {{
        margin-left: auto;
      }}
      .kv-mini-btn.redock {{
        background: rgba(6, 78, 59, 0.55);
        border-color: rgba(16, 185, 129, 0.55);
        color: #6ee7b7;
      }}
      .kv-drag-handle {{
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 3px 7px;
        border-radius: 6px;
        background: rgba(30, 41, 59, 0.9);
        border: 1px solid rgba(148, 163, 184, 0.22);
        color: #7dd3fc;
        font-weight: 700;
        cursor: grab;
        max-width: 190px;
        overflow: hidden;
        text-overflow: ellipsis;
      }}
      .kv-drag-handle:active {{
        cursor: grabbing;
      }}
      .kv-sep {{
        width: 1px;
        height: 18px;
        background: rgba(148, 163, 184, 0.22);
        flex: 0 0 auto;
      }}
      .kv-btn {{
        display: inline-flex;
        align-items: center;
        gap: 3px;
        background: rgba(30, 41, 59, 0.95);
        color: #e2e8f0;
        border: 1px solid rgba(148, 163, 184, 0.24);
        border-radius: 6px;
        padding: 4px 8px;
        font-size: 11px;
        font-weight: 600;
        cursor: pointer;
        white-space: nowrap;
      }}
      .kv-btn:hover {{
        background: rgba(51, 65, 85, 0.95);
        border-color: rgba(56, 189, 248, 0.55);
        color: #fff;
      }}
      .kv-btn.primary {{
        background: rgba(2, 132, 199, 0.9);
        border-color: rgba(56, 189, 248, 0.75);
        color: #fff;
      }}
      .kv-btn.primary:hover {{
        background: #0ea5e9;
      }}
      .kv-btn.totp {{
        background: rgba(6, 78, 59, 0.85);
        border-color: rgba(16, 185, 129, 0.48);
        color: #6ee7b7;
        font-family: ui-monospace, Consolas, monospace;
      }}
      .kv-btn.totp:hover {{
        background: rgba(5, 150, 105, 0.9);
        color: #fff;
      }}
      .kv-btn.auto-on {{
        background: rgba(16, 185, 129, 0.2);
        border-color: rgba(16, 185, 129, 0.55);
        color: #34d399;
      }}
      .kv-btn.auto-off {{
        background: rgba(30, 41, 59, 0.85);
        color: #94a3b8;
      }}
      .kv-status-pill {{
        max-width: 165px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        color: #94a3b8;
        font-size: 11px;
        padding: 0 4px;
      }}
      .kv-mini-btn {{
        background: rgba(255, 255, 255, 0.08);
        color: #cbd5e1;
        border: 1px solid rgba(255, 255, 255, 0.16);
        border-radius: 6px;
        padding: 3px 7px;
        font-size: 11px;
        font-weight: 600;
        cursor: pointer;
        white-space: nowrap;
      }}
      .kv-mini-btn:hover {{
        background: rgba(255, 255, 255, 0.18);
        color: #fff;
      }}
      .kv-mini-btn.collapse {{
        background: rgba(67, 56, 202, 0.45);
        border-color: rgba(129, 140, 248, 0.55);
        color: #e0e7ff;
      }}
      .kv-mini-btn.close:hover {{
        background: rgba(185, 28, 28, 0.7);
        border-color: rgba(248, 113, 113, 0.7);
      }}
      .kv-menu-wrap {{
        position: relative;
        display: inline-flex;
      }}
      .kv-dropdown {{
        position: absolute;
        top: calc(100% + 6px);
        right: 0;
        min-width: 168px;
        background: rgba(15, 23, 42, 0.98);
        border: 1px solid rgba(99, 102, 241, 0.48);
        border-radius: 9px;
        padding: 5px;
        box-shadow: 0 12px 28px rgba(0, 0, 0, 0.55);
        display: none;
        flex-direction: column;
        gap: 3px;
        z-index: 2147483647;
      }}
      .kv-dropdown.open {{
        display: flex;
      }}
      .kv-dropdown a {{
        display: block;
        color: #cbd5e1;
        text-decoration: none;
        padding: 5px 9px;
        border-radius: 6px;
        font-size: 11.5px;
        font-weight: 600;
      }}
      .kv-dropdown a:hover {{
        background: rgba(56, 189, 248, 0.2);
        color: #fff;
      }}
      .kv-mini-brand {{
        color: #e0e7ff;
        font-weight: 700;
        font-size: 11.5px;
      }}
      .kv-mini-totp {{
        font-family: ui-monospace, Consolas, monospace;
        font-weight: 700;
        color: #34d399;
        background: rgba(16, 185, 129, 0.14);
        padding: 2px 6px;
        border-radius: 999px;
        font-size: 11px;
      }}
    `;

    const container = document.createElement('div');
    container.className = 'kv-bar';
    shadow.appendChild(style);
    shadow.appendChild(container);
    rootTarget.appendChild(hostEl);

    let suppressClickUntil = 0;

    function applyBarPosition() {{
      const hasPos = !!(launchState.pos && typeof launchState.pos.left === 'number' && typeof launchState.pos.top === 'number');
      if (hasPos) {{
        const rect = hostEl.getBoundingClientRect();
        const w = rect.width || 220;
        const h = rect.height || 36;
        const maxL = Math.max(4, (window.innerWidth || 1280) - w - 4);
        const maxT = Math.max(4, (window.innerHeight || 800) - h - 4);
        const clampedL = Math.max(4, Math.min(maxL, launchState.pos.left));
        const clampedT = Math.max(4, Math.min(maxT, launchState.pos.top));
        hostEl.style.transform = 'none';
        hostEl.style.right = 'auto';
        hostEl.style.width = 'auto';
        hostEl.style.left = clampedL + 'px';
        hostEl.style.top = clampedT + 'px';
      }} else if (minimized) {{
        // Mini capsule without a custom position floats centered near the top,
        // never covering the top-right avatar / account menu.
        hostEl.style.right = 'auto';
        hostEl.style.width = 'auto';
        hostEl.style.left = '50%';
        hostEl.style.top = '8px';
        hostEl.style.transform = 'translateX(-50%)';
      }} else {{
        // Docked top bar spans the viewport width at y=0 (visible in F11/fullscreen).
        hostEl.style.transform = 'none';
        hostEl.style.left = '0px';
        hostEl.style.top = '0px';
        hostEl.style.right = '0px';
        hostEl.style.width = '100%';
      }}
      syncPageTopOffset();
    }}

    function attachDrag(el, miniOnly = false) {{
      if (!el || el.__kvDragBound) return;
      el.__kvDragBound = true;
      el.addEventListener('mousedown', (e) => {{
        if (e.button !== 0) return;
        if (miniOnly && !minimized) return;
        if (e.target && e.target.closest('button, a, input')) return;
        e.preventDefault();
        const startX = e.clientX;
        const startY = e.clientY;
        const wasDocked = isDockedTop();
        const rect = hostEl.getBoundingClientRect();
        const origLeft = wasDocked ? Math.max(0, startX - 40) : rect.left;
        const origTop = wasDocked ? Math.max(0, startY - 12) : rect.top;
        let moved = false;

        const onMove = (ev) => {{
          const dx = ev.clientX - startX;
          const dy = ev.clientY - startY;
          if (!moved && Math.abs(dx) + Math.abs(dy) < 4) return;
          if (!moved && wasDocked) {{
            // Dragging the docked bar away from the top edge turns it into a
            // free-floating compact bar (page top offset is restored at once).
            container.classList.remove('docked');
            container.classList.add('floating');
            hostEl.style.width = 'auto';
            hostEl.style.right = 'auto';
          }}
          moved = true;
          const curRect = hostEl.getBoundingClientRect();
          const maxL = Math.max(4, (window.innerWidth || 1280) - curRect.width - 4);
          const maxT = Math.max(4, (window.innerHeight || 800) - curRect.height - 4);
          const nextL = Math.round(Math.max(4, Math.min(maxL, origLeft + dx)));
          const nextT = Math.round(Math.max(4, Math.min(maxT, origTop + dy)));
          hostEl.style.transform = 'none';
          hostEl.style.left = nextL + 'px';
          hostEl.style.top = nextT + 'px';
          launchState.pos = {{ left: nextL, top: nextT }};
          syncPageTopOffset();
        }};

        const onUp = () => {{
          window.removeEventListener('mousemove', onMove, true);
          window.removeEventListener('mouseup', onUp, true);
          if (moved) {{
            suppressClickUntil = Date.now() + 180;
            // Snapping an expanded floating bar back to the top edge re-docks it.
            if (!minimized && launchState.pos && launchState.pos.top <= 10) {{
              launchState.pos = null;
              render(true);
            }} else {{
              render(true);
            }}
            persistLaunchState();
          }}
        }};

        window.addEventListener('mousemove', onMove, true);
        window.addEventListener('mouseup', onUp, true);
      }});
    }}

    if (!window.__kv_resize_bound) {{
      window.__kv_resize_bound = true;
      window.addEventListener('resize', () => applyBarPosition());
    }}

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
        setStatus('📋 未找到输入框，已复制' + label);
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
      const seqAtCopy = statusSeq;
      const done = () => {{
        if (statusSeq === seqAtCopy) setStatus('📋 已复制' + label);
      }};
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

    function buildLinksDropdown(linkDefs, buttonLabel) {{
      const wrap = mkEl('div', 'kv-menu-wrap');
      const trigger = mkEl('button', 'kv-btn', buttonLabel);
      const menu = mkEl('div', 'kv-dropdown' + (dropdownOpen ? ' open' : ''));
      for (const [text, href] of linkDefs) {{
        const link = mkEl('a', '', text);
        link.href = href;
        link.target = '_blank';
        // ``noopener`` only: keep the Referer so GitHub sees the same warm
        // first-party navigation chain a human would produce.
        link.rel = 'noopener';
        link.onclick = (e) => {{
          e.stopPropagation();
          dropdownOpen = false;
          menu.classList.remove('open');
        }};
        menu.appendChild(link);
      }}
      trigger.onclick = (e) => {{
        e.stopPropagation();
        dropdownOpen = !dropdownOpen;
        menu.classList.toggle('open', dropdownOpen);
      }};
      wrap.append(trigger, menu);
      return wrap;
    }}

    let uiRefs = null;

    function buildExpandedBar() {{
      const dragHandle = mkEl(
        'div',
        'kv-drag-handle',
        '⠿ 🛡️ ' + AUTH.profileCode + ' · ' + (AUTH.maskedAccount || AUTH.account || 'KeyVault')
      );
      dragHandle.title =
        (AUTH.account || '') + '\\n按住此处可自由拖拽顶部栏位置 · 双击缩为小胶囊';
      dragHandle.addEventListener('dblclick', (e) => {{
        e.stopPropagation();
        minimized = true;
        launchState.minimized = true;
        persistLaunchState();
        render(true);
      }});

      const elements = [dragHandle, mkEl('div', 'kv-sep')];
      let totpBtn = null;
      let toggleBtn = null;
      let statusEl = null;

      if (AUTH.isDomainEmail) {{
        const btnMailCopy = mkEl('button', 'kv-btn', '📧 邮箱');
        btnMailCopy.title = AUTH.account;
        btnMailCopy.onclick = (e) => {{ e.stopPropagation(); copyText(AUTH.account, '邮箱'); }};

        const btnPwdCopy = mkEl('button', 'kv-btn', '🔑 密码');
        btnPwdCopy.onclick = (e) => {{ e.stopPropagation(); copyText(AUTH.password, '密码'); }};

        const btnGhUser = mkEl('button', 'kv-btn', '👤 GitHub名');
        btnGhUser.title = AUTH.suggestedUsername;
        btnGhUser.onclick = (e) => {{ e.stopPropagation(); copyText(AUTH.suggestedUsername, 'GitHub名'); }};

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

        const domainLinks = [
          ['打开 GitHub 首页', 'https://github.com/'],
          ['打开 GitHub 注册', 'https://github.com/signup'],
          ['打开 WorkBuddy', 'https://www.workbuddy.ai/'],
          ['打开网页邮箱', AUTH.mailUrl || 'https://mail.ggb6.com/'],
        ];
        const navWrap = buildLinksDropdown(domainLinks, '🌐 快捷入口 ▾');
        elements.push(btnMailCopy, btnPwdCopy, btnGhUser, mailLoginBtn, fillPageBtn, navWrap);
      }} else {{
        const fillUserBtn = mkEl('button', 'kv-btn', '📧 账号');
        fillUserBtn.onclick = (e) => {{
          e.stopPropagation();
          fillIntoFocusedOrTarget(AUTH.account, '账号', [
            'input#identifierId', 'input[name="identifier"]', 'input[name="email"]',
            'input#email-input', 'input[name="username"]', 'input[type="email"]'
          ]);
        }};
        const fillPwdBtn = mkEl('button', 'kv-btn', '🔑 密码');
        fillPwdBtn.onclick = (e) => {{
          e.stopPropagation();
          fillIntoFocusedOrTarget(AUTH.password, '密码', [
            'input[type="password"][name="Passwd"]', 'input[type="password"][name="password"]', 'input[type="password"]'
          ]);
        }};

        totpBtn = mkEl('button', 'kv-btn totp', '🛡️ 无2FA');
        totpBtn.title = '点击填入 2FA 动态码并复制';
        totpBtn.onclick = (e) => {{
          e.stopPropagation();
          const sec = getActiveTotpSecret();
          if (!sec) {{
            setStatus('⚠️ 此账号未配置 2FA');
            return;
          }}
          const c = computeTotp(sec, Date.now());
          copyText(c, '2FA ' + c);
          if (fillOtpCodeOnPage(c)) {{
            setStatus('✅ 已填入 2FA: ' + c);
          }}
        }};

        elements.push(fillUserBtn, fillPwdBtn, totpBtn);

        if (AUTH.initialSecretKey && AUTH.initialSecretKey !== AUTH.secretKey) {{
          const fillInit2faBtn = mkEl('button', 'kv-btn', '原2FA');
          fillInit2faBtn.onclick = (e) => {{
            e.stopPropagation();
            const c = computeTotp(AUTH.initialSecretKey, Date.now());
            if (!fillOtpCodeOnPage(c)) {{
              fillIntoFocusedOrTarget(c, '初始 2FA 验证码', ['input[type="tel"]', 'input[type="text"]']);
            }} else {{
              setStatus('✅ 已填入初始 2FA 验证码: ' + c);
            }}
          }};
          elements.push(fillInit2faBtn);
        }}

        if (AUTH.recoveryEmail) {{
          const fillRecBtn = mkEl('button', 'kv-btn', '📮 辅邮');
          fillRecBtn.onclick = (e) => {{
            e.stopPropagation();
            fillIntoFocusedOrTarget(AUTH.recoveryEmail, '辅助邮箱', [
              'input#knowledge-preregistered-email-response', 'input[name="knowledgePreregisteredEmailResponse"]', 'input[type="email"]'
            ]);
          }};
          elements.push(fillRecBtn);
        }}

        if (AUTH.recoveryPassword) {{
          const fillRecPwdBtn = mkEl('button', 'kv-btn', '🔐 辅密');
          fillRecPwdBtn.onclick = (e) => {{
            e.stopPropagation();
            fillIntoFocusedOrTarget(AUTH.recoveryPassword, '辅助邮箱密码', ['input[type="password"]']);
          }};
          elements.push(fillRecPwdBtn);
        }}

        const genPwdBtn = mkEl('button', 'kv-btn', '🎲 强密');
        genPwdBtn.title = '生成 16 位高强度随机密码并复制/填入';
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

        toggleBtn = mkEl('button', 'kv-btn ' + (autoEnabled ? 'auto-on' : 'auto-off'), autoEnabled ? '⚡ 自动:开' : '⚡ 首轮登录');
        toggleBtn.title = '开馆首轮自动登录结束后自动关闭；点击可手动重触发或暂停';
        toggleBtn.onclick = (e) => {{
          e.stopPropagation();
          if (autoEnabled) {{
            finishAutoLoginRound('⏸ 已手动关闭自动登录');
          }} else {{
            startManualAutoLoginRound();
          }}
        }};

        const rescueLinks = AUTH.accountType === 'Google'
          ? [
              ['🔐 Google 安全中心', 'https://myaccount.google.com/security'],
              ['⚖️ 服务受限诊断', 'https://myaccount.google.com/restrictions'],
              ['🆘 TOS 封禁申诉表', 'https://forms.gle/hGzM9MEUv2azZsrb9'],
              ['🌍 地区归属变更', 'https://policies.google.com/country-association-form'],
              ['💳 关闭支付资料', 'https://payments.google.com/gp/w/home/reauthprompt?rtrp=/home/settings'],
              ['🚫 取消Pro订阅', 'https://myaccount.google.com/deleteservices'],
              ['🌐 出口 IP 检测', 'https://ipinfo.io/json'],
            ]
          : AUTH.accountType === 'Claude'
            ? [
                ['✴️ Anthropic Console', 'https://console.anthropic.com/'],
                ['🌐 出口 IP 检测', 'https://ipinfo.io/json'],
              ]
            : [
                ['🤖 OpenAI API 平台', 'https://platform.openai.com/api-keys'],
                ['🌐 出口 IP 检测', 'https://ipinfo.io/json'],
              ];
        const navWrap = buildLinksDropdown(rescueLinks, '🌐 工具 ▾');
        elements.push(mkEl('div', 'kv-sep'), genPwdBtn, toggleBtn, navWrap);
      }}

      statusEl = mkEl('span', 'kv-status-pill', statusText);
      statusEl.title = statusText;

      const minBtn = mkEl('button', 'kv-mini-btn collapse', '⊟ 最小');
      minBtn.title = '缩小为可自由拖拽的迷你小胶囊';
      minBtn.onclick = (e) => {{
        e.stopPropagation();
        dropdownOpen = false;
        minimized = true;
        launchState.minimized = true;
        persistLaunchState();
        render(true);
      }};

      const hideBtn = mkEl('button', 'kv-mini-btn close', '×');
      hideBtn.title = '暂时隐藏 (按 Alt+K 恢复显示)';
      hideBtn.onclick = (e) => {{
        e.stopPropagation();
        capsuleHidden = true;
        launchState.hidden = true;
        persistLaunchState();
        hostEl.remove();
        syncPageTopOffset();
      }};

      // Docked top bar splits into a left zone (drag handle + credential/tool
      // buttons) and a right zone (status + minimize + hide). A floating bar
      // also offers a compact 📌 吸顶 button to re-dock.
      const leftZone = mkEl('div', 'kv-bar-left');
      leftZone.replaceChildren(...elements);
      const rightZone = mkEl('div', 'kv-bar-right');
      const rightItems = [statusEl, minBtn];
      if (launchState.pos) {{
        const redockBtn = mkEl('button', 'kv-mini-btn redock', '📌 吸顶');
        redockBtn.title = '吸回网页顶部停靠';
        redockBtn.onclick = (e) => {{
          e.stopPropagation();
          launchState.pos = null;
          persistLaunchState();
          render(true);
        }};
        rightItems.push(redockBtn);
      }}
      rightItems.push(hideBtn);
      rightZone.replaceChildren(...rightItems);
      const floating = !!launchState.pos;
      container.className = 'kv-bar ' + (floating ? 'floating' : 'docked');
      container.replaceChildren(leftZone, rightZone);
      uiRefs = {{ mode: 'expanded', totpBtn, toggleBtn, statusEl }};
      // Apply / clear the page top offset in the same tick (not one rAF later)
      // so minimizing or hiding never leaves a blank strip at the page top.
      syncPageTopOffset();
    }}

    function buildMiniCapsule() {{
      const brandEl = mkEl('span', 'kv-mini-brand', '⠿ 🔐 ' + AUTH.profileCode);
      const totpPill = mkEl('span', 'kv-mini-totp', '无 2FA');
      totpPill.title = '点击复制当前 2FA 验证码';
      totpPill.onclick = (e) => {{
        if (Date.now() < suppressClickUntil) return;
        e.stopPropagation();
        const sec = getActiveTotpSecret();
        if (sec) {{
          const c = computeTotp(sec, Date.now());
          copyText(c, '2FA ' + c);
        }}
      }};

      const expandBtn = mkEl('button', 'kv-mini-btn collapse', '展开');
      expandBtn.title = '恢复顶部工具栏';
      expandBtn.onclick = (e) => {{
        if (Date.now() < suppressClickUntil) return;
        e.stopPropagation();
        minimized = false;
        launchState.minimized = false;
        persistLaunchState();
        render(true);
      }};

      container.className = 'kv-bar minimized';
      container.title = (AUTH.account || '') + '\\n按住小胶囊可自由拖动到页面任意位置';
      container.replaceChildren(brandEl, totpPill, expandBtn);
      uiRefs = {{ mode: 'mini', totpPill }};
      // A minimized capsule is never docked: restore the page top immediately.
      syncPageTopOffset();
    }}

    attachDrag(container, false);
    window.__kv_close_dropdown = () => {{
      if (dropdownOpen) {{
        dropdownOpen = false;
        shadow.querySelectorAll('.kv-dropdown.open').forEach((m) => m.classList.remove('open'));
      }}
    }};
    if (!window.__kv_outside_click_bound) {{
      window.__kv_outside_click_bound = true;
      document.addEventListener('click', () => {{
        if (typeof window.__kv_close_dropdown === 'function') window.__kv_close_dropdown();
      }});
    }}

    function updateLiveTexts() {{
      const secret = getActiveTotpSecret();
      const nowSec = Math.floor(Date.now() / 1000);
      const rem = 30 - (nowSec % 30);
      const code = secret ? computeTotp(secret, Date.now()) : '';
      const fmtCode = code ? code.slice(0, 3) + ' ' + code.slice(3) : '无 2FA';

      if (uiRefs && uiRefs.mode === 'mini') {{
        if (uiRefs.totpPill) {{
          uiRefs.totpPill.textContent = code ? (fmtCode + ' · ' + rem + 's') : '无2FA';
        }}
        return;
      }}
      if (uiRefs && uiRefs.mode === 'expanded') {{
        if (uiRefs.totpBtn) {{
          uiRefs.totpBtn.textContent = code ? ('🛡️ ' + fmtCode + ' (' + rem + 's)') : '🛡️ 无2FA';
        }}
        if (uiRefs.toggleBtn) {{
          uiRefs.toggleBtn.className = 'kv-btn ' + (autoEnabled ? 'auto-on' : 'auto-off');
          uiRefs.toggleBtn.textContent = autoEnabled ? '⚡ 自动:开' : '⚡ 首轮登录';
        }}
        if (uiRefs.statusEl) {{
          uiRefs.statusEl.textContent = statusText;
          uiRefs.statusEl.title = statusText;
        }}
      }}
    }}

    function render(forceRebuild = false) {{
      try {{
        const targetMode = minimized ? 'mini' : 'expanded';
        if (forceRebuild || currentUiMode !== targetMode || !uiRefs) {{
          currentUiMode = targetMode;
          if (minimized) buildMiniCapsule();
          else buildExpandedBar();
          if (typeof requestAnimationFrame === 'function') {{
            requestAnimationFrame(() => applyBarPosition());
          }} else {{
            applyBarPosition();
          }}
        }}
        updateLiveTexts();
      }} catch (_) {{}}
    }}

    statusUiUpdater = () => render(false);
    render(true);
    if (window.__kv_capsule_render_timer && typeof clearInterval === 'function') {{
      clearInterval(window.__kv_capsule_render_timer);
    }}
    window.__kv_capsule_render_timer = setInterval(() => render(false), 1000);
  }}

  function init() {{
    window.__kv_ensure_mounted = () => {{
      if (!storageReady) return;
      try {{ mountCapsule(); }} catch (_) {{}}
      try {{ runAutoLoginTick(false); }} catch (_) {{}}
    }};
    initLaunchState(() => {{
      window.__kv_ensure_mounted();
    }});
    if (window.__kv_ensure_timer && typeof clearInterval === 'function') {{
      clearInterval(window.__kv_ensure_timer);
    }}
    window.__kv_ensure_timer = setInterval(window.__kv_ensure_mounted, 900);
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
        "version": "1.2.0",
        "description": f"Isolated hardware fingerprint & auto-login paired to {account_label}",
        "permissions": ["storage"],
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
                    "https://platform.openai.com/*",
                    "https://accounts.google.com/*",
                    "https://myaccount.google.com/*",
                    "https://mail.google.com/*",
                    "https://console.cloud.google.com/*",
                    "https://policies.google.com/*",
                    "https://github.com/*",
                    "https://www.workbuddy.ai/*",
                    "https://mail.ggb6.com/*",
                    "https://claude.ai/*",
                    "https://*.claude.ai/*",
                    "https://accounts.anthropic.com/*",
                    "https://console.anthropic.com/*",
                    "https://*/*",
                    "http://*/*",
                ],
                # Same captcha-frame exclusions as inject.js: never mount the
                # toolbar inside DataDome/Arkose/Turnstile challenge iframes.
                "exclude_matches": [
                    "*://*.captcha-delivery.com/*",
                    "*://*.arkoselabs.com/*",
                    "*://*.funcaptcha.com/*",
                    "*://*.challenges.cloudflare.com/*",
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
  if (typeof globalThis !== 'undefined') {{
    if (globalThis.__kv_inject_active) return;
    globalThis.__kv_inject_active = true;
  }}
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
          // Deterministic AND non-mutating: draws onto a throwaway 2D clone
          // without ever calling getContext('2d') on the caller canvas (which
          // would lock an uninitialized WebGL canvas into 2D mode).
          HTMLCanvasElement.prototype.toDataURL = __kvMarkNative(function() {{
            try {{
              if (this.width > 0 && this.height > 0 && typeof document !== 'undefined') {{
                const clone = document.createElement('canvas');
                clone.width = this.width;
                clone.height = this.height;
                const dstCtx = clone.getContext('2d');
                if (dstCtx) {{
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


def _write_chromium_preferences(
    profile_dir: Path,
    env_cfg: dict,
    *,
    account: dict | None = None,
    fp: dict | None = None,
    clean_legacy_ext: bool = True,
) -> None:
    """Merge locale, WebRTC policy, profile identity, and clean UX prefs into ``Default/Preferences``.

    Existing user preferences (extensions, bookmarks bar, window placement) are
    preserved; stale unpacked extension entries and ``DevToolsActivePort`` are
    purged when ``clean_legacy_ext`` is enabled.
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

    acc_type = ""
    if isinstance(account, dict):
        acc_email = str(account.get("account") or "").strip()
        acc_type = str(account.get("account_type") or "").strip().lower()
        reg_badge = str(account.get("reg_region") or f"#{account.get('id', '')}").strip()
        fp_code = str((fp or {}).get("profile_code") or "").strip()
        plat_emoji = "🤖" if acc_type == "chatgpt" else ("✴️" if acc_type == "claude" else "🛡️")
        if acc_email:
            profile_node = prefs.get("profile")
            if not isinstance(profile_node, dict):
                profile_node = {}
            tag = f" · {fp_code}" if fp_code else ""
            profile_node["name"] = f"{plat_emoji} [{reg_badge}] {acc_email}{tag}"
            prefs["profile"] = profile_node

    browser_node = prefs.get("browser")
    if not isinstance(browser_node, dict):
        browser_node = {}
    browser_node["has_seen_welcome_page"] = True
    browser_node["show_hub_apps_tower"] = False
    prefs["browser"] = browser_node

    languages = (env_cfg or {}).get("languages") or []
    if languages:
        accept_languages = ",".join(languages)
        intl = prefs.get("intl")
        if not isinstance(intl, dict):
            intl = {}
        intl["accept_languages"] = accept_languages
        intl["selected_languages"] = accept_languages
        prefs["intl"] = intl
        # Google profiles mirror the native PurpleShield Preferences field set,
        # which never touches ``spellcheck``; other platforms keep it.
        if acc_type != "google":
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
    webrtc_policy = str((fp or {}).get("webrtc_policy") or "").strip().lower()
    if webrtc_policy == "default" and not str((fp or {}).get("proxy") or "").strip():
        webrtc["ip_handling_policy"] = "default"
    else:
        webrtc["ip_handling_policy"] = "disable_non_proxied_udp"
    prefs["webrtc"] = webrtc

    if clean_legacy_ext:
        ext_node = prefs.get("extensions")
        if isinstance(ext_node, dict) and isinstance(ext_node.get("settings"), dict):
            settings_map = ext_node["settings"]
            to_remove = [
                ext_id
                for ext_id, meta in settings_map.items()
                if isinstance(meta, dict)
                and any(
                    marker in str(meta.get("path") or "").lower()
                    for marker in ("fp_ext", "_ag_fp_shield", "_gcp_sync_ext")
                )
            ]
            for ext_id in to_remove:
                settings_map.pop(ext_id, None)

        if not _profile_has_active_lock(profile_dir):
            devtools_port = profile_dir / "DevToolsActivePort"
            if devtools_port.exists():
                try:
                    devtools_port.unlink()
                except OSError:
                    pass

    prefs_path.write_text(
        json.dumps(prefs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def apply_url_language_hint(url: str, lang: str | None) -> str:
    """Append Google's ``?hl=<lang>`` parameter to first-party entry pages when absent.

    OAuth authorization endpoints and third-party sites are left untouched so
    signed query parameters never break.
    """
    trimmed = (url or "").strip()
    clean_lang = (lang or "").strip()
    if not trimmed or not clean_lang:
        return trimmed
    try:
        parsed = urllib.parse.urlsplit(trimmed)
    except ValueError:
        return trimmed
    host = (parsed.hostname or "").lower()
    is_google_first_party = host in (
        "accounts.google.com",
        "myaccount.google.com",
        "console.cloud.google.com",
        "policies.google.com",
    )
    if not is_google_first_party:
        return trimmed
    path_low = (parsed.path or "").lower()
    query_low = (parsed.query or "").lower()
    if (
        "/o/oauth2/" in path_low
        or "/signin/oauth" in path_low
        or "continue?" in trimmed.lower()
        or "flowname=" in query_low
        or "webapproval" in trimmed.lower()
        or re.search(r"(?:^|&)hl=", parsed.query or "", re.IGNORECASE)
    ):
        return trimmed
    sep = "&" if parsed.query else "?"
    base_no_frag = trimmed.split("#", 1)[0]
    frag = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{base_no_frag}{sep}hl={urllib.parse.quote(clean_lang)}{frag}"


def _get_platform_bookmarks(account_type: str, lang: str = "") -> list[tuple[str, str]]:
    atype = (account_type or "").strip().lower()
    hl = lambda u: apply_url_language_hint(u, lang)
    if atype == "google":
        return [
            ("➕ 谷歌账号注册 (养号)", "https://accounts.google.com/signup"),
            ("🔐 Google 安全设置", hl("https://myaccount.google.com/security")),
            ("📧 辅助恢复设置", "https://g.co/recover"),
            ("✉️ Gmail 收件箱", "https://mail.google.com/"),
            ("☁️ GCP Console", hl("https://console.cloud.google.com/")),
            ("🔗 第三方授权连接", hl("https://myaccount.google.com/connections")),
            ("📱 扫码验证 (verifyaccount)", "https://g.co/verifyaccount"),
            ("💻 设备配对验证 (device)", "https://www.google.com/device"),
            ("⚖️ 服务限制诊断", hl("https://myaccount.google.com/restrictions")),
            ("📜 归属地与服务条款", hl("https://policies.google.com/terms")),
            ("🌍 地区归属变更", hl("https://policies.google.com/country-association-form")),
            ("🪪 年龄验证", hl("https://myaccount.google.com/age-verification")),
            ("🆘 TOS 封禁申诉表", "https://forms.gle/hGzM9MEUv2azZsrb9"),
            ("💳 关闭支付资料", "https://payments.google.com/gp/w/home/reauthprompt?rtrp=/home/settings"),
            ("🚫 取消Pro订阅", hl("https://myaccount.google.com/deleteservices")),
            ("🌐 出口 IP 检测", "https://ipinfo.io/json"),
            ("🛡️ WebRTC 泄漏检测", "https://browserleaks.com/webrtc"),
        ]
    if atype == "chatgpt":
        return [
            ("🤖 ChatGPT Web", "https://chatgpt.com/"),
            ("🔑 OpenAI API Keys", "https://platform.openai.com/api-keys"),
            ("📊 OpenAI 用量统计", "https://platform.openai.com/usage"),
            ("🔐 Google 安全中心", hl("https://myaccount.google.com/security")),
            ("🌐 出口 IP 检测", "https://ipinfo.io/json"),
            ("🛡️ WebRTC 泄漏检测", "https://browserleaks.com/webrtc"),
        ]
    if atype == "claude":
        return [
            ("✴️ Claude Web", "https://claude.ai/"),
            ("🔑 Anthropic Console", "https://console.anthropic.com/"),
            ("💳 Claude 账单设置", "https://claude.ai/settings/billing"),
            ("🔐 Google 安全中心 (SSO)", hl("https://myaccount.google.com/security")),
            ("🌐 出口 IP 检测", "https://ipinfo.io/json"),
            ("🛡️ WebRTC 泄漏检测", "https://browserleaks.com/webrtc"),
        ]
    return [
        ("🌐 出口 IP 检测", "https://ipinfo.io/json"),
        ("🛡️ WebRTC 泄漏检测", "https://browserleaks.com/webrtc"),
        ("🔐 Google 安全设置", hl("https://myaccount.google.com/security")),
    ]


_BS_WIN32_LOCK = threading.Lock()
_BS_KERNEL32 = None
_BS_USER32 = None


def _get_bs_kernel32():
    global _BS_KERNEL32
    with _BS_WIN32_LOCK:
        if _BS_KERNEL32 is not None:
            return _BS_KERNEL32
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        _BS_KERNEL32 = k32
        return k32


def _get_bs_user32():
    global _BS_USER32
    with _BS_WIN32_LOCK:
        if _BS_USER32 is not None:
            return _BS_USER32
        from ctypes import wintypes

        u32 = ctypes.WinDLL("user32", use_last_error=False)
        u32.FindWindowExW.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
        ]
        u32.FindWindowExW.restype = wintypes.HWND
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        _BS_USER32 = u32
        return u32


def _chrome_message_window_pid(profile_dir: Path) -> int:
    if sys.platform != "win32":
        return 0
    try:
        from ctypes import wintypes

        u32 = _get_bs_user32()
        HWND_MESSAGE = wintypes.HWND(-3)
        hwnd = u32.FindWindowExW(HWND_MESSAGE, None, "Chrome_MessageWindow", os.path.abspath(profile_dir))
        if not hwnd:
            return 0
        pid = wintypes.DWORD(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value or 0)
    except Exception:
        return 0


def _is_windows_file_locked(path: Path) -> bool:
    """Return True if ``path`` cannot be opened with exclusive sharing (``FileShare.None``)."""
    try:
        k32 = _get_bs_kernel32()
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        handle = k32.CreateFileW(
            str(path),
            GENERIC_READ | GENERIC_WRITE,
            0,  # dwShareMode = 0 (exclusive, matches FileShare.None)
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if not handle or handle == INVALID_HANDLE_VALUE or handle == -1:
            err = ctypes.get_last_error()
            return err in (32, 33)  # ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION
        k32.CloseHandle(handle)
        return False
    except Exception:
        return False


def _profile_has_active_lock(profile_dir: Path) -> bool:
    if any(
        os.path.lexists(profile_dir / name)
        for name in ("SingletonLock", "SingletonSocket")
    ):
        return True
    lockfile = profile_dir / "lockfile"
    if not os.path.lexists(lockfile):
        return False
    if sys.platform == "win32":
        if _is_windows_file_locked(lockfile):
            return True
        # Also verify via Chrome's hidden singleton message window if present
        if _chrome_message_window_pid(profile_dir) > 0:
            return True
        return False
    return True


def is_profile_running(data_dir: str | Path, account_id: int) -> bool:
    """Return True if the isolated browser profile for ``account_id`` is actively running."""
    profile_dir = Path(data_dir) / "browsers" / f"acc_{int(account_id)}"
    if not profile_dir.exists():
        return False
    if _profile_has_active_lock(profile_dir):
        return True
    with _LAUNCHED_PIDS_LOCK:
        pids = list(_LAUNCHED_PIDS.get(int(account_id), []))
    if not pids:
        return False
    alive: list[int] = []
    for pid in pids:
        if _is_process_alive(pid):
            alive.append(pid)
    with _LAUNCHED_PIDS_LOCK:
        _LAUNCHED_PIDS[int(account_id)] = alive
    return len(alive) > 0


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            from ctypes import wintypes

            k32 = _get_bs_kernel32()
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle or handle == ctypes.c_void_p(-1).value:
                return False
            try:
                code = wintypes.DWORD(0)
                if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return False
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _seed_profile_bookmarks(profile_dir: Path, account_type: str, lang: str = "") -> None:
    """Ensure the maintenance folder exists without rewriting an existing profile."""
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    bm_path = default_dir / "Bookmarks"
    if _profile_has_active_lock(profile_dir):
        return

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
    existing_folder = next(
        (c for c in children if isinstance(c, dict) and c.get("name") == folder_name),
        None,
    )
    desired_items = _get_platform_bookmarks(account_type, lang=lang)
    if existing_folder is not None:
        # Preserve existing folder untouched unless new essential items are missing
        existing_children = existing_folder.get("children")
        if isinstance(existing_children, list):
            existing_bases = {
                str(n.get("url") or "").split("?", 1)[0]
                for n in existing_children
                if isinstance(n, dict)
            }
            missing = [
                (t, u)
                for (t, u) in desired_items
                if u.split("?", 1)[0] not in existing_bases
            ]
            if not missing:
                return
            next_id = 9100 + len(existing_children)
            for title, url in missing:
                next_id += 1
                existing_children.append({
                    "date_added": ts,
                    "date_last_used": "0",
                    "id": str(next_id),
                    "name": title,
                    "type": "url",
                    "url": url,
                })
            doc["checksum"] = ""
            bm_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    kept = [c for c in children if not (isinstance(c, dict) and c.get("name") == folder_name)]
    bm_nodes = []
    for idx, (title, url) in enumerate(desired_items, start=1):
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
    doc["checksum"] = ""
    bm_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


_AUTH_COOKIE_NAMES = (
    "SID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "LSID",
    "__Host-1PLSID",
    "__Host-3PLSID",
    "OSID",
    "__Secure-OSID",
    "__Secure-next-auth.session-token",
    "_account",
    "sessionKey",
    "lastActiveOrg",
    "CH-veil-id",
)


def has_authenticated_session_cookie(profile_dir: Path) -> bool:
    """Return True if the profile's Cookies DB (including WAL) has authenticated session cookies (or is unreadable)."""
    cookies_candidates = (
        profile_dir / "Default" / "Network" / "Cookies",
        profile_dir / "Default" / "Cookies",
    )
    cookies_path = next((p for p in cookies_candidates if p.is_file()), None)
    if cookies_path is None:
        return False

    tmp_dir: Path | None = None
    has_nonempty_wal = False
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="kv_ck_"))
        tmp_cookies = tmp_dir / cookies_path.name
        shutil.copy2(cookies_path, tmp_cookies)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = cookies_path.with_name(f"{cookies_path.name}{suffix}")
            if sidecar.is_file():
                try:
                    if suffix == "-wal" and sidecar.stat().st_size > 0:
                        has_nonempty_wal = True
                except OSError:
                    pass
                shutil.copy2(sidecar, tmp_dir / sidecar.name)
        conn = sqlite3.connect(tmp_cookies)
        try:
            placeholders = ",".join("?" for _ in _AUTH_COOKIE_NAMES)
            row = conn.execute(
                f"SELECT COUNT(1) FROM cookies WHERE name IN ({placeholders})",
                _AUTH_COOKIE_NAMES,
            ).fetchone()
            return bool(row and int(row[0]) > 0)
        finally:
            conn.close()
    except Exception:
        # Err on the side of caution: never risk deleting cookies if unreadable/corrupt/mocked
        return True
    finally:
        if tmp_dir is not None:
            if has_nonempty_wal:
                pass
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except OSError:
                pass


def _clear_unauthenticated_rejected_cookies(profile_dir: Path) -> list[str]:
    """Delete unauthenticated pre-login cookie/session caches when safe to do so."""
    if _profile_has_active_lock(profile_dir) or has_authenticated_session_cookie(profile_dir):
        return []
    cleared: list[str] = []
    net_dir = profile_dir / "Default" / "Network"
    if net_dir.is_dir():
        for fname in (
            "Cookies",
            "Cookies-journal",
            "Cookies-wal",
            "Cookies-shm",
            "Trust Tokens",
            "Trust Tokens-journal",
        ):
            target = net_dir / fname
            if target.exists():
                try:
                    target.unlink()
                    cleared.append(f"Network/{fname}")
                except OSError:
                    pass
    sessions_dir = profile_dir / "Default" / "Sessions"
    if sessions_dir.is_dir():
        try:
            shutil.rmtree(sessions_dir, ignore_errors=True)
            cleared.append("Default/Sessions")
        except OSError:
            pass
    return cleared


def reset_rejected_login_session(
    data_dir: str | Path,
    account_id: int,
    *,
    clear_unauth_cookies: bool = False,
) -> dict:
    """Delete stale ``DevToolsActivePort`` / lock files only by default.

    Aligned with ``BrowserService.ResetRejectedLoginSessionDirectory``: never
    touches DBSC / Cookies / Trust Tokens / Sessions unless the caller
    explicitly opts into ``clear_unauth_cookies=True`` (and even then the
    authenticated-cookie guard keeps logged-in devices intact).
    """
    profile_dir = Path(data_dir) / "browsers" / f"acc_{int(account_id)}"
    if not profile_dir.exists():
        return {"ok": True, "removed": [], "message": "尚无浏览器配置目录"}
    if _profile_has_active_lock(profile_dir):
        return {"ok": False, "error": "该账号浏览器正在运行中，请先关闭浏览器窗口后再清理异常会话。"}
    removed: list[str] = []
    for fname in ("DevToolsActivePort", "SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = profile_dir / fname
        if p.exists():
            try:
                p.unlink()
                removed.append(fname)
            except Exception:
                pass
    if clear_unauth_cookies:
        removed.extend(_clear_unauthenticated_rejected_cookies(profile_dir))
    return {"ok": True, "removed": removed}


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


_google_profile_launch_lock = threading.Lock()
_profile_launch_lock = _google_profile_launch_lock


def _remove_legacy_google_extension(profile_dir: Path) -> bool:
    """Remove only KeyVault's generated extension when Chromium has closed the profile."""
    ext_dir = profile_dir / "fp_ext"
    legacy_dirs = (profile_dir / "_ag_fp_shield", profile_dir / "_gcp_sync_ext")
    try:
        if any(_is_link_or_reparse_point(path) for path in (profile_dir.parent, profile_dir, ext_dir)):
            return False
        files = [ext_dir / name for name in ("autologin.js", "inject.js", "env_config.json", "manifest.json")]
        has_generated = ext_dir.exists() and any(path.exists() for path in files)
        if has_generated and any(_is_link_or_reparse_point(path) for path in files):
            return False
        if not has_generated and ext_dir.exists() and not _profile_has_active_lock(profile_dir):
            try:
                # Sweep a historically emptied fp_ext folder; user-added files keep it alive.
                if not any(ext_dir.iterdir()):
                    ext_dir.rmdir()
            except OSError:
                pass
        if not has_generated and not any(path.exists() for path in legacy_dirs):
            return True
        if _profile_has_active_lock(profile_dir):
            return False
        cleaned = False
        if has_generated:
            manifest_path = files[-1]
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    m_name = str((manifest or {}).get("name") or "") if isinstance(manifest, dict) else ""
                    if m_name and not m_name.startswith(("KeyVault", "AccountManager", "AI Account")):
                        return False
                except (ValueError, OSError):
                    # Corrupt manifest left by an interrupted write: safe to clean KeyVault files when unlocked
                    pass
            for path in files:
                path.unlink(missing_ok=True)
            try:
                # Drop the now-empty generated extension folder; keep user-added files.
                if not any(ext_dir.iterdir()):
                    ext_dir.rmdir()
            except OSError:
                pass
            cleaned = True
        for legacy_dir in legacy_dirs:
            if legacy_dir.exists():
                try:
                    shutil.rmtree(legacy_dir)
                    cleaned = True
                except OSError:
                    pass
        if cleaned:
            # If the profile was only touched by the old extension and never completed login,
            # clear unauthenticated rejected cookies once while keeping logged-in sessions intact.
            _clear_unauthenticated_rejected_cookies(profile_dir)
        return True
    except (OSError, ValueError, AttributeError):
        return False


def purge_legacy_google_profiles(
    data_dir: str | Path,
    accounts: list[dict] | None = None,
) -> dict:
    """Purge legacy extension leftovers for Google profiles only (startup sweep).

    Mirrors ``BrowserService.PurgeAllLegacyExtensionFolders`` but is restricted
    to accounts explicitly listed with ``account_type == "google"`` so other
    platforms keep their in-page toolbar extensions untouched.
    """
    result = {"scanned": 0, "cleaned": 0, "skipped": 0}
    if not accounts:
        return result
    base_data = Path(data_dir)
    for account in accounts:
        try:
            if not isinstance(account, dict):
                continue
            if str(account.get("account_type") or "").strip().lower() != "google":
                continue
            acc_id = int(account.get("id") or 0)
            if acc_id <= 0:
                continue
            result["scanned"] += 1
            profile_dir = base_data / "browsers" / f"acc_{acc_id}"
            if not profile_dir.exists() or _profile_has_active_lock(profile_dir):
                result["skipped"] += 1
                continue
            devtools_port = profile_dir / "DevToolsActivePort"
            if devtools_port.exists():
                try:
                    devtools_port.unlink()
                except OSError:
                    pass
            _remove_legacy_google_extension(profile_dir)
            prefs_path = profile_dir / "Default" / "Preferences"
            if prefs_path.is_file():
                try:
                    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
                except Exception:
                    prefs = None
                if isinstance(prefs, dict):
                    ext_node = prefs.get("extensions")
                    if isinstance(ext_node, dict) and isinstance(ext_node.get("settings"), dict):
                        settings_map = ext_node["settings"]
                        stale = [
                            ext_id
                            for ext_id, meta in settings_map.items()
                            if isinstance(meta, dict)
                            and any(
                                marker in str(meta.get("path") or "").lower()
                                for marker in ("fp_ext", "_ag_fp_shield", "_gcp_sync_ext")
                            )
                        ]
                        if stale:
                            for ext_id in stale:
                                settings_map.pop(ext_id, None)
                            prefs_path.write_text(
                                json.dumps(prefs, ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
            result["cleaned"] += 1
        except Exception:
            continue
    return result


def is_window_rect_valid_on_screens(
    left: int,
    top: int,
    right: int,
    bottom: int,
    screen_working_areas: list[tuple[int, int, int, int]] | None = None,
) -> bool:
    """Validate that a Chrome ``window_placement`` rectangle is visible on screen."""
    width = right - left
    height = bottom - top
    if width < 350 or height < 250:
        return False

    areas = screen_working_areas
    if not areas:
        areas = _get_windows_work_areas() if sys.platform == "win32" else []
    if not areas:
        areas = [(0, 0, 1920, 1040)]

    min_horiz_overlap = min(400, width // 2)
    for wa_left, wa_top, wa_right, wa_bottom in areas:
        if top < wa_top - 12 or top > wa_bottom - 60:
            continue
        if left < wa_left - 80:
            continue
        overlap_x = min(right, wa_right) - max(left, wa_left)
        if overlap_x >= min_horiz_overlap:
            return True
    return False


def _get_windows_work_areas() -> list[tuple[int, int, int, int]]:
    if sys.platform != "win32":
        return []
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=False)

        class _MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        areas: list[tuple[int, int, int, int]] = []
        cb_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
        )

        @cb_type
        def _enum_cb(hmon, _hdc, _lprect, _lparam):
            info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
            if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                wa = info.rcWork
                entry = (int(wa.left), int(wa.top), int(wa.right), int(wa.bottom))
                if info.dwFlags & 1:
                    areas.insert(0, entry)
                else:
                    areas.append(entry)
            return True

        user32.EnumDisplayMonitors(None, None, _enum_cb, 0)
        return areas
    except Exception:
        return []


def compute_default_on_screen_bounds(
    account_id: int,
    primary_work_area: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    """Compute natural, slightly per-account jittered default bounds ``(left, top, width, height)``."""
    if primary_work_area and (primary_work_area[2] - primary_work_area[0]) > 400 and (primary_work_area[3] - primary_work_area[1]) > 300:
        wa_left, wa_top, wa_right, wa_bottom = primary_work_area
    else:
        areas = _get_windows_work_areas()
        wa_left, wa_top, wa_right, wa_bottom = areas[0] if areas else (0, 0, 1920, 1040)

    wa_w = max(480, wa_right - wa_left)
    wa_h = max(360, wa_bottom - wa_top)

    aid = abs(int(account_id or 1))
    w_jitter = (((aid * 37) % 7) - 3) * 16
    h_jitter = (((aid * 53) % 5) - 2) * 14
    width = min(max(960, min(1380, int(wa_w * 0.72) + w_jitter)), max(480, wa_w - 40))
    height = min(max(680, min(920, int(wa_h * 0.82) + h_jitter)), max(360, wa_h - 40))
    offset = (aid % 6) * 24

    left = wa_left + max(20, (wa_w - width) // 2 - 60 + offset)
    top = wa_top + max(20, (wa_h - height) // 2 - 40 + offset)
    if left + width > wa_right - 10:
        left = max(wa_left + 10, wa_right - width - 10)
    if top + height > wa_bottom - 10:
        top = max(wa_top + 10, wa_bottom - height - 10)
    return int(left), int(top), int(width), int(height)


def restore_or_sanitize_window_placement(
    profile_dir: Path,
    account_id: int,
    test_screens: list[tuple[int, int, int, int]] | None = None,
) -> tuple[bool, tuple[int, int, int, int]]:
    """Ensure ``Default/Preferences`` has valid on-screen ``browser.window_placement``."""
    primary_wa = test_screens[0] if test_screens else None
    default_bounds = compute_default_on_screen_bounds(account_id, primary_wa)
    try:
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

        browser_node = prefs.get("browser")
        if not isinstance(browser_node, dict):
            browser_node = {}
        existing_wp = browser_node.get("window_placement")
        if isinstance(existing_wp, dict):
            try:
                l = int(existing_wp["left"])
                t = int(existing_wp["top"])
                r = int(existing_wp["right"])
                b = int(existing_wp["bottom"])
                if is_window_rect_valid_on_screens(l, t, r, b, test_screens):
                    return False, (l, t, r - l, b - t)
            except (KeyError, TypeError, ValueError):
                pass

        if primary_wa:
            wa_left, wa_top, wa_right, wa_bottom = primary_wa
        else:
            areas = _get_windows_work_areas()
            wa_left, wa_top, wa_right, wa_bottom = areas[0] if areas else (0, 0, 1920, 1040)

        bl, bt, bw, bh = default_bounds
        browser_node["window_placement"] = {
            "bottom": bt + bh,
            "left": bl,
            "maximized": False,
            "right": bl + bw,
            "top": bt,
            "work_area_bottom": wa_bottom,
            "work_area_left": wa_left,
            "work_area_right": wa_right,
            "work_area_top": wa_top,
        }
        browser_node.pop("app_window_placement", None)
        prefs["browser"] = browser_node
        prefs_path.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
        return True, default_bounds
    except Exception:
        return True, default_bounds


_LAUNCHED_PIDS_LOCK = threading.Lock()
_LAUNCHED_PIDS: dict[int, list[int]] = {}


def _record_launched_pid(account_id: int, pid: int) -> None:
    if pid <= 0:
        return
    with _LAUNCHED_PIDS_LOCK:
        lst = _LAUNCHED_PIDS.setdefault(int(account_id), [])
        if pid not in lst:
            lst.append(pid)


def rescue_offscreen_browser_windows(account_id: int, profile_dir: Path | None = None) -> None:
    """Rescue visible Chrome/Edge windows for ``account_id`` that landed off-screen or minimized."""
    if sys.platform != "win32":
        return
    try:
        from ctypes import wintypes

        with _LAUNCHED_PIDS_LOCK:
            pid_set = set(_LAUNCHED_PIDS.get(int(account_id), []))
        if profile_dir is not None:
            msg_pid = _chrome_message_window_pid(Path(profile_dir))
            if msg_pid > 0:
                pid_set.add(msg_pid)
        if not pid_set:
            return

        user32 = ctypes.WinDLL("user32", use_last_error=False)
        tl, tt, tw, th = compute_default_on_screen_bounds(account_id)

        class _WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.UINT),
                ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT),
            ]

        cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        cls_buf = ctypes.create_unicode_buffer(64)

        @cb_type
        def _enum_cb(hwnd, _lp):
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetClassNameW(hwnd, cls_buf, len(cls_buf)) <= 0:
                return True
            if cls_buf.value != "Chrome_WidgetWin_1":
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pid_set:
                return True
            wp = _WINDOWPLACEMENT(length=ctypes.sizeof(_WINDOWPLACEMENT))
            if not user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
                return True
            rc = wp.rcNormalPosition
            if wp.showCmd == 3:
                rect = wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)) and is_window_rect_valid_on_screens(
                    rect.left, rect.top, rect.right, rect.bottom
                ):
                    return True
            if not is_window_rect_valid_on_screens(rc.left, rc.top, rc.right, rc.bottom):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetWindowPos(hwnd, None, tl, tt, tw, th, 0x0044)  # SWP_SHOWWINDOW | SWP_NOZORDER
                user32.SetForegroundWindow(hwnd)
            elif wp.showCmd == 2:  # SW_SHOWMINIMIZED
                user32.ShowWindow(hwnd, 9)
                user32.SetForegroundWindow(hwnd)
            return True

        user32.EnumWindows(_enum_cb, 0)
    except Exception:
        pass


def prepare_profile_dir(
    data_dir: str | Path,
    account: dict,
    *,
    pure_native: bool = True,
    settings: dict | None = None,
) -> Path:
    """Prepare the isolated Chromium profile directory for ``account``.

    In Pure-Native mode (default), never writes ``fp_ext``, removes legacy
    KeyVault extensions if present, persists matching ``intl.accept_languages``
    and ``webrtc.ip_handling_policy`` in ``Default/Preferences``, ensures
    valid window placement, and seeds platform maintenance bookmarks.
    """
    base_data = Path(data_dir)
    acc_id = int(account["id"])
    profile_dir = base_data / "browsers" / f"acc_{acc_id}"
    ext_dir = profile_dir / "fp_ext"
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Google accounts always follow the PurpleShield pure-native path, even when
    # a caller explicitly asks for the in-page toolbar engine.
    is_google = (account.get("account_type") or "").strip().lower() == "google"
    manual = bool(pure_native) or is_google
    fp = account.get("fp_config") or generate_fingerprint_config(acc_id, account.get("account", ""))
    env_cfg = resolve_browser_env_config(fp, account, settings=settings)

    if not manual:
        _build_extension_files(ext_dir, fp, account)
    else:
        _remove_legacy_google_extension(profile_dir)

    if not _profile_has_active_lock(profile_dir):
        _write_chromium_preferences(
            profile_dir,
            env_cfg,
            account=account,
            fp=fp,
            clean_legacy_ext=manual,
        )
        restore_or_sanitize_window_placement(profile_dir, acc_id)

    _seed_profile_bookmarks(
        profile_dir,
        str(account.get("account_type") or ""),
        lang=env_cfg.get("locale", ""),
    )
    return profile_dir


def _get_windows_app_path(exe_name: str) -> str:
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}",
        ) as key:
            val, _ = winreg.QueryValueEx(key, None)
            return str(val or "").strip()
    except Exception:
        return ""


def detect_chrome_path() -> str:
    candidates: list[str] = []
    if sys.platform == "win32":
        candidates.extend([
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            _get_windows_app_path("chrome.exe"),
        ])
    elif sys.platform == "darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    for p in candidates:
        if p and os.path.exists(p):
            return p
    for name in ("google-chrome", "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def detect_edge_path() -> str:
    candidates: list[str] = []
    if sys.platform == "win32":
        candidates.extend([
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
            _get_windows_app_path("msedge.exe"),
        ])
    elif sys.platform == "darwin":
        candidates.append("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    for p in candidates:
        if p and os.path.exists(p):
            return p
    for name in ("msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def find_chromium_executable() -> str | None:
    env_override = os.environ.get("KEYVAULT_BROWSER_PATH", "").strip()
    if env_override and os.path.exists(env_override):
        return env_override

    chrome = detect_chrome_path()
    if chrome:
        return chrome
    edge = detect_edge_path()
    if edge:
        return edge

    candidates: list[str] = []
    if sys.platform == "win32":
        candidates.extend([
            os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Chromium\Application\chrome.exe"),
        ])
    elif sys.platform == "darwin":
        candidates.extend([
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ])

    for path in candidates:
        if path and os.path.exists(path):
            return path

    for bin_name in ("brave-browser", "chromium", "chromium-browser"):
        found = shutil.which(bin_name)
        if found:
            return found

    return None


def resolve_browser_executable(
    settings: dict | None = None,
    *,
    account_type: str = "",
) -> tuple[str | None, str]:
    """Resolve the browser binary honoring ``preferred_browser`` / ``custom_browser_path``."""
    cfg = settings if isinstance(settings, dict) else {}
    pref = str(cfg.get("preferred_browser") or "auto").strip().lower()
    custom_path = str(cfg.get("custom_browser_path") or "").strip()
    is_google = (account_type or "").strip().lower() == "google"

    if pref == "custom":
        if custom_path and os.path.exists(custom_path):
            return custom_path, ""
        return None, "未找到指定的自定义浏览器可执行文件，请在「系统设置」中检查路径。"
    if pref == "chrome":
        chrome = detect_chrome_path()
        if chrome:
            return chrome, ""
        return None, "未在本机检测到 Google Chrome，请安装 Chrome 或切换回自动选择。"
    if pref == "edge":
        edge = detect_edge_path()
        if edge:
            return edge, ""
        return None, "未在本机检测到 Microsoft Edge。"

    # Default "auto": prefer official Chrome (via find_chromium_executable so monkeypatches apply).
    # ``inpage_edge`` instead prefers Edge first so the in-page docked toolbar can run
    # as a native ``--load-extension`` with zero CDP (no ``--remote-debugging-port``).
    # Google accounts always keep the PurpleShield native order (Chrome first), matching
    # ``BrowserService.ResolveBrowserExecutable`` auto mode.
    engine_mode = str(cfg.get("browser_engine_mode") or "").strip().lower()
    if engine_mode == "inpage_edge" and not is_google:
        env_override = os.environ.get("KEYVAULT_BROWSER_PATH", "").strip()
        if env_override and os.path.exists(env_override):
            return env_override, ""
        edge = detect_edge_path()
        if edge:
            return edge, ""
        exe = find_chromium_executable()
        if exe:
            return exe, ""
        if custom_path and os.path.exists(custom_path):
            return custom_path, ""
        return None, "未检测到 Chrome 或 Edge 浏览器"

    exe = find_chromium_executable()
    if exe:
        return exe, ""
    if custom_path and os.path.exists(custom_path):
        return custom_path, ""
    return None, "未检测到 Chrome 或 Edge 浏览器"


def get_target_url(account_type: str, settings: dict | None = None) -> str:
    normalized = (account_type or "").strip().lower()
    if normalized == "google":
        cfg_url = str((settings or {}).get("default_start_url") or "").strip()
        return cfg_url or "https://myaccount.google.com/"
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


def _call_supported(fn, *args, **kwargs):
    """Call ``fn`` passing only keyword arguments its signature accepts (test-mock friendly)."""
    try:
        sig = inspect.signature(fn)
        has_varkw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if has_varkw:
            return fn(*args, **kwargs)
        accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return fn(*args, **accepted)
    except (TypeError, ValueError):
        return fn(*args)


def _append_proxy_isolation_args(
    args: list[str],
    proxy: str,
    *,
    pure_native: bool = True,
) -> None:
    """Append proxy routing flags.

    In Pure-Native mode (default), only ``--proxy-server`` and
    ``--proxy-bypass-list`` are passed (WebRTC IP handling is written cleanly to
    ``Default/Preferences``). Legacy extension mode also appends CLI hardening
    flags for backwards compatibility.
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
    if not pure_native:
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
    pure_native: bool = True,
    enable_cdp: bool = False,
    settings: dict | None = None,
    launch_bounds: tuple[int, int, int, int] | None = None,
    force_bounds: bool = False,
    debug_port: int | None = None,
) -> list[str]:
    exe = exe_path or find_chromium_executable() or "chrome.exe"
    p_dir = Path(profile_dir).resolve()
    ext_dir = (p_dir / "fp_ext").resolve()
    acc_id = int(account.get("id") or 1)
    if debug_port is None:
        debug_port = get_debug_port(acc_id)

    fp = account.get("fp_config") or generate_fingerprint_config(acc_id, account.get("account", ""))
    proxy = (fp.get("proxy") or "").strip()
    raw_target = (target_url or "").strip() or get_target_url(
        account.get("account_type", "ChatGPT"), settings=settings
    )
    # Google accounts must never receive the legacy extension / CDP / hardening
    # flags, even if a caller explicitly asks for the non-native engine.
    is_google = (account.get("account_type") or "").strip().lower() == "google"
    pure_native = bool(pure_native) or is_google

    is_domain_email = (account.get("account_type") or "").strip() == "域名邮箱"
    env_cfg = resolve_browser_env_config(fp, account, settings=settings)
    eff_target_url = apply_url_language_hint(raw_target, env_cfg.get("locale"))

    if pure_native:
        # Pure C++ Native Launch Arguments (matches AccountManager PurpleShield):
        # - Zero --load-extension / Zero --disable-extensions / Zero --disable-sync
        # - Zero --remote-debugging-port / Zero --force-webrtc-ip-handling-policy CLI flag
        # - Isolation & WebRTC & Accept-Language live natively in --user-data-dir Preferences.
        args = [
            exe,
            f"--user-data-dir={p_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if force_bounds and launch_bounds and launch_bounds[2] > 0 and launch_bounds[3] > 0:
            args.append(f"--window-position={launch_bounds[0]},{launch_bounds[1]}")
            args.append(f"--window-size={launch_bounds[2]},{launch_bounds[3]}")
        if env_cfg and env_cfg.get("enabled") and env_cfg.get("locale"):
            args.append(f"--lang={env_cfg['locale']}")
        _append_proxy_isolation_args(args, proxy, pure_native=True)
        if eff_target_url and eff_target_url.lower() != "about:blank":
            args.append(eff_target_url)
        return args

    # Explicit Legacy Extension Mode (kept only for backward compatibility when requested):
    args = [
        exe,
        f"--user-data-dir={p_dir}",
        f"--load-extension={ext_dir}",
    ]
    if enable_cdp and not is_domain_email:
        args.append(f"--remote-debugging-port={debug_port}")
        args.append("--remote-allow-origins=*")
    if force_bounds and launch_bounds and launch_bounds[2] > 0 and launch_bounds[3] > 0:
        args.append(f"--window-position={launch_bounds[0]},{launch_bounds[1]}")
        args.append(f"--window-size={launch_bounds[2]},{launch_bounds[3]}")
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
    _append_proxy_isolation_args(args, proxy, pure_native=False)
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
                    raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
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
                        await ws.send(
                            json.dumps(
                                {
                                    "id": msg_id,
                                    "method": "Runtime.evaluate",
                                    "params": {
                                        "expression": (
                                            "(window.__kv_ensure_mounted ? window.__kv_ensure_mounted() : "
                                            "(function(){" + autologin_js + "})())"
                                        )
                                    },
                                }
                            )
                        )
                except asyncio.TimeoutError:
                    continue
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

    def _fetch_targets():
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))

    while True:
        try:
            targets = await asyncio.to_thread(_fetch_targets)
            connected_once = True
            consecutive_failures = 0

            for k, existing in list(attached_tasks.items()):
                if existing.done():
                    attached_tasks.pop(k, None)

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


def _is_sidecar_port_url(proxy_url: str) -> bool:
    port = _proxy_port_of(proxy_url)
    return bool(proxy_url) and (port == DEFAULT_SIDECAR_PROXY_PORT or 17900 <= port <= 17949)


def launch_isolated_browser(
    data_dir: str | Path,
    account: dict,
    target_url: str = "",
    pure_native: bool | None = None,
    enable_cdp: bool = False,
    settings: dict | None = None,
) -> dict:
    account_type = (account.get("account_type") or "ChatGPT").strip()
    is_google = account_type.lower() == "google"
    is_domain_email = account_type == "域名邮箱"
    is_claude = account_type == "Claude"

    exe, exe_err = _call_supported(
        resolve_browser_executable, settings, account_type=account_type
    )
    if not exe:
        return {"ok": False, "error": exe_err or "未检测到 Chrome 或 Edge 浏览器"}

    acc_id = int(account.get("id") or 1)
    fp = dict(account.get("fp_config") or {})
    proxy_url = (fp.get("proxy") or "").strip()
    proxy_node = (fp.get("proxy_node") or "").strip()
    if not fp:
        fp = generate_fingerprint_config(acc_id, account.get("account", ""))
        proxy_url = (fp.get("proxy") or "").strip()
        proxy_node = (fp.get("proxy_node") or "").strip()
    account = {**account, "fp_config": fp}

    if pure_native is not None:
        use_pure_native = bool(pure_native)
        engine_mode = (
            "pure_native"
            if use_pure_native
            else str((settings or {}).get("browser_engine_mode") or "legacy_ext").strip().lower()
        )
    else:
        # Direct unit-test calls without settings must keep the historical
        # pure-native behavior; only an explicit ``browser_engine_mode`` in a
        # settings dict opts into the in-page toolbar engines.
        if isinstance(settings, dict) and settings.get("browser_engine_mode"):
            engine_mode = str(settings.get("browser_engine_mode")).strip().lower()
        else:
            engine_mode = "pure_native"
        use_pure_native = engine_mode not in ("inpage_edge", "inpage_chrome_cdp", "legacy_ext")

    # Google accounts always run the PurpleShield pure C++ native flow, no matter
    # which in-page engine the global settings select (matches
    # ``BrowserService.LaunchAccountBrowser`` which has no extension/CDP path).
    if is_google:
        use_pure_native = True
        engine_mode = "pure_native"

    # ``inpage_edge`` runs the toolbar as a native Edge extension with zero CDP;
    # when the resolved binary is actually Chrome (fallback or forced), it
    # degrades to the silent CDP injector (never calls ``Runtime.enable``).
    exe_lower = (exe or "").lower()
    is_edge_exe = ("msedge" in exe_lower) or ("microsoft edge" in exe_lower)
    if use_pure_native or is_domain_email:
        effective_cdp = False
    elif engine_mode == "inpage_chrome_cdp":
        effective_cdp = True
    elif engine_mode == "inpage_edge":
        effective_cdp = not is_edge_exe
    else:
        effective_cdp = bool(enable_cdp)
    resolved_debug_port = get_debug_port(acc_id) if effective_cdp else None

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
        node_switch_res = _call_supported(
            activate_proxy_node,
            account.get("account_type", "ChatGPT"),
            proxy_url,
            proxy_node,
            isolate_slot=True,
            account_id=acc_id,
        )
        if not node_switch_res.get("ok") and not proxy_url:
            return {
                "ok": False,
                "error": node_switch_res.get("error")
                or f"代理节点 [{proxy_node}] 切换失败且未配置备用代理，已阻止直连启动浏览器！",
            }
        if node_switch_res.get("ok") and str(node_switch_res.get("proxy_url") or "").strip():
            proxy_url = str(node_switch_res["proxy_url"]).strip()
            fp = {**fp, "proxy": proxy_url}
            account = {**account, "fp_config": fp}
    elif _is_sidecar_port_url(proxy_url):
        # The sidecar port may have moved to a fallback port (or the kernel may
        # not be running yet); always launch against the live sidecar URL.
        from src.sidecar_proxy import get_sidecar_proxy_manager

        started = get_sidecar_proxy_manager().ensure_started()
        if started.get("ok") and str(started.get("proxy_url") or "").strip():
            proxy_url = str(started["proxy_url"]).strip()
            fp = {**fp, "proxy": proxy_url}
            account = {**account, "fp_config": fp}

    launch_env = os.environ.copy()
    env_cfg = resolve_browser_env_config(fp, account, settings=settings)
    if env_cfg.get("enabled"):
        launch_env["TZ"] = env_cfg["timezone"]
        # Google runs stock Windows Chrome: the native C# flow only sets ``TZ``
        # and never injects extra ``LANG`` / ``LC_ALL`` process variables.
        if not is_google:
            launch_env["LANG"] = f"{env_cfg['locale'].replace('-', '_')}.UTF-8"
            launch_env["LC_ALL"] = launch_env["LANG"]

    profile_root = Path(data_dir) / "browsers" / f"acc_{acc_id}"
    force_bounds = False
    launch_bounds: tuple[int, int, int, int] | None = None
    launch_target = target_url

    def open_profile(profile_dir: Path) -> subprocess.Popen:
        args = _call_supported(
            build_browser_launch_args,
            account,
            profile_dir,
            exe_path=exe,
            target_url=launch_target,
            pure_native=use_pure_native,
            enable_cdp=effective_cdp,
            settings=settings,
            launch_bounds=launch_bounds,
            force_bounds=force_bounds,
            debug_port=resolved_debug_port,
        )
        return subprocess.Popen(
            args,
            cwd=str(profile_dir),
            close_fds=True,
            env=launch_env,
        )

    try:
        if use_pure_native:
            with _profile_launch_lock:
                if not _remove_legacy_google_extension(profile_root):
                    return {
                        "ok": False,
                        "manual_login": True,
                        "profile_cleanup_pending": True,
                        "error": "旧登录助手待清理。请关闭该账号的浏览器窗口后重试。",
                    }
                already_running = _profile_has_active_lock(profile_root)
                if not already_running:
                    force_bounds, launch_bounds = restore_or_sanitize_window_placement(profile_root, acc_id)
                    profile_dir = _call_supported(
                        prepare_profile_dir,
                        data_dir,
                        account,
                        pure_native=True,
                        settings=settings,
                    )
                else:
                    # An already-open profile keeps its Preferences/Bookmarks/window
                    # placement untouched; Google also skips re-appending the start URL
                    # unless the caller explicitly requested one (BrowserService.cs:516).
                    profile_dir = profile_root
                    if is_google and not (target_url or "").strip():
                        launch_target = "about:blank"
                browser_process = open_profile(profile_dir)
        else:
            if not _profile_has_active_lock(profile_root):
                force_bounds, launch_bounds = restore_or_sanitize_window_placement(profile_root, acc_id)
            profile_dir = _call_supported(
                prepare_profile_dir,
                data_dir,
                account,
                pure_native=False,
                settings=settings,
            )
            browser_process = open_profile(profile_dir)

        try:
            exit_code = browser_process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            exit_code = None
        # Chromium can exit successfully after handing the URL to an existing window.
        if exit_code:
            return {"ok": False, "error": f"浏览器启动后立即退出（退出码 {exit_code}）。"}

        proc_pid = getattr(browser_process, "pid", None)
        if isinstance(proc_pid, int) and proc_pid > 0:
            _record_launched_pid(acc_id, proc_pid)
            if sys.platform == "win32":
                def _rescue_later():
                    for delay in (0.0, 0.45, 0.85):
                        if delay:
                            time.sleep(delay)
                        rescue_offscreen_browser_windows(acc_id, profile_dir=profile_dir)

                threading.Thread(target=_rescue_later, daemon=True).start()

        # Unified Zero-CDP mode: the silent CDP injector (never calls
        # ``Runtime.enable``) only starts when the resolved engine actually
        # needs it; ``域名邮箱`` always stays Zero-CDP.
        if effective_cdp and not is_domain_email and resolved_debug_port is not None:
            _start_cdp_injector(resolved_debug_port, profile_dir)

        engine_name = "Chrome" if "chrome" in exe_lower else ("Edge" if "msedge" in exe_lower else Path(exe).stem)
        return {
            "ok": True,
            "profile_dir": str(profile_dir),
            "fp_config": fp,
            "runtime_proxy": proxy_url,
            "env_cfg": env_cfg,
            "node_switched": node_switch_res.get("switched", False),
            "engine": engine_name,
            "pure_native": use_pure_native,
            "inpage_toolbar": not use_pure_native,
            "browser_engine_mode": engine_mode,
            "zero_cdp": bool(use_pure_native or not effective_cdp or is_domain_email),
            "manual_login": use_pure_native,
            "pid": proc_pid if isinstance(proc_pid, int) else 0,
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

    acc_id = int(account.get("id") or 1)
    result: dict = {
        "ok": True,
        "proxy_url": proxy_url,
        "proxy_node": proxy_node,
        "node_switch_res": {"ok": True, "switched": False},
    }
    sidecar_port = _proxy_port_of(proxy_url) if proxy_url else 0
    is_sidecar = bool(proxy_url) and (
        sidecar_port == DEFAULT_SIDECAR_PROXY_PORT
        or 17900 <= sidecar_port <= 17949
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
            switch = _call_supported(
                activate_proxy_node,
                "Claude",
                proxy_url or sidecar_url,
                target_node,
                isolate_slot=True,
                account_id=acc_id,
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
        switch = _call_supported(
            activate_proxy_node,
            "Claude",
            proxy_url,
            proxy_node,
            isolate_slot=True,
            account_id=acc_id,
        )
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
