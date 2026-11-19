import csv
from datetime import datetime
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from src.totp_service import normalize_secret


def _is_likely_base32_secret(val: str) -> bool:
    cleaned = normalize_secret(val)
    return len(cleaned) >= 16 and bool(re.fullmatch(r"[A-Z2-7]+=*", cleaned))


def _is_likely_year(val: str) -> bool:
    return bool(re.fullmatch(r"(199\d|20[0-3]\d)", val.strip()))


def _is_domain_email_account(val: str) -> bool:
    """Return True for credentials belonging to the configured domain mailbox."""
    local, separator, domain = (val or "").strip().rpartition("@")
    return bool(separator and local and domain.lower() == "ggb6.com")


_CLAUDE_HINT_RE = re.compile(r"claude|anthropic", re.IGNORECASE)


def _claude_hint_text(record: dict) -> str:
    """Hint sources: username, recovery email and notes (never the password)."""
    return " ".join(
        str(record.get(key) or "") for key in ("account", "recovery_email", "note")
    )


def _apply_account_type_override(record: dict, account_type: str = "") -> dict:
    """Apply an explicit account type unless the row is a domain mailbox."""
    explicit = (account_type or "").strip()
    if explicit and explicit != "域名邮箱" and record.get("account_type") != "域名邮箱":
        record["account_type"] = explicit
    return record


def _apply_claude_hint(record: dict, account_type: str = "") -> dict:
    """Tag default ChatGPT rows as Claude when the source clearly says Claude.

    Only the username, recovery email and notes are inspected — a password that
    happens to contain ``claude`` must never change the category.
    """
    if not (account_type or "").strip() and record.get("account_type") == "ChatGPT":
        if _CLAUDE_HINT_RE.search(_claude_hint_text(record)):
            record["account_type"] = "Claude"
    return _apply_account_type_override(record, account_type)


def normalize_date_str(val: str, default_year: int | None = None) -> str:
    val = (val or "").strip()
    if not val:
        return ""
    if default_year is None:
        default_year = datetime.now().year

    # YYYY-MM-DD or YYYY/MM/DD
    m_full = re.fullmatch(r"(\d{4})[-/\.](\d{1,2})[-/\.](\d{1,2})", val)
    if m_full:
        y, m, d = int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3))
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"

    # MMDD (4 digits, e.g. 1021 -> Oct 21)
    m_short = re.fullmatch(r"(\d{2})(\d{2})", val)
    if m_short:
        m, d = int(m_short.group(1)), int(m_short.group(2))
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{default_year:04d}-{m:02d}-{d:02d}"

    return val


def _is_mmdd_or_date(val: str) -> bool:
    val = val.strip()
    if re.fullmatch(r"\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}", val):
        return True
    m_short = re.fullmatch(r"(\d{2})(\d{2})", val)
    if m_short:
        m, d = int(m_short.group(1)), int(m_short.group(2))
        return 1 <= m <= 12 and 1 <= d <= 31
    return False


def _default_record() -> dict:
    return {
        "account_type": "ChatGPT",
        "account": "",
        "password": "",
        "recovery_email": "",
        "secret_key": "",
        "initial_secret_key": "",
        "twofa_modified": 0,
        "purchase_date": "",
        "deactivated_date": "",
        "reg_year": "",
        "reg_region": "",
        "note": "",
    }


def parse_single_line(
    line: str,
    default_year: int | None = None,
    account_type: str = "",
) -> dict | None:
    line = line.strip()
    if not line:
        return None

    # Support ----, ---, |, or tab delimiters
    delim_used = ""
    if "---" in line:
        delim_used = "---"
        parts = [p.strip() for p in re.split(r"-{3,}", line)]
    elif "|" in line:
        delim_used = "|"
        parts = [p.strip() for p in line.split("|")]
    elif "\t" in line:
        delim_used = "\t"
        parts = [p.strip() for p in line.split("\t")]
    else:
        # Collapse space-grouped 4-char Base32 chunks (e.g. 'abcd efgh ijkl mnop') before splitting
        collapsed = re.sub(
            r"\b([A-Za-z2-7]{4})(?:[\s\-]+([A-Za-z2-7]{4})){3,}\b",
            lambda m: re.sub(r"[\s\-]+", "", m.group(0)).upper(),
            line,
        )
        parts = [p.strip() for p in re.split(r"\s{2,}", collapsed)]

    # Preserve empty middle slots (e.g. empty recovery email in 6-field Google format)
    # when an explicit delimiter separated >= 6 columns with account+password populated.
    non_empty_parts = [p for p in parts if p != ""]
    if delim_used and len(parts) >= 6 and parts[0] and parts[1]:
        while len(parts) > 6 and parts[-1] == "":
            parts.pop()
        if len(parts) >= 6:
            pass
        else:
            parts = non_empty_parts
    else:
        parts = non_empty_parts
    if not parts:
        return None
    # Do not treat a standalone Base32 secret line as an account username
    if len(parts) == 1 and "@" not in parts[0] and _is_likely_base32_secret(parts[0]):
        return None

    result = _default_record()
    result["account"] = parts[0] if len(parts) >= 1 else ""
    result["password"] = parts[1] if len(parts) >= 2 else ""
    is_domain_email = _is_domain_email_account(result["account"])
    if is_domain_email:
        result["account_type"] = "域名邮箱"

    if len(parts) == 3:
        third = parts[2]
        if "@" in third and not _is_likely_base32_secret(third):
            result["recovery_email"] = third
        else:
            result["secret_key"] = normalize_secret(third)
        if result["account"].lower().endswith("@gmail.com") and result["recovery_email"]:
            result["account_type"] = "Google"
        if is_domain_email:
            result["account_type"] = "域名邮箱"
        return _apply_claude_hint(result, account_type)

    if len(parts) >= 6:
        # Format B: Account ---- Password ---- Recovery ---- 2FA ---- Year ---- Region
        result["account_type"] = "Google"
        result["recovery_email"] = parts[2]
        result["secret_key"] = normalize_secret(parts[3])
        result["reg_year"] = parts[4]
        result["reg_region"] = parts[5]
        if len(parts) > 6:
            result["note"] = " | ".join(parts[6:])
        if is_domain_email:
            result["account_type"] = "域名邮箱"
        return _apply_claude_hint(result, account_type)

    # Heuristic fallback for 2, 4, or 5 fields
    extra_notes = []
    for token in parts[2:]:
        if "@" in token and not result["recovery_email"]:
            result["recovery_email"] = token
            result["account_type"] = "Google"
        elif _is_likely_base32_secret(token):
            cleaned_tok = normalize_secret(token)
            if not result["secret_key"]:
                result["secret_key"] = cleaned_tok
            else:
                result["initial_secret_key"] = result["secret_key"]
                result["secret_key"] = cleaned_tok
                result["twofa_modified"] = 1
        elif _is_likely_year(token) and not result["reg_year"]:
            result["reg_year"] = token
            result["account_type"] = "Google"
        elif not result["reg_region"] and len(token) <= 32:
            result["reg_region"] = token
        else:
            extra_notes.append(token)

    if result["account"].lower().endswith("@gmail.com") and (
        result["recovery_email"] or result["reg_year"] or result["reg_region"]
    ):
        result["account_type"] = "Google"

    if is_domain_email:
        result["account_type"] = "域名邮箱"

    if extra_notes:
        result["note"] = " | ".join(extra_notes)

    return _apply_claude_hint(result, account_type)


def _parse_structured_block(
    block_text: str,
    default_year: int | None = None,
    account_type: str = "",
) -> dict | None:
    lines = [ln.strip() for ln in block_text.split("\n") if ln.strip()]
    if not lines:
        return None

    # Stitch credential continuation lines (e.g. email on line 1, ----password----2fa or |password|2fa on line 2)
    # BUT do NOT stitch standalone deactivation date lines like '----1029', '|1029', '新2FA----...' or '新2FA|...'
    stitched_lines: list[str] = []
    for ln in lines:
        if re.match(r"^[-|]+\s*(\d{4}|\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2})\s*$", ln):
            stitched_lines.append(ln)
        elif re.match(r"^(?:新2FA|new\s*2fa)\s*[-|]+", ln, flags=re.IGNORECASE):
            stitched_lines.append(ln)
        elif (ln.startswith("---") or ln.startswith("|")) and stitched_lines:
            stitched_lines[-1] = stitched_lines[-1] + ln
        elif stitched_lines and (stitched_lines[-1].endswith("---") or stitched_lines[-1].endswith("|")):
            stitched_lines[-1] = stitched_lines[-1] + ln
        else:
            stitched_lines.append(ln)

    record = _default_record()
    notes: list[str] = []
    new_2fa_secret = ""

    for ln in stitched_lines:
        # Skip pure index lines like '1.' or '20.'
        if re.fullmatch(r"\d+\.", ln):
            continue

        # Standalone trailing deactivation date: '----1029' or '|1029'
        m_deact_only = re.fullmatch(r"[-|]+\s*(\d{4}|\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2})", ln)
        if m_deact_only and _is_mmdd_or_date(m_deact_only.group(1)):
            record["deactivated_date"] = normalize_date_str(m_deact_only.group(1), default_year)
            continue

        # Date line: '1021', '1019----1029', or '1019|1029'
        m_dates = re.fullmatch(
            r"(\d{4}|\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2})(?:\s*[-|]+\s*(\d{4}|\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}))?",
            ln,
        )
        if m_dates and _is_mmdd_or_date(m_dates.group(1)):
            record["purchase_date"] = normalize_date_str(m_dates.group(1), default_year)
            if m_dates.group(2) and _is_mmdd_or_date(m_dates.group(2)):
                record["deactivated_date"] = normalize_date_str(m_dates.group(2), default_year)
            continue

        # New 2FA line: '新2FA----...' or '新2FA|...'
        m_new2fa = re.match(r"^(?:新2FA|new\s*2fa)\s*[-|]+\s*(.+)$", ln, flags=re.IGNORECASE)
        if m_new2fa:
            new_2fa_secret = normalize_secret(m_new2fa.group(1))
            continue

        # Main credential line containing '---' or '|' or '@'
        if "---" in ln or "|" in ln or ("@" in ln and not record["account"]):
            parsed_cred = parse_single_line(
                ln, default_year=default_year, account_type=account_type
            )
            if parsed_cred and parsed_cred["account"]:
                record["account_type"] = parsed_cred["account_type"]
                record["account"] = parsed_cred["account"]
                record["password"] = parsed_cred["password"]
                record["recovery_email"] = parsed_cred["recovery_email"]
                record["secret_key"] = parsed_cred["secret_key"]
                record["reg_year"] = parsed_cred["reg_year"]
                record["reg_region"] = parsed_cred["reg_region"]
                if parsed_cred["note"]:
                    notes.append(parsed_cred["note"])
                continue

        notes.append(ln)

    if not record["account"]:
        return None

    if new_2fa_secret:
        record["initial_secret_key"] = record["secret_key"]
        record["secret_key"] = new_2fa_secret
        record["twofa_modified"] = 1

    if notes:
        record["note"] = " | ".join(notes)

    return _apply_claude_hint(record, account_type)


_DOMAIN_EMAIL_HEADERS = {
    "email": "email",
    "账号": "email",
    "邮箱": "email",
    "邮箱账号": "email",
    "account": "email",
    "password": "password",
    "密码": "password",
    "邮箱密码": "password",
    "授权码": "password",
    "name": "name",
    "姓名": "name",
    "名称": "name",
    "昵称": "name",
    "webmail": "webmail",
    "登录地址": "webmail",
    "邮箱地址": "webmail",
    "wb_reg_status": "wb_reg_status",
    "注册状态": "wb_reg_status",
    "workbuddy注册": "wb_reg_status",
}


def _col_letter_to_idx(cell_ref: str) -> int:
    idx = 0
    for ch in cell_ref:
        if ch.isalpha():
            idx = idx * 26 + (ord(ch.upper()) - ord("A") + 1)
        else:
            break
    return max(0, idx - 1)


def _read_xlsx_rows(raw_bytes: bytes) -> list[list[str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in ss_root.iter():
                    if si.tag.endswith("}si") or si.tag == "si":
                        chunks = [
                            t.text or ""
                            for t in si.iter()
                            if (t.tag.endswith("}t") or t.tag == "t")
                        ]
                        shared_strings.append("".join(chunks))

            sheet_names = sorted(
                n for n in zf.namelist()
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
            )
            if not sheet_names:
                raise ValueError("Excel 文件中未找到工作表。")
            ws_root = ET.fromstring(zf.read(sheet_names[0]))
            rows: list[list[str]] = []
            for row_el in ws_root.iter():
                if not (row_el.tag.endswith("}row") or row_el.tag == "row"):
                    continue
                row_vals: list[str] = []
                for c_el in row_el:
                    if not (c_el.tag.endswith("}c") or c_el.tag == "c"):
                        continue
                    ref = c_el.attrib.get("r", "")
                    col_idx = _col_letter_to_idx(ref) if ref else len(row_vals)
                    while len(row_vals) <= col_idx:
                        row_vals.append("")
                    c_type = c_el.attrib.get("t", "")
                    val = ""
                    if c_type == "inlineStr":
                        chunks = [
                            t.text or ""
                            for t in c_el.iter()
                            if (t.tag.endswith("}t") or t.tag == "t")
                        ]
                        val = "".join(chunks)
                    else:
                        v_el = next(
                            (ch for ch in c_el if ch.tag.endswith("}v") or ch.tag == "v"),
                            None,
                        )
                        raw_v = (v_el.text or "") if v_el is not None else ""
                        if c_type == "s" and raw_v.isdigit():
                            s_idx = int(raw_v)
                            val = shared_strings[s_idx] if 0 <= s_idx < len(shared_strings) else raw_v
                        else:
                            val = raw_v
                    row_vals[col_idx] = val
                rows.append(row_vals)
            return rows
    except zipfile.BadZipFile as exc:
        raise ValueError("无法解析 Excel (.xlsx) 文件，请确认文件未损坏。") from exc


def _rows_to_domain_email_records(rows: list[list[str]]) -> list[dict]:
    non_empty = [(idx + 1, r) for idx, r in enumerate(rows) if any((c or "").strip() for c in r)]
    if not non_empty:
        return []

    first_line_no, first_row = non_empty[0]
    mapped_cols = [
        _DOMAIN_EMAIL_HEADERS.get((c or "").strip().lstrip("\ufeff").lower(), "")
        for c in first_row
    ]
    has_header = "email" in mapped_cols and "password" in mapped_cols
    if has_header:
        col_map = {name: i for i, name in enumerate(mapped_cols) if name}
        data_rows = non_empty[1:]
    else:
        col_map = {"email": 0, "password": 1, "name": 2, "webmail": 3}
        data_rows = non_empty

    records: list[dict] = []
    for line_no, r in data_rows:
        def _cell(key: str) -> str:
            pos = col_map.get(key, -1)
            return r[pos] if 0 <= pos < len(r) else ""

        email = (_cell("email") or "").strip()
        password = _cell("password") or ""
        name = (_cell("name") or "").strip()
        webmail = (_cell("webmail") or "").strip() or "https://mail.ggb6.com"
        raw_status = (_cell("wb_reg_status") or "").strip()
        wb_status = "已完成" if raw_status in ("已完成", "done", "completed", "1", "true", "True") else "未完成"

        if not email or "@" not in email or not password.strip():
            raise ValueError(f"第 {line_no} 行邮箱或密码缺失，请检查表格格式（email,password,name,webmail）。")

        record = _default_record()
        record.update(
            account_type="域名邮箱",
            account=email,
            password=password,
            note=name,
            custom_fields={
                "wb_reg_status": wb_status,
                "holder_name": name,
                "webmail": webmail,
            },
        )
        records.append(record)
    return records


def parse_domain_email_file(raw_bytes: bytes, filename: str = "") -> list[dict]:
    if not raw_bytes:
        return []
    fname_low = (filename or "").lower()
    if fname_low.endswith(".xlsx") or raw_bytes.startswith(b"PK\x03\x04"):
        return _rows_to_domain_email_records(_read_xlsx_rows(raw_bytes))

    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("文件编码无法识别，请使用 UTF-8 或标准 Excel (.xlsx / .csv) 格式。")

    if fname_low.endswith(".csv"):
        rows = list(csv.reader(io.StringIO(text)))
        return _rows_to_domain_email_records(rows)
    return parse_domain_email_text(text)


def parse_domain_email_text(raw_text: str) -> list[dict]:
    cleaned = (raw_text or "").lstrip("\ufeff")
    non_empty_lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    if not non_empty_lines:
        return []

    first_line = non_empty_lines[0].strip()
    if "----" not in first_line and "," in first_line:
        rows = list(csv.reader(io.StringIO(cleaned)))
        return _rows_to_domain_email_records(rows)

    records = []
    for number, line in enumerate(cleaned.splitlines(), 1):
        if not line.strip():
            continue
        account, separator, password = line.partition("----")
        if not separator or not account.strip() or not password.strip():
            raise ValueError(f"第 {number} 行格式不完整，请按 账号----密码 或 CSV(email,password,name,webmail) 填写。")
        record = _default_record()
        record.update(
            account_type="域名邮箱",
            account=account.strip(),
            password=password,
            custom_fields={
                "wb_reg_status": "未完成",
                "webmail": "https://mail.ggb6.com",
            },
        )
        records.append(record)
    return records


def parse_account_text(
    raw_text: str,
    default_year: int | None = None,
    account_type: str = "",
) -> list[dict]:
    if not raw_text or not raw_text.strip():
        return []

    normalized = raw_text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    first_non_empty = next((ln.strip() for ln in normalized.split("\n") if ln.strip()), "")
    if "----" not in first_non_empty and "," in first_non_empty:
        first_cols = [c.strip().lower() for c in first_non_empty.split(",")]
        if ("email" in first_cols and "password" in first_cols) or ("邮箱" in first_cols and "密码" in first_cols):
            return parse_domain_email_text(normalized)

    # Case 1: Numbered blocks (e.g. '1.\n1021\n...')
    if re.search(r"(?m)^\s*\d+\.\s*$", normalized):
        blocks = re.split(r"(?m)^\s*\d+\.\s*$", normalized)
        records = []
        for blk in blocks:
            if not blk.strip():
                continue
            rec = _parse_structured_block(
                blk, default_year=default_year, account_type=account_type
            )
            if rec and rec["account"]:
                records.append(rec)
        if records:
            return records

    # Case 2: Unnumbered structured block(s) containing '新2FA' or standalone MMDD date lines
    date_line_matches = re.findall(r"(?m)^\s*\d{4}(?:\s*[-|]+\s*\d{4})?\s*$", normalized)
    if re.search(r"(?i)新2FA|new\s*2fa", normalized) or date_line_matches:
        # Split on standalone MMDD date lines (when multiple blocks exist) OR blank lines between accounts
        chunks: list[str]
        if len(date_line_matches) >= 2:
            raw_chunks = re.split(r"(?m)(?=^\s*\d{4}(?:\s*[-|]+\s*\d{4})?\s*$)", normalized)
            chunks = [c for c in raw_chunks if c.strip()]
        else:
            blank_chunks = [c for c in re.split(r"\n\s*\n+", normalized) if c.strip()]
            # Only treat blank lines as block separators if at least 2 chunks each carry an account/credential
            cred_chunks = [
                c for c in blank_chunks
                if "@" in c or "---" in c or "|" in c or "\t" in c
            ]
            chunks = blank_chunks if len(cred_chunks) >= 2 else [normalized]

        records = []
        for blk in chunks:
            rec = _parse_structured_block(
                blk, default_year=default_year, account_type=account_type
            )
            if rec and rec["account"]:
                records.append(rec)
        if records:
            return records

    # Case 3: Standard line-by-line or cross-line wrapped records (support ---- or |)
    stitched = re.sub(r"\n\s*(-{3,}|\|+)", r"\1", normalized)
    stitched = re.sub(r"(-{3,}|\|+)\s*\n\s*", r"\1", stitched)

    records = []
    for line in stitched.split("\n"):
        parsed = parse_single_line(
            line, default_year=default_year, account_type=account_type
        )
        if parsed and parsed["account"]:
            records.append(parsed)
    return records
