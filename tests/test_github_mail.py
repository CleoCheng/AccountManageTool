from email.message import EmailMessage
from email.utils import formatdate
from unittest.mock import MagicMock

import pytest

from src.github_mail import _message_code, read_github_code


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
