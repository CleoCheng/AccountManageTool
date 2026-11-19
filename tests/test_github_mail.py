from email.message import EmailMessage
from email.utils import formatdate
from unittest.mock import MagicMock

import pytest

from src.github_mail import _message_code, fetch_latest_mail_code, read_github_code


NOW = 1793851200


def message(*, sender="noreply@github.com", recipient="worker@example.org", date=NOW, html=False):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(date, usegmt=True)
    msg["Subject"] = "Your GitHub launch code"
    msg.set_content("Your code is 12345678")
    if html:
        msg.clear_content()
        msg.set_content('<style>.x{color:#123456}</style><p>Your code is <b>12345678</b></p>', subtype="html")
    return msg


@pytest.mark.parametrize("html", [False, True])
def test_verification_message_supports_plain_and_html(html):
    assert _message_code(message(html=html).as_bytes(), "worker@example.org", NOW - 60, NOW) == "12345678"


@pytest.mark.parametrize("overrides", [
    {"sender": "noreply@github.com.evil.example"},
    {"recipient": "another@example.org"},
    {"date": NOW - 1800},
    {"date": NOW + 120},
])
def test_unrelated_or_expired_message_never_returns_code(overrides):
    assert _message_code(message(**overrides).as_bytes(), "worker@example.org", NOW - 60, NOW) is None


def test_ambiguous_message_does_not_guess_a_code():
    msg = message()
    msg.set_content("Try 12345678 or 87654321")
    with pytest.raises(ValueError, match="唯一"):
        _message_code(msg.as_bytes(), "worker@example.org", NOW - 60, NOW)


def test_reader_uses_tls_readonly_peek_and_current_mailbox(monkeypatch):
    monkeypatch.setattr("src.github_mail.time.time", lambda: NOW)
    inbox = MagicMock()
    inbox.select.return_value = ("OK", [b"1"])
    inbox.uid.side_effect = [("OK", [b"1"]), ("OK", [(b"1", message().as_bytes())])]
    client = MagicMock()
    client.return_value.__enter__.return_value = inbox
    monkeypatch.setattr("src.github_mail.imaplib.IMAP4_SSL", client)
    assert read_github_code("worker@example.org", "secret-for-test", NOW - 40) == "12345678"
    assert client.call_args.args == ("mail.ggb6.com", 993)
    assert client.call_args.kwargs["ssl_context"].check_hostname
    inbox.login.assert_called_once_with("worker@example.org", "secret-for-test")
    inbox.select.assert_called_once_with("INBOX", readonly=True)
    assert inbox.uid.call_args_list[-1].args[-1] == "(BODY.PEEK[]<0.262144>)"


def _mail(*, sender="GitHub <noreply@github.com>", subject="Your GitHub launch code", date=NOW, code="12345678", body=None):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "worker@example.org"
    msg["Date"] = formatdate(date, usegmt=True)
    msg["Subject"] = subject
    msg.set_content(body if body is not None else f"Your verification code is {code}")
    return msg


def _imap_for(monkeypatch, messages):
    inbox = MagicMock()
    inbox.select.return_value = ("OK", [b"1"])
    uid_list = b" ".join(str(i + 1).encode() for i in range(len(messages)))
    fetch_results = [
        ("OK", [(str(i + 1).encode(), raw.as_bytes())]) for i, raw in enumerate(messages)
    ]
    inbox.uid.side_effect = [("OK", [uid_list]), *fetch_results]
    client = MagicMock()
    client.return_value.__enter__.return_value = inbox
    monkeypatch.setattr("src.github_mail.imaplib.IMAP4_SSL", client)
    monkeypatch.setattr("src.github_mail.time.time", lambda: NOW)
    return client, inbox


def test_fetch_latest_mail_code_returns_recent_code(monkeypatch):
    client, inbox = _imap_for(monkeypatch, [_mail()])
    result = fetch_latest_mail_code("worker@example.org", "secret-for-test")
    assert result["code"] == "12345678"
    assert result["timestamp"] == NOW
    assert "GitHub" in result["from"]
    assert result["subject"] == "Your GitHub launch code"
    assert client.call_args.args == ("mail.ggb6.com", 993)
    inbox.login.assert_called_once_with("worker@example.org", "secret-for-test")
    inbox.select.assert_called_once_with("INBOX", readonly=True)


def test_fetch_latest_mail_code_prefers_verification_mail(monkeypatch):
    _imap_for(
        monkeypatch,
        [
            _mail(sender="news@example.org", subject="Weekly digest", code="111111"),
            _mail(sender="noreply@github.com", subject="Your launch code", code="87654321"),
        ],
    )
    result = fetch_latest_mail_code("worker@example.org", "secret-for-test")
    assert result["code"] == "87654321"


def test_fetch_latest_mail_code_supports_eight_digit_codes(monkeypatch):
    _imap_for(monkeypatch, [_mail(code="12345678")])
    assert fetch_latest_mail_code("worker@example.org", "secret-for-test")["code"] == "12345678"


def test_fetch_latest_mail_code_ignores_stale_mail(monkeypatch):
    _imap_for(monkeypatch, [_mail(date=NOW - 3600)])
    assert fetch_latest_mail_code("worker@example.org", "secret-for-test", max_age_seconds=600) is None


def test_fetch_latest_mail_code_returns_none_on_login_failure(monkeypatch):
    client = MagicMock()
    client.return_value.__enter__.return_value.login.side_effect = RuntimeError("boom")
    monkeypatch.setattr("src.github_mail.imaplib.IMAP4_SSL", client)
    with pytest.raises(RuntimeError):
        fetch_latest_mail_code("worker@example.org", "wrong")


def test_fetch_latest_mail_code_returns_none_on_imap_error(monkeypatch):
    import imaplib

    client = MagicMock()
    client.return_value.__enter__.return_value.login.side_effect = imaplib.IMAP4.error("auth failed")
    monkeypatch.setattr("src.github_mail.imaplib.IMAP4_SSL", client)
    assert fetch_latest_mail_code("worker@example.org", "wrong") is None


def test_imap_logout_error_on_teardown_does_not_discard_code(monkeypatch):
    client, _inbox = _imap_for(monkeypatch, [_mail(code="99887766")])
    client.return_value.__exit__.side_effect = OSError("socket reset during LOGOUT")
    res = fetch_latest_mail_code("worker@example.org", "secret-for-test")
    assert res is not None and res["code"] == "99887766"

    monkeypatch.setattr("src.github_mail.time.time", lambda: NOW)
    client2, _inbox2 = _imap_for(monkeypatch, [message(html=True)])
    client2.return_value.__exit__.side_effect = OSError("LOGOUT timed out")
    assert read_github_code("worker@example.org", "secret-for-test", NOW - 40) == "12345678"
