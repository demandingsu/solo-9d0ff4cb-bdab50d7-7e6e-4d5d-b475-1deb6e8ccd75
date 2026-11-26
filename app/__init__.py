"""窗帘作坊对花补裁 Flask 应用。"""

from __future__ import annotations

import os
import sqlite3

from flask import Flask, flash, redirect, render_template, request, url_for

from . import db as dbmod
from . import solver

DEFAULT_CONFIG = {
    "roll_count": 2,
    "pair_count": 2,
    "repeat": 10,
    "rolls": [
        {"index": 0, "reference": 0, "sections": [[0, 300]]},
        {"index": 1, "reference": 0, "sections": [[0, 300]]},
        {"index": 2, "reference": 0, "sections": [[0, 300]]},
    ],
    "pairs": [
        {"index": 0, "length": 80},
        {"index": 1, "length": 80},
        {"index": 2, "length": 80},
    ],
}

MAX_FORMS = 3


def _count(raw):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = 1
    return max(1, min(MAX_FORMS, value))


def _sections_text(sections):
    return ", ".join(f"{a}-{b}" for a, b in sections)


def create_app(database=None):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["DATABASE"] = (
        database or os.environ.get("DATABASE_PATH") or "/data/curtain.db"
    )
    app.teardown_appcontext(dbmod.close_db)

    with app.app_context():
        dbmod.init_db(app)

    @app.route("/")
    def index():
        db = dbmod.get_db()
        cfg = dbmod.get_config(db)
        cuts = dbmod.list_cuts(db)
        layout_fp, layout_items = dbmod.get_layout(db)
        if cfg is None:
            cfg = DEFAULT_CONFIG
            current_fp = None
            layout_fresh = False
        else:
            current_fp = cfg.get("fingerprint")
            layout_fresh = layout_fp is not None and layout_fp == current_fp
        cut_keys = {(c["pair_index"], c["side"]) for c in cuts}
        roll_sections_text = {
            r["index"]: _sections_text(r["sections"]) for r in cfg["rolls"]
        }
        return render_template(
            "index.html",
            cfg=cfg,
            current_fp=current_fp,
            layout_items=layout_items,
            layout_fresh=layout_fresh,
            layout_fp=layout_fp,
            cuts=cuts,
            cut_keys=cut_keys,
            roll_sections_text=roll_sections_text,
            max_forms=MAX_FORMS,
        )

    @app.route("/config", methods=["POST"])
    def save_config_route():
        db = dbmod.get_db()
        roll_count = _count(request.form.get("roll_count"))
        pair_count = _count(request.form.get("pair_count"))
        try:
            cfg = solver.build_config(request.form, roll_count, pair_count)
            cuts = dbmod.list_cuts(db)
            errors = solver.validate_against_cuts(cfg, cuts)
            if errors:
                for msg in errors:
                    flash(msg, "error")
                return redirect(url_for("index"))
            fingerprint = solver.config_fingerprint(cfg)
            dbmod.save_config(db, cfg, fingerprint)
            db.commit()
            flash("参数已保存，请点击“生成排法”。", "ok")
        except solver.ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.route("/solve", methods=["POST"])
    def solve_route():
        db = dbmod.get_db()
        cfg = dbmod.get_config(db)
        if cfg is None:
            flash("请先填写并保存参数。", "error")
            return redirect(url_for("index"))
        fingerprint = cfg.pop("fingerprint", None) or solver.config_fingerprint(cfg)
        cuts = dbmod.list_cuts(db)
        ok, payload = solver.solve(cfg, cuts)
        if not ok:
            dbmod.clear_layout(db)
            db.commit()
            flash(payload, "error")
            return redirect(url_for("index"))
        dbmod.save_layout(db, fingerprint, payload)
        db.commit()
        remaining = sum(1 for it in payload if not it["cut"])
        flash(f"排法已生成：共 {len(payload)} 片，其中 {remaining} 片待裁。", "ok")
        return redirect(url_for("index"))

    @app.route("/cut", methods=["POST"])
    def cut_route():
        db = dbmod.get_db()
        cfg = dbmod.get_config(db)
        if cfg is None:
            flash("请先填写并保存参数。", "error")
            return redirect(url_for("index"))
        current_fp = cfg.pop("fingerprint", None) or solver.config_fingerprint(cfg)
        try:
            pair_index = int(request.form.get("pair_index", ""))
            side = int(request.form.get("side", ""))
        except (TypeError, ValueError):
            flash("确认请求参数无效。", "error")
            return redirect(url_for("index"))
        if not (0 <= pair_index < cfg["pair_count"]) or side not in (0, 1):
            flash("确认请求参数无效。", "error")
            return redirect(url_for("index"))
        if dbmod.get_cut(db, pair_index, side) is not None:
            flash("该片已经确认过了。", "error")
            return redirect(url_for("index"))

        layout_fp, layout_items = dbmod.get_layout(db)
        if not layout_items or layout_fp != current_fp:
            flash(
                "可用区段等参数已改动，旧排法失效：请重新点击“生成排法”后再确认。",
                "error",
            )
            return redirect(url_for("index"))

        target = next(
            (
                it
                for it in layout_items
                if it["pair_index"] == pair_index and it["side"] == side
            ),
            None,
        )
        if target is None:
            flash("排法中找不到该片，请重新生成排法。", "error")
            return redirect(url_for("index"))

        roll = next(r for r in cfg["rolls"] if r["index"] == target["roll_index"])
        pair = next(p for p in cfg["pairs"] if p["index"] == pair_index)
        length = target["end_pos"] - target["start_pos"]
        if length != pair["length"]:
            flash("排法裁长与当前参数不一致，请重新生成排法。", "error")
            return redirect(url_for("index"))

        # 落库已裁记录（UNIQUE 约束兜底重复提交/并发确认）
        try:
            dbmod.add_cut(
                db,
                pair_index=pair_index,
                side=side,
                roll_index=target["roll_index"],
                start_pos=target["start_pos"],
                end_pos=target["end_pos"],
                reference=roll["reference"],
                length=pair["length"],
            )
        except sqlite3.IntegrityError:
            db.rollback()
            flash("该片已经确认过了，请勿重复提交。", "error")
            return redirect(url_for("index"))
        cuts = dbmod.list_cuts(db)
        ok, payload = solver.solve(cfg, cuts)
        if ok:
            dbmod.save_layout(db, current_fp, payload)
            msg = (
                f"第 {pair_index + 1} 对{'A' if side == 0 else 'B'}片已确认，"
                "其余帘片已自动重新排齐。"
            )
        else:
            dbmod.clear_layout(db)
            msg = (
                f"第 {pair_index + 1} 对{'A' if side == 0 else 'B'}片已确认并保留；"
                f"剩余片无解：{payload}"
            )
        db.commit()
        flash(msg, "ok" if ok else "error")
        return redirect(url_for("index"))

    @app.route("/reset", methods=["POST"])
    def reset_route():
        db = dbmod.get_db()
        dbmod.clear_cuts(db)
        dbmod.clear_layout(db)
        db.commit()
        flash("已清空全部已裁记录和排法，可重新排料。", "ok")
        return redirect(url_for("index"))

    return app
