from pathlib import Path
from src.api import AccountApi


def _domain_email(api: AccountApi, email: str, password: str = "MailPass#2026") -> dict:
    res = api.save_account({"account_type": "域名邮箱", "account": email, "password": password})
    assert res["ok"] is True
    return res["account"]


def test_api_smart_parse_and_save_and_settings(tmp_path: Path):
    data_dir = tmp_path / "data"
    api = AccountApi(data_dir=data_dir)

    raw = (
        "bob_google99@gmail.com----GoogleDemo#888----recovery_demo@example.org\n"
        "----gezdgnbvgy3tqojqgezdgnbvgy3tqojq----2024----US"
    )
    parsed = api.smart_parse(raw)
    assert parsed["count"] == 1
    assert parsed["records"][0]["account_type"] == "Google"
    assert parsed["records"][0]["totp"]["valid"] is True

    saved = api.save_account(parsed["records"][0])
    assert saved["ok"] is True
    acc_id = saved["account"]["id"]

    dash = api.get_dashboard()
    assert dash["stats"]["total"] == 1
    assert dash["stats"]["google"] == 1
    assert dash["accounts"][0]["id"] == acc_id
    assert "dicts" in dash

    updated_payload = dict(dash["accounts"][0])
    updated_payload["note"] = "主力谷歌号"
    res_up = api.save_account(updated_payload)
    assert res_up["account"]["note"] == "主力谷歌号"

    s = api.save_settings({"theme": "light", "view_mode": "table"})
    assert s["theme"] == "light"
    assert (data_dir / "settings.json").exists()

    totp_res = api.refresh_totp()
    assert str(acc_id) in totp_res["codes"]

    del_res = api.delete_account(acc_id)
    assert del_res["ok"] is True
    assert api.get_dashboard()["stats"]["total"] == 0


def test_2fa_secret_auto_strip_spaces_and_uppercase(tmp_path: Path):
    data_dir = tmp_path / "data"
    api = AccountApi(data_dir=data_dir)

    only_sec = api.smart_parse("jbsw y3dp ehpk 3pxp")
    assert "only_secret" in only_sec
    assert only_sec["only_secret"]["secret_key"] == "JBSWY3DPEHPK3PXP"
    assert only_sec["only_secret"]["totp"]["valid"] is True

    saved = api.save_account(
        {
            "account_type": "ChatGPT",
            "account": "user@example.com",
            "password": "pw",
            "secret_key": "gezd gnbv gy3t qojq gezd gnbv gy3t qojq",
            "initial_secret_key": "jbsw y3dp ehpk 3pxp jbsw y3dp ehpk 3pxp",
        }
    )
    assert saved["ok"] is True
    assert saved["account"]["secret_key"] == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert saved["account"]["initial_secret_key"] == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def test_domain_email_batch_import_uses_selected_category(tmp_path: Path):
    api = AccountApi(data_dir=tmp_path / "data")

    result = api.batch_import(
        "mailbox01@other.example----PasswordOne\nmailbox02@other.example----PasswordTwo",
        "域名邮箱",
    )

    assert result == {"ok": True, "imported": 2}
    items = api.get_dashboard(account_type="域名邮箱")["accounts"]
    assert [item["account"] for item in items] == [
        "mailbox01@other.example",
        "mailbox02@other.example",
    ]
    assert api.get_dashboard()["settings"]["domain_email_login_url"] == "https://mail.ggb6.com"


def test_api_health_status_lifecycle_and_dashboard_filter(tmp_path: Path):
    data_dir = tmp_path / "data"
    api = AccountApi(data_dir=data_dir)

    res = api.save_account({
        "account_type": "ChatGPT",
        "account": "user1@openai.com",
        "password": "Password999",
        "health_status": "可用",
    })
    assert res["ok"] is True
    acc_id = res["account"]["id"]
    assert res["account"]["health_status"] == "可用"

    set_res = api.set_health_status(acc_id, "验证")
    assert set_res["ok"] is True
    assert set_res["account"]["health_status"] == "验证"

    api.save_account({
        "account_type": "Google",
        "account": "user2@gmail.com",
        "password": "Password888",
        "health_status": "封号",
    })

    dash_verify = api.get_dashboard(health_status="验证")
    assert len(dash_verify["accounts"]) == 1
    assert dash_verify["accounts"][0]["account"] == "user1@openai.com"

    dash_banned = api.get_dashboard(health_status="封号")
    assert len(dash_banned["accounts"]) == 1
    assert dash_banned["accounts"][0]["account"] == "user2@gmail.com"

    dash_all = api.get_dashboard(health_status="All")
    assert len(dash_all["accounts"]) == 2
    assert dash_all["stats"]["health"]["验证"] == 1
    assert dash_all["stats"]["health"]["封号"] == 1
    assert dash_all["stats"]["available"] == 0
    assert dash_all["stats"]["unavailable"] == 2

    dash_unavail = api.get_dashboard(health_status="不可用")
    assert len(dash_unavail["accounts"]) == 2

    dash_avail = api.get_dashboard(health_status="可用")
    assert len(dash_avail["accounts"]) == 0


def test_api_antigravity_tier_lifecycle_and_dashboard_filter(tmp_path: Path):
    data_dir = tmp_path / "data"
    api = AccountApi(data_dir=data_dir)

    res1 = api.save_account({
        "account_type": "Google",
        "account": "ag_user1@gmail.com",
        "password": "Password999",
        "antigravity_tier": "蓝色Pro",
    })
    assert res1["ok"] is True
    acc_id = res1["account"]["id"]
    assert res1["account"]["antigravity_tier"] == "蓝色Pro"

    set_res = api.set_antigravity_tier(acc_id, "金色Pro")
    assert set_res["ok"] is True
    assert set_res["account"]["antigravity_tier"] == "金色Pro"

    api.save_account({
        "account_type": "Google",
        "account": "ag_user2@gmail.com",
        "password": "Password888",
        "antigravity_tier": "蓝色Pro",
    })

    dash_gold = api.get_dashboard(antigravity_tier="金色Pro")
    assert len(dash_gold["accounts"]) == 1
    assert dash_gold["accounts"][0]["account"] == "ag_user1@gmail.com"

    dash_blue = api.get_dashboard(antigravity_tier="蓝色Pro")
    assert len(dash_blue["accounts"]) == 1
    assert dash_blue["accounts"][0]["account"] == "ag_user2@gmail.com"

    dash_all = api.get_dashboard(antigravity_tier="All")
    assert len(dash_all["accounts"]) == 2
    assert dash_all["stats"]["antigravity"]["金色Pro"] == 1
    assert dash_all["stats"]["antigravity"]["蓝色Pro"] == 1


def test_api_dict_and_device_management(tmp_path: Path):
    data_dir = tmp_path / "data"
    api = AccountApi(data_dir=data_dir)

    dicts = api.get_system_dicts()
    assert len(dicts) >= 15

    add_res = api.save_system_dict({
        "dict_type": "device_tag",
        "dict_key": "MacBook",
        "dict_label": "MacBook",
        "icon": "💻",
        "sort_order": 4,
    })
    assert add_res["ok"] is True
    d_id = add_res["dict"]["id"]

    dev_dicts = api.get_system_dicts("device_tag")
    assert any(d["dict_key"] == "MacBook" for d in dev_dicts)

    acc = api.save_account({
        "account_type": "WorkBuddy",
        "account": "mac_user@workbuddy.ai",
        "password": "Password123",
        "device_tag": "MacBook",
    })
    assert acc["ok"] is True
    acc_id = acc["account"]["id"]
    assert acc["account"]["device_tag"] == "MacBook"

    set_dev = api.set_device_tag(acc_id, "iPhone")
    assert set_dev["ok"] is True
    assert set_dev["account"]["device_tag"] == "iPhone"

    dash = api.get_dashboard(device_tag="iPhone")
    assert len(dash["accounts"]) == 1

    del_res = api.delete_system_dict(d_id)
    assert del_res["ok"] is True


def test_attribute_api(tmp_path: Path):
    data_dir = tmp_path / "data"
    api = AccountApi(data_dir=data_dir)

    # 1. get_attributes returns system seeded items
    res = api.get_attributes()
    assert res["ok"] is True
    attrs = res["data"]
    assert len(attrs) >= 3
    keys = {a["attr_key"]: a for a in attrs}
    assert "health_status" in keys
    assert "device_tag" in keys
    assert "antigravity_tier" in keys

    # 2. save_attribute for custom attribute
    save_res = api.save_attribute({
        "attr_key": "region_zone",
        "attr_label": "可用区",
        "attr_type": "text",
        "bound_platforms": ["Google", "WorkBuddy"],
        "sort_order": 60,
    })
    assert save_res["ok"] is True
    attr_id = save_res["attribute"]["id"]
    assert save_res["attribute"]["attr_key"] == "region_zone"

    # Cannot rename system attribute key
    sys_attr = keys["health_status"]
    fail_res = api.save_attribute({
        "id": sys_attr["id"],
        "attr_key": "health_status_renamed",
        "attr_label": "新名称",
    })
    assert fail_res["ok"] is False
    assert "不能修改" in fail_res["error"]

    # 3. update_attribute_bindings
    bind_res = api.update_attribute_bindings(attr_id, ["Google"])
    assert bind_res["ok"] is True
    assert "Google" in str(bind_res["attribute"]["bound_platforms"])

    # 4. delete_attribute protection
    del_sys = api.delete_attribute(sys_attr["id"])
    assert del_sys["ok"] is False
    assert "系统内置" in del_sys["error"]

    del_custom = api.delete_attribute(attr_id)
    assert del_custom["ok"] is True

    # 5. dashboard payload has attributes
    dash = api.get_dashboard()
    assert "attributes" in dash
    assert len(dash["attributes"]) >= 3

    # 6. save_account supports custom_fields
    acc_res = api.save_account({
        "account_type": "Google",
        "account": "custom_prop@gmail.com",
        "password": "Password123",
        "custom_fields": {"backup_phone": "+123456789"},
    })
    assert acc_res["ok"] is True
    assert acc_res["account"]["custom_fields"].get("backup_phone") == "+123456789"


def test_launch_browser_domain_email_uses_isolated_browser(tmp_path: Path, monkeypatch):
    import src.browser_service as bs

    api = AccountApi(data_dir=tmp_path / "data")
    acc = _domain_email(api, "launch01@staff.example")

    opened = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: opened.append(a) or True)

    launched = {}

    def fake_launch(data_dir, account):
        launched["data_dir"] = data_dir
        launched["account"] = account
        return {"ok": True, "profile_dir": str(data_dir / "browsers" / f"acc_{account['id']}"), "fp_config": account["fp_config"]}

    monkeypatch.setattr(bs, "launch_isolated_browser", fake_launch)
    monkeypatch.setattr(api, "allocate_node_for_domain_email", lambda account_id: "Node-A")

    res = api.launch_browser(acc["id"])
    assert res["ok"] is True
    assert opened == []
    assert launched["account"]["account_type"] == "域名邮箱"
    assert "profile_dir" in res and "fp_config" in res


def test_launch_browser_domain_email_allocates_node_when_unlocked(tmp_path: Path, monkeypatch):
    import src.browser_service as bs

    api = AccountApi(data_dir=tmp_path / "data")
    acc = _domain_email(api, "launch02@staff.example")
    calls = []

    def fake_allocate(account_id):
        calls.append(account_id)
        api.db.bind_account_proxy_node(account_id, "http://127.0.0.1:17899", "Node-X", locked=True)
        return "Node-X"

    monkeypatch.setattr(api, "allocate_node_for_domain_email", fake_allocate)
    monkeypatch.setattr(bs, "launch_isolated_browser", lambda d, a: {"ok": True, "fp_config": a["fp_config"]})

    res = api.launch_browser(acc["id"])
    assert calls == [acc["id"]]
    assert res["fp_config"]["proxy_node"] == "Node-X"


def test_launch_browser_domain_email_keeps_locked_node(tmp_path: Path, monkeypatch):
    import src.browser_service as bs

    api = AccountApi(data_dir=tmp_path / "data")
    acc = _domain_email(api, "launch03@staff.example")
    api.db.bind_account_proxy_node(acc["id"], "http://127.0.0.1:17899", "Node-Locked", locked=True)

    monkeypatch.setattr(api, "allocate_node_for_domain_email", lambda account_id: (_ for _ in ()).throw(AssertionError("must not allocate")))
    monkeypatch.setattr(bs, "launch_isolated_browser", lambda d, a: {"ok": True, "fp_config": a["fp_config"]})

    res = api.launch_browser(acc["id"])
    assert res["fp_config"]["proxy_node"] == "Node-Locked"


def test_allocate_clean_node_skips_full_nodes(tmp_path: Path):
    api = AccountApi(data_dir=tmp_path / "data")
    full = [_domain_email(api, f"full{i}@staff.example") for i in range(2)]
    for acc in full:
        api.db.bind_account_proxy_node(acc["id"], "http://127.0.0.1:17899", "Node-Full", locked=True)
        api.db.record_email_verification_code(acc["id"], {"code": "123456", "timestamp": 1})

    fresh = _domain_email(api, "fresh@staff.example")
    chosen = api.db.allocate_clean_node_for_account(
        fresh["id"], ["Node-Full", "Node-Empty"], default_proxy_url="http://127.0.0.1:17899"
    )
    assert chosen == "Node-Empty"
    updated = api.db.get_account(fresh["id"])
    assert updated["fp_config"]["proxy_node"] == "Node-Empty"
    assert updated["fp_config"]["node_locked"] is True


def test_allocate_clean_node_keeps_existing_locked_binding(tmp_path: Path):
    api = AccountApi(data_dir=tmp_path / "data")
    acc = _domain_email(api, "locked@staff.example")
    api.db.bind_account_proxy_node(acc["id"], "http://127.0.0.1:17899", "Node-Locked", locked=True)

    chosen = api.db.allocate_clean_node_for_account(acc["id"], ["Node-Other"])
    assert chosen == "Node-Locked"
    assert api.db.get_account(acc["id"])["fp_config"]["proxy_node"] == "Node-Locked"


def test_reset_domain_email_nodes_clears_proxy_and_codes(tmp_path: Path):
    api = AccountApi(data_dir=tmp_path / "data")
    acc = _domain_email(api, "reset01@staff.example")
    other = _domain_email(api, "reset02@staff.example")
    api.db.bind_account_proxy_node(acc["id"], "http://127.0.0.1:17899", "Node-R1", locked=True)
    api.db.record_email_verification_code(acc["id"], {"code": "654321", "timestamp": 100, "subject": "Code"})
    api.db.bind_account_proxy_node(other["id"], "http://127.0.0.1:17899", "Node-R2", locked=True)
    api.db.record_email_verification_code(other["id"], {"code": "112233", "timestamp": 200, "subject": "Code"})

    res = api.reset_domain_email_nodes([acc["id"]])
    assert res == {"ok": True, "reset_count": 1}

    reset = api.db.get_account(acc["id"])
    assert reset["fp_config"]["proxy_node"] == ""
    assert reset["fp_config"]["node_locked"] is False
    for key in ("latest_code", "code_received_at", "code_subject", "has_received_code"):
        assert key not in reset["custom_fields"]

    untouched = api.db.get_account(other["id"])
    assert untouched["fp_config"]["proxy_node"] == "Node-R2"
    assert untouched["custom_fields"]["latest_code"] == "112233"

    res_all = api.reset_domain_email_nodes()
    assert res_all["ok"] is True
    assert res_all["reset_count"] == 2
    assert api.db.get_node_code_stats() == {}


def test_get_domain_email_codes_records_and_returns_codes(tmp_path: Path, monkeypatch):
    import src.api as api_module

    api = AccountApi(data_dir=tmp_path / "data")
    acc = _domain_email(api, "codes01@staff.example")
    api.db.bind_account_proxy_node(acc["id"], "http://127.0.0.1:17899", "Node-C1", locked=True)

    calls = []

    def fake_fetch(email, password, max_age_seconds=1800):
        calls.append((email, password))
        return {"code": "998877", "from": "GitHub <noreply@github.com>", "subject": "Your launch code", "date": "today", "timestamp": 123456}

    monkeypatch.setattr(api_module, "fetch_latest_mail_code", fake_fetch)

    res = api.get_domain_email_codes([acc["id"]])
    assert res["ok"] is True
    assert calls == [("codes01@staff.example", "MailPass#2026")]
    entry = res["codes"][acc["id"]]
    assert entry["code"] == "998877"
    assert entry["received_at"] == 123456
    assert entry["subject"] == "Your launch code"
    assert entry["proxy_node"] == "Node-C1"
    assert res["node_stats"] == {"Node-C1": 1}

    stored = api.db.get_account(acc["id"])["custom_fields"]
    assert stored["latest_code"] == "998877"
    assert stored["has_received_code"] is True
    assert api.get_dashboard()["node_code_stats"] == {"Node-C1": 1}


def test_get_domain_email_codes_ignores_missing_mail(tmp_path: Path, monkeypatch):
    import src.api as api_module

    api = AccountApi(data_dir=tmp_path / "data")
    acc = _domain_email(api, "codes02@staff.example")
    monkeypatch.setattr(api_module, "fetch_latest_mail_code", lambda *a, **k: None)

    res = api.get_domain_email_codes()
    assert res["ok"] is True
    assert res["codes"] == {}
    assert "latest_code" not in api.db.get_account(acc["id"])["custom_fields"]


def test_get_domain_email_codes_only_polls_active_accounts_when_many_exist(tmp_path: Path, monkeypatch):
    import json

    import src.api as api_module

    data_dir = tmp_path / "data"
    api = AccountApi(data_dir=data_dir)
    accounts = [_domain_email(api, f"active{i}@staff.example") for i in range(7)]
    active1, active2, active3 = accounts[0], accounts[1], accounts[2]
    idle = accounts[3:]

    api.db.bind_account_proxy_node(active1["id"], "http://127.0.0.1:17899", "Node-Active", locked=True)
    (data_dir / "browsers" / f"acc_{active2['id']}").mkdir(parents=True)
    # DatabaseService._prepare_fields normalises 进行中 back to 未完成 on save,
    # so persist the raw custom fields JSON to model an in-progress registration.
    with api.db._connect() as conn:
        conn.execute(
            "UPDATE accounts SET custom_fields_json = ? WHERE id = ?",
            (json.dumps({"wb_reg_status": "进行中"}, ensure_ascii=False), active3["id"]),
        )

    called_emails = []

    def fake_fetch(email, password, max_age_seconds=1800):
        called_emails.append(email)
        return None

    monkeypatch.setattr(api_module, "fetch_latest_mail_code", fake_fetch)

    res = api.get_domain_email_codes()
    assert res["ok"] is True
    assert set(called_emails) == {
        active1["account"],
        active2["account"],
        active3["account"],
    }
    for acc in idle:
        assert acc["account"] not in called_emails


def test_get_domain_email_codes_skips_all_when_many_and_none_active(tmp_path: Path, monkeypatch):
    import src.api as api_module

    api = AccountApi(data_dir=tmp_path / "data")
    for i in range(7):
        _domain_email(api, f"idle{i}@staff.example")

    calls = []
    monkeypatch.setattr(
        api_module,
        "fetch_latest_mail_code",
        lambda *a, **k: calls.append(a) or None,
    )

    res = api.get_domain_email_codes()
    assert res["ok"] is True
    assert res["codes"] == {}
    assert calls == []


def test_smart_parse_and_batch_import_with_claude_category(tmp_path: Path):
    api = AccountApi(data_dir=tmp_path / "data")

    raw = (
        "claude_one@example.com----ClaudePass#1----JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP\n"
        "claude_two@example.com----ClaudePass#2----GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    )
    parsed = api.smart_parse(raw, "Claude")
    assert parsed["count"] == 2
    assert all(r["account_type"] == "Claude" for r in parsed["records"])
    assert parsed["records"][0]["totp"]["valid"] is True

    imported = api.batch_import(raw, "Claude")
    assert imported == {"ok": True, "imported": 2}
    claude_items = api.get_dashboard(account_type="Claude")["accounts"]
    assert sorted(i["account"] for i in claude_items) == [
        "claude_one@example.com",
        "claude_two@example.com",
    ]
    stats = api.get_dashboard()["stats"]
    assert stats["claude"] == 2
    assert stats["types"]["Claude"] == 2
    assert stats["other"] == 0

    # Domain mailboxes keep their dedicated parser even with the Claude label.
    domain_res = api.batch_import("mailbox01@other.example----PasswordOne", "域名邮箱")
    assert domain_res == {"ok": True, "imported": 1}
    assert api.get_dashboard(account_type="域名邮箱")["stats"]["types"]["Claude"] == 2


def test_smart_parse_without_category_keeps_existing_behaviour(tmp_path: Path):
    api = AccountApi(data_dir=tmp_path / "data")
    parsed = api.smart_parse(
        "alice_demo01@example.com----DemoPass#2026!----JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    )
    assert parsed["records"][0]["account_type"] == "ChatGPT"
    google = api.smart_parse(
        "bob_google99@gmail.com----GoogleDemo#888----recovery_demo@example.org"
        "----gezdgnbvgy3tqojqgezdgnbvgy3tqojq----2024----US"
    )
    assert google["records"][0]["account_type"] == "Google"
    domain = api.smart_parse("mailbox01@ggb6.com----MailboxPass#2026!")
    assert domain["records"][0]["account_type"] == "域名邮箱"


def test_get_proxy_nodes_forwards_claude_category(tmp_path: Path, monkeypatch):
    import src.api as api_module

    calls = []

    def fake_detect(account_type=""):
        calls.append(account_type)
        return {"nodes": [{"name": "US | MiyaIP-2 HY2"}], "ports": []}

    monkeypatch.setattr(api_module, "detect_proxy_environment", fake_detect)
    api = AccountApi(data_dir=tmp_path / "data")
    acc = api.save_account(
        {"account_type": "Claude", "account": "claude_nodes@example.com", "password": "pw"}
    )["account"]

    api.get_proxy_nodes("Claude")
    api.get_proxy_nodes(acc["id"])
    assert calls == ["Claude", "Claude"]


def test_totp_otpauth_and_offset_and_persona_generators(tmp_path: Path):
    from src.totp_service import generate_totp, normalize_secret

    uri = "otpauth://totp/Google:test%40gmail.com?secret=jbsw-y3dp%20ehpk3pxp&issuer=Google"
    assert normalize_secret(uri) == "JBSWY3DPEHPK3PXP"
    assert normalize_secret("backup2fa:mfrg gzdf mztw q2lk") == "MFRGGZDFMZTWQ2LK"

    t0 = generate_totp("JBSWY3DPEHPK3PXP", at_time=1700000000, offset_seconds=0)
    t30 = generate_totp("JBSWY3DPEHPK3PXP", at_time=1700000000, offset_seconds=30)
    assert t0["valid"] is True and t30["valid"] is True
    assert t0["code"] != t30["code"]

    api = AccountApi(data_dir=tmp_path / "data")
    pwd_res = api.generate_strong_password(16)
    assert pwd_res["ok"] is True
    assert len(pwd_res["password"]) == 16
    assert len(pwd_res["secret_key"]) == 16

    persona = api.generate_account_persona(region="JP", domain="gmail.com")["persona"]
    assert persona["region"] == "JP"
    assert persona["suggested_email"].endswith("@gmail.com")
    assert len(persona["password"]) >= 12


def test_rt_inspect_save_batch_export_and_inject(tmp_path: Path, monkeypatch):
    api = AccountApi(data_dir=tmp_path / "data")
    acc = api.save_account({
        "account_type": "Google",
        "account": "rt_owner@gmail.com",
        "password": "pw",
    })["account"]

    insp = api.inspect_rt_json("1//09abcdef_test_refresh_token")
    assert insp["ok"] is True
    assert insp["refresh_token"] == "1//09abcdef_test_refresh_token"

    saved = api.save_account_rt(acc["id"], "1//09abcdef_test_refresh_token")
    assert saved["ok"] is True
    assert saved["account"]["has_rt"] is True
    assert saved["account"]["rt_status"] == "active"

    batch_res = api.batch_import_rt_json(
        '[{"email":"rt_new@gmail.com","refresh_token":"1//09xyz_new_token"}]'
    )
    assert batch_res["ok"] is True
    assert batch_res["imported"] == 1

    exp = api.export_rt_json()
    assert exp["ok"] is True
    assert exp["count"] == 2

    appdata_dir = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata_dir))
    inj = api.inject_rt_to_antigravity(acc["id"])
    assert inj["ok"] is True
    cred_file = appdata_dir / "Antigravity" / "oauth_credentials.json"
    assert cred_file.exists()
    assert "1//09abcdef_test_refresh_token" in cred_file.read_text(encoding="utf-8")


def test_probe_ai_and_appeal_channels_and_backup_endpoints(tmp_path: Path):
    import base64
    import json

    api = AccountApi(data_dir=tmp_path / "data")
    channels = api.get_appeal_channels()
    assert channels["ok"] is True
    assert len(channels["channels"]) >= 5

    # Construct an already-expired JWT session token for ChatGPT local probe verification
    hdr = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    pay = base64.urlsafe_b64encode(b'{"exp":1000000}').rstrip(b"=").decode()
    acc = api.save_account({
        "account_type": "ChatGPT",
        "account": "ai_jwt@example.com",
        "password": "pw",
        "custom_fields": {"session_token": f"{hdr}.{pay}.sig"},
    })["account"]

    probe = api.probe_ai_account(acc["id"])
    assert probe["ok"] is False
    assert "过期" in probe["error"]

    b1 = api.create_backup("manual")
    assert b1["ok"] is True
    blist = api.list_backups()
    assert any(x["filename"] == b1["filename"] for x in blist["backups"])
