# 验收核验报告（设计文档 §19）

逐项核验日期：2026-08-24。环境：macOS（Apple Silicon）+ Docker Desktop，`docker compose up -d`
拉起 `irs-api`（127.0.0.1:8000，healthcheck healthy）与 `irs-worker` 两个容器。

| # | 验收项 | 结果 | 证据 |
|---|--------|------|------|
| 1 | Web 可导入 CSV、JSONL、XLSX 和 TXT | ✅ | 后端 `ALLOWED_EXTENSIONS = {"csv","jsonl","xlsx","txt"}`（dataset_service.py:25）；上传向导 accept 列表与后端一致；CSV 全链路经 smoke 脚本与 E2E 双重验证 |
| 2 | 支持列映射、标签映射、未标注数据人工标注 | ✅ | 上传向导步骤 2/3 做列映射与标签映射（`/uploads/{id}/import` 带 `columns` + `label_mapping`）；未标注模式导入为 DRAFT，标注工作台 `/datasets/:id/label` 逐条打标（`test_import_label_mapping_and_unknown_label`、`test_split_and_unlabeled_guard`） |
| 3 | 数据冲突、重复、类别不平衡和 split 泄漏可见 | ✅ | 导入即生成质量报告（DATASET_LABEL_CONFLICT / DUPLICATE_TEXT / CLASS_IMBALANCE / GROUP_LEAK），前端数据集详情展示；`test_label_conflict_blocks` 验证冲突会拦截建 Run，`test_split_and_unlabeled_guard` 验证 group 泄漏控制 |
| 4 | Dataset Version 不可原地修改 | ✅ | FROZEN 版本 PATCH 样本返回 409 DATASET_IMMUTABLE；修改走 草稿→提交 生成新版本（`test_frozen_immutable_and_draft_flow`、API 集成测试） |
| 5 | 能配置并运行 BGE-small + SetFit 训练 | ✅ | `POST /projects/{id}/runs`（参数白名单校验 + PRESETS）→ Worker 领取训练 BAAI/bge-small-zh-v1.5 + SetFit；smoke 全链路 SUCCEEDED（见下） |
| 6 | API 与 Worker 独立，训练不阻塞 Web | ✅ | compose 两个进程；训练期间 API 持续可响应（smoke 训练等待循环中轮询 `/runs/{id}` 均即时返回）；SQLite WAL + 原子条件更新领取任务（queue.py） |
| 7 | 训练状态、日志和指标实时可见 | ✅ | SSE `/runs/{id}/events` 命名事件（log/progress/metric/terminal）+ Last-Event-ID 续传；实测对流捕获到 `id: 1..6` 的 progress 事件流；前端 RunDetail 用 LogStream 实时渲染 |
| 8 | 完成温度校准和风险约束阈值搜索 | ✅ | Worker 管线 CALIBRATING（温度缩放）+ SEARCHING_THRESHOLDS（max_false_write_rate≤0.005、min_write_precision≥0.95 下最大化 safe_coverage 的向量化网格搜索）；smoke 输出阈值与指标；`test_threshold_search.py` 覆盖不可行回退 |
| 9 | 同时展示 raw model 和 policy gate 指标 | ✅ | `/runs/{id}/metrics` 返回 `validation/test` 的 `classification`（raw）与 `routing`（policy gate：safe_coverage、false_write_rate 等）；前端 RunMetrics 分区展示 |
| 10 | 可查看混淆矩阵、风险切片和错误样本 | ✅ | metrics 含 `confusion_matrix` 与 `risk_slices`；`/runs/{id}/errors` 分页错误样本，可一键回流草稿（`/runs/{id}/errors/draft`） |
| 11 | Playground 展示 Top-K、confidence、margin 和 reason codes | ✅ | `/inference/predict` 返回 top_k（含每名次 label/prob）、confidence、margin、reason_codes；smoke 用 4 条探针 query 逐一验证期望路由 |
| 12 | 支持阈值临时模拟、版本保存和 A/B 对比 | ✅ | `POST /runs/{id}/thresholds/simulate`（即时指标+violations）、`POST/GET /runs/{id}/threshold-versions`（版本化保存）；前端 ThresholdSimulator 拖动防抖模拟，Playground 支持 A/B 双模型对比 |
| 13 | 模型制品、数据、配置、依赖和 hash 可追溯 | ✅ | 制品目录含 setfit_model/、calibration.json、thresholds.json、environment.json（依赖指纹）、model_card.md、manifest.json（SHA-256 逐文件 hash），发布前 `verify_manifest` 校验；发布 = 原子重命名 |
| 14 | 模型激活失败不会影响旧模型 | ✅ | 激活 = 校验新制品后原子更新 project.active_model_id，失败抛错不落库；API 启动时加载失败仅告警继续用旧模型（main.py `_startup_load_active_models`） |
| 15 | `write_action` 只输出写候选资格，不产生执行授权 | ✅ | policy 输出 `authority_ceiling = "external_write_candidate"`（taxonomy.py:54），reason codes 明示需二次确认；无任何执行类动作出口；`test_policy.py` 断言 ceiling 不随置信度升级 |
| 16 | 默认仅监听本机，默认不记录 Query 原文 | ✅ | compose 端口绑定 `127.0.0.1:8000:8000`；训练/推理日志只含 run_id、指标、阶段信息，无 query 原文（Playground 反馈样本仅在用户显式提交时落库并标记来源） |
| 17 | 单元、集成和 Playwright E2E 通过 | ✅ | 后端 `pytest -q`：53 passed（Docker 容器内实跑）；前端 `vitest run` 通过 + `tsc -b` 无错；Playwright `npm run test:e2e`（chromium）通过：项目创建、上传向导进入列映射、六个主页面导航 + API 健康 |
| 18 | README 能指导全新环境完成 smoke train | ✅ | README.md：Docker 快速开始（clone → compose up → 打开 127.0.0.1:8000 / 跑 smoke_train.py），含国内镜像加速参数与常见问题 |

## 冒烟实测记录（2026-08-24，Docker 容器内实跑，`scripts/smoke_train.py` 全绿 exit=0）

```
✅ API 健康（GET /api/v1/health → {"status":"ok"}）
✅ 项目 smoke-train-project
✅ 上传 queries.csv（400 行，5 类各 80）→ 预览（列自动识别）
✅ 导入 FROZEN 数据集版本 + 质量报告
✅ 切分 train=280 / validation=60 / test=60（group_id 不泄漏）
✅ 训练 SUCCEEDED，progress=100（CPU，quick 预设；embedding 560 步 + head +
   温度校准（T=0.618）+ 风险约束阈值搜索 + 评测 + 制品原子发布，92MB 制品含逐文件 SHA-256 manifest）
✅ 测试指标：macro_f1=0.849，safe_coverage=0.833，false_write_rate=0.021（test 集留出报告；
   阈值约束在 validation 集上满足 false_write_rate=0.0、write_precision=1.0）
✅ 注册模型 → 激活（原子切换，注册表 ACTIVE）
✅ 探针矩阵 4/4 路由正确（含延迟）：
   [Libra 怎么创建实验？]        information  conf=0.988  29.4ms
   [查一下实验 123 的状态]       read_only    conf=0.994  24.6ms
   [帮我撤回 Review 123]         write_action conf=0.982   9.8ms
   [帮我预订会议室]              oos          conf=0.990  12.4ms
✅ write_action 实测输出 effect_ceiling=external_write_candidate、
   required_next_gate=skill_match_and_confirmation（永不产生执行授权）
✅ 批量推理 /inference/batch 两条并发正确
✅ 阈值模拟（POST /runs/{id}/thresholds/simulate）：新阈值下 validation 指标即时返回，violations=[]；
   保存为 threshold version（source=manual）
✅ Playground 反馈回流保存（text_hash + 显式勾选原文）
✅ SSE 事件流实测：命名事件 log/progress/terminal，id 递增，Last-Event-ID 可续传
✅ 激活失败隔离实测：激活不存在的模型返回 404 错误结构，原 ACTIVE 模型继续正常服务
✅ Web 控制台 http://127.0.0.1:8000 ：FastAPI 托管 SPA（index + hash 资源 200）
```

## 已记录的偏差（详见 README「与设计文档的偏差」）

- ORM 建表用 `create_all` 而非 Alembic 迁移（本地单机 SQLite，无演进需求）。
- 前端表单用 AntD Form（校验规则内置）而非 RHF + Zod。
- `transformers` 固定 `<5`：setfit 1.1.x 依赖 `default_logdir`（transformers 5 已删除）；
  模型加载用 `SetFitModel.from_pretrained(本地目录)`。
- 部署默认 CPU 推理；代码保留 cuda/mps 自动探测。

## 过程中发现并修复的缺陷（回归记录）

1. request_id 中间件消费流式 body 后未重建响应 → 空响应体（已按分支重建）。
2. `recover_stale_runs` / `transition_status` 原生 SQL 绑定 dict → ProgrammingError（error 字段改 JSON 序列化）。
3. 制品原子发布（.tmp 重命名）后仍向旧路径写日志 → FileNotFoundError（RunLogger 增加 relocate + 写入容错）。
4. `progress()` 只推进 stage 不推进 status → 终态条件更新 from 集永不命中，Run 卡在 PACKAGING（status 随 stage 同步推进；终态/取消 from 集放宽为全部运行态）。
5. 运行中取消的 CANCELLING 转换 from 集错误（同上修复）。
6. 上传向导 accept 含后端不支持的 .tsv/.json（已对齐 csv/jsonl/xlsx/txt）。
