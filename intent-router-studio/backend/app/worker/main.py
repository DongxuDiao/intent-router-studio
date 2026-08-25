"""Worker 主进程：轮询领取训练任务（1s 间隔），处理 SIGTERM 优雅停机。"""
from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading

from app.config import get_settings
from app.db import SessionLocal, init_db

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
    stream=sys.stdout,
)
logger = logging.getLogger("worker")

_stop = threading.Event()


def _handle_sigterm(signum, frame):
    logger.info("收到信号 %s，准备在安全阶段停机", signum)
    _stop.set()


def main() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    os.environ["HF_HOME"] = str(settings.hf_home_path)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    init_db()
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    from app.models import TrainingRun
    from app.worker import queue
    from app.worker.run_executor import RunExecutor

    worker_id = f"{socket.gethostname()}:{os.getpid()}"

    db = SessionLocal()
    try:
        recovered = queue.recover_stale_runs(db)
        if recovered:
            logger.info("恢复检查：%d 个遗留运行态任务标记为 INTERRUPTED", recovered)
        queued = db.query(TrainingRun).filter(TrainingRun.status == "QUEUED").count()
        if queued:
            logger.info("待执行任务：%d", queued)
    finally:
        db.close()

    logger.info("Worker 启动完成 worker_id=%s artifact_root=%s", worker_id, settings.artifact_root_path)

    while not _stop.is_set():
        db = SessionLocal()
        try:
            run = queue.claim_next_run(db, worker_id)
        finally:
            db.close()
        if run is None:
            _stop.wait(1.0)
            continue
        logger.info("领取任务 %s (dataset=%s)", run.id, run.dataset_id)
        executor = RunExecutor(worker_id=worker_id, stop_check=_stop.is_set)
        final_status = executor.execute(run.id)
        logger.info("任务 %s 结束: %s", run.id, final_status)

    logger.info("Worker 已停机")


if __name__ == "__main__":
    main()
