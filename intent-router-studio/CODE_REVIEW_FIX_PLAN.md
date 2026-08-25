# Intent Router Studio 代码修改方案

## 1. 修改目标

修复当前代码审查发现的四类问题，确保：

- 小样本数据集能够稳定生成可训练、可校准、可评估的数据切分；
- 所有模型加载路径都完整校验模型权重和配套制品；
- Debug 请求与普通推理请求不会通过缓存互相污染；
- 阈值搜索指标定义准确，前端展示的数据与实际含义一致。

本次修改不调整五分类定义、模型训练算法和现有 API 主体结构。

## 2. 修改范围与优先级

| 优先级 | 问题 | 主要文件 | 发布要求 |
|---|---|---|---|
| P1 | 小样本切分可能产生空 validation/test | `backend/app/router_core/splitting.py`、`backend/app/services/dataset_service.py` | 阻断发布 |
| P1 | 显式模型版本加载未校验模型权重哈希 | `backend/app/router_core/runtime.py`、`backend/app/services/artifact_service.py` | 阻断发布 |
| P2 | Debug 与普通推理共用缓存导致响应串用 | `backend/app/router_core/runtime.py` | 发布前修复 |
| P2 | 阈值搜索重复计算 unclear，且可行解数量失真 | `backend/app/router_core/threshold_search.py` | 发布前修复 |

## 3. 方案一：修复小样本分组切分

### 3.1 当前问题

当前算法按标签聚合语义组，再按照行数配额贪心分配。对于每个类别只有 3 个语义组的情况，单个组可能大于 validation/test 配额，导致所有组都进入 train。

训练任务未在进入模型训练前验证三个 split 是否可用，后续可能在空数组推理或温度校准阶段才失败，错误位置与根因不一致。

### 3.2 修改设计

#### A. 切分算法增加最小集合保障

在 `group_split()` 中，对每个标签的组执行以下策略：

1. 少于 3 个组：不自动切分，返回阻断错误，而不是全部放入 train。
2. 恰好或多于 3 个组：
   - 先为 test 选择一个组；
   - 再为 validation 选择一个组；
   - 剩余组进入 train；
   - 仍有多余组时，再基于目标比例做贪心调整。
3. 选择 validation/test 首组时，优先选择最接近目标配额且不会耗尽 train 的组。
4. 保持同一 `group_id` 只能进入一个 split。

建议将单标签组分配提取为纯函数，便于单测：

```python
def assign_label_groups(
    groups: list[tuple[str, int]],
    ratios: dict[str, float],
    rng: random.Random,
) -> dict[str, str]:
    ...
```

#### B. 增加切分后置校验

新增 `validate_split()`，至少检查：

- train、validation、test 均非空；
- 每条样本恰好属于一个 split；
- 同一个 `group_id` 不跨 split；
- validation 和 test 包含全部五个标签；
- 每个标签在 train 中至少有一个语义组；
- `is_risk_test` 只能标记 test 中的样本。

不满足要求时返回结构化错误，例如：

```json
{
  "code": "INSUFFICIENT_SPLIT_GROUPS",
  "message": "类别 write_action 的语义组不足，无法同时生成 train/validation/test",
  "details": {
    "label": "write_action",
    "group_count": 2,
    "required_group_count": 3
  }
}
```

#### C. 训练启动前二次防御

在 Worker 合并数据集和 split 后、加载模型前再次验证：

```python
for split_name in ("train", "validation", "test"):
    if not texts_by_split[split_name]:
        raise RuntimeError(f"EMPTY_SPLIT: {split_name} 没有样本")
```

这层属于防御性检查，正常情况下应由创建 split 阶段提前阻断。

### 3.3 测试要求

新增测试：

- 五分类、每类恰好 3 个等大组：三个 split 均覆盖五类；
- 每类 3 个大小差异明显的组；
- 某类别只有 2 个组：返回明确阻断错误；
- 同一组包含多条样本：整组不跨 split；
- 固定 seed 时结果可复现；
- validation/test 为空时 Worker 在训练模型前失败；
- 风险样本只有进入 test 时才被标记为 `is_risk_test`。

### 3.4 验收标准

- 合法数据不会产生空 validation/test；
- 不满足切分条件的数据无法创建可执行训练任务；
- 错误信息能够直接指出具体标签及缺少的组数；
- 不发生 group 泄漏；
- 既有大数据集切分比例与当前结果近似，不产生明显回退。

## 4. 方案二：统一模型制品完整性校验

### 4.1 当前问题

Manifest 将文件哈希拆分为：

- `artifact_hashes`：阈值、标签、校准等普通文件；
- `artifact_hashes_model`：`setfit_model/` 下的模型文件。

`ModelRuntime.load(verify=True)` 只校验 `artifact_hashes`。模型激活路径会在外层调用完整校验，但 Playground 和 A/B 对比按显式版本加载模型时，会直接进入 `ModelRuntime.load()`，从而遗漏模型权重校验。

### 4.2 修改设计

#### A. 删除重复校验逻辑

`ModelRuntime.load()` 不再自行遍历部分哈希，统一调用：

```python
from app.services import artifact_service

if verify:
    manifest = artifact_service.verify_manifest(artifact_dir)
else:
    manifest = artifact_service.read_json(artifact_dir / "manifest.json")
```

完整校验必须覆盖：

- `artifact_hashes`；
- `artifact_hashes_model`；
- Manifest 中声明但实际缺失的文件；
- 文件内容被修改后的哈希不一致。

#### B. 保持所有加载入口默认 fail-closed

以下入口必须保持 `verify=True`：

- 激活模型；
- 服务重启后加载 ACTIVE 模型；
- Playground 指定模型版本；
- A/B 对比加载模型版本；
- 回滚并重新激活旧模型。

除专门的离线诊断工具外，业务代码不得传入 `verify=False`。

#### C. 统一异常类型

建议完整性校验统一返回：

```json
{
  "code": "HASH_MISMATCH",
  "message": "模型制品完整性校验失败",
  "details": {
    "problems": ["哈希不匹配: setfit_model/model.safetensors"]
  }
}
```

避免部分路径抛出 `ValueError`，部分路径抛出 `ApiError`。

### 4.3 测试要求

- 修改 `thresholds.json` 后，模型加载失败；
- 修改模型权重文件后，显式版本推理失败；
- 删除模型文件后，模型加载失败；
- 未修改的合法制品可以正常完成 smoke inference；
- ACTIVE 模型加载、显式版本加载、回滚路径均执行相同校验；
- 校验失败时不替换当前正在服务的 ACTIVE Runtime。

### 4.4 验收标准

- 任一 Manifest 覆盖文件被修改或删除后，所有业务加载入口均拒绝加载；
- 激活失败时旧模型继续正常提供服务；
- 模型加载只维护一套完整性校验实现。

## 5. 方案三：隔离 Debug 与普通推理缓存

### 5.1 当前问题

缓存键只包含模型版本、阈值版本、文本和上下文，不包含 `debug`。同时，带有 `debug` 字段的完整响应会被写入缓存，造成：

- 普通请求先执行后，Debug 请求命中缓存但没有 Debug 信息；
- Debug 请求先执行后，普通请求命中缓存并收到内部 Debug 信息。

### 5.2 修改设计

推荐缓存“与展示模式无关的基础预测”，而不是把 `debug` 加进缓存键。

执行流程调整为：

1. 根据模型、阈值、规范化输入生成缓存键；
2. 缓存只保存基础路由结果，不保存：
   - `debug`；
   - `cache_hit`；
   - `latency_ms`；
3. 命中或完成推理后复制基础结果；
4. 根据本次请求的 `debug` 参数动态附加 Debug 信息；
5. 根据本次执行状态附加 `cache_hit`。

参考结构：

```python
cached = self.cache.get(cache_key)
if cached is not None:
    result = dict(cached)
    result["cache_hit"] = True
else:
    result = runtime.predict(...)
    cache_payload = strip_request_specific_fields(result)
    self.cache.put(cache_key, cache_payload)

if debug:
    result["debug"] = build_debug_payload(runtime, threshold_overrides)
else:
    result.pop("debug", None)
```

带 `threshold_overrides` 的请求继续不进入共享缓存，避免不同实验阈值互相污染。

### 5.3 测试要求

针对同一输入验证以下顺序：

- 普通 → Debug：第二次命中缓存且包含 Debug；
- Debug → 普通：第二次命中缓存且不包含 Debug；
- 普通 → 普通：第二次命中缓存；
- Debug → Debug：第二次命中缓存且 Debug 内容正确；
- 带不同 `threshold_overrides` 的请求不复用结果；
- 模型激活或版本缓存更新后，旧预测缓存被清空。

### 5.4 验收标准

- Debug 字段严格由当前请求参数决定；
- 普通接口永远不会因为历史 Debug 请求返回内部信息；
- 修改后不降低基础预测缓存命中能力。

## 6. 方案四：修正阈值搜索指标

### 6.1 当前问题

#### A. `unclear` 被重复计入 Macro F1

五分类标签中已经包含 `unclear`，当前逻辑先在标签循环中计算一次，又作为附加路由类别计算一次。并列阈值比较时，`unclear` 权重相当于其他类别的两倍。

#### B. `n_feasible` 并非真实可行组合数

当前 `all_feasible` 只保存每个 margin 下 safe coverage 最大、且经过截断的部分候选，最终却把其长度展示为可行组合数。

### 6.2 修改设计

#### A. 明确最终路由标签定义

由于当前五分类已经包含 `unclear`，Macro F1 应只遍历五个标签一次：

```python
for i, cls in enumerate(labels):
    ...
return float(np.mean(f1s))
```

删除循环后的第二段 `unclear` 计算。

如果未来希望区分“模型预测 unclear”和“因阈值拒识产生的 clarification”，应新增独立名称，例如 `rejected`，不能继续共用 `unclear` 后重复计权。

#### B. 分离真实计数与保留候选

新增两个统计量：

- `n_feasible`：所有满足风险约束的网格组合数；
- `n_retained_candidates`：为并列比较或 Pareto 展示而保留的候选数。

在每轮 margin 搜索中执行：

```python
n_feasible += int(feasible.sum())
```

`all_feasible` 可以继续受内存上限控制，但不能再用于计算 `n_feasible`。

#### C. 更新 API Schema 和前端文案

阈值搜索响应增加：

```json
{
  "n_candidates": 1964160,
  "n_feasible": 25840,
  "n_retained_candidates": 500
}
```

前端分别展示：

- 可行组合：`n_feasible / n_candidates`；
- 用于可视化/择优的候选：`n_retained_candidates`。

### 6.3 测试要求

- 使用人工概率矩阵验证五类 Macro F1 与 sklearn 结果一致；
- 改变 unclear 样本数量时不会出现双倍权重；
- 小网格下以暴力枚举结果校验 `n_feasible`；
- `n_retained_candidates <= n_feasible`；
- 搜索结果满足 false write rate 和 write precision 约束；
- 修复前后的阈值差异可以通过固定样例解释。

### 6.4 验收标准

- Macro F1 的每个标签权重相同；
- `n_feasible` 与暴力枚举结果一致；
- 前端不再把截断后的最优候选数描述为全部可行解数量；
- false write rate、write precision、safe coverage 的计算口径不变。

## 7. 建议实施顺序

1. 先补失败测试，固定四类问题的复现条件；
2. 修复数据切分和切分后置校验；
3. 统一模型制品完整性校验；
4. 修复推理缓存的 Debug 串用；
5. 修正阈值搜索和前端字段；
6. 执行完整后端、前端和真实模型 smoke 测试。

建议拆分为四个独立提交，避免模型安全、训练数据和指标改动相互混杂。

## 8. 完整回归测试清单

### 后端自动化测试

```bash
docker compose exec -T api python -m pytest -q
```

要求：

- 既有 53 项测试全部通过；
- 新增上述四类回归测试；
- 不允许通过跳过测试规避真实模型或制品校验问题。

### 前端测试与构建

```bash
cd frontend
npm test -- --run
npm run build
```

补充阈值搜索统计展示的组件测试，验证缺失旧字段时的兼容降级。

### 真实链路验证

至少完成一次小规模 BGE + SetFit 训练，并依次验证：

1. 上传并冻结数据集；
2. 创建 split，确认 train/validation/test 和 risk_test 分布；
3. 完成训练、校准、阈值搜索和 test 评估；
4. 注册并激活模型；
5. 执行普通、Debug、批量和 A/B 推理；
6. 修改复制制品中的权重文件，确认显式版本加载被拒绝；
7. 确认完整性校验失败不会影响当前 ACTIVE 模型。

## 9. Definition of Done

满足以下条件才视为修改完成：

- 四类问题均有先失败、修复后通过的自动化回归测试；
- 后端和前端全量测试、构建均通过；
- 小样本合法数据可完成训练，无效数据能在训练开始前获得明确错误；
- 所有业务模型加载入口完整校验普通文件和模型权重；
- Debug 与普通响应在任意调用顺序下均不串用；
- 阈值搜索指标和前端文案含义一致；
- 至少一次真实模型 smoke 流程通过；
- 未改变 `information/read_only/write_action/unclear/oos` 五分类语义；
- `write_action` 仍然只代表候选动作意图，不构成执行授权。
