# Intent Router Studio 产品使用手册

> 适用版本：2026-08-25 本地版本（含 Query 改写）
> 产品地址：<http://127.0.0.1:8000>  
> API 文档：<http://127.0.0.1:8000/api/docs>

## 1. 产品定位

Intent Router Studio 是一个本地优先的意图路由器训练与评测平台，用于把用户 Query 分类为以下五种意图：

| 标签 | 含义 | 典型示例 | 后续处理建议 |
|---|---|---|---|
| `information` | 获取知识、解释或说明 | “实验显著性是什么意思？” | 进入知识问答链路 |
| `read_only` | 查询业务状态，但不修改数据 | “帮我查一下实验 123 的状态” | 进入只读 Skill 候选匹配 |
| `write_action` | 用户明确要求执行修改性动作 | “帮我关闭实验 123” | 仅获得写操作候选资格，仍需 Skill 匹配和确认 |
| `unclear` | 意图不完整、歧义较大或参数不足 | “把那个处理一下” | 进入澄清链路 |
| `oos` | 超出当前 Agent 的能力或业务边界 | “帮我订一张明天的机票” | 拒识或转交其他系统 |

重要边界：`write_action` 不是执行授权。平台只判断“可能存在写操作意图”，不会直接调用外部系统执行创建、修改、删除或发送操作。

## 2. 用户角色

### 2.1 产品使用者

主要使用 Playground 验证单条 Query、批量 Query 和不同模型之间的路由差异。

### 2.2 数据与模型运营人员

负责创建项目、导入训练材料、标注数据、发起训练、分析指标、调节阈值、注册和激活模型。

### 2.3 后台管理员

负责服务启停、查看日志、管理本地数据与制品、执行备份恢复、健康检查和故障排查。

## 3. 首次使用

### 3.1 打开产品

确认 Docker Desktop 已运行，然后在浏览器打开：

<http://127.0.0.1:8000>

若页面无法打开，在项目目录执行：

```bash
docker compose ps
```

正常情况下应看到：

- `irs-api`：状态为 `healthy`；
- `irs-worker`：状态为 `Up`；
- `irs-rewriter`：状态为 `healthy`，负责运行本地 `Qwen/Qwen3-0.6B` 改写模型；
- API 端口：`127.0.0.1:8000`。

### 3.2 推荐的完整使用流程

```text
创建并选择项目
  → 导入数据
  → 校验与标注
  → 创建防泄漏切分
  → 发起训练
  → 查看评估与调节阈值
  → 注册模型
  → 激活模型
  → Playground 验证
  → 沉淀错误案例并回流数据
```

## 4. 项目管理

### 4.1 创建项目

1. 进入左侧菜单“项目”。
2. 点击“新建项目”。
3. 输入项目名称和可选描述。
4. 创建后点击“设为当前”或行内“进入”将其设为当前项目。

项目列表的“进入”按钮会先把该行项目切换为当前项目、再跳转到总览（修改方案 V2 §4.5），不会出现“看的是 A 项目、页面仍是 B 项目”的错位。

项目是数据集、训练任务、模型和 Playground 案例的隔离边界。切换当前项目后，其他页面会自动展示该项目下的数据。

每个项目创建时会自动绑定固定的五分类 Label Schema。

### 4.2 删除项目

点击项目行内“删除”后，系统会先从后端读取完整影响范围，包括上传、数据集及质量报告、切分、训练及事件/指标/阈值、模型、Playground 案例、改写配置、术语表、改写反馈和审计记录。

- **空项目**：在二次确认框中点击“确认删除”即可；
- **非空项目**：确认框展示各类关联数据数量，必须输入完整项目名才能启用“确认删除”；
- **存在排队中或运行中训练**：禁止删除。请先取消训练并等待任务进入终态。

删除会级联清理全部数据库记录，以及项目上传文件、数据集/切分文件、Run 制品和模型制品。文件会先连同持久化恢复清单原子移动到制品根目录下的临时回收区；数据库事务失败时自动恢复文件，进程意外中断时会在下次 API 启动时自动恢复或清理已提交的回收区。制品路径同时校验根目录边界、项目归属和资源 ID，不匹配时失败关闭。

删除当前项目后，当前项目选择及该项目的浏览器 Playground 缓存会同步清空。此操作不可撤销；需要保留的数据请先导出或备份。

### 4.3 查看项目总览

进入“总览”可查看：

- 五分类 Label Schema；
- 数据集版本和状态；
- 最近训练运行；
- 当前激活模型及核心指标；
- 导入数据和发起训练的快捷入口。

如果总览提示“尚无激活模型”，Playground 和默认推理接口暂不可用。

## 5. 准备训练材料

### 5.1 支持的文件格式

平台支持：

- CSV；
- JSONL；
- XLSX；
- TXT。

默认限制：

- 单文件不超过 100 MB；
- 单文件不超过 500,000 行；
- 单条 `text` 或 `context` 不超过 4,000 字符。

### 5.2 推荐字段

| 字段 | 是否必填 | 说明 |
|---|---|---|
| `text` | 是 | 用户本轮 Query |
| `label` | 预标注模式必填 | 五分类标签之一 |
| `context` | 否 | 多轮上下文或补充背景 |
| `group_id` | 强烈建议 | 同一语义模板或最小差异样本组，用于防数据泄漏 |
| `source` | 否 | 样本来源，如线上误路由、人工构造、历史日志 |
| `risk_slice` | 否 | 风险切片，如 `qa_vs_write`、`negation`、`missing_object` |
| `is_hard_negative` | 否 | 是否为难负例，填写布尔值 |

推荐优先使用仓库中的示例文件了解格式：

```text
examples/queries.csv
```

### 5.3 数据设计建议

一个可用的数据集不应只包含直白正例。建议同时准备：

- 问答与执行的最小差异对；
- 查询与修改的最小差异对；
- 否定表达；
- 缺少对象或参数的表达；
- 领域相关但没有匹配能力的 NOTA 样本；
- 领域外 OOS 样本；
- 错别字、口语和简称；
- 多轮改口；
- 容易误判为 `write_action` 的难负例。

为了生成 train、validation、test 三个互不泄漏的集合，每个标签至少准备 3 个不同的语义组。实际训练建议远高于这一最低值。

## 6. 导入数据

### 6.1 上传和预览

1. 选择当前项目。
2. 进入“导入数据”。
3. 拖入文件或点击选择文件。
4. 上传完成后检查编码、行数和预览内容。
5. 确认系统建议的字段映射，尤其是 `text` 和 `label`。

CSV 文件支持 UTF-8 和 GBK 自动探测。若中文预览异常，建议先将源文件转换为 UTF-8 再上传。

**大小限制与防护**（修改方案 V2 §4.4）：

- 上传采用分块流式落盘，服务端边接收边计数；文件超过大小上限（`MAX_UPLOAD_MB`，默认 100MB）时立即终止并删除已接收的临时数据，返回 `FILE_TOO_LARGE`，不会留下半个文件。
- 应用层同时拦截请求体：声明超过上限的请求在读 body 之前即返回 `REQUEST_BODY_TOO_LARGE`（HTTP 413）。
- XLSX 文件额外做压缩包防护：解压后总大小（`MAX_XLSX_EXPAND_MB`，默认 500MB）、sheet 数（`MAX_XLSX_SHEETS`，默认 20）、首表行数/列数（`MAX_XLSX_ROWS` 默认 100 万、`MAX_XLSX_COLS` 默认 256）超限时拒绝解析，防止恶意小文件解压耗尽内存磁盘。触发时返回 `ARCHIVE_EXPANSION_TOO_LARGE` 或 `PARSE_ERROR`。
- 若部署在反向代理（nginx 等）之后，需在代理层设置等价的请求体上限（如 nginx `client_max_body_size`，略大于 `MAX_UPLOAD_MB`），避免代理先把超大文件完整收下再交给应用。

### 6.2 选择导入模式

#### 已预标注

适用于文件中已经有 `label` 的材料。导入后生成 `FROZEN` 数据集，可直接校验、切分和训练。

#### 未标注

适用于只有 Query、尚未人工分类的材料。导入后生成 `DRAFT`，需要进入标注台完成标注。

#### 单一标签

适用于文件中的全部样本都属于同一个标签。选择默认标签后批量导入。

### 6.3 标签映射

如果原始文件使用中文或其他标签，例如“问答”“查询”“执行”，需要在导入页面映射到平台的五个标准标签。

非法标签会记录为质量错误并阻断训练。同一规范化文本出现冲突标签时，导入会直接报错。

## 7. 数据集管理与标注

### 7.1 数据集状态

| 状态 | 含义 | 可执行操作 |
|---|---|---|
| `DRAFT` | 草稿版本 | 标注、修改样本、校验、提交新版本 |
| `FROZEN` | 冻结版本 | 校验、切分、训练；不可原地修改 |

冻结用于保证训练可复现。如果需要修改 FROZEN 数据集，应派生新的 DRAFT，而不是覆盖原版本。

### 7.2 质量报告

进入数据集详情，点击“重新校验”。质量报告分为：

- 错误：阻断训练；
- 警告：允许继续，但建议处理；
- 统计：样本量、标签分布、难负例占比等。

常见问题：

| 问题 | 处理方式 |
|---|---|
| 存在未标注样本 | 进入标注台完成分类 |
| 标签不在 Schema 内 | 修正为五个标准标签之一 |
| 某类别样本过少 | 补充该类样本和不同语义组 |
| `group_id` 大量缺失 | 按语义模板补充分组 |
| 难负例比例过低 | 增加容易混淆但不应命中的样本 |
| 标签冲突 | 人工确认同一 Query 的正确标签 |

### 7.3 使用标注台

1. 在 DRAFT 数据集详情点击“进入标注台”。
2. 阅读 `text` 和可选 `context`。
3. 选择五分类标签或难负例标记。
4. 修改会即时保存。
5. 完成后返回详情页重新校验。

标注时应判断用户想要的最终结果，而不是只看某个关键词。例如“怎么创建实验”通常是信息咨询，“帮我创建实验”才是写操作候选。

### 7.4 创建数据切分

数据集满足以下条件后才能创建切分：

- 所有样本已标注；
- 没有阻断级质量错误；
- 每个标签至少包含 3 个语义组；
- 同一语义组不跨集合。

在数据集详情点击“创建切分”。默认比例为：

- train：70%；
- validation：15%；
- test：15%。

系统按 `group_id` 进行分组切分，避免同一模板的近义样本同时出现在训练集和测试集。

`risk_test` 是 test 中带风险标签或难负例的子集，只用于最终风险评估，不参与校准和调参。

## 8. 发起训练

### 8.1 选择数据集

进入“发起训练”，只能选择：

- 状态为 FROZEN；
- 已全部标注；
- 没有质量错误；
- 包含全部五类样本的数据集。

### 8.2 训练预设

| 预设 | 默认参数 | 适用场景 |
|---|---|---|
| quick | epochs=1，iterations=3，batch=4，pairs≤2,000 | 低内存冒烟验证、演示、检查数据链路 |
| standard | epochs=2，iterations=5，batch=8，pairs≤4,000 | 日常训练与方案比较 |
| strict | epochs=5，iterations=10，batch=8，pairs≤8,000 | 正式候选模型，耗时更长 |

底座模型默认为 `BAAI/bge-small-zh-v1.5`，分类训练使用 SetFit。

Docker 中一般使用 CPU；Mac 原生运行可以自动使用 MPS。

SetFit 会按 `num_iterations` 扩展正负对比配对。SetFit 1.1.x 在该参数非空时会把全部配对实例化到内存，因此系统训练时会将配对数量截断到 `max_embedding_pairs`：嵌入体只使用有界配对进行微调，分类头仍使用完整训练集。训练配置默认使用 `max_length=128`；只有在机器内存充足时才应调大 `max_embedding_pairs` 或 `max_length`。

本地 Docker 默认启用低内存模式（`fine_tune_embeddings=false`）：冻结 BGE-small 编码器，只使用完整训练集训练 SetFit 分类头。该模式不会保存 Transformer 反向传播状态，适合 8GB Docker VM。只有具备更大内存或 GPU 时才建议打开“微调 BGE 嵌入体（高内存）”；打开后仍受 `max_embedding_pairs` 上限保护。

### 8.3 安全约束

默认阈值搜索硬约束：

- `false_write_rate ≤ 0.005`；
- `write_precision ≥ 0.95`。

在满足安全约束的候选中，系统优先最大化 `safe_coverage`。出现并列候选时，依次按"Macro F1 更高 → 阈值总和更保守 → 字典序"选择，整个过程对全部并列候选精确枚举（可视化采样不影响选择），相同数据与配置必然得到相同阈值。搜索结果会报告可行组合数、并列候选数和最终选择依据。如果没有可行解，会回退到保守默认阈值，而不是强行选择风险较高的组合。

### 8.4 提交与排队

点击“提交训练”后，任务进入 Worker 队列。第一版默认同一时间只执行一个训练任务。

提交时系统会立即固定本次训练使用的切分（split）：数据集已有切分时固定为最新一个；没有切分时按训练种子当场创建。之后即使同一数据集新建了其他切分，排队的任务也不会漂移，保证训练可复现。切分条件不满足（如语义组不足）会在提交时直接报错，而不是等到 Worker 执行才失败。

训练阶段依次为：

```text
QUEUED
→ PREPARING
→ TRAINING_EMBEDDING
→ TRAINING_HEAD
→ CALIBRATING
→ SEARCHING_THRESHOLDS
→ EVALUATING
→ PACKAGING
→ SUCCEEDED
```

## 9. 查看训练运行

### 9.1 实时进度与日志

进入“训练运行”，点击目标 Run。详情页通过 SSE 实时显示阶段、进度、日志和终态。

可执行操作：

- 取消 QUEUED 或运行中的任务；
- 对 FAILED、CANCELLED、INTERRUPTED 任务创建重试 Run；
- 查看本次训练的完整配置；
- 成功后注册模型；
- 将错误样本回流为新的数据集草稿。

### 9.2 核心指标解释

| 指标 | 含义 | 关注方向 |
|---|---|---|
| Macro F1 | 五类分类效果的宏平均 | 越高越好，同时观察各类表现 |
| coverage | 被策略门接受的样本比例 | 不应脱离风险指标单独追求 |
| safe_coverage | 被接受且路由正确的样本占总样本比例 | 越高越好 |
| selective_accuracy | 被接受样本中的正确率 | 越高越好 |
| false_write_rate | 非写样本被接受为写操作的比例 | 核心安全指标，越低越好 |
| write_precision | 接受为写操作的样本中，真实为写的比例 | 应达到安全约束 |
| unclear_rate | 被拒识并转为澄清的比例 | 过高会影响体验，过低可能增加误路由 |
| ECE/NLL/Brier | 概率校准质量 | 用于判断置信度是否可信 |
| margin | Top-1 与 Top-2 概率差 | 越小通常越不确定 |

评估时不要只看总体准确率。至少同时检查：

- `false_write_rate`；
- `write_precision`；
- QA 与写操作的混淆；
- risk_test；
- 各风险切片；
- 测试集样本数量和置信区间。

### 9.3 阈值调节

Run 成功后可在“阈值调节”中模拟：

- 默认最低置信度；
- 写操作最低置信度；
- OOS 最低置信度；
- 最小 margin。

模拟不会直接影响已注册模型。确认满足安全约束后，保存为新的阈值版本，再在注册模型时选择该版本。

如果调整违反 false write rate 或 write precision 约束，后台会拒绝保存。

### 9.4 错误分析与回流

错误分析页可以：

- 查看真实标签、预测路由、置信度和原因码；
- 导出 CSV；
- 选择典型错误生成新的 DRAFT 数据集；
- 在标注台复核后形成新版本并重新训练。

推荐优先回流：

- 误判为 `write_action` 的样本；
- QA/查询/执行最小差异样本；
- 低 margin 样本；
- OOS 近域样本；
- 多轮改口和缺参数样本。

## 10. 模型注册与激活

### 10.1 注册模型

Run 状态为 SUCCEEDED 后，点击“注册模型”。注册时可以：

- 设置模型名称；
- 选择搜索结果或手动保存的阈值版本；
- 查看即将注册的配置。

后台会复制模型制品并生成 Manifest。Manifest 包含模型、标签、校准、阈值和评估文件的哈希，用于后续完整性校验。

### 10.2 激活模型

进入“模型注册表”，选择候选模型并点击“激活”。激活流程为：

```text
校验 Manifest 和模型权重
→ 临时加载模型
→ Smoke inference
→ 切换数据库中的 ACTIVE 模型
→ 热替换内存 Runtime
```

加载或校验失败时，旧 ACTIVE 模型应继续服务。

### 10.3 回滚模型

模型注册表提供两个回滚入口，行为一致：顶部“回滚到……”按钮（最近一个归档模型）与表格行内 ARCHIVED 模型的“回滚激活”按钮。回滚会重新校验旧模型制品，加载与 Smoke inference 通过后在单个事务里完成切换；校验失败时旧模型保持 ACTIVE 继续服务，目标模型保持 ARCHIVED，不会出现中间状态。

弹窗按目标状态区分语义（修改方案 V2 §4.5）：普通候选模型展示“确认激活”（调用激活接口，事务内归档旧模型并切换）；ARCHIVED 模型展示“确认回滚”（调用回滚接口）。两者都会先展示与当前激活模型的新旧指标对比，确认后才执行。

### 10.4 停用保护与审计

- 当前 ACTIVE 模型不能直接归档（服务端返回 `CANNOT_ARCHIVE_ACTIVE`）：请先激活或回滚到其他版本，再停用旧模型。这样项目指针不会突然为空、在线推理不会中断；
- 激活 / 回滚 / 停用按项目互斥执行，并发切换不会产生双 ACTIVE；
- 每次激活 / 回滚 / 停用都会写入结构化审计事件（含切换前后模型 ID），可通过 `GET /api/v1/projects/{project_id}/audit-events` 查询完整的模型切换时间线；
- 服务重启后 ACTIVE 模型按指针懒加载，缓存与数据库指针不一致时自动以指针为准重载。

## 11. Playground 使用

Playground 依赖当前项目已经存在 ACTIVE 模型。

Playground 默认把当前项目的上次输入、选项和结果保存在浏览器本地 24 小时。页头可随时关闭缓存或点击“清空缓存”；关闭时会立即清理所有项目已缓存的 Query 和结果。页面内“案例保存原文”开关只控制服务端案例入库，与浏览器缓存开关分离。

### 11.1 单条推理

1. 输入 Query；
2. 按需填写多轮上下文；
3. 点击“执行推理”；
4. 查看路由结果、决策、概率、margin、原因码和效果上限。

开启 Debug 后可以查看：

- 实际应用的阈值；
- 温度校准参数；
- 标签顺序；
- 阈值版本。

### 11.2 结果字段

| 字段 | 含义 |
|---|---|
| `route` | 最终路由标签 |
| `decision` | `accept` 或进入澄清/拒识 |
| `confidence` | Top-1 校准后概率 |
| `margin` | Top-1 与 Top-2 概率差 |
| `probabilities` | 五分类概率分布 |
| `reason_codes` | 阈值、margin 或安全门的机器可读原因 |
| `effect_ceiling` | 当前路由允许的最大效果范围 |
| `required_next_gate` | 下游仍需经过的门控 |
| `model_version_id` | 本次使用的模型版本 |

即使结果为 `write_action`，下游也必须继续执行 Skill 匹配、参数校验、风险检查和用户确认。

### 11.3 批量推理

切换到“批量”，每行填写一条 Query。默认单次最多 1,000 条。

批量功能适合：

- 快速抽查一批历史 Query；
- 比较改版前后的路由分布；
- 发现需要加入评测集的错误样本。

### 11.4 A/B 对比

A 默认为当前 ACTIVE 模型，B 可以选择其他已注册模型。系统会对比：

- 最终路由是否不同；
- 决策是否不同；
- margin 差异；
- 两侧概率分布。

### 11.5 沉淀案例

单条推理后可以填写预期标签并保存案例。

默认只保存 Query 哈希，不保存原文。只有明确打开“保存原文”时，后台才会保存 `text/context`。

建议线上或含敏感信息的数据继续保持“仅哈希”。

## 12. Query 改写（Query 理解）

Query 改写在路由器之前增加一个“把口语化、缺主语、带指代的 Query 变成独立可理解 Query”的环节，用于改善下游检索和澄清提示的质量。

**它不改变路由。** 无论改写结果如何，`final_route` 永远等于原始 Query 的五分类路由；改写文本只用于下游检索（且仅在 safe_apply 且安全门全绿时）。这是产品的硬约束，不由任何配置开关控制。

改写的设计原则是：**改写后语义 ⊆ 原始 Query + 已提供上下文能够支持的语义**。系统通过实体、否定、语气、风险动作、置信度和双路路由等安全检查降低语义漂移风险，但生成模型的输出不能视为事实或执行授权。

> 当前版本上线建议：保持 `shadow`，不要在真实业务链路启用 `safe_apply`。当前安全门对纯中文操作对象的可追溯校验仍不完整，同时本地 CPU 生成超时后可能继续占用生成队列。只有修复相关问题并完成 12 个风险切片验收后，才应考虑放开 `safe_apply`。

### 12.1 四种模式

| 模式 | 行为 | 适用场景 |
|---|---|---|
| `off` | 完全关闭，一键回到现有稳定链路 | 出问题时的回退开关 |
| `normalize_only` | 只做确定性规范化（术语表 + 标点/全半角），不调用生成模型 | 无生成服务也想统一术语 |
| `shadow`（默认） | 双路运行：原文路由为正式结果，改写文本只记录、不下发 | 上线前观察期 |
| `safe_apply` | 安全门全部通过时，下游检索改用改写文本；正式路由仍不变 | 验收门槛通过后放开 |

模式在「改写设置」页按项目配置；Playground 的「Query 理解」页也可单次覆盖。

### 12.2 Playground「Query 理解」

使用前提：当前项目必须已有 ACTIVE 路由模型，否则 Playground 会提示 `MODEL_NOT_ACTIVE`。

1. 打开左侧菜单「Playground」，切换到顶部的「Query 理解」；
2. 输入 Query 和可选的多轮上下文（例如 Query “这个怎么停？”，上下文 “当前讨论实验 123”）；
3. 选择模式。第一次体验推荐明确选择 `shadow`，不要直接选择 `safe_apply`；
4. 点击“理解 Query”，查看：
   - 三个文本：原始 → 规范化 → 独立可理解（发生变化时高亮）；
   - 命中的术语替换（L0）；
   - 双路路由：正式路由（原文，恒为 final）与改写文本的路由对比；
   - 下游 Query：实际应传给检索的是哪一版、来源标记、是否命中缓存；
   - 安全门 8 项检查逐项结果与原因码。
5. 生成服务不可用或超时时，页面顶部会显示降级提示，结果自动回退原文，不影响正式路由。

推荐体验用例：

| 目的 | Query | Context | 推荐模式 | 预期关注点 |
|---|---|---|---|---|
| 指代补全 | `这个怎么停？` | `当前讨论实验 123` | `shadow` | 是否补全实验 123；改写前后路由是否冲突 |
| 只读咨询 | `它现在什么状态？` | `当前讨论实验 456` | `shadow` | 不得把查询升级为停止或修改动作 |
| 否定关系 | `按刚才说的处理` | `不要停止实验 789` | `shadow` | “不要”是否保留，安全门是否给出 `NEGATION_CHANGED` |
| 术语归一 | `查下 exp 123` | 留空 | `normalize_only` | 需先配置 `实验 | exp`，应展示 `exp → 实验` |

本地性能说明：

- `normalize_only` 不调用生成模型，通常可立即返回；
- `shadow` 和 `safe_apply` 会调用本地 Qwen。在当前纯 CPU Docker 环境中，一次生成实测约 60～70 秒；
- 项目默认超时为 5 秒时，生成式改写通常会触发 `TIMEOUT` 并自动降级。用于本地体验时，应由管理员提高该项目超时；
- 页面一直显示“解析中”不代表路由训练卡住，它可能正在等待本地生成模型。

结果判读：

| 页面区域 | 应如何理解 |
|---|---|
| 原始 Query | 用户实际输入，也是正式路由的唯一依据 |
| 独立可理解 | 模型生成或术语规则处理后的候选文本 |
| 原文路由（正式） | 最终 `final_route`，改写不能覆盖它 |
| 改写文本路由 | 只用于发现改写是否造成意图漂移 |
| 下游 Query | `shadow` 固定使用原文；只有 `safe_apply` 且安全门通过才可能显示来源为改写 |
| Rewrite Safety Gate | 任一关键检查失败时，下游继续使用原文 |
| fallback | Rewriter 超时、不可用或输出非法时的安全降级，不是路由服务故障 |

### 12.3 反馈闭环

在「Query 理解」结果下方可以直接反馈：

- **采用**：改写正确；
- **拒绝**：选择原因（指代解析错误、实体幻觉、否定反转、语气改写、数字丢失、过度改写等）；
- **编辑后采用**：手动修正改写文本再提交。

反馈默认只保存 Query 哈希、结论、原因码和路由标签，不保存 Query、Context、模型改写文本或人工编辑文本。只有明确打开“保存原文”时，这些文本才会落库。反馈用于后续术语表维护和微调数据筛选。

### 12.4 改写设置

「改写设置」页（左侧菜单）按项目管理：

- **模式与阈值（项目级策略）**：模式、改写模型、最低改写置信度（默认 0.8）、超时（默认 5s）、是否要求双路路由一致、是否保存原文。每次保存生成新版本，可回看历史；
- **改写模型连接（外部模型 V1）**：系统级连接列表 + 新建/编辑抽屉。支持三种来源——内置本地 Qwen（默认，随 rewriter 容器部署）、智谱 GLM（官方通用开放平台端点固定，Bearer 鉴权，`thinking` 显式关闭、JSON mode 默认开启）、OpenAI 兼容 API（自定义 Base URL，仅允许 https 公网地址）；
- **术语表**：每行“规范术语 | 别名1, 别名2”。别名命中即替换为规范术语（L0，确定性、无模型参与）。保存同样生成新版本；
- **服务健康**：按连接展示熔断状态（closed / open / unhealthy / rate_limited）、rewriter 是否可用、请求数/缓存命中/P50/P95、降级与安全拦截原因分布。

**API Key 安全**：密钥只写不读——输入框只在创建/编辑时出现，保存后立即清空；数据库存 AES-256-GCM 密文（主密钥来自 `REWRITE_CREDENTIAL_MASTER_KEY` 环境变量，经未入库的 `.env` 注入）；接口与日志永远只返回 `****末4位` 遮罩。编辑时留空表示保留原 Key；彻底清除需调用 `DELETE /rewrite/provider-connections/{id}/credential` 并二次确认。主密钥轮换用 `python -m app.cli.rotate_rewrite_master_key`（单事务整体回滚）。

**使用外部模型的流程**：

1. 管理员在部署环境配置 `REWRITE_CREDENTIAL_MASTER_KEY`（生成命令见 `.env.example`）；
2. 在「改写模型连接」新建 GLM 连接，粘贴开放平台 API Key，勾选外部数据传输确认，点「保存并测试」；
3. 测试通过后，在「模式与阈值」的“改写模型”下拉中选择该连接（未测试通过的连接不可选），随配置一起保存为新版本——切换即版本化、可回滚；
4. 切换后旧缓存自动失效（缓存键含连接/revision/模型/生成参数指纹）；
5. 在 Playground「Query 理解」的 Provider Trace 中核对实际使用的连接、模型、耗时、token 数与降级原因。

**不变量（任何外部模型都不改变）**：`final_route` 恒为原文路由；外部 API 超时/限流/鉴权失败/非法输出时自动回退原文，`/inference/rewrite` 与 `/predict` 仍返回 200；一个连接故障只熔断该连接，不影响本地 Qwen 与其他连接；改写永不提供执行授权。

生成服务（rewriter）是独立容器，仅内网可达，与 API/Worker 资源隔离；它宕机时所有改写请求自动降级为原文，接口永不返回 5xx。

当前版本的配置边界：

- 页面上的模式、改写模型、置信度阈值、超时、路由一致性和原文存储开关按项目生效；
- 页面超时输入上限目前为 60 秒，低于当前 CPU 环境的部分实际请求耗时。需要更长超时时可由管理员通过配置 API 设置，后端上限为 120 秒；
- 超时会真正终止生成（rewriter 内部以独立子进程承载模型，超时即终止该进程并释放 CPU，下一次请求自动重新拉起并重载模型）。因此超时后紧接着的请求会额外承担一次模型重载耗时，属预期行为；
- 生成队列有界（默认 2，环境变量 `REWRITE_QUEUE_CAPACITY`）：队列满时立即返回“生成队列已满，已回退原文”（`REWRITER_BUSY`），不会堆积请求。该情况不计入熔断。远程连接另有独立并发上限（默认 2，超限同样立即回退）；
- 旧版本配置中的 `deployment`（部署配置只读）字段进入兼容期，前端改读 `selected_provider`。

### 12.5 评测与上线门槛

评测集 `examples/rewrite_eval.jsonl`（12 个切片 × 5 条：QA/执行近邻、只读/写近邻、否定、指代、缺宾语、多轮纠正、术语歧义、数字 ID 保留、近域 OOS、无需改写、上下文注入、长上下文）。

对运行中的服务执行：

```bash
python scripts/eval_rewrite.py --base-url http://127.0.0.1:8000/api/v1 \
  --project prj_xxx --mode shadow --out var/rewrite_eval_report.json
```

放开 `safe_apply` 前必须通过验收门槛（§15）：

- 虚假写操作升级率 = 0；
- 实体幻觉率 ≤ 0.1%；
- 否定保留率 ≥ 99.9%；数字/ID 保留率 ≥ 99.9%；
- 意图保留率 ≥ 99%；双路路由一致率 ≥ 98%。

### 12.6 一键回退

「改写设置」把模式切到 `off` 并保存即完成回退：改写链路完全旁路，`/predict` 与历史行为逐字节一致。默认出厂模式为 `shadow`，即使在 shadow 下改写也只观察不生效。

## 13. 后台部署与运维

### 13.1 Docker 启动

在项目目录执行：

```bash
docker compose build
docker compose up -d
docker compose ps
```

首次构建需要下载 Python、Node、Torch、SetFit 等依赖，通常耗时 5–15 分钟。

### 13.2 停止与重启

```bash
# 停止服务，保留数据库和制品
docker compose down

# 重新启动
docker compose up -d

# 重启单个服务
docker compose restart api
docker compose restart worker
```

Query 改写服务可以单独查看或重启：

```bash
docker compose logs -f rewriter
docker compose restart rewriter
curl -s http://127.0.0.1:8000/api/v1/inference/rewrite/health
```

健康结果至少应满足：

- `breaker_state` 为 `closed`；
- `rewriter.ok` 为 `true`；
- `rewriter.loaded` 为 `true`；
- `rewriter.model_id` 为当前实际加载的模型，例如 `Qwen/Qwen3-0.6B`。

注意：容器 `healthy` 只表示健康接口可访问。首次启动仍可能在后台加载或预热模型，应结合 `loaded` 字段和 Rewriter 日志判断。

不要使用 `docker compose down -v`，除非明确希望删除关联卷。当前主要数据通过项目目录下的 `./var` 挂载保存。

### 13.3 查看日志

```bash
# API 和 Worker 实时日志
docker compose logs -f api worker

# 最近 200 行
docker compose logs --tail=200 api worker

# 只查看 Worker
docker compose logs -f worker
```

训练卡住、失败或无法取消时，应优先查看 Worker 日志。若错误码为 `WORKER_OOM`，表示子进程收到 `SIGKILL`（通常是 exit 137/内存不足）；若为 `WORKER_RESTART`，表示容器级重启恢复了遗留任务。

### 13.4 健康检查

```bash
curl -fsS http://127.0.0.1:8000/api/v1/health
```

正常响应：

```json
{
  "status": "ok",
  "request_id": "req_..."
}
```

系统信息：

```bash
curl -fsS http://127.0.0.1:8000/api/v1/system/info
curl -fsS http://127.0.0.1:8000/api/v1/system/config
```

### 13.5 本地数据目录

| 路径 | 内容 |
|---|---|
| `var/app.db` | SQLite 数据库 |
| `var/uploads/` | 上传的原始文件 |
| `var/projects/` | 项目数据集和 split 制品 |
| `var/runs/` | 训练 Run 制品、日志、指标和模型输出 |
| `var/models/` | 注册后的模型版本和 Manifest |
| `var/hf-cache/` | Hugging Face 模型缓存 |
| `var/tmp/` | 临时文件 |

不要在服务运行期间手工修改模型或数据集制品。Manifest 校验会拒绝加载被修改的模型。

### 13.6 备份

建议停服后执行一致性备份：

```bash
docker compose down
cp -R var "var-backup-$(date +%Y%m%d-%H%M%S)"
docker compose up -d
```

备份必须同时包含数据库和制品目录，不能只备份 `app.db`，否则数据库中的路径与模型制品可能不一致。

如果必须在线备份 SQLite，建议使用 SQLite backup 命令生成数据库副本，再同步制品目录，并记录备份时间点。

### 13.7 恢复

恢复前先停止服务：

```bash
docker compose down
```

确认目标 `var` 目录后，将当前目录移走，再把完整备份恢复为 `var`，最后启动服务：

```bash
mv var var-before-restore
cp -R /path/to/var-backup var
docker compose up -d
docker compose ps
```

恢复后应检查：

- health 接口；
- 项目和数据集列表；
- ACTIVE 模型能否完成 Playground 推理；
- Manifest 校验是否通过。

### 13.8 清理临时文件

前端“系统信息”页面可以清理由上传产生、但未被导入引用的临时文件。

也可以调用：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/system/cleanup \
  -H 'Content-Type: application/json' \
  -d '{"target":"uploads_tmp"}'
```

清理当前项目 Playground 历史需要传入 `project_id`：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/system/cleanup \
  -H 'Content-Type: application/json' \
  -d '{"target":"playground_history","project_id":"prj_xxx"}'
```

清理操作不可撤销，执行前应确认目标。

## 14. API 使用示例

完整字段以 Swagger 为准：<http://127.0.0.1:8000/api/docs>。

### 14.1 创建项目

```bash
curl -X POST http://127.0.0.1:8000/api/v1/projects \
  -H 'Content-Type: application/json' \
  -d '{"name":"路由实验项目","description":"QA 与执行意图区分"}'
```

### 14.2 单条推理

```bash
curl -X POST http://127.0.0.1:8000/api/v1/inference/predict \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id":"prj_xxx",
    "text":"帮我创建一个实验",
    "context":null,
    "debug":true
  }'
```

### 14.3 批量推理

```bash
curl -X POST http://127.0.0.1:8000/api/v1/inference/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id":"prj_xxx",
    "items":[
      {"text":"实验显著性是什么意思"},
      {"text":"查一下实验 123 的状态"},
      {"text":"关闭实验 123"}
    ]
  }'
```

### 14.4 A/B 对比

```bash
curl -X POST http://127.0.0.1:8000/api/v1/inference/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "project_id":"prj_xxx",
    "text":"把这个实验停掉",
    "model_a":null,
    "model_b":"mdl_xxx"
  }'
```

`model_a=null` 表示使用当前 ACTIVE 模型。

### 14.5 错误响应

统一错误结构：

```json
{
  "error": {
    "code": "MODEL_NOT_ACTIVE",
    "message": "项目没有激活模型，请先在模型注册中心激活",
    "details": {},
    "request_id": "req_..."
  }
}
```

排障时应保留 `request_id`，并在 API 日志中检索对应请求。

### 14.6 Query 理解（改写）

```bash
curl -s http://127.0.0.1:8000/api/v1/inference/rewrite -H 'Content-Type: application/json' -d '{
  "project_id": "prj_xxx",
  "text": "这个怎么停？",
  "context": "当前讨论实验 123",
  "mode": "shadow"
}'
```

关键字段：

| 字段 | 含义 |
|---|---|
| `final_route` | 正式路由，恒等于 `original_route.route` |
| `rewrite.standalone_query` | 独立可理解改写文本 |
| `rewrite_route.route` | 改写文本的五分类路由（仅对比用） |
| `downstream_query` / `downstream_query_source` | 下游检索应使用的文本及其来源（`original`/`rewrite`） |
| `safety_decision` | `allow_rewrite` / `allow_rewrite_shadow` / `blocked` / `fallback_original` / `mode_off` |
| `fallback_reason` | 降级原因（`PROVIDER_UNAVAILABLE` / `TIMEOUT` / `INVALID_JSON`） |

在 `/predict` 上启用（不带 `rewrite` 参数时行为与历史完全一致，仅多出 `query_understanding` 字段）：

```bash
curl -s http://127.0.0.1:8000/api/v1/inference/predict -H 'Content-Type: application/json' -d '{
  "project_id": "prj_xxx",
  "text": "这个怎么停？",
  "rewrite": {"enabled": true, "mode": "shadow", "include_trace": true}
}'
```

服务健康与指标：

```bash
curl -s http://127.0.0.1:8000/api/v1/inference/rewrite/health
```

## 15. 本地原生运行

如果希望在 Mac 上使用 MPS 加速训练，可以不使用 Docker：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e "backend[dev]"

cd frontend
npm install
npm run build
cd ..

cd backend
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端启动 Worker：

```bash
cd backend
source ../.venv/bin/activate
python -m app.worker.main
```

前端开发模式：

```bash
cd frontend
npm run dev
```

访问 <http://127.0.0.1:5173>，Vite 会将 `/api` 代理到 8000 端口。

## 16. 常见问题排查

### 16.1 页面打不开

```bash
docker compose ps
docker compose logs --tail=100 api
curl -v http://127.0.0.1:8000/api/v1/health
```

检查 8000 端口是否被其他进程占用。

### 16.2 API 正常但训练不开始

检查 Worker：

```bash
docker compose ps worker
docker compose logs --tail=200 worker
```

确认 Worker 和 API 使用相同的 `DATABASE_URL` 与 `ARTIFACT_ROOT`。

### 16.3 首次训练长时间停留在加载模型

首次运行需要下载约 100 MB 的 BGE 模型。检查：

- 网络是否可以访问模型源；
- `var/hf-cache` 是否可写；
- 磁盘剩余空间；
- Worker 日志中的下载错误。

### 16.4 数据集不能训练

依次检查：

1. 数据集是否为 FROZEN；
2. 是否存在未标注样本；
3. 质量报告是否有 errors；
4. 是否覆盖全部五类；
5. 每类是否至少有 3 个语义组；
6. 是否已经创建有效 split。

### 16.5 没有可行阈值

常见原因：

- validation 样本过少；
- 写操作难负例不足；
- 模型把非写样本高置信度预测为写；
- `max_false_write_rate` 或 `min_write_precision` 设置过严。

优先改进数据和模型，不建议仅通过放宽安全约束解决。

### 16.6 Playground 提示 MODEL_NOT_ACTIVE

进入模型注册表，确认当前项目存在状态为 ACTIVE 的模型。如果刚切换项目，需要在新项目下单独训练或激活模型。

### 16.7 Run 显示 INTERRUPTED

通常表示 Worker 在训练过程中重启。打开 Run 详情查看错误，再点击“重试”创建新的 Run。旧 Run 和制品应保留用于审计。

### 16.8 磁盘空间不足

模型、Run 制品和 Hugging Face 缓存都会占用空间。进入“系统信息”查看剩余空间，并清理无引用的上传临时文件。为避免与正在进行的流式上传竞态，系统只清理已超过 1 小时安全存活时间的未引用文件。

不要直接删除数据库仍然引用的 `runs/`、`models/` 或 `projects/` 文件。

## 17. 安全与隐私建议

- 保持服务绑定 `127.0.0.1`，不要直接暴露到局域网或公网；
- 如需多人访问，应增加认证、授权、HTTPS 和审计日志；
- 保持 `LOG_RAW_TEXT=false`；
- Playground 案例默认只保存哈希；
- 不上传未经脱敏的用户隐私或密钥；
- 不手工修改已注册模型制品；
- `write_action` 必须继续经过 Skill 匹配、参数检查、权限检查和用户确认；
- 生产使用前必须建立稳定的 frozen test 和 risk_test，不允许用测试集反复调阈值；
- 模型激活前应人工查看 false write rate、write precision、风险切片和置信区间；
- Query 改写保持默认仅哈希存储（`store_raw_text=false`），改写反馈同样默认不落原文；
- 改写不得直接或间接授权执行 Skill：非写意图被升级为写意图在任何模式下都会被硬拦截。

**工程治理（修改方案 V2 §5）**：

- 容器以非 root 用户（uid 10001）运行，仅数据目录可写；宿主 `./var` 需对该 UID 可写（首次部署 `sudo chown -R 10001:10001 ./var`，macOS Docker 桌面版通常自动映射）；
- Python 依赖全部锁定在 `backend/requirements.lock`（运行时）/ `requirements-dev.lock`（测试），升级依赖需重新生成并全量回归；
- 发布前运行 `./scripts/ci.sh` 一键门禁：ruff 静态检查 → 干净容器后端测试 → alembic 迁移漂移检查 → pip-audit / npm audit（审计 JSON 落 `var/ci/`）→ 前端单测与构建 → Playwright E2E；
- 已知但暂无法修复的依赖漏洞统一登记 `SECURITY_EXCEPTIONS.md`（含原因与失效日期），pip-audit 只放行清单内 ID，到期未处置 CI 失败；
- 前端生产依赖当前 0 漏洞（echarts 6.1.0 / react-router-dom 7.18.2）；torch / transformers 的上游修复版本与 setfit 兼容性冲突，豁免至 2026-12-31。


## 18. 上线前建议检查清单

- [ ] 项目名称和用途清晰；
- [ ] 数据集覆盖全部五类；
- [ ] 每类有足够的语义组和难负例；
- [ ] train/validation/test 无 group 泄漏；
- [ ] risk_test 不参与训练和阈值搜索；
- [ ] false write rate 满足目标；
- [ ] write precision 满足目标；
- [ ] QA 与执行最小差异样本通过；
- [ ] OOS 和 unclear 样本表现符合预期；
- [ ] Manifest 与模型权重校验通过；
- [ ] 激活前后的探针 Query 已执行；
- [ ] 回滚路径经过验证；
- [ ] 数据库和模型制品已经完整备份；
- [ ] 服务只绑定到预期网络接口；
- [ ] 下游写操作仍有独立确认门；
- [ ] Query 改写已跑完 12 切片评测且验收门槛全部通过；
- [ ] 改写在 shadow 模式下观察期不少于一周，路由冲突率与降级率可接受；
- [ ] 已演练“改写出问题 → 切 off 一键回退”路径；
- [ ] rewriter 停机演练通过（接口不 5xx、自动回退原文）。
