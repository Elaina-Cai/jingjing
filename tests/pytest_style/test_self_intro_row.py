"""「我是群聊X的Y」不该被算成她说的话（2026-10-30）。"""
from loop.snapshot import _fix_self_intro


class _R:
    def __init__(self, text, is_self=True, chat_id="wxid_x"):
        self.text = text
        self.is_self = is_self
        self.chat_id = chat_id
        self.speaker = ""


def test_intro_is_given_back_to_the_other_person():
    r = _R("我是群聊\"示例作业群\"的P4")
    out = _fix_self_intro([r])
    assert out[0].is_self is False
    assert out[0].speaker == "P4"


def test_normal_self_message_is_untouched():
    r = _R("嗯")
    _fix_self_intro([r])
    assert r.is_self is True
    assert r.speaker == ""


def test_other_peoples_intro_is_untouched():
    r = _R("我是群聊\"X\"的某人", is_self=False)
    _fix_self_intro([r])
    assert r.is_self is False
