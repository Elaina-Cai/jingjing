# -*- coding: utf-8 -*-
"""表情全流程的四处闭环（2026-11-01）：排队、情绪位/占比、标注三轴、落库成员。

这些测试只钉"结构"和"判据"，不去点微信：真发送在 loop/desk.py 里，靠 outbound_queue 驱动。
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in __import__("sys").path:
    __import__("sys").path.insert(0, ROOT)


def _src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- 1. 表情走队列
def test_sticker_goes_through_outbound_queue():
    s = _src("loop/brain.py")
    exec_i = s.find("async def _exec")
    i = s.find('if name == "sticker":', exec_i)
    assert i > 0, "brain 里应有 sticker 分支"
    blk = s[i:i + 4000]
    assert "world_db.enqueue(" in blk, "表情必须入队，否则送达对账看不见、失败不重试"
    assert '"kind": "sticker"' in blk
    assert "send_sticker" not in blk, "brain 不该自己点微信（那会让排队/重试失效）"


def test_desk_delivers_sticker_kind():
    s = _src("loop/desk.py")
    assert 'if kind == "sticker":' in s
    i = s.find('if kind == "sticker":')
    blk = s[i:i + 1600]
    assert "send_sticker" in blk, "desk 负责真正点表情面板"
    assert "mark_sent" in blk, "真发出去了才开始算冷却"


def test_audit_counts_stickers_as_says():
    s = _src("collector_ext.py")
    assert 'if _act not in ("say", "sticker")' in s, "对账要看得见表情"
    assert '"[表情] "' in s or "'[表情] '" in s


# ---------------------------------------------------------------- 2. 情绪位 / 占比
def test_gate_refuses_when_being_asked_a_question():
    from loop import stickers as S
    assert S.is_emotional("你明天几点上课？")[0] is False
    assert S.is_emotional("帮我看看这个怎么弄")[0] is False
    assert S.is_emotional("哈哈哈笑死")[0] is True
    assert S.is_emotional("", "image")[0] is True


def test_ratio_cap_exists_and_is_twentyfive():
    from loop import stickers as S
    assert 0.15 <= S.RATIO_MAX <= 0.30
    ok, why = S.ratio_state()
    assert isinstance(ok, bool)


def test_gate_order_covers_group_cooldown_context_ratio():
    s = _src("loop/stickers.py")
    i = s.find("def gate(")
    blk = s[i:i + 2600]
    for must in ("群里先不发表情", "COOLDOWN_S", "is_emotional", "ratio_state"):
        assert must in blk, "gate 缺一环：%s" % must


# ---------------------------------------------------------------- 3. 标注三轴
def test_register_validates_controlled_vocab():
    from console import executor as E
    assert len(E.FEEL_WORDS) == 20
    r = E.sticker_register("a" * 32, "测试图意", feel="瞎编的词")
    assert r["ok"] is False and "20 个词" in r["error"]
    r2 = E.sticker_register("a" * 32, "测试图意", scope="全宇宙")
    assert r2["ok"] is False


def test_pool_exposes_feel_words_for_the_dialog():
    from console import executor as E
    src = _src("console/executor.py")
    assert '"feels": list(FEEL_WORDS)' in src
    assert "members" in src and "_members_of" in src


# ---------------------------------------------------------------- 4. 落库 / 整簇
def test_catalog_members_fallback_and_schema():
    from db import sticker_catalog as SC
    assert SC.members_of("f" * 32) == ["f" * 32], "查不到就只返回它自己，不能抛"
    src = _src("db/sticker_catalog.py")
    for must in ("create table if not exists asset", "create table if not exists cluster",
                 "members", "px text primary key"):
        assert must in src
def main() -> bool:
    """2026-11-02 补：这个文件以前只有 def test_、没有运行入口（一直空转，一条都没跑过）。"""
    names = sorted(n for n in globals() if n.startswith("test_"))
    fails = []
    for n in names:
        fn = globals()[n]
        if not callable(fn):
            continue
        try:
            fn()
            print("  ok   %s" % n)
        except Exception as exc:  # noqa: BLE001 - 汇总用，单个用例炸了要继续跑完
            fails.append(n)
            print("  FAIL %s  %s" % (n, repr(exc)[:200]))
    print("RESULT %d failed / %d" % (len(fails), len(names)))
    return not fails


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
