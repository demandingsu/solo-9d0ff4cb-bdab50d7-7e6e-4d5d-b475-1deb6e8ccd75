"""SQLite 存取层。

表结构：
- config      当前生效的排料参数（单行 id=1）
- layout      最近一次成功生成的排法及其绑定的参数指纹（单行 id=1）
- layout_item 排法明细（每对两片一行）
- cut         师傅逐片确认的已裁记录
"""

from __future__ import annotations

import json
import os
import sqlite3

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
"""


def get_db():
    if "db" not in g:
        path = current_app.config["DATABASE"]
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = sqlite3.connect(path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    path = app.config["DATABASE"]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------- config ----------

def get_config(db):
    row = db.execute("SELECT payload, fingerprint FROM config WHERE id = 1").fetchone()
    if row is None:
        return None
    cfg = json.loads(row["payload"])
    cfg["fingerprint"] = row["fingerprint"]
    return cfg


def save_config(db, cfg, fingerprint):
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
    db.execute(
        """
        INSERT INTO cut
            (pair_index, side, roll_index, start_pos, end_pos, reference, length)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (pair_index, side, roll_index, start_pos, end_pos, reference, length),
    )


def clear_cuts(db):
    db.execute("DELETE FROM cut")
