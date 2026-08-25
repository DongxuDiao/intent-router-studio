# Intent Router Studio

一个本地优先、面向 Agent 的意图路由器训练与可视化平台。

它使用 `BAAI/bge-small-zh-v1.5 + SetFit` 训练五分类路由器，将用户请求区分为知识问答、只读动作、写动作、需要澄清和能力范围外请求，并通过置信度、margin、校准和风险阈值避免“相似就执行”。项目还提供本地 Query 改写、训练工作台、模型注册/回滚和可解释 Playground。

> 安全边界：`write_action` 只代表“外部写操作候选”，不能直接触发写入；下游仍须完成 Skill 匹配和用户显式确认。Query 改写永远不能覆盖原文的正式路由。

## 快速开始

```bash
cd intent-router-studio
docker compose build
docker compose up -d
docker compose ps
```

服务健康后打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。首次启动和训练会下载本地模型，请预留时间与磁盘空间。

完整说明请进入项目目录阅读：

- [项目 README](intent-router-studio/README.md)
- [产品使用手册](intent-router-studio/PRODUCT_USER_MANUAL.md)
- [技术设计](TECHNICAL_DESIGN.md)
- [Query 改写方案](intent-router-studio/QUERY_REWRITE_IMPLEMENTATION_PLAN.md)

## 主要能力

- 数据上传、预览、标签映射、标注和防泄漏切分；
- BGE-small + SetFit 本地训练、温度校准和约束阈值搜索；
- 模型评估、注册、激活、A/B、回滚和制品校验；
- `off` / `normalize_only` / `shadow` / `safe_apply` 四级 Query 改写策略；
- 单条、批量、模型对比和 Query 理解 Playground；
- Docker 化 API、训练 Worker 和独立 Rewriter 服务。

## 使用与许可说明

默认部署仅面向本机，不包含公网身份认证、租户隔离和 TLS，请勿直接暴露到公网。运行时数据库、训练材料、模型制品和缓存位于 `intent-router-studio/var/`，不会提交到 Git。

仓库当前未附带开源许可证。公开可见不等于授予复制、修改或分发许可。
