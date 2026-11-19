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


class _RunningBrowser:
    def wait(self, timeout):
        raise subprocess.TimeoutExpired("chrome", timeout)


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

    # Default mode is Pure-Native PurpleShield (zero fp_ext, clean Preferences + proxy bypass).
    native_profile = prepare_profile_dir(data_dir, account)
    assert native_profile == data_dir / "browsers" / "acc_10"
    assert not (native_profile / "fp_ext").exists()
    native_prefs = json.loads((native_profile / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert native_prefs["webrtc"]["ip_handling_policy"] == "disable_non_proxied_udp"
    assert native_prefs["bookmark_bar"]["show_on_all_tabs"] is True

    native_args = build_browser_launch_args(
        account, native_profile, exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    )
    assert any(a.startswith("--user-data-dir=") for a in native_args)
    assert not any(a.startswith("--load-extension=") for a in native_args)
    assert not any(a.startswith("--force-webrtc-ip-handling-policy") for a in native_args)
    assert "--proxy-server=http://127.0.0.1:7890" in native_args
    assert "--proxy-bypass-list=<-loopback>;localhost;127.0.0.1;[::1]" in native_args
    assert native_args[-1] == "https://chatgpt.com/"

    # Explicit legacy extension mode (pure_native=False) still builds valid inject.js & autologin.js.
    profile_dir = prepare_profile_dir(data_dir, account, pure_native=False)
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

    args = build_browser_launch_args(
        account,
        profile_dir,
        exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        pure_native=False,
    )
    assert any(a.startswith("--user-data-dir=") for a in args)
    assert any(a.startswith("--load-extension=") for a in args)
    assert any(a == "--proxy-server=http://127.0.0.1:7890" for a in args)
    assert args[-1] == "https://chatgpt.com/"


def test_get_target_url_domain_email():
    assert get_target_url("域名邮箱") == "https://mail.ggb6.com/"
    assert get_target_url("Google") == "https://myaccount.google.com/"
    assert get_target_url("google") == "https://myaccount.google.com/"
    assert get_target_url("ChatGPT") == "https://chatgpt.com/"
    # ``default_start_url`` overrides the Google maintenance start page.
    assert (
        get_target_url("Google", {"default_start_url": "https://mail.google.com/"})
        == "https://mail.google.com/"
    )
    assert get_target_url("Google", {"default_start_url": ""}) == "https://myaccount.google.com/"
    # Other platforms ignore the Google-only start page setting.
    assert (
        get_target_url("ChatGPT", {"default_start_url": "https://mail.google.com/"})
        == "https://chatgpt.com/"
    )


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

    profile_dir = prepare_profile_dir(data_dir, account, pure_native=False)
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
    # In-page docked toolbar must also appear on any other page (forms.gle,
    # docs.google.com, ipinfo.io...) instead of vanishing outside login hosts.
    assert "https://*/*" in matches and "http://*/*" in matches
    autologin_excludes = manifest["content_scripts"][1]["exclude_matches"]
    for pattern in (
        "*://*.captcha-delivery.com/*",
        "*://*.arkoselabs.com/*",
        "*://*.funcaptcha.com/*",
        "*://*.challenges.cloudflare.com/*",
    ):
        assert pattern in autologin_excludes, pattern

    args = build_browser_launch_args(
        account,
        profile_dir,
        exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        pure_native=False,
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

    # Default Pure-Native mode: zero --load-extension, zero CLI automation flags,
    # WebRTC + Accept-Language persisted cleanly in Default/Preferences.
    profile_dir = prepare_profile_dir(data_dir, account)
    args = build_browser_launch_args(
        account, profile_dir, exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    )
    assert any(a == f"--user-data-dir={profile_dir.resolve()}" for a in args)
    assert not any(a.startswith("--load-extension=") for a in args)
    assert not any(a.startswith("--remote-debugging-port=") for a in args)
    assert "--proxy-server=http://127.0.0.1:17899" in args
    assert "--proxy-bypass-list=<-loopback>;localhost;127.0.0.1;[::1]" in args
    assert "--lang=en-US" in args
    assert "--no-first-run" in args
    assert "--no-default-browser-check" in args
    assert args[-1] == "https://mail.ggb6.com/"

    prefs = json.loads((profile_dir / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["intl"]["accept_languages"] == "en-US,en"
    assert prefs["intl"]["selected_languages"] == "en-US,en"
    assert prefs["spellcheck"]["dictionaries"] == ["en-US"]
    assert prefs["webrtc"]["ip_handling_policy"] == "disable_non_proxied_udp"

    # Explicit legacy extension mode keeps fp_ext syntax and Zero-CDP invariant.
    legacy_profile = prepare_profile_dir(data_dir, account, pure_native=False)
    legacy_args = build_browser_launch_args(
        account,
        legacy_profile,
        exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        pure_native=False,
    )
    assert f"--load-extension={(legacy_profile / 'fp_ext').resolve()}" in legacy_args
    assert not any(a.startswith("--remote-debugging-port=") for a in legacy_args)
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in legacy_args
    assert "--disable-quic" in legacy_args
    assert (
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1 , EXCLUDE localhost"
        in legacy_args
    )

    for name in ("inject.js", "autologin.js"):
        node_check = subprocess.run(
            ["node", "--check", str(legacy_profile / "fp_ext" / name)],
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

    # Default Pure-Native mode has zero --load-extension / zero CDP / clean proxy args.
    native_profile = prepare_profile_dir(data_dir, account)
    native_args = build_browser_launch_args(
        account, native_profile, exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    )
    assert not any(a.startswith("--load-extension=") for a in native_args)
    assert not any(a.startswith("--remote-debugging-port=") for a in native_args)
    assert "--proxy-server=http://127.0.0.1:17899" in native_args
    assert native_args[-1] == "https://chatgpt.com/"

    # Explicit legacy mode (pure_native=False) keeps extension & hardening flags.
    profile_dir = prepare_profile_dir(data_dir, account, pure_native=False)
    args = build_browser_launch_args(
        account,
        profile_dir,
        exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        pure_native=False,
    )
    assert any(a.startswith("--load-extension=") for a in args)
    assert not any(a.startswith("--remote-debugging-port=") for a in args)
    assert "--remote-allow-origins=*" not in args
    assert "--proxy-server=http://127.0.0.1:17899" in args
    assert args.count("--force-webrtc-ip-handling-policy=disable_non_proxied_udp") == 1
    assert "--disable-quic" in args
    assert args[-1] == "https://chatgpt.com/"


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
        return _RunningBrowser()

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
    assert result["pure_native"] is True
    assert result["zero_cdp"] is True
    assert [c[0] for c in calls] == ["popen"]
    launch_env = popen_kwargs[-1]["env"]
    assert launch_env["TZ"] == "America/Los_Angeles"
    assert launch_env["LANG"] == "en_US.UTF-8"
    assert launch_env["LC_ALL"] == "en_US.UTF-8"

    calls.clear()
    popen_kwargs.clear()
    chatgpt_account = {**domain_account, "id": 16, "account_type": "ChatGPT", "account": "chatgpt16@example.com"}
    result = browser_service.launch_isolated_browser(tmp_path / "data", chatgpt_account)
    assert result["ok"] is True
    assert result["pure_native"] is True
    assert result["zero_cdp"] is True
    assert [c[0] for c in calls] == ["popen"]
    assert not (tmp_path / "data" / "browsers" / "acc_16" / "fp_ext").exists()

    # Explicit legacy mode (pure_native=False) builds autologin.js & supports enable_cdp=True.
    legacy_res = browser_service.launch_isolated_browser(tmp_path / "data", chatgpt_account, pure_native=False)
    assert legacy_res["ok"] is True
    assert legacy_res["pure_native"] is False
    autologin_code = (tmp_path / "data" / "browsers" / "acc_16" / "fp_ext" / "autologin.js").read_text(
        encoding="utf-8"
    )
    assert "launchToken" in autologin_code
    assert "finishAutoLoginRound" in autologin_code
    assert "claimAutoStep" in autologin_code
    assert "⊟ 最小" in autologin_code

    args_cdp = build_browser_launch_args(
        chatgpt_account,
        tmp_path / "data" / "browsers" / "acc_16",
        exe_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        pure_native=False,
        enable_cdp=True,
    )
    assert any(a.startswith("--remote-debugging-port=") for a in args_cdp)
    assert "--remote-allow-origins=*" in args_cdp
    assert "展开" in autologin_code
    assert "top: 0px; left: 0px; right: 0px; width: 100%" in autologin_code
    assert "floating" in autologin_code and "docked" in autologin_code
    assert "syncPageTopOffset" in autologin_code
    assert "📌 吸顶" in autologin_code
    assert "Alt+K" in autologin_code
    assert "setNativeInputValue(target, out)" in autologin_code


def test_launch_isolated_browser_applies_switched_node_proxy_url(tmp_path: Path, monkeypatch):
    launched: list[list[str]] = []
    switch_calls: list[tuple] = []

    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: "C:\\fake\\chrome.exe")
    monkeypatch.setattr(
        browser_service.subprocess,
        "Popen",
        lambda args, **kw: launched.append(list(args)) or _RunningBrowser(),
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
        lambda args, **kw: launched.append(list(args)) or _RunningBrowser(),
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

    profile_dir = prepare_profile_dir(data_dir, account, pure_native=False)
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
    assert "--proxy-server=http://127.0.0.1:17899" in args
    assert "--proxy-bypass-list=<-loopback>;localhost;127.0.0.1;[::1]" in args
    assert not any(a.startswith("--load-extension=") for a in args)
    assert not any(a.startswith("--force-webrtc-ip-handling-policy") for a in args)
    assert args[-1] == "https://claude.ai/"
    # Locale flag must precede the target URL.
    assert args.index("--lang=en-US") < args.index("https://claude.ai/")
    prefs = json.loads((profile_dir / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["webrtc"]["ip_handling_policy"] == "disable_non_proxied_udp"
    assert prefs["intl"]["accept_languages"] == "en-US,en"


def test_prepare_profile_dir_for_claude_writes_preferences_and_spoofing_scripts(tmp_path: Path):
    account = _claude_account(
        proxy="http://127.0.0.1:17899", proxy_node="USbyJPTT | MiyaIP-2 HY2"
    )
    profile_dir = prepare_profile_dir(tmp_path / "data", account, pure_native=False)

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
    profile_dir = prepare_profile_dir(tmp_path / "data", account, pure_native=False)
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
    claude_profile = prepare_profile_dir(data_dir, claude, pure_native=False)
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
    domain_profile = prepare_profile_dir(data_dir, domain, pure_native=False)

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
        lambda args, **kw: launched.append(list(args)) or _RunningBrowser(),
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
        lambda args, **kw: launched.append(list(args)) or _RunningBrowser(),
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
        lambda args, **kw: launched.append(list(args)) or _RunningBrowser(),
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
        lambda args, **kw: popen_kwargs.append(dict(kw)) or _RunningBrowser(),
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


def test_google_profile_seeds_bookmarks_without_fingerprint_extension(tmp_path: Path):
    import json

    data_dir = tmp_path / "data"
    account = {
        "id": 99,
        "account_type": "Google",
        "account": "bookmark_tester@gmail.com",
        "password": "pw",
        "reg_region": "US",
    }
    profile_dir = browser_service.prepare_profile_dir(data_dir, account)
    assert not (profile_dir / "fp_ext").exists()
    prefs_path = profile_dir / "Default" / "Preferences"
    assert prefs_path.exists()
    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert prefs["webrtc"]["ip_handling_policy"] == "disable_non_proxied_udp"
    assert prefs["intl"]["accept_languages"] == "en-US,en"
    assert prefs["bookmark_bar"]["show_on_all_tabs"] is True

    bm = json.loads((profile_dir / "Default" / "Bookmarks").read_text(encoding="utf-8"))
    children = bm["roots"]["bookmark_bar"]["children"]
    assert children[0]["name"] == "KeyVault 维护助手"
    urls = {node["url"].split("?", 1)[0] for node in children[0]["children"]}
    raw_urls = {node["url"] for node in children[0]["children"]}
    assert "https://myaccount.google.com/security" in urls
    assert "https://forms.gle/hGzM9MEUv2azZsrb9" in urls
    assert "https://policies.google.com/country-association-form" in urls
    assert "https://myaccount.google.com/age-verification" in urls
    assert "https://payments.google.com/gp/w/home/reauthprompt?rtrp=/home/settings" in raw_urls
    assert "https://payments.google.com/gp/w/home/reauthprompt" in urls
    assert "https://myaccount.google.com/deleteservices" in urls


def test_existing_profile_bookmarks_are_repaired_only_when_helper_is_missing(tmp_path: Path):
    import json

    data_dir = tmp_path / "data"
    account = {"id": 199, "account_type": "Google", "account": "repair@example.test"}
    profile = browser_service.prepare_profile_dir(data_dir, account)
    bookmarks_path = profile / "Default" / "Bookmarks"
    document = json.loads(bookmarks_path.read_text(encoding="utf-8"))
    document["roots"]["bookmark_bar"]["children"].append({
        "id": "7777", "name": "User bookmark", "type": "url", "url": "https://example.test/"
    })
    bookmarks_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    browser_service.prepare_profile_dir(data_dir, account)
    unchanged = json.loads(bookmarks_path.read_text(encoding="utf-8"))
    assert [node["name"] for node in unchanged["roots"]["bookmark_bar"]["children"]] == [
        "KeyVault 维护助手", "User bookmark"
    ]

    unchanged["roots"]["bookmark_bar"]["children"] = [
        node for node in unchanged["roots"]["bookmark_bar"]["children"]
        if node.get("name") != "KeyVault 维护助手"
    ]
    bookmarks_path.write_text(json.dumps(unchanged, ensure_ascii=False), encoding="utf-8")
    browser_service.prepare_profile_dir(data_dir, account)
    repaired = json.loads(bookmarks_path.read_text(encoding="utf-8"))
    assert repaired["roots"]["bookmark_bar"]["children"][0]["name"] == "KeyVault 维护助手"
    assert repaired["roots"]["bookmark_bar"]["children"][1]["name"] == "User bookmark"


def test_existing_active_profile_defers_bookmark_repair(tmp_path: Path):
    import json

    data_dir = tmp_path / "data"
    account = {"id": 200, "account_type": "Google", "account": "locked@example.test"}
    profile = browser_service.prepare_profile_dir(data_dir, account)
    bookmarks_path = profile / "Default" / "Bookmarks"
    document = json.loads(bookmarks_path.read_text(encoding="utf-8"))
    document["roots"]["bookmark_bar"]["children"] = []
    bookmarks_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    (profile / "SingletonLock").touch()

    browser_service.prepare_profile_dir(data_dir, account)
    repaired = json.loads(bookmarks_path.read_text(encoding="utf-8"))
    assert repaired["roots"]["bookmark_bar"]["children"] == []


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


def test_google_launch_reuses_profile_and_removes_only_generated_extension(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    account = {"id": 101, "account_type": "Google", "account": "manual@example.test", "password": "mock-only"}
    profile = browser_service.prepare_profile_dir(data_dir, account)
    extension = profile / "fp_ext"
    fp = generate_fingerprint_config(account["id"], account["account"])
    browser_service._build_extension_files(extension, fp, account)
    kept_file = extension / "other-file.txt"
    kept_file.write_text("user file", encoding="utf-8")
    cookies = profile / "Default" / "Cookies"
    cookies.write_bytes(b"mock session")
    bookmarks = profile / "Default" / "Bookmarks"
    saved_bookmarks = bookmarks.read_bytes()
    bookmarks.write_bytes(saved_bookmarks + b"\n")
    lock = profile / "SingletonLock"
    lock.touch()
    launched = []
    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: "C:\\fake\\chrome.exe")
    monkeypatch.setattr(
        browser_service.subprocess, "Popen", lambda args, **kwargs: launched.append(args) or _RunningBrowser()
    )

    pending = browser_service.launch_isolated_browser(data_dir, account)
    assert pending["ok"] is False and pending["profile_cleanup_pending"] is True
    assert not launched and lock.exists() and (extension / "autologin.js").exists()

    lock.unlink()
    for target in ("", "https://myaccount.google.com/security", "https://accounts.google.com/o/oauth2/v2/auth"):
        result = browser_service.launch_isolated_browser(data_dir, account, target_url=target, enable_cdp=True)
        assert result["ok"] and result["manual_login"] and result["zero_cdp"]
        assert f"--user-data-dir={profile}" in launched[-1]
        assert "--lang=en-US" in launched[-1]
        assert not any(arg.startswith(("--load-extension", "--remote-debugging", "--disable-extensions")) for arg in launched[-1])
        expected_url = (
            "https://myaccount.google.com/?hl=en-US"
            if not target
            else (
                "https://myaccount.google.com/security?hl=en-US"
                if "security" in target
                else target
            )
        )
        assert launched[-1][-1] == expected_url
    assert list(extension.iterdir()) == [kept_file]
    assert cookies.read_bytes() == b"mock session"
    assert bookmarks.read_bytes() == saved_bookmarks + b"\n"


def test_pure_native_cleans_unauthenticated_rejected_cookies_but_preserves_authenticated(tmp_path: Path):
    import sqlite3

    data_dir = tmp_path / "data"
    # 1. Unauthenticated profile touched by old fp_ext -> clears rejected Cookies DB
    acc_unauth = {"id": 201, "account_type": "Google", "account": "unauth@example.test"}
    p_unauth = browser_service.prepare_profile_dir(data_dir, acc_unauth)
    browser_service._build_extension_files(p_unauth / "fp_ext", generate_fingerprint_config(201, "unauth@example.test"), acc_unauth)
    net_unauth = p_unauth / "Default" / "Network"
    net_unauth.mkdir(parents=True, exist_ok=True)
    db_unauth = net_unauth / "Cookies"
    conn = sqlite3.connect(db_unauth)
    try:
        conn.execute("CREATE TABLE cookies (name TEXT)")
        conn.execute("INSERT INTO cookies (name) VALUES ('GAPS')")
        conn.commit()
    finally:
        conn.close()
    assert browser_service._remove_legacy_google_extension(p_unauth) is True
    assert not db_unauth.exists()

    # 2. Authenticated profile with SID cookie -> preserves Cookies DB
    acc_auth = {"id": 202, "account_type": "Google", "account": "auth@example.test"}
    p_auth = browser_service.prepare_profile_dir(data_dir, acc_auth)
    browser_service._build_extension_files(p_auth / "fp_ext", generate_fingerprint_config(202, "auth@example.test"), acc_auth)
    net_auth = p_auth / "Default" / "Network"
    net_auth.mkdir(parents=True, exist_ok=True)
    db_auth = net_auth / "Cookies"
    conn2 = sqlite3.connect(db_auth)
    try:
        conn2.execute("CREATE TABLE cookies (name TEXT)")
        conn2.execute("INSERT INTO cookies (name) VALUES ('SID')")
        conn2.commit()
    finally:
        conn2.close()
    assert browser_service._remove_legacy_google_extension(p_auth) is True
    assert db_auth.exists()


def test_window_placement_recovery_and_url_hl_hint(tmp_path: Path):
    assert browser_service.apply_url_language_hint("https://myaccount.google.com/security", "ja-JP") == "https://myaccount.google.com/security?hl=ja-JP"
    assert browser_service.apply_url_language_hint("https://accounts.google.com/o/oauth2/v2/auth?client_id=1", "en-US") == "https://accounts.google.com/o/oauth2/v2/auth?client_id=1"
    assert browser_service.apply_url_language_hint("https://chatgpt.com/", "en-US") == "https://chatgpt.com/"

    p_dir = tmp_path / "acc_301"
    (p_dir / "Default").mkdir(parents=True)
    # Offscreen invalid placement should be repaired onto screen
    (p_dir / "Default" / "Preferences").write_text(
        json.dumps({"browser": {"window_placement": {"left": -30000, "top": -30000, "right": -29000, "bottom": -29000}}}),
        encoding="utf-8",
    )
    repaired, bounds = browser_service.restore_or_sanitize_window_placement(
        p_dir, 301, test_screens=[(0, 0, 1920, 1040)]
    )
    assert repaired is True
    assert bounds[0] >= 0 and bounds[1] >= 0 and bounds[2] >= 960
    repaired2, bounds2 = browser_service.restore_or_sanitize_window_placement(
        p_dir, 301, test_screens=[(0, 0, 1920, 1040)]
    )
    assert repaired2 is False
    assert bounds2 == bounds


def test_native_launch_never_generates_extension(tmp_path, monkeypatch):
    account = {"id": 42, "account_type": "ChatGPT", "account": "manual@example.test"}
    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: "C:\\fake\\chrome.exe")
    monkeypatch.setattr(browser_service.subprocess, "Popen", lambda *args, **kwargs: _RunningBrowser())
    result = browser_service.launch_isolated_browser(tmp_path, account, pure_native=True)
    assert result["ok"] and not (Path(result["profile_dir"]) / "fp_ext").exists()


def test_launch_reports_early_failure_and_allows_a_normal_handoff(tmp_path, monkeypatch):
    account = {"id": 2, "account_type": "Google", "account": "browser@example.test"}
    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: "C:\\fake\\chrome.exe")

    class ExitedBrowser:
        exit_code = 23

        def wait(self, timeout):
            assert timeout == 1
            return self.exit_code

    process = ExitedBrowser()
    monkeypatch.setattr(browser_service.subprocess, "Popen", lambda *args, **kwargs: process)
    failure = browser_service.launch_isolated_browser(tmp_path, account)
    assert failure["ok"] is False
    assert "立即退出" in failure["error"] and "23" in failure["error"]

    process.exit_code = 0
    assert browser_service.launch_isolated_browser(tmp_path, account)["ok"] is True


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
    account = {
        "id": 102,
        "account_type": "Google",
        "account": "oneshot@gmail.com",
        "password": "OneShotPassword#1",
        "secret_key": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
    }
    extension = tmp_path / "legacy-fp-ext"
    fp = generate_fingerprint_config(account["id"], account["account"])
    browser_service._build_extension_files(extension, fp, account)
    autologin_path = extension / "autologin.js"
    node_check = subprocess.run(
        ["node", "--check", str(autologin_path)],
        capture_output=True,
        text=True,
    )
    assert node_check.returncode == 0, node_check.stderr

    autologin_code = autologin_path.read_text(encoding="utf-8")
    assert "关闭支付资料" in autologin_code
    assert "https://payments.google.com/gp/w/home/reauthprompt?rtrp=/home/settings" in autologin_code
    assert "取消Pro订阅" in autologin_code
    assert "https://myaccount.google.com/deleteservices" in autologin_code

    manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
    assert "storage" in (manifest.get("permissions") or [])

    # Behavioral Node harness simulating chrome.storage.local across 3 scenarios:
    # 1) ChatGPT already logged in -> immediately closes auto-login (autoLoginDone = true)
    # 2) Google login flow -> each step executes at most once automatically, finishes at myaccount.google.com
    # 3) Top bar minimize -> mini capsule drag persists pos & minimized state
    harness = f"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync({json.dumps(str(autologin_path))}, 'utf8');

function createHarness(initialHost, initialPath, selectorsMap, storeSeed, useChromeStorage, localSeed) {{
  const storageObj = storeSeed || {{}};
  const intervals = [];
  const timeouts = [];
  let mountedShadow = null;

  function makeStyle() {{
    const props = {{}};
    const style = {{
      _props: props,
      set cssText(v) {{
        String(v || '').split(';').forEach(part => {{
          const idx = part.indexOf(':');
          if (idx <= 0) return;
          const k = part.slice(0, idx).trim().toLowerCase();
          const val = part.slice(idx + 1).trim();
          if (k) props[k] = val;
        }});
      }},
      get cssText() {{
        return Object.keys(props).map(k => k + ': ' + props[k]).join('; ');
      }},
      setProperty(name, value, priority) {{ props[String(name).toLowerCase()] = String(value); }},
      removeProperty(name) {{
        const k = String(name).toLowerCase();
        const prev = props[k] || '';
        delete props[k];
        return prev;
      }},
      getPropertyValue(name) {{ return props[String(name).toLowerCase()] || ''; }},
      getPropertyPriority(name) {{ return ''; }},
    }};
    ['top', 'left', 'right', 'width', 'transform'].forEach((name) => {{
      Object.defineProperty(style, name, {{
        get() {{ return props[name] || ''; }},
        set(v) {{ props[name] = String(v); }},
      }});
    }});
    Object.defineProperty(style, 'marginTop', {{
      get() {{ return props['margin-top'] || ''; }},
      set(v) {{ props['margin-top'] = String(v); }},
    }});
    return style;
  }}

  class El {{
    constructor(tag) {{
      this.tagName = tag.toUpperCase();
      this.children = [];
      this.listeners = {{}};
      this.style = makeStyle();
      this.className = '';
      this.textContent = '';
      this.title = '';
      this.value = '';
      this.disabled = false;
      this.readOnly = false;
      this.isConnected = true;
      this.parentElement = null;
      this._rect = {{ left: 400, top: 6, width: 520, height: 34 }};
    }}
    append(...nodes) {{ nodes.forEach(n => this.appendChild(n)); }}
    appendChild(n) {{ if (n) {{ n.parentElement = this; this.children.push(n); }} return n; }}
    replaceChildren(...nodes) {{ this.children = []; this.append(...nodes); }}
    remove() {{
      this.removed = true;
      this.isConnected = false;
      if (this.parentElement && this.parentElement.children) {{
        this.parentElement.children = this.parentElement.children.filter(c => c !== this);
      }}
    }}
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
    get classList() {{
      const el = this;
      const names = () => (el.className || '').split(/\\s+/).filter(Boolean);
      return {{
        add(...items) {{
          const set = new Set(names());
          items.forEach(i => set.add(i));
          el.className = Array.from(set).join(' ');
        }},
        remove(...items) {{
          const set = new Set(names());
          items.forEach(i => set.delete(i));
          el.className = Array.from(set).join(' ');
        }},
        toggle(item, force) {{
          const has = names().includes(item);
          const next = force === undefined ? !has : !!force;
          if (next) this.add(item); else this.remove(item);
          return next;
        }},
        contains(item) {{ return names().includes(item); }},
      }};
    }}
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
  const local = new Map();
  const sessionStorageObj = {{
    getItem: (k) => sess.has(k) ? sess.get(k) : null,
    setItem: (k, v) => sess.set(k, String(v)),
    removeItem: (k) => sess.delete(k),
    get length() {{ return sess.size; }},
    key: (i) => Array.from(sess.keys())[i] || null,
  }};
  const localStorageObj = {{
    getItem: (k) => local.has(k) ? local.get(k) : null,
    setItem: (k, v) => local.set(k, String(v)),
    removeItem: (k) => local.delete(k),
    get length() {{ return local.size; }},
    key: (i) => Array.from(local.keys())[i] || null,
  }};

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
    chrome: useChromeStorage === false ? undefined : {{
      storage: {{
        local: {{
          get: (keys, cb) => cb({{ ...storageObj }}),
          set: (obj) => Object.assign(storageObj, obj),
        }}
      }}
    }},
    sessionStorage: sessionStorageObj,
    localStorage: localStorageObj,
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
  return {{
    ctx, storageObj, timeouts, winListeners, docListeners, docEl, local, sess,
    getShadow: () => mountedShadow,
    getHost: () => docEl.children.find(c => c.id === '__kv_autologin_host') || null,
  }};
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
const hostEl = s3.getHost();
if (!bar.className.includes('docked')) throw new Error('Expected expanded bar to be docked at the top');
if (!hostEl || hostEl.style.top !== '0px' || hostEl.style.width !== '100%') {{
  throw new Error('Expected hostEl docked at top with 100% width: ' + JSON.stringify({{
    top: hostEl && hostEl.style.top, width: hostEl && hostEl.style.width
  }}));
}}
if (!s3.docEl.style.getPropertyValue('margin-top')) {{
  throw new Error('Expected docked bar to push the page down via documentElement margin-top');
}}
const minBtn = bar.querySelector('.collapse');
minBtn.click();
if (!bar.className.includes('minimized') || !s3.storageObj.kv_launch_state.minimized) {{
  throw new Error('Expected minimize button to shrink into minimized capsule');
}}
if (s3.docEl.style.getPropertyValue('margin-top') !== '') {{
  throw new Error('Expected minimize to clear the page top offset immediately');
}}
bar.dispatchEvent({{ type: 'mousedown', button: 0, clientX: 450, clientY: 12, target: bar, preventDefault() {{}} }});
(s3.winListeners.mousemove || []).forEach(fn => fn({{ clientX: 600, clientY: 320 }}));
(s3.winListeners.mouseup || []).forEach(fn => fn({{}}));
if (!s3.storageObj.kv_launch_state.pos || s3.storageObj.kv_launch_state.pos.left < 500) {{
  throw new Error('Expected mini capsule drag to persist updated position');
}}

// Scenario 4: CDP-injected page (no chrome.storage.local) still mounts the
// docked bar from localStorage, Alt+K hide clears margin-top, Alt+K restores.
const s4 = createHarness('docs.google.com', '/document/d/1', {{}}, null, false);
const bar4 = s4.getShadow().children.find(c => c.className && c.className.includes('kv-bar'));
if (!bar4 || !bar4.className.includes('docked')) {{
  throw new Error('Expected CDP-injected page (no chrome.storage) to mount the docked bar');
}}
if (!s4.docEl.style.getPropertyValue('margin-top')) {{
  throw new Error('Expected CDP-injected docked bar to push the page down');
}}
if (!s4.local.size) {{
  throw new Error('Expected launch state to be mirrored into localStorage without chrome.storage');
}}
const keydown = (s4.docListeners.keydown || [])[0];
if (!keydown) throw new Error('Expected Alt+K hotkey listener to be bound');
keydown({{ altKey: true, key: 'k', code: 'KeyK', preventDefault() {{}} }});
if (s4.getHost()) throw new Error('Expected Alt+K to remove the bar host');
if (s4.docEl.style.getPropertyValue('margin-top') !== '') {{
  throw new Error('Expected Alt+K hide to clear the page top offset');
}}
keydown({{ altKey: true, key: 'k', code: 'KeyK', preventDefault() {{}} }});
if (!s4.getHost()) throw new Error('Expected Alt+K again to restore the bar host');
if (!s4.docEl.style.getPropertyValue('margin-top')) {{
  throw new Error('Expected Alt+K restore to re-apply the page top offset');
}}
console.log('ONESHOT_AND_DRAG_OK');
"""
    proc = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True)
    assert "ONESHOT_AND_DRAG_OK" in proc.stdout


def test_resolve_browser_executable_and_lang_settings(tmp_path: Path, monkeypatch):
    fake_chrome = tmp_path / "chrome.exe"
    fake_edge = tmp_path / "msedge.exe"
    fake_custom = tmp_path / "brave.exe"
    for p in (fake_chrome, fake_edge, fake_custom):
        p.write_bytes(b"")

    monkeypatch.setattr(browser_service, "detect_chrome_path", lambda: str(fake_chrome))
    monkeypatch.setattr(browser_service, "detect_edge_path", lambda: str(fake_edge))

    exe_c, err_c = browser_service.resolve_browser_executable({"preferred_browser": "chrome"})
    assert exe_c == str(fake_chrome) and err_c == ""
    exe_e, err_e = browser_service.resolve_browser_executable({"preferred_browser": "edge"})
    assert exe_e == str(fake_edge) and err_e == ""
    exe_u, err_u = browser_service.resolve_browser_executable(
        {"preferred_browser": "custom", "custom_browser_path": str(fake_custom)}
    )
    assert exe_u == str(fake_custom) and err_u == ""

    env_cfg = browser_service.resolve_browser_env_config(
        {},
        {"account_type": "Google", "reg_region": "JP"},
        settings={"default_browser_lang": "zh-CN"},
    )
    assert env_cfg["locale"] == "zh-CN"
    assert env_cfg["languages"][0] == "zh-CN"
    assert "ja-JP" in env_cfg["languages"]
    assert "en-US" in env_cfg["languages"]


def _stub_launch(monkeypatch):
    launched: list[list[str]] = []
    cdp_calls: list[tuple] = []
    monkeypatch.setattr(
        browser_service.subprocess,
        "Popen",
        lambda args, **kw: launched.append(list(args)) or _RunningBrowser(),
    )
    monkeypatch.setattr(browser_service, "_start_cdp_injector", lambda *a, **kw: cdp_calls.append((a, kw)))
    return launched, cdp_calls


def test_inpage_edge_prefers_edge_and_runs_zero_cdp_native_extension(tmp_path: Path, monkeypatch):
    fake_edge = tmp_path / "msedge.exe"
    fake_chrome = tmp_path / "chrome.exe"
    fake_edge.write_bytes(b"")
    fake_chrome.write_bytes(b"")
    monkeypatch.delenv("KEYVAULT_BROWSER_PATH", raising=False)
    monkeypatch.setattr(browser_service, "detect_edge_path", lambda: str(fake_edge))
    monkeypatch.setattr(browser_service, "detect_chrome_path", lambda: str(fake_chrome))
    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: str(fake_chrome))
    monkeypatch.setattr(
        browser_service,
        "activate_proxy_node",
        lambda *a, **kw: {"ok": True, "switched": False},
    )
    launched, cdp_calls = _stub_launch(monkeypatch)
    settings = {"browser_engine_mode": "inpage_edge", "preferred_browser": "auto"}

    exe, err = browser_service.resolve_browser_executable(settings)
    assert exe == str(fake_edge) and err == ""

    # Non-Google platforms keep the Edge-first in-page engine.
    chatgpt = {"id": 301, "account_type": "ChatGPT", "account": "edge_chat@example.test"}
    result = browser_service.launch_isolated_browser(
        tmp_path / "data", chatgpt, settings=settings
    )
    assert result["ok"] is True
    assert result["pure_native"] is False
    assert result["inpage_toolbar"] is True
    assert result["browser_engine_mode"] == "inpage_edge"
    assert result["engine"] == "Edge"
    assert result["zero_cdp"] is True
    assert result["manual_login"] is False
    # The in-page toolbar extension is generated for non-Google accounts.
    assert (Path(result["profile_dir"]) / "fp_ext" / "autologin.js").exists()
    assert cdp_calls == []
    args = launched[-1]
    assert any(a.startswith("--load-extension=") for a in args)
    assert not any(a.startswith("--remote-debugging-port=") for a in args)


def test_google_always_forces_pure_native_chrome_even_when_inpage_edge_or_chrome_cdp_selected(
    tmp_path: Path, monkeypatch
):
    fake_chrome = tmp_path / "chrome.exe"
    fake_edge = tmp_path / "msedge.exe"
    fake_chrome.write_bytes(b"")
    fake_edge.write_bytes(b"")
    monkeypatch.delenv("KEYVAULT_BROWSER_PATH", raising=False)
    monkeypatch.setattr(browser_service, "detect_edge_path", lambda: str(fake_edge))
    monkeypatch.setattr(browser_service, "detect_chrome_path", lambda: str(fake_chrome))
    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: str(fake_chrome))
    monkeypatch.setattr(
        browser_service,
        "activate_proxy_node",
        lambda *a, **kw: {"ok": True, "switched": False},
    )
    launched, cdp_calls = _stub_launch(monkeypatch)

    for index, engine in enumerate(("inpage_edge", "inpage_chrome_cdp")):
        settings = {"browser_engine_mode": engine, "preferred_browser": "auto"}
        exe, err = browser_service.resolve_browser_executable(settings, account_type="Google")
        assert exe == str(fake_chrome) and err == "", engine
        account = {
            "id": 401 + index,
            "account_type": "Google",
            "account": f"force_{engine}@example.test",
        }
        result = browser_service.launch_isolated_browser(
            tmp_path / "data", account, settings=settings
        )
        assert result["ok"] is True, result
        assert result["pure_native"] is True
        assert result["inpage_toolbar"] is False
        assert result["browser_engine_mode"] == "pure_native"
        assert result["engine"] == "Chrome"
        assert result["zero_cdp"] is True
        assert result["manual_login"] is True
        assert cdp_calls == []
        profile_dir = Path(result["profile_dir"])
        assert not (profile_dir / "fp_ext").exists()
        args = launched[-1]
        assert args[0] == str(fake_chrome)
        assert not any(a.startswith("--load-extension=") for a in args)
        assert not any(a.startswith("--remote-debugging-port=") for a in args)
        assert not any("--host-resolver-rules" in a for a in args)
        assert not any("--disable-quic" in a for a in args)
        assert not any("--disable-sync" in a for a in args)
        assert not any(a.startswith("--force-webrtc-ip-handling-policy") for a in args)
        assert not any(a.startswith("--accept-lang=") for a in args)
        launched.clear()


def test_google_launch_env_sets_only_tz(tmp_path: Path, monkeypatch):
    popen_kwargs: list[dict] = []
    monkeypatch.delenv("KEYVAULT_BROWSER_PATH", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: "C:\\fake\\chrome.exe")
    monkeypatch.setattr(
        browser_service,
        "activate_proxy_node",
        lambda *a, **kw: {"ok": True, "switched": False},
    )
    monkeypatch.setattr(
        browser_service.subprocess,
        "Popen",
        lambda args, **kw: popen_kwargs.append(dict(kw)) or _RunningBrowser(),
    )

    fp = generate_fingerprint_config(
        402, "env_google@example.test", proxy_node="US | MiyaIP-2 HY2"
    )
    google = {
        "id": 402,
        "account_type": "Google",
        "account": "env_google@example.test",
        "fp_config": fp,
    }
    assert browser_service.launch_isolated_browser(tmp_path / "data", google)["ok"] is True
    google_env = popen_kwargs[-1]["env"]
    assert google_env["TZ"] == "America/Los_Angeles"
    assert "LANG" not in google_env
    assert "LC_ALL" not in google_env

    # Non-Google platforms keep the locale env vars.
    chatgpt = {
        "id": 403,
        "account_type": "ChatGPT",
        "account": "env_chat@example.test",
        "fp_config": generate_fingerprint_config(
            403, "env_chat@example.test", proxy_node="US | MiyaIP-2 HY2"
        ),
    }
    assert browser_service.launch_isolated_browser(tmp_path / "data", chatgpt)["ok"] is True
    chat_env = popen_kwargs[-1]["env"]
    assert chat_env["TZ"] == "America/Los_Angeles"
    assert chat_env["LANG"] == "en_US.UTF-8"
    assert chat_env["LC_ALL"] == "en_US.UTF-8"


def test_purge_legacy_google_profiles_and_reset_session_preserves_cookies(tmp_path: Path):
    import sqlite3

    data_dir = tmp_path / "data"
    google = {"id": 501, "account_type": "Google", "account": "purge@gmail.test"}
    chatgpt = {"id": 502, "account_type": "ChatGPT", "account": "purge_chat@example.test"}

    g_profile = browser_service.prepare_profile_dir(data_dir, google)
    c_profile = browser_service.prepare_profile_dir(data_dir, chatgpt)
    browser_service._build_extension_files(
        g_profile / "fp_ext",
        generate_fingerprint_config(501, google["account"]),
        google,
    )
    browser_service._build_extension_files(
        c_profile / "fp_ext",
        generate_fingerprint_config(502, chatgpt["account"]),
        chatgpt,
    )
    shield = g_profile / "_ag_fp_shield"
    shield.mkdir()
    (shield / "manifest.json").write_text("{}", encoding="utf-8")
    (g_profile / "DevToolsActivePort").write_text("9222\n/devtools", encoding="utf-8")

    prefs_path = g_profile / "Default" / "Preferences"
    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    prefs["extensions"] = {
        "settings": {
            "legacyfp": {"path": str(g_profile / "fp_ext")},
            "legacyshield": {"path": str(shield)},
            "keepme": {"path": str(tmp_path / "unrelated_ext")},
        }
    }
    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")

    net_dir = g_profile / "Default" / "Network"
    net_dir.mkdir(parents=True, exist_ok=True)
    cookies_db = net_dir / "Cookies"
    conn = sqlite3.connect(cookies_db)
    try:
        conn.execute("CREATE TABLE cookies (name TEXT)")
        conn.execute("INSERT INTO cookies (name) VALUES ('SID')")
        conn.commit()
    finally:
        conn.close()

    result = browser_service.purge_legacy_google_profiles(data_dir, [google, chatgpt])
    assert result == {"scanned": 1, "cleaned": 1, "skipped": 0}
    assert not (g_profile / "fp_ext").exists()
    assert not shield.exists()
    assert not (g_profile / "DevToolsActivePort").exists()
    cleaned_prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert "legacyfp" not in cleaned_prefs["extensions"]["settings"]
    assert "legacyshield" not in cleaned_prefs["extensions"]["settings"]
    assert "keepme" in cleaned_prefs["extensions"]["settings"]
    # Logged-in (SID) cookies are never touched.
    assert cookies_db.exists()
    # ChatGPT keeps its in-page toolbar extension.
    assert (c_profile / "fp_ext" / "autologin.js").exists()

    # Without an explicit account list nothing is swept (never touch non-Google).
    assert browser_service.purge_legacy_google_profiles(data_dir) == {
        "scanned": 0,
        "cleaned": 0,
        "skipped": 0,
    }

    # Default reset only clears automation markers: pre-login Cookies & Sessions stay.
    unauth = {"id": 503, "account_type": "Google", "account": "unauth@gmail.test"}
    u_profile = browser_service.prepare_profile_dir(data_dir, unauth)
    (u_profile / "DevToolsActivePort").write_text("9222\n/devtools", encoding="utf-8")
    u_net = u_profile / "Default" / "Network"
    u_net.mkdir(parents=True, exist_ok=True)
    u_cookies = u_net / "Cookies"
    conn = sqlite3.connect(u_cookies)
    try:
        conn.execute("CREATE TABLE cookies (name TEXT)")
        conn.execute("INSERT INTO cookies (name) VALUES ('GAPS')")
        conn.commit()
    finally:
        conn.close()
    sessions = u_profile / "Default" / "Sessions"
    sessions.mkdir(parents=True, exist_ok=True)

    reset = browser_service.reset_rejected_login_session(data_dir, 503)
    assert reset["ok"] is True
    assert "DevToolsActivePort" in reset["removed"]
    assert u_cookies.exists() and sessions.exists()

    # Explicit opt-in still clears unauthenticated rejected caches.
    (u_profile / "DevToolsActivePort").write_text("9222\n/devtools", encoding="utf-8")
    cleared = browser_service.reset_rejected_login_session(
        data_dir, 503, clear_unauth_cookies=True
    )
    assert cleared["ok"] is True
    assert not u_cookies.exists()
    assert not sessions.exists()

    # A historically emptied fp_ext folder is swept, while its Cookies survive untouched.
    empty_ext = {"id": 504, "account_type": "Google", "account": "emptyext@gmail.test"}
    e_profile = browser_service.prepare_profile_dir(data_dir, empty_ext)
    (e_profile / "fp_ext").mkdir()
    e_net = e_profile / "Default" / "Network"
    e_net.mkdir(parents=True, exist_ok=True)
    e_cookies = e_net / "Cookies"
    conn = sqlite3.connect(e_cookies)
    try:
        conn.execute("CREATE TABLE cookies (name TEXT)")
        conn.execute("INSERT INTO cookies (name) VALUES ('SID')")
        conn.commit()
    finally:
        conn.close()

    # A fp_ext holding user files (no generated extension) is never removed.
    kept_ext = {"id": 505, "account_type": "Google", "account": "keptext@gmail.test"}
    k_profile = browser_service.prepare_profile_dir(data_dir, kept_ext)
    (k_profile / "fp_ext").mkdir()
    kept_file = k_profile / "fp_ext" / "other-file.txt"
    kept_file.write_text("user data", encoding="utf-8")

    swept = browser_service.purge_legacy_google_profiles(data_dir, [empty_ext, kept_ext])
    assert swept == {"scanned": 2, "cleaned": 2, "skipped": 0}
    assert not (e_profile / "fp_ext").exists()
    assert e_cookies.exists()
    assert kept_file.read_text(encoding="utf-8") == "user data"


def test_google_already_running_skips_url_unless_override_specified(tmp_path: Path, monkeypatch):
    fake_chrome = tmp_path / "chrome.exe"
    fake_chrome.write_bytes(b"")
    monkeypatch.delenv("KEYVAULT_BROWSER_PATH", raising=False)
    monkeypatch.setattr(browser_service, "detect_chrome_path", lambda: str(fake_chrome))
    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: str(fake_chrome))
    monkeypatch.setattr(
        browser_service,
        "activate_proxy_node",
        lambda *a, **kw: {"ok": True, "switched": False},
    )
    launched, cdp_calls = _stub_launch(monkeypatch)
    settings = {"browser_engine_mode": "inpage_edge", "preferred_browser": "auto"}
    account = {"id": 601, "account_type": "Google", "account": "running@gmail.test"}
    profile = browser_service.prepare_profile_dir(tmp_path / "data", account)
    prefs_path = profile / "Default" / "Preferences"
    prefs_before = prefs_path.read_bytes()
    (profile / "SingletonLock").touch()
    try:
        running = browser_service.launch_isolated_browser(
            tmp_path / "data", account, settings=settings
        )
        assert running["ok"] is True
        assert running["pure_native"] is True and running["zero_cdp"] is True
        args = launched[-1]
        # Already-open profile: never re-append the default start URL.
        assert not any(a.startswith(("http://", "https://")) for a in args)
        assert "about:blank" not in args
        # An in-use profile is never rewritten.
        assert prefs_path.read_bytes() == prefs_before

        launched.clear()
        override = browser_service.launch_isolated_browser(
            tmp_path / "data",
            account,
            target_url="https://myaccount.google.com/security",
            settings=settings,
        )
        assert override["ok"] is True
        assert launched[-1][-1] == "https://myaccount.google.com/security?hl=en-US"
        assert cdp_calls == []
    finally:
        (profile / "SingletonLock").unlink(missing_ok=True)


def test_inpage_edge_falls_back_to_chrome_with_silent_cdp_but_domain_email_stays_zero_cdp(
    tmp_path: Path, monkeypatch
):
    fake_chrome = tmp_path / "chrome.exe"
    fake_chrome.write_bytes(b"")
    monkeypatch.delenv("KEYVAULT_BROWSER_PATH", raising=False)
    monkeypatch.setattr(browser_service, "detect_edge_path", lambda: "")
    monkeypatch.setattr(browser_service, "detect_chrome_path", lambda: str(fake_chrome))
    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: str(fake_chrome))
    monkeypatch.setattr(
        browser_service,
        "activate_proxy_node",
        lambda *a, **kw: {"ok": True, "switched": False},
    )
    launched, cdp_calls = _stub_launch(monkeypatch)
    settings = {"browser_engine_mode": "inpage_edge", "preferred_browser": "auto"}

    exe, err = browser_service.resolve_browser_executable(settings)
    assert exe == str(fake_chrome) and err == ""

    chatgpt = {"id": 302, "account_type": "ChatGPT", "account": "fallback_chat@example.test"}
    result = browser_service.launch_isolated_browser(tmp_path / "data", chatgpt, settings=settings)
    assert result["ok"] is True
    assert result["engine"] == "Chrome"
    assert result["inpage_toolbar"] is True
    assert result["zero_cdp"] is False
    assert len(cdp_calls) == 1
    assert any(a.startswith("--remote-debugging-port=") for a in launched[-1])

    # Explicitly forcing Chrome has the same silent-CDP fallback.
    launched.clear()
    cdp_calls.clear()
    forced = {**settings, "preferred_browser": "chrome"}
    forced_res = browser_service.launch_isolated_browser(tmp_path / "data", chatgpt, settings=forced)
    assert forced_res["ok"] is True and forced_res["zero_cdp"] is False
    assert len(cdp_calls) == 1

    # ``域名邮箱`` always stays Zero-CDP even on the silent-CDP engine path.
    launched.clear()
    cdp_calls.clear()
    domain = {
        "id": 303,
        "account_type": "域名邮箱",
        "account": "edge_mail@staff.example",
        "password": "MailPass#2031",
        "fp_config": generate_fingerprint_config(303, "edge_mail@staff.example"),
    }
    domain_res = browser_service.launch_isolated_browser(tmp_path / "data", domain, settings=settings)
    assert domain_res["ok"] is True
    assert domain_res["zero_cdp"] is True
    assert cdp_calls == []
    assert not any(a.startswith("--remote-debugging-port=") for a in launched[-1])


def test_inpage_chrome_cdp_uses_chrome_and_silent_cdp(tmp_path: Path, monkeypatch):
    fake_chrome = tmp_path / "chrome.exe"
    fake_edge = tmp_path / "msedge.exe"
    fake_chrome.write_bytes(b"")
    fake_edge.write_bytes(b"")
    monkeypatch.delenv("KEYVAULT_BROWSER_PATH", raising=False)
    monkeypatch.setattr(browser_service, "detect_chrome_path", lambda: str(fake_chrome))
    monkeypatch.setattr(browser_service, "detect_edge_path", lambda: str(fake_edge))
    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: str(fake_chrome))
    monkeypatch.setattr(
        browser_service,
        "activate_proxy_node",
        lambda *a, **kw: {"ok": True, "switched": False},
    )
    launched, cdp_calls = _stub_launch(monkeypatch)
    settings = {"browser_engine_mode": "inpage_chrome_cdp", "preferred_browser": "auto"}

    exe, err = browser_service.resolve_browser_executable(settings)
    assert exe == str(fake_chrome) and err == ""

    chatgpt_cdp = {
        "id": 304,
        "account_type": "ChatGPT",
        "account": "chrome_cdp@example.test",
        "fp_config": generate_fingerprint_config(304, "chrome_cdp@example.test"),
    }
    result = browser_service.launch_isolated_browser(tmp_path / "data", chatgpt_cdp, settings=settings)
    assert result["ok"] is True
    assert result["pure_native"] is False
    assert result["inpage_toolbar"] is True
    assert result["browser_engine_mode"] == "inpage_chrome_cdp"
    assert result["engine"] == "Chrome"
    assert result["zero_cdp"] is False
    assert len(cdp_calls) == 1
    port_arg = next(a for a in launched[-1] if a.startswith("--remote-debugging-port="))
    assert cdp_calls[0][0] == (int(port_arg.split("=", 1)[1]), Path(result["profile_dir"]))

    launched.clear()
    cdp_calls.clear()
    domain = {
        "id": 305,
        "account_type": "域名邮箱",
        "account": "chrome_mail@staff.example",
        "password": "MailPass#2032",
        "fp_config": generate_fingerprint_config(305, "chrome_mail@staff.example"),
    }
    domain_res = browser_service.launch_isolated_browser(tmp_path / "data", domain, settings=settings)
    assert domain_res["zero_cdp"] is True
    assert domain_res["inpage_toolbar"] is True
    assert (Path(domain_res["profile_dir"]) / "fp_ext" / "autologin.js").exists()
    assert cdp_calls == []
    assert not any(a.startswith("--remote-debugging-port=") for a in launched[-1])


def test_inpage_launch_args_carry_window_bounds_and_shared_debug_port(tmp_path: Path):
    account = {"id": 309, "account_type": "ChatGPT", "account": "bounds_chat@example.test"}
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    args = build_browser_launch_args(
        account,
        profile_dir,
        exe_path="C:\\fake\\chrome.exe",
        pure_native=False,
        enable_cdp=True,
        launch_bounds=(10, 20, 1280, 800),
        force_bounds=True,
        debug_port=34567,
    )
    assert "--window-position=10,20" in args
    assert "--window-size=1280,800" in args
    assert "--remote-debugging-port=34567" in args
    assert args[-1] == "https://chatgpt.com/"
    # Explicit debug_port wins over the per-account port derivation.
    assert not any(a.startswith("--remote-debugging-port=") and not a.endswith("34567") for a in args)


def test_pure_native_mode_keeps_zero_extension_and_zero_cdp(tmp_path: Path, monkeypatch):
    fake_chrome = tmp_path / "chrome.exe"
    fake_chrome.write_bytes(b"")
    monkeypatch.setattr(browser_service, "detect_chrome_path", lambda: str(fake_chrome))
    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: str(fake_chrome))
    monkeypatch.setattr(
        browser_service,
        "activate_proxy_node",
        lambda *a, **kw: {"ok": True, "switched": False},
    )
    launched, cdp_calls = _stub_launch(monkeypatch)
    settings = {"browser_engine_mode": "pure_native", "preferred_browser": "auto"}

    account = {"id": 306, "account_type": "Google", "account": "native_google@example.test"}
    # A stale generated extension from a previous in-page run must be cleaned.
    # Google never generates ``fp_ext`` itself, so seed the legacy files directly.
    stale_profile = browser_service.prepare_profile_dir(tmp_path / "data", account)
    browser_service._build_extension_files(
        stale_profile / "fp_ext",
        generate_fingerprint_config(account["id"], account["account"]),
        account,
    )
    assert (stale_profile / "fp_ext" / "autologin.js").exists()

    result = browser_service.launch_isolated_browser(tmp_path / "data", account, settings=settings)
    assert result["ok"] is True
    assert result["pure_native"] is True
    assert result["inpage_toolbar"] is False
    assert result["browser_engine_mode"] == "pure_native"
    assert result["zero_cdp"] is True
    assert result["manual_login"] is True
    assert cdp_calls == []
    # The generated extension files are purged (an empty fp_ext dir may remain).
    ext_dir = Path(result["profile_dir"]) / "fp_ext"
    assert not (ext_dir / "autologin.js").exists()
    assert not (ext_dir / "manifest.json").exists()
    assert not (ext_dir / "inject.js").exists()
    args = launched[-1]
    assert not any(a.startswith("--load-extension=") for a in args)
    assert not any(a.startswith("--remote-debugging-port=") for a in args)


def test_explicit_pure_native_oauth_override_stays_zero_injection(tmp_path: Path, monkeypatch):
    fake_edge = tmp_path / "msedge.exe"
    fake_edge.write_bytes(b"")
    monkeypatch.delenv("KEYVAULT_BROWSER_PATH", raising=False)
    monkeypatch.setattr(browser_service, "detect_edge_path", lambda: str(fake_edge))
    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: str(fake_edge))
    monkeypatch.setattr(
        browser_service,
        "activate_proxy_node",
        lambda *a, **kw: {"ok": True, "switched": False},
    )
    launched, cdp_calls = _stub_launch(monkeypatch)
    settings = {"browser_engine_mode": "inpage_edge", "preferred_browser": "auto"}

    account = {"id": 307, "account_type": "Google", "account": "oauth_google@example.test"}
    result = browser_service.launch_isolated_browser(
        tmp_path / "data",
        account,
        target_url="https://accounts.google.com/o/oauth2/v2/auth?client_id=1",
        pure_native=True,
        settings=settings,
    )
    assert result["ok"] is True
    assert result["pure_native"] is True
    assert result["inpage_toolbar"] is False
    assert result["zero_cdp"] is True
    assert cdp_calls == []
    # Reuses the resolved Edge binary (avoids Windows App-Bound Cookie damage).
    assert launched[-1][0] == str(fake_edge)
    assert not any(a.startswith("--load-extension=") for a in launched[-1])
    assert launched[-1][-1] == "https://accounts.google.com/o/oauth2/v2/auth?client_id=1"


def test_legacy_ext_alias_stays_backward_compatible(tmp_path: Path, monkeypatch):
    fake_chrome = tmp_path / "chrome.exe"
    fake_chrome.write_bytes(b"")
    monkeypatch.delenv("KEYVAULT_BROWSER_PATH", raising=False)
    monkeypatch.setattr(browser_service, "detect_chrome_path", lambda: str(fake_chrome))
    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: str(fake_chrome))
    monkeypatch.setattr(
        browser_service,
        "activate_proxy_node",
        lambda *a, **kw: {"ok": True, "switched": False},
    )
    launched, cdp_calls = _stub_launch(monkeypatch)

    account = {"id": 308, "account_type": "ChatGPT", "account": "legacy_chat@example.test"}
    # legacy_ext behaves like non-pure-native and honors explicit enable_cdp.
    result = browser_service.launch_isolated_browser(
        tmp_path / "data",
        account,
        enable_cdp=True,
        settings={"browser_engine_mode": "legacy_ext"},
    )
    assert result["ok"] is True
    assert result["pure_native"] is False
    assert result["inpage_toolbar"] is True
    assert result["browser_engine_mode"] == "legacy_ext"
    assert result["zero_cdp"] is False
    assert len(cdp_calls) == 1
    assert any(a.startswith("--remote-debugging-port=") for a in launched[-1])

    # Without an explicit enable_cdp the legacy alias keeps Zero-CDP.
    launched.clear()
    cdp_calls.clear()
    quiet = browser_service.launch_isolated_browser(
        tmp_path / "data", account, settings={"browser_engine_mode": "legacy_ext"}
    )
    assert quiet["ok"] is True and quiet["zero_cdp"] is True and cdp_calls == []
    assert any(a.startswith("--load-extension=") for a in launched[-1])


def test_wal_session_cookie_preserved_and_corrupt_manifest_recovered(tmp_path: Path, monkeypatch):
    import sqlite3

    profile = tmp_path / "data" / "browsers" / "acc_401"
    net_dir = profile / "Default" / "Network"
    net_dir.mkdir(parents=True, exist_ok=True)
    db_file = net_dir / "Cookies"
    # Create a WAL-mode Cookies DB and leave SID only in the uncheckpointed -wal
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE cookies (name TEXT, value TEXT)")
    conn.execute("INSERT INTO cookies (name, value) VALUES ('SID', 'auth_secret_in_wal')")
    conn.commit()
    conn.close()
    assert browser_service.has_authenticated_session_cookie(profile) is True

    # Corrupt fp_ext/manifest.json must be cleaned when unlocked without touching user files
    ext_dir = profile / "fp_ext"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "autologin.js").write_text("// old", encoding="utf-8")
    (ext_dir / "manifest.json").write_text("{corrupt-json", encoding="utf-8")
    user_file = ext_dir / "user_keep.txt"
    user_file.write_text("keep", encoding="utf-8")

    assert browser_service._remove_legacy_google_extension(profile) is True
    assert not (ext_dir / "autologin.js").exists()
    assert not (ext_dir / "manifest.json").exists()
    assert user_file.exists()


def test_failed_proxy_node_blocks_non_google_direct_launch_and_inject_idempotent(tmp_path: Path, monkeypatch):
    fake_chrome = tmp_path / "chrome.exe"
    fake_chrome.write_bytes(b"")
    monkeypatch.delenv("KEYVAULT_BROWSER_PATH", raising=False)
    monkeypatch.setattr(browser_service, "detect_chrome_path", lambda: str(fake_chrome))
    monkeypatch.setattr(browser_service, "find_chromium_executable", lambda: str(fake_chrome))
    monkeypatch.setattr(
        browser_service,
        "activate_proxy_node",
        lambda *a, **kw: {"ok": False, "error": "node offline"},
    )
    launched, _ = _stub_launch(monkeypatch)

    account = {
        "id": 402,
        "account_type": "ChatGPT",
        "account": "strict_proxy@example.test",
        "fp_config": {"proxy": "", "proxy_node": "US-Node-Down"},
    }
    res = browser_service.launch_isolated_browser(tmp_path / "data", account)
    assert res["ok"] is False
    assert "node offline" in res["error"] or "阻止直连" in res["error"]
    assert launched == []

    # inject.js contains idempotency guard & does not call this.getContext('2d') inside toDataURL
    ext_dir = tmp_path / "ext_check"
    fp = generate_fingerprint_config(403, "inject@example.test")
    browser_service._build_extension_files(ext_dir, fp, {"id": 403, "account": "inject@example.test"})
    inject_src = (ext_dir / "inject.js").read_text(encoding="utf-8")
    assert "__kv_inject_active" in inject_src
    assert "const srcCtx = this.getContext('2d')" not in inject_src

    autologin_src = (ext_dir / "autologin.js").read_text(encoding="utf-8")
    assert "__kv_capsule_render_timer" in autologin_src
    assert "__kv_ensure_timer" in autologin_src
    assert "seqAtCopy" in autologin_src
