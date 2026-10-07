# -*- coding: utf-8 -*-
"""SQLite 持久层：settings / credentials / api_keys / usage_log。

单进程单 worker 使用；sqlite3 连接跨线程共享 + 全局锁，足够覆盖本服务的并发量。
"""
import json
import os
import secrets
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        path = os.path.join(config.DATA_DIR, "transhub.db")
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def init() -> None:
    with _lock:
        c = _connect()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS credentials(
                provider TEXT NOT NULL, name TEXT NOT NULL,
                data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                PRIMARY KEY(provider, name));
            CREATE TABLE IF NOT EXISTS api_keys(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
                prefix TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL, last_used_at INTEGER);
            CREATE TABLE IF NOT EXISTS usage_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL, provider TEXT NOT NULL, endpoint TEXT NOT NULL,
                ok INTEGER NOT NULL, code TEXT, chars INTEGER, ms INTEGER,
                ip TEXT, msg TEXT);
            """
        )
        c.commit()


# ------------------------------------------------------------------ settings
def get_setting(key: str, default=None):
    with _lock:
        row = _connect().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row[0])


def set_setting(key: str, value) -> None:
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        c.commit()


def get_or_create_secret() -> str:
    sec = get_setting("secret")
    if not sec:
        sec = secrets.token_urlsafe(32)
        set_setting("secret", sec)
    return sec


# -------------------------------------------------------------- credentials
def put_credential(provider: str, data: dict, name: str = "default", status: str = "active") -> None:
    now = int(time.time())
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO credentials(provider,name,data,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(provider,name) DO UPDATE SET "
            "data=excluded.data, status=excluded.status, updated_at=excluded.updated_at",
            (provider, name, json.dumps(data, ensure_ascii=False), status, now, now),
        )
        c.commit()


def get_credential(provider: str, name: str | None = None):
    """取可用凭据：优先 status=active 的最新一条；name 为 None 表示任意。"""
    with _lock:
        c = _connect()
        if name:
            row = c.execute(
                "SELECT * FROM credentials WHERE provider=? AND name=?", (provider, name)
            ).fetchone()
        else:
            row = c.execute(
                "SELECT * FROM credentials WHERE provider=? AND status='active' "
                "ORDER BY updated_at DESC LIMIT 1", (provider,)
            ).fetchone()
            if row is None:
                row = c.execute(
                    "SELECT * FROM credentials WHERE provider=? "
                    "ORDER BY updated_at DESC LIMIT 1", (provider,)
                ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["data"] = json.loads(d["data"])
    return d


def list_credentials(provider: str | None = None):
    with _lock:
        c = _connect()
        if provider:
            rows = c.execute(
                "SELECT * FROM credentials WHERE provider=? ORDER BY updated_at DESC", (provider,)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM credentials ORDER BY updated_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d["data"])
        out.append(d)
    return out


def set_credential_status(provider: str, status: str, name: str | None = None) -> None:
    with _lock:
        c = _connect()
        if name:
            c.execute("UPDATE credentials SET status=?, updated_at=? WHERE provider=? AND name=?",
                      (status, int(time.time()), provider, name))
        else:
            c.execute("UPDATE credentials SET status=?, updated_at=? WHERE provider=?",
                      (status, int(time.time()), provider))
        c.commit()


def delete_credential(provider: str, name: str) -> bool:
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM credentials WHERE provider=? AND name=?", (provider, name))
        c.commit()
    return cur.rowcount > 0


# ----------------------------------------------------------------- api keys
def create_api_key(name: str) -> tuple[int, str]:
    """返回 (id, 明文 key)。明文仅此一次展示，库里只存 sha256。"""
    full = "th-" + secrets.token_urlsafe(24)
    import hashlib
    khash = hashlib.sha256(full.encode()).hexdigest()
    with _lock:
        c = _connect()
        cur = c.execute(
            "INSERT INTO api_keys(name,key_hash,prefix,created_at) VALUES(?,?,?,?)",
            (name, khash, full[:9] + "…", int(time.time())),
        )
        c.commit()
    return cur.lastrowid, full


def list_api_keys():
    with _lock:
        rows = _connect().execute(
            "SELECT id,name,prefix,enabled,created_at,last_used_at FROM api_keys ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def verify_api_key(token: str) -> str | None:
    """返回 key 名称；无效返回 None。"""
    if not token:
        return None
    import hashlib
    khash = hashlib.sha256(token.encode()).hexdigest()
    with _lock:
        c = _connect()
        row = c.execute("SELECT id,name,enabled FROM api_keys WHERE key_hash=?", (khash,)).fetchone()
        if row is None or not row["enabled"]:
            return None
        c.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (int(time.time()), row["id"]))
        c.commit()
    return row["name"]


def key_enabled_exists() -> bool:
    with _lock:
        row = _connect().execute("SELECT COUNT(*) FROM api_keys WHERE enabled=1").fetchone()
    return row[0] > 0


def set_api_key_enabled(key_id: int, enabled: bool) -> None:
    with _lock:
        c = _connect()
        c.execute("UPDATE api_keys SET enabled=? WHERE id=?", (1 if enabled else 0, key_id))
        c.commit()


def delete_api_key(key_id: int) -> None:
    with _lock:
        c = _connect()
        c.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
        c.commit()


# -------------------------------------------------------------- usage log
def log_usage(provider: str, endpoint: str, ok: bool, code=None, chars=None, ms=None, ip=None, msg=None) -> None:
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO usage_log(ts,provider,endpoint,ok,code,chars,ms,ip,msg) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (int(time.time()), provider, endpoint, 1 if ok else 0,
             str(code) if code is not None else None, chars, ms, ip,
             (msg or "")[:300]),
        )
        c.commit()


def recent_usage(limit: int = 100):
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM usage_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def usage_stats(since_seconds: int = 86400) -> dict:
    since = int(time.time()) - since_seconds
    with _lock:
        rows = _connect().execute(
            "SELECT provider, COUNT(*) cnt, SUM(ok) ok, SUM(COALESCE(chars,0)) chars, "
            "AVG(COALESCE(ms,0)) avg_ms FROM usage_log WHERE ts>=? GROUP BY provider",
            (since,),
        ).fetchall()
    return {r["provider"]: dict(r) for r in rows}
