# 过敏原溯源服务（共线生产食品批次）

基于 **Python + FastAPI + SQLite** 的过敏原溯源服务，面向共线（共享设备）生产场景：

- 登记**原料批次**的过敏原集合或**待确认**状态（声明可更正，保留版本历史）；
- 登记**加工批次**：设备、UTC 起止时间、投入批次、产出批次；
- 产出继承投入的过敏原/待确认状态，并继承**设备上一次加工的残留**；
  只有两次加工之间**已验证且未撤销**的清洗才清除残留；
- 查询任一批次，返回**每项过敏原与待确认状态的来源路径**及当前有效依据；
- 原料声明更正、清洗撤销后，**未签发结论按当前有效记录实时重算**；
- **签发标签**冻结签发时刻的结论与依据，后续变更不改旧标签，
  可签发关联旧版（`supersedes_id`）的新标签；
- 无效时序、来源环、设备时间重叠、批次来源冲突一律**拒绝并保持原状态**
  （校验与写入在同一事务内，失败即回滚）。

## 快速启动

### Docker Compose（推荐，一键启动）

```bash
docker compose up --build
```

依赖安装与数据库初始化（建表）均在容器启动时自动完成，无需额外步骤。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

## 地址与端口

| 项目 | 地址 |
|---|---|
| 服务地址 | http://localhost:8000 |
| 交互式 API 文档 (Swagger) | http://localhost:8000/docs |
| OpenAPI 规格 | http://localhost:8000/openapi.json |
| 健康检查 | http://localhost:8000/health |

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `APP_PORT` | `8000` | 服务监听端口（Compose 下同时作为宿主机映射端口） |
| `DB_PATH` | 容器内 `/data/allergen.db`，本地 `app.db` | SQLite 数据库文件路径 |

修改宿主机端口：`APP_PORT=9000 docker compose up --build`，然后访问 http://localhost:9000 。

## 数据与规则模型

- **原料批次**：当前有效声明 = 最新版本声明（过敏原集合 + 是否待确认）。
- **批次可用时间**：原料 = 首次登记时间；加工产出 = 产出加工的结束时间。
  投入批次的可用时间必须 ≤ 加工开始时间。
- **设备残留**：加工开始时，若同一设备存在上一次加工（`end_utc ≤ 本次 start_utc`），
  且两次加工之间没有 `verified=true 且未撤销` 的清洗
  （`上次 end_utc ≤ cleaned_at ≤ 本次 start_utc`），
  则上一次加工产出的全部过敏原/待确认状态作为残留继承。
- **标签**：签发时将结论（每项过敏原/待确认的来源路径）与依据
  （命中的原料声明版本、赖以清除残留的清洗记录）整体冻结为快照，永不可变。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/materials` | 登记原料批次（过敏原集合或待确认状态） |
| GET | `/materials` / `/materials/{batch_id}` | 查询原料批次及当前有效声明 |
| POST | `/materials/{batch_id}/declarations` | 更正声明（追加新版本） |
| POST | `/processings` | 登记加工批次（含全部校验） |
| GET | `/processings` / `/processings/{id}` | 查询加工批次 |
| POST | `/cleanings` | 登记设备清洗（`verified` 标记是否已验证） |
| GET | `/cleanings` / `/cleanings/{id}` | 查询清洗记录（可按 `?equipment_id=` 过滤） |
| POST | `/cleanings/{id}/revoke` | 撤销清洗验证 |
| GET | `/batches/{batch_id}/trace` | 实时溯源：每项过敏原/待确认的来源路径 + 依据 |
| POST | `/batches/{batch_id}/labels` | 签发标签（可带 `supersedes_id` 关联旧版） |
| GET | `/batches/{batch_id}/labels` / `/labels/{id}` | 查询标签 |

错误约定：`400` 无效时序/参数（如投入批次在加工开始前不可用）；
`404` 资源不存在；`409` 冲突（设备时间重叠、来源环、批次号占用、重复撤销）；
`422` 请求格式错误（如时间缺少时区）。所有时间必须为带时区的 ISO8601（建议 UTC `Z`）。

## 调用示例（curl）

完整演示脚本（覆盖下述全部流程）：`./examples/demo.sh`

```bash
BASE=http://localhost:8000

# 1. 登记原料批次
curl -s $BASE/materials -X POST -H 'Content-Type: application/json' -d '{
  "batch_id": "RM-MILK", "allergens": ["milk"],
  "declared_at": "2026-01-02T06:00:00Z"}'
curl -s $BASE/materials -X POST -H 'Content-Type: application/json' -d '{
  "batch_id": "RM-MYST", "pending": true,
  "declared_at": "2026-01-02T06:00:00Z"}'   # 待确认状态

# 2. 登记加工批次（同一设备 EQ-1 先后两批）
curl -s $BASE/processings -X POST -H 'Content-Type: application/json' -d '{
  "processing_id": "P1", "equipment_id": "EQ-1",
  "start_utc": "2026-01-02T08:00:00Z", "end_utc": "2026-01-02T09:00:00Z",
  "inputs": ["RM-MILK"], "outputs": ["OUT-MILK-1"]}'
curl -s $BASE/processings -X POST -H 'Content-Type: application/json' -d '{
  "processing_id": "P2", "equipment_id": "EQ-1",
  "start_utc": "2026-01-02T10:00:00Z", "end_utc": "2026-01-02T11:00:00Z",
  "inputs": ["RM-OAT"], "outputs": ["OUT-OAT-1"]}'

# 3. 溯源：milk 经设备残留进入 OUT-OAT-1，路径逐步可见
curl -s $BASE/batches/OUT-OAT-1/trace
# -> allergen_paths.milk: RM-MILK --(P1, input)--> OUT-MILK-1
#    --(P2, equipment_residue, EQ-1)--> OUT-OAT-1

# 4. 两次加工之间的已验证清洗 -> 残留清除
curl -s $BASE/cleanings -X POST -H 'Content-Type: application/json' -d '{
  "equipment_id": "EQ-1", "cleaned_at": "2026-01-02T09:30:00Z", "verified": true}'

# 5. 撤销清洗 -> 未签发结论实时重算，milk 残留恢复
curl -s $BASE/cleanings/1/revoke -X POST

# 6. 原料声明更正（追加版本，实时结论随之更新）
curl -s $BASE/materials/RM-OAT/declarations -X POST \
  -H 'Content-Type: application/json' \
  -d '{"allergens": ["gluten", "sesame"]}'

# 7. 签发标签（冻结当前结论）；变更后签发关联旧版的新标签
curl -s $BASE/batches/OUT-OAT-1/labels -X POST \
  -H 'Content-Type: application/json' -d '{}'
curl -s $BASE/batches/OUT-OAT-1/labels -X POST \
  -H 'Content-Type: application/json' -d '{"supersedes_id": 1}'
curl -s $BASE/labels/1   # 旧标签保持签发时的结论不变
```

## 项目结构

```
app/
  main.py      # FastAPI 路由与错误映射
  service.py   # 业务规则：校验、溯源计算、标签签发
  db.py        # SQLite 连接、建表、写事务
  schemas.py   # 请求模型（强制时区感知时间）
tests/test_app.py   # 19 个端到端测试
examples/demo.sh    # 全流程 curl 演示
Dockerfile / docker-compose.yml / requirements*.txt
```
