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

1. **隔板同序相配（LCS 锚点，不是逐位硬比）**：对两路隔板标号求最长公共
   子序列对齐——同标号且保持相对顺序的隔板成为相配锚点；一路缺某张隔板
   只产生两个缺口，**不会**被误报成“下一张隔板顺序冲突”。
   平局裁决与纹板对齐一致：缺口（未配隔板）数最少，仍并列时配对索引序列
   字典序最小。
   - 某标号只出现在一路 → `separator_missing`，定位到那一路，修复目标是**补隔板**；
   - 某标号两路都有但无法同序相配（两侧都落在缺口）→ `separator_order`，
     各指向两路中的真实位置，修复目标是**理顺顺序**。
2. **分段单调全局对齐**（Needleman–Wunsch 变体），每个由相配隔板锚点围成、
   且区间内没有未配隔板的段独立对齐；含未配隔板的区间身份无界，整体挂起：
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
4. **母版孔位与编号**：
   - 配对纹板的针位一致 → 该针位确定（`0`/`1`）；
   - 针位分歧（灰尘假孔等）→ 母版记 `?`，该针位进入复扫清单；
   - 单边纹板（漏扫/重扫）→ 保留一个全部 `?` 的挂起槽位，整张进入复扫清单，
     **绝不**用下一张纹板顶替；
   - `slot` 是整链**全局唯一**的纹板槽位号（隔板不占号），即使隔板冲突后
     两路纹板分别挂起也不会重号，外部系统可凭 `slot`（或 `(segment,slot)`）
     唯一引用槽位；`seq` 为含隔板的全局条目号。
5. **发布门控**：隔板不全、顺序冲突、或存在任何未决针位时，发布接口返回
   `409`，只有复扫补全后重新提交的任务可发布。
6. **畸形输入不中断服务**：异常扫描标签（孤立代理字符、非字符串、空白、
   超长等）在校验层转成带位置的 `400`，不会以 500 中断任务创建。

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
tests/           # 对齐裁决、合片装配、HTTP 接口、畸形输入 fuzz 测试
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
| 一路漏隔板不被误报成顺序冲突 | `test_missing_separator_not_misreported_as_order`、`test_missing_separator_not_misreported_via_api` |
| 隔板顺序冲突定位到真实位置 | `test_separator_order_conflict`、`test_separator_order_conflict_points_at_real_positions` |
| 冲突后母版槽位全局唯一 | `test_master_slots_globally_unique_under_conflict`、`test_master_slots_globally_unique_via_api` |
| 畸形扫描标签不中断服务 | `test_malformed_separator_label_is_actionable_400`、`test_malformed_label_returns_400_not_500`、`test_fuzz.py`（120 随机结构 + 代理字符 fuzz） |
| 最优解平局（代价 → 缺口 → 字典序） | `test_tie_break_fewest_gaps`、`test_tie_break_lexicographic_pairs_explicit`、`test_bruteforce_cross_check`（80 随机例穷举校验） |
| 坏掩码定位 | `test_bad_requests_are_located`、`test_bad_mask_returns_400_with_needle_location` |
| 发布门控与幂等 | `test_publish_ready_task_returns_master`、`test_publish_blocked_task_is_409_with_locations` |
