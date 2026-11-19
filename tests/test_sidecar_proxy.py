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


def test_rank_nodes_for_workbuddy_prefers_iepl_dedicated_lines_in_same_region():
    from src.sidecar_proxy import rank_nodes_for_workbuddy

    nodes = [
        {"name": "🇸🇬 Singapore | 01", "delay": 20},
        {"name": "🇸🇬 新加坡实验性 IEPL 专线 1", "delay": 300},
        {"name": "🇸🇬 Singapore | 02", "delay": 10},
        {"name": "🇸🇬 新加坡 IPLC 专线 2", "delay": 400},
    ]
    ranked = [n["name"] for n in rank_nodes_for_workbuddy(nodes)]
    # IEPL / 专线 / IPLC nodes lead their region bucket even with higher latency.
    assert ranked[:2] == [
        "🇸🇬 新加坡实验性 IEPL 专线 1",
        "🇸🇬 新加坡 IPLC 专线 2",
    ]
    assert ranked.index("🇸🇬 新加坡实验性 IEPL 专线 1") < ranked.index("🇸🇬 Singapore | 02")
    assert ranked.index("🇸🇬 Singapore | 02") < ranked.index("🇸🇬 Singapore | 01")


def test_rank_nodes_for_workbuddy_prefers_residential_nodes():
    from src.sidecar_proxy import rank_nodes_for_workbuddy

    nodes = [
        {"name": "🇺🇸 Datacenter | 01", "delay": 10},
        {"name": "USbyJPTT | MiyaIP-2 HY2", "delay": 90},
        {"name": "US | MiyaIP-2 HY2", "delay": 120},
        {"name": "🇸🇬 Singapore | 01", "delay": 20},
        {"name": "🇯🇵 Japan | 01", "delay": 30},
    ]
    ranked = [n["name"] for n in rank_nodes_for_workbuddy(nodes)]
    # Residential tier is the primary key: direct residential beats relay
    # residential, and both lead every generic airport node — even SG/JP — so
    # WorkBuddy signups never land on a shared airport blacklist IP.
    assert ranked[:2] == ["US | MiyaIP-2 HY2", "USbyJPTT | MiyaIP-2 HY2"]
    for residential in ("US | MiyaIP-2 HY2", "USbyJPTT | MiyaIP-2 HY2"):
        assert ranked.index(residential) < ranked.index("🇸🇬 Singapore | 01")
        assert ranked.index(residential) < ranked.index("🇯🇵 Japan | 01")
        assert ranked.index(residential) < ranked.index("🇺🇸 Datacenter | 01")
    # Inside the US bucket direct residential beats relay residential, and both
    # beat a lower-latency datacenter node.
    us = [name for name in ranked if name in (
        "US | MiyaIP-2 HY2",
        "USbyJPTT | MiyaIP-2 HY2",
        "🇺🇸 Datacenter | 01",
    )]
    assert us == [
        "US | MiyaIP-2 HY2",
        "USbyJPTT | MiyaIP-2 HY2",
        "🇺🇸 Datacenter | 01",
    ]

    # A dead residential line is useless: it sinks below the healthy node.
    dead = [
        {"name": "US | MiyaIP-Dead", "timed_out": True},
        {"name": "🇺🇸 Datacenter | 01", "delay": 200},
    ]
    dead_ranked = [n["name"] for n in rank_nodes_for_workbuddy(dead)]
    assert dead_ranked == ["🇺🇸 Datacenter | 01", "US | MiyaIP-Dead"]

    # HK residential markers must never promote a blocked HK exit.
    hk = [
        {"name": "🇭🇰 Hong Kong | 01", "delay": 10},
        {"name": "🇺🇸 Datacenter | 01", "delay": 300},
    ]
    hk_ranked = [n["name"] for n in rank_nodes_for_workbuddy(hk)]
    assert hk_ranked == ["🇺🇸 Datacenter | 01", "🇭🇰 Hong Kong | 01"]

    # Standard / IEPL ordering for non-residential nodes is untouched.
    standard = [
        {"name": "🇸🇬 Singapore | 01", "delay": 20},
        {"name": "🇸🇬 新加坡 IEPL 专线 1", "delay": 300},
        {"name": "🇸🇬 Singapore | 02", "delay": 10},
    ]
    assert [n["name"] for n in rank_nodes_for_workbuddy(standard)] == [
        "🇸🇬 新加坡 IEPL 专线 1",
        "🇸🇬 Singapore | 02",
        "🇸🇬 Singapore | 01",
    ]


# ---------------------------------------------------------------------------
# Read-only extraction + environment description
# ---------------------------------------------------------------------------
def test_build_config_bypasses_domestic_mail_and_keeps_sidecar_group(tmp_path: Path):
    from src.sidecar_proxy import SIDECAR_GROUP_NAME, SidecarProxyManager

    manager = SidecarProxyManager(runtime_dir=tmp_path / "sidecar")
    config = manager._build_config(
        [
            {"name": "🇸🇬 Singapore | 01", "type": "ss", "server": "127.0.0.1", "port": 30001},
            {"name": "🇯🇵 Japan | 01", "type": "ss", "server": "127.0.0.1", "port": 30002},
        ]
    )

    rules = config["rules"]
    assert "DOMAIN-SUFFIX,ggb6.com,DIRECT" in rules
    # ``no-resolve`` keeps Mihomo from using the local Windows DNS resolver to
    # classify proxy-bound domains via GeoIP.
    assert "GEOIP,CN,DIRECT,no-resolve" in rules
    assert "GEOIP,CN,DIRECT" not in rules
    assert config["find-process-mode"] == "off"
    assert f"MATCH,{SIDECAR_GROUP_NAME}" in rules
    assert rules[-1] == "MATCH,KEYVAULT_PROXY"
    # The mail bypass must be evaluated before traffic falls through to the proxy.
    assert rules.index("DOMAIN-SUFFIX,ggb6.com,DIRECT") < rules.index("MATCH,KEYVAULT_PROXY")
    assert config["proxy-groups"][0]["name"] == SIDECAR_GROUP_NAME
    assert config["proxy-groups"][0]["proxies"] == ["🇸🇬 Singapore | 01", "🇯🇵 Japan | 01"]

    # STUN/TURN must be blocked so WebRTC cannot leak the real local IP.
    assert "DOMAIN-KEYWORD,stun,REJECT" in rules
    assert "DST-PORT,3478,REJECT" in rules
    assert "DST-PORT,5349,REJECT" in rules
    # Anthropic traffic must always exit through the sidecar selector.
    for domain in ("claude.ai", "anthropic.com", "claudeusercontent.com"):
        rule = f"DOMAIN-SUFFIX,{domain},{SIDECAR_GROUP_NAME}"
        assert rule in rules
        assert rules.index(rule) < rules.index("GEOIP,CN,DIRECT,no-resolve")
    assert rules.index("DOMAIN-SUFFIX,ggb6.com,DIRECT") < rules.index(
        "GEOIP,CN,DIRECT,no-resolve"
    )
    assert rules.index("GEOIP,CN,DIRECT,no-resolve") < rules.index("MATCH,KEYVAULT_PROXY")
    assert rules.index("DOMAIN-KEYWORD,stun,REJECT") < rules.index("MATCH,KEYVAULT_PROXY")


def test_rank_nodes_for_claude_prefers_us_residential_then_us_then_clean_regions():
    from src.sidecar_proxy import rank_nodes_for_claude

    nodes = [
        {"name": "🇭🇰 Hong Kong | 01"},
        {"name": "🇯🇵 Japan | 01"},
        {"name": "🇸🇬 Singapore | 01"},
        {"name": "USbyJPTT | MiyaIP-2 HY2"},
        {"name": "USbyJP-54 | MiyaIP-2 HY2"},
        {"name": "US | MiyaIP-2 HY2"},
        {"name": "🇺🇸 United States | 01"},
        {"name": "🇬🇧 United Kingdom | 01"},
        {"name": "🌍 Other | 01"},
    ]
    ranked = [n["name"] for n in rank_nodes_for_claude(nodes)]
    assert ranked[0] == "US | MiyaIP-2 HY2"
    assert ranked[1:3] == ["USbyJPTT | MiyaIP-2 HY2", "USbyJP-54 | MiyaIP-2 HY2"]
    assert ranked[3] == "🇺🇸 United States | 01"
    # Clean non-US regions beat HK.
    assert ranked.index("🇸🇬 Singapore | 01") < ranked.index("🇭🇰 Hong Kong | 01")
    assert ranked.index("🇯🇵 Japan | 01") < ranked.index("🇭🇰 Hong Kong | 01")
    assert ranked[-1] == "🇭🇰 Hong Kong | 01"
    # Every US node outranks SG/JP.
    for us_name in ("US | MiyaIP-2 HY2", "🇺🇸 United States | 01"):
        assert ranked.index(us_name) < ranked.index("🇸🇬 Singapore | 01")
        assert ranked.index(us_name) < ranked.index("🇯🇵 Japan | 01")

    # Caller inputs are not mutated.
    assert "region" not in nodes[0]
    assert all(isinstance(n, dict) for n in nodes)

    # Empty / malformed input must not raise.
    assert rank_nodes_for_claude([]) == []
    assert rank_nodes_for_claude([None, "nope"]) == []


def test_rank_nodes_for_claude_sinks_timed_out_and_honours_latency():
    from src.sidecar_proxy import rank_nodes_for_claude

    nodes = [
        {"name": "US | MiyaIP-Slow", "delay": 400},
        {"name": "US | MiyaIP-Fast", "delay": 80},
        {"name": "US | MiyaIP-Dead", "timed_out": True},
        {"name": "🇺🇸 Datacenter | 01", "delay": 20},
    ]
    ranked = [n["name"] for n in rank_nodes_for_claude(nodes)]
    assert ranked[:2] == ["US | MiyaIP-Fast", "US | MiyaIP-Slow"]
    assert ranked[2] == "🇺🇸 Datacenter | 01"
    assert ranked[-1] == "US | MiyaIP-Dead"


def test_miyaip2_tiers_rank_workbuddy_and_claude_identically():
    from src.sidecar_proxy import (
        _residential_tier,
        rank_nodes_for_claude,
        rank_nodes_for_workbuddy,
    )

    expected_tiers = {
        "US | MiyaIP-2 HY2": 0,
        "US | MiyaIP-2": 1,
        "USbyJPTT | MiyaIP-2 HY2": 1,
        "USbyJP-54 | MiyaIP-2 HY2": 1,
        "US | MiyaIP": 2,
        "US | MiyaIP HY2": 2,
        "USbyJPTT | MiyaIP": 3,
        "🇸🇬 Singapore | 01": 4,
    }
    for name, expected in expected_tiers.items():
        assert _residential_tier(name) == expected, name

    nodes = [
        {"name": "US | MiyaIP"},
        {"name": "US | MiyaIP HY2"},
        {"name": "US | MiyaIP-2"},
        {"name": "USbyJPTT | MiyaIP-2 HY2"},
        {"name": "USbyJP-54 | MiyaIP-2 HY2"},
        {"name": "US | MiyaIP-2 HY2"},
    ]
    expected_order = [
        "US | MiyaIP-2 HY2",
        "US | MiyaIP-2",
        "USbyJPTT | MiyaIP-2 HY2",
        "USbyJP-54 | MiyaIP-2 HY2",
        "US | MiyaIP",
        "US | MiyaIP HY2",
    ]
    for ranker in (rank_nodes_for_workbuddy, rank_nodes_for_claude):
        ranked = [n["name"] for n in ranker(nodes)]
        # The MiyaIP-2 HY2 direct line is always the first choice.
        assert ranked[0] == "US | MiyaIP-2 HY2"
        assert ranked == expected_order
        # Every MiyaIP-2 line outranks the legacy US | MiyaIP lines.
        for miyaip2 in (
            "US | MiyaIP-2 HY2",
            "US | MiyaIP-2",
            "USbyJPTT | MiyaIP-2 HY2",
            "USbyJP-54 | MiyaIP-2 HY2",
        ):
            assert ranked.index(miyaip2) < ranked.index("US | MiyaIP")
            assert ranked.index(miyaip2) < ranked.index("US | MiyaIP HY2")

    # Ranking never mutates the caller's node dicts.
    assert all("region" not in n for n in nodes)


def test_describe_environment_for_claude_ranks_us_first_and_exposes_local_ai_us_port(
    tmp_path: Path, monkeypatch
):
    from src.sidecar_proxy import SidecarProxyManager

    config = tmp_path / "clash-verge.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "mixed-port": 17898,
                "listeners": [
                    {
                        "name": "AI-US",
                        "type": "mixed",
                        "listen": "127.0.0.1",
                        "port": 7897,
                        "proxy": "US",
                    }
                ],
                "proxies": [
                    {"name": "🇭🇰 Hong Kong | 01", "type": "ss", "server": "127.0.0.1", "port": 30001},
                    {"name": "🇯🇵 Japan | 01", "type": "ss", "server": "127.0.0.1", "port": 30002},
                    {"name": "USbyJPTT | MiyaIP-2 HY2", "type": "ss", "server": "127.0.0.1", "port": 30003},
                    {"name": "US | MiyaIP-2 HY2", "type": "ss", "server": "127.0.0.1", "port": 30004},
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYVAULT_CLASH_CONFIG", str(config))
    monkeypatch.setattr(
        "src.sidecar_proxy.find_mihomo_binary", lambda: Path("C:/fake/mihomo.exe")
    )
    monkeypatch.setattr(
        "src.sidecar_proxy._is_local_port_open", lambda port, timeout=0.08: int(port) == 7897
    )

    manager = SidecarProxyManager(runtime_dir=tmp_path / "sidecar")
    env = manager.describe_environment("Claude")

    names = [n["name"] for n in env["nodes"]]
    assert names[0] == "US | MiyaIP-2 HY2"
    assert names[1] == "USbyJPTT | MiyaIP-2 HY2"
    assert names.index("🇯🇵 Japan | 01") < names.index("🇭🇰 Hong Kong | 01")
    assert env["recommended_node"] == "US | MiyaIP-2 HY2"

    # The sidecar port stays first; the live AI-US listener follows it.
    ports = env["ports"]
    assert len(ports) == 2
    assert ports[0]["bound_group"] == "KEYVAULT_PROXY"
    assert ports[1]["port"] == 7897
    assert "AI-US" in ports[1]["label"]

    # A dead listener is never exposed.
    monkeypatch.setattr(
        "src.sidecar_proxy._is_local_port_open", lambda port, timeout=0.08: False
    )
    env_dead = manager.describe_environment("Claude")
    assert len(env_dead["ports"]) == 1

    # WorkBuddy / default behaviour is untouched (single dedicated port).
    env_wb = manager.describe_environment("WorkBuddy")
    assert len(env_wb["ports"]) == 1
    assert "recommended_node" not in env_wb
    env_default = manager.describe_environment()
    assert len(env_default["ports"]) == 1
    assert "recommended_node" not in env_default


def test_extract_leaf_proxies_resolves_hosts_decoys_and_config_keeps_hosts(
    tmp_path: Path, monkeypatch
):
    from src.sidecar_proxy import SidecarProxyManager

    monkeypatch.delenv("KEYVAULT_CLASH_CONFIG", raising=False)

    verge_dir = tmp_path / "verge"
    profiles_dir = verge_dir / "profiles"
    profiles_dir.mkdir(parents=True)

    primary = verge_dir / "clash-verge.yaml"
    primary.write_text(
        yaml.safe_dump(
            {
                "hosts": {
                    "63182sh8-4d7b.aws-agent.net": "37f36sh0-2fd9.apt-agent.net",
                    "oss-cn-shanghai.toshiba-asdf.com": "gpt-ai-shanghai.micron-asdf.com",
                    "gpt-ai-shanghai.micron-asdf.com": ["10.0.0.9"],
                    "list-valued.example.net": ["", "1.2.3.4"],
                    "empty-value.example.net": "",
                },
                "proxies": [
                    {
                        "name": "🇸🇬 Singapore | Decoy",
                        "type": "ss",
                        "server": "63182sh8-4d7b.aws-agent.net",
                        "port": 30001,
                        "cipher": "aes-128-gcm",
                        "password": "sg-pass",
                    },
                    {
                        "name": "🇯🇵 Japan | Chained",
                        "type": "ss",
                        "server": "oss-cn-shanghai.toshiba-asdf.com",
                        "port": 30002,
                        "cipher": "aes-128-gcm",
                        "password": "jp-pass",
                    },
                    {
                        "name": "🇭🇰 Hong Kong | Plain",
                        "type": "ss",
                        "server": "hk.example.com",
                        "port": 30003,
                        "cipher": "aes-128-gcm",
                        "password": "hk-pass",
                    },
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # The remote profile declares its own decoy host and a proxy using it.
    (profiles_dir / "remote-profile.yaml").write_text(
        yaml.safe_dump(
            {
                "hosts": {"per-file-decoy.aws-agent.net": "jp.example.com"},
                "proxies": [
                    {
                        "name": "🇺🇸 United States | PerFile",
                        "type": "ss",
                        "server": "per-file-decoy.aws-agent.net",
                        "port": 30004,
                        "cipher": "aes-128-gcm",
                        "password": "us-pass",
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (verge_dir / "profiles.yaml").write_text(
        yaml.safe_dump(
            {
                "current": "remote-profile",
                "items": [
                    {
                        "uid": "remote-profile",
                        "type": "remote",
                        "name": "Remote Sub",
                        "file": "remote-profile.yaml",
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "src.sidecar_proxy.find_clash_config_files",
        lambda: [("Clash Verge Rev", primary)],
    )

    manager = SidecarProxyManager(runtime_dir=tmp_path / "sidecar")
    _, _, raw_proxies, node_summaries = manager.extract_leaf_proxies()

    servers = {p["name"]: p["server"] for p in raw_proxies}
    assert servers["🇸🇬 Singapore | Decoy"] == "37f36sh0-2fd9.apt-agent.net"
    # Chained hosts entries are followed until the final address.
    assert servers["🇯🇵 Japan | Chained"] == "10.0.0.9"
    # Non-decoy servers and per-file hosts maps pass through untouched.
    assert servers["🇭🇰 Hong Kong | Plain"] == "hk.example.com"
    assert servers["🇺🇸 United States | PerFile"] == "jp.example.com"
    summary_servers = {n["name"]: n["server"] for n in node_summaries}
    assert summary_servers == servers

    # The source YAML must stay untouched (read-only extraction).
    original = yaml.safe_load(primary.read_text(encoding="utf-8"))
    assert original["proxies"][0]["server"] == "63182sh8-4d7b.aws-agent.net"

    assert manager._last_hosts_map["63182sh8-4d7b.aws-agent.net"] == (
        "37f36sh0-2fd9.apt-agent.net"
    )
    assert manager._last_hosts_map["list-valued.example.net"] == "1.2.3.4"
    assert "empty-value.example.net" not in manager._last_hosts_map

    config = manager._build_config(raw_proxies)
    assert config["dns"] == {"enable": False, "use-hosts": True}
    assert config["hosts"]["63182sh8-4d7b.aws-agent.net"] == "37f36sh0-2fd9.apt-agent.net"
    assert config["hosts"]["oss-cn-shanghai.toshiba-asdf.com"] == (
        "gpt-ai-shanghai.micron-asdf.com"
    )
    assert config["hosts"]["per-file-decoy.aws-agent.net"] == "jp.example.com"
    # An explicit empty map must not leak the cached hosts section.
    assert "hosts" not in manager._build_config(raw_proxies, {})


def test_prepare_sidecar_exe_uses_proxybridge_direct_process_name(tmp_path: Path, monkeypatch):
    import src.sidecar_proxy as sidecar_module
    from src.sidecar_proxy import SidecarProxyManager

    manager = SidecarProxyManager(runtime_dir=tmp_path / "sidecar")
    manager.runtime_dir.mkdir(parents=True)
    mihomo_bin = tmp_path / "verge-mihomo.exe"
    mihomo_bin.write_bytes(b"kernel-binary")

    monkeypatch.setattr(sidecar_module.sys, "platform", "win32")
    staged = manager._prepare_sidecar_exe(mihomo_bin)
    # ProxyBridge's 代理核心直连 rule matches mihomo.exe, so the kernel's own
    # traffic goes DIRECT instead of double-proxying through 17898.
    assert staged == manager.runtime_dir / "mihomo.exe"
    assert staged.read_bytes() == b"kernel-binary"

    # A copy failure still returns an already staged mihomo.exe.
    with monkeypatch.context() as patched:
        def _boom(*args, **kwargs):
            raise PermissionError("kernel is running")

        patched.setattr(sidecar_module.shutil, "copy2", _boom)
        assert manager._prepare_sidecar_exe(mihomo_bin) == manager.runtime_dir / "mihomo.exe"

        # Without a staged mihomo.exe the legacy name is used, then the source.
        staged.unlink()
        fallback = manager.runtime_dir / "keyvault-mihomo.exe"
        fallback.write_bytes(b"kernel-binary")
        assert manager._prepare_sidecar_exe(mihomo_bin) == fallback
        fallback.unlink()
        assert manager._prepare_sidecar_exe(mihomo_bin) == mihomo_bin

    # Non-Windows keeps the plain ``mihomo`` filename.
    monkeypatch.setattr(sidecar_module.sys, "platform", "linux")
    linux_staged = manager._prepare_sidecar_exe(mihomo_bin)
    assert linux_staged == manager.runtime_dir / "mihomo"


def test_apply_active_or_default_node_restores_or_ranks_selection(tmp_path: Path):
    from src.sidecar_proxy import SIDECAR_GROUP_NAME, SidecarProxyManager

    nodes = [
        {"name": "🇭🇰 Hong Kong | 01", "region": "🇭🇰 香港 (HK)"},
        {"name": "🇸🇬 Singapore | 01", "region": "🇸🇬 新加坡 (SG)"},
    ]
    manager = SidecarProxyManager(runtime_dir=tmp_path / "sidecar")
    calls: list[tuple[str, str, dict | None]] = []
    ack_status = 204

    def fake_request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return ack_status, {}

    manager._ctrl_request = fake_request

    # No selection yet: the WorkBuddy ranking picks SG over HK.
    manager._apply_active_or_default_node(nodes)
    assert calls[-1] == ("PUT", f"/proxies/{SIDECAR_GROUP_NAME}", {"name": "🇸🇬 Singapore | 01"})
    assert manager.active_node == "🇸🇬 Singapore | 01"

    # An existing active node wins over the ranking.
    calls.clear()
    manager.active_node = "🇭🇰 Hong Kong | 01"
    manager._apply_active_or_default_node(nodes)
    assert calls == [("PUT", f"/proxies/{SIDECAR_GROUP_NAME}", {"name": "🇭🇰 Hong Kong | 01"})]
    assert manager.active_node == "🇭🇰 Hong Kong | 01"

    # A node that vanished from the subscription falls back to the ranking.
    calls.clear()
    manager.active_node = "🇯🇵 Japan | 99"
    manager._apply_active_or_default_node(nodes)
    assert manager.active_node == "🇸🇬 Singapore | 01"

    # A rejected selection must not be recorded as active.
    calls.clear()
    manager.active_node = ""
    ack_status = 400
    manager._apply_active_or_default_node(nodes)
    assert calls
    assert manager.active_node == ""

    # Empty node lists are a no-op.
    calls.clear()
    manager._apply_active_or_default_node([])
    assert calls == []


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


def test_extract_leaf_proxies_reads_remote_profiles_when_no_env_override(
    tmp_path: Path, monkeypatch
):
    from src.sidecar_proxy import SidecarProxyManager

    monkeypatch.delenv("KEYVAULT_CLASH_CONFIG", raising=False)

    verge_dir = tmp_path / "verge"
    profiles_dir = verge_dir / "profiles"
    provider_dir = verge_dir / "proxy_provider"
    profiles_dir.mkdir(parents=True)
    provider_dir.mkdir(parents=True)

    primary = verge_dir / "clash-verge.yaml"
    primary.write_text(
        yaml.safe_dump(
            {
                "proxies": [
                    {
                        "name": "🇭🇰 Hong Kong | 01",
                        "type": "ss",
                        "server": "127.0.0.1",
                        "port": 30001,
                        "cipher": "aes-128-gcm",
                        "password": "hk-pass",
                    },
                    {
                        "name": "🇯🇵 Japan | 01",
                        "type": "ss",
                        "server": "1.1.1.1",
                        "port": 30002,
                        "cipher": "aes-128-gcm",
                        "password": "jp-pass",
                    },
                ],
                "proxy-groups": [
                    {"name": "Proxies", "type": "select", "proxies": ["🇭🇰 Hong Kong | 01"]}
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    (profiles_dir / "remote-profile.yaml").write_text(
        yaml.safe_dump(
            {
                "proxies": [
                    {
                        "name": "🇸🇬 Singapore | 01",
                        "type": "ss",
                        "server": "127.0.0.1",
                        "port": 30003,
                        "cipher": "aes-128-gcm",
                        "password": "sg-pass",
                    },
                    {
                        "name": "🇭🇰 Hong Kong | 01",
                        "type": "ss",
                        "server": "127.0.0.1",
                        "port": 30001,
                        "cipher": "aes-128-gcm",
                        "password": "hk-pass",
                    },
                ],
                "proxy-groups": [
                    {"name": "RemoteGroup", "type": "select", "proxies": ["🇸🇬 Singapore | 01"]}
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    (provider_dir / "provider-a.yaml").write_text(
        yaml.safe_dump(
            {
                "proxies": [
                    {
                        "name": "🇯🇵 Japan | 02",
                        "type": "ss",
                        "server": "2.2.2.2",
                        "port": 30004,
                        "cipher": "aes-128-gcm",
                        "password": "jp-pass",
                    },
                    {
                        "name": "🇯🇵 Japan | 03",
                        "type": "ss",
                        "server": "127.0.0.1",
                        "port": 30005,
                        "cipher": "aes-128-gcm",
                        "password": "jp-pass",
                    },
                ]
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    (verge_dir / "profiles.yaml").write_text(
        yaml.safe_dump(
            {
                "current": "remote-profile",
                "items": [
                    {
                        "uid": "remote-profile",
                        "type": "remote",
                        "name": "Remote Sub",
                        "file": "remote-profile.yaml",
                        "url": "https://example.invalid/sub",
                    },
                    {"uid": "local-profile", "type": "local", "name": "Local", "file": "local.yaml"},
                    {"uid": "missing", "type": "remote", "name": "Missing", "file": "missing.yaml"},
                    "not-a-dict",
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "src.sidecar_proxy.find_clash_config_files",
        lambda: [("Clash Verge Rev", primary)],
    )

    manager = SidecarProxyManager(runtime_dir=tmp_path / "sidecar")
    label, yaml_path, raw_proxies, node_summaries = manager.extract_leaf_proxies()

    assert label == "Clash Verge Rev"
    assert Path(yaml_path).resolve() == primary.resolve()
    leaf_names = [p["name"] for p in raw_proxies]
    # Primary file first, then remote profile, then proxy_provider YAMLs; deduped.
    assert leaf_names == [
        "🇭🇰 Hong Kong | 01",
        "🇸🇬 Singapore | 01",
        "🇯🇵 Japan | 03",
    ]
    assert len(leaf_names) == len(set(leaf_names))
    # Invalid placeholder servers from every source are filtered out.
    assert all(p["server"] not in ("1.1.1.1", "2.2.2.2") for p in raw_proxies)
    assert [n["name"] for n in node_summaries] == leaf_names
    assert manager._proc is None

    # profiles.yaml changes must invalidate the cached signature and trigger a reload.
    base_sig = manager._yaml_signature(primary)
    assert base_sig is not None
    (verge_dir / "profiles.yaml").write_text(
        (verge_dir / "profiles.yaml").read_text(encoding="utf-8") + "\n# changed\n",
        encoding="utf-8",
    )
    changed_sig = manager._yaml_signature(primary)
    assert changed_sig is not None
    assert changed_sig != base_sig


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
        assert ("GET", "/connections", 200) in calls
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


def test_apply_active_or_default_node_prefers_explicit_node_and_claude_ranking(tmp_path: Path):
    from src.sidecar_proxy import SIDECAR_GROUP_NAME, SidecarProxyManager

    nodes = [
        {"name": "🇭🇰 Hong Kong | 01", "region": "🇭🇰 香港 (HK)"},
        {"name": "🇸🇬 Singapore | 01", "region": "🇸🇬 新加坡 (SG)"},
        {"name": "US | MiyaIP-2 HY2", "region": "🇺🇸 美国 (US)"},
    ]
    manager = SidecarProxyManager(runtime_dir=tmp_path / "sidecar")
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return 204, {}

    manager._ctrl_request = fake_request

    # An explicit preferred node wins over the Claude ranking.
    manager._apply_active_or_default_node(
        nodes, preferred_node="🇸🇬 Singapore | 01", account_type="Claude"
    )
    assert calls[-1] == ("PUT", f"/proxies/{SIDECAR_GROUP_NAME}", {"name": "🇸🇬 Singapore | 01"})
    assert manager.active_node == "🇸🇬 Singapore | 01"

    # Without a preference, Claude ranking picks the US residential node.
    calls.clear()
    manager.active_node = ""
    manager._apply_active_or_default_node(nodes, account_type="Claude")
    assert manager.active_node == "US | MiyaIP-2 HY2"

    # A vanished preferred node falls back to the ranking instead of erroring.
    calls.clear()
    manager.active_node = ""
    manager._apply_active_or_default_node(
        nodes, preferred_node="🇺🇸 Missing | 99", account_type="Claude"
    )
    assert manager.active_node == "US | MiyaIP-2 HY2"

    # WorkBuddy ranking prefers the residential node when one is available.
    calls.clear()
    manager.active_node = ""
    manager._apply_active_or_default_node(nodes, account_type="WorkBuddy")
    assert manager.active_node == "US | MiyaIP-2 HY2"

    # Without residential lines, WorkBuddy keeps the legacy region ranking.
    airport_nodes = [
        {"name": "🇭🇰 Hong Kong | 01", "region": "🇭🇰 香港 (HK)"},
        {"name": "🇸🇬 Singapore | 01", "region": "🇸🇬 新加坡 (SG)"},
        {"name": "🇺🇸 United States | 01", "region": "🇺🇸 美国 (US)"},
    ]
    calls.clear()
    manager.active_node = ""
    manager._apply_active_or_default_node(airport_nodes, account_type="WorkBuddy")
    assert manager.active_node == "🇸🇬 Singapore | 01"


def test_ensure_started_accepts_preferred_node_without_breaking_no_arg_calls(tmp_path: Path):
    import inspect

    from src.sidecar_proxy import SidecarProxyManager

    signature = inspect.signature(SidecarProxyManager.ensure_started)
    assert signature.parameters["preferred_node"].default == ""
    assert signature.parameters["account_type"].default == ""
    # Keyword-only so existing positional callers keep working.
    assert all(
        p.kind == inspect.Parameter.KEYWORD_ONLY
        for name, p in signature.parameters.items()
        if name != "self"
    )


def test_sidecar_isolated_slots_prevent_cross_account_node_clobber(tmp_path: Path, monkeypatch):
    from src.sidecar_proxy import SidecarProxyManager, find_mihomo_binary

    manager = SidecarProxyManager(runtime_dir=tmp_path / "slots", proxy_port=17941, ctrl_port=29141)
    config = manager._build_config(
        [
            {"name": "🇸🇬 Singapore | 01", "type": "ss", "server": "127.0.0.1", "port": 30011},
            {"name": "🇯🇵 Japan | 01", "type": "ss", "server": "127.0.0.1", "port": 30012},
        ]
    )
    assert len(config["listeners"]) == 16
    assert config["listeners"][0]["name"] == "kv-slot-0"
    assert any(r == "IN-NAME,kv-slot-0,KV_SLOT_0" for r in config["rules"])
    assert config["rules"].index("IN-NAME,kv-slot-0,KV_SLOT_0") < config["rules"].index("GEOIP,CN,DIRECT,no-resolve")

    # Verify slot allocation via mocked controller
    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(manager, "ensure_started", lambda: {"ok": True, "proxy_url": "http://127.0.0.1:17941"})
    monkeypatch.setattr(manager, "_ctrl_request", lambda m, p, b=None, **kw: calls.append((m, p, b)) or (204, {}))

    r1 = manager.switch_node("🇸🇬 Singapore | 01", isolate_slot=True, account_id=101)
    r2 = manager.switch_node("🇯🇵 Japan | 01", isolate_slot=True, account_id=102)
    assert r1["ok"] and r2["ok"]
    assert r1["slot"] != r2["slot"]
    assert r1["proxy_url"] != r2["proxy_url"]
    assert r1["groups"] == [f"KV_SLOT_{r1['slot']}"]
    assert r2["groups"] == [f"KV_SLOT_{r2['slot']}"]

    # Live Mihomo config & slot switch test when mihomo binary is installed
    mihomo_bin = find_mihomo_binary()
    if mihomo_bin:
        cfg_file = tmp_path / "live-slots.yaml"
        cfg_file.write_text(
            yaml.safe_dump(
                {
                    "proxies": [
                        {"name": "🇸🇬 Singapore | 01", "type": "ss", "server": "127.0.0.1", "port": 30011, "cipher": "aes-128-gcm", "password": "sg"},
                        {"name": "🇯🇵 Japan | 01", "type": "ss", "server": "127.0.0.1", "port": 30012, "cipher": "aes-128-gcm", "password": "jp"},
                    ]
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("KEYVAULT_CLASH_CONFIG", str(cfg_file))
        monkeypatch.setattr("src.proxy_service.find_active_mihomo_pipe", lambda: "")
        live = SidecarProxyManager(runtime_dir=tmp_path / "live_rt", proxy_port=_preferred_port(17961), ctrl_port=_preferred_port(29161))
        try:
            assert live.ensure_started()["ok"] is True
            s1 = live.switch_node("🇸🇬 Singapore | 01", isolate_slot=True, account_id=1)
            s2 = live.switch_node("🇯🇵 Japan | 01", isolate_slot=True, account_id=2)
            assert s1["ok"] and s2["ok"] and s1["proxy_url"] != s2["proxy_url"]
            st, px = live._ctrl_request("GET", "/proxies", timeout=2.0)
            assert st == 200
            assert px["proxies"][f"KV_SLOT_{s1['slot']}"]["now"] == "🇸🇬 Singapore | 01"
            assert px["proxies"][f"KV_SLOT_{s2['slot']}"]["now"] == "🇯🇵 Japan | 01"
        finally:
            live.stop()


def test_sidecar_lru_slot_eviction_selective_conn_close_and_provider_signature(tmp_path: Path, monkeypatch):
    from src.sidecar_proxy import SidecarProxyManager

    manager = SidecarProxyManager(runtime_dir=tmp_path / "slots", proxy_port=17941, ctrl_port=29141)
    manager._slot_ports = [18000, 18001]  # Force 2 slots so account #3 evicts account #1
    deleted_conns: list[str] = []

    def fake_ctrl(method: str, path: str, body=None, timeout=1.5):
        if method == "GET" and path == "/connections":
            return 200, {
                "connections": [
                    {"id": "slot-conn", "metadata": {"inboundName": "kv-slot-0"}},
                    {"id": "shared-conn", "metadata": {"inboundName": "mixed-in"}},
                ]
            }
        if method == "DELETE":
            deleted_conns.append(path)
        return 204, {}

    monkeypatch.setattr(manager, "ensure_started", lambda: {"ok": True, "proxy_url": "http://127.0.0.1:17941"})
    monkeypatch.setattr(manager, "_ctrl_request", fake_ctrl)

    r1 = manager.switch_node("NodeA", isolate_slot=True, account_id=101)
    r2 = manager.switch_node("NodeB", isolate_slot=True, account_id=102)
    manager._slot_last_used[r1["slot"]] = 1.0
    manager._slot_last_used[r2["slot"]] = 100.0
    r3 = manager.switch_node("NodeC", isolate_slot=True, account_id=103)
    # Account 101 must have been evicted from _account_slots when its slot was taken by 103
    assert r3["slot"] == r1["slot"]
    assert 101 not in manager._account_slots
    assert manager._account_slots[103] == r3["slot"]

    # Non-isolated switch closes ONLY shared mixed-in connections, keeping kv-slot-0 untouched
    deleted_conns.clear()
    manager.switch_node("NodeA", isolate_slot=False)
    assert deleted_conns == ["/connections/shared-conn"]

    # proxy_provider/*.yaml updates change _yaml_signature
    sub_dir = tmp_path / "cfg"
    prov_dir = sub_dir / "proxy_provider"
    prov_dir.mkdir(parents=True)
    main_yaml = sub_dir / "config.yaml"
    main_yaml.write_text("mixed-port: 7890\n", encoding="utf-8")
    pfile = prov_dir / "extra.yaml"
    pfile.write_text("proxies: []\n", encoding="utf-8")
    sig1 = manager._yaml_signature(main_yaml)
    pfile.write_text("proxies:\n  - name: X\n", encoding="utf-8")
    sig2 = manager._yaml_signature(main_yaml)
    assert sig1 != sig2
