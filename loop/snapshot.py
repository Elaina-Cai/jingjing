"""眼睛：把世界压成一屏。

穷举会话（不穷举消息）：每个会话一行，够她判断"要不要动、对谁动"。
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime

from . import ports, vision
from core.conversation import media_ref
from safelog import warn  # 2026-11-02 修 F821：补拉失败的告警一直在被吞

SESSIONS = 30
CELL_CHARS = 10 ** 9  # 2026-10-31：主人要求取消上限（原 200；等价于不截）
BUDGET_CHARS = 10 ** 9  # 2026-10-31：主人要求取消上限（原 8000；等价于不截）
PREV_IN_GROUP = 11    # ★ 的群：连同『上一条』一共 12 条原文          # 有人在等我（★）的群：带前面 3 条
PREV_IN_GROUP_QUIET = 11  # 安静群也照样给全，不再省那几十字    # 安静的群：只带 1 条，别撑爆 1000 字预算   # 群里多带几条上下文（只私聊一句太薄）
# 2026-10-30（用户定「甲」）：私聊也带上下文，不再只给「他上一条」。
# 6 条 ≈ 一来一回三轮；比群少，因为私聊一屏之内叙事通常不会拐弯。
PREV_IN_DM = 6
ADDR_WINDOW_S = 900   # 「我那句话是在跟谁说话」只认 15 分钟内最后一条别人的话
_QUOTE_RE = re.compile(r"^\[引用\s*([^:：\]]{1,24})[:：]\s*(.+?)\]\s*(.*)$", re.S)
_BARE_RE = re.compile(r"[\s，。、！？…~\-—\"“”'‘’（）()\[\]【】*]+")


# 2026-10-27：群里的系统消息在库里是原始 XML
# （<?xml version="1.0"?><sysmsg type="revokemsg"><revokemsg><content>"Elaina" 撤回了一条消息…
#  以及 <sysmsg type="pat">），直接渲染进视界就是一坨标签。这里洗成人话。
_SYSMSG_RE = re.compile(r'<sysmsg[^>]*type="([^"]+)"[^>]*>(.*?)(?:</sysmsg>|$)', re.S)
_CONTENT_RE = re.compile(r"<(?:content|template)>(.*?)</(?:content|template)>", re.S)


_PLACEHOLDER_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)\$")


def _fill_placeholders(tpl: str, body: str) -> str:
    """sysmsgtemplate 把名字留成 $username$，真值在同一条 XML 的 <link_list> 里。

    2026-10-27：不填的话，她和面板看到的是 `"$username$"修改群名为“$remark$”`——
    比原始 XML 好一点，但还是一屏没法读的垃圾。
    """
    if "$" not in (tpl or ""):
        return tpl
    vals: dict[str, str] = {}
    try:
        root_el = ET.fromstring(body if body.lstrip().startswith("<") else "<sysmsg>%s</sysmsg>" % body)
    except ET.ParseError:
        return tpl
    for link in root_el.iter("link"):
        name = link.get("name") or ""
        if not name:
            continue
        val = (link.findtext("plain") or link.findtext(".//nickname")
               or link.findtext(".//plain") or "")
        if val.strip():
            vals[name] = val.strip()
    return _PLACEHOLDER_RE.sub(lambda m: vals.get(m.group(1), m.group(0)), tpl)


def _strip_cdata(s: str) -> str:
    """模板正文被 CDATA 包着（`<![CDATA[...]]>`），不剥掉就会把这对标记一起显示。"""
    s = (s or "").strip()
    if s.startswith("<![CDATA["):
        s = s[len("<![CDATA["):]
    if s.endswith("]]>"):
        s = s[:-3]
    return s.strip()


def human_sysmsg(text: str) -> str:
    """把 sysmsg XML 洗成一句话；不是系统消息就原样返回。"""
    raw = str(text or "")
    if "<sysmsg" not in raw:
        return raw
    m = _SYSMSG_RE.search(raw)
    kind = (m.group(1) if m else "").strip().lower()
    body = m.group(2) if m else raw
    inner = _CONTENT_RE.search(body)
    said = _strip_cdata(_flat(inner.group(1))) if inner else ""
    if said and "$" in said:
        said = _fill_placeholders(said, body)
    if kind == "revokemsg":
        return said or "（有人撤回了一条消息）"
    if kind == "pat":
        return said or "（有人拍了拍谁）"
    if kind in ("roomtool", "qrcode", "chatroomtool"):
        return "（群里的系统消息）"
    return said or "（系统消息）"


def _fmt_cell(who: str, text: str) -> str:
    # 2026-10-28: 引用单独成行，免得她把「你怎么这么说话」当成泛指
    m = _QUOTE_RE.match(str(text or ""))
    if not m:
        return "%s: %s" % (who, text)
    q_who = (m.group(1) or "").strip()
    q_txt = (m.group(2) or "").strip()[:80]
    rest = (m.group(3) or "").strip() or "…"
    return ("%s: %s" % (who, rest)) + chr(10) + ("    ↳ %s 引用那句：「%s」" % (q_who, q_txt))


def _flat(text: str) -> str:
    """把多行消息压成一行：引用、转发、换行正文都不能把「一屏一会话」撑断。"""
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    s = " / ".join(part.strip() for part in s.split("\n") if part.strip())
    return " ".join(s.split())


DESC_CHARS = 80        # 一张图的描述给到 80 字（视觉那层自己已经压到 60 字以内）


def _media_key(path: str) -> str:
    """认一张图：只看文件名（两库 local_path 的前缀不一样），大小写无关。"""
    return os.path.basename(str(path or "").replace("\\", "/")).strip().lower()


_QUOTED_IMG_RE = re.compile(r"(\[引用 [^\]]{1,24}: )\[图片\]")
_BAD_DESC = ("30字以内", "30字内", "禁止项", "用户想要", "目标：", "目标:", "字数",
             "最终一句话", "草稿", "一句话概括", "总结：")
# 2026-10-27：引用一张还没描述过的图时，宁可多等几秒也让她真看见；每拍最多一张。
_QUOTE_WAIT_S = 6.0


def _with_quoted_image(text: str, media_path: str, descs: dict[str, str] | None) -> str:
    """`[引用 P6: [图片]]` → `[引用 P6: [图片: 描述]]`。

    2026-10-27：微信把被引用那张图的 media_path 记在引用行自己身上。
    """
    if "[引用" not in (text or ""):
        return text
    if "[图片]" not in text and not media_ref.has_locator(text):
        return text
    return media_ref.render_media_locators(text, descs=descs or {}, path=media_path,
                                           mode="wait", budget_s=_QUOTE_WAIT_S)


_HARD_NO = re.compile(r"\d{3,}")
_TIME_RE = re.compile(r"\d{1,2}[:：]\d{2}")
_OCR_NOISE = ("首页", "推荐", "关注", "粉丝", "点赞", "评论", "分享", "收藏", "转发",
              "更多", "搜索", "全屏观看", "下载", "广告", "打开")


def _ocr_gist(text: str, n: int = 40) -> str:
    """从 OCR 里挑出"摘要常丢掉、但确实有用"的硬信息：车牌/金额/编号/时间。

    规则：只留含 **三连数字** 或 `时:分` 的片段（"9516" "粤KD37998" "19:11" "1473"），
    最多四段、总计 n 字。界面噪声（首页/推荐/关注…）与纯英文字母缩写一律不要。
    """
    flat = " ".join(str(text or "").split())
    if not flat:
        return ""
    keep: list[str] = []
    for x in flat.split(" "):
        if not x or x in _OCR_NOISE:
            continue
        if not (_HARD_NO.search(x) or _TIME_RE.search(x)):
            continue
        if len(x) < 4 and not any(c in x for c in ":：%¥￥"):
            continue          # "211" / "140" 这种孤立小数字不加
        keep.append(x)
        if len(keep) >= 4:
            break
    body = " ".join(keep).strip()
    return body[:n] if len(body) >= 3 else ""


def _vision_mode(r) -> str:
    """刚发生的事才值得为她多等几秒；十分钟以前的图让后台慢慢补，别卡住这一拍。"""
    try:
        age = (datetime.now() - datetime.strptime(str(getattr(r, "at", ""))[:19],
                                                  "%Y-%m-%d %H:%M:%S")).total_seconds()
    except Exception:  # noqa: BLE001 - 时间看不懂就当旧的
        return "prefetch"
    return "wait" if age <= 600 else "prefetch"


def _text_of(r, descs: dict[str, str] | None = None) -> str:
    """一条消息给人看的短文本。

    图片先说她**看见了什么**（`message_media.description`，入站时视觉模型写的），
    再退到读图里的字（OCR，有缓存），两样都没有才只剩 [图片]。
    2026-10-26：以前只有 OCR —— 一眼就懂的图（头像、表情、照片、聊天截图）
    对她等于空白，于是她说"图我这边只是个空框"。
    """
    if "<sysmsg" in str(getattr(r, "text", "") or ""):
        return human_sysmsg(r.text)[:DESC_CHARS]
    if getattr(r, "kind", "") in ("image", "quote_image") or (
            getattr(r, "kind", "") == "file"
            and media_ref.is_image_path(getattr(r, "media_path", ""))):
        # 2026-10-27: 动图/图片常以「文件」形态进来（kind=file），后缀是 gif/png 的
        # 一律按图片走描述，别让「[文件]」把她蒙住。
        _mp = getattr(r, "media_path", "")
        _d = (descs or {}).get(_media_key(_mp))
        if not _d and _media_key(_mp):
            # 2026-10-27：入站没写成描述（失败 / gif 没有文字层 / 她引用时才第一次见），
            # 就在建快照时现问一次视觉 —— 否则她只能对一张一眼就懂的图说"是个空框"。
            _d = vision.describe(_mp, mode=_vision_mode(r))
        # 2026-10-27：早期描述里混进过"复述任务"的稿子（「目标：…30字以内…」），
        # 那不是图的内容。像任务书的描述一律不采信，退回 OCR。
        if _d and any(_bad in str(_d) for _bad in ("30字以内", "禁止项", "用户想要", "目标：")):
            _d = ""
        if _d:
            tag = "图片"
            try:  # 2026-10-26：认得出是自己就明说，省得她把自己的图当陌生图聊
                from core.identity.self_look import judge_text
                if judge_text(_d).get("verdict") == "self":
                    tag = "图片·是我自己"
            except Exception:  # noqa: BLE001 - 认错不如不认
                pass
            # 2026-10-27：摘要让她"一眼看懂画面"，OCR 让她"读到具体数字/药名/金额"。
            # 两样都给，但只取 OCR 里带数字或字母的那几处，别把界面噪声塞进去。
            _ocr = _ocr_gist(vision.image_text(getattr(r, "media_path", ""), max_chars=160))
            if _ocr and _ocr not in _d:
                return ("[%s: %s｜原文：%s]" % (tag, _d, _ocr))[:CELL_CHARS + 60]
            return ("[%s: %s]" % (tag, _d))[:DESC_CHARS + 16]
        _it = vision.image_text(getattr(r, "media_path", ""))
        if _it:
            # 2026-10-27：以前这里按 CELL_CHARS 截断，OCR 读到的后半句全没了。
            # 她说"看不清里面写的什么"时，其实读到过字，只是被我切掉了。
            return ("[图片]「%s」" % _it)[:CELL_CHARS + 120]
    if getattr(r, "kind", "") in ("sticker", "emoji"):
        # 2026-10-27：以前她只看得到裸「[表情包]」，连 md5/token 都没有。
        return ports.sticker_text(getattr(r, "raw", "") or getattr(r, "text", ""),
                                  descs, _vision_mode(r))[:CELL_CHARS]
    _t = _flat(r.text)
    if "[引用" in _t:
        _t = _with_quoted_image(_t, getattr(r, "media_path", ""), descs)
    return _t[:CELL_CHARS]


def _sec(at: str) -> float:
    """'2026-10-26 19:43:54' → 秒。取不到就 0。"""
    s = str(at or "")[:26]
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def _bare(text: str, n: int = 14) -> str:
    return _BARE_RE.sub("", str(text or ""))[:n]


def _snip(text: str, n: int = 20) -> str:
    """一句话的摘要。别人那句本身是引用时，只取他真正说的那半句。"""
    t = _flat(text)
    m = _QUOTE_RE.match(t)
    if m:
        t = (m.group(3) or "").strip() or (m.group(2) or "").strip()
    t = t.lstrip("/ ").strip()
    return t[:n]


def chat_addressing(rows_c: list) -> dict:
    """我那句话之前，最后一条别人的话是什么——只给事实，不做推断。

    2026-10-26 用户提的：有人引用她一句「（鳍耳抖了一下，往她那边挪了半步）嗯。」
    问「她是谁」——那个"她"指的是她当时对着说话的人（Elaina 刚说了「乖」）。
    视界里从来没有这条事实，她只能猜，猜成了"今晚突然冒出来的陌生人"。

    这里**不猜**"对谁说的"（群里三秒内好几个人说话，猜错比不猜更糟），
    只把"前一条是谁、几点、说了什么"摆出来，让她自己认。
    """
    ordered = sorted(rows_c, key=lambda r: (r.at, r.msg_id or 0))
    prev, peer = {}, None
    for r in ordered:
        if getattr(r, "is_self", False):
            if peer is not None:
                prev[(r.at, r.msg_id or 0)] = {"who": peer.speaker or "", "at": peer.at,
                                               "snip": _snip(peer.text)}
        else:
            _t = _flat(r.text)
            # 表情包/图片不顶掉"前一条有人说的话"——引用里要的是那句人话
            if not (_t.startswith("[表情包") or _t.startswith("[图片") or "拍了拍" in _t):
                peer = r
    hist = [{"at": r.at, "key": _bare(r.text), "prev": prev[(r.at, r.msg_id or 0)]}
            for r in ordered if (r.at, r.msg_id or 0) in prev]
    return {"self": prev, "mine": hist}


def _annot(r, text: str, ctx: dict, me: tuple, is_group: bool) -> str:
    """别人引用我的话时，把那句话补成「我几点的、我前一条看到的是谁说的什么」。"""
    if not text or not is_group or getattr(r, "quote_id", ""):
        return text
    m = _QUOTE_RE.match(text)
    if not m:
        return text
    q_who, q_body, rest = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    if not any(a and a in q_who for a in me):
        return text
    k = _bare(q_body, 10)
    hit = None
    for cand in (ctx.get("mine") or []):
        ck = cand.get("key") or ""
        if k and ck and (ck.startswith(k[:6]) or k.startswith(ck[:6])):
            hit = cand
            break
    if not hit:
        return text
    pv = hit.get("prev") or {}
    lead = "[引用我 %s 说的" % hit["at"][11:16]
    if pv.get("who"):
        lead += "（前一条 %s %s「%s」）" % (pv["who"], str(pv.get("at") or "")[11:16], pv.get("snip") or "")
    lead += ": %s]%s" % (q_body, (" " + rest) if rest else "")
    return lead



# 2026-10-27（用户「要」）：这个号主人也在用。
# 库里 is_self=1 只说明"这个微信号发的"，不说明"我发的"。
# 渲染前拿她自己的发送账本对账：对不上就标成「这个号」，
# 否则她会把主人替她发的话当成自己说过的 —— P5那次就是这样沉默掉的。
ALIEN_LABEL = "这个号"
MINE_WINDOW_S = 90.0

try:  # 世界账本的时间换算（本地 <-> UTC）
    from db.world_db import to_utc  # noqa: E402
except Exception:  # pragma: no cover
    def to_utc(text: str) -> str:
        return ""
_SAY_IDX: dict = {"at": 0.0, "m": {}}


def _ts(text) -> float:
    s = str(text or "")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def _say_index() -> dict:
    """最近三天的发送账本 → {chat_id: [秒戳…]}。60 秒缓存，读不到就当空。"""
    now = time.time()
    if _SAY_IDX["m"] and now - _SAY_IDX["at"] < 60.0:
        return _SAY_IDX["m"]
    out: dict = {}
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for fp in sorted(glob.glob(os.path.join(root, "data", "loop", "ledger-*.jsonl")))[-3:]:
            try:
                fh = open(fp, encoding="utf-8")
            except OSError:
                continue
            with fh:
                for ln in fh:
                    if '"say"' not in ln:
                        continue
                    try:
                        row = json.loads(ln)
                    except Exception:  # noqa: BLE001
                        continue
                    if row.get("act") != "say" or row.get("result") != "sent":
                        continue
                    cid = str(row.get("target") or "")
                    t = _ts(row.get("at"))
                    if cid and t:
                        out.setdefault(cid, []).append(t)
    except Exception:  # noqa: BLE001 - 账本读不到也别把世界弄瞎
        out = {}
    _SAY_IDX["at"], _SAY_IDX["m"] = now, out
    return out


def _mark_own_files(rows) -> list:
    """把自己发出的**文件**标回「我」。

    2026-10-28：微信给这类消息落库时 is_self=0（不认它属于本号），于是视界里
    她自己发的文件会被当成别人发的、显示成「这个号」。文件消息的 text 就是文件名，
    账本里记的是「文件：<文件名>」，world_db.by_text 已经同时试过两种 hash，
    所以能对上就认定是她发的。
    """
    try:
        from db import world_db as _w
    except Exception:
        return rows
    for r in rows or []:
        try:
            if getattr(r, "is_self", False):
                continue
            name = str(getattr(r, "text", "") or getattr(r, "content", "") or "")
            kind = str(getattr(r, "kind", "") or getattr(r, "msg_type_name", "") or "")
            if kind not in ("file", "file_upload") and not name.lower().endswith(
                    (".txt", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".7z",
                     ".png", ".jpg", ".jpeg", ".gif", ".mp3", ".mp4")):
                continue
            _cid = str(getattr(r, "chat_id", "") or "")
            if not _cid or not name:
                continue
            if _w.by_text(_cid, name, window_s=900):
                r.is_self = True
                r.speaker = "我"
                r._own_file = True
        except Exception:
            continue
    return rows


_SELF_INTRO = re.compile(r"^我是群聊.+?的(.{1,20})$")


def _fix_self_intro(rows: list) -> list:
    """「我是群聊X的Y」永远是**对方**发来的招呼语，不可能是本号说的。

    2026-10-30：微信库把这类消息标成 `is_self=1` 且不给 `who`，渲染层只好写“我”，
    于是提示词里出现“我说过一句我没说过的话”。这里按语义纠正，并落一条事件。
    """
    for r in rows:
        try:
            if not getattr(r, "is_self", False):
                continue
            txt = str(getattr(r, "text", "") or "").strip()
            m = _SELF_INTRO.match(txt)
            if not m:
                continue
            name = m.group(1).strip()
            r.is_self = False
            r.speaker = name or "（来源不明）"
            from safelog import warn

            warn("snapshot_self_intro_reassigned", chat=str(getattr(r, "chat_id", "") or "")[:40],
                 name=name[:20])
        except Exception as exc:  # noqa: BLE001
            from safelog import warn

            warn("snapshot_self_intro_failed", why=repr(exc)[:60])
    return rows


def _mark_aliens_world(rows: list) -> list:
    """以世界账本的 text_hash 为准判"是不是我发的"。

    2026-10-28：±90 秒窗口只能猜，账本可以直接查到"我在这个会话里说过这句"。
    只对账本建立之后的消息生效，历史不动（避免把旧记录误判成别人）。
    """
    try:
        from db import world_db as _wdb
        _since = _wdb.installed_at()
    except Exception:
        return rows
    if not _since:
        return rows
    for r in rows:
        if not getattr(r, "is_self", False):
            continue
        ts = to_utc(_ts(str(getattr(r, "at", "") or "")))
        if not ts or ts < _since:
            continue
        text = str(getattr(r, "text", "") or getattr(r, "preview", "") or "")
        try:
            hit = _wdb.by_text(str(getattr(r, "chat_id", "") or ""), text, window_s=180)
        except Exception:
            hit = None
        if hit:
            continue
        r.is_self = False
        r.speaker = ALIEN_LABEL
        r._alien = True
    rows = _fix_self_intro(rows)
    return rows


def _context_rows(db: str, chat_id: str, need: int, *, since_hours: int = 87600) -> list:
    """按会话补拉几条上下文。

    全局那一把是 ``since_hours=168 / limit=800``，800 行是整个账号共享的配额，
    按时间倒序切 —— 安静的会话常常只剩最新一条（2026-10-30 实测：私聊 166 行
    在库，快照里只有 1 行）。这里按 ``chat_id`` 单独再要一次，让「再往前几条」
    真的有东西可给。失败不抛：补不到就少看见几条。
    """
    if not chat_id or need <= 0:
        return []
    try:
        return list(ports.sense_sync(db, since_hours=since_hours, limit=int(need), chat_id=chat_id))
    except Exception as exc:  # noqa: BLE001 - 补拉失败不许把拍子带崩
        warn("snapshot_context_rows_failed", err=repr(exc)[:120])
        return []


def _merge_rows(base: list, extra: list) -> list:
    """按 ``(at, msg_id)`` 去重合并，保持原顺序（先来的优先）。"""
    seen = {(getattr(r, "at", ""), getattr(r, "msg_id", 0)) for r in base}
    out = list(base)
    for r in extra:
        key = (getattr(r, "at", ""), getattr(r, "msg_id", 0))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _verify_group_quotes(db: str, chat_id: str, rows: list) -> None:
    """Mark only the group quotes whose sender and target resolve in this account."""
    quoted_rows = [row for row in rows if getattr(row, "quote_id", "")]
    if not quoted_rows or not db:
        return
    from .goal_review import database_scope
    from core.context import appraisal
    from core.context.cognitive_commit import Scope
    from core.terminal import shadow

    scope = Scope(*database_scope(db), chat_id)
    if not scope.complete:
        return
    for row in quoted_rows:
        row.quote_targets_self = False
        if not row.platform_msg_id:
            continue
        try:
            store = shadow.get_default_store()
            message = store.get_by_external_msg_id(platform=scope.platform, bot_account_id=scope.account_id,
                                                   chat_id=scope.chat_id, external_msg_id=row.platform_msg_id)
            row.quote_targets_self = appraisal._quoted_self_event(store, scope, message, row.quote_id) is not None
        except Exception as exc:  # noqa: BLE001 - 缺少凭据不影响同群其他发言
            warn("snapshot_quote_verify_failed", chat_id=chat_id, msg_id=row.msg_id, err=repr(exc)[:120])


def build_snapshot(db: str, *, at: datetime | None = None, sessions: int = SESSIONS) -> dict:
    at = at or datetime.now()
    vision.reset_budget()
    media_ref.reset_budget()
    descs = ports.media_descriptions(db)   # 这一库里已存好的图片描述（零延迟）
    try:  # 世界的账本：把各账号库里已有的消息增量抄进来（节流 2 秒，失败不影响这一拍）
        from db import world_db as _wdb0
        _wdb0.sync_accounts()
    except Exception:
        pass
    # 2026-10-31：主人要求取消上限（原 168h/800 行）。时间窗放到 10 年，但**每次从库里拉的
    # 行数**保留一个性能上限：实测 limit=200000 时 build_snapshot 要 52 秒，并且会把全部
    # 历史图片重新送进 vision（每张一次 LLM 调用），引擎启动就卡死在 starting。
    rows = ports.sense_sync(db, since_hours=87600, limit=3000)
    # 2026-10-28：归属只用账本判（outbound_receipt 的 text_hash）。
    # 旧的 ±90 秒时间窗已删除 —— 没有 ledger 记录时它会把她的消息误判成「这个号」。
    _own = _mark_own_files(rows)
    rows = _mark_aliens_world(_own)
    try:
        me = ports.me_aliases()
    except Exception:  # noqa: BLE001 - 名单取不到不该让眼睛瞎掉
        me = ()
    seen: dict[str, dict] = {}
    for r in rows:
        if not r.chat_id:
            continue
        slot = seen.setdefault(r.chat_id, {
            "chat_id": r.chat_id,
            "chat_type": (r.chat_type or ("group" if ports.is_group_chat(db, r.chat_id) else "private")),
            "label": ports.chat_label(db, r.chat_id),
            "self_chat": ports.is_self_chat(r.chat_id),
            "last_at": r.at, "last_speaker": "", "last_is_self": r.is_self,
            "preview": _flat(r.text)[:CELL_CHARS], "after_mine": [],
        })
        slot["last_at"] = r.at
        slot["last_is_self"] = r.is_self
        slot["last_speaker"] = "" if r.is_self else (r.speaker or "")
        slot["preview"] = _flat(r.text)[:CELL_CHARS]
        # 2026-10-24：以前"我说话就清空 after_mine"依赖行序（DESC 时反而清掉更新的），
        # 导致「他上一条没答」被埋掉。改成按时间戳判定，与行序无关。
        slot.setdefault("_rows", []).append(r)
    for d in seen.values():
        rows_c = d.pop("_rows", [])
        _ctx = chat_addressing(rows_c)
        _is_group = d.get("chat_type") == "group"
        # 2026-10-25：preview/最后一条以前跟着遍历顺序走，行序一变就取到旧的那条
        # （出现了"最后一句"和"再往前"重复同一句话）。这里按时间戳统一认最后一次。
        if rows_c:
            _last = sorted(rows_c, key=lambda r: (r.at, r.msg_id or 0))[-1]
            d["last_at"] = _last.at
            d["last_msg_id"] = _last.msg_id
            d["last_is_self"] = _last.is_self
            d["last_speaker"] = "" if _last.is_self else (_last.speaker or "")
            d["preview"] = _annot(_last, _text_of(_last, descs), _ctx, me, _is_group)
        def _okey(r):
            # 2026-10-25：先后用 (时间, msg_id) 判。以前只比 r.at，而 r.at 只到分，
            # 于是"同一分钟里有人回我"被判成"没人"（★ 不亮、她被蒙在鼓里）。
            return (r.at, r.msg_id or 0)
        mine = [r for r in rows_c if r.is_self]
        _my = max(mine, key=_okey, default=None)
        my_last = _my.at if _my else ""
        peers = [r for r in rows_c if not r.is_self]
        d["my_last_at"] = my_last
        if _my is not None:
            d["my_last"] = {"at": my_last, "text": _my.text}
        if _my is not None and _is_group:
            d["peer_before_mine"] = max(
                (r for r in peers if _okey(r) < _okey(_my)), key=_okey, default=None)
        d["after_mine"] = [r for r in peers if _my is None or _okey(r) > _okey(_my)]
        if _is_group and rows_c:
            candidates = d["after_mine"] + ([d["peer_before_mine"]] if d.get("peer_before_mine") else [])
            _verify_group_quotes(db, d["chat_id"], candidates)
            try:
                d["toward"] = ports.toward_label(_last) if not _last.is_self else ""
            except Exception:  # noqa: BLE001
                d["toward"] = ""
        # 他上一条（不管我后来有没有说）——我一屏一行时，这条最容易被埋
        peer_last = max(peers, key=_okey, default=None)
        d["peer_last"] = ({"at": peer_last.at, "msg_id": peer_last.msg_id, "speaker": peer_last.speaker or "",
                           "text": _annot(peer_last, _text_of(peer_last, descs), _ctx, me, _is_group)}
                          if peer_last else None)
        # 2026-10-25：群里只给最后一句太薄——再带前面几条，让她看得到"这段在聊什么"。
        # 先算 tier：★ 的格子多给几条，安静的少给，否则一屏会被撑到 1000 字被截断。
        d["tier"], d["hits"] = ports.address_tier(d["after_mine"], d["chat_type"] != "group")
        prev: list[dict] = []
        _limit_ctx = PREV_IN_GROUP if d["chat_type"] == "group" else PREV_IN_DM
        if _limit_ctx > 0:  # 2026-10-30：私聊也带上下文（用户定「甲」）
            _cid = str(d.get("chat_id") or (getattr(rows_c[0], "chat_id", "") if rows_c else ""))
            if len(rows_c) <= _limit_ctx:
                rows_c = _merge_rows(rows_c, _context_rows(db, _cid, _limit_ctx + 2))
            ordered = sorted(rows_c, key=lambda r: (r.at, r.msg_id or 0))
            # peer_last 单独展示，同一消息从 prev 排除。
            seen_ids = {peer_last.msg_id} if peer_last else set()
            _txt_rows, _media_rows = [], []
            for r in reversed(ordered[:-1]):
                if r is peer_last or (r.msg_id and r.msg_id in seen_ids):
                    continue
                seen_ids.add(r.msg_id)
                _txt = _text_of(r, descs)
                row = {"at": r.at, "msg_id": r.msg_id, "speaker": "我" if r.is_self else (r.speaker or "?"),
                       "text": _annot(r, _txt, _ctx, me, _is_group)}
                if _txt.startswith("[表情包") or _txt.startswith("[图片"):
                    _media_rows.append(row)
                else:
                    _txt_rows.append(row)
            # 2026-10-31 主人要「全部上限取消」：以前私聊只留最近 6 条（PREV_IN_DM），
            # 10-29 12:30 那条引用+提问就是这样被挤出去的，她看不到。现在有文字的全留。
            prev = list(_txt_rows)
            if _media_rows:   # 2026-10-31 取消上限：表情包/图片行不再只留 1 条垫底
                prev = prev + _media_rows
            # 2026-10-25 修时序：媒体那条是"垫底"塞进来的，它的时间可能比前面的行
            # 更早也可能更晚，直接追加会让「↳ 再往前」倒错（16:48 → 16:35 → 16:38）。
            prev = sorted(prev, key=lambda row: (row["at"], row["msg_id"] or 0), reverse=True)
        d["prev"] = prev
    for _d in seen.values():
        _d.pop("_ctx", None)
    cells = sorted(seen.values(), key=lambda d: d["last_at"], reverse=True)[:sessions]
    return {"at": at.strftime("%Y-%m-%d %H:%M"), "week": "周" + "一二三四五六日"[at.weekday()],
            "db": db, "cells": cells, "total": len(seen)}



def _clock_cn(at: str) -> str:
    """'2026-10-25 01:28' → '凌晨一点二十八'。只为让她照着说，不做别的用。"""
    try:
        hh, mm = str(at)[11:16].split(":")
        h, m = int(hh), int(mm)
    except Exception:  # noqa: BLE001
        return ""
    seg = ("凌晨" if h < 5 else "早上" if h < 9 else "上午" if h < 12 else
           "中午" if h < 13 else "下午" if h < 18 else "晚上")
    _n = "零一二三四五六七八九十"

    def _n2(x: int) -> str:
        if x < 10:
            return _n[x]
        if x == 10:
            return "十"
        if x < 20:
            return "十" + _n[x - 10]
        return _n[x // 10] + "十" + (_n[x % 10] if x % 10 else "")
    h2 = h if h <= 12 else h - 12
    h2 = 12 if h2 == 0 else h2
    tail = "整" if m == 0 else (("零" + _n2(m)) if m < 10 else _n2(m))
    return "%s%s点%s" % (seg, _n2(h2), tail)

def _epoch_of(at: str) -> float | None:
    """把 'YYYY-MM-DD HH:MM[:SS]' 换成 epoch；认不出返回 None，**不抛**。

    2026-10-30：`newest_at()` 给的是 `last_at`，带秒（18:16:40），而这里原来只用
    `%Y-%m-%d %H:%M` 解析 → ValueError 被外层 `except: pass` 吞掉 → 那句
    “视界里最新一条消息是…”安静地整句消失。宁可认不出时返回 None，也不许抛。
    """
    s = str(at or "").strip()
    failed = 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(s[:19], fmt))
        except (ValueError, OverflowError):
            failed += 1
            continue
    # 2026-11-22：以前每试一种格式失败就 warn 一次。`last_at` 只到分钟时，
    # 第一种格式必然失败、第二种能解析，于是每条分钟级时间戳都写一条噪声
    # （实测 silent_warnings.log 里 2498 条）。只在全部失败时出声。
    from safelog import warn

    warn("snapshot_time_unparsed", at=s[:19])
    return None


def newest_at(cells: list[dict]) -> str:
    """视界里最新一条消息的时间（没有就空串）。句子里的“最新”必须取自消息，不取自构建时刻。"""
    return max((str(c.get("last_at") or "") for c in (cells or [])), default="")


def render_text(snap: dict, only: str = "", *, back: bool = True,
                now: datetime | None = None) -> str:
    """把世界渲染成她看到的样子。

    ``back=False``：每个会话只出**最新一条** + 那句注释（`｜…`），不再出
    `↳ 上一条` / `↳ 再往前` 的回看行 —— 回看交给 ``render_turns()`` 的 `[原文]` 段
    （2026-10-31 用户定：同一批话不许在两段里各说一遍）。`look` 之类
    "她主动要历史"的路径仍用默认 ``back=True``，信息一点没少。

    ``now``：``[现在]`` 与「离现在多久」用的时刻。默认取真实时间；评测/录制要
    可复现的 prompt 时传固定值（否则同一场景每跑一次哈希都变）。
    """
    if only:  # 2026-10-28 分窗：这一拍只渲染一个会话，整份预算都给它
        _c1 = [c for c in (snap.get("cells") or []) if only in (c.get("chat_id"), c.get("label"))]
        if _c1:
            snap = dict(snap, cells=_c1, total=len(_c1))
            _only_hdr = "「%s」" % (_c1[0].get("label") or only)
    _only_hdr = locals().get("_only_hdr", "")
    _now = now or datetime.now()
    _now_txt = _now.strftime("%Y-%m-%d %H:%M")
    out = ["[现在] %s %s（口语：%s）" % (_now_txt, "周" + "一二三四五六日"[_now.weekday()], _clock_cn(_now_txt)),
           ("[视界] 这一拍只看 %s（其余会话不在这一拍里，要看用 look）" % _only_hdr) if _only_hdr
           else ("[视界] %d 个会话（★=有人在问我 ·=提到过我 空白=跟我无关）" % snap["total"])]
    try:  # 2026-10-28 新鲜度：她得知道自己手上的世界有多旧
        from db import world_db as _wdb1
        _fr = _wdb1.freshness()
        _age = float(_fr.get("age_s") or -1)
        if _age >= 0:
            if _age < 90:
                _txt = "刚刚"
            elif _age < 3600:
                _txt = "%d 分钟前" % max(1, int(_age // 60))
            else:
                _txt = "%.1f 小时前" % (_age / 3600.0)
            out.append("    （我手上最新一条消息：%s）" % _txt)
    except Exception:
        pass
    try:  # 2026-10-28：视界里最新那条离现在多久（只是事实，不提醒她该怎么做）
        _snap_at = newest_at(snap.get("cells") or [])
        if _snap_at:
            _t1 = _epoch_of(_snap_at)
            if _t1 is None:
                raise ValueError("time not understood: %r" % (_snap_at,))
            _mins = int((_now.timestamp() - _t1) // 60)
            if _mins >= 0:
                out.append("    （视界里最新一条消息是 %s，离现在 %s）" % (_snap_at, (
                    "刚刚" if _mins < 1 else ("%d 分钟" % _mins if _mins < 90 else "%.1f 小时" % (_mins / 60.0)))))
    except Exception:
        pass
    _al = sum(1 for c in snap.get("cells") or [] if (c.get("last_speaker") or "") == ALIEN_LABEL)
    if _al:
        out.append("    （「这个号」= 这个号被主人手动用过，那句话不是我说的）")
    for c in snap["cells"]:
        tier, hits = c.get("tier", ""), c.get("hits", 0)
        star = ("★%d" % hits) if tier == "ask" else ("·%d" % hits if tier == "mention" else " ")
        kind = "群" if c["chat_type"] == "group" else "私"
        if c["last_is_self"]:
            who = "我"
        else:
            who = c["last_speaker"] or "?"
        # 系统类内容不写发言人：它不是谁说的话
        if c["preview"].startswith("（系统）"):
            body = c["preview"]
        else:
            body = _fmt_cell(who, c["preview"])
        out.append("%s %s「%s」 %s %s" % (star, kind, c["label"], c["last_at"][5:16], body))
        if c.get("toward"):
            out.append("    ｜ " + c["toward"])
        if back:   # 2026-10-31：回看行与 [原文] 段重复，交给 render_turns()
            pl = c.get("peer_last")
            if c["last_is_self"] and pl and pl["text"] and not pl["text"].startswith("（系统）"):
                out.append("    ↳ 上一条 %s %s: %s" % (pl["at"][5:16], pl["speaker"] or "?", pl["text"]))
            for pv in (c.get("prev") or []):
                out.append("    ↳ 再往前 %s %s: %s" % (pv["at"][5:16], pv["speaker"], pv["text"]))
    # 2026-10-25：以前直接 [:BUDGET_CHARS] 硬切，最后一行会被砍成半句
    # （出现过 "↳ 上一条 10-2" 这种残句，她只能靠猜）。现在按整行收，
    # 收不下就在末尾明说"还有几个会话没显示"，宁可她少看，不给她残句。
    _txt = "\n".join(out)
    if len(_txt) <= BUDGET_CHARS:
        return _txt
    _keep, _used, _cells = [], 0, 0
    for _ln in out:
        if _used + len(_ln) + 1 > BUDGET_CHARS - 24:
            break
        _keep.append(_ln)
        _used += len(_ln) + 1
        if _ln and not _ln.startswith("    "):
            _cells += 1
    _more = max(0, int(snap.get("total") or 0) - _cells)
    _keep.append("…（一屏放不下：%s）" % ("还有 %d 个会话没显示" % _more if _more else "后面的细节被省略了，想看就 look"))
    return "\n".join(_keep)

# ── 2026-10-31 Step 5（提示词形状改造）：把「待回原文」从 render_text 里拎出来 ──
# 为什么单独一个函数：render_text() 是"她看到的世界"，里面的注释（离现在几小时、
# 「这条没提到我的名字」、引用补全）是有意的；但 C5「待回原文」要的是干净原话，
# 带括号的元说明会让她把注释当成对方说的话（改造文档 §5 人工核对清单第 1 条：
# 原文行里不许出现 `（…）` 注释）。所以这里只出「时间 发言人：正文」，一个注释都不带。
_META_TAIL_RE = re.compile(r"\s*｜.*$")
_TURN_QUOTE_RE = re.compile(
    r"^\[引用我\s*([0-9]{1,2}[:：][0-9]{2})\s*说的(?:（[^）]*）)?\s*[:：]\s*(.+?)\]\s*(.*)$", re.S)


def _strip_meta(text: str) -> str:
    """把 _annot / _fmt_cell 注入的元说明剥掉，只留原话。"""
    s = str(text or "")
    s = _META_TAIL_RE.sub("", s)
    # 2026-10-31：切掉「｜原文：…」时，尾巴上的 `]` 也一起没了（`[图片: 描述｜原文：X]`
    # 会变成不闭合的 `[图片: 描述`）。那看起来像一句没说完的话，补回括号。
    if s.startswith("[") and s.count("[") > s.count("]"):
        s = s + "]" * (s.count("[") - s.count("]"))
    m = _TURN_QUOTE_RE.match(s)
    if m:
        _q, _rest = m.group(2).strip(), (m.group(3) or "").strip()
        s = ("（引用「%s」）%s" % (_q, _rest)) if _rest else ("（引用「%s」）" % _q)
    s = s.replace("\n", " / ")
    return " ".join(s.split())


# 2026-10-31：`[原文]` 不再按「条数」卡，改按「字数」自适应（主人选的方案）。
# 有 2024 条 6 万字的群聊，也有 8 条 400 字的私聊——同一个 15 条对前者太肥、对后者太薄。
TURNS_BUDGET_CHARS = 1500


def render_turns(snap: dict, label: str = "", *, limit: int = 15,
                 budget: int | None = TURNS_BUDGET_CHARS) -> str:
    """只出原文：「时间 发言人：正文」，一行一句，不带任何元注释。

    label 给会话名（label）或 chat_id 时只出那一个会话；limit<=0 表示不限条数。
    budget>0 表示最多这么多字（从最新往回装，至少留最新一条）；None/0 不限字数。
    按时间和消息 ID 正序（最老的在上）。
    """
    _cells = list(snap.get("cells") or [])
    if label:
        _cells = [c for c in _cells
                  if (c.get("label") or "") == label or (c.get("chat_id") or "") == label]
    turns: list[tuple[str, int, str, str]] = []
    seen: set[tuple] = set()
    for c in _cells:
        chat = c.get("chat_id") or c.get("label") or ""
        rows = list(c.get("prev") or [])
        peer = c.get("peer_last")
        if isinstance(peer, dict):
            rows.append(peer)
        rows.append({
            "at": c.get("last_at"),
            "msg_id": c.get("last_msg_id"),
            "speaker": "我" if c.get("last_is_self") else c.get("last_speaker"),
            "text": c.get("preview"),
        })
        for row in rows:
            at = str(row.get("at") or "")
            if not at:
                continue
            msg_id = int(row.get("msg_id") or 0)
            who = row.get("speaker") or "?"
            body = _strip_meta(row.get("text") or "")
            # 旧快照没有消息 ID，仍按时间、发言人和原文合并重复展示。
            key = (chat, msg_id) if msg_id else (chat, at, who, body)
            if key in seen:
                continue
            seen.add(key)
            turns.append((at, msg_id, who, body))
    turns.sort(key=lambda row: (row[0], row[1]))
    if limit and limit > 0:
        turns = turns[-int(limit):]
    if budget and budget > 0:
        # 从最新往回装，装满就停；最新一条永远留（哪怕它自己就超了）
        keep = []
        used = 0
        for at, msg_id, who, body in reversed(turns):
            if not body:
                continue
            cost = len(at[5:16]) + 1 + len(who) + 2 + len(body) + 1
            if keep and used + cost > budget:
                break
            keep.append((at, msg_id, who, body))
            used += cost
        turns = list(reversed(keep))
    out = ["%s %s：%s" % (at[5:16], who, body) for at, _, who, body in turns if body]
    return "\n".join(out)
