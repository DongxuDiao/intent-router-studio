"""数据切分（设计文档 5.5）：group 分组 + 标签分布 + 泄漏检查。"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import pandas as pd

from app.errors import ApiError

SPLIT_ALGORITHM_VERSION = "group_stratified_v2"
DEFAULT_RATIOS = {"train": 0.70, "validation": 0.15, "test": 0.15}
MIN_GROUPS_PER_LABEL = 3  # train / validation / test 各至少一个语义组

# risk_test 关注的最小差异 / 否定 / OOS 近域 / 多轮改口切片
RISK_SLICES = {
    "qa_vs_write",
    "readonly_vs_write",
    "negation",
    "missing_object",
    "oos_near_domain",
    "typo_colloquial",
    "multi_turn_correction",
    "long_context",
}


@dataclass
class SplitResult:
    df: pd.DataFrame                     # 增加 split 与 is_risk_test 列
    stats: dict = field(default_factory=dict)
    warnings: list[dict] = field(default_factory=list)

    def leakage_report(self) -> list[dict]:
        return [w for w in self.warnings if w["code"] == "SPLIT_LEAKAGE"]


def _group_key(row: pd.Series, group_col: str | None, hash_col: str | None) -> str:
    if group_col and isinstance(row.get(group_col), str) and row[group_col].strip():
        return str(row[group_col]).strip()
    if hash_col and isinstance(row.get(hash_col), str) and row[hash_col].strip():
        return f"hash:{row[hash_col]}"
    return "ungrouped"


def assign_label_groups(
    groups: list[tuple[str, int]],
    ratios: dict[str, float],
    rng: random.Random,
) -> dict[str, str]:
    """单标签的组 → split 分配（纯函数，便于单测）。

    策略（设计修改方案 3.2A）：
    1. 先为 test 选一个最接近 test 配额的组，再为 validation 选一个；
       两步都保证剩余组数足够留给其它 split（含 train 至少一组）。
    2. 剩余组按「配额缺口」贪心分配，向目标比例收敛。
    3. 同一 group 只进入一个 split（由逐组分配天然保证）。

    要求 len(groups) >= 3，由调用方（group_split）负责结构化阻断。
    """
    pool = list(groups)
    rng.shuffle(pool)
    total = sum(count for _, count in pool)
    assignment: dict[str, str] = {}
    filled = {"train": 0, "validation": 0, "test": 0}

    def take_closest(target: float, reserve: int) -> tuple[str, int]:
        """选行数最接近 target 的组；reserve 为其它 split（含 train）至少要保留的组数。"""
        if len(pool) <= reserve:
            return pool[0]
        ordered = sorted(pool, key=lambda gc: (abs(gc[1] - target), gc[1]))
        return ordered[0]

    test_group = take_closest(total * ratios.get("test", 0.15), reserve=2)
    assignment[test_group[0]] = "test"
    filled["test"] = test_group[1]
    pool.remove(test_group)

    val_group = take_closest(total * ratios.get("validation", 0.15), reserve=1)
    assignment[val_group[0]] = "validation"
    filled["validation"] = val_group[1]
    pool.remove(val_group)

    quotas = {split: total * ratios.get(split, 0.0) for split in ("train", "validation", "test")}
    for key, count in pool:
        # 缺口最大的 split 优先；并列时 train > validation > test（确定性）
        deficits = {s: quotas[s] - filled[s] for s in ("train", "validation", "test")}
        best = max(("train", "validation", "test"), key=lambda s: (deficits[s], s == "train", s == "validation"))
        assignment[key] = best
        filled[best] += count
    return assignment


def validate_split(df: pd.DataFrame, label_col: str = "label") -> list[dict]:
    """切分后置校验（设计修改方案 3.2B）：返回结构化问题列表，空列表表示通过。

    检查项：
    - train / validation / test 均非空（EMPTY_SPLIT）
    - 每条样本恰好属于一个 split（SAMPLE_UNASSIGNED）
    - 同一 group_id 不跨 split（GROUP_LEAKAGE）
    - 数据中出现过的每个标签在 train/validation/test 中均有样本（SPLIT_LABEL_MISSING）
    - 每个标签在 train 中至少有一个语义组（TRAIN_GROUP_MISSING）
    - is_risk_test 只能标记 test 中的样本（RISK_FLAG_OUT_OF_TEST）
    """
    problems: list[dict] = []
    if "split" not in df.columns:
        return [{"code": "EMPTY_SPLIT", "message": "缺少 split 列", "details": {}}]

    valid_splits = {"train", "validation", "test"}
    for split in ("train", "validation", "test"):
        n = int((df["split"] == split).sum())
        if n == 0:
            problems.append({"code": "EMPTY_SPLIT", "message": f"{split} 没有样本", "details": {"split": split}})

    bad = ~df["split"].isin(valid_splits) | df["split"].isna()
    if bad.any():
        problems.append(
            {
                "code": "SAMPLE_UNASSIGNED",
                "message": f"{int(bad.sum())} 条样本未分配到合法 split",
                "details": {"count": int(bad.sum())},
            }
        )

    effective_groups = _effective_group_values(df)
    if effective_groups is not None:
        grouped = df.assign(_effective_group=effective_groups).groupby("_effective_group")["split"].nunique()
        leaked = grouped[grouped > 1]
        if len(leaked):
            problems.append(
                {
                    "code": "GROUP_LEAKAGE",
                    "message": f"{len(leaked)} 个语义组跨越多个 split",
                    "details": {"groups": [str(g) for g in leaked.index[:20]]},
                }
            )

    if label_col in df.columns:
        present_labels = set(df[label_col].dropna().astype(str)) - {""}
        for split in ("train", "validation", "test"):
            sub_labels = set(df.loc[df["split"] == split, label_col].dropna().astype(str)) - {""}
            missing = sorted(present_labels - sub_labels)
            if missing:
                problems.append(
                    {
                        "code": "SPLIT_LABEL_MISSING",
                        "message": f"{split} 缺少类别 {missing}",
                        "details": {"split": split, "missing_labels": missing},
                    }
                )
        if effective_groups is not None:
            train_rows = df[df["split"] == "train"]
            train_groups = effective_groups.loc[train_rows.index]
            for lab in sorted(present_labels):
                has_group = (
                    train_groups.loc[train_rows[label_col].astype(str) == lab]
                    .loc[lambda s: s != "ungrouped"]
                    .nunique()
                )
                if has_group == 0:
                    problems.append(
                        {
                            "code": "TRAIN_GROUP_MISSING",
                            "message": f"类别 {lab} 在 train 中没有任何语义组",
                            "details": {"label": lab},
                        }
                    )

    if "is_risk_test" in df.columns:
        bad_flag = (df["is_risk_test"] == True) & (df["split"] != "test")  # noqa: E712
        if bad_flag.any():
            problems.append(
                {
                    "code": "RISK_FLAG_OUT_OF_TEST",
                    "message": f"{int(bad_flag.sum())} 条非 test 样本被标记 is_risk_test",
                    "details": {"count": int(bad_flag.sum())},
                }
            )
    return problems


def _effective_group_values(df: pd.DataFrame) -> pd.Series | None:
    """返回与 group_split 一致的分组键：group_id 优先，normalized_hash 兜底。"""
    if "group_id" not in df.columns and "normalized_hash" not in df.columns:
        return None
    if "group_id" in df.columns:
        groups = df["group_id"].fillna("").astype(str).str.strip()
    else:
        groups = pd.Series("", index=df.index, dtype="string")
    if "normalized_hash" in df.columns:
        hashes = df["normalized_hash"].fillna("").astype(str).str.strip()
        fallback = hashes.map(lambda value: f"hash:{value}" if value else "ungrouped")
        groups = groups.where(groups.ne(""), fallback)
    return groups


def ensure_splits_trainable(df: pd.DataFrame, label_col: str = "label") -> None:
    """Worker 训练前的防御性校验：空 split / 泄漏等问题在加载模型前暴露。"""
    problems = validate_split(df, label_col)
    if problems:
        first = problems[0]
        code = first["code"] if first["code"] in ("EMPTY_SPLIT", "GROUP_LEAKAGE") else "INVALID_SPLIT"
        raise ApiError(code, f"数据切分不可训练: {first['message']}", 422, {"problems": problems[:10]})


def group_split(
    df: pd.DataFrame,
    label_col: str = "label",
    group_col: str | None = "group_id",
    hash_col: str | None = "normalized_hash",
    ratios: dict | None = None,
    seed: int = 42,
) -> SplitResult:
    """按 group 分组切分：同一语义模板组只出现在一个 split，组级维持标签分布。

    已提供 split 列的数据保留原切分（仍执行泄漏检查）——
    调用方通过 respect_existing=True 使用该行为。
    """
    ratios = ratios or DEFAULT_RATIOS
    rng = random.Random(seed)
    df = df.copy()

    if "split" in df.columns and df["split"].notna().any():
        result = SplitResult(df=df)
        result.warnings.extend(_leakage_warnings(df, hash_col))
        result.stats = _split_stats(df, label_col)
        return result

    # 组信息：group -> (dominant label, count)
    groups: dict[str, dict] = {}
    for idx, row in df.iterrows():
        key = _group_key(row, group_col, hash_col)
        label = row.get(label_col)
        label = label if isinstance(label, str) and label else "__unlabeled__"
        g = groups.setdefault(key, {"labels": {}, "rows": []})
        g["labels"][label] = g["labels"].get(label, 0) + 1
        g["rows"].append(idx)

    # 按主导标签聚合组
    by_label: dict[str, list[tuple[str, int]]] = {}
    for key, g in groups.items():
        dominant = max(g["labels"].items(), key=lambda kv: kv[1])[0]
        total = sum(g["labels"].values())
        by_label.setdefault(dominant, []).append((key, total))

    assignment: dict[str, str] = {}
    for label, group_list in by_label.items():
        if len(group_list) < MIN_GROUPS_PER_LABEL:
            raise ApiError(
                "INSUFFICIENT_SPLIT_GROUPS",
                f"类别 {label} 的语义组不足，无法同时生成 train/validation/test",
                422,
                {
                    "label": label,
                    "group_count": len(group_list),
                    "required_group_count": MIN_GROUPS_PER_LABEL,
                },
            )
        assignment.update(assign_label_groups(group_list, ratios, rng))

    df["split"] = [_assignment_for_row(row, assignment, group_col, hash_col) for _, row in df.iterrows()]
    _annotate_risk_test(df)

    # 后置校验（3.2B）：任何结构性问题在切分产出前即失败
    problems = validate_split(df, label_col)
    if problems:
        raise ApiError(
            "INVALID_SPLIT",
            "切分结果未通过后置校验",
            422,
            {"problems": problems[:10]},
        )

    result = SplitResult(df=df)
    result.warnings.extend(_leakage_warnings(df, hash_col))
    result.stats = _split_stats(df, label_col)
    return result


def _assignment_for_row(row, assignment, group_col, hash_col) -> str:
    key = _group_key(row, group_col, hash_col)
    return assignment.get(key, "train")


def _annotate_risk_test(df: pd.DataFrame) -> None:
    """risk_test：test split 中带风险标记的样本（不参与调参）。"""
    def _flag(row) -> bool:
        if row.get("split") != "test":
            return False
        rs = row.get("risk_slice")
        if isinstance(rs, str) and rs.strip():
            return True
        hn = row.get("is_hard_negative")
        return bool(hn) if hn is not None else False

    df["is_risk_test"] = df.apply(_flag, axis=1)


def _leakage_warnings(df: pd.DataFrame, hash_col: str | None) -> list[dict]:
    if not hash_col or hash_col not in df.columns:
        return []
    warnings = []
    splits = [s for s in ("train", "validation", "test") if s in set(df.get("split", []))]
    sets = {
        s: set(df.loc[df["split"] == s, hash_col].dropna()) for s in splits
    }
    for i, a in enumerate(splits):
        for b in splits[i + 1 :]:
            overlap = sets[a] & sets[b]
            if overlap:
                warnings.append(
                    {
                        "code": "SPLIT_LEAKAGE",
                        "message": f"{a} 与 {b} 存在 {len(overlap)} 条规范化文本重叠",
                        "details": {"samples": sorted(overlap)[:20], "count": len(overlap)},
                    }
                )
    return warnings


def _split_stats(df: pd.DataFrame, label_col: str) -> dict:
    stats: dict = {"rows": {}}
    for split in ("train", "validation", "test"):
        sub = df[df["split"] == split]
        stats["rows"][split] = int(len(sub))
        dist = sub[label_col].value_counts().to_dict() if label_col in sub.columns else {}
        stats[f"{split}_label_distribution"] = {str(k): int(v) for k, v in dist.items()}
    if "is_risk_test" in df.columns:
        stats["risk_test_rows"] = int(df["is_risk_test"].sum())
    return stats


def check_group_leakage(df: pd.DataFrame, group_col: str = "group_id") -> list[dict]:
    """同一 group 出现在多个 split 的泄漏检查（近义模板被随机分开）。"""
    problems = []
    if group_col not in df.columns or "split" not in df.columns:
        return problems
    g = df.dropna(subset=[group_col]).groupby(group_col)["split"].nunique()
    leaked = g[g > 1]
    for name in leaked.index[:50]:
        problems.append({"group_id": str(name), "splits": sorted(df[df[group_col] == name]["split"].unique().tolist())})
    return problems
