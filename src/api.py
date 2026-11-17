import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from src.browser_service import launch_isolated_browser
from src.db_service import DatabaseService, compute_usage_info
from src.github_mail import fetch_latest_mail_code
from src.parser import parse_account_text, parse_domain_email_file, parse_domain_email_text
from src.proxy_service import detect_proxy_environment
from src.totp_service import generate_totp, mask_secret, normalize_secret
from src.workbuddy import WorkBuddyQueue


DEFAULT_SETTINGS = {
    "theme": "dark",
    "view_mode": "compact",
    "default_account_type": "ChatGPT",
    "default_device_tag": "主机",
    "totp_interval": 30,
    "domain_email_login_url": "https://mail.ggb6.com",
    "last_filter_type": "All",
}


class AccountApi:
    def __init__(self, data_dir: str | Path, auto_sync_account_data: bool = False):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = DatabaseService(self.data_dir / "accounts.db")
        self._workbuddy = WorkBuddyQueue(self.db, self.data_dir)
        self.settings_path = self.data_dir / "settings.json"
        self._ensure_settings()
        self.auto_sync_result: dict | None = None
        if auto_sync_account_data:
            self.auto_sync_result = self._auto_sync_account_data_silent()

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
        accounts = self.db.list_accounts(
            account_type=account_type,
            search=search,
            status=status_filter,
            health_status=health_filter,
            antigravity_tier=ag_filter,
            device_tag=device_filter,
        )
        stats = self.db.get_stats()
        settings = self._ensure_settings()
        dicts = self.db.list_system_dicts()
        attributes = self.db.list_attributes()
        now = int(time.time())
        return {
            "accounts": accounts,
            "stats": stats,
            "settings": settings,
            "dicts": dicts,
            "attributes": attributes,
            "workbuddy": self._workbuddy.snapshot(),
            "node_code_stats": self.db.get_node_code_stats(),
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
                        "totp": generate_totp(cleaned),
                    },
                }

        for rec in records:
            rec["secret_key"] = normalize_secret(rec.get("secret_key", ""))
            rec["initial_secret_key"] = normalize_secret(rec.get("initial_secret_key", ""))
            rec["totp"] = generate_totp(rec.get("secret_key", ""))
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
        return {
            "cleaned_secret": cleaned,
            "masked_secret": mask_secret(cleaned),
            "totp": generate_totp(cleaned),
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

    def launch_browser(self, account_id: int) -> dict:
        import src.browser_service as bs
        account = self.db.get_account(int(account_id))
        if not account:
            return {"ok": False, "error": "账号不存在"}
        if account["account_type"] == "域名邮箱":
            fp_cfg = account.get("fp_config") or {}
            if not (fp_cfg.get("node_locked") and str(fp_cfg.get("proxy_node") or "").strip()):
                self.allocate_node_for_domain_email(int(account_id))
                account = self.db.get_account(int(account_id)) or account
            result = bs.launch_isolated_browser(self.data_dir, account)
            result.setdefault("fp_config", account.get("fp_config") or {})
            result.setdefault(
                "profile_dir",
                str(self.data_dir / "browsers" / f"acc_{int(account_id)}"),
            )
            return result
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

    def batch_import(self, raw_text: str, account_type: str = "") -> dict:
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
        count = self.db.batch_add_accounts(records)
        return {"ok": True, "imported": count}

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
                from src.sidecar_proxy import shutdown_sidecar_proxy
                shutdown_sidecar_proxy()
            except Exception:
                pass

    def delete_account(self, account_id: int) -> dict:
        deleted = self.db.delete_account(int(account_id))
        return {"ok": deleted}

    def refresh_totp(self) -> dict:
        now = int(time.time())
        codes = self.db.refresh_totp_codes()
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
        self.settings_path.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return current
