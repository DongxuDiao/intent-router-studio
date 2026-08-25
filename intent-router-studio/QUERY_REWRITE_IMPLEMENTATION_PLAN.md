# Intent Router Studio Query 改写能力落地方案

## 1. 目标与边界

### 1.1 建设目标

在当前 `BGE-small-zh-v1.5 + SetFit` 五分类路由器之前增加 Query Understanding / Rewrite 能力，使系统能够：

- 将依赖上下文的 Query 改写为可独立理解的 Query；
- 将口语、简称和非标准业务词归一为项目术语；
- 识别指代、省略、缺失参数和不确定假设；
- 同时保留原始 Query、规范化 Query 和改写 Query；
- 在 Playground 中解释改写内容及其对路由结果的影响；
- 为后续知识库检索和 Skill 候选召回输出更稳定的 `downstream_query`。

### 1.2 非目标

首版不做以下事情：

- 不根据猜测补造实验 ID、用户、时间、金额等事实；
- 不把咨询改写成执行命令；
- 不把否定表达改写成肯定表达；
- 不让生成模型直接授权或执行 Skill；
- 不用改写模型替代 BGE + SetFit 五分类器；
- 不在第一期微调生成模型；
- 不把改写后的 Query 默认写入训练集或日志原文。

### 1.3 第一性原则

系统要解决的是“让下游更准确地理解用户原意”，而不是“把用户的话变得更像某个 Skill”。

因此必须满足：

```text
改写后语义 ⊆ 原始 Query + 已提供上下文能够支持的语义
```

改写只能消除表达噪声和上下文依赖，不能增加新的授权、对象、参数或动作。

## 2. 核心产品决策

### 2.1 改写与意图路由解耦

当前链路：

```text
text + context
→ normalize_text / encode_input
→ BGE + SetFit 五分类
→ confidence + margin 策略门
```

目标链路：

```text
original_query + context
        ├──────────────────────────────┐
        ↓                              ↓
确定性规范化                    Query Rewrite
        ↓                              ↓
原文五分类 original_route       rewritten_query
                                       ↓
                                影子五分类 rewrite_route
        └─────────────┬────────────────┘
                      ↓
               Rewrite Safety Gate
                      ↓
       route + downstream_query + rewrite_trace
```

其中：

- `original_route` 是现有安全路由结果；
- `rewrite_route` 仅用于一致性检查；
- `downstream_query` 供知识库或 Skill 候选召回使用；
- 所有执行授权仍由下游参数、权限、风险与确认门决定。

### 2.2 首版采用旁路模式

第一阶段改写能力默认处于 `shadow`：

- 正常返回现有路由结果；
- 同时生成改写结果并对改写 Query 再分类；
- 展示两路结果是否一致；
- 不自动用改写结果替换正式路由；
- 收集人工反馈和评测数据。

完成离线评测后再开放 `safe_apply`：

- 正式路由仍基于原始 Query；
- 只有通过安全门时，`downstream_query` 才使用改写 Query；
- 任何意图或效果等级冲突都回退到原文或澄清。

### 2.3 不把“改写置信度”当作执行授权

即使改写模型输出 `confidence=0.99`，也只能表示它认为改写忠实，不能证明用户授权执行。

## 3. 能力分层

### 3.1 L0：确定性规范化

复用当前 `normalize_text()`：

- Unicode NFC；
- 全角转半角；
- 空白折叠；
- ASCII 大小写归一。

新增项目术语词典能力：

- 别名到标准术语映射；
- 明确的拼写纠正；
- 不改变动作和否定关系；
- 每次替换记录 `source_span`、`target_term`、`rule_id`。

示例：

```text
“libra exp 看下”
→ “Libra 实验 看下”
```

L0 无需生成模型，低风险、低延迟，可默认启用。

### 3.2 L1：上下文独立化

使用本地小型生成模型将依赖上下文的 Query 改写为 standalone Query。

示例：

```text
context: “实验 123 当前流量是 10%。”
query: “这个怎么调到 20%？”

standalone_query:
“如何将实验 123 的流量从 10% 调整到 20%？”
```

注意：这里仍然是“如何调整”的信息请求，不能改成“将实验 123 调整到 20%”。

### 3.3 L2：结构化理解

在改写结果中附带非授权型结构信息：

- `mentioned_action`：用户明确提到的动作；
- `objects`：文本中明确出现或可由上下文唯一解析的对象；
- `constraints`：时间、范围、目标值等；
- `missing_slots`：完成理解仍缺失的参数；
- `assumptions`：改写过程中使用的假设；
- `used_context_refs`：使用了哪些上下文片段。

这些字段用于解释、检索和澄清，不直接映射为 Skill 参数执行。

## 4. 改写模式

项目级配置提供四种模式：

| 模式 | 行为 | 默认用途 |
|---|---|---|
| `off` | 只执行现有规范化与路由 | 回滚与对照 |
| `normalize_only` | L0 术语归一，不调用生成模型 | 低延迟线上模式 |
| `shadow` | 生成改写并双路分类，但不应用 | 首次上线默认模式 |
| `safe_apply` | 安全门通过后将改写用于下游检索 | 评测通过后的正式模式 |

默认配置：

```json
{
  "mode": "shadow",
  "provider": "local_qwen",
  "model_id": "Qwen/Qwen3-0.6B",
  "max_context_chars": 4000,
  "max_new_tokens": 256,
  "timeout_ms": 5000,
  "min_rewrite_confidence": 0.8,
  "require_route_consistency": true,
  "fallback": "original",
  "store_raw_text": false
}
```

## 5. 本地模型方案

### 5.1 推荐模型

首版推荐 `Qwen/Qwen3-0.6B`：

- 用于生成式改写，而不是替代 BGE 分类；
- 0.6B 量级，适合本地试验；
- 支持中文和结构化输出；
- 使用 Apache 2.0 权重；
- 可通过 Transformers 本地加载；
- 作为 Provider 可随时替换为 1.7B、MLX、GGUF 或远程服务。

官方模型卡：<https://huggingface.co/Qwen/Qwen3-0.6B>

### 5.2 为什么不使用 BGE 做改写

BGE 是编码器模型，擅长分类、召回和相似度计算，不具备开放式文本生成能力。因此：

- BGE + SetFit：继续负责五分类安全路由；
- Qwen：只负责受约束的结构化改写；
- Rewrite Safety Gate：负责阻止语义和授权漂移。

### 5.3 独立服务而非加载进 API 进程

在 `docker-compose.yml` 增加 `rewriter` 服务：

```yaml
rewriter:
  image: intent-router-studio:latest
  command: ["python", "-m", "app.rewriter.main"]
  volumes:
    - ./var:/data/var
  environment:
    HF_HOME: /data/var/hf-cache
    REWRITE_MODEL_ID: Qwen/Qwen3-0.6B
    REWRITE_DEVICE: auto
    REWRITE_PORT: 8010
  expose:
    - "8010"
  deploy:
    resources:
      limits:
        memory: 3g
```

API 通过 Docker 内网访问 `http://rewriter:8010`。优势：

- 避免生成模型占满 API 进程内存；
- 改写服务失败不影响现有路由器；
- 可以单独限流、重启和扩容；
- 后续可替换为 MLX、llama.cpp 或远程服务。

### 5.4 推理参数

改写是确定性结构任务，建议：

```json
{
  "do_sample": false,
  "max_new_tokens": 256,
  "repetition_penalty": 1.05,
  "enable_thinking": false
}
```

如果所选模型或版本不适合 greedy decoding，应通过 Provider 配置调整，但最终必须通过 JSON Schema 校验和语义安全门，不能直接信任原始输出。

## 6. 输出协议

### 6.1 RewriteResult

```json
{
  "original_query": "这个怎么调到20%？",
  "normalized_query": "这个怎么调到20%?",
  "standalone_query": "如何将实验 123 的流量从 10% 调整到 20%？",
  "rewrite_type": "context_resolution",
  "changed": true,
  "should_use": true,
  "confidence": 0.93,
  "preserved_intent": true,
  "mentioned_action": "调整实验流量",
  "objects": [
    {
      "type": "experiment",
      "value": "123",
      "source": "context",
      "confidence": 1.0
    }
  ],
  "constraints": {
    "current_traffic": "10%",
    "target_traffic": "20%"
  },
  "missing_slots": [],
  "assumptions": [],
  "used_context_refs": ["context[0]"],
  "reason_codes": ["RESOLVED_PRONOUN", "PRESERVED_QUESTION_FORM"],
  "model": {
    "provider": "local_qwen",
    "model_id": "Qwen/Qwen3-0.6B",
    "prompt_version": "rewrite-prompt-v1"
  },
  "latency_ms": 820
}
```

### 6.2 枚举约束

`rewrite_type`：

```text
none
normalization
term_normalization
context_resolution
ellipsis_completion
mixed
```

`reason_codes` 至少支持：

```text
NO_REWRITE_NEEDED
NORMALIZED_TERM
RESOLVED_PRONOUN
COMPLETED_ELLIPSIS
MISSING_CONTEXT
AMBIGUOUS_REFERENCE
UNSUPPORTED_ASSUMPTION
NEGATION_CHANGED
MODALITY_CHANGED
ACTION_INTENSIFIED
OBJECT_INVENTED
ROUTE_CONFLICT
LOW_CONFIDENCE
TIMEOUT
INVALID_JSON
PROVIDER_UNAVAILABLE
```

### 6.3 响应兼容

现有 `/inference/predict` 字段保持不变，只增加可选字段：

```json
{
  "route": "information",
  "decision": "accept",
  "...": "现有字段",
  "query_understanding": {
    "rewrite": {},
    "original_route": {},
    "rewrite_route": {},
    "route_consistent": true,
    "downstream_query": "如何将实验 123 的流量从 10% 调整到 20%？",
    "downstream_query_source": "rewrite",
    "safety_decision": "allow_rewrite"
  }
}
```

旧客户端忽略新增字段即可，无需立即升级。

## 7. Rewrite Safety Gate

### 7.1 必须进行的检查

改写结果至少经过以下检查：

1. JSON Schema 合法；
2. `standalone_query` 非空且不超过长度限制；
3. 原文否定词与改写否定关系一致；
4. 疑问、请求执行、条件假设等语气不被强化；
5. 改写中的 ID、时间、数值、人员和对象都能追溯到原文或上下文；
6. 没有新增高风险动作；
7. `confidence` 达到阈值；
8. 原文与改写后的五分类结果满足一致性政策。

### 7.2 效果等级

定义保守的效果等级：

```text
none < read_only < external_write_candidate
```

若改写后的效果等级高于原文，禁止应用改写：

```text
information → write_action  禁止
read_only → write_action    禁止
unclear → write_action      禁止
oos → write_action          禁止
```

`write_action → information` 同样不能直接接受，因为改写可能淡化了用户的执行要求。此时保留原路由，并标记 `ROUTE_CONFLICT`。

### 7.3 一致性矩阵

| 原文路由 | 改写路由 | downstream_query | 正式路由 |
|---|---|---|---|
| 相同 | 相同 | 可在高置信时使用改写 | 原文路由 |
| information | read_only | 原文 | `unclear` 或原文路由，进入澄清 |
| read_only | information | 原文 | 原文路由 |
| 任意非写 | write_action | 原文 | 原文路由并告警 |
| write_action | 任意非写 | 原文 | `write_action`，继续确认门 |
| unclear | 明确类别 | 默认原文 | `unclear`，除非后续人工确认 |
| oos | 非 oos | 原文 | `oos` 或澄清 |
| 非 oos | oos | 原文 | 原文路由并标记冲突 |

### 7.4 首版正式决策

首版不允许改写修改正式路由：

```python
final_route = original_route
```

只有 `downstream_query` 可以在安全门通过后切换为改写结果。

## 8. Prompt 设计

### 8.1 System Prompt

```text
你是 Query Rewrite Engine。你的任务是把用户本轮 Query 改写为脱离上下文后仍可理解的 Query。

必须遵守：
1. 只能使用 original_query 和 context 中明确存在的信息。
2. 不得创造 ID、名称、时间、数值、对象、动作或授权。
3. 不得将“如何做/能否做/想了解”改成执行命令。
4. 不得删除或反转否定、条件、犹豫、撤销、假设和不确定表达。
5. 指代无法唯一解析时，不猜测；保留原文并输出 missing_slots。
6. 输出必须符合给定 JSON Schema，不输出解释性正文。
7. confidence 表示改写忠实度，不表示执行授权。
```

### 8.2 Few-shot 样例

#### 咨询不能变执行

```json
{
  "context": "当前讨论实验 123",
  "original_query": "这个怎么停？",
  "expected": {
    "standalone_query": "如何停止实验 123？",
    "mentioned_action": "了解如何停止实验",
    "preserved_intent": true
  }
}
```

禁止输出：

```text
停止实验 123
```

#### 无法解析时不猜

```json
{
  "context": "",
  "original_query": "把那个关了",
  "expected": {
    "standalone_query": "把那个关了",
    "should_use": false,
    "missing_slots": ["target_object"],
    "reason_codes": ["AMBIGUOUS_REFERENCE"]
  }
}
```

#### 保留否定

```json
{
  "context": "实验 123 正在运行",
  "original_query": "先别停它",
  "expected": {
    "standalone_query": "暂时不要停止实验 123",
    "preserved_intent": true
  }
}
```

## 9. 后端代码改造

### 9.1 新增目录

```text
backend/app/query_rewrite/
├── __init__.py
├── schemas.py            # RewriteRequest / RewriteResult
├── prompt.py             # prompt-v1 与 few-shot
├── terminology.py        # 项目术语归一
├── provider.py           # Provider 抽象
├── qwen_provider.py      # 本地 Qwen 实现
├── client.py             # API → rewriter HTTP 客户端
├── safety.py             # 语义、实体、否定、路由一致性门
├── runtime.py            # 模型加载与健康检查
└── evaluation.py         # 改写评测指标

backend/app/services/rewrite_service.py
backend/app/api/rewrite.py
backend/app/rewriter/main.py
```

### 9.2 Provider 接口

```python
class RewriteProvider(Protocol):
    provider_name: str
    model_id: str

    def health(self) -> dict: ...

    def rewrite(
        self,
        original_query: str,
        context: str | None,
        terminology: dict[str, str],
        timeout_ms: int,
    ) -> RewriteResult: ...
```

Provider 抽象应支持：

- `local_qwen_transformers`；
- 后续 `local_mlx`；
- 后续 `llama_cpp`；
- 测试用 `stub`；
- 可选远程兼容接口。

### 9.3 Rewrite Service

核心伪代码：

```python
def understand_query(db, request, router_runtime):
    config = get_project_rewrite_config(db, request.project_id)
    normalized = normalize_text(request.text)

    original_result = router_runtime.predict(request.text, request.context)

    if config.mode == "off":
        return original_result, no_rewrite_trace(normalized)

    term_result = apply_terminology(normalized, config.terminology_version)

    if config.mode == "normalize_only":
        candidate = term_result.text
    else:
        candidate = provider.rewrite(
            original_query=term_result.text,
            context=request.context,
            terminology=config.terminology,
        )

    rewrite_result = router_runtime.predict(candidate, context=None)
    safety = evaluate_rewrite_safety(
        original=request.text,
        context=request.context,
        rewrite=candidate,
        original_route=original_result,
        rewrite_route=rewrite_result,
        config=config,
    )

    downstream_query = candidate if safety.allow and config.mode == "safe_apply" else request.text

    return original_result, build_understanding_trace(...)
```

### 9.4 超时与降级

生成模型调用不能阻断原有路由：

```text
rewriter timeout / unavailable / invalid JSON
→ 记录 reason_code
→ downstream_query 使用原文
→ 正常返回原文五分类结果
```

默认超时建议 5 秒，熔断策略：

- 连续 5 次失败后打开熔断 30 秒；
- 熔断期间直接回退原文；
- health 接口暴露模型状态、最近错误和熔断状态；
- 不把 Provider 异常转换为 `/predict` 的 5xx。

## 10. API 设计

### 10.1 独立改写接口

```http
POST /api/v1/inference/rewrite
```

请求：

```json
{
  "project_id": "prj_xxx",
  "text": "这个怎么调到20%？",
  "context": "实验 123 当前流量为 10%",
  "mode": "shadow",
  "debug": true
}
```

响应：

```json
{
  "rewrite": {},
  "original_route": {},
  "rewrite_route": {},
  "route_consistent": true,
  "safety_decision": "allow_rewrite",
  "downstream_query": "这个怎么调到20%？",
  "downstream_query_source": "original"
}
```

### 10.2 扩展现有 PredictRequest

```python
class RewriteOptions(BaseModel):
    enabled: bool = False
    mode: Literal["project_default", "off", "normalize_only", "shadow", "safe_apply"] = "project_default"
    include_trace: bool = False

class PredictRequest(BaseModel):
    # 现有字段保持
    rewrite: RewriteOptions | None = None
```

未传 `rewrite` 时保持现有行为，避免 API 兼容性问题。

### 10.3 配置接口

```http
GET  /api/v1/projects/{project_id}/rewrite-config
PUT  /api/v1/projects/{project_id}/rewrite-config
POST /api/v1/projects/{project_id}/rewrite-config/validate
GET  /api/v1/projects/{project_id}/terminology
PUT  /api/v1/projects/{project_id}/terminology
```

配置修改要生成版本，不原地覆盖：

- `rewrite_config_version`；
- `terminology_version`；
- `prompt_version`；
- 创建时间与 hash。

### 10.4 批量接口

批量改写开销较高，首版限制：

- 默认最多 100 条；
- 后端内部微批量；
- 每条单独降级；
- 返回 `rewrite_failed_count`；
- 不因单条失败中止整个批次。

## 11. 数据模型

### 11.1 RewriteConfigVersion

```python
class RewriteConfigVersion(Base):
    id: str
    project_id: str
    version: int
    config: JSON
    hash: str
    status: str          # DRAFT / ACTIVE / ARCHIVED
    created_at: datetime
```

`Project` 增加：

```python
active_rewrite_config_id: str | None
```

### 11.2 TerminologyVersion

```python
class TerminologyVersion(Base):
    id: str
    project_id: str
    version: int
    terms: JSON
    hash: str
    created_at: datetime
```

术语结构：

```json
{
  "canonical": "实验",
  "aliases": ["exp", "实验单"],
  "confusable_with": ["任务"],
  "never_replace_when": ["正则或边界规则"],
  "enabled": true
}
```

### 11.3 RewriteFeedback

```python
class RewriteFeedback(Base):
    id: str
    project_id: str
    input_hash: str
    original_text: str | None
    context: str | None
    proposed_rewrite: str | None
    edited_rewrite: str | None
    verdict: str          # accept / reject / edit
    reason_codes: JSON
    original_route: str | None
    rewrite_route: str | None
    model_id: str
    prompt_version: str
    created_at: datetime
```

默认只保存 hash。只有用户显式允许时保存原文和上下文。

### 11.4 数据库迁移

当前项目使用 `create_all`，但增加字段和表后应正式引入 Alembic；`create_all` 不会为已有 SQLite 表增加列。

最低要求：

- 新表使用 migration 创建；
- `projects.active_rewrite_config_id` 使用 migration 添加；
- migration 前自动备份 `var/app.db`；
- 提供 upgrade 和 downgrade 测试；
- Docker 启动时先执行 migration，再启动 API。

## 12. 缓存设计

### 12.1 Rewrite Cache Key

```text
sha256(
  project_id
  + rewrite_config_version
  + terminology_version
  + prompt_version
  + model_id
  + normalized_query
  + normalized_context
)
```

不能只使用 Query 文本，否则不同术语版本或 Prompt 会串用缓存。

### 12.2 缓存内容

缓存只保存：

- 结构化 RewriteResult；
- Provider 元数据；
- 安全检查结果。

不缓存：

- 当前请求的 debug 展示字段；
- `request_id`；
- 请求级延迟；
- 用户人工编辑结果。

### 12.3 容量与过期

首版：

- 进程内 LRU 5,000 条；
- TTL 24 小时；
- 配置或模型切换时清空相关项目缓存；
- 不引入 Redis。

## 13. 前端产品方案

### 13.1 Playground 增加“Query 理解”模式

在现有“单条 / 批量 / A-B”旁增加：

```text
Query 理解
```

页面布局：

```text
┌ 原始输入 ──────────────────────────────────────┐
│ Query                                             │
│ Context                                           │
│ 模式：off / normalize / shadow / safe_apply       │
│ [分析并改写]                                       │
└───────────────────────────────────────────────────┘

┌ 改写对比 ──────────────────────────────────────┐
│ 原始 Query         | 改写 Query                  │
│ 原文路由           | 改写路由                    │
│ information        | information                 │
│ [采用改写] [使用原文] [编辑]                     │
└───────────────────────────────────────────────────┘

┌ 安全检查 ──────────────────────────────────────┐
│ 路由一致 ✓  否定保留 ✓  无新增实体 ✓             │
│ downstream_query 来源：rewrite                    │
└───────────────────────────────────────────────────┘
```

### 13.2 展示要求

必须同时展示：

- 原始 Query；
- 规范化 Query；
- standalone Query；
- 使用了哪些上下文；
- 补全或替换内容；
- 假设与缺失参数；
- 原文和改写后的路由；
- 安全门结果；
- 最终 downstream Query；
- 模型、Prompt 和术语版本；
- 改写延迟与是否命中缓存。

### 13.3 反馈闭环

用户可以：

- “改写正确”；
- “应使用原文”；
- 编辑为正确改写；
- 选择错误原因；
- 选择是否保存原文。

错误原因：

```text
改变了意图
改变了否定关系
创造了对象或参数
指代解析错误
术语归一错误
遗漏重要信息
表达不自然
不需要改写
```

### 13.4 项目设置页

新增“Query 改写配置”：

- 模式；
- Provider 与模型；
- 置信度阈值；
- 超时；
- 是否要求路由一致；
- 术语表管理；
- Prompt 版本；
- Shadow 指标概览；
- 一键回退 `off`。

## 14. 改写评测集

### 14.1 数据格式

独立于五分类数据集，建议 JSONL：

```json
{
  "id": "rw_001",
  "original_query": "这个怎么停？",
  "context": "当前讨论实验 123",
  "expected_rewrite": "如何停止实验 123？",
  "expected_should_use": true,
  "expected_route": "information",
  "forbidden_facts": [],
  "required_facts": ["实验 123"],
  "risk_slice": "qa_vs_write",
  "group_id": "stop_exp_question_001"
}
```

### 14.2 必备风险切片

- `qa_vs_write`；
- `readonly_vs_write`；
- `negation`；
- `ambiguous_reference`；
- `missing_object`；
- `multi_turn_correction`；
- `term_ambiguity`；
- `number_and_id_preservation`；
- `oos_near_domain`；
- `no_rewrite_needed`；
- `context_injection`；
- `long_context`。

### 14.3 指标

#### 忠实性指标

- `intent_preservation_rate`；
- `negation_preservation_rate`；
- `entity_hallucination_rate`；
- `number_id_preservation_rate`；
- `required_fact_recall`；
- `forbidden_fact_rate`。

#### 路由指标

- `route_consistency_rate`；
- `false_write_escalation_rate`：非写原文被改写为写的比例；
- `write_downgrade_rate`：写意图被改写淡化的比例；
- 原文和改写分别对应的 Macro F1；
- 原文和改写分别对应的 false write rate。

#### 产品指标

- `rewrite_accept_rate`；
- `manual_edit_rate`；
- `fallback_rate`；
- `clarification_reduction_rate`；
- P50/P95 延迟；
- Provider 超时率和非法 JSON 率。

## 15. 验收门槛

首版从 `shadow` 升级为 `safe_apply` 前，冻结测试集至少满足：

| 指标 | 建议门槛 |
|---|---|
| false write escalation rate | 0 |
| entity hallucination rate | ≤ 0.1% |
| negation preservation rate | ≥ 99.9% |
| number/ID preservation rate | ≥ 99.9% |
| intent preservation rate | ≥ 99% |
| route consistency rate | ≥ 98% |
| P95 rewrite latency | 本地环境 ≤ 3 秒，超时 ≤ 5 秒 |
| Provider failure fallback | 100% 回退原文且原路由可用 |

高风险切片必须单独达标，不能被总体平均数掩盖。

## 16. 测试方案

### 16.1 单元测试

新增：

```text
backend/tests/test_rewrite_schema.py
backend/tests/test_terminology.py
backend/tests/test_rewrite_safety.py
backend/tests/test_rewrite_cache.py
backend/tests/test_rewrite_prompt.py
backend/tests/test_rewrite_fallback.py
```

必须覆盖：

- 非法 JSON；
- 缺字段和未知字段；
- 否定反转；
- 疑问变命令；
- 新增 ID/数值/对象；
- 原文与改写路由冲突；
- 超时和服务不可用；
- 不同配置版本缓存隔离；
- Debug 字段不污染缓存；
- `write_action` 不被降级授权。

### 16.2 API 集成测试

- rewrite 关闭时响应与当前版本一致；
- shadow 模式返回 trace 但 downstream Query 仍是原文；
- safe_apply 仅在安全门通过时应用；
- Provider 失败时 `/predict` 仍返回 200 和原文路由；
- 批量中单条失败不影响其他条目；
- 项目间术语和配置不串用；
- 原文默认不落库。

### 16.3 E2E

Playwright 覆盖：

1. 输入带上下文的 Query；
2. 查看三种 Query；
3. 对比原文与改写路由；
4. 查看安全检查；
5. 切换使用原文/改写；
6. 编辑正确改写并提交反馈；
7. rewriter 服务不可用时显示降级提示。

### 16.4 性能测试

- 单请求并发 1/2/4；
- 冷启动与热启动延迟；
- 100 条批量；
- API、Worker、rewriter 同时运行时的内存峰值；
- 训练期间改写请求的延迟和资源隔离。

## 17. 可观测性

### 17.1 指标

```text
rewrite_requests_total
rewrite_success_total
rewrite_fallback_total{reason}
rewrite_route_conflict_total{from,to}
rewrite_safety_reject_total{reason}
rewrite_latency_ms
rewrite_cache_hit_total
rewrite_provider_health
```

### 17.2 日志

默认不记录原文，只记录：

- `request_id`；
- Query hash；
- project_id；
- 配置、模型、Prompt、术语版本；
- latency；
- safety decision；
- reason codes；
- 原文路由和改写路由。

只有 `LOG_RAW_TEXT=true` 且本地调试时才允许记录原文。

## 18. 分阶段实施

### 阶段 0：基线冻结（1–2 天）

- 冻结当前 `/predict` 行为和测试；
- 建立 Query 改写评测集；
- 定义 JSON Schema 和安全规则；
- 引入 Alembic 并完成已有数据库迁移验证。

交付：评测集、接口协议、migration 基线。

### 阶段 1：L0 术语归一（2–3 天）

- 项目术语表；
- 规则改写；
- 变更 trace；
- Playground 对比；
- 不调用生成模型。

交付：`normalize_only`。

### 阶段 2：本地生成式 Shadow（4–6 天）

- 独立 rewriter 服务；
- Qwen Provider；
- 严格 JSON；
- 原文/改写双路分类；
- Rewrite Safety Gate；
- 超时、熔断和降级；
- Playground 反馈。

交付：`shadow`，不改变正式路由与 downstream Query。

### 阶段 3：安全应用（3–5 天）

- 离线评测达标；
- 启用 `safe_apply`；
- downstream Query 接入知识库/Skill 候选召回；
- 项目级开关与回滚；
- 监控仪表盘。

交付：改写只影响召回输入，不影响授权。

### 阶段 4：数据闭环与可选微调

- 汇总 accept/reject/edit 反馈；
- 构造高质量 rewrite pairs；
- 对比 Prompt 优化、LoRA 微调和更大模型；
- 只有离线指标显著提升才引入微调模型。

## 19. 文件级改造清单

### 后端新增

```text
backend/app/query_rewrite/*
backend/app/services/rewrite_service.py
backend/app/api/rewrite.py
backend/app/rewriter/main.py
backend/app/models/rewrite_tables.py
backend/tests/test_rewrite_*.py
backend/alembic/*
```

### 后端修改

```text
backend/app/main.py                         注册 rewrite router
backend/app/config.py                       rewriter URL、超时、模型配置
backend/app/schemas/__init__.py             RewriteOptions 与响应 Schema
backend/app/services/inference_service.py   双路执行和保守合并
backend/app/router_core/runtime.py           支持复用同一分类 Runtime 双路预测
backend/app/models/tables.py                Project 配置指针
docker-compose.yml                          rewriter 服务与资源限制
.env.example                                REWRITE_* 配置
```

### 前端新增

```text
frontend/src/components/QueryRewritePanel.tsx
frontend/src/components/RewriteSafetyChecks.tsx
frontend/src/components/RewriteDiff.tsx
frontend/src/pages/RewriteSettings.tsx
```

### 前端修改

```text
frontend/src/pages/Playground.tsx
frontend/src/App.tsx
frontend/src/types/index.ts
frontend/src/api/client.ts
```

## 20. Definition of Done

- [ ] 现有不带 rewrite 参数的 API 行为完全兼容；
- [ ] 原始 Query、规范化 Query、standalone Query 可追溯；
- [ ] 改写不能直接改变正式五分类路由；
- [ ] 非写意图不会因改写升级为写操作授权；
- [ ] 所有实体、ID、时间和数字可追溯到输入；
- [ ] 否定、疑问、条件和撤销语气得到保留；
- [ ] Provider 超时或崩溃时自动回退原文；
- [ ] 项目配置、术语、Prompt 和模型均有版本；
- [ ] 缓存键包含所有影响改写结果的版本；
- [ ] 默认日志和反馈不保存原文；
- [ ] Shadow 离线评测达到验收门槛；
- [ ] 前端可以对比、接受、拒绝和编辑改写；
- [ ] 后端单测、API 集成测试和前端 E2E 全部通过；
- [ ] 训练 Worker 与 rewriter 的资源相互隔离；
- [ ] 一键切换 `off` 后恢复到当前稳定链路；
- [ ] 产品手册和 API 文档同步更新。

## 21. 最小可行版本建议

如果希望先快速验证，不要一次实现全部能力。最小版本只做：

1. `POST /inference/rewrite`；
2. 独立本地 Qwen3-0.6B 服务；
3. 固定 Prompt + JSON Schema；
4. 原文和改写后的双路五分类；
5. 禁止任何路由冲突的改写被应用；
6. Playground 展示原文、改写、两路路由和安全原因；
7. 默认 `shadow`，不修改现有 `/predict`。

完成 100–300 条高风险人工评测后，再决定是否建设项目术语、safe_apply、反馈库和模型微调。
