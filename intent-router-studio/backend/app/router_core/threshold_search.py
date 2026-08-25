"""风险约束阈值搜索（设计文档 6.6）与路由级指标（7.2）。

在满足 false_write_rate / write_precision 约束的候选中最大化 safe_coverage，
并列时选择 Macro F1 更高、阈值更保守（更大）的组合。
输出完整 Pareto 数据与四条阈值曲线供前端可视化。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.router_core.policy import Thresholds
from app.router_core.taxonomy import LABELS

DEFAULT_SEARCH_SPEC = {
    "default_range": [0.50, 0.95, 0.01],
    "write_range": [0.70, 0.99, 0.01],
    "oos_range": [0.50, 0.95, 0.01],
    "margin_range": [0.00, 0.30, 0.01],
    "constraints": {
        "max_false_write_rate": 0.005,
        "min_write_precision": 0.95,
    },
    "objective": "maximize_safe_coverage",
}


def _grid(spec_range: list[float]) -> np.ndarray:
    start, stop, step = spec_range
    n = int(round((stop - start) / step)) + 1
    return np.round(start + step * np.arange(n), 6)


def _top1_stats(probs: np.ndarray):
    """返回 top1 概率、top2 概率、margin。"""
    part = -np.partition(-probs, 1, axis=1)
    p1 = part[:, 0]
    p2 = part[:, 1]
    return p1, p2, p1 - p2


def route_metrics(
    probs: np.ndarray,
    y: np.ndarray,
    thresholds: Thresholds,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """给定（校准后）概率与阈值，计算路由级指标（设计文档 7.2）。

    - accepted：决策门输出 accept
    - coverage = accepted / total
    - selective_accuracy = accepted 且正确 / accepted
    - false_write_rate = 接受为写但真值非写 / 真值非写总数
    - clarification_rate = unclear 决策数 / total
    """
    labels = labels or LABELS
    probs = np.asarray(probs, dtype=np.float64)
    y = np.asarray(y)
    n = len(y)
    top1_idx = probs.argmax(axis=1)
    p1, _p2, margin = _top1_stats(probs)

    label_pos = {lab: i for i, lab in enumerate(labels)}
    w = label_pos["write_action"]
    write_thr = np.full(n, thresholds.default_min_confidence)
    write_thr[top1_idx == w] = thresholds.write_min_confidence
    write_thr[top1_idx == label_pos["oos"]] = thresholds.oos_min_confidence

    accepted = (p1 >= write_thr) & (margin >= thresholds.min_margin)
    final_routes = np.where(accepted, np.array(labels, dtype=object)[top1_idx], "unclear")
    correct = top1_idx == y

    accepted_count = int(accepted.sum())
    accepted_correct = int((accepted & correct).sum())
    non_write = y != w
    n_non_write = int(non_write.sum())
    accepted_write = accepted & (top1_idx == w)
    accepted_write_correct = int((accepted_write & correct).sum())
    accepted_write_on_non_write = int((accepted_write & non_write).sum())
    n_true_write = int((y == w).sum())

    route_counts: dict[str, int] = {lab: 0 for lab in labels}
    route_counts["unclear"] = 0
    for r in final_routes:
        route_counts[r] += 1

    return {
        "n": n,
        "accepted_count": accepted_count,
        "coverage": round(accepted_count / n, 6) if n else None,
        "safe_coverage": round(accepted_correct / n, 6) if n else None,
        "selective_accuracy": round(accepted_correct / accepted_count, 6) if accepted_count else None,
        "false_write_rate": round(accepted_write_on_non_write / n_non_write, 6) if n_non_write else 0.0,
        "false_write_count": accepted_write_on_non_write,
        "write_precision": round(accepted_write_correct / int(accepted_write.sum()), 6)
        if accepted_write.sum() > 0
        else None,
        "write_recall": round(accepted_write_correct / n_true_write, 6) if n_true_write else None,
        "unclear_rate": round((n - accepted_count) / n, 6) if n else None,
        "route_counts": route_counts,
    }


def _macro_f1(final_routes: np.ndarray, y: np.ndarray, labels: list[str]) -> float:
    """最终路由的 Macro F1，用于并列时择优。

    五类各计一次（unclear 本身是标签之一，不再作为附加类重复加权）；
    y 为真值标签索引，拒识样本路由为 unclear，与真值 unclear 同样按类统计。
    """
    f1s = []
    for i, cls in enumerate(labels):
        pred = final_routes == cls
        true = y == i
        tp = int((pred & true).sum())
        fp = int((pred & ~true).sum())
        fn = int((~pred & true).sum())
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return float(np.mean(f1s))


@dataclass
class ThresholdSearchResult:
    best: Thresholds
    best_metrics: dict
    feasible: bool
    n: int
    n_candidates: int
    n_feasible: int
    n_retained_candidates: int = 0
    n_tied: int = 0
    selection: dict = field(default_factory=dict)
    curves: dict[str, list[dict]] = field(default_factory=dict)
    pareto: list[dict] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "best": self.best.to_dict(),
            "best_metrics": self.best_metrics,
            "feasible": self.feasible,
            "n": self.n,
            "n_candidates": self.n_candidates,
            "n_feasible": self.n_feasible,
            "n_retained_candidates": self.n_retained_candidates,
            "n_tied": self.n_tied,
            "selection": self.selection,
            "curves": self.curves,
            "pareto": self.pareto,
            "note": self.note,
        }


def search_thresholds(
    probs: np.ndarray,
    y: np.ndarray,
    label_list: list[str] | None = None,
    spec: dict | None = None,
) -> ThresholdSearchResult:
    """在 validation 概率上做约束网格搜索。

    向量化实现：按 top1 标签分为 write / oos / 其它三组，
    组内按 p1 排序后用前缀和 + searchsorted 将任意阈值的统计降为 O(1)。
    """
    label_list = label_list or LABELS
    spec = {**DEFAULT_SEARCH_SPEC, **(spec or {})}
    constraints = {**DEFAULT_SEARCH_SPEC["constraints"], **(spec.get("constraints") or {})}

    probs = np.asarray(probs, dtype=np.float64)
    y = np.asarray(y)
    n = len(y)
    if n == 0:
        return ThresholdSearchResult(
            best=Thresholds(),
            best_metrics={},
            feasible=False,
            n=0,
            n_candidates=0,
            n_feasible=0,
            note="validation 集为空",
        )

    pos = {lab: i for i, lab in enumerate(label_list)}
    w_idx, oos_idx = pos["write_action"], pos["oos"]
    top1_idx = probs.argmax(axis=1)
    p1, _p2, margin = _top1_stats(probs)
    correct = top1_idx == y
    non_write = y != w_idx
    n_non_write = int(non_write.sum())

    d_grid, w_grid, o_grid = _grid(spec["default_range"]), _grid(spec["write_range"]), _grid(spec["oos_range"])
    m_grid = _grid(spec["margin_range"])

    def group_tables(m: float):
        """按 top1 标签分组后，对每组阈值向量给出 O(1) 级统计表。

        组内按 p1 升序排序，用前缀和 + searchsorted 得到
        「p1 >= 阈值」子集上的 accepted / accepted_correct / false_write 计数。
        """
        active = margin >= m
        tables = {}
        for name, thr in (("default", d_grid), ("write", w_grid), ("oos", o_grid)):
            if name == "write":
                members = active & (top1_idx == w_idx)
            elif name == "oos":
                members = active & (top1_idx == oos_idx)
            else:
                members = active & (top1_idx != w_idx) & (top1_idx != oos_idx)
            ps = p1[members]
            order = np.argsort(ps)
            ps_sorted = ps[order]
            corr_sorted = correct[members][order].astype(np.float64)
            fw_sorted = ((top1_idx[members] == w_idx) & non_write[members])[order].astype(np.float64)

            cut = np.searchsorted(ps_sorted, thr, side="left")
            # 前缀和长度 = len+1（prefix[k] = 前 k 个元素之和），cut ∈ [0, len] 均合法
            corr_prefix = np.concatenate([[0.0], np.cumsum(corr_sorted)])
            fw_prefix = np.concatenate([[0.0], np.cumsum(fw_sorted)])

            acc = (len(ps_sorted) - cut).astype(np.float64)
            acc_correct = corr_prefix[-1] - corr_prefix[cut] if len(ps_sorted) else np.zeros_like(thr)
            acc_fw = fw_prefix[-1] - fw_prefix[cut] if len(ps_sorted) else np.zeros_like(thr)

            if name == "write":
                group_fw, group_wc, group_w = acc_fw, acc_correct, acc
            else:
                zero = np.zeros_like(thr, dtype=np.float64)
                group_fw, group_wc, group_w = zero, zero, zero
            tables[name] = {
                "acc": acc,
                "acc_correct": acc_correct,
                "acc_false_write": group_fw,
                "acc_write_correct": group_wc,
                "acc_write": group_w,
                # V2 §4.2：暴露切点与成员行号，供并列候选按"接受集合"去重后精确择优
                "cut": cut,
                "positions": np.where(members)[0],
                "order": order,
            }
        return tables

    def _grids_3d(t: dict):
        """由分组表重建 3D 的接受数/正确数/约束指标。"""
        acc = t["default"]["acc"][:, None, None] + t["write"]["acc"][None, :, None] + t["oos"]["acc"][None, None, :]
        acc_correct = (
            t["default"]["acc_correct"][:, None, None]
            + t["write"]["acc_correct"][None, :, None]
            + t["oos"]["acc_correct"][None, None, :]
        )
        fw = t["write"]["acc_false_write"][None, :, None] * np.ones_like(acc)
        aw = t["write"]["acc"][None, :, None] * np.ones_like(acc)
        awc = t["write"]["acc_write_correct"][None, :, None] * np.ones_like(acc)

        fwr = np.divide(fw, n_non_write, out=np.zeros_like(fw), where=n_non_write > 0)
        wp = np.divide(awc, aw, out=np.ones_like(aw), where=aw > 0)

        feasible = (fwr <= constraints["max_false_write_rate"] + 1e-12) & (
            (wp >= constraints["min_write_precision"] - 1e-12) | (aw == 0)
        )
        return acc, acc_correct.astype(np.int64), feasible

    # V2 §4.2：最优解选择不再截断候选。第一遍扫描用整数计数（acc_correct 本就是
    # 整数）确定全局最优 safe_coverage，第二遍只在达到该值的 margin 档上做
    # 精确并列枚举；Pareto 可视化采样不影响选择。
    best_coverage = -1  # 全局最优 safe_coverage 的分子（正确接受总数）
    tables_by_m: dict[float, dict] = {}
    local_max_by_m: dict[float, int] = {}
    viz_candidates: list[dict] = []  # 仅用于 Pareto 可视化采样
    n_feasible_total = 0  # 全网格真实可行组合数（不受保留上限影响）

    for m in m_grid:
        m_val = float(m)
        t = group_tables(m_val)
        tables_by_m[m_val] = t
        _acc, acc_correct_int, feasible = _grids_3d(t)
        n_feasible_total += int(feasible.sum())
        masked = np.where(feasible, acc_correct_int, -1)
        local_max = int(masked.max()) if masked.size and masked.max() >= 0 else -1
        local_max_by_m[m_val] = local_max
        best_coverage = max(best_coverage, local_max)

        if local_max >= 0:  # 可视化采样：每档 margin 最多保留 200 个并列点
            di, wi, oi = np.where(feasible & (acc_correct_int == local_max))
            for d_i, w_i, o_i in list(zip(di.tolist(), wi.tolist(), oi.tolist(), strict=True))[:200]:
                viz_candidates.append(
                    {
                        "thresholds": (float(d_grid[d_i]), float(w_grid[w_i]), float(o_grid[o_i]), m_val),
                        "safe_coverage": local_max / n,
                    }
                )

    if best_coverage < 0:
        return ThresholdSearchResult(
            best=Thresholds(),
            best_metrics=route_metrics(probs, y, Thresholds(), label_list),
            feasible=False,
            n=n,
            n_candidates=int(len(d_grid) * len(w_grid) * len(o_grid) * len(m_grid)),
            n_feasible=n_feasible_total,
            n_retained_candidates=len(viz_candidates),
            note="无满足约束的阈值组合，回退冷启动默认阈值（需检查数据或放宽约束）",
        )

    # ---- 精确并列择优：safe_coverage → macro_f1 → 保守性 → 字典序 ----
    # 同一 margin 下，接受集合只由各组切点 (cut_d, cut_w, cut_o) 决定：切点相同的
    # 候选宏 F1 必然相同，先按切点去重再算 F1，避免大并列时的组合爆炸。
    labels_arr = np.array(label_list, dtype=object)[top1_idx]

    def _f1_from_cuts(t: dict, cut_d: int, cut_w: int, cut_o: int) -> float:
        accepted = np.zeros(n, dtype=bool)
        for name, cut in (("default", cut_d), ("write", cut_w), ("oos", cut_o)):
            entry = t[name]
            if len(entry["order"]):
                accepted[entry["positions"][entry["order"][cut:]]] = True
        final = np.where(accepted, labels_arr, "unclear")
        return _macro_f1(final, y, label_list)

    key_state: dict[tuple, tuple] = {}  # (cut_d, cut_w, cut_o, m) -> (保守性, 阈值元组)
    n_tied = 0
    for m in m_grid:
        m_val = float(m)
        if local_max_by_m[m_val] != best_coverage:
            continue
        t = tables_by_m[m_val]
        _acc, acc_correct_int, feasible = _grids_3d(t)
        di, wi, oi = np.where(feasible & (acc_correct_int == best_coverage))
        n_tied += int(len(di))
        cut_d_arr, cut_w_arr, cut_o_arr = t["default"]["cut"], t["write"]["cut"], t["oos"]["cut"]
        for d_i, w_i, o_i in zip(di.tolist(), wi.tolist(), oi.tolist(), strict=True):
            key = (int(cut_d_arr[d_i]), int(cut_w_arr[w_i]), int(cut_o_arr[o_i]), m_val)
            cand = (float(d_grid[d_i]), float(w_grid[w_i]), float(o_grid[o_i]), m_val)
            score = (cand[0] + cand[1] + cand[2] + cand[3], cand)
            prev = key_state.get(key)
            if prev is None or score > prev:
                key_state[key] = score

    scored = []
    for (cut_d, cut_w, cut_o, m_val), (conservatism, cand) in key_state.items():
        f1 = _f1_from_cuts(tables_by_m[m_val], cut_d, cut_w, cut_o)
        scored.append((f1, conservatism, cand))
    scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
    chosen_f1, chosen_conservatism, chosen = scored[0]

    best_thr = Thresholds(
        default_min_confidence=chosen[0],
        write_min_confidence=chosen[1],
        oos_min_confidence=chosen[2],
        min_margin=chosen[3],
    )
    best_metrics = route_metrics(probs, y, best_thr, label_list)
    selection = {
        "n_tied": n_tied,
        "n_unique_route_patterns": len(scored),
        "criterion": "safe_coverage → macro_f1 → 保守性（阈值和最大）→ 字典序",
        "chosen_macro_f1": round(chosen_f1, 6),
        "chosen_conservatism": round(chosen_conservatism, 6),
    }

    # 四条阈值曲线：固定其余为最优，扫描单一参数
    curves: dict[str, list[dict]] = {}
    axis_defs = {
        "default": ("default_min_confidence", d_grid),
        "write": ("write_min_confidence", w_grid),
        "oos": ("oos_min_confidence", o_grid),
        "margin": ("min_margin", m_grid),
    }
    for axis_name, (attr, grid) in axis_defs.items():
        points = []
        for v in grid:
            kw = {attr: float(v)}
            thr = Thresholds(
                default_min_confidence=kw.get("default_min_confidence", best_thr.default_min_confidence),
                write_min_confidence=kw.get("write_min_confidence", best_thr.write_min_confidence),
                oos_min_confidence=kw.get("oos_min_confidence", best_thr.oos_min_confidence),
                min_margin=kw.get("min_margin", best_thr.min_margin),
            )
            mt = route_metrics(probs, y, thr, label_list)
            points.append(
                {
                    "value": float(v),
                    "coverage": mt["coverage"],
                    "safe_coverage": mt["safe_coverage"],
                    "false_write_rate": mt["false_write_rate"],
                    "unclear_rate": mt["unclear_rate"],
                    "write_precision": mt["write_precision"],
                }
            )
        curves[axis_name] = points

    # Pareto：safe_coverage vs false_write_rate 前沿（可视化采样，不影响最优解）
    pareto: list[dict] = []
    sampled = viz_candidates[:: max(1, len(viz_candidates) // 500)]
    for f in sampled[:500]:
        c = f["thresholds"]
        thr = Thresholds(*c)
        mt = route_metrics(probs, y, thr, label_list)
        pareto.append(
            {
                "thresholds": thr.to_dict(),
                "safe_coverage": mt["safe_coverage"],
                "false_write_rate": mt["false_write_rate"],
                "selective_accuracy": mt["selective_accuracy"],
                "unclear_rate": mt["unclear_rate"],
            }
        )

    return ThresholdSearchResult(
        best=best_thr,
        best_metrics=best_metrics,
        feasible=True,
        n=n,
        n_candidates=int(len(d_grid) * len(w_grid) * len(o_grid) * len(m_grid)),
        n_feasible=n_feasible_total,
        n_retained_candidates=len(viz_candidates),
        n_tied=n_tied,
        selection=selection,
        curves=curves,
        pareto=pareto,
    )
