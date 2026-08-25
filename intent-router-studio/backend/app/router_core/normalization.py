"""文本规范化与多轮输入编码（设计文档 5.2）。"""
from __future__ import annotations

import hashlib
import re
import unicodedata

NORMALIZATION_VERSION = "norm-v1"

_WHITESPACE_RE = re.compile(r"\s+")
# 全角字符（U+FF01–U+FF5E）→ 半角
_FULLWIDTH_OFFSET = 0xFEE0


def normalize_text(text: str) -> str:
    """规范化 Query：

    - Unicode NFC 归一
    - 全角字母数字标点转半角
    - 连续空白折叠为单个空格
    - 去除首尾空白
    - 拉丁字母小写化（中文不受影响）
    """
    if text is None:
        return ""
    text = unicodedata.normalize("NFC", str(text))
    text = "".join(
        chr(ord(ch) - _FULLWIDTH_OFFSET) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in text
    )
    text = _WHITESPACE_RE.sub(" ", text)
    text = text.strip()
    # 只对 ASCII 拉丁字母做小写化
    text = "".join(ch.lower() if "A" <= ch <= "Z" else ch for ch in text)
    return text


def normalized_hash(text: str, context: str | None = None) -> str:
    """规范化文本（含上下文）的 sha256，用于去重与泄漏检查。"""
    norm_text = normalize_text(text)
    norm_context = normalize_text(context) if context else ""
    payload = f"{NORMALIZATION_VERSION}\x00{norm_text}\x00{norm_context}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def encode_input(text: str, context: str | None = None) -> str:
    """多轮输入统一编码：默认最多保留最近一轮必要上下文。

    [CONTEXT]
    助手上一轮：{context}
    [USER]
    {text}
    """
    norm_text = normalize_text(text)
    if context:
        norm_context = normalize_text(context)
        return f"[CONTEXT]\n助手上一轮：{norm_context}\n[USER]\n{norm_text}"
    return norm_text


def text_hash(text: str, context: str | None = None) -> str:
    """隐私友好哈希：日志与 Playground 历史默认只保存该值。"""
    payload = f"{normalize_text(text)}\x00{normalize_text(context) if context else ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
