# 提花纹板链双路扫描合片服务（Jacquard Merge）

旧式提花纹板链在两次光学扫描中会出现三类典型损伤：

- **断链漏张**：整张纹板在一路中缺失；
- **回带重扫**：同一张纹板在一路中出现两次；
- **灰尘假孔**：污点被识别成孔，造成单针位分歧。

若在漏张处直接按相同下标“硬合并”，漏张之后的所有花纹都会整体错位。
本服务接收两路有序扫描（纹板 + 带唯一标号的隔板），以**隔板强制相配、
段内单调全局对齐**的方式生成可复刻的纹板母版：漏张/重扫作为缺口保留固定
槽位并进入复扫清单，后续纹板的物理身份永不串位。

## 裁决规则

1. **隔板同序强制相配**：两路隔板按顺序逐位比较标号；首个不一致点即为
   顺序冲突，其后所有隔板不再相配，受影响段落一律挂起。
2. **分段单调全局对齐**（Needleman–Wunsch 变体），每个由相配隔板围成的段独立对齐：
   - 配对代价 = 两掩码的**汉明距离**；
   - 插入 / 删除（单边纹板）代价 = **4**；
   - 只允许单调、不交叉的配对。
3. **最优解裁决顺序**：
   1. 总代价最小；
   2. 总缺口（插入 + 删除）数最少；
   3. 仍并列时，**配对索引序列字典序最小**。
      配对索引序列为命中配对的段内下标元组
      `((i1,j1),(i2,j2),…)`，按 Python 元组字典序比较——即并列时优先
      “尽早、按下标自然顺序配对”的方案，保证结果唯一、可复刻。
4. **母版孔位裁决**：
   - 配对纹板的针位一致 → 该针位确定（`0`/`1`）；
   - 针位分歧（灰尘假孔等）→ 母版记 `?`，该针位进入复扫清单；
   - 单边纹板（漏扫/重扫）→ 保留一个全部 `?` 的挂起槽位，整张进入复扫清单，
     **绝不**用下一张纹板顶替。
5. **发布门控**：隔板不全、顺序冲突、或存在任何未决针位时，发布接口返回
   `409`，只有复扫补全后重新提交的任务可发布。

### 段落安全边界

- 头段（第一张隔板之前）以及相配隔板之间的段：两侧都有相配隔板封界，独立对齐；
- 最后一张相配隔板**之后的尾段**：只有当两路隔板恰好全部同序相配
  （无冲突、无缺张、无多余隔板）时才对齐；否则尾段身份无界，全部挂起待复扫。
- 若某处隔板顺序冲突，冲突点两侧的隔板与后续段不再跨路配对，两路剩余内容
  各自挂起，从结构上杜绝“按下标硬合”导致的整链串位。

## 数据模型

请求体（`POST /tasks`）：

```json
{
  "name": "chain-1907-c",
  "needle_count": 8,
  "tracks": [
    {"name": "scan-A", "items": [
      {"type": "card", "mask": "11110000"},
      {"type": "separator", "label": "S1"},
      {"type": "card", "mask": "00001111"}
    ]},
    {"name": "scan-B", "items": [
      {"type": "card", "mask": "11110000"},
      {"type": "separator", "label": "S1"},
      {"type": "card", "mask": "00001111"}
    ]}
  ]
}
```

- `needle_count`：统一针位数；所有纹板掩码必须恰好是该长度的 `0`/`1` 串；
- `tracks`：必须恰好两路，每路 `items` 保持物理扫描顺序；
- 隔板 `label` 在**单路内必须唯一且非空**，两路隔板必须同序相配。

错误响应统一定位到**路次 / 纹板 / 针位**（均为 1 基）：

```json
{
  "error": {
    "code": "mask_invalid_char",
    "message": "掩码第 3 针位出现非法字符 'x'（灰尘/坏掩码）",
    "location": {"track": 1, "item": 1, "needle": 3}
  }
}
```

结果关键字段：

| 字段 | 含义 |
| --- | --- |
| `input_hash` | 规范化请求体的 SHA-256，随任务持久化，保证母版可复刻可追溯 |
| `separator_review` | 相配标号前缀、相配数与隔板缺陷（缺张 / 顺序冲突） |
| `segments[]` | 每段的 `cost`、`gaps`、`pairs`（配对索引序列）、逐操作 `ops` |
| `alignment_totals` | 全链总代价、总缺口数、总替换代价 |
| `master.entries[]` | 母版槽位：纹板 `confirmed`/`pending` 或隔板；`seq` 全局固定身份 |
| `rescan[]` | 复扫清单，含原因、路次、纹板位置与未决针位 |
| `verdict` | `publishable`、`blockers` 与裁决汇总 |

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/tasks` | 创建合片任务：校验 → 隔板相配 → 分段对齐 → 裁决 → 落库（`201`） |
| `GET` | `/tasks/{id}` | 读取任务结果（含哈希、映射、代价、裁决；`404`） |
| `POST` | `/tasks/{id}/publish` | 发布母版（幂等）；未决时 `409` 并附 blockers 与复扫清单 |
| `GET` | `/health` | 存活探针 |

- `POST /tasks` 的请求级错误（坏掩码、缺标号、标号重复、结构错误等）返回
  `400` 并带位置；隔板跨路缺陷（缺张 / 顺序冲突）任务会创建为 `blocked`，
  裁决随结果持久化，但禁止发布。
- 已发布任务再次调用发布接口返回 `200`，`published_at` 保持首次发布时间。

快速试跑：

```bash
curl -s -X POST localhost:8080/tasks -H 'content-type: application/json' -d '{
  "needle_count": 8,
  "tracks": [
    {"items": [{"type":"card","mask":"11110000"},
               {"type":"card","mask":"10101010"},
               {"type":"separator","label":"S1"},
               {"type":"card","mask":"00001111"}]},
    {"items": [{"type":"card","mask":"11110000"},
               {"type":"separator","label":"S1"},
               {"type":"card","mask":"00001111"}]}
  ]
}'
```

第二路漏掉的 `10101010` 会在母版中保留 `????????` 挂起槽位（不影响
S1 后纹板的身份），并在 `rescan` 中定位到 `track=1, item=2`。

## 运行（Docker Compose）

```bash
# 默认宿主端口 8080 → 容器 8080
docker compose up --build -d

# 用 API_PORT 覆盖宿主端口
API_PORT=9090 docker compose up --build -d

curl localhost:8080/health
```

数据持久化于命名卷 `jacquard-data`（容器内 `/data/jacquard.db`，SQLite，
可用环境变量 `DB_PATH` 覆盖路径）。

### 一次性验收服务 verify

`verify` 服务会等待 API 健康后，在一次性容器内运行**全部单元 / 接口测试**
与端到端 HTTP 验收脚本，结束即退出，退出码即验收结论：

```bash
docker compose --profile verify up --build verify
# 尾部出现 “verify: ALL CHECKS PASSED” 即通过
docker compose --profile verify rm -f verify   # 清理一次性容器
```

## 本地开发与测试

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

pytest -q                          # 单元 + HTTP 接口测试（临时 SQLite）
BASE_URL=http://localhost:8080 python scripts/acceptance.py   # 端到端验收

DB_PATH=/tmp/jacquard.db uvicorn app.main:app --port 8080
```

## 仓库结构

```
app/
  alignment.py   # 单调全局对齐 + 三级裁决
  merge.py       # 校验、输入哈希、隔板相配、分段与母版/复扫装配
  models.py      # 域模型
  errors.py      # 可定位错误（路次/纹板/针位）
  storage.py     # SQLite：输入哈希、映射、代价、裁决持久化
  main.py        # FastAPI：创建 / 读取 / 发布 / 健康检查
tests/           # 对齐裁决、合片装配、HTTP 接口测试
scripts/
  acceptance.py  # verify 服务使用的端到端验收脚本
docker-compose.yml   # api（API_PORT 可覆盖宿主端口）+ 一次性 verify
Dockerfile
```

## 需求场景与测试对应

| 场景 | 测试 |
| --- | --- |
| 断链漏扫、后续不串位 | `test_missing_card_does_not_shift_later_match`、`test_missing_card_keeps_later_positions_stable`、`test_missing_card_scenarios_end_to_end_no_shift` |
| 回带重扫 | `test_duplicate_scan_is_gap_not_forced_match`、`test_duplicate_scan_marked_single_side_without_shift` |
| 灰尘假孔 / 针位分歧 | `test_dust_disagreement_is_cheap_substitution`、`test_dust_disagreement_marks_exact_needles` |
| 隔板缺张 / 顺序冲突 | `test_separator_missing_on_one_track`、`test_separator_order_conflict`、`test_separator_prefix_matched_head_still_aligned` |
| 最优解平局（代价 → 缺口 → 字典序） | `test_tie_break_fewest_gaps`、`test_tie_break_lexicographic_pairs_explicit`、`test_bruteforce_cross_check`（80 随机例穷举校验） |
| 坏掩码定位 | `test_bad_requests_are_located`、`test_bad_mask_returns_400_with_needle_location` |
| 发布门控与幂等 | `test_publish_ready_task_returns_master`、`test_publish_blocked_task_is_409_with_locations` |
