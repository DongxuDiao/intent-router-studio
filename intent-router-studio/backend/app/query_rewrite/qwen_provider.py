"""本地 Qwen 生成式改写 Provider（修改方案 §5 / V2 §3.3）。

- Qwen3-0.6B（Apache 2.0），Transformers 加载，运行在独立 rewriter 服务进程
- 确定性结构任务：greedy（do_sample=False）、repetition_penalty=1.05、enable_thinking=False
- 输出必须经 parse_provider_output 严格校验（§5.4：不能直接信任原始输出）

V2 §3.3 可终止执行：
- 模型由常驻"生成 worker 子进程"持有，父进程永不 import torch；
- 超时直接 terminate 子进程（SIGTERM → SIGKILL 兜底），真正释放 CPU，
  替换传统"daemon 线程 + join(timeout) 放弃结果"方案（残余线程会一直占 CPU）；
- 有界准入（默认 2）：队列已满立即拒绝 ProviderBusy → 上层 429 REWRITER_BUSY
  → 主 API 回退原文，绝不排队堆积；
- 指标：queue_capacity / queue_depth / active_generation /
  busy_reject_total / generation_timeout_total / worker_restarts。
"""
from __future__ import annotations

import multiprocessing as mp
import os
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import wait as _wait_conn
from typing import Any

from app.query_rewrite.prompt import PROMPT_VERSION, build_messages
from app.query_rewrite.provider import ProviderBusy, ProviderReply, ProviderTimeout, ProviderUnavailable
from app.query_rewrite.schemas import ProviderOutput, RewriteParseError, parse_provider_output

DEFAULT_MODEL_ID = "Qwen/Qwen3-0.6B"
DEFAULT_MAX_NEW_TOKENS = 96
# 子进程模型加载上限（CPU 冷启动实测 ~60s，留余量）
WORKER_LOAD_TIMEOUT_S = float(os.environ.get("REWRITE_WORKER_LOAD_TIMEOUT", "240"))


def _has_complete_json(text: str) -> bool:
    """只在首个 JSON 对象完整闭合时停止，忽略字符串内的大括号。"""
    start = text.find("{")
    if start < 0:
        return False
    depth = 0
    in_string = False
    escaped = False
    for char in text[start:]:
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return True
    return False


def _generation_worker_main(  # 按子进程入口签名传递
    conn, model_id: str, device: str, max_new_tokens: int, repetition_penalty: float, threads: int
) -> None:
    """生成 worker 子进程入口（spawn 上下文，必须是模块级可 pickle 函数）。

    协议：("ready", device) / ("load_error", msg) / 请求 ("gen", messages, max_new_tokens)
    → ("ok", text) | ("error", msg)；父进程可随时 terminate，无清理副作用。
    """
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from transformers import StoppingCriteria, StoppingCriteriaList

        # 混合大小核 CPU（如 Apple P/E core）上，线程数超过性能核数会
        # 显著拖慢生成（实测 8 线程 ~0.6 tok/s → 4 线程 ~2.5 tok/s）
        if threads > 0:
            torch.set_num_threads(threads)
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        model = AutoModelForCausalLM.from_pretrained(model_id, dtype="auto")
        model.eval()
        if device != "auto":
            model.to(device)
        elif getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
            model.to("mps")
        conn.send(("ready", str(getattr(model, "device", "unknown"))))
    except Exception as exc:  # 加载失败上报后退出，父进程按不可用处理
        try:
            conn.send(("load_error", f"{type(exc).__name__}: {exc}"))
        except Exception:  # 管道可能已断
            pass
        conn.close()
        return

    while True:
        try:
            request = conn.recv()
        except (EOFError, OSError):
            return  # 父进程退出/断开
        if not request or request[0] == "shutdown":
            return
        _, messages, gen_max_new_tokens = request
        try:
            with torch.inference_mode():
                prompt = tokenizer.apply_chat_template(
                    messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
                )
                inputs = tokenizer(prompt, return_tensors="pt")

                class _CompleteJsonCriteria(StoppingCriteria):
                    def __init__(self, prompt_tokens: int) -> None:
                        self.prompt_tokens = prompt_tokens

                    def __call__(self, input_ids, scores, **kwargs) -> bool:
                        generated = tokenizer.decode(
                            input_ids[0][self.prompt_tokens:],
                            skip_special_tokens=True,
                            clean_up_tokenization_spaces=False,
                        )
                        return _has_complete_json(generated)

                out = model.generate(
                    **inputs,
                    max_new_tokens=gen_max_new_tokens,
                    do_sample=False,
                    repetition_penalty=repetition_penalty,
                    pad_token_id=tokenizer.eos_token_id,
                    # 只观察本次新增 token；完整 JSON 闭合后立即停止。
                    stopping_criteria=StoppingCriteriaList(
                        [_CompleteJsonCriteria(inputs["input_ids"].shape[1])]
                    ),
                )
            text = tokenizer.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
            conn.send(("ok", text))
        except Exception as exc:  # 单条失败不退出 worker
            conn.send(("error", f"{type(exc).__name__}: {exc}"))


class _GenerationWorker:
    """持有生成子进程的句柄：提交请求、按调用方超时判定、可强制终止。"""

    def __init__(
        self,
        *,
        model_id: str,
        device: str,
        max_new_tokens: int,
        repetition_penalty: float,
        threads: int,
        load_timeout_s: float = WORKER_LOAD_TIMEOUT_S,
        entry: Callable[..., None] = _generation_worker_main,
    ) -> None:
        ctx = mp.get_context("spawn")
        self._parent_conn, child_conn = ctx.Pipe(duplex=True)
        self._proc = ctx.Process(
            target=entry,
            args=(child_conn, model_id, device, max_new_tokens, repetition_penalty, threads),
            daemon=True,
        )
        self._state_lock = threading.Lock()
        self._draining = False
        self._proc.start()
        try:
            if not _wait_conn([self._parent_conn], load_timeout_s):
                self.terminate()
                raise ProviderUnavailable(f"生成进程加载超时（>{load_timeout_s:.0f}s）")
            kind, payload = self._parent_conn.recv()
            if kind == "load_error":
                self.terminate()
                raise ProviderUnavailable(f"生成进程加载失败: {payload}")
            self.device = payload
        except (EOFError, OSError) as exc:
            self.terminate()
            raise ProviderUnavailable(f"生成进程启动失败: {exc}") from exc

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.is_alive() and not self._parent_conn.closed

    @property
    def available(self) -> bool:
        with self._state_lock:
            return self.alive and not self._draining

    def _drain_late_result(self) -> None:
        """排空调用方软超时后的迟到响应，保留已加载的模型进程。"""
        hard_timeout_s = float(os.environ.get("REWRITE_WORKER_HARD_TIMEOUT", "300"))
        try:
            if not _wait_conn([self._parent_conn], hard_timeout_s):
                self.terminate()
                return
            self._parent_conn.recv()
        except (EOFError, OSError):
            pass
        finally:
            with self._state_lock:
                self._draining = False

    def generate(self, messages: list[dict[str, str]], timeout_ms: int, max_new_tokens: int) -> str:
        """提交请求；调用方软超时后后台排空结果，不销毁已加载模型。"""
        with self._state_lock:
            if self._draining:
                raise ProviderBusy("上一条生成仍在后台完成，请回退原文")
        try:
            self._parent_conn.send(("gen", messages, max_new_tokens))
        except (BrokenPipeError, OSError) as exc:
            raise ProviderUnavailable(f"生成进程已断开: {exc}") from exc
        if not _wait_conn([self._parent_conn], timeout_ms / 1000.0):
            with self._state_lock:
                self._draining = True
            threading.Thread(target=self._drain_late_result, daemon=True).start()
            raise ProviderTimeout(f"生成超过 {timeout_ms}ms，模型继续后台完成并复用")
        try:
            kind, payload = self._parent_conn.recv()
        except (EOFError, OSError) as exc:
            raise ProviderUnavailable(f"生成进程崩溃: {exc}") from exc
        if kind == "error":
            raise RuntimeError(payload)
        return payload

    def terminate(self) -> None:
        proc = getattr(self, "_proc", None)
        conn = getattr(self, "_parent_conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # 关闭已断开的管道是常态
                pass
        if proc is not None and proc.is_alive():
            proc.terminate()  # SIGTERM：默认处置直接终止，无视 C 层生成循环
            proc.join(5)
            if proc.is_alive():
                proc.kill()
                proc.join(2)


class QwenProvider:
    provider_name = "local_qwen"

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        device: str = "auto",
        max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
        repetition_penalty: float = 1.05,
        queue_capacity: int | None = None,
        worker_entry: Callable[..., None] = _generation_worker_main,
    ) -> None:
        self.model_id = model_id
        self.device = device
        self.max_new_tokens = max_new_tokens
        self.repetition_penalty = repetition_penalty
        # 有界准入（V2 §3.3）：在途 + 排队调用者总数上限，满了立即拒绝
        self.queue_capacity = (
            int(os.environ.get("REWRITE_QUEUE_CAPACITY", "2")) if queue_capacity is None else queue_capacity
        )
        if self.queue_capacity < 1:
            raise ValueError("queue_capacity 必须 ≥ 1")
        self._slots = threading.BoundedSemaphore(self.queue_capacity)
        self._gate = threading.Lock()  # 同一时刻只有一个线程占用生成进程管道
        self._worker_entry = worker_entry
        self._worker: _GenerationWorker | None = None
        self._worker_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._admitted = 0
        self._active = 0
        self._busy_rejects = 0
        self._gen_timeouts = 0
        self._restarts = 0
        self._load_error: str | None = None
        self.last_error: str | None = None

    # ---- worker 生命周期 ----
    def _get_worker(self) -> _GenerationWorker:
        """获取存活 worker；不存在/已被超时终止则重新拉起（模型重载代价由下一请求承担）。"""
        with self._worker_lock:
            if self._worker is not None and self._worker.alive:
                return self._worker
            if self._worker is not None:
                self._worker.terminate()  # 清理半死句柄
                self._worker = None
                self._restarts += 1
            try:
                worker = _GenerationWorker(
                    model_id=self.model_id,
                    device=self.device,
                    max_new_tokens=self.max_new_tokens,
                    repetition_penalty=self.repetition_penalty,
                    threads=int(os.environ.get("REWRITE_TORCH_THREADS", "0") or 0),
                    entry=self._worker_entry,
                )
            except ProviderUnavailable as exc:
                self._load_error = str(exc)
                self.last_error = str(exc)
                raise
            self._worker = worker
            self._load_error = None
            return worker

    def warmup(self) -> None:
        """启动时预加载 + 一次空生成，避免首个请求承担冷启动。"""
        worker = self._get_worker()
        worker.generate([{"role": "user", "content": "ping"}], 60_000, 8)

    def health(self) -> dict[str, Any]:
        worker = self._worker
        process_alive = worker is not None and worker.alive
        loaded = worker is not None and worker.available
        info: dict[str, Any] = {
            "ok": loaded,
            "provider": self.provider_name,
            "model_id": self.model_id,
            "loaded": loaded,
            "process_alive": process_alive,
            "device": str(getattr(worker, "device", "not-loaded")),
            "max_new_tokens": self.max_new_tokens,
            "prompt_version": PROMPT_VERSION,
            "metrics": self.metrics(),
        }
        if self.last_error:
            info["last_error"] = self.last_error
        return info

    def metrics(self) -> dict[str, Any]:
        """V2 §3.3 观测指标：队列深度 / 在途生成 / 拒绝与超时计数 / worker 重启次数。"""
        with self._metrics_lock:
            admitted, active = self._admitted, self._active
            return {
                "queue_capacity": self.queue_capacity,
                "queue_depth": max(0, admitted - active),
                "active_generation": active,
                "busy_reject_total": self._busy_rejects,
                "generation_timeout_total": self._gen_timeouts,
                "worker_restarts": self._restarts,
            }

    # ---- 生成 ----
    def _generate(self, messages: list[dict[str, str]], timeout_ms: int) -> str:
        if not self._slots.acquire(blocking=False):
            with self._metrics_lock:
                self._busy_rejects += 1
            raise ProviderBusy(f"生成队列已满（capacity={self.queue_capacity}），请回退原文")
        with self._metrics_lock:
            self._admitted += 1
        try:
            with self._gate:  # 排队中的调用者在此等待；各自超时从获得管道起算
                worker = self._get_worker()
                with self._metrics_lock:
                    self._active = 1
                try:
                    return worker.generate(messages, timeout_ms, self.max_new_tokens)
                except ProviderBusy:
                    with self._metrics_lock:
                        self._busy_rejects += 1
                    raise
                except ProviderTimeout:
                    with self._metrics_lock:
                        self._gen_timeouts += 1
                    raise
                finally:
                    with self._metrics_lock:
                        self._active = 0
        finally:
            with self._metrics_lock:
                self._admitted -= 1
            self._slots.release()

    def rewrite(
        self,
        original_query: str,
        context: str | None,
        terminology: dict[str, str] | None = None,
        timeout_ms: int = 5000,
    ) -> ProviderReply:
        start = time.perf_counter()
        messages = build_messages(original_query, context, terminology)
        raw = self._generate(messages, timeout_ms)
        try:
            output: ProviderOutput = parse_provider_output(raw)
        except RewriteParseError:
            self.last_error = "INVALID_JSON"
            raise
        return ProviderReply(
            output=output,
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
            provider=self.provider_name,
            model_id=self.model_id,
            prompt_version=PROMPT_VERSION,
        )
