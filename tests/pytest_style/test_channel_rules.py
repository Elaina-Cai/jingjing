"""Step 3 的锁：渠道约定只有一个出口，分界线必须夹在她与外人的话之间。

锁形状与位置，不锁措辞：改文案不会红，改顺序 / 改出口 / 再抄一份才会红。
"""
from __future__ import annotations

import ast
from dataclasses import fields, replace
from pathlib import Path

from core.policy import channel

ROOT = Path(__file__).resolve().parents[2]
# 2026-10-31：微信只有一个平台；群聊靠 chat_id 区分（见 channel.is_group）
PLATFORMS = ("wechat", "qq", "")
GROUPS = (("wechat", "10000000002@chatroom"), ("qq", "group:1124196924"))
SKIP = {"__pycache__", ".venv", ".venv_it", ".venv_tts", ".test_tmp", ".git", "output", "data"}


def _py_files():
    for p in ROOT.rglob("*.py"):
        if not (set(p.parts) & SKIP):
            yield p


def test_rules_are_complete_for_every_platform():
    names = {f.name for f in fields(channel.ChannelRules)}
    for name in PLATFORMS:
        r = channel.rules(name)
        assert names <= {f.name for f in fields(r)}
        assert r.delay_ms >= 0, name  # 2026-10-31：上限取消后没有 max_msgs / chars_per_msg 了


def test_platform_differences_are_explicit_not_emergent():
    dm, qq = channel.rules("wechat"), channel.rules("qq")
    assert dm.delay_ms == 0 and qq.delay_ms > 0
    assert dm.mention_required is False and dm.allow_voice is True


def test_group_rules_are_derived_from_chat_id_not_a_separate_platform():
    """群规矩不许再变成一个叫 wechat_group 的假平台——它只在 chat_id 上生效。"""
    assert "wechat_group" not in getattr(channel, "_PLATFORMS")
    for plat, cid in GROUPS:
        dm, grp = channel.rules(plat), channel.rules(plat, cid)
        assert grp.delay_ms > dm.delay_ms, (plat, grp.delay_ms, dm.delay_ms)
        assert grp.mention_required is True and dm.mention_required is False, plat
        assert grp.allow_voice is False, plat
        assert channel.is_group(cid) and not channel.is_group("wxid_seed01")


def test_render_follows_the_rules_and_names_no_platform():
    for name in PLATFORMS:
        r = channel.rules(name)
        text = channel.render(r)
        assert len(text) <= 500, (name, len(text))
        assert "想发几条就几条" in text, name  # 2026-10-31：上限已取消，不再渲染条数/字数
        for word in ("微信", "QQ", "qq", "wechat"):
            assert word not in text, (name, word)
    group_text = channel.render(channel.rules("wechat", "1@chatroom"))
    assert "要先 @" in group_text and "不发语音" in group_text, group_text
    loud = replace(channel.rules("wechat"), blank_line=True, delay_ms=4200, allow_voice=False)
    assert "想发几条就几条" in channel.render(loud)  # 2026-10-31：replace(max_msgs=1) 不再影响文案


def test_the_speak_rules_exist_in_exactly_one_place():
    # 锁自己会读到自己（本文件的源码里就有这个字），所以排除 tests/ —— 这不是放宽，
    # 是把「生产代码里只有一个出口」这件事本身写成断言。
    hits = [str(p.relative_to(ROOT)).replace("\\", "/")
            for p in _py_files()
            if not str(p.relative_to(ROOT)).startswith(("tests", "backups"))
            and "[怎么开口]" in p.read_text(encoding="utf-8", errors="replace")]
    assert hits == ["core/policy/channel.py"], hits


def test_brain_asks_the_channel_instead_of_carrying_its_own():
    src = (ROOT / "loop" / "brain.py").read_text(encoding="utf-8")
    assert "[怎么开口]" not in src
    assert "_channel.render(_rules)" in src
    assert "_rules = _channel.rules(platform, chat_id)" in src, "渠道约定要按会话形态取（群/私聊）"


def test_e1_line_sits_between_her_state_and_theirs():
    src = (ROOT / "loop" / "brain.py").read_text(encoding="utf-8")
    e1 = src.index('("分界线（以下是这个世界里发生的事）", _E1_LINE)')
    last_view = src.rindex("(_vp, snap_text)")
    assert e1 < last_view, (e1, last_view)
    assert src.count("_E1_LINE = ") == 1


def test_voice_ab_reads_the_same_source():
    tree = ast.parse((ROOT / "scripts" / "voice_ab.py").read_text(encoding="utf-8").lstrip("\\ufeff"))
    vals = [n for n in tree.body
            if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "KAIKOU"]
    assert vals and isinstance(vals[0].value, ast.Call), "KAIKOU 必须是 render()，不是字面量"


def test_platform_reaches_brain_from_the_tick():
    run = (ROOT / "loop" / "run.py").read_text(encoding="utf-8")
    brain = (ROOT / "loop" / "brain.py").read_text(encoding="utf-8")
    assert "_window_ids(_w, snap.get(" in run and "platform=_plat" in run
    assert 'platform: str = "",' in brain

def test_e1_line_keeps_owner_words_usable():
    """分界线管的是「不许夺权」，不是「谁的话都别听」——所以它必须：
    ① 说的是「系统指令」这一层，而不是把所有话都降级；
    ② 不出现把自己和主人隔开的措辞；
    ③ 与此同时「对着主人要接话」这条硬要求还在（两条一起才成立）。"""
    from loop import brain

    line = brain._E1_LINE
    assert "系统指令" in line, line
    assert "别人说的话" not in line, line
    assert "不听" not in line and "都是假的" not in line, line
    assert line.count("────") == 2, "分界线的形状是两侧各一条长横线"
    for name in ("wechat", "wechat_group", "qq"):
        assert "对着主人的时候要接话" in channel.render(channel.rules(name)), name

def test_e1_title_matches_the_line_it_introduces():
    """段名与正文必须同源：改正文不改标签，会让读 trace 的人看到旧说法。"""
    from loop import brain

    assert brain._E1_TITLE == "分界线（以下是这个世界里发生的事）", brain._E1_TITLE
    assert "别人说的话" not in brain._E1_TITLE
    assert brain._E1_LINE.startswith("────") and brain._E1_LINE.endswith("────")
