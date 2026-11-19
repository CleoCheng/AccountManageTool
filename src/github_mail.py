import imaplib
import re
import ssl
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser


MAIL_HOST = "mail.ggb6.com"
MAIL_URL = "https://" + MAIL_HOST
_CODE = re.compile(r"(?<![\w-])(?:[0-9]{8}|[0-9]{6})(?![\w-])")
_SUBJECT = re.compile(r"launch code|verif(?:y|ication)|验证码|验证.*邮箱", re.I)
_PREFERRED = re.compile(r"github|workbuddy|launch code|verification|verify|验证码", re.I)
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.hidden = False

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.hidden = True

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.hidden = False

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def _message_code(raw: bytes, email: str, earliest: float, now: float) -> str | None:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    recipients = [str(value) for key in ("To", "Cc", "Delivered-To", "X-Original-To")
                  for value in message.get_all(key, [])]
    senders = getaddresses([str(message.get("From", ""))])
    if len(senders) != 1 or senders[0][1].lower() != "noreply@github.com":
        return None
    if email.casefold() not in {address.casefold() for _, address in getaddresses(recipients)}:
        return None
    subject = str(message.get("Subject", ""))
    if not _SUBJECT.search(subject):
        return None
    try:
        date = parsedate_to_datetime(str(message.get("Date", "")))
        if date.tzinfo is None or not earliest <= date.timestamp() <= now + 60:
            return None
    except (ValueError, TypeError, OverflowError):
        return None

    body = message.get_body(preferencelist=("plain", "html"))
    content = body.get_content() if body else ""
    if body and body.get_content_type() == "text/html":
        parser = _VisibleText()
        try:
            parser.feed(content)
        finally:
            parser.close()
        content = " ".join(parser.text)
    codes = set(_CODE.findall(subject + "\n" + re.sub(r"https?://\S+", "", content)))
    if len(codes) != 1:
        raise ValueError("最新验证邮件里没有唯一的验证码，请打开网页邮箱查看。")
    return codes.pop()


def _generic_message(raw: bytes, max_age_seconds: int, now: float) -> dict | None:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    subject = str(message.get("Subject", ""))
    sender = str(message.get("From", ""))
    date_header = str(message.get("Date", ""))
    try:
        date = parsedate_to_datetime(date_header)
        if date is None:
            return None
        timestamp = date.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None
    if timestamp > now + 60 or timestamp < now - max(0, int(max_age_seconds)):
        return None

    body = message.get_body(preferencelist=("plain", "html"))
    content = body.get_content() if body else ""
    if body and body.get_content_type() == "text/html":
        parser = _VisibleText()
        try:
            parser.feed(content)
        finally:
            parser.close()
        content = " ".join(parser.text)
    match = _CODE.search(subject + "\n" + re.sub(r"https?://\S+", "", content))
    if not match:
        return None
    return {
        "code": match.group(0),
        "from": sender,
        "subject": subject,
        "date": date_header,
        "timestamp": int(timestamp),
        "preferred": bool(_PREFERRED.search(sender) or _PREFERRED.search(subject)),
    }


@contextmanager
def _open_imap_readonly():
    ctx = imaplib.IMAP4_SSL(
        MAIL_HOST,
        993,
        ssl_context=ssl.create_default_context(),
        timeout=8,
    )
    inbox = ctx.__enter__()
    try:
        yield inbox
    finally:
        try:
            ctx.__exit__(None, None, None)
        except Exception:
            pass


def fetch_latest_mail_code(email: str, password: str, max_age_seconds: int = 1800) -> dict | None:
    """Read the latest inbox messages and return a recent verification code."""
    now = time.time()
    try:
        with _open_imap_readonly() as inbox:
            inbox.login(email, password)
            if inbox.select("INBOX", readonly=True)[0] != "OK":
                return None
            status, matches = inbox.uid("SEARCH", None, "ALL")
            if status != "OK":
                return None
            uids = (matches[0] or b"").split()[-8:]
            candidates: list[dict] = []
            for uid in reversed(uids):
                status, parts = inbox.uid("FETCH", uid, "(BODY.PEEK[]<0.262144>)")
                if status != "OK":
                    continue
                for part in parts:
                    if not isinstance(part, tuple):
                        continue
                    try:
                        info = _generic_message(part[1], max_age_seconds, now)
                    except (ValueError, TypeError, UnicodeError, LookupError):
                        continue
                    if info:
                        candidates.append(info)
            if not candidates:
                return None
            best = max(candidates, key=lambda item: (item["preferred"], item["timestamp"]))
            best.pop("preferred", None)
            return best
    except (OSError, imaplib.IMAP4.error, UnicodeError, LookupError, ValueError, TypeError):
        # IMAP servers and malformed mail headers raise a wide range of errors;
        # a failed fetch simply means "no code available right now".
        return None


def read_github_code(email: str, password: str, started_at: int) -> str:
    now = time.time()
    earliest = max(started_at - 60, now - 20 * 60)
    # IMAP SINCE uses whole days in the server's timezone. Check the exact
    # timestamp and recipient inside the message before exposing a code.
    search_date = datetime.fromtimestamp(earliest - 86400, timezone.utc)
    since = f"{search_date.day:02d}-{_MONTHS[search_date.month - 1]}-{search_date.year}"
    try:
        with _open_imap_readonly() as inbox:
            inbox.login(email, password)
            if inbox.select("INBOX", readonly=True)[0] != "OK":
                raise ValueError("无法打开收件箱，请打开网页邮箱查看。")
            status, matches = inbox.uid("SEARCH", None, "SINCE", since, "FROM", '"noreply@github.com"')
            if status != "OK":
                raise ValueError("收件箱查询失败，请稍后重试。")
            for uid in reversed((matches[0] or b"").split()[-12:]):
                status, parts = inbox.uid("FETCH", uid, "(BODY.PEEK[]<0.262144>)")
                if status != "OK":
                    raise ValueError("读取验证邮件失败，请稍后重试。")
                for part in parts:
                    if isinstance(part, tuple):
                        code = _message_code(part[1], email, earliest, now)
                        if code:
                            return code
    except (OSError, imaplib.IMAP4.error, UnicodeError, LookupError) as exc:
        raise ValueError("邮箱登录或收取失败，请检查邮箱密码，或打开网页邮箱查看。") from exc
    raise ValueError("尚未找到发给此邮箱的近期 GitHub 验证邮件，可稍后重新读取。")
