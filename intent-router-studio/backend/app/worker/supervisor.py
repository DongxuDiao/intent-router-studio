"""训练 worker 子进程监督器：把 SIGKILL/137 从普通重启中区分出来。"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.worker import queue

logger = logging.getLogger("worker.supervisor")
_stop = threading.Event()


def classify_worker_exit(returncode: int) -> tuple[str, str]:
    """返回持久化到 Run.error 的错误码与用户可读说明。"""
    if returncode in (-signal.SIGKILL, 137):
        return "WORKER_OOM", "Worker 被 SIGKILL（exit 137）终止，通常表示内存不足；请降低训练资源参数后重试"
    if returncode == 0:
        return "WORKER_EXITED", "Worker 子进程意外退出，运行中断，可重试"
    return "WORKER_CRASH", f"Worker 子进程异常退出（exit {returncode}），运行中断，可重试"


def _handle_signal(signum, _frame):
    logger.info("监督器收到信号 %s，准备停止子进程", signum)
    _stop.set()


def _recover(code: str, message: str) -> int:
    db = SessionLocal()
    try:
        return queue.recover_stale_runs(db, error_code=code, error_message=message)
    finally:
        db.close()


def main() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    init_db()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # 容器重启时先处理上一个容器留下的运行态任务；子进程重启则由
    # supervisor 根据真实 returncode 分类，不重复覆盖 WORKER_OOM。
    recovered = _recover("WORKER_RESTART", "Worker 容器重启，运行中断，可重试")
    if recovered:
        logger.warning("容器启动恢复：%d 个遗留运行态任务标记为 INTERRUPTED", recovered)

    child_env = os.environ.copy()
    child_env["WORKER_SKIP_RECOVERY"] = "1"
    while not _stop.is_set():
        child = subprocess.Popen([sys.executable, "-m", "app.worker.main"], env=child_env)
        logger.info("Worker 子进程启动 pid=%s", child.pid)
        while child.poll() is None and not _stop.wait(1.0):
            pass
        if _stop.is_set():
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            break

        returncode = child.returncode
        code, message = classify_worker_exit(returncode)
        recovered = _recover(code, message)
        logger.error("Worker 子进程退出 returncode=%s，%d 个运行态任务标记为 %s", returncode, recovered, code)
        time.sleep(1.0)

    logger.info("Worker 监督器已停机")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
        stream=sys.stdout,
    )
    main()
