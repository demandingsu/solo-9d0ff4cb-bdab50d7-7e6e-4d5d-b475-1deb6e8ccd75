"""窗帘作坊对花补裁 Flask 应用。

并发约定（裁切页与参数页可能同时操作）：

- 所有写请求在 ``db.write_txn``（BEGIN IMMEDIATE）内完成“读取快照 → 校验 →
  写入”，SQLite 保证它们串行执行，不会交叉覆盖。
- 手动生成排法不持写锁：先用 ``read_txn`` 取参数/已裁记录的版本快照，在锁外
  求解；发布前再开写事务比对版本。期间参数或已裁记录有任何变化，迟到的结果
  （无论有解无解）一律丢弃，绝不覆盖或清除当前排法。
"""

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

STALE_LAYOUT_MSG = (
    "参数（含可用区段）已改动，旧排法失效且未入账，不能确认未裁片："
    "请重新排料（点击“生成排法”）后再确认。"
)


class _Abort(Exception):
    """在写事务内部提前结束请求：回滚本次写入并提示用户。"""

    def __init__(self, message, category="error"):
        super().__init__(message)
        self.message = message
        self.category = category


def _count(raw):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = 1
    return max(1, min(MAX_FORMS, value))


def _sections_text(sections):
    return ", ".join(f"{a}-{b}" for a, b in sections)


def _fingerprint_of(cfg):
    """取出配置携带的指纹；缺失时按内容重算。"""
    return cfg.pop("fingerprint", None) or solver.config_fingerprint(cfg)


def create_app(database=None):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["DATABASE"] = (
        database or os.environ.get("DATABASE_PATH") or "/data/curtain.db"
    )
    app.teardown_appcontext(dbmod.close_db)

    with app.app_context():
        dbmod.init_db(app)

    @app.errorhandler(dbmod.DatabaseBusy)
    def busy(_exc):
        flash("数据库正忙，本次操作未保存，请稍后重试。", "error")
        return redirect(url_for("index"))

    @app.route("/healthz")
    def healthz():
        # 供 Docker Compose healthcheck 使用：能读到数据库即视为就绪。
        try:
            with dbmod.read_txn(dbmod.get_db()):
                pass
        except Exception:  # noqa: BLE001 - 健康检查只回报状态
            return "db unavailable", 503
        return "ok", 200

    @app.route("/")
    def index():
        db = dbmod.get_db()
        # 一致快照：避免页面上的排法行与已裁表互相矛盾。
        with dbmod.read_txn(db):
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
        except solver.ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))

        fingerprint = solver.config_fingerprint(cfg)
        try:
            with dbmod.write_txn(db):
                # 在本事务内读取已裁记录：如果确认裁片的请求先提交，这里立刻
                # 可见它的记录；如果确认请求在等待，它会在本事务提交后读到新
                # 参数（旧排法指纹不符 → 拒绝确认）。两种顺序都不会产生
                # “A 片按旧裁长入账、B 片按新裁长排料”的混合状态。
                cuts = dbmod.list_cuts(db)
                errors = solver.validate_against_cuts(cfg, cuts)
                if errors:
                    raise _Abort("\n".join(errors))
                previous_layout_fp, _ = dbmod.get_layout(db)
                changed = dbmod.save_config(db, cfg, fingerprint)
                stale = changed and previous_layout_fp not in (None, fingerprint)
            if stale:
                flash(
                    "参数已保存；旧排法已失效、未入账，请重新点击“生成排法”排料。"
                    "已裁记录继续保留。",
                    "ok",
                )
            else:
                flash("参数已保存，请点击“生成排法”。", "ok")
        except _Abort as exc:
            flash(exc.message, exc.category)
        except dbmod.DatabaseBusy:
            raise
        return redirect(url_for("index"))

    @app.route("/solve", methods=["POST"])
    def solve_route():
        db = dbmod.get_db()
        try:
            # 1) 短暂读快照，立即释放读事务；求解期间不持有任何锁，
            #    师傅可在另一页保存参数或确认裁片。
            with dbmod.read_txn(db):
                cfg = dbmod.get_config(db)
                if cfg is None:
                    raise _Abort("请先填写并保存参数。")
                fingerprint = _fingerprint_of(cfg)
                cuts = dbmod.list_cuts(db)
                snapshot = dbmod.get_state(db)

            # 2) 锁外求解（可能较慢；期间其他请求可以正常写入）。
            ok, payload = solver.solve(cfg, cuts)

            # 3) 发布前在同一写事务里核对版本：迟到的结果一律作废。
            with dbmod.write_txn(db):
                current = dbmod.get_state(db)
                current_cfg = dbmod.get_config(db)
                current_fp = (
                    None if current_cfg is None else _fingerprint_of(current_cfg)
                )
                if current != snapshot or current_fp != fingerprint:
                    raise _Abort(
                        "排料期间参数或已裁记录已被另一页改动，"
                        "本次结果已作废、未发布；请按当前参数重新生成排法。"
                    )
                if not ok:
                    # 仅在快照未变时按无解清理；迟到的无解绝不能清掉现有排法。
                    dbmod.clear_layout(db)
                    message, category = payload, "error"
                else:
                    dbmod.save_layout(db, fingerprint, payload)
                    remaining = sum(1 for it in payload if not it["cut"])
                    message, category = (
                        f"排法已生成：共 {len(payload)} 片，其中 {remaining} 片待裁。",
                        "ok",
                    )
        except _Abort as exc:
            flash(exc.message, exc.category)
        except dbmod.DatabaseBusy:
            raise
        else:
            flash(message, category)
        return redirect(url_for("index"))

    @app.route("/cut", methods=["POST"])
    def cut_route():
        db = dbmod.get_db()
        try:
            pair_index = int(request.form.get("pair_index", ""))
            side = int(request.form.get("side", ""))
        except (TypeError, ValueError):
            flash("确认请求参数无效。", "error")
            return redirect(url_for("index"))
        if side not in (0, 1):
            flash("确认请求参数无效。", "error")
            return redirect(url_for("index"))

        # 页面自带的排法版本/坐标（可选）。老客户端不传也能用；新页面必传，
        # 用于拒绝“页面渲染后另一页已重新排料”的过期确认。
        expected_fp = (request.form.get("layout_fp") or "").strip() or None
        expected_pos = {}
        for field in ("roll_index", "start_pos", "end_pos"):
            raw = (request.form.get(field) or "").strip()
            if raw:
                try:
                    expected_pos[field] = int(raw)
                except ValueError:
                    flash("确认请求参数无效。", "error")
                    return redirect(url_for("index"))
        if expected_pos and len(expected_pos) != 3:
            flash("确认请求参数无效。", "error")
            return redirect(url_for("index"))

        # 确认裁片与参数保存共用同一把写锁：这里读到的参数、排法、已裁记录
        # 在落库前不会被另一页改掉，因此确认的裁长与随后排料所用的参数必然
        # 属于同一版本。
        try:
            with dbmod.write_txn(db):
                cfg = dbmod.get_config(db)
                if cfg is None:
                    raise _Abort("请先填写并保存参数。")
                current_fp = _fingerprint_of(cfg)
                if not (0 <= pair_index < cfg["pair_count"]):
                    raise _Abort("确认请求参数无效。")
                if dbmod.get_cut(db, pair_index, side) is not None:
                    raise _Abort("该片已经确认过了。")

                layout_fp, layout_items = dbmod.get_layout(db)
                if not layout_items or layout_fp != current_fp:
                    raise _Abort(STALE_LAYOUT_MSG)
                if expected_fp is not None and expected_fp != layout_fp:
                    raise _Abort(
                        "该页显示的排法已被另一页更新，本次确认已退回；"
                        "请刷新页面后按当前排法重新确认。"
                    )

                # 历史数据可能已有与当前参数不一致的已裁记录（旧版并发缺陷
                # 遗留）。此时继续确认会把错误固化，宁可退回要求先处理。
                existing = dbmod.list_cuts(db)
                inconsistencies = solver.validate_against_cuts(cfg, existing)
                if inconsistencies:
                    raise _Abort("\n".join(inconsistencies))

                target = next(
                    (
                        it
                        for it in layout_items
                        if it["pair_index"] == pair_index and it["side"] == side
                    ),
                    None,
                )
                if target is None:
                    raise _Abort("排法中找不到该片，请重新生成排法。")
                if expected_pos and (
                    target["roll_index"] != expected_pos["roll_index"]
                    or target["start_pos"] != expected_pos["start_pos"]
                    or target["end_pos"] != expected_pos["end_pos"]
                ):
                    raise _Abort(
                        "该页显示的裁片位置已被另一页更新，本次确认已退回；"
                        "请刷新页面后按当前排法重新确认。"
                    )

                roll = next(
                    (r for r in cfg["rolls"] if r["index"] == target["roll_index"]),
                    None,
                )
                pair = next(
                    (p for p in cfg["pairs"] if p["index"] == pair_index), None
                )
                if roll is None or pair is None:
                    raise _Abort("当前参数找不到该片对应的卷或对，请重新生成排法。")
                length = target["end_pos"] - target["start_pos"]
                if length != pair["length"]:
                    raise _Abort(
                        "排法裁长与当前参数不一致（参数可能在确认前已被改动），"
                        "请重新生成排法后再确认。"
                    )

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
                    raise _Abort("该片已经确认过了，请勿重复提交。")

                cuts = dbmod.list_cuts(db)
                ok, payload = solver.solve(cfg, cuts)
                if ok:
                    dbmod.save_layout(db, current_fp, payload)
                    message, category = (
                        f"第 {pair_index + 1} 对{'A' if side == 0 else 'B'}片已确认，"
                        "其余帘片已自动重新排齐。",
                        "ok",
                    )
                else:
                    dbmod.clear_layout(db)
                    message, category = (
                        f"第 {pair_index + 1} 对{'A' if side == 0 else 'B'}片已确认并保留；"
                        f"剩余片无解：{payload}",
                        "error",
                    )
        except _Abort as exc:
            flash(exc.message, exc.category)
        except dbmod.DatabaseBusy:
            raise
        else:
            flash(message, category)
        return redirect(url_for("index"))

    @app.route("/reset", methods=["POST"])
    def reset_route():
        db = dbmod.get_db()
        try:
            with dbmod.write_txn(db):
                dbmod.clear_cuts(db)
                dbmod.clear_layout(db)
        except dbmod.DatabaseBusy:
            raise
        flash("已清空全部已裁记录和排法，可重新排料。", "ok")
        return redirect(url_for("index"))

    return app
