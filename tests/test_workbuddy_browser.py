"""Native Chrome + non-``Runtime.enable`` CDP integration tests.

These tests never import Playwright. ``WorkBuddyBrowser`` launches the real
system Chrome/Edge itself and drives it through its own raw CDP client, so the
tests assert the production launch flags and the zero-``Runtime.enable``
invariant for real.

All pages are served by a local self-signed HTTPS server that Chrome is told to
resolve ``github.com`` / ``www.workbuddy.ai`` to via ``--host-resolver-rules``.
That exercises Chrome's real network stack (including ``Network.responseReceived``
Document status codes 403 / 200) completely offline, without root privileges and
without binding port 443.
"""

import datetime
import inspect
import shutil
import ssl
import subprocess
import sys
import threading
import time
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import src.workbuddy_browser as wb
from src.browser_service import find_chromium_executable
from src.workbuddy_browser import WorkBuddyBrowser, _FILL_FIELDS


requires_chrome = pytest.mark.skipif(
    not find_chromium_executable(),
    reason="Requires an installed Chrome/Edge/Chromium browser",
)


# ---------------------------------------------------------------------------
# Local HTTPS fixture site
# ---------------------------------------------------------------------------
GITHUB_HOME_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>GitHub</title></head>
<body>
  <header>
    <a href="/login">Sign in</a>
    <a href="/signup" class="HeaderMenu-link--sign-up">Sign up</a>
  </header>
  <main>
    <form action="/signup" method="get">
      <input id="hero_user_email" name="user_email" type="email" />
      <button type="submit">Sign up for GitHub</button>
    </form>
  </main>
</body></html>
"""

GITHUB_SIGNUP_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Join GitHub</title></head>
<body>
  <div id="email-container">
    <input id="email" type="email" autocomplete="email">
    <button type="button" data-continue-to="password-container"
      onclick="document.getElementById('password').style.display='block'">Continue</button>
  </div>
  <div id="password-container">
    <input id="password" type="password" autocomplete="new-password" style="display:none">
    <button type="button" data-continue-to="username-container"
      onclick="document.getElementById('login').style.display='block'">Continue</button>
  </div>
  <div id="username-container">
    <input id="login" type="text" autocomplete="username" style="display:none">
  </div>
</body></html>
"""

GITHUB_OAUTH_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Authorize</title></head>
<body>
  <h1>Authorize KeyVault</h1>
  <input id="password" type="password">
  <button type="button">Authorize</button>
</body></html>
"""

GITHUB_RESTRICTED_HTML = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
    "<h1>访问暂时受限</h1>"
    "<p>为什么需要经过「我不是机器人」的验证？</p>"
    "<p>系统侦测到您浏览网页的速度异常，为非正常操作</p>"
    "<p>您使用的IP位址(IP:103.142.140.241)与网路机器人相同</p>"
    "</body></html>"
)

# /signup with a real submit button. Clicking it navigates to the verification
# page, exactly like GitHub does after "Create account".
GITHUB_SIGNUP_SUBMIT_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Join GitHub</title></head>
<body>
  <h1>Join GitHub</h1>
  <form action="/account_verifications" method="get">
    <input id="email" name="user[email]" type="email" autocomplete="email">
    <input id="password" name="user[password]" type="password" autocomplete="new-password">
    <input id="login" name="user[login]" type="text" autocomplete="username">
    <button type="submit">Create account</button>
    <button type="button">Continue with Google</button>
  </form>
</body></html>
"""

GITHUB_SIGNUP_TAKEN_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Join GitHub</title></head>
<body>
  <h1>Join GitHub</h1>
  <form action="/account_verifications" method="get">
    <input id="email" name="user[email]" type="email" autocomplete="email">
    <input id="password" name="user[password]" type="password" autocomplete="new-password">
    <input id="login" name="user[login]" type="text" autocomplete="username" aria-invalid="true">
    <p class="error" role="alert">Username worker1234 is not available</p>
    <button type="submit">Create account</button>
  </form>
  <script>
    // GitHub keeps the "not available" error until the value actually changes.
    document.getElementById('login').addEventListener('input', (event) => {
      if (!event.target.value || event.target.value === 'worker1234') return;
      document.getElementById('login').setAttribute('aria-invalid', 'false');
      document.querySelector('.error').style.display = 'none';
    });
  </script>
</body></html>
"""

# /signup as it looks right after the operator solved a slider: every field is
# already filled. Submitting records #login's value and how many ``input``
# events it received in sessionStorage, so a test can prove the resume path
# never re-filled or cleared it (which would invalidate the solved token).
GITHUB_SIGNUP_VERIFIED_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Join GitHub</title></head>
<body>
  <h1>Join GitHub</h1>
  <form action="/account_verifications" method="get"
    onsubmit="sessionStorage.setItem('login_at_submit', document.getElementById('login').value);">
    <input id="email" name="user[email]" type="email" autocomplete="email">
    <input id="password" name="user[password]" type="password" autocomplete="new-password">
    <input id="login" name="user[login]" type="text" autocomplete="username">
    <button type="submit">Create account</button>
  </form>
  <script>
    document.getElementById('email').value = 'worker@example.org';
    document.getElementById('password').value = 'testPassword123';
    document.getElementById('login').value = 'worker1234';
    // sessionStorage is shared with the CDP isolated world, unlike `window`
    // globals, so a test can count every input event #login received.
    if (!sessionStorage.getItem('login_input_events')) {
      sessionStorage.setItem('login_input_events', '0');
    }
    document.getElementById('login').addEventListener('input', () => {
      const n = parseInt(sessionStorage.getItem('login_input_events') || '0', 10);
      sessionStorage.setItem('login_input_events', String(n + 1));
    });
  </script>
</body></html>
"""

# Same as GITHUB_SIGNUP_VERIFIED_HTML, but models DataDome's ``history.back()``:
# Chrome restores #email and #login from session history yet clears the
# password field, so only #password needs to be filled again.
GITHUB_SIGNUP_VERIFIED_CLEARED_PASSWORD_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Join GitHub</title></head>
<body>
  <h1>Join GitHub</h1>
  <form action="/account_verifications" method="get"
    onsubmit="sessionStorage.setItem('login_at_submit', document.getElementById('login').value);
              sessionStorage.setItem('password_at_submit', document.getElementById('password').value);">
    <input id="email" name="user[email]" type="email" autocomplete="email">
    <input id="password" name="user[password]" type="password" autocomplete="new-password">
    <input id="login" name="user[login]" type="text" autocomplete="username">
    <button type="submit">Create account</button>
  </form>
  <script>
    document.getElementById('email').value = 'worker@example.org';
    document.getElementById('login').value = 'worker1234';
    // history.back() restoration: the password input comes back empty.
    document.getElementById('password').value = '';
    if (!sessionStorage.getItem('login_input_events')) {
      sessionStorage.setItem('login_input_events', '0');
    }
    document.getElementById('login').addEventListener('input', () => {
      const n = parseInt(sessionStorage.getItem('login_input_events') || '0', 10);
      sessionStorage.setItem('login_input_events', String(n + 1));
    });
    if (!sessionStorage.getItem('password_input_events')) {
      sessionStorage.setItem('password_input_events', '0');
    }
    document.getElementById('password').addEventListener('input', () => {
      const n = parseInt(sessionStorage.getItem('password_input_events') || '0', 10);
      sessionStorage.setItem('password_input_events', String(n + 1));
    });
  </script>
</body></html>
"""

GITHUB_VERIFY_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Verify your email</title></head>
<body>
  <h1>Enter the code</h1>
  <p>We sent a launch code to your email.</p>
  <form action="/signup" method="get">
    <input name="launch_code[]" maxlength="1">
    <input name="launch_code[]" maxlength="1">
    <input name="launch_code[]" maxlength="1">
    <input name="launch_code[]" maxlength="1">
    <input name="launch_code[]" maxlength="1">
    <input name="launch_code[]" maxlength="1">
    <button type="submit">Verify</button>
  </form>
</body></html>
"""

# Verification page whose submit button advances to a logged-in page.
GITHUB_VERIFY_OK_HTML = GITHUB_VERIFY_HTML.replace(
    '<form action="/signup" method="get">', '<form action="/dashboard" method="get">'
)

GITHUB_DASHBOARD_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Dashboard</title>
<meta name="user-login" content="worker1234"></head>
<body><h1>Dashboard</h1></body></html>
"""

GITHUB_OAUTH_AUTHORIZE_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Authorize</title></head>
<body>
  <h1>Authorize WorkBuddy</h1>
  <form action="https://www.workbuddy.ai/oauth/callback" method="get">
    <button id="js-oauth-authorize-btn" name="authorize" value="1" type="submit">Authorize</button>
  </form>
</body></html>
"""

# Authorize submit lands on a tiny hop page that seeds a JWT into storage and
# bounces to the WorkBuddy app, mirroring the real OAuth round trip.
WORKBUDDY_OAUTH_CALLBACK_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Signing in</title></head>
<body>
<script>
  const token = [
    'eyJhbGciOiJIUzI1NiJ9',
    'eyJzdWIiOiJ3b3JrZXIxMjM0IiwiZXhwIjo0MTAyNDQ0ODAwfQ',
    'c2lnbmF0dXJl'
  ].join('.');
  localStorage.setItem('wb_auth', JSON.stringify({accessToken: token, refreshToken: 'refresh-1', uid: 'worker1234'}));
  location.replace('/app');
</script>
</body></html>
"""

WORKBUDDY_APP_LOGGED_IN_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>WorkBuddy App</title></head>
<body>
  <main class="wb-home-composer">
    <div role="textbox" contenteditable="true" data-slate-editor="true"></div>
    <div data-cb-chat-input-toolbar-right>
      <button role="button" data-track-id="agent_session_input_status"
        onclick="this.textContent='received'">send</button>
    </div>
  </main>
  <div class="wb-chat-message" data-role="assistant">你好，我是 WorkBuddy！</div>
</body></html>
"""

# Homepage where "Continue with GitHub" starts the real OAuth redirect chain.
WORKBUDDY_HOME_OAUTH_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>WorkBuddy</title></head>
<body>
  <header>
    <button onclick="document.getElementById('gh-oauth').style.display='inline-block'">Log in</button>
  </header>
  <button id="gh-oauth" style="display:none"
    onclick="location.href='https://github.com/login/oauth/authorize'">Continue with GitHub</button>
</body></html>
"""

WORKBUDDY_APP_REPLY_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>WorkBuddy App</title></head>
<body>
  <main class="wb-home-composer">
    <div role="textbox" contenteditable="true" data-slate-editor="true"></div>
    <div data-cb-chat-input-toolbar-right>
      <button role="button" data-track-id="agent_session_input_status"
        onclick="
          this.textContent = 'received';
          const reply = document.createElement('div');
          reply.className = 'wb-chat-message';
          reply.setAttribute('data-role', 'assistant');
          reply.textContent = '你好！我是 WorkBuddy，有什么可以帮你？';
          document.body.appendChild(reply);
        ">send</button>
    </div>
  </main>
</body></html>
"""

WORKBUDDY_APP_SILENT_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>WorkBuddy App</title></head>
<body>
  <main class="wb-home-composer">
    <div role="textbox" contenteditable="true" data-slate-editor="true"></div>
    <div data-cb-chat-input-toolbar-right>
      <button role="button" data-track-id="agent_session_input_status">send</button>
    </div>
  </main>
</body></html>
"""

WORKBUDDY_REGION_HOME_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>WorkBuddy</title></head>
<body>
  <header>
    <a href="/app">Web App</a>
  </header>
  <main><p>Welcome back</p></main>
</body></html>
"""


def _workbuddy_home_with_profile_html() -> str:
    return """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>WorkBuddy</title></head>
<body>
  <header>
    <button id="complete-profile"
      onclick="document.getElementById('profile-modal').style.display='block'">完善信息</button>
  </header>
  <div id="profile-modal" style="display:none">
    <select id="region">
      <option value="cn">中国</option>
      <option value="us">United States</option>
    </select>
    <button id="confirm-profile"
      onclick="document.getElementById('profile-modal').style.display='none';
               document.getElementById('complete-profile').style.display='none'">确认</button>
  </div>
</body></html>
"""

WORKBUDDY_HOME_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>WorkBuddy</title></head>
<body>
  <header>
    <button onclick="document.getElementById('gh-oauth').style.display='inline-block'">Log in</button>
    <a href="/app">Web App</a>
  </header>
  <button id="gh-oauth" style="display:none"
    onclick="this.dataset.clicked='1'">Continue with GitHub</button>
</body></html>
"""

WORKBUDDY_APP_HTML = """
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>WorkBuddy App</title></head>
<body>
  <main class="wb-home-composer">
    <div role="textbox" contenteditable="true" data-slate-editor="true"></div>
    <div data-cb-chat-input-toolbar-right>
      <button role="button" data-track-id="agent_session_input_status"
        onclick="this.textContent='received'">send</button>
    </div>
  </main>
</body></html>
"""


def _generate_self_signed_cert(directory: Path) -> tuple[Path, Path]:
    """Create a throw-away cert covering github.com + www.workbuddy.ai."""
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        x509 = None

    if x509 is not None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "github.com")])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("github.com"),
                        x509.DNSName("www.workbuddy.ai"),
                        x509.DNSName("127.0.0.1"),
                    ]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        return cert_path, key_path

    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("No certificate generator available (cryptography or openssl)")
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "2",
            "-subj",
            "/CN=github.com",
            "-addext",
            "subjectAltName=DNS:github.com,DNS:www.workbuddy.ai,DNS:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert_path, key_path


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ssl.SSLError, OSError)):
            return
        super().handle_error(request, client_address)


class _SiteHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KeyVaultTestSite/1.0"

    def do_GET(self):  # noqa: N802 - stdlib naming
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        path = urllib.parse.urlsplit(self.path).path or "/"
        result = self.server.site.resolve(host, path, self.path)
        status, body, content_type = result[:3]
        extra_headers = result[3] if len(result) > 3 else {}
        payload = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            self.close_connection = True

    def log_message(self, fmt, *args):  # keep pytest output clean
        return


class LocalHttpsSite:
    """Offline HTTPS stand-in for github.com and www.workbuddy.ai."""

    def __init__(self, cert_dir: Path):
        self.requests: list[dict] = []
        self.github_status = 403
        # Scenario switches used by the browser-auto tests.
        self.github_signup_mode = "wizard"  # "wizard" | "submit" | "taken" | "verified"
        self.github_verify_mode = "pending"  # "pending" | "ok"
        self.github_oauth_mode = "plain"  # "plain" | "authorize"
        self.workbuddy_home_mode = "login"  # "login" | "region" | "profile"
        self.workbuddy_app_mode = "plain"  # "plain" | "replied"
        self._username_taken_polls = 0
        cert_path, key_path = _generate_self_signed_cert(cert_dir)
        self._httpd = _QuietThreadingHTTPServer(("127.0.0.1", 0), _SiteHandler)
        self._httpd.site = self
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path))
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="kv-test-https", daemon=True
        )
        self._thread.start()

    # -- Chrome wiring -------------------------------------------------
    def chrome_args(self) -> list[str]:
        mapping = (
            f"MAP github.com 127.0.0.1:{self.port}, "
            f"MAP www.workbuddy.ai 127.0.0.1:{self.port}, "
            f"MAP geo.captcha-delivery.com 127.0.0.1:{self.port}, "
            f"MAP octocaptcha.com 127.0.0.1:{self.port}"
        )
        return [
            f"--host-resolver-rules={mapping}",
            "--ignore-certificate-errors",
            "--no-proxy-server",
            "--disable-quic",
        ]

    # -- routing -------------------------------------------------------
    def resolve(self, host: str, path: str, raw_path: str):
        self.requests.append(
            {"host": host, "path": path, "raw": raw_path, "at": time.time()}
        )
        html = "text/html; charset=utf-8"
        if host == "github.com":
            if path == "/":
                if self.github_status != 200:
                    return self.github_status, "<html><body>Forbidden</body></html>", html
                return 200, GITHUB_HOME_HTML, html
            if path == "/signup":
                if self.github_signup_mode == "submit":
                    return 200, GITHUB_SIGNUP_SUBMIT_HTML, html
                if self.github_signup_mode == "taken":
                    return 200, GITHUB_SIGNUP_TAKEN_HTML, html
                if self.github_signup_mode == "verified":
                    return 200, GITHUB_SIGNUP_VERIFIED_HTML, html
                if self.github_signup_mode == "verified_cleared_password":
                    return 200, GITHUB_SIGNUP_VERIFIED_CLEARED_PASSWORD_HTML, html
                return 200, GITHUB_SIGNUP_HTML, html
            if path == "/account_verifications":
                if self.github_verify_mode == "ok":
                    return 200, GITHUB_VERIFY_OK_HTML, html
                return 200, GITHUB_VERIFY_HTML, html
            if path == "/dashboard":
                return 200, GITHUB_DASHBOARD_HTML, html
            if path == "/login/oauth/authorize":
                if self.github_oauth_mode == "authorize":
                    return 200, GITHUB_OAUTH_AUTHORIZE_HTML, html
                return 200, GITHUB_OAUTH_HTML, html
            if path == "/restricted":
                return 200, GITHUB_RESTRICTED_HTML, html
            if path == "/login":
                return 200, "<html><body><input id='login'></body></html>", html
            return 404, "<html><body>Not Found</body></html>", html
        if host in ("geo.captcha-delivery.com", "octocaptcha.com"):
            return 200, "<html><body>challenge frame</body></html>", html
        if host == "www.workbuddy.ai":
            if path == "/app":
                if self.workbuddy_app_mode == "replied":
                    return 200, WORKBUDDY_APP_LOGGED_IN_HTML, html
                if self.workbuddy_app_mode == "reply_on_send":
                    return 200, WORKBUDDY_APP_REPLY_HTML, html
                if self.workbuddy_app_mode == "silent":
                    return 200, WORKBUDDY_APP_SILENT_HTML, html
                return 200, WORKBUDDY_APP_HTML, html
            if path == "/oauth/callback":
                return 200, WORKBUDDY_OAUTH_CALLBACK_HTML, html
            if path == "/":
                if self.workbuddy_home_mode == "region":
                    return 200, WORKBUDDY_REGION_HOME_HTML, html
                if self.workbuddy_home_mode == "profile":
                    return 200, _workbuddy_home_with_profile_html(), html
                if self.workbuddy_home_mode == "oauth":
                    return 200, WORKBUDDY_HOME_OAUTH_HTML, html
                return 200, WORKBUDDY_HOME_HTML, html
            return 404, "<html><body>Not Found</body></html>", html
        return 404, "<html><body>Unknown host</body></html>", html
    def github_paths(self) -> list[str]:
        return [r["path"] for r in self.requests if r["host"] == "github.com"]

    def close(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass


@pytest.fixture()
def site(tmp_path: Path):
    server = LocalHttpsSite(tmp_path / "certs")
    try:
        yield server
    finally:
        server.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _new_helper(tmp_path: Path, site: LocalHttpsSite) -> WorkBuddyBrowser:
    helper = WorkBuddyBrowser(tmp_path / "sessions")
    helper.headless = True
    helper.extra_chromium_args = site.chrome_args()
    return helper


def _wait_until(predicate, timeout: float = 8.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _assert_no_runtime_enable(helper: WorkBuddyBrowser) -> None:
    checked = 0
    for session in helper.sessions.values():
        context = session.get("context")
        if context is None:
            continue
        for page in context.pages:
            assert "Runtime.enable" not in page.sent_methods, (
                f"Runtime.enable leaked into {page.url}: {page.sent_methods}"
            )
            checked += 1
    assert checked >= 1, "expected at least one live CDP page to inspect"


def _read_process_cmdline(pid: int) -> str:
    """Best-effort read of a live process command line (all platforms)."""
    try:
        if sys.platform == "win32":
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}')"
                    ".CommandLine",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
            return (completed.stdout or "").strip()
        proc_cmdline = Path(f"/proc/{int(pid)}/cmdline")
        if proc_cmdline.exists():
            return (
                proc_cmdline.read_bytes()
                .replace(b"\x00", b" ")
                .decode("utf-8", "replace")
                .strip()
            )
        completed = subprocess.run(
            ["ps", "-o", "command=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
        return (completed.stdout or "").strip()
    except Exception:
        return ""


def _force_signup_channel(monkeypatch, header_first: bool) -> None:
    """Deterministically choose the GitHub homepage signup entry channel."""
    real_randbelow = wb.secrets.randbelow

    def _randbelow(n: int) -> int:
        if n == 2:
            return 1 if header_first else 0
        return real_randbelow(n)

    monkeypatch.setattr(
        wb,
        "secrets",
        types.SimpleNamespace(randbelow=_randbelow, choice=wb.secrets.choice),
    )


def _add_frame(
    page,
    frame_id: str,
    src: str,
    *,
    title: str = "",
    width: str = "900",
    height: str = "800",
    class_name: str = "",
) -> None:
    """Append a fresh iframe with the given identity and size to the page."""
    page.evaluate(
        """([id, src, title, width, height, className]) => {
            const old = document.getElementById(id);
            if (old) old.remove();
            const frame = document.createElement('iframe');
            frame.id = id;
            frame.src = src;
            if (title) frame.title = title;
            if (className) frame.className = className;
            frame.width = width;
            frame.height = height;
            document.body.appendChild(frame);
        }""",
        [frame_id, src, title, width, height, class_name],
    )


@pytest.fixture()
def action_delays(monkeypatch):
    """Record human pacing calls without actually sleeping."""
    calls: list[tuple[float, float]] = []

    def _record(min_s: float = 1.0, max_s: float = 2.0) -> None:
        calls.append((min_s, max_s))

    monkeypatch.setattr(wb, "_sleep_action_delay", _record)
    # Also disable the tiny 80-90ms click settle pause so tests stay quick.
    monkeypatch.setattr(wb, "ACTION_DELAY", 0)
    return calls


@pytest.fixture()
def launched_processes(monkeypatch):
    """Record every Chrome process spawned by WorkBuddyBrowser and fail on leaks."""
    records: list[dict] = []
    real_subprocess = wb.subprocess

    class _Recorder:
        def __getattr__(self, item):
            return getattr(real_subprocess, item)

        def Popen(self, args, **kwargs):  # noqa: N802 - mirrors subprocess
            proc = real_subprocess.Popen(args, **kwargs)
            records.append({"args": list(args), "kwargs": kwargs, "proc": proc})
            return proc

    monkeypatch.setattr(wb, "subprocess", _Recorder())
    yield records
    leaked = [record for record in records if record["proc"].poll() is None]
    for record in leaked:
        try:
            record["proc"].kill()
        except Exception:
            pass
    assert not leaked, (
        "Chrome process leak detected: "
        + ", ".join(str(record["proc"].pid) for record in leaked)
    )


# ---------------------------------------------------------------------------
# 0. Human pacing helper (no browser required)
# ---------------------------------------------------------------------------
def test_sleep_action_delay_uses_random_1_to_2_second_window(monkeypatch):
    sleeps: list[float] = []

    class _TimeShim:
        def __getattr__(self, item):
            return getattr(time, item)

        def sleep(self, value):
            sleeps.append(value)

    monkeypatch.setattr(wb, "time", _TimeShim())
    monkeypatch.setattr(
        wb, "secrets", types.SimpleNamespace(randbelow=lambda n: 0, choice=lambda s: s[0])
    )
    wb._sleep_action_delay()
    assert sleeps == [1.0]

    sleeps.clear()
    monkeypatch.setattr(
        wb,
        "secrets",
        types.SimpleNamespace(randbelow=lambda n: n - 1, choice=lambda s: s[0]),
    )
    wb._sleep_action_delay()
    assert sleeps == [2.0]

    sleeps.clear()
    monkeypatch.setattr(wb, "ACTION_DELAY", 0)
    wb._sleep_action_delay()
    assert sleeps == []


# ---------------------------------------------------------------------------
# 1 + 2. Native launch flags and zero Runtime.enable
# ---------------------------------------------------------------------------
@requires_chrome
def test_native_launch_flags_and_zero_runtime_enable(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 1,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        helper.open(task, "testPassword123")

        assert launched_processes, "Chrome was never launched"
        launch = launched_processes[-1]
        args = launch["args"]
        assert args[0] == find_chromium_executable()
        assert "--enable-automation" not in args
        assert all("AutomationControlled" not in arg for arg in args)
        assert not any(arg.startswith("--disable-blink-features") for arg in args)
        assert "--headless=new" in args
        assert any(arg.startswith("--user-data-dir=") for arg in args)
        assert any(arg.startswith("--remote-debugging-port=") for arg in args)

        # The real OS process must carry the same clean command line.
        pid = launch["proc"].pid
        assert launch["proc"].poll() is None
        cmdline = _read_process_cmdline(pid)
        if cmdline:
            assert "--remote-debugging-port=" in cmdline
            assert "--enable-automation" not in cmdline
            assert "AutomationControlled" not in cmdline

        # Zero Runtime.enable across every opened tab/session.
        _assert_no_runtime_enable(helper)
        page = helper.tabs["github"]
        assert "Runtime.enable" not in page.sent_methods
        with pytest.raises(RuntimeError, match="Runtime.enable"):
            page.send("Runtime.enable")
        assert "Runtime.enable" not in page.sent_methods

        # Opening another tab must not introduce Runtime.enable either.
        second = helper.context.new_page()
        second.goto("https://github.com/signup")
        assert "Runtime.enable" not in second.sent_methods
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 2b. Visible windows must never use CREATE_NO_WINDOW
# ---------------------------------------------------------------------------
@requires_chrome
def test_visible_windows_use_zero_creationflags_and_headless_hides_console(
    tmp_path: Path, monkeypatch
):
    captured: list[dict] = []

    class _FakeProc:
        pid = 424242

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    def _fake_popen(args, **kwargs):
        captured.append({"args": list(args), "kwargs": dict(kwargs)})
        return _FakeProc()

    monkeypatch.setattr(wb.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(wb, "_wait_for_devtools", lambda *args, **kwargs: True)

    helper = WorkBuddyBrowser(tmp_path / "sessions_flags")
    helper.headless = False
    helper._launch_browser(tmp_path / "sessions_flags" / "visible", 0, {}, "")
    visible = captured[-1]
    assert visible["kwargs"]["creationflags"] == 0, (
        "visible desktop Chrome must launch exactly like a normal window"
    )
    assert "--headless=new" not in visible["args"]

    helper.headless = True
    helper._launch_browser(tmp_path / "sessions_flags" / "hidden", 1, {}, "")
    hidden = captured[-1]
    assert "--headless=new" in hidden["args"]
    expected = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        if sys.platform == "win32"
        else 0
    )
    assert hidden["kwargs"]["creationflags"] == expected


# ---------------------------------------------------------------------------
# 3. HTTP 403 preservation, window lock and form scope
# ---------------------------------------------------------------------------
@requires_chrome
def test_http_403_preserved_window_lock_and_form_scope(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 1,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        with pytest.raises(ValueError, match="HTTP 403"):
            helper.open(task, "testPassword123")
        assert len(helper.sessions) == 1
        page = helper.tabs["github"]

        # Another KeyVault instance must not be able to steal the window lock.
        other = WorkBuddyBrowser(helper.root)
        with pytest.raises(ValueError, match="占用"):
            other.close(discard_id=1)
        assert (helper.root / "1").is_dir()

        site.github_status = 200
        page.reload()
        helper.open(task, "testPassword123")

        assert page.locator("#email").input_value() == task["email"]
        assert page.locator("#password").input_value() == "testPassword123"
        assert page.locator("#login").input_value() == task["username"]

        # A non-empty user-edited username must survive a re-open untouched.
        page.locator("#login").fill("manual-user5678")
        helper.open(task, "testPassword123")
        assert page.locator("#login").input_value() == "manual-user5678"

        # Password must never be filled on the OAuth authorize page.
        page.goto("https://github.com/login/oauth/authorize")
        assert page.locator("#password").input_value() == ""
        filled = page.evaluate(
            _FILL_FIELDS,
            {
                "origin": "https://github.com",
                "path": "/signup",
                "fields": [["#password", "never-filled"]],
            },
        )
        assert filled == 0
        assert (
            helper._fill(
                page, "https://github.com", "/signup", [["#password", "never-filled"]]
            )
            == 0
        )
        assert (
            helper._fill_github_signup(
                page, task["email"], "never-filled", task["username"], task=task
            )
            == 0
        )
        assert page.locator("#password").input_value() == ""

        helper.show_paused_window(task)
        assert page.url == "https://github.com/login/oauth/authorize"
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 4. Homepage-first random channels + slow 1~2s pacing
# ---------------------------------------------------------------------------
@requires_chrome
def test_homepage_channels_progressive_wizard_and_pacing(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays, monkeypatch
):
    helper = _new_helper(tmp_path, site)
    task_hero = {
        "id": 11,
        "email": "hero@ggb6.com",
        "username": "hero1111",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    task_header = {
        "id": 12,
        "email": "header@ggb6.com",
        "username": "header2222",
        "step": "github",
        "greeting": "hi",
        "round_slot": 1,
    }
    try:
        site.github_status = 200

        # --- Channel A: hero email box in the middle of the homepage -------
        _force_signup_channel(monkeypatch, header_first=False)
        site.requests.clear()
        action_delays.clear()
        message_a = helper.open(task_hero, "HeroPass#1")
        assert "首页中间邮箱框" in message_a
        assert site.github_paths()[:1] == ["/"], "GitHub homepage must be visited first"
        assert "/signup" in site.github_paths()

        page_a = helper._tabs_for(11)["github"]
        assert page_a.locator("#email").input_value() == "hero@ggb6.com"
        assert page_a.locator("#password").input_value() == "HeroPass#1"
        assert page_a.locator("#login").input_value() == "hero1111"

        # Progressive Continue wizard: password/login start hidden and are
        # revealed by the Continue buttons while each field is filled.
        assert page_a.locator("#password").is_visible()
        assert page_a.locator("#login").is_visible()
        page_a.goto("https://github.com/signup")
        assert page_a.locator("#password").is_visible() is False
        assert page_a.locator("#login").is_visible() is False
        filled = helper._fill_github_signup(
            page_a, "hero@ggb6.com", "HeroPass#1", "hero1111", task=task_hero
        )
        assert filled == 3
        assert page_a.locator("#password").is_visible()
        assert page_a.locator("#login").is_visible()
        assert page_a.locator("#email").input_value() == "hero@ggb6.com"
        assert page_a.locator("#password").input_value() == "HeroPass#1"
        assert page_a.locator("#login").input_value() == "hero1111"

        # Slow 1~2s human pacing between click -> paste -> next field.
        assert len(action_delays) >= 6
        assert all(call == (1.0, 2.0) for call in action_delays)

        # --- Channel B: top-right Sign up button --------------------------
        _force_signup_channel(monkeypatch, header_first=True)
        site.requests.clear()
        action_delays.clear()
        message_b = helper.open(task_header, "HeaderPass#2")
        assert "右上角注册按钮" in message_b
        assert site.github_paths()[:1] == ["/"], "GitHub homepage must be visited first"
        page_b = helper._tabs_for(12)["github"]
        assert page_b.locator("#email").input_value() == "header@ggb6.com"
        assert page_b.locator("#password").input_value() == "HeaderPass#2"
        assert page_b.locator("#login").input_value() == "header2222"
        assert len(action_delays) >= 6
        assert all(call == (1.0, 2.0) for call in action_delays)

        assert len(helper.sessions) == 2
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 5. WorkBuddy homepage login trigger + /app greeting + draft protection
# ---------------------------------------------------------------------------
@requires_chrome
def test_workbuddy_login_greeting_and_draft_protection(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 21,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "workbuddy",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        site.requests.clear()
        message = helper.open(task, "testPassword123")
        assert "WorkBuddy" in message
        wb_page = helper._tabs_for(21)["workbuddy"]
        # Homepage "Log in" was clicked, then "Continue with GitHub".
        assert wb_page.locator("#gh-oauth").get_attribute("data-clicked") == "1"
        assert any(r["host"] == "www.workbuddy.ai" and r["path"] == "/" for r in site.requests)

        # Greeting step opens the web app.
        task["step"] = "greeting"
        helper.open(task, "testPassword123")
        assert wb_page.url == "https://www.workbuddy.ai/app"

        composer = wb_page.locator(".wb-home-composer")
        editor = composer.locator(
            '[role="textbox"][contenteditable="true"][data-slate-editor="true"]'
        )
        assert editor.count() == 1

        # A non-empty user draft must be protected.
        editor.fill("草稿")
        with pytest.raises(ValueError, match="草稿"):
            helper.prepare_greeting(task)
        assert editor.inner_text() == "草稿"

        # Empty composer: greeting is inserted and the send button is clicked.
        editor.fill("")
        send = helper.prepare_greeting(task)
        send.click()
        assert editor.inner_text() == "hi"
        assert (
            wb_page.locator('[data-track-id="agent_session_input_status"]').inner_text()
            == "received"
        )
        assert all(call == (1.0, 2.0) for call in action_delays)
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 6. Challenge & Imperva detection
# ---------------------------------------------------------------------------
@requires_chrome
def test_challenge_and_imperva_detection(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 31,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        page = helper._page(task["id"], "github", "https://github.com/", task=task)
        assert helper.challenge_page(task) is None
        assert helper.challenge_page({"id": 999}) is None

        # Visible octocaptcha iframe -> detected.
        page.evaluate(
            """() => {
                const frame = document.createElement('iframe');
                frame.id = 'verification';
                frame.src = 'https://octocaptcha.com/challenge';
                frame.width = '300';
                frame.height = '200';
                document.body.appendChild(frame);
            }"""
        )
        assert helper.challenge_page(task) is page

        # Hidden challenge iframe -> not detected.
        page.locator("#verification").evaluate("el => el.style.display = 'none'")
        assert helper.challenge_page(task) is None

        # Imperva "访问暂时受限" page -> detected.
        page.goto("https://github.com/restricted")
        assert "访问暂时受限" in page.evaluate("() => document.body.innerText")
        assert helper.challenge_page(task) is page

        # Normal page -> not detected.
        page.goto("https://github.com/signup")
        assert helper.challenge_page(task) is None
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 7. Dual-browser round lifecycle & cleanup
# ---------------------------------------------------------------------------
@requires_chrome
def test_dual_browser_round_lifecycle_and_cleanup(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays, monkeypatch
):
    switched: list[tuple] = []
    monkeypatch.setattr(
        wb, "activate_proxy_node", lambda *args, **kwargs: switched.append(args)
    )
    helper = _new_helper(tmp_path, site)
    task_one = {
        "id": 1,
        "email": "one@example.org",
        "username": "oneuser1",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    task_two = {
        "id": 2,
        "email": "two@example.org",
        "username": "twouser2",
        "step": "github",
        "greeting": "hi",
        "round_slot": 1,
    }
    keep = tmp_path / "keep"
    keep.mkdir()
    try:
        site.github_status = 200
        _force_signup_channel(monkeypatch, header_first=False)
        helper.open(task_one, "firstPass111")
        _force_signup_channel(monkeypatch, header_first=True)
        helper.open(task_two, "secondPass222")

        assert len(helper.sessions) == 2
        context_one = helper.sessions[1]["context"]
        context_two = helper.sessions[2]["context"]
        proc_one = context_one._proc
        proc_two = context_two._proc
        assert proc_one.pid != proc_two.pid
        assert proc_one.poll() is None
        assert proc_two.poll() is None
        assert helper._tabs_for(1)["github"].locator("#email").input_value() == task_one["email"]
        assert helper._tabs_for(2)["github"].locator("#email").input_value() == task_two["email"]
        _assert_no_runtime_enable(helper)

        helper.close(discard_id=1)
        assert 1 not in helper.sessions
        assert 2 in helper.sessions
        assert _wait_until(lambda: proc_one.poll() is not None)
        assert proc_two.poll() is None
        assert not (helper.root / "1").exists()
        assert (helper.root / "2").is_dir()

        helper.close(discard_id=2)
        assert helper.sessions == {}
        assert _wait_until(lambda: proc_two.poll() is not None)
        assert not (helper.root / "2").exists()

        helper.purge_cache_and_switch_node(
            "http://127.0.0.1:17899", "🇯🇵 Japan | 01"
        )
        assert switched == [("WorkBuddy", "http://127.0.0.1:17899", "🇯🇵 Japan | 01")]
        assert not (helper.root / "1").exists()
        assert not (helper.root / "2").exists()
        assert keep.exists()
        assert proc_one.poll() is not None
        assert proc_two.poll() is not None
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 8. GitHub inline octocaptcha (github.com/octocaptcha) detection
# ---------------------------------------------------------------------------
@requires_chrome
def test_challenge_detects_github_inline_octocaptcha(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 41,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        page = helper._page(task["id"], "github", "https://github.com/signup", task=task)
        assert helper.challenge_page(task) is None

        # Hidden octocaptcha placeholder before "Create account" -> not flagged.
        page.evaluate(
            """() => {
                const parent = document.createElement('div');
                parent.className = 'js-octocaptcha-parent';
                parent.style.display = 'none';
                const frame = document.createElement('iframe');
                frame.className = 'js-octocaptcha-frame';
                frame.src = 'https://github.com/octocaptcha?origin=signup';
                parent.appendChild(frame);
                document.body.appendChild(parent);
            }"""
        )
        assert helper.challenge_page(task) is None

        # Once the frame becomes visible GitHub's inline challenge is flagged.
        page.evaluate(
            """() => {
                const parent = document.querySelector('.js-octocaptcha-parent');
                parent.style.display = 'block';
                const frame = parent.querySelector('iframe');
                frame.style.width = '320px';
                frame.style.height = '240px';
            }"""
        )
        assert helper.challenge_page(task) is page
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 9. submit_github_signup: fill -> blur -> Create account -> OTP page
# ---------------------------------------------------------------------------
@requires_chrome
def test_submit_github_signup_fills_form_and_reaches_email_step(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays, monkeypatch
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 51,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        site.github_signup_mode = "submit"
        page = helper._page(task["id"], "github", "https://github.com/signup", task=task)

        result = helper.submit_github_signup(task, "testPassword123")

        assert result["status"] == "next_step", result
        assert result["next"] == "email"
        assert result["username"] == "worker1234"
        assert "邮箱验证码" in result["message"]
        assert "/account_verifications" in page.url
        assert any(call == (1.0, 2.0) for call in action_delays)
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 10. submit_github_signup: taken username is regenerated before submit
# ---------------------------------------------------------------------------
@requires_chrome
def test_submit_github_signup_regenerates_taken_username(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 52,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        site.github_signup_mode = "taken"
        page = helper._page(task["id"], "github", "https://github.com/signup", task=task)

        result = helper.submit_github_signup(task, "testPassword123")

        assert result["status"] == "next_step", result
        assert result["next"] == "email"
        # The fixture page rejects worker1234; a random suffix must be generated.
        new_username = result["username"]
        assert new_username != "worker1234"
        assert new_username.startswith("worker")
        assert len(new_username) > len("worker")
        assert new_username.endswith(tuple("0123456789"))
        assert "/account_verifications" in page.url
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 11. submit_github_otp: fill code -> Verify -> WorkBuddy step
# ---------------------------------------------------------------------------
@requires_chrome
def test_submit_github_otp_verifies_code_and_advances(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 61,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "email",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        site.github_verify_mode = "ok"
        page = helper._page(
            task["id"], "github", "https://github.com/account_verifications", task=task
        )
        assert page.locator('input[name="launch_code[]"]').count() == 6

        result = helper.submit_github_otp(task, "123456")

        assert result["status"] == "next_step", result
        assert result["next"] == "workbuddy"
        assert result["filled"] is True
        assert "/dashboard" in page.url
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 12. complete_workbuddy_github_login: login -> authorize -> JWT captured
# ---------------------------------------------------------------------------
@requires_chrome
def test_complete_workbuddy_github_login_authorizes_and_captures_jwt(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 71,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "workbuddy",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        site.workbuddy_home_mode = "oauth"
        site.github_oauth_mode = "authorize"

        result = helper.complete_workbuddy_github_login(task)

        assert result["status"] == "next_step", result
        assert result["next"] == "region"
        assert any(
            r["host"] == "github.com" and r["path"] == "/login/oauth/authorize"
            for r in site.requests
        ), "GitHub authorize page was never opened"
        assert helper.captured_tokens.get(71, {}).get("accessToken")
        assert helper.captured_tokens[71]["accessToken"].count(".") == 2
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 13. complete_workbuddy_region: no profile entry -> not_required
# ---------------------------------------------------------------------------
@requires_chrome
def test_complete_workbuddy_region_not_required(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 81,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "region",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        site.workbuddy_home_mode = "region"

        result = helper.complete_workbuddy_region(task)

        assert result["status"] == "next_step", result
        assert result["next"] == "greeting"
        assert result["region"] == "not_required"
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 14. complete_workbuddy_region: profile dialog is confirmed
# ---------------------------------------------------------------------------
@requires_chrome
def test_complete_workbuddy_region_confirms_profile_dialog(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 82,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "region",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        site.workbuddy_home_mode = "profile"
        page = helper._page(task["id"], "workbuddy", "https://www.workbuddy.ai/", task=task)
        assert page.locator("#profile-modal").is_visible() is False

        result = helper.complete_workbuddy_region(task)

        assert result["status"] == "next_step", result
        assert result["region"] == "confirmed"
        assert result["next"] == "greeting"
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 15. send_greeting_and_wait_reply: sends on /app and sees the reply
# ---------------------------------------------------------------------------
@requires_chrome
def test_send_greeting_and_wait_reply_detects_assistant_reply(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 91,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "greeting",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        site.workbuddy_app_mode = "reply_on_send"

        result = helper.send_greeting_and_wait_reply(task)

        assert result["status"] == "replied", result
        assert result["sent"] is True
        page = helper._tabs_for(91)["workbuddy"]
        assert page.url == "https://www.workbuddy.ai/app"
        editor = page.locator(
            '[role="textbox"][contenteditable="true"][data-slate-editor="true"]'
        )
        assert editor.inner_text() == "hi"
        assert page.locator('[data-role="assistant"]').count() == 1
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 16. send_greeting_and_wait_reply: no reply still completes the wait
# ---------------------------------------------------------------------------
@requires_chrome
def test_send_greeting_and_wait_reply_times_out_gracefully(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 92,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "greeting",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        site.workbuddy_app_mode = "silent"

        result = helper.send_greeting_and_wait_reply(task)

        assert result["status"] == "replied", result
        assert result["sent"] is True
        assert "保存凭据" in result["message"]
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 17. Isolated-world caching: one Page.createIsolatedWorld per navigation
# ---------------------------------------------------------------------------
@requires_chrome
def test_eval_caches_isolated_world_per_frame_navigation(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 101,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        page = helper._page(task["id"], "github", "https://github.com/signup", task=task)

        def created_worlds() -> int:
            return page.sent_methods.count("Page.createIsolatedWorld")

        before = created_worlds()
        calls_before = len(page.sent_methods)
        # A burst of locator/evaluate calls must all reuse the cached context.
        for _ in range(5):
            assert page.evaluate("() => document.title") == "Join GitHub"
        assert page.locator("#email").count() == 1
        assert page.locator("#email").input_value() == ""
        assert page.evaluate("() => location.pathname") == "/signup"
        assert created_worlds() == before, (
            "isolated world was re-created on repeated evaluate calls: "
            f"{page.sent_methods.count('Page.createIsolatedWorld')}"
        )
        burst = page.sent_methods[calls_before:]
        assert "Page.getFrameTree" not in burst, (
            "cached evaluates must not fall back to Page.getFrameTree"
        )
        assert burst and set(burst) == {"Runtime.evaluate"}, burst

        # A real navigation invalidates the cached context exactly once.
        before_nav = created_worlds()
        page.goto("https://github.com/")
        assert page.evaluate("() => document.title") == "GitHub"
        assert page.evaluate("() => document.title") == "GitHub"
        assert created_worlds() == before_nav + 1, (
            "navigation should create exactly one new isolated world, got "
            f"{created_worlds() - before_nav}"
        )

        # A stale context (browser reports "Cannot find context") self-heals.
        page._isolated_world = ("stale-frame", 987654)
        assert page.evaluate("() => 1 + 1") == 2
        assert page._isolated_world is not None
        assert page._isolated_world[1] != 987654
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 18. Imperva IP restriction vs. interactive slider challenge
# ---------------------------------------------------------------------------
@requires_chrome
def test_is_ip_restricted_page_distinguishes_slider_from_dead_end(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 111,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        page = helper._page(task["id"], "github", "https://github.com/restricted", task=task)
        assert "访问暂时受限" in page.evaluate("() => document.body.innerText")
        assert "IP:103.142.140.241" in page.evaluate("() => document.body.innerText")
        assert helper.is_ip_restricted_page(task) is True

        # A visible interactive octocaptcha/slider means the operator can still
        # solve it: this is a challenge, NOT a dead-end IP block.
        page.evaluate(
            """() => {
                const frame = document.createElement('iframe');
                frame.id = 'verification';
                frame.src = 'https://octocaptcha.com/challenge';
                frame.width = '300';
                frame.height = '200';
                document.body.appendChild(frame);
            }"""
        )
        assert helper.is_ip_restricted_page(task) is False
        assert helper.challenge_page(task) is page

        # A pure slider widget (no iframe) is also an interactive challenge.
        page.evaluate(
            """() => {
                document.getElementById('verification').remove();
                const slider = document.createElement('div');
                slider.className = 'nc-container slider-verify';
                slider.style.width = '300px';
                slider.style.height = '50px';
                slider.textContent = '请拖动滑块';
                document.body.appendChild(slider);
            }"""
        )
        assert helper.is_ip_restricted_page(task) is False
        assert helper.challenge_page(task) is page

        # Plain restriction page again -> dead-end IP block.
        page.goto("https://github.com/restricted")
        assert helper.is_ip_restricted_page(task) is True

        # Normal signup page -> neither.
        page.goto("https://github.com/signup")
        assert helper.is_ip_restricted_page(task) is False
        assert helper.challenge_page(task) is None
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


@requires_chrome
def test_submit_github_signup_returns_ip_restricted_on_imperva_block(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 112,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        page = helper._page(task["id"], "github", "https://github.com/restricted", task=task)

        result = helper.submit_github_signup(task, "testPassword123")

        assert result["status"] == "ip_restricted", result
        assert result["username"] == "worker1234"
        assert "访问暂时受限" in result["message"]
        assert "切换新节点" in result["message"]
        # No form was touched on the dead-end page.
        assert page.evaluate("() => location.pathname") == "/restricted"
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


@requires_chrome
def test_is_ip_restricted_and_challenge_detection_ignore_occluded_visibility(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    """Native window occlusion (``visibilityState = 'hidden'``) must not hide a challenge.

    ``page.evaluate`` runs in the ``kv_assist`` isolated world, where
    ``window.dd`` is undefined, so DataDome's inline ``var dd = {...}`` script
    must be detected from the shared DOM's ``<script>`` tags instead.
    """
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 122,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        page = helper._page(task["id"], "github", "https://github.com/signup", task=task)

        # Emulate KeyVault's window covering Chrome on Windows.
        page.evaluate(
            """() => {
                Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true});
                Object.defineProperty(document, 'hidden', {value: true, configurable: true});
                return document.visibilityState;
            }"""
        )
        assert page.evaluate("() => document.visibilityState") == "hidden"
        assert helper.challenge_page(task) is None
        assert helper.is_ip_restricted_page(task) is False

        def _set_dd_script(body: str, src: str = "") -> None:
            page.evaluate(
                """([text, scriptSrc]) => {
                    document.querySelectorAll('script[data-test-dd]').forEach(s => s.remove());
                    const script = document.createElement('script');
                    script.setAttribute('data-test-dd', '1');
                    if (scriptSrc) script.src = scriptSrc;
                    if (text) script.textContent = text;
                    document.head.appendChild(script);
                }""",
                [body, src],
            )

        # Inline hard-block script while window.dd is undefined in kv_assist.
        _set_dd_script("var dd = {'rt':'c','t':'bv','host':'geo.captcha-delivery.com'};")
        assert page.evaluate("() => typeof window.dd") == "undefined"
        assert helper.is_ip_restricted_page(task) is True
        assert helper.challenge_page(task) is page

        # ``rt: 'c'`` without ``t: 'bv'`` is an interactive challenge.
        _set_dd_script("var dd = {'rt':'c','t':'fe','host':'geo.captcha-delivery.com'};")
        assert helper.is_ip_restricted_page(task) is False
        assert helper.challenge_page(task) is page

        # DataDome's silent device check (``rt: 'i'`` / ``i.js``) is not a
        # human challenge and must never be treated as a hard block.
        _set_dd_script("var dd = {'rt':'i','host':'geo.captcha-delivery.com'};")
        assert helper.challenge_page(task) is None
        assert helper.is_ip_restricted_page(task) is False

        _set_dd_script("", src="https://ct.captcha-delivery.com/i.js")
        assert helper.challenge_page(task) is None
        assert helper.is_ip_restricted_page(task) is False

        # ``c.js`` is the active challenge shell.
        _set_dd_script("", src="https://ct.captcha-delivery.com/c.js")
        assert helper.challenge_page(task) is page

        # A slider script alone is not a dead-end IP block.
        _set_dd_script("var dd = {'t':'fe','host':'geo.captcha-delivery.com'};")
        assert helper.is_ip_restricted_page(task) is False

        # Occlusion must never re-enter the probes.
        for method in (
            WorkBuddyBrowser.is_ip_restricted_page,
            WorkBuddyBrowser.challenge_page,
        ):
            source = inspect.getsource(method)
            assert "document.visibilityState !==" not in source, method.__name__
            assert "visibilityState !== 'visible'" not in source, method.__name__
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 19. Verified resume must not re-fill / clear #login (solved slider token)
# ---------------------------------------------------------------------------
@requires_chrome
def test_submit_github_signup_verified_resume_preserves_filled_fields(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays, monkeypatch
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 113,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
        "status": "verified",
    }
    try:
        site.github_status = 200
        site.github_signup_mode = "verified"
        page = helper._page(task["id"], "github", "https://github.com/signup", task=task)
        assert page.locator("#login").input_value() == "worker1234"

        refill_calls: list[int] = []
        real_fill = helper._fill_github_signup

        def _spy_fill(*args, **kwargs):
            refill_calls.append(1)
            return real_fill(*args, **kwargs)

        monkeypatch.setattr(helper, "_fill_github_signup", _spy_fill)

        result = helper.submit_github_signup(task, "testPassword123")

        assert result["status"] == "next_step", result
        assert result["next"] == "email"
        assert result["username"] == "worker1234"
        assert "/account_verifications" in page.url
        assert refill_calls == [], "_fill_github_signup ran on a verified resume"
        # The submit handler recorded #login's value at submit time: the resume
        # path must have left it byte-identical (no clear, no re-paste).
        submitted_login = page.evaluate("() => sessionStorage.getItem('login_at_submit')")
        input_events = page.evaluate("() => sessionStorage.getItem('login_input_events')")
        assert submitted_login == "worker1234"
        assert input_events == "0", (
            "resume re-dispatched input events on #login, invalidating the "
            f"solved CAPTCHA token (events={input_events})"
        )
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


@requires_chrome
def test_submit_github_signup_verified_resume_keeps_operator_edited_username(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    """A username the operator typed before solving the slider must survive."""
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 114,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
        "status": "verified",
    }
    try:
        site.github_status = 200
        site.github_signup_mode = "verified"
        page = helper._page(task["id"], "github", "https://github.com/signup", task=task)
        # Operator edited the username field while solving the slider.
        page.locator("#login").fill("operator-edit99")
        assert page.locator("#login").input_value() == "operator-edit99"
        baseline_events = page.evaluate("() => sessionStorage.getItem('login_input_events')")

        result = helper.submit_github_signup(task, "testPassword123")

        assert result["status"] == "next_step", result
        assert page.evaluate("() => sessionStorage.getItem('login_at_submit')") == "operator-edit99"
        assert page.evaluate(
            "() => sessionStorage.getItem('login_input_events')"
        ) == baseline_events, "resume re-dispatched input events on #login"
    finally:
        helper.close()


@requires_chrome
def test_submit_github_signup_verified_resume_refills_only_cleared_password(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays, monkeypatch
):
    """DataDome's ``history.back()`` restores #email/#login but clears #password.

    Only ``#password`` may be filled again: touching ``#login`` would
    invalidate the solved token and restart the username Tab/loop.
    """
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 115,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
        "status": "verified",
    }
    try:
        site.github_status = 200
        site.github_signup_mode = "verified_cleared_password"
        page = helper._page(task["id"], "github", "https://github.com/signup", task=task)
        assert page.locator("#email").input_value() == "worker@example.org"
        assert page.locator("#login").input_value() == "worker1234"
        assert page.locator("#password").input_value() == ""

        pasted: list[str] = []
        real_paste = helper._human_paste_input

        def _spy_paste(page_arg, locator, value, **kwargs):
            pasted.append(str(value))
            return real_paste(page_arg, locator, value, **kwargs)

        monkeypatch.setattr(helper, "_human_paste_input", _spy_paste)

        refill_calls: list[int] = []
        real_fill = helper._fill_github_signup

        def _spy_fill(*args, **kwargs):
            refill_calls.append(1)
            return real_fill(*args, **kwargs)

        monkeypatch.setattr(helper, "_fill_github_signup", _spy_fill)

        result = helper.submit_github_signup(task, "testPassword123")

        assert result["status"] == "next_step", result
        assert result["next"] == "email"
        assert result["username"] == "worker1234"
        assert pasted == ["testPassword123"], pasted
        assert refill_calls == [], "_fill_github_signup ran on a verified resume"
        assert page.evaluate("() => sessionStorage.getItem('login_at_submit')") == "worker1234"
        assert page.evaluate("() => sessionStorage.getItem('login_input_events')") == "0", (
            "resume re-dispatched input events on #login"
        )
        assert page.evaluate("() => sessionStorage.getItem('password_at_submit')") == "testPassword123"
        assert page.evaluate("() => sessionStorage.getItem('password_input_events')") == "1"
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 20. DataDome false-positive challenge detection
# ---------------------------------------------------------------------------
@requires_chrome
def test_challenge_page_ignores_datadome_interstitial_and_suppressed_octocaptcha(
    tmp_path: Path, site: LocalHttpsSite, launched_processes, action_delays
):
    helper = _new_helper(tmp_path, site)
    task = {
        "id": 121,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    try:
        site.github_status = 200
        page = helper._page(task["id"], "github", "https://github.com/signup", task=task)

        # GitHub's /signup carries static "Verify your account" section text.
        page.evaluate(
            """() => {
                const heading = document.createElement('h2');
                heading.textContent = 'Verify your account';
                document.body.appendChild(heading);
            }"""
        )
        assert helper.challenge_page(task) is None

        # Suppressed DataDome placeholder frame is not a human challenge.
        _add_frame(
            page,
            "octo-suppressed",
            "https://octocaptcha.com/datadome?origin_page=github_signup_redesign",
            title="Account verification.",
            class_name="js-octocaptcha-frame width-full d-block v-hidden border-0 rounded-2",
        )
        assert helper.challenge_page(task) is None

        # DataDome's silent automatic device check interstitial.
        _add_frame(
            page,
            "dd-interstitial",
            "https://geo.captcha-delivery.com/interstitial/?initialCid=123",
            title="DataDome Device Check",
        )
        assert helper.challenge_page(task) is None

        # A real visible DataDome captcha frame is still a human challenge.
        _add_frame(
            page,
            "dd-captcha",
            "https://geo.captcha-delivery.com/captcha/?initialCid=123&t=fe",
            title="DataDome CAPTCHA",
        )
        assert helper.challenge_page(task) is page
        assert helper.is_ip_restricted_page(task) is False

        # ``t=bv`` means DataDome already decided this IP is a bot: dead end.
        _add_frame(
            page,
            "dd-captcha",
            "https://geo.captcha-delivery.com/captcha/?initialCid=123&t=bv",
            title="DataDome CAPTCHA",
        )
        assert helper.is_ip_restricted_page(task) is True
        _assert_no_runtime_enable(helper)
    finally:
        helper.close()


# ---------------------------------------------------------------------------
# 21. Human typing fidelity: character-by-character key events
# ---------------------------------------------------------------------------
def test_keyboard_insert_text_emits_per_character_key_events(monkeypatch):
    sent: list[tuple[str, dict]] = []

    class _FakePage:
        def send(self, method, params=None):
            sent.append((method, dict(params or {})))
            return {}

    sleeps: list[float] = []

    class _TimeShim:
        def __getattr__(self, item):
            return getattr(time, item)

        def sleep(self, value):
            sleeps.append(value)

    monkeypatch.setattr(wb, "ACTION_DELAY", 1)
    monkeypatch.setattr(wb, "time", _TimeShim())
    monkeypatch.setattr(wb, "secrets", types.SimpleNamespace(randbelow=lambda n: 0, choice=lambda s: s[0]))

    keyboard = wb._CdpKeyboard(_FakePage())
    keyboard.insert_text("ab")

    assert [m for m, _ in sent] == [
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
    ]
    assert sent[0][1]["type"] == "keyDown" and sent[0][1]["text"] == "a"
    assert sent[0][1]["code"] == "KeyA"
    assert sent[0][1]["windowsVirtualKeyCode"] == 65
    assert sent[0][1]["nativeVirtualKeyCode"] == 65
    assert sent[1][1]["type"] == "keyUp" and sent[1][1]["key"] == "a"
    assert sent[2][1]["type"] == "keyDown" and sent[2][1]["text"] == "b"
    assert sent[2][1]["code"] == "KeyB"
    assert sent[2][1]["windowsVirtualKeyCode"] == 66
    assert sent[2][1]["nativeVirtualKeyCode"] == 66
    assert sent[3][1]["type"] == "keyUp" and sent[3][1]["key"] == "b"
    assert sleeps == [0.012, 0.032, 0.012, 0.032]

    # Shifted characters must carry the physical key identity and Shift
    # modifier, otherwise DataDome records ``keyCode === 0`` telemetry. A real
    # keyboard also emits a genuine Shift rawKeyDown/keyUp pair around them.
    sent.clear()
    sleeps.clear()
    keyboard.insert_text("A@")
    assert [m for m, _ in sent] == ["Input.dispatchKeyEvent"] * 8
    # -- uppercase A -----------------------------------------------------
    assert sent[0][1]["type"] == "rawKeyDown"
    assert sent[0][1]["key"] == "Shift"
    assert sent[0][1]["code"] == "ShiftLeft"
    assert sent[0][1]["windowsVirtualKeyCode"] == 16
    assert sent[0][1]["nativeVirtualKeyCode"] == 16
    assert sent[0][1]["modifiers"] == 8
    assert sent[0][1]["location"] == 1
    assert sent[1][1]["type"] == "keyDown" and sent[1][1]["text"] == "A"
    assert sent[1][1]["code"] == "KeyA"
    assert sent[1][1]["windowsVirtualKeyCode"] == 65
    assert sent[1][1]["nativeVirtualKeyCode"] == 65
    assert sent[1][1]["modifiers"] == 8
    assert sent[1][1]["unmodifiedText"] == "a"
    assert sent[2][1]["type"] == "keyUp" and sent[2][1]["key"] == "A"
    assert sent[2][1]["modifiers"] == 8
    assert sent[3][1]["type"] == "keyUp" and sent[3][1]["key"] == "Shift"
    assert sent[3][1]["code"] == "ShiftLeft"
    assert sent[3][1]["windowsVirtualKeyCode"] == 16
    assert sent[3][1]["nativeVirtualKeyCode"] == 16
    assert sent[3][1]["modifiers"] == 0
    # -- shifted @ -------------------------------------------------------
    assert sent[4][1]["type"] == "rawKeyDown" and sent[4][1]["key"] == "Shift"
    assert sent[4][1]["modifiers"] == 8
    assert sent[5][1]["type"] == "keyDown" and sent[5][1]["text"] == "@"
    assert sent[5][1]["code"] == "Digit2"
    assert sent[5][1]["windowsVirtualKeyCode"] == 50
    assert sent[5][1]["nativeVirtualKeyCode"] == 50
    assert sent[5][1]["modifiers"] == 8
    assert sent[6][1]["type"] == "keyUp" and sent[6][1]["key"] == "@"
    assert sent[6][1]["modifiers"] == 8
    assert sent[7][1]["type"] == "keyUp" and sent[7][1]["key"] == "Shift"
    assert sent[7][1]["modifiers"] == 0
    assert sleeps == [
        0.008, 0.012, 0.008, 0.032,  # Shift down, A down, A up, Shift up
        0.008, 0.012, 0.008, 0.032,  # Shift down, @ down, @ up, Shift up
    ]

    # Unshifted characters never emit a Shift key event.
    sent.clear()
    keyboard.insert_text("z")
    assert [params["key"] for _, params in sent] == ["z", "z"]

    # Zero delay keeps the single instantaneous Input.insertText for tests.
    sent.clear()
    sleeps.clear()
    monkeypatch.setattr(wb, "ACTION_DELAY", 0)
    keyboard.insert_text("ab")
    assert sent == [("Input.insertText", {"text": "ab"})]
    assert sleeps == []


def test_keyboard_press_tab_uses_native_cdp_tab_key(monkeypatch):
    sent: list[tuple[str, dict]] = []

    class _FakePage:
        def send(self, method, params=None):
            sent.append((method, dict(params or {})))
            return {}

    sleeps: list[float] = []

    class _TimeShim:
        def __getattr__(self, item):
            return getattr(time, item)

        def sleep(self, value):
            sleeps.append(value)

    monkeypatch.setattr(wb, "ACTION_DELAY", 1)
    monkeypatch.setattr(wb, "time", _TimeShim())
    wb._CdpKeyboard(_FakePage()).press_tab()

    assert [params["type"] for _, params in sent] == ["keyDown", "keyUp"]
    for _, params in sent:
        assert params["key"] == "Tab"
        assert params["code"] == "Tab"
        assert params["windowsVirtualKeyCode"] == 9
        assert params["nativeVirtualKeyCode"] == 9
    assert sleeps == [0.025]


# ---------------------------------------------------------------------------
# 22. DataDome OOPIF discovery via Target.getTargets (Chrome 118+)
# ---------------------------------------------------------------------------
class _FakeTargetPage:
    """Minimal page target exposing the CDP surface used by the probes."""

    def __init__(
        self,
        target_infos: list[dict] | None = None,
        evaluate_result: str = "check_oopif",
        url: str = "https://github.com/signup",
        transitioning: bool = False,
    ):
        self.target_infos = list(target_infos or [])
        self.evaluate_result = evaluate_result
        self.url = url
        self.transitioning = transitioning
        self.sent: list[tuple[str, dict]] = []
        self.closed = False

    def send(self, method, params=None):
        self.sent.append((method, dict(params or {})))
        if method == "Target.getTargets":
            return {"targetInfos": list(self.target_infos)}
        raise AssertionError(f"unexpected CDP method on page: {method}")

    def evaluate(self, expression):
        # ``check_paused_challenge_state`` first asks whether the GitHub tab is
        # mid-reload / running DataDome's silent interstitial; every other probe
        # is classified by ``evaluate_result``.
        if "document.readyState === 'loading'" in expression:
            return self.transitioning
        return self.evaluate_result

    def is_closed(self):
        return self.closed


class _FakeCtx:
    """Context whose ``/json`` listing omits cross-origin iframe targets."""

    debug_port = 9333

    def __init__(self, pages: list, targets: list[dict]):
        self.pages = list(pages)
        self.targets = list(targets)
        self.fetch_calls: list[bool] = []

    def _fetch_targets(self, force: bool = False):
        self.fetch_calls.append(bool(force))
        return list(self.targets)


class _FakeCdpContext(wb._CdpContext):
    """``_CdpContext``-typed fake so ``allow_active_fe_skip`` actually engages."""

    def __init__(self, pages: list, targets: list[dict] | None = None):
        self._pages = list(pages)
        self._targets = list(targets or [])
        self.debug_port = 9333
        self.fetch_calls: list[bool] = []

    @property
    def pages(self) -> list:
        return list(self._pages)

    def _fetch_targets(self, force: bool = False):
        self.fetch_calls.append(bool(force))
        return list(self._targets)


class _FakeDomClient:
    """CDP client answering only the zero-JS Blink DOM inspection methods."""

    instances: list["_FakeDomClient"] = []

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.methods: list[str] = []
        self.closed = False
        _FakeDomClient.instances.append(self)

    def send(self, method, params=None):
        self.methods.append(method)
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.getOuterHTML":
            return {"outerHTML": "<html><body><h1>访问暂时受限</h1></body></html>"}
        raise AssertionError(f"unexpected CDP method: {method}")

    def close(self):
        self.closed = True


def _dom_client_returning(html: str):
    """Build a DOM client whose ``DOM.getOuterHTML`` returns ``html``."""

    class _HtmlDomClient(_FakeDomClient):
        def send(self, method, params=None):
            if method == "DOM.getOuterHTML":
                self.methods.append(method)
                return {"outerHTML": html}
            return super().send(method, params)

    return _HtmlDomClient


class _FakeJsClient:
    """CDP client without the DOM domain: forces the isolated-world fallback."""

    instances: list["_FakeJsClient"] = []

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.methods: list[str] = []
        self.closed = False
        _FakeJsClient.instances.append(self)

    def send(self, method, params=None):
        self.methods.append(method)
        if method == "DOM.getDocument":
            raise RuntimeError("DOM domain unavailable")
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "FRAME-1"}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 42}
        if method == "Runtime.evaluate":
            return {"result": {"value": "slider"}}
        raise AssertionError(f"unexpected CDP method: {method}")

    def close(self):
        self.closed = True


def test_datadome_oopif_discovered_via_target_get_targets(tmp_path: Path, monkeypatch):
    """Chrome 118+ hides OOPIFs from ``/json``; ``Target.getTargets`` finds them."""
    iframe_target = {
        "type": "iframe",
        "targetId": "IFRAME-TARGET-1",
        "url": "https://geo.captcha-delivery.com/captcha/?initialCid=abc&t=fe",
    }
    top_page_target = {
        "type": "page",
        "id": "PAGE-TARGET-1",
        "url": "https://github.com/signup",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/PAGE-TARGET-1",
    }
    page = _FakeTargetPage([iframe_target])
    ctx = _FakeCtx([page], [top_page_target])
    assert [t.get("type") for t in ctx.targets] == ["page"], (
        "the /json listing must contain no iframe target in this scenario"
    )

    _FakeDomClient.instances.clear()
    monkeypatch.setattr(wb, "_CdpClient", _FakeDomClient)

    helper = WorkBuddyBrowser(tmp_path / "sessions")
    task = {
        "id": 201,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    helper.sessions[task["id"]] = {"context": ctx}

    assert helper._inspect_datadome_oopif(ctx) == "restricted"
    assert ctx.fetch_calls == [True], "the /json listing must still be consulted"
    assert page.sent[0] == (
        "Target.getTargets",
        {"filter": [{"type": "iframe"}, {"type": "page"}]},
    )
    assert len(_FakeDomClient.instances) == 1
    client = _FakeDomClient.instances[0]
    # No webSocketDebuggerUrl: it must be derived from the debug port + targetId.
    assert client.ws_url == "ws://127.0.0.1:9333/devtools/page/IFRAME-TARGET-1"
    assert client.methods == ["DOM.getDocument", "DOM.getOuterHTML"]
    assert client.closed is True, "the short-lived CDP connection must be closed"

    assert helper.is_ip_restricted_page(task) is True


def test_datadome_oopif_falls_back_to_isolated_world_probe(tmp_path: Path, monkeypatch):
    """When DOM inspection is unavailable the isolated-world JS probe still runs."""
    iframe_target = {
        "type": "iframe",
        "targetId": "IFRAME-TARGET-2",
        "url": "https://geo.captcha-delivery.com/captcha/?initialCid=xyz&t=fe",
    }
    page = _FakeTargetPage([iframe_target])
    ctx = _FakeCtx([page], [])

    _FakeJsClient.instances = []
    monkeypatch.setattr(wb, "_CdpClient", _FakeJsClient)

    helper = WorkBuddyBrowser(tmp_path / "sessions")
    assert helper._inspect_datadome_oopif(ctx) == "slider"
    assert _FakeJsClient.instances[0].methods == [
        "DOM.getDocument",
        "Page.getFrameTree",
        "Page.createIsolatedWorld",
        "Runtime.evaluate",
    ]
    assert _FakeJsClient.instances[0].closed is True


def test_datadome_oopif_slider_footer_is_not_a_dead_end_ip_block(
    tmp_path: Path, monkeypatch
):
    """The standard DataDome footer appears on the slider page too.

    Its ``与网路机器人相同`` sentence must never classify an interactive slider
    as a dead-end IP block, or the operator never gets to drag the slider.
    """
    task = {
        "id": 203,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }

    def _ctx_for() -> _FakeCtx:
        iframe_target = {
            "type": "iframe",
            "targetId": "IFRAME-FOOTER-1",
            "url": "https://geo.captcha-delivery.com/captcha/?initialCid=abc&t=fe",
        }
        return _FakeCtx([_FakeTargetPage([iframe_target])], [])

    slider_html = (
        "<html>"
        "<head><title>你被封锁了。 | Github.com</title></head>"
        "<body>"
        "<h2>向右滑动以保护您的访问</h2>"
        '<div class="slider-container"><div class="slider"></div></div>'
        "<p>为什么需要经过「我不是机器人」的验证？在您浏览网页过程中，"
        "可能有以下几种原因启动此验证程式：</p>"
        "<p>您使用的IP位址(IP:212.107.30.68)与网路机器人相同。</p>"
        "</body></html>"
    )
    monkeypatch.setattr(wb, "_CdpClient", _dom_client_returning(slider_html))
    helper = WorkBuddyBrowser(tmp_path / "sessions")
    slider_ctx = _ctx_for()
    helper.sessions[task["id"]] = {"context": slider_ctx}

    assert helper._inspect_datadome_oopif(slider_ctx) == "slider"
    assert helper.is_ip_restricted_page(task) is False

    restricted_html = (
        "<html><body>"
        "<h1>访问暂时受限</h1>"
        "<p>为什么需要经过「我不是机器人」的验证？</p>"
        "<p>您使用的IP位址(IP:1.2.3.4)与网路机器人相同。</p>"
        "</body></html>"
    )
    monkeypatch.setattr(wb, "_CdpClient", _dom_client_returning(restricted_html))
    restricted_ctx = _ctx_for()
    helper.sessions[task["id"]] = {"context": restricted_ctx}

    assert helper._inspect_datadome_oopif(restricted_ctx) == "restricted"
    assert helper.is_ip_restricted_page(task) is True

    # The footer sentence must not creep back into any restricted regex,
    # including the isolated-world fallback that Step A short-circuits.
    assert "与网路机器人相同" not in inspect.getsource(
        WorkBuddyBrowser._inspect_datadome_oopif
    )
    assert "与网路机器人相同" not in inspect.getsource(
        WorkBuddyBrowser.is_ip_restricted_page
    )


def test_datadome_oopif_never_runs_isolated_world_after_successful_dom_read(
    tmp_path: Path, monkeypatch
):
    """A successful ``DOM.getDocument`` must never be followed by Step B.

    The OOPIF can still be an empty/loading ``<html><body></body></html>`` when
    inspected. Falling through to ``Page.createIsolatedWorld`` would create a
    V8 context inside the live DataDome iframe and taint the solved token.
    """
    iframe_target = {
        "type": "iframe",
        "targetId": "IFRAME-EMPTY-1",
        "url": "https://geo.captcha-delivery.com/captcha/?initialCid=abc&t=fe",
    }
    ctx = _FakeCtx([_FakeTargetPage([iframe_target])], [])

    loading_client = _dom_client_returning("<html><body></body></html>")
    monkeypatch.setattr(wb, "_CdpClient", loading_client)
    helper = WorkBuddyBrowser(tmp_path / "sessions")

    assert helper._inspect_datadome_oopif(ctx, allow_active_fe_skip=True) == ""
    client = loading_client.instances[0]
    assert client.methods == ["DOM.getDocument", "DOM.getOuterHTML"], client.methods
    assert "Page.createIsolatedWorld" not in client.methods
    assert "Runtime.evaluate" not in client.methods
    assert client.closed is True

    # Without ``allow_active_fe_skip`` the same loading document is still
    # inspected, and Step B is still skipped.
    loading_client.instances.clear()
    assert helper._inspect_datadome_oopif(ctx) == ""
    assert loading_client.instances[0].methods == ["DOM.getDocument", "DOM.getOuterHTML"]


def test_datadome_oopif_detects_hard_block_attribute_as_restricted(
    tmp_path: Path, monkeypatch
):
    """``data-dd-response-page="hard-block"`` marks a dead-end IP block."""
    iframe_target = {
        "type": "iframe",
        "targetId": "IFRAME-HARDBLOCK-1",
        "url": "https://geo.captcha-delivery.com/captcha/?initialCid=abc&t=fe&refererHeader=xyz",
    }
    ctx = _FakeCtx([_FakeTargetPage([iframe_target])], [])
    html = (
        '<html><body data-dd-response-page="hard-block">'
        "<p>Something went wrong.</p></body></html>"
    )
    client_cls = _dom_client_returning(html)
    monkeypatch.setattr(wb, "_CdpClient", client_cls)
    helper = WorkBuddyBrowser(tmp_path / "sessions")

    assert helper._inspect_datadome_oopif(ctx) == "restricted"
    assert client_cls.instances[0].methods == ["DOM.getDocument", "DOM.getOuterHTML"]

    # The CSS-class variant is equivalent.
    class_html = (
        '<html><body class="dd-response-page--hard-block">'
        "<p>Something went wrong.</p></body></html>"
    )
    class_client = _dom_client_returning(class_html)
    monkeypatch.setattr(wb, "_CdpClient", class_client)
    assert helper._inspect_datadome_oopif(ctx) == "restricted"


def test_datadome_oopif_active_fe_skip_avoids_touching_live_slider(
    tmp_path: Path, monkeypatch
):
    """A fresh ``t=fe`` OOPIF is the live slider: never open a CDP connection.

    ``is_ip_restricted_page`` passes ``allow_active_fe_skip=True`` while the
    operator drags the slider; any DevTools attach would taint ``plv3``.
    """
    fresh_slider = {
        "type": "iframe",
        "targetId": "IFRAME-LIVE-FE",
        "url": "https://geo.captcha-delivery.com/captcha/?initialCid=abc&t=fe",
    }
    reopened_slider = {
        "type": "iframe",
        "targetId": "IFRAME-REOPENED-FE",
        "url": "https://geo.captcha-delivery.com/captcha/?initialCid=abc&t=fe&refererHeader=https%3A%2F%2Fgithub.com",
    }

    def _explode(ws_url):  # pragma: no cover - must never run for the fresh FE
        raise AssertionError(f"opened a CDP connection to the live slider: {ws_url}")

    monkeypatch.setattr(wb, "_CdpClient", _explode)
    helper = WorkBuddyBrowser(tmp_path / "sessions")

    live_ctx = _FakeCdpContext([_FakeTargetPage([fresh_slider])])
    assert helper._inspect_datadome_oopif(live_ctx, allow_active_fe_skip=True) == "slider"

    # ``t=bv`` is always a dead end, even with the active-slider skip enabled.
    blocked = {
        "type": "iframe",
        "targetId": "IFRAME-BV",
        "url": "https://geo.captcha-delivery.com/captcha/?initialCid=abc&t=bv",
    }
    bv_ctx = _FakeCdpContext([_FakeTargetPage([blocked])])
    assert helper._inspect_datadome_oopif(bv_ctx, allow_active_fe_skip=True) == "restricted"

    # A ``refererHeader=`` re-opened FE iframe is read through Step A again.
    reopened_client = _dom_client_returning(
        '<html><body data-dd-response-page="hard-block"></body></html>'
    )
    monkeypatch.setattr(wb, "_CdpClient", reopened_client)
    reopened_ctx = _FakeCdpContext([_FakeTargetPage([reopened_slider])])
    assert helper._inspect_datadome_oopif(reopened_ctx, allow_active_fe_skip=True) == "restricted"
    assert reopened_client.instances[0].methods == ["DOM.getDocument", "DOM.getOuterHTML"]

    # The skip is opt-in: without it, the same fresh FE iframe is inspected.
    opt_in_client = _dom_client_returning("<html><body></body></html>")
    monkeypatch.setattr(wb, "_CdpClient", opt_in_client)
    assert helper._inspect_datadome_oopif(live_ctx) == ""
    assert opt_in_client.instances[0].methods == ["DOM.getDocument", "DOM.getOuterHTML"]


def test_is_ip_restricted_page_uses_active_fe_skip(tmp_path: Path, monkeypatch):
    """The restriction probe must pass ``allow_active_fe_skip=True``."""
    calls: list[bool] = []
    helper = WorkBuddyBrowser(tmp_path / "sessions")
    task = {"id": 205, "step": "github"}
    page = _FakeTargetPage(evaluate_result="check_oopif")
    helper.sessions[task["id"]] = {"context": _FakeCdpContext([page])}

    def _spy(ctx, *, allow_active_fe_skip: bool = False) -> str:
        calls.append(allow_active_fe_skip)
        return "slider"

    monkeypatch.setattr(helper, "_inspect_datadome_oopif", _spy)
    assert helper.is_ip_restricted_page(task) is False
    assert calls == [True]


def test_wait_for_datadome_interstitial_detects_i_js_script(monkeypatch):
    """``i.js`` / ``var dd={'rt':'i'}`` is detected before the iframe exists."""
    probes: list[object] = []

    class _FakePage:
        def __init__(self):
            self.load_state_calls: list[tuple[str, int]] = []
            self.responses = [True, False]

        def evaluate(self, expression):
            probes.append(expression)
            return self.responses.pop(0) if self.responses else False

        def wait_for_load_state(self, state, timeout=0):
            self.load_state_calls.append((state, timeout))

    sleeps: list[float] = []

    class _TimeShim:
        def __getattr__(self, item):
            return getattr(time, item)

        def sleep(self, value):
            sleeps.append(value)

    monkeypatch.setattr(wb, "time", _TimeShim())
    page = _FakePage()
    helper = WorkBuddyBrowser(Path("."))
    helper._is_real_browser = True

    helper._wait_for_datadome_interstitial(page)

    assert probes, "the interstitial probe must run"
    assert "captcha-delivery\\.com\\/i\\.js" in str(probes[0])
    assert "rt" in str(probes[0])
    assert sleeps == [0.6, 0.65]
    assert page.load_state_calls == [("domcontentloaded", 5000)]


def test_wait_for_datadome_interstitial_returns_fast_when_absent(monkeypatch):
    """No interstitial -> no extra sleeps at all."""
    sleeps: list[float] = []

    class _FakePage:
        def evaluate(self, expression):
            return False

        def wait_for_load_state(self, state, timeout=0):  # pragma: no cover
            raise AssertionError("must not wait for a load state without an interstitial")

    class _TimeShim:
        def __getattr__(self, item):
            return getattr(time, item)

        def sleep(self, value):
            sleeps.append(value)

    monkeypatch.setattr(wb, "time", _TimeShim())
    helper = WorkBuddyBrowser(Path("."))
    helper._is_real_browser = True

    helper._wait_for_datadome_interstitial(_FakePage())
    assert sleeps == []


def test_check_paused_challenge_state_waits_out_datadome_interstitial(
    tmp_path: Path, monkeypatch
):
    """A GitHub tab mid device-check must not be reported as solved."""
    helper = WorkBuddyBrowser(tmp_path / "sessions")
    task = {
        "id": 204,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }
    page = _FakeTargetPage(
        evaluate_result="signup",
        url="https://github.com/signup",
        transitioning=True,
    )
    helper.sessions[task["id"]] = {"context": _FakeCtx([page], [])}
    monkeypatch.setattr(helper, "is_ip_restricted_page", lambda _task: False)
    monkeypatch.setattr(helper, "challenge_page", lambda _task: None)

    assert helper.check_paused_challenge_state(task) == "still_challenged"


def test_check_paused_challenge_state_treats_closed_window_as_still_challenged(
    tmp_path: Path, monkeypatch
):
    """Closing the Chrome window while paused must never report the task solved."""
    helper = WorkBuddyBrowser(tmp_path / "sessions")
    task = {
        "id": 206,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }

    # A real ``_CdpContext`` whose process is still alive but has no open pages
    # (the operator closed the window).
    helper.sessions[task["id"]] = {"context": _FakeCdpContext([], [])}
    monkeypatch.setattr(helper, "is_ip_restricted_page", lambda _task: False)
    monkeypatch.setattr(helper, "challenge_page", lambda _task: None)
    assert helper.check_paused_challenge_state(task) == "still_challenged"

    # The Chrome process exited entirely.
    dead_proc = types.SimpleNamespace(poll=lambda: 1)
    dead_ctx = _FakeCdpContext([], [])
    dead_ctx._proc = dead_proc
    helper.sessions[task["id"]] = {"context": dead_ctx}
    assert helper.check_paused_challenge_state(task) == "still_challenged"

    # A live ``_CdpContext`` with an open page keeps the normal behaviour.
    live_ctx = _FakeCdpContext([_FakeTargetPage(evaluate_result="signup")], [])
    helper.sessions[task["id"]] = {"context": live_ctx}
    assert helper.check_paused_challenge_state(task) == "solved"


def test_is_ip_restricted_page_disables_active_fe_skip_for_verified_task(
    tmp_path: Path, monkeypatch
):
    """A resume-time probe must inspect the OOPIF DOM for a transitioned frame."""
    calls: list[bool] = []
    helper = WorkBuddyBrowser(tmp_path / "sessions")
    task = {"id": 207, "step": "github", "status": "verified"}
    page = _FakeTargetPage(evaluate_result="check_oopif")
    helper.sessions[task["id"]] = {"context": _FakeCdpContext([page])}

    def _spy(ctx, *, allow_active_fe_skip: bool = False) -> str:
        calls.append(allow_active_fe_skip)
        return "slider"

    monkeypatch.setattr(helper, "_inspect_datadome_oopif", _spy)
    assert helper.is_ip_restricted_page(task) is False
    assert calls == [False]

    # An explicit override always wins.
    calls.clear()
    assert helper.is_ip_restricted_page(task, allow_active_fe_skip=True) is False
    assert calls == [True]


def test_check_paused_challenge_state_reports_ip_restricted_solved_and_pending(
    tmp_path: Path, monkeypatch
):
    helper = WorkBuddyBrowser(tmp_path / "sessions")
    task = {
        "id": 202,
        "email": "worker@example.org",
        "username": "worker1234",
        "step": "github",
        "greeting": "hi",
        "round_slot": 0,
    }

    # A dead-end IP block wins over any still-visible challenge widget.
    helper.sessions[task["id"]] = {"context": _FakeCtx([], [])}
    monkeypatch.setattr(helper, "is_ip_restricted_page", lambda _task: True)
    monkeypatch.setattr(helper, "challenge_page", lambda _task: object())
    assert helper.check_paused_challenge_state(task) == "ip_restricted"

    # No live context at all: the operator still has to act.
    helper.sessions.pop(task["id"])
    monkeypatch.setattr(helper, "is_ip_restricted_page", lambda _task: False)
    assert helper.check_paused_challenge_state(task) == "still_challenged"

    # The challenge disappeared -> solved.
    helper.sessions[task["id"]] = {"context": _FakeCtx([], [])}
    monkeypatch.setattr(helper, "challenge_page", lambda _task: None)
    assert helper.check_paused_challenge_state(task) == "solved"

    # The GitHub tab advanced to OTP -> solved even if a challenge page remains.
    otp_page = _FakeTargetPage(
        evaluate_result="otp",
        url="https://github.com/account_verifications?query=worker%40example.org",
    )
    helper.sessions[task["id"]] = {"context": _FakeCtx([otp_page], [])}
    monkeypatch.setattr(helper, "challenge_page", lambda _task: object())
    assert helper.check_paused_challenge_state(task) == "solved"

    # A slider still waiting for the operator -> still challenged.
    signup_page = _FakeTargetPage(
        evaluate_result="signup",
        url="https://github.com/signup",
    )
    helper.sessions[task["id"]] = {"context": _FakeCtx([signup_page], [])}
    assert helper.check_paused_challenge_state(task) == "still_challenged"
