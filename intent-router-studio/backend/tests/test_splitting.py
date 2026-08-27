"""切分与泄漏检查测试。"""
from __future__ import annotations

import pandas as pd
import pytest

from app.errors import ApiError
from app.router_core.normalization import normalized_hash
from app.router_core.splitting import check_group_leakage, group_split, validate_split
from app.router_core.taxonomy import LABELS


def _df(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "sample_id": f"s{i}",
                "text": text,
                "label": label,
                "group_id": group,
                "context": None,
                "source": "test",
                "is_hard_negative": False,
                "risk_slice": None,
                "metadata_json": None,
                "normalized_hash": normalized_hash(text),
            }
            for i, (text, label, group) in enumerate(rows)
        ]
    )


def test_group_split_no_group_leakage():
    rows = []
    for lab in ("information", "read_only", "write_action", "unclear", "oos"):
        for g in range(6):
            for v in range(4):
                rows.append((f"{lab} 模板{g} 变体{v}", lab, f"{lab}-g{g}"))
    df = _df(rows)
    result = group_split(df, seed=42)
    assert check_group_leakage(result.df) == []
    stats = result.stats
    assert stats["rows"]["train"] + stats["rows"]["validation"] + stats["rows"]["test"] == len(df)
    # 比例接近 70/15/15（组粒度有噪声）
    assert stats["rows"]["train"] > stats["rows"]["test"] > 0
    assert stats["rows"]["validation"] > 0


def test_near_duplicates_stay_together():
    """近义样本（帮我创建/新建一个实验）必须落在同一 split（同 group）。"""
    rows = [
        ("帮我创建一个实验", "write_action", "create-exp"),
        ("帮我新建一个实验", "write_action", "create-exp"),
        ("创建一个新实验", "write_action", "create-exp"),
    ] + [
        (f"写入补充 {i}", "write_action", f"write-filler-{i}") for i in range(4)
    ] + [
        (f"查询实验 {i} 的状态", "read_only", f"query-{i}") for i in range(20)
    ] + [
        (f"模板 {lab} {i}", lab, f"{lab}-g{i}") for lab in ("information", "unclear", "oos") for i in range(8)
    ]
    df = _df(rows)
    result = group_split(df, seed=1)
    create_rows = result.df[result.df["group_id"] == "create-exp"]
    assert create_rows["split"].nunique() == 1


def test_split_leakage_by_hash_detected():
    df = _df([("查询实验", "read_only", "a"), ("查询实验", "read_only", "b")] * 1)
    df.loc[0, "split"] = "train"
    df.loc[1, "split"] = "test"
    result = group_split(df, seed=1)
    codes = [w["code"] for w in result.warnings]
    assert "SPLIT_LEAKAGE" in codes


def test_existing_split_preserved():
    rows = [(f"样本 {i}", "information" if i % 2 else "read_only", f"g{i}") for i in range(30)]
    df = _df(rows)
    df["split"] = ["train"] * 15 + ["validation"] * 8 + ["test"] * 7
    result = group_split(df, seed=9)
    assert result.df["split"].value_counts().to_dict() == {"train": 15, "validation": 8, "test": 7}


def test_risk_test_annotation():
    """风险标记只在样本进入 test 时生效为 is_risk_test。"""
    rows = [
        ("只看状态不要改", "read_only", "ro-risk"),
        ("帮我改一下配置", "write_action", "wa-risk"),
    ] + [
        (f"read 填充 {g} {i}", "read_only", f"ro-g{g}") for g in range(3) for i in range(5)
    ] + [
        (f"write 填充 {g} {i}", "write_action", f"wa-g{g}") for g in range(3) for i in range(5)
    ] + [
        (f"填充 {lab} {g} {i}", lab, f"{lab}-g{g}") for lab in ("information", "unclear", "oos") for g in range(3) for i in range(5)
    ]
    df = _df(rows)
    df.loc[df["group_id"] == "ro-risk", "risk_slice"] = "negation"
    df.loc[df["group_id"] == "wa-risk", "is_hard_negative"] = True
    result = group_split(df, seed=3)
    assert "is_risk_test" in result.df.columns
    flagged = result.df[result.df["is_risk_test"]]
    assert (flagged["split"] == "test").all()
    # 被标记的组：只有进入 test 的那部分样本才带 is_risk_test
    marked_groups = result.df[result.df["group_id"].isin(["ro-risk", "wa-risk"])]
    expected = marked_groups[marked_groups["split"] == "test"].index.sort_values()
    assert flagged.index.sort_values().equals(expected)
    assert len(flagged) >= 1  # 单行风险组最接近 test 配额，至少一个进入 test


# ---------------- 小样本切分保障 ----------------

def _rows_per_label(group_sizes: dict[str, list[int]]) -> list[tuple[str, str, str]]:
    """group_sizes: label -> 每组行数列表。"""
    rows = []
    for lab, sizes in group_sizes.items():
        for g, size in enumerate(sizes):
            for v in range(size):
                rows.append((f"{lab} 模板{g} 变体{v}", lab, f"{lab}-g{g}"))
    return rows


def test_three_groups_per_label_covers_all_splits():
    """每类恰好 3 个等大组：train/validation/test 均非空且覆盖五类。"""
    df = _df(_rows_per_label({lab: [6, 6, 6] for lab in LABELS}))
    result = group_split(df, seed=42)
    for split in ("train", "validation", "test"):
        assert result.stats["rows"][split] > 0, f"{split} 为空"
    for split in ("validation", "test"):
        covered = set(result.stats[f"{split}_label_distribution"])
        assert covered == set(LABELS), f"{split} 缺少类别: {set(LABELS) - covered}"
    assert check_group_leakage(result.df) == []


def test_three_unequal_groups_still_all_splits():
    """每类 3 个大小悬殊的组（如 30/5/3）：仍保证三个 split 均非空。"""
    df = _df(_rows_per_label({lab: [30, 5, 3] for lab in LABELS}))
    result = group_split(df, seed=7)
    for split in ("train", "validation", "test"):
        assert result.stats["rows"][split] > 0, f"{split} 为空"
    for split in ("validation", "test"):
        assert set(result.stats[f"{split}_label_distribution"]) == set(LABELS)


def test_two_groups_blocks_with_structured_error():
    """某类别只有 2 个组：返回结构化阻断错误而非全部进 train。"""
    sizes = {lab: [6, 6, 6] for lab in LABELS}
    sizes["write_action"] = [10, 4]
    df = _df(_rows_per_label(sizes))
    with pytest.raises(ApiError) as exc:
        group_split(df, seed=42)
    assert exc.value.code == "INSUFFICIENT_SPLIT_GROUPS"
    assert exc.value.details["label"] == "write_action"
    assert exc.value.details["group_count"] == 2
    assert exc.value.details["required_group_count"] == 3


def test_multirow_group_never_crosses_split():
    """同组多条样本整组进入同一 split（含恰好 3 组的小数据）。"""
    df = _df(_rows_per_label({lab: [4, 4, 4] for lab in LABELS}))
    result = group_split(df, seed=11)
    assert result.df.groupby("group_id")["split"].nunique().max() == 1


def test_seed_reproducible():
    df = _df(_rows_per_label({lab: [3, 5, 7, 2] for lab in LABELS}))
    a = group_split(df, seed=99).df["split"].tolist()
    b = group_split(df, seed=99).df["split"].tolist()
    assert a == b


def test_validate_split_passes_on_good_split():
    df = _df(_rows_per_label({lab: [6, 6, 6] for lab in LABELS}))
    result = group_split(df, seed=42)
    assert validate_split(result.df) == []


def test_split_with_missing_group_id_uses_normalized_hash_fallback():
    """group_id 是可选列时，切分后置校验必须与切分算法使用同一哈希兜底。"""
    df = _df(_rows_per_label({lab: [6, 6, 6] for lab in LABELS}))
    df["group_id"] = None
    result = group_split(df, seed=42)
    assert validate_split(result.df) == []


def test_validate_split_detects_problems():
    df = _df(_rows_per_label({lab: [6, 6, 6] for lab in LABELS}))
    result = group_split(df, seed=42)

    # validation 被清空 → 报错
    broken = result.df.copy()
    broken.loc[broken["split"] == "validation", "split"] = "train"
    problems = validate_split(broken)
    assert any(p["code"] == "EMPTY_SPLIT" for p in problems)

    # group 跨 split → 报错（只移动组内一行，制造泄漏）
    leaked = result.df.copy()
    first_test_group = leaked[leaked["split"] == "test"]["group_id"].iloc[0]
    row_idx = leaked.index[(leaked["group_id"] == first_test_group) & (leaked["split"] == "test")][0]
    leaked.loc[row_idx, "split"] = "train"
    problems = validate_split(leaked)
    assert any(p["code"] == "GROUP_LEAKAGE" for p in problems)

    # 整组移出 test → 该类别在 test 中缺失 → 标签覆盖错误
    moved = result.df.copy()
    moved.loc[moved["group_id"] == first_test_group, "split"] = "train"
    problems = validate_split(moved)
    assert any(p["code"] == "SPLIT_LABEL_MISSING" for p in problems)

    # train 中样本被标 is_risk_test → 报错
    flagged = result.df.copy()
    flagged.loc[flagged["split"] == "train", "is_risk_test"] = True
    problems = validate_split(flagged)
    assert any(p["code"] == "RISK_FLAG_OUT_OF_TEST" for p in problems)

    # 每条样本恰好属于一个 split：制造 NaN → 报错
    holey = result.df.copy()
    holey.loc[holey.index[0], "split"] = None
    problems = validate_split(holey)
    assert any(p["code"] == "SAMPLE_UNASSIGNED" for p in problems)


def test_ensure_splits_trainable_blocks_before_training():
    """Worker 训练前防御：空 split 在加载模型前即失败。"""
    from app.router_core.splitting import ensure_splits_trainable

    df = _df(_rows_per_label({lab: [6, 6, 6] for lab in LABELS}))
    result = group_split(df, seed=42)
    ensure_splits_trainable(result.df)  # 合法切分不抛错

    broken = result.df.copy()
    broken.loc[broken["split"] == "test", "split"] = "train"
    with pytest.raises(ApiError) as exc:
        ensure_splits_trainable(broken)
    assert exc.value.code == "EMPTY_SPLIT"
