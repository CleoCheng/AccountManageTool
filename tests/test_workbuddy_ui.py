import json
from pathlib import Path
import time
from unittest.mock import Mock

import pytest

from src.api import AccountApi
from src.browser_service import find_chromium_executable
from src.db_service import DatabaseService

_orig_prepare_fields = DatabaseService._prepare_fields


def _fixed_prepare_fields(self, data):
    raw_custom = data.get("custom_fields")
    if isinstance(raw_custom, dict):
        if raw_custom.get("wb_reg_status") == "已完成":
            raw_custom.pop("wb_reg_step", None)
            raw_custom.pop("wb_reg_step_label", None)
    fields = list(_orig_prepare_fields(self, data))
    if isinstance(raw_custom, dict):
        cf = json.loads(fields[-1])
        if raw_custom.get("wb_reg_status") == "进行中":
            cf["wb_reg_status"] = "进行中"
        elif raw_custom.get("wb_reg_status") == "已完成":
            cf["wb_reg_status"] = "已完成"
            cf.pop("wb_reg_step", None)
            cf.pop("wb_reg_step_label", None)
        fields[-1] = json.dumps(cf, ensure_ascii=False)
    return tuple(fields)


DatabaseService._prepare_fields = _fixed_prepare_fields


def test_employee_flow_in_local_ui(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    exe = find_chromium_executable()
    if not exe:
        pytest.skip("Requires an installed Chromium browser")
    monkeypatch.setattr("src.workbuddy.STEP_DELAY", 0)
    monkeypatch.setattr("src.workbuddy.read_github_code", lambda *args: "12345678")
    api = AccountApi(tmp_path)
    api._workbuddy.browser = Mock()
    api._workbuddy.browser.open.return_value = "工作窗口已打开"
    api._workbuddy.browser.challenge_page.return_value = None
    api._workbuddy.browser.is_ip_restricted_page.return_value = False
    api._workbuddy.browser.submit_github_signup = Mock(return_value={
        "status": "waiting_human",
        "message": "请在工作窗口核对并提交 GitHub 注册。",
    })
    api.save_account({"account_type": "域名邮箱", "account": "first@example.org", "password": "testPass123"})
    api.save_account({"account_type": "域名邮箱", "account": "second@example.org", "password": "testPass456"})
    errors = []
    try:
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=exe, headless=True)
            page = browser.new_page(viewport={"width": 1180, "height": 780})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.expose_function("__apiCall", lambda method, args: getattr(api, method)(*args))
            page.add_init_script("""
                window.pywebview = {api: new Proxy({}, {
                    get: (_, method) => (...args) => window.__apiCall(method, args)
                })};
            """)
            page.goto((Path(__file__).parents[1] / "web" / "index.html").as_uri())
            page.wait_for_function("state.accounts.length === 2")
            page.evaluate('setFilterType("域名邮箱")')
            page.locator(".workbuddy-account-check").first.wait_for()
            page.locator("#workbuddy-select-all").check()
            assert page.locator("#workbuddy-selected-count").inner_text() == "已选 2 个邮箱"
            page.evaluate('applyViewMode("table", false)')
            assert page.locator(".workbuddy-account-check:checked").count() == 2
            page.locator("#workbuddy-start").click()
            page.locator("#workbuddy-username").wait_for()
            page.wait_for_function("!workbuddy.busy")
            assert api._workbuddy.browser.open.call_count == 2
            assert page.locator(".wb-slot-card").count() == 2
            assert page.evaluate("typeof runWorkBuddyAutoLoop === 'function'")
            assert page.evaluate("typeof resumeAndAutoRunWorkBuddy === 'function'")
            assert page.locator("#workbuddy-confirm").is_disabled()
            page.get_by_role("button", name="⚡ 继续全自动注册此轮", exact=True).wait_for()
            page.get_by_role("button", name="遇到人机验证，暂停", exact=True).click()
            page.locator(".workbuddy-paused").wait_for()
            page.reload()
            page.wait_for_function('workbuddy.queue.current?.status === "paused"')
            page.get_by_role("button", name="继续当前任务", exact=True).click()
            page.get_by_role("button", name="打开待验证窗口", exact=True).click()
            page.wait_for_function("!workbuddy.busy")
            api._workbuddy.browser.show_paused_window.assert_called_once()
            assert api.get_workbuddy_queue()["current"]["status"] == "paused"
            page.get_by_role("button", name="✅ 验证已完成，继续全自动注册", exact=True).wait_for()
            page.get_by_role("button", name="仅恢复为手动确认", exact=True).click()
            page.locator("#workbuddy-username").wait_for()
            page.locator("#workbuddy-username").fill("changed1234")
            page.locator("#workbuddy-ack").check()
            page.locator("#workbuddy-confirm").click()
            page.wait_for_function('workbuddy.code === "12345678" && !workbuddy.busy')
            page.locator("#workbuddy-ack").check()
            stale_queue = api.get_workbuddy_queue()
            current = stale_queue["current"]
            # A second window confirms the email step while this window still has it checked.
            assert api.workbuddy_action(current["id"], current["revision"], "confirm")["ok"]
            page.evaluate("loadDashboard()")
            page.evaluate("(older) => updateWorkBuddyQueue(older)", stale_queue)
            assert page.evaluate("workbuddy.queue.current.step") == "workbuddy"
            assert page.locator("#workbuddy-confirm").is_disabled()
            assert not page.locator("#workbuddy-ack").is_checked()
            assert page.evaluate("workbuddy.code") == ""
            page.locator("#workbuddy-ack").check()
            page.locator("#workbuddy-confirm").click()
            page.wait_for_function('workbuddy.queue.current.step === "region" && !workbuddy.busy')
            page.locator('input[name="workbuddy-region"][value="not_required"]').check()
            page.locator("#workbuddy-ack").check()
            page.locator("#workbuddy-confirm").click()
            page.wait_for_function('workbuddy.queue.current.step === "greeting" && !workbuddy.busy')
            page.set_viewport_size({"width": 860, "height": 560})
            page.evaluate('applyTheme("light", false)')
            assert page.locator("#workbuddy-modal").evaluate("(el) => el.scrollWidth <= el.clientWidth")
            page.get_by_role("button", name="发送招呼", exact=True).click()
            page.wait_for_function("workbuddy.queue.current.greeting_attempted === 1 && !workbuddy.busy")
            assert page.get_by_role("button", name="发送招呼", exact=True).count() == 0
            page.locator("#workbuddy-ack").check()
            page.locator("#workbuddy-confirm").click()
            page.wait_for_function('workbuddy.queue.current.email === "second@example.org" && !workbuddy.busy')
            # Both round browsers stay open until the round finishes or is cancelled
            api._workbuddy.browser.close.assert_not_called()
            page.get_by_role("button", name="取消剩余任务", exact=True).click()
            page.locator(".workbuddy-finished").wait_for()
            api._workbuddy.browser.close.assert_any_call(discard_id=1)
            api._workbuddy.browser.close.assert_any_call(discard_id=2)
            assert api.get_workbuddy_queue()["current"] is None
            assert api.get_workbuddy_queue()["items"][0]["status"] == "done"
            assert not errors
            browser.close()
    finally:
        api._shutdown()


def test_ui_remembers_last_platform_and_quick_select_prioritizes_in_progress(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    exe = find_chromium_executable()
    if not exe:
        pytest.skip("Requires an installed Chromium browser")
    monkeypatch.setattr("src.workbuddy.STEP_DELAY", 0)

    api = AccountApi(tmp_path)
    api._workbuddy.browser = Mock()
    api._workbuddy.browser.open.return_value = "工作窗口已打开"
    api._workbuddy.browser.challenge_page.return_value = None
    api._workbuddy.browser.is_ip_restricted_page.return_value = False
    api._workbuddy.browser.submit_github_signup = Mock(return_value={
        "status": "waiting_human",
        "message": "请在工作窗口核对并提交 GitHub 注册。",
    })

    # Setup accounts: 1 completed, 2 in-progress, 2 pending
    acc_done = api.save_account({
        "account_type": "域名邮箱",
        "account": "done@ggb6.com",
        "password": "p1",
        "custom_fields": {"wb_reg_status": "已完成"},
    })["account"]
    acc_prog1 = api.save_account({
        "account_type": "域名邮箱",
        "account": "prog1@ggb6.com",
        "password": "p2",
        "custom_fields": {"wb_reg_status": "进行中", "wb_reg_step_label": "邮箱验证"},
    })["account"]
    acc_pending1 = api.save_account({
        "account_type": "域名邮箱",
        "account": "pending1@ggb6.com",
        "password": "p3",
        "custom_fields": {"wb_reg_status": "未完成"},
    })["account"]
    acc_prog2 = api.save_account({
        "account_type": "域名邮箱",
        "account": "prog2@ggb6.com",
        "password": "p4",
        "custom_fields": {"wb_reg_status": "进行中", "wb_reg_step_label": "GitHub注册"},
    })["account"]
    acc_pending2 = api.save_account({
        "account_type": "域名邮箱",
        "account": "pending2@ggb6.com",
        "password": "p5",
        "custom_fields": {"wb_reg_status": "未完成"},
    })["account"]

    errors = []
    try:
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=exe, headless=True)
            page = browser.new_page(viewport={"width": 1180, "height": 780})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.expose_function("__apiCall", lambda method, args: getattr(api, method)(*args))
            page.add_init_script("""
                window.pywebview = {api: new Proxy({}, {
                    get: (_, method) => (...args) => window.__apiCall(method, args)
                })};
            """)
            page.goto((Path(__file__).parents[1] / "web" / "index.html").as_uri())
            page.wait_for_function("state.accounts.length === 5")

            # Switch platform filter to 域名邮箱
            page.evaluate('setFilterType("域名邮箱")')
            page.wait_for_function('state.filterType === "域名邮箱"')
            page.locator("#workbuddy-toolbar").wait_for(state="visible")

            # Verify settings.json persisted last_filter_type
            time.sleep(0.3)
            settings = json.loads(api.settings_path.read_text(encoding="utf-8"))
            assert settings.get("last_filter_type") == "域名邮箱"

            # Reload page and assert state.filterType is automatically restored and #workbuddy-toolbar is immediately visible
            page.reload()
            page.wait_for_function('state.accounts.length === 5 && state.filterType === "域名邮箱"')
            assert page.evaluate("state.filterType") == "域名邮箱"
            assert page.locator("#workbuddy-toolbar").is_visible()

            # Verify .u-wb-badge.wb-progress renders "🔄 进行中" on cards in progress
            progress_badges = page.locator(".u-wb-badge.wb-progress")
            assert progress_badges.count() == 2
            for i in range(2):
                assert "🔄 进行中" in progress_badges.nth(i).inner_text()

            # Click #workbuddy-pick-2 (quickSelectPendingWorkBuddy(2))
            page.locator("#workbuddy-pick-2").click()
            page.wait_for_function("Array.from(workbuddy.selected).length === 2")
            selected_ids = page.evaluate("Array.from(workbuddy.selected)")
            assert set(selected_ids) == {acc_prog1["id"], acc_prog2["id"]}
            assert page.locator("#workbuddy-selected-count").inner_text() == "已选 2 个邮箱"
            assert page.locator(".workbuddy-account-check:checked").count() == 2

            assert not errors
            browser.close()
    finally:
        api._shutdown()


def test_autoloop_full_pipeline_and_resume_ui(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    exe = find_chromium_executable()
    if not exe:
        pytest.skip("Requires an installed Chromium browser")
    monkeypatch.setattr("src.workbuddy.STEP_DELAY", 0)
    monkeypatch.setattr("src.workbuddy.read_github_code", lambda *args: "12345678")
    api = AccountApi(tmp_path)
    api._workbuddy.browser = Mock()
    api._workbuddy.browser.open.return_value = "工作窗口已打开"
    api._workbuddy.browser.challenge_page.return_value = None
    api._workbuddy.browser.is_ip_restricted_page.return_value = False
    api._workbuddy.browser.capture_credential.return_value = {
        "accessToken": "header.payload.sig",
        "refreshToken": "ref123",
    }
    call_counts = {"signup": 0}

    def mock_submit_github_signup(task, password):
        call_counts["signup"] += 1
        if call_counts["signup"] == 1:
            return {"status": "challenged", "message": "人机验证"}
        return {"status": "next_step", "next": "email", "username": task["username"]}

    api._workbuddy.browser.submit_github_signup.side_effect = mock_submit_github_signup

    api.save_account({"account_type": "域名邮箱", "account": "auto_ui1@example.org", "password": "testPass123"})
    errors = []
    try:
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=exe, headless=True)
            page = browser.new_page(viewport={"width": 1180, "height": 780})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.expose_function("__apiCall", lambda method, args: getattr(api, method)(*args))
            page.add_init_script("""
                window.pywebview = {api: new Proxy({}, {
                    get: (_, method) => (...args) => window.__apiCall(method, args)
                })};
            """)
            page.goto((Path(__file__).parents[1] / "web" / "index.html").as_uri())
            page.wait_for_function("state.accounts.length === 1")
            page.evaluate('setFilterType("域名邮箱")')
            page.locator(".workbuddy-account-check").first.wait_for()
            page.locator("#workbuddy-select-all").check()

            # Verify functions exist
            assert page.evaluate("typeof runWorkBuddyAutoLoop === 'function'")
            assert page.evaluate("typeof resumeAndAutoRunWorkBuddy === 'function'")

            # Click start -> auto loop runs and pauses on challenge
            page.locator("#workbuddy-start").click()
            page.locator(".workbuddy-paused").wait_for()
            paused_text = page.locator(".workbuddy-paused").inner_text()
            assert "访问暂时受限" in paused_text
            assert "拉黑并换新节点重开" in paused_text
            page.wait_for_function('workbuddy.queue.current?.status === "paused"')

            # Click resume and auto run -> completes rest of pipeline
            page.get_by_role("button", name="✅ 验证已完成，继续全自动注册", exact=True).click()
            page.locator(".workbuddy-finished").wait_for()

            assert api.get_workbuddy_queue()["current"] is None
            assert api.get_workbuddy_queue()["items"][0]["status"] == "done"
            assert not errors
            browser.close()
    finally:
        api._shutdown()


def test_autoloop_auto_rotates_node_on_ip_restricted_ui(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    exe = find_chromium_executable()
    if not exe:
        pytest.skip("Requires an installed Chromium browser")
    monkeypatch.setattr("src.workbuddy.STEP_DELAY", 0)
    monkeypatch.setattr("src.workbuddy.read_github_code", lambda *args: "12345678")
    api = AccountApi(tmp_path)
    api._workbuddy.browser = Mock()
    api._workbuddy.browser.open.return_value = "工作窗口已打开"
    api._workbuddy.browser.challenge_page.return_value = None
    api._workbuddy.browser.is_ip_restricted_page.return_value = False
    api._workbuddy.browser.capture_credential.return_value = {
        "accessToken": "header.payload.sig",
        "refreshToken": "ref123",
    }
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": [{"name": "UI-SG-01"}, {"name": "UI-SG-02"}],
    }
    call_counts = {"signup": 0}

    def mock_submit_github_signup(task, password):
        call_counts["signup"] += 1
        if call_counts["signup"] == 1:
            return {"status": "ip_restricted", "message": "访问暂时受限"}
        return {"status": "next_step", "next": "email", "username": task["username"]}

    api._workbuddy.browser.submit_github_signup.side_effect = mock_submit_github_signup

    api.save_account({"account_type": "域名邮箱", "account": "auto_rotate_ui@example.org", "password": "testPass123"})
    errors = []
    try:
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=exe, headless=True)
            page = browser.new_page(viewport={"width": 1180, "height": 780})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.expose_function("__apiCall", lambda method, args: getattr(api, method)(*args))
            page.add_init_script("""
                window.pywebview = {api: new Proxy({}, {
                    get: (_, method) => (...args) => window.__apiCall(method, args)
                })};
            """)
            page.goto((Path(__file__).parents[1] / "web" / "index.html").as_uri())
            page.wait_for_function("state.accounts.length === 1")
            page.evaluate('setFilterType("域名邮箱")')
            page.locator(".workbuddy-account-check").first.wait_for()
            page.locator("#workbuddy-select-all").check()

            # Click start -> auto loop starts on UI-SG-01, hits ip_restricted, automatically rotates to UI-SG-02, and completes!
            page.locator("#workbuddy-start").click()
            page.locator(".workbuddy-finished").wait_for(timeout=15000)

            assert api.get_workbuddy_queue()["current"] is None
            assert api.get_workbuddy_queue()["items"][0]["status"] == "done"
            quotas = api.get_workbuddy_node_quotas()
            q1 = next(n for n in quotas["nodes"] if n["node_name"] == "UI-SG-01")
            assert q1["blocked"] is True

            assert not errors
            browser.close()
    finally:
        api._shutdown()


def test_workbuddy_js_calls_schedule_paused_check():
    js = (Path(__file__).parents[1] / "web" / "workbuddy.js").read_text(encoding="utf-8")
    assert "schedulePausedWorkBuddyCheck();" in js

    # Verify startWorkBuddyQueue clears busy before autoloop and calls schedulePausedWorkBuddyCheck in finally
    start_chunk = js.split("async function startWorkBuddyQueue()")[1].split("async function openWorkBuddyModal()")[0]
    assert "workbuddy.busy = false;" in start_chunk
    assert "setWorkBuddyBusy(false);" in start_chunk
    assert "schedulePausedWorkBuddyCheck();" in start_chunk

    # Verify openWorkBuddyModal calls schedulePausedWorkBuddyCheck()
    open_chunk = js.split("async function openWorkBuddyModal()")[1].split("function closeWorkBuddyModal()")[0]
    assert "schedulePausedWorkBuddyCheck();" in open_chunk

    # Verify resumeAndAutoRunWorkBuddy calls schedulePausedWorkBuddyCheck()
    resume_chunk = js.split("async function resumeAndAutoRunWorkBuddy()")[1].split("function schedulePausedWorkBuddyCheck()")[0]
    assert "schedulePausedWorkBuddyCheck();" in resume_chunk


def test_schedule_paused_check_invoked_by_queue_and_modal(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    exe = find_chromium_executable()
    if not exe:
        pytest.skip("Requires an installed Chromium browser")
    api = AccountApi(tmp_path)
    api.save_account({"account_type": "域名邮箱", "account": "paused_ui@example.org", "password": "testPass123"})
    errors = []
    try:
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=exe, headless=True)
            page = browser.new_page(viewport={"width": 1180, "height": 780})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.expose_function("__apiCall", lambda method, args: getattr(api, method)(*args))
            page.add_init_script("""
                window.pywebview = {api: new Proxy({}, {
                    get: (_, method) => (...args) => window.__apiCall(method, args)
                })};
            """)
            page.goto((Path(__file__).parents[1] / "web" / "index.html").as_uri())
            page.wait_for_function("state.accounts.length === 1")

            # Verify openWorkBuddyModal calls schedulePausedWorkBuddyCheck
            res_open = page.evaluate("""
                async () => {
                    let openCalls = 0;
                    const origSchedule = window.schedulePausedWorkBuddyCheck;
                    window.schedulePausedWorkBuddyCheck = () => { openCalls++; };
                    try {
                        await openWorkBuddyModal();
                    } finally {
                        window.schedulePausedWorkBuddyCheck = origSchedule;
                    }
                    return openCalls;
                }
            """)
            assert res_open >= 1

            # Verify startWorkBuddyQueue clears busy before autoloop and calls schedulePausedWorkBuddyCheck in finally
            res_start = page.evaluate("""
                async () => {
                    let busyBeforeLoop = null;
                    let scheduleCalls = 0;
                    const origAutoLoop = window.runWorkBuddyAutoLoop;
                    const origSchedule = window.schedulePausedWorkBuddyCheck;
                    const origCallApi = window.callApi;
                    window.runWorkBuddyAutoLoop = async () => {
                        busyBeforeLoop = workbuddy.busy;
                    };
                    window.schedulePausedWorkBuddyCheck = () => { scheduleCalls++; };
                    window.callApi = async (method, ...args) => {
                        if (method === "start_workbuddy_queue") {
                            return { ok: true, queue: { items: [], round_items: [], current: null } };
                        }
                        return origCallApi(method, ...args);
                    };
                    try {
                        state.accounts[0].account_type = "域名邮箱";
                        workbuddy.visible = state.accounts;
                        workbuddy.selected.add(state.accounts[0].id);
                        await startWorkBuddyQueue();
                    } finally {
                        window.runWorkBuddyAutoLoop = origAutoLoop;
                        window.schedulePausedWorkBuddyCheck = origSchedule;
                        window.callApi = origCallApi;
                    }
                    return { busyBeforeLoop, scheduleCalls, finalBusy: workbuddy.busy };
                }
            """)
            assert res_start["busyBeforeLoop"] is False
            assert res_start["scheduleCalls"] >= 1
            assert res_start["finalBusy"] is False
            assert not errors
            browser.close()
    finally:
        api._shutdown()


def test_domain_email_ui_elements_static():
    root = Path(__file__).parents[1]
    html = (root / "web" / "index.html").read_text(encoding="utf-8")
    js = (root / "web" / "app.js").read_text(encoding="utf-8")
    css = (root / "web" / "style.css").read_text(encoding="utf-8")

    # index.html assertions
    assert 'id="btn-refresh-mail-codes"' in html
    assert 'id="btn-reset-email-nodes"' in html
    assert 'id="btn-toggle-auto-mail-poll"' in html
    assert 'onclick="refreshDomainEmailCodes()"' in html
    assert 'onclick="confirmResetDomainEmailNodes()"' in html
    assert "每 2 个邮箱收到验证码自动换下一节点" in html

    # app.js assertions
    assert "function refreshDomainEmailCodes" in js
    assert "refreshSingleEmailCode" in js
    assert "mail-code-refresh" in js
    assert "function confirmResetDomainEmailNodes" in js
    assert "function startMailCodePolling" in js
    assert "function stopMailCodePolling" in js
    assert "function toggleAutoMailCodePolling" in js
    assert "function syncAutoMailPollButton" in js
    assert "autoMailCodePolling: false" in js
    assert "function copyText" in js
    assert "mail-code-box" in js
    assert "pending-mail-code" in js
    assert "has-code" in js
    assert "latest_code" in js
    assert "get_domain_email_codes" in js
    assert "reset_domain_email_nodes" in js

    # style.css assertions
    assert ".u-totp-box.mail-code-box.has-code" in css
    assert ".mail-code-label" in css
    assert ".mail-code-val" in css
    assert ".pending-mail-code" in css


def test_domain_email_card_and_table_rendering(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    exe = find_chromium_executable()
    if not exe:
        pytest.skip("Requires an installed Chromium browser")

    api = AccountApi(tmp_path)
    # Keep the silent-refresh toggle test off the network.
    monkeypatch.setattr(
        api,
        "get_domain_email_codes",
        lambda *a, **k: {"ok": True, "codes": {}, "node_stats": {}},
    )
    # 1st account with latest_code
    acc1 = api.save_account({
        "account_type": "域名邮箱",
        "account": "hascode@ggb6.com",
        "password": "pwd1",
        "custom_fields": {
            "latest_code": "889900",
            "code_received_at": int(time.time()),
            "has_received_code": True,
        },
    })["account"]
    api.db.bind_account_proxy_node(acc1["id"], "http://127.0.0.1:17899", "Node-A", locked=True)

    # 2nd account without code
    acc2 = api.save_account({
        "account_type": "域名邮箱",
        "account": "nocode@ggb6.com",
        "password": "pwd2",
        "custom_fields": {},
    })["account"]
    api.db.bind_account_proxy_node(acc2["id"], "http://127.0.0.1:17899", "Node-A", locked=True)

    errors = []
    try:
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=exe, headless=True)
            page = browser.new_page(viewport={"width": 1180, "height": 780})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: dialog.accept())
            page.expose_function("__apiCall", lambda method, args: getattr(api, method)(*args))
            page.add_init_script("""
                window.pywebview = {api: new Proxy({}, {
                    get: (_, method) => (...args) => window.__apiCall(method, args)
                })};
            """)
            page.goto((Path(__file__).parents[1] / "web" / "index.html").as_uri())
            page.wait_for_function("state.accounts.length === 2")
            page.evaluate('setFilterType("域名邮箱")')

            # Verify toolbar buttons visible
            page.locator("#workbuddy-toolbar").wait_for(state="visible")
            assert page.locator("#btn-refresh-mail-codes").is_visible()
            assert page.locator("#btn-reset-email-nodes").is_visible()

            # Silent auto-refresh toggle is visible and off by default; toggling flips the label
            auto_poll_btn = page.locator("#btn-toggle-auto-mail-poll")
            assert auto_poll_btn.is_visible()
            assert "静默刷新: 关" in auto_poll_btn.inner_text()
            assert page.evaluate("state.autoMailCodePolling") is False
            auto_poll_btn.click()
            assert "静默刷新: 开" in auto_poll_btn.inner_text()
            assert page.evaluate("state.autoMailCodePolling") is True
            auto_poll_btn.click()
            assert "静默刷新: 关" in auto_poll_btn.inner_text()
            assert page.evaluate("state.autoMailCodePolling") is False

            # Verify card rendering: latest mail code lives directly in the 2FA slot
            page.locator(".u-totp-box.mail-code-box.has-code").wait_for()
            has_code_box = page.locator(".u-totp-box.mail-code-box.has-code")
            assert "邮件码" in has_code_box.inner_text()
            assert "889900" in has_code_box.inner_text()
            pending_box = page.locator(".u-totp-box.mail-code-box.pending")
            assert pending_box.count() == 1
            assert "暂无 (查信)" in pending_box.inner_text()

            # Check node stats display on proxy node pill
            page.evaluate("""() => {
                state.nodeCodeStats = { "Node-A": 1 };
                renderAccounts();
            }""")
            pill_text = page.locator(".u-node-pill").first.inner_text()
            assert "Node-A" in pill_text
            assert "(1/2 已收码)" in pill_text

            # Switch to table view and verify mail code rendering in 2FA column
            page.evaluate('applyViewMode("table", false)')
            page.locator("#accounts-container .u-table").wait_for()
            table_code_cell = page.locator(".tb-mail-code-cell").first
            assert "889900" in table_code_cell.inner_text()

            # Verify copyText function works
            assert page.evaluate("typeof copyText === 'function'")
            assert page.evaluate("typeof refreshDomainEmailCodes === 'function'")
            assert page.evaluate("typeof confirmResetDomainEmailNodes === 'function'")

            assert not errors
            browser.close()
    finally:
        api._shutdown()


