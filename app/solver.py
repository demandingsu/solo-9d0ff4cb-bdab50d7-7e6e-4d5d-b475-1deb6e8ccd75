"""对花补裁核心逻辑：参数校验与排料求解。

约定：
- 所有位置、长度均为整数厘米；区段使用半开区间 [start, end)。
- 卷号、对号在内部均为 0 基，界面显示时 +1。
- 每对帘片记两片：side=0（A 片）、side=1（B 片）。
- 对花约束：同对两片 (起刀位置 - 所在卷花纹基准) 对花距取余，余数相同。
"""

from __future__ import annotations

import hashlib
import json
import re

MAX_ROLLS = 3
MAX_PAIRS = 3
MAX_POSITION = 300

_SEGMENT_SPLIT_RE = re.compile(r"[,，;；\s]+")
_SEGMENT_RE = re.compile(r"^(-?\d+)\s*[-~～至]\s*(-?\d+)$")


class ConfigError(ValueError):
    """参数表单校验失败，message 为可直接展示的中文说明。"""


def parse_sections(text):
    """解析“0-120, 150-300”形式的可用区段文本。

    返回 [[start, end], ...]；非法输入抛 ConfigError。
    """
    text = (text or "").strip()
    if not text:
        raise ConfigError("可用区段不能为空，例如 0-300")
    sections = []
    for part in _SEGMENT_SPLIT_RE.split(text):
        part = part.strip()
        if not part:
            continue
        m = _SEGMENT_RE.match(part)
        if not m:
            raise ConfigError(f"区段“{part}”格式不正确，应为 起-止，例如 0-300")
        start, end = int(m.group(1)), int(m.group(2))
        if not (0 <= start < end <= MAX_POSITION):
            raise ConfigError(
                f"区段“{part}”越界：起点须 ≥0、终点须 >起点且 ≤{MAX_POSITION} 厘米"
            )
        sections.append([start, end])
    if not sections:
        raise ConfigError("可用区段不能为空，例如 0-300")
    sections.sort()
    for (a1, b1), (a2, b2) in zip(sections, sections[1:]):
        if a2 < b1:
            raise ConfigError(f"区段 {a1}-{b1} 与 {a2}-{b2} 重叠或重复")
    return sections


def _parse_int(raw, field, lo, hi):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{field}必须是整数")
    if not (lo <= value <= hi):
        raise ConfigError(f"{field}必须在 {lo}～{hi} 之间")
    return value


def build_config(form, roll_count, pair_count):
    """从表单字典构建配置 dict，并做完整校验（不考虑历史已裁数据）。

    rolls/pairs 始终保留 3 个槽位，实际生效数量由 roll_count/pair_count 决定，
    模板固定渲染 3 行、按 count 禁用多余行。
    """
    repeat = _parse_int(form.get("repeat"), "花距", 1, MAX_POSITION)
    rolls = []
    for i in range(MAX_ROLLS):
        reference = _parse_int(form.get(f"ref_{i}"), f"第 {i + 1} 卷花纹基准", 0, MAX_POSITION)
        if i < roll_count:
            sections = parse_sections(form.get(f"sections_{i}"))
        else:
            sections = [[0, MAX_POSITION]]
        rolls.append({"index": i, "reference": reference, "sections": sections})
    pairs = []
    for j in range(MAX_PAIRS):
        if j < pair_count:
            length = _parse_int(form.get(f"length_{j}"), f"第 {j + 1} 对裁长", 1, MAX_POSITION)
        else:
            length = 1
        pairs.append({"index": j, "length": length})
    return {
        "roll_count": roll_count,
        "pair_count": pair_count,
        "repeat": repeat,
        "rolls": rolls,
        "pairs": pairs,
    }


def validate_against_cuts(cfg, cuts):
    """改动配置时保护已裁记录：删除卷/对、改基准、改裁长均不允许。"""
    errors = []
    for cut in cuts:
        if cut["roll_index"] >= cfg["roll_count"]:
            errors.append(
                f"第 {cut['roll_index'] + 1} 卷已有已裁记录，不能减少卷数"
            )
        if cut["pair_index"] >= cfg["pair_count"]:
            errors.append(
                f"第 {cut['pair_index'] + 1} 对已有已裁记录，不能减少对数"
            )
    roll_by_index = {r["index"]: r for r in cfg["rolls"]}
    pair_by_index = {p["index"]: p for p in cfg["pairs"]}
    for cut in cuts:
        roll = roll_by_index.get(cut["roll_index"])
        if roll is not None and cut.get("reference") is not None:
            if cut["reference"] != roll["reference"]:
                errors.append(
                    f"第 {roll['index'] + 1} 卷已有已裁记录，花纹基准不能修改"
                )
        pair = pair_by_index.get(cut["pair_index"])
        if pair is not None and cut.get("length") is not None:
            if cut["length"] != pair["length"]:
                errors.append(
                    f"第 {pair['index'] + 1} 对已有已裁记录，裁长不能修改"
                )
    return errors


def config_fingerprint(cfg):
    blob = json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _subtract_cuts(intervals, cut_intervals):
    """从可用区段中扣掉已裁区间，返回剩余空闲区间列表。"""
    remaining = []
    for a, b in intervals:
        pieces = [[a, b]]
        for ca, cb in cut_intervals:
            updated = []
            for x, y in pieces:
                if cb <= x or ca >= y:
                    updated.append([x, y])
                    continue
                if ca > x:
                    updated.append([x, ca])
                if cb < y:
                    updated.append([cb, y])
            pieces = updated
        remaining.extend(pieces)
    return remaining


def _overlaps(occupied_list, start, end):
    return any(not (end <= s or e <= start) for s, e in occupied_list)


def _candidates(free_intervals, length, reference, repeat, residue):
    """枚举某卷上满足花位余数、且能容纳 length 的全部起刀位置（升序）。"""
    out = []
    for a, b in free_intervals:
        if b - a < length:
            continue
        current = (a - reference) % repeat
        delta = (residue - current) % repeat
        start = a + delta
        while start + length <= b:
            out.append(start)
            start += repeat
    return out


def solve(cfg, cuts):
    """对未裁片排料。

    返回 (ok, payload)：
    - 成功：(True, [item, ...])，item 含 pair_index/side/roll_index/start/end/residue/cut；
    - 失败：(False, "中文原因")。
    已裁片原样列入结果并标记 cut=True。
    """
    repeat = cfg["repeat"]
    rolls = [r for r in cfg["rolls"] if r["index"] < cfg["roll_count"]]
    pairs = [p for p in cfg["pairs"] if p["index"] < cfg["pair_count"]]
    refs = {r["index"]: r["reference"] for r in rolls}

    cut_map = {(c["pair_index"], c["side"]): c for c in cuts}

    # 防御性检查：已裁记录必须仍在当前配置范围内
    for c in cuts:
        if c["roll_index"] not in refs:
            return False, f"已裁记录引用了不存在的第 {c['roll_index'] + 1} 卷"
        if c["pair_index"] >= len(pairs):
            return False, f"已裁记录引用了不存在的第 {c['pair_index'] + 1} 对"
        if not any(a <= c["start_pos"] and c["end_pos"] <= b
                   for a, b in next(r for r in rolls if r["index"] == c["roll_index"])["sections"]):
            return False, (
                f"第 {c['pair_index'] + 1} 对"
                f"{'A' if c['side'] == 0 else 'B'}片的已裁区段 "
                f"{c['start_pos']}-{c['end_pos']} 已不在当前可用区段内"
            )

    # 同对两片都已裁时，历史花位余数必须一致
    pair_cut_count = {p["index"]: 0 for p in pairs}
    for (pj, side), c in cut_map.items():
        pair_cut_count[pj] = pair_cut_count.get(pj, 0) + 1
    for pj, count in pair_cut_count.items():
        if count == 2:
            residues = {
                (c["start_pos"] - refs[c["roll_index"]]) % repeat
                for (ppj, _side), c in cut_map.items()
                if ppj == pj
            }
            if len(residues) > 1:
                return False, f"第 {pj + 1} 对两片已裁记录的花位余数不一致，无法继续排料"

    # 每卷：可用区段扣掉已裁区间
    free = {}
    for r in rolls:
        roll_cuts = sorted(
            (c["start_pos"], c["end_pos"])
            for c in cuts
            if c["roll_index"] == r["index"]
        )
        free[r["index"]] = _subtract_cuts(
            [tuple(s) for s in r["sections"]], roll_cuts
        )

    lengths = {p["index"]: p["length"] for p in pairs}

    # 任务清单：双未裁对、单未裁对；已裁两片的对无需排
    tasks = []
    for p in pairs:
        pj = p["index"]
        cut_sides = sorted(side for (ppj, side) in cut_map if ppj == pj)
        if cut_sides == [0, 1]:
            continue
        if len(cut_sides) == 1:
            cut_side = cut_sides[0]
            cut = cut_map[(pj, cut_side)]
            residue = (cut["start_pos"] - refs[cut["roll_index"]]) % repeat
            tasks.append({"kind": "single", "pair": pj,
                          "side": 1 - cut_side, "residue": residue,
                          "length": lengths[pj]})
        else:
            tasks.append({"kind": "double", "pair": pj,
                          "residue": None, "length": lengths[pj]})

    # 预计算 (卷, 余数) -> 起刀位置列表，避免搜索中反复枚举。
    # 双未裁对的余数任意，故覆盖 0..repeat-1（最多 300 个余数 × 3 卷）。
    candidates = {}  # (roll_index, residue) -> [start,...]
    for r in rolls:
        ri = r["index"]
        for residue in range(repeat):
            candidates[(ri, residue)] = _candidates(
                free[ri], 1, r["reference"], repeat, residue
            )

    def _segment_end(ri, start):
        for a, b in free[ri]:
            if a <= start < b:
                return b
        return None

    def _all_starts(ri, length):
        # 某卷上指定裁长的全部可行起刀（任意余数），带各自余数
        out = []
        for residue in range(repeat):
            for start in candidates[(ri, residue)]:
                seg_end = _segment_end(ri, start)
                if seg_end is not None and start + length <= seg_end:
                    out.append((start, residue))
        return out

    occupied = {r["index"]: [] for r in rolls}
    placements = {}

    def _free(ri):
        total = sum(b - a for a, b in free[ri])
        used = sum(b - a for a, b in occupied[ri])
        return total - used

    def _fits(ri, start, length):
        end = start + length
        if not any(a <= start and end <= b for a, b in free[ri]):
            return False
        return not _overlaps(occupied[ri], start, end)

    def _single_options(task):
        length, residue = task["length"], task["residue"]
        out = []
        for r in rolls:
            ri = r["index"]
            for start in candidates[(ri, residue)]:
                if _fits(ri, start, length):
                    out.append((ri, start))
        return out

    def _double_options(task):
        # 返回 (ri_a, start_a, residue) 且该 A 选择至少能配到一个 B
        length = task["length"]
        out = []
        for r in rolls:
            ri = r["index"]
            for start_a, residue in _all_starts(ri, length):
                if not _fits(ri, start_a, length):
                    continue
                if _has_b(length, residue, ri, start_a, length):
                    out.append((ri, start_a, residue))
        return out

    def _has_b(length, residue, ri_a, start_a, len_a):
        end_a = start_a + len_a
        for r in rolls:
            ri = r["index"]
            for start_b in candidates[(ri, residue)]:
                end_b = start_b + length
                if not any(a <= start_b and end_b <= b for a, b in free[ri]):
                    continue
                if _overlaps(occupied[ri], start_b, end_b):
                    continue
                # A、B 同卷时还要排除彼此
                if ri == ri_a and not (end_b <= start_a or end_a <= start_b):
                    continue
                return True
        return False

    def _b_options(task, ri_a, start_a, residue):
        length = task["length"]
        end_a = start_a + length
        out = []
        for r in rolls:
            ri = r["index"]
            for start_b in candidates[(ri, residue)]:
                end_b = start_b + length
                if not any(a <= start_b and end_b <= b for a, b in free[ri]):
                    continue
                if _overlaps(occupied[ri], start_b, end_b):
                    continue
                if ri == ri_a and not (end_b <= start_a or end_a <= start_b):
                    continue
                out.append((ri, start_b))
        return out

    # 容量预检：每卷剩余空闲必须容纳得下分配约束的总量
    total_needed = sum(t["length"] * (1 if t["kind"] == "single" else 2)
                       for t in tasks)
    if sum(_free(r["index"]) for r in rolls) < total_needed:
        return False, "无解：各卷可用区段总长不足，放不下全部待裁帘片。已裁记录继续保留。"

    def _forward_check(remaining):
        # 容量再检
        need = sum(t["length"] * (1 if t["kind"] == "single" else 2)
                   for t in remaining)
        if sum(_free(r["index"]) for r in rolls) < need:
            return False
        for t in remaining:
            if t["kind"] == "single":
                if not _single_options(t):
                    return False
            else:
                if not _double_options(t):
                    return False
        return True

    def dfs(remaining):
        if not remaining:
            return True
        # MRV：选当前可行分支最少的任务
        def branch_count(t):
            if t["kind"] == "single":
                return len(_single_options(t))
            return len(_double_options(t))

        scored = [(branch_count(t), i, t) for i, t in enumerate(remaining)]
        scored.sort(key=lambda x: (x[0], x[1]))
        if scored[0][0] == 0:
            return False
        _, pick_idx, task = min(scored, key=lambda x: x[0])
        rest = remaining[:pick_idx] + remaining[pick_idx + 1:]
        pj = task["pair"]

        if task["kind"] == "single":
            side = task["side"]
            length = task["length"]
            for ri, start in _single_options(task):
                end = start + length
                occupied[ri].append((start, end))
                placements[(pj, side)] = (ri, start, end)
                if _forward_check(rest) and dfs(rest):
                    return True
                placements.pop((pj, side))
                occupied[ri].pop()
            return False

        length = task["length"]
        for ri_a, start_a, residue in _double_options(task):
            end_a = start_a + length
            occupied[ri_a].append((start_a, end_a))
            placements[(pj, 0)] = (ri_a, start_a, end_a)
            for ri_b, start_b in _b_options(task, ri_a, start_a, residue):
                end_b = start_b + length
                occupied[ri_b].append((start_b, end_b))
                placements[(pj, 1)] = (ri_b, start_b, end_b)
                if _forward_check(rest) and dfs(rest):
                    return True
                placements.pop((pj, 1))
                occupied[ri_b].pop()
            placements.pop((pj, 0))
            occupied[ri_a].pop()
        return False

    if not dfs(tasks):
        detail = "；".join(
            (f"第 {t['pair'] + 1} 对剩余单片无法满足花位/区段约束"
             if t["kind"] == "single"
             else f"第 {t['pair'] + 1} 对找不到满足对花余数的两片")
            for t in tasks
        )
        return False, f"无解：{detail or '没有可排的帘片'}。已裁记录继续保留。"

    items = []
    for p in pairs:
        pj = p["index"]
        for side in (0, 1):
            key = (pj, side)
            if key in cut_map:
                c = cut_map[key]
                ri, start, end = c["roll_index"], c["start_pos"], c["end_pos"]
                items.append({
                    "pair_index": pj, "side": side, "roll_index": ri,
                    "start": start, "end": end,
                    "residue": (start - refs[ri]) % repeat, "cut": True,
                })
            else:
                ri, start, end = placements[key]
                items.append({
                    "pair_index": pj, "side": side, "roll_index": ri,
                    "start": start, "end": end,
                    "residue": (start - refs[ri]) % repeat, "cut": False,
                })
    return True, items
