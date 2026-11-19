from pathlib import Path
from src.proxy_service import (
    classify_node_region,
    find_clash_config_files,
    is_valid_proxy_node_name,
    parse_clash_yaml_summary,
)
from src.db_service import DatabaseService


def test_classify_node_region_us_relays_never_misclassified_as_japan():
    # ``US`` sits adjacent to ``by`` in these relay names, so a naive ``\bUS\b``
    # or ``\bJP\b`` check would misclassify them.
    assert classify_node_region("US | MiyaIP-2 HY2") == "🇺🇸 美国 (US)"
    assert classify_node_region("USbyJPTT | MiyaIP-2 HY2") == "🇺🇸 美国 (US)"
    assert classify_node_region("USbyJP-54 | MiyaIP-2 HY2") == "🇺🇸 美国 (US)"
    assert classify_node_region("🇺🇸 United States | 01") == "🇺🇸 美国 (US)"
    assert classify_node_region("MiyaIP-US-Residential") == "🇺🇸 美国 (US)"

    # Genuine JP/SG/HK nodes must stay correctly classified.
    assert classify_node_region("🇯🇵 Japan | 01") == "🇯🇵 日本 (JP)"
    assert classify_node_region("JP | Tokyo") == "🇯🇵 日本 (JP)"
    assert classify_node_region("🇸🇬 Singapore | 01") == "🇸🇬 新加坡 (SG)"
    assert classify_node_region("SG | 02") == "🇸🇬 新加坡 (SG)"
    assert classify_node_region("🇭🇰 Hong Kong | 01") == "🇭🇰 香港 (HK)"
    assert classify_node_region("🌍 Unknown | 01") == "🌍 其他地区"

    # Convenience regions.
    assert classify_node_region("🇬🇧 United Kingdom | 01") == "🇬🇧 英国 (UK)"
    assert classify_node_region("🇩🇪 Germany | Frankfurt") == "🇩🇪 德国 (DE)"
    assert classify_node_region("🇨🇦 Canada | Toronto") == "🇨🇦 加拿大 (CA)"


def test_filter_valid_proxy_node_names():
    assert is_valid_proxy_node_name("🇸🇬 Singapore | 04") is True
    assert is_valid_proxy_node_name("🇺🇸 United States | 01") is True
    assert is_valid_proxy_node_name("Traffic: 0 GB / 200 GB") is False
    assert is_valid_proxy_node_name("Expire: 2027-10-29") is False
    assert is_valid_proxy_node_name("DIRECT") is False


def test_parse_clash_yaml_summary(tmp_path: Path):
    sample_yaml = tmp_path / "clash-verge.yaml"
    sample_yaml.write_text(
        """
mixed-port: 17898
secret: my-secret
listeners:
  - name: ai-fixed-sg
    type: mixed
    listen: 127.0.0.1
    port: 7897
    proxy: SG
proxies:
  - name: "Traffic: 10 GB / 200 GB"
    type: trojan
  - name: "🇸🇬 Singapore | 01"
    type: trojan
  - name: "🇺🇸 United States | 01"
    type: trojan
""",
        encoding="utf-8",
    )
    info = parse_clash_yaml_summary(sample_yaml)
    assert info["mixed_port"] == 17898
    assert any(p["port"] == 17898 for p in info["ports"])
    assert any(p["port"] == 7897 for p in info["ports"])
    node_names = [n["name"] for n in info["nodes"]]
    assert "🇸🇬 Singapore | 01" in node_names
    assert "🇺🇸 United States | 01" in node_names
    assert "Traffic: 10 GB / 200 GB" not in node_names


def test_account_node_lock_prevents_modification_until_unlocked(tmp_path: Path):
    db = DatabaseService(tmp_path / "data" / "accounts.db")
    acc = db.add_account(
        {
            "account_type": "ChatGPT",
            "account": "lock_test@icloud.com",
            "password": "pwd",
        }
    )
    acc_id = acc["id"]

    # Bind and lock node
    res1 = db.bind_account_proxy_node(
        acc_id,
        proxy_url="http://127.0.0.1:17898",
        proxy_node="🇸🇬 Singapore | 04",
        locked=True,
    )
    assert res1["ok"] is True
    assert res1["account"]["fp_config"]["proxy_node"] == "🇸🇬 Singapore | 04"
    assert res1["account"]["fp_config"]["node_locked"] is True

    # Attempt to change to US node while locked -> MUST be rejected!
    res2 = db.bind_account_proxy_node(
        acc_id,
        proxy_url="http://127.0.0.1:17898",
        proxy_node="🇺🇸 United States | 01",
        locked=True,
    )
    assert res2["ok"] is False
    assert "已锁定" in res2["error"]
    assert db.get_account(acc_id)["fp_config"]["proxy_node"] == "🇸🇬 Singapore | 04"

    # Unlock first -> succeeds
    res_unlock = db.set_account_node_lock(acc_id, False)
    assert res_unlock["ok"] is True
    assert res_unlock["account"]["fp_config"]["node_locked"] is False

    # Now changing to US node succeeds and locks again
    res3 = db.bind_account_proxy_node(
        acc_id,
        proxy_url="http://127.0.0.1:17898",
        proxy_node="🇺🇸 United States | 01",
        locked=True,
    )
    assert res3["ok"] is True
    assert res3["account"]["fp_config"]["proxy_node"] == "🇺🇸 United States | 01"
    assert res3["account"]["fp_config"]["node_locked"] is True


def test_env_override_clash_config_takes_priority(tmp_path: Path, monkeypatch):
    custom_yaml = tmp_path / "my-custom-clash.yaml"
    custom_yaml.write_text("mixed-port: 7890\n", encoding="utf-8")
    monkeypatch.setenv("KEYVAULT_CLASH_CONFIG", str(custom_yaml))

    found = find_clash_config_files()
    assert found, "custom config should be discovered"
    label, path = found[0]
    assert Path(path).resolve() == custom_yaml.resolve()
    assert "KEYVAULT_CLASH_CONFIG" in label


def test_env_override_clash_config_missing_file_is_ignored(tmp_path: Path, monkeypatch):
    missing = tmp_path / "does-not-exist.yaml"
    monkeypatch.setenv("KEYVAULT_CLASH_CONFIG", str(missing))
    found = find_clash_config_files()
    assert all(Path(p).resolve() != missing.resolve() for _, p in found)

def test_resolve_leaf_active_proxy_node():
    from src.proxy_service import _resolve_leaf_node
    mock_px = {
        "Google": {"type": "Selector", "now": "SG"},
        "SG": {"type": "Selector", "now": "🇸🇬 Singapore | 04"},
        "OpenAI": {"type": "Selector", "now": "Proxies"},
        "Proxies": {"type": "Selector", "now": "SG"},
        "🇸🇬 Singapore | 04": {"type": "trojan"},
        "🇺🇸 United States | 01": {"type": "ss"},
        "GLOBAL": {"type": "Selector", "now": "DIRECT"},
    }
    assert _resolve_leaf_node(mock_px, "SG") == "🇸🇬 Singapore | 04"
    assert _resolve_leaf_node(mock_px, "Google") == "🇸🇬 Singapore | 04"
    assert _resolve_leaf_node(mock_px, "OpenAI") == "🇸🇬 Singapore | 04"
    assert _resolve_leaf_node(mock_px, "DIRECT") == "DIRECT"


def test_activate_proxy_node_delegates_to_isolated_sidecar_without_touching_verge(monkeypatch):
    import src.proxy_service as ps
    import src.sidecar_proxy as sidecar_module

    def _forbidden(*args, **kwargs):
        raise AssertionError("Must never touch Clash Verge controller!")

    monkeypatch.setattr(ps, "find_active_mihomo_pipe", _forbidden)
    monkeypatch.setattr(ps, "_call_controller", _forbidden)

    calls = []

    class _StubSidecar:
        def switch_node(self, node_name, *, preflight=False, preflight_timeout=6.0):
            calls.append({"node": node_name, "preflight": preflight})
            return {
                "ok": True,
                "switched": True,
                "node": node_name,
                "proxy_url": "http://127.0.0.1:17899",
                "groups": ["KEYVAULT_PROXY"],
                "preflight_ok": True,
                "reachable": True,
                "restricted": False,
            }

    stub = _StubSidecar()
    manager_calls = []

    def _get_manager():
        manager_calls.append(True)
        return stub

    monkeypatch.setattr(sidecar_module, "get_sidecar_proxy_manager", _get_manager)

    res = ps.activate_proxy_node(
        "WorkBuddy", "http://127.0.0.1:17899", "🇸🇬 Singapore | 01", preflight=True
    )
    assert calls == [{"node": "🇸🇬 Singapore | 01", "preflight": True}]
    assert manager_calls == [True]
    assert res["ok"] is True
    assert res["switched"] is True
    assert res["groups"] == ["KEYVAULT_PROXY"]
    assert res["proxy_url"] == "http://127.0.0.1:17899"
    assert res["preflight_ok"] is True

    # The legacy Clash Verge controller helpers must remain untouched.
    assert ps.find_active_mihomo_pipe is _forbidden
    assert ps._call_controller is _forbidden

    # A blank node never reaches the sidecar at all.
    calls.clear()
    manager_calls.clear()
    blank = ps.activate_proxy_node("WorkBuddy", "http://127.0.0.1:17899", "   ")
    assert blank == {"ok": True, "switched": False, "proxy_url": "http://127.0.0.1:17899"}
    assert calls == []
    assert manager_calls == []


def test_parse_clash_yaml_summary_handles_malformed_ports(tmp_path: Path):
    bad_yaml = tmp_path / "bad_port.yaml"
    bad_yaml.write_text(
        "mixed-port: not-a-number\nsocks-port: 99999\nlisteners:\n  - name: bad\n    port: abc\n",
        encoding="utf-8",
    )
    info = parse_clash_yaml_summary(bad_yaml)
    assert info["mixed_port"] == 0
    assert info["ports"] == []
