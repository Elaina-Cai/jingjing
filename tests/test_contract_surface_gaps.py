# -*- coding: utf-8 -*-
"""把 7 项「文档要求、代码无落点」逐条接到真位置，并钉住。

来源：``python -X utf8 -u scripts/check_contract_surface.py`` 报的 7 项
（``speech_excerpt`` / ``window_title`` / ``ui_blocked`` / ``http_5xx`` /
``too_large`` / ``unsupported_type`` / ``content_sha256``）。

这份测试的作用不是「证明代码对」，而是**证明这 7 个词在代码里有一个能被调用、
能被断言的落点**——否则文档那句话谁也拦不住它被忘掉。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}")
        _failures.append(name)


def main() -> bool:
    from core.terminal.event_identity import content_sha256, normalize_body
    from core.tools.registry import (
        STATUS_CODES,
        ToolSpec,
        can_ui_control,
        is_known_status,
    )
    from core.terminal.turn_trace import FULL_TRACE_FIELDS, normalize_full_detail

    # —— 感知层 §10-4：content_sha256 是字段，不进幂等键 ——
    a = content_sha256("今天 好热\r\n\r\n")
    b = content_sha256("今天 好热")
    check("content_sha256 忽略换行与行尾空白", a == b, f"{a[:8]} != {b[:8]}")
    check("content_sha256 是 64 位十六进制", len(a) == 64 and all(c in "0123456789abcdef" for c in a))
    check("content_sha256 空正文给空串（没有内容可关联）", content_sha256("   ") == "")
    check("content_sha256 不改语义：全角半角不归一", content_sha256("A") != content_sha256("Ａ"))
    check("normalize_body 只管空白", normalize_body(" x \t\r\n") == "x")

    # —— 工具包 §3：失败码是一个封闭集合 ——
    for code in ("http_5xx", "too_large", "unsupported_type", "no_result", "timeout"):
        check(f"失败码 {code} 已登记", is_known_status(code))
    check("未知失败码不算已知", not is_known_status("weird_code"))
    check("ok 与 no_result 必须分开", {"ok", "no_result"} <= set(STATUS_CODES))

    # —— 工具包 §4.1：ui_control 三条同时成立才算前台可见 ——
    spec = ToolSpec(name="click_ok", family="act", effects="ui_control", window_title="微信")
    ok, reason = can_ui_control(spec, foreground_ok=True, title_ok=True, pixels_changed=120)
    check("三条全过 → 可动手", ok and reason == "")
    ok, reason = can_ui_control(spec, foreground_ok=True, title_ok=False, pixels_changed=120)
    check("标题不匹配 → ui_blocked", (not ok) and reason == "ui_blocked")
    ok, reason = can_ui_control(spec, foreground_ok=True, title_ok=True, pixels_changed=99)
    check("像素变化 <100 → ui_blocked", (not ok) and reason == "ui_blocked")
    ok, reason = can_ui_control(spec, foreground_ok=False, title_ok=True, pixels_changed=120)
    check("进程不在前台 → ui_blocked", (not ok) and reason == "ui_blocked")
    check("没声明 window_title 的注册项不许 ui_control",
          not can_ui_control(ToolSpec(name="x", family="act", effects="ui_control"),
                             foreground_ok=True, title_ok=True, pixels_changed=999)[0])
    reader = ToolSpec(name="reader", family="observe", effects="read")
    check("read 类工具不需要前台",
          can_ui_control(reader, foreground_ok=False, title_ok=False, pixels_changed=0)[0])
    check("ui_blocked 是登记过的失败码", is_known_status("ui_blocked"))

    # —— 契约 §8.1：完整留痕字段必须齐，摘录必须标「以账本为准」 ——
    detail = normalize_full_detail(
        reading="P5在问我项目进度",
        trimmed=["我觉得她一定很失望（无依据）"],
        tools=[{"name": "search_memory", "q": "项目", "purpose": "看上次说过什么"}],
        memory=[{"kind": "fact", "key": "项目", "basis": "w#7"}],
        speech_ref="1284",
        speech_excerpt="做完了",
    )
    check("完整留痕字段全覆盖", set(detail) >= set(FULL_TRACE_FIELDS))
    check("speech_ref 指向账本行号", detail["speech_ref"] == "1284")
    check("speech_excerpt 标了以账本为准", "以账本为准" in detail["speech_excerpt"])
    long_one = normalize_full_detail(speech_excerpt="字" * 500)["speech_excerpt"]
    check("speech_excerpt 截到 200 字以内", len(long_one) <= 220, f"len={len(long_one)}")
    check("缺字段时补空形状而不是消失",
          all(k in normalize_full_detail() for k in FULL_TRACE_FIELDS))

    # —— 感知层 §10-4：哈希落进账本行，但不污染事实对象 ——
    from types import SimpleNamespace

    from core.terminal.event_identity import content_sha256_of
    from core.terminal.event_store_db import _metadata_from_row, _metadata_with_content_hash

    fake = SimpleNamespace(content="同一句话", metadata={"k": "v"})
    stamped = _metadata_with_content_hash(fake)
    check("账本行 metadata 带 content_sha256", "content_sha256" in stamped)
    check("归档哈希 = 正文哈希", stamped["content_sha256"] == content_sha256_of(fake))
    check("原有 metadata 不被冲掉", stamped.get("k") == "v")
    check("空正文不写空哈希", "content_sha256" not in _metadata_with_content_hash(
        SimpleNamespace(content="   ", metadata={})))
    row = SimpleNamespace(metadata_json='{"k": "v", "content_sha256": "deadbeef"}')
    check("读回时剥掉派生哈希（事实对象不被污染）",
          _metadata_from_row(row) == {"k": "v"})

    print(f"RESULT {len(_failures)} failed")
    return not _failures


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
