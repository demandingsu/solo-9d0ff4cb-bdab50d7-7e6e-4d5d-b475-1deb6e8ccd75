"""并发与升级回归测试。

覆盖：
1. 两种提交顺序：
   - 已裁记录先提交 → 随后保存参数不得改该对裁长/该卷基准，也不得删卷删对；
   - 新参数先提交 → 旧排法的确认必须退回，提示重新排料，且不得写入已裁记录。
2. 手动生成排法期间另一页有写入：
   - 迟到的有解结果不得发布，不得覆盖新排法；
   - 迟到的无解结果不得清除当前排法。
3. 从旧版数据库升级：既有 SQLite 已裁记录原样保留。

运行：python -I tests/test_concurrency.py
"""

import os
import re
import sqlite3
import sys
import tempfile
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_DEPS = os.path.join(_ROOT, ".pydeps")
if os.path.isdir(_DEPS):
    sys.path.insert(0, _DEPS)

from app import create_app  # noqa: E402
from app import db as dbmod  # noqa: E402
from app import solver  # noqa: E402


def config_form(roll_sections=None, refs=None, lengths=None, repeat=10,
                roll_count=2, pair_count=2):
    roll_sections = (roll_sections or ["0-300", "0-300"]) + ["0-300"] * 3
    refs = (refs or [0, 0, 0]) + [0] * 3
    lengths = (lengths or [80, 80, 80]) + [80] * 3
    data = {"roll_count": str(roll_count), "pair_count": str(pair_count),
            "repeat": str(repeat)}
    for i in range(3):
        data[f"ref_{i}"] = str(refs[i])
        data[f"sections_{i}"] = roll_sections[i]
    for j in range(3):
        data[f"length_{j}"] = str(lengths[j])
    return data


def flash_text(html):
    return re.findall(r'<div class="msg [^"]*">(.*?)</div>', html, re.S)


def new_app(prefix):
    tmp = tempfile.mkdtemp(prefix=prefix)
    return create_app(database=os.path.join(tmp, "test.db"))


def snapshot(app):
    with app.app_context():
        db = dbmod.get_db()
        cfg = dbmod.get_config(db)
        fp = cfg.pop("fingerprint") if cfg else None
        return {
            "cfg": cfg,
            "fp": fp,
            "cuts": dbmod.list_cuts(db),
            "layout_fp": dbmod.get_layout(db)[0],
            "layout_items": dbmod.get_layout(db)[1],
        }


def run_thread(fn):
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 测试需带回线程异常
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def wait_event(event, label):
    assert event.wait(10), f"等待“{label}”超时（可能死锁）"


class Gate:
    """只拦截一次指定函数调用的闸门，用于构造确定性交错。"""

    def __init__(self, module, name, replacement=None):
        self.module = module
        self.name = name
        self.replacement = replacement
        self.original = getattr(module, name)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.used = False

    def __enter__(self):
        gate = self

        def wrapper(*args, **kwargs):
            if not gate.used:
                gate.used = True
                gate.entered.set()
                assert gate.release.wait(10), "闸门释放超时"
                if gate.replacement is not None:
                    return gate.replacement(*args, **kwargs)
            return gate.original(*args, **kwargs)

        setattr(self.module, self.name, wrapper)
        return self

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.original)
        return False


def test_cut_commits_first_then_config_rejected():
    """已裁记录先入账：并发的参数保存必须被拒绝，不得改该对裁长。"""
    app = new_app("curtain-order-cut-")
    client = app.test_client()
    client.post("/config", data=config_form(), follow_redirects=True)
    client.post("/solve", data={}, follow_redirects=True)

    # 在 cut 事务内部（读取校验已完成、落库前）暂停，此时写锁已被 cut 持有。
    with Gate(dbmod, "add_cut") as gate:
        cut_thread, cut_box = run_thread(
            lambda: client.post("/cut", data={"pair_index": "1", "side": "0"},
                                follow_redirects=True).get_data(as_text=True)
        )
        wait_event(gate.entered, "cut 事务持锁")

        # 参数页尝试把第 2 对裁长从 80 改为 70（该对刚确认了 A 片）。
        other = app.test_client()
        cfg_thread, cfg_box = run_thread(
            lambda: other.post("/config",
                               data=config_form(lengths=[80, 70]),
                               follow_redirects=True).get_data(as_text=True)
        )
        time.sleep(0.2)  # 让参数请求排在写锁后面
        gate.release.set()
        cut_thread.join(10)
        cfg_thread.join(10)

    assert "error" not in cut_box, cut_box["error"]
    assert "error" not in cfg_box, cfg_box["error"]
    assert any("已确认" in t for t in flash_text(cut_box["value"])), flash_text(cut_box["value"])
    assert any("裁长不能修改" in t for t in flash_text(cfg_box["value"])), \
        flash_text(cfg_box["value"])

    state = snapshot(app)
    assert len(state["cuts"]) == 1, state["cuts"]
    cut = state["cuts"][0]
    assert cut["length"] == 80, cut
    assert state["cfg"]["pairs"][1]["length"] == 80, state["cfg"]["pairs"][1]
    # 同对 B 片仍按 80 排料，且排法与当前参数一致
    assert state["layout_fp"] == state["fp"]
    b = next(it for it in state["layout_items"]
             if (it["pair_index"], it["side"]) == (1, 1))
    assert b["end_pos"] - b["start_pos"] == 80, b

    # 同一条已裁记录仍然阻止：改该卷花纹基准、减少对数、减少卷数。
    cut_roll = cut["roll_index"]
    refs = [0, 0, 0]
    refs[cut_roll] = 5
    html = client.post("/config", data=config_form(refs=refs),
                       follow_redirects=True).get_data(as_text=True)
    assert any("花纹基准不能修改" in t for t in flash_text(html)), flash_text(html)
    html = client.post("/config", data=config_form(pair_count=1),
                       follow_redirects=True).get_data(as_text=True)
    assert any("不能减少对数" in t for t in flash_text(html)), flash_text(html)
    if cut_roll > 0:
        html = client.post("/config", data=config_form(roll_count=cut_roll),
                           follow_redirects=True).get_data(as_text=True)
        assert any("不能减少卷数" in t for t in flash_text(html)), flash_text(html)
    assert len(snapshot(app)["cuts"]) == 1

def test_config_commits_first_then_stale_cut_rejected():
    """新参数先保存：旧排法的确认必须退回并提示重新排料，不得入账。"""
    app = new_app("curtain-order-config-")
    client = app.test_client()
    client.post("/config", data=config_form(), follow_redirects=True)
    client.post("/solve", data={}, follow_redirects=True)

    with Gate(dbmod, "save_config") as gate:
        other = app.test_client()
        cfg_thread, cfg_box = run_thread(
            lambda: other.post("/config",
                               data=config_form(lengths=[80, 70]),
                               follow_redirects=True).get_data(as_text=True)
        )
        wait_event(gate.entered, "config 事务持锁")

        # 参数事务持锁期间，裁切页仍拿旧排法确认第 2 对 B 片（旧裁长 80）。
        cut_thread, cut_box = run_thread(
            lambda: client.post("/cut", data={"pair_index": "1", "side": "1"},
                                follow_redirects=True).get_data(as_text=True)
        )
        time.sleep(0.2)
        gate.release.set()
        cfg_thread.join(10)
        cut_thread.join(10)

    assert "error" not in cfg_box, cfg_box["error"]
    assert "error" not in cut_box, cut_box["error"]
    assert any("参数已保存" in t for t in flash_text(cfg_box["value"])), \
        flash_text(cfg_box["value"])
    cut_flash = flash_text(cut_box["value"])
    assert any(("旧排法" in t and "重新排料" in t) for t in cut_flash), cut_flash

    state = snapshot(app)
    assert state["cuts"] == [], state["cuts"]
    assert state["cfg"]["pairs"][1]["length"] == 70, state["cfg"]["pairs"][1]
    # 旧排法仍在页面上但标记失效，不可确认
    html = client.get("/").get_data(as_text=True)
    assert "旧排法不可确认" in html


def test_late_solution_not_published_after_concurrent_change():
    """排法计算期间参数被改：迟到的有解结果作废，不覆盖新排法。"""
    app = new_app("curtain-late-ok-")
    client = app.test_client()
    client.post("/config", data=config_form(), follow_redirects=True)

    with Gate(solver, "solve") as gate:
        solve_thread, solve_box = run_thread(
            lambda: client.post("/solve", data={},
                                follow_redirects=True).get_data(as_text=True)
        )
        wait_event(gate.entered, "旧参数求解中")

        # 另一页改参数并生成新排法（闸门只拦一次，这次走真实求解）。
        other = app.test_client()
        other.post("/config", data=config_form(roll_sections=["0-260", "0-300"]),
                   follow_redirects=True)
        other.post("/solve", data={}, follow_redirects=True)
        new_state = snapshot(app)
        assert new_state["layout_items"], "新排法应已发布"
        assert new_state["layout_fp"] == new_state["fp"]

        gate.release.set()
        solve_thread.join(10)

    assert "error" not in solve_box, solve_box["error"]
    assert any("作废" in t for t in flash_text(solve_box["value"])), \
        flash_text(solve_box["value"])

    after = snapshot(app)
    assert after["layout_fp"] == new_state["fp"], (after["layout_fp"], new_state["fp"])
    assert after["layout_items"] == new_state["layout_items"], "新排法被迟到结果覆盖"


def test_late_no_solution_does_not_clear_layout():
    """排法计算期间已裁记录变化：迟到的无解结果不得清除当前排法。"""
    app = new_app("curtain-late-none-")
    client = app.test_client()
    client.post("/config", data=config_form(), follow_redirects=True)

    with Gate(solver, "solve", replacement=lambda cfg, cuts: (False, "无解：模拟迟到结果")) as gate:
        solve_thread, solve_box = run_thread(
            lambda: client.post("/solve", data={},
                                follow_redirects=True).get_data(as_text=True)
        )
        wait_event(gate.entered, "无解求解中")

        # 另一页正常生成排法并确认一片，产生新的当前排法。
        other = app.test_client()
        other.post("/solve", data={}, follow_redirects=True)
        cut_html = other.post("/cut", data={"pair_index": "0", "side": "0"},
                              follow_redirects=True).get_data(as_text=True)
        assert any("已确认" in t for t in flash_text(cut_html)), flash_text(cut_html)
        expected = snapshot(app)
        assert expected["layout_items"], "确认后应仍有排法"

        gate.release.set()
        solve_thread.join(10)

    assert "error" not in solve_box, solve_box["error"]
    assert any("作废" in t for t in flash_text(solve_box["value"])), \
        flash_text(solve_box["value"])

    after = snapshot(app)
    assert after["layout_items"] == expected["layout_items"], "迟到无解清除了当前排法"
    assert len(after["cuts"]) == 1


def test_stale_page_form_rejected_after_reflow():
    """页面渲染后另一页确认了裁片导致重新排料：旧页面提交须退回，不得按旧坐标入账。"""
    app = new_app("curtain-stale-form-")
    client = app.test_client()
    client.post("/config", data=config_form(), follow_redirects=True)
    html = client.post("/solve", data={},
                       follow_redirects=True).get_data(as_text=True)
    layout_fp = re.search(r'name="layout_fp" value="([0-9a-f]+)"', html).group(1)
    # 默认排法：对 1 A 片 (卷0,160-240)，对 1 B 片 (卷1,0-80)
    # 另一页先确认对 1 A 片 → 剩余片自动重排，B 片坐标可能改变。
    other = app.test_client()
    resp = other.post("/cut", data={"pair_index": "1", "side": "0"},
                      follow_redirects=True).get_data(as_text=True)
    assert any("已确认" in t for t in flash_text(resp)), flash_text(resp)

    # 用旧页面的表单快照提交对 1 B 片（旧坐标）。
    stale = client.post(
        "/cut",
        data={"pair_index": "1", "side": "1", "layout_fp": layout_fp,
              "roll_index": "1", "start_pos": "0", "end_pos": "80"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert any("已被另一页更新" in t for t in flash_text(stale)), flash_text(stale)
    assert len(snapshot(app)["cuts"]) == 1, snapshot(app)["cuts"]

    # 刷新后按当前排法确认成功。
    fresh = client.get("/").get_data(as_text=True)
    m = re.search(
        r'name="pair_index" value="1">\s*<input type="hidden" name="side" value="1">'
        r'\s*<input type="hidden" name="layout_fp" value="([0-9a-f]+)">'
        r'\s*<input type="hidden" name="roll_index" value="(\d+)">'
        r'\s*<input type="hidden" name="start_pos" value="(\d+)">'
        r'\s*<input type="hidden" name="end_pos" value="(\d+)">',
        fresh,
    )
    assert m, "页面缺少确认表单隐藏字段"
    fp, roll, start, end = m.groups()
    resp = client.post(
        "/cut",
        data={"pair_index": "1", "side": "1", "layout_fp": fp,
              "roll_index": roll, "start_pos": start, "end_pos": end},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert any("已确认" in t for t in flash_text(resp)), flash_text(resp)
    assert len(snapshot(app)["cuts"]) == 2


def test_legacy_database_upgrade_preserves_cuts():
    """旧版数据库（无 state 表）升级后，已裁记录与排法原样保留。"""
    tmp = tempfile.mkdtemp(prefix="curtain-legacy-")
    db_path = os.path.join(tmp, "legacy.db")
    legacy_schema = dbmod.SCHEMA.split("-- 升级时新建")[0]
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(legacy_schema)
        conn.execute(
            "INSERT INTO cut (pair_index, side, roll_index, start_pos, end_pos, "
            "reference, length) VALUES (0, 0, 0, 0, 80, 0, 80)"
        )
        conn.commit()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "state" not in tables
    finally:
        conn.close()

    app = create_app(database=db_path)
    state = snapshot(app)
    assert len(state["cuts"]) == 1, state["cuts"]
    assert state["cuts"][0]["length"] == 80
    with app.app_context():
        db = dbmod.get_db()
        revs = dbmod.get_state(db)
        assert revs == {"config_rev": 0, "cuts_rev": 0}, revs

    # 升级后的页面正常渲染，旧已裁记录可见，且不能改该卷基准
    client = app.test_client()
    html = client.get("/").get_data(as_text=True)
    assert "第 1 对 A 片" in html
    client.post("/config", data=config_form(), follow_redirects=True)
    resp = client.post("/config", data=config_form(refs=[9, 0, 0]),
                       follow_redirects=True).get_data(as_text=True)
    assert any("花纹基准不能修改" in t for t in flash_text(resp)), flash_text(resp)


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} 个并发/升级测试全部通过")


if __name__ == "__main__":
    main()
