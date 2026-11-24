"""几句“现场补充”（施工 Step 7）。

三段都遵守同一条规矩：**没有内容就不出现**（不许只留一个空标题），
因为空标题会让她以为自己“看过”了，也让日志里看起来像有数据。

- `room_line()`：群里都有谁（旁路表 `room_members`，来自微信解密库的成员关系）。
- `recall_line()`：从她自己的小本本/日记里按关键词捞一两句旧事。
- `chat_note_line()`：这个群「平时是什么样」（`person_notes` 里 `person_key=''`
  的群基线；2026-11-02 接上，之前一直没人渲染）。
- `open_loop_line()`：她手上还没了结的事（世界模型里 `status='open'` 的 `Intention`；
  2026-11-03 接上，之前这本账只有写和执行的半边、没人读）。
"""

from __future__ import annotations

import re
from typing import Iterable

from core.context import room_members, situation_store
from core.context.goal_source import goal_source_state, source_label
from core.context.situation import open_intentions
from core.infra import envflags
from loop import history, memo

try:  # safelog 是仓库的统一告警出口；没有它也不许把主流程带崩
    from safelog import warn
except Exception:  # pragma: no cover
    def warn(event: str, **kw) -> None:  # type: ignore[misc]
        return None


RECALL_MAX = None  # 2026-10-31：主人要求取消上限——None = 一段旧事原样给她，不截

_CJK = re.compile(r"[\u4e00-\u9fff]{2,6}")


def _roster_rows(chat_id: str) -> list[tuple[str, str, int]]:
    con = room_members.connect()
    try:
        cur = con.execute(
            "select wxid, ifnull(nick,''), ifnull(is_owner,0) from room_member "
            "where chat_id=? and ifnull(still_listed,1)=1",
            (chat_id,))
        return [(str(a), str(b), int(c or 0)) for a, b, c in cur.fetchall()]
    finally:
        con.close()


NOTE_CHARS = 46          # 每人档案截到多少字
MAX_LISTED = 8           # 群里最多列几个人（其余只报数量）
GROUP_CAP = 420          # 整段上限，别把一屏撑爆


def _clip(s: str, n: int) -> str:
    t = " ".join(str(s or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _is_group(chat_id: str) -> bool:
    """微信群 …@chatroom，QQ 群 group:…。判不出来当私聊。"""
    c = str(chat_id or "")
    return c.endswith("@chatroom") or c.startswith("group:")


def _owner_ids() -> set[str]:
    """主人 wxid 集合。

    2026-11-02：**不信 `room_member.is_owner`** ——实测 7 个群里有 6 个标错
    （「可爱的天使」「幼儿园第一老太」和一串空白都被标成主人）。
    真正的来源是 `.env` 的 `WECHAT_OWNER_WXIDS`。
    """
    raw = " ".join((envflags.text("WECHAT_OWNER_WXIDS"),
                    envflags.text("YINYUE_OWNER_WXIDS")))
    return {x.strip() for x in re.split(r"[,;，；\s]+", raw) if x.strip()}


def _notes_by_person(chat_id: str = "") -> dict[str, list[str]]:
    """{wxid: [档案片段，最新的在前]}。读不到就返回空表——绝不能因为这张表让她这一拍炸。"""
    try:
        from core.context import person_notes as _pn

        rows = _pn.list_notes(chat_id=str(chat_id or ""), active_only=True)
        from . import ports

        scope = ports.current_read_scope()
    except Exception as exc:  # noqa: BLE001 - 档案读不到只是少一段，不该带崩
        warn("brief_notes_failed", error=repr(exc)[:80])
        return {}
    rows = sorted(rows or [], key=lambda r: str((r or {}).get("updated_at") or ""), reverse=True)
    out: dict[str, list[str]] = {}
    for r in rows:
        row_chat = str((r or {}).get("chat_id") or "").strip()
        if chat_id and row_chat and row_chat != str(chat_id):
            continue
        if scope is not None:
            row_platform = str((r or {}).get("platform") or "").strip()
            row_account = str((r or {}).get("account_id") or "").strip()
            if row_platform and row_platform != scope.platform:
                continue
            if row_account and row_account != scope.account_id:
                continue
        k = str((r or {}).get("person_key") or "").strip()
        v = str((r or {}).get("content") or "").strip()
        if k and v:
            out.setdefault(k, []).append(v)
    return out


def _merge_notes(parts: list[str], limit: int) -> str:
    """同一个人的多条档案**只取最新的那条**。

    实测同一个事实会存两版（「计算机25-4班，前学委（已卸任），女」和
    「P5是计算机25-4班的前学委，期末卸任，女。」），两条拼起来就是复读。
    把多条合成一条是「压缩」（清单 2.2）该做的事，渲染层不猜。
    """
    if not parts:
        return ""
    # 数据里常写「身份：P5是…」，直接念出来像念档案，把前缀剥掉。
    return _clip(re.sub(r"^身份[：:]\s*", "", parts[0]), limit)


def _group_line(cid: str, who: str) -> str:
    """群聊：群里有谁。有档案的排前面，没档案的只给名字。"""
    rows = _roster_rows(cid)
    if not rows:
        return ""
    notes = _notes_by_person(cid)
    owners = _owner_ids()
    total = len(rows)
    owner_in = any(wxid in owners for wxid, _, _ in rows)
    _self_wxid = ""
    _self_names: set[str] = set()
    try:
        from core.config import settings as _settings
        _self_wxid = str(_settings.config.wechat.get_self_wxid() or "").strip()
    except Exception:  # noqa: BLE001 - 标不出“我”只少一个标记，不该拖垮整段名单
        _self_wxid = ""
    if not _self_wxid:
        try:
            from loop import ports as _ports
            _self_names = {str(n) for n in _ports.me_aliases() if str(n)}
        except Exception:  # noqa: BLE001 - 名单取不到就不标，绝不猜
            _self_names = set()
    _self_rows = ({wxid for wxid, _, _ in rows if wxid == _self_wxid}
                  if _self_wxid else
                  {wxid for wxid, nick, _ in rows if nick in _self_names})
    _self_row = next(iter(_self_rows)) if len(_self_rows) == 1 else ""
    ordered = sorted(rows, key=lambda t: (0 if t[0] in owners else 1,
                                          0 if t[0] in notes else 1,
                                          str(t[1] or "")))
    lines: list[str] = []
    for wxid, nick, _ignored in ordered[:MAX_LISTED]:
        d = notes.get(wxid)
        tail = ("：" + _merge_notes(d, NOTE_CHARS)) if d else ""
        name = (nick or "?")[:12]
        if _self_row and wxid == _self_row:
            name += "（我）"
        lines.append("- %s%s" % (name, tail))
    rest = total - len(lines)
    if rest > 0:
        lines.append("- …还有 %d 人" % rest)
    head = "群里有 %d 个人（%s）：" % (total, "主人在" if owner_in else "主人不在")
    out = head + "\n" + "\n".join(lines)
    # 2026-11-02：原来会补一句「名单里没有叫 X 的」。但调用方传进来的是**会话名**
    # （群聊时就是群名本身），于是永远匹配不上、永远输出这句废话，还会把群号当人名。
    # 这句话没有任何一处需要，删掉。
    return out if len(out) <= GROUP_CAP else out[: GROUP_CAP - 1] + "…"


def _person_line(cid: str, who: str) -> str:
    """私聊：对面是谁。没有档案就不说——空档案比不说更糟。"""
    d = _notes_by_person(cid).get(cid)
    if not d:
        return ""
    name = str(who or "").strip()[:12] or "对方"
    return "对面是：%s（%s）" % (name, _merge_notes(d, NOTE_CHARS * 2))


CHAT_NOTE_MAX = 120      # 「场外补充」整段上限
CHAT_NOTE_ITEM = 90      # 单条上限（与 person_notes.NOTE_MAX 同量级）
CHAT_NOTE_LIMIT = 3      # 最多几条


def chat_note_line(chat_id: str, platform: str = "") -> str:
    """这个群「平时是什么样」——原文里看不出来、要待几天才知道的那种背景。

    2026-11-02：`person_notes` 里 `person_key=''` 的**群基线**一直没人渲染。
    它的消费者是已删除的 `agents/conversational.py`（模块 docstring 还写着
    「登记：agents/conversational.py 构建 [场外补充]」），flag 开着、聊天名单也
    配着，但新引擎里没有任何代码读它。实测 278 个提示快照（11-01/11-02）里
    「拆台 / 玩梗 / 玩笑 / 氛围」命中 **0**；11-01 23:21 她在作业群里把
    Elaina 一句「笑死我了」判成「没头没尾，她笑的是哪件事我根本没看见」，
    然后沉默——缺的就是「这个群日常就是互相拆台、玩梗」这一句。

    只渲染群基线（person_key=''）；逐人卡由 `room_line` 一人一行负责，
    两处不重叠。开关沿用 `YINYUE_FEATURE_PERSON_NOTES` + 聊天名单：
    关 flag 这段就整段消失，不必改代码。
    """
    cid = str(chat_id or "").strip()
    if not cid:
        return ""
    try:
        from core.context import person_notes as _pn

        if not _pn.notes_enabled_for(cid):
            return ""
        rows = _pn.list_notes(chat_id=cid, active_only=True)
    except Exception as exc:  # noqa: BLE001 - 读不到只是少一段，不该带崩这一拍
        warn("brief_chat_note_failed", error=repr(exc)[:80])
        return ""

    try:
        from . import ports

        active = ports.current_read_scope()
    except Exception:
        active = None
    got: list[str] = []
    for r in rows or []:
        r = r or {}
        if str(r.get("person_key") or "").strip():
            continue                      # 逐人卡不归这段
        if str(r.get("chat_id") or "").strip() != cid:
            continue
        plat = str(r.get("platform") or "").strip()
        if plat and platform and plat != str(platform):
            continue
        if active is not None and str(active.chat_id) == cid:
            acct = str(r.get("account_id") or "").strip()
            if acct and acct != active.account_id:
                continue
        v = " ".join(str(r.get("content") or "").split())
        if v:
            got.append(v)
    if not got:
        return ""                         # 没有就不出现（空标题会让她以为看过）
    text = "\n".join("- " + _clip(x, CHAT_NOTE_ITEM) for x in got[:CHAT_NOTE_LIMIT])
    return text if len(text) <= CHAT_NOTE_MAX else text[: CHAT_NOTE_MAX - 1] + "…"


OPEN_LOOP_MAX = 2        # 「未完的事」最多几行
OPEN_LOOP_CHARS = 200    # 整段上限（prompt 里这一段最长就这么点）
OPEN_LOOP_ITEM = 90      # 单条上限（一条一行，超了带省略号）
OPEN_LOOP_HEAD = "我手上还没了结的事："   # 内容自带的提示行（块的标题在设计上不进 prompt，见方案文档 E-16）


def _intent_line(item, evidence_state: str = "") -> str:
    """一条未了结的事：先说到没到点，再说欠的是什么。"""
    key = " ".join(str(getattr(item, "key", "") or "").split())
    due = str(getattr(item, "due_at", "") or "").strip()
    cond = " ".join(str(getattr(item, "condition", "") or "").split())
    kind = str(getattr(item, "kind", "") or "")
    if due:
        head, tail = "- 还没到点：", "（%s）" % due
    elif kind == "observe" and cond:
        head, tail = "- 在等：", "（等 %s 开口）" % cond
    else:
        head, tail = "- 没办完：", ""
    label = source_label(evidence_state)
    suffix = (" " + label) if label else ""
    room = max(8, OPEN_LOOP_ITEM - len(head) - len(tail) - len(suffix))
    line = head + _clip(key, room) + tail + suffix
    return line if len(line) <= OPEN_LOOP_ITEM else line[: OPEN_LOOP_ITEM - 1] + "…"


def open_loop_line(chat_id: str, model=None) -> str:
    """她手上还没了结的事——欠着谁什么。

    2026-11-03：世界模型里早就有这本账（`Intention`：`open_intention` 开、
    `close_intention` 关、`due_intentions` 到点、`match_observe` 一开口就销账），
    但**读它的代码一处都没有**（本文件与 `loop/brain.py` 对 `intention` 全零命中）。
    于是她知道"欠着"，十分钟后就看不见了：时间型（答应明天问一下，今天一天
    不知道欠着）、条件型（在等某人回话，对方开口前不知道自己正在等）。

    只读不写：`open_intentions(model)` 是纯投影，不碰库、不开冷却、不记状态。
    没有内容就返回 ""（连提示行也不留）。有内容时**自带一行提示**：`_nparts` 里的
    标题在设计上不进 prompt（`_reshape` 只取每段的值重拼 user 消息），裸条目会被读成
    隔壁那张单子的续行（2026-11-03 冒烟实测：它紧跟在「这个会话我最近说过」的 - 列表后面）。
    """
    cid = str(chat_id or "").strip()
    if not cid:
        return ""
    try:
        if model is None:
            model = situation_store.get_world_model(cid)
        items = []
        for item in open_intentions(model):
            if not str(getattr(item, "key", "") or "").strip():
                continue
            state = goal_source_state(item, model.scope)
            if state != "dead":
                items.append((item, state))
        if not items:
            return ""
        # 到点的排前面（越早越前），其余按 key——顺序稳定，台账才对得上。
        items.sort(key=lambda pair: (0 if str(pair[0].due_at or "").strip() else 1,
                                     str(pair[0].due_at or ""), str(pair[0].key or "")))
        out: list[str] = []
        room_chars = OPEN_LOOP_CHARS - len(OPEN_LOOP_HEAD) - 1   # 提示行也占额度
        for it, state in items:
            if len(out) >= OPEN_LOOP_MAX:
                break
            line = _intent_line(it, state)
            if len("\n".join(out + [line])) > room_chars:
                break                    # 装不下就整条不要，不许截半句
            out.append(line)
        if not out:
            return ""
        return OPEN_LOOP_HEAD + "\n" + "\n".join(out)
    except Exception as exc:
        warn("brief_open_loop_failed", chat_id=cid, error=repr(exc)[:80])
        return ""


def room_line(chat_id: str, who: str = "") -> str:
    """群聊说「群里有谁」，私聊说「对面是谁」。

    2026-11-02：原来两件事共用一个函数，对私聊也输出「群里 N 个人」——
    概念上错的（私聊哪来的「几个人」）。这里按会话类型分开。
    """
    cid = str(chat_id or "").strip()
    if not cid:
        return ""
    try:
        if _is_group(cid):
            return _group_line(cid, who)
        return _person_line(cid, who)
    except Exception as exc:  # noqa: BLE001 - 读不到就整段不出现，别带崩这一拍
        warn("brief_room_line_failed", error=repr(exc)[:80])
        return ""


def _hits(rows: Iterable[dict], words: list[str], limit: int) -> list[str]:
    """按关键词捞旧事，**从最新的往回捞**。

    2026-11-02：原来是从头遍历（文件里旧的在前），于是永远先捞到最旧的那条。
    她 10-31 记「幸福一家人屋子不存在」，11-02 自己更正过三次，
    检索却一直把那条旧结论递给她——「回忆」应该最近优先。
    这比「作废机制」便宜得多，而且不修它，加了作废也白搭（捞到的本来就不该出现）。
    """
    out: list[str] = []
    for row in reversed(list(rows)):
        text = str(row.get("text") or row.get("note") or "").strip()
        if not text:
            continue
        if any(w in text for w in words):
            out.append(text if RECALL_MAX is None else text[:RECALL_MAX])
            if len(out) >= limit:
                break
    return out


_STOP = ("这个", "那个", "什么", "现在", "一下", "我们", "他们", "就是", "可以", "不是")


def _words(body: str, *, limit: int = 24) -> list[str]:
    """没有分词器，就用二元字窗口当关键词。

    为什么不用「整段连写」：中文没有空格，"他今天又加班" 整串去比永远配不上
    本子里的 "他连加三天班"。切成二字窗口，"加班" 就能命中——这是小检索能落地的边界。
    """
    out: list[str] = []
    # 2026-11-22：改成从后往前扫。原来从头扫，一段长消息的前 24 个二元词就能把名额占满，
    # 结果「最近在说的那句」反而进不来。从后往前 = 最近的话优先。
    for run in reversed(_CJK.findall(body)):
        for i in range(len(run) - 2, -1, -1):
            w = run[i:i + 2]
            if w not in _STOP and w not in out:
                out.append(w)
        if len(out) >= limit:
            break
    return out[:limit]


def recall_line(text: str, who: str = "", *, target: str = "", memo_rows=None, history_rows=None) -> str:
    """按关键词从本子/日记里捞旧事；捞不到返回空串（整段省略）。"""
    body = str(text or "")
    # 2026-11-22：`_words()` 只取开头 24 个二元词，而快照开头是「群里有 N 个人」这类表头和名单。
    # 长窗里后面真正在说的话一个词都进不去，检索只能靠表头去撞。取尾部作检索面。
    if len(body) > 600:
        body = body[-600:]
    words = _words(body + " " + str(who or ""))
    if not words:
        return ""
    try:
        from loop import notes
        mrows = notes._all_rows() if memo_rows is None else list(memo_rows)
        hrows = [] if history_rows is None else list(history_rows)
    except Exception as exc:
        warn("brief_recall_failed", error=repr(exc)[:80])
        return ""
    found = _hits(notes.visible_rows(mrows, target), words, 2) + _hits(notes.visible_rows(hrows, target), words, 1)
    if not found:
        return ""
    text_out = "我还记得：" + "；".join(found)
    return text_out if RECALL_MAX is None else text_out[:RECALL_MAX] + ("…" if len(text_out) > RECALL_MAX else "")


def enabled() -> bool:
    """这两段属于 v2 形状；开关关着就不进提示词。"""
    return envflags.flag("PROMPT_SHAPE_V2")
