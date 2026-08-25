#!/usr/bin/env python3
"""生成脱敏示例数据集 examples/queries.csv（五分类 + group_id + 风险切片）。

用法: python scripts/make_example_data.py [--out examples/queries.csv] [--rows-per-template 4]
"""
from __future__ import annotations

import argparse
import csv
import random
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 每类模板：正例 + 反例（hard negative 见 risk_slice / is_hard_negative）
TEMPLATES: dict[str, list[dict]] = {
    "information": [
        ("Libra 怎么创建实验？", "qa_vs_write"),
        ("创建实验有哪些前置条件？", "qa_vs_write"),
        ("如何给实验添加分流组？", None),
        ("Review 流程是什么样的？", None),
        ("怎么查看实验的指标口径说明？", None),
        ("Libra 支持哪些统计方式？", None),
        ("什么是置信区间？", None),
        ("怎么理解实验的显著性结果？", None),
        ("如何配置实验的观察周期？", None),
        ("告警规则在哪里配置？", None),
        ("实验报告支持导出吗？", None),
        ("怎么写实验结论？", None),
        ("分层实验是什么概念？", None),
        ("怎么设置对照组？", None),
        ("Libra 是否支持暂停实验？", "oos_near_domain"),
        ("新人怎么申请 Libra 权限？", None),
        ("指标异动一般有哪些原因？", None),
        ("怎么估算实验所需样本量？", None),
        ("最小可检测提升是什么？", None),
        ("什么是 MAB 实验？", None),
    ],
    "read_only": [
        ("查一下实验 123 的状态", "readonly_vs_write"),
        ("实验 456 现在跑得怎么样了？", None),
        ("帮我看下审批到哪一步了", "readonly_vs_write"),
        ("实验 789 的核心指标是多少？", None),
        ("看下昨天实验组的转化率", None),
        ("查实验 321 的分流比例", None),
        ("实验 654 的置信区间是多少？", None),
        ("帮我看下最近的实验列表", None),
        ("实验 111 有没有触发告警？", None),
        ("查一下 Review 88 的进度", "readonly_vs_write"),
        ("实验组这周的数据怎么样？", None),
        ("看下实验 222 的显著性能不能通过", None),
        ("帮我确认实验 333 是否已经结束", None),
        ("实验 444 的创建人是谁？", None),
        ("查实验 555 的运行日志", None),
        ("现在有多少个进行中的实验？", None),
        ("看下我负责的实验", None),
        ("实验 666 的观察周期到几号？", None),
        ("帮我读一下实验报告里的结论", None),
        ("实验 777 的指标趋势如何？", None),
    ],
    "write_action": [
        ("帮我创建一个实验", "qa_vs_write"),
        ("创建一个新实验", None),
        ("帮我新建一个实验", None),
        ("把实验 123 暂停掉", None),
        ("启动实验 456", None),
        ("帮我撤回 Review 123", "readonly_vs_write"),
        ("撤销刚才的提交", None),
        ("把实验 789 的分流比例改成 50/50", None),
        ("给实验加一个告警规则", None),
        ("把实验 321 标记为已完成", None),
        ("帮我催一下审批", "readonly_vs_write"),
        ("提交实验 654 的 Review", None),
        ("删除实验 111", None),
        ("恢复实验 222 的运行", None),
        ("把实验 333 的观察期延长一周", None),
        ("帮我把实验组流量调到 80%", None),
        ("给实验 888 添加一个指标", None),
        ("关闭实验 999 的告警", None),
        ("把实验报告发送给团队群", None),
        ("给实验 101 复制一份新版本", None),
    ],
    "unclear": [
        ("帮我处理一下这个实验", "missing_object"),
        ("帮我看看这个", "missing_object"),
        ("把它改一下", "missing_object"),
        ("这个实验有点问题", None),
        ("处理一下告警", "missing_object"),
        ("帮我优化下实验", None),
        ("那个东西怎么不对", None),
        ("帮我弄一下", "missing_object"),
        ("分析实验 123 为什么异常", None),
        ("看看能不能救一下这个实验", None),
        ("帮我跟一下这个事", None),
        ("这个数不对吧", None),
        ("实验好像没跑起来", None),
        ("帮我想想办法", None),
        ("搞一下昨天说的那个", "missing_object"),
        ("这边显示有问题", None),
        ("是不是哪里配置错了", None),
        ("帮我确认下情况", None),
        ("看看咋回事", None),
        ("帮我弄个东西", "missing_object"),
    ],
    "oos": [
        ("帮我预订会议室", "oos_near_domain"),
        ("今天天气怎么样", None),
        ("帮我写一份周报", None),
        ("订一张明天去上海的机票", None),
        ("播放一首歌", None),
        ("帮我订外卖", None),
        ("附近有什么好吃的餐厅", None),
        ("帮我查一下股价", None),
        ("翻译这段英文", None),
        ("写一首诗", None),
        ("明天几点开会", None),
        ("帮我发一封邮件给老板", None),
        ("打开窗户", None),
        ("设置一个明天 7 点的闹钟", None),
        ("报销单怎么填", "oos_near_domain"),
        ("帮我抢一张演唱会门票", None),
        ("推荐一部电影", None),
        ("今天限行尾号是多少", None),
        ("怎么注册新公司", None),
        ("查一下快递单号 12345", None),
    ],
}

# 近义变体（同一 group，验证 group split 不泄漏）
VARIANTS = [
    "{t}",
    "{t}。",
    "{t}，谢谢",
    "麻烦{t_lower}",
]

CONTEXTS = [None, None, None, "你是想查看状态还是撤回 Review？"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(REPO / "examples" / "queries.csv"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    rows = []
    for label, templates in TEMPLATES.items():
        for tpl_idx, (text, risk) in enumerate(templates):
            group_id = f"{label}-tpl-{tpl_idx:02d}"
            for variant in VARIANTS:
                t_lower = text[0].lower() + text[1:] if text[0].isascii() else text
                final_text = variant.format(t=text, t_lower=t_lower)
                context = rng.choice(CONTEXTS)
                hard_negative = risk is not None and rng.random() < 0.7
                rows.append(
                    {
                        "text": final_text,
                        "label": label,
                        "group_id": group_id,
                        "context": context or "",
                        "source": "synthetic",
                        "risk_slice": risk or "",
                        "is_hard_negative": "true" if hard_negative else "false",
                    }
                )

    rng.shuffle(rows)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    label_counts: dict[str, int] = {}
    for row in rows:
        label_counts[row["label"]] = label_counts.get(row["label"], 0) + 1
    print(f"生成 {len(rows)} 条样本 -> {out_path}")
    print("类别分布:", label_counts)


if __name__ == "__main__":
    main()
