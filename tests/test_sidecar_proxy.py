import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def clash_yaml(tmp_path: Path, monkeypatch) -> Path:
    """Write a Clash Verge-style subscription YAML and point discovery at it."""
    config = tmp_path / "clash-verge.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "mixed-port": 17898,
                "secret": "verge-secret",
                "external-controller": "127.0.0.1:9097",
                "proxies": [
                    {"name": "Traffic: 0 GB / 200 GB", "type": "trojan", "server": "127.0.0.1", "port": 1},
                    {"name": "Expire: 2027-10-29", "type": "trojan", "server": "127.0.0.1", "port": 2},
                    {
                        "name": "🇭🇰 Hong Kong | 01",
                        "type": "ss",
                        "server": "127.0.0.1",
                        "port": 30001,
                        "cipher": "aes-128-gcm",
                        "password": "hk-pass",
                    },
                    {
                        "name": "🇸🇬 Singapore | 01",
                        "type": "ss",
                        "server": "127.0.0.1",
                        "port": 30002,
                        "cipher": "aes-128-gcm",
                        "password": "sg-pass",
                    },
                    {
                        "name": "🇯🇵 Japan | 01",
                        "type": "ss",
                        "server": "127.0.0.1",
                        "port": 30003,
                        "cipher": "aes-128-gcm",
                        "password": "jp-pass",
                    },
                ],
                "proxy-groups": [
                    {
                        "name": "HK",
                        "type": "url-test",
                        "proxies": ["🇭🇰 Hong Kong | 01"],
                        "url": "https://www.gstatic.com/generate_204",
                    },
                    {
                        "name": "Proxies",
                        "type": "select",
                        "proxies": ["🇭🇰 Hong Kong | 01", "🇸🇬 Singapore | 01", "🇯🇵 Japan | 01"],
                    },
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYVAULT_CLASH_CONFIG", str(config))
    return config


@pytest.fixture()
def no_verge_touch(monkeypatch):
    """Fail loudly if any sidecar code path tries to reach Clash Verge's pipe."""

    def _forbidden(*args, **kwargs):
        raise AssertionError("Clash Verge pipe must never be touched!")

    monkeypatch.setattr("src.proxy_service.find_active_mihomo_pipe", _forbidden)
    monkeypatch.setattr("src.proxy_service._call_controller", _forbidden)
    monkeypatch.setattr("src.proxy_service._discover_controller", _forbidden)
    return _forbidden


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def test_rank_nodes_for_workbuddy_prioritizes_clean_regions_over_hk():
    from src.sidecar_proxy import rank_nodes_for_workbuddy

    nodes = [
        {"name": "🇭🇰 Hong Kong | 01"},
        {"name": "🌍 Other | 01"},
        {"name": "🇰🇷 Korea | 01"},
        {"name": "🇺🇸 United States | 01"},
        {"name": "🇨🇳 台湾 | 01"},
        {"name": "🇯🇵 Japan | 01"},
        {"name": "🇸🇬 Singapore | 01"},
    ]
    ranked = [n["name"] for n in rank_nodes_for_workbuddy(nodes)]
    assert ranked == [
        "🇸🇬 Singapore | 01",
        "🇯🇵 Japan | 01",
        "🇨🇳 台湾 | 01",
        "🇺🇸 United States | 01",
        "🇰🇷 Korea | 01",
        "🌍 Other | 01",
        "🇭🇰 Hong Kong | 01",
    ]

    # timed_out nodes must sink to the bottom of their own region bucket only.
    mixed = [
        {"name": "🇸🇬 Singapore | 01", "timed_out": True, "delay": 20},
        {"name": "🇭🇰 Hong Kong | 01", "timed_out": False, "delay": 10},
        {"name": "🇯🇵 Japan | 01", "timed_out": False, "delay": 900},
        {"name": "🇸🇬 Singapore | 02", "timed_out": False, "delay": 300},
        {"name": "🇸🇬 Singapore | 03", "timed_out": False, "delay": 100},
        {"name": "🇯🇵 Japan | 02", "timed_out": True, "delay": 5},
    ]
    order = [n["name"] for n in rank_nodes_for_workbuddy(mixed)]
    assert order == [
        "🇸🇬 Singapore | 03",
        "🇸🇬 Singapore | 02",
        "🇸🇬 Singapore | 01",
        "🇯🇵 Japan | 01",
        "🇯🇵 Japan | 02",
        "🇭🇰 Hong Kong | 01",
    ]
    # A timed-out SG node still ranks ahead of a healthy HK node.
    assert order.index("🇸🇬 Singapore | 01") < order.index("🇭🇰 Hong Kong | 01")

    # Empty / malformed input must not raise.
    assert rank_nodes_for_workbuddy([]) == []
    assert rank_nodes_for_workbuddy([None, "nope"]) == []


# ---------------------------------------------------------------------------
# Read-only extraction + environment description
# ---------------------------------------------------------------------------
def test_extract_leaf_proxies_and_describe_environment_never_touches_clash_verge(
    tmp_path: Path, clash_yaml: Path, no_verge_touch
):
    from src.sidecar_proxy import DEFAULT_SIDECAR_PROXY_PORT, SidecarProxyManager

    manager = SidecarProxyManager(runtime_dir=tmp_path / "sidecar")
    label, yaml_path, raw_proxies, node_summaries = manager.extract_leaf_proxies()

    assert Path(yaml_path).resolve() == clash_yaml.resolve()
    assert "KEYVAULT_CLASH_CONFIG" in label
    leaf_names = [p["name"] for p in raw_proxies]
    assert leaf_names == ["🇭🇰 Hong Kong | 01", "🇸🇬 Singapore | 01", "🇯🇵 Japan | 01"]
    assert all("Traffic:" not in n for n in leaf_names)
    assert all("Expire:" not in n for n in leaf_names)
    assert "HK" not in leaf_names
    assert "Proxies" not in leaf_names

    env = manager.describe_environment(account_type="WorkBuddy")

    # Dedicated sidecar port, never the Verge mixed port 17898.
    ports = env["ports"]
    assert ports, "sidecar environment must expose exactly one dedicated port"
    assert len(ports) == 1
    assert DEFAULT_SIDECAR_PROXY_PORT <= ports[0]["port"] <= DEFAULT_SIDECAR_PROXY_PORT + 31
    assert ports[0]["url"] == f"http://127.0.0.1:{ports[0]['port']}"
    assert ports[0]["port"] != 17898, "must never reuse the Clash Verge mixed port"
    assert ports[0]["bound_group"] == "KEYVAULT_PROXY"
    assert env["sidecar"] is True
    assert env["controller_online"] is True
    assert env["client_name"].startswith("KeyVault 独立代理内核")

    names = [n["name"] for n in env["nodes"]]
    assert names == ["🇸🇬 Singapore | 01", "🇯🇵 Japan | 01", "🇭🇰 Hong Kong | 01"]
    assert names.index("🇸🇬 Singapore | 01") < names.index("🇭🇰 Hong Kong | 01")
    assert names.index("🇯🇵 Japan | 01") < names.index("🇭🇰 Hong Kong | 01")
    assert all("Traffic:" not in n and "Expire:" not in n for n in names)
    assert "HK" not in names and "Proxies" not in names

    # Extracting again and describing a non-WorkBuddy account must stay read-only.
    assert manager.extract_leaf_proxies()[3]
    generic = manager.describe_environment(account_type="ChatGPT")
    assert {n["name"] for n in generic["nodes"]} == set(names)
    assert generic["sidecar"] is True
    assert not manager._proc, "describe_environment must never spawn the kernel"


# ---------------------------------------------------------------------------
# GitHub preflight
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self, size: int = -1) -> bytes:
        return self._body if size is None or size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, result):
        self._result = result
        self.requests: list[urllib.request.Request] = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if isinstance(self._result, BaseException):
            raise self._result
        if isinstance(self._result, tuple):
            status, body = self._result
            if status >= 400:
                raise urllib.error.HTTPError(
                    request.full_url, status, "error", {}, None
                )
            return _FakeResponse(status, body)
        return _FakeResponse(200, self._result)


def _preflight_with(monkeypatch, tmp_path: Path, result) -> dict:
    from src.sidecar_proxy import SidecarProxyManager

    opener = _FakeOpener(result)
    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *handlers: opener
    )
    manager = SidecarProxyManager(runtime_dir=tmp_path / "preflight")
    outcome = manager.preflight_github(timeout=0.5)
    assert opener.requests, "preflight must actually issue a request"
    assert opener.requests[0].full_url == "https://github.com/"
    return outcome


def test_preflight_github_detects_imperva_restriction_and_clean_200(monkeypatch, tmp_path: Path):
    clean = _preflight_with(
        monkeypatch,
        tmp_path,
        (200, "<!DOCTYPE html><html><head><title>GitHub</title></head><body>Sign in</body></html>"),
    )
    assert clean["ok"] is True
    assert clean["reachable"] is True
    assert clean["restricted"] is False
    assert clean["status"] == 200

    imperva = _preflight_with(
        monkeypatch,
        tmp_path,
        (
            200,
            '<html><body><h1>访问暂时受限</h1>'
            "<p>为什么需要经过「我不是机器人」的验证？</p></body></html>",
        ),
    )
    assert imperva["ok"] is False
    assert imperva["reachable"] is True
    assert imperva["restricted"] is True

    forbidden = _preflight_with(monkeypatch, tmp_path, (403, "<html>Forbidden</html>"))
    assert forbidden["ok"] is False
    assert forbidden["reachable"] is True
    assert forbidden["restricted"] is True
    assert forbidden["status"] == 403

    timeout = _preflight_with(monkeypatch, tmp_path, TimeoutError("timed out"))
    assert timeout["ok"] is False
    assert timeout["reachable"] is False
    assert timeout["restricted"] is False
    assert timeout["status"] == 0

    unreachable = _preflight_with(
        monkeypatch, tmp_path, urllib.error.URLError("connection refused")
    )
    assert unreachable["ok"] is False
    assert unreachable["reachable"] is False
    assert unreachable["restricted"] is False


# ---------------------------------------------------------------------------
# Live sidecar integration (skipped when no Mihomo kernel is installed)
# ---------------------------------------------------------------------------
def _port_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", int(port)))
        except OSError:
            return False
    return True


def _preferred_port(preferred: int) -> int:
    """Use the documented sidecar port when free, otherwise the next free one."""
    if _port_free(preferred):
        return preferred
    import socket

    for port in range(preferred + 1, preferred + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("no free port found")


def _wait_process_gone(proc: subprocess.Popen, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return proc.poll() is not None


def test_live_sidecar_start_switch_and_clean_stop(tmp_path: Path, monkeypatch):
    from src.sidecar_proxy import SidecarProxyManager, find_mihomo_binary

    mihomo_bin = find_mihomo_binary()
    if not mihomo_bin:
        pytest.skip("No Mihomo kernel installed on this machine")

    config = tmp_path / "live-clash.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "proxies": [
                    {
                        "name": "🇸🇬 Singapore | 01",
                        "type": "ss",
                        "server": "127.0.0.1",
                        "port": 30011,
                        "cipher": "aes-128-gcm",
                        "password": "sg-pass",
                    },
                    {
                        "name": "🇯🇵 Japan | 01",
                        "type": "ss",
                        "server": "127.0.0.1",
                        "port": 30012,
                        "cipher": "aes-128-gcm",
                        "password": "jp-pass",
                    },
                ],
                "proxy-groups": [
                    {
                        "name": "Proxies",
                        "type": "select",
                        "proxies": ["🇸🇬 Singapore | 01", "🇯🇵 Japan | 01"],
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYVAULT_CLASH_CONFIG", str(config))
    # Never let the sidecar fall back onto the user's Clash Verge controller.
    monkeypatch.setattr(
        "src.proxy_service.find_active_mihomo_pipe",
        lambda: (_ for _ in ()).throw(AssertionError("Clash Verge pipe must never be touched!")),
    )

    proxy_port = _preferred_port(17941)
    ctrl_port = _preferred_port(29141)
    manager = SidecarProxyManager(
        runtime_dir=tmp_path / "rt", proxy_port=proxy_port, ctrl_port=ctrl_port
    )
    proc = None
    try:
        started = manager.ensure_started()
        assert started["ok"] is True, started
        assert started["proxy_url"] == f"http://127.0.0.1:{manager.proxy_port}"
        assert started["ctrl_url"] == f"http://127.0.0.1:{manager.ctrl_port}"
        assert {n["name"] for n in started["nodes"]} == {
            "🇸🇬 Singapore | 01",
            "🇯🇵 Japan | 01",
        }
        proc = manager._proc
        assert proc is not None and proc.poll() is None

        calls: list[tuple[str, str, int]] = []
        original_request = manager._ctrl_request

        def recording_request(method, path, body=None, **kwargs):
            status, payload = original_request(method, path, body, **kwargs)
            calls.append((method, path, status))
            return status, payload

        manager._ctrl_request = recording_request
        switched = manager.switch_node("🇯🇵 Japan | 01", preflight=False)
        assert switched["ok"] is True, switched
        assert switched["switched"] is True
        assert switched["groups"] == ["KEYVAULT_PROXY"]
        assert switched["node"] == "🇯🇵 Japan | 01"
        assert switched["proxy_url"] == f"http://127.0.0.1:{manager.proxy_port}"
        assert ("PUT", "/proxies/KEYVAULT_PROXY", 204) in calls
        assert ("DELETE", "/connections", 204) in calls
        assert ("GET", "/proxies", 200) in calls

        # The sidecar's own controller must report the freshly selected leaf.
        status, proxies = original_request("GET", "/proxies", timeout=2.0)
        assert status == 200
        assert proxies["proxies"]["KEYVAULT_PROXY"]["now"] == "🇯🇵 Japan | 01"

        invalid = manager.switch_node("🇺🇸 Not In Config | 99", preflight=False)
        assert invalid["ok"] is False
        assert invalid["switched"] is False
        assert "Not In Config" in invalid["error"]
        status, proxies = original_request("GET", "/proxies", timeout=2.0)
        assert status == 200
        assert proxies["proxies"]["KEYVAULT_PROXY"]["now"] == "🇯🇵 Japan | 01"
    finally:
        manager.stop()
        assert manager._proc is None
        if proc is not None:
            assert _wait_process_gone(proc), "keyvault-mihomo process leaked after stop()"
            assert proc.poll() is not None


def test_stop_is_idempotent_and_never_leaks_a_process(tmp_path: Path, monkeypatch):
    from src.sidecar_proxy import SidecarProxyManager

    manager = SidecarProxyManager(runtime_dir=tmp_path / "rt2")
    manager.stop()  # never started -> no-op
    manager.stop()

    fake = mock.Mock()
    fake.poll.return_value = None
    manager._proc = fake
    manager.stop()
    fake.terminate.assert_called_once()
    fake.wait.assert_called_once()
    assert manager._proc is None
