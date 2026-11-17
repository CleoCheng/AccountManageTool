import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
import pyotp


def normalize_secret(secret: str) -> str:
    if not secret:
        return ""
    raw = str(secret).strip()
    if raw.lower().startswith("otpauth://"):
        try:
            parsed = urllib.parse.urlparse(raw)
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("secret"):
                raw = qs["secret"][0]
        except Exception:
            pass
    elif ":" in raw:
        parts = [re.sub(r"[\s\-]+", "", p).upper() for p in raw.split(":")]
        b32_parts = [p for p in parts if len(p) >= 8 and re.fullmatch(r"[A-Z2-7]+=*", p)]
        if b32_parts:
            raw = max(b32_parts, key=len)
    cleaned = re.sub(r"[\s\-]+", "", raw).upper()
    return cleaned


def mask_email(email: str) -> str:
    if not email:
        return ""
    email = email.strip()
    if "@" in email:
        local, domain = email.split("@", 1)
        if len(local) > 5:
            masked_local = f"{local[:3]}***{local[-2:]}"
        elif len(local) >= 2:
            masked_local = f"{local[0]}***{local[-1]}"
        else:
            masked_local = f"{local}***"
        return f"{masked_local}@{domain}"
    else:
        if len(email) > 5:
            return f"{email[:3]}***{email[-2:]}"
        return f"{email[:1]}***"


def mask_password(password: str) -> str:
    if not password:
        return ""
    return "••••••••"


def mask_secret(secret: str) -> str:
    cleaned = normalize_secret(secret)
    if not cleaned:
        return ""
    if len(cleaned) > 8:
        return f"{cleaned[:4]}****{cleaned[-4:]}"
    return "****"


def generate_totp(secret: str, at_time: int | None = None, offset_seconds: int = 0) -> dict:
    cleaned = normalize_secret(secret)
    base_time = int(at_time) if at_time is not None else int(time.time())
    now = base_time + int(offset_seconds or 0)
    remaining = 30 - (now % 30)
    if not cleaned:
        return {"code": "", "remaining": remaining, "valid": False}

    # Base32 only allows A-Z and 2-7 and optional '='
    if not re.fullmatch(r"[A-Z2-7]+=*", cleaned):
        return {"code": "", "remaining": remaining, "valid": False}

    # Pad to multiple of 8 for strict base32 decoders
    pad_len = (8 - (len(cleaned) % 8)) % 8
    padded = cleaned + ("=" * pad_len)
    try:
        key = base64.b32decode(padded, casefold=True)
        if not key:
            return {"code": "", "remaining": remaining, "valid": False}
        counter = now // 30
        msg = struct.pack(">Q", counter)
        digest = hmac.new(key, msg, hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
        code = f"{binary % 1000000:06d}"
        return {"code": code, "remaining": remaining, "valid": True}
    except Exception:
        return {"code": "", "remaining": remaining, "valid": False}


def _normalize_http_proxy(proxy_url: str | None) -> str:
    raw = str(proxy_url or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(pwd, safe='')}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return f"http://{raw}"


def sync_google_time_offset(proxy_url: str | None = None, timeout: float = 8.0) -> dict:
    """Measure clock offset (in seconds) against Google HTTP Date response header."""
    urls = [
        "https://www.google.com/generate_204",
        "https://accounts.google.com/generate_204",
        "https://clients3.google.com/generate_204",
    ]
    proxy = _normalize_http_proxy(proxy_url)
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        if proxy
        else urllib.request.build_opener()
    )
    last_err = ""
    for url in urls:
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
            t0 = time.time()
            with opener.open(req, timeout=timeout) as resp:
                t1 = time.time()
                date_hdr = resp.headers.get("Date")
            if not date_hdr:
                continue
            server_dt = parsedate_to_datetime(date_hdr)
            server_ts = server_dt.timestamp()
            local_mid = (t0 + t1) / 2.0
            offset = int(round(server_ts - local_mid))
            return {
                "ok": True,
                "offset_seconds": offset,
                "server_time": int(server_ts),
                "local_time": int(local_mid),
                "url": url,
            }
        except Exception as exc:
            last_err = str(exc)
    return {
        "ok": False,
        "offset_seconds": 0,
        "error": last_err or "无法从 Google 响应头读取时间",
    }


def generate_strong_password(length: int = 16) -> str:
    """Generate a high-entropy password containing all character classes."""
    size = max(12, min(64, int(length or 16)))
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghijkmnopqrstuvwxyz"
    digits = "23456789"
    specials = "!@#$%^&*_-+="
    all_chars = upper + lower + digits + specials

    chars = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits),
        secrets.choice(specials),
    ]
    chars.extend(secrets.choice(all_chars) for _ in range(size - len(chars)))
    for idx in range(len(chars) - 1, 0, -1):
        swap_idx = secrets.randbelow(idx + 1)
        chars[idx], chars[swap_idx] = chars[swap_idx], chars[idx]
    return "".join(chars)


def generate_random_base32_secret(length: int = 16) -> str:
    """Generate a random RFC 4648 Base32 secret suitable for TOTP enrollment."""
    size = max(16, min(64, int(length or 16)))
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    return "".join(secrets.choice(alphabet) for _ in range(size))


_PERSONA_POOLS = {
    "US": {
        "country": "United States",
        "locale": "en-US",
        "male_first": ["Liam", "Noah", "Ethan", "Mason", "Logan", "Lucas", "Caleb", "Nathan", "Owen", "Isaac"],
        "female_first": ["Emma", "Olivia", "Sophia", "Mia", "Ava", "Harper", "Evelyn", "Chloe", "Grace", "Hannah"],
        "last": ["Miller", "Davis", "Wilson", "Taylor", "Anderson", "Thomas", "Moore", "Martin", "Clark", "Walker"],
    },
    "JP": {
        "country": "Japan",
        "locale": "ja-JP",
        "male_first": ["Haruto", "Ren", "Yuto", "Sora", "Kaito", "Riku", "Hinata", "Daiki", "Takumi", "Shota"],
        "female_first": ["Yui", "Aoi", "Hina", "Sakura", "Mio", "Rina", "Koharu", "Mei", "Yuna", "Natsuki"],
        "last": ["Sato", "Suzuki", "Takahashi", "Tanaka", "Watanabe", "Ito", "Yamamoto", "Nakamura", "Kobayashi", "Kato"],
    },
    "DE": {
        "country": "Germany",
        "locale": "de-DE",
        "male_first": ["Lukas", "Jonas", "Felix", "Maximilian", "Paul", "Leon", "Tim", "Julian", "Moritz", "Niklas"],
        "female_first": ["Hannah", "Sophia", "Mia", "Emma", "Anna", "Lea", "Lena", "Marie", "Laura", "Clara"],
        "last": ["Mueller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker", "Hoffmann", "Koch"],
    },
}


def generate_account_persona(region: str = "US", domain: str = "gmail.com") -> dict:
    """Generate realistic registration identity details (name, birth date, handle, strong password)."""
    region_key = str(region or "US").strip().upper()
    if region_key not in _PERSONA_POOLS:
        region_key = "US"
    pool = _PERSONA_POOLS[region_key]
    gender = secrets.choice(["male", "female"])
    first_name = secrets.choice(pool["male_first"] if gender == "male" else pool["female_first"])
    last_name = secrets.choice(pool["last"])
    birth_year = 1986 + secrets.randbelow(16)
    birth_month = 1 + secrets.randbelow(12)
    birth_day = 1 + secrets.randbelow(28)
    suffix_num = 100 + secrets.randbelow(8999)
    sep = secrets.choice([".", "", "_"])
    username = f"{first_name.lower()}{sep}{last_name.lower()}{suffix_num}".strip(".")
    clean_domain = (domain or "gmail.com").strip().lstrip("@") or "gmail.com"
    return {
        "region": region_key,
        "country": pool["country"],
        "locale": pool["locale"],
        "first_name": first_name,
        "last_name": last_name,
        "full_name": f"{first_name} {last_name}",
        "gender": gender,
        "gender_label": "男" if gender == "male" else "女",
        "birth_year": birth_year,
        "birth_month": birth_month,
        "birth_day": birth_day,
        "birth_date": f"{birth_year:04d}-{birth_month:02d}-{birth_day:02d}",
        "username": username,
        "suggested_email": f"{username}@{clean_domain}",
        "password": generate_strong_password(16),
    }
