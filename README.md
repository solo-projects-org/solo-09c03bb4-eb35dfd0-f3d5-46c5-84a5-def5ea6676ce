# 联合目录补采采集服务

联合目录管理员对单馆书目进行补采的 OAI-PMH 采集服务。核心保证：**翻页失败绝不让半批记录入库**；
留存期到期的撤下书目可**清空元数据但保留撤下记录**，清理过的标识在补采时按库内 datestamp 守卫。

## 需求与实现对照

| 需求 | 实现 |
| --- | --- |
| FastAPI 接收截止日，首次另填起始日 | `POST /api/v1/harvest`，请求体 `until` 必填、`from` 选填 |
| 之后从上次成功采集的截止日接着采，起止日都包含 | `from` 留空时取 `harvest_progress.last_until` 作为起始日（含当天） |
| OAI-PMH ListRecords 按日期拉取 oai_dc | 首请求 `verb=ListRecords&metadataPrefix=oai_dc&from=...&until=...` |
| 续页用 resumptionToken 替代日期和格式参数 | 续页请求只带 `verb` + `resumptionToken`；样例源对混用参数返回 `badArgument` |
| 令牌失效丢弃已取页，按原区间重抓一次 | 捕获 `badResumptionToken` 后清空已取页，用原始 from/until 重新拉取；只重抓一次 |
| 按标识更新 SQLite 书目 | `bib_records.identifier` 主键 upsert，重复采集幂等 |
| 遇删除标记就撤下 | header `status=deleted` 的记录置为 `withdrawn`（库中不存在则写墓碑） |
| 整批采完才提交书目变更和截止日 | 单事务 `BEGIN IMMEDIATE`，全部页取完才写库并提交；异常整体回滚 |
| 无匹配也推进截止日 | `noRecordsMatch` 不写书目，同事务推进 `last_until` |
| 清理 API 接收截止日，只处理仍撤下且 datestamp 不晚于该日的书目 | `POST /api/v1/cleanup`，按 `status='withdrawn' AND write_guarded=0 AND datestamp <= until` 选取 |
| 清空 Dublin Core 各列、raw_dc 置空对象，保留 OAI 标识/日期/撤下状态 | 14 个 DC 列置 NULL、`raw_dc='{}'`，identifier/datestamp/status 原样保留 |
| 返回本次实际清掉元数据的标识，不改续采进度 | 响应 `cleared_identifiers`；清理不触碰 `harvest_progress`，`last_until` 不变 |
| 清理后补采用库内 datestamp 日粒度比较 | `write_guarded=1` 的标识走守卫分支：更早忽略、同日冲突保留撤下、较新在架可重新上架 |
| 同日在架与撤下冲突保留撤下状态 | 同日（含同日删除标记）到来记录整体跳过，库内状态不变，计入 `skipped` |
| 较新在架版本可重新上架 | 较新在架记录写回完整 DC、状态转 active，守卫标记保留 |
| 翻页等待期间馆员仍可完成清理 | 清理不获取采集锁，独立短事务；采集在网络翻页（未开事务）时清理可正常提交 |
| 重新上架或服务重启后规则仍生效 | 守卫标记持久化在 SQLite（`write_guarded`），重启/重新上架均不清除 |
| 清理与补采各自整批提交，失败不留半批 | 两者均在单事务内完成，异常整体 rollback；采集另记 `harvest_runs.failed` |
| 可切换书目版本的本地样例源 | 样例源提供 `POST /admin/records/<id>`，切换旧版/同日在架/新版/删除 |
| Docker Compose 启动服务和本地分页样例源 | `docker compose up --build` 启动 `harvester-api` + `oai-source` |
| Swagger 演示清理后重采旧版和新版 | http://localhost:8000/docs（见下文“清理与重采演示”） |

## 目录结构

```
app/                  FastAPI 采集服务
  main.py             路由（采集/清理/目录/进度/运行历史/健康检查）
  harvester.py        采集编排：翻页、令牌失效重抓、整批事务、守卫写入规则
  cleanup.py          清理编排：独立整批事务，不抢采集锁
  oai_client.py       OAI-PMH 2.0 客户端（httpx + xml.etree）
  db.py               SQLite 表结构、迁移、事务操作、清理与守卫查询
  schemas.py          Pydantic 请求/响应模型
  Dockerfile
sample-oai/           本地分页样例 OAI-PMH 源（纯标准库）
  server.py           27 条样例记录（rec-013 默认为删除记录），分页 + 版本切换 admin
  Dockerfile
tests/                pytest 端到端测试（33 个用例）
scripts/              辅助脚本（故障回滚演示）
docker-compose.yml
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 采集服务 API / Swagger：http://localhost:8000/docs
- 样例 OAI 源：http://localhost:8080/oai?verb=Identify
- SQLite 数据保存在命名卷 `harvester-data`（容器内 `/data/harvester.db`）

## 操作流程（Swagger 或 curl）

### 1. 首次采集：另填起始日，起止日都包含

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-15"}'
```

响应：

```json
{
  "status": "success",
  "from_date": "2026-10-01",
  "until_date": "2026-10-15",
  "pages": 2, "fetched": 15,
  "inserted": 14, "updated": 0, "withdrawn": 1,
  "retries": 0, "run_id": 1
}
```

### 2. 后续采集：只填截止日，自动从上次截止日（含）续采

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"until": "2026-10-27"}'
# 实际区间：2026-10-15 ~ 2026-10-27（last_until 含当天，避免边界漏采）
```

### 3. 查询目录与进度

```bash
curl 'http://localhost:8000/api/v1/catalog?status=active'   # 在架（默认）
curl 'http://localhost:8000/api/v1/catalog?status=all'      # 含已撤下
curl 'http://localhost:8000/api/v1/catalog/oai:sample.lib:rec-013'
curl 'http://localhost:8000/api/v1/progress'                # 上次成功截止日
curl 'http://localhost:8000/api/v1/runs'                    # 采集运行历史
```

### 4. 无匹配也推进截止日

对没有任何记录的区间发起采集，返回 `fetched=0` 且 `last_until` 照常推进。

## 元数据清理（留存期到期的撤下书目）

撤下记录过了成员馆约定的留存期后，馆员清掉元数据但保留撤下痕迹：

```bash
# 截止日（含当天）：只处理仍为 withdrawn 且 datestamp <= 2026-10-13 的书目
curl -X POST http://localhost:8000/api/v1/cleanup \
  -H 'Content-Type: application/json' \
  -d '{"until": "2026-10-13"}'
```

响应返回**本次实际清掉元数据**的标识（重复调用幂等，已清理的不会再返回）：

```json
{
  "cutoff_date": "2026-10-13",
  "count": 1,
  "cleared_identifiers": ["oai:sample.lib:rec-013"]
}
```

清理语义：

- 清空 14 个 Dublin Core 列，`raw_dc` 置为空对象 `{}`；
- **保留** OAI 标识、datestamp、`withdrawn` 撤下状态；
- 打上持久守卫标记 `write_guarded=1`（接口输出 `write_guarded` / `cleared_at`）；
- 不触碰 `harvest_progress`，续采截止日 `last_until` 完全不变；
- 单事务整批提交，任何失败整体回滚，不留半批；
- 清理不持有采集锁：补采在源馆翻页等待（网络 IO，未开写事务）时，清理仍可即时完成。

## 清理后补采的写入规则

对 `write_guarded=1` 的标识，补采写库时拿到来记录的 datestamp 与**库内当前 datestamp**
做日粒度（`YYYY-MM-DD`）比较：

| 到来记录相对库内 | 处理 |
| --- | --- |
| 更早（`<`） | 忽略，库内现状不变，计入 `skipped` |
| 同日（`=`） | 在架与撤下冲突时**保留撤下状态**：同日到来记录（含同日删除标记）整体跳过 |
| 更新（`>`）且在架 | 重新上架，写回完整 DC；守卫标记保留，规则继续生效 |
| 更新（`>`）且删除 | 保持撤下、更新 datestamp，不恢复元数据 |

未清理过的标识沿用原采集语义（删除标记撤下，否则按标识 upsert），不受守卫规则影响。
守卫标记持久化在 SQLite，**书目重新上架或服务重启后规则仍然生效**。

## 清理后重采旧版和新版（Swagger / curl 演示）

样例源内置的 `rec-013` 默认是删除记录（datestamp `2026-10-13`）。先用 admin 端点切换
该书目的版本，再经 Swagger（http://localhost:8000/docs）发起采集观察差异。

### 步骤 0：先采集再清理

```bash
curl -X POST http://localhost:8000/api/v1/harvest -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-15"}'
curl -X POST http://localhost:8000/api/v1/cleanup -H 'Content-Type: application/json' \
  -d '{"until": "2026-10-13"}'
```

### 步骤 1：样例源切换书目版本

```bash
# 支持 JSON 请求体（Swagger 友好）或 ?version= 查询参数
curl -X POST http://localhost:8080/admin/records/oai:sample.lib:rec-013 \
  -H 'Content-Type: application/json' -d '{"version": "active-old"}'
# 可选版本：
#   active-old   在架，datestamp=2026-10-12（更早，重采应被忽略）
#   active-same  在架，datestamp=2026-10-13（同日冲突，保留撤下）
#   active-new   在架，datestamp=2026-11-12（较新在架，重新上架）
#   deleted-new  删除，datestamp=2026-11-12（较新删除，保持撤下）
#   default      清除覆盖，恢复内置版本
curl http://localhost:8080/admin/state          # 查看当前覆盖与令牌状态
```

### 步骤 2：重采并观察

```bash
# 旧版/同日：窗口覆盖 10 月即可
curl -X POST http://localhost:8000/api/v1/harvest -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-31"}'
# 新版（11 月 datestamp）：窗口需延伸到 11 月
curl -X POST http://localhost:8000/api/v1/harvest -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-11-30"}'

curl http://localhost:8000/api/v1/catalog/oai:sample.lib:rec-013
```

预期：

- `active-old` → 响应 `skipped=1`，记录仍 `withdrawn`、datestamp 不变；
- `active-same` → 响应 `skipped=1`，同日在架无法覆盖撤下状态；
- `active-new` → 响应 `skipped=0`、`updated` 增加，记录变 `active` 且写回完整 DC，
  `write_guarded` 仍为 `true`；
- 之后再切回 `active-old` 重采，即便记录已重新上架，旧版依旧被忽略。

> 注意：样例源的 resumptionToken 会缓存首次请求的标识列表。切换版本后请发起一个
> **新的日期窗口请求**（或重启样例源），让新 datestamp 的记录进入新令牌，这与真实
> OAI-PMH 源“版本变更产生新 datestamp、需新窗口采集”的行为一致。

## 令牌失效重抓的验证方法

样例源提供故障注入端点：

```bash
# 令下一次 resumptionToken 请求返回 badResumptionToken
curl -X POST http://localhost:8080/admin/fail-next
curl http://localhost:8080/admin/state

# 全区间 27 条共 3 页：第 1 页取到后续页令牌失效 -> 丢弃该页 -> 原区间重抓成功
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-27"}'
# 响应中 retries=1；运行历史与服务日志可查
curl -X POST http://localhost:8080/admin/reset   # 清除故障开关
```

若重抓过程中令牌**再次**失效（不可恢复），接口返回 `502`，本批所有书目变更回滚、
截止日保持不变，`harvest_runs` 中记录一条 `failed` 历史——任何半批数据都不会进库。

## 事务与一致性说明

- 采集过程先在内存中收集全部页面，**不向 SQLite 写任何数据**；
- 全部页成功后，在单个 `BEGIN IMMEDIATE` 事务内完成 upsert、撤下、守卫判断、截止日推进、运行历史更新；
- 事务中任何一步失败都会 rollback，库状态回到采集前；
- 进程内互斥锁保证同一时刻只有一个采集任务，并发采集请求返回 `409`；
- 清理在**独立的单事务**内完成 SELECT + UPDATE，失败整体回滚；它不获取采集锁，
  因此与采集的网络翻页阶段可并行，仅在双方都进入 SQLite 写事务时由 `BEGIN IMMEDIATE`
  做短暂串行，互不丢数据。

## 本地开发（无 Docker）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 终端 1：样例 OAI 源
python3 sample-oai/server.py

# 终端 2：API
APP_DB_PATH=./data/harvester.db OAI_ENDPOINT=http://127.0.0.1:8080/oai \
  uvicorn app.main:app --reload

# 测试
pytest
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OAI_ENDPOINT` | `http://oai-source:8080/oai` | OAI-PMH 端点 |
| `APP_DB_PATH` | `/data/harvester.db` | SQLite 数据库路径 |
| `PAGE_SIZE`（样例源） | `10` | 样例源每页记录数 |
| `TOKEN_TTL`（样例源） | `3600` | 样例源 resumptionToken 有效期秒数 |

## 样例源管理端点

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/admin/state` | GET | 查看分页令牌、故障开关与书目版本覆盖 |
| `/admin/fail-next` | POST | 下一次 resumptionToken 请求返回 badResumptionToken |
| `/admin/reset` | POST | 清除故障开关与过期令牌 |
| `/admin/records/<identifier>` | POST | 切换书目版本，JSON 体 `{"version": "..."}` 或查询参数 `?version=...`；`default` 恢复内置 |

