# -*- coding: utf-8 -*-
"""她自己的表情（2026-11-01）

主人手工登记的几张图，存在 ``data/platforms/<平台>/<账号>/emoji/registry.json``。
``db/sticker_store`` 是它的读取层，``core/execution/wx_sticker_sender`` 是发送端
（开表情面板、模板匹配、**只点一次**、点完验）。

这一层只负责三件小事，别的地方不要直接碰 sticker_store：

1. ``available()`` —— 现在真的发得出去的有哪几张（只有登记过、模板图还在的才算）；
2. ``note()`` —— 塞进她这一拍的那一行「我手上有哪些表情」；
3. ``gate()`` —— 该不该让她现在发（群里先不发、同一张 30 分钟不重发）。

口径来自 2026-11-01 定稿：只在情绪位发；池子小就先不报；同一会话同一张
30 分钟内不重发；一张没登记过就当作没有。
"""
from __future__ import annotations

import os
import time

import structlog

log = structlog.get_logger()

# 同一会话、同一张图，这段时间内不许重发
COOLDOWN_S = 30 * 60.0
# 池子小于这个数就不在提示词里露出来（她只有两三张时会一直重复同一张）
MIN_POOL = 4
#: 表情排队等发送的有效期（秒）：desk 挂了也不会永远挂着这张图
QUEUE_TTL_S = 30 * 60

def _root() -> str:
    """仓库根目录（本文件在 loop/ 下）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_SENT: dict = {}          # (chat_id, key) -> ts
_CACHE: dict = {"at": 0.0, "items": {}}
_CACHE_S = 60.0


def available(platform: str = "wechat", *, force: bool = False) -> dict:
    """{key(她看到的名字) -> Sticker}，只含真的能发出去的。"""
    now = time.time()
    if not force and _CACHE["items"] and now - _CACHE["at"] < _CACHE_S:
        return _CACHE["items"]
    items: dict = {}
    try:
        from db import sticker_store

        for st in sticker_store.sendable_stickers(platform, None):
            key = (st.emotion or st.label or "").strip()
            if key and key not in items:
                items[key] = st
    except Exception as exc:  # noqa: BLE001 - 读不到就当没有表情，别让她这一拍炸
        from safelog import warn
        warn("stickers.available_failed", error=repr(exc)[:160])
        items = {}
    _CACHE.update({"at": now, "items": items})
    return items


def note(platform: str = "wechat") -> str:
    """提示词里那一行；池子太小就返回空串（不露 = 不提，免得她反复用一个）。"""
    items = available(platform)
    if len(items) < MIN_POOL:
        return ""
    keys = list(items.keys())
    return ("我手上有这几张表情（想表达情绪时才用；同一张别老发，也别用它代替说话）："
            + " / ".join(keys[:12]))


# ---------------------------------------------------------------- 情绪位 / 占比
#: 过去这段时间里，"表情"最多占她开口总数的这个比例（定稿 15–25%）
RATIO_MAX = 0.25
RATIO_WINDOW_S = 6 * 3600.0
#: 样本太少时不做占比判断（一共才说两三句，比例没有意义）
RATIO_MIN_SAMPLES = 8

#: 一看就是在问她事情/要她办事的字眼 —— 这种时候发表情等于敷衍
_ASK_MARKS = ("？", "?", "怎么", "为什么", "为何", "是不是", "能不能", "可不可以",
              "帮我", "帮我", "请", "麻烦", "几点", "多少", "哪里", "哪个", "有没有",
              "教", "告诉", "查", "写", "做", "去", "什么时候", "怎么样")
#: 一看就是情绪/招呼的 —— 这种时候发表情是对的
_MOOD_MARKS = ("哈", "呜呜", "呜", "嘤", "笑死", "难受", "累", "困", "烦", "开心", "好耶",
               "抱", "晚安", "早安", "在吗", "想你", "气死", "哭", "么么", "贴贴", "awsl")


def last_inbound(chat_id: str = "") -> dict:
    """这个会话最新一条"对方说的话"（用来判是不是在问她事情）。读不到就返回 {}。"""
    import sqlite3

    db = os.path.join(_root(), "data", "world.db")
    try:
        with sqlite3.connect(db, timeout=3.0) as c:
            c.row_factory = sqlite3.Row
            if chat_id:
                r = c.execute("select text, kind, ts_utc from inbound_event where chat_id=?"
                              " and ifnull(is_self,0)=0 order by id desc limit 1", (chat_id,)).fetchone()
            else:
                r = c.execute("select text, kind, ts_utc from inbound_event where"
                              " ifnull(is_self,0)=0 order by id desc limit 1").fetchone()
        return dict(r) if r else {}
    except Exception as exc:  # noqa: BLE001 - 判不出来就当没有上下文，别拦她
        from safelog import warn
        warn("stickers.last_inbound_failed", error=repr(exc)[:160])
        return {}


def is_emotional(text: str, kind: str = "text") -> tuple:
    """这句是不是"情绪位"。返回 (是否情绪位, 说明)。

    判据只有两条，都能说人话：
    1. 对方发的是图/表情/很短的一句感叹 —— 是情绪位；
    2. 对方在提问/让人办事（含"？""怎么""帮我"这类）—— 不是情绪位。
    """
    t = str(text or "").strip()
    k = str(kind or "text")
    if k in ("image", "sticker", "emoji", "video"):
        return True, "对方发的是图/表情"
    if not t:
        return False, "不知道对方说了什么"
    if any(m in t for m in _ASK_MARKS):
        return False, "对方在问你事情/让你做事，先用话说"
    if any(m in t for m in _MOOD_MARKS):
        return True, "对方在情绪上"
    if len(t) <= 6:
        return True, "对方就一句短的"
    return False, "这句更像正经话，不是情绪位"


def _ledger_counts(now: float | None = None) -> tuple:
    # (近窗口内她说的句数, 其中的表情数)。读 data/loop/ledger-*.jsonl。
    # 2026-11-01（PIT-071 纪律）：账本里解不开的行不许静默跳过 —— 占比统计会悄悄偏小，
    # 于是"她是不是只会发表情"这个判断就没了依据。这里只计数、最后一条日志说清楚。
    import json as _json

    now = time.time() if now is None else now
    cut = now - RATIO_WINDOW_S
    folder = os.path.join(_root(), "data", "loop")
    says = stks = 0
    bad_lines = bad_files = 0
    if not os.path.isdir(folder):
        return 0, 0
    for name in sorted(os.listdir(folder)):
        if not (name.startswith("ledger-") and name.endswith(".jsonl")):
            continue
        try:
            fh = open(os.path.join(folder, name), encoding="utf-8")
        except Exception as exc:
            bad_files += 1
            log.warning("stickers.ledger_open_failed", file=name, error=repr(exc)[:120])
            continue
        with fh:
            for line in fh:
                try:
                    r = _json.loads(line)
                except Exception:
                    bad_lines += 1
                    continue
                act = str(r.get("act") or "")
                if act not in ("say", "sticker"):
                    continue
                at = str(r.get("at") or "")
                try:
                    ts = time.mktime(time.strptime(at[:19], "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    bad_lines += 1
                    continue
                if ts < cut:
                    continue
                if str(r.get("result") or "") not in ("sent", "queued"):
                    continue
                if act == "say":
                    says += 1
                else:
                    stks += 1
    if bad_lines or bad_files:
        log.warning("stickers.ledger_dirty", bad_lines=bad_lines, bad_files=bad_files,
                    hint="账本里有解不开的行，表情占比会偏小，别拿它当准数")
    return says, stks


def ratio_state(now: float | None = None) -> tuple:
    # (是否还能发, 说明)。表情占比超过上限就先说话，别刷图。
    says, stks = _ledger_counts(now)
    total = says + stks
    if total < RATIO_MIN_SAMPLES:
        return True, ""
    r = stks / float(total)
    if r > RATIO_MAX:
        return False, "这一阵我说的话里图太多了（%d 张 / 共 %d 次开口），先用嘴说几句" % (stks, total)
    return True, ""


def _is_group(cid: str) -> bool:
    c = str(cid or "")
    return c.endswith("@chatroom") or c.startswith("group:")


def meta_of(key: str) -> dict:
    """这张表情的标注（feel / intensity / scope / taboo / members）。读不到就是 {}。"""
    import glob as _glob
    import json as _json

    key = str(key or "").strip()
    if not key:
        return {}
    pat = os.path.join(_root(), "data", "platforms", "*", "*", "emoji", "registry.json")
    for fp in _glob.glob(pat):
        try:
            with open(fp, encoding="utf-8") as f:
                reg = _json.load(f) or {}
        except Exception as exc:
            log.warning("stickers.meta_read_failed", file=os.path.basename(fp),
                        error=repr(exc)[:120], hint="这份 registry 读不了，这张图的标注就当作没有")
            continue
        v = reg.get(key)
        if isinstance(v, dict):
            return v
    return {}


def gate(chat_id: str, key: str, *, now: float | None = None,
         check_context: bool = True) -> tuple:
    """能不能现在发这张。返回 (ok, 理由)。

    顺序：群聊 → 有没有这张 → 冷却 → **是不是情绪位** → **最近表情占比**。
    后两条是 2026-11-01 定稿里"只在情绪位发、占比 15–25%"的落地。
    """
    now = time.time() if now is None else now
    if not key:
        return False, "没说发哪张"
    cid = str(chat_id or "")
    if cid.endswith("@chatroom") or cid.startswith("group:"):
        # 定稿：群里先不发表情（强度/分寸还没验过）
        return False, "群里先不发表情，只发字"
    items = available()
    if key not in items:
        names = "、".join(list(items.keys())[:8]) or "（现在一张都没有）"
        return False, "手上没有这张表情（能发的是：%s）" % names
    _m = meta_of(key)
    _scope = str(_m.get("scope") or "any").strip().lower()
    if _scope == "owner" and _is_group(cid):
        return False, "这张是只对主人发的，群里不发"
    if _scope == "group" and not _is_group(cid):
        return False, "这张是给群里用的，私聊先别发"
    if bool(_m.get("taboo")) and _is_group(cid):
        return False, "这张不适合在群里发"
    last = _SENT.get((cid, key))
    if last and now - last < COOLDOWN_S:
        left = int((COOLDOWN_S - (now - last)) / 60)
        return False, "这张刚发过，再等 %d 分钟" % max(1, left)
    if check_context:
        _in = last_inbound(cid)
        _ok_e, _why_e = is_emotional(_in.get("text") or "", _in.get("kind") or "text")
        if not _ok_e:
            return False, _why_e
        _ok_r, _why_r = ratio_state(now)
        if not _ok_r:
            return False, _why_r
    return True, ""


def mark_sent(chat_id: str, key: str, *, now: float | None = None) -> None:
    _SENT[(str(chat_id or ""), str(key or ""))] = time.time() if now is None else now


def account_of(key: str) -> str:
    items = available()
    st = items.get(key)
    return str(getattr(st, "account_id", "") or "") if st else ""
