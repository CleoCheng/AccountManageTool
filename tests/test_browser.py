import json
import subprocess
import time
from pathlib import Path
import src.browser_service as browser_service
from src.browser_service import (
    generate_fingerprint_config,
    get_target_url,
    prepare_profile_dir,
    build_browser_launch_args,
)
from src.totp_service import generate_totp


def test_generate_unique_paired_fingerprints():
    fp1 = generate_fingerprint_config(1, "user1@example.com")
    fp2 = generate_fingerprint_config(2, "user2@example.com")

    assert fp1["profile_code"] == "FP-001"
    assert fp2["profile_code"] == "FP-002"
    assert fp1["canvas_seed"] != fp2["canvas_seed"]
    assert fp1["audio_seed"] != fp2["audio_seed"]
    assert "webgl_renderer" in fp1
    assert fp1["cpu_cores"] in (8, 12, 16, 20, 24)
    assert fp1["memory_gb"] in (8, 16, 32)


def test_prepare_profile_dir_and_extension(tmp_path: Path):
    data_dir = tmp_path / "data"
    fp = generate_fingerprint_config(10, "demo_account10@example.com", proxy="http://127.0.0.1:7890")
    secret = "JBSWY3DPEHPK3PXP"
    account = {
        "id": 10,
        "account_type": "ChatGPT",
        "account": "demo_account10@example.com",
        "password": "TestPassword123!",
        "secret_key": secret,
        "fp_config": fp,
    }

    profile_dir = prepare_profile_dir(data_dir, account)
    assert profile_dir == data_dir / "browsers" / "acc_10"
    ext_dir = profile_dir / "fp_ext"
    assert (ext_dir / "manifest.json").exists()
    assert (ext_dir / "inject.js").exists()
    assert (ext_dir / "autologin.js").exists()

    manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["content_scripts"][0]["world"] == "MAIN"
    assert manifest["content_scripts"][0]["run_at"] == "document_start"
    assert manifest["content_scripts"][1]["js"] == ["autologin.js"]

    inject_code = (ext_dir / "inject.js").read_text(encoding="utf-8")
    assert str(fp["canvas_seed"]) in inject_code
    assert fp["webgl_renderer"] in inject_code

    autologin_code = (ext_dir / "autologin.js").read_text(encoding="utf-8")
    assert "demo_account10@example.com" in autologin_code
    assert "TestPassword123!" in autologin_code
    assert secret in autologin_code

    # Verify JS TOTP implementation in autologin.js matches Python pyotp 100%
    now_ms = int(time.time() * 1000)
    expected_code = generate_totp(secret)["code"]
    js_runner = f"""
    const fs = require('fs');
    const code = fs.readFileSync({json.dumps(str(ext_dir / "autologin.js"))}, 'utf8');
    eval(code);
    process.stdout.write(globalThis.__kvComputeTotp({json.dumps(secret)}, {now_ms}));
    """
    proc = subprocess.run(["node", "-e", js_runner], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == expected_code

    args = build_browser_launch_args(account, profile_dir, exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
    assert any(a.startswith("--user-data-dir=") for a in args)
    assert any(a.startswith("--load-extension=") for a in args)
    assert any(a == "--proxy-server=http://127.0.0.1:7890" for a in args)
    assert args[-1] == "https://chatgpt.com/"


def test_get_target_url_domain_email():
    assert get_target_url("域名邮箱") == "https://mail.ggb6.com/"
    assert get_target_url("Google") == "https://accounts.google.com/"
    assert get_target_url("ChatGPT") == "https://chatgpt.com/"


def test_domain_email_extension_exposes_credentials_and_quick_links(tmp_path: Path):
    data_dir = tmp_path / "data"
    fp = generate_fingerprint_config(11, "mailbox11@staff.example")
    account = {
        "id": 11,
        "account_type": "域名邮箱",
        "account": "mailbox11@staff.example",
        "password": "DomainPass#2026",
        "fp_config": fp,
    }

    profile_dir = prepare_profile_dir(data_dir, account)
    assert profile_dir == data_dir / "browsers" / "acc_11"
    autologin_code = (profile_dir / "fp_ext" / "autologin.js").read_text(encoding="utf-8")

    assert "mailbox11@staff.example" in autologin_code
    assert "DomainPass#2026" in autologin_code
    assert '"isDomainEmail": true' in autologin_code
    assert '"suggestedUsername":' in autologin_code
    assert "https://github.com/" in autologin_code
    assert "https://github.com/signup" in autologin_code
    assert "https://www.workbuddy.ai/" in autologin_code
    assert "https://mail.ggb6.com/" in autologin_code
    assert "打开 GitHub 首页" in autologin_code
    assert "打开 GitHub 注册" in autologin_code
    assert "打开 WorkBuddy" in autologin_code
    assert "打开网页邮箱" in autologin_code
    assert "⚡ 自动登录邮箱" in autologin_code
    assert "runRoundcubeMailFlow(true)" in autologin_code
    # The GitHub helper is strictly manual: a capsule button, never a timer.
    assert "⚡ 填当前页表单" in autologin_code
    assert "runManualFormFill" in autologin_code
    assert "input#hero_user_email" in autologin_code
    assert "input[name=\"user[email]\"]" in autologin_code
    assert "input[name=\"user[password]\"]" in autologin_code
    assert "input[name=\"user[login]\"]" in autologin_code
    assert "AUTH.suggestedUsername" in autologin_code
    # ``rel="noopener"`` keeps the Referer for a warm first-party transition.
    assert "link.rel = 'noopener';" in autologin_code
    assert "noopener noreferrer" not in autologin_code

    manifest = json.loads((profile_dir / "fp_ext" / "manifest.json").read_text(encoding="utf-8"))
    matches = manifest["content_scripts"][1]["matches"]
    assert "https://github.com/*" in matches
    assert "https://mail.ggb6.com/*" in matches

    args = build_browser_launch_args(
        account, profile_dir, exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    )
    assert args[-1] == "https://mail.ggb6.com/"


def test_suggest_github_username_is_deterministic_and_valid():
    suggest = browser_service.suggest_github_username

    first = suggest("mailbox11@staff.example")
    assert first == suggest("mailbox11@staff.example")
    assert first != suggest("mailbox12@staff.example")
    # GitHub logins: 3-20 chars, alphanumerics only, never digit-first.
    for email in (
        "mailbox11@staff.example",
        "user.name+tag@example.org",
        "___@example.org",
        "1234567890@example.org",
        "a" * 40 + "@example.org",
        "",
    ):
        name = suggest(email)
        assert 3 <= len(name) <= 20, (email, name)
        assert name.isalnum(), (email, name)
        assert not name[0].isdigit(), (email, name)


def test_domain_email_launch_args_keep_zero_cdp_but_carry_locale(tmp_path: Path):
    data_dir = tmp_path / "data"
    fp = generate_fingerprint_config(
        13,
        "mailbox13@staff.example",
        proxy="http://127.0.0.1:17899",
        proxy_node="US | MiyaIP-2 HY2",
    )
    account = {
        "id": 13,
        "account_type": "域名邮箱",
        "account": "mailbox13@staff.example",
        "password": "DomainPass#2028",
        "fp_config": fp,
    }

    profile_dir = prepare_profile_dir(data_dir, account)
    args = build_browser_launch_args(
        account, profile_dir, exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    )

    assert any(a == f"--user-data-dir={profile_dir.resolve()}" for a in args)
    # The fingerprint / auto-login extension is loaded even though CDP stays off.
    assert f"--load-extension={(profile_dir / 'fp_ext').resolve()}" in args
    assert "--proxy-server=http://127.0.0.1:17899" in args
    # Proxied launches must close every local DNS / UDP leak path.
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert args.count("--force-webrtc-ip-handling-policy=disable_non_proxied_udp") == 1
    assert "--disable-quic" in args
    assert (
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1 , EXCLUDE localhost"
        in args
    )
    # Matched timezone/locale must never leak the host's Asia/Shanghai / zh-CN.
    assert "--lang=en-US" in args
    assert "--accept-lang=en-US,en" in args
    assert args.index("--lang=en-US") < args.index("https://mail.ggb6.com/")
    assert args.index("--accept-lang=en-US,en") < args.index("https://mail.ggb6.com/")
    assert (
        "--disable-features=WebRtcHideLocalIpsWithMdns,OptimizationHints,Translate,"
        "DisableLoadExtensionCommandLineSwitch"
    ) in args
    assert args[-1] == "https://mail.ggb6.com/"

    # ZERO-CDP INVARIANT: DataDome/Arkose must never see an attached inspector.
    assert not any(a.startswith("--remote-debugging-port=") for a in args)
    assert "--remote-allow-origins=*" not in args

    assert "--no-first-run" in args
    assert "--no-default-browser-check" in args
    assert "--disable-sync" in args

    # The matched locale is persisted before Chrome's first paint.
    prefs = json.loads((profile_dir / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["intl"]["accept_languages"] == "en-US,en"
    assert prefs["intl"]["selected_languages"] == "en-US,en"
    assert prefs["spellcheck"]["dictionaries"] == ["en-US"]

    env_cfg = json.loads((profile_dir / "fp_ext" / "env_config.json").read_text(encoding="utf-8"))
    assert env_cfg["timeZone"] == "America/Los_Angeles"
    assert env_cfg["locale"] == "en-US"
    assert env_cfg["enabled"] is True

    # Both injected scripts must be syntactically valid JavaScript.
    for name in ("inject.js", "autologin.js"):
        node_check = subprocess.run(
            ["node", "--check", str(profile_dir / "fp_ext" / name)],
            capture_output=True,
            text=True,
        )
        assert node_check.returncode == 0, (name, node_check.stderr)

    # Without a configured proxy the flag must simply be omitted.
    account_no_proxy = {**account, "fp_config": {**fp, "proxy": ""}}
    args_no_proxy = build_browser_launch_args(
        account_no_proxy, profile_dir, exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    )
    assert not any(a.startswith("--proxy-server=") for a in args_no_proxy)
    # No proxy means no tunnel DNS rewrite either: keep the plain launch intact.
    assert not any(a.startswith("--host-resolver-rules=") for a in args_no_proxy)
    assert "--disable-quic" not in args_no_proxy
    assert not any(a.startswith("--remote-debugging-port=") for a in args_no_proxy)
    assert args_no_proxy[-1] == "https://mail.ggb6.com/"


def test_non_domain_email_launch_args_keep_extension_and_cdp(tmp_path: Path):
    data_dir = tmp_path / "data"
    fp = generate_fingerprint_config(14, "chatgpt14@example.com", proxy="http://127.0.0.1:17899")
    account = {
        "id": 14,
        "account_type": "ChatGPT",
        "account": "chatgpt14@example.com",
        "password": "TestPassword123!",
        "fp_config": fp,
    }

    profile_dir = prepare_profile_dir(data_dir, account)
    args = build_browser_launch_args(
        account, profile_dir, exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    )

    assert any(a.startswith("--load-extension=") for a in args)
    assert not any(a.startswith("--remote-debugging-port=") for a in args)
    assert "--remote-allow-origins=*" not in args
    assert "--proxy-server=http://127.0.0.1:17899" in args
    # The shared isolation helper must not duplicate the flag the normal branch
    # already carries in its base arg list.
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert args.count("--force-webrtc-ip-handling-policy=disable_non_proxied_udp") == 1
    assert "--disable-quic" in args
    assert (
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1 , EXCLUDE localhost"
        in args
    )
    assert args[-1] == "https://chatgpt.com/"

    # Non-proxy launches keep the pre-existing WebRTC hardening only.
    account_no_proxy = {**account, "fp_config": {**fp, "proxy": ""}}
    args_no_proxy = build_browser_launch_args(
        account_no_proxy, profile_dir, exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    )
    assert not any(a.startswith("--proxy-server=") for a in args_no_proxy)
    assert not any(a.startswith("--host-resolver-rules=") for a in args_no_proxy)
    assert "--disable-quic" not in args_no_proxy
    assert args_no_proxy.count("--force-webrtc-ip-handling-policy=disable_non_proxied_udp") == 1
    assert args_no_proxy[-1] == "https://chatgpt.com/"


def test_launch_isolated_browser_skips_cdp_injector_for_domain_email(tmp_path: Path, monkeypatch):
    calls: list[tuple] = []
    popen_kwargs: list[dict] = []

    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: "C:\\fake\\chrome.exe")
    # Never let the test spawn a real sidecar kernel.
    monkeypatch.setattr(
        browser_service,
        "activate_proxy_node",
        lambda account_type, proxy_url, node_name: {"ok": True, "switched": False},
    )

    def _fake_popen(*a, **kw):
        calls.append(("popen", a))
        popen_kwargs.append(dict(kw))

    monkeypatch.setattr(browser_service.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        browser_service,
        "_start_cdp_injector",
        lambda *a, **kw: calls.append(("cdp", a, kw)),
    )

    fp = generate_fingerprint_config(15, "mailbox15@staff.example", proxy_node="US | MiyaIP-2 HY2")
    domain_account = {
        "id": 15,
        "account_type": "域名邮箱",
        "account": "mailbox15@staff.example",
        "password": "DomainPass#2029",
        "fp_config": fp,
    }
    result = browser_service.launch_isolated_browser(tmp_path / "data", domain_account)
    assert result["ok"] is True
    # Exactly one browser launch and no CDP injector thread.
    assert [c[0] for c in calls] == ["popen"]
    # The Chrome subprocess inherits the matched TZ/LANG so native JS never
    # reports the host's Asia/Shanghai clock.
    launch_env = popen_kwargs[-1]["env"]
    assert launch_env["TZ"] == "America/Los_Angeles"
    assert launch_env["LANG"] == "en_US.UTF-8"
    assert launch_env["LC_ALL"] == "en_US.UTF-8"

    calls.clear()
    popen_kwargs.clear()
    chatgpt_account = {**domain_account, "id": 16, "account_type": "ChatGPT", "account": "chatgpt16@example.com"}
    result = browser_service.launch_isolated_browser(tmp_path / "data", chatgpt_account)
    assert result["ok"] is True
    assert result["zero_cdp"] is True
    assert [c[0] for c in calls] == ["popen"]
    autologin_code = (tmp_path / "data" / "browsers" / "acc_16" / "fp_ext" / "autologin.js").read_text(
        encoding="utf-8"
    )
    assert "launchToken" in autologin_code
    assert "finishAutoLoginRound" in autologin_code
    assert "claimAutoStep" in autologin_code
    assert "⊟ 最小" in autologin_code
    assert "展开" in autologin_code
    assert "top: 6px; left: 50%; transform: translateX(-50%);" in autologin_code
    assert "Alt+K" in autologin_code
    assert "setNativeInputValue(target, out)" in autologin_code

    # Explicit enable_cdp=True keeps optional CDP debug flag available.
    args_cdp = build_browser_launch_args(
        chatgpt_account,
        tmp_path / "data" / "browsers" / "acc_16",
        exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        enable_cdp=True,
    )
    assert any(a.startswith("--remote-debugging-port=") for a in args_cdp)
    assert "--remote-allow-origins=*" in args_cdp


def test_launch_isolated_browser_applies_switched_node_proxy_url(tmp_path: Path, monkeypatch):
    launched: list[list[str]] = []
    switch_calls: list[tuple] = []

    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: "C:\\fake\\chrome.exe")
    monkeypatch.setattr(
        browser_service.subprocess,
        "Popen",
        lambda args, **kw: launched.append(list(args)),
    )
    monkeypatch.setattr(browser_service, "_start_cdp_injector", lambda *a, **kw: None)
    monkeypatch.setattr(
        browser_service,
        "activate_proxy_node",
        lambda account_type, proxy_url, node_name: (
            switch_calls.append((account_type, proxy_url, node_name))
            or {"ok": True, "switched": True, "proxy_url": "http://127.0.0.1:17899"}
        ),
    )

    # A stale Verge mixed port or an empty proxy must both end up on the sidecar.
    for stale_proxy in ("http://127.0.0.1:17898", ""):
        launched.clear()
        switch_calls.clear()
        fp = generate_fingerprint_config(21, "chatgpt21@example.com", proxy=stale_proxy)
        fp["proxy_node"] = "🇸🇬 Singapore | 01"
        account = {
            "id": 21,
            "account_type": "ChatGPT",
            "account": "chatgpt21@example.com",
            "password": "TestPassword123!",
            "fp_config": fp,
        }

        result = browser_service.launch_isolated_browser(tmp_path / "data", account)

        assert result["ok"] is True
        assert switch_calls == [("ChatGPT", stale_proxy, "🇸🇬 Singapore | 01")]
        assert result["fp_config"]["proxy"] == "http://127.0.0.1:17899"
        assert result["node_switched"] is True
        assert launched
        assert "--proxy-server=http://127.0.0.1:17899" in launched[0]
        assert not any("17898" in arg for arg in launched[0])


def test_launch_isolated_browser_starts_sidecar_for_17899_without_node(tmp_path: Path, monkeypatch):
    import src.sidecar_proxy as sidecar_module

    launched: list[list[str]] = []
    prepare_calls: list[dict] = []
    args_calls: list[dict] = []
    ensure_calls: list[bool] = []

    real_prepare = browser_service.prepare_profile_dir
    real_build_args = browser_service.build_browser_launch_args

    def recording_prepare(data_dir, account):
        prepare_calls.append(dict(account["fp_config"]))
        return real_prepare(data_dir, account)

    def recording_build_args(account, profile_dir, exe_path=None):
        args_calls.append(dict(account["fp_config"]))
        return real_build_args(account, profile_dir, exe_path=exe_path)

    class _StubManager:
        def ensure_started(self):
            ensure_calls.append(True)
            return {"ok": True, "proxy_url": "http://127.0.0.1:17911", "nodes": []}

    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: "C:\\fake\\chrome.exe")
    monkeypatch.setattr(
        browser_service.subprocess,
        "Popen",
        lambda args, **kw: launched.append(list(args)),
    )
    monkeypatch.setattr(browser_service, "_start_cdp_injector", lambda *a, **kw: None)
    monkeypatch.setattr(browser_service, "prepare_profile_dir", recording_prepare)
    monkeypatch.setattr(browser_service, "build_browser_launch_args", recording_build_args)
    monkeypatch.setattr(sidecar_module, "get_sidecar_proxy_manager", lambda: _StubManager())

    fp = generate_fingerprint_config(22, "chatgpt22@example.com", proxy="http://127.0.0.1:17899")
    account = {
        "id": 22,
        "account_type": "ChatGPT",
        "account": "chatgpt22@example.com",
        "password": "TestPassword123!",
        "fp_config": fp,
    }

    result = browser_service.launch_isolated_browser(tmp_path / "data", account)

    assert result["ok"] is True
    assert ensure_calls == [True]
    # The fallback port the sidecar actually bound must reach every consumer.
    assert result["fp_config"]["proxy"] == "http://127.0.0.1:17911"
    assert prepare_calls and prepare_calls[0]["proxy"] == "http://127.0.0.1:17911"
    assert args_calls and args_calls[0]["proxy"] == "http://127.0.0.1:17911"
    assert "--proxy-server=http://127.0.0.1:17911" in launched[0]

    # A failing sidecar leaves the configured proxy untouched.
    ensure_calls.clear()
    launched.clear()
    prepare_calls.clear()
    args_calls.clear()

    class _FailingManager:
        def ensure_started(self):
            ensure_calls.append(True)
            return {"ok": False, "error": "no kernel", "proxy_url": ""}

    monkeypatch.setattr(sidecar_module, "get_sidecar_proxy_manager", lambda: _FailingManager())
    result = browser_service.launch_isolated_browser(tmp_path / "data", account)
    assert result["ok"] is True
    assert ensure_calls == [True]
    assert result["fp_config"]["proxy"] == "http://127.0.0.1:17899"
    assert "--proxy-server=http://127.0.0.1:17899" in launched[0]

    # A non-sidecar proxy must never start the sidecar.
    ensure_calls.clear()
    launched.clear()
    verport_fp = generate_fingerprint_config(23, "chatgpt23@example.com", proxy="http://127.0.0.1:17898")
    result = browser_service.launch_isolated_browser(
        tmp_path / "data",
        {**account, "id": 23, "account": "chatgpt23@example.com", "fp_config": verport_fp},
    )
    assert ensure_calls == []
    assert "--proxy-server=http://127.0.0.1:17898" in launched[0]


def test_extension_roundcube_login_script(tmp_path: Path):
    data_dir = tmp_path / "data"
    fp = generate_fingerprint_config(12, "mailbox12@staff.example")
    account = {
        "id": 12,
        "account_type": "域名邮箱",
        "account": "mailbox12@staff.example",
        "password": "DomainPass#2027",
        "fp_config": fp,
    }

    profile_dir = prepare_profile_dir(data_dir, account)
    autologin_code = (profile_dir / "fp_ext" / "autologin.js").read_text(encoding="utf-8")

    # Roundcube login form automation
    assert "runRoundcubeMailFlow" in autologin_code
    assert "rcmloginuser" in autologin_code
    assert "input[name=\"_user\"]" in autologin_code
    assert "rcmloginpwd" in autologin_code
    assert "input[name=\"_pass\"]" in autologin_code
    assert "rcmloginsubmit" in autologin_code
    assert "form#login-form" in autologin_code
    assert "setNativeInputValue(userEl, AUTH.account)" in autologin_code
    assert "setNativeInputValue(pwdEl, AUTH.password)" in autologin_code
    assert "⚡ 已自动填入邮箱账号与密码，正在登录..." in autologin_code
    assert "✅ 已成功登录 GGB6 邮箱收件箱" in autologin_code
    assert "task-mail" in autologin_code

    # Dispatch: mail.ggb6.com / Roundcube login page routes to the mail flow
    assert "host.includes('mail.ggb6.com') || document.querySelector('#rcmloginuser')" in autologin_code

    # Roundcube JS is syntactically valid JavaScript
    node_check = subprocess.run(
        ["node", "--check", str(profile_dir / "fp_ext" / "autologin.js")],
        capture_output=True,
        text=True,
    )
    assert node_check.returncode == 0, node_check.stderr


# ---------------------------------------------------------------------------
# Claude category: target URL, timezone/locale anti-leak, proxy guard
# ---------------------------------------------------------------------------
def _claude_account(account_id: int = 31, proxy: str = "", proxy_node: str = "") -> dict:
    fp = generate_fingerprint_config(account_id, "claude_user@example.com")
    fp["proxy"] = proxy
    fp["proxy_node"] = proxy_node
    return {
        "id": account_id,
        "account_type": "Claude",
        "account": "claude_user@example.com",
        "password": "ClaudePass#2026",
        "fp_config": fp,
    }


def test_get_target_url_claude():
    assert get_target_url("Claude") == "https://claude.ai/"
    assert get_target_url("claude") == "https://claude.ai/"


def test_resolve_browser_env_config_us_relays_are_west_coast_not_japan():
    for node in ("USbyJPTT | MiyaIP-2 HY2", "USbyJP-54 | MiyaIP-2 HY2", "US | MiyaIP-2 HY2"):
        cfg = browser_service.resolve_browser_env_config(
            {"proxy_node": node, "proxy": "http://127.0.0.1:17899"},
            {"account_type": "Claude"},
        )
        assert cfg["timezone"] == "America/Los_Angeles", node
        assert cfg["locale"] == "en-US", node
        assert cfg["languages"] == ["en-US", "en"], node
        assert cfg["accept_languages"] == "en-US,en", node
        assert cfg["enabled"] is True, node

    # Genuine JP / SG nodes keep their own timezone.
    jp = browser_service.resolve_browser_env_config(
        {"proxy_node": "🇯🇵 Japan | 01"}, {"account_type": "Claude"}
    )
    assert jp["timezone"] == "Asia/Tokyo"
    assert jp["locale"] == "en-US"
    sg = browser_service.resolve_browser_env_config(
        {"proxy_node": "🇸🇬 Singapore | 01"}, {"account_type": "Claude"}
    )
    assert sg["timezone"] == "Asia/Singapore"
    tw = browser_service.resolve_browser_env_config(
        {"proxy_node": "🇹🇼 台湾 | 01"}, {"account_type": "Claude"}
    )
    assert tw["timezone"] == "Asia/Taipei"
    assert tw["locale"] == "zh-TW"
    assert tw["languages"] == ["zh-TW", "en-US", "en"]
    uk = browser_service.resolve_browser_env_config(
        {"proxy_node": "🇬🇧 London | 01"}, {"account_type": "Claude"}
    )
    assert uk["timezone"] == "Europe/London"
    assert uk["locale"] == "en-GB"
    hk = browser_service.resolve_browser_env_config(
        {"proxy_node": "🇭🇰 Hong Kong | 01"}, {"account_type": "Claude"}
    )
    assert hk["timezone"] == "Asia/Hong_Kong"

    # US East Coast keywords resolve to New York.
    east = browser_service.resolve_browser_env_config(
        {"proxy_node": "🇺🇸 New York | 01"}, {"account_type": "Claude"}
    )
    assert east["timezone"] == "America/New_York"
    assert east["locale"] == "en-US"


def test_resolve_browser_env_config_explicit_overrides_and_fallbacks():
    explicit = browser_service.resolve_browser_env_config(
        {
            "proxy_node": "🇯🇵 Japan | 01",
            "timezone": "Europe/Berlin",
            "locale": "de-DE",
        },
        {"account_type": "Claude"},
    )
    assert explicit["timezone"] == "Europe/Berlin"
    assert explicit["locale"] == "de-DE"
    assert explicit["languages"][0] == "de-DE"

    # Unknown region on a proxied Claude account defaults to US West.
    fallback = browser_service.resolve_browser_env_config(
        {"proxy_node": "🌍 Other | 01", "proxy": "http://127.0.0.1:17899"},
        {"account_type": "Claude"},
    )
    assert fallback["timezone"] == "America/Los_Angeles"
    assert fallback["locale"] == "en-US"

    # The dedicated Clash Verge AI-US port implies a US exit.
    port_cfg = browser_service.resolve_browser_env_config(
        {"proxy": "http://127.0.0.1:7897"}, {"account_type": "Claude"}
    )
    assert port_cfg["timezone"] == "America/Los_Angeles"

    # 域名邮箱 now resolves the matched profile too: its Chrome subprocess
    # must not leak the host's Asia/Shanghai clock or zh-CN UI language.
    domain_cfg = browser_service.resolve_browser_env_config(
        {"proxy_node": "🇺🇸 United States | 01"},
        {"account_type": "域名邮箱"},
    )
    assert domain_cfg["enabled"] is True
    assert domain_cfg["timezone"] == "America/Los_Angeles"
    assert domain_cfg["locale"] == "en-US"
    assert domain_cfg["languages"] == ["en-US", "en"]
    assert domain_cfg["accept_languages"] == "en-US,en"

    domain_sg = browser_service.resolve_browser_env_config(
        {"proxy_node": "🇸🇬 Singapore | 01"},
        {"account_type": "域名邮箱"},
    )
    assert domain_sg["timezone"] == "Asia/Singapore"
    assert domain_sg["locale"] == "en-US"

    domain_jp = browser_service.resolve_browser_env_config(
        {"proxy_node": "🇯🇵 Japan | 01"},
        {"account_type": "域名邮箱"},
    )
    assert domain_jp["timezone"] == "Asia/Tokyo"

    # A relay US node must never resolve to the JP hop's timezone.
    domain_relay = browser_service.resolve_browser_env_config(
        {"proxy_node": "USbyJPTT | MiyaIP-2 HY2"},
        {"account_type": "域名邮箱"},
    )
    assert domain_relay["timezone"] == "America/Los_Angeles"

    # No region hint at all still falls back to a coherent US profile.
    domain_fallback = browser_service.resolve_browser_env_config(
        {}, {"account_type": "域名邮箱"}
    )
    assert domain_fallback["enabled"] is True
    assert domain_fallback["timezone"] == "America/Los_Angeles"
    assert domain_fallback["locale"] == "en-US"


def test_build_browser_launch_args_for_claude_carries_locale_and_proxy_guards(tmp_path: Path):
    account = _claude_account(
        proxy="http://127.0.0.1:17899", proxy_node="US | MiyaIP-2 HY2"
    )
    profile_dir = prepare_profile_dir(tmp_path / "data", account)
    args = build_browser_launch_args(
        account,
        profile_dir,
        exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    )
    assert "--lang=en-US" in args
    assert "--accept-lang=en-US,en" in args
    assert "--proxy-server=http://127.0.0.1:17899" in args
    assert "--disable-quic" in args
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert args[-1] == "https://claude.ai/"
    # Locale flags must precede the target URL.
    assert args.index("--lang=en-US") < args.index("https://claude.ai/")
    assert args.index("--accept-lang=en-US,en") < args.index("https://claude.ai/")


def test_prepare_profile_dir_for_claude_writes_preferences_and_spoofing_scripts(tmp_path: Path):
    account = _claude_account(
        proxy="http://127.0.0.1:17899", proxy_node="USbyJPTT | MiyaIP-2 HY2"
    )
    profile_dir = prepare_profile_dir(tmp_path / "data", account)

    prefs_path = profile_dir / "Default" / "Preferences"
    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert prefs["intl"]["accept_languages"] == "en-US,en"
    assert prefs["intl"]["selected_languages"] == "en-US,en"
    assert prefs["spellcheck"]["dictionaries"] == ["en-US"]

    inject_code = (profile_dir / "fp_ext" / "inject.js").read_text(encoding="utf-8")
    assert '"timeZone": "America/Los_Angeles"' in inject_code
    assert '"locale": "en-US"' in inject_code
    assert "Intl.DateTimeFormat" in inject_code
    assert "getTimezoneOffset" in inject_code
    assert "Emulation" not in inject_code

    autologin_code = (profile_dir / "fp_ext" / "autologin.js").read_text(encoding="utf-8")
    assert "runClaudeFlow" in autologin_code
    assert "✅ 已成功登录 Claude" in autologin_code
    assert "host.includes('claude.ai') || host.includes('anthropic.com')" in autologin_code

    manifest = json.loads((profile_dir / "fp_ext" / "manifest.json").read_text(encoding="utf-8"))
    matches = manifest["content_scripts"][1]["matches"]
    assert "https://claude.ai/*" in matches
    assert "https://*.claude.ai/*" in matches
    assert "https://accounts.anthropic.com/*" in matches

    env_cfg = json.loads((profile_dir / "fp_ext" / "env_config.json").read_text(encoding="utf-8"))
    assert env_cfg == {
        "timeZone": "America/Los_Angeles",
        "locale": "en-US",
        "languages": ["en-US", "en"],
        "enabled": True,
    }

    # Both scripts must be valid JavaScript.
    for name in ("inject.js", "autologin.js"):
        node_check = subprocess.run(
            ["node", "--check", str(profile_dir / "fp_ext" / name)],
            capture_output=True,
            text=True,
        )
        assert node_check.returncode == 0, (name, node_check.stderr)


def test_inject_js_timezone_spoof_handles_dst_and_language(tmp_path: Path):
    account = _claude_account(
        proxy="http://127.0.0.1:17899", proxy_node="US | MiyaIP-2 HY2"
    )
    profile_dir = prepare_profile_dir(tmp_path / "data", account)
    inject_path = profile_dir / "fp_ext" / "inject.js"

    runner = f"""
    const fs = require('fs');
    eval(fs.readFileSync({json.dumps(str(inject_path))}, 'utf8'));
    const summer = new Date('2026-07-15T12:00:00Z');
    const winter = new Date('2026-01-15T12:00:00Z');
    const out = {{
      summerOffset: summer.getTimezoneOffset(),
      winterOffset: winter.getTimezoneOffset(),
      language: navigator.language,
      languages: navigator.languages,
      resolvedTz: new Intl.DateTimeFormat().resolvedOptions().timeZone,
      toString: summer.toString(),
      timeString: summer.toTimeString(),
      invalidOffset: String(new Date('not-a-date').getTimezoneOffset()),
    }};
    process.stdout.write(JSON.stringify(out));
    """
    proc = subprocess.run(["node", "-e", runner], capture_output=True, text=True, check=True)
    out = json.loads(proc.stdout)
    assert out["summerOffset"] == 420  # PDT (UTC-7)
    assert out["winterOffset"] == 480  # PST (UTC-8)
    assert out["language"] == "en-US"
    assert out["languages"] == ["en-US", "en"]
    assert out["resolvedTz"] == "America/Los_Angeles"
    assert "GMT-0700" in out["toString"]
    assert "Pacific" in out["toString"]
    assert "GMT-0700" in out["timeString"]
    assert "中国标准时间" not in out["toString"]
    assert out["invalidOffset"] == "NaN"


def test_inject_js_stealth_native_tostring_and_zero_own_props(tmp_path: Path):
    """DataDome's plv3 check must not see a patched-looking environment.

    The generated ``inject.js`` has to cloak every patched function as native,
    keep ``navigator`` free of own properties, and stay out of third-party
    captcha frames entirely.
    """
    data_dir = tmp_path / "data"

    # --- manifest: inject.js must never run inside captcha frames ---
    claude = _claude_account(proxy="http://127.0.0.1:17899", proxy_node="US | MiyaIP-2 HY2")
    claude_profile = prepare_profile_dir(data_dir, claude)
    manifest = json.loads(
        (claude_profile / "fp_ext" / "manifest.json").read_text(encoding="utf-8")
    )
    inject_entry = manifest["content_scripts"][0]
    assert inject_entry["js"] == ["inject.js"]
    assert inject_entry["all_frames"] is True
    exclude_matches = inject_entry["exclude_matches"]
    for pattern in (
        "*://*.captcha-delivery.com/*",
        "*://*.arkoselabs.com/*",
        "*://*.funcaptcha.com/*",
        "*://*.challenges.cloudflare.com/*",
    ):
        assert pattern in exclude_matches, pattern

    # --- both account flavours produce a fully cloaked script ---
    domain = {
        "id": 41,
        "account_type": "域名邮箱",
        "account": "mailbox41@staff.example",
        "password": "DomainPass#2031",
        "fp_config": generate_fingerprint_config(
            41, "mailbox41@staff.example", proxy_node="US | MiyaIP-2 HY2"
        ),
    }
    domain_profile = prepare_profile_dir(data_dir, domain)

    runner_template = r"""
    const fs = require('fs');
    function Navigator() {}
    globalThis.Navigator = Navigator;
    const nav = Object.create(Navigator.prototype);
    Object.defineProperty(globalThis, 'navigator', { value: nav, configurable: true });
    function Screen() {}
    globalThis.Screen = Screen;
    globalThis.window = { screen: Object.create(Screen.prototype) };
    eval(fs.readFileSync(__INJECT_PATH__, 'utf8'));
    const out = {
      dtfName: Intl.DateTimeFormat.name,
      dtfToString: Intl.DateTimeFormat.toString(),
      dtfFnToString: Function.prototype.toString.call(Intl.DateTimeFormat),
      tzOffsetToString: Date.prototype.getTimezoneOffset.toString(),
      dateToStringToString: Date.prototype.toString.toString(),
      fnToStringToString: Function.prototype.toString.toString(),
      leakedGlobal: '__kvGetTimezoneOffset' in globalThis,
      navOwnProps: Object.getOwnPropertyNames(nav),
      language: nav.language,
      languages: nav.languages,
    };
    process.stdout.write(JSON.stringify(out));
    """

    for label, profile_dir in (("Claude", claude_profile), ("域名邮箱", domain_profile)):
        inject_path = profile_dir / "fp_ext" / "inject.js"
        cfg = json.loads((profile_dir / "fp_ext" / "env_config.json").read_text(encoding="utf-8"))
        assert cfg["enabled"] is True

        runner = runner_template.replace("__INJECT_PATH__", json.dumps(str(inject_path)))
        proc = subprocess.run(["node", "-e", runner], capture_output=True, text=True, check=True)
        out = json.loads(proc.stdout)

        assert out["dtfName"] == "DateTimeFormat", (label, out)
        native_dtf = "function DateTimeFormat() { [native code] }"
        assert out["dtfToString"] == native_dtf, (label, out)
        assert out["dtfFnToString"] == native_dtf, (label, out)
        assert out["tzOffsetToString"] == "function getTimezoneOffset() { [native code] }", (label, out)
        assert out["dateToStringToString"] == "function toString() { [native code] }", (label, out)
        assert out["fnToStringToString"] == "function toString() { [native code] }", (label, out)
        # Zero custom globals leaked onto window / globalThis.
        assert out["leakedGlobal"] is False, (label, out)
        # Zero own properties added to the navigator instance.
        assert out["navOwnProps"] == [], (label, out)
        assert out["language"] == "en-US", (label, out)
        assert out["languages"] == ["en-US", "en"], (label, out)

    # 域名邮箱 keeps heavy fingerprint metrics native (no hardware spoofing).
    domain_inject = (domain_profile / "fp_ext" / "inject.js").read_text(encoding="utf-8")
    assert '"isDomainEmail": true' in domain_inject
    claude_inject = (claude_profile / "fp_ext" / "inject.js").read_text(encoding="utf-8")
    assert '"isDomainEmail": false' in claude_inject

    # Both injected scripts must be syntactically valid JavaScript.
    for profile_dir in (claude_profile, domain_profile):
        for name in ("inject.js", "autologin.js"):
            node_check = subprocess.run(
                ["node", "--check", str(profile_dir / "fp_ext" / name)],
                capture_output=True,
                text=True,
            )
            assert node_check.returncode == 0, (name, node_check.stderr)


def test_launch_isolated_browser_claude_refuses_direct_unproxied_launch(tmp_path: Path, monkeypatch):
    import src.sidecar_proxy as sidecar_module

    launched: list[list[str]] = []

    class _EmptySidecar:
        def describe_environment(self, account_type=""):
            return {
                "nodes": [],
                "ports": [
                    {
                        "port": 17899,
                        "url": "http://127.0.0.1:17899",
                        "label": "sidecar",
                        "bound_group": "KEYVAULT_PROXY",
                    }
                ],
            }

    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: "C:\\fake\\chrome.exe")
    monkeypatch.setattr(
        browser_service.subprocess,
        "Popen",
        lambda args, **kw: launched.append(list(args)),
    )
    monkeypatch.setattr(browser_service, "_start_cdp_injector", lambda *a, **kw: None)
    monkeypatch.setattr(browser_service, "activate_proxy_node", lambda *a, **kw: {"ok": False, "switched": False})
    monkeypatch.setattr(browser_service, "_is_local_port_open", lambda *a, **kw: False)
    monkeypatch.setattr(sidecar_module, "get_sidecar_proxy_manager", lambda: _EmptySidecar())

    result = browser_service.launch_isolated_browser(tmp_path / "data", _claude_account())
    assert result["ok"] is False
    assert "Claude 账号禁止无代理直连启动" in result["error"]
    assert launched == []

    # A configured sidecar URL that never came up must not be trusted either.
    dead_sidecar = browser_service.launch_isolated_browser(
        tmp_path / "data",
        _claude_account(
            account_id=33,
            proxy="http://127.0.0.1:17899",
            proxy_node="US | MiyaIP-2 HY2",
        ),
    )
    assert dead_sidecar["ok"] is False
    assert "未启动" in dead_sidecar["error"]
    assert launched == []


def test_launch_isolated_browser_claude_auto_activates_top_us_sidecar_node(
    tmp_path: Path, monkeypatch
):
    import src.sidecar_proxy as sidecar_module

    launched: list[list[str]] = []
    switch_calls: list[tuple] = []

    class _StubSidecar:
        def describe_environment(self, account_type=""):
            assert account_type == "Claude"
            return {
                "nodes": [
                    {"name": "US | MiyaIP-2 HY2"},
                    {"name": "USbyJPTT | MiyaIP-2 HY2"},
                    {"name": "🇭🇰 Hong Kong | 01"},
                ],
                "ports": [
                    {
                        "port": 17899,
                        "url": "http://127.0.0.1:17899",
                        "label": "sidecar",
                        "bound_group": "KEYVAULT_PROXY",
                    }
                ],
            }

    def fake_activate(account_type, proxy_url, node_name, **kwargs):
        switch_calls.append((account_type, proxy_url, node_name))
        return {"ok": True, "switched": True, "proxy_url": "http://127.0.0.1:17899"}

    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: "C:\\fake\\chrome.exe")
    monkeypatch.setattr(
        browser_service.subprocess,
        "Popen",
        lambda args, **kw: launched.append(list(args)),
    )
    monkeypatch.setattr(browser_service, "_start_cdp_injector", lambda *a, **kw: None)
    monkeypatch.setattr(browser_service, "activate_proxy_node", fake_activate)
    monkeypatch.setattr(sidecar_module, "get_sidecar_proxy_manager", lambda: _StubSidecar())

    result = browser_service.launch_isolated_browser(tmp_path / "data", _claude_account())
    assert result["ok"] is True, result
    assert switch_calls == [("Claude", "http://127.0.0.1:17899", "US | MiyaIP-2 HY2")]
    assert result["fp_config"]["proxy"] == "http://127.0.0.1:17899"
    assert result["fp_config"]["proxy_node"] == "US | MiyaIP-2 HY2"
    assert result["node_switched"] is True
    assert "--proxy-server=http://127.0.0.1:17899" in launched[0]
    assert launched[0][-1] == "https://claude.ai/"
    assert "--lang=en-US" in launched[0]


def test_launch_isolated_browser_claude_falls_back_to_live_ai_us_port(
    tmp_path: Path, monkeypatch
):
    import src.sidecar_proxy as sidecar_module

    launched: list[list[str]] = []
    open_ports: set[int] = set()

    class _NoNodesSidecar:
        def describe_environment(self, account_type=""):
            return {
                "nodes": [],
                "ports": [
                    {
                        "port": 17899,
                        "url": "http://127.0.0.1:17899",
                        "label": "sidecar",
                        "bound_group": "KEYVAULT_PROXY",
                    },
                    {
                        "port": 7897,
                        "url": "http://127.0.0.1:7897",
                        "label": "127.0.0.1:7897 (Clash Verge AI-US 固定美国出口)",
                        "bound_group": "US",
                    },
                ],
            }

    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: "C:\\fake\\chrome.exe")
    monkeypatch.setattr(
        browser_service.subprocess,
        "Popen",
        lambda args, **kw: launched.append(list(args)),
    )
    monkeypatch.setattr(browser_service, "_start_cdp_injector", lambda *a, **kw: None)
    monkeypatch.setattr(browser_service, "activate_proxy_node", lambda *a, **kw: {"ok": False, "switched": False})
    monkeypatch.setattr(
        browser_service,
        "_is_local_port_open",
        lambda port, timeout=0.08: int(port) in open_ports,
    )
    monkeypatch.setattr(sidecar_module, "get_sidecar_proxy_manager", lambda: _NoNodesSidecar())

    open_ports.add(7897)
    result = browser_service.launch_isolated_browser(tmp_path / "data", _claude_account())
    assert result["ok"] is True, result
    assert "--proxy-server=http://127.0.0.1:7897" in launched[0]
    assert result["fp_config"]["proxy"] == "http://127.0.0.1:7897"

    # A bound but dead Clash Verge port must fail loudly, never launch direct.
    launched.clear()
    open_ports.clear()
    dead_fp = generate_fingerprint_config(32, "claude_dead@example.com", proxy="http://127.0.0.1:7897")
    result = browser_service.launch_isolated_browser(
        tmp_path / "data",
        {
            "id": 32,
            "account_type": "Claude",
            "account": "claude_dead@example.com",
            "password": "pw",
            "fp_config": dead_fp,
        },
    )
    assert result["ok"] is False
    assert "未在监听" in result["error"]
    assert launched == []


def test_launch_isolated_browser_claude_sets_tz_and_lang_env(tmp_path: Path, monkeypatch):
    import src.sidecar_proxy as sidecar_module

    popen_kwargs: list[dict] = []

    class _StubSidecar:
        def describe_environment(self, account_type=""):
            return {
                "nodes": [{"name": "US | MiyaIP-2 HY2"}],
                "ports": [
                    {
                        "port": 17899,
                        "url": "http://127.0.0.1:17899",
                        "label": "sidecar",
                        "bound_group": "KEYVAULT_PROXY",
                    }
                ],
            }

    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: "C:\\fake\\chrome.exe")
    monkeypatch.setattr(
        browser_service.subprocess,
        "Popen",
        lambda args, **kw: popen_kwargs.append(dict(kw)),
    )
    monkeypatch.setattr(browser_service, "_start_cdp_injector", lambda *a, **kw: None)
    monkeypatch.setattr(
        browser_service,
        "activate_proxy_node",
        lambda *a, **kw: {"ok": True, "switched": True, "proxy_url": "http://127.0.0.1:17899"},
    )
    monkeypatch.setattr(sidecar_module, "get_sidecar_proxy_manager", lambda: _StubSidecar())

    browser_service.launch_isolated_browser(tmp_path / "data", _claude_account())
    assert popen_kwargs
    env = popen_kwargs[0]["env"]
    assert env["TZ"] == "America/Los_Angeles"
    assert env["LANG"] == "en_US.UTF-8"
    assert popen_kwargs[0]["cwd"]
    assert popen_kwargs[0]["close_fds"] is True


def test_prepare_profile_seeds_bookmarks_and_preferences(tmp_path: Path):
    import json

    data_dir = tmp_path / "data"
    account = {
        "id": 99,
        "account_type": "Google",
        "account": "bookmark_tester@gmail.com",
        "password": "pw",
    }
    profile_dir = browser_service.prepare_profile_dir(data_dir, account)
    prefs = json.loads((profile_dir / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["bookmark_bar"]["show_on_all_tabs"] is True
    assert prefs["webrtc"]["ip_handling_policy"] == "disable_non_proxied_udp"

    bm = json.loads((profile_dir / "Default" / "Bookmarks").read_text(encoding="utf-8"))
    children = bm["roots"]["bookmark_bar"]["children"]
    assert children[0]["name"] == "KeyVault 维护助手"
    urls = {node["url"] for node in children[0]["children"]}
    assert "https://myaccount.google.com/security" in urls
    assert "https://forms.gle/hGzM9MEUv2azZsrb9" in urls


def test_pure_native_and_target_url_and_reset_rejected_session(tmp_path: Path):
    data_dir = tmp_path / "data"
    profile_dir = browser_service.prepare_profile_dir(
        data_dir,
        {"id": 101, "account_type": "Google", "account": "native_test@gmail.com"},
    )
    (profile_dir / "DevToolsActivePort").write_text("9222\n/devtools/browser/1", encoding="utf-8")
    reset_res = browser_service.reset_rejected_login_session(data_dir, 101)
    assert reset_res["ok"] is True
    assert "DevToolsActivePort" in reset_res["removed"]
    assert not (profile_dir / "DevToolsActivePort").exists()

    custom_url = "https://accounts.google.com/o/oauth2/v2/auth?client_id=test"
    args = browser_service.build_browser_launch_args(
        {
            "id": 101,
            "account_type": "Google",
            "account": "native_test@gmail.com",
            "fp_config": {"proxy": "http://127.0.0.1:7890"},
        },
        profile_dir,
        exe_path="C:\\fake\\chrome.exe",
        target_url=custom_url,
        pure_native=True,
    )
    assert args[-1] == custom_url
    assert not any(a.startswith("--load-extension=") for a in args)
    assert not any(a.startswith("--remote-debugging-port=") for a in args)
    assert "--proxy-bypass-list=<-loopback>;localhost;127.0.0.1;[::1]" in args


def test_proxy_relay_parses_4part_credentials():
    from src.maintenance_service import resolve_proxy_for_chromium_and_urllib, shutdown_auth_relays

    try:
        resolved = resolve_proxy_for_chromium_and_urllib("127.0.0.1:8888:user1:pass1")
        assert resolved["uses_relay"] is True
        assert resolved["chromium_proxy"].startswith("http://127.0.0.1:")
        assert resolved["urllib_proxy"] == "http://user1:pass1@127.0.0.1:8888"
    finally:
        shutdown_auth_relays()


def test_autologin_top_bar_draggable_mini_capsule_and_one_shot_round(tmp_path: Path):
    data_dir = tmp_path / "data"
    profile_dir = browser_service.prepare_profile_dir(
        data_dir,
        {
            "id": 102,
            "account_type": "Google",
            "account": "oneshot@gmail.com",
            "password": "OneShotPassword#1",
            "secret_key": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
        },
    )
    autologin_path = profile_dir / "fp_ext" / "autologin.js"
    node_check = subprocess.run(
        ["node", "--check", str(autologin_path)],
        capture_output=True,
        text=True,
    )
    assert node_check.returncode == 0, node_check.stderr

    manifest = json.loads((profile_dir / "fp_ext" / "manifest.json").read_text(encoding="utf-8"))
    assert "storage" in (manifest.get("permissions") or [])

    # Behavioral Node harness simulating chrome.storage.local across 3 scenarios:
    # 1) ChatGPT already logged in -> immediately closes auto-login (autoLoginDone = true)
    # 2) Google login flow -> each step executes at most once automatically, finishes at myaccount.google.com
    # 3) Top bar minimize -> mini capsule drag persists pos & minimized state
    harness = f"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync({json.dumps(str(autologin_path))}, 'utf8');

function createHarness(initialHost, initialPath, selectorsMap, storeSeed) {{
  const storageObj = storeSeed || {{}};
  const intervals = [];
  const timeouts = [];
  let mountedShadow = null;

  class El {{
    constructor(tag) {{
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.listeners = {{}};
      this.style = {{ cssText: '' }};
      this.className = '';
      this.textContent = '';
      this.title = '';
      this.value = '';
      this.disabled = false;
      this.readOnly = false;
      this.isConnected = true;
      this._rect = {{ left: 400, top: 6, width: 520, height: 34 }};
    }}
    append(...nodes) {{ nodes.forEach(n => this.appendChild(n)); }}
    appendChild(n) {{ if (n) {{ n.parentElement = this; this.children.push(n); }} return n; }}
    replaceChildren(...nodes) {{ this.children = []; this.append(...nodes); }}
    remove() {{ this.removed = true; }}
    addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }}
    removeEventListener(ev, fn) {{
      if (this.listeners[ev]) this.listeners[ev] = this.listeners[ev].filter(f => f !== fn);
    }}
    dispatchEvent(ev) {{
      const handlers = this.listeners[ev.type] || [];
      handlers.forEach(fn => fn(ev));
      return true;
    }}
    focus() {{}}
    click() {{ if (typeof this.onclick === 'function') this.onclick({{ stopPropagation() {{}}, preventDefault() {{}} }}); }}
    getBoundingClientRect() {{ return this._rect; }}
    closest(sel) {{
      if (sel.includes('button') && this.tagName === 'BUTTON') return this;
      if (sel.includes('a') && this.tagName === 'A') return this;
      return null;
    }}
    attachShadow() {{
      const sr = new El('#shadow');
      mountedShadow = sr;
      return sr;
    }}
    querySelector(sel) {{
      const list = this.querySelectorAll(sel);
      return list[0] || null;
    }}
    querySelectorAll(sel) {{
      const res = [];
      const walk = (node) => {{
        for (const c of (node.children || [])) {{
          if (sel.split(',').some(s => {{
            s = s.trim();
            if (s.startsWith('.')) return (' ' + (c.className || '') + ' ').includes(' ' + s.slice(1) + ' ');
            return c.tagName && c.tagName.toLowerCase() === s.toLowerCase();
          }})) res.push(c);
          walk(c);
        }}
      }};
      walk(this);
      return res;
    }}
  }}

  const docEl = new El('html');
  const bodyEl = new El('body');
  docEl.appendChild(bodyEl);
  const docListeners = {{}};
  const winListeners = {{}};
  const sess = new Map();

  const ctx = {{
    console, Math, Date, JSON, Uint8Array, Uint32Array, DataView, Array, Object, String, Number, Boolean, Error, RegExp, Map, Set, URL,
    CSS: {{ escape: (s) => s }},
    HTMLInputElement: {{ prototype: Object.getOwnPropertyDescriptor({{ set value(v){{}}, get value(){{ return ''; }} }}, 'value') ? {{}} : {{}} }},
    KeyboardEvent: function(t, o) {{ this.type = t; Object.assign(this, o); }},
    InputEvent: function(t, o) {{ this.type = t; Object.assign(this, o); }},
    MouseEvent: function(t, o) {{ this.type = t; Object.assign(this, o); }},
    Event: function(t, o) {{ this.type = t; Object.assign(this, o); }},
    crypto: {{ getRandomValues: (arr) => {{ for (let i = 0; i < arr.length; i++) arr[i] = i + 1; return arr; }} }},
    navigator: {{ clipboard: {{ writeText: () => Promise.resolve() }} }},
    chrome: {{
      storage: {{
        local: {{
          get: (keys, cb) => cb({{ ...storageObj }}),
          set: (obj) => Object.assign(storageObj, obj),
        }}
      }}
    }},
    sessionStorage: {{
      getItem: (k) => sess.has(k) ? sess.get(k) : null,
      setItem: (k, v) => sess.set(k, String(v)),
      removeItem: (k) => sess.delete(k),
      get length() {{ return sess.size; }},
      key: (i) => Array.from(sess.keys())[i] || null,
    }},
    setTimeout: (fn, ms) => {{ timeouts.push(fn); return timeouts.length; }},
    setInterval: (fn, ms) => {{ intervals.push(fn); return intervals.length; }},
    requestAnimationFrame: (fn) => fn(),
    location: {{ protocol: 'https:', hostname: initialHost, pathname: initialPath }},
    document: {{
      readyState: 'complete',
      documentElement: docEl,
      body: bodyEl,
      createElement: (tag) => new El(tag),
      getElementById: (id) => (docEl.children.find(c => c.id === id) || null),
      querySelector: (sel) => {{
        for (const k of sel.split(',').map(s => s.trim())) {{
          if (selectorsMap[k]) return Array.isArray(selectorsMap[k]) ? selectorsMap[k][0] : selectorsMap[k];
        }}
        return null;
      }},
      querySelectorAll: (sel) => {{
        for (const k of sel.split(',').map(s => s.trim())) {{
          if (selectorsMap[k]) return Array.isArray(selectorsMap[k]) ? selectorsMap[k] : [selectorsMap[k]];
        }}
        return [];
      }},
      addEventListener: (ev, fn) => {{ (docListeners[ev] = docListeners[ev] || []).push(fn); }},
    }},
    window: null,
    globalThis: null,
  }};
  Object.defineProperty(ctx.HTMLInputElement.prototype, 'value', {{
    configurable: true,
    get() {{ return this._val || ''; }},
    set(v) {{ this._val = v; }}
  }});
  ctx.window = ctx;
  ctx.top = ctx;
  ctx.globalThis = ctx;
  ctx.innerWidth = 1280;
  ctx.innerHeight = 800;
  ctx.getComputedStyle = () => ({{ display: 'block', visibility: 'visible', opacity: '1' }});
  ctx.addEventListener = (ev, fn) => {{ (winListeners[ev] = winListeners[ev] || []).push(fn); }};
  ctx.removeEventListener = (ev, fn) => {{
    if (winListeners[ev]) winListeners[ev] = winListeners[ev].filter(f => f !== fn);
  }};

  vm.runInNewContext(code, ctx);
  return {{ ctx, storageObj, timeouts, winListeners, getShadow: () => mountedShadow }};
}}

// Scenario 1: ChatGPT already signed in -> closes auto-login immediately
const s1 = createHarness('chatgpt.com', '/', {{ '#prompt-textarea': {{ tagName: 'DIV' }} }}, {{}});
if (!s1.storageObj.kv_launch_state || !s1.storageObj.kv_launch_state.autoLoginDone) {{
  throw new Error('Expected already-logged-in ChatGPT to set autoLoginDone=true');
}}

// Scenario 2: Google login step runs ONCE on accounts.google.com, does NOT repeat on second tick, then finishes on myaccount.google.com
const sharedStore = {{}};
const emailBox = {{
  tagName: 'INPUT', isConnected: true, disabled: false, readOnly: false, _val: '',
  focus() {{}}, dispatchEvent() {{}},
  getBoundingClientRect() {{ return {{ width: 200, height: 30 }}; }},
}};
let nextClicks = 0;
const nextBtn = {{
  tagName: 'BUTTON', isConnected: true, disabled: false, innerText: 'Next',
  focus() {{}}, dispatchEvent() {{}},
  click() {{ nextClicks++; }},
  getBoundingClientRect() {{ return {{ width: 80, height: 30 }}; }},
}};
const s2 = createHarness('accounts.google.com', '/signin', {{
  'input#identifierId': emailBox,
  '#identifierNext button': nextBtn,
}}, sharedStore);
if (!s2.storageObj.kv_launch_state.stepsDone.google_email) {{
  throw new Error('Expected google_email step claimed');
}}
s2.timeouts.forEach(fn => fn());
if (nextClicks !== 1) throw new Error('Expected 1 Next click, got ' + nextClicks);

// Simulate reload back to same login form with SAME sharedStore: must NOT click Next again!
emailBox._val = '';
const s2b = createHarness('accounts.google.com', '/signin', {{
  'input#identifierId': emailBox,
  '#identifierNext button': nextBtn,
}}, sharedStore);
s2b.timeouts.forEach(fn => fn());
if (nextClicks !== 1) throw new Error('Duplicate step should be blocked in same round!');

// Navigate to myaccount.google.com -> marks autoLoginDone=true and test minimize + drag
const s3 = createHarness('myaccount.google.com', '/', {{}}, sharedStore);
if (!s3.storageObj.kv_launch_state.autoLoginDone) {{
  throw new Error('Expected myaccount.google.com to mark autoLoginDone=true');
}}
const bar = s3.getShadow().children.find(c => c.className && c.className.includes('kv-bar'));
const minBtn = bar.querySelector('.collapse');
minBtn.click();
if (!bar.className.includes('minimized') || !s3.storageObj.kv_launch_state.minimized) {{
  throw new Error('Expected minimize button to shrink into minimized capsule');
}}
bar.dispatchEvent({{ type: 'mousedown', button: 0, clientX: 450, clientY: 12, target: bar, preventDefault() {{}} }});
(s3.winListeners.mousemove || []).forEach(fn => fn({{ clientX: 600, clientY: 320 }}));
(s3.winListeners.mouseup || []).forEach(fn => fn({{}}));
if (!s3.storageObj.kv_launch_state.pos || s3.storageObj.kv_launch_state.pos.left < 500) {{
  throw new Error('Expected mini capsule drag to persist updated position');
}}
console.log('ONESHOT_AND_DRAG_OK');
"""
    proc = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True)
    assert "ONESHOT_AND_DRAG_OK" in proc.stdout
