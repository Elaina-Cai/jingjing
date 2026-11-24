"""D1 渠道约定（施工 Step 8）：面板改的值落在 data/prompt/channel.json，rules() 读时盖在出厂默认上。

这条链只有三个要求：
① 文件不在 = 出厂默认（面板从没改过，行为必须和以前一模一样）；
② 文件在 = 那些键盖上去，别的键不动；
③ 文件坏了（写一半 / 不是 JSON）= 当没改，**绝不能因为她读不到渠道约定就说不出话**。
"""
from __future__ import annotations

import json

import pytest

from core.policy import channel


@pytest.fixture()
def ov(tmp_path, monkeypatch):
    p = tmp_path / "channel.json"
    monkeypatch.setattr(channel, "OVERRIDE_PATH", str(p))
    channel._cache["at"], channel._cache["data"] = None, {}
    yield p
    channel._cache["at"], channel._cache["data"] = None, {}


GROUP_WECHAT = "10000000002@chatroom"


def test_no_file_means_factory_default(ov):
    r = channel.rules("wechat", GROUP_WECHAT)
    assert r.delay_ms == 1500 and r.mention_required is True and r.allow_voice is False
    # 面板显示的是"平台级的底"（私聊那一套），群里那几条是派生出来的
    assert channel.effective("wechat") == channel.factory("wechat")
    assert channel.group_extra("wechat") == {"mention_required": True, "allow_voice": False,
                                             "delay_ms": 1500}


def test_overrides_only_touch_the_named_keys(ov):
    ov.write_text(json.dumps({"wechat.allow_markdown": True}, ensure_ascii=False), encoding="utf-8")
    channel._cache["at"] = None
    r = channel.rules("wechat")
    assert r.allow_markdown is True
    assert r.mention_required is False, "没写的键必须还是出厂值"
    grp = channel.rules("wechat", GROUP_WECHAT)
    assert grp.mention_required is True, "没改的群规矩仍然生效"
    assert grp.allow_markdown is True, "面板改过的字段在群里也生效"


def test_override_text_reaches_the_prompt(ov):
    ov.write_text(json.dumps({"wechat.delay_ms": 2500}), encoding="utf-8")
    channel._cache["at"] = None
    text = channel.render(channel.rules("wechat"))
    assert "2.5 秒" in text
    # 面板显式改过 delay，群里就按主人设的走（不再被 1.5 秒盖掉）
    gtext = channel.render(channel.rules("wechat", GROUP_WECHAT))
    assert "2.5 秒" in gtext and "1.5 秒" not in gtext
    assert "要先 @" in gtext


def test_one_platform_does_not_leak_into_another(ov):
    ov.write_text(json.dumps({"qq.allow_voice": False}), encoding="utf-8")
    channel._cache["at"] = None
    assert channel.rules("qq").allow_voice is False
    assert channel.rules("wechat").allow_voice is True
    assert channel.rules("wechat", GROUP_WECHAT).delay_ms == 1500


def test_broken_file_falls_back_to_defaults_and_never_raises(ov):
    ov.write_text('{"wechat.delay_ms": 900,', encoding="utf-8")  # 写了一半
    channel._cache["at"] = None
    assert channel.rules("wechat").delay_ms == 0
    assert "[怎么开口]" in channel.render(channel.rules("wechat"))


def test_bad_types_are_coerced_not_crashed(ov):
    ov.write_text(json.dumps({"wechat.delay_ms": "1800", "qq.blank_line": "yes",
                              "qq.ask_hint": None}), encoding="utf-8")
    channel._cache["at"] = None
    assert channel.rules("wechat").delay_ms == 1800
    assert channel.rules("qq").blank_line is True
    assert channel.rules("qq").ask_hint == ""


def test_unknown_platform_ignores_overrides_of_known_ones(ov):
    ov.write_text(json.dumps({"wechat.delay_ms": 4000}), encoding="utf-8")
    channel._cache["at"] = None
    assert channel.rules("telegram").platform == ""
    assert channel.rules("telegram").delay_ms == 0


def test_editable_fields_are_exactly_what_the_panel_offers():
    """面板 SCHEMA 与 channel.EDITABLE 必须一一对应——加字段忘了接，面板会显示改不动的空格。"""
    from console import executor

    keys = {k.split(".", 2)[2] for k in executor.SCHEMA if k.startswith("channel.")}
    assert keys == set(channel.EDITABLE), (keys, channel.EDITABLE)
