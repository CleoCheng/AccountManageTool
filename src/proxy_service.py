import json
import os
import re
import socket
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote
import yaml


INVALID_NODE_PATTERNS = (
    "traffic:",
    "expire:",
    "剩余流量",
    "套餐到期",
    "距离下次",
    "官网",
    "重置",
    "direct",
    "reject",
    "compatible",
    "pass",
    "自动选择",
    "故障转移",
    "负载均衡",
    "建议",
    "更新订阅",
    "刷新",
    "电报",
    "防失联",
    "使用文档",
    "客户端",
    "http://",
    "https://",
)

COMMON_GROUP_NAMES = {
    "GLOBAL",
    "Proxies",
    "Proxy",
    "节点选择",
    "🔰 节点选择",
    "🚀 节点选择",
    "OpenAI",
    "ChatGPT",
    "AI",
    "Google",
    "YouTube",
    "Netflix",
    "Disney",
    "Bilibili",
    "Spotify",
    "Steam",
    "Telegram",
    "Microsoft",
    "PayPal",
    "Apple",
    "Final",
    "漏网之鱼",
    "HK",
    "JP",
    "SG",
    "TW",
    "US",
    "KR",
    "UK",
    "DE",
}


def is_valid_proxy_node_name(name: str, extra_group_names: set[str] | None = None) -> bool:
    if not name or not name.strip():
        return False
    s = name.strip()
    if s in COMMON_GROUP_NAMES:
        return False
    if extra_group_names and s in extra_group_names:
        return False
    low = s.lower()
    for pat in INVALID_NODE_PATTERNS:
        if pat in low:
            return False
    return True


def classify_node_region(name: str) -> str:
    low = name.lower()
    if any(k in low for k in ("singapore", "新加坡", "狮城")) or "🇸🇬" in name or re.search(r"\bSG\b", name):
        return "🇸🇬 新加坡 (SG)"
    if any(k in low for k in ("united states", "america", "美国", "洛杉矶", "圣何塞", "西雅图", "硅谷")) or "🇺🇸" in name or re.search(r"\bUS\b", name):
        return "🇺🇸 美国 (US)"
    if any(k in low for k in ("japan", "日本", "东京", "大阪")) or "🇯🇵" in name or re.search(r"\bJP\b", name):
        return "🇯🇵 日本 (JP)"
    if any(k in low for k in ("taiwan", "台湾", "台北", "新北")) or "🇹🇼" in name or "🇨🇳" in name or re.search(r"\bTW\b", name):
        return "🇨🇳 台湾 (TW)"
    if any(k in low for k in ("hong kong", "hongkong", "香港")) or "🇭🇰" in name or re.search(r"\bHK\b", name):
        return "🇭🇰 香港 (HK)"
    if any(k in low for k in ("korea", "韩国", "首尔")) or "🇰🇷" in name or re.search(r"\bKR\b", name):
        return "🇰🇷 韩国 (KR)"
    return "🌍 其他地区"


def get_system_proxy() -> dict:
    """Detect system proxy across Windows, macOS, and environment variables."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                srv = str(server or "").strip()
                # Handle "http=127.0.0.1:7890;https=127.0.0.1:7890" format
                if "=" in srv:
                    for part in srv.split(";"):
                        if part.startswith("http="):
                            srv = part.split("=", 1)[1]
                            break
                    else:
                        srv = srv.split("=", 1)[-1].split(";")[0]
                return {"enabled": bool(enabled), "server": srv}
        except Exception:
            pass

    for env_key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        val = (os.environ.get(env_key) or "").strip()
        if val:
            cleaned = re.sub(r"^[a-zA-Z0-9]+://", "", val).strip("/")
            return {"enabled": True, "server": cleaned}

    return {"enabled": False, "server": ""}


# Keep backward-compatible alias
get_windows_system_proxy = get_system_proxy


def _is_local_port_open(port: int, timeout: float = 0.08) -> bool:
    if not (1 <= int(port) <= 65535):
        return False
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def find_clash_config_files() -> list[tuple[str, Path]]:
    """Discover Clash / Mihomo config files across mainstream clients on Windows, macOS, and Linux."""
    appdata = Path(os.environ.get("APPDATA", "")) if os.environ.get("APPDATA") else Path.home()
    localappdata = Path(os.environ.get("LOCALAPPDATA", "")) if os.environ.get("LOCALAPPDATA") else Path.home()
    home = Path.home()

    candidates: list[tuple[str, Path]] = []
    # User override takes priority: KEYVAULT_CLASH_CONFIG=/path/to/config.yaml
    env_override = (os.environ.get("KEYVAULT_CLASH_CONFIG") or "").strip().strip('"')
    if env_override:
        candidates.append(("自定义配置 (KEYVAULT_CLASH_CONFIG)", Path(os.path.expanduser(env_override))))

    candidates += [
        # Clash Verge Rev / Clash Verge
        ("Clash Verge Rev", appdata / "io.github.clash-verge-rev.clash-verge-rev" / "clash-verge.yaml"),
        ("Clash Verge Rev", appdata / "io.github.clash-verge-rev.clash-verge-rev" / "config.yaml"),
        ("Clash Verge", appdata / "clash-verge" / "clash-verge.yaml"),
        ("Clash Verge", home / ".config" / "clash-verge" / "clash-verge.yaml"),
        ("Clash Verge Rev", home / "Library" / "Application Support" / "io.github.clash-verge-rev.clash-verge-rev" / "clash-verge.yaml"),
        # Mihomo Party / Clash Party
        ("Mihomo Party", appdata / "mihomo-party" / "work" / "config.yaml"),
        ("Mihomo Party", home / ".config" / "mihomo-party" / "work" / "config.yaml"),
        ("Mihomo Party", home / "Library" / "Application Support" / "mihomo-party" / "work" / "config.yaml"),
        # Clash Nyanpasu
        ("Clash Nyanpasu", appdata / "clash-nyanpasu" / "clash-config.yaml"),
        ("Clash Nyanpasu", home / ".config" / "clash-nyanpasu" / "clash-config.yaml"),
        # FlClash
        ("FlClash", appdata / "com.follow" / "FlClash" / "config.yaml"),
        ("FlClash", localappdata / "FlClash" / "config.yaml"),
        # Clash for Windows / Classic Clash / Mihomo
        ("Clash / Mihomo", home / ".config" / "clash" / "config.yaml"),
        ("Clash / Mihomo", home / ".config" / "mihomo" / "config.yaml"),
        ("Clash Meta", home / ".config" / "clash.meta" / "config.yaml"),
    ]
    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for client_name, path in candidates:
        try:
            if path.exists() and path.is_file():
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append((client_name, path))
        except Exception:
            continue
    return found


def find_clash_verge_yaml() -> Path | None:
    found = find_clash_config_files()
    return found[0][1] if found else None


def parse_clash_yaml_summary(yaml_path: str | Path) -> dict:
    p = Path(yaml_path)
    if not p.exists():
        return {"mixed_port": 0, "secret": "", "external_controller": "", "ports": [], "nodes": []}

    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8", errors="ignore")) or {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    mixed_port = int(data.get("mixed-port") or data.get("port") or 0)
    socks_port = int(data.get("socks-port") or 0)
    secret = str(data.get("secret") or "")
    ext_ctrl = str(data.get("external-controller") or "").strip()

    ports = []
    if mixed_port:
        ports.append(
            {
                "port": mixed_port,
                "url": f"http://127.0.0.1:{mixed_port}",
                "label": f"127.0.0.1:{mixed_port} (主混合端口 · 支持全节点切换)",
                "bound_group": "",
            }
        )
    elif socks_port:
        ports.append(
            {
                "port": socks_port,
                "url": f"socks5://127.0.0.1:{socks_port}",
                "label": f"socks5://127.0.0.1:{socks_port} (SOCKS5 代理端口)",
                "bound_group": "",
            }
        )

    for lst in data.get("listeners") or []:
        if not isinstance(lst, dict):
            continue
        lport = lst.get("port")
        lname = lst.get("name") or "listener"
        lproxy = lst.get("proxy") or ""
        ltype = str(lst.get("type") or "mixed").lower()
        if lport:
            scheme = "socks5" if "socks" in ltype else "http"
            ports.append(
                {
                    "port": int(lport),
                    "url": f"{scheme}://127.0.0.1:{int(lport)}",
                    "label": f"127.0.0.1:{int(lport)} ({lname} · 绑定 {lproxy or '默认'} 组)",
                    "bound_group": lproxy,
                }
            )

    group_names_in_yaml = {
        str(g.get("name") or "").strip()
        for g in (data.get("proxy-groups") or [])
        if isinstance(g, dict) and g.get("name")
    }

    nodes = []
    seen = set()
    for px in data.get("proxies") or []:
        if not isinstance(px, dict):
            continue
        nname = str(px.get("name") or "").strip()
        if not is_valid_proxy_node_name(nname, group_names_in_yaml) or nname in seen:
            continue
        seen.add(nname)
        nodes.append(
            {
                "name": nname,
                "type": str(px.get("type") or ""),
                "region": classify_node_region(nname),
                "delay": None,
            }
        )

    return {
        "mixed_port": mixed_port,
        "secret": secret,
        "external_controller": ext_ctrl,
        "ports": ports,
        "nodes": nodes,
    }


def find_active_mihomo_pipe() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        candidate_pipes = []
        for name in os.listdir(r"\\.\pipe"):
            low = name.lower()
            if "service" in low or "tunnel" in low or "helper" in low:
                continue
            if any(k in low for k in ("verge-mihomo", "mihomo", "clash")):
                candidate_pipes.append(f"\\\\.\\pipe\\{name}")

        candidate_pipes.sort(key=lambda p: (0 if "verge-mihomo" in p.lower() else 1, p))

        for pipe_path in candidate_pipes:
            try:
                code, data = _request_mihomo_pipe(pipe_path, "GET", "/version")
                if code == 200:
                    return pipe_path
            except Exception:
                continue
        return None
    except Exception:
        return None


def _request_mihomo_pipe(
    pipe_path: str,
    method: str,
    path: str,
    secret: str = "",
    body_dict: dict | None = None,
) -> tuple[int, dict | None]:
    body_bytes = json.dumps(body_dict, ensure_ascii=False).encode("utf-8") if body_dict is not None else b""
    headers = [
        f"{method} {path} HTTP/1.1",
        "Host: localhost",
        f"Authorization: Bearer {secret}",
        "Content-Type: application/json",
        f"Content-Length: {len(body_bytes)}",
        "Connection: close",
        "",
        "",
    ]
    req_bytes = "\r\n".join(headers).encode("utf-8") + body_bytes

    with open(pipe_path, "r+b", buffering=0) as f:
        f.write(req_bytes)
        raw_resp = f.read()

    header_b, _, resp_body_b = raw_resp.partition(b"\r\n\r\n")
    header_str = header_b.decode("utf-8", errors="ignore")
    first_line = header_str.splitlines()[0] if header_str else ""
    m_code = re.search(r"HTTP/\d\.\d\s+(\d+)", first_line)
    status_code = int(m_code.group(1)) if m_code else 0

    if not resp_body_b or status_code == 204:
        return status_code, {}

    if "transfer-encoding: chunked" in header_str.lower():
        pos = 0
        out = bytearray()
        while pos < len(resp_body_b):
            line_end = resp_body_b.find(b"\r\n", pos)
            if line_end == -1:
                break
            size_str = resp_body_b[pos:line_end].strip()
            if not size_str:
                pos = line_end + 2
                continue
            size = int(size_str, 16)
            if size == 0:
                break
            start = line_end + 2
            out.extend(resp_body_b[start : start + size])
            pos = start + size + 2
        json_str = out.decode("utf-8", errors="ignore")
    else:
        json_str = resp_body_b.decode("utf-8", errors="ignore")

    try:
        return status_code, json.loads(json_str)
    except Exception:
        return status_code, None


def _request_http_controller(
    base_url: str,
    method: str,
    path: str,
    secret: str = "",
    body_dict: dict | None = None,
) -> tuple[int, dict | None]:
    url = base_url.rstrip("/") + path
    body_bytes = json.dumps(body_dict, ensure_ascii=False).encode("utf-8") if body_dict is not None else None
    headers = {"Accept": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    if body_bytes is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="ignore")
            if not raw or status == 204:
                return status, {}
            return status, json.loads(raw)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def _discover_controller(summary: dict) -> tuple[str, str, str]:
    """Return (mode, endpoint, secret) where mode is 'pipe', 'http', or ''."""
    secret = summary.get("secret", "")
    pipe_path = find_active_mihomo_pipe()
    if pipe_path:
        return ("pipe", pipe_path, secret)

    ext_ctrl = summary.get("external_controller", "")
    http_candidates = []
    if ext_ctrl:
        host_port = ext_ctrl if ":" in ext_ctrl else f"127.0.0.1:{ext_ctrl}"
        if host_port.startswith(":"):
            host_port = "127.0.0.1" + host_port
        if host_port.startswith("0.0.0.0:"):
            host_port = "127.0.0.1:" + host_port.split(":", 1)[1]
        http_candidates.append(f"http://{host_port}")

    for default_port in (9090, 9097, 19090, 6170):
        u = f"http://127.0.0.1:{default_port}"
        if u not in http_candidates and _is_local_port_open(default_port):
            http_candidates.append(u)

    for base_url in http_candidates:
        code, data = _request_http_controller(base_url, "GET", "/proxies", secret=secret)
        if code == 200 and isinstance(data, dict) and "proxies" in data:
            return ("http", base_url, secret)

    return ("", "", secret)


def _call_controller(
    mode: str,
    endpoint: str,
    secret: str,
    method: str,
    path: str,
    body_dict: dict | None = None,
) -> tuple[int, dict | None]:
    if mode == "pipe":
        return _request_mihomo_pipe(endpoint, method, path, secret=secret, body_dict=body_dict)
    if mode == "http":
        return _request_http_controller(endpoint, method, path, secret=secret, body_dict=body_dict)
    return 0, None


def _resolve_leaf_node(px_map: dict, start_name: str, visited: set | None = None) -> str:
    if visited is None:
        visited = set()
    if not start_name or start_name in visited:
        return start_name or ""
    visited.add(start_name)
    node = px_map.get(start_name)
    if not isinstance(node, dict):
        return start_name
    ntype = str(node.get("type") or "")
    if ntype in ("Selector", "Fallback", "URLTest", "LoadBalance"):
        now = str(node.get("now") or "").strip()
        if now and now not in ("DIRECT", "REJECT", "GLOBAL"):
            return _resolve_leaf_node(px_map, now, visited)
    return start_name


def detect_proxy_environment(account_type: str = "") -> dict:
    """Describe KeyVault's isolated sidecar proxy environment (never touches Clash Verge)."""
    from src.sidecar_proxy import get_sidecar_proxy_manager

    return get_sidecar_proxy_manager().describe_environment(account_type=account_type)


def activate_proxy_node(
    account_type: str,
    proxy_url: str,
    node_name: str,
    *,
    preflight: bool = False,
) -> dict:
    """Switch KeyVault's dedicated sidecar selector group to ``node_name``."""
    if not node_name or not node_name.strip():
        return {"ok": True, "switched": False, "proxy_url": proxy_url}

    from src.sidecar_proxy import get_sidecar_proxy_manager

    return get_sidecar_proxy_manager().switch_node(node_name.strip(), preflight=preflight)
