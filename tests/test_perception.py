# -*- coding: utf-8 -*-
"""批A4：①感知模块（入站消息 → ②原始消息账）。

覆盖：
* 谁/几点/原文 + 拍/撤回/媒体/系统 分类；
* 父链（引用 id/作者/原话）与提及；
* GROUP_SCENE 作用域闸门（off / 作用域外 / 私聊不写、不建模型）；
* msg_id 幂等；原文保真与超长标记；尾部上限；recent_perceptions 只读口。
"""
from __future__ import annotations

import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.perception import (  # noqa: E402
    perception_payload,
    record_perception,
)
from core.context.situation import (  # noqa: E402
    PERCEPTION_TAIL_MAX,
    PERCEPTION_TEXT_MAX,
    WorldModel,
    note_perception,
    project,
    recent_perceptions,
)
from core.context.situation_store import (  # noqa: E402
    known_world_models,
    reset_world_models,
)

CHAT = "room@chatroom"


@contextlib.contextmanager
def _env(**values):
    """临时改 env（features 每次读 env）：standalone / pytest 两用。"""
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update({key: str(value) for key, value in values.items()})
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _msg(**overrides):
    base = {
        "msg_id": "m1",
        "sender_id": "wxid_pan",
        "sender_name": "P5",
        "chat_id": CHAT,
        "chat_type": "group",
        "content": "在吗",
        "msg_type_name": "text",
        "created_at": "2026-03-01 12:00:00",
        "source": "wechat",
    }
    base.update(overrides)
    return base


@contextlib.contextmanager
def _scene_env(chat=CHAT, mode="shadow"):
    with _env(YINYUE_FEATURE_GROUP_SCENE=mode, YINYUE_GROUP_SOCIAL_CHATS=chat):
        yield


def test_payload_extracts_identity_text_and_time() -> None:
    payload = perception_payload(_msg(), account_id="acc1")
    assert payload["who"] == "P5" and payload["who_id"] == "wxid_pan"
    assert payload["text"] == "在吗"
    assert payload["kind"] == "text" and payload["msg_type"] == "text"
    assert payload["msg_id"] == "m1"
    assert payload["at"] == "2026-03-01 12:00"
    assert payload["platform"] == "wechat" and payload["account_id"] == "acc1"


def test_kind_pat_revoke_media_system() -> None:
    assert perception_payload(_msg(
        msg_type_name="pat", content='"P5" 拍了拍 "鲸鲸"',
    ))["kind"] == "pat"
    assert perception_payload(_msg(
        content='<sysmsg type="revokemsg"><revokemsg><content>P5撤回了一条消息</content></revokemsg></sysmsg>',
    ))["kind"] == "revoke"
    media = perception_payload(_msg(
        msg_type_name="image", content="", media_path="/tmp/a.jpg",
        media_mime="image/jpeg",
    ))
    assert media["kind"] == "media" and media["media"]["ref"] == "/tmp/a.jpg"
    assert media["media"]["media_mime"] == "image/jpeg"
    assert perception_payload(_msg(
        msg_type_name="system", content="xxx 撤回了一条消息",
    ))["kind"] == "system"
    # 正文里说"拍了拍桌子"不是拍一拍系统消息
    assert perception_payload(_msg(content="他拍了拍桌子就走了"))["kind"] == "text"


def test_payload_parent_chain_mentions_and_quote() -> None:
    payload = perception_payload(_msg(
        reply_to="m0",
        quote_reply_id="m9",
        quote_reply_user="P6",
        quote_reply_content="原话",
        mentions=json.dumps(
            [{"user_id": "wxid_a", "user_name": "阿黎"}], ensure_ascii=False,
        ),
    ))
    assert payload["reply_to"] == "m9"          # 平台引用 id 优先于 legacy reply_to
    assert payload["quote_user"] == "P6" and payload["quote_text"] == "原话"
    assert payload["mentions"] == ("阿黎",)
    # 认不出名字时保 id，不猜
    fallback = perception_payload(_msg(
        mentions=json.dumps([{"user_id": "wxid_b"}]),
    ))
    assert fallback["mentions"] == ("wxid_b",)


def test_record_perception_gated_by_scene_scope() -> None:
    reset_world_models()
    try:
        with _env(YINYUE_FEATURE_GROUP_SCENE="off",
                  YINYUE_GROUP_SOCIAL_CHATS=CHAT):
            assert record_perception(_msg()) is False
            assert CHAT not in known_world_models()      # off：不建模型

        with _scene_env(chat="other@chatroom"):
            assert record_perception(_msg()) is False
            assert CHAT not in known_world_models()      # 作用域外：不写

        with _scene_env():
            assert record_perception(_msg(chat_type="private")) is False
            assert CHAT not in known_world_models()      # 私聊：不写

        with _scene_env():
            assert record_perception(_msg()) is True
            view = project(known_world_models()[CHAT].events)
            assert len(view.perceptions) == 1
            assert view.perceptions[0].who == "P5"
            assert view.perceptions[0].kind == "text"
    finally:
        reset_world_models()


def test_record_perception_keeps_media_with_empty_content() -> None:
    reset_world_models()
    try:
        with _scene_env():
            assert record_perception(_msg(
                msg_type_name="image", content="", media_path="/tmp/a.jpg",
            )) is True     # 正文空也要入账（scheduler 里正文空判之前的挂点）
            item = project(known_world_models()[CHAT].events).perceptions[0]
            assert item.kind == "media" and item.text == ""
            assert item.media["ref"] == "/tmp/a.jpg"
    finally:
        reset_world_models()


def test_record_perception_is_idempotent_by_msg_id() -> None:
    reset_world_models()
    try:
        with _scene_env():
            assert record_perception(_msg()) is True
            assert record_perception(_msg()) is False       # 重复投递不落第二条
            view = project(known_world_models()[CHAT].events)
            assert len(view.perceptions) == 1
    finally:
        reset_world_models()


def test_text_preserved_and_long_text_marked() -> None:
    model = WorldModel()
    note_perception(model, who="P5", text="第一行\n第二行", at="12:00")
    item = project(model.events).perceptions[0]
    assert item.text == "第一行\n第二行"                    # 不折叠内部换行
    long_text = "长" * (PERCEPTION_TEXT_MAX + 500)
    note_perception(model, who="P5", msg_id="long", text=long_text, at="12:01")
    item = project(model.events).perceptions[1]
    assert item.text_len == len(long_text) and item.truncated is True
    assert len(item.text) <= PERCEPTION_TEXT_MAX and item.text.endswith("…")


def test_perception_tail_is_bounded_and_keeps_other_events() -> None:
    model = WorldModel()
    # 非感知事件必须原样保留，不被环形清理误伤
    model.add("fact_asserted", {"key": "P1.location", "value": "到了"}, at="11:59")
    total = PERCEPTION_TAIL_MAX + 3
    for index in range(total):
        note_perception(model, who="P5", msg_id=f"m{index}", text="x")
    view = project(model.events)
    assert len(view.perceptions) == PERCEPTION_TAIL_MAX
    assert view.perceptions[0].msg_id == "m3"               # 最老三条已被环形清掉
    assert view.perceptions[-1].msg_id == f"m{total - 1}"
    assert any(event.kind == "fact_asserted" for event in model.events)
    assert len([e for e in model.events if e.kind == "perception_observed"]) == PERCEPTION_TAIL_MAX


def test_recent_perceptions_returns_tail_old_to_new() -> None:
    model = WorldModel()
    for index in range(3):
        note_perception(model, who="P5", msg_id=f"m{index}", text=str(index))
    tail = recent_perceptions(model, limit=2)
    assert [item.msg_id for item in tail] == ["m1", "m2"]
    assert recent_perceptions(model, limit=0) == ()


# ---------------------------------------------------------------------------
# standalone runner（项目约定：python tests/test_perception.py；与 pytest 双兼容）
# ---------------------------------------------------------------------------

def main() -> bool:
    checks = [
        test_payload_extracts_identity_text_and_time,
        test_kind_pat_revoke_media_system,
        test_payload_parent_chain_mentions_and_quote,
        test_record_perception_gated_by_scene_scope,
        test_record_perception_keeps_media_with_empty_content,
        test_record_perception_is_idempotent_by_msg_id,
        test_text_preserved_and_long_text_marked,
        test_perception_tail_is_bounded_and_keeps_other_events,
        test_recent_perceptions_returns_tail_old_to_new,
    ]
    passed = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - 失败必须逐个报出来，不许静默
            print(f"  FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"  ok   {check.__name__}")
    print(f"perception checks: {passed}/{len(checks)} passed")
    return passed == len(checks)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
