from pathlib import Path
import pytest

from src.api import AccountApi
from src.browser_service import find_chromium_executable


def test_settings_modal_sort_mode_and_open_count_in_ui(tmp_path: Path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    exe = find_chromium_executable()
    if not exe:
        pytest.skip("Requires an installed Chromium browser")

    api = AccountApi(tmp_path / "data")
    g_ok = api.save_account({"account_type": "Google", "account": "healthy@gmail.com", "health_status": "可用"})["account"]
    g_banned = api.save_account({"account_type": "Google", "account": "banned@gmail.com", "health_status": "封号"})["account"]
    g_captcha = api.save_account({"account_type": "Google", "account": "captcha@gmail.com", "health_status": "人机"})["account"]
    g_unavail = api.save_account({"account_type": "Google", "account": "disabled@gmail.com", "health_status": "不可用"})["account"]
    c_banned = api.save_account({"account_type": "ChatGPT", "account": "chatgpt_banned@example.com", "health_status": "封号"})["account"]

    # Record 2 launches for g_ok so open_count=2 and browser_last_opened_at is non-empty
    api.db.record_browser_launch(g_ok["id"])
    api.db.record_browser_launch(g_ok["id"])

    errors: list[str] = []
    try:
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.launch(
                executable_path=exe,
                headless=True,
                args=["--disable-background-networking", "--disable-sync", "--host-resolver-rules=MAP * 127.0.0.1"],
            )
            context = browser.new_context(service_workers="block")
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
            page = context.new_page()
            page.set_default_timeout(5000)
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.expose_function("__apiCall", lambda method, args: getattr(api, method)(*args))
            page.add_init_script("""
                window.pywebview = {api: new Proxy({}, {
                    get: (_, method) => (...args) => window.__apiCall(method, args)
                })};
            """)
            page.goto((Path(__file__).parents[1] / "web" / "index.html").as_uri())
            page.wait_for_function("state.accounts.length === 5")

            # On "All" platform, RT/OAuth and Appeal buttons MUST be hidden
            btn_rt = page.locator("#btn-rt-oauth")
            btn_appeal = page.locator("#btn-appeal")
            playwright.expect(btn_rt).to_be_hidden()
            playwright.expect(btn_appeal).to_be_hidden()

            # Verify open count badge renders on g_ok
            card1 = page.locator(f'article.u-card[data-id="{g_ok["id"]}"]')
            playwright.expect(card1).to_contain_text("2次")

            # Verify warming sort mode: warming_stale puts never-opened first, warming_count puts g_ok first
            page.evaluate('setSortMode("warming_stale")')
            first_id_stale = page.locator("article.u-card").first.get_attribute("data-id")
            assert first_id_stale != str(g_ok["id"])

            page.evaluate('setSortMode("warming_count")')
            first_id_count = page.locator("article.u-card").first.get_attribute("data-id")
            assert first_id_count == str(g_ok["id"])

            # Verify Settings modal loads & saves Option C browser engine / kernel / language settings
            page.evaluate("openSettingsModal()")
            page.locator("#set-browser-engine").wait_for(state="attached")
            assert page.evaluate("document.getElementById('set-browser-engine').value") == "inpage_edge"
            engine_opts = page.evaluate(
                "Array.from(document.querySelectorAll('#set-browser-engine option')).map(o => o.value)"
            )
            assert engine_opts == ["inpage_edge", "inpage_chrome_cdp", "pure_native"]
            assert page.evaluate("document.getElementById('set-preferred-browser').value") == "auto"
            assert page.evaluate("document.getElementById('set-browser-lang').value") == "match_region"
            assert page.evaluate("document.getElementById('set-default-start-url').value") == "https://myaccount.google.com/"

            page.evaluate("""
                document.getElementById('set-browser-engine').value = 'inpage_chrome_cdp';
                document.getElementById('set-preferred-browser').value = 'edge';
                document.getElementById('set-browser-lang').value = 'en-US';
                document.getElementById('set-default-start-url').value = 'https://mail.google.com/';
                document.getElementById('set-auto-probe-geo').checked = false;
            """)
            page.evaluate("saveGeneralSettings()")
            page.wait_for_function("state.settings.preferred_browser === 'edge'")
            saved = api._ensure_settings()
            assert saved["browser_engine_mode"] == "inpage_chrome_cdp"
            assert saved["preferred_browser"] == "edge"
            assert saved["default_browser_lang"] == "en-US"
            assert saved["default_start_url"] == "https://mail.google.com/"
            assert saved["auto_probe_geo_on_launch"] is False

            # Switching to ChatGPT keeps RT/OAuth & Appeal hidden
            page.evaluate('setFilterType("ChatGPT")')
            page.wait_for_function('state.filterType === "ChatGPT"')
            playwright.expect(btn_rt).to_be_hidden()
            playwright.expect(btn_appeal).to_be_hidden()

            # Switching to Google shows RT/OAuth & Appeal buttons
            page.evaluate('setFilterType("Google")')
            page.wait_for_function('state.filterType === "Google"')
            playwright.expect(btn_rt).to_be_visible()
            playwright.expect(btn_appeal).to_be_visible()

            # Appeal modal shows ONLY abnormal/unavailable Google accounts (never healthy Google or ChatGPT)
            btn_appeal.click()
            page.locator("#appeal-modal").wait_for(state="visible")
            page.wait_for_function("document.querySelectorAll('#appeal-account-select option').length === 3")
            appeal_wrap = page.locator('.c-select-container[data-for-id="appeal-account-select"]')
            appeal_opts = appeal_wrap.locator(".c-select-option")
            assert appeal_opts.count() == 3
            opt_texts = [appeal_opts.nth(i).inner_text() for i in range(3)]
            joined = "\n".join(opt_texts)
            assert "banned@gmail.com" in joined
            assert "captcha@gmail.com" in joined
            assert "disabled@gmail.com" in joined
            assert "healthy@gmail.com" not in joined
            assert "chatgpt_banned@example.com" not in joined

            appeal_wrap.locator(".c-select-trigger").click()
            playwright.expect(appeal_wrap.locator(".c-select-menu")).to_be_visible()
            appeal_opts.nth(1).click()
            assert page.evaluate("document.getElementById('appeal-account-select').value") == str(g_captcha["id"])
            page.evaluate("closeAppealModal()")

            # RT modal shows ONLY Google accounts (4 Google accounts, not ChatGPT)
            btn_rt.click()
            page.locator("#rt-oauth-modal").wait_for(state="visible")
            page.wait_for_function("document.querySelectorAll('#rt-modal-account-select option').length === 4")
            rt_wrap = page.locator('.c-select-container[data-for-id="rt-modal-account-select"]')
            rt_opts = rt_wrap.locator(".c-select-option")
            assert rt_opts.count() == 4
            rt_joined = "\n".join(rt_opts.nth(i).inner_text() for i in range(4))
            assert "chatgpt_banned@example.com" not in rt_joined

            assert not errors
            browser.close()
    finally:
        api._shutdown()


def test_frontend_xss_delegation_disabled_select_esc_and_light_theme(tmp_path: Path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    exe = find_chromium_executable()
    if not exe:
        pytest.skip("Requires an installed Chromium browser")

    api = AccountApi(tmp_path / "data")
    g_acc = api.save_account({"account_type": "Google", "account": "ui_sec@gmail.com", "password": "pw"})["account"]
    api.db.bind_account_proxy_node(g_acc["id"], "http://127.0.0.1:7890", "US'\"alert(1)//", locked=True)
    monkeypatch.setattr(
        api,
        "get_proxy_nodes",
        lambda _arg="": {
            "client_name": "Stub",
            "controller_online": True,
            "ports": [{"port": 7890, "url": "http://127.0.0.1:7890", "label": "7890"}],
            "nodes": [{"name": "HK'\"\\xss", "region": "HK'Region", "delay": 42}],
            "active_node": "",
        },
    )
    # Inject a custom dict with HTML in icon and color_class to test XSS escaping
    api.save_system_dict({
        "dict_type": "account_type",
        "dict_key": "CustomTest",
        "dict_label": "CustomLabel",
        "color_class": 'x"><img src=x onerror=window.__xss=1>',
        "icon": '<img src=x onerror=window.__xss=1>',
    })

    errors: list[str] = []
    try:
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.launch(
                executable_path=exe,
                headless=True,
                args=["--disable-background-networking", "--disable-sync", "--host-resolver-rules=MAP * 127.0.0.1"],
            )
            context = browser.new_context(service_workers="block")
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
            page = context.new_page()
            page.set_default_timeout(5000)
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.expose_function("__apiCall", lambda method, args: getattr(api, method)(*args))
            page.add_init_script("""
                window.__xss = 0;
                window.pywebview = {api: new Proxy({}, {
                    get: (_, method) => (...args) => window.__apiCall(method, args)
                })};
            """)
            page.goto((Path(__file__).parents[1] / "web" / "index.html").as_uri())
            page.wait_for_function("state.accounts.length === 1")

            # Theme syncs body.light-theme for settings modals
            page.evaluate('applyTheme("light", false)')
            assert page.evaluate("document.body.classList.contains('light-theme')") is True
            page.evaluate('applyTheme("dark", false)')
            assert page.evaluate("document.body.classList.contains('light-theme')") is False

            # ESC closes account-modal cleanly (previously checked nonexistent #modal / closeModal)
            page.evaluate(f"openEditModal({g_acc['id']})")
            page.locator("#account-modal").wait_for(state="visible")
            page.keyboard.press("Escape")
            page.locator("#account-modal").wait_for(state="hidden")

            # Locked node disables custom select dropdown & dangerous node names use data-* delegation
            page.evaluate(f"openNodeModal({g_acc['id']})")
            page.locator("#node-modal-main-content").wait_for(state="visible")
            port_wrap = page.locator('.c-select-container[data-for-id="node-port-select"]')
            assert "is-disabled" in (port_wrap.get_attribute("class") or "")
            port_wrap.locator(".c-select-trigger").click(force=True)
            assert "open" not in (port_wrap.get_attribute("class") or "")
            page.locator("#node-region-tabs .region-tab").nth(1).click()
            assert page.evaluate("state.selectedRegion") == "HK'Region"
            page.evaluate("closeNodeModal()")

            # Google QuickCred hides duplicate #qc-generate-secret while keeping #qc-google-review
            page.evaluate(f"openQuickCredModal({g_acc['id']})")
            page.locator("#quick-cred-modal").wait_for(state="visible")
            assert page.evaluate("document.getElementById('qc-generate-secret').hidden") is True
            page.keyboard.press("Escape")

            # Empty Appeal modal never renders non-Google fingerprint button
            page.evaluate("openAppealModal()")
            page.locator("#appeal-modal").wait_for(state="visible")
            assert page.locator("#appeal-channels-list button", has_text="指纹浏览器打开").count() == 0

            assert page.evaluate("window.__xss") == 0
            assert not errors
            browser.close()
    finally:
        api._shutdown()
