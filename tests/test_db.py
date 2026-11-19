from pathlib import Path

import pytest

from src.db_service import DatabaseService, compute_usage_info


def test_db_crud_and_export(tmp_path: Path):
    db_file = tmp_path / "data" / "accounts.db"
    svc = DatabaseService(db_file)

    acc1 = svc.add_account(
        {
            "account_type": "ChatGPT",
            "account": "alice_demo01@example.com",
            "password": "DemoPass#2026!",
            "secret_key": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
        }
    )
    assert acc1["id"] > 0
    assert acc1["masked_account"] == "ali***01@example.com"
    assert acc1["masked_password"] == "••••••••"
    assert acc1["masked_secret"] == "JBSW****3PXP"
    assert len(acc1["totp"]["code"]) == 6
    assert acc1["device_tag"] == "主机"

    acc2 = svc.add_account(
        {
            "account_type": "Google",
            "account": "bob_google99@gmail.com",
            "password": "GoogleDemo#888",
            "recovery_email": "recovery_demo@example.org",
            "secret_key": "gezdgnbvgy3tqojqgezdgnbvgy3tqojq",
            "reg_year": "2024",
            "reg_region": "US",
            "device_tag": "iPhone",
        }
    )
    assert acc2["device_tag"] == "iPhone"

    all_items = svc.list_accounts()
    assert len(all_items) == 2

    google_only = svc.list_accounts(account_type="Google")
    assert len(google_only) == 1
    assert google_only[0]["account"] == "bob_google99@gmail.com"

    iphone_only = svc.list_accounts(device_tag="iPhone")
    assert len(iphone_only) == 1
    assert iphone_only[0]["account"] == "bob_google99@gmail.com"

    exported = svc.export_accounts_text()
    assert "alice_demo01@example.com----DemoPass#2026!----JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP" in exported
    assert "bob_google99@gmail.com----GoogleDemo#888----recovery_demo@example.org----GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ----2024----US" in exported

    svc.delete_account(acc1["id"])
    assert len(svc.list_accounts()) == 1


def test_usage_duration_and_twofa_modified_toggle(tmp_path: Path):
    u_deact = compute_usage_info("2026-10-19", "2026-10-29", today_str="2026-10-30")
    assert u_deact["status"] == "deactivated"
    assert u_deact["status_label"] == "已停用"
    assert u_deact["days"] == 10
    assert u_deact["duration_text"] == "共使用 10 天"

    u_act = compute_usage_info("2026-10-21", "", today_str="2026-10-30")
    assert u_act["status"] == "active"
    assert u_act["status_label"] == "使用中"
    assert u_act["days"] == 9
    assert u_act["duration_text"] == "已使用 9 天"

    db_file = tmp_path / "data" / "accounts.db"
    svc = DatabaseService(db_file)
    acc = svc.add_account(
        {
            "account_type": "ChatGPT",
            "account": "test_user@example.com",
            "password": "secretPassword",
            "initial_secret_key": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
            "secret_key": "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
            "twofa_modified": 1,
            "purchase_date": "2026-10-19",
            "deactivated_date": "2026-10-29",
        }
    )
    assert acc["twofa_modified"] == 1
    assert acc["masked_initial_secret"] == "JBSW****3PXP"
    assert acc["masked_secret"] == "GEZD****QOJQ"
    assert acc["usage"]["status"] == "deactivated"
    assert acc["usage"]["days"] == 10

    toggled = svc.set_twofa_modified(acc["id"], 0)
    assert toggled["twofa_modified"] == 0

    assert acc["password_modified"] == 0
    pwd_toggled = svc.set_password_modified(acc["id"], 1)
    assert pwd_toggled["password_modified"] == 1
    pwd_toggled_back = svc.set_password_modified(acc["id"], 0)
    assert pwd_toggled_back["password_modified"] == 0

    updated = svc.update_account(
        acc["id"],
        {
            "account_type": "ChatGPT",
            "account": "test_user@example.com",
            "password": "brandNewPassword999",
        },
    )
    assert updated["password_modified"] == 1


def test_account_health_status_and_filtering(tmp_path: Path):
    db_file = tmp_path / "data" / "accounts.db"
    svc = DatabaseService(db_file)

    a1 = svc.add_account({
        "account_type": "ChatGPT",
        "account": "normal@chatgpt.com",
        "password": "Password123!",
    })
    assert a1["health_status"] == "可用"

    a2 = svc.add_account({
        "account_type": "ChatGPT",
        "account": "banned@chatgpt.com",
        "password": "Password123!",
        "health_status": "封号",
    })
    assert a2["health_status"] == "封号"

    a3 = svc.add_account({
        "account_type": "Google",
        "account": "captcha@gmail.com",
        "password": "Password123!",
        "health_status": "人机",
    })
    assert a3["health_status"] == "人机"

    up1 = svc.set_health_status(a1["id"], "短信")
    assert up1["health_status"] == "短信"

    up1_back = svc.set_health_status(a1["id"], "可用")
    assert up1_back["health_status"] == "可用"

    availables = svc.list_accounts(health_status="可用")
    assert len(availables) == 1
    assert availables[0]["account"] == "normal@chatgpt.com"

    banned_list = svc.list_accounts(health_status="封号")
    assert len(banned_list) == 1
    assert banned_list[0]["account"] == "banned@chatgpt.com"

    captcha_list = svc.list_accounts(health_status="人机")
    assert len(captcha_list) == 1
    assert captcha_list[0]["account"] == "captcha@gmail.com"

    stats = svc.get_stats()
    assert stats["total"] == 3
    assert stats["available"] == 1
    assert stats["unavailable"] == 2
    assert stats["health"]["可用"] == 1
    assert stats["health"]["封号"] == 1
    assert stats["health"]["人机"] == 1
    assert stats["health"]["短信"] == 0

    a4 = svc.add_account({
        "account_type": "ChatGPT",
        "account": "deactivated_acc@example.com",
        "password": "Password123!",
        "health_status": "可用",
        "deactivated_date": "2026-10-25",
    })
    stats2 = svc.get_stats()
    assert stats2["total"] == 4
    assert stats2["available"] == 1
    assert stats2["unavailable"] == 3
    assert stats2["health"]["可用"] == 1
    assert stats2["health"]["不可用"] == 3

    avail_filtered = svc.list_accounts(health_status="可用")
    assert len(avail_filtered) == 1
    assert avail_filtered[0]["account"] == "normal@chatgpt.com"

    unavail_filtered = svc.list_accounts(health_status="不可用")
    unavail_accounts = [a["account"] for a in unavail_filtered]
    assert "deactivated_acc@example.com" in unavail_accounts
    assert len(unavail_filtered) == 3


def test_antigravity_tier_and_filtering(tmp_path: Path):
    db_file = tmp_path / "data" / "accounts.db"
    svc = DatabaseService(db_file)

    a1 = svc.add_account({
        "account_type": "Google",
        "account": "free_user@gmail.com",
        "password": "Password123!",
    })
    assert a1["antigravity_tier"] == "无"

    a2 = svc.add_account({
        "account_type": "Google",
        "account": "blue_pro@gmail.com",
        "password": "Password123!",
        "antigravity_tier": "蓝色Pro",
    })
    assert a2["antigravity_tier"] == "蓝色Pro"

    a3 = svc.add_account({
        "account_type": "Google",
        "account": "gold_pro@gmail.com",
        "password": "Password123!",
        "antigravity_tier": "金色背景色Pro",
    })
    assert a3["antigravity_tier"] == "金色Pro"

    up1 = svc.set_antigravity_tier(a1["id"], "蓝色Pro")
    assert up1["antigravity_tier"] == "蓝色Pro"

    up1_back = svc.set_antigravity_tier(a1["id"], "无")
    assert up1_back["antigravity_tier"] == "无"

    blue_list = svc.list_accounts(antigravity_tier="蓝色Pro")
    assert len(blue_list) == 1
    assert blue_list[0]["account"] == "blue_pro@gmail.com"

    gold_list = svc.list_accounts(antigravity_tier="金色Pro")
    assert len(gold_list) == 1
    assert gold_list[0]["account"] == "gold_pro@gmail.com"

    none_list = svc.list_accounts(antigravity_tier="无")
    assert len(none_list) == 1
    assert none_list[0]["account"] == "free_user@gmail.com"

    stats = svc.get_stats()
    assert stats["antigravity"]["无"] == 1
    assert stats["antigravity"]["蓝色Pro"] == 1
    assert stats["antigravity"]["金色Pro"] == 1


def test_device_tags_and_system_dicts(tmp_path: Path):
    db_file = tmp_path / "data" / "accounts.db"
    svc = DatabaseService(db_file)

    dicts = svc.list_system_dicts()
    types = [d for d in dicts if d["dict_type"] == "account_type"]
    devices = [d for d in dicts if d["dict_type"] == "device_tag"]
    assert any(d["dict_key"] == "WorkBuddy" for d in types)
    assert any(d["dict_key"] == "Qcoder" for d in types)
    assert any(d["dict_key"] == "主机" for d in devices)
    assert any(d["dict_key"] == "iPhone" for d in devices)
    assert any(d["dict_key"] == "Pad" for d in devices)

    # Claude is a built-in system account type, pre-seeded on every DB init.
    claude_dict = [d for d in types if d["dict_key"] == "Claude"][0]
    assert claude_dict["is_system"] == 1
    assert claude_dict["icon"] == "✴️"
    assert claude_dict["dict_label"] == "Claude"

    a1 = svc.add_account({
        "account_type": "WorkBuddy",
        "account": "wb_user@workbuddy.ai",
        "password": "Password123",
        "device_tag": "主机",
    })
    assert a1["account_type"] == "WorkBuddy"
    assert a1["device_tag"] == "主机"

    up = svc.set_device_tag(a1["id"], "Pad")
    assert up["device_tag"] == "Pad"

    pad_list = svc.list_accounts(device_tag="Pad")
    assert len(pad_list) == 1
    assert pad_list[0]["account"] == "wb_user@workbuddy.ai"

    wb_list = svc.list_accounts(account_type="WorkBuddy")
    assert len(wb_list) == 1

    save_res = svc.save_system_dict({
        "dict_type": "account_type",
        "dict_key": "Perplexity",
        "dict_label": "Perplexity",
        "color_class": "perplexity",
        "icon": "🤖",
        "sort_order": 10,
    })
    assert save_res["ok"] is True
    assert save_res["dict"]["dict_key"] == "Perplexity"

    del_wb = [d for d in svc.list_system_dicts("account_type") if d["dict_key"] == "WorkBuddy"][0]
    del_wb_res = svc.delete_system_dict(del_wb["id"])
    assert del_wb_res["ok"] is False
    assert "正在使用" in del_wb_res["error"]

    del_perplexity = [
        d for d in svc.list_system_dicts("account_type") if d["dict_key"] == "Perplexity"
    ][0]
    del_perplexity_res = svc.delete_system_dict(del_perplexity["id"])
    assert del_perplexity_res["ok"] is True

    # Built-in system dictionaries can never be deleted, even when unused.
    del_claude_res = svc.delete_system_dict(claude_dict["id"])
    assert del_claude_res["ok"] is False
    assert "系统" in del_claude_res["error"]


def test_attributes_and_custom_fields(tmp_path: Path):
    db_file = tmp_path / "data" / "accounts.db"
    svc = DatabaseService(db_file)

    # 1. Default seeded system attributes
    attrs = svc.list_attributes()
    assert len(attrs) >= 3
    attr_keys = {a["attr_key"]: a for a in attrs}
    assert "health_status" in attr_keys
    assert "device_tag" in attr_keys
    assert "antigravity_tier" in attr_keys

    health_attr = attr_keys["health_status"]
    assert health_attr["attr_type"] == "select"
    assert health_attr["bound_platforms"] == "All"
    assert health_attr["is_system"] == 1
    assert health_attr["is_filter"] == 1

    device_attr = attr_keys["device_tag"]
    assert device_attr["attr_type"] == "select"
    assert "Google" in device_attr["bound_platforms"]
    assert device_attr["is_system"] == 1

    tier_attr = attr_keys["antigravity_tier"]
    assert tier_attr["attr_type"] == "select"
    assert "Google" in tier_attr["bound_platforms"]
    assert tier_attr["is_system"] == 1

    # 2. Add custom attribute
    save_res = svc.save_attribute({
        "attr_key": "org_team",
        "attr_label": "所属团队",
        "attr_type": "text",
        "bound_platforms": ["WorkBuddy"],
        "sort_order": 50,
        "is_system": 0,
    })
    assert save_res["ok"] is True
    created_id = save_res["attribute"]["id"]

    # 3. Update attribute bindings
    bind_res = svc.update_attribute_bindings(device_attr["id"], ["Google", "WorkBuddy"])
    assert bind_res["ok"] is True
    assert "WorkBuddy" in bind_res["attribute"]["bound_platforms"]

    # 4. Delete attribute
    del_sys_res = svc.delete_attribute(health_attr["id"])
    assert del_sys_res["ok"] is False
    assert "系统内置" in del_sys_res["error"]

    del_custom_res = svc.delete_attribute(created_id)
    assert del_custom_res["ok"] is True
    assert not any(a["attr_key"] == "org_team" for a in svc.list_attributes())

    # 5. Account custom fields persistence
    acc = svc.add_account({
        "account_type": "WorkBuddy",
        "account": "custom_team@workbuddy.ai",
        "password": "Password123",
        "custom_fields": {"project_id": "proj_999"},
    })
    assert acc["custom_fields"].get("project_id") == "proj_999"

    loaded = [a for a in svc.list_accounts() if a["id"] == acc["id"]][0]
    assert loaded["custom_fields"].get("project_id") == "proj_999"

    updated = svc.update_account(acc["id"], {
        "custom_fields": {"project_id": "proj_888", "role": "admin"},
    })
    assert updated["custom_fields"].get("project_id") == "proj_888"
    assert updated["custom_fields"].get("role") == "admin"

    # 6. Default dict_type for select attributes
    save_sel = svc.save_attribute({
        "attr_key": "team_role",
        "attr_label": "角色",
        "attr_type": "select",
        "bound_platforms": ["WorkBuddy"],
    })
    assert save_sel["ok"] is True
    assert save_sel["attribute"]["dict_type"] == "team_role"

    # 7. attr_key format validation
    bad_key_res = svc.save_attribute({
        "attr_key": "123 bad-key!",
        "attr_label": "非法键",
    })
    assert bad_key_res["ok"] is False

    # 8. custom_fields searchable
    search_res = svc.list_accounts(search="proj_888")
    assert len(search_res) == 1
    assert search_res[0]["account"] == "custom_team@workbuddy.ai"


def test_security_checklist_and_credential_history_archiving(tmp_path: Path):
    svc = DatabaseService(tmp_path / "data" / "accounts.db")
    acc = svc.add_account({
        "account_type": "Google",
        "account": "sec.user@gmail.com",
        "password": "OldPassword!1",
        "recovery_email": "old.rec@example.com",
        "secret_key": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
        "recovery_password": "OldRecPwd!1",
    })
    assert acc["security"]["status"] == "unmodified"
    assert acc["security"]["completed"] == 0

    partial = svc.quick_update_credentials(acc["id"], {
        "google_confirmed": True,
        "password": "NewStrongPassword#2026",
        "password_modified": True,
        "sec_recovery_modified": True,
        "recovery_email": "new.rec@example.com",
        "recovery_password": "NewRecPwd#2",
    })
    assert partial["security"]["status"] == "partial"
    assert partial["security"]["completed"] == 2
    assert partial["recovery_password"] == "NewRecPwd#2"

    secured = svc.quick_update_credentials(acc["id"], {
        "google_confirmed": True,
        "secret_key": "MFRGGZDFMZTWQ2LKMFRGGZDFMZTWQ2LK",
        "twofa_modified": True,
        "sec_devices_cleared": True,
    })
    assert secured["security"]["status"] == "secured"
    assert secured["security"]["completed"] == 4
    assert secured["initial_secret_key"] == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

    history = svc.get_credential_history(acc["id"])
    assert len(history) == 2
    assert history[0]["old_secret_key"] == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    assert history[1]["old_password"] == "OldPassword!1"
    assert history[1]["old_recovery_email"] == "old.rec@example.com"
    assert history[1]["old_recovery_password"] == "OldRecPwd!1"


def test_warming_lifecycle_and_risk_rewarm_reset(tmp_path: Path):
    from datetime import date, timedelta
    from src.db_service import compute_warming_info

    svc = DatabaseService(tmp_path / "data" / "accounts.db")
    today = date.today()
    two_days_ago = (today - timedelta(days=2)).strftime("%Y-%m-%d")
    ten_days_ago = (today - timedelta(days=10)).strftime("%Y-%m-%d")

    w_new = compute_warming_info("", two_days_ago, "可用", {}, today_dt=today)
    assert w_new["phase"] == "warming"
    assert w_new["days_remaining"] == 5
    assert w_new["ready"] is False

    w_ready = compute_warming_info("", ten_days_ago, "可用", {}, today_dt=today)
    assert w_ready["phase"] == "ready"
    assert w_ready["ready"] is True

    acc = svc.add_account({
        "account_type": "Google",
        "account": "warm_test@gmail.com",
        "password": "pw",
        "purchase_date": ten_days_ago,
    })
    assert acc["warming"]["phase"] == "ready"

    risk_acc = svc.set_health_status(acc["id"], "人机")
    assert risk_acc["warming"]["phase"] == "rewarm"
    assert risk_acc["custom_fields"]["warming_reset_at"] == today.strftime("%Y-%m-%d")

    svc.set_health_status(acc["id"], "可用")
    scan_acc = svc.reset_warming_time(acc["id"], scan_qr_mode=True, reset_date=ten_days_ago)
    assert scan_acc["warming"]["phase"] == "scan_qr"
    assert scan_acc["warming"]["target_days"] == 30
    assert scan_acc["warming"]["days_remaining"] == 20


def test_google_health_review_and_reminder_are_independent(tmp_path: Path):
    from datetime import date, datetime, timedelta

    svc = DatabaseService(tmp_path / "accounts.db")
    purchase_date = (date.today() - timedelta(days=31)).isoformat()
    account = svc.add_account({
        "account_type": "Google",
        "account": "review@example.com",
        "purchase_date": purchase_date,
    })
    account_id = account["id"]
    assert "google_reviewed_at" not in account["custom_fields"]
    assert account["warming"]["label"] == "待复核"

    with pytest.raises(ValueError, match="核对状态"):
        svc.set_health_status(account_id, "invalid")
    assert "google_reviewed_at" not in svc.get_account(account_id)["custom_fields"]

    for status, label in (("封号", "封号"), ("不可用", "不可用"), ("短信", "待验证（短信）")):
        reviewed = svc.set_health_status(account_id, status)
        reviewed_at = datetime.fromisoformat(reviewed["custom_fields"]["google_reviewed_at"])
        assert reviewed_at.utcoffset() == timedelta(0)
        updated = svc.reset_warming_time(account_id, scan_qr_mode=True, reset_date=purchase_date)
        assert updated["health_status"] == status
        assert updated["warming"]["ready"] is True
        assert updated["warming"]["label"] == label
        assert updated["custom_fields"]["google_reviewed_at"] == reviewed_at.isoformat()

    confirmed = svc.set_health_status(account_id, "可用")
    assert confirmed["health_status"] == "可用"
    assert confirmed["warming"]["target_days"] == 30
    assert confirmed["warming"]["label"] == "待复核"
    edited = svc.update_account(account_id, {"account": "another@example.com"})
    assert "google_reviewed_at" not in edited["custom_fields"]


def test_google_quick_update_requires_confirmation_and_archives_once(tmp_path: Path):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor

    svc = DatabaseService(tmp_path / "accounts.db")
    old_secret = "JBSWY3DPEHPK3PXP"
    account = svc.add_account({
        "account_type": "Google",
        "account": "credentials@example.com",
        "password": "old-password",
        "recovery_email": "old-recovery@example.com",
        "recovery_password": "old-recovery-password",
        "secret_key": old_secret,
    })
    account_id = account["id"]
    assert svc.quick_update_credentials(account_id, {"note": "reviewed locally"})["note"] == "reviewed locally"

    for changes in (
        {"password": "new-password"},
        {"secret_key": "MFRGGZDFMZTWQ2LK"},
        {"recovery_email": "new-recovery@example.com"},
        {"recovery_password": "new-recovery-password"},
        {"password_modified": True},
        {"twofa_modified": True},
        {"sec_recovery_modified": True},
        {"sec_devices_cleared": True},
    ):
        with pytest.raises(ValueError, match="确认"):
            svc.quick_update_credentials(account_id, {"note": "unsaved", "google_confirmed": "true", **changes})
    unchanged = svc.get_account(account_id)
    assert unchanged["note"] == "reviewed locally"
    assert unchanged["password"] == "old-password"
    assert unchanged["security"]["completed"] == 0
    assert svc.get_credential_history(account_id) == []

    changes = {"google_confirmed": True, "password": "new-password", "password_modified": True}
    with svc._connect() as conn:
        conn.execute("""
            CREATE TRIGGER reject_update BEFORE UPDATE ON accounts
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="simulated failure"):
        svc.quick_update_credentials(account_id, changes)
    assert svc.get_account(account_id)["password"] == "old-password"
    assert svc.get_credential_history(account_id) == []
    with svc._connect() as conn:
        conn.execute("DROP TRIGGER reject_update")

    with ThreadPoolExecutor(max_workers=2) as workers:
        saves = list(workers.map(lambda _: svc.quick_update_credentials(account_id, changes), range(2)))
    for saved in saves:
        assert saved["password"] == "new-password"
        assert saved["password_modified"] == 1
    assert len(svc.get_credential_history(account_id)) == 1


def test_google_import_and_local_correction_do_not_mark_credentials_changed(tmp_path: Path):
    svc = DatabaseService(tmp_path / "accounts.db")
    svc.batch_import_accounts([{
        "account_type": "Google",
        "account": "import@example.com",
        "password": "original",
        "initial_secret_key": "JBSWY3DPEHPK3PXP",
        "secret_key": "MFRGGZDFMZTWQ2LK",
        "twofa_modified": 1,
    }])
    account = svc.list_accounts(account_type="Google")[0]
    assert account["twofa_modified"] == 0
    assert account["totp"]["valid"] is True
    corrected = svc.update_account(account["id"], {
        "password": "local-correction",
        "secret_key": "GEZDGNBVGY3TQOJQ",
    })
    assert corrected["password_modified"] == 0
    assert corrected["twofa_modified"] == 0
    assert corrected["secret_key"] == "GEZDGNBVGY3TQOJQ"
    assert corrected["totp"]["valid"] is True


def test_batch_import_duplicate_strategies_and_top_recovery(tmp_path: Path):
    svc = DatabaseService(tmp_path / "data" / "accounts.db")
    svc.add_account({
        "account_type": "Google",
        "account": "Alice.Demo@gmail.com",
        "password": "Pass1",
        "recovery_email": "shared_rec@outlook.com",
    })
    svc.add_account({
        "account_type": "Google",
        "account": "other_demo@gmail.com",
        "password": "Pass2",
        "recovery_email": "shared_rec@outlook.com",
    })

    tops = svc.get_top_recovery_emails(5)
    assert tops[0]["email"] == "shared_rec@outlook.com"
    assert tops[0]["count"] == 2

    # Gmail dot normalization matches Alice.Demo@gmail.com -> fill_empty fills empty secret_key
    res_skip = svc.batch_import_accounts(
        [{"account_type": "Google", "account": "alicedemo@gmail.com", "password": "New", "note": "filled"}],
        duplicate_strategy="skip",
    )
    assert res_skip == {"imported": 0, "updated": 0, "skipped": 1}

    res_fill = svc.batch_import_accounts(
        [{"account_type": "Google", "account": "alicedemo@gmail.com", "password": "IgnoredPassword", "note": "filled"}],
        duplicate_strategy="fill_empty",
    )
    assert res_fill == {"imported": 0, "updated": 1, "skipped": 0}
    alice = svc.list_accounts(search="Alice.Demo")[0]
    assert alice["password"] == "Pass1"
    assert alice["note"] == "filled"

    res_over = svc.batch_import_accounts(
        [{"account_type": "Google", "account": "alicedemo@gmail.com", "password": "ReplacedPassword"}],
        duplicate_strategy="overwrite",
    )
    assert res_over == {"imported": 0, "updated": 1, "skipped": 0}
    alice_after = svc.list_accounts(search="Alice.Demo")[0]
    assert alice_after["password"] == "ReplacedPassword"
    assert svc.get_credential_history(alice_after["id"])[0]["old_password"] == "Pass1"


def test_rolling_backups_and_restore(tmp_path: Path):
    svc = DatabaseService(tmp_path / "data" / "accounts.db")
    a1 = svc.add_account({"account_type": "ChatGPT", "account": "keep@example.com", "password": "p1"})
    bak = svc.create_rolling_backup(reason="unit", max_keep=5, min_interval_minutes=0)
    assert bak["ok"] is True

    svc.add_account({"account_type": "ChatGPT", "account": "temp@example.com", "password": "p2"})
    assert len(svc.list_accounts()) == 2

    restore_res = svc.restore_backup(bak["filename"])
    assert restore_res["ok"] is True
    accounts = svc.list_accounts()
    assert len(accounts) == 1
    assert accounts[0]["id"] == a1["id"]
    assert any("pre_restore" in b["filename"] for b in svc.list_backups())


def test_delete_account_cleans_credential_history_and_restore_validates_schema(tmp_path: Path):
    import sqlite3

    svc = DatabaseService(tmp_path / "data" / "accounts.db")
    acc = svc.add_account({"account_type": "ChatGPT", "account": "del@example.com", "password": "old"})
    svc.update_account(acc["id"], {"password": "new"})
    assert len(svc.get_credential_history(acc["id"])) == 1
    assert svc.delete_account(acc["id"]) is True
    assert svc.get_credential_history(acc["id"]) == []

    # Invalid / foreign sqlite backup must be rejected without touching accounts.db
    svc.add_account({"account_type": "ChatGPT", "account": "safe@example.com", "password": "p1"})
    bad_backup = svc.backups_dir / "accounts_bad_schema.db"
    bad_backup.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(bad_backup) as bad_conn:
        bad_conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    res = svc.restore_backup(bad_backup.name)
    assert res["ok"] is False
    assert len(svc.list_accounts()) == 1


def test_allocate_clean_node_and_search_like_escape_and_concurrent_writes(tmp_path: Path):
    import concurrent.futures

    svc = DatabaseService(tmp_path / "data" / "accounts.db")
    pct_acc = svc.add_account({"account_type": "ChatGPT", "account": "100%real_user@example.com"})
    svc.add_account({"account_type": "ChatGPT", "account": "1000normal@example.com"})
    matched = svc.list_accounts(search="100%real")
    assert [a["id"] for a in matched] == [pct_acc["id"]]

    # Concurrent node allocations must serialize cleanly under _write_tx without locked errors
    mail_ids = [
        svc.add_account({"account_type": "域名邮箱", "account": f"m{i}@domain.test"})["id"]
        for i in range(6)
    ]
    nodes = ["NodeA", "NodeB", "NodeC"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(svc.allocate_clean_node_for_account, mid, nodes) for mid in mail_ids]
        assigned = [f.result(timeout=5) for f in futs]
    assert all(n in nodes for n in assigned)
