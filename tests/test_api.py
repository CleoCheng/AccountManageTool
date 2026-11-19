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


def test_dashboard_health_counts_follow_platform_and_other_filters(tmp_path: Path):
    api = AccountApi(tmp_path)

    def add(platform, name, **fields):
        saved = api.save_account({"account_type": platform, "account": f"{name}@example.test", **fields})
        assert saved["ok"]
        return saved["account"]["id"]

    google_fields = {"device_tag": "iPhone", "custom_fields": {"warming_group": "team"}}
    confirmed = add("Google", "in-scope-confirmed", **google_fields)
    unreviewed = add("Google", "in-scope-unreviewed", **google_fields)
    deactivated = add("Google", "in-scope-deactivated", deactivated_date="2026-10-01", **google_fields)
    challenged = add("Google", "other-challenged", health_status="验证")
    assert api.set_health_status(confirmed, "可用")["ok"]
    assert api.set_health_status(deactivated, "可用")["ok"]
    chatgpt = {add("ChatGPT", "chat-1"), add("ChatGPT", "chat-2")}
    add("ChatGPT", "chat-banned", health_status="封号")
    add("Qcoder", "other-active")

    overall = api.get_dashboard()
    assert overall["health_counts"] == {"可用": 4, "不可用": 3, "未核对": 1}
    assert overall["stats"]["health"]["未核对"] == 1
    assert overall["stats"]["total"] == 8

    expected_google = {"可用": (confirmed,), "不可用": (deactivated, challenged), "未核对": (unreviewed,)}
    for status, ids in expected_google.items():
        dashboard = api.get_dashboard(account_type="Google", health_status=status)
        assert tuple(account["id"] for account in dashboard["accounts"]) == ids
        assert dashboard["health_counts"] == {"可用": 1, "不可用": 2, "未核对": 1}
        assert dashboard["stats"]["types"]["ChatGPT"] == 3
    assert [item["id"] for item in api.db.list_accounts(health_status="未核对")] == [unreviewed]
    assert [item["id"] for item in api.get_dashboard("Google", health_status="验证")["accounts"]] == [challenged]

    chat_dashboard = api.get_dashboard(account_type="ChatGPT", health_status="可用")
    assert {account["id"] for account in chat_dashboard["accounts"]} == chatgpt
    assert chat_dashboard["health_counts"] == {"可用": 2, "不可用": 1, "未核对": 0}
    assert api.get_dashboard(account_type="Qcoder")["health_counts"] == {"可用": 1, "不可用": 0, "未核对": 0}
    assert api.get_dashboard(account_type="WorkBuddy")["health_counts"] == {"可用": 0, "不可用": 0, "未核对": 0}

    scope = dict(account_type="Google", search="in-scope", status="active", device_tag="iPhone")
    narrowed = api.get_dashboard(**scope, custom_filters={"warming_group": "team"}, health_status="未核对")
    assert narrowed["health_counts"] == {"可用": 1, "不可用": 0, "未核对": 1}
    assert [item["id"] for item in narrowed["accounts"]] == [unreviewed]
    empty = api.get_dashboard(**scope, custom_filters={"warming_group": "unknown"})
    assert empty["health_counts"] == {"可用": 0, "不可用": 0, "未核对": 0}
    assert not empty["accounts"]

    assert api.set_health_status(unreviewed, "可用")["ok"]
    assert api.get_dashboard(**scope)["health_counts"] == {"可用": 2, "不可用": 0, "未核对": 0}


def test_google_interactive_save_rejects_invalid_totp_without_archiving(tmp_path: Path):
    api = AccountApi(tmp_path)
    google = {"account_type": "Google", "account": "totp-check@example.test"}
    invalid = api.save_account({**google, "secret_key": "123456"})
    assert not invalid["ok"] and "2FA 密钥格式无效" in invalid["error"]
    assert not api.db.list_accounts()

    uri = "otpauth://totp/Test?secret=jbsw%20y3dp%20ehpk%203pxp"
    created = api.save_account({**google, "secret_key": uri, "note": "original"})
    assert created["ok"]
    account = created["account"]
    account_id = account["id"]
    assert account["secret_key"] == "JBSWY3DPEHPK3PXP"
    assert account["totp"]["valid"]

    invalid_edit = api.save_account({**google, "id": account_id, "secret_key": "234567", "note": "discard"})
    assert not invalid_edit["ok"] and "2FA 密钥格式无效" in invalid_edit["error"]
    invalid_quick = api.quick_update_credentials(account_id, {
        "secret_key": "123456", "google_confirmed": True, "note": "discard",
    })
    assert not invalid_quick["ok"] and "2FA 密钥格式无效" in invalid_quick["error"]
    assert api.db.get_account(account_id)["secret_key"] == account["secret_key"]
    assert api.db.get_account(account_id)["note"] == "original"
    assert api.get_credential_history(account_id)["history"] == []

    legacy = api.db.add_account({**google, "account": "legacy-totp@example.test", "secret_key": "123456"})
    assert api.save_account({**legacy, "note": "keep the existing key"})["ok"]
    corrected = api.quick_update_credentials(legacy["id"], {"secret_key": uri, "google_confirmed": True})
    assert corrected["ok"] and corrected["account"]["totp"]["valid"]
    cleared = api.quick_update_credentials(legacy["id"], {"secret_key": "", "google_confirmed": True})
    assert cleared["ok"] and not cleared["account"]["secret_key"]


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
    ag_dir = appdata_dir / "Antigravity"
    ag_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APPDATA", str(appdata_dir))

    from src import maintenance_service as ms
    monkeypatch.setattr(
        ms,
        "_http_json_request",
        lambda url, **kw: (200, {"access_token": "fresh_at_123", "expires_in": 3600})
        if url.endswith("/token")
        else (404, {}),
    )
    inj = api.inject_rt_to_antigravity(acc["id"])
    assert inj["ok"] is True
    cred_file = ag_dir / "oauth_credentials.json"
    assert cred_file.exists()
    text = cred_file.read_text(encoding="utf-8")
    assert "1//09abcdef_test_refresh_token" in text
    assert "fresh_at_123" in text


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


def test_builtin_oauth_only_reports_success_after_saving_matching_account(tmp_path, monkeypatch):
    import threading
    from src import maintenance_service

    api = AccountApi(data_dir=tmp_path / "data")
    account = api.save_account({"account_type": "Google", "account": "test.owner@gmail.com"})["account"]
    authorized_email = "different.user@gmail.com"

    def fake_request(url, **kwargs):
        if url.endswith("/token"):
            return 200, {"access_token": "mock-access-token", "refresh_token": "1//09mock_refresh_token"}
        return 200, {"email": authorized_email}

    def exchange(session_id):
        api._oauth._sessions[session_id] = {
            "status": "pending",
            "session_id": session_id,
            "account_id": account["id"],
            "redirect_uri": "http://127.0.0.1:12345/oauth-callback",
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "code_verifier": "mock-verifier",
            "proxy_url": "",
            "stop_event": threading.Event(),
        }
        api._oauth._exchange_code(session_id, "mock-code")
        return api.get_builtin_oauth_status(session_id)

    monkeypatch.setattr(maintenance_service, "_http_json_request", fake_request)
    rejected = exchange("different-email")
    assert rejected["status"] == "error" and not rejected["has_rt"]
    assert not api.db.get_account(account["id"])["has_rt"]

    authorized_email = "test.owner@gmail.com"
    accepted = exchange("same-email")
    assert accepted["status"] == "success" and accepted["has_rt"]
    assert api.db.get_account(account["id"])["has_rt"]


def test_google_session_reset_live_validation_and_browser_launch_tracking(tmp_path: Path, monkeypatch):
    import src.browser_service as bs
    from src import maintenance_service

    api = AccountApi(data_dir=tmp_path / "data")
    google = api.save_account({"account_type": "Google", "account": "track@gmail.com"})["account"]
    assert google["browser_open_count"] == 0
    assert google["browser_last_opened_at"] == ""

    # Both Chrome and Edge exist, and the global default is ``inpage_edge``.
    monkeypatch.setattr(bs, "find_chromium_executable", lambda: "C:\\fake\\chrome.exe")
    monkeypatch.delenv("KEYVAULT_BROWSER_PATH", raising=False)
    monkeypatch.setattr(bs, "detect_edge_path", lambda: "C:\\fake\\msedge.exe")
    monkeypatch.setattr(bs, "detect_chrome_path", lambda: "C:\\fake\\chrome.exe")
    cdp_calls: list[tuple] = []
    monkeypatch.setattr(bs, "_start_cdp_injector", lambda *a, **kw: cdp_calls.append((a, kw)))
    shown: list[tuple] = []
    api._show_login_tools = lambda account_id, profile, *, launching=False: shown.append(
        (account_id, profile, launching)
    )

    class _Running:
        pid = 0

        def wait(self, timeout):
            import subprocess

            raise subprocess.TimeoutExpired("chrome", timeout)

    monkeypatch.setattr(bs.subprocess, "Popen", lambda *a, **kw: _Running())

    # Google always forces the PurpleShield pure-native Chrome flow, even though
    # the global engine is ``inpage_edge``: no toolbar, no CDP, external strip.
    launched = api.launch_browser(google["id"])
    assert launched["ok"] is True
    assert launched["inpage_toolbar"] is False
    assert launched["pure_native"] is True
    assert launched["browser_engine_mode"] == "pure_native"
    assert launched["engine"] == "Chrome"
    assert launched["zero_cdp"] is True
    assert cdp_calls == []
    assert shown == [(google["id"], tmp_path / "data" / "browsers" / f"acc_{google['id']}", True)]
    assert launched["account"]["browser_open_count"] == 1
    assert launched["account"]["browser_last_opened_at"] != ""

    # An explicit target_url (security page / validation / appeal) still spawns
    # the external WinForms pilot strip for Google accounts.
    shown.clear()
    with_url = api.launch_browser(google["id"], target_url="https://myaccount.google.com/security")
    assert with_url["ok"] is True
    assert with_url["pure_native"] is True
    assert shown == [(google["id"], tmp_path / "data" / "browsers" / f"acc_{google['id']}", True)]

    # Non-Google accounts keep the in-page Edge toolbar and never spawn the
    # external strip ("other platforms stay untouched").
    chatgpt = api.save_account({"account_type": "ChatGPT", "account": "chat_track@example.test"})["account"]
    shown.clear()
    launched_chat = api.launch_browser(chatgpt["id"])
    assert launched_chat["ok"] is True
    assert launched_chat["inpage_toolbar"] is True
    assert launched_chat["pure_native"] is False
    assert launched_chat["browser_engine_mode"] == "inpage_edge"
    assert launched_chat["engine"] == "Edge"
    assert launched_chat["zero_cdp"] is True
    assert cdp_calls == []
    assert shown == []

    # Google reset_rejected_session is now supported when unlocked
    p_dir = tmp_path / "data" / "browsers" / f"acc_{google['id']}"
    (p_dir / "DevToolsActivePort").write_text("9222\n/devtools", encoding="utf-8")
    reset = api.reset_rejected_session(google["id"])
    assert reset["ok"] is True
    assert "DevToolsActivePort" in reset["removed"]

    # Probe live validation falls back gracefully when no RT, and extracts validationUrl when RT returns VALIDATION_REQUIRED
    fallback_val = api.probe_live_validation(google["id"])
    assert fallback_val["ok"] is True
    assert fallback_val["status"] == "security_checkup"
    assert "security-checkup" in fallback_val["target_url"]

    api.save_account_rt(google["id"], "1//09abcdef_test_refresh_token")
    monkeypatch.setattr(
        maintenance_service,
        "probe_antigravity_rt",
        lambda acc, proxy_url="", timeout=15.0: {
            "ok": False,
            "gcp_status": "validation",
            "validation_url": "https://accounts.google.com/v3/signin/continue?plt=live123",
        },
    )
    live_val = api.probe_live_validation(google["id"])
    assert live_val["ok"] is True
    assert live_val["status"] == "live_validation_url"
    assert live_val["target_url"] == "https://accounts.google.com/v3/signin/continue?plt=live123"


def test_api_startup_purges_only_google_legacy_extensions_and_keeps_cookies(tmp_path: Path):
    import sqlite3

    import src.browser_service as bs

    data_dir = tmp_path / "data"
    first = AccountApi(data_dir=data_dir)
    google = first.save_account({"account_type": "Google", "account": "startup_google@gmail.test"})["account"]
    chatgpt = first.save_account({"account_type": "ChatGPT", "account": "startup_chat@example.test"})["account"]

    google_profile = data_dir / "browsers" / f"acc_{google['id']}"
    chat_profile = data_dir / "browsers" / f"acc_{chatgpt['id']}"
    bs.prepare_profile_dir(data_dir, google)
    bs.prepare_profile_dir(data_dir, chatgpt)
    bs._build_extension_files(
        google_profile / "fp_ext",
        bs.generate_fingerprint_config(google["id"], google["account"]),
        google,
    )
    bs._build_extension_files(
        chat_profile / "fp_ext",
        bs.generate_fingerprint_config(chatgpt["id"], chatgpt["account"]),
        chatgpt,
    )
    (google_profile / "DevToolsActivePort").write_text("9222\n/devtools", encoding="utf-8")
    net_dir = google_profile / "Default" / "Network"
    net_dir.mkdir(parents=True, exist_ok=True)
    cookies_db = net_dir / "Cookies"
    conn = sqlite3.connect(cookies_db)
    try:
        conn.execute("CREATE TABLE cookies (name TEXT)")
        conn.execute("INSERT INTO cookies (name) VALUES ('SID')")
        conn.commit()
    finally:
        conn.close()

    # A fresh AccountApi startup sweep cleans Google leftovers only.
    AccountApi(data_dir=data_dir)
    assert not (google_profile / "fp_ext").exists()
    assert not (google_profile / "DevToolsActivePort").exists()
    assert cookies_db.exists()
    assert (chat_profile / "fp_ext" / "autologin.js").exists()


def test_google_risk_status_and_warming_reset_clear_automation_markers_only(tmp_path: Path):
    api = AccountApi(data_dir=tmp_path / "data")
    google = api.save_account({"account_type": "Google", "account": "markers@gmail.test"})["account"]
    chatgpt = api.save_account({"account_type": "ChatGPT", "account": "markers_chat@example.test"})["account"]

    google_profile = tmp_path / "data" / "browsers" / f"acc_{google['id']}"
    chat_profile = tmp_path / "data" / "browsers" / f"acc_{chatgpt['id']}"
    for profile in (google_profile, chat_profile):
        (profile / "Default").mkdir(parents=True, exist_ok=True)
        (profile / "Default" / "Cookies").write_bytes(b"session")

    for status in ("人机", "短信", "验证", "封号"):
        (google_profile / "DevToolsActivePort").write_text("9222\n/devtools", encoding="utf-8")
        (chat_profile / "DevToolsActivePort").write_text("9222\n/devtools", encoding="utf-8")
        assert api.set_health_status(google["id"], status)["ok"] is True
        assert api.set_health_status(chatgpt["id"], status)["ok"] is True
        assert not (google_profile / "DevToolsActivePort").exists()
        # Non-Google accounts keep their session markers untouched.
        assert (chat_profile / "DevToolsActivePort").exists()
        assert (google_profile / "Default" / "Cookies").exists()

    (google_profile / "DevToolsActivePort").write_text("9222\n/devtools", encoding="utf-8")
    (chat_profile / "DevToolsActivePort").write_text("9222\n/devtools", encoding="utf-8")
    assert api.reset_warming_time(google["id"], scan_qr_mode=True)["ok"] is True
    assert api.reset_warming_time(chatgpt["id"], scan_qr_mode=True)["ok"] is True
    assert not (google_profile / "DevToolsActivePort").exists()
    assert (chat_profile / "DevToolsActivePort").exists()
    assert (google_profile / "Default" / "Cookies").exists()


def test_handle_dispatched_url_routes_auth_and_forwards_non_auth(tmp_path: Path, monkeypatch):
    import src.api as api_module

    api = AccountApi(data_dir=tmp_path / "data")
    g_acc = api.save_account({"account_type": "Google", "account": "dispatch@gmail.com"})["account"]
    c_acc = api.save_account({"account_type": "Claude", "account": "claude_dispatch@example.com"})["account"]

    forwarded: list[str] = []
    monkeypatch.setattr(api_module, "forward_to_original_browser", lambda u: forwarded.append(u) or True)
    res_non = api.handle_dispatched_url("https://news.ycombinator.com/")
    assert res_non["ok"] is True and res_non["forwarded"] is True
    assert forwarded == ["https://news.ycombinator.com/"]

    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        api,
        "launch_browser",
        lambda acc_id, target_url="", pure_native=None: calls.append((acc_id, target_url)) or {"ok": True},
    )
    res_claude = api.handle_dispatched_url("https://claude.ai/oauth/authorize?code=1")
    assert res_claude["ok"] is True and res_claude["dispatched_account_id"] == c_acc["id"]
    res_google = api.handle_dispatched_url("https://accounts.google.com/v3/signin/continue?plt=1")
    assert res_google["ok"] is True and res_google["dispatched_account_id"] == g_acc["id"]
    assert calls == [
        (c_acc["id"], "https://claude.ai/oauth/authorize?code=1"),
        (g_acc["id"], "https://accounts.google.com/v3/signin/continue?plt=1"),
    ]


def test_api_settings_atomic_validation_hud_notify_and_bounded_shutdown(tmp_path: Path, monkeypatch):
    api = AccountApi(data_dir=tmp_path / "data", base_dir=tmp_path)
    saved = api.save_settings({
        "theme": "invalid_theme",
        "totp_offset_seconds": "15",
        "browser_engine_mode": "pure_native",
    })
    assert saved["theme"] == "dark"
    assert saved["totp_offset_seconds"] == 15
    assert saved["browser_engine_mode"] == "pure_native"

    notified: list[int] = []
    class _FakePanel:
        def notify_account_updated(self, acc):
            notified.append(int(acc["id"]))
    api._login_tools_panel = _FakePanel()

    acc = api.save_account({"account_type": "Google", "account": "hud@gmail.com", "password": "pw"})["account"]
    api.set_health_status(acc["id"], "可用")
    api.bind_proxy_node(acc["id"], "http://127.0.0.1:7890", "HK-1", locked=True)
    api.set_node_lock(acc["id"], False)
    assert len(notified) == 4

    seen_timeouts: list[float] = []
    monkeypatch.setattr(api._workbuddy, "close", lambda timeout=5.0: seen_timeouts.append(timeout))
    api._shutdown()
    assert seen_timeouts == [5.0]


def test_rt_probe_classification_custom_secret_and_proxy_specs(tmp_path: Path, monkeypatch):
    from src import maintenance_service as ms

    # Colon in password & SOCKS guard
    spec = ms.parse_proxy_spec("127.0.0.1:8080:myuser:p@ss:with:colons")
    assert spec["user"] == "myuser"
    assert spec["password"] == "p@ss:with:colons"
    socks_stat, socks_body = ms._http_json_request("https://example.com", proxy_url="socks5://127.0.0.1:1080")
    assert socks_stat == 0 and socks_body["error"] == "unsupported_proxy_scheme"

    # Custom client_id keeps its own client_secret instead of overwriting with ANTIGRAVITY_CLIENT_SECRET
    custom = ms.inspect_rt_json('{"refresh_token":"1//custom","client_id":"other-client","client_secret":"other-sec"}')
    assert custom["ok"] and custom["client_id"] == "other-client" and custom["client_secret"] == "other-sec"
    no_sec = ms.inspect_rt_json('{"refresh_token":"1//custom","client_id":"other-client"}')
    assert no_sec["ok"] and no_sec["client_secret"] == ""

    # 503 on token refresh preserves prior rt_status instead of overwriting with invalid_grant
    acc = {"account": "probe@gmail.com", "custom_fields": {"rt_json": "1//tok", "rt_status": "active", "gcp_status": "active"}}
    monkeypatch.setattr(ms, "_http_json_request", lambda *_a, **_kw: (503, {"error": "temporarily_unavailable"}))
    transient = ms.probe_antigravity_rt(acc)
    assert transient["ok"] is False and transient["transient"] is True and transient["rt_status"] == "active"

    # Generic 403 -> suspended unless secondary probe yields validationUrl -> validation
    def fake_403_suspended(url, **_kw):
        if url.endswith("/token"):
            return 200, {"access_token": "at_1", "expires_in": 3600}
        return 403, {"error": {"status": "PERMISSION_DENIED", "message": "Account disabled"}}

    monkeypatch.setattr(ms, "_http_json_request", fake_403_suspended)
    res_susp = ms.probe_antigravity_rt(acc)
    assert res_susp["rt_status"] == "suspended" and res_susp["gcp_status"] == "suspended"

    def fake_403_with_secondary_validation(url, **_kw):
        if url.endswith("/token"):
            return 200, {"access_token": "at_1", "expires_in": 3600}
        if "streamGenerateContent" in url:
            return 403, {"validationUrl": "https://accounts.google.com/v3/signin/continue?plt=val"}
        return 403, {"error": {"status": "PERMISSION_DENIED", "message": "Restricted"}}

    monkeypatch.setattr(ms, "_http_json_request", fake_403_with_secondary_validation)
    res_val = ms.probe_antigravity_rt(acc)
    assert res_val["gcp_status"] == "validation"
    assert res_val["validation_url"] == "https://accounts.google.com/v3/signin/continue?plt=val"

    # inject_rt_to_antigravity_client rejects missing APPDATA\Antigravity directory
    missing_app = tmp_path / "no_appdata"
    monkeypatch.setenv("APPDATA", str(missing_app))
    inj_missing = ms.inject_rt_to_antigravity_client(acc)
    assert inj_missing["ok"] is False and "未找到" in inj_missing["error"]


def test_oauth_loopback_escapes_html_preempts_and_adds_hl(monkeypatch):
    import urllib.request
    from src import maintenance_service as ms

    mgr = ms.OAuthLoopbackManager()
    s1 = mgr.start_session(1, login_hint="a@gmail.com", hl="en-US")
    assert s1["ok"] and "&hl=en-US" in s1["auth_url"]

    # Starting s2 preempts s1 and verifies reflected ?error= is HTML-escaped on callback page
    s2 = mgr.start_session(2, login_hint="b@gmail.com", hl="ja-JP", preempt=True)
    assert s2["ok"] and "&hl=ja-JP" in s2["auth_url"]
    assert mgr.get_status(s1["session_id"])["status"] == "cancelled"

    cb_url = s2["redirect_uri"] + "?error=%3Cscript%3Ealert(1)%3C/script%3E"
    try:
        urllib.request.urlopen(cb_url, timeout=3.0)
        pytest.fail("Expected HTTP 400 on OAuth error callback")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
        body = exc.read().decode("utf-8", errors="replace")
        assert "<script>" not in body
        assert "&lt;script&gt;" in body
    finally:
        mgr.shutdown()
