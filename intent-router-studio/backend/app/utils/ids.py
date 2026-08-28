"""ULID 风格 ID 生成。

格式：{prefix}_{10位时间戳}{16位随机}，使用 Crockford Base32，
与设计文档中的 prj_01J... / dsv_01J... 风格一致。
时间部分单调递增，便于按创建顺序排序且不暴露连续数量。
"""
from __future__ import annotations

import secrets
import threading
import time

_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_lock = threading.Lock()
_last_timestamp = -1
_last_random = 0


def _encode_time(ts_ms: int) -> str:
    """48 毫秒时间戳编码为 10 个字符。"""
    value = ts_ms & ((1 << 48) - 1)
    chars = []
    for _ in range(10):
        chars.append(_ENCODING[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def _encode_random(random_int: int) -> str:
    """80 位随机数编码为 16 个字符。"""
    value = random_int & ((1 << 80) - 1)
    chars = []
    for _ in range(16):
        chars.append(_ENCODING[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid() -> str:
    """生成 26 位 ULID 字符串（单调，同毫秒内递增随机部分）。"""
    global _last_timestamp, _last_random
    with _lock:
        ts = int(time.time() * 1000)
        if ts == _last_timestamp:
            _last_random += 1
        else:
            _last_timestamp = ts
            _last_random = secrets.randbits(80)
        return _encode_time(ts) + _encode_random(_last_random)


def prefixed(prefix: str) -> str:
    """生成带前缀的实体 ID，例如 prefixed('prj') -> 'prj_01J...'。"""
    return f"{prefix}_{new_ulid()}"


def new_request_id() -> str:
    return prefixed("req")


# 常用前缀
PROJECT = "prj"
LABEL_SCHEMA = "lsv"
UPLOAD = "upl"
DATASET = "dsv"
SPLIT = "spl"
RUN = "run"
MODEL = "mdl"
THRESHOLD = "thv"
EVENT = "evt"
CASE = "pc"
SAMPLE = "sid"
REWRITE_CONFIG = "rwcfg"
TERMINOLOGY = "termv"
REWRITE_FEEDBACK = "rwfb"
PROVIDER_CONNECTION = "rpc"
