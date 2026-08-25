"""任务队列：SQLite 原子条件更新领取任务（设计文档第 11 节）。"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.constants import RunStatus
from app.models import TrainingRun

# 运行态集合：status 随 stage 同步推进（两者共用 RunStatus 枚举值），
# 终态/取消的条件更新以该集合为 from 集，避免阶段推进不齐导致更新落空。
RUNNING_STATUSES = [
    RunStatus.PREPARING,
    RunStatus.TRAINING_EMBEDDING,
    RunStatus.TRAINING_HEAD,
    RunStatus.CALIBRATING,
    RunStatus.SEARCHING_THRESHOLDS,
    RunStatus.EVALUATING,
    RunStatus.PACKAGING,
    RunStatus.CANCELLING,
]


def claim_next_run(db: Session, worker_id: str) -> TrainingRun | None:
    """原子领取最早的 QUEUED 任务；仅领取成功的 Worker 执行训练。"""
    now = datetime.now(UTC).isoformat()
    stmt = text(
        """
        UPDATE training_runs
        SET status = :preparing,
            stage = :preparing,
            progress = 0,
            worker_id = :worker_id,
            started_at = :now,
            heartbeat_at = :now
        WHERE id = (
            SELECT id FROM training_runs
            WHERE status = :queued
            ORDER BY created_at
            LIMIT 1
        )
        AND status = :queued
        RETURNING id
        """
    )
    row = db.execute(
        stmt,
        {"preparing": RunStatus.PREPARING, "queued": RunStatus.QUEUED, "worker_id": worker_id, "now": now},
    ).fetchone()
    db.commit()
    if row is None:
        return None
    return db.get(TrainingRun, row[0])


def is_cancel_requested(db: Session, run_id: str) -> bool:
    run = db.get(TrainingRun, run_id)
    return bool(run and run.cancel_requested)


def update_heartbeat(db: Session, run_id: str) -> None:
    db.execute(
        text("UPDATE training_runs SET heartbeat_at = :now WHERE id = :id"),
        {"now": datetime.now(UTC).isoformat(), "id": run_id},
    )
    db.commit()


def transition_status(
    db: Session,
    run_id: str,
    from_statuses: set[str] | list[str],
    to_status: str,
    **fields,
) -> bool:
    """条件状态迁移：避免并发覆盖（设计文档 10）。

    from_statuses 只接受内部常量，不做用户输入拼接。
    """
    allowed_columns = {"stage", "progress", "error", "finished_at", "artifacts_dir", "worker_id"}
    values = {"status": to_status}
    for key, val in fields.items():
        if key not in allowed_columns:
            raise ValueError(f"非法字段 {key}")
        # raw SQL 绑定不走 ORM 的 JSON 序列化，error 字段需手动转字符串
        if key == "error" and isinstance(val, (dict, list)):
            val = json.dumps(val, ensure_ascii=False)
        values[key] = val
    set_clause = ", ".join(f"{k} = :{k}" for k in values)
    params = dict(values)
    params["id"] = run_id
    # 状态为内部枚举常量，直接内联（非用户输入）
    in_list = ",".join(f"'{s}'" for s in from_statuses)
    result = db.execute(
        text(f"UPDATE training_runs SET {set_clause} WHERE id = :id AND status IN ({in_list})"),
        params,
    )
    db.commit()
    return result.rowcount > 0


def recover_stale_runs(db: Session) -> int:
    """Worker 启动时把遗留的运行态任务标记为 INTERRUPTED（QUEUED 保留）。"""
    in_list = ",".join(f"'{s}'" for s in RUNNING_STATUSES)
    now = datetime.now(UTC).isoformat()
    result = db.execute(
        text(
            f"""
            UPDATE training_runs
            SET status = '{RunStatus.INTERRUPTED}',
                finished_at = :now,
                error = :error
            WHERE status IN ({in_list})
            """
        ),
        {"now": now, "error": json.dumps({"code": "WORKER_RESTART", "message": "Worker 重启，运行中断，可重试"}, ensure_ascii=False)},
    )
    db.commit()
    return result.rowcount
