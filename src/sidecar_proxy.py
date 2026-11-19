"""KeyVault's own isolated Mihomo sidecar proxy.

This module NEVER touches the user's running Clash Verge instance. It reads the
subscription YAML strictly read-only, extracts the leaf ``proxies`` and launches
KeyVault's own ``mihomo`` sidecar child process on a dedicated local port with
a flat ``KEYVAULT_PROXY`` selector group.
"""

import atexit
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
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from src.proxy_service import (
    _is_local_port_open,
    classify_node_region,
    find_clash_config_files,
    get_system_proxy,
    is_valid_proxy_node_name,
    parse_clash_yaml_summary,
)


DEFAULT_SIDECAR_PROXY_PORT = 17899
DEFAULT_SIDECAR_CTRL_PORT = 29091
SIDECAR_GROUP_NAME = "KEYVAULT_PROXY"
SIDECAR_SLOT_COUNT = 16
# Bumped whenever the generated sidecar config layout changes so a live kernel
# hot-reloads instead of silently keeping a stale config.
SIDECAR_CONFIG_VERSION = "v4-per-slot-listeners"

_GITHUB_PREFLIGHT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
_RESTRICTED_PATTERN = re.compile(
    r"访问暂时受限|为什么需要经过「我不是机器人」的验证|Access temporarily restricted|"
    r"_Incapsula_Resource|You have triggered an abuse detection mechanism"
)


def find_mihomo_binary() -> Path | None:
    """Return the first existing Mihomo kernel binary, or ``None``."""
    candidates: list[Path] = []
    env_override = (os.environ.get("KEYVAULT_MIHOMO_PATH") or "").strip().strip('"').strip("'")
    if env_override:
        candidates.append(Path(os.path.expanduser(env_override)))

    candidates.extend(
        [
            Path(r"C:\Program Files\Clash Verge\verge-mihomo.exe"),
            Path(r"C:\Program Files\Clash Verge\verge-mihomo-alpha.exe"),
        ]
    )
    localappdata = (os.environ.get("LOCALAPPDATA") or "").strip()
    if localappdata:
        candidates.append(Path(localappdata) / "Clash Verge" / "verge-mihomo.exe")
        candidates.append(Path(localappdata) / "Programs" / "Clash Verge" / "verge-mihomo.exe")
    candidates.append(Path(r"D:\Documents\Servers\vps-protocol-lab-20261105\client-runtime\mihomo.exe"))

    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue

    for bin_name in ("mihomo", "verge-mihomo", "clash-meta"):
        found = shutil.which(bin_name)
        if found:
            return Path(found)
    return None


def _region_priority(region: str) -> int:
    r = str(region or "")
    if "🇸🇬" in r or "SG" in r:
        return 0
    if "🇯🇵" in r or "JP" in r:
        return 1
    if "🇹🇼" in r or "TW" in r or "台湾" in r:
        return 2
    if "🇺🇸" in r or "US" in r:
        return 3
    if "🇰🇷" in r or "KR" in r:
        return 4
    if "🇭🇰" in r or "HK" in r or "香港" in r:
        return 6
    return 5


_RESIDENTIAL_RE = re.compile(
    r"(?<![a-z0-9])(?:miyaip|residential|isp|native|att|verizon|comcast)(?![a-z0-9])",
    re.IGNORECASE,
)


def _is_residential_node(name: str) -> bool:
    """Detect clean residential/ISP lines without matching words like "whisper"."""
    if any(k in name for k in ("住宅", "家宽", "原生")):
        return True
    return bool(_RESIDENTIAL_RE.search(name))


def _residential_tier(name: str) -> int:
    """Tier a node for residential preference; lower is better.

    ``0``: direct MiyaIP-2 with HY2 (``US | MiyaIP-2 HY2``).
    ``1``: direct MiyaIP-2 without HY2 (``US | MiyaIP-2``) or relayed
    MiyaIP-2 with HY2 (``USbyJPTT | MiyaIP-2 HY2``).
    ``2``: other direct residential/ISP lines (``US | MiyaIP``,
    ``US | MiyaIP HY2``).
    ``3``: relayed residential/ISP lines (``USbyJPTT | MiyaIP``).
    ``4``: generic datacenter/airport nodes.

    Callers must gate this with their own alive/blocked-region checks.
    """
    if not _is_residential_node(name):
        return 4
    low = name.lower()
    has_usby = "usby" in low
    has_m2 = "miyaip-2" in low or "miyaip2" in low
    has_hy2 = "hy2" in low
    if not has_usby and has_m2 and has_hy2:
        return 0
    if (has_usby and has_m2 and has_hy2) or (not has_usby and has_m2):
        return 1
    if not has_usby:
        return 2
    return 3


def _is_blocked_hk_cn_ru(region: str) -> bool:
    """True for HK/CN/RU exits, which trigger anti-bot or regional blocks."""
    r = str(region or "")
    low = r.lower()
    # Legacy classification tags every 🇨🇳 name as Taiwan, so the TW label wins.
    if "台湾" in r or "taiwan" in low or "TW" in r:
        return False
    return (
        "🇭🇰" in r
        or "HK" in r
        or "香港" in r
        or "🇨🇳" in r
        or "CN" in r
        or "中国大陆" in r
        or "中国内地" in r
        or "🇷🇺" in r
        or "RU" in r
        or "俄罗斯" in r
    )


def rank_nodes_for_workbuddy(nodes: list[dict]) -> list[dict]:
    """Rank residential/ISP exits first, then clean regions ahead of HK.

    The residential tier is the primary key: direct MiyaIP-2 HY2 lines
    (``US | MiyaIP-2 HY2``, tier 0) lead, followed by MiyaIP-2 lines
    (``US | MiyaIP-2`` / ``USbyJPTT | MiyaIP-2 HY2``, tier 1), then other
    direct residential lines (``US | MiyaIP``, tier 2) and relayed residential
    lines (``USbyJPTT | MiyaIP``, tier 3). All of them lead every generic
    airport node, including SG/JP IEPL dedicated lines, so
    domain-mailbox/WorkBuddy signups never land on a shared blacklisted airport
    IP. Only then does region priority apply: ``SG`` > ``JP`` > ``TW`` >
    ``US`` > ``KR`` > other > ``HK``. Within a tier timed-out nodes go last,
    IEPL/专线/IPLC lines come next, and lower positive latency comes first.
    Non-residential nodes keep tier 4, so their historical ordering (including
    timed-out SG still ahead of healthy HK) is unchanged.
    """
    indexed: list[tuple[int, dict]] = []
    for idx, node in enumerate(nodes or []):
        if not isinstance(node, dict):
            continue
        indexed.append((idx, node))

    def sort_key(item: tuple[int, dict]) -> tuple:
        idx, node = item
        name = str(node.get("name") or "")
        region = str(node.get("region") or classify_node_region(name))
        delay = node.get("delay")
        try:
            delay_val = int(delay) if delay and int(delay) > 0 else 0
        except (TypeError, ValueError):
            delay_val = 0
        is_iepl = 0 if re.search(r"iepl|专线|iplc", name, re.I) else 1
        is_hk = _is_blocked_hk_cn_ru(region)
        timed_out = bool(node.get("timed_out"))
        # Residential lines only lead when they are alive and not HK/CN/RU
        # exits; every other node keeps tier 4 so the legacy region ordering
        # still governs generic airport nodes.
        tier = 4
        if not is_hk and not timed_out:
            tier = _residential_tier(name)
        return (
            tier,
            _region_priority(region),
            1 if timed_out else 0,
            is_iepl,
            delay_val if delay_val > 0 else 10**9,
            idx,
        )

    return [node for _, node in sorted(indexed, key=sort_key)]


_CLAUDE_REGION_PRIORITY = {
    "🇺🇸 美国 (US)": 0,
    "🇸🇬 新加坡 (SG)": 1,
    "🇯🇵 日本 (JP)": 2,
    "🇹🇼 台湾 (TW)": 3,
    "🇨🇳 台湾 (TW)": 3,
    "🇰🇷 韩国 (KR)": 4,
    "🇭🇰 香港 (HK)": 7,
}


def _is_claude_residential(name: str) -> bool:
    """Detect clean residential/ISP lines without matching words like "whisper"."""
    return _is_residential_node(name)


def _is_claude_blocked_region(name: str, region: str) -> bool:
    """Anthropic blocks HK/CN/RU exits, so those rank last."""
    low = name.lower()
    # Explicit mainland-China / Russia exits come first: legacy classification
    # tags any 🇨🇳 name as Taiwan, so the TW label alone cannot be trusted.
    if any(k in low for k in ("俄罗斯", "russia", "中国大陆", "中国内地", "china")):
        return True
    if "🇷🇺" in name:
        return True
    if "台湾" in name or "taiwan" in low or "TW" in region:
        return False
    return _is_blocked_hk_cn_ru(region) or "🇨🇳" in name


def rank_nodes_for_claude(nodes: list[dict]) -> list[dict]:
    """Rank nodes for Claude: US first, clean residential/ISP US nodes on top.

    Anthropic blocks HK/CN/RU exits. Within the US bucket direct MiyaIP-2 HY2
    lines (``US | MiyaIP-2 HY2``) beat other MiyaIP-2 lines and relayed ones
    (``USbyJPTT | MiyaIP``), and all residential lines beat generic
    datacenter/airport nodes. Returns new dicts and never mutates the caller's
    node dicts.
    """
    indexed: list[tuple[int, dict]] = []
    for idx, node in enumerate(nodes or []):
        if not isinstance(node, dict):
            continue
        indexed.append((idx, node))

    def sort_key(item: tuple[int, dict]) -> tuple:
        idx, node = item
        name = str(node.get("name") or "")
        region = str(node.get("region") or classify_node_region(name))
        is_blocked = _is_claude_blocked_region(name, region)
        region_rank = 7 if is_blocked else _CLAUDE_REGION_PRIORITY.get(region, 5)

        is_us = region == "🇺🇸 美国 (US)"
        is_residential = _is_claude_residential(name)
        if is_us:
            tier = _residential_tier(name)
        else:
            tier = 0 if is_residential else 4

        delay = node.get("delay")
        try:
            delay_val = int(delay) if delay and int(delay) > 0 else 0
        except (TypeError, ValueError):
            delay_val = 0
        is_iepl = 0 if re.search(r"iepl|专线|iplc", name, re.I) else 1
        return (
            region_rank,
            # A dead residential line is useless: timed-out nodes sink below
            # every healthy node in the same region before tier is compared.
            1 if node.get("timed_out") else 0,
            tier,
            is_iepl,
            delay_val if delay_val > 0 else 10**9,
            idx,
        )

    ranked: list[dict] = []
    for _, node in sorted(indexed, key=sort_key):
        name = str(node.get("name") or "")
        region = str(node.get("region") or classify_node_region(name))
        enriched = dict(node)
        enriched["region"] = region
        ranked.append(enriched)
    return ranked


def _port_is_free(port: int) -> bool:
    if not (1 <= int(port) <= 65535):
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", int(port)))
            return True
    except OSError:
        return False


def _pick_free_port(preferred: int, fallback_range: range) -> int:
    if _port_is_free(preferred):
        return int(preferred)
    for candidate in fallback_range:
        if _port_is_free(candidate):
            return int(candidate)
    return int(preferred)


def _collect_hosts_map(data: dict) -> dict[str, str]:
    """Extract a ``hostname -> ip`` map from a Clash ``hosts:`` section.

    Values may be a plain string or a list of candidates; the first non-empty
    string wins. Non-string / empty entries are ignored.
    """
    raw_hosts = data.get("hosts") if isinstance(data, dict) else None
    if not isinstance(raw_hosts, dict):
        return {}
    hosts: dict[str, str] = {}
    for host, target in raw_hosts.items():
        key = str(host or "").strip()
        if not key:
            continue
        candidates = target if isinstance(target, (list, tuple)) else [target]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                hosts[key] = candidate.strip()
                break
    return hosts


def _resolve_host(
    server: str,
    per_file_hosts: dict[str, str],
    global_hosts: dict[str, str],
    *,
    max_hops: int = 5,
) -> str:
    """Resolve a decoy hostname through ``hosts:`` chains, IPs pass through.

    ``per_file_hosts`` (the YAML that declared the proxy) wins over the merged
    ``global_hosts`` map. At most ``max_hops`` hops are followed and a
    ``visited`` guard breaks cycles, so a misconfigured map can never loop.
    """
    resolved = str(server or "").strip()
    if not resolved:
        return resolved
    visited: set[str] = set()
    for _ in range(max_hops):
        if resolved in visited:
            break
        visited.add(resolved)
        target = per_file_hosts.get(resolved) or global_hosts.get(resolved)
        if not target or target == resolved:
            break
        resolved = str(target).strip()
    return resolved


class SidecarProxyManager:
    """Owns a private Mihomo process fed from a read-only subscription YAML."""

    def __init__(
        self,
        runtime_dir: str | Path | None = None,
        proxy_port: int = DEFAULT_SIDECAR_PROXY_PORT,
        ctrl_port: int = DEFAULT_SIDECAR_CTRL_PORT,
    ):
        if runtime_dir is None:
            self.runtime_dir = Path(__file__).resolve().parent.parent / "data" / "runtime" / "mihomo_sidecar"
        else:
            self.runtime_dir = Path(runtime_dir)
        self.preferred_proxy_port = int(proxy_port)
        self.preferred_ctrl_port = int(ctrl_port)
        self.proxy_port = int(proxy_port)
        self.ctrl_port = int(ctrl_port)
        self._secret = secrets.token_hex(16)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self.active_node: str = ""
        self._loaded_yaml_sig: tuple | None = None
        self._last_hosts_map: dict[str, str] = {}
        self._atexit_registered = False
        self._config_path = self.runtime_dir / "config.yaml"
        self._slot_ports: list[int] = [
            self.proxy_port + 1 + i for i in range(SIDECAR_SLOT_COUNT)
        ]
        self._slot_nodes: dict[int, str] = {}
        self._slot_last_used: dict[int, float] = {}
        self._account_slots: dict[int, int] = {}

    # ------------------------------------------------------------------
    # Read-only discovery
    # ------------------------------------------------------------------
    def extract_leaf_proxies(self) -> tuple[str, Path | None, list[dict], list[dict]]:
        """Read the first discovered subscription YAML and extract leaf proxies."""
        configs = find_clash_config_files()
        if not configs:
            return ("", None, [], [])
        client_label, yaml_path = configs[0]
        primary_path = Path(yaml_path)
        yaml_files: list[Path] = [primary_path]

        if not (os.environ.get("KEYVAULT_CLASH_CONFIG") or "").strip():
            profiles_index = primary_path.parent / "profiles.yaml"
            if profiles_index.is_file():
                try:
                    profiles_data = (
                        yaml.safe_load(
                            profiles_index.read_text(encoding="utf-8", errors="ignore")
                        )
                        or {}
                    )
                except Exception:
                    profiles_data = {}
                if isinstance(profiles_data, dict):
                    for item in profiles_data.get("items") or []:
                        if not isinstance(item, dict):
                            continue
                        # Local/merged profiles are also valid sources of leaf
                        # proxies, so scan them alongside remote subscriptions.
                        if item.get("type") not in ("remote", "local") or not item.get("file"):
                            continue
                        profile_path = (
                            primary_path.parent / "profiles" / str(item["file"])
                        )
                        if profile_path.is_file():
                            yaml_files.append(profile_path)

            provider_dir = primary_path.parent / "proxy_provider"
            if provider_dir.is_dir():
                yaml_files.extend(sorted(provider_dir.glob("*.yaml")))

        loaded: list[tuple[Path, dict]] = []
        per_file_hosts: dict[str, dict[str, str]] = {}
        global_hosts: dict[str, str] = {}
        group_names_in_yaml: set[str] = set()
        for path in yaml_files:
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore")) or {}
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            loaded.append((path, data))
            file_hosts = _collect_hosts_map(data)
            per_file_hosts[str(path)] = file_hosts
            for host, target in file_hosts.items():
                global_hosts.setdefault(host, target)
            group_names_in_yaml.update(
                str(g.get("name") or "").strip()
                for g in (data.get("proxy-groups") or [])
                if isinstance(g, dict) and g.get("name")
            )

        self._last_hosts_map = dict(global_hosts)

        raw_proxies: list[dict] = []
        node_summaries: list[dict] = []
        seen: set[str] = set()
        invalid_servers = (None, "", "1.1.1.1", "2.2.2.2", "0.0.0.0")
        for path, data in loaded:
            file_hosts = per_file_hosts.get(str(path), {})
            for px in data.get("proxies") or []:
                if not isinstance(px, dict):
                    continue
                nname = str(px.get("name") or "").strip()
                ptype = str(px.get("type") or "").strip()
                if (
                    not nname
                    or not ptype
                    or px.get("server") in invalid_servers
                    or px.get("port") in (None, "")
                ):
                    continue
                if not is_valid_proxy_node_name(nname, group_names_in_yaml) or nname in seen:
                    continue
                seen.add(nname)
                px_copy = dict(px)
                raw_srv = str(px_copy.get("server") or "").strip()
                resolved_srv = _resolve_host(raw_srv, file_hosts, global_hosts)
                px_copy["server"] = resolved_srv
                raw_proxies.append(px_copy)
                node_summaries.append(
                    {
                        "name": nname,
                        "type": ptype,
                        "server": resolved_srv,
                        "region": classify_node_region(nname),
                        "delay": None,
                    }
                )
        return (client_label, primary_path, raw_proxies, node_summaries)

    # ------------------------------------------------------------------
    # Environment description (fast, never spawns the kernel)
    # ------------------------------------------------------------------
    def describe_environment(self, account_type: str = "") -> dict:
        client_label, yaml_path, raw_proxies, node_summaries = self.extract_leaf_proxies()
        mihomo_bin = find_mihomo_binary()
        sys_proxy = get_system_proxy()

        nodes = list(node_summaries)
        acc_low = (account_type or "").lower()
        is_claude = acc_low in ("claude", "anthropic")
        if acc_low in ("workbuddy", "域名邮箱", "github", "domain-email"):
            nodes = rank_nodes_for_workbuddy(nodes)
        elif is_claude:
            nodes = rank_nodes_for_claude(nodes)

        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            port = int(self.proxy_port)
            active_node_snapshot = self.active_node
        if not running:
            port = _pick_free_port(
                self.preferred_proxy_port,
                range(self.preferred_proxy_port + 2, self.preferred_proxy_port + 32),
            )

        if raw_proxies and mihomo_bin:
            proxy_url = f"http://127.0.0.1:{port}"
            ports = [
                {
                    "port": port,
                    "url": proxy_url,
                    "label": f"127.0.0.1:{port} (KeyVault 专属独立代理端口 · 零干扰本地 Verge)",
                    "bound_group": SIDECAR_GROUP_NAME,
                }
            ]
            controller_online = True
            client_name = f"KeyVault 独立代理内核 (订阅源: {client_label or 'Clash 配置'})"
            sidecar = True
            if is_claude and yaml_path is not None:
                ports.extend(self._claude_local_listener_ports(yaml_path, port))
        else:
            summary = (
                parse_clash_yaml_summary(yaml_path)
                if yaml_path
                else {"ports": []}
            )
            ports = list(summary.get("ports") or [])
            if sys_proxy.get("server"):
                srv = str(sys_proxy["server"])
                srv_url = srv if "://" in srv else f"http://{srv}"
                if not any(p.get("url") == srv_url for p in ports):
                    ports.insert(
                        0,
                        {
                            "port": 0,
                            "url": srv_url,
                            "label": f"{srv} (系统当前代理端口)",
                            "bound_group": "",
                        },
                    )
            controller_online = False
            if client_label:
                client_name = client_label
            elif sys_proxy.get("server"):
                client_name = f"系统代理 ({sys_proxy['server']})"
            elif ports:
                client_name = "本地代理端口"
            else:
                client_name = "未检测到代理客户端 (可手动输入代理地址)"
            sidecar = False

        result = {
            "client_name": client_name,
            "controller_online": controller_online,
            "system_proxy": sys_proxy,
            "ports": ports,
            "nodes": nodes,
            "current_groups": {SIDECAR_GROUP_NAME: active_node_snapshot} if active_node_snapshot else {},
            "active_node": active_node_snapshot,
            "sidecar": bool(sidecar),
        }
        if is_claude:
            result["recommended_node"] = str(nodes[0].get("name") or "") if nodes else ""
        return result

    def _claude_local_listener_ports(self, yaml_path: Path, sidecar_port: int) -> list[dict]:
        """Expose live local Clash Verge listeners as Claude fallback exits.

        Only ports that are actually listening are returned, and the sidecar's
        own port is never duplicated. Fixed ``AI-US`` listeners (7897) lead so
        Claude can be pinned to the dedicated US exit without touching the
        multi-kernel mixed port.
        """
        try:
            summary = parse_clash_yaml_summary(yaml_path)
        except Exception:
            return []
        candidates: list[dict] = []
        for entry in summary.get("ports") or []:
            if not isinstance(entry, dict):
                continue
            try:
                listener_port = int(entry.get("port") or 0)
            except (TypeError, ValueError):
                continue
            if listener_port <= 0 or listener_port == int(sidecar_port):
                continue
            if not _is_local_port_open(listener_port):
                continue
            label = str(entry.get("label") or "")
            url = str(entry.get("url") or f"http://127.0.0.1:{listener_port}")
            if listener_port == 7897:
                label = (
                    "127.0.0.1:7897 (Clash Verge AI-US 固定美国出口 · "
                    "防止与多内核串流)"
                )
            candidates.append(
                {
                    "port": listener_port,
                    "url": url,
                    "label": label,
                    "bound_group": str(entry.get("bound_group") or ""),
                }
            )
        candidates.sort(key=lambda p: (0 if p["port"] == 7897 else 1, p["port"]))
        return candidates

    # ------------------------------------------------------------------
    # Controller HTTP helpers
    # ------------------------------------------------------------------
    def _ctrl_request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        timeout: float = 3.0,
    ) -> tuple[int, dict | None]:
        url = f"http://127.0.0.1:{self.ctrl_port}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self._secret}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = int(getattr(resp, "status", 200))
                raw = resp.read()
                if not raw or status == 204:
                    return status, {}
                try:
                    parsed = json.loads(raw.decode("utf-8", errors="ignore"))
                except Exception:
                    return status, None
                return status, parsed if isinstance(parsed, dict) else None
        except urllib.error.HTTPError as exc:
            return int(exc.code), None
        except Exception:
            return 0, None

    def _yaml_signature(self, yaml_path: Path | None) -> tuple | None:
        if yaml_path is None:
            return None
        try:
            stat = yaml_path.stat()
            signature: list = [
                SIDECAR_CONFIG_VERSION,
                str(yaml_path),
                float(stat.st_mtime),
                int(stat.st_size),
            ]
        except OSError:
            return None
        profiles_index = Path(yaml_path).parent / "profiles.yaml"
        try:
            if profiles_index.is_file():
                profiles_stat = profiles_index.stat()
                signature.extend(
                    [
                        str(profiles_index),
                        float(profiles_stat.st_mtime),
                        int(profiles_stat.st_size),
                    ]
                )
                try:
                    profiles_data = (
                        yaml.safe_load(
                            profiles_index.read_text(encoding="utf-8", errors="ignore")
                        )
                        or {}
                    )
                except Exception:
                    profiles_data = {}
                if isinstance(profiles_data, dict):
                    for item in profiles_data.get("items") or []:
                        if not isinstance(item, dict) or not item.get("file"):
                            continue
                        profile_path = (
                            Path(yaml_path).parent / "profiles" / str(item["file"])
                        )
                        try:
                            profile_stat = profile_path.stat()
                        except OSError:
                            continue
                        signature.extend(
                            [
                                str(profile_path),
                                float(profile_stat.st_mtime),
                                int(profile_stat.st_size),
                            ]
                        )
        except OSError:
            pass
        provider_dir = Path(yaml_path).parent / "proxy_provider"
        try:
            if provider_dir.is_dir():
                for pfile in sorted(provider_dir.glob("*.yaml")):
                    try:
                        pstat = pfile.stat()
                        signature.extend([str(pfile), float(pstat.st_mtime), int(pstat.st_size)])
                    except OSError:
                        continue
        except OSError:
            pass
        return tuple(signature)

    def _build_config(
        self,
        raw_proxies: list[dict],
        hosts_map: dict[str, str] | None = None,
    ) -> dict:
        resolved_hosts = (
            dict(hosts_map)
            if hosts_map is not None
            else dict(getattr(self, "_last_hosts_map", {}) or {})
        )
        proxy_names = [p["name"] for p in raw_proxies]
        slot_ports = getattr(self, "_slot_ports", None) or [
            self.proxy_port + 1 + i for i in range(SIDECAR_SLOT_COUNT)
        ]
        listeners = [
            {
                "name": f"kv-slot-{idx}",
                "type": "mixed",
                "listen": "127.0.0.1",
                "port": int(port),
            }
            for idx, port in enumerate(slot_ports)
        ]
        proxy_groups = [
            {
                "name": SIDECAR_GROUP_NAME,
                "type": "select",
                "proxies": proxy_names,
            },
            *[
                {
                    "name": f"KV_SLOT_{idx}",
                    "type": "select",
                    "proxies": proxy_names,
                }
                for idx in range(len(slot_ports))
            ],
        ]
        slot_rules = [
            f"IN-NAME,kv-slot-{idx},KV_SLOT_{idx}"
            for idx in range(len(slot_ports))
        ]
        config: dict = {
            "mixed-port": self.proxy_port,
            "bind-address": "127.0.0.1",
            "allow-lan": False,
            "mode": "rule",
            # The sidecar serves dedicated local ports, so process sniffing
            # only wastes time and could mix rules across kernels.
            "find-process-mode": "off",
            "log-level": "warning",
            "ipv6": False,
            "external-controller": f"127.0.0.1:{self.ctrl_port}",
            "secret": self._secret,
            "profile": {"store-selected": False},
            "tun": {"enable": False},
            # ``use-hosts`` lets the kernel resolve the subscription's decoy
            # hostnames through the ``hosts`` map we merge below.
            "dns": {"enable": False, "use-hosts": True},
            "listeners": listeners,
            "proxies": raw_proxies,
            "proxy-groups": proxy_groups,
            "rules": [
                # Block STUN/TURN so WebRTC can never leak the local IP.
                "DOMAIN-KEYWORD,stun,REJECT",
                "DST-PORT,3478,REJECT",
                "DST-PORT,5349,REJECT",
                "DOMAIN-SUFFIX,ggb6.com,DIRECT",
                # Dedicated per-account/per-node concurrent isolation slots.
                *slot_rules,
                # Anthropic/Claude traffic must always exit through the proxy.
                f"DOMAIN-SUFFIX,claude.ai,{SIDECAR_GROUP_NAME}",
                f"DOMAIN-SUFFIX,anthropic.com,{SIDECAR_GROUP_NAME}",
                f"DOMAIN-SUFFIX,claudeusercontent.com,{SIDECAR_GROUP_NAME}",
                # ``no-resolve`` keeps Mihomo from falling back to the local
                # Windows DNS resolver to classify proxy-bound domains.
                "GEOIP,CN,DIRECT,no-resolve",
                f"MATCH,{SIDECAR_GROUP_NAME}",
            ],
        }
        if resolved_hosts:
            config["hosts"] = resolved_hosts
        return config

    def _write_config(
        self,
        raw_proxies: list[dict],
        hosts_map: dict[str, str] | None = None,
    ) -> Path:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        config_path = self._config_path
        config_path.write_text(
            yaml.safe_dump(
                self._build_config(raw_proxies, hosts_map),
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return config_path

    def _prepare_sidecar_exe(self, mihomo_bin: Path) -> Path:
        """Stage the kernel under a ProxyBridge-friendly executable name.

        On Windows the target is ``mihomo.exe`` so ProxyBridge's
        ``代理核心直连`` process rule (``verge-mihomo.exe; mihomo.exe; ...``)
        sends the kernel's own traffic DIRECT instead of double-proxying it
        through the catch-all ``17898`` config.
        """
        if sys.platform == "win32":
            preferred = self.runtime_dir / "mihomo.exe"
            if self._copy_sidecar_binary(mihomo_bin, preferred):
                return preferred
            if preferred.is_file() and preferred.stat().st_size > 0:
                return preferred
            fallback = self.runtime_dir / "keyvault-mihomo.exe"
            if self._copy_sidecar_binary(mihomo_bin, fallback):
                return fallback
            if fallback.is_file() and fallback.stat().st_size > 0:
                return fallback
            return mihomo_bin

        target = self.runtime_dir / "mihomo"
        if self._copy_sidecar_binary(mihomo_bin, target):
            return target
        if target.is_file() and target.stat().st_size > 0:
            return target
        return mihomo_bin

    @staticmethod
    def _copy_sidecar_binary(source: Path, target: Path) -> bool:
        """Copy ``source`` onto ``target`` unless an identical copy is staged."""
        try:
            needs_copy = (
                not target.exists()
                or target.stat().st_size != source.stat().st_size
            )
            if needs_copy:
                shutil.copy2(source, target)
            return True
        except Exception:
            # A running kernel may hold the destination open; callers fall
            # back to whatever binary is already staged.
            return False

    def _stage_geoip_databases(self, yaml_path: Path | None, mihomo_bin: Path) -> None:
        """Stage a local MMDB so the kernel never has to download one.

        The sidecar kernel now runs DIRECT (ProxyBridge's ``代理核心直连`` rule),
        so mihomo cannot reach the geodata CDN itself. Copying an existing
        database from the Clash Verge install keeps ``GEOIP`` rules working and
        startup instant.
        """
        source_dirs: list[Path] = []
        if yaml_path is not None:
            source_dirs.append(Path(yaml_path).parent)
        if mihomo_bin is not None:
            source_dirs.append(Path(mihomo_bin).parent)
            source_dirs.append(Path(mihomo_bin).parent / "resources")
        for name in ("Country.mmdb", "geoip.metadb"):
            target = self.runtime_dir / name
            if target.exists():
                continue
            for source_dir in source_dirs:
                source = source_dir / name
                try:
                    if source.is_file():
                        shutil.copy2(source, target)
                        break
                except Exception:
                    continue

    def _wait_for_controller(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False
            status, _ = self._ctrl_request("GET", "/proxies", timeout=0.8)
            if status == 200:
                return True
            time.sleep(0.1)
        return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _apply_active_or_default_node(
        self,
        node_summaries: list[dict],
        *,
        preferred_node: str = "",
        account_type: str = "",
    ) -> None:
        """Re-select the active node after a fresh launch or a hot reload.

        ``self.active_node`` is only overwritten once the kernel acknowledges
        the selection, so callers can trust it as the live active node.
        ``preferred_node`` wins over the previous selection and over ranking.
        """
        preferred = (preferred_node or "").strip()
        if preferred and any(n.get("name") == preferred for n in node_summaries):
            status, _ = self._ctrl_request(
                "PUT",
                f"/proxies/{SIDECAR_GROUP_NAME}",
                {"name": preferred},
                timeout=2.5,
            )
            if status in (200, 204):
                self.active_node = preferred
                return

        if self.active_node:
            if any(n.get("name") == self.active_node for n in node_summaries):
                status, _ = self._ctrl_request(
                    "PUT",
                    f"/proxies/{SIDECAR_GROUP_NAME}",
                    {"name": self.active_node},
                    timeout=2.5,
                )
                if status in (200, 204):
                    return
            self.active_node = ""

        if not node_summaries:
            return
        if (account_type or "").strip().lower() in ("claude", "anthropic"):
            ranked = rank_nodes_for_claude(node_summaries)
        else:
            ranked = rank_nodes_for_workbuddy(node_summaries)
        ranked = ranked or list(node_summaries)
        candidate = str(ranked[0].get("name") or "").strip()
        if not candidate:
            return
        status, _ = self._ctrl_request(
            "PUT",
            f"/proxies/{SIDECAR_GROUP_NAME}",
            {"name": candidate},
            timeout=2.5,
        )
        if status in (200, 204):
            self.active_node = candidate

    def _cleanup_orphan_sidecars(self) -> None:
        """Kill stale sidecar kernels left behind by a previous app instance.

        Only processes whose executable lives inside ``self.runtime_dir`` are
        touched — never the user's ``verge-mihomo.exe`` or any kernel staged
        elsewhere.
        """
        if sys.platform == "win32":
            self._cleanup_orphan_sidecars_windows()
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if _port_is_free(self.preferred_proxy_port) and _port_is_free(
                    self.preferred_ctrl_port
                ):
                    return
                time.sleep(0.1)

    def _cleanup_orphan_sidecars_windows(self) -> None:
        allowed = {
            os.path.normcase(str((self.runtime_dir / name).resolve()))
            for name in ("mihomo.exe", "keyvault-mihomo.exe")
        }
        script = (
            "$ErrorActionPreference='SilentlyContinue'; "
            "Get-CimInstance Win32_Process "
            "| Where-Object { $_.ExecutablePath } "
            "| Select-Object ProcessId,ExecutablePath "
            "| ConvertTo-Json -Compress"
        )
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
        except Exception:
            return
        raw = (completed.stdout or "").strip()
        if not raw:
            return
        try:
            rows = json.loads(raw)
        except Exception:
            return
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            exe_path = str(row.get("ExecutablePath") or "").strip()
            if not exe_path:
                continue
            try:
                normalized = os.path.normcase(str(Path(exe_path).resolve()))
            except OSError:
                continue
            if normalized not in allowed:
                continue
            pid = row.get("ProcessId")
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                continue
            if pid <= 0:
                continue
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                )
            except Exception:
                continue

    def ensure_started(
        self,
        *,
        preferred_node: str = "",
        account_type: str = "",
    ) -> dict:
        with self._lock:
            client_label, yaml_path, raw_proxies, node_summaries = self.extract_leaf_proxies()
            mihomo_bin = find_mihomo_binary()
            if not raw_proxies or not mihomo_bin:
                return {
                    "ok": False,
                    "error": "未找到可用的 Mihomo 内核或订阅节点配置",
                    "proxy_url": "",
                    "nodes": [],
                }

            yaml_sig = self._yaml_signature(yaml_path)

            if self._proc is not None and self._proc.poll() is None:
                status, _ = self._ctrl_request("GET", "/proxies", timeout=1.0)
                if status == 200:
                    reloaded = False
                    if yaml_sig is not None and yaml_sig != self._loaded_yaml_sig:
                        try:
                            config_path = self._write_config(
                                raw_proxies, self._last_hosts_map
                            )
                            reload_status, _ = self._ctrl_request(
                                "PUT",
                                "/configs?force=true",
                                {"path": str(config_path)},
                                timeout=2.0,
                            )
                            if reload_status in (200, 204):
                                self._loaded_yaml_sig = yaml_sig
                                reloaded = True
                        except Exception:
                            pass
                    if reloaded:
                        # A forced reload resets the selector to its first node.
                        self._apply_active_or_default_node(
                            node_summaries,
                            preferred_node=preferred_node,
                            account_type=account_type,
                        )
                        self._reapply_slot_nodes()
                    return {
                        "ok": True,
                        "proxy_url": f"http://127.0.0.1:{self.proxy_port}",
                        "ctrl_url": f"http://127.0.0.1:{self.ctrl_port}",
                        "nodes": node_summaries,
                        "active_node": self.active_node,
                    }
                self.stop()

            if not _port_is_free(self.preferred_proxy_port) or not _port_is_free(
                self.preferred_ctrl_port
            ):
                self._cleanup_orphan_sidecars()

            self.proxy_port = _pick_free_port(
                self.preferred_proxy_port,
                range(self.preferred_proxy_port + 2, self.preferred_proxy_port + 32),
            )
            self.ctrl_port = _pick_free_port(
                self.preferred_ctrl_port,
                range(self.preferred_ctrl_port + 1, self.preferred_ctrl_port + 30),
            )
            self._slot_ports = self._allocate_free_slot_ports(
                start_port=max(self.proxy_port + 1, 17900),
                count=SIDECAR_SLOT_COUNT,
                exclude={self.proxy_port, self.ctrl_port},
            )

            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            sidecar_exe = self._prepare_sidecar_exe(mihomo_bin)
            self._stage_geoip_databases(yaml_path, mihomo_bin)

            try:
                config_path = self._write_config(raw_proxies, self._last_hosts_map)
            except Exception as exc:
                return {
                    "ok": False,
                    "error": f"写入独立代理配置失败: {exc}",
                    "proxy_url": "",
                    "nodes": node_summaries,
                }

            creationflags = 0
            if sys.platform == "win32":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            try:
                self._proc = subprocess.Popen(
                    [str(sidecar_exe), "-d", str(self.runtime_dir), "-f", str(config_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
            except Exception as exc:
                self._proc = None
                return {
                    "ok": False,
                    "error": f"启动独立代理内核失败: {exc}",
                    "proxy_url": "",
                    "nodes": node_summaries,
                }

            if not self._atexit_registered:
                atexit.register(self.stop)
                self._atexit_registered = True

            if not self._wait_for_controller(5.0):
                self.stop()
                return {
                    "ok": False,
                    "error": "启动独立代理内核超时",
                    "proxy_url": "",
                    "nodes": node_summaries,
                }

            self._loaded_yaml_sig = yaml_sig
            self._apply_active_or_default_node(
                node_summaries,
                preferred_node=preferred_node,
                account_type=account_type,
            )
            return {
                "ok": True,
                "proxy_url": f"http://127.0.0.1:{self.proxy_port}",
                "ctrl_url": f"http://127.0.0.1:{self.ctrl_port}",
                "nodes": node_summaries,
                "active_node": self.active_node,
            }

    def _allocate_free_slot_ports(
        self, start_port: int, count: int, exclude: set[int]
    ) -> list[int]:
        ports: list[int] = []
        used = set(exclude)
        candidate = int(start_port)
        limit = candidate + 200
        while len(ports) < count and candidate < limit:
            if candidate not in used and _port_is_free(candidate):
                ports.append(candidate)
                used.add(candidate)
            candidate += 1
        while len(ports) < count:
            ports.append(candidate)
            candidate += 1
        return ports

    def _reapply_slot_nodes(self) -> None:
        for slot_idx, node_name in list(self._slot_nodes.items()):
            if node_name:
                self._ctrl_request(
                    "PUT",
                    f"/proxies/KV_SLOT_{slot_idx}",
                    {"name": node_name},
                    timeout=2.0,
                )

    def _choose_slot_for_account(
        self, node_name: str, account_id: int | None
    ) -> int:
        total = max(1, len(self._slot_ports))
        if account_id is not None:
            aid = int(account_id)
            if aid in self._account_slots and 0 <= self._account_slots[aid] < total:
                return self._account_slots[aid]
            assigned = set(self._account_slots.values())
            for idx in range(total):
                if idx not in assigned:
                    return idx
        # Reuse a slot already pointed at the exact same node if available
        for idx in range(total):
            if self._slot_nodes.get(idx) == node_name:
                return idx
        # Otherwise pick an unused slot first, else LRU
        for idx in range(total):
            if idx not in self._slot_nodes:
                return idx
        return min(range(total), key=lambda i: self._slot_last_used.get(i, 0.0))

    def _close_slot_connections(self, slot_idx: int) -> None:
        inbound = f"kv-slot-{slot_idx}"
        status, payload = self._ctrl_request("GET", "/connections", timeout=1.5)
        if status != 200 or not isinstance(payload, dict):
            return
        for conn in payload.get("connections") or []:
            if not isinstance(conn, dict):
                continue
            meta = conn.get("metadata") or {}
            if meta.get("inboundName") == inbound and conn.get("id"):
                self._ctrl_request("DELETE", f"/connections/{conn['id']}", timeout=1.0)

    def _close_shared_connections(self) -> None:
        status, payload = self._ctrl_request("GET", "/connections", timeout=1.5)
        if status != 200 or not isinstance(payload, dict):
            return
        for conn in payload.get("connections") or []:
            if not isinstance(conn, dict) or not conn.get("id"):
                continue
            inbound = str(((conn.get("metadata") or {}).get("inboundName")) or "")
            if not inbound.startswith("kv-slot-"):
                self._ctrl_request("DELETE", f"/connections/{conn['id']}", timeout=1.0)

    def preflight_github(self, timeout: float = 6.0, proxy_url: str = "") -> dict:
        eff_proxy = proxy_url or f"http://127.0.0.1:{self.proxy_port}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": eff_proxy, "https": eff_proxy})
        )
        req = urllib.request.Request(
            "https://github.com/",
            headers={
                "User-Agent": _GITHUB_PREFLIGHT_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        started = time.time()
        status = 0
        reachable = False
        body_text = ""
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = int(getattr(resp, "status", 200))
                reachable = True
                body_text = resp.read(24576).decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as exc:
            reachable = True
            status = int(exc.code)
            try:
                body_text = exc.read(24576).decode("utf-8", errors="ignore")
            except Exception:
                body_text = ""
        except Exception:
            reachable = False

        latency_ms = int((time.time() - started) * 1000)
        restricted = bool(
            status in (403, 429) or (body_text and _RESTRICTED_PATTERN.search(body_text))
        )
        return {
            "ok": bool(reachable and status < 400 and not restricted),
            "reachable": reachable,
            "restricted": restricted,
            "status": status,
            "latency_ms": latency_ms,
        }

    def switch_node(
        self,
        node_name: str,
        *,
        preflight: bool = False,
        preflight_timeout: float = 6.0,
        isolate_slot: bool = False,
        account_id: int | None = None,
    ) -> dict:
        with self._lock:
            started = self.ensure_started()
            if not started.get("ok"):
                return {
                    "ok": False,
                    "switched": False,
                    "error": started.get("error", "sidecar unavailable"),
                }

            if isolate_slot and self._slot_ports:
                slot_idx = self._choose_slot_for_account(node_name, account_id)
                group_name = f"KV_SLOT_{slot_idx}"
                prev_node = self._slot_nodes.get(slot_idx, "")
                status, _ = self._ctrl_request(
                    "PUT",
                    f"/proxies/{group_name}",
                    {"name": node_name},
                    timeout=2.5,
                )
                if status not in (200, 204):
                    return {
                        "ok": False,
                        "switched": False,
                        "error": f"独立代理内核未找到节点: {node_name}",
                        "proxy_url": started["proxy_url"],
                    }
                if prev_node and prev_node != node_name:
                    self._close_slot_connections(slot_idx)
                self._slot_nodes[slot_idx] = node_name
                self._slot_last_used[slot_idx] = time.time()
                if account_id is not None:
                    aid = int(account_id)
                    for other_aid, other_slot in list(self._account_slots.items()):
                        if other_slot == slot_idx and other_aid != aid:
                            del self._account_slots[other_aid]
                    self._account_slots[aid] = slot_idx
                slot_port = self._slot_ports[slot_idx]
                slot_url = f"http://127.0.0.1:{slot_port}"
                result = {
                    "ok": True,
                    "switched": True,
                    "node": node_name,
                    "slot": slot_idx,
                    "proxy_url": slot_url,
                    "groups": [group_name],
                }
                if preflight:
                    pf = self.preflight_github(timeout=preflight_timeout, proxy_url=slot_url)
                    result.update(
                        {
                            "preflight_ok": pf["ok"],
                            "reachable": pf["reachable"],
                            "restricted": pf["restricted"],
                            "latency_ms": pf["latency_ms"],
                        }
                    )
                return result

            status, _ = self._ctrl_request(
                "PUT",
                f"/proxies/{SIDECAR_GROUP_NAME}",
                {"name": node_name},
                timeout=2.5,
            )
            if status not in (200, 204):
                return {
                    "ok": False,
                    "switched": False,
                    "error": f"独立代理内核未找到节点: {node_name}",
                    "proxy_url": started["proxy_url"],
                }

            # Drop only shared mixed-port connections so isolated account slots stay untouched.
            self._close_shared_connections()

            self.active_node = node_name
            result = {
                "ok": True,
                "switched": True,
                "node": node_name,
                "proxy_url": f"http://127.0.0.1:{self.proxy_port}",
                "groups": [SIDECAR_GROUP_NAME],
            }
            if preflight:
                pf = self.preflight_github(timeout=preflight_timeout)
                result.update(
                    {
                        "preflight_ok": pf["ok"],
                        "reachable": pf["reachable"],
                        "restricted": pf["restricted"],
                        "latency_ms": pf["latency_ms"],
                    }
                )
            return result

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            self.active_node = ""
            self._slot_nodes.clear()
            self._slot_last_used.clear()
            self._account_slots.clear()
            if proc is None:
                return
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        proc.kill()
                        try:
                            proc.wait(timeout=2.0)
                        except Exception:
                            pass
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


_GLOBAL_SIDECAR: SidecarProxyManager | None = None
_GLOBAL_LOCK = threading.Lock()


def get_sidecar_proxy_manager() -> SidecarProxyManager:
    global _GLOBAL_SIDECAR
    with _GLOBAL_LOCK:
        if _GLOBAL_SIDECAR is None:
            _GLOBAL_SIDECAR = SidecarProxyManager()
        return _GLOBAL_SIDECAR


def shutdown_sidecar_proxy() -> None:
    global _GLOBAL_SIDECAR
    with _GLOBAL_LOCK:
        manager = _GLOBAL_SIDECAR
        _GLOBAL_SIDECAR = None
    if manager is not None:
        try:
            manager.stop()
        except Exception:
            pass
