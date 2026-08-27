import signal

from app.worker.supervisor import classify_worker_exit


def test_classify_sigkill_as_oom():
    code, message = classify_worker_exit(-signal.SIGKILL)
    assert code == "WORKER_OOM"
    assert "exit 137" in message


def test_classify_regular_crash_separately():
    code, message = classify_worker_exit(1)
    assert code == "WORKER_CRASH"
    assert "exit 1" in message
