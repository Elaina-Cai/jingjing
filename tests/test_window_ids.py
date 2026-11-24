"""`loop/run.py::_window_ids`：这一窗到底是哪个平台、哪个会话。

2026-10-31 事故：群聊窗口的 platform/chat_id 只从快照 cells 里查，
cells 一空就都成了 ("", "")，于是群聊永远套用私聊那套渠道约定
（群里不 @ 对方、连发、还发语音）。这里锁住兜底判据。
"""
from __future__ import annotations

import inspect

import sys as _sys
from pathlib import Path as _Path

_ROOT = str(_Path(__file__).resolve().parents[1])
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

from core.policy import channel  # noqa: E402
from loop import run as R  # noqa: E402


def test_wechat_group_suffix():
    assert R._window_ids("10000000002@chatroom", []) == ("wechat", "10000000002@chatroom")


def test_qq_group_prefix():
    assert R._window_ids("group:1124196924", []) == ("qq", "group:1124196924")


def test_wechat_private():
    assert R._window_ids("wxid_seed01", []) == ("wechat", "wxid_seed01")
    assert R._window_ids("filehelper", []) == ("wechat", "filehelper")


def test_qq_private_is_digits():
    assert R._window_ids("1768136519", []) == ("qq", "1768136519")


def test_unknown_name_stays_empty():
    assert R._window_ids("幸福一家人", []) == ("", "")


def test_cells_win_over_guess():
    cells = [{"chat_id": "10000000002@chatroom", "platform": "wechat", "label": "幸福一家人"}]
    assert R._window_ids("10000000002@chatroom", cells) == ("wechat", "10000000002@chatroom")
    # 用 label 也能对上
    assert R._window_ids("幸福一家人", cells) == ("wechat", "10000000002@chatroom")


def test_group_window_gets_group_rules_end_to_end():
    plat, cid = R._window_ids("10000000001@chatroom", [])
    r = channel.rules(plat, cid)
    text = channel.render(r)
    assert r.mention_required is True
    assert r.allow_voice is False
    assert r.delay_ms == 1500
    assert "群里的公开场合要先 @ 对方再说话" in text
    assert "不发语音，只发字" in text


def test_one_tick_uses_window_ids():
    src = inspect.getsource(R.one_tick)
    assert "_window_ids(_w, snap.get(" in src
    assert "chat_id=_cid" in src


def test_cells_with_empty_platform_falls_back_to_shape():
    cells = [{"chat_id": "10000000001@chatroom", "platform": "", "label": "示例作业群"}]
    assert R._window_ids("10000000001@chatroom", cells) == ("wechat", "10000000001@chatroom")
    assert R._window_ids("示例作业群", cells) == ("wechat", "10000000001@chatroom")


def test_unknown_platform_still_gets_hard_group_rules():
    # cells 没写 platform 时（平台判不出来），群的硬规矩也要生效，
    # 但 delay 不猜（免得给 QQ 群套微信的 1.5 秒）。
    r = channel.rules("", "10000000001@chatroom")
    text = channel.render(r)
    assert r.mention_required is True
    assert r.allow_voice is False
    assert r.delay_ms == 0
    assert "群里的公开场合要先 @ 对方再说话" in text
    assert "不发语音，只发字" in text
    assert channel.rules("", "wxid_seed01").mention_required is False
def main() -> bool:
    """2026-11-02 补：这个文件以前只有 def test_、没有运行入口（一直空转）。"""
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
