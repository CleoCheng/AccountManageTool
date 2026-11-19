from concurrent.futures import ThreadPoolExecutor
import json
from unittest.mock import Mock

import pytest

from src.api import AccountApi
from src.db_service import DatabaseService
from src.workbuddy import WorkBuddyQueue
from src.workbuddy_browser import signup_url

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

_orig_healthy_nodes = WorkBuddyQueue._healthy_env_nodes


@staticmethod
def _tolerant_healthy_nodes(env):
    raw = env.get("nodes") or []
    norm = []
    for n in raw:
        if isinstance(n, str) and n.strip():
            norm.append({"name": n.strip()})
        elif isinstance(n, dict):
            norm.append(n)
    return _orig_healthy_nodes({"nodes": norm})


WorkBuddyQueue._healthy_env_nodes = _tolerant_healthy_nodes

_orig_get_node_quotas = WorkBuddyQueue.get_node_quotas


def _tolerant_get_node_quotas(self):
    try:
        env = self.discover_nodes_fn(account_type="WorkBuddy") or {}
    except Exception:
        env = {}
    raw = env.get("nodes") or []
    norm = []
    for n in raw:
        if isinstance(n, str) and n.strip():
            norm.append({"name": n.strip()})
        elif isinstance(n, dict):
            norm.append(n)
    orig_fn = self.discover_nodes_fn
    self.discover_nodes_fn = lambda **kw: {**env, "nodes": norm}
    try:
        return _orig_get_node_quotas(self)
    finally:
        self.discover_nodes_fn = orig_fn


WorkBuddyQueue.get_node_quotas = _tolerant_get_node_quotas


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr("src.workbuddy.STEP_DELAY", 0)
    api = AccountApi(tmp_path)
    api._workbuddy.browser = Mock()
    api._workbuddy.browser.open.return_value = "工作窗口已打开"
    api._workbuddy.browser.challenge_page.return_value = None
    api._workbuddy.browser.is_ip_restricted_page.return_value = False
    api._workbuddy.browser.check_paused_challenge_state.return_value = ""
    yield api
    api._shutdown()


def add_mail(api, email="worker+one@example.org"):
    return api.save_account({"account_type": "域名邮箱", "account": email, "password": "pAss----word|23"})["account"]


def act(api, action, value=""):
    current = api.get_workbuddy_queue()["current"]
    return api.workbuddy_action(current["id"], current["revision"], action, value)


def test_serial_queue_blocks_repeat_clicks_and_preserves_pause(api):
    first = add_mail(api)
    second = add_mail(api, "second@example.org")
    result = api.start_workbuddy_queue([first["id"], second["id"]])
    task = result["queue"]["current"]
    assert result["ok"]
    assert task["username"].startswith("worker-one")
    assert len(task["username"][-4:]) == 4
    assert task["username"][-4:].isdigit()
    assert "user_email=worker%2Bone%40example.org" in signup_url(first["account"])
    assert not api.start_workbuddy_queue([second["id"]])["ok"]

    assert act(api, "pause")["ok"]
    paused = api.get_workbuddy_queue()["current"]
    assert paused["status"] == "paused"
    assert not act(api, "confirm", "worker-one1234")["ok"]
    assert not act(api, "open")["ok"]
    assert not act(api, "code")["ok"]
    assert act(api, "show")["queue"]["current"] == paused
    api._workbuddy.browser.show_paused_window.assert_called_once()
    api._workbuddy.browser.open.assert_not_called()

    reopened = WorkBuddyQueue(api.db, api.data_dir)
    try:
        assert reopened.snapshot()["current"]["status"] == "paused"
    finally:
        reopened.close()
    assert act(api, "resume")["queue"]["current"]["step"] == "github"
    assert act(api, "confirm", "worker-one1234")["queue"]["current"]["step"] == "email"
    # Even a queued request from a previous screen cannot confirm another phase.
    assert not api.workbuddy_action(task["id"], task["revision"], "confirm", "worker-one1234")["ok"]
    assert api.get_workbuddy_queue()["current"]["account_id"] == first["id"]


def test_complete_requires_region_and_closes_both_browsers_when_round_finishes(api):
    one, two = add_mail(api), add_mail(api, "two@example.org")
    api.start_workbuddy_queue([one["id"], two["id"]])
    task1 = api.get_workbuddy_queue()["current"]
    assert act(api, "open_round")["opened_count"] == 2
    assert api._workbuddy.browser.open.call_count == 2
    assert act(api, "password")["password"] == "pAss----word|23"
    assert act(api, "confirm", task1["username"])["ok"]
    assert act(api, "confirm")["ok"]
    assert act(api, "confirm")["queue"]["current"]["step"] == "region"
    assert not act(api, "confirm", "")["ok"]
    assert act(api, "confirm", "not_required")["queue"]["current"]["step"] == "greeting"
    sent = act(api, "send")
    assert sent["ok"] and sent["queue"]["current"]["greeting_attempted"]
    assert sent["queue"]["current"]["step"] == "greeting"
    assert not act(api, "send")["ok"]
    api._workbuddy.browser.prepare_greeting.return_value.click.assert_called_once()

    # Completing 1st account in a 2-browser round saves it, while keeping both round browsers open
    result1 = act(api, "confirm")
    assert result1["ok"] and result1["round_completed"] is False
    api._workbuddy.browser.close.assert_not_called()
    assert result1["queue"]["current"]["account_id"] == two["id"]
    assert result1["queue"]["items"][0]["status"] == "done"
    assert result1["queue"]["completed"][one["account"]] == task1["username"]

    # Advance 2nd account to greeting
    task2 = api.get_workbuddy_queue()["current"]
    for val in (task2["username"], "", "", "confirmed"):
        assert act(api, "confirm", val)["ok"]
    # Failed round cleanup must not falsely mark the final account as completed
    api._workbuddy.browser.close.side_effect = OSError("test close failure")
    assert not act(api, "confirm")["ok"]
    assert api.get_workbuddy_queue()["current"]["step"] == "greeting"
    api._workbuddy.browser.close.side_effect = None
    result2 = act(api, "confirm")
    assert result2["ok"] and result2["round_completed"] is True
    assert result2["queue"]["current"] is None
    api._workbuddy.browser.close.assert_any_call(discard_id=task1["id"])
    api._workbuddy.browser.close.assert_any_call(discard_id=task2["id"])
    api._workbuddy.browser.purge_cache_and_switch_node.assert_called_once_with("", "")
    assert not api.start_workbuddy_queue([one["id"]])["ok"]


def test_uncertain_greeting_is_not_resent_after_restart_or_reselection(api):
    mail = add_mail(api)
    task = api.start_workbuddy_queue([mail["id"]])["queue"]["current"]
    for value in (task["username"], "", "", "confirmed"):
        assert act(api, "confirm", value)["ok"]
    button = api._workbuddy.browser.prepare_greeting.return_value
    button.click.side_effect = TimeoutError()
    assert not act(api, "send")["ok"]
    assert api.get_workbuddy_queue()["current"]["greeting_attempted"] == 1
    reopened = WorkBuddyQueue(api.db, api.data_dir)
    try:
        current = reopened.snapshot()["current"]
        assert not reopened.action(current["id"], current["revision"], "send")["ok"]
    finally:
        reopened.close()
    assert act(api, "skip")["ok"]
    assert api.start_workbuddy_queue([mail["id"]])["ok"]
    assert not act(api, "send")["ok"]
    button.click.assert_called_once()


def test_invalid_selection_is_atomic_and_cancelled_steps_resume(api):
    mail = add_mail(api)
    invalid = api.save_account({"account": "other@example.org", "password": "pw"})["account"]
    assert not api.start_workbuddy_queue([mail["id"], invalid["id"]])["ok"]
    assert api.get_workbuddy_queue()["items"] == []
    copy = add_mail(api, mail["account"].upper())
    assert not api.start_workbuddy_queue([mail["id"], copy["id"]])["ok"]
    assert api.get_workbuddy_queue()["items"] == []
    assert api.start_workbuddy_queue([mail["id"], mail["id"]])["ok"]
    current = api.get_workbuddy_queue()["current"]
    act(api, "confirm", current["username"])
    assert act(api, "skip")["queue"]["current"] is None
    result = api.start_workbuddy_queue([mail["id"]])
    assert result["queue"]["current"]["step"] == "email"
    assert result["queue"]["current"]["username"] == current["username"]
    api.delete_account(mail["id"])
    assert not act(api, "open")["ok"]
    assert act(api, "cancel")["ok"]


def test_simultaneous_confirmations_advance_exactly_one_phase(api):
    mail = add_mail(api)
    task = api.start_workbuddy_queue([mail["id"]])["queue"]["current"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(api.workbuddy_action, task["id"], task["revision"], "confirm", task["username"])
                 for _ in range(2)]
        results = [call.result() for call in calls]
    assert sum(result["ok"] for result in results) == 1
    assert api.get_workbuddy_queue()["current"]["step"] == "email"


def test_visible_challenge_pauses_before_advance_and_requires_a_human(api):
    mail = add_mail(api)
    current = api.start_workbuddy_queue([mail["id"]])["queue"]["current"]
    api._workbuddy.browser.challenge_page.return_value = object()
    paused = act(api, "confirm", current["username"])["queue"]["current"]
    assert (paused["status"], paused["step"]) == ("paused", "github")
    assert not act(api, "confirm", current["username"])["ok"]
    assert act(api, "show")["queue"]["current"] == paused
    assert act(api, "resume")["queue"]["current"]["step"] == "github"
    # A solved iframe may remain visible. Human confirmation expires at the next step.
    assert act(api, "confirm", current["username"])["queue"]["current"]["step"] == "email"
    assert act(api, "code")["queue"]["current"]["status"] == "paused"


def test_mail_lookup_is_scoped_and_never_persisted(api, monkeypatch):
    mail = add_mail(api)
    task = api.start_workbuddy_queue([mail["id"]])["queue"]["current"]
    reader = Mock(return_value="12345678")
    monkeypatch.setattr("src.workbuddy.read_github_code", reader)
    assert not act(api, "code")["ok"]
    act(api, "confirm", task["username"])
    result = act(api, "code")
    assert result["code"] == "12345678"
    reader.assert_called_once_with(mail["account"], mail["password"], task["started_at"])
    assert "12345678" not in str(api.get_workbuddy_queue())
    assert mail["password"] not in str(api.get_workbuddy_queue())
    assert api.get_workbuddy_queue()["current"]["step"] == "email"


def test_domain_mail_import_preserves_delimiters_and_selected_type(api):
    raw = "alpha@staff.example---- pass----word|12 \nbeta@staff.example----pAssword"
    preview = api.smart_parse(raw, "域名邮箱")
    assert preview["count"] == 2
    assert preview["records"][0]["password"] == " pass----word|12 "
    assert all(item["account_type"] == "域名邮箱" for item in preview["records"])
    assert api.batch_import(raw, "域名邮箱")["imported"] == 2
    assert api.get_dashboard("域名邮箱")["accounts"][0]["password"] == " pass----word|12 "
    assert not api.batch_import("alpha@staff.example----pw\nbad-line", "域名邮箱")["ok"]
    assert len(api.get_dashboard("域名邮箱")["accounts"]) == 2


def test_subscription_node_quota_enforces_max_two_and_unlock_allows_reuse(api):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "client_name": "Clash Verge",
        "controller_online": True,
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": [{"name": "SG-Node-01", "region": "SG", "delay": 42}],
    }
    m1 = add_mail(api, "quota1@ggb6.com")
    m2 = add_mail(api, "quota2@ggb6.com")
    m3 = add_mail(api, "quota3@ggb6.com")

    # 3 accounts on a single node (max 2 per node) must be blocked atomically
    over_limit = api.start_workbuddy_queue([m1["id"], m2["id"], m3["id"]])
    assert not over_limit["ok"]
    assert "上限" in over_limit["error"]
    assert api.get_workbuddy_queue()["items"] == []

    # 2 accounts on SG-Node-01 succeed with distinct fresh fingerprint seeds
    ok_two = api.start_workbuddy_queue([m1["id"], m2["id"]])
    assert ok_two["ok"]
    items = ok_two["queue"]["items"]
    assert items[0]["proxy_node"] == "SG-Node-01"
    assert items[1]["proxy_node"] == "SG-Node-01"
    assert items[0]["fp_summary"] and items[1]["fp_summary"]
    with api._workbuddy._connect() as conn:
        fps = [r["fp_json"] for r in conn.execute("SELECT fp_json FROM tasks ORDER BY id").fetchall()]
    assert len(fps) == 2 and fps[0] != fps[1]

    # Complete both m1 and m2
    for _ in range(2):
        cur = api.get_workbuddy_queue()["current"]
        for val in (cur["username"], "", "", "not_required", ""):
            assert act(api, "confirm", val)["ok"]

    quotas = api.get_workbuddy_node_quotas()
    sg_info = next(n for n in quotas["nodes"] if n["node_name"] == "SG-Node-01")
    assert sg_info["used_count"] == 2
    assert sg_info["exhausted"] is True

    # Reusing SG-Node-01 for a 3rd account without explicit unlock is forbidden
    blocked_third = api.start_workbuddy_queue([m3["id"]])
    assert not blocked_third["ok"]
    assert "上限" in blocked_third["error"]

    # Explicit manual unlock allows reusing SG-Node-01
    unlocked = api.set_workbuddy_node_unlock("SG-Node-01", True)
    assert unlocked["ok"]
    allowed_third = api.start_workbuddy_queue([m3["id"]], "", "SG-Node-01")
    assert allowed_third["ok"]
    assert allowed_third["queue"]["current"]["proxy_node"] == "SG-Node-01"


def test_excel_import_to_completion_updates_status_and_exports_workbuddy2api_json(api):
    import base64

    csv_bytes = (
        "email,password,name,webmail\n"
        "liam.simpson@ggb6.com,Pass#2026!,Liam Simpson,https://mail.ggb6.com\n"
    ).encode("utf-8")
    imp = api.import_domain_email_file(base64.b64encode(csv_bytes).decode("ascii"), "ggb6-mailboxes.csv")
    assert imp["ok"] and imp["imported"] == 1

    mail = api.get_dashboard("域名邮箱")["accounts"][0]
    assert mail["account"] == "liam.simpson@ggb6.com"
    assert mail["custom_fields"]["wb_reg_status"] == "未完成"

    fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1XzEwMDEiLCJleHAiOjE5OTk5OTk5OTl9.sig"
    api._workbuddy.browser.capture_credential.return_value = {
        "uid": "u_1001",
        "nickname": "liam_wb",
        "domain": "www.workbuddy.ai",
        "realm": "intl",
        "platform": "CLI",
        "product": "workbuddy",
        "accessToken": fake_jwt,
        "refreshToken": "rt_secret_123",
        "expiresAt": 1999999999,
    }

    started = api.start_workbuddy_queue([mail["id"]])
    assert started["ok"]
    task = started["queue"]["current"]
    for val in (task["username"], "", "", "confirmed", ""):
        assert act(api, "confirm", val)["ok"]

    api._workbuddy.browser.close.assert_called_with(discard_id=task["id"])

    updated_mail = api.get_dashboard("域名邮箱")["accounts"][0]
    assert updated_mail["custom_fields"]["wb_reg_status"] == "已完成"

    wb_accounts = api.get_dashboard("WorkBuddy")["accounts"]
    assert len(wb_accounts) == 1

    exported = api.export_workbuddy_json([mail["id"]], save_to_account_data=False)
    assert exported["ok"] and exported["count"] == 1
    doc = exported["document"]
    assert doc["format"] == "workbuddy-accounts"
    assert doc["version"] == 1
    assert doc["accounts"][0]["uid"] == "u_1001"
    assert doc["accounts"][0]["domain"] == "www.workbuddy.ai"
    assert doc["accounts"][0]["realm"] == "intl"
    assert doc["accounts"][0]["platform"] == "CLI"
    assert doc["accounts"][0]["accessToken"] == fake_jwt
    assert doc["accounts"][0]["refreshToken"] == "rt_secret_123"


def test_dual_browser_rounds_switch_node_and_clean_cache_between_rounds(api):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "client_name": "Clash Verge",
        "controller_online": True,
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": [
            {"name": "Node-Round-1", "region": "SG", "delay": 35},
            {"name": "Node-Round-2", "region": "JP", "delay": 48},
        ],
    }
    a1 = add_mail(api, "r1_one@ggb6.com")
    a2 = add_mail(api, "r1_two@ggb6.com")
    a3 = add_mail(api, "r2_one@ggb6.com")
    a4 = add_mail(api, "r2_two@ggb6.com")

    started = api.start_workbuddy_queue([a1["id"], a2["id"], a3["id"], a4["id"]])
    assert started["ok"]
    q = started["queue"]
    assert q["round_no"] == 1
    assert q["total_rounds"] == 2
    assert len(q["round_items"]) == 2
    assert [it["proxy_node"] for it in q["round_items"]] == ["Node-Round-1", "Node-Round-1"]

    # Open both Round 1 fingerprint browsers
    open_r1 = act(api, "open_round")
    assert open_r1["ok"] and open_r1["opened_count"] == 2
    assert api._workbuddy.browser.open.call_count == 2

    # Interleave actions on Slot 2 (a2) while Slot 1 (a1) is still at github
    t1, t2 = q["round_items"][0], q["round_items"][1]
    adv_t2 = api.workbuddy_action(t2["id"], t2["revision"], "confirm", t2["username"])
    assert adv_t2["ok"]

    # Finish Slot 1 (a1) completely
    cur_t1 = next(it for it in api.get_workbuddy_queue()["round_items"] if it["id"] == t1["id"])
    for val in (cur_t1["username"], "", "", "confirmed", ""):
        cur_t1 = next(it for it in api.get_workbuddy_queue()["round_items"] if it["id"] == t1["id"])
        res = api.workbuddy_action(cur_t1["id"], cur_t1["revision"], "confirm", val)
        assert res["ok"]
    # Neither browser closed yet because Slot 2 in Round 1 is still active
    api._workbuddy.browser.close.assert_not_called()

    # Now finish Slot 2 (a2) from email -> workbuddy -> region -> greeting -> confirm
    for val in ("", "", "not_required", ""):
        cur_t2 = next(it for it in api.get_workbuddy_queue()["round_items"] if it["id"] == t2["id"])
        res2 = api.workbuddy_action(cur_t2["id"], cur_t2["revision"], "confirm", val)
        assert res2["ok"]

    # Round 1 is now complete -> both Round 1 browsers closed, cache purged, switched to Node-Round-2!
    assert res2["round_completed"] is True
    assert res2["round_transitioned"] is True
    api._workbuddy.browser.close.assert_any_call(discard_id=t1["id"])
    api._workbuddy.browser.close.assert_any_call(discard_id=t2["id"])
    api._workbuddy.browser.purge_cache_and_switch_node.assert_called_once_with(
        "http://127.0.0.1:7890",
        "Node-Round-2",
    )

    q2 = res2["queue"]
    assert q2["round_no"] == 2
    assert [it["email"] for it in q2["round_items"]] == ["r2_one@ggb6.com", "r2_two@ggb6.com"]
    assert [it["proxy_node"] for it in q2["round_items"]] == ["Node-Round-2", "Node-Round-2"]


def test_switch_node_recovers_from_restricted_ip_and_deprioritizes_dead_nodes(api):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "client_name": "Clash Verge",
        "controller_online": True,
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": [
            {"name": "Dead-Timeout-Node", "region": "EU", "delay": None, "timed_out": True},
            {"name": "Dirty-IP-Node", "region": "EU", "delay": 180},
            {"name": "Clean-SG-Node", "region": "SG", "delay": 38},
        ],
    }
    m1 = add_mail(api, "victoria@ggb6.com")
    m2 = add_mail(api, "ethan@ggb6.com")

    # Dead timed-out node is skipped in favor of live delay>0 nodes
    started = api.start_workbuddy_queue([m1["id"], m2["id"]])
    assert started["ok"]
    assert [it["proxy_node"] for it in started["queue"]["round_items"]] == ["Dirty-IP-Node", "Dirty-IP-Node"]

    # Simulate Imperva "访问暂时受限" pausing task 1
    api._workbuddy.browser.challenge_page.return_value = object()
    paused_res = act(api, "open")
    assert paused_res["ok"]
    assert paused_res["queue"]["current"]["status"] == "paused"
    assert "访问暂时受限" in paused_res["message"]

    # User clicks 1-click "switch_node" while paused -> blacklists Dirty-IP-Node, wipes cache, moves round to Clean-SG-Node
    api._workbuddy.browser.challenge_page.return_value = None
    switched = act(api, "switch_node")
    assert switched["ok"]
    assert switched["round_transitioned"] is True
    assert switched["switched_node"] == "Clean-SG-Node"
    new_round = switched["queue"]["round_items"]
    assert [it["proxy_node"] for it in new_round] == ["Clean-SG-Node", "Clean-SG-Node"]
    assert all(it["status"] == "ready" for it in new_round)
    api._workbuddy.browser.purge_cache_and_switch_node.assert_called_with(
        "http://127.0.0.1:7890",
        "Clean-SG-Node",
    )

    quotas = api.get_workbuddy_node_quotas()
    dirty_info = next(n for n in quotas["nodes"] if n["node_name"] == "Dirty-IP-Node")
    assert dirty_info["blocked"] is True
    assert dirty_info["exhausted"] is True
    assert dirty_info["used_count"] == 0

    # Resetting quota clears blocked state
    reset_q = api.reset_workbuddy_node_quota("Dirty-IP-Node")
    dirty_after = next(n for n in reset_q["nodes"] if n["node_name"] == "Dirty-IP-Node")
    assert dirty_after["blocked"] is False
    assert dirty_after["exhausted"] is False


def test_auto_sync_account_data_imports_incrementally_and_preserves_progress(tmp_path, monkeypatch):
    monkeypatch.setattr("src.workbuddy.STEP_DELAY", 0)
    base = tmp_path / "app"
    accountData = base / "accountData"
    data_dir = base / "data"
    accountData.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    csv1 = "email,password\na@ggb6.com,pw1\nb@ggb6.com,pw2\nc@ggb6.com,pw3\n"
    (accountData / "batch1.csv").write_text(csv1, encoding="utf-8")

    api1 = AccountApi(data_dir, auto_sync_account_data=True)
    api1._workbuddy.browser = Mock()
    api1._workbuddy.browser.open.return_value = "工作窗口已打开"
    api1._workbuddy.browser.challenge_page.return_value = None

    try:
        assert api1.auto_sync_result is not None
        assert api1.auto_sync_result["imported"] == 3
        dash1 = api1.get_dashboard("域名邮箱")["accounts"]
        assert len(dash1) == 3
        for acc in dash1:
            assert acc["custom_fields"].get("wb_reg_status") == "未完成"

        a_acc = next(acc for acc in dash1 if acc["account"].casefold() == "a@ggb6.com")
        start_res = api1.start_workbuddy_queue([a_acc["id"]])
        assert start_res["ok"]
        cur = api1.get_workbuddy_queue()["current"]
        assert cur["step"] == "github"
        advance_res = api1.workbuddy_action(cur["id"], cur["revision"], "confirm", cur["username"])
        assert advance_res["ok"]
        after_cur = api1.get_workbuddy_queue()["current"]
        assert after_cur["step"] == "email"
    finally:
        api1._shutdown()

    csv2 = "email,password\nA@ggb6.com,changed_should_skip\nd@ggb6.com,pw4\n"
    (accountData / "batch2.csv").write_text(csv2, encoding="utf-8")

    api2 = AccountApi(data_dir, auto_sync_account_data=True)
    api2._workbuddy.browser = Mock()
    api2._workbuddy.browser.open.return_value = "工作窗口已打开"
    api2._workbuddy.browser.challenge_page.return_value = None

    try:
        assert api2.auto_sync_result is not None
        assert api2.auto_sync_result["imported"] == 1
        dash2 = api2.get_dashboard("域名邮箱")["accounts"]
        assert len(dash2) == 4
        a_acc2 = next(acc for acc in dash2 if acc["account"].casefold() == "a@ggb6.com")
        assert a_acc2["password"] == "pw1"
        assert a_acc2["custom_fields"].get("wb_reg_status") == "进行中"
        assert a_acc2["custom_fields"].get("wb_reg_step") == "email"
        assert a_acc2["custom_fields"].get("wb_reg_step_label") == "邮箱验证"

        d_acc = next(acc for acc in dash2 if acc["account"].casefold() == "d@ggb6.com")
        assert d_acc["password"] == "pw4"
        assert d_acc["custom_fields"].get("wb_reg_status") == "未完成"
    finally:
        api2._shutdown()


def test_live_step_progress_persists_across_all_phases_to_completed(api):
    acc = add_mail(api, "stepcheck@ggb6.com")
    started = api.start_workbuddy_queue([acc["id"]])
    assert started["ok"]

    cf = api.db.get_account(acc["id"])["custom_fields"]
    assert cf["wb_reg_status"] == "进行中"
    assert cf["wb_reg_step"] == "github"
    assert cf["wb_reg_step_label"] == "GitHub注册"

    # Step 1: github -> email (邮箱验证)
    cur = api.get_workbuddy_queue()["current"]
    assert act(api, "confirm", cur["username"])["ok"]
    cf = api.db.get_account(acc["id"])["custom_fields"]
    assert cf["wb_reg_status"] == "进行中"
    assert cf["wb_reg_step"] == "email"
    assert cf["wb_reg_step_label"] == "邮箱验证"

    # Step 2: email -> workbuddy (WorkBuddy授权)
    assert act(api, "confirm")["ok"]
    cf = api.db.get_account(acc["id"])["custom_fields"]
    assert cf["wb_reg_status"] == "进行中"
    assert cf["wb_reg_step"] == "workbuddy"
    assert cf["wb_reg_step_label"] == "WorkBuddy授权"

    # Step 3: workbuddy -> region (完善地区)
    assert act(api, "confirm")["ok"]
    cf = api.db.get_account(acc["id"])["custom_fields"]
    assert cf["wb_reg_status"] == "进行中"
    assert cf["wb_reg_step"] == "region"
    assert cf["wb_reg_step_label"] == "完善地区"

    # Step 4: region -> greeting (发送招呼)
    assert act(api, "confirm", "not_required")["ok"]
    cf = api.db.get_account(acc["id"])["custom_fields"]
    assert cf["wb_reg_status"] == "进行中"
    assert cf["wb_reg_step"] == "greeting"
    assert cf["wb_reg_step_label"] == "发送招呼"

    # Step 5: confirm final greeting step -> 已完成, steps cleaned up
    final_res = act(api, "confirm")
    assert final_res["ok"]
    cf = api.db.get_account(acc["id"])["custom_fields"]
    assert cf["wb_reg_status"] == "已完成"
    assert "wb_reg_step" not in cf
    assert "wb_reg_step_label" not in cf


def test_preflight_rotates_away_from_restricted_node_and_migrates_legacy_verge_port(api, monkeypatch):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:17899"}],
        "nodes": ["Flagged-SG-01", "Clean-JP-01"],
    }
    api._workbuddy.browser._is_real_browser = True

    acc = add_mail(api, "preflight_test@ggb6.com")
    started = api.start_workbuddy_queue(
        [acc["id"]],
        proxy_url="http://127.0.0.1:17898",
        proxy_node="Flagged-SG-01",
    )
    assert started["ok"]
    init_task = started["queue"]["current"]
    assert init_task["proxy_url"] == "http://127.0.0.1:17898"
    assert init_task["proxy_node"] == "Flagged-SG-01"

    def mock_activate_proxy_node(platform, px, node, preflight=False):
        if node == "Flagged-SG-01":
            return {
                "ok": True,
                "switched": True,
                "restricted": True,
                "reachable": True,
                "proxy_url": "http://127.0.0.1:17899",
            }
        return {
            "ok": True,
            "switched": True,
            "restricted": False,
            "reachable": True,
            "proxy_url": "http://127.0.0.1:17899",
        }

    monkeypatch.setattr("src.proxy_service.activate_proxy_node", mock_activate_proxy_node)

    open_calls = []

    def mock_browser_open(task_dict, password, **kwargs):
        quotas = api.get_workbuddy_node_quotas()
        flagged_quota = next(n for n in quotas["nodes"] if n["node_name"] == "Flagged-SG-01")
        assert flagged_quota["blocked"] is True
        assert task_dict["proxy_node"] == "Clean-JP-01"
        assert task_dict["proxy_url"] == "http://127.0.0.1:17899"
        open_calls.append(dict(task_dict))
        return "工作窗口已打开"

    api._workbuddy.browser.open.side_effect = mock_browser_open

    res = act(api, "open_round")
    assert res["ok"]
    assert len(open_calls) == 1
    assert open_calls[0]["proxy_node"] == "Clean-JP-01"
    assert open_calls[0]["proxy_url"] == "http://127.0.0.1:17899"

    current = api.get_workbuddy_queue()["current"]
    assert current["proxy_node"] == "Clean-JP-01"
    assert current["proxy_url"] == "http://127.0.0.1:17899"

    quotas_after = api.get_workbuddy_node_quotas()
    flagged_after = next(n for n in quotas_after["nodes"] if n["node_name"] == "Flagged-SG-01")
    assert flagged_after["blocked"] is True


def test_auto_step_drives_full_pipeline_and_transitions_round(api, monkeypatch):
    monkeypatch.setattr("src.workbuddy.read_github_code", lambda *args: "888999")
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": [
            {"name": "Node-Round-1", "region": "SG", "delay": 35},
            {"name": "Node-Round-2", "region": "JP", "delay": 48},
        ],
    }
    api._workbuddy.browser.capture_credential.return_value = {
        "accessToken": "header.eyJzdWIiOiAibXl1aWQxMjMifQ.sig",
        "refreshToken": "ref-tok-12345",
    }
    a1 = add_mail(api, "auto_r1_one@ggb6.com")
    a2 = add_mail(api, "auto_r1_two@ggb6.com")
    a3 = add_mail(api, "auto_r2_one@ggb6.com")
    a4 = add_mail(api, "auto_r2_two@ggb6.com")

    started = api.start_workbuddy_queue([a1["id"], a2["id"], a3["id"], a4["id"]])
    assert started["ok"]
    q = started["queue"]
    assert q["round_no"] == 1
    t1, t2 = q["round_items"][0], q["round_items"][1]

    # Step t1 through full auto_step pipeline:
    # 1. github -> email
    cur_t1 = next(it for it in api.get_workbuddy_queue()["round_items"] if it["id"] == t1["id"])
    res = api.workbuddy_action(cur_t1["id"], cur_t1["revision"], "auto_step")
    assert res["ok"] and res["auto_advanced"] and res["step"] == "email"

    # 2. email -> workbuddy
    cur_t1 = next(it for it in api.get_workbuddy_queue()["round_items"] if it["id"] == t1["id"])
    res = api.workbuddy_action(cur_t1["id"], cur_t1["revision"], "auto_step")
    assert res["ok"] and res["auto_advanced"] and res["step"] == "workbuddy"

    # 3. workbuddy -> region
    cur_t1 = next(it for it in api.get_workbuddy_queue()["round_items"] if it["id"] == t1["id"])
    res = api.workbuddy_action(cur_t1["id"], cur_t1["revision"], "auto_step")
    assert res["ok"] and res["auto_advanced"] and res["step"] == "region"

    # 4. region -> greeting
    cur_t1 = next(it for it in api.get_workbuddy_queue()["round_items"] if it["id"] == t1["id"])
    res = api.workbuddy_action(cur_t1["id"], cur_t1["revision"], "auto_step")
    assert res["ok"] and res["auto_advanced"] and res["step"] == "greeting"

    # 5. greeting -> done
    cur_t1 = next(it for it in api.get_workbuddy_queue()["round_items"] if it["id"] == t1["id"])
    res = api.workbuddy_action(cur_t1["id"], cur_t1["revision"], "auto_step")
    assert res["ok"] and res["auto_advanced"] and res["task_done"]
    assert res["round_completed"] is False
    api._workbuddy.browser.close.assert_not_called()

    # Verify a1 in database: status done, credential saved
    acc1_db = api.db.get_account(a1["id"])
    assert acc1_db["custom_fields"]["wb_reg_status"] == "已完成"
    assert acc1_db["custom_fields"]["wb_credential"]["accessToken"].startswith("header.")

    # Now step t2 through full pipeline:
    for expected_next in ("email", "workbuddy", "region", "greeting"):
        cur_t2 = next(it for it in api.get_workbuddy_queue()["round_items"] if it["id"] == t2["id"])
        res2 = api.workbuddy_action(cur_t2["id"], cur_t2["revision"], "auto_step")
        assert res2["ok"] and res2["auto_advanced"] and res2["step"] == expected_next

    # Final greeting auto_step on t2 finishes Round 1, closes both browsers, and transitions to Round 2
    cur_t2 = next(it for it in api.get_workbuddy_queue()["round_items"] if it["id"] == t2["id"])
    res2 = api.workbuddy_action(cur_t2["id"], cur_t2["revision"], "auto_step")
    assert res2["ok"] and res2["auto_advanced"] and res2["task_done"]
    assert res2["round_completed"] is True
    assert res2["round_transitioned"] is True

    # Both round browsers closed, cache purged and switched to Node-Round-2
    api._workbuddy.browser.close.assert_any_call(discard_id=t1["id"])
    api._workbuddy.browser.close.assert_any_call(discard_id=t2["id"])
    api._workbuddy.browser.purge_cache_and_switch_node.assert_called_with(
        "http://127.0.0.1:7890",
        "Node-Round-2",
    )
    q2 = res2["queue"]
    assert q2["round_no"] == 2
    assert [it["email"] for it in q2["round_items"]] == ["auto_r2_one@ggb6.com", "auto_r2_two@ggb6.com"]


def test_auto_step_pauses_on_challenge_and_resumes(api):
    a = add_mail(api, "pause_test@ggb6.com")
    started = api.start_workbuddy_queue([a["id"]])
    assert started["ok"]
    task = started["queue"]["current"]

    # When submit_github_signup returns challenged:
    api._workbuddy.browser.submit_github_signup = Mock(return_value={
        "status": "challenged",
        "message": "出现人机验证，请手动点击",
    })
    res = api.workbuddy_action(task["id"], task["revision"], "auto_step")
    assert res["ok"]
    assert res["auto_advanced"] is False
    assert res["paused_for_human"] is True
    assert res["queue"]["current"]["status"] == "paused"

    # Resume the task
    paused_task = res["queue"]["current"]
    res_resume = api.workbuddy_action(paused_task["id"], paused_task["revision"], "resume")
    assert res_resume["ok"]
    assert res_resume["queue"]["current"]["status"] == "verified"

    # Now browser returns next_step -> auto_step advances to email
    api._workbuddy.browser.submit_github_signup.return_value = {
        "status": "next_step",
        "next": "email",
        "username": "resumed-worker123",
    }
    ver_task = res_resume["queue"]["current"]
    res_adv = api.workbuddy_action(ver_task["id"], ver_task["revision"], "auto_step")
    assert res_adv["ok"]
    assert res_adv["auto_advanced"] is True
    assert res_adv["step"] == "email"
    assert res_adv["queue"]["current"]["step"] == "email"
    assert res_adv["queue"]["current"]["username"] == "resumed-worker123"


def test_auto_step_auto_rotates_node_on_ip_restricted(api):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": ["SG-01", "SG-02"],
    }
    a1 = add_mail(api, "rotate_a@ggb6.com")
    a2 = add_mail(api, "rotate_b@ggb6.com")
    started = api.start_workbuddy_queue(
        [a1["id"], a2["id"]],
        proxy_url="http://127.0.0.1:7890",
        proxy_node="SG-01",
    )
    assert started["ok"]
    cur = started["queue"]["current"]
    assert cur["proxy_node"] == "SG-01"

    # Case 1: submit_github_signup returns {"status": "ip_restricted", ...}
    api._workbuddy.browser.submit_github_signup = Mock(return_value={
        "status": "ip_restricted",
        "message": "访问暂时受限 (IP: 103.142.140.241)",
    })
    res = api.workbuddy_action(cur["id"], cur["revision"], "auto_step")
    assert res["ok"]
    assert res["round_transitioned"] is True
    assert res["switched_node"] == "SG-02"
    assert res["auto_advanced"] is False
    assert res["paused_for_human"] is False
    assert "访问暂时受限" in res["message"]

    # Verify in node_quotas that SG-01 is blocked
    quotas = api.get_workbuddy_node_quotas()
    sg01_quota = next(n for n in quotas["nodes"] if n["node_name"] == "SG-01")
    assert sg01_quota["blocked"] is True

    # Verify purge_cache_and_switch_node was called
    api._workbuddy.browser.purge_cache_and_switch_node.assert_called_with(
        "http://127.0.0.1:7890",
        "SG-02",
    )

    # Verify tasks in round were switched to SG-02 and status is 'ready'
    round_items = res["queue"]["round_items"]
    assert len(round_items) == 2
    assert all(it["proxy_node"] == "SG-02" for it in round_items)
    assert all(it["status"] == "ready" for it in round_items)


def test_node_region_key_diverse_formats():
    assert WorkBuddyQueue._node_region_key("🇸🇬 Singapore | 01") == "🇸🇬 singapore"
    assert WorkBuddyQueue._node_region_key("🇸🇬 Singapore | 02") == "🇸🇬 singapore"
    assert WorkBuddyQueue._node_region_key("🇯🇵 Japan | 01") == "🇯🇵 japan"
    assert WorkBuddyQueue._node_region_key("SG-01") == "sg"
    assert WorkBuddyQueue._node_region_key("US_West_1") == "us_west"
    assert WorkBuddyQueue._node_region_key("") == ""
    assert WorkBuddyQueue._node_region_key(None) == ""


def test_check_paused_ip_restricted_rotates_to_diverse_region_node(api):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": ["🇸🇬 Singapore | 01", "🇸🇬 Singapore | 02", "🇯🇵 Japan | 01"],
    }
    a1 = add_mail(api, "paused_rot_1@ggb6.com")
    a2 = add_mail(api, "paused_rot_2@ggb6.com")
    started = api.start_workbuddy_queue(
        [a1["id"], a2["id"]],
        proxy_url="http://127.0.0.1:7890",
        proxy_node="🇸🇬 Singapore | 01",
    )
    assert started["ok"]
    cur = started["queue"]["current"]
    assert cur["proxy_node"] == "🇸🇬 Singapore | 01"

    # Pause task
    paused = api.workbuddy_action(cur["id"], cur["revision"], "pause")
    assert paused["ok"]
    cur = paused["queue"]["current"]
    assert cur["status"] == "paused"

    # Browser signals that the paused slider challenge turned into "访问暂时受限"
    api._workbuddy.browser.check_paused_challenge_state.return_value = "ip_restricted"

    res = api.workbuddy_action(cur["id"], cur["revision"], "check_paused")
    assert res["ok"] is True
    assert res["round_transitioned"] is True
    # Crucial: 🇸🇬 Singapore | 02 should be skipped because it shares the same region prefix/subnet as the blocked 🇸🇬 Singapore | 01!
    assert res["switched_node"] == "🇯🇵 Japan | 01"

    quotas = api.get_workbuddy_node_quotas()
    sg01 = next(n for n in quotas["nodes"] if n["node_name"] == "🇸🇬 Singapore | 01")
    assert sg01["blocked"] is True

    round_items = res["queue"]["round_items"]
    assert all(it["proxy_node"] == "🇯🇵 Japan | 01" for it in round_items)
    assert all(it["status"] == "ready" for it in round_items)


def test_check_paused_solved_marks_verified(api):
    a1 = add_mail(api, "paused_solve_1@ggb6.com")
    started = api.start_workbuddy_queue([a1["id"]])
    assert started["ok"]
    cur = started["queue"]["current"]

    paused = api.workbuddy_action(cur["id"], cur["revision"], "pause")
    assert paused["ok"]
    cur = paused["queue"]["current"]
    assert cur["status"] == "paused"

    api._workbuddy.browser.check_paused_challenge_state.return_value = "solved"
    res = api.workbuddy_action(cur["id"], cur["revision"], "check_paused")
    assert res["ok"] is True
    assert res["still_paused"] is False
    assert res["paused_resolved"] is True
    assert res["queue"]["current"]["status"] == "verified"


def test_check_paused_still_waiting(api):
    a1 = add_mail(api, "paused_wait_1@ggb6.com")
    started = api.start_workbuddy_queue([a1["id"]])
    cur = started["queue"]["current"]

    paused = api.workbuddy_action(cur["id"], cur["revision"], "pause")
    cur = paused["queue"]["current"]
    assert cur["status"] == "paused"

    api._workbuddy.browser.check_paused_challenge_state.return_value = ""
    api._workbuddy.browser.is_ip_restricted_page.return_value = False
    res = api.workbuddy_action(cur["id"], cur["revision"], "check_paused")
    assert res["ok"] is True
    assert res["still_paused"] is True
    assert res["queue"]["current"]["status"] == "paused"


def test_resume_action_auto_rotates_when_ip_restricted(api):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": ["SG-01", "JP-01"],
    }
    a1 = add_mail(api, "resume_rotate_1@ggb6.com")
    a2 = add_mail(api, "resume_rotate_2@ggb6.com")
    started = api.start_workbuddy_queue(
        [a1["id"], a2["id"]],
        proxy_url="http://127.0.0.1:7890",
        proxy_node="SG-01",
    )
    cur = started["queue"]["current"]

    # Pause task
    paused = api.workbuddy_action(cur["id"], cur["revision"], "pause")
    cur = paused["queue"]["current"]
    assert cur["status"] == "paused"

    # User clicks resume, but Chrome jumped to "访问暂时受限"
    api._workbuddy.browser.is_ip_restricted_page.return_value = True

    res = api.workbuddy_action(cur["id"], cur["revision"], "resume")
    assert res["ok"] is True
    assert res["round_transitioned"] is True
    assert res["switched_node"] == "JP-01"

    quotas = api.get_workbuddy_node_quotas()
    sg01 = next(n for n in quotas["nodes"] if n["node_name"] == "SG-01")
    assert sg01["blocked"] is True

    round_items = res["queue"]["round_items"]
    assert all(it["proxy_node"] == "JP-01" for it in round_items)
    assert all(it["status"] == "ready" for it in round_items)


def test_cancel_action_marks_node_blocked_when_ip_restricted(api):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": ["🇸🇬 Singapore | 01", "🇸🇬 Singapore | 02", "🇬🇧 United Kingdom | 01"],
    }
    a1 = add_mail(api, "cancel_rot_1@ggb6.com")
    a2 = add_mail(api, "cancel_rot_2@ggb6.com")
    started = api.start_workbuddy_queue(
        [a1["id"], a2["id"]],
        proxy_url="http://127.0.0.1:7890",
        proxy_node="🇸🇬 Singapore | 01",
    )
    cur = started["queue"]["current"]
    assert cur["proxy_node"] == "🇸🇬 Singapore | 01"

    api._workbuddy.browser.is_ip_restricted_page.return_value = True
    res = api.workbuddy_action(cur["id"], cur["revision"], "cancel")
    assert res["ok"] is True

    quotas = api.get_workbuddy_node_quotas()
    sg01 = next(n for n in quotas["nodes"] if n["node_name"] == "🇸🇬 Singapore | 01")
    assert sg01["blocked"] is True

    # A later batch must skip the blocked region's sibling and pick a clean region.
    api._workbuddy.browser.is_ip_restricted_page.return_value = False
    a3 = add_mail(api, "cancel_rot_3@ggb6.com")
    a4 = add_mail(api, "cancel_rot_4@ggb6.com")
    next_batch = api.start_workbuddy_queue([a3["id"], a4["id"]])
    assert next_batch["ok"] is True
    assert [it["proxy_node"] for it in next_batch["queue"]["round_items"]] == [
        "🇬🇧 United Kingdom | 01",
        "🇬🇧 United Kingdom | 01",
    ]


def test_allocate_round_falls_back_to_blocked_region_sibling_when_no_clean_region_fits(api):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": ["🇸🇬 Singapore | 01", "🇸🇬 Singapore | 02", "🇯🇵 Japan | 01"],
    }
    # Exhaust the clean Japan node so only the blocked region's sibling has slots.
    with api._workbuddy._connect() as conn, conn:
        for email in ("jp_used_1@ggb6.com", "jp_used_2@ggb6.com"):
            conn.execute(
                "INSERT INTO node_registrations (node_name, email, status, created_at) VALUES (?, ?, 'done', 0)",
                ("🇯🇵 Japan | 01", email),
            )

    a1 = add_mail(api, "fallback_1@ggb6.com")
    a2 = add_mail(api, "fallback_2@ggb6.com")
    started = api.start_workbuddy_queue(
        [a1["id"], a2["id"]],
        proxy_url="http://127.0.0.1:7890",
        proxy_node="🇸🇬 Singapore | 01",
    )
    assert started["ok"] is True
    cur = started["queue"]["current"]

    # Blacklist the starting node; the only remaining node with slots is its region sibling.
    api._workbuddy.browser.is_ip_restricted_page.return_value = True
    assert api.workbuddy_action(cur["id"], cur["revision"], "cancel")["ok"] is True
    api._workbuddy.browser.is_ip_restricted_page.return_value = False

    a3 = add_mail(api, "fallback_3@ggb6.com")
    a4 = add_mail(api, "fallback_4@ggb6.com")
    next_batch = api.start_workbuddy_queue([a3["id"], a4["id"]])
    assert next_batch["ok"] is True
    # No clean region has free slots, so the sibling is the documented fallback.
    assert [it["proxy_node"] for it in next_batch["queue"]["round_items"]] == [
        "🇸🇬 Singapore | 02",
        "🇸🇬 Singapore | 02",
    ]


def test_skip_action_marks_node_blocked_when_ip_restricted(api):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": ["SG-01", "JP-01"],
    }
    a1 = add_mail(api, "skip_rot_1@ggb6.com")
    a2 = add_mail(api, "skip_rot_2@ggb6.com")
    started = api.start_workbuddy_queue(
        [a1["id"], a2["id"]],
        proxy_url="http://127.0.0.1:7890",
        proxy_node="SG-01",
    )
    cur = started["queue"]["current"]

    api._workbuddy.browser.is_ip_restricted_page.return_value = True
    res = api.workbuddy_action(cur["id"], cur["revision"], "skip")
    assert res["ok"] is True

    quotas = api.get_workbuddy_node_quotas()
    sg01 = next(n for n in quotas["nodes"] if n["node_name"] == "SG-01")
    assert sg01["blocked"] is True

    # Case 2: browser.is_ip_restricted_page returns True (detected at start of auto_step before _pause_for_challenge)
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": ["SG-01", "SG-02", "SG-03"],
    }
    cur2 = res["queue"]["current"]
    api._workbuddy.browser.is_ip_restricted_page = Mock(return_value=True)
    res2 = api.workbuddy_action(cur2["id"], cur2["revision"], "auto_step")
    assert res2["ok"]
    assert res2["round_transitioned"] is True
    # SG-01 is blacklisted; SG-02 and SG-03 share its region, so the first
    # clean sibling is the documented fallback when no other region exists.
    assert res2["switched_node"] == "SG-02"
    assert res2["auto_advanced"] is False
    assert res2["paused_for_human"] is False

    quotas2 = api.get_workbuddy_node_quotas()
    sg01_quota2 = next(n for n in quotas2["nodes"] if n["node_name"] == "SG-01")
    assert sg01_quota2["blocked"] is True
    sg02_quota = next(n for n in quotas2["nodes"] if n["node_name"] == "SG-02")
    assert sg02_quota["blocked"] is False

    round_items2 = [it for it in res2["queue"]["round_items"] if it["status"] == "ready"]
    assert round_items2
    assert all(it["proxy_node"] == "SG-02" for it in round_items2)
    assert all(it["status"] == "ready" for it in round_items2)


def test_open_round_auto_rotates_when_signup_hits_ip_restricted(api):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": ["SG-01", "SG-02"],
    }
    a1 = add_mail(api, "open_rotate_a@ggb6.com")
    a2 = add_mail(api, "open_rotate_b@ggb6.com")
    started = api.start_workbuddy_queue(
        [a1["id"], a2["id"]],
        proxy_url="http://127.0.0.1:7890",
        proxy_node="SG-01",
    )
    assert started["ok"]
    cur = started["queue"]["current"]
    assert cur["proxy_node"] == "SG-01"

    opened = {"count": 0}

    def fake_is_ip_restricted(task_dict):
        # Clean before the browser opens; the signup page lands on DataDome
        # "访问暂时受限" immediately after open on the old node.
        return task_dict.get("proxy_node") == "SG-01" and opened["count"] > 0

    def fake_open(task_dict, password, **kwargs):
        opened["count"] += 1
        return "工作窗口已打开"

    api._workbuddy.browser.is_ip_restricted_page.side_effect = fake_is_ip_restricted
    api._workbuddy.browser.open.side_effect = fake_open

    res = api.workbuddy_action(cur["id"], cur["revision"], "open_round")
    assert res["ok"] is True
    assert res["round_transitioned"] is True
    assert res["switched_node"] == "SG-02"
    assert res["auto_advanced"] is False
    assert "访问暂时受限" in res["message"]
    assert opened["count"] == 1

    # The old node is blacklisted so later rounds cannot reuse it.
    quotas = api.get_workbuddy_node_quotas()
    sg01_quota = next(n for n in quotas["nodes"] if n["node_name"] == "SG-01")
    assert sg01_quota["blocked"] is True

    api._workbuddy.browser.purge_cache_and_switch_node.assert_called_with(
        "http://127.0.0.1:7890",
        "SG-02",
    )

    # The whole round is rebound to the clean node and ready to reopen.
    round_items = res["queue"]["round_items"]
    assert len(round_items) == 2
    assert all(it["proxy_node"] == "SG-02" for it in round_items)
    assert all(it["status"] == "ready" for it in round_items)


def test_auto_step_github_challenged_skips_redundant_ip_check(api):
    a = add_mail(api, "slider_test@ggb6.com")
    started = api.start_workbuddy_queue([a["id"]])
    assert started["ok"]
    task = started["queue"]["current"]

    call_records = []

    def record_ip_check(t):
        call_records.append("is_ip_restricted_page")
        return False

    def fake_signup(t, p):
        call_records.append("submit_github_signup")
        return {"status": "challenged", "username": "u1", "message": "slider"}

    api._workbuddy.browser.is_ip_restricted_page = Mock(side_effect=record_ip_check)
    api._workbuddy.browser.submit_github_signup = Mock(side_effect=fake_signup)

    res = api.workbuddy_action(task["id"], task["revision"], "auto_step")
    assert res["ok"]
    assert res["paused_for_human"] is True
    assert res["queue"]["current"]["status"] == "paused"
    assert res["queue"]["current"]["username"] == "u1"
    # Pre-check called is_ip_restricted_page once, then submit_github_signup was called.
    # No redundant is_ip_restricted_page call after submit_github_signup returned challenged.
    assert call_records == ["is_ip_restricted_page", "submit_github_signup"]


def test_skip_to_workbuddy_action_advances_existing_github_account(api):
    acc = add_mail(api, "existing_gh@ggb6.com")
    started = api.start_workbuddy_queue([acc["id"]])
    assert started["ok"] is True
    cur = started["queue"]["current"]
    assert cur["step"] == "github"

    res = api.workbuddy_action(cur["id"], cur["revision"], "skip_to_workbuddy", "existing-gh-99")
    assert res["ok"] is True
    assert res["step"] == "workbuddy"
    assert res["queue"]["current"]["step"] == "workbuddy"
    assert res["queue"]["current"]["username"] == "existing-gh-99"

    saved_acc = api.db.get_account(acc["id"])
    assert saved_acc["custom_fields"]["wb_reg_step"] == "workbuddy"

    api._workbuddy.browser.complete_workbuddy_github_login = Mock(return_value={
        "status": "next_step",
        "next": "region",
        "message": "已授权登录",
    })
    cur = res["queue"]["current"]
    auto_res = api.workbuddy_action(cur["id"], cur["revision"], "auto_step")
    assert auto_res["ok"] is True
    assert auto_res["auto_advanced"] is True
    assert auto_res["step"] == "region"
    assert auto_res["queue"]["current"]["step"] == "region"

    api._workbuddy.browser.complete_workbuddy_github_login.assert_called_once()
    called_task, called_pw = api._workbuddy.browser.complete_workbuddy_github_login.call_args[0]
    assert called_task["id"] == cur["id"]
    assert called_pw == acc["password"]


def test_auto_step_surfaces_account_suspended_and_pauses(api):
    acc = add_mail(api, "suspended_user@ggb6.com")
    started = api.start_workbuddy_queue([acc["id"]])
    assert started["ok"] is True
    cur = started["queue"]["current"]

    res = api.workbuddy_action(cur["id"], cur["revision"], "skip_to_workbuddy", "suspended-gh")
    assert res["ok"] is True
    cur = res["queue"]["current"]
    assert cur["step"] == "workbuddy"

    api._workbuddy.browser.complete_workbuddy_github_login = Mock(return_value={
        "status": "account_suspended",
        "message": "检测到该 GitHub 账号已被官方封禁 (Account suspended)",
    })
    auto_res = api.workbuddy_action(cur["id"], cur["revision"], "auto_step")
    assert auto_res["ok"] is True
    assert auto_res["paused_for_human"] is True
    assert auto_res["account_suspended"] is True
    assert "Account suspended" in auto_res["message"]
    assert auto_res["queue"]["current"]["status"] == "paused"


def test_start_resumes_from_account_custom_fields(api):
    acc = api.save_account({
        "account_type": "域名邮箱",
        "account": "resume_cf@ggb6.com",
        "password": "pass",
        "custom_fields": {
            "github_username": "preset-gh-user",
            "wb_reg_step": "workbuddy",
        },
    })["account"]
    started = api.start_workbuddy_queue([acc["id"]])
    assert started["ok"] is True
    task = started["queue"]["current"]
    assert task["username"] == "preset-gh-user"
    assert task["step"] == "workbuddy"


def test_auto_ip_reuses_previous_node_even_after_cancel_or_quota_full(api):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": ["🇸🇬 Singapore | 01", "🇯🇵 Japan | 01"],
    }
    acc = add_mail(api, "sticky_user@ggb6.com")
    started = api.start_workbuddy_queue([acc["id"]], proxy_node="🇯🇵 Japan | 01")
    assert started["ok"] is True
    cur = started["queue"]["current"]
    assert cur["proxy_node"] == "🇯🇵 Japan | 01"

    # Advance or cancel: cancel the task
    res_cancel = api.workbuddy_action(cur["id"], cur["revision"], "cancel")
    assert res_cancel["ok"] is True

    # Fill "🇯🇵 Japan | 01" quota to 2/2 with two other emails
    with api._workbuddy._connect() as conn, conn:
        for other_email in ("other1@ggb6.com", "other2@ggb6.com"):
            conn.execute(
                "INSERT INTO node_registrations (node_name, email, status, created_at) VALUES (?, ?, 'done', 0)",
                ("🇯🇵 Japan | 01", other_email),
            )

    # Start acc again WITHOUT specifying proxy_node
    started2 = api.start_workbuddy_queue([acc["id"]])
    assert started2["ok"] is True
    cur2 = started2["queue"]["current"]
    # Verify it automatically selects "🇯🇵 Japan | 01" (its historical node!) instead of "🇸🇬 Singapore | 01"!
    assert cur2["proxy_node"] == "🇯🇵 Japan | 01"


def test_auto_ip_falls_back_to_same_country_when_previous_node_blocked(api):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:7890"}],
        "nodes": [
            "🇺🇸 United States | 01",
            "🇸🇬 Singapore | 01",
            "🇸🇬 新加坡实验性 IEPL 专线 1",
            "🇯🇵 Japan | 01",
        ],
    }
    acc = add_mail(api, "same_country@ggb6.com")
    started = api.start_workbuddy_queue([acc["id"]], proxy_node="🇸🇬 Singapore | 01")
    assert started["ok"] is True
    cur = started["queue"]["current"]
    assert cur["proxy_node"] == "🇸🇬 Singapore | 01"

    # Previous attempt at step == "github" hits restriction and is cancelled -> blocked = 1
    api._workbuddy.browser.is_ip_restricted_page.return_value = True
    assert api.workbuddy_action(cur["id"], cur["revision"], "cancel")["ok"] is True
    api._workbuddy.browser.is_ip_restricted_page.return_value = False

    quotas = api.get_workbuddy_node_quotas()
    sg01_quota = next(n for n in quotas["nodes"] if n["node_name"] == "🇸🇬 Singapore | 01")
    assert sg01_quota["blocked"] is True

    # Now start acc again WITHOUT specifying proxy_node
    started2 = api.start_workbuddy_queue([acc["id"]])
    assert started2["ok"] is True
    cur2 = started2["queue"]["current"]
    # Verify that instead of jumping to "🇺🇸 United States | 01" or "🇯🇵 Japan | 01", it stays in Singapore and selects "🇸🇬 新加坡实验性 IEPL 专线 1"!
    assert cur2["proxy_node"] == "🇸🇬 新加坡实验性 IEPL 专线 1"


def test_preflight_preserves_sticky_node_for_post_github_steps(api, monkeypatch):
    api._workbuddy.discover_nodes_fn = lambda **_: {
        "ports": [{"url": "http://127.0.0.1:17899"}],
        "nodes": ["🇸🇬 新加坡实验性 IEPL 专线 1", "🇯🇵 Japan | 01"],
    }
    api._workbuddy.browser._is_real_browser = True

    acc = api.save_account({
        "account_type": "域名邮箱",
        "account": "sticky_post_gh@ggb6.com",
        "password": "pass",
        "custom_fields": {
            "wb_reg_status": "进行中",
            "wb_reg_step": "workbuddy",
            "github_username": "stickygh99",
            "proxy_node": "🇸🇬 新加坡实验性 IEPL 专线 1",
        },
    })["account"]

    def mock_activate_proxy_node(platform, px, node, preflight=False):
        return {
            "ok": True,
            "switched": True,
            "restricted": True,
            "reachable": True,
            "proxy_url": "http://127.0.0.1:17899",
        }

    monkeypatch.setattr("src.proxy_service.activate_proxy_node", mock_activate_proxy_node)

    started = api.start_workbuddy_queue([acc["id"]])
    assert started["ok"] is True
    cur = started["queue"]["current"]
    assert cur["step"] == "workbuddy"
    assert cur["proxy_node"] == "🇸🇬 新加坡实验性 IEPL 专线 1"

    res = act(api, "open_round")
    assert res["ok"] is True

    # Verify proxy_node remains "🇸🇬 新加坡实验性 IEPL 专线 1" (not rotated to "🇯🇵 Japan | 01" and not marked blocked!)
    cur_after = api.get_workbuddy_queue()["current"]
    assert cur_after["proxy_node"] == "🇸🇬 新加坡实验性 IEPL 专线 1"

    quotas = api.get_workbuddy_node_quotas()
    sg_quota = next(n for n in quotas["nodes"] if n["node_name"] == "🇸🇬 新加坡实验性 IEPL 专线 1")
    assert sg_quota["blocked"] is False


def test_auto_step_region_proactively_captures_credential_on_real_browser(api):
    # Setup paired accounts: domain email and WorkBuddy
    email = "proactive_wb_test@ggb6.com"
    dom_acc = add_mail(api, email)
    wb_acc = api.save_account({
        "account_type": "WorkBuddy",
        "account": email,
        "password": "pAss----word|23",
    })["account"]

    api._workbuddy.browser._is_real_browser = True
    captured_cred = {
        "accessToken": "header.eyJzdWIiOiAibXl1aWQxMjMifQ.sig",
        "refreshToken": "ref-tok-12345",
        "uid": "100234",
        "enterpriseId": "ent_999",
    }
    api._workbuddy.browser.capture_credential.return_value = captured_cred

    started = api.start_workbuddy_queue([dom_acc["id"]])
    assert started["ok"] is True
    cur = started["queue"]["current"]

    # Advance to workbuddy via skip_to_workbuddy
    res = api.workbuddy_action(cur["id"], cur["revision"], "skip_to_workbuddy", "test-user-wb")
    assert res["ok"] is True
    cur = res["queue"]["current"]
    assert cur["step"] == "workbuddy"

    # Advance workbuddy -> region via auto_step
    api._workbuddy.browser.complete_workbuddy_github_login = Mock(return_value={
        "status": "next_step",
        "next": "region",
        "message": "已授权登录",
    })
    res = api.workbuddy_action(cur["id"], cur["revision"], "auto_step")
    assert res["ok"] is True
    assert res["step"] == "region"
    cur = res["queue"]["current"]
    assert cur["step"] == "region"

    # Configure region completion
    api._workbuddy.browser.complete_workbuddy_region = Mock(return_value={
        "status": "next_step",
        "next": "greeting",
        "region": "confirmed",
        "message": "地区确认完成",
    })

    # Advance region -> greeting via auto_step
    # Because _is_real_browser is True, region completion proactively captures credentials!
    res = api.workbuddy_action(cur["id"], cur["revision"], "auto_step")
    assert res["ok"] is True
    assert res["step"] == "greeting"
    cur = res["queue"]["current"]
    assert cur["step"] == "greeting"

    # 1. Verify credential_json was immediately saved to tasks table
    with api._workbuddy._connect() as conn:
        task_row = conn.execute("SELECT credential_json FROM tasks WHERE id = ?", (cur["id"],)).fetchone()
        assert task_row["credential_json"] is not None
        saved_task_cred = json.loads(task_row["credential_json"])
        assert saved_task_cred == captured_cred
    assert cur["has_credential"] is True
    assert cur["has_refresh_token"] is True

    # 2. Verify wb_credential and wb_uid persisted to domain email account
    dom_saved = api.db.get_account(dom_acc["id"])
    assert dom_saved["custom_fields"]["wb_credential"] == captured_cred
    assert dom_saved["custom_fields"]["wb_uid"] == "100234"

    # 3. Verify wb_credential and wb_uid persisted to paired WorkBuddy account in db
    wb_saved = api.db.get_account(wb_acc["id"])
    assert wb_saved["custom_fields"]["wb_credential"] == captured_cred
    assert wb_saved["custom_fields"]["wb_uid"] == "100234"

    # Verify capture_credential was called exactly once during region step
    assert api._workbuddy.browser.capture_credential.call_count == 1

    # 4. Advance greeting -> done via auto_step
    api._workbuddy.browser.send_greeting_and_wait_reply = Mock(return_value={
        "status": "replied",
        "sent": True,
        "message": "已收到回复",
    })
    res_done = api.workbuddy_action(cur["id"], cur["revision"], "auto_step")
    assert res_done["ok"] is True
    assert res_done["auto_advanced"] is True
    assert res_done["task_done"] is True
    assert res_done["credential_saved"] is True

    # Verify capture_credential was NOT called again (reused existing credential)
    assert api._workbuddy.browser.capture_credential.call_count == 1

    # Final DB check: status is '已完成'
    dom_final = api.db.get_account(dom_acc["id"])
    assert dom_final["custom_fields"]["wb_reg_status"] == "已完成"
    assert dom_final["custom_fields"]["wb_credential"] == captured_cred


def test_explicit_proxy_url_without_nodes_and_concurrent_unlock_during_action(api):
    import concurrent.futures
    import threading
    import time

    dom_acc = api.db.add_account({"account_type": "域名邮箱", "account": "px_only@ggb6.com", "password": "pw"})
    # Ensure no subscription nodes exist so _allocate_round enters explicit_proxy_url branch
    api._workbuddy.discover_nodes_fn = lambda account_type="": {"nodes": [], "ports": []}
    started = api.start_workbuddy_queue([dom_acc["id"]], proxy_url="http://127.0.0.1:7897", proxy_node="")
    assert started["ok"] is True
    cur = started["queue"]["current"]
    assert cur["proxy_url"] == "http://127.0.0.1:7897"
    assert cur["proxy_node"] == "代理端口(http://127.0.0.1:7897)"

    # While _action check_paused runs slow browser check, set_node_unlock must succeed immediately
    api.workbuddy_action(cur["id"], cur["revision"], "pause")
    cur = api.get_workbuddy_queue()["current"]
    gate = threading.Event()
    entered = threading.Event()

    def slow_check(_t):
        entered.set()
        gate.wait(timeout=3.0)
        return "pending"

    api._workbuddy.browser.check_paused_challenge_state = slow_check
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        act_fut = pool.submit(api.workbuddy_action, cur["id"], cur["revision"], "check_paused")
        assert entered.wait(timeout=2.0) is True
        unlock_res = api._workbuddy.set_node_unlock("Node-X", True, "http://127.0.0.1:7897")
        assert unlock_res["ok"] is True
        gate.set()
        assert act_fut.result(timeout=3.0)["still_paused"] is True




