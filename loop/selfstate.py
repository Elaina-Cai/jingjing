"""她的「我」：一张会变的表，不是人设卡里的形容词。

人设卡说的是"我是什么样的人"（不变的部分，判断面/表达面都从它渲染）；
这张表说的是"我现在怎么样"——心境、手头在做什么、我怎么看某个人、
我上一次跟他们说了什么。想改可以调 myself。对当前发言人的看法只在对应的会话和对象下读回。

存在 data/loop/self_state.json。坏了/没有都不致命：render() 会给一句空表引导。
"""
from __future__ import annotations

import json as _json

from core.infra.jsonio import atomic_write_json
import os as _os
import time as _time
from pathlib import Path as _Path
from core.infra.envflags import text as _text

try:
    from .safelog import warn as _warn
except Exception:  # noqa: silent(safelog 缺失时静默)
    def _warn(*a, **k):
        return None

_ROOT = _Path(__file__).resolve().parents[1]
STATE = _ROOT / "data" / "loop" / "self_state.json"

_MOOD_MAX = 60
# 心境写的是「此刻」：超过 3 小时就只算旧心情，来源最多留 3 条原话够说明因何而来。
MOOD_FRESH_S = 3 * 3600
MOOD_REFS_MAX = 3
_DOING_MAX = 80
_NOTE_MAX = 60
_SAID_MAX = 40
_KEEP_STANCE = 6
_KEEP_MOOD = 8
_KEEP_SAID = 6
# 2026-10-27：她"事后才看清"的图（响应慢/当时没描述成）记在这，20 分钟内有效。
_VISION_MAX = 3
_VISION_TTL_S = 1200.0
_VISION_CHARS = 140


def _blank() -> dict:
    return {"mood": {"text": "", "at": 0.0}, "moods": {}, "doing": {"text": "", "at": 0.0},
            "stance": {}, "scoped_stance": {}, "said": {}, "vision": {}, "updated_at": 0.0,
            # 2026-10-25：她自己会动的部分（不是标签，是会随时间变的量）
            "pulse": {"social": 0.0, "arousal": 0.5, "at": 0.0,
                      "last_spoke": 0.0, "last_heard": 0.0,
                      "last_spont": 0.0}}



# ---------------- 会动的那部分：想说话的程度 / 困不困 ----------------
# 我（设计者）只提供两个物理量：时间、昼夜。她感受到的是"我有点想说话""我困了"，
# 不是一个需求清单。阈值控制"她敢不敢开口"，不控制"她想说什么"。
SOCIAL_PER_MIN = 0.020      # 每安静一分钟，想说话的程度涨这么多（约 36 分钟到阈值）
SOCIAL_CEIL = 1.0
# 2026-11-02 收回来（主人要求"主动开口调低"）。10-28 放开成 0.4/60s/300s 之后
# 实测代价：10-26~11-02 自发醒来 **1218 拍，只说出 36 句（3%）**，其中发到群的 12 句；
# 最差的一天（10-29）706 拍 **一句没说**。白醒一拍就是一次完整 LLM 调用，不值。
# 现在：需安静 30 分钟才"想开口"（0.6 / 0.020），最小间隔 30 分钟（退避仍会翻倍到 4 小时），
# 想说话这股劲儿 5 分钟只许用一次。
# 三个数都能用环境变量调，不用改代码：
#   YINYUE_SPEAK_SOCIAL=0.4 / YINYUE_SPONT_COOLDOWN_S=60 / YINYUE_SPONT_MIN_GAP_S=300
# 想回到 10-28 那种"想找人就自己来"的状态，把这三个值填回右边即可。
SPEAK_SOCIAL = float(_text("YINYUE_SPEAK_SOCIAL", "0.6") or 0.6)
SPONT_COOLDOWN_S = float(_text("YINYUE_SPONT_COOLDOWN_S", "300") or 300)
SPONTANEOUS_MIN_GAP_S = float(_text("YINYUE_SPONT_MIN_GAP_S", "1800") or 1800)
SPEAK_SOCIAL_PROACTIVE = float(_text("YINYUE_SPEAK_SOCIAL_PROACTIVE", "0.35") or 0.35)

# ---------------- 夜间静默 / 没人理就退避（2026-10-26） ----------------
# 旧链路的 quiet_hours 在 proactive_service 里，新引擎从来没接过：凌晨只要压力够，
# 她就会自己开口。现在这里是**硬门**——静默时段里不"自己找话题"；
# 被点名/@ 的回合不走这条路，所以人叫她她照样答。
_QUIET = {"hours": (23, 7), "loaded": 0.0, "src": "默认"}
SPONT_GAP_MAX_S = 14400.0    # 退避封顶 4 小时


def quiet_hours(now: float | None = None) -> tuple[int, int]:
    """静默时段（本地小时，左闭右开）。start==end 表示不静默。60 秒读一次配置。"""
    t = _time.time()
    if t - _QUIET["loaded"] > 60.0:
        _QUIET["loaded"] = t
        try:
            cfg = _json.loads((_ROOT / "configs" / "runtime.json").read_text(encoding="utf-8"))
            v = cfg.get("quiet_hours")
            if isinstance(v, (list, tuple)) and len(v) == 2:
                _QUIET["hours"] = (int(v[0]) % 24, int(v[1]) % 24)
                _QUIET["src"] = "runtime.json"
        except Exception:  # noqa: silent(配置缺失就用默认)
            pass
    return _QUIET["hours"]


def in_quiet_hours(now: float | None = None) -> bool:
    now = float(now or _time.time())
    start, end = quiet_hours(now)
    if start == end:
        return False
    h = _time.localtime(now).tm_hour
    if start < end:
        return start <= h < end
    return h >= start or h < end          # 跨夜，例如 23 -> 7


def _spont_streak(p: dict) -> int:
    return max(0, int(p.get("spont_streak") or 0))


def _spont_gap(p: dict) -> float:
    """自己开口但他没接，就把下一次的间隔翻倍：20 → 40 → 80 → 160 → 240 分钟。"""
    n = min(_spont_streak(p), 5)
    return min(SPONT_GAP_MAX_S, SPONTANEOUS_MIN_GAP_S * (2 ** n))


def _arousal(now: float) -> float:
    """昼夜：16 点最清醒(0.95)，04 点最困(0.05)。不写表格，只用一条余弦。"""
    import math
    lt = _time.localtime(now)
    h = lt.tm_hour + lt.tm_min / 60.0
    return round(0.5 + 0.45 * math.cos((h - 16.0) / 24.0 * 2 * math.pi), 3)


def _social_now(p: dict, now: float) -> float:
    base = float(p.get("social") or 0.0)
    last = float(p.get("at") or 0.0) or now
    return min(SOCIAL_CEIL, base + max(0.0, (now - last) / 60.0) * SOCIAL_PER_MIN)


def note_tick(*, spoke: bool = False, heard: bool = False, now: float | None = None) -> dict:
    """一拍结束后更新自己：说话/被搭理会让她"没那么想说话"。"""
    now = now or _time.time()
    d = load()
    p = d.setdefault("pulse", {})
    soc = _social_now(p, now)
    if heard:
        soc = max(0.0, soc - 0.55)
        p["last_heard"] = now
        p["spont_streak"] = 0          # 有人理我了，退避归零
    if spoke:
        soc = max(0.0, soc - 0.40)
        p["last_spoke"] = now
        if not heard:                  # 我说了，但这一拍没人接
            p["spont_streak"] = _spont_streak(p) + 1
    p["social"] = round(soc, 3)
    p["arousal"] = _arousal(now)
    p["at"] = now
    d["updated_at"] = now
    save(d)
    return p


def note_spont_silent(now: float | None = None) -> None:
    """自发醒来、这一拍却什么都没说：算一次「白醒」，让下一次来得更晚。

    2026-11-02：退避原来只认「说了话但没人接」（note_tick 的 spoke 分支）。
    可实测 96% 的自发醒来是「醒了、看了、没话说」——那种情况 streak 永远不涨，
    间隔永远卡在 SPONTANEOUS_MIN_GAP_S（5 分钟）。
    后果可查：10-29 一整天 353 拍自发、0 句输出、1346 次模型调用；
    7 天合计 750 拍自发 / 722 拍沉默。

    这里只动 streak，不碰 social / at —— 那是"想说话的劲儿"的时间基准，
    归零会让她本该继续积累的冲动被抹掉。
    """
    now = now or _time.time()
    d = load()
    p = dict((d.get("pulse") or {}))
    if not p:
        return
    p["spont_streak"] = _spont_streak(p) + 1
    d["pulse"] = p
    save(d)


def spontaneous_ready(now: float = None, social_floor: float = None) -> bool:
    """这一刻我想不想自己开个口（跟"有没有人找我"无关）。

    2026-10-25 修：原来只看 social >= 阈值，于是一想说话就每 10 秒醒一拍，
    连着五拍写同一条私记。加冷却：想说话这股劲儿每 5 分钟只许用一次。
    """
    now = float(now or _time.time())
    p = dict((load().get("pulse") or {}))
    if not p:
        return False
    if in_quiet_hours(now):          # 夜里不自己找话题（被叫醒的回合不受影响）
        return False
    if now - float(p.get("last_spont") or 0.0) < SPONT_COOLDOWN_S:
        return False
    if _social_now(p, now) < (SPEAK_SOCIAL if social_floor is None else float(social_floor)):
        return False
    if now - float(p.get("last_spoke") or 0.0) < _spont_gap(p):
        return False
    return True


def mark_spontaneous(now: float = None) -> None:
    """记下这一拍是我自己想说话醒的，用来压冷却。"""
    now = float(now or _time.time())
    d = load()
    p = dict((d.get("pulse") or {}))
    if not p:
        return
    p["last_spont"] = now
    d["pulse"] = p
    save(d)

def pulse_line(now: float | None = None) -> str:
    """给 prompt 的一行体感（不是标签）。"""
    now = now or _time.time()
    d = load()
    p = d.get("pulse") or {}
    soc = _social_now(p, now)
    ar = _arousal(now)
    _last = max(float(p.get("last_heard") or 0.0), float(p.get("last_spoke") or 0.0), float(p.get("at") or 0.0))
    out = []
    if soc >= 0.45:
        out.append("有点想跟人说句话（憋了 %s，程度 %.2f/1）" % (_age(_last or now), soc))
    if ar <= 0.30:
        out.append("困、反应慢（这个点：%s）" % _time.strftime("%H:%M", _time.localtime(now)))
    if not out:
        return ""
    return "- 我此刻：" + "；".join(out) + "。"

def load() -> dict:
    """读表。坏了就当空表——绝不能因为这张表让她没人格。"""
    d = _blank()
    try:
        if STATE.exists():
            raw = _json.loads(STATE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in ("mood", "doing"):
                    v = raw.get(k)
                    if isinstance(v, dict) and str(v.get("text") or "").strip():
                        d[k] = {"text": str(v["text"]).strip(), "at": float(v.get("at") or 0.0)}
                        # 2026-11-24：心境原来读回时把 evidence_refs 丢掉了——写了来源也永远显示不出来。
                        if k == "mood" and v.get("evidence_refs"):
                            d[k]["evidence_refs"] = [str(ref) for ref in v.get("evidence_refs") or ()
                                                     if str(ref)][:MOOD_REFS_MAX]
                        if k == "doing":
                            # 2026-11-04：verified 必须跟着走，否则「核对过的手头在做」
                            # 也会被 render() 挡掉（连核对过的都看不见 = 这个字段白记）。
                            d[k]["verified"] = bool(v.get("verified"))
                # 2026-11-24：心境按会话存（自我立方体的设计口径是「会话维度」）。
                # 旧的顶层 mood 保留，作为没有会话坐标时的回退。
                _moods = raw.get("moods")
                if isinstance(_moods, dict):
                    for _key, _item in _moods.items():
                        if not isinstance(_item, dict) or not str(_item.get("text") or "").strip():
                            continue
                        d["moods"][str(_key)] = {"text": str(_item["text"]).strip(),
                                                 "at": float(_item.get("at") or 0.0)}
                        if _item.get("evidence_refs"):
                            d["moods"][str(_key)]["evidence_refs"] = [
                                str(ref) for ref in _item.get("evidence_refs") or () if str(ref)
                            ][:MOOD_REFS_MAX]
                for k in ("stance", "scoped_stance", "said", "vision"):
                    v = raw.get(k)
                    if isinstance(v, dict):
                        for cid, item in v.items():
                            if isinstance(item, dict) and str(item.get("text") or "").strip():
                                d[k][str(cid)] = {"text": str(item["text"]).strip(),
                                                  "at": float(item.get("at") or 0.0)}
                                if k in ("stance", "scoped_stance"):
                                    if item.get("evidence_refs"):
                                        d[k][str(cid)]["evidence_refs"] = [
                                            str(ref)
                                            for ref in item.get("evidence_refs") or ()
                                            if str(ref)
                                        ]
                                    if item.get("stance_scope"):
                                        d[k][str(cid)]["stance_scope"] = [
                                            str(value)
                                            for value in item.get("stance_scope") or ()
                                            if str(value)
                                        ]
                                if k == "scoped_stance":
                                    d[k][str(cid)]["who"] = str(item.get("who") or "")
                d["updated_at"] = float(raw.get("updated_at") or 0.0)
                _p = raw.get("pulse")
                if isinstance(_p, dict):
                    d["pulse"] = {"social": float(_p.get("social") or 0.0),
                                  "arousal": float(_p.get("arousal") or 0.5),
                                  "at": float(_p.get("at") or 0.0),
                                  "last_spoke": float(_p.get("last_spoke") or 0.0),
                                  "last_heard": float(_p.get("last_heard") or 0.0)}
    except Exception as exc:  # noqa: BLE001 - 表坏了退回空表
        _warn("selfstate:load_failed", err=repr(exc))
        d = _blank()
    _p_raw = None
    try:
        _p_raw = _json.loads(STATE.read_text(encoding="utf-8")).get("pulse") if STATE.exists() else None
    except Exception:  # noqa: BLE001
        _p_raw = None
    if isinstance(_p_raw, dict):
        _p = dict(_p_raw)
        _p.setdefault("last_spont", 0.0)
        d["pulse"] = _p
    return d

def save(d: dict) -> bool:
    # 落盘只走 jsonio：唯一临时名 + rename + 退避重试都在那一层，本模块不再自己拼 .tmp。
    try:
        ok = atomic_write_json(STATE, d, indent=2)
    except Exception as exc:  # noqa: BLE001
        _warn("selfstate:save_failed", err=repr(exc))
        return False
    if not ok:
        _warn("selfstate:save_failed", err="write_failed")
    return ok


def _clip(s, n) -> str:
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    head = s[:n]
    return head[: n - 1] + "…"


def _clip_tail(s, n) -> str:
    """指令类文本超长时保住结尾那句（结尾常是做法），中间省掉。

    2026-10-28：push_vision_hint 以前硬切到 140 字，她读到的是「…就输…」。
    """
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    head, tail = s[: max(20, n - 60)], s[-60:]
    for _i, _ch in enumerate(tail):
        if _ch in "。！？；，":
            tail = tail[_i + 1:].lstrip()
            break
    return head + "…" + tail




def _age(at) -> str:
    try:
        s = int(_time.time() - float(at or 0))
    except Exception:  # noqa: BLE001
        return ""
    if s < 0:
        return ""
    if s < 90:
        return "刚刚"
    if s < 3600:
        return "%d 分钟前" % (s // 60)
    if s < 86400:
        return "%d 小时前" % (s // 3600)
    return "%d 天前" % (s // 86400)


def _scope_coords(scope) -> tuple[str, str, str] | None:
    """(platform, account_id, chat_id)；不完整/不是三段就返回 None（不猜账号）。"""
    if not isinstance(scope, (tuple, list)) or len(scope) != 3:
        return None
    coords = tuple(str(v or "").strip() for v in scope)
    return coords if all(coords) else None


def _scope_key(scope) -> str:
    return _json.dumps(list(scope), ensure_ascii=False)


def _entry_chat_id(key: str) -> str:
    if key.startswith("["):
        try:
            coords = _scope_coords(_json.loads(key))
        except ValueError:
            return key
        if coords:
            return coords[2]
    return key


def _record_slot(chat_id: str, scope) -> tuple[str, tuple | None]:
    """said/vision 的落盘键：完整且与 chat_id 一致的 scope 才升级为 scoped 键。"""
    chat_id = str(chat_id or "").strip()
    coords = _scope_coords(scope)
    if coords and coords[2] == chat_id:
        return _scope_key(coords), coords
    return chat_id, None


def stance_coordinates(scope) -> tuple[str, ...]:
    """Stable conversation and current speaker for a subjective note."""
    if scope and all((scope.platform, scope.account_id, scope.chat_id, scope.sender_id)):
        return scope.platform, scope.account_id, scope.chat_id, scope.sender_id
    return ()


def _stance_source(refs: tuple[str, ...], coords: tuple[str, ...]) -> str:
    """The latest usable words from the same speaker behind a saved impression."""
    if not refs:
        return ""
    from core.conversation.addressing import has_quote_prefix, strip_quote_block
    from core.terminal import shadow

    newest = ("", "")
    try:
        store = shadow.get_default_store()
        for ref in refs:
            event = store.get(ref)
            if event is None or (event.platform, event.bot_account_id, event.chat_id,
                                 event.actor_account_id or event.actor_person_id) != coords:
                continue
            text = str(event.content or "").strip()
            if has_quote_prefix(text):
                body = strip_quote_block(text)
                text = body if body != text else ""
            when = event.occurred_at.isoformat()
            if text and when >= newest[0]:
                newest = (when, _clip(text, 80))
    except Exception as exc:
        _warn("selfstate_stance_source_unavailable", err=repr(exc)[:120])
    return newest[1]


def _mood_source(refs, clip: int = 60) -> str:
    """从这批证据编号里找出可读的最新一条原话（心境是因为什么来的）。"""
    if not refs:
        return ""
    newest = ("", "")
    try:
        from core.conversation.addressing import has_quote_prefix, strip_quote_block
        from core.terminal import shadow

        store = shadow.get_default_store()
        for ref in refs:
            event = store.get(ref)
            if event is None:
                continue
            text = str(event.content or "").strip()
            if has_quote_prefix(text):
                body = strip_quote_block(text)
                if body == text:
                    continue
                text = body
            when = event.occurred_at.isoformat()
            if text and when >= newest[0]:
                newest = (when, _clip(text, clip))
    except Exception as exc:
        _warn("selfstate_mood_source_unavailable", err=repr(exc)[:120])
        return ""
    return newest[1]


def update(
    mood="",
    doing="",
    chat_id="",
    note="",
    who="",
    *,
    stance_scope: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    mood_scope: tuple[str, ...] = (),
) -> str:
    """改表。返回一句人话（给她看，也写进账）。"""
    d = load()
    now = _time.time()
    did = []
    def _same(cur, text):
        # 2026-10-25：和表里一模一样就不写。以前每拍都写盘，账里一屏 mysail 空转。
        return bool(cur) and str(cur.get("text") or "").strip() == str(text).strip()
    _mood_key = _json.dumps([str(x) for x in mood_scope], ensure_ascii=False) if mood_scope else ""
    _mood = d["moods"].setdefault(_mood_key, {"text": "", "at": 0.0}) if _mood_key else d["mood"]
    if str(mood or "").strip():
        _t = _clip(mood, _MOOD_MAX)
        _refs = list(dict.fromkeys(
            str(ref) for ref in (evidence_refs or ()) if str(ref)
        ))[:MOOD_REFS_MAX]
        _old_refs = [str(ref) for ref in (_mood.get("evidence_refs") or ()) if str(ref)]
        # 同一句心境再写一次说明「我现在还是这样」，要前移 at；证据变了也算变化。
        if not _same(_mood, _t) or _refs != _old_refs:
            _mood.clear()
            _mood.update({"text": _t, "at": now})
            if _refs:
                _mood["evidence_refs"] = _refs
            did.append("心境")
    if str(doing or "").strip():
        _t = _clip(doing, _DOING_MAX)
        if not _same(d["doing"], _t):
            # 2026-11-04：她替自己写「手头在做」是不受约束的 —— 那次她写「把作业群里那几条
            # 自己编的报到安排撤回来了」，可那几条她根本没发过；下一拍这句被当成事实喂回去，
            # 她就去「撤回」一件不存在的事。所以模型自述只算「没核对过的自述」，
            # render() 不再把它当事实展示（见 self_state.json 里 2026-11-04 那条）。
            d["doing"] = {"text": _t, "at": now, "verified": False}
            did.append("手头在做的事")
    if (stance_scope or str(chat_id or "").strip()) and str(note or "").strip():
        _cid, _t = str(chat_id).strip(), _clip(note, _NOTE_MAX)
        _entries = d["scoped_stance"] if stance_scope else d["stance"]
        _key = _json.dumps(stance_scope, ensure_ascii=False) if stance_scope else _cid
        _current = _entries.get(_key) or {}
        _old_refs = [str(ref) for ref in (_current.get("evidence_refs") or ()) if str(ref)]
        _new_refs = list(dict.fromkeys(
            str(ref) for ref in (evidence_refs or ()) if str(ref)
        ))
        # 截断过的旧看法无法核对全文。只有能辨认是同一条内容时才沿用旧依据。
        _note_norm = " ".join(str(note or "").split())
        _note_same = _same(_current, _note_norm) and not (
            len(_note_norm) == _NOTE_MAX and _note_norm.endswith("…")
        )
        _refs = (list(dict.fromkeys([*_old_refs, *_new_refs]))
                 if _note_same else _new_refs)
        if (not _same(_current, _t)
                or _refs != _old_refs
                or (stance_scope and list(stance_scope) != list(_current.get("stance_scope") or ()))):
            _entries[_key] = {"text": _t, "at": now}
            if _refs:
                _entries[_key]["evidence_refs"] = _refs
            if stance_scope:
                _entries[_key]["who"] = str(who or stance_scope[-1])
                _entries[_key]["stance_scope"] = list(stance_scope)
            did.append("对「%s」的看法" % (str(who or _cid).strip() or "他"))
    if not did:
        return ""
    d["updated_at"] = now
    # 只留最近几条，别让表越滚越大
    if d.get("moods"):
        d["moods"] = dict(sorted(d["moods"].items(), key=lambda kv: kv[1].get("at") or 0.0,
                                 reverse=True)[:_KEEP_MOOD])
    for k, keep in (("stance", _KEEP_STANCE), ("scoped_stance", _KEEP_STANCE), ("said", _KEEP_SAID)):
        items = sorted(d[k].items(), key=lambda kv: kv[1].get("at") or 0.0, reverse=True)[:keep]
        d[k] = dict(items)
    if not save(d):
        return ""
    _tail = "。"
    if str(note or "").strip() and (stance_scope or chat_id):
        _tail += ("这份看法只在同一会话的同一个人发言时读回。" if stance_scope else "这份看法没有绑定当前发言人，只保留在状态记录里。")
    if "手头在做的事" in did:
        _tail += ("手头在做的事没核对过，不会出现在你眼前那张表里 —— "
                 "要它出现，得先真有那件事的记录。")
    return "已记下：" + "、".join(did) + _tail


SAID_VERIFY_WINDOW_S = 900


def _sent_for_real(chat_id: str, text: str, *, window_s: int = SAID_VERIFY_WINDOW_S,
                   conn=None, scope=None) -> bool:
    """这句是不是真进了投递队列（或已发出）。对不上就说明她并没有说过。

    2026-11-04：`self_state.json` 里那条「把作业群那几条自己编的报到安排撤回来了」是模型
    自己写的，`messages` 与 `outbound_queue` 里都没有对应记录；它被当成事实喂回下一拍，
    她于是去「撤回」一件没发生的事。凡「我说过什么」都必须先对账。
    """
    body = " ".join(str(text or "").split())
    if not chat_id or not body:
        return False
    coords = _scope_coords(scope)
    own = conn is None
    try:
        if own:
            from db import world_db as _w   # 懒导入：这个模块导入期不许开库
            conn = _w.conn()
        sql = ("select count(*) from outbound_queue where chat_id = ? "
               "and state in ('pending', 'sending', 'sent') "
               "and coalesce(json_extract(payload, '$.text'), '') = ? "
               "and coalesce(updated_ts, created_ts, 0) >= cast(strftime('%s', 'now') as integer) - ?")
        args = [chat_id, body, int(window_s)]
        if coords:
            # 同一个群在不同 bot 账号下是同 chat_id：只认这个账号自己发的那条。
            sql += " and platform = ? and account_id = ? and state = 'sent' and ack = 'confirmed'"
            args.extend(coords[:2])
        cur = conn.execute(sql, tuple(args))
        return int((cur.fetchone() or [0])[0] or 0) > 0
    except Exception as exc:  # noqa: BLE001 - 对账失败按「没说过」处理
        _warn("selfstate.said_check_failed", chat_id=chat_id, err=repr(exc)[:80])
        return False


def touch_said(chat_id: str, text: str, *, check: bool = True, conn=None,
               scope=None) -> bool:
    """她自己说出去的话，自动记进表里（不用她手动写）。

    `check=True` 时先对账（见 `_sent_for_real`）：对不上就不写、只记一条 warn ——
    宁可这句没记上，也不许她把「没说过的话」当成说过的。表情/文件那种由回执生成的
    摘要行（`[表情] …` / `文件：…`）本就对不上队列正文，调用方传 `check=False`。

    `scope=(platform, account_id, chat_id)` 时按账号隔离落盘，并在对账时带上账号；
    不完整或与 chat_id 不符的 scope 退回老式 chat_id 键，不猜账号。
    """
    chat_id = str(chat_id or "").strip()
    summary = _clip(text, _SAID_MAX)
    if not chat_id or not summary:
        return False
    key, coords = _record_slot(chat_id, scope)
    if check and not _sent_for_real(chat_id, text, conn=conn, scope=coords):
        _warn("selfstate.said_unverified", chat_id=chat_id, chars=len(text))
        return False
    d = load()
    d["said"][key] = {"text": summary, "at": _time.time()}
    d["said"] = dict(sorted(d["said"].items(), key=lambda kv: kv[1].get("at") or 0.0,
                            reverse=True)[:_KEEP_SAID])
    save(d)
    return True


def push_vision_hint(chat_id: str, text: str, *, at: float | None = None,
                     scope=None) -> None:
    """她"事后才看清的一张图"记进表里，下一拍自己读得到。

    2026-10-27：`agents/late_vision_followup.py` 补的那句话以前发 `PROACTIVE_TRIGGER`，
    而那条路接的是 2026-10-24 就退役的旧大脑（日志 `trigger_failed: 旧大脑
    ConversationalAgent 已退役…`）—— 于是"事后才看清"永远到不了她眼前。现在把事实
    记在这张表里，由 loop 每拍自己读。

    `scope=(platform, account_id, chat_id)` 时按账号隔离；不完整/不符则退回老式键。
    """
    chat_id = str(chat_id or "").strip()
    text = _clip_tail(text, _VISION_CHARS)
    if not chat_id or not text:
        return
    key, _ = _record_slot(chat_id, scope)
    d = load()
    d.setdefault("vision", {})[key] = {"text": text, "at": float(at or _time.time())}
    d["vision"] = dict(sorted(d["vision"].items(), key=lambda kv: kv[1].get("at") or 0.0,
                              reverse=True)[:_VISION_MAX])
    save(d)


def _conversation_items(items: dict, key: str | None):
    if key is None:
        return items.items()
    if key and key in items:
        return ((key, items[key]),)
    return ()


def _live_vision(d: dict, key: str | None) -> list:
    """还没过期的「事后才看清」（20 分钟；过期就当没这回事）。"""
    now = _time.time()
    out = [(cid, item) for cid, item in _conversation_items(d.get("vision") or {}, key)
           if str(item.get("text") or "") and now - float(item.get("at") or 0.0) <= _VISION_TTL_S]
    return sorted(out, key=lambda kv: kv[1].get("at") or 0.0, reverse=True)[:_VISION_MAX]


def render(
    label=None,
    budget: int = 900,
    *,
    stance_scope: tuple[str, ...] | None = None,
    detail: dict | None = None,
    conversation_scope: tuple[str, ...] | None = None,
) -> str:
    """渲染成给模型看的一段事实。label(chat_id)->名字，可缺省。

    `conversation_scope=(platform, account_id, chat_id)` 独立于 stance_scope：
    隔离模式只使用完整会话坐标。缺省时供概览及旧调用方使用。
    """
    d = load()
    if conversation_scope is None:
        conversation_key = stance_scope[2] if stance_scope and len(stance_scope) > 2 else None
    else:
        coords = _scope_coords(conversation_scope)
        conversation_key = _scope_key(coords) if coords else ""
    def nm(cid):
        try:
            n = label(cid) if label else ""
        except Exception:  # noqa: BLE001
            n = ""
        return str(n or cid)

    def stance_quality(item):
        refs = tuple(
            str(ref) for ref in (item.get("evidence_refs") or ()) if str(ref)
        )
        if not refs:
            return "unrecorded", ()
        coords = tuple(item.get("stance_scope") or stance_scope or ())
        if len(coords) < 3:
            return "unknown", ()
        from core.context.cognitive_commit import Scope
        from core.memory import belief_commit

        scope = Scope(str(coords[0]), str(coords[1]), str(coords[2]))
        states = [
            belief_commit.evidence_state(ref, scope)
            for ref in refs
        ]
        live_refs = tuple(ref for ref, state in zip(refs, states) if state == "alive")
        if live_refs:
            return "alive", live_refs
        return ("dead" if all(state == "dead" for state in states) else "unknown"), ()

    def stance_suffix(quality):
        if quality == "unknown":
            return " [依据未核实]"
        if quality == "unrecorded":
            return " [来源未记录]"
        return ""

    uncertain_stances = (
        detail.setdefault("uncertain_memory_claims", [])
        if detail is not None else None
    )

    def record_stance(item, quality):
        if uncertain_stances is None or quality not in {"unknown", "unrecorded"}:
            return
        content = str(item.get("text") or "").strip()
        if not content:
            return
        uncertain_stances.append({
            "content": content,
            "state": "unverified" if quality == "unrecorded" else "unknown",
            "source_event_ids": list(item.get("evidence_refs") or ()),
        })

    lines = []
    _pl = pulse_line()
    if _pl:
        lines.append(_pl)
    _mood_item = d.get("moods", {}).get(conversation_key) if conversation_key else None
    if not (_mood_item and str(_mood_item.get("text") or "").strip()):
        _mood_item = d["mood"]
    if _mood_item["text"]:
        _at = float(_mood_item.get("at") or 0.0)
        _src = _mood_source(_mood_item.get("evidence_refs") or ())
        _tail = ("；那会儿是因为：" + _json.dumps(_src, ensure_ascii=False)) if _src else ""
        if _at and _time.time() - _at <= MOOD_FRESH_S:
            lines.append("- 心境：%s（%s）%s" % (_mood_item["text"], _age(_at), _tail))
        else:
            lines.append("- 之前记下的心情：%s（%s，那会儿已经过去，现在不一定还是这样）%s"
                         % (_mood_item["text"], _age(_at), _tail))
    # 2026-11-04：只展示核对过的。模型自述的「手头在做」没有任何记录能对上，
    # 展示它等于把「她自己编的一句话」升级成「事实」，再喂回去当记忆。
    if d["doing"]["text"] and d["doing"].get("verified"):
        lines.append("- 手头在做：%s（%s）" % (d["doing"]["text"], _age(d["doing"]["at"])))
    _vs = _live_vision(d, conversation_key)
    if _vs:
        # 2026-10-27: 原来是「我刚看清的图（…提不提她自己判断）」，第三人称旁白口吻，
        # 她读完说「这行字是图里带的，还是系统替我写的，我分不清」。事实没变，
        # 但要说成她自己眼睛看到的画面 —— 描述是她的视觉，不是系统递来的注记。
        lines.append("- 我眼睛里看见的画面（我自己的眼睛看到的，不是别人写的注、"
                     "也不是系统替我说的话；要不要提我自己定）：")
        for cid, item in _vs:
            lines.append("  · %s 那边那张图 —— %s（%s）"
                         % (nm(_entry_chat_id(cid)), item["text"], _age(item["at"])))
    if stance_scope is None:
        items = [(item.get("who") or "", item) for item in d["scoped_stance"].values()]
        items.extend((nm(cid), item) for cid, item in d["stance"].items())
        qualified = []
        for who, item in items:
            quality, _ = stance_quality(item)
            if quality != "dead":
                qualified.append((who, item, quality))
                record_stance(item, quality)
        items = qualified
        if items:
            lines.append("- 我对这几个人的看法：")
            for who, item, quality in sorted(items, key=lambda row: row[1].get("at") or 0.0, reverse=True)[:4]:
                lines.append(
                    "  · %s —— %s（%s）%s"
                    % (who, item["text"], _age(item["at"]), stance_suffix(quality))
                )
    elif stance_scope:
        item = d["scoped_stance"].get(_json.dumps(stance_scope, ensure_ascii=False))
        quality, live_refs = stance_quality(item) if item else ("dead", ())
        if item and quality != "dead":
            record_stance(item, quality)
            line = (
                "- 我当时对当前发言人的看法（可修正）：%s（%s）%s"
                % (item["text"], _age(item["at"]), stance_suffix(quality))
            )
            source = _stance_source(live_refs, stance_scope)
            if source:
                line += "。当时对方说：" + _json.dumps(source, ensure_ascii=False)
            lines.append(line)
    said = sorted(_conversation_items(d["said"], conversation_key),
                  key=lambda kv: kv[1].get("at") or 0.0, reverse=True)[:3]
    if said:
        lines.append("- 我刚跟他们说过：")
        for cid, item in said:
            lines.append("  · %s —— 「%s」（%s）"
                         % (nm(_entry_chat_id(cid)), item["text"], _age(item["at"])))

    if not lines:
        return ("[我自己的状态]（这张表还是空的——没有人替我填，只能我自己写）\n"
                "- 想留下一个「我此刻」，就调 myself：mood=现在的心情，doing=手头在做什么，"
                "about=当前发言人 + note=我怎么看这个人。看法只在同一会话的同一个人发言时读回。")
    body = "\n".join(lines)
    if len(body) > budget:
        body = body[: budget - 1] + "…"
    return ("[我自己的状态]（这张表是我自己写的，不是别人塞给我的）\n" + body)
