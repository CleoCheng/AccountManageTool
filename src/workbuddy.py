import json
import re
import secrets
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from src.browser_service import generate_fingerprint_config
from src.github_mail import read_github_code
from src.proxy_service import detect_proxy_environment
from src.workbuddy_browser import WorkBuddyBrowser


STEPS = ("github", "email", "workbuddy", "region", "greeting")
STEP_LABELS = {
    "github": "GitHub注册",
    "email": "邮箱验证",
    "workbuddy": "WorkBuddy授权",
    "region": "完善地区",
    "greeting": "发送招呼",
}
STEP_DELAY = 1
DEFAULT_NODE_QUOTA = 2
ROUND_SIZE = 2
_ACTIVE = "status IN ('ready', 'paused', 'verified')"


def _username(email):
    local = email.split("@", 1)[0]
    prefix = re.sub(r"[^A-Za-z0-9]+", "-", local).strip("-")[:35].rstrip("-") or "employee"
    return f"{prefix}{secrets.randbelow(10000):04d}"


def _sleep_step_delay():
    if STEP_DELAY <= 0:
        return
    if STEP_DELAY == 1:
        time.sleep(1.0 + secrets.randbelow(1001) / 1000.0)
    else:
        time.sleep(STEP_DELAY)


class WorkBuddyQueue:
    def __init__(self, accounts, data_dir: Path, discover_nodes_fn=None):
        self.accounts = accounts
        # Browser and mail operations can take seconds. Keep their lock off the vault database.
        self.path = data_dir / "workbuddy.sqlite3"
        self.browser = WorkBuddyBrowser(data_dir / "workbuddy_sessions")
        self.discover_nodes_fn = discover_nodes_fn or detect_proxy_environment
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workbuddy")
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch TEXT NOT NULL, account_id INTEGER NOT NULL, email TEXT NOT NULL COLLATE NOCASE,
                username TEXT NOT NULL, greeting TEXT NOT NULL, step TEXT NOT NULL,
                greeting_attempted INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ready', revision INTEGER NOT NULL DEFAULT 0,
                region TEXT NOT NULL DEFAULT '',
                proxy_url TEXT NOT NULL DEFAULT '',
                proxy_node TEXT NOT NULL DEFAULT '',
                fp_json TEXT NOT NULL DEFAULT '',
                oauth_state TEXT NOT NULL DEFAULT '',
                credential_json TEXT NOT NULL DEFAULT '',
                round_no INTEGER NOT NULL DEFAULT 1,
                round_slot INTEGER NOT NULL DEFAULT 0,
                started_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, UNIQUE(batch, email)
            )""")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
            migrations = {
                "greeting_attempted": "ALTER TABLE tasks ADD COLUMN greeting_attempted INTEGER NOT NULL DEFAULT 0",
                "proxy_url": "ALTER TABLE tasks ADD COLUMN proxy_url TEXT NOT NULL DEFAULT ''",
                "proxy_node": "ALTER TABLE tasks ADD COLUMN proxy_node TEXT NOT NULL DEFAULT ''",
                "fp_json": "ALTER TABLE tasks ADD COLUMN fp_json TEXT NOT NULL DEFAULT ''",
                "oauth_state": "ALTER TABLE tasks ADD COLUMN oauth_state TEXT NOT NULL DEFAULT ''",
                "credential_json": "ALTER TABLE tasks ADD COLUMN credential_json TEXT NOT NULL DEFAULT ''",
                "round_no": "ALTER TABLE tasks ADD COLUMN round_no INTEGER NOT NULL DEFAULT 1",
                "round_slot": "ALTER TABLE tasks ADD COLUMN round_slot INTEGER NOT NULL DEFAULT 0",
            }
            for col_name, ddl in migrations.items():
                if col_name not in columns:
                    conn.execute(ddl)

            conn.execute("""CREATE TABLE IF NOT EXISTS node_quotas (
                node_name TEXT PRIMARY KEY,
                proxy_url TEXT NOT NULL DEFAULT '',
                quota_limit INTEGER NOT NULL DEFAULT 2,
                unlocked INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0
            )""")
            nq_cols = {row["name"] for row in conn.execute("PRAGMA table_info(node_quotas)")}
            if "blocked" not in nq_cols:
                conn.execute("ALTER TABLE node_quotas ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
            conn.execute("""CREATE TABLE IF NOT EXISTS node_registrations (
                node_name TEXT NOT NULL,
                email TEXT NOT NULL COLLATE NOCASE,
                status TEXT NOT NULL DEFAULT 'reserved',
                created_at INTEGER NOT NULL,
                PRIMARY KEY (node_name, email)
            )""")

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=3)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _current(conn):
        return conn.execute(f"SELECT * FROM tasks WHERE {_ACTIVE} ORDER BY round_no, round_slot, id LIMIT 1").fetchone()

    @staticmethod
    def _current_round_tasks(conn):
        first = conn.execute(f"SELECT * FROM tasks WHERE {_ACTIVE} ORDER BY round_no, round_slot, id LIMIT 1").fetchone()
        if not first:
            return []
        return conn.execute(
            f"SELECT * FROM tasks WHERE batch = ? AND round_no = ? AND {_ACTIVE} ORDER BY round_slot, id",
            (first["batch"], first["round_no"]),
        ).fetchall()

    def _node_usage_map(self, conn) -> dict[str, dict]:
        counts: dict[str, int] = {}
        for row in conn.execute(
            "SELECT node_name, COUNT(DISTINCT email) AS cnt FROM node_registrations GROUP BY node_name"
        ):
            counts[row["node_name"]] = int(row["cnt"])
        meta: dict[str, dict] = {}
        for row in conn.execute("SELECT * FROM node_quotas ORDER BY node_name"):
            name = row["node_name"]
            used = counts.get(name, 0)
            limit = int(row["quota_limit"] or DEFAULT_NODE_QUOTA)
            unlocked = bool(row["unlocked"])
            blocked = bool(row["blocked"]) if "blocked" in row.keys() else False
            meta[name] = {
                "node_name": name,
                "proxy_url": row["proxy_url"] or "",
                "used_count": used,
                "quota_limit": limit,
                "unlocked": unlocked,
                "blocked": blocked,
                "exhausted": (blocked or used >= limit) and (not unlocked),
            }
        for name, used in counts.items():
            if name not in meta:
                meta[name] = {
                    "node_name": name,
                    "proxy_url": "",
                    "used_count": used,
                    "quota_limit": DEFAULT_NODE_QUOTA,
                    "unlocked": False,
                    "blocked": False,
                    "exhausted": used >= DEFAULT_NODE_QUOTA,
                }
        return meta

    @staticmethod
    def _public_task_dict(row) -> dict:
        d = dict(row)
        raw_cred = d.pop("credential_json", "") or ""
        has_cred = False
        has_refresh = False
        if raw_cred:
            try:
                cobj = json.loads(raw_cred)
                if isinstance(cobj, dict) and str(cobj.get("accessToken") or "").count(".") == 2:
                    has_cred = True
                    has_refresh = bool(cobj.get("refreshToken"))
            except Exception:
                pass
        d["has_credential"] = has_cred
        d["has_refresh_token"] = has_refresh
        raw_fp = d.pop("fp_json", "") or ""
        fp_summary = ""
        if raw_fp:
            try:
                fobj = json.loads(raw_fp)
                if isinstance(fobj, dict):
                    fp_summary = f"{fobj.get('gpu_short', 'GPU')} · {fobj.get('screen', '1920x1080')}"
            except Exception:
                pass
        d["fp_summary"] = fp_summary
        return d

    def snapshot(self):
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            rows = conn.execute("""SELECT * FROM tasks
                WHERE batch = (SELECT batch FROM tasks ORDER BY id DESC LIMIT 1) ORDER BY round_no, round_slot, id""")
            items = [self._public_task_dict(row) for row in rows]
            active_items = [item for item in items if item["status"] in ("ready", "paused", "verified")]
            active_round = active_items[0]["round_no"] if active_items else 0
            round_items = [item for item in items if active_round and item.get("round_no") == active_round]
            current = active_items[0] if active_items else None
            total_rounds = max((int(item.get("round_no") or 1) for item in items), default=0)
            completed = {row["email"].lower(): row["username"] for row in conn.execute(
                "SELECT email, username FROM tasks WHERE status = 'done'")}
            quotas = list(self._node_usage_map(conn).values())
        return {
            "items": items,
            "current": current,
            "round_no": active_round,
            "total_rounds": total_rounds,
            "round_items": round_items,
            "completed": completed,
            "node_quotas": quotas,
        }

    def get_node_quotas(self) -> dict:
        try:
            env = self.discover_nodes_fn(account_type="WorkBuddy") or {}
        except Exception:
            env = {}
        default_port = ""
        ports = env.get("ports") or []
        if ports and isinstance(ports[0], dict):
            default_port = str(ports[0].get("url") or "")
        env_nodes = env.get("nodes") or []

        with closing(self._connect()) as conn:
            usage_map = self._node_usage_map(conn)

        merged: list[dict] = []
        seen: set[str] = set()
        for n in env_nodes:
            name = str(n.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            info = usage_map.get(name) or {
                "node_name": name,
                "proxy_url": default_port,
                "used_count": 0,
                "quota_limit": DEFAULT_NODE_QUOTA,
                "unlocked": False,
                "blocked": False,
                "exhausted": False,
            }
            merged.append({
                **info,
                "proxy_url": info.get("proxy_url") or default_port,
                "region": n.get("region") or "",
                "delay": n.get("delay"),
            })
        for name, info in usage_map.items():
            if name not in seen:
                merged.append({**info, "region": "", "delay": None})
        return {
            "ok": True,
            "client_name": env.get("client_name") or "",
            "controller_online": bool(env.get("controller_online")),
            "default_proxy_url": default_port,
            "ports": ports,
            "nodes": merged,
        }

    def set_node_unlock(self, node_name: str, unlocked: bool = True, proxy_url: str = "") -> dict:
        name = (node_name or "").strip()
        if not name:
            return {"ok": False, "error": "节点名称不能为空"}
        now = int(time.time())
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM node_quotas WHERE node_name = ?", (name,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE node_quotas SET unlocked = ?, blocked = CASE WHEN ? = 1 THEN 0 ELSE blocked END, "
                    "proxy_url = COALESCE(NULLIF(?, ''), proxy_url), updated_at = ? WHERE node_name = ?",
                    (1 if unlocked else 0, 1 if unlocked else 0, (proxy_url or "").strip(), now, name),
                )
            else:
                conn.execute(
                    "INSERT INTO node_quotas (node_name, proxy_url, quota_limit, unlocked, blocked, updated_at) VALUES (?, ?, ?, ?, 0, ?)",
                    (name, (proxy_url or "").strip(), DEFAULT_NODE_QUOTA, 1 if unlocked else 0, now),
                )
        return self.get_node_quotas()

    def reset_node_quota(self, node_name: str) -> dict:
        name = (node_name or "").strip()
        if not name:
            return {"ok": False, "error": "节点名称不能为空"}
        now = int(time.time())
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            active_emails = {
                r["email"].casefold()
                for r in conn.execute(
                    f"SELECT email FROM tasks WHERE proxy_node = ? AND {_ACTIVE}",
                    (name,),
                ).fetchall()
            }
            for row in conn.execute("SELECT email FROM node_registrations WHERE node_name = ?", (name,)).fetchall():
                if row["email"].casefold() not in active_emails:
                    conn.execute(
                        "DELETE FROM node_registrations WHERE node_name = ? AND email = ?",
                        (name, row["email"]),
                    )
            conn.execute(
                "INSERT INTO node_quotas (node_name, proxy_url, quota_limit, unlocked, blocked, updated_at) VALUES (?, '', ?, 0, 0, ?) "
                "ON CONFLICT(node_name) DO UPDATE SET unlocked = 0, blocked = 0, updated_at = excluded.updated_at",
                (name, DEFAULT_NODE_QUOTA, now),
            )
        return self.get_node_quotas()

    def _run(self, fn, *args):
        try:
            result = self.worker.submit(fn, *args).result()
            return {"ok": True, "queue": self.snapshot(), **(result or {})}
        except ValueError as exc:
            error = str(exc)
        except sqlite3.Error:
            error = "任务库暂不可写，请检查是否有其他窗口正在操作，然后重试。"
        except Exception:
            # Driver exceptions can contain form contents or OAuth redirect URLs.
            error = "操作未完成，当前进度已保留。请检查工作窗口或网络后重试。"
        return {"ok": False, "error": error, "queue": self.snapshot()}

    def start(self, account_ids, proxy_url: str = "", proxy_node: str = ""):
        return self._run(self._start, account_ids, proxy_url, proxy_node)

    def _reserve_node_slot(
        self,
        conn,
        email: str,
        node_name: str,
        px_url: str,
        default_proxy_url: str,
        usage_map: dict[str, dict],
        is_same_email: bool,
    ) -> tuple[str, str]:
        now = int(time.time())
        conn.execute(
            "INSERT OR IGNORE INTO node_registrations (node_name, email, status, created_at) VALUES (?, ?, 'reserved', ?)",
            (node_name, email, now),
        )
        conn.execute(
            "INSERT INTO node_quotas (node_name, proxy_url, quota_limit, unlocked, updated_at) VALUES (?, ?, ?, 0, ?) "
            "ON CONFLICT(node_name) DO UPDATE SET proxy_url = COALESCE(NULLIF(excluded.proxy_url, ''), node_quotas.proxy_url), updated_at = excluded.updated_at",
            (node_name, px_url, DEFAULT_NODE_QUOTA, now),
        )
        if node_name not in usage_map:
            usage_map[node_name] = {
                "node_name": node_name,
                "proxy_url": px_url,
                "used_count": 0,
                "quota_limit": DEFAULT_NODE_QUOTA,
                "unlocked": False,
            }
        if not is_same_email:
            usage_map[node_name]["used_count"] = int(usage_map[node_name]["used_count"]) + 1
        final_px = px_url or usage_map[node_name].get("proxy_url") or default_proxy_url
        return node_name, final_px

    def _allocate_round(
        self,
        conn,
        pending_entries: list[dict],
        explicit_node: str,
        explicit_proxy_url: str,
        env_nodes: list[str],
        default_proxy_url: str,
        usage_map: dict[str, dict],
    ) -> tuple[list[dict], str, str]:
        """Take 1 or 2 entries for a single registration round sharing the same subscription node."""
        first = pending_entries[0]
        second = pending_entries[1] if len(pending_entries) >= 2 else None

        def _prev_node(email: str) -> str:
            row = conn.execute(
                "SELECT node_name FROM node_registrations WHERE email = ? LIMIT 1",
                (email,),
            ).fetchone()
            return str(row["node_name"]) if row else ""

        def _free_slots(n_name: str) -> int:
            info = usage_map.get(n_name)
            if not info:
                return DEFAULT_NODE_QUOTA
            if info.get("unlocked"):
                return 999999
            if info.get("blocked"):
                return 0
            return max(0, int(info.get("quota_limit") or DEFAULT_NODE_QUOTA) - int(info.get("used_count") or 0))

        prev1 = _prev_node(first["email"])
        prev2 = _prev_node(second["email"]) if second else ""

        if explicit_node:
            pair = [first, second] if second else [first]
            chosen_px = explicit_proxy_url or default_proxy_url
            for ent in pair:
                same = (_prev_node(ent["email"]) == explicit_node)
                if not same and _free_slots(explicit_node) < 1:
                    info = usage_map[explicit_node]
                    raise ValueError(
                        f"订阅节点「{explicit_node}」已达到 {info['quota_limit']} 个账号上限或已标记受限（已用 {info['used_count']}/{info['quota_limit']}），未主动解限禁止复用该节点！"
                    )
                _, chosen_px = self._reserve_node_slot(
                    conn, ent["email"], explicit_node, chosen_px, default_proxy_url, usage_map, same
                )
            return pair, explicit_node, chosen_px

        if prev1 and not ((usage_map.get(prev1) or {}).get("blocked") and not (usage_map.get(prev1) or {}).get("unlocked")):
            px = explicit_proxy_url or (usage_map.get(prev1) or {}).get("proxy_url") or default_proxy_url
            _, px = self._reserve_node_slot(conn, first["email"], prev1, px, default_proxy_url, usage_map, True)
            if second and (prev2 == prev1 or (not prev2 and _free_slots(prev1) >= 1)):
                _, px = self._reserve_node_slot(
                    conn, second["email"], prev1, px, default_proxy_url, usage_map, prev2 == prev1
                )
                return [first, second], prev1, px
            return [first], prev1, px

        if env_nodes:
            blocked_regions = {
                self._node_region_key(k)
                for k, v in usage_map.items()
                if isinstance(v, dict) and v.get("blocked") and not v.get("unlocked")
            }
            ordered_nodes = sorted(
                env_nodes,
                key=lambda c: 1 if self._node_region_key(c) in blocked_regions else 0,
            )
            can_pair = bool(second and not prev2)
            chosen_node = ""
            pair: list[dict] = []
            if can_pair:
                # Prefer a clean non-blocked node with 2 full quota slots available so both browsers share 1 node
                for cand in ordered_nodes:
                    info = usage_map.get(cand)
                    if info and info.get("blocked") and not info.get("unlocked"):
                        continue
                    used = int((info or {}).get("used_count") or 0)
                    limit = int((info or {}).get("quota_limit") or DEFAULT_NODE_QUOTA)
                    if limit - used >= ROUND_SIZE:
                        chosen_node = cand
                        pair = [first, second]
                        break
                if not chosen_node:
                    for cand in ordered_nodes:
                        info = usage_map.get(cand)
                        if info and info.get("unlocked"):
                            chosen_node = cand
                            pair = [first, second]
                            break
            if not chosen_node:
                for cand in ordered_nodes:
                    if _free_slots(cand) >= 1:
                        chosen_node = cand
                        pair = [first]
                        break
            if not chosen_node:
                raise ValueError(
                    "所有可用订阅节点均已达到每节点 2 个账号的注册上限（或已标记受限）！请先在「节点配额与解限」中主动解限，或切换新订阅节点。"
                )
            chosen_px = explicit_proxy_url or default_proxy_url
            for ent in pair:
                _, chosen_px = self._reserve_node_slot(
                    conn, ent["email"], chosen_node, chosen_px, default_proxy_url, usage_map, False
                )
            return pair, chosen_node, chosen_px

        if explicit_proxy_url:
            synthetic_node = f"代理端口({explicit_proxy_url})"
            pair = [first, second] if second else [first]
            for ent in pair:
                same = (_prev_node(ent["email"]) == synthetic_node)
                if not same and _free_slots(synthetic_node) < 1:
                    info = usage_map[synthetic_node]
                    raise ValueError(
                        f"代理节点「{synthetic_node}」已达到 {info['quota_limit']} 个账号上限，未主动解限禁止复用。"
                    )
                self._reserve_node_slot(
                    conn, ent["email"], synthetic_node, explicit_proxy_url, default_proxy_url, usage_map, same
                )
            return pair, synthetic_node, explicit_proxy_url

        pair = [first, second] if second else [first]
        return pair, "", ""

    @staticmethod
    def _healthy_env_nodes(env: dict) -> list[str]:
        raw = env.get("nodes") or []
        norm = []
        for n in raw:
            if isinstance(n, str) and n.strip():
                norm.append({"name": n.strip()})
            elif isinstance(n, dict) and str(n.get("name") or "").strip():
                norm.append(n)
        ranked = sorted(norm, key=lambda n: 1 if n.get("timed_out") else 0)
        return [str(n["name"]).strip() for n in ranked]

    @staticmethod
    def _node_region_key(node_name: str) -> str:
        raw = str(node_name or "").strip()
        if not raw:
            return ""
        if "|" in raw:
            return raw.split("|", 1)[0].strip().casefold()
        m = re.match(r"^([^\d\-#]+)", raw)
        return ((m.group(1).strip().rstrip("_") if m else raw[:6])).casefold()

    def _sync_account_step(
        self,
        account_id: int,
        step: str,
        username: str = "",
        proxy_url: str = "",
        proxy_node: str = "",
    ) -> None:
        if hasattr(self.accounts, "update_workbuddy_progress"):
            try:
                self.accounts.update_workbuddy_progress(
                    account_id,
                    step=step,
                    step_label=STEP_LABELS.get(step, step),
                    username=username,
                    proxy_url=proxy_url,
                    proxy_node=proxy_node,
                )
            except Exception:
                pass

    def _preflight_and_prepare_round_node(self, conn, round_tasks: list) -> list:
        if getattr(self.browser, "_is_real_browser", False) is not True:
            return round_tasks
        if not round_tasks:
            return []

        first = round_tasks[0]
        try:
            env = self.discover_nodes_fn(account_type="WorkBuddy") or {}
        except Exception:
            env = {}

        ports = env.get("ports") or []
        sidecar_px = str(ports[0].get("url") or "").strip() if (ports and isinstance(ports[0], dict)) else ""
        cur_px = str(first["proxy_url"] or "").strip()
        now = int(time.time())

        if sidecar_px and (not cur_px or ":17898" in cur_px or ":7897" in cur_px):
            cur_px = sidecar_px
            for t in round_tasks:
                conn.execute(
                    "UPDATE tasks SET proxy_url = ?, updated_at = ? WHERE id = ?",
                    (cur_px, now, t["id"]),
                )
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")

        cur_node = str(first["proxy_node"] or "").strip()
        if cur_node:
            from src.proxy_service import activate_proxy_node
            env_nodes = self._healthy_env_nodes(env)
            max_preflight_tries = min(12, max(4, len(env_nodes)))
            for _ in range(max_preflight_tries):
                try:
                    sw = activate_proxy_node("WorkBuddy", cur_px, cur_node, preflight=True)
                except Exception:
                    sw = None

                if (
                    isinstance(sw, dict)
                    and sw.get("ok")
                    and not sw.get("restricted")
                    and sw.get("reachable", True) is not False
                ):
                    if sw.get("proxy_url") and sw["proxy_url"] != cur_px:
                        cur_px = sw["proxy_url"]
                        for t in round_tasks:
                            conn.execute(
                                "UPDATE tasks SET proxy_url = ?, updated_at = ? WHERE id = ?",
                                (cur_px, now, t["id"]),
                            )
                        conn.commit()
                        conn.execute("BEGIN IMMEDIATE")
                    break

                conn.execute(
                    "INSERT INTO node_quotas (node_name, proxy_url, quota_limit, unlocked, blocked, updated_at) "
                    "VALUES (?, ?, ?, 0, 1, ?) "
                    "ON CONFLICT(node_name) DO UPDATE SET unlocked = 0, blocked = 1, updated_at = excluded.updated_at",
                    (cur_node, cur_px, DEFAULT_NODE_QUOTA, now),
                )
                for t in round_tasks:
                    self._release_unused_node_slot_if_untouched(conn, t)

                usage_map = self._node_usage_map(conn)
                needed = len(round_tasks)
                candidates = [c for c in env_nodes if c != cur_node]
                bad_region = self._node_region_key(cur_node)
                blocked_regions = {
                    self._node_region_key(k)
                    for k, v in usage_map.items()
                    if isinstance(v, dict) and v.get("blocked") and not v.get("unlocked")
                }
                if bad_region:
                    blocked_regions.add(bad_region)
                candidates.sort(
                    key=lambda c: (
                        1 if self._node_region_key(c) in blocked_regions else 0,
                        1 if (bad_region and self._node_region_key(c) == bad_region) else 0,
                    )
                )
                new_node = ""
                for cand in candidates:
                    if not cand:
                        continue
                    info = usage_map.get(cand)
                    if info and info.get("blocked") and not info.get("unlocked"):
                        continue
                    used = int((info or {}).get("used_count") or 0)
                    limit = int((info or {}).get("quota_limit") or DEFAULT_NODE_QUOTA)
                    if (info and info.get("unlocked")) or (limit - used >= needed):
                        new_node = cand
                        break

                if not new_node:
                    break

                cur_node = new_node
                default_px = sidecar_px or cur_px
                for t in round_tasks:
                    same = bool(
                        conn.execute(
                            "SELECT 1 FROM node_registrations WHERE node_name = ? AND email = ? LIMIT 1",
                            (cur_node, t["email"]),
                        ).fetchone()
                    )
                    _, cur_px = self._reserve_node_slot(
                        conn, t["email"], cur_node, cur_px, default_px, usage_map, same
                    )
                    fresh_fp = generate_fingerprint_config(
                        t["account_id"],
                        t["email"],
                        proxy=cur_px,
                        proxy_node=cur_node,
                        node_locked=True,
                        salt=uuid4().hex,
                    )
                    conn.execute(
                        "UPDATE tasks SET proxy_node = ?, proxy_url = ?, fp_json = ?, updated_at = ? WHERE id = ?",
                        (cur_node, cur_px, json.dumps(fresh_fp, ensure_ascii=False), now, t["id"]),
                    )
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")

        task_ids = [t["id"] for t in round_tasks]
        if not task_ids:
            return []
        placeholders = ",".join("?" for _ in task_ids)
        return conn.execute(
            f"SELECT * FROM tasks WHERE id IN ({placeholders}) ORDER BY round_slot, id",
            task_ids,
        ).fetchall()

    def _start(self, account_ids, proxy_url: str = "", proxy_node: str = ""):
        if not isinstance(account_ids, list) or not account_ids or any(type(i) is not int or i < 1 for i in account_ids):
            raise ValueError("请先勾选要处理的域名邮箱。")
        explicit_px = (proxy_url or "").strip()
        explicit_nd = (proxy_node or "").strip()

        env_nodes: list[str] = []
        default_px = explicit_px
        if not explicit_nd:
            try:
                env = self.discover_nodes_fn(account_type="WorkBuddy") or {}
                env_nodes = self._healthy_env_nodes(env)
                if not default_px:
                    ports = env.get("ports") or []
                    if ports and isinstance(ports[0], dict):
                        default_px = str(ports[0].get("url") or "").strip()
            except Exception:
                env_nodes = []

        batch = uuid4().hex
        seen = set()
        queued_sync_tasks: list[tuple[int, str, str, str, str]] = []
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._current(conn):
                raise ValueError("已有未完成的批次，请继续当前任务。")
            usage_map = self._node_usage_map(conn)
            validated: list[dict] = []
            for account_id in dict.fromkeys(account_ids):
                account = self.accounts.get_account(account_id)
                if not account or account["account_type"] != "域名邮箱":
                    raise ValueError("所选记录包含已删除或已改类的邮箱，请刷新后重选。")
                email = account["account"]
                if not re.fullmatch(r"[^@\s\x00-\x1f]+@[^@\s\x00-\x1f]+", email) or not account["password"].strip():
                    raise ValueError("所选邮箱缺少有效地址或密码，请先编辑邮箱资料。")
                if email.casefold() in seen:
                    raise ValueError("所选记录含有重复的邮箱地址，请保留其中一条。")
                seen.add(email.casefold())
                last = conn.execute("SELECT * FROM tasks WHERE email = ? ORDER BY id DESC LIMIT 1", (email,)).fetchone()
                if (last and last["status"] == "done") or (account.get("custom_fields") or {}).get("wb_reg_status") == "已完成":
                    raise ValueError(f"{email} 已完成开户，请取消勾选。")
                validated.append({"account_id": account_id, "email": email, "last": last})

            idx = 0
            round_no = 1
            while idx < len(validated):
                pair, bound_node, bound_px = self._allocate_round(
                    conn,
                    pending_entries=validated[idx:],
                    explicit_node=explicit_nd,
                    explicit_proxy_url=explicit_px,
                    env_nodes=env_nodes,
                    default_proxy_url=default_px,
                    usage_map=usage_map,
                )
                for slot_idx, entry in enumerate(pair):
                    account_id = entry["account_id"]
                    email = entry["email"]
                    last = entry["last"]
                    fresh_fp = generate_fingerprint_config(
                        account_id,
                        email,
                        proxy=bound_px,
                        proxy_node=bound_node,
                        node_locked=bool(bound_node),
                        salt=uuid4().hex,
                    )
                    now = int(time.time())
                    task_username = last["username"] if last else _username(email)
                    task_step = last["step"] if last else STEPS[0]
                    conn.execute("""INSERT INTO tasks
                        (batch, account_id, email, username, greeting, greeting_attempted, step, region,
                         proxy_url, proxy_node, fp_json, oauth_state, credential_json,
                         round_no, round_slot, started_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                        batch, account_id, email,
                        task_username,
                        last["greeting"] if last else secrets.choice(("hello", "hi", "你好", "你好呀")),
                        last["greeting_attempted"] if last else 0,
                        task_step,
                        last["region"] if last else "",
                        bound_px,
                        bound_node,
                        json.dumps(fresh_fp, ensure_ascii=False),
                        last["oauth_state"] if (last and "oauth_state" in last.keys()) else "",
                        last["credential_json"] if (last and "credential_json" in last.keys()) else "",
                        round_no,
                        slot_idx,
                        last["started_at"] if last else now,
                        now,
                    ))
                    queued_sync_tasks.append((account_id, task_step, task_username, bound_px, bound_node))
                idx += len(pair)
                round_no += 1

        for acc_id, t_step, t_user, b_px, b_node in queued_sync_tasks:
            self._sync_account_step(acc_id, t_step, username=t_user, proxy_url=b_px, proxy_node=b_node)

    def action(self, task_id, revision, action, value=""):
        return self._run(self._action, task_id, revision, action, value)

    def _send_greeting(self, conn, task):
        if task["greeting_attempted"]:
            raise ValueError("已请求发送过招呼，请在对话中核对发送结果并等待回复。")
        button = self.browser.prepare_greeting(dict(task))
        # Persist the attempt before the external click. A timeout or crash
        # after dispatch is ambiguous and must not send a second greeting.
        conn.execute("UPDATE tasks SET greeting_attempted=1, revision=revision+1, updated_at=? WHERE id=?",
                     (int(time.time()), task["id"]))
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        refreshed = conn.execute(f"SELECT * FROM tasks WHERE id=? AND {_ACTIVE}", (task["id"],)).fetchone()
        if not refreshed or refreshed["revision"] != task["revision"] + 1:
            raise ValueError("任务进度已更新，请在工作窗口核对招呼。")
        try:
            button.click(timeout=4000)
        except Exception as exc:
            raise ValueError("未能确认招呼是否发出。请在工作窗口检查，必要时手动发送，再等候回复。") from exc
        return {"message": "已点击发送。请等到 WorkBuddy 回复后勾选确认。"}

    def _pause_for_challenge(self, conn, task):
        # An explicit human confirmation applies only to this step. A solved
        # widget can stay visible until the site navigates to the next page.
        if task["status"] != "verified" and self.browser.challenge_page(dict(task)):
            conn.execute("UPDATE tasks SET status='paused', revision=revision+1, updated_at=? WHERE id=?",
                         (int(time.time()), task["id"]))
            return {
                "message": "工作窗口出现人机验证或「访问暂时受限」，已自动暂停。您可在窗口完成验证后点击「恢复此步」，或点击「换新节点重开本轮」切换干净 IP。"
            }
        return None

    def _release_unused_node_slot_if_untouched(self, conn, task_row):
        # If skipped/cancelled before completing GitHub step and never completed before, release the reserved slot
        if task_row["step"] == STEPS[0] and task_row["proxy_node"]:
            done_exists = conn.execute(
                "SELECT 1 FROM tasks WHERE email = ? AND status = 'done' LIMIT 1",
                (task_row["email"],),
            ).fetchone()
            advanced_exists = conn.execute(
                "SELECT 1 FROM tasks WHERE email = ? AND step != ? LIMIT 1",
                (task_row["email"], STEPS[0]),
            ).fetchone()
            if not done_exists and not advanced_exists:
                conn.execute(
                    "DELETE FROM node_registrations WHERE node_name = ? AND email = ? AND status = 'reserved'",
                    (task_row["proxy_node"], task_row["email"]),
                )

    def _finish_round_browsers(self, conn, batch: str, round_no: int) -> tuple[bool, str]:
        """Close all browsers of the finished round, purge cache, and switch node if a next round exists."""
        all_round_rows = conn.execute(
            "SELECT id FROM tasks WHERE batch = ? AND round_no = ? ORDER BY round_slot, id",
            (batch, round_no),
        ).fetchall()
        next_round_row = conn.execute(
            f"SELECT * FROM tasks WHERE batch = ? AND round_no > ? AND {_ACTIVE} ORDER BY round_no, round_slot, id LIMIT 1",
            (batch, round_no),
        ).fetchone()
        next_px = str(next_round_row["proxy_url"] or "") if next_round_row else ""
        next_node = str(next_round_row["proxy_node"] or "") if next_round_row else ""
        for r in all_round_rows:
            self.browser.close(discard_id=r["id"])
        if hasattr(self.browser, "purge_cache_and_switch_node"):
            self.browser.purge_cache_and_switch_node(next_px, next_node)
        return bool(next_round_row), next_node

    def _read_github_code_with_retry(self, email: str, password: str, started_at: int) -> str:
        is_real = getattr(self.browser, "_is_real_browser", False) is True
        max_attempts = 8 if is_real else 1
        retry_sleep = 3.0 if is_real else 0.0
        since_ts = max(0, int(started_at) - 90)
        last_exc = None
        for attempt in range(max_attempts):
            try:
                return read_github_code(email, password, int(started_at))
            except Exception as exc:
                last_exc = exc
                if attempt > 0:
                    try:
                        return read_github_code(email, password, since_ts)
                    except Exception as exc2:
                        last_exc = exc2
            if attempt + 1 < max_attempts and retry_sleep > 0:
                time.sleep(retry_sleep)
        if last_exc:
            raise last_exc
        raise ValueError("尚未找到发给此邮箱的近期 GitHub 验证邮件，可稍后重新读取。")

    def _rotate_round_to_clean_node(
        self, conn, round_active: list, task, explicit_target: str = ""
    ) -> dict | None:
        now = int(time.time())
        bad_node = str(task["proxy_node"] or "").strip()
        try:
            env = self.discover_nodes_fn(account_type="WorkBuddy") or {}
        except Exception:
            env = {}
        env_nodes = self._healthy_env_nodes(env)
        default_px = str(task["proxy_url"] or "").strip()
        if not default_px:
            ports = env.get("ports") or []
            if ports and isinstance(ports[0], dict):
                default_px = str(ports[0].get("url") or "").strip()

        for rt in round_active:
            self._release_unused_node_slot_if_untouched(conn, rt)
        if bad_node:
            conn.execute(
                "INSERT INTO node_quotas (node_name, proxy_url, quota_limit, unlocked, blocked, updated_at) "
                "VALUES (?, ?, ?, 0, 1, ?) "
                "ON CONFLICT(node_name) DO UPDATE SET unlocked = 0, blocked = 1, updated_at = excluded.updated_at",
                (bad_node, default_px, DEFAULT_NODE_QUOTA, now),
            )
        usage_map = self._node_usage_map(conn)
        needed = len(round_active)
        explicit = explicit_target.strip()
        candidates = [explicit] if explicit else [c for c in env_nodes if c != bad_node]
        if not explicit:
            bad_region = self._node_region_key(bad_node)
            blocked_regions = {
                self._node_region_key(k)
                for k, v in usage_map.items()
                if isinstance(v, dict) and v.get("blocked") and not v.get("unlocked")
            }
            if bad_region:
                blocked_regions.add(bad_region)
            candidates.sort(
                key=lambda c: (
                    1 if self._node_region_key(c) in blocked_regions else 0,
                    1 if (bad_region and self._node_region_key(c) == bad_region) else 0,
                )
            )
        chosen_node = ""
        for cand in candidates:
            if not cand:
                continue
            info = usage_map.get(cand)
            if info and info.get("blocked") and not info.get("unlocked"):
                continue
            used = int((info or {}).get("used_count") or 0)
            limit = int((info or {}).get("quota_limit") or DEFAULT_NODE_QUOTA)
            if (info and info.get("unlocked")) or (limit - used >= needed):
                chosen_node = cand
                break
        if not chosen_node:
            return None

        chosen_px = default_px
        for rt in round_active:
            same = bool(
                conn.execute(
                    "SELECT 1 FROM node_registrations WHERE node_name = ? AND email = ? LIMIT 1",
                    (chosen_node, rt["email"]),
                ).fetchone()
            )
            _, chosen_px = self._reserve_node_slot(
                conn, rt["email"], chosen_node, chosen_px, default_px, usage_map, same
            )
            fresh_fp = generate_fingerprint_config(
                rt["account_id"],
                rt["email"],
                proxy=chosen_px,
                proxy_node=chosen_node,
                node_locked=True,
                salt=uuid4().hex,
            )
            conn.execute(
                "UPDATE tasks SET proxy_node=?, proxy_url=?, fp_json=?, status='ready', revision=revision+1, updated_at=? WHERE id=?",
                (chosen_node, chosen_px, json.dumps(fresh_fp, ensure_ascii=False), now, rt["id"]),
            )
            self._sync_account_step(
                rt["account_id"],
                rt["step"],
                username=rt["username"],
                proxy_url=chosen_px,
                proxy_node=chosen_node,
            )

        all_round_rows = conn.execute(
            "SELECT id FROM tasks WHERE batch = ? AND round_no = ? ORDER BY round_slot, id",
            (task["batch"], task["round_no"]),
        ).fetchall()
        for r in all_round_rows:
            self.browser.close(discard_id=r["id"])
        if hasattr(self.browser, "purge_cache_and_switch_node"):
            self.browser.purge_cache_and_switch_node(chosen_px, chosen_node)
        return {
            "round_transitioned": True,
            "switched_node": chosen_node,
            "auto_advanced": False,
            "paused_for_human": False,
            "message": f"检测到节点「{bad_node or '旧节点'}」触发 GitHub IP 访问暂时受限，已自动拉黑该节点、清理浏览器缓存并切换至新节点「{chosen_node}」重开本轮窗口…",
        }

    def _action(self, task_id, revision, action, value):
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            round_active = self._current_round_tasks(conn)
            task = next((t for t in round_active if t["id"] == task_id), None)
            if not task or task["revision"] != revision:
                raise ValueError("任务进度已更新，请核对当前邮箱和步骤。")
            now = int(time.time())
            if action == "cancel":
                if hasattr(self.browser, "is_ip_restricted_page"):
                    for rt in round_active:
                        bad_nd = str(rt["proxy_node"] or "").strip()
                        if bad_nd and self.browser.is_ip_restricted_page(dict(rt)) is True:
                            conn.execute(
                                "INSERT INTO node_quotas (node_name, proxy_url, quota_limit, unlocked, blocked, updated_at) "
                                "VALUES (?, ?, ?, 0, 1, ?) "
                                "ON CONFLICT(node_name) DO UPDATE SET unlocked = 0, blocked = 1, updated_at = excluded.updated_at",
                                (bad_nd, str(rt["proxy_url"] or ""), DEFAULT_NODE_QUOTA, now),
                            )
                all_round_rows = conn.execute(
                    "SELECT id FROM tasks WHERE batch = ? AND round_no = ? ORDER BY id",
                    (task["batch"], task["round_no"]),
                ).fetchall()
                for r in all_round_rows:
                    self.browser.close(discard_id=r["id"])
                if hasattr(self.browser, "purge_cache_and_switch_node"):
                    self.browser.purge_cache_and_switch_node("", "")
                active_rows = conn.execute(
                    f"SELECT * FROM tasks WHERE batch=? AND {_ACTIVE}",
                    (task["batch"],),
                ).fetchall()
                conn.execute(f"""UPDATE tasks SET status='cancelled', revision=revision+1, updated_at=?
                    WHERE batch=? AND {_ACTIVE}""", (now, task["batch"]))
                for r in active_rows:
                    self._release_unused_node_slot_if_untouched(conn, r)
                return

            if action == "skip":
                if hasattr(self.browser, "is_ip_restricted_page"):
                    for rt in round_active:
                        bad_nd = str(rt["proxy_node"] or "").strip()
                        if bad_nd and self.browser.is_ip_restricted_page(dict(rt)) is True:
                            conn.execute(
                                "INSERT INTO node_quotas (node_name, proxy_url, quota_limit, unlocked, blocked, updated_at) "
                                "VALUES (?, ?, ?, 0, 1, ?) "
                                "ON CONFLICT(node_name) DO UPDATE SET unlocked = 0, blocked = 1, updated_at = excluded.updated_at",
                                (bad_nd, str(rt["proxy_url"] or ""), DEFAULT_NODE_QUOTA, now),
                            )
                other_active = [t for t in round_active if t["id"] != task_id]
                round_transitioned = False
                if other_active:
                    self.browser.close(discard_id=task_id)
                else:
                    round_transitioned, _ = self._finish_round_browsers(conn, task["batch"], task["round_no"])
                conn.execute("UPDATE tasks SET status='skipped', revision=revision+1, updated_at=? WHERE id=?",
                             (now, task_id))
                self._release_unused_node_slot_if_untouched(conn, task)
                return {"round_transitioned": round_transitioned}

            if action == "open_round":
                sessions = getattr(self.browser, "sessions", None)
                any_session_open = (
                    isinstance(sessions, dict)
                    and any(rt["id"] in sessions for rt in round_active)
                )
                if not any_session_open:
                    round_active = self._preflight_and_prepare_round_node(conn, round_active)
                ready_list = [t for t in round_active if t["status"] == "ready"]
                if not ready_list:
                    if any(t["status"] == "verified" for t in round_active):
                        return {"opened_count": 0, "message": "人工验证已确认，正在继续全自动注册…"}
                    return {"message": "本轮窗口正在等待人工验证，请先完成验证。"}
                pairs_to_open = []
                for rt in ready_list:
                    acc = self.accounts.get_account(rt["account_id"])
                    if not acc or acc["account_type"] != "域名邮箱" or acc["account"].casefold() != rt["email"].casefold():
                        raise ValueError("所选邮箱资料已变动，请检查或跳过对应任务。")
                    pairs_to_open.append((rt, acc))
                opened_cnt = 0
                paused_cnt = 0
                for idx_rt, (rt, acc) in enumerate(pairs_to_open):
                    if (
                        rt["step"] == "github"
                        and hasattr(self.browser, "is_ip_restricted_page")
                        and self.browser.is_ip_restricted_page(dict(rt)) is True
                    ):
                        rotated = self._rotate_round_to_clean_node(conn, round_active, rt)
                        if rotated is not None:
                            return rotated
                    if self._pause_for_challenge(conn, rt):
                        paused_cnt += 1
                        continue
                    if idx_rt > 0:
                        _sleep_step_delay()
                    if rt["step"] == "email":
                        try:
                            code = read_github_code(rt["email"], acc["password"], rt["started_at"])
                            self.browser.fill_github_code(dict(rt), code)
                        except Exception:
                            self.browser.open(dict(rt), acc["password"])
                    else:
                        self.browser.open(dict(rt), acc["password"])
                    opened_cnt += 1
                    if (
                        rt["step"] == "github"
                        and hasattr(self.browser, "is_ip_restricted_page")
                        and self.browser.is_ip_restricted_page(dict(rt)) is True
                    ):
                        rotated = self._rotate_round_to_clean_node(conn, round_active, rt)
                        if rotated is not None:
                            return rotated
                    if self._pause_for_challenge(conn, rt):
                        paused_cnt += 1
                msg = f"已同时开启本轮 {opened_cnt} 个独立指纹浏览器，并按真人点击+粘贴节奏完成准备。"
                if paused_cnt:
                    msg += f"（其中 {paused_cnt} 个窗口出现人机验证已自动暂停等待人工点击）"
                return {"opened_count": opened_cnt, "message": msg}

            if action == "switch_node":
                bad_node = str(task["proxy_node"] or "").strip()
                rotated = self._rotate_round_to_clean_node(
                    conn,
                    round_active,
                    task,
                    explicit_target=value.strip() if isinstance(value, str) else "",
                )
                if rotated is None:
                    raise ValueError(
                        "未找到其他有足够配额的干净订阅节点！请在代理客户端中刷新订阅，或在「节点配额与解限」中解限其他可用节点后重试。"
                    )
                rotated["message"] = f"已拉黑受限节点「{bad_node or '旧节点'}」，清理缓存并切换至新节点「{rotated['switched_node']}」，正在重开本轮窗口…"
                return rotated

            paused = task["status"] == "paused"
            if action == "resume" and paused:
                if (
                    hasattr(self.browser, "is_ip_restricted_page")
                    and self.browser.is_ip_restricted_page(dict(task)) is True
                ):
                    rotated = self._rotate_round_to_clean_node(conn, round_active, task)
                    if rotated is not None:
                        return rotated
                conn.execute(
                    "UPDATE tasks SET status='verified', revision=revision+1, updated_at=? WHERE id=?",
                    (now, task_id),
                )
                return

            if action == "pause" and not paused:
                conn.execute(
                    "UPDATE tasks SET status='paused', revision=revision+1, updated_at=? WHERE id=?",
                    (now, task_id),
                )
                return

            if action == "check_paused":
                if not paused:
                    return {"still_paused": False}
                state = ""
                if hasattr(self.browser, "check_paused_challenge_state"):
                    try:
                        raw_state = self.browser.check_paused_challenge_state(dict(task))
                        if isinstance(raw_state, str):
                            state = raw_state.strip()
                        elif raw_state and not hasattr(raw_state, "_mock_return_value"):
                            state = str(raw_state).strip()
                    except Exception:
                        state = ""
                if not state and hasattr(self.browser, "is_ip_restricted_page") and self.browser.is_ip_restricted_page(dict(task)) is True:
                    state = "ip_restricted"
                if state == "ip_restricted":
                    rotated = self._rotate_round_to_clean_node(conn, round_active, task)
                    if rotated is not None:
                        return rotated
                    return {"still_paused": True, "ip_restricted": True}
                if state == "solved":
                    conn.execute(
                        "UPDATE tasks SET status='verified', revision=revision+1, updated_at=? WHERE id=?",
                        (now, task_id),
                    )
                    return {
                        "still_paused": False,
                        "paused_resolved": True,
                        "message": "检测到浏览器人机验证已通过，正在自动继续注册…",
                    }
                return {"still_paused": True}

            if paused:
                if action == "show":
                    self.browser.show_paused_window(dict(task))
                    return {"message": "已打开工作窗口，队列仍在等待人工验证。"}
                raise ValueError("任务正在等待人工验证。请先完成验证，再点击恢复此步。")
            account = self.accounts.get_account(task["account_id"])
            if not account or account["account_type"] != "域名邮箱" or account["account"].casefold() != task["email"].casefold():
                raise ValueError("当前邮箱已被删除或改动，请恢复原资料，或跳过这条任务。")
            if action in ("open", "mailbox", "code", "confirm", "send", "oauth", "auto_step"):
                if (
                    action == "auto_step"
                    and task["step"] == "github"
                    and hasattr(self.browser, "is_ip_restricted_page")
                    and self.browser.is_ip_restricted_page(dict(task)) is True
                ):
                    rotated = self._rotate_round_to_clean_node(conn, round_active, task)
                    if rotated is not None:
                        return rotated
                blocked = self._pause_for_challenge(conn, task)
                if blocked:
                    return blocked
                _sleep_step_delay()
            if action == "open" or (action == "mailbox" and task["step"] == "email"):
                if action == "open" and (
                    isinstance(getattr(self.browser, "sessions", None), dict)
                    and task["id"] not in self.browser.sessions
                ):
                    refreshed_round = self._preflight_and_prepare_round_node(conn, [task])
                    if refreshed_round:
                        task = refreshed_round[0]
                message = self.browser.open(dict(task), account["password"], mailbox=action == "mailbox")
                return self._pause_for_challenge(conn, task) or {"message": message}
            if action == "password" and task["step"] in ("github", "email", "workbuddy"):
                return {"password": account["password"]}
            if action == "code" and task["step"] == "email":
                code = read_github_code(task["email"], account["password"], task["started_at"])
                filled = False
                try:
                    filled = bool(self.browser.fill_github_code(dict(task), code) is True)
                except Exception:
                    filled = False
                return {"code": code, "filled": filled}
            if action == "oauth" and task["step"] in ("workbuddy", "region", "greeting"):
                task_dict = dict(task)
                if task["oauth_state"]:
                    try:
                        polled_cred = self.browser.capture_credential(task_dict, oauth_state=task["oauth_state"])
                    except Exception:
                        polled_cred = None
                    if isinstance(polled_cred, dict) and polled_cred.get("accessToken"):
                        cred_json = json.dumps(polled_cred, ensure_ascii=False)
                        conn.execute(
                            "UPDATE tasks SET credential_json=?, revision=revision+1, updated_at=? WHERE id=?",
                            (cred_json, now, task_id),
                        )
                        if hasattr(self.accounts, "save_workbuddy_credential"):
                            self.accounts.save_workbuddy_credential(task["account_id"], polled_cred)
                        return {"credential_saved": True, "message": "已成功捕获并保存 WorkBuddy2API 登录凭据！"}

                oauth_res = self.browser.start_cli_oauth(task_dict)
                if not isinstance(oauth_res, dict):
                    raise ValueError("无法启动 WorkBuddy CLI 凭据授权，请检查窗口状态。")
                new_state = str(oauth_res.get("state") or "")
                cred_obj = oauth_res.get("credential") if isinstance(oauth_res.get("credential"), dict) else None
                cred_json = json.dumps(cred_obj, ensure_ascii=False) if cred_obj else task["credential_json"]
                conn.execute(
                    "UPDATE tasks SET oauth_state=?, credential_json=?, revision=revision+1, updated_at=? WHERE id=?",
                    (new_state or task["oauth_state"], cred_json, now, task_id),
                )
                if cred_obj and hasattr(self.accounts, "save_workbuddy_credential"):
                    self.accounts.save_workbuddy_credential(task["account_id"], cred_obj)
                return {
                    "credential_saved": bool(cred_obj),
                    "message": str(oauth_res.get("message") or "已触发凭据捕获"),
                }
            if action == "send" and task["step"] == "greeting":
                return self._send_greeting(conn, task)
            if action == "auto_step":
                sessions = getattr(self.browser, "sessions", None)
                if isinstance(sessions, dict) and task["id"] not in sessions:
                    refreshed_round = self._preflight_and_prepare_round_node(conn, [task])
                    if refreshed_round:
                        task = refreshed_round[0]

                step = task["step"]
                if step == "github":
                    if hasattr(self.browser, "submit_github_signup"):
                        res = self.browser.submit_github_signup(dict(task), account["password"])
                        if not isinstance(res, dict):
                            msg = self.browser.open(dict(task), account["password"])
                            res = {"status": "next_step", "next": "email", "username": task["username"], "message": msg}
                    else:
                        msg = self.browser.open(dict(task), account["password"])
                        res = {"status": "next_step", "next": "email", "username": task["username"], "message": msg}

                    if res.get("status") == "ip_restricted" or (
                        res.get("status") not in ("challenged", "next_step")
                        and hasattr(self.browser, "is_ip_restricted_page")
                        and self.browser.is_ip_restricted_page(dict(task)) is True
                    ):
                        rotated = self._rotate_round_to_clean_node(conn, round_active, task)
                        if rotated is not None:
                            return rotated

                    raw_user = str(res.get("username") or task["username"]).strip()
                    if len(raw_user) <= 39 and re.fullmatch(r"[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*", raw_user):
                        username = raw_user
                    else:
                        username = task["username"]

                    if res.get("status") in ("challenged", "ip_restricted") or self._pause_for_challenge(conn, task):
                        conn.execute(
                            "UPDATE tasks SET status='paused', username=?, revision=revision+1, updated_at=? WHERE id=?",
                            (username, now, task_id),
                        )
                        self._sync_account_step(task["account_id"], step, username=username, proxy_url=task["proxy_url"], proxy_node=task["proxy_node"])
                        return {
                            "auto_advanced": False,
                            "paused_for_human": True,
                            "message": str(res.get("message") or "出现人机验证，已自动暂停等待人工点击验证。"),
                        }

                    if res.get("status") == "next_step":
                        next_step = str(res.get("next") or "email")
                        if next_step not in STEPS:
                            next_step = "email"
                        conn.execute(
                            "UPDATE tasks SET step=?, status='ready', username=?, revision=revision+1, updated_at=? WHERE id=?",
                            (next_step, username, now, task_id),
                        )
                        self._sync_account_step(task["account_id"], next_step, username=username, proxy_url=task["proxy_url"], proxy_node=task["proxy_node"])
                        return {
                            "auto_advanced": True,
                            "step": next_step,
                            "message": str(res.get("message") or "已自动提交 GitHub 注册，进入邮箱验证。"),
                        }

                    # Else (waiting_human)
                    conn.execute(
                        "UPDATE tasks SET username=?, revision=revision+1, updated_at=? WHERE id=?",
                        (username, now, task_id),
                    )
                    return {
                        "auto_advanced": False,
                        "paused_for_human": True,
                        "message": str(res.get("message") or "请在工作窗口核对并提交 GitHub 注册。"),
                    }

                if step == "email":
                    try:
                        code = self._read_github_code_with_retry(task["email"], account["password"], task["started_at"])
                    except ValueError as exc:
                        return {
                            "auto_advanced": False,
                            "paused_for_human": True,
                            "message": f"{exc}（可稍候点击「继续全自动注册」重试读取，或手动填入验证码）",
                        }

                    if hasattr(self.browser, "submit_github_otp"):
                        res = self.browser.submit_github_otp(dict(task), code)
                        if not isinstance(res, dict):
                            fill_res = self.browser.fill_github_code(dict(task), code)
                            filled = bool(fill_res is not False and fill_res is not None and fill_res != "")
                            res = {
                                "status": "next_step" if filled else "waiting_human",
                                "next": "workbuddy",
                                "filled": filled,
                                "message": "已自动读取并填入 GitHub 验证码。",
                            }
                    else:
                        fill_res = self.browser.fill_github_code(dict(task), code)
                        filled = bool(fill_res is not False and fill_res is not None and fill_res != "")
                        res = {
                            "status": "next_step" if filled else "waiting_human",
                            "next": "workbuddy",
                            "filled": filled,
                            "message": "已自动读取并填入 GitHub 验证码。",
                        }

                    if res.get("status") == "challenged" or self._pause_for_challenge(conn, task):
                        conn.execute(
                            "UPDATE tasks SET status='paused', revision=revision+1, updated_at=? WHERE id=?",
                            (now, task_id),
                        )
                        return {
                            "auto_advanced": False,
                            "paused_for_human": True,
                            "code": code,
                            "filled": bool(res.get("filled")),
                            "message": str(res.get("message") or "提交验证码后出现人机验证，已暂停。"),
                        }

                    if res.get("status") == "next_step":
                        conn.execute(
                            "UPDATE tasks SET step='workbuddy', status='ready', revision=revision+1, updated_at=? WHERE id=?",
                            (now, task_id),
                        )
                        self._sync_account_step(task["account_id"], "workbuddy", username=task["username"], proxy_url=task["proxy_url"], proxy_node=task["proxy_node"])
                        return {
                            "auto_advanced": True,
                            "step": "workbuddy",
                            "code": code,
                            "filled": True,
                            "message": str(res.get("message") or "GitHub 邮箱验证已自动通过，进入 WorkBuddy 授权！"),
                        }

                    return {
                        "auto_advanced": False,
                        "paused_for_human": True,
                        "code": code,
                        "filled": bool(res.get("filled")),
                        "message": str(res.get("message") or "已读取验证码，请在窗口确认验证结果。"),
                    }

                if step == "workbuddy":
                    if hasattr(self.browser, "complete_workbuddy_github_login"):
                        res = self.browser.complete_workbuddy_github_login(dict(task))
                        if not isinstance(res, dict):
                            msg = self.browser.open(dict(task), account["password"])
                            res = {"status": "next_step", "next": "region", "message": msg}
                    else:
                        msg = self.browser.open(dict(task), account["password"])
                        res = {"status": "next_step", "next": "region", "message": msg}

                    if res.get("status") == "challenged" or self._pause_for_challenge(conn, task):
                        conn.execute(
                            "UPDATE tasks SET status='paused', revision=revision+1, updated_at=? WHERE id=?",
                            (now, task_id),
                        )
                        return {
                            "auto_advanced": False,
                            "paused_for_human": True,
                            "message": str(res.get("message") or "WorkBuddy 登录出现人机验证，已暂停。"),
                        }

                    if res.get("status") == "next_step":
                        conn.execute(
                            "UPDATE tasks SET step='region', status='ready', revision=revision+1, updated_at=? WHERE id=?",
                            (now, task_id),
                        )
                        self._sync_account_step(task["account_id"], "region", username=task["username"], proxy_url=task["proxy_url"], proxy_node=task["proxy_node"])
                        return {
                            "auto_advanced": True,
                            "step": "region",
                            "message": str(res.get("message") or "已完成 WorkBuddy GitHub 登录授权，正在检查右上角完善信息按钮…"),
                        }

                    return {
                        "auto_advanced": False,
                        "paused_for_human": True,
                        "message": str(res.get("message") or "请在工作窗口完成 GitHub 授权登录。"),
                    }

                if step == "region":
                    if hasattr(self.browser, "complete_workbuddy_region"):
                        res = self.browser.complete_workbuddy_region(dict(task))
                        if not isinstance(res, dict):
                            msg = self.browser.open(dict(task), account["password"])
                            res = {"status": "next_step", "next": "greeting", "region": "not_required", "message": msg}
                    else:
                        msg = self.browser.open(dict(task), account["password"])
                        res = {"status": "next_step", "next": "greeting", "region": "not_required", "message": msg}

                    if res.get("status") == "challenged" or self._pause_for_challenge(conn, task):
                        conn.execute(
                            "UPDATE tasks SET status='paused', revision=revision+1, updated_at=? WHERE id=?",
                            (now, task_id),
                        )
                        return {
                            "auto_advanced": False,
                            "paused_for_human": True,
                            "message": str(res.get("message") or "出现人机验证，已暂停。"),
                        }

                    if res.get("status") == "next_step":
                        region_val = res.get("region") if res.get("region") in ("confirmed", "not_required") else "not_required"
                        conn.execute(
                            "UPDATE tasks SET step='greeting', region=?, status='ready', revision=revision+1, updated_at=? WHERE id=?",
                            (region_val, now, task_id),
                        )
                        self._sync_account_step(task["account_id"], "greeting", username=task["username"], proxy_url=task["proxy_url"], proxy_node=task["proxy_node"])
                        return {
                            "auto_advanced": True,
                            "step": "greeting",
                            "region": region_val,
                            "message": str(res.get("message") or "地区检查已完成，正在进入在线 WorkBuddy 发送招呼…"),
                        }

                    return {
                        "auto_advanced": False,
                        "paused_for_human": True,
                        "message": str(res.get("message") or "请在窗口确认非国区地区后继续。"),
                    }

                if step == "greeting":
                    if hasattr(self.browser, "send_greeting_and_wait_reply"):
                        res = self.browser.send_greeting_and_wait_reply(dict(task), already_sent=bool(task["greeting_attempted"]))
                        if not isinstance(res, dict):
                            if not task["greeting_attempted"]:
                                btn = self.browser.prepare_greeting(dict(task))
                                if hasattr(btn, "click"):
                                    btn.click(timeout=4000)
                            res = {"status": "replied", "sent": True, "message": "已发送招呼并收到回复！"}
                    else:
                        if not task["greeting_attempted"]:
                            btn = self.browser.prepare_greeting(dict(task))
                            if hasattr(btn, "click"):
                                btn.click(timeout=4000)
                        res = {"status": "replied", "sent": True, "message": "已发送招呼并收到回复！"}

                    if res.get("sent") and not task["greeting_attempted"]:
                        conn.execute("UPDATE tasks SET greeting_attempted=1, updated_at=? WHERE id=?", (now, task_id))

                    if res.get("status") == "challenged" or self._pause_for_challenge(conn, task):
                        conn.execute(
                            "UPDATE tasks SET status='paused', revision=revision+1, updated_at=? WHERE id=?",
                            (now, task_id),
                        )
                        return {
                            "auto_advanced": False,
                            "paused_for_human": True,
                            "message": str(res.get("message") or "发送招呼时出现人机验证，已暂停。"),
                        }

                    if res.get("status") != "replied":
                        conn.execute(
                            "UPDATE tasks SET revision=revision+1, updated_at=? WHERE id=?",
                            (now, task_id),
                        )
                        return {
                            "auto_advanced": False,
                            "paused_for_human": True,
                            "message": str(res.get("message") or "请在在线 WorkBuddy 窗口核对招呼回复后点击完成。"),
                        }

                    saved_cred = None
                    if task["credential_json"]:
                        try:
                            parsed = json.loads(task["credential_json"])
                            if isinstance(parsed, dict) and parsed.get("accessToken"):
                                saved_cred = parsed
                        except Exception:
                            saved_cred = None

                    if not (saved_cred and saved_cred.get("refreshToken")):
                        try:
                            captured = self.browser.capture_credential(dict(task), oauth_state=task["oauth_state"])
                            if isinstance(captured, dict) and captured.get("accessToken"):
                                saved_cred = captured
                        except Exception:
                            pass

                    other_active = [t for t in round_active if t["id"] != task_id]
                    round_completed = False
                    round_transitioned = False
                    next_node_label = ""
                    if not other_active:
                        round_completed = True
                        round_transitioned, next_node_label = self._finish_round_browsers(
                            conn, task["batch"], task["round_no"]
                        )

                    final_cred_json = json.dumps(saved_cred, ensure_ascii=False) if saved_cred else task["credential_json"]
                    conn.execute("""UPDATE tasks SET step=?, status=?, revision=revision+1,
                        username=?, region=?, credential_json=?, updated_at=? WHERE id=?""", (
                        "greeting", "done",
                        task["username"], task["region"], final_cred_json, now, task_id,
                    ))

                    if task["proxy_node"]:
                        conn.execute(
                            "UPDATE node_registrations SET status='done' WHERE node_name=? AND email=?",
                            (task["proxy_node"], task["email"]),
                        )
                    if hasattr(self.accounts, "mark_workbuddy_registered"):
                        self.accounts.mark_workbuddy_registered(
                            task["account_id"],
                            username=task["username"],
                            region=task["region"],
                            credential=saved_cred,
                            proxy_url=task["proxy_url"],
                            proxy_node=task["proxy_node"],
                        )

                    if not round_completed:
                        msg = "当前账号已完成并保存凭据！请继续完成本轮第 2 个指纹浏览器注册，全部完成后将统一关闭双浏览器、清理缓存并切换下一节点。"
                    elif round_transitioned:
                        msg = f"本轮已全部完成！已关闭两个指纹浏览器、清理缓存并切换节点{('至 ' + next_node_label) if next_node_label else ''}，正在开启新一轮两个账号注册…"
                    else:
                        msg = "本轮及全部批次账号已注册完成！已关闭全部指纹浏览器并清理缓存。"

                    return {
                        "auto_advanced": True,
                        "task_done": True,
                        "round_completed": round_completed,
                        "round_transitioned": round_transitioned,
                        "credential_saved": bool(saved_cred),
                        "message": msg,
                    }

            if action != "confirm":
                raise ValueError("当前步骤不支持此操作。")

            step = task["step"]
            username, region = task["username"], task["region"]
            if step == "github":
                username = value.strip() if isinstance(value, str) else ""
                if len(username) > 39 or not re.fullmatch(r"[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*", username):
                    raise ValueError("请填写实际提交的 GitHub 用户名，最多 39 位，可使用英文字母、数字和单个连字符。")
            if step == "region":
                if value not in ("confirmed", "not_required"):
                    raise ValueError("请选择已确认实际地区，或页面无需确认。")
                region = value
            following = STEPS.index(step) + 1
            finished = following == len(STEPS)

            saved_cred = None
            if task["credential_json"]:
                try:
                    parsed = json.loads(task["credential_json"])
                    if isinstance(parsed, dict) and parsed.get("accessToken"):
                        saved_cred = parsed
                except Exception:
                    saved_cred = None

            other_active = [t for t in round_active if t["id"] != task_id]
            round_completed = False
            round_transitioned = False
            next_node_label = ""
            if finished:
                if not (saved_cred and saved_cred.get("refreshToken")):
                    try:
                        captured = self.browser.capture_credential(dict(task), oauth_state=task["oauth_state"])
                        if isinstance(captured, dict) and captured.get("accessToken"):
                            saved_cred = captured
                    except Exception:
                        pass
                if not other_active:
                    round_completed = True
                    round_transitioned, next_node_label = self._finish_round_browsers(
                        conn, task["batch"], task["round_no"]
                    )

            final_cred_json = json.dumps(saved_cred, ensure_ascii=False) if saved_cred else task["credential_json"]
            conn.execute("""UPDATE tasks SET step=?, status=?, revision=revision+1,
                username=?, region=?, credential_json=?, updated_at=? WHERE id=?""", (
                step if finished else STEPS[following], "done" if finished else "ready",
                username, region, final_cred_json, now, task_id,
            ))
            if not finished:
                self._sync_account_step(
                    task["account_id"],
                    STEPS[following],
                    username=username,
                    proxy_url=task["proxy_url"],
                    proxy_node=task["proxy_node"],
                )
            if finished:
                if task["proxy_node"]:
                    conn.execute(
                        "UPDATE node_registrations SET status='done' WHERE node_name=? AND email=?",
                        (task["proxy_node"], task["email"]),
                    )
                if hasattr(self.accounts, "mark_workbuddy_registered"):
                    self.accounts.mark_workbuddy_registered(
                        task["account_id"],
                        username=username,
                        region=region,
                        credential=saved_cred,
                        proxy_url=task["proxy_url"],
                        proxy_node=task["proxy_node"],
                    )
                if not round_completed:
                    return {
                        "round_completed": False,
                        "round_transitioned": False,
                        "message": "当前账号已完成并保存凭据！请继续完成本轮第 2 个指纹浏览器注册，全部完成后将统一关闭双浏览器、清理缓存并切换下一节点。",
                    }
                if round_transitioned:
                    return {
                        "round_completed": True,
                        "round_transitioned": True,
                        "message": f"本轮已全部完成！已关闭两个指纹浏览器、清理缓存并切换节点{('至 ' + next_node_label) if next_node_label else ''}，正在开启新一轮两个账号注册…",
                    }
                return {
                    "round_completed": True,
                    "round_transitioned": False,
                    "message": "本轮及全部批次账号已注册完成！已关闭全部指纹浏览器并清理缓存。",
                }

    def close(self):
        try:
            self.worker.submit(self.browser.close).result()
        finally:
            self.worker.shutdown()


WorkBuddyService = WorkBuddyQueue
