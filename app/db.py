"""SQLite 存取层。

表结构：
- config      当前生效的排料参数（单行 id=1）
- layout      最近一次成功生成的排法及其绑定的参数指纹（单行 id=1）
- layout_item 排法明细（每对两片一行）
- cut         师傅逐片确认的已裁记录
- state       参数/已裁记录的单调版本号，用于发现并发改动、拒绝迟到结果

写入统一走 ``write_txn``（BEGIN IMMEDIATE）串行化；需要先取快照再计算的请求
（例如手动生成排法）用 ``read_txn`` 短暂读一致快照后释放锁，计算完再在写事务
里比对版本号，避免把过期结果写回。已裁记录只增不删，除非师傅显式“清空重新开工”。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    payload     TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS layout (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    fingerprint TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS layout_item (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_index  INTEGER NOT NULL,
    side        INTEGER NOT NULL,
    roll_index  INTEGER NOT NULL,
    start_pos   INTEGER NOT NULL,
    end_pos     INTEGER NOT NULL,
    residue     INTEGER NOT NULL,
    UNIQUE(pair_index, side)
);

CREATE TABLE IF NOT EXISTS cut (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_index  INTEGER NOT NULL,
    side        INTEGER NOT NULL,
    roll_index  INTEGER NOT NULL,
    start_pos   INTEGER NOT NULL,
    end_pos     INTEGER NOT NULL,
    reference   INTEGER NOT NULL,
    length      INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(pair_index, side)
);

-- 升级时新建；CREATE TABLE IF NOT EXISTS 不会触碰旧表，历史已裁记录原样保留。
CREATE TABLE IF NOT EXISTS state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    config_rev  INTEGER NOT NULL DEFAULT 0,
    cuts_rev    INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO state (id, config_rev, cuts_rev) VALUES (1, 0, 0);
"""


class DatabaseBusy(RuntimeError):
    """写事务在等待期内拿不到 SQLite 写锁。"""


@contextmanager
def write_txn(db):
    """串行化写事务。异常时回滚；正常结束时提交。"""
    try:
        db.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if _is_busy(exc):
            raise DatabaseBusy("数据库正忙，请稍后重试") from exc
        raise
    try:
        yield
    except BaseException:
        _rollback_quietly(db)
        raise
    else:
        try:
            db.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            _rollback_quietly(db)
            if _is_busy(exc):
                raise DatabaseBusy("数据库正忙，请稍后重试") from exc
            raise


@contextmanager
def read_txn(db):
    """短暂的一致性读快照；结束即释放，不阻塞其他页面的写入。"""
    db.execute("BEGIN")
    try:
        yield
    finally:
        _rollback_quietly(db)


def _rollback_quietly(db):
    # SQLite 在某些错误后会自行回滚；此时再 ROLLBACK 会报“no transaction”。
    try:
        db.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


def _is_busy(exc):
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def get_db():
    if "db" not in g:
        path = current_app.config["DATABASE"]
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # isolation_level=None：由 write_txn/read_txn 显式控制事务边界。
        conn = sqlite3.connect(path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        g.db = conn
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    """建表并做幂等升级；只新增 state 表，绝不重建/清空既有业务表。"""
    path = app.config["DATABASE"]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    try:
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.executescript(SCHEMA)
        # WAL：读请求不被写事务阻塞，便于“另一页保存参数/确认裁片”。
        conn.execute("PRAGMA journal_mode = WAL")
        conn.commit()
    finally:
        conn.close()


# ---------- revisions ----------

def get_state(db):
    row = db.execute("SELECT config_rev, cuts_rev FROM state WHERE id = 1").fetchone()
    if row is None:
        return {"config_rev": 0, "cuts_rev": 0}
    return {"config_rev": row["config_rev"], "cuts_rev": row["cuts_rev"]}


def _bump(db, column):
    db.execute(f"UPDATE state SET {column} = {column} + 1 WHERE id = 1")


# ---------- config ----------

def get_config(db):
    row = db.execute("SELECT payload, fingerprint FROM config WHERE id = 1").fetchone()
    if row is None:
        return None
    cfg = json.loads(row["payload"])
    cfg["fingerprint"] = row["fingerprint"]
    return cfg


def save_config(db, cfg, fingerprint):
    """保存参数；指纹变化时推进 config_rev。调用方负责事务。"""
    row = db.execute("SELECT fingerprint FROM config WHERE id = 1").fetchone()
    changed = row is None or row["fingerprint"] != fingerprint
    payload = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    db.execute(
        """
        INSERT INTO config (id, payload, fingerprint, updated_at)
        VALUES (1, ?, ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            payload = excluded.payload,
            fingerprint = excluded.fingerprint,
            updated_at = datetime('now')
        """,
        (payload, fingerprint),
    )
    if changed:
        _bump(db, "config_rev")
    return changed


# ---------- layout ----------

def get_layout(db):
    row = db.execute("SELECT fingerprint FROM layout WHERE id = 1").fetchone()
    if row is None:
        return None, []
    items = [
        dict(r)
        for r in db.execute(
            "SELECT pair_index, side, roll_index, start_pos, end_pos, residue "
            "FROM layout_item ORDER BY pair_index, side"
        ).fetchall()
    ]
    return row["fingerprint"], items


def save_layout(db, fingerprint, items):
    """整表替换排法。调用方负责事务。"""
    db.execute("DELETE FROM layout_item")
    db.execute("DELETE FROM layout")
    db.execute("INSERT INTO layout (id, fingerprint) VALUES (1, ?)", (fingerprint,))
    db.executemany(
        """
        INSERT INTO layout_item
            (pair_index, side, roll_index, start_pos, end_pos, residue)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                it["pair_index"], it["side"], it["roll_index"],
                it["start"], it["end"], it["residue"],
            )
            for it in items
        ],
    )


def clear_layout(db):
    db.execute("DELETE FROM layout_item")
    db.execute("DELETE FROM layout")


# ---------- cuts ----------

def list_cuts(db):
    return [
        dict(r)
        for r in db.execute(
            "SELECT id, pair_index, side, roll_index, start_pos, end_pos, "
            "reference, length, created_at FROM cut ORDER BY pair_index, side"
        ).fetchall()
    ]


def get_cut(db, pair_index, side):
    return db.execute(
        "SELECT * FROM cut WHERE pair_index = ? AND side = ?",
        (pair_index, side),
    ).fetchone()


def add_cut(db, *, pair_index, side, roll_index, start_pos, end_pos, reference, length):
    """写入已裁记录并推进 cuts_rev。调用方负责事务。"""
    db.execute(
        """
        INSERT INTO cut
            (pair_index, side, roll_index, start_pos, end_pos, reference, length)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (pair_index, side, roll_index, start_pos, end_pos, reference, length),
    )
    _bump(db, "cuts_rev")


def clear_cuts(db):
    db.execute("DELETE FROM cut")
    _bump(db, "cuts_rev")
