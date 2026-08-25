"""Query Rewrite 能力包（修改方案 §9.1）。

分层：
- schemas      输出协议与解析校验（§6）
- prompt       prompt-v1 与 few-shot（§8）
- terminology  L0 术语归一（§3.1）
- safety       Rewrite Safety Gate（§7）
- cache        版本化改写缓存（§12）
- provider     Provider 抽象 + 实现（§9.2）
- client       API → rewriter HTTP 客户端（§9.4 熔断降级）
"""
