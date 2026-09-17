# Local Compute Quota Reservation API

本地计算**额度预占（quota hold/reservation）服务**：FastAPI + SQLite，零外部依赖。

- 额度池设置总量，三桶记账：`available + held(有效预占) + used(已用) = total`
- 客户端携带 `Idempotency-Key` 创建带 TTL 的预占
- 并发请求**不会超额**（所有写入经 `BEGIN IMMEDIATE` 串行化）
- 重复请求（幂等键）**冻结并原样重放首次结果**，包括 402/409 等错误
- 预占可按实际用量**结算**（差额返还，只能成功一次，用量不得超过预占）或**释放**（全额返还）
- 过期预占在**任何查询或写入时自动回收**
- 所有余额变化写入**不可变流水**（SQLite 触发器禁止 UPDATE/DELETE）
- 提供余额状态、预占详情、流水查询，并提供内置不变量校验接口
- 数据库落盘 + WAL，容器重启后余额、流水、幂等记录完全保持

## 一条命令启动

```bash
docker compose up -d --build
```

启动后：

| 项目 | 地址 |
| --- | --- |
| API 根 | http://localhost:8000/ |
| 健康检查 | http://localhost:8000/health |
| Swagger UI（在线调试） | http://localhost:8000/docs |
| OpenAPI Schema | http://localhost:8000/openapi.json |

容器启动流程（`entrypoint.sh`）会先执行 `python -m app.init_db`：建表、初始化默认额度池、运行不变量校验，然后再启动 Uvicorn。SQLite 数据保存在命名卷 `quota-data`（挂载到容器内 `/data`），`docker compose down` 不会丢数据；如需彻底清除：`docker compose down -v`。

> 端口固定为 **8000**，可在 `compose.yaml` 的 `ports` 中修改映射（如 `"9000:8000"`）。

## 环境变量配置

修改 `compose.yaml` 的默认值，或通过项目根目录的 `.env` 文件（复制 `.env.example`，docker compose 会自动读取做变量插值）/ shell 环境变量覆盖。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QUOTA_DB_PATH` | `/data/quota.db` | SQLite 数据库文件路径（容器内） |
| `QUOTA_POOL_TOTAL` | `1000000` | 启动时自动创建的 `default` 池总量（整数单位） |
| `QUOTA_INIT_DEFAULT_POOL` | `true` | 是否在启动时自动创建默认池（已存在则跳过） |
| `QUOTA_DEFAULT_TTL_SECONDS` | `300` | 未指定 TTL 时预占的默认存活秒数 |
| `QUOTA_MAX_TTL_SECONDS` | `86400` | 允许客户端请求的最大 TTL（秒） |
| `QUOTA_BUSY_TIMEOUT_MS` | `5000` | 写事务争抢 SQLite 锁时的最长等待（毫秒） |
| `PORT` | `8000` | Uvicorn 监听端口 |
| `UVICORN_WORKERS` | `1` | Uvicorn worker 数；SQLite 单写模型建议保持 1（多 worker 也安全，写操作仍由锁串行化） |

配置示例（自定义总量与 TTL）：

```bash
QUOTA_POOL_TOTAL=500000 QUOTA_DEFAULT_TTL_SECONDS=600 docker compose up -d --build
```

> 注意：`QUOTA_POOL_TOTAL` 仅在池**首次创建**时生效；池已存在后重启不会改变其总量。需要新池见下文 `PUT /pools/{pool_id}`。

## API 速览

### 1. 创建预占（必须带 Idempotency-Key）

```bash
curl -s -X POST http://localhost:8000/pools/default/reservations \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-123' \
  -d '{"amount": 1000, "ttl_seconds": 300}'
```

- 同一 `Idempotency-Key` + 同一请求体的重试，返回**同一个预占**（201 与首次响应体完全一致）
- 同键但不同请求体 → `409 idempotency_key_conflict`
- 余额不足 → `402 quota_exhausted`（该结果同样会被冻结并重放）
- 省略 `ttl_seconds` 时使用 `QUOTA_DEFAULT_TTL_SECONDS`

### 2. 按实际用量结算（差额返还，仅可成功一次）

```bash
curl -s -X POST http://localhost:8000/pools/default/reservations/<RESV_ID>/settle \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: order-123-settle' \
  -d '{"used_amount": 700}'
```

- `700` 计入 `used`，剩余 `300` 返还 `available`
- `used_amount > amount` → `422 usage_exceeds_hold`（不冻结，允许同键修正后重试）
- 对已结算/已释放/已过期的预占再操作 → `409`

### 3. 释放预占（全额返还）

```bash
curl -s -X POST http://localhost:8000/pools/default/reservations/<RESV_ID>/release \
  -H 'Idempotency-Key: order-123-release'
```

### 4. 查询

```bash
# 池状态（GET 也会触发过期回收）
curl -s http://localhost:8000/pools/default/status

# 预占详情
curl -s http://localhost:8000/pools/default/reservations/<RESV_ID>

# 预占列表（可加 ?status=held|settled|released|expired&limit=100）
curl -s http://localhost:8000/pools/default/reservations

# 不可变流水（游标分页 ?after_seq=<seq>&limit=50）
curl -s 'http://localhost:8000/pools/default/ledger?limit=50'

# 不变量校验：available + held + used == total，并与流水累加结果对账
curl -s http://localhost:8000/pools/default/verify
```

### 5. 额外池

```bash
curl -s -X PUT http://localhost:8000/pools/gpu-team-a \
  -H 'Content-Type: application/json' -d '{"total": 500000}'
# 该接口也支持 Idempotency-Key
```

## 记账模型

每个池维护 `total / available / held / used` 四个整数，表级 `CHECK` 约束强制
`available + held + used = total`。每次状态变化追加一条流水，三个桶的 delta 之和恒为 0：

| 事件 | delta_available | delta_held | delta_used |
| --- | ---: | ---: | ---: |
| reserve（预占 a） | -a | +a | 0 |
| settle（预占 a，实际用 u） | +(a-u) | -a | +u |
| release（释放 a） | +a | -a | 0 |
| expire（过期回收 a） | +a | -a | 0 |

流水表 `ledger_entries` 上有 `BEFORE UPDATE/DELETE` 触发器，任何篡改尝试都会被数据库拒绝。`GET /pools/{id}/verify` 会同时校验：桶之和、桶值与流水累加一致、held 与状态为 held 的预占金额之和一致。

## 不使用 Docker 时的本地运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m app.init_db                      # 初始化（幂等）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest -q
```

测试覆盖：完整生命周期、结算幂等与超额拒绝、TTL 在读/写路径上的自动回收、
40 线程并发不超额、并发同幂等键只创建一次、结算与释放并发只有一个生效、
流水不可变触发器、以及模拟容器重启后的状态与幂等一致性。

## 项目结构

```
app/
  main.py         # FastAPI 路由
  services.py     # 记账/预占/结算/回收/幂等/对账核心逻辑
  db.py           # SQLite 连接、schema（含 CHECK 与不可变触发器）、事务
  schemas.py      # Pydantic 请求模型
  config.py       # 环境变量配置
  errors.py       # 统一错误结构
  timeutil.py     # 微秒级 UTC 时间戳
  init_db.py      # 容器启动初始化入口
entrypoint.sh     # 先初始化再启动 Uvicorn
Dockerfile
compose.yaml
tests/            # pytest 测试套件
```
