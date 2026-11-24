# -*- coding: utf-8 -*-
"""主动触达的「怎么认 / 记一笔」（core/speech/proactive_ledger.py）。

从 `core/speech/outbound.py` 迁出的纯逻辑。这里钉四件事：
1. 三条继承路径（draft.extra / 语音快照 / agent 上下文）都认；
2. 正常回复永不算主动触达 —— 尤其是「备注名 ≠ wxid」时**不许**靠名字差异推断跨会话
   （历史事故：她正常回群友，被记成主动搭话）；
3. 记忆在哪个会话上：显式给的优先，其次才是 who / 快照目标；
4. 记账只有一个副作用：不是主动触达 / 没目标 / 服务建不出来 ⇒ 一条都不记。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.speech import proactive_ledger as PL  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print("  ok  " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name + ((" :: " + str(detail)) if detail else ""))


def _draft(**kw):
    base = dict(extra={}, who="", content="正文", filepath="",
                target=SimpleNamespace(platform="wechat", account_id="bot1"))
    base.update(kw)
    return SimpleNamespace(**base)


def _snapshot(trigger_reason="", audience=None, target_chat="", target_platform="wechat"):
    return SimpleNamespace(
        turn=SimpleNamespace(
            trigger_reason=trigger_reason,
            target=SimpleNamespace(chat_id=target_chat, platform=target_platform),
        ),
        audience=audience,
    )


def _ctx(trigger="", extra=None):
    return SimpleNamespace(trigger=trigger, extra={} if extra is None else dict(extra))


def t_explicit_flags() -> None:
    for key, value in (("proactive", True), ("proactive_send", "yes"), ("_proactive", 1)):
        check(f"draft.extra[{key!r}]={value!r} 算主动", PL.is_proactive_send(_draft(extra={key: value}), _snapshot()) is True)
    check("send_mode=proactive 算主动",
          PL.is_proactive_send(_draft(extra={"send_mode": "proactive"}), _snapshot()) is True)
    check("extra.trigger=proactive 算主动",
          PL.is_proactive_send(_draft(extra={"trigger": "proactive"}), _snapshot()) is True)
    check("proactive=False 不算", PL.is_proactive_send(_draft(extra={"proactive": False}), _snapshot()) is False)


def t_snapshot_and_context() -> None:
    check("快照 trigger_reason=proactive 算主动",
          PL.is_proactive_send(_draft(), _snapshot(trigger_reason="proactive")) is True)
    check("快照 audience=PROACTIVE 算主动",
          PL.is_proactive_send(_draft(), _snapshot(audience=SimpleNamespace(value="proactive"))) is True)
    for trigger in ("proactive", "proactive_trigger", "yinyue_autonomous"):
        check(f"context.trigger={trigger} 算主动",
              PL.is_proactive_send(_draft(), _snapshot(), _ctx(trigger=trigger)) is True)
    check("context.extra.trigger_reason=proactive 算主动",
          PL.is_proactive_send(_draft(), _snapshot(), _ctx(extra={"trigger_reason": "proactive"})) is True)


def t_normal_reply_is_never_proactive() -> None:
    # 回归点：提醒回合曾被判成主动搭话，于是按主动触达策略记账、还过了静默时段闸。
    reminder = _draft(extra={"proactive": False, "trigger": "reminder"}, who="群友备注名")
    snap = _snapshot(trigger_reason="mention", target_chat="wxid_group_member")
    check("提醒回合不算主动", PL.is_proactive_send(reminder, snap, _ctx()) is False)
    check("备注名与 wxid 不一致也不推断跨会话", PL.is_proactive_send(reminder, snap, _ctx(trigger="mention")) is False)
    check("什么都没有时不算", PL.is_proactive_send(_draft(), _snapshot(), None) is False)


def t_target_precedence() -> None:
    snap = _snapshot(target_chat="wxid_from_snapshot")
    check("显式 proactive_chat_id 最优先",
          PL.proactive_target_id(_draft(extra={"proactive_chat_id": "wxid_explicit"}, who="wxid_who"), snap) == "wxid_explicit")
    check("target_chat_id 次之",
          PL.proactive_target_id(_draft(extra={"target_chat_id": "wxid_t2"}, who="wxid_who"), snap) == "wxid_t2")
    check("没有显式就用 who",
          PL.proactive_target_id(_draft(who="wxid_who"), snap) == "wxid_who")
    check("who 为空时落到快照目标",
          PL.proactive_target_id(_draft(who=""), snap) == "wxid_from_snapshot")
    check("群 id 原样返回",
          PL.proactive_target_id(_draft(who="123@chatroom"), snap) == "123@chatroom")
    check("非微信平台不做解析",
          PL.proactive_target_id(_draft(who="某个昵称"), _snapshot(target_platform="qq"), account_id="bot1") == "某个昵称")


def t_record_only_when_proactive() -> None:
    calls: list[dict] = []

    def fake_build():
        return SimpleNamespace(record_send=lambda **kw: calls.append(kw))

    import core.terminal.proactive_service as svc
    old = svc.build_proactive_service
    svc.build_proactive_service = fake_build
    try:
        snap = _snapshot(target_chat="wxid_owner")
        PL.record_send(_draft(extra={"proactive": True}, who="wxid_owner"), snap, _ctx(), "bot1",
                       content_preview="预览", ok=False, error="超时")
        check("主动触达记一笔", len(calls) == 1, calls)
        got = calls[0] if calls else {}
        check("记的是触发类型 proactive", got.get("trigger") == "proactive")
        check("记的是目标会话", got.get("chat_id") == "wxid_owner")
        check("记的是账号", got.get("bot_account_id") == "bot1")
        check("预览由调用方给（出口的 200 字口径）", got.get("content_preview") == "预览")
        check("失败也记（ok/error 原样传）", got.get("ok") is False and got.get("error") == "超时")

        calls.clear()
        PL.record_send(_draft(extra={}, who="wxid_owner"), snap, _ctx(), "bot1", content_preview="x")
        check("正常回复一条都不记", calls == [], calls)

        calls.clear()
        PL.record_send(_draft(extra={"proactive": True}, who=""), _snapshot(), _ctx(), "bot1")
        check("没有目标就不记", calls == [], calls)
    finally:
        svc.build_proactive_service = old


def t_service_failure_is_silent() -> None:
    def boom():
        raise RuntimeError("服务没起来")

    import core.terminal.proactive_service as svc
    old = svc.build_proactive_service
    svc.build_proactive_service = boom
    try:
        check("服务建不出来返回 None", PL.proactive_service() is None)
        PL.record_send(_draft(extra={"proactive": True}, who="wxid_owner"), _snapshot(), _ctx(), "bot1")
        check("服务建不出来也不炸（记账不许影响已发出的那句）", True)
    finally:
        svc.build_proactive_service = old


def t_purity() -> None:
    src = (ROOT / "core" / "speech" / "proactive_ledger.py").read_text(encoding="utf-8")
    for bad in ("sqlite3", "llm.chat", "asyncio", "requests", "structlog"):
        check(f"不碰 {bad}", bad not in src)
    outbound = (ROOT / "core" / "speech" / "outbound.py").read_text(encoding="utf-8")
    check("出口只留委托", "proactive_ledger.is_proactive_send(" in outbound
          and "proactive_ledger.record_send(" in outbound)
    check("判据本体不在出口里了", "active_chat_id\", \"target_chat_id" not in outbound)


def main() -> int:
    for fn in (
        t_explicit_flags,
        t_snapshot_and_context,
        t_normal_reply_is_never_proactive,
        t_target_precedence,
        t_record_only_when_proactive,
        t_service_failure_is_silent,
        t_purity,
    ):
        print("\n" + fn.__name__)
        fn()
    print(f"\nRESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())