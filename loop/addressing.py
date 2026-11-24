"""这句是不是在跟我说：只读结构事实的判定器（不让模型自评）。

2026-11-04（m02679 / m02680，用户选 B）：群里
- ``named``：@我 / 拍了拍我 / 引用我上一句 -> **必答**
- ``called``：正文里出现我的名字、但没 @ 我 -> 可答可不答
- ``following``：刚点过我名的同一账号在短时间内接着说，中间没人插话 -> 可答可不答
- ``questioned``：他这句带问句，而同会话最近 ``QUESTION_WINDOW_S`` 秒内没别人说过话
  （＝这句不是在跟旁人说话）-> 可答可不答
- ``""``：别人的闲聊 -> **不许开新话题**（可以只是看着）

判定只用名单、发言人账号、时间戳与消息形态。
**不许**在这里调模型、算相似度、猜语气 —— 猜出来的东西没法回放，也没法判对错。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import re
import time

from core.conversation.addressing import strip_quote_block

_AT_RE = re.compile(r"@[^\s@]{1,30}")

FOLLOW_WINDOW_S = 90.0
ASK_WINDOW_S = 12 * 3600
ASK_LAG_S = 4 * 3600
_MIN_PLAUSIBLE_TS = 946684800.0
QUESTION_WINDOW_S = 300.0
QUESTION_MARKS = ("?", "？")
MUST_ANSWER_KINDS = ("named",)
ADDRESSED_KINDS = ("named", "called", "following", "questioned")


def _live_for_ask(row, *, newest: float, now: float) -> bool:
    """A direct address only belongs to this round while it is recent and not buried."""
    at = _row_ts(row)
    if at <= 0:
        return True
    if at < _MIN_PLAUSIBLE_TS:
        return True
    if now - at > ASK_WINDOW_S:
        return False
    return newest - at <= ASK_LAG_S


_PAUSE_CLAUSE = re.compile(
    r"(?:这(?:条|句|段)(?:消息|话)?)?(?:你)?(?:请)?(?:先|暂时|现在|这会儿)?"
    r"(?:别|不[用要必])(?:先|再|急着|马上|立刻|着急)?回(?:复)?"
    r"(?:我|这条消息|这条)?[了啊吧哦呀]?"
    # 2026-11-22：真实说法里还有「先别理我」「别急着回我」「不用马上回」。
    r"|(?:你)?(?:请)?(?:先|暂时)?(?:别|不用|不要)理我[了啊吧哦呀]?"
)
_THIRD_PERSON = re.compile(r"^(?:他|她|别人|有人|对方|人家)")
_PAUSE_ATTRIBUTION = re.compile(
    r"[「」『』“”\"?？]|吗|如果|假如|要是|万一|听说|据说|"
    r"(?:他|她|别人|有人|对方|人家)(?:们)?(?:说|讲|让)"
)
# 「先别查，我想跟你说件事」：这一拍只要我听，不要替我去查。
# 只认「不做一件事（查/搜）」+「让我说/听我」这两截，不列举具体任务。
_LISTEN_NO_LOOKUP = re.compile(
    r"(?:你)?(?:请)?(?:先|暂时)?(?:别|不用|不要|不必|不需要|甭)"
    r"(?:先|再)?(?:帮我|替我|给我)?(?:去)?(?:查|搜)(?:一?下|一眼)?"
    r"[了啊吧哦呀呢哈先]*"
)
_LISTEN_ASK = re.compile(
    r"(?:你)?(?:先)?听(?:我|一下)(?:说|讲)?(?:完|(?:两|几|一)句(?:话)?)?(?:就好|就行)?"
    r"|我(?:只|就)?(?:是)?(?:想|要)(?:先)?(?:跟|和|给)?(?:你)?"
    r"(?:说说?|讲讲?|聊聊?|吐槽)(?:一?下|一?件|几?句|两句|点)?(?:事)?(?:就好|就行)?"
)
# 2026-11-22：「只想被倾听」在真实聊天里也常说「别给建议」「别急着安慰我」。
# 原来只认「别查/别搜」，于是这类请求会被当成普通求助，她直接开始给方案。
_LISTEN_NO_ADVICE = re.compile(
    r"(?:你)?(?:请)?(?:先|暂时)?(?:别|不用|不要|不必|不需要|甭)"
    r"(?:急着|忙着)?(?:给我|替我|跟我|对我|给)?"
    r"(?:建议|讲道理|说教|分析|劝我|教我(?:怎么|如何)?(?:做|办)?)"
    r"[了啊吧哦呀呢哈先]*"
)
_LISTEN_NO_COMFORT = re.compile(
    r"(?:你)?(?:请)?(?:先|暂时)?(?:别|不用|不要|不必|不需要)"
    r"(?:急着|忙着)?(?:安慰|劝|哄)(?:我)?"
    r"[了啊吧哦呀呢哈先]*"
)


@dataclass(frozen=True)
class NamedRequest:
    who: str
    request_text: str
    request_row_id: str


@dataclass(frozen=True)
class Verdict:
    """一句话结论 + 一句给人看的理由（who 是说话的人）。"""

    kind: str = ""
    why: str = ""
    who: str = ""
    n: int = 0
    sample: str = ""
    request_text: str = ""
    request_row_id: str = ""
    request_context_row_ids: tuple[str, ...] = ()
    other_named_requests: tuple[NamedRequest, ...] = ()

    @property
    def addressed(self) -> bool:
        return self.kind in ADDRESSED_KINDS

    @property
    def must_answer(self) -> bool:
        return self.kind in MUST_ANSWER_KINDS

    def short(self) -> str:
        """给提示词用的一行（≤ 50 字），没有结论就返回空串。"""
        if not self.kind:
            return ""
        who = self.who or "有人"
        return "%s：%s（%s）" % (who, self.why, "必答" if self.must_answer else "可答可不答")


def _aliases() -> tuple:
    try:
        from loop import ports as _p

        return tuple(_p.me_aliases())
    except Exception:
        return ()


def _try_strptime(text: str, fmt: str) -> float | None:
    """按一个格式解析时间；认不出返回 None。

    2026-11-04：原来写成 ``except ValueError: continue``，被
    ``tests/pytest_style/test_silent_except_discipline.py`` 判成"静默吞异常"
    （body 全是 continue/pass 就算吞）。换成显式 None，调用方看得见"这个格式不认"。
    """
    try:
        return datetime.strptime(text, fmt).timestamp()
    except (ValueError, TypeError):
        return None


def _ts(value) -> float:
    """时间戳。认 float/int、认 ``YYYY-MM-DD HH:MM:SS``，认不出来当 0。"""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value or "").strip()
    if not s:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        got = _try_strptime(s[:19], fmt)
        if got is not None:
            return got
    return 0.0


def _row_ts(row) -> float:
    """行的时间戳。``_ts`` 只认值；把行对象传进去会一路退化成 0（2026-11-04 踩过）。"""
    return _ts(getattr(row, "at", 0.0))


def _text(row) -> str:
    raw = str(getattr(row, "raw", "") or "")
    txt = str(getattr(row, "text", "") or "")
    if not raw:
        return txt
    if not txt or txt in raw:
        return raw
    return raw + " " + txt


def _speaker(row) -> str:
    return str(getattr(row, "speaker", "") or "") or "有人"


def _request_text(row, *, full: bool = False) -> str:
    text = str(getattr(row, "text", "") or "").strip()
    if getattr(row, "quote_id", ""):
        text = strip_quote_block(text) or text
    if not full and len(text) > 420:
        text = text[:180] + "…（中间略）…" + text[-240:]
    return text


def _with_request(kind: str, row, why: str, count: int, context_row_ids: tuple[str, ...] = (),
                  other_named_requests: tuple[NamedRequest, ...] = ()) -> Verdict:
    return Verdict(kind, why, _speaker(row), count, _text(row)[:60],
                   request_text=_request_text(row), request_row_id=str(getattr(row, "msg_id", "") or ""),
                   request_context_row_ids=context_row_ids, other_named_requests=other_named_requests)


def _new_text(row) -> str:
    return strip_quote_block(str(getattr(row, "raw", "") or "")) + " " + strip_quote_block(
        str(getattr(row, "text", "") or ""))


def named_reason(row, names: tuple) -> str:
    """Read direct address signals, preferring verified authorship for platform quotes."""
    body = _new_text(row)
    if any(("@" + a) in body for a in names) or _mention_hit(body, names):
        return "这条 @ 了你"
    if any(('拍了拍 "%s"' % a) in body for a in names) or ("拍了拍我" in body):
        return "这条拍了拍你"
    if getattr(row, "quote_id", ""):
        return "这条引用了你的话" if getattr(row, "quote_targets_self", False) else ""
    if any(_text(row).lstrip().startswith("[引用 %s" % a) for a in names):
        return "这条回了你上一句"
    return ""


def mentions_self(row, names: tuple) -> bool:
    return any(a in _new_text(row) for a in names)


def _is_question(text: str) -> bool:
    return any(k in text for k in QUESTION_MARKS)


def explicit_reply_pause(text: str) -> bool:
    """Recognize a direct, standalone pause in the current speaker's request."""
    if _PAUSE_ATTRIBUTION.search(text):
        return False
    body = re.sub(r"^\s*@[^\s，,：:]+[\s，,：:]+", "", text, count=1)
    for clause in re.split(r"[，,。.!！；;\r\n]", body):
        got = clause.strip()
        # 2026-11-22：第三人称作主语的句子是在说别人的状态（「他现在别回了」），
        # 不是让我停。直接说「你现在别回」仍然算。
        if _THIRD_PERSON.match(got):
            continue
        if _PAUSE_CLAUSE.fullmatch(got):
            return True
    return False


def explicit_listening_request(text: str) -> bool:
    """Recognize a direct 「先别查，听我说」 request in the current speaker's words.

    Narrow by design: exactly one pause clause (no lookup / no advice / no comfort)
    plus one ask-to-listen clause.
    Quoted old words, hypotheticals, third-party reports and any extra clause
    (resume / do something else) keep this from firing.
    """
    body = strip_quote_block(str(text or ""))
    if not body or _PAUSE_ATTRIBUTION.search(body):
        return False
    body = re.sub(r"^\s*@[^\s，,：:]+[\s，,：:]+", "", body, count=1)
    clauses = [clause.strip() for clause in re.split(r"[，,。.!！；;\r\n]", body) if clause.strip()]
    if len(clauses) != 2:
        return False
    first, second = clauses

    def _pause(clause: str) -> bool:
        return bool(_LISTEN_NO_LOOKUP.fullmatch(clause)
                    or _LISTEN_NO_ADVICE.fullmatch(clause)
                    or _LISTEN_NO_COMFORT.fullmatch(clause))

    return bool(
        (_pause(first) and _LISTEN_ASK.fullmatch(second))
        or (_LISTEN_ASK.fullmatch(first) and _pause(second))
    )


def _is_noise(text: str) -> bool:
    """系统消息（撤回、拍一拍通知的 xml、入群提示）不算谁在说话。"""
    s = str(text or "").strip()
    return (not s) or s.startswith("<?xml") or "<sysmsg" in s


def _mention_hit(text: str, names: tuple) -> bool:
    """``@某人`` 里含我的名字也算 @（微信的 @ 后面跟的是显示名，可能带前缀）。"""
    for chunk in _AT_RE.findall(str(text or "")):
        if any(a in chunk for a in names):
            return True
    return False


def _aimed_elsewhere(row, text: str) -> bool:
    """这条明确指向别人或另一条消息（@ 别人 / 引用）。"""
    return bool(_AT_RE.search(text) or getattr(row, "quote_id", "")
                or getattr(row, "quote_user", ""))


def _advance_request(window, start: int):
    """Follow the same speaker's adjacent text or exact quote of their request.

    Returns ``(request, earlier_row_ids)``. ``earlier_row_ids`` is empty unless
    the named row was promoted to a later same-sender continuation; otherwise it
    keeps the original named row plus up to two most recent admitted
    continuations (max 3 stable IDs, chronological, current row excluded).
    """
    anchor = window[start]
    sender = str(getattr(anchor, "sender_id", "") or "").strip()
    addressed_at = _row_ts(anchor)
    if not sender or not addressed_at:
        return anchor, ()
    admitted = []
    anchors = {str(getattr(anchor, "platform_msg_id", "") or "")}
    anchors.discard("")
    adjacent = True
    for index in range(start + 1, len(window)):
        row = window[index]
        if getattr(row, "is_self", False) or not 0 <= _row_ts(row) - addressed_at <= FOLLOW_WINDOW_S:
            break
        body = strip_quote_block(str(getattr(row, "text", "") or ""))
        linked = str(getattr(row, "quote_id", "") or "") in anchors and not _AT_RE.search(body)
        kind = getattr(row, "kind", "text")
        if (str(getattr(row, "sender_id", "") or "").strip() != sender
                or (kind != "text" and not (linked and kind == "quote_reply"))
                or not str(getattr(row, "msg_id", "") or "").strip()
                or _is_noise(body)
                or not (linked or (adjacent and not _aimed_elsewhere(row, _text(row))))):
            adjacent = False
            continue
        admitted.append(row)
        adjacent = True
        platform_id = str(getattr(row, "platform_msg_id", "") or "")
        if platform_id:
            anchors.add(platform_id)
    if not admitted:
        return anchor, ()
    effective = admitted[-1]
    current_id = str(effective.msg_id).strip()
    ids = []
    for row in (anchor, *admitted[-3:-1]):
        rid = str(getattr(row, "msg_id", "") or "").strip()
        if rid and rid != current_id and rid not in ids:
            ids.append(rid)
    return effective, tuple(ids)


def verdict(rows, *, is_private: bool = False, names=None, my_last_at=None, peer_before_mine=None) -> Verdict:
    """给一串同一会话的消息（旧->新）判一句：这一拍在跟谁说。

    只算「我最后一句之后」的那几条 —— 我已经回过的、以及更早的旧话都不再算
    在等我（与 ``loop/snapshot.py`` 里 ``after_mine`` 的口径一致；她开口后 ★ 本来就消失）。
    ``rows`` 已经是 ``after_mine`` 时，另传最后一次发言的时间和此前最后一个发言人。

    判定顺序是有意的：硬信号（@/拍/引用）压过叫名字，叫名字压过"接着我说"，
    最后才轮到"没人跟他说话的问句"。系统消息（撤回 xml 等）直接忽略。
    """
    seq = sorted(rows or [], key=lambda row: (_row_ts(row), getattr(row, "msg_id", 0) or 0))
    if my_last_at is not None:
        my_ts = _ts(my_last_at)
        after = seq
    else:
        last_mine = -1
        for i, r in enumerate(seq):
            if bool(getattr(r, "is_self", 0)):
                last_mine = i
        after = seq[last_mine + 1:] if last_mine >= 0 else seq
        my_ts = _row_ts(seq[last_mine]) if last_mine >= 0 else None
        if last_mine >= 0:
            peer_before_mine = next(
                (r for r in reversed(seq[:last_mine]) if not getattr(r, "is_self", False)
                 and not _is_noise(_text(r))), None)
    peers = [r for r in after if not bool(getattr(r, "is_self", 0)) and not _is_noise(_text(r))]

    if is_private:
        if not peers:
            return Verdict("", "私聊里没有新话", "", 0)
        last = peers[-1]
        return _with_request("named", last, "私聊里他在跟你说话", len(peers))
    if not peers:
        return Verdict("", "你已经回过了，没人接着说", "", 0)

    al = tuple(names) if names is not None else _aliases()
    newest = max((_row_ts(r) for r in peers), default=0.0)
    now = time.time()
    asks = []
    for row in peers:
        why = named_reason(row, al)
        if why and _live_for_ask(row, newest=newest, now=now):
            asks.append((row, why))
    if asks:
        positions = {id(row): index for index, row in enumerate(after)}
        picked = None
        by_speaker = {}
        for anchor, why in reversed(asks):
            request, context_ids = _advance_request(after, positions[id(anchor)])
            position = positions[id(request)]
            # Ties keep the more recent direct address.
            if picked is None or position > picked[0]:
                picked = (position, anchor, request, context_ids, why)
            speaker = str(getattr(request, "sender_id", "") or "").strip()
            if speaker and (speaker not in by_speaker or position > by_speaker[speaker][0]):
                by_speaker[speaker] = position, request
        _, anchor, request, context_ids, why = picked
        if request is not anchor:
            why = ("同一个人引用自己的委托接着说" if getattr(request, "quote_id", "")
                   else "点名以后，同一个人紧接着说")
        primary = str(getattr(request, "sender_id", "") or "").strip()
        request_at = _row_ts(request)
        secondary = []
        for speaker, (_, row) in sorted(by_speaker.items(), key=lambda item: item[1][0], reverse=True):
            row_at = _row_ts(row)
            if not (primary and speaker != primary and request_at and row_at
                    and 0 <= request_at - row_at <= FOLLOW_WINDOW_S):
                continue
            row_id = str(getattr(row, "msg_id", "") or "")
            text = _request_text(row)
            if row_id and text and not explicit_reply_pause(_request_text(row, full=True)):
                secondary.append(NamedRequest(_speaker(row), text, row_id))
            if len(secondary) == 2:
                break
        return _with_request("named", request, why, len(asks), context_ids, tuple(reversed(secondary)))

    calleds = [r for r in peers if mentions_self(r, al)
               and not (named_reason(r, al) and not _live_for_ask(r, newest=newest, now=now))]
    if calleds:
        last = calleds[-1]
        return _with_request("called", last, "提到了你的名字（没 @ 你）", len(calleds))

    last = peers[-1]
    text = _text(last)
    if _aimed_elsewhere(last, text):
        return Verdict("", "这条指向了其他人或另一条消息", sample=text[:60])

    last_ts = _row_ts(last)
    prior_ts = _row_ts(peer_before_mine)
    prior_id = getattr(peer_before_mine, "sender_id", "")
    if (my_ts and prior_ts and prior_id and named_reason(peer_before_mine, al)
            and 0 <= my_ts - prior_ts <= FOLLOW_WINDOW_S
            and 0 <= last_ts - my_ts <= FOLLOW_WINDOW_S
            and all(getattr(peer, "sender_id", "") == prior_id for peer in peers)):
        return _with_request("following", last, "刚才点过你名，又接着说", 1)

    earlier = bool(prior_ts and 0 <= last_ts - prior_ts <= QUESTION_WINDOW_S)
    if _is_question(text) and not earlier and not any(
        0 <= last_ts - _row_ts(peer) <= QUESTION_WINDOW_S for peer in peers[:-1]
    ):
        return _with_request("questioned", last, "这是群里的开放提问", 1)
    return Verdict("", "没在跟你说话", "", 0, text[:60])
