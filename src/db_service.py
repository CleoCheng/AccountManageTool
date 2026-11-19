import re
import shutil
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path
from src.browser_service import generate_fingerprint_config
from src.parser import normalize_date_str
from src.totp_service import (
    normalize_secret,
    mask_email,
    mask_password,
    mask_secret,
    generate_totp,
)


VALID_HEALTH_STATUSES = ["可用", "不可用", "封号", "人机", "短信", "验证"]
AVAILABILITY_STATES = ("可用", "不可用", "未核对")
HEALTH_CHALLENGES = ("人机", "短信", "验证")
MAILBOX_REGISTRATION_STATES = ("未完成", "进行中", "已完成")
ACCOUNT_EDIT_FIELDS = {
    "account_type": "账号类型", "health_status": "可用状态", "antigravity_tier": "Pro 选项",
    "device_tag": "设备标记", "account": "账号", "password": "登录密码",
    "password_modified": "密码已改", "recovery_email": "辅助邮箱", "secret_key": "2FA 密钥",
    "initial_secret_key": "初始 2FA 密钥", "twofa_modified": "2FA 已改", "purchase_date": "购买日期",
    "deactivated_date": "停用日期", "reg_year": "注册年份", "reg_region": "注册地区", "note": "备注",
}
ACCOUNT_CUSTOM_EDIT_FIELDS = {
    "recovery_password": "辅助邮箱密码", "api_key": "API Key", "session_token": "Session Token",
    "wb_reg_status": "WorkBuddy 注册状态", "chatgpt_bound": "是否绑定 ChatGPT",
    "sec_recovery_modified": "已更换辅助邮箱", "sec_devices_cleared": "已清理旧登录设备",
    "scan_qr_mode": "30 天复核提醒", "warming_reset_at": "复核提醒起始日",
}
SENSITIVE_HISTORY_FIELD = re.compile(r"password|secret|token|api[_-]?key|credential|proxy|密码|密钥|令牌", re.I)
VALID_ANTIGRAVITY_TIERS = ["无", "蓝色Pro", "金色Pro", "蓝色背景色Pro", "金色背景色Pro"]
VALID_DEVICE_TAGS = ["主机", "iPhone", "Pad"]

DEFAULT_ATTRIBUTES = [
    # (attr_key, attr_label, attr_type, bound_platforms, dict_type, sort_order, is_system, is_required, is_filter)
    ("health_status", "可用状态", "select", "All", "health_status", 10, 1, 1, 1),
    ("wb_reg_status", "WorkBuddy注册", "select", '["域名邮箱"]', "wb_reg_status", 15, 1, 0, 1),
    ("device_tag", "设备标记", "select", '["Google"]', "device_tag", 20, 1, 0, 1),
    ("antigravity_tier", "Pro 选项", "select", '["Google"]', "antigravity_tier", 30, 1, 0, 1),
    ("subscription_plan", "订阅计划", "select", '["Google"]', "subscription_plan", 35, 1, 0, 1),
    ("gcp_status", "GCP 状态", "select", '["Google"]', "gcp_status", 36, 1, 0, 1),
    ("rt_status", "RT 凭据", "select", '["Google"]', "rt_status", 37, 1, 0, 1),
    ("openai_plan", "AI 订阅方案", "select", '["ChatGPT", "Claude"]', "openai_plan", 38, 1, 0, 1),
    ("warming_group", "养号分组", "select", "All", "warming_group", 40, 1, 0, 1),
]

DEFAULT_DICTS = [
    # 账号类型
    ("account_type", "ChatGPT", "ChatGPT", "ChatGPT", "chatgpt", "🤖", 1, 1, 1),
    ("account_type", "Google", "Google", "Google", "google", "🔍", 2, 0, 1),
    ("account_type", "WorkBuddy", "WorkBuddy", "WorkBuddy", "workbuddy", "💼", 3, 0, 0),
    ("account_type", "域名邮箱", "域名邮箱", "https://mail.ggb6.com", "domain-email", "✉️", 4, 0, 1),
    ("account_type", "Qcoder", "Qcoder", "Qcoder", "qcoder", "⚡", 5, 0, 0),
    ("account_type", "Claude", "Claude", "Claude", "claude", "✴️", 6, 0, 1),
    # 设备标记
    ("device_tag", "主机", "主机", "主机", "host", "🖥️", 1, 1, 1),
    ("device_tag", "iPhone", "iPhone", "iPhone", "iphone", "📱", 2, 0, 1),
    ("device_tag", "Pad", "Pad", "Pad", "pad", "📱", 3, 0, 1),
    # 可用状态
    ("health_status", "可用", "可用", "可用", "available", "🟢", 1, 1, 1),
    ("health_status", "不可用", "不可用", "不可用", "unavailable", "⚪", 2, 0, 1),
    ("health_status", "封号", "封号", "封号", "banned", "🔴", 3, 0, 1),
    ("health_status", "人机", "人机", "人机", "captcha", "🟠", 4, 0, 1),
    ("health_status", "短信", "短信", "短信", "sms", "🟣", 5, 0, 1),
    ("health_status", "验证", "验证", "验证", "verify", "🔵", 6, 0, 1),
    # Antigravity Pro 选项
    ("antigravity_tier", "无", "无", "无", "none", "⚪", 1, 1, 1),
    ("antigravity_tier", "蓝色Pro", "Pro", "蓝色Pro", "blue", "💎", 2, 0, 1),
    ("antigravity_tier", "金色Pro", "Pro", "金色Pro", "gold", "👑", 3, 0, 1),
    # 域名邮箱 WorkBuddy 注册状态
    ("wb_reg_status", "未完成", "未完成", "未完成", "wb-pending", "⏳", 1, 1, 1),
    ("wb_reg_status", "进行中", "进行中", "进行中", "wb-progress", "🔄", 2, 0, 1),
    ("wb_reg_status", "已完成", "已完成", "已完成", "wb-done", "✅", 3, 0, 1),
    # Google 订阅计划
    ("subscription_plan", "Free", "Free", "Free", "sub-free", "🆓", 1, 1, 1),
    ("subscription_plan", "Pro", "Pro", "Pro", "sub-pro", "💎", 2, 0, 1),
    ("subscription_plan", "Ultra", "Ultra", "Ultra", "sub-ultra", "👑", 3, 0, 1),
    ("subscription_plan", "Trial", "Trial", "Trial", "sub-trial", "🎁", 4, 0, 1),
    # GCP 状态
    ("gcp_status", "none", "未检测", "none", "gcp-none", "⚪", 1, 1, 1),
    ("gcp_status", "active", "可用", "active", "gcp-active", "🟢", 2, 0, 1),
    ("gcp_status", "no_billing", "无结算", "no_billing", "gcp-nobilling", "🟡", 3, 0, 1),
    ("gcp_status", "validation", "需验证", "validation", "gcp-validation", "🟠", 4, 0, 1),
    ("gcp_status", "unsupported", "地区受限", "unsupported", "gcp-unsupported", "🟣", 5, 0, 1),
    ("gcp_status", "suspended", "已封停", "suspended", "gcp-suspended", "🔴", 6, 0, 1),
    # RT 凭据状态
    ("rt_status", "none", "未绑定", "none", "rt-none", "⚪", 1, 1, 1),
    ("rt_status", "active", "活跃", "active", "rt-active", "🟢", 2, 0, 1),
    ("rt_status", "invalid_grant", "已失效", "invalid_grant", "rt-invalid", "🔴", 3, 0, 1),
    ("rt_status", "unsupported", "地区受限", "unsupported", "rt-unsupported", "🟠", 4, 0, 1),
    ("rt_status", "suspended", "已封停", "suspended", "rt-suspended", "🟣", 5, 0, 1),
    # ChatGPT / Claude AI 方案
    ("openai_plan", "free", "Free", "free", "ai-free", "🆓", 1, 1, 1),
    ("openai_plan", "plus", "Plus", "plus", "ai-plus", "✨", 2, 0, 1),
    ("openai_plan", "pro", "Pro", "pro", "ai-pro", "👑", 3, 0, 1),
    ("openai_plan", "team", "Team", "team", "ai-team", "👥", 4, 0, 1),
    ("openai_plan", "api", "API Key", "api", "ai-api", "🔑", 5, 0, 1),
    # 养号分组
    ("warming_group", "默认组", "默认组", "默认组", "wg-default", "🌱", 1, 1, 1),
    ("warming_group", "A组(高频)", "A组(高频)", "A组(高频)", "wg-a", "🔥", 2, 0, 1),
    ("warming_group", "B组(静默)", "B组(静默)", "B组(静默)", "wg-b", "🌙", 3, 0, 1),
    ("warming_group", "C组(备用)", "C组(备用)", "C组(备用)", "wg-c", "📦", 4, 0, 1),
    ("warming_group", "沙盒组", "沙盒组", "沙盒组", "wg-sandbox", "🧪", 5, 0, 1),
]

MAINTENANCE_CF_KEYS = (
    "sec_recovery_modified",
    "sec_devices_cleared",
    "recovery_password",
    "scan_qr_mode",
    "warming_reset_at",
    "warming_group",
    "subscription_plan",
    "gcp_status",
    "rt_status",
    "openai_plan",
    "api_key",
    "session_token",
    "rt_json",
    "rt_refresh_token",
    "rt_access_token",
    "rt_expiry",
    "rt_email",
    "rt_project_id",
    "rt_tier",
    "rt_validation_url",
    "rt_last_checked",
    "rt_last_error",
    "ai_last_checked",
    "ai_models_count",
    "ai_last_error",
)


def normalize_gmail_address(email: str) -> str:
    cleaned = (email or "").strip().lower()
    if "@" not in cleaned:
        return cleaned
    local, domain = cleaned.split("@", 1)
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        return f"{local}@gmail.com"
    return f"{local}@{domain}"


def _decode_custom_fields(raw: str | None) -> dict:
    try:
        fields = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return fields if isinstance(fields, dict) else {}


def _account_availability(account_type: str, health_status: str, deactivated_date: str, reviewed: bool) -> str:
    if deactivated_date or health_status != "可用":
        return "不可用"
    if account_type == "Google" and not reviewed:
        return "未核对"
    return "可用"


def matches_health_filter(account: dict, status: str) -> bool:
    if status in AVAILABILITY_STATES:
        return account["availability"] == status
    return status in ("", "All", account["health_status"])


def _validate_changed_totp(secret_key: str, previous_key: str = "") -> None:
    if secret_key and secret_key != previous_key and not generate_totp(secret_key, at_time=0)["valid"]:
        raise ValueError("2FA 密钥格式无效，请填写 Base32 密钥或验证器设置链接。")


def compute_security_info(
    password_modified: int | bool,
    twofa_modified: int | bool,
    custom_fields: dict,
    recovery_email: str = "",
) -> dict:
    cf = custom_fields if isinstance(custom_fields, dict) else {}
    sec_pwd = bool(password_modified)
    sec_2fa = bool(twofa_modified)
    sec_recovery = bool(cf.get("sec_recovery_modified"))
    sec_devices = bool(cf.get("sec_devices_cleared"))
    completed = int(sec_pwd) + int(sec_2fa) + int(sec_recovery) + int(sec_devices)
    if completed == 4:
        status = "secured"
        label = "🔒 安全"
    elif completed > 0:
        status = "partial"
        label = f"🔧 改密中 ({completed}/4)"
    else:
        status = "unmodified"
        label = "⚠️ 未改密"
    return {
        "status": status,
        "label": label,
        "completed": completed,
        "total": 4,
        "password_modified": sec_pwd,
        "twofa_modified": sec_2fa,
        "recovery_modified": sec_recovery,
        "devices_cleared": sec_devices,
        "has_recovery_email": bool((recovery_email or "").strip()),
    }


def compute_warming_info(
    created_at: str,
    purchase_date: str,
    health_status: str,
    custom_fields: dict,
    today_dt: date | None = None,
    *,
    google_account: bool = False,
) -> dict:
    cf = custom_fields if isinstance(custom_fields, dict) else {}
    today = today_dt or date.today()
    anchor_raw = str(cf.get("warming_reset_at") or "").strip()
    if not anchor_raw:
        anchor_raw = (purchase_date or "").strip()
    if not anchor_raw and created_at:
        anchor_raw = str(created_at).strip()[:10]
    anchor_dt = _parse_date(anchor_raw) or today
    elapsed = max((today - anchor_dt).days, 0)
    status = (health_status or "").strip()
    is_risk = status in HEALTH_CHALLENGES
    is_scan_qr = bool(cf.get("scan_qr_mode"))
    target_days = 30 if is_scan_qr and not is_risk else 7
    remaining = max(target_days - elapsed, 0)
    ready = remaining == 0
    if is_risk:
        phase = "rewarm_ready" if ready else "rewarm"
        label = "风控静默已满" if ready else f"二次养号 {elapsed}/{target_days}天"
        hint = (
            f"风控状态已静默 {elapsed} 天，可尝试重测解控"
            if ready
            else f"触发风控后需静默 {target_days} 天再试（还差 {remaining} 天）"
        )
    elif is_scan_qr:
        phase = "scan_qr_ready" if ready else "scan_qr"
        label = "扫码期已满" if ready else f"扫码养号 {elapsed}/{target_days}天"
        hint = (
            f"扫码养号已满 {elapsed} 天，可转入常规改密"
            if ready
            else f"扫码防风控养号第 {elapsed}/{target_days} 天（还差 {remaining} 天）"
        )
    else:
        phase = "ready" if ready else "warming"
        label = "可改密" if ready else f"养号 {elapsed}/{target_days}天"
        hint = (
            f"已稳定养护 {elapsed} 天，可安全修改密码/2FA"
            if ready
            else f"新号第 {elapsed}/{target_days} 天养号期，建议第7天后再改密（还差 {remaining} 天）"
        )

    if google_account:
        label = "待复核" if ready else f"复核提醒 {elapsed}/{target_days}天"
        hint = "本地复核提醒已到期" if ready else f"距本地复核提醒还剩 {remaining} 天"
        if is_risk:
            label = f"待验证（{status}）"
        elif status in ("不可用", "封号"):
            label = status

    return {
        "phase": phase,
        "days_elapsed": elapsed,
        "target_days": target_days,
        "days_remaining": remaining,
        "ready": ready,
        "anchor_date": anchor_dt.strftime("%Y-%m-%d"),
        "scan_qr_mode": is_scan_qr,
        "label": label,
        "hint": hint,
    }


def _parse_date(d_str: str) -> date | None:
    if not d_str or not d_str.strip():
        return None
    norm = normalize_date_str(d_str.strip())
    try:
        return datetime.strptime(norm, "%Y-%m-%d").date()
    except Exception:
        return None


def compute_usage_info(purchase_date: str, deactivated_date: str, today_str: str | None = None) -> dict:
    p_dt = _parse_date(purchase_date)
    d_dt = _parse_date(deactivated_date)
    t_dt = _parse_date(today_str) if today_str else date.today()

    if d_dt is not None:
        status = "deactivated"
        status_label = "已停用"
        if p_dt is not None:
            days = max((d_dt - p_dt).days, 0)
            duration_text = f"共使用 {days} 天" if days > 0 else "共使用 <1 天"
        else:
            days = None
            duration_text = "已停用"
    else:
        status = "active"
        status_label = "使用中"
        if p_dt is not None and t_dt is not None:
            days = max((t_dt - p_dt).days, 0)
            duration_text = f"已使用 {days} 天" if days > 0 else "今日启用"
        else:
            days = None
            duration_text = "使用中"

    return {
        "status": status,
        "status_label": status_label,
        "days": days,
        "duration_text": duration_text,
    }


class DatabaseService:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.totp_offset_seconds = 0
        self._write_lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.db_path, timeout=30.0)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=15000;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            yield conn

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def _init_db(self) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_type TEXT NOT NULL DEFAULT 'ChatGPT',
                    health_status TEXT NOT NULL DEFAULT '可用',
                    antigravity_tier TEXT NOT NULL DEFAULT '无',
                    device_tag TEXT NOT NULL DEFAULT '主机',
                    account TEXT NOT NULL,
                    password TEXT NOT NULL,
                    password_modified INTEGER DEFAULT 0,
                    recovery_email TEXT DEFAULT '',
                    secret_key TEXT DEFAULT '',
                    initial_secret_key TEXT DEFAULT '',
                    twofa_modified INTEGER DEFAULT 0,
                    purchase_date TEXT DEFAULT '',
                    deactivated_date TEXT DEFAULT '',
                    reg_year TEXT DEFAULT '',
                    reg_region TEXT DEFAULT '',
                    note TEXT DEFAULT '',
                    custom_fields_json TEXT DEFAULT '{}',
                    fp_config_json TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            existing_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(accounts);").fetchall()
            }
            migrations = {
                "health_status": "ALTER TABLE accounts ADD COLUMN health_status TEXT DEFAULT '可用'",
                "antigravity_tier": "ALTER TABLE accounts ADD COLUMN antigravity_tier TEXT DEFAULT '无'",
                "device_tag": "ALTER TABLE accounts ADD COLUMN device_tag TEXT DEFAULT '主机'",
                "password_modified": "ALTER TABLE accounts ADD COLUMN password_modified INTEGER DEFAULT 0",
                "initial_secret_key": "ALTER TABLE accounts ADD COLUMN initial_secret_key TEXT DEFAULT ''",
                "twofa_modified": "ALTER TABLE accounts ADD COLUMN twofa_modified INTEGER DEFAULT 0",
                "purchase_date": "ALTER TABLE accounts ADD COLUMN purchase_date TEXT DEFAULT ''",
                "deactivated_date": "ALTER TABLE accounts ADD COLUMN deactivated_date TEXT DEFAULT ''",
                "fp_config_json": "ALTER TABLE accounts ADD COLUMN fp_config_json TEXT DEFAULT ''",
                "custom_fields_json": "ALTER TABLE accounts ADD COLUMN custom_fields_json TEXT DEFAULT '{}'",
            }
            for col_name, ddl in migrations.items():
                if col_name not in existing_cols:
                    conn.execute(ddl)

            # Ensure any null or empty fields have valid defaults
            conn.execute("UPDATE accounts SET custom_fields_json = '{}' WHERE custom_fields_json IS NULL OR custom_fields_json = ''")
            conn.execute("UPDATE accounts SET health_status = '可用' WHERE health_status IS NULL OR health_status = ''")
            conn.execute("UPDATE accounts SET antigravity_tier = '无' WHERE antigravity_tier IS NULL OR antigravity_tier = ''")
            conn.execute("UPDATE accounts SET device_tag = '主机' WHERE device_tag IS NULL OR device_tag = ''")
            # Deactivated accounts default to '不可用'
            conn.execute("UPDATE accounts SET health_status = '不可用' WHERE deactivated_date IS NOT NULL AND deactivated_date != '' AND health_status = '可用'")

            # System Dictionaries Table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_dicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dict_type TEXT NOT NULL,
                    dict_key TEXT NOT NULL,
                    dict_label TEXT NOT NULL,
                    dict_value TEXT NOT NULL,
                    color_class TEXT DEFAULT '',
                    icon TEXT DEFAULT '',
                    sort_order INTEGER DEFAULT 0,
                    is_default INTEGER DEFAULT 0,
                    is_system INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_dict_type_key ON system_dicts(dict_type, dict_key);")

            # Seed default dictionaries
            for dt, dk, dl, dv, cc, icon, so, is_def, is_sys in DEFAULT_DICTS:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO system_dicts 
                    (dict_type, dict_key, dict_label, dict_value, color_class, icon, sort_order, is_default, is_system)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (dt, dk, dl, dv, cc, icon, so, is_def, is_sys),
                )

            # Account Attributes Table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_attributes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attr_key TEXT NOT NULL UNIQUE,
                    attr_label TEXT NOT NULL,
                    attr_type TEXT NOT NULL DEFAULT 'text',
                    bound_platforms TEXT NOT NULL DEFAULT 'All',
                    dict_type TEXT DEFAULT '',
                    sort_order INTEGER DEFAULT 0,
                    is_system INTEGER DEFAULT 0,
                    is_required INTEGER DEFAULT 0,
                    is_filter INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            for ak, al, at, bp, dt, so, is_sys, is_req, is_filt in DEFAULT_ATTRIBUTES:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO account_attributes
                    (attr_key, attr_label, attr_type, bound_platforms, dict_type, sort_order, is_system, is_required, is_filter)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ak, al, at, bp, dt, so, is_sys, is_req, is_filt),
                )


            # Credential change history archive table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS credential_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    change_type TEXT NOT NULL DEFAULT 'update',
                    old_password TEXT DEFAULT '',
                    old_recovery_email TEXT DEFAULT '',
                    old_secret_key TEXT DEFAULT '',
                    old_recovery_password TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cred_hist_account ON credential_history(account_id, id DESC);"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_change_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    field TEXT NOT NULL,
                    label TEXT NOT NULL,
                    old_value TEXT NOT NULL,
                    new_value TEXT NOT NULL,
                    sensitive INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_account_change_history
                ON account_change_history(account_id, created_at DESC, id DESC)
            """)

            # Backfill permanent paired fingerprint config for any account missing it
            rows = conn.execute(
                "SELECT id, account, fp_config_json FROM accounts WHERE fp_config_json IS NULL OR fp_config_json = ''"
            ).fetchall()
            for r in rows:
                fp = generate_fingerprint_config(r["id"], r["account"])
                conn.execute(
                    "UPDATE accounts SET fp_config_json = ? WHERE id = ?",
                    (json.dumps(fp, ensure_ascii=False), r["id"]),
                )

            # Normalize all existing 2FA secrets (strip spaces/hyphens and uppercase)
            secret_rows = conn.execute(
                "SELECT id, secret_key, initial_secret_key FROM accounts"
            ).fetchall()
            for r in secret_rows:
                raw_sk = r["secret_key"] or ""
                raw_isk = r["initial_secret_key"] or ""
                clean_sk = normalize_secret(raw_sk)
                clean_isk = normalize_secret(raw_isk)
                if clean_sk != raw_sk or clean_isk != raw_isk:
                    conn.execute(
                        "UPDATE accounts SET secret_key = ?, initial_secret_key = ? WHERE id = ?",
                        (clean_sk, clean_isk, r["id"]),
                    )
            cnt_row = conn.execute("SELECT COUNT(1) AS cnt FROM accounts").fetchone()
            has_rows = bool(cnt_row and cnt_row["cnt"] > 0)
        if has_rows:
            self.create_rolling_backup(reason="startup", max_keep=15, min_interval_minutes=30)

    def _enrich_row(self, row: sqlite3.Row, offset_seconds: int | None = None) -> dict:
        item = dict(row)
        eff_offset = self.totp_offset_seconds if offset_seconds is None else int(offset_seconds or 0)
        item["health_status"] = (item.get("health_status") or "可用").strip()
        raw_ag = (item.get("antigravity_tier") or "无").strip()
        if raw_ag in ("蓝色Pro", "蓝色背景色Pro", "蓝Pro"):
            item["antigravity_tier"] = "蓝色Pro"
        elif raw_ag in ("金色Pro", "金色背景色Pro", "金Pro"):
            item["antigravity_tier"] = "金色Pro"
        else:
            item["antigravity_tier"] = "无"

        item["device_tag"] = (item.get("device_tag") or "主机").strip()
        item["password_modified"] = int(item.get("password_modified") or 0)
        item["twofa_modified"] = int(item.get("twofa_modified") or 0)
        item["secret_key"] = normalize_secret(item.get("secret_key") or "")
        item["initial_secret_key"] = normalize_secret(item.get("initial_secret_key") or "")
        item["purchase_date"] = item.get("purchase_date") or ""
        item["deactivated_date"] = item.get("deactivated_date") or ""
        item["masked_account"] = mask_email(item["account"])
        item["masked_password"] = mask_password(item["password"])
        item["masked_recovery_email"] = mask_email(item["recovery_email"]) if item["recovery_email"] else ""
        item["masked_secret"] = mask_secret(item["secret_key"])
        item["masked_initial_secret"] = mask_secret(item["initial_secret_key"]) if item["initial_secret_key"] else ""
        item["totp"] = generate_totp(item["secret_key"], offset_seconds=eff_offset)
        item["usage"] = compute_usage_info(item["purchase_date"], item["deactivated_date"])

        item["custom_fields"] = _decode_custom_fields(item.get("custom_fields_json"))
        if item.get("account_type") == "域名邮箱":
            if item["custom_fields"].get("wb_reg_status") not in MAILBOX_REGISTRATION_STATES:
                item["custom_fields"]["wb_reg_status"] = "未完成"
            item["custom_fields"]["chatgpt_bound"] = item["custom_fields"].get("chatgpt_bound") is True

        cf = item["custom_fields"]
        item["availability"] = _account_availability(
            item["account_type"], item["health_status"], item["deactivated_date"], bool(cf.get("google_reviewed_at"))
        )
        item["sec_recovery_modified"] = 1 if cf.get("sec_recovery_modified") else 0
        item["sec_devices_cleared"] = 1 if cf.get("sec_devices_cleared") else 0
        item["recovery_password"] = str(cf.get("recovery_password") or "")
        item["masked_recovery_password"] = mask_password(item["recovery_password"]) if item["recovery_password"] else ""
        item["security"] = compute_security_info(
            item["password_modified"], item["twofa_modified"], cf, item["recovery_email"]
        )
        is_google = item.get("account_type") == "Google"
        if is_google:
            security = item["security"]
            security["label"] = f"养护清单 {security['completed']}/{security['total']}"
        item["warming"] = compute_warming_info(
            str(item.get("created_at") or ""),
            item["purchase_date"],
            item["health_status"],
            cf,
            google_account=is_google,
        )
        item["has_rt"] = bool(cf.get("rt_json") or cf.get("rt_refresh_token"))
        item["rt_status"] = str(cf.get("rt_status") or ("active" if item["has_rt"] else "none"))
        item["gcp_status"] = str(cf.get("gcp_status") or "none")
        item["subscription_plan"] = str(cf.get("subscription_plan") or "Free")
        item["openai_plan"] = str(cf.get("openai_plan") or "free")
        item["warming_group"] = str(cf.get("warming_group") or "默认组")
        item["api_key"] = str(cf.get("api_key") or "")
        item["masked_api_key"] = (
            mask_secret(item["api_key"])
            if len(item["api_key"]) >= 8
            else ("****" if item["api_key"] else "")
        )
        item["has_ai_cred"] = bool(cf.get("api_key") or cf.get("session_token"))
        item["browser_last_opened_at"] = str(cf.get("browser_last_opened_at") or "")
        try:
            item["browser_open_count"] = int(cf.get("browser_open_count") or 0)
        except (TypeError, ValueError):
            item["browser_open_count"] = 0

        raw_fp = item.get("fp_config_json") or ""
        try:
            fp_cfg = json.loads(raw_fp) if raw_fp else None
        except Exception:
            fp_cfg = None
        if not fp_cfg:
            fp_cfg = generate_fingerprint_config(item["id"], item["account"])
        fp_cfg.setdefault("proxy", "")
        fp_cfg.setdefault("proxy_node", "")
        fp_cfg.setdefault("node_locked", False)
        for key in ("timezone", "locale", "accept_languages", "browser_lang", "last_exit_ip", "exit_country_code"):
            if fp_cfg.get(key) is None:
                fp_cfg[key] = ""
        item["fp_config"] = fp_cfg
        return item

    def list_accounts(
        self,
        account_type: str = "",
        search: str = "",
        status: str = "",
        health_status: str = "",
        antigravity_tier: str = "",
        device_tag: str = "",
    ) -> list[dict]:
        query = "SELECT * FROM accounts WHERE 1=1"
        params: list = []
        if account_type and account_type != "All":
            query += " AND account_type = ?"
            params.append(account_type)
        if device_tag and device_tag != "All":
            query += " AND device_tag = ?"
            params.append(device_tag)
        if status == "active":
            query += " AND (deactivated_date IS NULL OR deactivated_date = '')"
        elif status == "deactivated":
            query += " AND (deactivated_date IS NOT NULL AND deactivated_date != '')"
        if antigravity_tier and antigravity_tier != "All":
            if antigravity_tier in ("蓝色Pro", "蓝色背景色Pro", "蓝Pro"):
                query += " AND (antigravity_tier = '蓝色Pro' OR antigravity_tier = '蓝色背景色Pro' OR antigravity_tier = '蓝Pro')"
            elif antigravity_tier in ("金色Pro", "金色背景色Pro", "金Pro"):
                query += " AND (antigravity_tier = '金色Pro' OR antigravity_tier = '金色背景色Pro' OR antigravity_tier = '金Pro')"
            else:
                query += " AND (antigravity_tier = '无' OR antigravity_tier IS NULL OR antigravity_tier = '')"
        if search and search.strip():
            escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            kw = f"%{escaped}%"
            query += (
                " AND (account LIKE ? ESCAPE '\\' OR recovery_email LIKE ? ESCAPE '\\' OR reg_region LIKE ? ESCAPE '\\' "
                "OR reg_year LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\' OR purchase_date LIKE ? ESCAPE '\\' OR deactivated_date LIKE ? ESCAPE '\\' "
                "OR health_status LIKE ? ESCAPE '\\' OR antigravity_tier LIKE ? ESCAPE '\\' OR device_tag LIKE ? ESCAPE '\\' OR account_type LIKE ? ESCAPE '\\' "
                "OR custom_fields_json LIKE ? ESCAPE '\\')"
            )
            params.extend([kw] * 12)
        query += " ORDER BY id ASC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        accounts = [self._enrich_row(row) for row in rows]
        return [account for account in accounts if matches_health_filter(account, health_status)]

    def get_account(self, account_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return self._enrich_row(row) if row else None

    def _prepare_fields(self, data: dict) -> tuple:
        account_type = (data.get("account_type") or "ChatGPT").strip()
        health_status = (data.get("health_status") or "可用").strip()
        if health_status not in VALID_HEALTH_STATUSES:
            health_status = "可用"
        raw_ag = (data.get("antigravity_tier") or "无").strip()
        if raw_ag in ("蓝色Pro", "蓝色背景色Pro", "蓝Pro"):
            antigravity_tier = "蓝色Pro"
        elif raw_ag in ("金色Pro", "金色背景色Pro", "金Pro"):
            antigravity_tier = "金色Pro"
        else:
            antigravity_tier = "无"

        device_tag = (data.get("device_tag") or "主机").strip()
        account = (data.get("account") or "").strip()
        password = data.get("password") or ""
        if account_type != "域名邮箱":
            password = password.strip()
        password_modified = 1 if data.get("password_modified") in (1, True, "1", "true") else 0
        recovery_email = (data.get("recovery_email") or "").strip()
        secret_key = normalize_secret(data.get("secret_key") or "")
        initial_secret_key = normalize_secret(data.get("initial_secret_key") or "")
        twofa_modified = 1 if data.get("twofa_modified") in (1, True, "1", "true") else 0
        if account_type != "Google" and initial_secret_key and secret_key and initial_secret_key != secret_key:
            twofa_modified = 1
        purchase_date = normalize_date_str(data.get("purchase_date") or "")
        deactivated_date = normalize_date_str(data.get("deactivated_date") or "")
        reg_year = (data.get("reg_year") or "").strip()
        reg_region = (data.get("reg_region") or "").strip()
        note = (data.get("note") or "").strip()
        raw_custom = data.get("custom_fields")
        if isinstance(raw_custom, dict):
            cf_dict = dict(raw_custom)
        elif isinstance(data.get("custom_fields_json"), str) and data.get("custom_fields_json").strip():
            try:
                parsed_cf = json.loads(data.get("custom_fields_json").strip())
                cf_dict = dict(parsed_cf) if isinstance(parsed_cf, dict) else {}
            except Exception:
                cf_dict = {}
        else:
            cf_dict = {}
        if account_type == "域名邮箱":
            if cf_dict.get("wb_reg_status") not in MAILBOX_REGISTRATION_STATES:
                cf_dict["wb_reg_status"] = "未完成"
            if cf_dict["wb_reg_status"] == "已完成":
                cf_dict.pop("wb_reg_step", None)
                cf_dict.pop("wb_reg_step_label", None)
        custom_fields_json = json.dumps(cf_dict, ensure_ascii=False)
        return (
            account_type,
            health_status,
            antigravity_tier,
            device_tag,
            account,
            password,
            password_modified,
            recovery_email,
            secret_key,
            initial_secret_key,
            twofa_modified,
            purchase_date,
            deactivated_date,
            reg_year,
            reg_region,
            note,
            custom_fields_json,
        )

    def _merge_maintenance_cf(self, data: dict, base_cf: dict | None = None) -> dict:
        cf = dict(base_cf) if isinstance(base_cf, dict) else {}
        if isinstance(data.get("custom_fields"), dict):
            cf.update(data["custom_fields"])
        elif isinstance(data.get("custom_fields_json"), str) and data.get("custom_fields_json").strip():
            try:
                parsed = json.loads(data["custom_fields_json"].strip())
                if isinstance(parsed, dict):
                    cf.update(parsed)
            except Exception:
                pass
        for key in MAINTENANCE_CF_KEYS:
            if key in data:
                cf[key] = data[key]
        return cf

    def _record_credential_history(
        self,
        conn: sqlite3.Connection,
        account_id: int,
        existing: dict,
        new_pwd: str,
        new_rec: str,
        new_sk: str,
        new_rpwd: str,
        reason: str = "update",
    ) -> bool:
        old_pwd = existing.get("password") or ""
        old_rec = existing.get("recovery_email") or ""
        old_sk = normalize_secret(existing.get("secret_key") or "")
        old_rpwd = str((existing.get("custom_fields") or {}).get("recovery_password") or "")
        changed = (
            (bool(old_pwd) and new_pwd != old_pwd)
            or (bool(old_rec) and new_rec != old_rec)
            or (bool(old_sk) and new_sk != old_sk)
            or (bool(old_rpwd) and new_rpwd != old_rpwd)
        )
        if not changed:
            return False
        conn.execute(
            """
            INSERT INTO credential_history (
                account_id, change_type, old_password, old_recovery_email,
                old_secret_key, old_recovery_password, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (int(account_id), reason, old_pwd, old_rec, old_sk, old_rpwd, reason),
        )
        return True

    def _insert_account_in_tx(
        self, conn: sqlite3.Connection, data: dict, *, validate_totp: bool = False
    ) -> dict:
        prepared_input = dict(data)
        cf_dict = self._merge_maintenance_cf(prepared_input)
        cf_dict.pop("google_reviewed_at", None)
        prepared_input["custom_fields"] = cf_dict
        fields = self._prepare_fields(prepared_input)
        if validate_totp and fields[0] == "Google":
            _validate_changed_totp(fields[8])
        proxy = (data.get("proxy") or "").strip()
        cur = conn.execute(
            """
            INSERT INTO accounts (
                account_type, health_status, antigravity_tier, device_tag, account, password, password_modified, recovery_email,
                secret_key, initial_secret_key, twofa_modified,
                purchase_date, deactivated_date, reg_year, reg_region, note, custom_fields_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            fields,
        )
        new_id = cur.lastrowid
        fp = generate_fingerprint_config(
            new_id,
            fields[4],
            proxy=proxy,
            proxy_node=str(data.get("proxy_node") or "").strip(),
            node_locked=bool(data.get("node_locked", False)),
            timezone=str(data.get("timezone") or "").strip(),
            locale=str(data.get("locale") or "").strip(),
            accept_languages=str(data.get("accept_languages") or "").strip(),
        )
        conn.execute(
            "UPDATE accounts SET fp_config_json = ? WHERE id = ?",
            (json.dumps(fp, ensure_ascii=False), new_id),
        )
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (new_id,)).fetchone()
        return self._enrich_row(row)

    def add_account(self, data: dict, *, validate_totp: bool = False) -> dict:
        with self._write_tx() as conn:
            return self._insert_account_in_tx(conn, data, validate_totp=validate_totp)

    def batch_add_accounts(self, items: list[dict]) -> int:
        res = self.batch_import_accounts(items, duplicate_strategy="none")
        return int(res["imported"])

    def batch_import_accounts(self, items: list[dict], duplicate_strategy: str = "skip") -> dict:
        strategy = (duplicate_strategy or "skip").strip().lower()
        if strategy not in ("skip", "fill_empty", "overwrite", "none"):
            strategy = "skip"

        imported = 0
        updated = 0
        skipped = 0
        with self._write_tx() as conn:
            existing_map: dict[tuple[str, str], dict] = {}
            if strategy != "none":
                for row in conn.execute("SELECT * FROM accounts ORDER BY id ASC").fetchall():
                    acc = self._enrich_row(row)
                    atype = (acc.get("account_type") or "").strip().lower()
                    email_key = (
                        normalize_gmail_address(acc.get("account") or "")
                        if atype == "google"
                        else (acc.get("account") or "").strip().lower()
                    )
                    if email_key:
                        existing_map[(atype, email_key)] = acc

            for data in items:
                account = (data.get("account") or "").strip()
                if not account:
                    continue
                atype = (data.get("account_type") or "ChatGPT").strip().lower()
                email_key = normalize_gmail_address(account) if atype == "google" else account.lower()
                dup = existing_map.get((atype, email_key)) if strategy != "none" else None

                if dup is not None:
                    if strategy == "skip":
                        skipped += 1
                        continue
                    if strategy == "fill_empty":
                        patch: dict = {}
                        for col in ("password", "recovery_email", "secret_key", "initial_secret_key",
                                    "purchase_date", "deactivated_date", "reg_year", "reg_region", "note"):
                            new_val = str(data.get(col) or "").strip()
                            old_val = str(dup.get(col) or "").strip()
                            if new_val and not old_val:
                                patch[col] = new_val
                        fp_old = dup.get("fp_config") or {}
                        for fkey in ("proxy", "timezone", "locale", "accept_languages"):
                            new_fval = str(data.get(fkey) or "").strip()
                            old_fval = str(fp_old.get(fkey) or "").strip()
                            if new_fval and not old_fval:
                                patch[fkey] = new_fval
                        cf_in = self._merge_maintenance_cf(data)
                        cf_old = dup.get("custom_fields") or {}
                        cf_patch = {}
                        for ck, cv in cf_in.items():
                            if cv not in ("", None, False) and cf_old.get(ck) in ("", None, False):
                                cf_patch[ck] = cv
                        if cf_patch:
                            patch["custom_fields"] = cf_patch
                        if patch:
                            upd_row = self._update_account(conn, dup, patch)
                            existing_map[(atype, email_key)] = self._enrich_row(upd_row)
                            updated += 1
                        else:
                            skipped += 1
                        continue
                    if strategy == "overwrite":
                        patch = {}
                        for col in ("health_status", "antigravity_tier", "device_tag", "password",
                                    "recovery_email", "secret_key", "initial_secret_key",
                                    "purchase_date", "deactivated_date", "reg_year", "reg_region", "note"):
                            val = data.get(col)
                            if val not in (None, ""):
                                patch[col] = val
                        for fkey in ("proxy", "timezone", "locale", "accept_languages"):
                            val = str(data.get(fkey) or "").strip()
                            if val:
                                patch[fkey] = val
                        cf_in = self._merge_maintenance_cf(data)
                        cf_non_empty = {k: v for k, v in cf_in.items() if v not in ("", None)}
                        if cf_non_empty:
                            patch["custom_fields"] = cf_non_empty
                        if patch:
                            upd_row = self._update_account(conn, dup, patch)
                            existing_map[(atype, email_key)] = self._enrich_row(upd_row)
                            updated += 1
                        else:
                            skipped += 1
                        continue

                if atype == "google":
                    data = {**data, "password_modified": 0, "twofa_modified": 0}
                created = self._insert_account_in_tx(conn, data)
                imported += 1
                if strategy != "none" and email_key:
                    existing_map[(atype, email_key)] = created

        return {"imported": imported, "updated": updated, "skipped": skipped}

    def update_account(self, account_id: int, data: dict, *, validate_totp: bool = False) -> dict | None:
        with self._write_tx() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if row is None:
                return None
            updated = self._update_account(conn, self._enrich_row(row), data, validate_totp=validate_totp)
        return self._enrich_row(updated)

    def _update_account(
        self, conn: sqlite3.Connection, existing: dict, data: dict, *, validate_totp: bool = False
    ) -> sqlite3.Row:
        account_id = existing["id"]
        merged = dict(existing)
        merged.update(data)
        is_google = (merged.get("account_type") or "").strip() == "Google"
        cf_dict = self._merge_maintenance_cf(data, existing.get("custom_fields"))
        cf_dict.pop("google_reviewed_at", None)
        reviewed_at = existing["custom_fields"].get("google_reviewed_at")
        if (
            is_google
            and reviewed_at
            and existing["account_type"] == "Google"
            and normalize_gmail_address(merged["account"]) == normalize_gmail_address(existing["account"])
            and merged["health_status"] == existing["health_status"]
        ):
            cf_dict["google_reviewed_at"] = reviewed_at
        if data.get("health_status") in HEALTH_CHALLENGES and existing.get("health_status") != data.get("health_status"):
            has_explicit_reset = (
                "warming_reset_at" in data
                or (isinstance(data.get("custom_fields"), dict) and "warming_reset_at" in data["custom_fields"])
            )
            if not has_explicit_reset:
                cf_dict["warming_reset_at"] = date.today().strftime("%Y-%m-%d")
        merged["custom_fields"] = cf_dict

        if not is_google and "password_modified" not in data:
            if data.get("password") and existing.get("password") and data["password"].strip() != existing["password"]:
                merged["password_modified"] = 1
            else:
                merged["password_modified"] = existing.get("password_modified", 0)
        if not is_google and "twofa_modified" not in data:
            new_sk_norm = normalize_secret(data.get("secret_key") or "")
            old_sk_norm = normalize_secret(existing.get("secret_key") or "")
            if new_sk_norm and old_sk_norm and new_sk_norm != old_sk_norm:
                merged["twofa_modified"] = 1
                if not merged.get("initial_secret_key"):
                    merged["initial_secret_key"] = old_sk_norm
        fields = self._prepare_fields(merged)
        if validate_totp and is_google:
            _validate_changed_totp(fields[8], existing["secret_key"])
        fp_cfg = dict(existing.get("fp_config") or generate_fingerprint_config(account_id, fields[4]))
        if not fp_cfg.get("node_locked"):
            if "proxy" in data:
                fp_cfg["proxy"] = (data.get("proxy") or "").strip()
            if "proxy_node" in data:
                fp_cfg["proxy_node"] = (data.get("proxy_node") or "").strip()
        # Explicit timezone/locale overrides live on the fingerprint config so
        # the browser env resolver can honour them on every launch.
        for key in ("timezone", "locale", "accept_languages"):
            if key in data:
                fp_cfg[key] = str(data.get(key) or "").strip()
        for key in ("proxy", "proxy_node", "node_locked"):
            fp_cfg.setdefault(key, "" if key != "node_locked" else False)
        for key in ("timezone", "locale", "accept_languages"):
            fp_cfg.setdefault(key, "")

        self._record_credential_history(
            conn,
            account_id,
            existing,
            new_pwd=fields[5],
            new_rec=fields[7],
            new_sk=fields[8],
            new_rpwd=str(cf_dict.get("recovery_password") or ""),
            reason=str(data.get("_history_reason") or "update"),
        )
        conn.execute(
            """
            UPDATE accounts SET
                account_type = ?,
                health_status = ?,
                antigravity_tier = ?,
                device_tag = ?,
                account = ?,
                password = ?,
                password_modified = ?,
                recovery_email = ?,
                secret_key = ?,
                initial_secret_key = ?,
                twofa_modified = ?,
                purchase_date = ?,
                deactivated_date = ?,
                reg_year = ?,
                reg_region = ?,
                note = ?,
                custom_fields_json = ?,
                fp_config_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (*fields, json.dumps(fp_cfg, ensure_ascii=False), account_id),
        )
        updated = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        self._record_account_changes(conn, existing, updated)
        return updated

    def _record_account_changes(self, conn: sqlite3.Connection, previous: dict, updated: sqlite3.Row) -> None:
        current = self._enrich_row(updated)
        changes = [
            (key, label, previous[key], current[key])
            for key, label in ACCOUNT_EDIT_FIELDS.items()
            if previous[key] != current[key]
        ]
        previous_proxy = previous["fp_config"].get("proxy", "")
        current_proxy = current["fp_config"].get("proxy", "")
        if previous_proxy != current_proxy:
            changes.append(("proxy", "专属代理", previous_proxy, current_proxy))

        before = previous["custom_fields"]
        after = current["custom_fields"]
        custom_keys = {key for key in before.keys() | after.keys() if before.get(key, "") != after.get(key, "")}
        if custom_keys:
            editable = {
                row["attr_key"]: row["attr_label"]
                for row in conn.execute("SELECT attr_key, attr_label FROM account_attributes")
                if row["attr_key"] not in ACCOUNT_EDIT_FIELDS and row["attr_key"] != "proxy"
            }
            editable.update(ACCOUNT_CUSTOM_EDIT_FIELDS)
            changes.extend(
                (key, editable[key], before.get(key, ""), after.get(key, ""))
                for key in sorted(custom_keys & editable.keys())
            )
        if changes:
            created_at = datetime.now(timezone.utc).isoformat()
            conn.executemany("""
                INSERT INTO account_change_history
                    (account_id, field, label, old_value, new_value, sensitive, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [
                (
                    previous["id"], key, label, json.dumps(old, ensure_ascii=False),
                    json.dumps(new, ensure_ascii=False),
                    int(key not in ("password_modified", "twofa_modified") and bool(
                        SENSITIVE_HISTORY_FIELD.search(key) or SENSITIVE_HISTORY_FIELD.search(label)
                    )),
                    created_at,
                )
                for key, label, old, new in changes
            ])

    def get_account_change_history(self, account_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT id, field, label, old_value, new_value, sensitive, created_at
                FROM account_change_history WHERE account_id = ?
                ORDER BY created_at DESC, id DESC
            """, (int(account_id),)).fetchall()
        return [
            {
                **dict(row),
                "old_value": json.loads(row["old_value"]),
                "new_value": json.loads(row["new_value"]),
                "sensitive": bool(row["sensitive"]),
            }
            for row in rows
        ]

    def set_domain_email_metadata(self, account_id: int, field: str, value: object) -> dict | None:
        if field == "chatgpt_bound":
            if type(value) is not bool:
                raise ValueError("请选择有效的 ChatGPT 绑定状态")
        elif field == "wb_reg_status":
            if value not in MAILBOX_REGISTRATION_STATES:
                raise ValueError("请选择有效的注册状态")
        else:
            raise ValueError("不支持的域名邮箱标记")
        with self._write_tx() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
            if row is None:
                return None
            if row["account_type"] != "域名邮箱":
                raise ValueError("此标记仅适用于域名邮箱")
            previous = self._enrich_row(row)
            if previous["custom_fields"][field] == value:
                return previous
            fields = {**previous["custom_fields"], field: value}
            if field == "wb_reg_status":
                fields["wb_reg_manually_set"] = True
                fields.pop("wb_reg_step", None)
                fields.pop("wb_reg_step_label", None)
            conn.execute(
                "UPDATE accounts SET custom_fields_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(fields, ensure_ascii=False), int(account_id)),
            )
            updated = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
            self._record_account_changes(conn, previous, updated)
        return self._enrich_row(updated)

    def bind_account_proxy_node(
        self,
        account_id: int,
        proxy_url: str,
        proxy_node: str,
        locked: bool = True,
    ) -> dict:
        existing = self.get_account(account_id)
        if not existing:
            return {"ok": False, "error": "账号不存在"}
        fp_cfg = dict(existing.get("fp_config") or {})
        new_url = (proxy_url or "").strip()
        new_node = (proxy_node or "").strip()

        if fp_cfg.get("node_locked"):
            if new_url != fp_cfg.get("proxy", "") or new_node != fp_cfg.get("proxy_node", ""):
                return {
                    "ok": False,
                    "error": f"该账号已锁定节点 [{fp_cfg.get('proxy_node') or '直连'}]，不解锁不可变更节点！",
                }

        fp_cfg["proxy"] = new_url
        fp_cfg["proxy_node"] = new_node
        fp_cfg["node_locked"] = bool(locked)

        with self._connect() as conn:
            conn.execute(
                "UPDATE accounts SET fp_config_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(fp_cfg, ensure_ascii=False), int(account_id)),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
        return {"ok": True, "account": self._enrich_row(row)}

    def set_account_node_lock(self, account_id: int, locked: bool) -> dict:
        existing = self.get_account(account_id)
        if not existing:
            return {"ok": False, "error": "账号不存在"}
        fp_cfg = dict(existing.get("fp_config") or {})
        fp_cfg["node_locked"] = bool(locked)
        with self._connect() as conn:
            conn.execute(
                "UPDATE accounts SET fp_config_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(fp_cfg, ensure_ascii=False), int(account_id)),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
        return {"ok": True, "account": self._enrich_row(row)}

    # -------------------------------------------------------------
    # Domain Email Verification Codes & Node Allocation
    # -------------------------------------------------------------
    def record_email_verification_code(self, account_id: int, code_info: dict) -> dict | None:
        existing = self.get_account(int(account_id))
        if not existing or not isinstance(code_info, dict):
            return existing
        return self.update_account(int(account_id), {"custom_fields": {
            "latest_code": str(code_info.get("code") or ""),
            "code_received_at": int(code_info.get("timestamp") or time.time()),
            "code_subject": str(code_info.get("subject") or ""),
            "has_received_code": True,
        }})

    def get_node_code_stats(self) -> dict[str, int]:
        stats: dict[str, int] = {}
        for acc in self.list_accounts():
            cf = acc.get("custom_fields") or {}
            received = cf.get("has_received_code") is True or bool(
                str(cf.get("latest_code") or "").strip()
            )
            if not received:
                continue
            node = str((acc.get("fp_config") or {}).get("proxy_node") or "").strip()
            if node:
                stats[node] = stats.get(node, 0) + 1
        return stats

    def allocate_clean_node_for_account(
        self,
        account_id: int,
        available_nodes: list[str],
        default_proxy_url: str = "http://127.0.0.1:17899",
    ) -> str:
        existing = self.get_account(int(account_id))
        if not existing:
            return ""
        fp_cfg = existing.get("fp_config") or {}
        locked_node = str(fp_cfg.get("proxy_node") or "").strip()
        if fp_cfg.get("node_locked") and locked_node:
            return locked_node

        stats = self.get_node_code_stats()
        chosen_node = ""
        for node in available_nodes or []:
            name = str(node or "").strip()
            if not name:
                continue
            if stats.get(name, 0) < 2:
                chosen_node = name
                break
        if not chosen_node:
            return ""

        if fp_cfg.get("node_locked"):
            # A stale lock without a usable node must be released before rebinding.
            self.set_account_node_lock(int(account_id), False)
        self.bind_account_proxy_node(
            int(account_id),
            proxy_url=default_proxy_url,
            proxy_node=chosen_node,
            locked=True,
        )
        return chosen_node

    def reset_domain_email_nodes(self, account_ids: list[int] | None = None) -> dict:
        if account_ids:
            wanted = {int(i) for i in account_ids if int(i) > 0}
            candidates = [acc for acc in self.list_accounts() if acc["id"] in wanted]
        else:
            candidates = self.list_accounts(account_type="域名邮箱")

        reset_count = 0
        for acc in candidates:
            fp_cfg = dict(acc.get("fp_config") or {})
            fp_cfg["proxy_node"] = ""
            fp_cfg["node_locked"] = False
            cf = dict(acc.get("custom_fields") or {})
            cf.pop("latest_code", None)
            cf.pop("code_received_at", None)
            cf.pop("code_subject", None)
            cf.pop("has_received_code", None)
            with self._connect() as conn:
                conn.execute(
                    "UPDATE accounts SET fp_config_json = ?, custom_fields_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (
                        json.dumps(fp_cfg, ensure_ascii=False),
                        json.dumps(cf, ensure_ascii=False),
                        int(acc["id"]),
                    ),
                )
            reset_count += 1
        return {"ok": True, "reset_count": reset_count}

    def regenerate_fingerprint(self, account_id: int, salt: str = "") -> dict | None:
        existing = self.get_account(account_id)
        if not existing:
            return None
        old_fp = existing.get("fp_config") or {}
        new_fp = generate_fingerprint_config(
            account_id,
            existing["account"],
            proxy=old_fp.get("proxy", ""),
            proxy_node=old_fp.get("proxy_node", ""),
            node_locked=bool(old_fp.get("node_locked", False)),
            salt=salt,
            timezone=str(old_fp.get("timezone") or ""),
            locale=str(old_fp.get("locale") or ""),
            accept_languages=str(old_fp.get("accept_languages") or ""),
        )
        with self._connect() as conn:
            conn.execute(
                "UPDATE accounts SET fp_config_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(new_fp, ensure_ascii=False), account_id),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return self._enrich_row(row) if row else None

    def set_twofa_modified(self, account_id: int, modified: int | bool) -> dict | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if previous is None:
                return None
            conn.execute(
                "UPDATE accounts SET twofa_modified = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(bool(modified)), account_id),
            )
            updated = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            self._record_account_changes(conn, self._enrich_row(previous), updated)
        return self._enrich_row(updated)

    def set_password_modified(self, account_id: int, modified: int | bool) -> dict | None:
        return self.update_account(account_id, {"password_modified": int(bool(modified))})

    def set_health_status(self, account_id: int, health_status: str) -> dict | None:
        status_val = str(health_status or "").strip()
        with self._write_tx() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
            if row is None:
                return None
            existing = self._enrich_row(row)
            is_google = existing["account_type"] == "Google"
            if status_val not in VALID_HEALTH_STATUSES:
                if is_google:
                    raise ValueError("请选择有效的 Google 账号核对状态")
                status_val = "可用"
            cf = dict(existing["custom_fields"])
            if is_google:
                cf["google_reviewed_at"] = datetime.now(timezone.utc).isoformat()
            if status_val in HEALTH_CHALLENGES and existing["health_status"] != status_val:
                cf["warming_reset_at"] = date.today().isoformat()
            conn.execute(
                """
                UPDATE accounts SET
                    health_status = ?,
                    custom_fields_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status_val, json.dumps(cf, ensure_ascii=False), int(account_id)),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
            self._record_account_changes(conn, existing, row)
        return self._enrich_row(row) if row else None

    def set_antigravity_tier(self, account_id: int, antigravity_tier: str) -> dict | None:
        return self.update_account(int(account_id), {"antigravity_tier": antigravity_tier})

    def set_device_tag(self, account_id: int, device_tag: str) -> dict | None:
        return self.update_account(int(account_id), {"device_tag": device_tag})

    # -------------------------------------------------------------
    # System Dictionaries CRUD
    # -------------------------------------------------------------
    def list_system_dicts(self, dict_type: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if dict_type:
                rows = conn.execute(
                    "SELECT * FROM system_dicts WHERE dict_type = ? ORDER BY sort_order ASC, id ASC",
                    (dict_type,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM system_dicts ORDER BY dict_type ASC, sort_order ASC, id ASC"
                ).fetchall()
        return [dict(r) for r in rows]

    def save_system_dict(self, data: dict) -> dict:
        item_id = data.get("id")
        dict_type = (data.get("dict_type") or "").strip()
        dict_key = (data.get("dict_key") or "").strip()
        dict_label = (data.get("dict_label") or dict_key).strip()
        dict_value = (data.get("dict_value") or dict_key).strip()
        color_class = (data.get("color_class") or "").strip()
        icon = (data.get("icon") or "").strip()
        sort_order = int(data.get("sort_order") or 0)
        is_default = 1 if data.get("is_default") in (1, True, "1", "true") else 0

        if not dict_type or not dict_key:
            return {"ok": False, "error": "字典分类和标识键不能为空"}

        with self._write_tx() as conn:
            existing = conn.execute(
                "SELECT id FROM system_dicts WHERE dict_type = ? AND dict_key = ?",
                (dict_type, dict_key),
            ).fetchone()
            if existing and (not item_id or int(existing["id"]) != int(item_id)):
                return {"ok": False, "error": f"字典项「{dict_key}」在「{dict_type}」中已存在"}

            if is_default:
                conn.execute(
                    "UPDATE system_dicts SET is_default = 0 WHERE dict_type = ?",
                    (dict_type,),
                )

            if item_id:
                conn.execute(
                    """
                    UPDATE system_dicts SET
                        dict_label = ?, dict_value = ?, color_class = ?, icon = ?,
                        sort_order = ?, is_default = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (dict_label, dict_value, color_class, icon, sort_order, is_default, int(item_id)),
                )
                row = conn.execute("SELECT * FROM system_dicts WHERE id = ?", (int(item_id),)).fetchone()
            else:
                cur = conn.execute(
                    """
                    INSERT INTO system_dicts 
                    (dict_type, dict_key, dict_label, dict_value, color_class, icon, sort_order, is_default, is_system)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (dict_type, dict_key, dict_label, dict_value, color_class, icon, sort_order, is_default),
                )
                row = conn.execute("SELECT * FROM system_dicts WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {"ok": True, "dict": dict(row) if row else None}

    def delete_system_dict(self, dict_id: int) -> dict:
        with self._write_tx() as conn:
            row = conn.execute("SELECT * FROM system_dicts WHERE id = ?", (int(dict_id),)).fetchone()
            if not row:
                return {"ok": False, "error": "字典项不存在"}
            if row["is_system"]:
                return {"ok": False, "error": "内置系统字典项受系统保护，不可删除"}

            dict_type = row["dict_type"]
            dict_key = row["dict_key"]
            if dict_type == "account_type":
                cnt = conn.execute("SELECT COUNT(*) FROM accounts WHERE account_type = ?", (dict_key,)).fetchone()[0]
                if cnt > 0:
                    return {"ok": False, "error": f"当前有 {cnt} 个账号正在使用类型「{dict_key}」，请先修改或迁移账号后再删除"}
            elif dict_type == "device_tag":
                cnt = conn.execute("SELECT COUNT(*) FROM accounts WHERE device_tag = ?", (dict_key,)).fetchone()[0]
                if cnt > 0:
                    return {"ok": False, "error": f"当前有 {cnt} 个账号标记为设备「{dict_key}」，请先修改账号后再删除"}
            elif dict_type == "health_status":
                cnt = conn.execute("SELECT COUNT(*) FROM accounts WHERE health_status = ?", (dict_key,)).fetchone()[0]
                if cnt > 0:
                    return {"ok": False, "error": f"当前有 {cnt} 个账号处于状态「{dict_key}」，不可直接删除"}
            elif dict_type == "antigravity_tier":
                cnt = conn.execute("SELECT COUNT(*) FROM accounts WHERE antigravity_tier = ?", (dict_key,)).fetchone()[0]
                if cnt > 0:
                    return {"ok": False, "error": f"当前有 {cnt} 个账号处于Pro选项「{dict_key}」，不可直接删除"}

            conn.execute("DELETE FROM system_dicts WHERE id = ?", (int(dict_id),))
            return {"ok": True}

    def delete_account(self, account_id: int) -> bool:
        with self._write_tx() as conn:
            cur = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            conn.execute("DELETE FROM account_change_history WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM credential_history WHERE account_id = ?", (account_id,))
            return cur.rowcount > 0

    def get_stats(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT account_type, health_status, deactivated_date, device_tag, antigravity_tier,
                       CASE WHEN account_type = 'Google' THEN custom_fields_json END AS review_fields
                FROM accounts
            """).fetchall()
            total = len(rows)
            platform_totals = Counter(row["account_type"] for row in rows)
            device_totals = Counter(row["device_tag"] for row in rows)
            google, chatgpt, claude = (platform_totals[key] for key in ("Google", "ChatGPT", "Claude"))
            health_counts = dict.fromkeys((*VALID_HEALTH_STATUSES, "未核对"), 0)
            ag_none = 0
            ag_blue = 0
            ag_gold = 0
            for row in rows:
                status = (row["health_status"] or "可用").strip()
                reviewed = bool(_decode_custom_fields(row["review_fields"]).get("google_reviewed_at"))
                availability = _account_availability(row["account_type"], status, row["deactivated_date"], reviewed)
                health_counts[availability] += 1
                if status in health_counts and status not in AVAILABILITY_STATES:
                    health_counts[status] += 1
                tier = row["antigravity_tier"] or ""
                if tier in ("蓝色Pro", "蓝色背景色Pro", "蓝Pro"):
                    ag_blue += 1
                elif tier in ("金色Pro", "金色背景色Pro", "金Pro"):
                    ag_gold += 1
                elif tier in ("", "无"):
                    ag_none += 1
            available = health_counts["可用"]
            unavailable = health_counts["不可用"]

            # Dynamic account_type counts
            type_counts = {}
            dict_types = conn.execute("SELECT dict_key FROM system_dicts WHERE dict_type = 'account_type' ORDER BY sort_order ASC, id ASC").fetchall()
            for r in dict_types:
                k = r["dict_key"]
                type_counts[k] = platform_totals[k]

            # Dynamic device_tag counts
            device_counts = {}
            dict_devices = conn.execute("SELECT dict_key FROM system_dicts WHERE dict_type = 'device_tag' ORDER BY sort_order ASC, id ASC").fetchall()
            for r in dict_devices:
                k = r["dict_key"]
                device_counts[k] = device_totals[k]

            antigravity_counts = {
                "无": ag_none,
                "蓝色Pro": ag_blue,
                "金色Pro": ag_gold,
                "蓝色背景色Pro": ag_blue,
                "金色背景色Pro": ag_gold,
            }

        return {
            "total": total,
            "google": google,
            "chatgpt": chatgpt,
            "claude": claude,
            "types": type_counts,
            "devices": device_counts,
            "other": max(0, total - google - chatgpt - claude),
            "available": available,
            "unavailable": unavailable,
            "unreviewed": health_counts["未核对"],
            "active": available,
            "deactivated": unavailable,
            "health": health_counts,
            "antigravity": antigravity_counts,
        }

    def refresh_totp_codes(self, offset_seconds: int | None = None) -> dict[int, dict]:
        eff_offset = self.totp_offset_seconds if offset_seconds is None else int(offset_seconds or 0)
        with self._connect() as conn:
            rows = conn.execute("SELECT id, secret_key FROM accounts WHERE secret_key != ''").fetchall()
        return {row["id"]: generate_totp(row["secret_key"], offset_seconds=eff_offset) for row in rows}

    def export_accounts_text(self, account_type: str = "") -> str:
        items = self.list_accounts(account_type=account_type)
        lines = []
        for item in items:
            if item["account_type"] == "Google" or item["recovery_email"] or item["reg_year"] or item["reg_region"]:
                parts = [
                    item["account"],
                    item["password"],
                    item["recovery_email"],
                    item["secret_key"],
                    item["reg_year"],
                    item["reg_region"],
                ]
                while parts and parts[-1] == "":
                    parts.pop()
                lines.append("----".join(parts))
            else:
                parts = [item["account"], item["password"]]
                if item["secret_key"]:
                    parts.append(item["secret_key"])
                lines.append("----".join(parts))
        return "\n".join(lines)


    # -------------------------------------------------------------
    # Attributes and Platform Binding CRUD
    # -------------------------------------------------------------
    def list_attributes(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM account_attributes ORDER BY sort_order ASC, id ASC"
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                bp = d.get("bound_platforms") or "All"
                try:
                    d["platforms_list"] = ["All"] if bp == "All" else json.loads(bp)
                except Exception:
                    d["platforms_list"] = ["All"]
                result.append(d)
            return result

    def get_attribute(self, attr_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM account_attributes WHERE id = ?", (int(attr_id),)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            bp = d.get("bound_platforms") or "All"
            try:
                d["platforms_list"] = ["All"] if bp == "All" else json.loads(bp)
            except Exception:
                d["platforms_list"] = ["All"]
            return d

    def save_attribute(self, data: dict) -> dict:
        attr_id = data.get("id")
        attr_key = (data.get("attr_key") or "").strip()
        attr_label = (data.get("attr_label") or "").strip()
        attr_type = (data.get("attr_type") or "text").strip()
        dict_type = (data.get("dict_type") or "").strip()
        sort_order = int(data.get("sort_order") or 0)
        is_filter = 1 if data.get("is_filter") in (1, True, "1", "true") else 0
        is_required = 1 if data.get("is_required") in (1, True, "1", "true") else 0

        bound = data.get("bound_platforms", "All")
        if isinstance(bound, (list, tuple)):
            if not bound or "All" in bound:
                bound_str = "All"
            else:
                bound_str = json.dumps(list(bound), ensure_ascii=False)
        elif isinstance(bound, str):
            bound_str = bound.strip() or "All"
        else:
            bound_str = "All"

        if not attr_key:
            return {"ok": False, "error": "属性标识键不能为空"}
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]{1,63}$", attr_key):
            return {"ok": False, "error": "属性标识键必须以字母开头，且仅包含字母、数字和下划线 (2-64位)"}
        if not attr_label:
            return {"ok": False, "error": "属性显示名称不能为空"}
        if attr_type == "select" and not dict_type:
            dict_type = attr_key

        with self._write_tx() as conn:
            if attr_id:
                existing = conn.execute("SELECT * FROM account_attributes WHERE id = ?", (int(attr_id),)).fetchone()
                if not existing:
                    return {"ok": False, "error": "属性不存在"}
                if existing["is_system"] == 1 and existing["attr_key"] != attr_key:
                    return {"ok": False, "error": "系统内置属性的标识键不能修改"}

                conn.execute(
                    """
                    UPDATE account_attributes SET
                        attr_label = ?,
                        attr_type = ?,
                        bound_platforms = ?,
                        dict_type = ?,
                        sort_order = ?,
                        is_filter = ?,
                        is_required = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (attr_label, attr_type, bound_str, dict_type, sort_order, is_filter, is_required, int(attr_id)),
                )
                target_id = int(attr_id)
            else:
                conflict = conn.execute("SELECT id FROM account_attributes WHERE attr_key = ?", (attr_key,)).fetchone()
                if conflict:
                    return {"ok": False, "error": f"属性标识键 '{attr_key}' 已存在"}
                is_system = 1 if data.get("is_system") in (1, True, "1", "true") else 0
                cur = conn.execute(
                    """
                    INSERT INTO account_attributes
                    (attr_key, attr_label, attr_type, bound_platforms, dict_type, sort_order, is_system, is_required, is_filter)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (attr_key, attr_label, attr_type, bound_str, dict_type, sort_order, is_system, is_required, is_filter),
                )
                target_id = cur.lastrowid

        return {"ok": True, "attribute": self.get_attribute(target_id)}

    def delete_attribute(self, attr_id: int) -> dict:
        with self._write_tx() as conn:
            existing = conn.execute("SELECT * FROM account_attributes WHERE id = ?", (int(attr_id),)).fetchone()
            if not existing:
                return {"ok": False, "error": "属性不存在"}
            if existing["is_system"] == 1:
                return {"ok": False, "error": "系统内置属性不可删除"}
            conn.execute("DELETE FROM account_attributes WHERE id = ?", (int(attr_id),))
        return {"ok": True}

    def update_attribute_bindings(self, attr_id: int, bound_platforms: str | list) -> dict:
        if isinstance(bound_platforms, (list, tuple)):
            if not bound_platforms or "All" in bound_platforms:
                bound_str = "All"
            else:
                bound_str = json.dumps(list(bound_platforms), ensure_ascii=False)
        elif isinstance(bound_platforms, str):
            bound_str = bound_platforms.strip() or "All"
        else:
            bound_str = "All"

        with self._write_tx() as conn:
            existing = conn.execute("SELECT * FROM account_attributes WHERE id = ?", (int(attr_id),)).fetchone()
            if not existing:
                return {"ok": False, "error": "属性不存在"}
            conn.execute(
                "UPDATE account_attributes SET bound_platforms = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (bound_str, int(attr_id)),
            )
        return {"ok": True, "attribute": self.get_attribute(int(attr_id))}

    # -------------------------------------------------------------
    # Domain Email Import, WorkBuddy Registration Status & JSON Export
    # -------------------------------------------------------------
    def batch_import_domain_emails(self, items: list[dict]) -> dict:
        imported = 0
        skipped = 0
        with self._write_tx() as conn:
            existing_emails = {
                (r["account"] or "").strip().casefold()
                for r in conn.execute(
                    "SELECT account FROM accounts WHERE account_type = '域名邮箱'"
                ).fetchall()
            }
            for data in items:
                account = (data.get("account") or "").strip()
                if not account:
                    continue
                key = account.casefold()
                if key in existing_emails:
                    skipped += 1
                    continue
                existing_emails.add(key)
                row_data = dict(data)
                row_data["account_type"] = "域名邮箱"
                fields = self._prepare_fields(row_data)
                cur = conn.execute(
                    """
                    INSERT INTO accounts (
                        account_type, health_status, antigravity_tier, device_tag, account, password, password_modified, recovery_email,
                        secret_key, initial_secret_key, twofa_modified,
                        purchase_date, deactivated_date, reg_year, reg_region, note, custom_fields_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    fields,
                )
                new_id = cur.lastrowid
                fp = generate_fingerprint_config(new_id, account)
                conn.execute(
                    "UPDATE accounts SET fp_config_json = ? WHERE id = ?",
                    (json.dumps(fp, ensure_ascii=False), new_id),
                )
                imported += 1
        return {"imported": imported, "skipped": skipped}

    def save_workbuddy_credential(self, account_id: int, credential: dict) -> dict | None:
        if not isinstance(credential, dict):
            return self.get_account(int(account_id))
        with self._write_tx() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
            if not row:
                return None
            existing = self._enrich_row(row)
            cf = {"wb_credential": credential}
            if credential.get("uid"):
                cf["wb_uid"] = str(credential["uid"])
            updated_row = self._update_account(conn, existing, {"custom_fields": cf})
            if existing.get("account_type") == "域名邮箱":
                wb_row = conn.execute(
                    "SELECT * FROM accounts WHERE account_type = 'WorkBuddy' AND LOWER(account) = LOWER(?)",
                    (existing["account"],),
                ).fetchone()
                if wb_row:
                    wb_acc = self._enrich_row(wb_row)
                    wb_cf = dict(wb_acc.get("custom_fields") or {})
                    wb_cf["wb_credential"] = credential
                    if credential.get("uid"):
                        wb_cf["wb_uid"] = str(credential["uid"])
                    self._update_account(conn, wb_acc, {"custom_fields": wb_cf})
        return self._enrich_row(updated_row)

    def update_workbuddy_progress(
        self,
        account_id: int,
        step: str,
        step_label: str = "",
        username: str = "",
        proxy_url: str = "",
        proxy_node: str = "",
    ) -> dict | None:
        existing = self.get_account(int(account_id))
        if not existing:
            return None
        if existing["custom_fields"].get("wb_reg_status") == "已完成":
            return existing

        cf = {
            "wb_reg_status": "进行中",
            "wb_reg_manually_set": False,
            "wb_reg_step": str(step or ""),
            "wb_reg_step_label": str(step_label or step or ""),
        }
        if username:
            cf["github_username"] = str(username)
        if proxy_node:
            cf["proxy_node"] = str(proxy_node)

        upd_payload: dict = {"custom_fields": cf}
        if proxy_url:
            upd_payload["proxy"] = proxy_url
        if proxy_node:
            upd_payload["proxy_node"] = proxy_node
        return self.update_account(int(account_id), upd_payload)

    def mark_workbuddy_registered(
        self,
        account_id: int,
        username: str,
        region: str = "",
        credential: dict | None = None,
        proxy_url: str = "",
        proxy_node: str = "",
    ) -> dict | None:
        with self._write_tx() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
            if not row:
                return None
            existing = self._enrich_row(row)
            cf = {
                "wb_reg_status": "已完成",
                "wb_reg_manually_set": False,
                "wb_reg_step": "",
                "wb_reg_step_label": "",
                "github_username": username,
            }
            if region:
                cf["wb_region"] = region
            if proxy_node:
                cf["proxy_node"] = proxy_node
            if isinstance(credential, dict) and credential:
                cf["wb_credential"] = credential
                if credential.get("uid"):
                    cf["wb_uid"] = str(credential["uid"])

            upd_payload: dict = {"custom_fields": cf}
            if region:
                upd_payload["reg_region"] = region
            if proxy_url:
                upd_payload["proxy"] = proxy_url
            if proxy_node:
                upd_payload["proxy_node"] = proxy_node
            updated_mail_row = self._update_account(conn, existing, upd_payload)
            updated_mail = self._enrich_row(updated_mail_row)

            # Also upsert a paired WorkBuddy account entry so it appears in WorkBuddy platform list
            wb_cf = {
                "wb_reg_status": "已完成",
                "github_username": username,
                "source_email_id": int(account_id),
            }
            if region:
                wb_cf["wb_region"] = region
            if proxy_node:
                wb_cf["proxy_node"] = proxy_node
            if isinstance(credential, dict) and credential:
                wb_cf["wb_credential"] = credential
                if credential.get("uid"):
                    wb_cf["wb_uid"] = str(credential["uid"])

            note_parts = [f"GitHub: {username}"]
            if isinstance(credential, dict) and credential.get("uid"):
                note_parts.append(f"UID: {credential['uid']}")
            if proxy_node:
                note_parts.append(f"节点: {proxy_node}")
            wb_note = " | ".join(note_parts)

            wb_row = conn.execute(
                "SELECT * FROM accounts WHERE account_type = 'WorkBuddy' AND LOWER(account) = LOWER(?)",
                (existing["account"],),
            ).fetchone()

            if wb_row:
                prev_wb = self._enrich_row(wb_row)
                merged_wb_cf = {**(prev_wb.get("custom_fields") or {}), **wb_cf}
                self._update_account(
                    conn,
                    prev_wb,
                    {
                        "password": existing["password"],
                        "reg_region": region or prev_wb.get("reg_region", ""),
                        "note": wb_note,
                        "custom_fields": merged_wb_cf,
                        "proxy": proxy_url,
                        "proxy_node": proxy_node,
                    },
                )
            else:
                self._insert_account_in_tx(
                    conn,
                    {
                        "account_type": "WorkBuddy",
                        "account": existing["account"],
                        "password": existing["password"],
                        "reg_region": region,
                        "note": wb_note,
                        "custom_fields": wb_cf,
                        "proxy": proxy_url,
                        "proxy_node": proxy_node,
                        "node_locked": False,
                    },
                )
        return updated_mail

    def export_workbuddy_credentials_json(self, account_ids: list[int] | None = None) -> dict:
        all_accs = self.list_accounts()
        wb_creds_by_email: dict[str, dict] = {}
        mail_creds_by_email: dict[str, dict] = {}
        for a in all_accs:
            cred_a = (a.get("custom_fields") or {}).get("wb_credential")
            if not isinstance(cred_a, dict) or not cred_a:
                continue
            key_a = (a.get("account") or "").casefold()
            if a.get("account_type") == "WorkBuddy" and key_a not in wb_creds_by_email:
                wb_creds_by_email[key_a] = cred_a
            elif a.get("account_type") == "域名邮箱" and key_a not in mail_creds_by_email:
                mail_creds_by_email[key_a] = cred_a

        if account_ids:
            wanted = {int(i) for i in account_ids if int(i) > 0}
            candidates = [a for a in all_accs if a["id"] in wanted]
        else:
            candidates = [
                a for a in all_accs
                if a["account_type"] in ("WorkBuddy", "域名邮箱")
            ]

        rows: list[dict] = []
        seen_keys: set[str] = set()
        missing_token_emails: list[str] = []

        for acc in candidates:
            cf = acc.get("custom_fields") or {}
            cred = cf.get("wb_credential")
            if not isinstance(cred, dict) or not cred:
                email_cf = (acc.get("account") or "").casefold()
                if acc["account_type"] == "域名邮箱":
                    cred = wb_creds_by_email.get(email_cf)
                elif acc["account_type"] == "WorkBuddy":
                    cred = mail_creds_by_email.get(email_cf)

            if not isinstance(cred, dict) or not cred:
                if account_ids:
                    missing_token_emails.append(acc["account"])
                continue

            dedup_key = str(cred.get("uid") or acc["account"]).strip().casefold()
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            export_row = {
                "uid": str(cred.get("uid") or ""),
                "nickname": str(cred.get("nickname") or cf.get("github_username") or acc["account"].split("@")[0]),
                "domain": str(cred.get("domain") or "www.workbuddy.ai"),
                "realm": str(cred.get("realm") or "intl"),
                "platform": str(cred.get("platform") or "CLI"),
                "product": str(cred.get("product") or "workbuddy"),
                "enterpriseId": str(cred.get("enterpriseId") or ""),
                "accessToken": str(cred.get("accessToken") or ""),
                "refreshToken": str(cred.get("refreshToken") or ""),
                "expiresAt": int(cred.get("expiresAt") or 0),
                "addedAt": str(cred.get("addedAt") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "source": str(cred.get("source") or "oauth"),
                "apiKey": False,
                "proxySlot": str(cred.get("proxySlot") or ""),
                "proxy": str(cred.get("proxy") or (acc.get("fp_config") or {}).get("proxy") or ""),
                "enabled": bool(cred.get("enabled", True)),
                "tasksEnabled": bool(cred.get("tasksEnabled", True)),
                "weight": int(cred.get("weight") or 1),
                "note": str(cred.get("note") or acc.get("note") or acc["account"]),
            }
            if not export_row["accessToken"] or export_row["accessToken"].count(".") != 2:
                missing_token_emails.append(acc["account"])
                continue
            rows.append(export_row)

        doc = {
            "format": "workbuddy-accounts",
            "version": 1,
            "exportedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(rows),
            "accounts": rows,
        }
        return {
            "ok": len(rows) > 0,
            "count": len(rows),
            "missing_token_emails": missing_token_emails,
            "document": doc,
            "json_text": json.dumps(doc, ensure_ascii=False, indent=2),
            "error": "" if rows else "所选账号尚未保存 WorkBuddy 登录凭据，请先完成注册与凭据授权。",
        }

    # --- Maintenance, Credential History, Warming & Rolling Backups ---

    def get_credential_history(self, account_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, account_id, change_type, old_password, old_recovery_email,
                       old_secret_key, old_recovery_password, reason, created_at
                FROM credential_history
                WHERE account_id = ?
                ORDER BY id DESC
                """,
                (int(account_id),),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["masked_old_password"] = mask_password(d.get("old_password") or "")
            d["masked_old_secret_key"] = mask_secret(d.get("old_secret_key") or "")
            d["masked_old_recovery_password"] = mask_password(d.get("old_recovery_password") or "")
            result.append(d)
        return result

    def quick_update_credentials(self, account_id: int, payload: dict) -> dict | None:
        with self._write_tx() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
            if row is None:
                return None
            existing = self._enrich_row(row)
            patch = self._quick_credential_patch(existing, payload)
            updated = self._update_account(conn, existing, patch, validate_totp=True)
        return self._enrich_row(updated)

    def _quick_credential_patch(self, existing: dict, payload: dict) -> dict:
        is_google = existing["account_type"] == "Google"
        patch: dict = {"_history_reason": str(payload.get("reason") or "quick_edit")}
        for key in ("password", "recovery_email", "note"):
            if key in payload:
                patch[key] = payload[key]
        if "secret_key" in payload:
            new_sk = normalize_secret(payload.get("secret_key") or "")
            old_sk = normalize_secret(existing.get("secret_key") or "")
            patch["secret_key"] = new_sk
            if new_sk and old_sk and new_sk != old_sk and not existing.get("initial_secret_key"):
                patch["initial_secret_key"] = old_sk
                if not is_google:
                    patch["twofa_modified"] = 1
        for flag_key in ("password_modified", "twofa_modified"):
            if flag_key in payload:
                patch[flag_key] = 1 if payload[flag_key] else 0
        for cf_flag in ("sec_recovery_modified", "sec_devices_cleared", "scan_qr_mode"):
            if cf_flag in payload:
                patch[cf_flag] = bool(payload[cf_flag])
        if "recovery_password" in payload:
            patch["recovery_password"] = str(payload.get("recovery_password") or "")
        if is_google and payload.get("google_confirmed") is not True:
            credentials_changed = (
                any((patch[key] or "").strip() != existing[key] for key in ("password", "recovery_email") if key in patch)
                or any(patch[key] != existing[key] for key in ("secret_key", "recovery_password") if key in patch)
            )
            marked_complete = any(
                patch.get(key) and not existing[key]
                for key in ("password_modified", "twofa_modified", "sec_recovery_modified", "sec_devices_cleared")
            )
            if credentials_changed or marked_complete:
                raise ValueError("请先确认这些变更已在 Google 生效")
        return patch

    def reset_warming_time(
        self,
        account_id: int,
        scan_qr_mode: bool | None = None,
        reset_date: str = "",
    ) -> dict | None:
        anchor = normalize_date_str(reset_date.strip()) if reset_date and reset_date.strip() else ""
        cf = {"warming_reset_at": anchor or date.today().isoformat()}
        if scan_qr_mode is not None:
            cf["scan_qr_mode"] = bool(scan_qr_mode)
        return self.update_account(int(account_id), {"custom_fields": cf})

    def record_browser_launch(self, account_id: int) -> dict | None:
        """Record latest isolated browser launch timestamp & cumulative open count silently."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._write_tx() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
            if row is None:
                return None
            cf = _decode_custom_fields(row["custom_fields_json"])
            try:
                prev_cnt = int(cf.get("browser_open_count") or 0)
            except (TypeError, ValueError):
                prev_cnt = 0
            cf["browser_last_opened_at"] = now_str
            cf["browser_open_count"] = prev_cnt + 1
            conn.execute(
                "UPDATE accounts SET custom_fields_json = ? WHERE id = ?",
                (json.dumps(cf, ensure_ascii=False), int(account_id)),
            )
            updated = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
        return self._enrich_row(updated)

    def update_account_fp_geo_silent(
        self,
        account_id: int,
        geo_info: dict,
        *,
        auto_apply_tz: bool = True,
    ) -> dict | None:
        """Silently persist exit IP & geo metadata on ``fp_config_json`` without history spam."""
        if not isinstance(geo_info, dict):
            return self.get_account(int(account_id))
        with self._write_tx() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
            if row is None:
                return None
            enriched = self._enrich_row(row)
            fp_cfg = dict(enriched.get("fp_config") or {})
            ip = str(geo_info.get("ip") or "").strip()
            cc = str(geo_info.get("country_code") or "").strip().upper()
            if ip:
                fp_cfg["last_exit_ip"] = f"{ip} ({cc})" if cc else ip
            if cc:
                fp_cfg["exit_country_code"] = cc
            tz = str(geo_info.get("timezone") or "").strip()
            if auto_apply_tz and tz:
                fp_cfg["timezone"] = tz
            loc = str(geo_info.get("locale") or "").strip()
            if auto_apply_tz and loc and not str(fp_cfg.get("browser_lang") or "").strip():
                fp_cfg["locale"] = loc
            accept_lang = str(geo_info.get("accept_languages") or "").strip()
            if auto_apply_tz and accept_lang and not str(fp_cfg.get("browser_lang") or "").strip():
                fp_cfg["accept_languages"] = accept_lang
            conn.execute(
                "UPDATE accounts SET fp_config_json = ? WHERE id = ?",
                (json.dumps(fp_cfg, ensure_ascii=False), int(account_id)),
            )
            updated = conn.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),)).fetchone()
        return self._enrich_row(updated)

    def get_top_recovery_emails(self, limit: int = 8) -> list[dict]:
        lim = max(1, min(50, int(limit or 8)))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT LOWER(TRIM(recovery_email)) AS email_norm,
                       MAX(TRIM(recovery_email)) AS email,
                       COUNT(1) AS cnt
                FROM accounts
                WHERE TRIM(COALESCE(recovery_email, '')) != ''
                GROUP BY LOWER(TRIM(recovery_email))
                ORDER BY cnt DESC, email_norm ASC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
        return [{"email": r["email"], "count": int(r["cnt"])} for r in rows]

    @property
    def backups_dir(self) -> Path:
        return self.db_path.parent / "backups"

    def create_rolling_backup(
        self,
        reason: str = "manual",
        max_keep: int = 15,
        min_interval_minutes: int = 0,
    ) -> dict:
        if not self.db_path.exists():
            return {"ok": False, "error": "数据库文件不存在"}
        bdir = self.backups_dir
        bdir.mkdir(parents=True, exist_ok=True)
        existing_files = sorted(bdir.glob("accounts_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        if min_interval_minutes > 0 and existing_files:
            age_sec = time.time() - existing_files[0].stat().st_mtime
            if age_sec < min_interval_minutes * 60:
                return {"ok": True, "skipped": True, "filename": existing_files[0].name}

        safe_reason = re.sub(r"[^a-zA-Z0-9_-]", "", (reason or "manual")) or "manual"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:21]
        dest_path = bdir / f"accounts_{ts}_{safe_reason}.db"
        try:
            with closing(sqlite3.connect(self.db_path)) as src_conn:
                with closing(sqlite3.connect(dest_path)) as dst_conn:
                    src_conn.backup(dst_conn)
            all_backups = sorted(bdir.glob("accounts_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in all_backups[max(1, int(max_keep)):]:
                try:
                    stale.unlink()
                except Exception:
                    pass
            return {
                "ok": True,
                "filename": dest_path.name,
                "path": str(dest_path),
                "size_bytes": dest_path.stat().st_size,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def list_backups(self) -> list[dict]:
        bdir = self.backups_dir
        if not bdir.exists():
            return []
        files = sorted(bdir.glob("accounts_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        items = []
        for f in files:
            st = f.stat()
            items.append({
                "filename": f.name,
                "size_bytes": st.st_size,
                "created_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
        return items

    def _validate_backup_db(self, src_path: Path) -> str | None:
        required_cols = {
            "id",
            "account_type",
            "account",
            "password",
            "fp_config_json",
            "custom_fields_json",
        }
        try:
            with closing(sqlite3.connect(src_path, timeout=10.0)) as conn:
                chk = conn.execute("PRAGMA quick_check;").fetchone()
                if not chk or str(chk[0]).lower() != "ok":
                    return f"备份数据库完整性校验失败: {chk[0] if chk else 'unknown'}"
                tbl = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'"
                ).fetchone()
                if not tbl:
                    return "备份文件缺少 accounts 主表，已拒绝恢复。"
                cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts);").fetchall()}
                missing = sorted(required_cols - cols)
                if missing:
                    return f"备份文件缺少必要字段 ({', '.join(missing)})，已拒绝恢复。"
        except Exception as exc:
            return f"备份文件无法读取或已损坏: {exc}"
        return None

    def restore_backup(self, filename: str) -> dict:
        fname = Path(str(filename or "")).name
        if not fname or fname != str(filename).strip() or not fname.endswith(".db"):
            return {"ok": False, "error": "非法的备份文件名"}
        src_path = (self.backups_dir / fname).resolve()
        try:
            src_path.relative_to(self.backups_dir.resolve())
        except Exception:
            return {"ok": False, "error": "非法的备份路径"}
        if not src_path.exists():
            return {"ok": False, "error": f"备份文件不存在: {fname}"}

        invalid_reason = self._validate_backup_db(src_path)
        if invalid_reason:
            return {"ok": False, "error": invalid_reason}

        with self._write_lock:
            self.create_rolling_backup(reason="pre_restore", max_keep=15, min_interval_minutes=0)
            try:
                with closing(sqlite3.connect(src_path, timeout=30.0)) as src_conn:
                    with closing(sqlite3.connect(self.db_path, timeout=30.0)) as dst_conn:
                        src_conn.backup(dst_conn)
                self._init_db()
                return {"ok": True, "restored": fname}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

