"""规范化与 ID 生成测试。"""
from __future__ import annotations

from app.router_core.normalization import encode_input, normalize_text, normalized_hash, text_hash
from app.utils import ids


def test_normalize_fullwidth_and_whitespace():
    assert normalize_text("  创建　实验  ") == "创建 实验"
    assert normalize_text("ＡＢＣ ｘｙｚ") == "abc xyz"
    assert normalize_text("帮我\n创建\t一个\r\n实验") == "帮我 创建 一个 实验"


def test_normalize_chinese_untouched():
    assert normalize_text("Ｌｉｂｒａ怎么创建实验？") == "libra怎么创建实验?"


def test_normalized_hash_stable_and_case_insensitive():
    a = normalized_hash("创建实验", None)
    b = normalized_hash("创建实验", "")
    c = normalized_hash("创建实验", "上一轮")
    assert a == b
    assert a != c
    assert len(a) == 64


def test_encode_input_multiturn():
    encoded = encode_input("只看状态", "你是想查看状态还是撤回 Review？")
    assert encoded.startswith("[CONTEXT]")
    assert "助手上一轮：" in encoded
    assert "[USER]" in encoded
    assert "只看状态" in encoded
    assert encode_input("你好", None) == "你好"


def test_ulid_monotonic_and_prefixed():
    a, b = ids.new_ulid(), ids.new_ulid()
    assert b > a  # 字典序即时间序
    pid = ids.prefixed("prj")
    assert pid.startswith("prj_") and len(pid) == len("prj_") + 26


def test_text_hash():
    assert text_hash("查询实验") == text_hash(" 查询实验 ")
    assert len(text_hash("查询实验")) == 64
