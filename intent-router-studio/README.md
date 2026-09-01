# Intent Router Studio

本地优先的意图路由器训练与可视化平台：导入数据 → 标注 → 训练（BGE-small + SetFit）→ 温度校准 → 风险约束阈值搜索 → 评估 → 模型注册/激活 → Playground 推理。

**安全第一**：`write_action` 路由只输出 `external_write_candidate` 效果上限（"写候选资格"），永不直接执行任何外部写入；写路径必须由下游 `skill_match_and_confirmation` 门（Skill 匹配 + 用户显式确认）把守。

## 解决什么问题

当知识库问答、只读工具和有副作用的 Skill 共用一个 Agent 入口时，单纯依靠关键词或向量相似度容易出现两类错误：把“想了解”误判成“要执行”，以及把模糊表达路由到语义相似但实际无关的能力。本项目把路由做成一个可训练、可评估、可拒识、可回滚的独立安全层。

核心能力：

- **固定五分类路由**：区分知识问答、只读动作、写动作、需要澄清和能力范围外请求；
- **本地小模型训练**：使用 `BAAI/bge-small-zh-v1.5 + SetFit`，数据和模型制品默认只保存在本机；
- **风险约束决策**：联合使用分类置信度、Top-1/Top-2 margin、温度校准和阈值搜索，允许输出 `unclear`/`oos`，不强制选择危险的 Top-1；
- **完整训练工作台**：数据上传、预览、标签映射、标注、切分、训练、指标分析、错误回流、注册、激活和回滚均可视化完成；
- **安全 Query 理解**：本地 Qwen 可做术语归一和上下文独立化，但正式路由始终来自原文；生成服务失败会自动降级到原文；
- **可解释 Playground**：支持单条、批量、A/B 和 Query 理解调试，并按项目在浏览器本地缓存上次输入、选项和结果。

## 五分类 Schema

| 标签 | 含义 | 下游建议 |
|---|---|---|
| `information` | 解释、定义、规则或知识问答 | 回答或知识库检索 |
| `read_only` | 查询外部状态，但不修改外部系统 | 匹配只读 Skill |
| `write_action` | 创建、修改、发送、删除等有副作用操作 | 仅进入 Skill 候选，并强制确认 |
| `unclear` | 信息不足、动作/对象不明确或候选难区分 | 澄清用户意图 |
| `oos` | 超出当前 Agent 能力范围 | 明示能力边界或转交 |

该 Schema 是安全契约而非普通业务标签：模型可以重训、阈值可以调节，但动作效果上限和下游门禁不会由模型分数覆盖。

## 自定义意图标签（两层安全路由）

上述五分类同时是平台的**系统效果类型**固定枚举。项目可通过 `label-schemas` API 用自己的业务意图标签训练路由器：

- 每个业务标签（如 `create_task`、`status_query`）必须映射到唯一的系统效果类型（`create_task → write_action`）；Schema 版本发布后不可变，数据集在导入时绑定当时的 Schema 版本；
- 阈值（含写入专用阈值）、效果上限与下游门禁一律按**服务端解析的系统效果类型**生效，与标签叫什么无关：任何映射到 `write_action` 的业务标签都会走更严的写入阈值、`external_write_candidate` 上限与 `skill_match_and_confirmation` 确认门；
- 推理响应为两层结构：`intent` = `{key, name}`（业务意图，name 来自 Schema 定义），`route`/`effect_type` = 系统效果类型（`route` 为兼容字段，两者恒相等），并携带 `schema_id`/`schema_hash` 溯源；旧五分类模型是恒等映射，行为与历史逐字节一致；
- 训练制品 `label_schema.json`（`intent-schema-v2`）必须完整携带 `label_definitions`；Worker 打包前校验标签与分类头逐位一致、定义不缺不重、效果合法、哈希可复算，加载侧对缺映射/非法映射一律 `MODEL_SCHEMA_MISMATCH` 拒绝服务（fail closed）。

## 快速开始（Docker，推荐）

前置要求：Docker Desktop（本仓库在 Apple Silicon / arm64 与 x86_64 Linux 均可构建）。

```bash
cd intent-router-studio

# 1. 构建镜像（首次约 5-15 分钟：node 构建前端 + 安装 torch/setfit 等）
docker compose build

# 2. 启动 api + worker（共享 ./var 卷：SQLite、制品库、HF 缓存）
docker compose up -d
docker compose ps          # 等 api 变为 healthy

# 3. 端到端冒烟：创建项目 → 导入示例数据 → 切分 → 训练（真实 BGE 下载 + CPU SetFit 微调）→ 注册 → 激活 → 4 条探针推理
python3 scripts/smoke_train.py --base-url http://127.0.0.1:8000/api/v1
```

冒烟脚本会在容器内首次下载 `BAAI/bge-small-zh-v1.5`（约 100MB，缓存到 `./var/hf-cache`），随后用 quick 预设在 400 条示例数据上完成一次真实训练。Docker 默认冻结 BGE 编码器并训练 SetFit 分类头；打开 `fine_tune_embeddings` 后，嵌入配对仍受 `max_embedding_pairs` 限制。

打开 Web 控制台：**http://127.0.0.1:8000**（前端由 FastAPI 直接托管，单origin，无需单独起 Node）。

```bash
docker compose logs -f api worker   # 看日志
docker compose down                 # 停止（./var 数据保留）
```

第一次体验建议按以下顺序操作：创建项目 → 上传 `examples/queries.csv` → 完成导入和切分 → 发起 Quick 训练 → 注册并激活模型 → 在 Playground 验证。完整的前台和后台操作见 [产品使用手册](PRODUCT_USER_MANUAL.md)。

## Query 改写与安全边界

Query 改写用于把口语表达、术语别名和上下文指代整理成更适合下游检索的独立 Query，它不会替代五分类路由器，也不构成执行授权。

| 模式 | 行为 | 推荐用途 |
|---|---|---|
| `off` | 完全旁路改写 | 一键回退 |
| `normalize_only` | 只做确定性术语归一，不调用生成模型 | 低延迟场景 |
| `shadow` | 生成并评估改写，但下游仍使用原文 | 默认观察模式 |
| `safe_apply` | 八项安全检查全部通过后，下游检索可使用改写文本 | 仅在专项评测通过后启用 |

无论使用哪种模式，`final_route` 永远来自原始 Query。`safe_apply` 也只能改变送往下游检索的文本，不能改变动作分类、效果上限或确认要求。当前建议保持 `shadow`；详见 [Query 改写落地方案](QUERY_REWRITE_IMPLEMENTATION_PLAN.md)。

### 外部改写模型（智谱 GLM / OpenAI 兼容 API）

生成模型除内置本地 Qwen 外，可在 Web「改写设置 → 改写模型连接」接入外部 API 并按项目选择：

```bash
# 1. 生成并配置凭据主密钥（只经未入库的 .env 注入，不入 Git/compose）
python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
#    写入 intent-router-studio/.env: REWRITE_CREDENTIAL_MASTER_KEY=...
docker compose up -d   # rewriter 自动读取该密钥与共享数据库

# 2. Web 后台：新建 GLM 连接 → 粘贴 API Key → 保存并测试 → 项目选择该连接
# 3. 评测对比本地 Qwen 与 GLM：
python3 scripts/eval_rewrite.py --project <ID> --provider-connection rpc_xxx
```

- API Key 以 AES-256-GCM 加密落库（AAD 绑定连接与 revision），接口/日志/前端只出 `****末4位`；轮换主密钥用 `python -m app.cli.rotate_rewrite_master_key`（单事务，失败整体回滚）；
- GLM 经官方 `zai-sdk` 调用，端点档位二选一：通用开放平台（按量计费）/ Coding Plan 专用端点（消耗订阅额度；官方条款限编码工具使用，接入属灰色地带、可能被拒，不伪装工具特征）；`thinking` 按模型能力设置——常规模型默认关闭，始终思考模型如 `glm-5.3-flash` 强制开启；JSON mode 开启、`stream=false`；SDK 自带重试关闭，重试与总超时由 Provider 统一管理。自定义 OpenAI 兼容端点只允许 https 公网地址（SSRF 校验：拒绝私网/回环/metadata，请求前二次解析防 DNS rebinding）；
- 失败语义：超时/限流/鉴权失败/非法 JSON 一律回退原文，`/predict` 永不 5xx；熔断按连接隔离，一个坏 Key 不影响本地 Qwen；429 只进短暂限流窗口不计故障；
- 切换模型、更新连接（revision +1）后缓存键自动隔离，旧改写不复用。

## 本地原生运行（Mac 上用 MPS 训练更快）

```bash
# 后端依赖
python3 -m venv .venv && source .venv/bin/activate
pip install -e "backend[dev]"

# 前端依赖 + 构建（或用 dev server）
cd frontend && npm install && npm run build && cd ..

# API（托管 frontend/dist）
cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000 &

# Worker（监督器 + 训练子进程；可将 SIGKILL/exit 137 标记为 WORKER_OOM）
cd backend && python -m app.worker.supervisor &

# 前端开发模式（热更新，代理 /api → 127.0.0.1:8000）
cd frontend && npm run dev   # http://127.0.0.1:5173
```

## 测试

```bash
# 后端单元 + 集成测试（也可在容器内跑：docker compose exec api python -m pytest -q）
cd backend && python -m pytest -q

# 前端单元测试（CSV 公式注入防护等）
cd frontend && npx vitest run

# Playwright E2E（需要本地跑着 api；国内网络建议先
# export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/）
cd frontend && npx playwright install chromium && npm run test:e2e
```

## 质量门禁（修改方案 V2 §5）

一键执行全部 CI 门禁（静态检查、干净容器测试、迁移漂移、依赖审计、前端构建、E2E）：

```bash
docker build --target runtime -t intent-router-studio:latest .
docker build --target test -t intent-router-studio:test .
SKIP_E2E=1 ./scripts/ci.sh          # 无本地栈时跳过 E2E
./scripts/ci.sh                     # 全量（E2E 需 docker compose 栈运行中）
```

- 依赖走锁文件（`backend/requirements.lock` 运行时 / `requirements-dev.lock` 测试），生产镜像不装 `[dev]`；
- 镜像以非 root（uid 10001）运行，`./var` 需 `chown -R 10001:10001`（见 docker-compose.yml 顶部说明）；
- 测试镜像自带 tests/examples/scripts，测试在干净容器内运行，不依赖宿主挂载；
- 依赖漏洞豁免统一登记在 `SECURITY_EXCEPTIONS.md`（带失效日期），pip-audit 只放行清单内 ID；
- 审计制品输出到 `var/ci/`（pip-audit / npm audit JSON、Python 与 npm 依赖清单）。

## 架构

```
┌──────────────┐     /api/v1 (REST + SSE)     ┌──────────────┐
│  React SPA   │ ⇄ FastAPI (api)               │  SQLite WAL  │
│ AntD+ECharts │      ↓ 队列（DB 行锁 claim）    │  ./var/app.db│
└──────────────┘ ┌──────────────┐              └──────┬───────┘
                 │ Worker 进程   │ 训练流水线：          │
                 │ SetFit 训练   │ PREPARING→EMBEDDING→HEAD→CALIBRATING
                 │ 校准+阈值搜索  │ →SEARCHING→EVALUATING→PACKAGING→SUCCEEDED
                 └──────┬───────┘
                        ↓ 原子发布（os.replace）
                 ./var/runs/{id}/   制品：setfit_model/ label_schema.json
                 ./var/models/{id}/     calibration.json thresholds.json
                                        metrics.json per_sample_predictions.parquet
                                        environment.json model_card.md manifest.json
```

关键目录：

| 路径 | 说明 |
|---|---|
| `backend/app/router_core/` | 归一化、策略门（阈值+margin）、温度校准、约束阈值搜索、评估、防泄漏切分、SetFit 训练封装 |
| `backend/app/services/` | 数据集导入/校验/草稿、Run 编排、制品与 manifest、推理运行时（LRU + 预测缓存） |
| `backend/app/worker/` | 独立 Worker：原子认领队列、心跳、取消/中断恢复、7 阶段流水线 |
| `backend/app/api/` | projects / datasets / runs(SSE) / models / inference / system |
| `frontend/src/pages/` | 导入向导、标注台（键盘 1-5）、训练配置、Run 详情（SSE 日志+指标+阈值调节）、模型注册表、Playground（单条/批量/A-B） |
| `examples/queries.csv` | 400 条示例数据（5 类 × 80，含 group_id / risk_slice / 多轮 context / 难负例） |
| `scripts/smoke_train.py` | 端到端冒烟：上传→导入→切分→训练→注册→激活→探针推理→案例沉淀 |

## 核心安全机制

1. **效果上限（effect_ceiling）**：`write_action` → `external_write_candidate`（永不自动执行）；`read_only` → `read_only`；其余 → `none`。每个路由附带 `required_next_gate`。
2. **策略门**：按系统效果类型取阈值（write 更严）+ top1/top2 margin → `accept` / `unclear`（转人工），输出机器可读 `reason_codes`；自定义标签经 Schema 映射到效果类型后适用同一套门。
3. **约束阈值搜索**：在 validation 上网格搜索，硬约束 `false_write_rate ≤ 0.005` 且 `write_precision ≥ 0.95`，目标最大化 `safe_coverage`；无可行解回退保守默认阈值。手动调节同样受约束，违反即 422 拒绝保存。
4. **不可变性**：数据集冻结（FROZEN）后不可原地修改（修改走 draft→新版本）；训练配置/超参白名单校验；制品 manifest 哈希校验（激活与注册时 verify，防篡改）。
5. **激活原子性**：verify → 临时加载冒烟 → 事务内切换 → 运行时热替换；任一步失败不影响旧模型。
6. **隐私与边界**：默认仅监听 127.0.0.1；日志默认不落 query 原文（`LOG_RAW_TEXT=false`）；Playground 案例默认只存哈希；CSV 导出防公式注入；不加载 `trust_remote_code`；不接受用户 pickle/任意代码。

## API 概览

`http://127.0.0.1:8000/api/docs` 有完整 OpenAPI。核心：

```
POST /api/v1/projects                                 创建项目（自动带 5 分类 schema）
GET  /api/v1/projects/{id}/deletion-impact           预览项目级联删除影响范围
DELETE /api/v1/projects/{id}                          删除项目；非空项目用 JSON {"confirm_name":"..."} 确认
POST /api/v1/projects/{id}/uploads                    上传 CSV/JSONL/XLSX/TXT
GET  /api/v1/uploads/{id}/preview                     服务端解码预览 + 列名建议
POST /api/v1/uploads/{id}/import                      列映射/标签映射导入（prelabeled/unlabeled/single_label）
POST /api/v1/datasets/{id}/split                      group-stratified 防泄漏切分
POST /api/v1/projects/{id}/runs                       创建训练（参数白名单校验）
GET  /api/v1/runs/{id}/events                         SSE 实时事件（Last-Event-ID 续传）
GET  /api/v1/runs/{id}/metrics                        raw + policy gate 指标、校准、曲线、切片
POST /api/v1/runs/{id}/thresholds/simulate            阈值实时模拟（validation）
POST /api/v1/runs/{id}/threshold-versions             保存阈值版本（约束校验）
POST /api/v1/runs/{id}/register-model                 注册模型（manifest verify）
POST /api/v1/models/{id}/activate                     原子激活（冒烟 + 事务）
POST /api/v1/inference/predict|batch|compare          推理（request_id 注入；compare 为 A/B）
```

错误结构统一为 `{"error": {"code", "message", "details", "request_id"}}`，成功响应注入 `request_id`。

## 环境变量

见 `.env.example`。容器内默认 `ARTIFACT_ROOT=/data/var`、`DATABASE_URL=sqlite:////data/var/app.db`、`HF_HOME=/data/var/hf-cache`（compose 中 bind mount 到宿主机 `./var`）。

首次公开部署前请注意：

- 默认配置面向单机本地体验，不包含公网身份认证、租户隔离或 TLS；不要直接把 8000 端口暴露到公网；
- `var/`、数据库、上传材料、模型和 Hugging Face 缓存均已加入 `.gitignore`，提交前仍应自行检查工作区；
- 项目默认不记录 Query 原文，只有明确开启 `LOG_RAW_TEXT` 或项目级 `store_raw_text` 后才会保存相关文本；
- 仓库当前未附带开源许可证。公开可见不等于自动授予复制、修改或分发许可。

## 文档导航

- [产品使用手册](PRODUCT_USER_MANUAL.md)：前台体验、训练后台、Query 改写、部署运维与排障；
- [技术设计](../TECHNICAL_DESIGN.md)：系统边界、数据模型、接口和验收设计；
- [Query 改写落地方案](QUERY_REWRITE_IMPLEMENTATION_PLAN.md)：改写协议、安全门、评测和上线策略；
- [外部模型 API 接入方案](QUERY_REWRITE_EXTERNAL_PROVIDER_IMPLEMENTATION_PLAN.md)：GLM/OpenAI-compatible Provider、密钥管理、动态切换、测试与发布计划；
- [验收记录](docs/ACCEPTANCE.md)：设计验收项及核验状态；
- [安全例外](SECURITY_EXCEPTIONS.md)：依赖漏洞例外和到期策略。

## 与设计文档的已记录偏差

1. **数据库迁移**：用 SQLAlchemy `create_all` 代替 Alembic（第一版 schema 稳定，减少依赖）。
2. **前端表单**：AntD Form 代替 React Hook Form + Zod（功能等价，依赖更少）。
3. **TRAINING_HEAD 阶段**：SetFit 一体训练，该阶段表示分类头应用/概率输出，进度区间照常推进。
4. **SetFit 版本兼容**：`SetFitTrainer` 不可用时自动回退到 HF `Trainer`（过采样在输入侧完成），概率列顺序通过 `classes_`/`labels` 探测。
5. **transformers 固定 `<5`**：setfit 1.1.x 仍引用 transformers 5 已删除的 `default_logdir`；模型加载用 `SetFitModel.from_pretrained(本地目录)`。

## 验收清单状态

设计文档 §19 的 18 项验收在 `docs/ACCEPTANCE.md` 中逐项核验记录。
