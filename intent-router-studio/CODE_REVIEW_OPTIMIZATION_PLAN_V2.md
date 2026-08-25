# Intent Router Studio 全量代码优化方案（V2）

## 1. 优化目标

在不改变现有五分类定义和产品主流程的前提下，解决本次全量代码审查发现的安全、数据一致性、训练可复现性和工程质量问题，使系统达到以下状态：

- 不存在可由普通 CLI 参数触发的目录越界、任意递归删除风险；
- Query 改写不得凭空补充写操作对象，超时请求不得继续无限占用计算资源；
- 数据集、切分、训练 Run、模型状态和运行时模型保持一致且可追溯；
- 项目配置与实际运行行为一致，不再出现“配置保存成功但不生效”；
- 后端、前端、迁移、依赖和静态检查形成可执行的发布门禁。

本方案分三阶段实施：

1. **阶段一：安全止损与关键一致性修复**，完成前禁止发布；
2. **阶段二：正确性与产品体验修复**，完成后进入回归测试；
3. **阶段三：工程治理与发布门禁**，降低后续回归概率。

## 2. 问题与实施优先级

| 阶段 | 优先级 | 优化项 | 发布要求 |
|---|---|---|---|
| 一 | P0 | 模型导出路径约束与安全覆盖 | 阻断发布 |
| 一 | P1 | 改写对象必须可追溯 | 阻断发布 |
| 一 | P1 | 改写超时任务可终止、队列有界 | 阻断发布 |
| 一 | P1 | 五分类 Schema 全链路强校验 | 阻断发布 |
| 一 | P1 | ACTIVE 模型、数据库指针和运行时缓存一致 | 阻断发布 |
| 一 | P1 | Run 创建时固定数据切分 | 阻断发布 |
| 二 | P2 | 迁移锁失败关闭 | 发布前修复 |
| 二 | P2 | 阈值搜索完整且确定性地处理并列候选 | 发布前修复 |
| 二 | P2 | 项目改写配置与 Rewriter 实际配置对齐 | 发布前修复 |
| 二 | P2 | 上传文件流式限流 | 发布前修复 |
| 二 | P2 | 模型重新激活、回滚及项目进入体验修复 | 发布前修复 |
| 三 | P2 | 依赖、镜像、测试数据和静态检查治理 | 持续门禁 |

---

## 3. 阶段一：安全止损与关键一致性修复

### 3.1 安全重构模型导出命令

涉及文件：

- `scripts/export_model.py`
- `backend/app/services/artifact_service.py`
- 新增 `backend/tests/test_export_model.py`

#### 修改设计

1. `model_id` 必须满足服务端模型 ID 格式，并通过数据库或模型制品索引解析，禁止直接与目录拼接。
2. 使用 `Path.resolve()` 和 `Path.is_relative_to()` 校验源目录确实位于 `models_dir` 下，不再使用字符串前缀判断。
3. 默认要求输出路径不存在；存在时返回非零退出码，不做删除。
4. 如确需覆盖，增加显式 `--force`，但仍拒绝以下目标：
   - `/`、用户目录、仓库根目录、`ARTIFACT_ROOT`；
   - 当前工作目录及其父目录；
   - 源模型目录及其父目录；
   - 符号链接或越界路径。
5. 先复制到同级临时目录，校验哈希后再原子重命名到目标目录。
6. tar 导出使用 Python `tarfile`，避免依赖外部命令；输出文件同样禁止静默覆盖。

#### 验收标准

- `--model-id ../../x` 被拒绝；
- `--out .`、仓库根目录、用户目录、已有目录均不会被删除；
- 导出中断不会留下一个看似完整的目标目录；
- 导出后文件哈希与 Manifest 一致。

### 3.2 强化 Query 改写安全门

涉及文件：

- `backend/app/query_rewrite/safety.py`
- `backend/app/query_rewrite/prompt.py`
- `backend/app/services/rewrite_service.py`
- `backend/tests/test_rewrite_safety.py`

#### 修改设计

对 `write_action` 增加独立的“动作对象溯源”规则：

1. 从原 Query、Context、术语表中抽取允许使用的对象跨度；
2. 如果原文包含代词或省略对象，如“它”“这个”“删掉吧”，只有 Context 明确包含候选对象时才允许补全；
3. 改写新增的普通中文名词不能再因为不属于数字/英文 ID 而绕过检查；
4. 无法可靠做通用中文实体识别时采取失败关闭策略：
   - 写意图补充了原文不存在的对象，默认 `OBJECT_INVENTED`；
   - 保留原文作为 `downstream_query`；
   - 路由仍由原文模型决定，并进入参数澄清/确认门；
5. 否定词、疑问语气和对象检查都基于 `original + context`，不能只比较原 Query。

建议在 Provider 输出中增加结构化字段：

```json
{
  "standalone_query": "删除实验 123",
  "resolved_references": [
    {"surface": "它", "resolved_to": "实验 123", "evidence": "context"}
  ]
}
```

该字段只能作为证据候选，最终仍由安全门验证，不能信任模型自报。

#### 必测用例

- `删除它` + 无 Context → 禁止补充任何对象；
- `删除它` + Context 明确出现“实验 123” → 可补全“实验 123”；
- `删除它` → `删除飞书实验` → 必须返回 `OBJECT_INVENTED`；
- `不要删除它` + Context → 否定语义必须保留；
- information/read_only 被改写成 write_action → 永远禁止应用改写。

### 3.3 将 Rewriter 从“线程超时”改为“可终止执行”

涉及文件：

- `backend/app/query_rewrite/qwen_provider.py`
- `backend/app/rewriter/main.py`
- `backend/app/query_rewrite/client.py`
- `docker-compose.yml`

#### 修改设计

推荐采用单模型 Worker 进程加有界队列：

1. Rewriter 服务只允许一个生成任务占用模型；待处理队列长度默认 1～2；
2. 队列已满立即返回 `429 REWRITER_BUSY`，主 API 回退原文，不继续排队；
3. 超时时终止承载该次生成的子进程，或重启模型 Worker，确保计算真正停止；
4. 线程模式只能作为开发环境方案，不允许把 `daemon thread + join(timeout)` 用于生产超时；
5. 增加指标：
   - `active_generation`、`queue_depth`；
   - `generation_timeout_total`、`busy_reject_total`；
   - 实际生成耗时和客户端等待耗时；
6. CPU 模式下默认使用 `normalize_only` 或 `shadow`，只有经过延迟验收后才能启用 `safe_apply`。

#### 验收标准

- 一次超时后，CPU 使用率能在限定时间内下降；
- 连续 20 个请求不会创建 20 个后台生成线程；
- 队列满时主路由仍正常返回原文结果；
- Rewriter 不可用不会让 `/predict` 返回 5xx。

### 3.4 固定五分类 Schema 的全链路约束

涉及文件：

- `backend/app/schemas/__init__.py`
- `backend/app/services/dataset_service.py`
- `backend/app/router_core/taxonomy.py`

#### 修改设计

1. 定义唯一的 `IntentLabel` 类型，API Schema、导入、草稿、单样本更新、冻结、切分和训练全部复用；
2. `DraftChange.label` 和 `SamplePatch.label` 使用 `Literal` 或自定义校验器；
3. `create_draft()` 的 add/update 使用同一验证函数；
4. `validate_dataset()` 必须扫描并阻断：
   - 非法标签；
   - 空标签；
   - 固定五类缺失；
   - 同一规范化文本标签冲突；
5. Worker 在加载模型前再次断言训练标签集合严格等于五分类集合；
6. 前端标签控件只能从后端 Schema 生成，不允许自由输入。

#### 验收标准

- 任何 API 路径都无法保存第六类标签；
- 历史非法数据在冻结或训练前被阻断，并指出样本 ID；
- 五类缺少任意一类时不能开始训练。

### 3.5 统一模型生命周期和运行时状态

涉及文件：

- `backend/app/services/inference_service.py`
- `backend/app/services/run_service.py`
- `backend/app/api/models.py`

#### 修改设计

1. 禁止直接归档当前 ACTIVE 模型；如果产品需要“停用模型”，新增独立 API，并在同一临界区内完成：
   - 清空项目指针；
   - 更新模型状态；
   - 驱逐运行时缓存；
2. 运行时缓存项保存 `model_id`；每次读取时与 `project.active_model_id` 对比，不一致立即重新加载或报错；
3. 激活、回滚、停用按 `project_id` 加互斥锁，避免并发激活导致数据库和内存指向不同模型；
4. 回滚不得提前提交 `ARCHIVED → CANDIDATE`：先完成 Manifest 校验、加载和冒烟，再在单事务中切换；失败保持原状态；
5. 状态切换成功后增加结构化审计事件，记录旧模型、新模型、操作者和时间。

#### 验收标准

- ACTIVE 模型不能通过普通 archive API 被归档；
- 数据库无 ACTIVE 模型时，缓存中即使残留模型也不能继续预测；
- 回滚加载失败时，旧 ACTIVE 模型和目标 ARCHIVED 状态均不变化；
- 两个并发激活请求结束后，数据库指针、模型状态和运行时模型完全一致。

### 3.6 Run 创建时固定 Split

涉及文件：

- `backend/app/services/run_service.py`
- `backend/app/worker/run_executor.py`
- `backend/app/models/tables.py`

#### 修改设计

1. 创建 Run 时选择或创建 Split，并立即写入 `run.split_id`；
2. Worker 只允许按 `run.split_id` 加载，不得重新查询“最新 Split”；
3. Retry 默认继承父 Run 的 `dataset_id + split_id + config`；如需重新切分，必须创建新的 Run 并明确展示差异；
4. Run Manifest 记录：Dataset ID、Split ID、Split 文件哈希、seed、算法版本和各集合统计；
5. Worker 校验 Split 属于 Run 的 Dataset，且 Split 文件哈希未变化。

#### 验收标准

- Run 排队后再创建新 Split，不影响该 Run；
- Retry 使用与父 Run 完全相同的样本划分；
- 任意成功 Run 都能从 Manifest 精确恢复训练输入。

---

## 4. 阶段二：正确性与产品体验修复

### 4.1 迁移锁失败关闭

- 锁文件写入 PID、hostname、启动时间；
- 只有进程已不存在且锁超过阈值时才能清理陈旧锁；
- 60 秒仍无法获得有效锁时，服务启动失败，不再无锁迁移；
- 迁移失败自动保留备份，并输出恢复命令；
- 增加双进程并发迁移测试和陈旧锁恢复测试。

### 4.2 修复阈值搜索的候选截断

不再使用 `[:200]`、`[:500]` 影响最优解选择：

1. 在每个向量化批次中直接维护排序键：
   `safe_coverage → macro_f1 → conservatism → 稳定字典序`；
2. Pareto 可视化允许采样，但最优解选择不得采样；
3. 相同输入和配置必须始终输出相同阈值；
4. 增加大规模并列候选测试，期望选择满足约束的最保守组合；
5. 报告 `n_feasible`、`n_tied` 和最终选择原因。

### 4.3 对齐项目改写配置和运行时配置

二选一实施，推荐方案 A：

#### 方案 A：部署级模型配置，项目级策略配置

- Rewriter 的 `provider/model_id/device/max_new_tokens` 只由部署环境管理；
- 项目配置只保留 `mode/timeout/min_confidence/route_consistency/fallback`；
- 前端将模型信息展示为只读，并标注“部署配置”；
- 删除保存后不会生效的字段。

#### 方案 B：支持多模型动态配置

- Rewriter 建立允许列表和 Provider Registry；
- 请求传递 `model_id/max_context_chars/max_new_tokens`；
- 按模型做缓存和资源预算；
- 禁止用户传入任意 Hugging Face 路径或远程代码模型。

本地单机场景优先选择方案 A，复杂度和资源风险更低。

### 4.4 上传链路流式限流

- 使用固定大小分块读取并实时累计字节数；
- 写入随机临时文件，同时增量计算 SHA-256；
- 超过限制立即关闭并删除临时文件；
- 完成后原子移动到上传目录；
- XLSX 增加压缩包展开大小、sheet 数量、行列数限制，防止压缩炸弹；
- 反向代理和应用层同时设置请求体上限。

### 4.5 修复模型和项目操作体验

1. ARCHIVED 模型“重新激活”调用 `/rollback`，普通候选模型才调用 `/activate`；
2. 弹窗根据目标状态展示“激活”或“回滚”；
3. 项目列表“进入”必须先 `setProjectId(p.id)` 再导航；
4. 所有 mutation 增加错误提示和成功后的相关 Query 失效；
5. 增加 E2E：切换项目、激活候选、回滚归档、失败后状态不变。

---

## 5. 阶段三：工程治理与发布门禁

### 5.1 依赖与镜像治理

1. Python 使用锁文件固定完整依赖树，生产镜像不安装 `[dev]`；
2. 分开 runtime、test、dev 依赖；
3. 升级或替换审计发现的 `torch`、`transformers`、ECharts、React Router 风险版本；
4. 若上游暂时无兼容修复，建立带失效日期的漏洞豁免，而不是永久忽略；
5. Docker 使用非 root 用户，制品目录只授予必要写权限；
6. 增加镜像 SBOM 和依赖审计产物。

### 5.2 测试与构建一致性

- Dockerfile 将 `examples` 复制到测试期约定路径，或让测试通过统一环境变量读取 `/data/examples`；
- 生产镜像不携带测试代码；单独建立 test stage；
- 后端测试、前端单测、E2E、迁移测试都必须在干净容器中运行；
- 为本方案每个 P0/P1 问题增加至少一个回归测试；
- 当前 2.4MB 前端包按页面拆包，ECharts 仅在图表页异步加载。

### 5.3 静态质量门禁

1. 清理现有 Ruff 结果，先修复真实缺陷和无效 `noqa`，再统一格式；
2. CI 执行：

```bash
ruff check backend
python -m pytest -q
npm --prefix frontend test -- --run
npm --prefix frontend run build
npm --prefix frontend run test:e2e
pip-audit
npm --prefix frontend audit --omit=dev
alembic check
```

3. 新增代码不得增加 lint、安全审计或测试失败；
4. 对不可立即修复的告警建立明确 owner、原因和截止日期。

---

## 6. 推荐实施顺序

### 第一批：安全热修复

1. 禁止导出命令覆盖已有目录并限制路径；
2. 写操作改写无法溯源时一律回退原文；
3. 禁止归档 ACTIVE 模型并在归档/停用时清缓存；
4. 草稿 update 和冻结阶段校验固定标签。

这批修改范围较小，应优先发布止损。

### 第二批：一致性重构

1. Run 创建时固定 Split；
2. 模型状态切换事务化并加入项目级锁；
3. Rewriter 改为有界队列和可终止 Worker；
4. 迁移锁失败关闭。

### 第三批：算法、产品与工程治理

1. 阈值搜索确定性修复；
2. 配置模型简化；
3. 上传流式处理；
4. 前端交互和 E2E；
5. 依赖升级、镜像瘦身和 CI 门禁。

---

## 7. 总体验收标准

满足以下全部条件后，方可认为本轮优化完成：

- P0/P1 回归用例全部通过；
- 无法通过 CLI 参数删除仓库或任意已有目录；
- 写操作改写新增无依据对象时应用率为 0；
- 改写超时后不存在持续运行的遗留生成任务；
- 所有 FROZEN 数据集的标签集合严格属于固定五分类；
- 所有新建 Run 均有非空 `split_id`，Retry 使用相同 Split；
- 停用/归档/回滚后，数据库状态和运行时模型一致；
- 阈值搜索在大规模并列候选上选择结果正确且可复现；
- 项目级配置中不存在保存后不生效的字段；
- 超限上传不会被完整加载进内存；
- 后端测试、前端测试、E2E、构建、迁移检查和静态检查全部通过；
- 生产依赖审计无未处置的高危/严重漏洞，其他漏洞均有明确处置记录。

## 8. 建议新增的发布指标

| 指标 | 目标 |
|---|---|
| 写操作虚构对象应用率 | 0 |
| Rewrite 超时后遗留任务数 | 0 |
| Run 的 `split_id` 完整率 | 100% |
| 模型 DB/Runtime 不一致次数 | 0 |
| 非法标签冻结成功数 | 0 |
| P0/P1 回归测试通过率 | 100% |
| 后端与前端自动化测试通过率 | 100% |
| 生产未处置高危依赖漏洞数 | 0 |
| 上传超限请求内存增长 | 不超过一个读取分块加固定开销 |
