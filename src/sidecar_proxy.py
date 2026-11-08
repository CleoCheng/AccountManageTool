"""KeyVault's own isolated Mihomo sidecar proxy.

This module NEVER touches the user's running Clash Verge instance. It reads the
subscription YAML strictly read-only, extracts the leaf ``proxies`` and launches
KeyVault's own ``keyvault-mihomo`` child process on a dedicated local port with
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
    classify_node_region,
    find_clash_config_files,
    get_system_proxy,
    is_valid_proxy_node_name,
    parse_clash_yaml_summary,
)


DEFAULT_SIDECAR_PROXY_PORT = 17899
DEFAULT_SIDECAR_CTRL_PORT = 29091
SIDECAR_GROUP_NAME = "KEYVAULT_PROXY"

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


def rank_nodes_for_workbuddy(nodes: list[dict]) -> list[dict]:
    """Rank clean overseas regions ahead of HK (whose shared IPs trigger Imperva).

    ``SG`` > ``JP`` > ``TW`` > ``US`` > ``KR`` > other > ``HK``. Within a bucket
    timed-out nodes go last and lower positive latency comes first.
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
        return (
            _region_priority(region),
            1 if node.get("timed_out") else 0,
            delay_val if delay_val > 0 else 10**9,
            idx,
        )

    return [node for _, node in sorted(indexed, key=sort_key)]


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
        self._loaded_yaml_sig: tuple[str, float, int] | None = None
        self._atexit_registered = False
        self._config_path = self.runtime_dir / "config.yaml"

    # ------------------------------------------------------------------
    # Read-only discovery
    # ------------------------------------------------------------------
    def extract_leaf_proxies(self) -> tuple[str, Path | None, list[dict], list[dict]]:
        """Read the first discovered subscription YAML and extract leaf proxies."""
        configs = find_clash_config_files()
        if not configs:
            return ("", None, [], [])
        client_label, yaml_path = configs[0]
        try:
            data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8", errors="ignore")) or {}
        except Exception:
            return (client_label, Path(yaml_path), [], [])
        if not isinstance(data, dict):
            return (client_label, Path(yaml_path), [], [])

        group_names_in_yaml = {
            str(g.get("name") or "").strip()
            for g in (data.get("proxy-groups") or [])
            if isinstance(g, dict) and g.get("name")
        }

        raw_proxies: list[dict] = []
        node_summaries: list[dict] = []
        seen: set[str] = set()
        for px in data.get("proxies") or []:
            if not isinstance(px, dict):
                continue
            nname = str(px.get("name") or "").strip()
            ptype = str(px.get("type") or "").strip()
            if not nname or not ptype or px.get("server") in (None, "") or px.get("port") in (None, ""):
                continue
            if not is_valid_proxy_node_name(nname, group_names_in_yaml) or nname in seen:
                continue
            seen.add(nname)
            raw_proxies.append(px)
            node_summaries.append(
                {
                    "name": nname,
                    "type": ptype,
                    "region": classify_node_region(nname),
                    "delay": None,
                }
            )
        return (client_label, Path(yaml_path), raw_proxies, node_summaries)

    # ------------------------------------------------------------------
    # Environment description (fast, never spawns the kernel)
    # ------------------------------------------------------------------
    def describe_environment(self, account_type: str = "") -> dict:
        client_label, yaml_path, raw_proxies, node_summaries = self.extract_leaf_proxies()
        mihomo_bin = find_mihomo_binary()
        sys_proxy = get_system_proxy()

        nodes = list(node_summaries)
        acc_low = (account_type or "").lower()
        if acc_low in ("workbuddy", "域名邮箱", "github", "domain-email"):
            nodes = rank_nodes_for_workbuddy(nodes)

        running = self._proc is not None and self._proc.poll() is None
        port = int(self.proxy_port)
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

        return {
            "client_name": client_name,
            "controller_online": controller_online,
            "system_proxy": sys_proxy,
            "ports": ports,
            "nodes": nodes,
            "current_groups": {SIDECAR_GROUP_NAME: self.active_node} if self.active_node else {},
            "active_node": self.active_node,
            "sidecar": bool(sidecar),
        }

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

    def _yaml_signature(self, yaml_path: Path | None) -> tuple[str, float, int] | None:
        if yaml_path is None:
            return None
        try:
            stat = yaml_path.stat()
            return (str(yaml_path), float(stat.st_mtime), int(stat.st_size))
        except OSError:
            return None

    def _build_config(self, raw_proxies: list[dict]) -> dict:
        return {
            "mixed-port": self.proxy_port,
            "bind-address": "127.0.0.1",
            "allow-lan": False,
            "mode": "rule",
            "log-level": "warning",
            "ipv6": False,
            "external-controller": f"127.0.0.1:{self.ctrl_port}",
            "secret": self._secret,
            "profile": {"store-selected": False},
            "tun": {"enable": False},
            "dns": {"enable": False},
            "proxies": raw_proxies,
            "proxy-groups": [
                {
                    "name": SIDECAR_GROUP_NAME,
                    "type": "select",
                    "proxies": [p["name"] for p in raw_proxies],
                }
            ],
            "rules": [f"MATCH,{SIDECAR_GROUP_NAME}"],
        }

    def _write_config(self, raw_proxies: list[dict]) -> Path:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        config_path = self._config_path
        config_path.write_text(
            yaml.safe_dump(self._build_config(raw_proxies), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return config_path

    def _prepare_sidecar_exe(self, mihomo_bin: Path) -> Path:
        target_name = "keyvault-mihomo.exe" if sys.platform == "win32" else "keyvault-mihomo"
        sidecar_exe = self.runtime_dir / target_name
        try:
            needs_copy = (
                not sidecar_exe.exists()
                or sidecar_exe.stat().st_size != mihomo_bin.stat().st_size
            )
            if needs_copy:
                shutil.copy2(mihomo_bin, sidecar_exe)
            return sidecar_exe
        except Exception:
            return mihomo_bin

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
    def ensure_started(self) -> dict:
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
                    if yaml_sig is not None and yaml_sig != self._loaded_yaml_sig:
                        try:
                            config_path = self._write_config(raw_proxies)
                            reload_status, _ = self._ctrl_request(
                                "PUT",
                                "/configs?force=true",
                                {"path": str(config_path)},
                                timeout=2.0,
                            )
                            if reload_status in (200, 204):
                                self._loaded_yaml_sig = yaml_sig
                        except Exception:
                            pass
                    return {
                        "ok": True,
                        "proxy_url": f"http://127.0.0.1:{self.proxy_port}",
                        "ctrl_url": f"http://127.0.0.1:{self.ctrl_port}",
                        "nodes": node_summaries,
                        "active_node": self.active_node,
                    }
                self.stop()

            self.proxy_port = _pick_free_port(
                self.preferred_proxy_port,
                range(self.preferred_proxy_port + 2, self.preferred_proxy_port + 32),
            )
            self.ctrl_port = _pick_free_port(
                self.preferred_ctrl_port,
                range(self.preferred_ctrl_port + 1, self.preferred_ctrl_port + 30),
            )

            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            sidecar_exe = self._prepare_sidecar_exe(mihomo_bin)

            try:
                if (
                    yaml_path is not None
                    and (yaml_path.parent / "Country.mmdb").is_file()
                    and not (self.runtime_dir / "Country.mmdb").exists()
                ):
                    shutil.copy2(yaml_path.parent / "Country.mmdb", self.runtime_dir / "Country.mmdb")
            except Exception:
                pass

            try:
                config_path = self._write_config(raw_proxies)
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
            return {
                "ok": True,
                "proxy_url": f"http://127.0.0.1:{self.proxy_port}",
                "ctrl_url": f"http://127.0.0.1:{self.ctrl_port}",
                "nodes": node_summaries,
                "active_node": self.active_node,
            }

    def preflight_github(self, timeout: float = 6.0) -> dict:
        proxy_url = f"http://127.0.0.1:{self.proxy_port}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
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
    ) -> dict:
        started = self.ensure_started()
        if not started.get("ok"):
            return {
                "ok": False,
                "switched": False,
                "error": started.get("error", "sidecar unavailable"),
            }

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

        # Drop previous keep-alive connections so the next request really exits
        # through the freshly selected node.
        self._ctrl_request("DELETE", "/connections", timeout=2.0)

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
