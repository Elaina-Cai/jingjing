"""问「要不要我动手」：按这件任务写、尾巴固定、三要素不齐就退回模板。

为什么不能是固定文案：同一句话在「发一句」时是对的，换成撤回/点提交/付钱就变成撒谎。
为什么尾巴必须固定：`parse_approval` 只认那几个词（可以/行/算了/不用…），
让模型自由发挥就会出现「人回了可以、它却不认」这种最糟的失败。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

log = logging.getLogger(__name__)

APPROVE_HINT = "回「可以」我就动手；回「算了」就不动。"
MAX_ASK_CHARS = 200
MAX_ACTION_CHARS = 60

# 出现了就算「说了没做的事」——只在将来式里用，完成式一律不许。
DONE_CLAIM_RE = re.compile(r"(已发|已经发|已办|已经办|我去发|这就发|已撤回|已经撤回|已付|已经付)")
FUTURE_HINT_RE = re.compile(r"(要不要|要我|确认吗|可以吗)")

EFFECT_CONSEQUENCE = {
    "external_send": "发出去就收不回来了",
    "ui_control": "点完可能就改不回去了",
    "write_local": "会写进本地的记录里",
    "read": "只是看一眼，不改动任何东西",
}



def owner_private_chats(*, engine: Any = None) -> tuple[str, ...]:
    """主人本人的私聊地址（``account_registry.role='owner'``）。

    为什么批准请求只许送到这里：群里问一句「要不要我动手」，等于**当着别人
    谈授权**；而且群里任何人的「可以」都不算数（``core/jobs/approval.py`` 早有
    这道闸，但**问句本身就不该出现在群里**）。真机 2026-10-21 业主原话：
    「为什么不是私聊她，我都在群里说了还要她说什么」。

    查不到就返回空元组 —— 调用方按「不在主人私聊」处理（宁可不问，也不乱问）。
    """
    try:
        if engine is None:
            # 只读句柄：这条判据是「查一下主人是谁」，不是写库。
            # 用可写句柄会被 db.write_guard 拦（生产库不许脚本拿写权），
            # 真机 2026-10-21 就是用它把「不确定」变成了「全部 False」。
            from db.write_guard import read_only_engine
            from core.observability.weekly import meta_db_path

            engine = read_only_engine(meta_db_path())
        from sqlalchemy import text as _text

        with engine.connect() as conn:
            rows = conn.execute(
                _text("select account_id from account_registry where role = 'owner'")
            ).fetchall()
        return tuple(str(row[0]) for row in rows if str(row[0] or "").strip())
    except Exception as exc:  # noqa: BLE001 - 查不到不算错，只是不问
        log.warning("owner_private_chats_failed error=%s", exc)
        return ()


def is_owner_private(chat_id: str, *, engine: Any = None) -> bool:
    """这个会话是不是「主人本人的私聊」。"""
    value = str(chat_id or "").strip()
    if not value:
        return False
    return value in owner_private_chats(engine=engine)


def _clip(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def action_phrase(step: Any) -> str:
    """把 ``工具:半句话`` 翻成人话；认不出来就原样念（不猜）。"""
    action = str(getattr(step, "action", "") or "")
    tool, _, arg = action.partition(":")
    tool = tool.strip()
    arg = arg.strip()
    mapping = {
        "send": "发一句话",
        "gui_click": "点一下页面上的东西",
        "gui_input": "往输入框里填字",
        "gui_navigate": "打开一个网页",
        "gui_state": "看一眼当前窗口",
        "gui_snapshot": "读一下当前屏幕",
        "wechat_revoke": "撤回一条消息",
    }
    if tool in ("send", "send_message"):
        _tgt, _body = split_send(action)
        if _body:
            return _clip(f"发一句话：{_body}", MAX_ACTION_CHARS)
    head = mapping.get(tool, tool or "动手")
    return _clip(f"{head}：{arg}" if arg else head, MAX_ACTION_CHARS)


#: 发消息类动作的前缀：``send:目标|正文``（``|`` 见 ``core/jobs/executor.py::EXTERNAL_SEP``）。
SEND_PREFIXES = ("send:", "send_message:")


def split_send(action: str) -> tuple[str, str]:
    """把 ``send:目标|正文`` 拆成 ``(目标, 正文)``；不是发消息就返回 ``("", "")``。"""
    text = str(action or "").strip()
    lowered = text.lower()
    for prefix in SEND_PREFIXES:
        if lowered.startswith(prefix):
            body = text[len(prefix):]
            target, _, payload = body.partition("|")
            return (target.strip(), payload.strip() if payload.strip() else body.strip())
    return ("", "")


def effects_of(step: Any) -> tuple[str, ...]:
    """``Step.effects`` 按契约是**一个字符串**；这里统一成元组。

    踩过的坑：直接 ``tuple(str(item) for item in raw)`` 会把 ``"external_send"``
    拆成一个个字母，于是后果永远回落成「只是看一眼」。
    """
    raw = getattr(step, "effects", ()) or ()
    if isinstance(raw, str):
        return (raw.strip(),) if raw.strip() else ()
    return tuple(str(item) for item in raw if str(item).strip())


def consequence(step: Any) -> str:
    for effect in effects_of(step):
        if effect in EFFECT_CONSEQUENCE:
            return EFFECT_CONSEQUENCE[effect]
    return EFFECT_CONSEQUENCE["read"]


def template(job: Any, step: Any, *, target_name: str = "") -> str:
    """兜底模板：一定问得出口。"""
    send_target, _send_body = split_send(str(getattr(step, "action", "") or ""))
    where = target_name or send_target or str(getattr(job, "chat_id", "") or "")
    body = f"想做的事：往「{where}」{action_phrase(step)}（{consequence(step)}）。{APPROVE_HINT}"
    return _clip(body, MAX_ASK_CHARS)


def complete(text: str, *, target_name: str = "") -> bool:
    """三要素校验：动作、地点、后果。缺一条就不许发出去。"""
    value = str(text or "")
    has_place = bool(target_name) and target_name in value
    has_consequence = any(item in value for item in EFFECT_CONSEQUENCE.values())
    has_action = bool(value.strip()) and len(value.strip()) >= 8
    return has_place and has_consequence and has_action


def build_ask(
    job: Any,
    step: Any,
    *,
    target_name: str = "",
    generate: Callable[[str], str] | None = None,
    prompt: str = "",
) -> str:
    """先让模型按任务写一句；不合格（或模型挂了/说了完成式）就用模板。"""
    fallback = template(job, step, target_name=target_name)
    if generate is None:
        return fallback
    try:
        draft = str(generate(prompt or _default_prompt(job, step, target_name=target_name)) or "").strip()
    except Exception as exc:  # noqa: BLE001 - 问不出去比问得像机器人严重
        log.warning("ask_generate_failed error=%s", repr(exc))
        return fallback
    if not draft or DONE_CLAIM_RE.search(draft):
        log.info("ask_rejected reason=claim_or_empty draft=%r", draft[:80])
        return fallback
    if not complete(draft, target_name=target_name):
        log.info("ask_rejected reason=incomplete draft=%r", draft[:80])
        return fallback
    if APPROVE_HINT not in draft:
        draft = draft + APPROVE_HINT
    return _clip(draft, MAX_ASK_CHARS)


def _default_prompt(job: Any, step: Any, *, target_name: str = "") -> str:
    return (
        "你在替主人办一件事，这一步需要他批准。\n"
        f"会话：{target_name}\n动作：{action_phrase(step)}\n后果：{consequence(step)}\n"
        f"要求：一句话说清「要做什么、在哪、什么后果」，再问他做不做。不许说已经做了。\n"
        f"结尾固定带上：{APPROVE_HINT}"
    )


ASK_KIND = "ask"


def ask_ref(job_id: str, step_n: int) -> str:
    """一件事的某一步只问一次：回执里用这个 ref 记「已经问过了」。"""
    return f"{job_id}#{step_n}"


def pending_asks(
    store: Any,
    chat_id: str,
    *,
    target_name: str = "",
    asked: tuple[str, ...] | list[str] = (),
    generate: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    """找出「在等批准、但还没问过」的那几步，给出要说的话。

    不在这里发送：发送是出口的事，计划器/问批准都不许自己开口（R3）。
    """
    if not chat_id:
        return []
    seen = set(asked or ())
    out: list[dict[str, Any]] = []
    try:
        jobs = store.open_jobs(chat_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("ask_pending_failed error=%s", repr(exc))
        return []
    for job in jobs:
        if str(getattr(job, "state", "")) != "awaiting":
            continue
        awaiting = getattr(job, "awaiting", None)
        if str(getattr(awaiting, "kind", "")) != "approval":
            continue
        step = getattr(job, "next_step", lambda: None)()
        if step is None:
            continue
        ref = ask_ref(job.job_id, getattr(step, "n", 0))
        if ref in seen:
            continue
        out.append(
            {
                "job_id": job.job_id,
                "step_n": getattr(step, "n", 0),
                "ref": ref,
                "text": build_ask(job, step, target_name=target_name, generate=generate),
            }
        )
    return out[:3]


def looks_like_done_claim(text: str) -> bool:
    """出口/审核侧用：这句话是不是在宣称已经做过。"""
    return bool(DONE_CLAIM_RE.search(str(text or "")))
