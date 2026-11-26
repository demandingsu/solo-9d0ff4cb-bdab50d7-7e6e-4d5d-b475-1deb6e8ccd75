# 窗帘对花补裁（Flask + SQLite）

师傅按排法下刀，遇到布疵时改用余布补齐剩下的帘片。本应用根据同花色布料的
**花距、各卷花纹基准、可用区段**和**每对帘片裁长**自动排料，并支持逐片确认已裁。

## 业务规则

- 最多 **3 卷**布、**3 对**帘片；每卷最长 **300 厘米**；位置与长度均为整数厘米。
- 可用区段用半开区间 `[起, 止)` 表示，支持多段，如 `0-120, 150-300`。
- 排料结果按片给出**卷号、起刀位置、止刀位置**，并保证：
  - 每片完整落在所在卷的可用区段内；
  - 同一卷各片互不重叠，且不会占用已裁区段；
  - 对花：同一对两片 `(起刀位置 − 所在卷花纹基准) mod 花距` 余数相同。
- 师傅可按当前排法**逐片确认已裁**，记录写入 SQLite，确认后剩余帘片自动重新排齐。
- **可用区段（或任何参数）一改，旧排法立即失效**，不能再按旧排法确认未裁片，
  需重新生成排法；已裁记录始终保留。
- 排不齐时页面提示“无解”及原因，已裁记录继续保留。

## 启动（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

SQLite 数据文件位于命名卷 `curtain_data` 的 `/data/curtain.db`，容器重建不丢数据。
生产环境请通过环境变量 `SECRET_KEY` 覆盖默认密钥。

## 本地（无 Docker）运行

```bash
pip install -r requirements.txt
DATABASE_PATH=./curtain.db python wsgi.py
# 或开发模式：FLASK_APP=wsgi:application flask --app app run --debug
```

## 测试

```bash
python -I tests/test_solver.py   # 求解器规则单元测试（16 项）
python -I tests/test_smoke.py    # Flask 端到端冒烟测试（9 项）
```

## 目录结构

```
app/
  __init__.py        Flask 工厂与路由（参数/排料/确认/重置）
  solver.py          区段解析、参数校验、对花排料回溯求解
  db.py              SQLite 建表与存取
  templates/index.html  单页表单、排法表、已裁记录表
  static/            样式与前端联动
wsgi.py              waitress 生产入口
Dockerfile / docker-compose.yml
tests/               单元测试与端到端冒烟测试
```

## 求解器说明

未裁帘片建模为约束满足问题，深度优先回溯求解，并采用：

- **容量预检/前向检查**：总长不足或某任务已无可放位置时立即剪枝；
- **MRV（最少可选值优先）**：每次先挑可行分支最少的帘片，显著缩小搜索空间；
- 候选起刀位置按花位余数预计算并按卷号、位置升序枚举，结果**确定可复现**。

问题规模很小（≤3 卷、≤6 片、每卷 ≤300cm），求解在毫秒级完成。
