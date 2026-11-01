"""A10 (§18): record filenames keep CJK in the slug (no more T-000001-.md)."""
from __future__ import annotations

import pytest

from proj.core.templates import slugify


@pytest.mark.parametrize(
    ("title", "want"),
    [
        ("清洗 PCB 数据集", "清洗-pcb-数据集"),            # CJK + ASCII, spaces → '-'
        ("ONNX 导出 + TensorRT 部署 (edge)", "onnx-导出-tensorrt-部署-edge"),
        ("数据 增强", "数据-增强"),                          # CJK kept
        ("缺陷检测 v1 上线", "缺陷检测-v1-上线"),
        ("Hello World", "hello-world"),                    # ASCII lowercased
        ("a b  c", "a-b-c"),                              # runs collapse to one '-'
        ("数据" * 60, "数据" * 25),                         # 50-char cap (by char)
        ("-leading", "leading"),                          # no leading '-'
        ("trailing-", "trailing"),                        # no trailing '-'
    ],
)
def test_slugify(title, want):
    assert slugify(title) == want


def test_slugify_symbols_only_falls_back_to_id():
    # a title with no letters/digits → "" (the caller uses the id alone,
    # e.g. T-000001.md) — never a dangling trailing '-'
    assert slugify("####") == ""
    assert slugify("（）·——") == ""
    assert slugify("") == ""
