"""结构性不变量：发送路径不许有「清空输入框」的能力。

2026-10-28 事故「哦」：1~2 个字的短句被贴上输入框后又被删掉，反复如此。
根因不是判据不准，而是发送路径本身握着「清空」这个破坏性能力。
这里把"这能力不存在"钉成测试，防止下次再以"加个判断"的方式回来。
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = open(os.path.join(ROOT, "core", "execution", "wx_sender.py"), encoding="utf-8").read()


def test_send_path_has_no_clear():
    assert "_clear_focused_field" not in SRC, "发送路径又能清空输入框了"
    assert "clear_message_box" not in SRC, "发送路径又能清空输入框了"


def test_no_repaste_retry():
    # 重贴必须靠"清空"才幂等，既然清空没了，重贴也必须没有
    assert "wx_send_text_missing_retry" not in SRC


def test_fail_closed_on_dirty_box():
    assert "wx_send_box_not_empty" in SRC


def test_missing_destroys_nothing():
    i = SRC.index('"wx_send_text_missing"')
    tail = SRC[i:i + 900]
    assert "我没有清空" in tail
    assert "press_enter()" not in tail.split("return")[0]


GUI = open(os.path.join(ROOT, "core", "execution", "gui", "wechat.py"), encoding="utf-8").read()


def test_fill_path_does_not_clear_composer():
    # 填字路径发现残留时必须停手，不许擦掉（残留是证据）
    assert "_clear_dirty_composer(impl" not in GUI
    assert "dirty_composer_blocked" in GUI


def test_clear_capability_lives_in_manual_tool_only():
    tool = os.path.join(ROOT, "scripts", "wx_clear_box.py")
    assert os.path.exists(tool), "人工清理入口不见了"
