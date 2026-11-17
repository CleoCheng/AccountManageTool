import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from src.browser_service import launch_isolated_browser, reset_rejected_login_session
from src.db_service import DatabaseService, compute_usage_info, normalize_gmail_address
from src.github_mail import fetch_latest_mail_code
from src.maintenance_service import (
    GCP_APPEAL_CHANNELS,
    OAuthLoopbackManager,
    inspect_rt_json,
    inject_rt_to_antigravity_client,
    probe_ai_credential,
    probe_antigravity_rt,
    probe_proxy_geo,
    shutdown_auth_relays,
)
from src.parser import parse_account_text, parse_domain_email_file, parse_domain_email_text
from src.proxy_service import detect_proxy_environment
from src.totp_service import (
    generate_account_persona,
    generate_random_base32_secret,
    generate_strong_password,
    generate_totp,
    mask_secret,
    normalize_secret,
    sync_google_time_offset,
)
from src.workbuddy import WorkBuddyQueue


DEFAULT_SETTINGS = {
    "theme": "dark",
    "view_mode": "compact",
    "default_account_type": "ChatGPT",
    "default_device_tag": "主机",
    "totp_interval": 30,
    "totp_offset_seconds": 0,
    "domain_email_login_url": "https://mail.ggb6.com",
    "last_filter_type": "All",
}


class AccountApi:
    def __init__(self, data_dir: str | Path, auto_sync_account_data: bool = False):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = DatabaseService(self.data_dir / "accounts.db")
        self._workbuddy = WorkBuddyQueue(self.db, self.data_dir)
        self._oauth = OAuthLoopbackManager(on_success_callback=self._on_oauth_success)
        self.settings_path = self.data_dir / "settings.json"
        settings = self._ensure_settings()
        self.db.totp_offset_seconds = int(settings.get("totp_offset_seconds") or 0)
        self.auto_sync_result: dict | None = None
        if auto_sync_account_data:
            self.auto_sync_result = self._auto_sync_account_data_silent()

    def _on_oauth_success(self, account_id: int, rt_payload: dict, rt_json_str: str) -> None:
        if int(account_id or 0) > 0:
            self.save_account_rt(int(account_id), rt_json_str)

    def _auto_sync_account_data_silent(self) -> dict | None:
        try:
            folder = self.data_dir.parent / "accountData"
            if not folder.exists() or not folder.is_dir():
                return None
            imported_total = 0
            skipped_total = 0
            files_scanned = 0
            for p in sorted(folder.iterdir()):
                if p.is_file() and p.suffix.lower() in (".xlsx", ".csv"):
                    try:
                        raw_bytes = p.read_bytes()
                        recs = parse_domain_email_file(raw_bytes, filename=p.name)
                        if recs:
                            res = self.db.batch_import_domain_emails(recs)
                            imported_total += res.get("imported", 0)
                            skipped_total += res.get("skipped", 0)
                        files_scanned += 1
                    except Exception:
                        continue
            return {
                "files_scanned": files_scanned,
                "imported": imported_total,
                "skipped": skipped_total,
            }
        except Exception:
            return None

    def _ensure_settings(self) -> dict:
        if not self.settings_path.exists():
            self.settings_path.write_text(
                json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return dict(DEFAULT_SETTINGS)
        try:
            loaded = json.loads(self.settings_path.read_text(encoding="utf-8"))
            merged = {**DEFAULT_SETTINGS, **loaded}
            return merged
        except Exception:
            return dict(DEFAULT_SETTINGS)

    def get_dashboard(
        self,
        account_type: str = "All",
        search: str = "",
        status: str = "All",
        health_status: str = "All",
        antigravity_tier: str = "All",
        device_tag: str = "All",
    ) -> dict:
        status_filter = "" if status in ("", "All") else status
        health_filter = "" if health_status in ("", "All") else health_status
        ag_filter = "" if antigravity_tier in ("", "All") else antigravity_tier
        device_filter = "" if device_tag in ("", "All") else device_tag
        settings = self._ensure_settings()
        offset = int(settings.get("totp_offset_seconds") or 0)
        self.db.totp_offset_seconds = offset
        accounts = self.db.list_accounts(
            account_type=account_type,
            search=search,
            status=status_filter,
            health_status=health_filter,
            antigravity_tier=ag_filter,
            device_tag=device_filter,
        )
        stats = self.db.get_stats()
        dicts = self.db.list_system_dicts()
        attributes = self.db.list_attributes()
        now = int(time.time()) + offset
        return {
            "accounts": accounts,
            "stats": stats,
            "settings": settings,
            "dicts": dicts,
            "attributes": attributes,
            "workbuddy": self._workbuddy.snapshot(),
            "node_code_stats": self.db.get_node_code_stats(),
            "top_recovery_emails": self.db.get_top_recovery_emails(8),
            "appeal_channels": GCP_APPEAL_CHANNELS,
            "server_time": now,
            "remaining": 30 - (now % 30),
        }

    def get_proxy_nodes(self, account_type_or_id: str | int = "") -> dict:
        acc_type = ""
        if isinstance(account_type_or_id, int) or (isinstance(account_type_or_id, str) and account_type_or_id.isdigit()):
            acc = self.db.get_account(int(account_type_or_id))
            if acc:
                acc_type = acc.get("account_type", "")
        elif isinstance(account_type_or_id, str):
            acc_type = account_type_or_id
        return detect_proxy_environment(account_type=acc_type)

    def bind_proxy_node(
        self,
        account_id: int,
        proxy_url: str,
        proxy_node: str,
        locked: bool = True,
    ) -> dict:
        return self.db.bind_account_proxy_node(
            int(account_id),
            proxy_url=proxy_url,
            proxy_node=proxy_node,
            locked=bool(locked),
        )

    def set_node_lock(self, account_id: int, locked: bool) -> dict:
        return self.db.set_account_node_lock(int(account_id), bool(locked))

    def smart_parse(self, raw_text: str, account_type: str = "") -> dict:
        import re
        from src.parser import _is_likely_base32_secret
        try:
            records = (
                parse_domain_email_text(raw_text)
                if account_type == "域名邮箱"
                else parse_account_text(raw_text, account_type=account_type)
            )
        except ValueError as exc:
            return {"records": [], "count": 0, "error": str(exc)}
        offset = int(self._ensure_settings().get("totp_offset_seconds") or 0)
        if not records and raw_text and raw_text.strip():
            stripped = raw_text.strip()
            m_new = re.match(r"^(?:新2FA|new\s*2fa)\s*[-|]*\s*(.+)$", stripped, flags=re.IGNORECASE)
            candidate = m_new.group(1) if m_new else stripped
            if "@" not in candidate and _is_likely_base32_secret(candidate):
                cleaned = normalize_secret(candidate)
                return {
                    "records": [],
                    "count": 0,
                    "only_secret": {
                        "secret_key": cleaned,
                        "twofa_modified": 1 if m_new else 0,
                        "masked_secret": mask_secret(cleaned),
                        "totp": generate_totp(cleaned, offset_seconds=offset),
                    },
                }

        for rec in records:
            rec["secret_key"] = normalize_secret(rec.get("secret_key", ""))
            rec["initial_secret_key"] = normalize_secret(rec.get("initial_secret_key", ""))
            rec["totp"] = generate_totp(rec.get("secret_key", ""), offset_seconds=offset)
            rec["masked_secret"] = mask_secret(rec.get("secret_key", ""))
            rec["masked_initial_secret"] = mask_secret(rec.get("initial_secret_key", ""))
            rec["usage"] = compute_usage_info(
                rec.get("purchase_date", ""),
                rec.get("deactivated_date", ""),
            )
        return {
            "records": records,
            "count": len(records),
        }

    def preview_totp(self, secret_key: str) -> dict:
        cleaned = normalize_secret(secret_key)
        offset = int(self._ensure_settings().get("totp_offset_seconds") or 0)
        return {
            "cleaned_secret": cleaned,
            "masked_secret": mask_secret(cleaned),
            "totp": generate_totp(cleaned, offset_seconds=offset),
        }

    def save_account(self, data: dict) -> dict:
        account_val = (data.get("account") or "").strip()
        if not account_val:
            return {"ok": False, "error": "账号不能为空"}

        acc_id = data.get("id")
        if acc_id:
            updated = self.db.update_account(int(acc_id), data)
            return {"ok": True, "account": updated}
        else:
            created = self.db.add_account(data)
            return {"ok": True, "account": created}

    def toggle_twofa_modified(self, account_id: int, modified: bool | int) -> dict:
        updated = self.db.set_twofa_modified(int(account_id), modified)
        if not updated:
            return {"ok": False, "error": "账号不存在"}
        return {"ok": True, "account": updated}

    def toggle_password_modified(self, account_id: int, modified: bool | int) -> dict:
        updated = self.db.set_password_modified(int(account_id), modified)
        if not updated:
            return {"ok": False, "error": "账号不存在"}
        return {"ok": True, "account": updated}

    def set_health_status(self, account_id: int, health_status: str) -> dict:
        updated = self.db.set_health_status(int(account_id), health_status)
        if not updated:
            return {"ok": False, "error": "账号不存在"}
        return {"ok": True, "account": updated}

    def set_antigravity_tier(self, account_id: int, antigravity_tier: str) -> dict:
        updated = self.db.set_antigravity_tier(int(account_id), antigravity_tier)
        if not updated:
            return {"ok": False, "error": "账号不存在"}
        return {"ok": True, "account": updated}

    def set_device_tag(self, account_id: int, device_tag: str) -> dict:
        updated = self.db.set_device_tag(int(account_id), device_tag)
        if not updated:
            return {"ok": False, "error": "账号不存在"}
        return {"ok": True, "account": updated}

    def get_system_dicts(self, dict_type: str | None = None) -> list[dict]:
        return self.db.list_system_dicts(dict_type)

    def save_system_dict(self, data: dict) -> dict:
        return self.db.save_system_dict(data)

    def delete_system_dict(self, dict_id: int) -> dict:
        return self.db.delete_system_dict(int(dict_id))

    def get_attributes(self) -> dict:
        return {"ok": True, "data": self.db.list_attributes()}

    def save_attribute(self, data: dict) -> dict:
        return self.db.save_attribute(data)

    def delete_attribute(self, attr_id: int) -> dict:
        return self.db.delete_attribute(int(attr_id))

    def update_attribute_bindings(self, attr_id: int, bound_platforms) -> dict:
        return self.db.update_attribute_bindings(int(attr_id), bound_platforms)

    def launch_browser(
        self,
        account_id: int,
        target_url: str = "",
        pure_native: bool = False,
    ) -> dict:
        import src.browser_service as bs
        account = self.db.get_account(int(account_id))
        if not account:
            return {"ok": False, "error": "账号不存在"}
        if account["account_type"] == "域名邮箱":
            fp_cfg = account.get("fp_config") or {}
            if not (fp_cfg.get("node_locked") and str(fp_cfg.get("proxy_node") or "").strip()):
                self.allocate_node_for_domain_email(int(account_id))
                account = self.db.get_account(int(account_id)) or account
            result = (
                bs.launch_isolated_browser(
                    self.data_dir,
                    account,
                    target_url=target_url,
                    pure_native=bool(pure_native),
                )
                if (target_url or pure_native)
                else bs.launch_isolated_browser(self.data_dir, account)
            )
            result.setdefault("fp_config", account.get("fp_config") or {})
            result.setdefault(
                "profile_dir",
                str(self.data_dir / "browsers" / f"acc_{int(account_id)}"),
            )
            return result
        if target_url or pure_native:
            return bs.launch_isolated_browser(
                self.data_dir,
                account,
                target_url=target_url,
                pure_native=bool(pure_native),
            )
        return bs.launch_isolated_browser(self.data_dir, account)

    def allocate_node_for_domain_email(self, account_id: int) -> str:
        env = self.get_proxy_nodes("WorkBuddy")
        nodes: list[str] = []
        for node in env.get("nodes") or []:
            if isinstance(node, str):
                name = node.strip()
            elif isinstance(node, dict):
                name = str(node.get("name") or "").strip()
            else:
                name = ""
            if name and name not in nodes:
                nodes.append(name)
        default_proxy_url = "http://127.0.0.1:17899"
        for port in env.get("ports") or []:
            if isinstance(port, dict) and port.get("url"):
                default_proxy_url = str(port["url"])
                break
        return self.db.allocate_clean_node_for_account(
            int(account_id),
            nodes,
            default_proxy_url=default_proxy_url,
        )

    def _is_active_domain_email(self, account: dict) -> bool:
        fp_cfg = account.get("fp_config") or {}
        cf = account.get("custom_fields") or {}
        acc_id = int(account["id"])
        if bool(str(fp_cfg.get("proxy_node") or "").strip()):
            return True
        if (self.data_dir / "browsers" / f"acc_{acc_id}").exists():
            return True
        if str(cf.get("wb_reg_status") or "").strip() == "进行中":
            return True
        return False

    def get_domain_email_codes(self, account_ids: list[int] | None = None) -> dict:
        wanted = {int(i) for i in account_ids} if account_ids else None
        accounts = self.db.list_accounts(account_type="域名邮箱")
        if wanted is not None:
            target_accounts = [acc for acc in accounts if acc["id"] in wanted]
        else:
            active_accounts = [acc for acc in accounts if self._is_active_domain_email(acc)]
            if active_accounts:
                target_accounts = active_accounts
            elif len(accounts) <= 5:
                # 兼容未绑定节点的少量测试数据，避免误伤小数据集
                target_accounts = accounts
            else:
                # 无活跃邮箱时避免全量扫描上百个闲置账号
                target_accounts = []

        codes: dict[int, dict] = {}
        if target_accounts:
            def _fetch_one(acc):
                try:
                    return fetch_latest_mail_code(acc["account"], acc["password"])
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=min(6, len(target_accounts))) as pool:
                code_infos = list(pool.map(_fetch_one, target_accounts))

            # SQLite 写入保留在主线程，避免跨线程并发写库
            for account, code_info in zip(target_accounts, code_infos):
                if not code_info:
                    continue
                updated = self.db.record_email_verification_code(account["id"], code_info)
                fp_cfg = (updated or account).get("fp_config") or {}
                codes[account["id"]] = {
                    "code": code_info.get("code", ""),
                    "received_at": code_info.get("timestamp"),
                    "subject": code_info.get("subject", ""),
                    "proxy_node": fp_cfg.get("proxy_node", ""),
                }
        return {
            "ok": True,
            "codes": codes,
            "node_stats": self.db.get_node_code_stats(),
        }

    def reset_domain_email_nodes(self, account_ids: list[int] | None = None) -> dict:
        res = self.db.reset_domain_email_nodes(account_ids)
        return {"ok": True, "reset_count": res.get("reset_count", 0)}

    def regenerate_fingerprint(self, account_id: int) -> dict:
        updated = self.db.regenerate_fingerprint(int(account_id), salt=str(time.time()))
        if not updated:
            return {"ok": False, "error": "账号不存在"}
        return {"ok": True, "account": updated}

    def batch_import(
        self,
        raw_text: str,
        account_type: str = "",
        duplicate_strategy: str = "skip",
    ) -> dict:
        try:
            records = (
                parse_domain_email_text(raw_text)
                if account_type == "域名邮箱"
                else parse_account_text(raw_text, account_type=account_type)
            )
        except ValueError as exc:
            return {"ok": False, "imported": 0, "error": str(exc)}
        if not records:
            return {"ok": False, "imported": 0, "error": "未识别到有效账号数据"}
        if account_type == "域名邮箱" or all(r.get("account_type") == "域名邮箱" for r in records):
            res = self.db.batch_import_domain_emails(records)
            out = {"ok": True, "imported": res["imported"]}
            if res.get("skipped"):
                out["skipped"] = res["skipped"]
            return out
        res = self.db.batch_import_accounts(records, duplicate_strategy=duplicate_strategy)
        out = {"ok": True, "imported": res["imported"]}
        if res.get("updated"):
            out["updated"] = res["updated"]
        if res.get("skipped"):
            out["skipped"] = res["skipped"]
        return out

    def import_domain_email_file(self, base64_data: str, filename: str = "") -> dict:
        raw_str = (base64_data or "").strip()
        if not raw_str:
            return {"ok": False, "imported": 0, "error": "文件内容为空"}
        if "," in raw_str and raw_str.lower().startswith("data:"):
            raw_str = raw_str.split(",", 1)[1]
        try:
            raw_bytes = base64.b64decode(raw_str)
            records = parse_domain_email_file(raw_bytes, filename=filename)
        except Exception as exc:
            return {"ok": False, "imported": 0, "error": f"解析表格失败: {exc}"}
        if not records:
            return {"ok": False, "imported": 0, "error": "表格中未识别到有效的域名邮箱账号与密码"}
        res = self.db.batch_import_domain_emails(records)
        return {
            "ok": True,
            "imported": res["imported"],
            "skipped": res["skipped"],
            "total": len(records),
            "filename": filename,
        }

    def import_account_data_files(self) -> dict:
        folder = self.data_dir.parent / "accountData"
        if not folder.is_dir():
            return {"ok": False, "imported": 0, "error": f"未找到目录: {folder}"}
        files = sorted(
            [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in (".xlsx", ".csv")],
            key=lambda x: x.name.lower(),
        )
        if not files:
            return {"ok": False, "imported": 0, "error": "accountData 目录下未找到 .xlsx 或 .csv 文件"}
        all_records: list[dict] = []
        parsed_names: list[str] = []
        for f in files:
            try:
                recs = parse_domain_email_file(f.read_bytes(), filename=f.name)
                if recs:
                    all_records.extend(recs)
                    parsed_names.append(f.name)
            except Exception as exc:
                return {"ok": False, "imported": 0, "error": f"读取 {f.name} 失败: {exc}"}
        if not all_records:
            return {"ok": False, "imported": 0, "error": "accountData 表格中未发现有效邮箱数据"}
        res = self.db.batch_import_domain_emails(all_records)
        return {
            "ok": True,
            "imported": res["imported"],
            "skipped": res["skipped"],
            "total": len(all_records),
            "files": parsed_names,
        }

    def get_workbuddy_queue(self) -> dict:
        return self._workbuddy.snapshot()

    def start_workbuddy_queue(
        self,
        account_ids: list[int],
        proxy_url: str = "",
        proxy_node: str = "",
    ) -> dict:
        return self._workbuddy.start(account_ids, proxy_url=proxy_url, proxy_node=proxy_node)

    def workbuddy_action(self, task_id: int, revision: int, action: str, value: str = "") -> dict:
        return self._workbuddy.action(task_id, revision, action, value)

    def get_workbuddy_node_quotas(self) -> dict:
        return self._workbuddy.get_node_quotas()

    def set_workbuddy_node_unlock(self, node_name: str, unlocked: bool = True, proxy_url: str = "") -> dict:
        return self._workbuddy.set_node_unlock(node_name, unlocked=bool(unlocked), proxy_url=proxy_url)

    def reset_workbuddy_node_quota(self, node_name: str) -> dict:
        return self._workbuddy.reset_node_quota(node_name)

    def export_workbuddy_json(
        self,
        account_ids: list[int] | None = None,
        save_to_account_data: bool = True,
    ) -> dict:
        res = self.db.export_workbuddy_credentials_json(account_ids)
        if res.get("ok") and save_to_account_data:
            try:
                out_dir = self.data_dir.parent / "accountData"
                out_dir.mkdir(parents=True, exist_ok=True)
                out_file = out_dir / "workbuddy-accounts-export.json"
                out_file.write_text(res["json_text"], encoding="utf-8")
                res["saved_path"] = str(out_file)
            except Exception:
                res["saved_path"] = ""
        return res

    def _shutdown(self) -> None:
        try:
            self._workbuddy.close()
        finally:
            try:
                self._oauth.shutdown()
            except Exception:
                pass
            try:
                shutdown_auth_relays()
            except Exception:
                pass
            try:
                from src.sidecar_proxy import shutdown_sidecar_proxy
                shutdown_sidecar_proxy()
            except Exception:
                pass

    def delete_account(self, account_id: int) -> dict:
        deleted = self.db.delete_account(int(account_id))
        return {"ok": deleted}

    def refresh_totp(self) -> dict:
        offset = int(self._ensure_settings().get("totp_offset_seconds") or 0)
        self.db.totp_offset_seconds = offset
        now = int(time.time()) + offset
        codes = self.db.refresh_totp_codes(offset_seconds=offset)
        return {
            "codes": {str(k): v for k, v in codes.items()},
            "remaining": 30 - (now % 30),
        }

    def export_text(self, account_type: str = "All") -> dict:
        text = self.db.export_accounts_text(account_type=account_type)
        return {"text": text}

    def save_settings(self, new_settings: dict) -> dict:
        current = self._ensure_settings()
        current.update(new_settings or {})
        self.db.totp_offset_seconds = int(current.get("totp_offset_seconds") or 0)
        self.settings_path.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return current

    # --- Maintenance, Security, Warming, Backups, RT/OAuth & Appeal APIs ---

    def get_credential_history(self, account_id: int) -> dict:
        return {"ok": True, "history": self.db.get_credential_history(int(account_id))}

    def quick_update_credentials(self, account_id: int, payload: dict) -> dict:
        updated = self.db.quick_update_credentials(int(account_id), payload or {})
        if not updated:
            return {"ok": False, "error": "账号不存在"}
        return {"ok": True, "account": updated}

    def generate_strong_password(self, length: int = 16) -> dict:
        return {
            "ok": True,
            "password": generate_strong_password(length),
            "secret_key": generate_random_base32_secret(16),
        }

    def generate_account_persona(self, region: str = "US", domain: str = "gmail.com") -> dict:
        return {
            "ok": True,
            "persona": generate_account_persona(region=region, domain=domain),
        }

    def get_top_recovery_emails(self, limit: int = 8) -> dict:
        return {"ok": True, "emails": self.db.get_top_recovery_emails(limit)}

    def reset_warming_time(
        self,
        account_id: int,
        scan_qr_mode: bool | None = None,
        reset_date: str = "",
    ) -> dict:
        updated = self.db.reset_warming_time(
            int(account_id),
            scan_qr_mode=scan_qr_mode,
            reset_date=reset_date,
        )
        if not updated:
            return {"ok": False, "error": "账号不存在"}
        return {"ok": True, "account": updated}

    def sync_google_time(self, proxy_url: str = "") -> dict:
        res = sync_google_time_offset(proxy_url=proxy_url)
        if res.get("ok"):
            self.save_settings({"totp_offset_seconds": int(res.get("offset_seconds") or 0)})
        return res

    def list_backups(self) -> dict:
        return {"ok": True, "backups": self.db.list_backups()}

    def create_backup(self, reason: str = "manual") -> dict:
        return self.db.create_rolling_backup(reason=reason, max_keep=15, min_interval_minutes=0)

    def restore_backup(self, filename: str) -> dict:
        return self.db.restore_backup(filename)

    def inspect_rt_json(self, raw_text: str) -> dict:
        return inspect_rt_json(raw_text)

    def save_account_rt(self, account_id: int, raw_rt_text: str) -> dict:
        inspected = inspect_rt_json(raw_rt_text)
        if not inspected.get("ok") or inspected.get("is_array"):
            return {"ok": False, "error": inspected.get("error") or "请提供单个有效的 RT JSON 或 Refresh Token"}
        existing = self.db.get_account(int(account_id))
        if not existing:
            return {"ok": False, "error": "账号不存在"}
        cf_patch = {
            "rt_json": inspected["normalized_json"],
            "rt_refresh_token": inspected.get("refresh_token", ""),
            "rt_access_token": inspected.get("access_token", ""),
            "rt_email": inspected.get("email", ""),
            "rt_project_id": inspected.get("project_id", ""),
            "rt_status": "active",
        }
        updated = self.db.update_account(int(account_id), {"custom_fields": cf_patch})
        return {"ok": True, "account": updated}

    def batch_import_rt_json(self, raw_text: str) -> dict:
        inspected = inspect_rt_json(raw_text)
        if not inspected.get("ok"):
            return {"ok": False, "error": inspected.get("error") or "未解析出有效的 RT 凭据"}
        items = inspected["items"] if inspected.get("is_array") else [json.loads(inspected["normalized_json"])]

        existing_google = {
            normalize_gmail_address(acc.get("account") or ""): acc
            for acc in self.db.list_accounts(account_type="Google")
        }
        imported = 0
        updated = 0
        skipped = 0
        for obj in items:
            if not isinstance(obj, dict):
                continue
            item_json = json.dumps(obj, ensure_ascii=False)
            check = inspect_rt_json(item_json)
            if not check.get("ok") or check.get("is_array"):
                skipped += 1
                continue
            email = str(check.get("email") or "").strip()
            norm = normalize_gmail_address(email) if email else ""
            if norm and norm in existing_google:
                res = self.save_account_rt(existing_google[norm]["id"], check["normalized_json"])
                if res.get("ok"):
                    updated += 1
                else:
                    skipped += 1
            elif email:
                created = self.db.add_account({
                    "account_type": "Google",
                    "account": email,
                    "custom_fields": {
                        "rt_json": check["normalized_json"],
                        "rt_refresh_token": check.get("refresh_token", ""),
                        "rt_access_token": check.get("access_token", ""),
                        "rt_email": email,
                        "rt_project_id": check.get("project_id", ""),
                        "rt_status": "active",
                    },
                })
                existing_google[norm] = created
                imported += 1
            else:
                skipped += 1
        return {
            "ok": (imported + updated) > 0,
            "imported": imported,
            "updated": updated,
            "skipped": skipped,
            "error": "" if (imported + updated) > 0 else "RT JSON 缺少 email 字段且未指定绑定账号",
        }

    def export_rt_json(self, account_ids: list[int] | None = None) -> dict:
        wanted = {int(i) for i in account_ids} if account_ids else None
        accounts = self.db.list_accounts(account_type="Google")
        exported = []
        for acc in accounts:
            if wanted is not None and acc["id"] not in wanted:
                continue
            cf = acc.get("custom_fields") or {}
            raw_rt = str(cf.get("rt_json") or "").strip()
            if not raw_rt:
                continue
            try:
                obj = json.loads(raw_rt)
            except Exception:
                obj = {"refresh_token": cf.get("rt_refresh_token", ""), "email": acc.get("account", "")}
            if isinstance(obj, dict):
                obj.setdefault("email", acc.get("account", ""))
                exported.append(obj)
        return {
            "ok": len(exported) > 0,
            "count": len(exported),
            "json_text": json.dumps(exported, ensure_ascii=False, indent=2),
            "error": "" if exported else "没有可导出的 RT 凭据",
        }

    def probe_account_gcp_rt(self, account_id: int) -> dict:
        acc = self.db.get_account(int(account_id))
        if not acc:
            return {"ok": False, "error": "账号不存在"}
        res = probe_antigravity_rt(acc)
        cf_patch = {
            "rt_status": res.get("rt_status", acc.get("rt_status", "none")),
            "gcp_status": res.get("gcp_status", acc.get("gcp_status", "none")),
            "rt_last_checked": res.get("checked_at", ""),
            "rt_last_error": res.get("error", ""),
        }
        if res.get("subscription_plan"):
            cf_patch["subscription_plan"] = res["subscription_plan"]
        if res.get("project_id"):
            cf_patch["rt_project_id"] = res["project_id"]
        if res.get("validation_url") is not None:
            cf_patch["rt_validation_url"] = res.get("validation_url") or ""
        if res.get("access_token"):
            cf_patch["rt_access_token"] = res["access_token"]
        if res.get("expiry_timestamp"):
            cf_patch["rt_expiry"] = res["expiry_timestamp"]

        update_payload: dict = {"custom_fields": cf_patch}
        if res.get("antigravity_tier") in ("蓝色Pro", "金色Pro"):
            update_payload["antigravity_tier"] = res["antigravity_tier"]
        updated = self.db.update_account(int(account_id), update_payload)
        return {**res, "account": updated}

    def batch_probe_gcp_rt(self, account_ids: list[int] | None = None) -> dict:
        wanted = {int(i) for i in account_ids} if account_ids else None
        google_accs = self.db.list_accounts(account_type="Google")
        targets = [
            acc for acc in google_accs
            if acc.get("has_rt") and (wanted is None or acc["id"] in wanted)
        ]
        if not targets:
            return {"ok": False, "total": 0, "active": 0, "results": [], "error": "没有配置 RT 凭据的 Google 账号"}

        with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
            probe_results = list(pool.map(probe_antigravity_rt, targets))

        results = []
        active_cnt = 0
        for acc, res in zip(targets, probe_results):
            cf_patch = {
                "rt_status": res.get("rt_status", acc.get("rt_status", "none")),
                "gcp_status": res.get("gcp_status", acc.get("gcp_status", "none")),
                "rt_last_checked": res.get("checked_at", ""),
                "rt_last_error": res.get("error", ""),
            }
            if res.get("subscription_plan"):
                cf_patch["subscription_plan"] = res["subscription_plan"]
            if res.get("project_id"):
                cf_patch["rt_project_id"] = res["project_id"]
            if res.get("validation_url") is not None:
                cf_patch["rt_validation_url"] = res.get("validation_url") or ""
            if res.get("access_token"):
                cf_patch["rt_access_token"] = res["access_token"]
            if res.get("expiry_timestamp"):
                cf_patch["rt_expiry"] = res["expiry_timestamp"]
            up: dict = {"custom_fields": cf_patch}
            if res.get("antigravity_tier") in ("蓝色Pro", "金色Pro"):
                up["antigravity_tier"] = res["antigravity_tier"]
            self.db.update_account(acc["id"], up)
            if res.get("rt_status") == "active":
                active_cnt += 1
            results.append({"id": acc["id"], "account": acc["account"], **res})
        return {"ok": True, "total": len(targets), "active": active_cnt, "results": results}

    def inject_rt_to_antigravity(self, account_id: int) -> dict:
        acc = self.db.get_account(int(account_id))
        if not acc:
            return {"ok": False, "error": "账号不存在"}
        return inject_rt_to_antigravity_client(acc)

    def start_builtin_oauth(
        self,
        account_id: int,
        launch_isolated: bool = True,
        pure_native: bool = True,
    ) -> dict:
        acc = self.db.get_account(int(account_id))
        if not acc:
            return {"ok": False, "error": "账号不存在"}
        proxy = str((acc.get("fp_config") or {}).get("proxy") or "").strip()
        sess = self._oauth.start_session(
            account_id=int(account_id),
            login_hint=acc.get("account", ""),
            proxy_url=proxy,
        )
        if launch_isolated and sess.get("ok"):
            launch_res = self.launch_browser(
                int(account_id),
                target_url=sess["auth_url"],
                pure_native=bool(pure_native),
            )
            sess["browser_launch"] = launch_res
        return sess

    def get_builtin_oauth_status(self, session_id: str) -> dict:
        return self._oauth.get_status(session_id)

    def cancel_builtin_oauth(self, session_id: str) -> dict:
        return self._oauth.cancel_session(session_id)

    def probe_ai_account(self, account_id: int) -> dict:
        acc = self.db.get_account(int(account_id))
        if not acc:
            return {"ok": False, "error": "账号不存在"}
        res = probe_ai_credential(acc)
        cf_patch = {
            "ai_last_checked": res.get("checked_at", ""),
            "ai_models_count": res.get("models_count", 0),
            "ai_last_error": res.get("error", ""),
        }
        if res.get("ok") and "API" in str(res.get("provider") or "") and (acc.get("openai_plan") or "free") == "free":
            cf_patch["openai_plan"] = "api"
        updated = self.db.update_account(int(account_id), {"custom_fields": cf_patch})
        return {**res, "account": updated}

    def reset_rejected_session(self, account_id: int) -> dict:
        return reset_rejected_login_session(self.data_dir, int(account_id))

    def probe_account_proxy_geo(self, account_id: int, auto_apply_tz: bool = True) -> dict:
        acc = self.db.get_account(int(account_id))
        if not acc:
            return {"ok": False, "error": "账号不存在"}
        proxy = str((acc.get("fp_config") or {}).get("proxy") or "").strip()
        res = probe_proxy_geo(proxy)
        if res.get("ok") and auto_apply_tz and res.get("timezone"):
            updated = self.db.update_account(
                int(account_id),
                {
                    "timezone": res["timezone"],
                    "locale": res.get("locale", ""),
                    "accept_languages": res.get("accept_languages", ""),
                },
            )
            return {**res, "account": updated}
        return res

    def get_appeal_channels(self) -> dict:
        return {"ok": True, "channels": GCP_APPEAL_CHANNELS}
