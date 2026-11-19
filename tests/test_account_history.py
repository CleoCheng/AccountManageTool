import sqlite3
from pathlib import Path

import pytest

from src.api import AccountApi
from src.browser_service import find_chromium_executable


def test_account_edit_history_records_normalized_changes_and_rolls_back(tmp_path):
    api = AccountApi(tmp_path)
    account = api.save_account({
        "account": "first@example.test", "password": "old-value", "note": "old note", "reg_region": "CN",
    })["account"]
    account_id = account["id"]
    assert api.get_account_change_history(account_id)["history"] == []

    edited = api.save_account({
        "id": account_id, "account": account["account"], "note": " edited note ",
        "password": "new-value", "password_modified": 0, "reg_region": "US",
    })
    assert edited["ok"]
    history = api.get_account_change_history(account_id)["history"]
    assert {row["field"]: (row["old_value"], row["new_value"]) for row in history} == {
        "note": ("old note", "edited note"),
        "password": ("old-value", "new-value"),
        "reg_region": ("CN", "US"),
    }
    assert len({row["created_at"] for row in history}) == 1
    assert [row["sensitive"] for row in history if row["field"] == "password"] == [True]
    assert api.save_account({"id": account_id, "account": account["account"], "note": "edited note"})["ok"]
    assert api.get_account_change_history(account_id)["history"] == history

    assert api.save_account({"id": account_id, "account": account["account"], "note": "latest"})["ok"]
    latest = api.get_account_change_history(account_id)["history"]
    assert latest[0]["new_value"] == "latest" and latest[1:] == history
    assert "history" not in api.get_dashboard()["accounts"][0]
    other = api.save_account({"account": "second@example.test"})["account"]
    assert api.get_account_change_history(other["id"])["history"] == []

    with api.db._connect() as conn:
        conn.execute("""
            CREATE TRIGGER fail_change_history BEFORE INSERT ON account_change_history
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="simulated failure"):
        api.save_account({"id": account_id, "account": account["account"], "password": "unsaved"})
    assert api.db.get_account(account_id)["password"] == "new-value"
    assert api.get_account_change_history(account_id)["history"] == latest
    assert len(api.get_credential_history(account_id)["history"]) == 1
    assert api.db.delete_account(account_id)
    assert api.get_account_change_history(account_id)["history"] == []


def test_mailbox_markers_persist_and_are_audited(tmp_path):
    api = AccountApi(tmp_path)
    mailbox = api.save_account({
        "account_type": "域名邮箱", "account": "mail@example.test", "password": " keep spaces ",
        "custom_fields": {"untouched": "some value"},
    })["account"]
    assert mailbox["custom_fields"]["chatgpt_bound"] is False
    account_id = mailbox["id"]
    assert not api.set_domain_email_metadata(account_id, "chatgpt_bound", "true")["ok"]
    assert not api.set_domain_email_metadata(account_id, "wb_reg_status", "未知")["ok"]
    assert not api.set_domain_email_metadata(account_id, "note", "blocked")["ok"]
    other = api.save_account({"account": "other@example.test"})["account"]
    assert not api.set_domain_email_metadata(other["id"], "chatgpt_bound", True)["ok"]
    assert api.set_domain_email_metadata(account_id, "chatgpt_bound", True)["ok"]
    for status in ("进行中", "已完成", "未完成"):
        result = api.set_domain_email_metadata(account_id, "wb_reg_status", status)
        assert result["ok"] and result["account"]["custom_fields"]["wb_reg_status"] == status
        matches = api.get_dashboard("域名邮箱", custom_filters={"wb_reg_status": status})["accounts"]
        assert [item["id"] for item in matches] == [account_id]
    assert api.set_domain_email_metadata(account_id, "wb_reg_status", "未完成")["ok"]
    history = api.get_account_change_history(account_id)["history"]
    assert [(row["field"], row["old_value"], row["new_value"]) for row in history] == [
        ("wb_reg_status", "已完成", "未完成"),
        ("wb_reg_status", "进行中", "已完成"),
        ("wb_reg_status", "未完成", "进行中"),
        ("chatgpt_bound", False, True),
    ]
    assert api.db.get_account(account_id)["password"] == " keep spaces "
    assert api.db.get_account(account_id)["custom_fields"]["untouched"] == "some value"
    api.db.update_workbuddy_progress(account_id, "email", "邮箱验证")
    assert api.db.get_account(account_id)["custom_fields"]["chatgpt_bound"] is True


def test_edit_history_and_domain_markers_in_desktop_ui(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    exe = find_chromium_executable()
    if not exe:
        pytest.skip("Requires an installed Chromium browser")
    api = AccountApi(tmp_path)
    mailbox = api.save_account({
        "account_type": "域名邮箱", "account": "mail@example.test", "password": "synthetic-start",
    })["account"]
    account_id = mailbox["id"]
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=exe, headless=True, args=[
            "--disable-background-networking", "--disable-sync", "--host-resolver-rules=MAP * 127.0.0.1",
        ])
        context = browser.new_context(service_workers="block")
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
        page = context.new_page()
        page.set_default_timeout(5000)
        page.expose_function("__apiCall", lambda method, args: getattr(api, method)(*args))
        page.add_init_script("""
            window.pywebview = {api: new Proxy({}, {
                get: (_, method) => (...args) => window.__apiCall(method, args)
            })};
        """)
        page.goto((Path(__file__).parents[1] / "web" / "index.html").as_uri())
        card = page.locator(f'article.u-card[data-id="{account_id}"]')
        card.get_by_role("button", name="ChatGPT 未绑定").click()
        playwright.expect(card.get_by_role("button", name="ChatGPT 已绑定")).to_have_attribute("aria-pressed", "true")
        card.get_by_role("button", name="未完成注册").click()
        playwright.expect(card.get_by_role("button", name="进行中")).to_be_visible()
        page.reload()
        playwright.expect(card.get_by_role("button", name="ChatGPT 已绑定")).to_be_visible()
        playwright.expect(card.get_by_role("button", name="进行中")).to_be_visible()

        page.evaluate("openEditModal", account_id)
        section = page.locator("#account-edit-history")
        rows = section.locator(".account-edit-history-entry")
        playwright.expect(rows).to_have_count(2)
        playwright.expect(rows.first).to_contain_text("进行中")
        playwright.expect(rows.last).to_contain_text("是否绑定 ChatGPT")
        page.locator("#field-password").fill('synthetic "<new>"')
        page.locator("#field-note").fill("test-note")
        page.locator("#btn-save-account").click()
        playwright.expect(section).to_be_hidden()
        updated_fields = {row["field"] for row in api.get_account_change_history(account_id)["history"][:3]}
        assert updated_fields == {"note", "password", "password_modified"}
        page.evaluate("openEditModal", account_id)
        password_row = rows.filter(has=page.get_by_text("登录密码", exact=True))
        playwright.expect(password_row).to_have_count(1)
        assert 'synthetic "<new>"' not in password_row.inner_text()
        password_row.get_by_role("button", name="显示原值").click()
        playwright.expect(password_row).to_contain_text('synthetic "<new>"')
        playwright.expect(rows.filter(has=page.get_by_text("备注", exact=True))).to_contain_text("test-note")
        page.evaluate("closeAccountModal(); openAddModal()")
        playwright.expect(section).to_be_hidden()
        browser.close()
