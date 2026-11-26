"""求解器核心规则单元测试。

运行：python -I tests/test_solver.py
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
# 本机未装系统 Flask 时使用项目本地依赖（Docker 内无此目录则忽略）
_DEPS = os.path.join(_ROOT, ".pydeps")
if os.path.isdir(_DEPS):
    sys.path.insert(0, _DEPS)

from app import solver  # noqa: E402


def make_cfg(repeat=10, rolls=None, pairs=None):
    rolls = rolls or [
        {"index": 0, "reference": 0, "sections": [[0, 300]]},
        {"index": 1, "reference": 0, "sections": [[0, 300]]},
    ]
    pairs = pairs or [
        {"index": 0, "length": 80},
        {"index": 1, "length": 80},
    ]
    return {
        "roll_count": len(rolls),
        "pair_count": len(pairs),
        "repeat": repeat,
        "rolls": rolls,
        "pairs": pairs,
    }


def assert_equal(actual, expected, label):
    assert actual == expected, f"{label}: 期望 {expected!r}，实际 {actual!r}"


def test_basic_solve_and_pattern_rule():
    cfg = make_cfg()
    ok, items = solver.solve(cfg, [])
    assert ok, items
    assert_equal(len(items), 4, "片数")
    for j in range(2):
        sides = [it for it in items if it["pair_index"] == j]
        r0 = (sides[0]["start"] - cfg["rolls"][sides[0]["roll_index"]]["reference"]) % 10
        r1 = (sides[1]["start"] - cfg["rolls"][sides[1]["roll_index"]]["reference"]) % 10
        assert_equal(r0, r1, f"第{j + 1}对花位余数")
        for it in sides:
            assert_equal(it["end"] - it["start"], 80, "裁长")
            assert any(a <= it["start"] and it["end"] <= b
                       for a, b in cfg["rolls"][it["roll_index"]]["sections"]), "落在可用区段"


def test_no_overlap_same_roll():
    # 仅一卷，一对两片都要落在同一卷且不得重叠
    cfg = make_cfg(
        rolls=[{"index": 0, "reference": 0, "sections": [[0, 300]]}],
        pairs=[{"index": 0, "length": 80}],
    )
    cfg["roll_count"] = 1
    cfg["pair_count"] = 1
    ok, items = solver.solve(cfg, [])
    assert ok, items
    intervals = sorted((it["start"], it["end"]) for it in items)
    assert len(intervals) == 2
    for (a, b), (c, d) in zip(intervals, intervals[1:]):
        assert b <= c, f"同卷片重叠: {(a, b)} {(c, d)}"


def test_no_overlap_multi_pairs_one_roll():
    # 一卷 300cm，三对长度 40（共 6 片 240cm），全部排下且互不重叠
    cfg = make_cfg(
        rolls=[{"index": 0, "reference": 0, "sections": [[0, 300]]}],
        pairs=[{"index": 0, "length": 40}, {"index": 1, "length": 40},
               {"index": 2, "length": 40}],
    )
    cfg["roll_count"] = 1
    cfg["pair_count"] = 3
    ok, items = solver.solve(cfg, [])
    assert ok, items
    intervals = sorted((it["start"], it["end"]) for it in items)
    assert len(intervals) == 6
    for (a, b), (c, d) in zip(intervals, intervals[1:]):
        assert b <= c, f"同卷片重叠: {(a, b)} {(c, d)}"


def test_no_solution_when_section_too_short():
    cfg = make_cfg(
        rolls=[{"index": 0, "reference": 0, "sections": [[0, 50]]},
               {"index": 1, "reference": 0, "sections": [[0, 50]]}],
        pairs=[{"index": 0, "length": 80}],
    )
    cfg["roll_count"] = 2
    cfg["pair_count"] = 1
    ok, payload = solver.solve(cfg, [])
    assert not ok
    assert "无解" in payload


def test_cut_blocked_region_not_reused():
    cfg = make_cfg()
    cuts = [{
        "pair_index": 0, "side": 0, "roll_index": 0,
        "start_pos": 0, "end_pos": 80, "reference": 0, "length": 80,
    }]
    ok, items = solver.solve(cfg, cuts)
    assert ok, items
    for it in items:
        if it["roll_index"] == 0 and not it["cut"]:
            assert it["end"] <= 0 or it["start"] >= 80, \
                f"占用了已裁区段 [0,80): {it['start']}-{it['end']}"
    marked = next(it for it in items if (it["pair_index"], it["side"]) == (0, 0))
    assert marked["cut"] is True


def test_single_remaining_side_matches_cut_residue():
    cfg = make_cfg(repeat=12)
    # A 片已裁：起刀 15，卷0基准3 → 余数 (15-3)%12 = 0
    cuts = [{
        "pair_index": 0, "side": 0, "roll_index": 0,
        "start_pos": 15, "end_pos": 95, "reference": 3, "length": 80,
    }]
    cfg["rolls"][0]["reference"] = 3
    ok, items = solver.solve(cfg, cuts)
    assert ok, items
    b = next(it for it in items if (it["pair_index"], it["side"]) == (0, 1))
    ref_b = cfg["rolls"][b["roll_index"]]["reference"]
    assert_equal((b["start"] - ref_b) % 12, 0, "B片须与已裁A片同余数")


def test_different_references_pattern_rule():
    cfg = make_cfg(
        repeat=10,
        rolls=[{"index": 0, "reference": 2, "sections": [[0, 300]]},
               {"index": 1, "reference": 7, "sections": [[0, 300]]}],
        pairs=[{"index": 0, "length": 50}],
    )
    cfg["roll_count"] = 2
    cfg["pair_count"] = 1
    ok, items = solver.solve(cfg, [])
    assert ok, items
    a, b = [it for it in items if it["pair_index"] == 0]
    ra = (a["start"] - cfg["rolls"][a["roll_index"]]["reference"]) % 10
    rb = (b["start"] - cfg["rolls"][b["roll_index"]]["reference"]) % 10
    assert_equal(ra, rb, "不同基准卷的对花余数")


def test_cut_outside_current_section_rejected():
    cfg = make_cfg(
        rolls=[{"index": 0, "reference": 0, "sections": [[100, 200]]},
               {"index": 1, "reference": 0, "sections": [[0, 300]]}],
        pairs=[{"index": 0, "length": 80}],
    )
    cfg["roll_count"] = 2
    cfg["pair_count"] = 1
    cuts = [{
        "pair_index": 0, "side": 0, "roll_index": 0,
        "start_pos": 0, "end_pos": 80, "reference": 0, "length": 80,
    }]
    ok, msg = solver.solve(cfg, cuts)
    assert not ok and "已裁区段" in msg


def test_parse_sections_validation():
    assert solver.parse_sections("0-100, 150-300") == [[0, 100], [150, 300]]
    for bad in ["", "abc", "100-50", "0-301", "0-100, 50-150"]:
        try:
            solver.parse_sections(bad)
        except solver.ConfigError:
            continue
        raise AssertionError(f"非法区段未被拒绝: {bad!r}")


def test_validate_against_cuts_protection():
    cfg = make_cfg()
    cuts = [
        {"pair_index": 0, "side": 0, "roll_index": 0, "reference": 0, "length": 80},
        {"pair_index": 1, "side": 1, "roll_index": 1, "reference": 0, "length": 80},
    ]
    # 改基准 → 报错
    cfg_bad_ref = make_cfg()
    cfg_bad_ref["rolls"][0]["reference"] = 9
    assert solver.validate_against_cuts(cfg_bad_ref, cuts)
    # 改裁长 → 报错
    cfg_bad_len = make_cfg()
    cfg_bad_len["pairs"][1]["length"] = 70
    assert solver.validate_against_cuts(cfg_bad_len, cuts)
    # 只改可用区段 → 允许
    cfg_ok = make_cfg()
    cfg_ok["rolls"][0]["sections"] = [[10, 200]]
    assert solver.validate_against_cuts(cfg_ok, cuts) == []
    # 减少对数/卷数 → 报错
    cfg_fewer = make_cfg(pairs=[{"index": 0, "length": 80}])
    cfg_fewer["pair_count"] = 1
    assert solver.validate_against_cuts(cfg_fewer, cuts)


def test_fingerprint_changes_with_sections():
    c1 = make_cfg()
    c2 = make_cfg()
    c2["rolls"][0]["sections"] = [[0, 299]]
    assert solver.config_fingerprint(c1) != solver.config_fingerprint(c2)


def test_inconsistent_double_cut_residue():
    cfg = make_cfg(repeat=10, pairs=[{"index": 0, "length": 80}])
    cfg["pair_count"] = 1
    cuts = [
        {"pair_index": 0, "side": 0, "roll_index": 0,
         "start_pos": 0, "end_pos": 80, "reference": 0, "length": 80},
        {"pair_index": 0, "side": 1, "roll_index": 1,
         "start_pos": 3, "end_pos": 83, "reference": 0, "length": 80},
    ]
    ok, msg = solver.solve(cfg, cuts)
    assert not ok and "余数不一致" in msg


def test_insufficient_total_length_fast_fail():
    # 单卷 300，6 片×55=330 > 300，容量预检直接无解
    cfg = make_cfg(
        rolls=[{"index": 0, "reference": 0, "sections": [[0, 300]]}],
        pairs=[{"index": 0, "length": 55}, {"index": 1, "length": 55},
               {"index": 2, "length": 55}],
        repeat=7,
    )
    cfg["roll_count"] = 1
    cfg["pair_count"] = 3
    ok, msg = solver.solve(cfg, [])
    assert not ok and ("容量" in msg or "无解" in msg)


def test_fragmented_section_no_placement():
    # 总容量够，但没有任何区段放得下整段裁长
    cfg = make_cfg(
        rolls=[{"index": 0, "reference": 0,
                "sections": [[0, 40], [100, 140], [200, 240]]}],
        pairs=[{"index": 0, "length": 50}],
        repeat=10,
    )
    cfg["roll_count"] = 1
    cfg["pair_count"] = 1
    ok, msg = solver.solve(cfg, [])
    assert not ok and "无解" in msg


def test_same_roll_residue_spacing():
    # 单卷同基准、大花距：两片同余数需相距 repeat，空间不足则无解
    cfg = make_cfg(
        rolls=[{"index": 0, "reference": 0, "sections": [[0, 300]]}],
        pairs=[{"index": 0, "length": 200}],
        repeat=300,
    )
    cfg["roll_count"] = 1
    cfg["pair_count"] = 1
    ok, _ = solver.solve(cfg, [])
    assert not ok
    # 花距小、放得下等距两片时应有解，且两片余数相同
    cfg_ok = make_cfg(
        rolls=[{"index": 0, "reference": 0, "sections": [[0, 300]]}],
        pairs=[{"index": 0, "length": 100}],
        repeat=10,
    )
    cfg_ok["roll_count"] = 1
    cfg_ok["pair_count"] = 1
    ok, items = solver.solve(cfg_ok, [])
    assert ok
    assert items[0]["residue"] == items[1]["residue"]


def test_deterministic_output():
    # 同样输入多次求解结果一致（枚举顺序固定）
    cfg = make_cfg(
        rolls=[{"index": 0, "reference": 0, "sections": [[0, 300]]},
               {"index": 1, "reference": 4, "sections": [[0, 300]]}],
        pairs=[{"index": 0, "length": 70}, {"index": 1, "length": 60}],
        repeat=11,
    )
    first = solver.solve(cfg, [])[1]
    for _ in range(3):
        assert solver.solve(cfg, [])[1] == first


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} 个求解器测试全部通过")
