"""Flask 端到端冒烟测试：参数保存、生成排法、逐片确认、旧排法失效、无解保留记录。

运行：python -I tests/test_smoke.py
"""

import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_DEPS = os.path.join(_ROOT, ".pydeps")
if os.path.isdir(_DEPS):
    sys.path.insert(0, _DEPS)

from app import create_app  # noqa: E402
from app import db as dbmod  # noqa: E402


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


def count_cuts(app):
    with app.app_context():
        return len(dbmod.list_cuts(dbmod.get_db()))


def main():
    tmp = tempfile.mkdtemp(prefix="curtain-test-")
    db_path = os.path.join(tmp, "test.db")
    app = create_app(database=db_path)
    client = app.test_client()

    # 1. 首页可打开
    resp = client.get("/")
    assert resp.status_code == 200
    assert "窗帘对花补裁".encode() in resp.data
    print("PASS 首页渲染正常")

    # 2. 非法区段被拒绝
    bad = config_form(roll_sections=["300-100", "0-300"])
    html = client.post("/config", data=bad, follow_redirects=True).get_data(as_text=True)
    assert any("越界" in t for t in flash_text(html)), flash_text(html)
    print("PASS 非法区段被拒绝并提示中文原因")

    # 3. 保存合法参数并生成排法
    html = client.post("/config", data=config_form(),
                       follow_redirects=True).get_data(as_text=True)
    assert any("参数已保存" in t for t in flash_text(html))
    html = client.post("/solve", data={},
                       follow_redirects=True).get_data(as_text=True)
    assert any("排法已生成" in t for t in flash_text(html)), flash_text(html)
    # 排法表：4 片，每片显示卷号与起止
    result_table = re.search(r'<table class="grid result">(.*?)</table>', html, re.S).group(1)
    rows = re.findall(r"<td>第 \d 卷</td>", result_table)
    assert len(rows) == 4, len(rows)
    assert result_table.count("确认已裁") == 4
    # 对花余数校验：同对两行余数相同
    residues = [int(x) for x in re.findall(
        r"<td>(\d+)</td>\s*<td>(?:已裁|未裁)</td>", result_table)]
    assert residues[0] == residues[1] and residues[2] == residues[3], residues
    print("PASS 生成排法：4 片均给出卷号/起止，同对花位余数一致")

    # 4. 确认第 1 对 A 片 → 已裁入库，剩余 3 片自动重排
    html = client.post("/cut", data={"pair_index": "0", "side": "0"},
                       follow_redirects=True).get_data(as_text=True)
    assert any("已确认" in t for t in flash_text(html)), flash_text(html)
    assert count_cuts(app) == 1
    result_table = re.search(r'<table class="grid result">(.*?)</table>', html, re.S).group(1)
    assert result_table.count("确认已裁") == 3
    assert "已裁" in result_table
    print("PASS 逐片确认：记录写入 SQLite，其余片自动重排")

    # 5. 改可用区段 → 旧排法失效，不能再确认未裁片
    changed = config_form(roll_sections=["0-250", "0-300"])
    client.post("/config", data=changed, follow_redirects=True)
    html = client.post("/cut", data={"pair_index": "0", "side": "1"},
                       follow_redirects=True).get_data(as_text=True)
    assert any("旧排法失效" in t for t in flash_text(html)), flash_text(html)
    assert count_cuts(app) == 1  # 已裁记录保留
    assert "旧排法不可确认" in html
    print("PASS 可用区段改动后旧排法不可确认，已裁记录保留")

    # 6. 重新生成排法后又可以确认
    html = client.post("/solve", data={},
                       follow_redirects=True).get_data(as_text=True)
    assert any("排法已生成" in t for t in flash_text(html))
    html = client.post("/cut", data={"pair_index": "0", "side": "1"},
                       follow_redirects=True).get_data(as_text=True)
    assert any("已确认" in t for t in flash_text(html)), flash_text(html)
    assert count_cuts(app) == 2
    print("PASS 重新排料后可继续确认")

    # 7. 改成无解参数：保留已裁片所在区段，空闲部分缩短到放不下剩余 80cm 片
    #    → 提示无解，已裁记录继续保留
    impossible = config_form(roll_sections=["0-160", "0-50"], lengths=[80, 80])
    client.post("/config", data=impossible, follow_redirects=True)
    html = client.post("/solve", data={},
                       follow_redirects=True).get_data(as_text=True)
    assert any("无解" in t for t in flash_text(html)), flash_text(html)
    assert count_cuts(app) == 2
    print("PASS 排不齐时提示无解，已裁记录继续保留")

    # 8. 有已裁记录时禁止改基准/裁长/减少对数
    protected = config_form(refs=[9, 0, 0])
    html = client.post("/config", data=protected,
                       follow_redirects=True).get_data(as_text=True)
    assert any("花纹基准不能修改" in t for t in flash_text(html)), flash_text(html)
    # 再确认第 2 对的一片，使已裁记录覆盖 pair_index=1
    client.post("/config", data=config_form(), follow_redirects=True)
    client.post("/solve", data={}, follow_redirects=True)
    html = client.post("/cut", data={"pair_index": "1", "side": "0"},
                       follow_redirects=True).get_data(as_text=True)
    assert any("已确认" in t for t in flash_text(html)), flash_text(html)
    assert count_cuts(app) == 3
    shorter = config_form(pair_count=1)
    html = client.post("/config", data=shorter,
                       follow_redirects=True).get_data(as_text=True)
    assert any("不能减少对数" in t for t in flash_text(html)), flash_text(html)
    assert count_cuts(app) == 3
    print("PASS 已裁记录保护：改基准/减少对数被拒绝")

    # 9. 清空记录后恢复正常
    client.post("/reset", data={}, follow_redirects=True)
    assert count_cuts(app) == 0
    client.post("/config", data=config_form(), follow_redirects=True)
    html = client.post("/solve", data={},
                       follow_redirects=True).get_data(as_text=True)
    assert any("排法已生成" in t for t in flash_text(html))
    print("PASS 清空记录后可重新排料")

    print("\n全部冒烟测试通过，数据库文件：", db_path)


if __name__ == "__main__":
    main()
