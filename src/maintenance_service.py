import base64
from datetime import datetime, timezone
import hashlib
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


ANTIGRAVITY_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
ANTIGRAVITY_CLIENT_SECRET = "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"
ANTIGRAVITY_DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile "
    "https://www.googleapis.com/auth/cclog "
    "https://www.googleapis.com/auth/experimentsandconfigs"
)
ANTIGRAVITY_USER_AGENT = "Antigravity/4.1.29 Chrome/132.0.6834.160 Electron/39.2.3"
ANTIGRAVITY_DAILY_BASE = "https://daily-cloudcode-pa.googleapis.com/v1internal"
ANTIGRAVITY_FALLBACK_BASE = "https://cloudcode-pa.googleapis.com/v1internal"

UNSUPPORTED_ANTIGRAVITY_REGIONS = {"HK", "MO", "CN", "RU", "BY", "CU", "IR", "KP", "SY"}

COUNTRY_TIMEZONE_DEFAULTS = {
    "US": ("America/New_York", "en-US", "en-US,en;q=0.9"),
    "CA": ("America/Toronto", "en-CA", "en-CA,en-US;q=0.9,en;q=0.8"),
    "GB": ("Europe/London", "en-GB", "en-GB,en;q=0.9"),
    "JP": ("Asia/Tokyo", "ja-JP", "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7"),
    "SG": ("Asia/Singapore", "en-SG", "en-SG,en;q=0.9,zh-SG;q=0.8"),
    "TW": ("Asia/Taipei", "zh-TW", "zh-TW,zh;q=0.9,en;q=0.8"),
    "KR": ("Asia/Seoul", "ko-KR", "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7"),
    "DE": ("Europe/Berlin", "de-DE", "de-DE,de;q=0.9,en;q=0.8"),
    "FR": ("Europe/Paris", "fr-FR", "fr-FR,fr;q=0.9,en;q=0.8"),
    "NL": ("Europe/Amsterdam", "nl-NL", "nl-NL,nl;q=0.9,en;q=0.8"),
    "AU": ("Australia/Sydney", "en-AU", "en-AU,en;q=0.9"),
    "HK": ("Asia/Hong_Kong", "zh-HK", "zh-HK,zh;q=0.9,en;q=0.8"),
}

GCP_APPEAL_CHANNELS = [
    {
        "id": "antigravity_tos",
        "title": "Antigravity / Code Assist TOS 封禁申诉表",
        "category": "Antigravity / Gemini",
        "badge": "官方表单",
        "url": "https://forms.gle/hGzM9MEUv2azZsrb9",
        "summary": "当 RT 探测返回 TOS_VIOLATION 或账号被 Antigravity 封停时使用此官方申诉通道。",
        "steps": [
            "点击右侧『指纹环境打开』使用账号专属隔离浏览器并先确认登录该 Gmail。",
            "确认当前代理出口节点不在 HK/MO/CN/RU 等受限区域（推荐 US/JP/SG）。",
            "如实填写邮箱与正常 IDE 编码使用场景，提交后等待 1-5 个工作日邮件反馈。",
        ],
    },
    {
        "id": "google_restrictions",
        "title": "Google 服务限制状态检查",
        "category": "Google 账号安全",
        "badge": "状态诊断",
        "url": "https://myaccount.google.com/restrictions",
        "summary": "查看特定 Google 服务（Cloud / Gemini / YouTube 等）是否触发限制或人工复核。",
        "steps": [
            "使用账号绑定代理启动隔离浏览器打开该页面。",
            "检查页面列出的受限服务与可申诉按钮，按指引提交申诉说明。",
        ],
    },
    {
        "id": "gcp_eval_contact",
        "title": "GCP 账号评估与结算恢复工单",
        "category": "Google Cloud",
        "badge": "Cloud 工单",
        "url": "https://support.google.com/cloud/contact/cloud_platform_eval",
        "summary": "适用于 GCP 项目冻结、结算验证失败或需要人工恢复 Cloud 项目配额。",
        "steps": [
            "先在 Google Cloud Console 核对账号绑定的结算资料与姓名地址一致。",
            "填写项目 ID 与账号邮箱，说明为个人开发测试用途并提交。",
        ],
    },
    {
        "id": "google_suspended",
        "title": "Google 账号停用/封禁恢复表单",
        "category": "Google 账号安全",
        "badge": "停用恢复",
        "url": "https://support.google.com/accounts/contact/suspended?hl=zh-Hans",
        "summary": "用于整个 Google 账号被停用（Disabled / Suspended）时的官方复审申诉。",
        "steps": [
            "使用纯正注册地或长期稳定节点访问，避免频繁切换出口 IP。",
            "填写可联系的辅助恢复邮箱（建议从常用辅助邮箱快捷列表选择）。",
            "简要说明账号用于日常开发与学习，无自动化滥用行为。",
        ],
    },
    {
        "id": "country_association",
        "title": "Google 服务地区归属变更申请",
        "category": "地区合规",
        "badge": "IP/归属修正",
        "url": "https://policies.google.com/country-association-form",
        "summary": "当账号因早期使用受限 IP 导致地区归属被锁在 CN/HK 等无法使用 Antigravity 时提交修正。",
        "steps": [
            "确保账号已稳定使用目标国家（如 US / JP / SG）干净住宅代理至少 7 天。",
            "选择目标国家/地区并简要说明当前常驻工作环境在该地区。",
        ],
    },
    {
        "id": "age_verification",
        "title": "Google 年龄验证申诉页面",
        "category": "身份验证",
        "badge": "权限解锁",
        "url": "https://myaccount.google.com/age-verification",
        "summary": "解决因未确认成年人身份导致 AI / Gemini / Cloud 高级功能不可用的问题。",
        "steps": [
            "进入页面查看推荐验证方式（信用卡小额预授权 / 证件 / 自拍评估）。",
            "如无需强制人工核验，优先确认生日信息已完整且年满 18 周岁。",
        ],
    },
]


_RELAY_LOCK = threading.Lock()
_ACTIVE_RELAYS: dict[tuple[str, int, str, str], "LocalAuthHttpProxyRelay"] = {}


def parse_proxy_spec(raw_proxy: str) -> dict:
    """Parse flexible proxy strings (`http://...`, `socks5://...`, `host:port:user:pass`, `host:port`)."""
    raw = str(raw_proxy or "").strip()
    if not raw:
        return {"raw": "", "scheme": "", "host": "", "port": 0, "user": "", "password": ""}

    if "://" in raw:
        parsed = urllib.parse.urlparse(raw)
        try:
            parsed_port = parsed.port
        except ValueError:
            parsed_port = None
        return {
            "raw": raw,
            "scheme": (parsed.scheme or "http").lower(),
            "host": parsed.hostname or "",
            "port": int(parsed_port or (1080 if "socks" in (parsed.scheme or "").lower() else 8080)),
            "user": urllib.parse.unquote(parsed.username or ""),
            "password": urllib.parse.unquote(parsed.password or ""),
        }

    parts = raw.split(":")
    if len(parts) >= 4 and parts[1].isdigit():
        return {
            "raw": raw,
            "scheme": "http",
            "host": parts[0].strip(),
            "port": int(parts[1]),
            "user": parts[2].strip(),
            "password": ":".join(parts[3:]).strip(),
        }
    if len(parts) == 2 and parts[1].isdigit():
        return {
            "raw": raw,
            "scheme": "http",
            "host": parts[0].strip(),
            "port": int(parts[1]),
            "user": "",
            "password": "",
        }
    return {"raw": raw, "scheme": "http", "host": raw, "port": 8080, "user": "", "password": ""}


class LocalAuthHttpProxyRelay:
    """Loopback unauthenticated HTTP proxy that injects Proxy-Authorization Basic for upstream HTTP proxies."""

    def __init__(self, upstream_host: str, upstream_port: int, user: str, password: str):
        self.upstream_host = upstream_host
        self.upstream_port = int(upstream_port)
        self.user = user
        self.password = password
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        self.auth_header = f"Basic {token}"
        self._stop_event = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.local_port = int(self._sock.getsockname()[1])
        self._sock.listen(64)
        self._sock.settimeout(1.0)
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"

    @property
    def is_alive(self) -> bool:
        try:
            return (
                not self._stop_event.is_set()
                and self._thread.is_alive()
                and self._sock.fileno() != -1
            )
        except Exception:
            return False

    def _serve_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                client_sock, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True)
            t.start()

    def _handle_client(self, client_sock: socket.socket) -> None:
        upstream = None
        try:
            client_sock.settimeout(15.0)
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 65536:
                chunk = client_sock.recv(4096)
                if not chunk:
                    return
                buf += chunk

            hdr_end = buf.find(b"\r\n\r\n")
            if hdr_end < 0:
                return
            head_bytes = buf[:hdr_end]
            rest_bytes = buf[hdr_end + 4 :]
            lines = head_bytes.split(b"\r\n")
            if not lines:
                return

            req_line = lines[0].decode("latin-1", errors="replace")
            is_connect = req_line.upper().startswith("CONNECT ")
            has_proxy_auth = any(ln.lower().startswith(b"proxy-authorization:") for ln in lines[1:])
            filtered_lines = [lines[0]]
            for ln in lines[1:]:
                low_ln = ln.lower()
                if low_ln.startswith(b"proxy-connection:") or (not is_connect and low_ln.startswith(b"connection:")):
                    continue
                filtered_lines.append(ln)
            if not is_connect:
                filtered_lines.append(b"Connection: close")
            if not has_proxy_auth:
                filtered_lines.append(f"Proxy-Authorization: {self.auth_header}".encode("latin-1"))

            upstream = socket.create_connection((self.upstream_host, self.upstream_port), timeout=15.0)
            upstream.settimeout(30.0)
            out_head = b"\r\n".join(filtered_lines) + b"\r\n\r\n"
            upstream.sendall(out_head + rest_bytes)

            if req_line.upper().startswith("CONNECT "):
                # Forward upstream 200 Connection Established (or error) directly back to Chromium
                resp_buf = b""
                while b"\r\n\r\n" not in resp_buf and len(resp_buf) < 32768:
                    chunk = upstream.recv(4096)
                    if not chunk:
                        break
                    resp_buf += chunk
                if resp_buf:
                    client_sock.sendall(resp_buf)
                if not resp_buf.split(b"\r\n", 1)[0].startswith((b"HTTP/1.1 200", b"HTTP/1.0 200")):
                    return

            self._pump_bidirectional(client_sock, upstream)
        except Exception:
            pass
        finally:
            for s in (upstream, client_sock):
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass

    def _pump_bidirectional(self, a: socket.socket, b: socket.socket) -> None:
        a.settimeout(None)
        b.settimeout(None)
        sockets = [a, b]
        while not self._stop_event.is_set():
            try:
                readable, _, exceptional = select.select(sockets, [], sockets, 2.0)
            except Exception:
                break
            if exceptional:
                break
            if not readable:
                continue
            for sock in readable:
                other = b if sock is a else a
                try:
                    data = sock.recv(16384)
                except Exception:
                    return
                if not data:
                    return
                try:
                    other.sendall(data)
                except Exception:
                    return

    def close(self) -> None:
        self._stop_event.set()
        try:
            self._sock.close()
        except Exception:
            pass


def resolve_proxy_for_chromium_and_urllib(raw_proxy: str) -> dict:
    """Resolve raw proxy config into Chromium `--proxy-server` arg and urllib proxy URL."""
    spec = parse_proxy_spec(raw_proxy)
    if not spec["host"]:
        return {"chromium_proxy": "", "urllib_proxy": "", "uses_relay": False}

    scheme = spec["scheme"]
    host = spec["host"]
    port = spec["port"]
    user = spec["user"]
    password = spec["password"]

    if user or password:
        u_q = urllib.parse.quote(user, safe="")
        p_q = urllib.parse.quote(password, safe="")
        urllib_proxy = f"{scheme}://{u_q}:{p_q}@{host}:{port}"
        if scheme.startswith("http"):
            key = (host, port, user, password)
            with _RELAY_LOCK:
                relay = _ACTIVE_RELAYS.get(key)
                if relay is None or not relay.is_alive:
                    if relay is not None:
                        try:
                            relay.close()
                        except Exception:
                            pass
                    relay = LocalAuthHttpProxyRelay(host, port, user, password)
                    _ACTIVE_RELAYS[key] = relay
            return {
                "chromium_proxy": relay.local_url,
                "urllib_proxy": urllib_proxy,
                "uses_relay": True,
            }
        # Authenticated SOCKS cannot be rewritten cleanly as HTTP without upstream HTTP proxy; pass host:port fallback
        return {
            "chromium_proxy": f"{scheme}://{host}:{port}",
            "urllib_proxy": urllib_proxy,
            "uses_relay": False,
        }

    plain = f"{scheme}://{host}:{port}"
    return {"chromium_proxy": plain, "urllib_proxy": plain, "uses_relay": False}


def shutdown_auth_relays() -> None:
    with _RELAY_LOCK:
        relays = list(_ACTIVE_RELAYS.values())
        _ACTIVE_RELAYS.clear()
    for relay in relays:
        try:
            relay.close()
        except Exception:
            pass


def _build_urllib_opener(raw_proxy: str = "") -> urllib.request.OpenerDirector:
    resolved = resolve_proxy_for_chromium_and_urllib(raw_proxy)
    up = resolved["urllib_proxy"]
    if up:
        if up.lower().startswith("socks"):
            raise ValueError("urllib 暂不支持直连 SOCKS 代理，请配置 HTTP 代理端口或 Clash 混合端口。")
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": up, "https": up}))
    return urllib.request.build_opener()


def _http_json_request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    payload: dict | bytes | None = None,
    proxy_url: str = "",
    timeout: float = 15.0,
) -> tuple[int, dict | str]:
    try:
        opener = _build_urllib_opener(proxy_url)
    except ValueError as exc:
        return 0, {"error": "unsupported_proxy_scheme", "error_description": str(exc)}
    req_headers = dict(headers or {})
    data_bytes = None
    if isinstance(payload, dict):
        data_bytes = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    elif isinstance(payload, bytes):
        data_bytes = payload

    req = urllib.request.Request(url, data=data_bytes, method=method, headers=req_headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw_text = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
            try:
                return status, json.loads(raw_text)
            except Exception:
                return status, raw_text
    except urllib.error.HTTPError as exc:
        raw_err = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw_err)
        except Exception:
            return exc.code, raw_err
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None)
        msg = str(reason if reason is not None else exc)
        return 0, {"error": "network_error", "error_description": msg}


_IANA_STANDARD_OFFSET_HOURS: dict[str, float] = {
    "America/Los_Angeles": -8.0,
    "America/Vancouver": -8.0,
    "America/Denver": -7.0,
    "America/Chicago": -6.0,
    "America/New_York": -5.0,
    "America/Toronto": -5.0,
    "America/Sao_Paulo": -3.0,
    "Europe/London": 0.0,
    "Europe/Berlin": 1.0,
    "Europe/Paris": 1.0,
    "Europe/Amsterdam": 1.0,
    "Europe/Istanbul": 3.0,
    "Europe/Moscow": 3.0,
    "Asia/Kolkata": 5.5,
    "Asia/Bangkok": 7.0,
    "Asia/Ho_Chi_Minh": 7.0,
    "Asia/Jakarta": 7.0,
    "Asia/Singapore": 8.0,
    "Asia/Kuala_Lumpur": 8.0,
    "Asia/Manila": 8.0,
    "Asia/Hong_Kong": 8.0,
    "Asia/Taipei": 8.0,
    "Asia/Shanghai": 8.0,
    "Asia/Seoul": 9.0,
    "Asia/Tokyo": 9.0,
    "Australia/Sydney": 10.0,
}


def resolve_iana_utc_offset_hours(iana_tz: str, fallback_seconds: int | None = None) -> float | None:
    """Resolve current UTC offset in hours for an IANA timezone without requiring ``tzdata``."""
    if fallback_seconds is not None:
        try:
            return round(float(fallback_seconds) / 3600.0, 2)
        except (TypeError, ValueError):
            pass
    tz_name = (iana_tz or "").strip()
    if not tz_name:
        return None
    try:
        from zoneinfo import ZoneInfo

        offset = datetime.now(ZoneInfo(tz_name)).utcoffset()
        if offset is not None:
            return round(offset.total_seconds() / 3600.0, 2)
    except Exception:
        pass
    try:
        import clr  # type: ignore

        from System import DateTime, TimeZoneInfo  # type: ignore

        tzi = TimeZoneInfo.FindSystemTimeZoneById(tz_name)
        return round(float(tzi.GetUtcOffset(DateTime.UtcNow).TotalHours), 2)
    except Exception:
        pass
    if tz_name in _IANA_STANDARD_OFFSET_HOURS:
        return _IANA_STANDARD_OFFSET_HOURS[tz_name]
    return None


def compare_host_timezone(iana_tz: str, exit_offset_seconds: int | None = None) -> dict:
    """Compare proxy exit timezone against the local host Windows/OS timezone."""
    local_offset = datetime.now().astimezone().utcoffset()
    host_hours = round(local_offset.total_seconds() / 3600.0, 2) if local_offset else 0.0
    proxy_hours = resolve_iana_utc_offset_hours(iana_tz, exit_offset_seconds)
    if proxy_hours is None:
        return {
            "host_offset_hours": host_hours,
            "proxy_offset_hours": None,
            "diff_hours": 0.0,
            "mismatch": False,
        }
    diff = abs(proxy_hours - host_hours)
    return {
        "host_offset_hours": host_hours,
        "proxy_offset_hours": proxy_hours,
        "diff_hours": round(diff, 2),
        "mismatch": diff >= 1.0,
    }


def probe_proxy_geo(proxy_url: str = "", timeout: float = 8.0) -> dict:
    """Probe the exit IP country/timezone through the specified proxy (or direct connection)."""
    opener = _build_urllib_opener(proxy_url)
    endpoints = [
        ("https://ipwho.is/", "ipwhois"),
        ("https://ipinfo.io/json", "ipinfo"),
    ]
    last_err = ""
    for url, kind in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with opener.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if kind == "ipwhois" and data.get("success") is False:
                last_err = str(data.get("message") or "ipwho.is failed")
                continue
            cc = str(data.get("country_code") or data.get("country") or "").upper()
            tz = ""
            tz_offset_secs = None
            if isinstance(data.get("timezone"), dict):
                tz = str(data["timezone"].get("id") or "")
                raw_off = data["timezone"].get("offset")
                if isinstance(raw_off, (int, float)):
                    tz_offset_secs = int(raw_off)
            else:
                tz = str(data.get("timezone") or "")
            defaults = COUNTRY_TIMEZONE_DEFAULTS.get(cc, ("", "en-US", "en-US,en;q=0.9"))
            if not tz:
                tz = defaults[0]
            supported = cc not in UNSUPPORTED_ANTIGRAVITY_REGIONS if cc else True
            tz_cmp = compare_host_timezone(tz, tz_offset_secs)
            return {
                "ok": True,
                "ip": str(data.get("ip") or ""),
                "country_code": cc,
                "country": str(data.get("country") if kind == "ipwhois" else cc),
                "city": str(data.get("city") or ""),
                "timezone": tz,
                "locale": defaults[1],
                "accept_languages": defaults[2],
                "antigravity_supported": supported,
                "host_offset_hours": tz_cmp["host_offset_hours"],
                "proxy_offset_hours": tz_cmp["proxy_offset_hours"],
                "tz_mismatch": tz_cmp["mismatch"],
                "warning": (
                    f"出口区域 {cc} 不支持 Antigravity / 部分 Google AI 服务，请切换节点！"
                    if not supported
                    else ""
                ),
            }
        except Exception as exc:
            last_err = str(exc)
    return {"ok": False, "error": last_err or "出口 IP 探测失败"}


def inspect_rt_json(raw_text: str) -> dict:
    """Validate and inspect Google/Antigravity Refresh Token JSON or bare `1//...` token string."""
    raw = str(raw_text or "").strip()
    if not raw:
        return {"ok": False, "error": "内容为空"}

    if raw.startswith("1//") and "\n" not in raw and "{" not in raw:
        return {
            "ok": True,
            "format": "bare_refresh_token",
            "is_array": False,
            "refresh_token": raw,
            "has_access_token": False,
            "email": "",
            "project_id": "",
            "client_id": ANTIGRAVITY_CLIENT_ID,
            "client_secret": ANTIGRAVITY_CLIENT_SECRET,
            "normalized_json": json.dumps(
                {
                    "refresh_token": raw,
                    "client_id": ANTIGRAVITY_CLIENT_ID,
                    "client_secret": ANTIGRAVITY_CLIENT_SECRET,
                    "token_uri": "https://oauth2.googleapis.com/token",
                },
                ensure_ascii=False,
            ),
        }

    try:
        parsed = json.loads(raw)
    except Exception as exc:
        return {"ok": False, "error": f"JSON 解析失败: {exc}"}

    if isinstance(parsed, list):
        items = [x for x in parsed if isinstance(x, dict) and (x.get("refresh_token") or x.get("refreshToken"))]
        return {
            "ok": len(items) > 0,
            "format": "array",
            "is_array": True,
            "count": len(items),
            "items": items,
            "error": "" if items else "JSON 数组中未找到有效的 refresh_token 条目",
        }

    if not isinstance(parsed, dict):
        return {"ok": False, "error": "RT 凭据需为 JSON 对象、数组或 1// 开头的 Refresh Token"}

    rt = str(
        parsed.get("refresh_token")
        or parsed.get("refreshToken")
        or parsed.get("rt")
        or ""
    ).strip()
    at = str(parsed.get("access_token") or parsed.get("accessToken") or "").strip()
    if not rt and not at:
        return {"ok": False, "error": "JSON 中缺少 refresh_token"}

    raw_cid = str(parsed.get("client_id") or parsed.get("clientId") or "").strip()
    has_explicit_cid = bool(raw_cid)
    eff_cid = raw_cid if has_explicit_cid else ANTIGRAVITY_CLIENT_ID
    raw_csec = str(parsed.get("client_secret") or parsed.get("clientSecret") or "").strip()
    if raw_csec:
        eff_csec = raw_csec
    elif eff_cid.lower() == ANTIGRAVITY_CLIENT_ID.lower():
        eff_csec = ANTIGRAVITY_CLIENT_SECRET
    else:
        eff_csec = ""

    normalized = dict(parsed)
    normalized["client_id"] = eff_cid
    if eff_csec:
        normalized["client_secret"] = eff_csec
    elif "client_secret" not in normalized and "clientSecret" not in normalized and not has_explicit_cid:
        normalized["client_secret"] = ANTIGRAVITY_CLIENT_SECRET
    normalized["refresh_token"] = rt
    if at:
        normalized["access_token"] = at

    return {
        "ok": True,
        "format": "json_object",
        "is_array": False,
        "refresh_token": rt,
        "access_token": at,
        "has_access_token": bool(at),
        "email": str(parsed.get("email") or ""),
        "project_id": str(
            parsed.get("project_id")
            or parsed.get("projectId")
            or parsed.get("cloudaicompanionProject")
            or ""
        ),
        "client_id": eff_cid,
        "client_secret": eff_csec,
        "normalized_json": json.dumps(normalized, ensure_ascii=False),
    }


def _find_first_key_recursive(obj, keys: tuple[str, ...]) -> str:
    if isinstance(obj, dict):
        for k in keys:
            val = obj.get(k)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for v in obj.values():
            found = _find_first_key_recursive(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_first_key_recursive(item, keys)
            if found:
                return found
    return ""


def _map_tier_to_plan(tier_id: str) -> tuple[str, str]:
    tid = (tier_id or "").lower()
    if "ultra" in tid:
        return "Ultra", "金色Pro"
    if "pro" in tid or "g1-pro" in tid or "standard" in tid:
        return "Pro", "蓝色Pro"
    if "trial" in tid:
        return "Trial", "无"
    return "Free", "无"


def _extract_gcp_error_info(resp_obj) -> tuple[str, str, str]:
    raw_dump = json.dumps(resp_obj, ensure_ascii=False) if isinstance(resp_obj, (dict, list)) else str(resp_obj or "")
    reason_tag = ""
    err_msg = ""
    root = resp_obj[0] if isinstance(resp_obj, list) and resp_obj else resp_obj
    if isinstance(root, dict):
        err_obj = root.get("error") if isinstance(root.get("error"), dict) else root
        if isinstance(err_obj, dict):
            err_msg = str(err_obj.get("message") or err_obj.get("error_description") or "")
            reason_tag = str(err_obj.get("status") or err_obj.get("error") or "")
            details = err_obj.get("details")
            if isinstance(details, list):
                for d in details:
                    if isinstance(d, dict) and d.get("reason"):
                        reason_tag = str(d["reason"])
                        break
    return reason_tag, err_msg, raw_dump


def _is_unsupported_location_text(text: str) -> bool:
    low = (text or "").lower()
    return (
        "unsupported_location" in low
        or "not available in your country" in low
        or "restricted_region" in low
    )


def _classify_antigravity_response(
    status_code: int,
    resp_obj,
    *,
    checked_at: str,
    access_token: str,
    expiry_ts: int,
    prev_rt_status: str = "active",
    prev_gcp_status: str = "none",
) -> dict:
    reason_tag, err_msg, raw_dump = _extract_gcp_error_info(resp_obj)
    val_url = _find_first_key_recursive(resp_obj, ("validationUrl", "validation_url", "appealUrl"))
    combined = f"{reason_tag} {err_msg} {raw_dump}"

    if status_code == 0:
        return {
            "done": True,
            "ok": False,
            "transient": True,
            "rt_status": prev_rt_status if prev_rt_status not in ("", "none") else "active",
            "gcp_status": prev_gcp_status or "none",
            "error": f"网络或代理连接失败: {err_msg or raw_dump}".strip(),
            "checked_at": checked_at,
        }

    if status_code == 429:
        return {
            "done": True,
            "ok": True,
            "rt_status": "active",
            "gcp_status": "active",
            "subscription_plan": "Free",
            "antigravity_tier": "无",
            "project_id": "",
            "tier_id": "free-tier",
            "models_count": 0,
            "sample_models": [],
            "validation_url": "",
            "access_token": access_token,
            "expiry_timestamp": expiry_ts,
            "checked_at": checked_at,
            "error": "反重力 RT 有效 (当前请求 429 额度冷却中)",
        }

    if status_code == 401:
        return {
            "done": True,
            "ok": False,
            "rt_status": "invalid_grant",
            "gcp_status": "none",
            "error": "Access Token 未授权或已失效 (401)",
            "checked_at": checked_at,
        }

    if _is_unsupported_location_text(combined):
        return {
            "done": True,
            "ok": False,
            "rt_status": "unsupported",
            "gcp_status": "unsupported",
            "error": f"检测到 UNSUPPORTED_LOCATION（{reason_tag or '地区不受支持'}，请切换支持节点）",
            "checked_at": checked_at,
            "access_token": access_token,
            "expiry_timestamp": expiry_ts,
        }

    if "VALIDATION_REQUIRED" in raw_dump or "verify your account" in combined.lower() or val_url:
        return {
            "done": True,
            "ok": False,
            "rt_status": "active",
            "gcp_status": "validation",
            "validation_url": val_url,
            "error": "需要完成验证 (VALIDATION_REQUIRED)，请打开验证链接完成人机/账号验证",
            "checked_at": checked_at,
            "access_token": access_token,
            "expiry_timestamp": expiry_ts,
        }

    if re.search(r"invalid_grant|token_has_been_expired|token\s+expired|access_token_expired|unauthenticated|reauth", combined, re.I):
        return {
            "done": True,
            "ok": False,
            "rt_status": "invalid_grant",
            "gcp_status": "none",
            "error": f"RT/Token 已失效 ({status_code})",
            "checked_at": checked_at,
        }

    if "TOS_VIOLATION" in raw_dump or (status_code == 403 and re.search(r"violat|terms\s+of\s+service|tos", combined, re.I)):
        return {
            "done": True,
            "ok": False,
            "rt_status": "suspended",
            "gcp_status": "suspended",
            "validation_url": val_url,
            "error": f"检测到 TOS_VIOLATION（账号或 GCP 服务已被风控封禁 · {reason_tag or 403}）",
            "checked_at": checked_at,
            "access_token": access_token,
            "expiry_timestamp": expiry_ts,
        }

    if status_code == 403:
        detail = f" · {reason_tag}" if reason_tag else ""
        msg = f" ({err_msg})" if err_msg else ""
        return {
            "done": True,
            "needs_validation_probe": not bool(val_url),
            "ok": False,
            "rt_status": "suspended",
            "gcp_status": "suspended",
            "validation_url": "",
            "error": f"反重力返回 403 (GCP被封或服务受限{detail}){msg}",
            "checked_at": checked_at,
            "access_token": access_token,
            "expiry_timestamp": expiry_ts,
        }

    if status_code == 400:
        return {
            "done": True,
            "ok": False,
            "transient": True,
            "rt_status": prev_rt_status if prev_rt_status not in ("", "none") else "active",
            "gcp_status": prev_gcp_status or "none",
            "error": f"反重力接口返回 HTTP 400 ({err_msg or reason_tag or 'Bad Request'})",
            "checked_at": checked_at,
            "access_token": access_token,
            "expiry_timestamp": expiry_ts,
        }

    if status_code >= 500:
        return {
            "done": True,
            "ok": False,
            "transient": True,
            "rt_status": prev_rt_status if prev_rt_status not in ("", "none") else "active",
            "gcp_status": prev_gcp_status or "none",
            "error": f"Google Antigravity 服务端临时错误 ({status_code})",
            "checked_at": checked_at,
        }

    # HTTP 200 inspection: guard against ineligible/suspended accounts returning 200 with empty tiers
    project_id = ""
    tier_id = ""
    has_allowed_tiers = False
    inelig_reasons: list[str] = []
    if isinstance(resp_obj, dict):
        proj_val = resp_obj.get("cloudaicompanionProject")
        if isinstance(proj_val, dict):
            project_id = str(proj_val.get("id") or "")
        elif proj_val is not None:
            project_id = str(proj_val or "")
        paid = resp_obj.get("paidTier") or resp_obj.get("currentTier") or {}
        if isinstance(paid, dict):
            tier_id = str(paid.get("id") or paid.get("name") or "")
        allowed = resp_obj.get("allowedTiers")
        has_allowed_tiers = isinstance(allowed, list) and len(allowed) > 0
        for inelig in (resp_obj.get("ineligibleTiers") or []):
            if isinstance(inelig, dict):
                rc = str(inelig.get("reasonCode") or inelig.get("reasonMessage") or "").strip()
                if rc:
                    inelig_reasons.append(rc)
                    if _is_unsupported_location_text(rc):
                        return {
                            "done": True,
                            "ok": False,
                            "rt_status": "unsupported",
                            "gcp_status": "unsupported",
                            "error": f"反重力地区不支持 ({rc})",
                            "checked_at": checked_at,
                            "access_token": access_token,
                            "expiry_timestamp": expiry_ts,
                        }
        if not project_id and not tier_id and not has_allowed_tiers and (inelig_reasons or "allowedTiers" in resp_obj or "ineligibleTiers" in resp_obj):
            reason_note = inelig_reasons[0] if inelig_reasons else "无可用套餐/项目"
            return {
                "done": True,
                "ok": False,
                "rt_status": "suspended",
                "gcp_status": "suspended",
                "error": f"反重力/GCP 资格受限 ({reason_note})",
                "checked_at": checked_at,
                "access_token": access_token,
                "expiry_timestamp": expiry_ts,
            }

    return {
        "done": False,
        "project_id": project_id,
        "tier_id": tier_id,
        "validation_url": val_url,
    }


def _probe_secondary_validation_url(
    api_headers: dict,
    meta_payload: dict,
    project_id: str,
    proxy_url: str,
    timeout: float,
) -> str:
    stream_payload = {
        "project": project_id or "aicode-consumers",
        "model": "gemini-2.5-flash-lite",
        "request": {"contents": [{"role": "user", "parts": [{"text": "1"}]}]},
    }
    _, s_resp = _http_json_request(
        f"{ANTIGRAVITY_DAILY_BASE}:streamGenerateContent?alt=sse",
        method="POST",
        headers=api_headers,
        payload=stream_payload,
        proxy_url=proxy_url,
        timeout=min(timeout, 10.0),
    )
    url = _find_first_key_recursive(s_resp, ("validationUrl", "validation_url", "appealUrl"))
    if url:
        return url
    _, p_resp = _http_json_request(
        f"{ANTIGRAVITY_FALLBACK_BASE}:loadCodeAssist",
        method="POST",
        headers=api_headers,
        payload=meta_payload,
        proxy_url=proxy_url,
        timeout=min(timeout, 10.0),
    )
    return _find_first_key_recursive(p_resp, ("validationUrl", "validation_url", "appealUrl"))


def probe_antigravity_rt(account: dict, proxy_url: str = "", timeout: float = 15.0) -> dict:
    """Run Antigravity 4-step RT diagnosis (token refresh -> loadCodeAssist -> onboardUser -> fetchAvailableModels)."""
    cf = account.get("custom_fields") or {}
    prev_rt_status = str(cf.get("rt_status") or "none")
    prev_gcp_status = str(cf.get("gcp_status") or "none")
    raw_rt = str(cf.get("rt_json") or cf.get("rt_refresh_token") or "").strip()
    if not raw_rt:
        return {
            "ok": False,
            "rt_status": "none",
            "gcp_status": prev_gcp_status,
            "error": "账号尚未导入或授权 RT 凭据",
        }

    inspected = inspect_rt_json(raw_rt)
    if not inspected.get("ok") or inspected.get("is_array"):
        return {
            "ok": False,
            "rt_status": "invalid_grant",
            "gcp_status": prev_gcp_status,
            "error": inspected.get("error") or "RT 凭据格式无效",
        }

    eff_proxy = proxy_url or str((account.get("fp_config") or {}).get("proxy") or "").strip()
    rt = inspected["refresh_token"]
    client_id = inspected["client_id"] or ANTIGRAVITY_CLIENT_ID
    client_secret = inspected.get("client_secret") or ""
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Step 0: Refresh access_token from Google OAuth2 token endpoint
    form_fields = {
        "client_id": client_id,
        "refresh_token": rt,
        "grant_type": "refresh_token",
    }
    if client_secret:
        form_fields["client_secret"] = client_secret
    form_body = urllib.parse.urlencode(form_fields).encode("utf-8")
    tok_status, tok_resp = _http_json_request(
        "https://oauth2.googleapis.com/token",
        method="POST",
        headers={"User-Agent": ANTIGRAVITY_USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
        payload=form_body,
        proxy_url=eff_proxy,
        timeout=timeout,
    )
    if tok_status == 0 or tok_status == 429 or tok_status >= 500:
        err_desc = tok_resp.get("error_description") if isinstance(tok_resp, dict) else str(tok_resp)
        return {
            "ok": False,
            "transient": True,
            "rt_status": prev_rt_status if prev_rt_status not in ("", "none") else "active",
            "gcp_status": prev_gcp_status,
            "error": f"Token 刷新遇到临时网络/限流问题 ({tok_status}): {err_desc}".strip(),
            "checked_at": checked_at,
        }
    if tok_status >= 400 or not isinstance(tok_resp, dict) or not tok_resp.get("access_token"):
        err_code = tok_resp.get("error") if isinstance(tok_resp, dict) else ""
        err_desc = tok_resp.get("error_description") if isinstance(tok_resp, dict) else str(tok_resp)
        is_revoked = "invalid_grant" in f"{err_code} {err_desc}".lower() or tok_status in (400, 401)
        return {
            "ok": False,
            "rt_status": "invalid_grant" if is_revoked else prev_rt_status,
            "gcp_status": prev_gcp_status,
            "error": f"Token 刷新失败 ({tok_status}): {err_code} {err_desc}".strip(),
            "checked_at": checked_at,
        }

    access_token = str(tok_resp["access_token"])
    expires_in = int(tok_resp.get("expires_in") or 3600)
    expiry_ts = int(time.time()) + expires_in

    api_headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": ANTIGRAVITY_USER_AGENT,
        "Content-Type": "application/json",
    }
    meta_payload = {
        "metadata": {
            "ideType": "ANTIGRAVITY",
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI",
        }
    }

    # Step 1: loadCodeAssist
    load_status, load_resp = _http_json_request(
        f"{ANTIGRAVITY_DAILY_BASE}:loadCodeAssist",
        method="POST",
        headers=api_headers,
        payload=meta_payload,
        proxy_url=eff_proxy,
        timeout=timeout,
    )
    if load_status >= 500 or load_status == 404:
        load_status, load_resp = _http_json_request(
            f"{ANTIGRAVITY_FALLBACK_BASE}:loadCodeAssist",
            method="POST",
            headers=api_headers,
            payload=meta_payload,
            proxy_url=eff_proxy,
            timeout=timeout,
        )

    classified = _classify_antigravity_response(
        load_status,
        load_resp,
        checked_at=checked_at,
        access_token=access_token,
        expiry_ts=expiry_ts,
        prev_rt_status=prev_rt_status,
        prev_gcp_status=prev_gcp_status,
    )
    if classified.get("done"):
        if classified.pop("needs_validation_probe", False):
            sec_url = _probe_secondary_validation_url(api_headers, meta_payload, "", eff_proxy, timeout)
            if sec_url:
                classified["rt_status"] = "active"
                classified["gcp_status"] = "validation"
                classified["validation_url"] = sec_url
                classified["error"] = "需要完成验证 (VALIDATION_REQUIRED)，请打开验证链接完成人机/账号验证"
        return classified

    project_id = str(classified.get("project_id") or "")
    tier_id = str(classified.get("tier_id") or "")
    val_url = str(classified.get("validation_url") or "")

    # Step 2: onboardUser if needed
    if not project_id:
        ob_status, ob_resp = _http_json_request(
            f"{ANTIGRAVITY_DAILY_BASE}:onboardUser",
            method="POST",
            headers=api_headers,
            payload={
                "tierId": tier_id or "free-tier",
                **meta_payload,
            },
            proxy_url=eff_proxy,
            timeout=timeout,
        )
        if ob_status in (400, 401, 403):
            ob_cls = _classify_antigravity_response(
                ob_status,
                ob_resp,
                checked_at=checked_at,
                access_token=access_token,
                expiry_ts=expiry_ts,
                prev_rt_status=prev_rt_status,
                prev_gcp_status=prev_gcp_status,
            )
            if ob_cls.get("done"):
                ob_cls.pop("needs_validation_probe", None)
                return ob_cls
        if isinstance(ob_resp, dict):
            ob_dump = json.dumps(ob_resp, ensure_ascii=False)
            val_url = val_url or _find_first_key_recursive(ob_resp, ("validationUrl", "validation_url"))
            if "VALIDATION_REQUIRED" in ob_dump or val_url:
                return {
                    "ok": False,
                    "rt_status": "active",
                    "gcp_status": "validation",
                    "validation_url": val_url,
                    "error": "onboardUser 返回 VALIDATION_REQUIRED，请先完成身份验证",
                    "checked_at": checked_at,
                    "access_token": access_token,
                    "expiry_timestamp": expiry_ts,
                }
            if "TOS_VIOLATION" in ob_dump:
                return {
                    "ok": False,
                    "rt_status": "suspended",
                    "gcp_status": "suspended",
                    "error": "onboardUser 返回 TOS_VIOLATION",
                    "checked_at": checked_at,
                    "access_token": access_token,
                    "expiry_timestamp": expiry_ts,
                }
            if _is_unsupported_location_text(ob_dump):
                return {
                    "ok": False,
                    "rt_status": "unsupported",
                    "gcp_status": "unsupported",
                    "error": "onboardUser 返回 UNSUPPORTED_LOCATION",
                    "checked_at": checked_at,
                    "access_token": access_token,
                    "expiry_timestamp": expiry_ts,
                }
            resp_inner = ob_resp.get("response") if isinstance(ob_resp.get("response"), dict) else ob_resp
            proj_obj = resp_inner.get("cloudaicompanionProject")
            if isinstance(proj_obj, dict):
                project_id = str(proj_obj.get("id") or "")
            elif isinstance(proj_obj, str):
                project_id = proj_obj

    # Step 3: fetchAvailableModels
    models_count = 0
    model_names: list[str] = []
    m_status, m_resp = _http_json_request(
        f"{ANTIGRAVITY_DAILY_BASE}:fetchAvailableModels",
        method="POST",
        headers=api_headers,
        payload={"project": project_id} if project_id else {},
        proxy_url=eff_proxy,
        timeout=timeout,
    )
    if m_status in (400, 401, 403):
        m_cls = _classify_antigravity_response(
            m_status,
            m_resp,
            checked_at=checked_at,
            access_token=access_token,
            expiry_ts=expiry_ts,
            prev_rt_status=prev_rt_status,
            prev_gcp_status=prev_gcp_status,
        )
        if m_cls.get("done"):
            m_cls.pop("needs_validation_probe", None)
            return m_cls
    if isinstance(m_resp, dict) and isinstance(m_resp.get("models"), dict):
        model_names = list(m_resp["models"].keys())
        models_count = len(model_names)

    # Step 4 (best-effort): real inference check catches silent stream 403 TOS_VIOLATION / 400 region block
    s_status, s_resp = _http_json_request(
        f"{ANTIGRAVITY_DAILY_BASE}:streamGenerateContent?alt=sse",
        method="POST",
        headers=api_headers,
        payload={
            "project": project_id or "aicode-consumers",
            "model": "gemini-2.5-flash-lite",
            "request": {"contents": [{"role": "user", "parts": [{"text": "1"}]}]},
        },
        proxy_url=eff_proxy,
        timeout=min(timeout, 10.0),
    )
    if s_status in (401, 403) or (s_status == 400 and _is_unsupported_location_text(str(s_resp))):
        s_cls = _classify_antigravity_response(
            s_status,
            s_resp,
            checked_at=checked_at,
            access_token=access_token,
            expiry_ts=expiry_ts,
            prev_rt_status=prev_rt_status,
            prev_gcp_status=prev_gcp_status,
        )
        if s_cls.get("done") and not s_cls.get("transient"):
            s_cls.pop("needs_validation_probe", None)
            return s_cls

    sub_plan, ag_tier = _map_tier_to_plan(tier_id)
    gcp_stat = "active" if (project_id or models_count > 0) else ("no_billing" if load_status == 200 else "suspended")
    return {
        "ok": load_status < 400 and (bool(project_id) or models_count > 0),
        "rt_status": "active",
        "gcp_status": gcp_stat,
        "subscription_plan": sub_plan,
        "antigravity_tier": ag_tier,
        "project_id": project_id,
        "tier_id": tier_id or "free-tier",
        "models_count": models_count,
        "sample_models": model_names[:6],
        "validation_url": val_url,
        "access_token": access_token,
        "expiry_timestamp": expiry_ts,
        "checked_at": checked_at,
        "error": "" if (load_status < 400 and (project_id or models_count > 0)) else f"探测状态 {load_status}/{m_status}",
    }


def _get_antigravity_app_dir() -> Path:
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / "Antigravity"
    return Path.home() / ".config" / "Antigravity"


def inject_rt_to_antigravity_client(
    account: dict,
    clear_session_dirs: bool = True,
    proxy_url: str = "",
    timeout: float = 20.0,
) -> dict:
    """Exchange RT for a fresh access_token and inject into `%APPDATA%\\Antigravity\\oauth_credentials.json`."""
    cf = account.get("custom_fields") or {}
    raw_rt = str(cf.get("rt_json") or cf.get("rt_refresh_token") or "").strip()
    if not raw_rt:
        return {"ok": False, "error": "该账号未保存 RT 凭据"}

    inspected = inspect_rt_json(raw_rt)
    if not inspected.get("ok") or inspected.get("is_array"):
        return {"ok": False, "error": inspected.get("error") or "RT 格式无效"}

    ag_dir = _get_antigravity_app_dir()
    if not ag_dir.is_dir():
        return {
            "ok": False,
            "error": "未找到反重力数据目录 (%APPDATA%\\Antigravity)，请确认已安装并至少运行过一次。",
        }

    rt = inspected["refresh_token"]
    client_id = inspected["client_id"] or ANTIGRAVITY_CLIENT_ID
    client_secret = inspected.get("client_secret") or ""
    eff_proxy = proxy_url or str((account.get("fp_config") or {}).get("proxy") or "").strip()

    form_fields = {
        "client_id": client_id,
        "refresh_token": rt,
        "grant_type": "refresh_token",
    }
    if client_secret:
        form_fields["client_secret"] = client_secret
    form_body = urllib.parse.urlencode(form_fields).encode("utf-8")

    tok_status, tok_resp = _http_json_request(
        "https://oauth2.googleapis.com/token",
        method="POST",
        headers={"User-Agent": ANTIGRAVITY_USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
        payload=form_body,
        proxy_url=eff_proxy,
        timeout=timeout,
    )
    if tok_status == 0:
        err_desc = tok_resp.get("error_description") if isinstance(tok_resp, dict) else str(tok_resp)
        return {"ok": False, "error": f"网络请求失败 (代理可能不通): {err_desc}".strip()}
    if tok_status >= 400 or not isinstance(tok_resp, dict) or not tok_resp.get("access_token"):
        err_code = tok_resp.get("error") if isinstance(tok_resp, dict) else ""
        err_desc = tok_resp.get("error_description") if isinstance(tok_resp, dict) else str(tok_resp)
        if "invalid_grant" in f"{err_code} {err_desc}".lower():
            return {"ok": False, "error": "RT 已失效 (invalid_grant)，需要重新通过 📱OAuth扫码授权 获取新 RT。"}
        return {"ok": False, "error": f"换取 access_token 失败 ({tok_status}): {err_code} {err_desc}".strip()}

    access_token = str(tok_resp["access_token"]).strip()
    id_token = str(tok_resp.get("id_token") or "").strip()
    expires_in = int(tok_resp.get("expires_in") or 3600)
    expiry_ts = int(time.time()) + expires_in
    email = str(account.get("account") or cf.get("rt_email") or inspected.get("email") or "").strip()
    claims = inspect_jwt_claims(id_token) if id_token else None
    display_name = str((claims or {}).get("name") or (email.split("@")[0] if "@" in email else email))

    cred_file = ag_dir / "oauth_credentials.json"
    if cred_file.exists():
        try:
            shutil.copy2(cred_file, ag_dir / "oauth_credentials.json.bak")
        except Exception:
            pass

    if clear_session_dirs:
        for sub in ("Session Storage", "Partitions"):
            sub_dir = ag_dir / sub
            if sub_dir.exists() and sub_dir.is_dir():
                try:
                    shutil.rmtree(sub_dir, ignore_errors=True)
                except Exception:
                    pass

    payload = {
        "access_token": access_token,
        "refresh_token": rt,
        "email": email,
        "name": display_name,
        "expiry_timestamp": expiry_ts,
        "expiry_date": expiry_ts * 1000,
        "token_type": "Bearer",
        "antigravity_cockpit_active_email": email,
    }
    if id_token:
        payload["id_token"] = id_token
    cred_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "path": str(cred_file),
        "email": email,
        "expiry_timestamp": expiry_ts,
    }


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class OAuthLoopbackManager:
    """Thread-safe local Loopback OAuth 2.0 + PKCE manager for Google / Antigravity RT capture."""

    _SESSION_RETENTION_SECONDS = 900.0

    def __init__(self, on_success_callback=None):
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self._on_success_callback = on_success_callback

    def _prune_stale_sessions_locked(self, now: float | None = None) -> None:
        cutoff = (now or time.time()) - self._SESSION_RETENTION_SECONDS
        for sid, sess in list(self._sessions.items()):
            if sess.get("status") != "pending" and float(sess.get("created_at") or 0.0) < cutoff:
                self._sessions.pop(sid, None)

    def start_session(
        self,
        account_id: int,
        login_hint: str = "",
        proxy_url: str = "",
        scopes: str = "",
        hl: str = "zh-CN",
        preempt: bool = True,
        is_profile_alive=None,
    ) -> dict:
        if preempt:
            with self._lock:
                pending_sids = [
                    sid for sid, s in self._sessions.items() if s.get("status") == "pending"
                ]
            for old_sid in pending_sids:
                self.cancel_session(old_sid, reason="已切换新的 OAuth 授权请求，上一等待已自动中止。")
        else:
            with self._lock:
                same_sids = [
                    sid
                    for sid, s in self._sessions.items()
                    if s.get("status") == "pending" and int(s.get("account_id") or 0) == int(account_id)
                ]
            for old_sid in same_sids:
                self.cancel_session(old_sid, reason="已重新发起当前账号的 OAuth 授权请求。")

        session_id = secrets.token_hex(8)
        state = secrets.token_urlsafe(24)
        verifier, challenge = _generate_pkce_pair()
        eff_scopes = (scopes or ANTIGRAVITY_DEFAULT_SCOPES).strip()

        manager = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/oauth-callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                code = (qs.get("code") or [""])[0]
                got_state = (qs.get("state") or [""])[0]
                err = (qs.get("error") or [""])[0]

                if err:
                    manager._finish_session(session_id, ok=False, error=f"Google OAuth 拒绝或取消: {err}")
                    self._send_html(400, "授权未完成", f"Google 返回错误：{err}，请关闭此标签页返回客户端重试。")
                    return
                if not code or got_state != state:
                    manager._finish_session(session_id, ok=False, error="回调 state 校验失败或缺少 authorization code")
                    self._send_html(400, "校验失败", "State 校验未通过或缺少授权码，请重新发起授权。")
                    return

                self._send_html(
                    200,
                    "授权已接收",
                    "已捕获 OAuth 授权码，正在后台换取 Refresh Token，您可以安全关闭此浏览器窗口。",
                )
                threading.Thread(
                    target=manager._exchange_code,
                    args=(session_id, code),
                    daemon=True,
                ).start()

            def _send_html(self, status_code: int, title: str, message: str):
                safe_title = html.escape(str(title or ""), quote=True)
                safe_message = html.escape(str(message or ""), quote=True)
                html_doc = (
                    "<!doctype html><html><head><meta charset='utf-8'>"
                    f"<title>{safe_title}</title>"
                    "<style>body{font-family:system-ui,sans-serif;background:#0b1120;color:#e2e8f0;"
                    "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
                    ".box{background:#111827;border:1px solid #334155;border-radius:14px;padding:28px 34px;"
                    "max-width:480px;text-align:center;box-shadow:0 16px 40px rgba(0,0,0,.45)}"
                    "h2{margin:0 0 10px;color:#38bdf8}p{margin:0;line-height:1.6;color:#cbd5e1}</style></head>"
                    f"<body><div class='box'><h2>{safe_title}</h2><p>{safe_message}</p></div></body></html>"
                )
                body = html_doc.encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
        server.timeout = 1.0
        port = int(server.server_address[1])
        redirect_uri = f"http://127.0.0.1:{port}/oauth-callback"

        params = {
            "client_id": ANTIGRAVITY_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": eff_scopes,
            "access_type": "offline",
            "prompt": "consent",
            "hl": (hl or "zh-CN").strip() or "zh-CN",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if login_hint:
            params["login_hint"] = login_hint
        auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

        stop_event = threading.Event()
        sess = {
            "session_id": session_id,
            "account_id": int(account_id),
            "status": "pending",
            "exchanging": False,
            "auth_url": auth_url,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "proxy_url": proxy_url,
            "login_hint": login_hint,
            "email": "",
            "error": "",
            "rt_json": "",
            "server": server,
            "stop_event": stop_event,
            "created_at": time.time(),
        }
        with self._lock:
            self._prune_stale_sessions_locked()
            self._sessions[session_id] = sess

        def serve_until_done():
            deadline = time.time() + 300.0
            while not stop_event.is_set() and time.time() < deadline:
                try:
                    server.handle_request()
                except Exception:
                    break
            try:
                server.server_close()
            except Exception:
                pass
            with self._lock:
                cur = self._sessions.get(session_id)
                if cur and cur["status"] == "pending" and not cur.get("exchanging"):
                    cur["status"] = "expired"
                    cur["error"] = "OAuth 授权等待超时（5分钟）"
                    cur["stop_event"].set()

        def watch_browser_closed():
            if not callable(is_profile_alive):
                return
            if stop_event.wait(3.5):
                return
            seen_alive = False
            # Confirm lockfile was observed before assuming browser close means exit
            for _ in range(10):
                if stop_event.is_set():
                    return
                try:
                    if is_profile_alive():
                        seen_alive = True
                        break
                except Exception:
                    return
                if stop_event.wait(0.5):
                    return
            if not seen_alive:
                return
            while not stop_event.is_set():
                try:
                    alive = bool(is_profile_alive())
                except Exception:
                    return
                if not alive:
                    if stop_event.wait(1.5):
                        return
                    try:
                        still_dead = not bool(is_profile_alive())
                    except Exception:
                        return
                    if still_dead:
                        with self._lock:
                            cur = self._sessions.get(session_id)
                            if not cur or cur.get("status") != "pending" or cur.get("exchanging"):
                                return
                        self.cancel_session(session_id, reason="检测到独立浏览器窗口已关闭，已自动结束 OAuth 授权等待。")
                        return
                if stop_event.wait(1.0):
                    return

        threading.Thread(target=serve_until_done, name=f"kv-oauth-srv-{session_id[:6]}", daemon=True).start()
        if callable(is_profile_alive):
            threading.Thread(target=watch_browser_closed, name=f"kv-oauth-watch-{session_id[:6]}", daemon=True).start()
        return {
            "ok": True,
            "session_id": session_id,
            "account_id": int(account_id),
            "status": "pending",
            "auth_url": auth_url,
            "redirect_uri": redirect_uri,
        }

    def _exchange_code(self, session_id: str, code: str) -> None:
        with self._lock:
            sess = self._sessions.get(session_id)
            if not sess or sess.get("status") != "pending" or sess.get("exchanging"):
                return
            sess["exchanging"] = True
            redirect_uri = sess["redirect_uri"]
            verifier = sess["code_verifier"]
            proxy_url = sess["proxy_url"]

        body = urllib.parse.urlencode({
            "client_id": ANTIGRAVITY_CLIENT_ID,
            "client_secret": ANTIGRAVITY_CLIENT_SECRET,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }).encode("utf-8")

        status, resp = _http_json_request(
            "https://oauth2.googleapis.com/token",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": ANTIGRAVITY_USER_AGENT},
            payload=body,
            proxy_url=proxy_url,
            timeout=15.0,
        )
        if status >= 400 or not isinstance(resp, dict) or not resp.get("refresh_token"):
            err_msg = (
                f"{resp.get('error', '')} {resp.get('error_description', '')}".strip()
                if isinstance(resp, dict)
                else str(resp)
            )
            self._finish_session(session_id, ok=False, error=f"换取 RT 失败 ({status}): {err_msg}")
            return

        access_token = str(resp.get("access_token") or "")
        refresh_token = str(resp.get("refresh_token") or "")
        expires_in = int(resp.get("expires_in") or 3600)
        email = ""
        if access_token:
            u_stat, u_resp = _http_json_request(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                method="GET",
                headers={"Authorization": f"Bearer {access_token}"},
                proxy_url=proxy_url,
                timeout=10.0,
            )
            if u_stat == 200 and isinstance(u_resp, dict):
                email = str(u_resp.get("email") or "")

        rt_payload = {
            "refresh_token": refresh_token,
            "access_token": access_token,
            "client_id": ANTIGRAVITY_CLIENT_ID,
            "client_secret": ANTIGRAVITY_CLIENT_SECRET,
            "token_uri": "https://oauth2.googleapis.com/token",
            "email": email,
            "expiry_timestamp": int(time.time()) + expires_in,
        }
        rt_json_str = json.dumps(rt_payload, ensure_ascii=False)
        self._finish_session(session_id, ok=True, email=email, rt_json=rt_json_str, rt_payload=rt_payload)

    def _finish_session(
        self,
        session_id: str,
        ok: bool,
        email: str = "",
        rt_json: str = "",
        error: str = "",
        rt_payload: dict | None = None,
    ) -> None:
        with self._lock:
            sess = self._sessions.get(session_id)
            if not sess or sess.get("status") != "pending":
                return
            sess["status"] = "completing"
            account_id = sess["account_id"]
            callback = self._on_success_callback if (ok and rt_payload is not None and callable(self._on_success_callback)) else None

        if callback is not None:
            try:
                callback(account_id, rt_payload, rt_json)
            except ValueError as exc:
                ok, error = False, str(exc)
            except Exception:
                ok, error = False, "授权结果无法保存到所选 Google 账号。"

        with self._lock:
            sess = self._sessions.get(session_id)
            if not sess or sess.get("status") != "completing":
                return
            sess["status"] = "success" if ok else "error"
            sess["email"] = email or sess.get("email", "")
            sess["rt_json"] = rt_json if ok else ""
            sess["error"] = error
            sess["stop_event"].set()
            srv = sess.get("server")
            if srv is not None:
                try:
                    srv.server_close()
                except Exception:
                    pass

    def get_status(self, session_id: str) -> dict:
        with self._lock:
            sess = self._sessions.get(session_id)
            if not sess:
                return {"ok": False, "status": "not_found", "error": "OAuth 会话不存在"}
            eff_status = "pending" if sess.get("status") == "completing" else sess.get("status")
            return {
                "ok": True,
                "session_id": sess["session_id"],
                "account_id": sess["account_id"],
                "status": eff_status,
                "auth_url": sess["auth_url"],
                "email": sess.get("email", ""),
                "error": sess.get("error", ""),
                "has_rt": bool(sess.get("rt_json")),
            }

    def cancel_session(self, session_id: str, reason: str = "已手动取消") -> dict:
        with self._lock:
            sess = self._sessions.get(session_id)
            if not sess:
                return {"ok": True, "cancelled": False}
            if sess.get("status") in ("pending", "completing"):
                sess["status"] = "cancelled"
                sess["error"] = reason
            sess["stop_event"].set()
            srv = sess.get("server")
            if srv is not None:
                try:
                    srv.server_close()
                except Exception:
                    pass
        return {"ok": True, "cancelled": True}

    def shutdown(self) -> None:
        with self._lock:
            sids = list(self._sessions.keys())
        for sid in sids:
            self.cancel_session(sid)


def inspect_jwt_claims(token: str) -> dict | None:
    parts = str(token or "").strip().split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + ("=" * ((4 - (len(parts[1]) % 4)) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json.loads(raw.decode("utf-8", errors="replace"))
        return claims if isinstance(claims, dict) else None
    except Exception:
        return None


def probe_ai_credential(account: dict, proxy_url: str = "", timeout: float = 12.0) -> dict:
    """Validate ChatGPT / Claude API Key or JWT Session token."""
    cf = account.get("custom_fields") or {}
    atype = str(account.get("account_type") or "").strip().lower()
    api_key = str(cf.get("api_key") or "").strip()
    session_token = str(cf.get("session_token") or "").strip()
    eff_proxy = proxy_url or str((account.get("fp_config") or {}).get("proxy") or "").strip()
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if api_key:
        if atype == "claude" or api_key.startswith("sk-ant-"):
            status, resp = _http_json_request(
                "https://api.anthropic.com/v1/models",
                method="GET",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "User-Agent": "AccountManager/1.0",
                },
                proxy_url=eff_proxy,
                timeout=timeout,
            )
            models = resp.get("data", []) if isinstance(resp, dict) else []
            ok = status == 200
            err = "" if ok else (
                (resp.get("error") or {}).get("message", str(resp)) if isinstance(resp, dict) else str(resp)
            )
            return {
                "ok": ok,
                "provider": "Claude API",
                "models_count": len(models) if isinstance(models, list) else 0,
                "checked_at": checked_at,
                "error": err,
            }

        status, resp = _http_json_request(
            "https://api.openai.com/v1/models",
            method="GET",
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "AccountManager/1.0",
            },
            proxy_url=eff_proxy,
            timeout=timeout,
        )
        models = resp.get("data", []) if isinstance(resp, dict) else []
        ok = status == 200
        err = "" if ok else (
            (resp.get("error") or {}).get("message", str(resp)) if isinstance(resp, dict) else str(resp)
        )
        return {
            "ok": ok,
            "provider": "OpenAI API",
            "models_count": len(models) if isinstance(models, list) else 0,
            "checked_at": checked_at,
            "error": err,
        }

    if session_token:
        claims = inspect_jwt_claims(session_token)
        if claims:
            exp = claims.get("exp")
            now = int(time.time())
            expired = isinstance(exp, (int, float)) and exp < now
            exp_str = (
                datetime.fromtimestamp(exp, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
                if isinstance(exp, (int, float))
                else ""
            )
            return {
                "ok": not expired,
                "provider": "JWT Session",
                "models_count": 0,
                "expires_at": exp_str,
                "checked_at": checked_at,
                "error": f"Session JWT 已于 {exp_str} 过期" if expired else "",
            }
        return {
            "ok": len(session_token) >= 20,
            "provider": "Cookie/Session",
            "models_count": 0,
            "checked_at": checked_at,
            "error": "" if len(session_token) >= 20 else "Session Token 长度过短或格式异常",
        }

    return {
        "ok": False,
        "provider": atype or "ai",
        "models_count": 0,
        "checked_at": checked_at,
        "error": "未配置 API Key 或 Session Token",
    }


def probe_live_validation_challenge(
    account: dict,
    proxy_url: str = "",
    timeout: float = 15.0,
) -> dict:
    """Fetch a live Google/Antigravity ``validationUrl`` via RT when available, or return safe fallback."""
    cf = account.get("custom_fields") or {}
    has_rt = bool(str(cf.get("rt_json") or cf.get("rt_refresh_token") or "").strip())
    if not has_rt:
        return {
            "ok": True,
            "status": "security_checkup",
            "target_url": "https://myaccount.google.com/security-checkup",
            "message": "该账号尚未录入 RT，已为您指向 Google 安全检查页（也可直接打开 g.co/verifyaccount）。",
        }
    res = probe_antigravity_rt(account, proxy_url=proxy_url, timeout=timeout)
    val_url = str(res.get("validation_url") or "").strip()
    if val_url:
        return {
            "ok": True,
            "status": "live_validation_url",
            "target_url": val_url,
            "message": "已实时提取 Google Verify 验证直链！请在隔离浏览器内完成扫码或手机验证。",
        }
    if res.get("gcp_status") == "suspended":
        return {
            "ok": True,
            "status": "tos_violation",
            "target_url": "https://forms.gle/hGzM9MEUv2azZsrb9",
            "message": res.get("error") or "检测到 TOS_VIOLATION，已指向官方申诉表单。",
        }
    if res.get("gcp_status") == "unsupported":
        return {
            "ok": True,
            "status": "region_unsupported",
            "target_url": "https://policies.google.com/country-association-form",
            "message": res.get("error") or "当前地区不支持，可提交地区归属变更表单。",
        }
    if res.get("ok"):
        return {
            "ok": True,
            "status": "healthy",
            "target_url": "",
            "message": "✓ 该账号 RT 与 Antigravity 探测正常（200 正常，无需验证）",
        }
    return {
        "ok": True,
        "status": "security_checkup",
        "target_url": "https://myaccount.google.com/security-checkup",
        "message": res.get("error") or "未提取到实时验证直链，已指向 Google 安全检查页。",
    }


def clear_antigravity_session_cache() -> dict:
    """Clear local Antigravity ``Session Storage`` & ``Partitions`` dirs to break Verify loops."""
    ag_dir = _get_antigravity_app_dir()
    removed: list[str] = []
    errors: list[str] = []
    for sub in ("Session Storage", "Partitions"):
        target = ag_dir / sub
        if target.exists():
            try:
                shutil.rmtree(target)
                removed.append(sub)
            except Exception as exc:
                errors.append(f"{sub}: {exc}")
    if errors:
        return {"ok": False, "removed": removed, "error": "; ".join(errors)}
    return {
        "ok": True,
        "removed": removed,
        "path": str(ag_dir),
        "message": "已清理反重力本地 Session 缓存，请在反重力内重新登录或执行 Reset Onboarding。",
    }
