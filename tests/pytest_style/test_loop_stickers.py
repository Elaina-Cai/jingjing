# -*- coding: utf-8 -*-
"""她自己的表情：能不能发、什么时候不发（2026-11-01）

口径来自当天定稿：只在情绪位发、群里先不发、同一张 30 分钟内不重发、
池子太小就不在提示词里报（免得她翻来覆去只发同一张）。
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 2026-11-02 搬进 pytest_style/ 后深一层


def _mod():
    import importlib
    import sys

    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    return importlib.import_module("loop.stickers")


def _fake(monkeypatch, keys):
    st = _mod()
    monkeypatch.setattr(st, "available", lambda platform="wechat", force=False: {k: object() for k in keys})
    st._SENT.clear()
    return st


def test_group_is_refused():
    st = _mod()
    ok, why = st.gate("10000000001@chatroom", "嗯", now=1000.0, check_context=False)
    assert not ok and "群" in why          # 群里先不发表情


def test_unknown_key_lists_what_she_has(monkeypatch):
    st = _fake(monkeypatch, ["嗯", "晚安"])
    ok, why = st.gate("wxid_x", "不存在", now=1000.0)
    assert not ok and "嗯" in why and "晚安" in why


def test_private_chat_passes_first_time(monkeypatch):
    st = _fake(monkeypatch, ["嗯"])
    ok, why = st.gate("wxid_x", "嗯", now=1000.0, check_context=False)
    assert ok and why == ""


def test_same_sticker_is_cooled_down_30_minutes(monkeypatch):
    st = _fake(monkeypatch, ["嗯"])
    st.mark_sent("wxid_x", "嗯", now=1000.0)
    ok, why = st.gate("wxid_x", "嗯", now=1000.0 + 60, check_context=False)
    assert not ok and "刚发过" in why
    ok2, _ = st.gate("wxid_x", "嗯", now=1000.0 + 31 * 60, check_context=False)
    assert ok2, "30 分钟以后应该放行"


def test_cooldown_is_per_chat(monkeypatch):
    st = _fake(monkeypatch, ["嗯"])
    st.mark_sent("wxid_a", "嗯", now=1000.0)
    ok, _ = st.gate("wxid_b", "嗯", now=1060.0, check_context=False)
    assert ok, "换个人发同一张不该被自己的冷却挡住"


def test_small_pool_is_not_advertised(monkeypatch):
    st = _fake(monkeypatch, ["嗯"])
    assert st.note() == "", "只有一两张时别在提示词里报，否则她会一直重复同一张"


def test_note_lists_keys_when_pool_is_big_enough(monkeypatch):
    st = _fake(monkeypatch, ["嗯", "晚安", "敬礼", "看戏"])
    line = st.note()
    assert "嗯" in line and "晚安" in line and "代替说话" in line


def test_brain_wires_the_tool_and_the_guards():
    src = open(os.path.join(ROOT, "loop", "brain.py"), encoding="utf-8").read()
    assert '"name": "sticker"' in src, "工具得挂进 schema，不然她根本不知道有这个动作"
    assert 'if name == "sticker":' in src
    assert "world_db.enqueue(" in src, "表情要入队，让 desk 去发（这样对账看得见、失败能重试）"
    assert "_stk.gate(" in src, "入队前必须先过闸"
    assert "我手上有的表情" in src and "_stkmod.note()" in src
    assert '"remember", "sticker"' in src, "试衣间里也不许真发表情"


def test_sender_is_only_touched_through_the_gate():
    """她这条路必须经过 loop/brain.py 的闸；旧的 agent 路径不许偷偷绕过去发表情。"""
    hits = []
    for dirpath, _dirs, files in os.walk(ROOT):
        if any(p in dirpath for p in ("backups", ".git", "__pycache__", ".test_tmp", "tests")):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                txt = open(p, encoding="utf-8").read()
            except Exception:
                continue
            if "wx_sticker_sender" in txt:
                hits.append(os.path.relpath(p, ROOT).replace("\\", "/"))
    assert "loop/desk.py" in hits, "真正点表情面板的只有 loop/desk.py（队列驱动）"
    assert "loop/brain.py" not in hits, "brain 只负责排队，不许自己点微信"
    assert not [h for h in hits if h.startswith("agents/")], "旧 agent 路径不许偷偷发"
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
