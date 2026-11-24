"""旧系统（驱动层）与新大脑之间**唯一**的接触面。

三条规矩：
1. 只有本文件允许 import 旧代码；`loop/` 其它文件一律不许（tests 会红）。
2. 全部 async，全部返回 `Result`，**永不抛异常**——错误进 `Result.error`。
3. 这里只出现"能力"：看见世界 / 展开 ref / 动手 / 说话 / 查人。
   **不许**出现"该不该回""要不要说"这类判断——那是她的事。
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import sys
import time
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from .safelog import warn
from core.infra import liveness  # 心跳与判死只有一处实现
from core.conversation import media_ref
from core.infra.envflags import text as _text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_XML_CONTENT = re.compile(r"<content>(.*?)</content>", re.S)

_SYSTEM_KIND = re.compile(r'<sysmsg[^>]*type="([a-z_]+)"')
_SYSTEM_LABELS = {"revokemsg": "有人撤回了一条消息", "sysmsgtemplate": "群公告/系统提示",
                   "pat": "拍了拍", "roomtool": "群工具提示", "delchatroommember": "有人退群/被移出群"}

@dataclass
class Row:
    msg_id: int
    chat_id: str
    chat_type: str
    speaker: str
    at: str
    text: str
    is_self: bool = False
    kind: str = "text"
    raw: str = ""            # 未清洗的正文（用于判断有没有叫我）
    quote_user: str = ""     # 引用的是谁/哪个会话
    media_path: str = ""     # 图片/表情包在磁盘上的文件（图片能 OCR 出字）
    # 2026-11-04：真实发送者 id（wxid）。speaker 是**给人看的显示名**，不能拿来比身份；
    # 空串＝采集层还没落 who_id（见 db/world_db.py::sense_rows 与 E-42/E-44）。
    sender_id: str = ""
    quote_id: str = ""
    platform_msg_id: str = ""
    quote_targets_self: bool = False


@dataclass
class Result:
    ok: bool
    text: str = ""
    error: str = ""
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 库

def db_paths() -> list[str]:
    pat = os.path.join(ROOT, "data", "platforms", "*", "*", "account.db")
    return sorted(glob.glob(pat))


def pick_db(account: str | None = None) -> str | None:
    paths = db_paths()
    if not paths:
        return None
    if account:
        for p in paths:
            if os.path.basename(os.path.dirname(p)) == account:
                return p
        return None
    return max(paths, key=os.path.getsize)


def _connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------- 名字

_NAMES: dict[str, dict[str, str]] = {}
_GROUP: dict[str, set[str]] = {}


# 系统自带会话：contacts 里压根没有，但人人都有，别让它们掉进「未标注」
_SYSTEM_NAMES = {
    "filehelper": "文件传输助手",
    "weixin": "微信团队",
    "notifymessage": "微信团队",
    "newsapp": "腾讯新闻",
    "qqmail": "QQ邮箱提醒",
    "medianote": "语音记事本",
    "floatbottle": "漂流瓶",
    "fmessage": "朋友推荐消息",
}

# 发给自己用的会话（微信「文件传输助手」这类）：当收藏夹看，不是等她回话的人
# 2026-10-24：旧账/旧 memo 里存过的兜底名 → 真 chat_id（"未标注的会话…helper" 其实是文件传输助手）
_LEGACY_LABELS = {"未标注的会话…helper": "filehelper", "未标注的会话…weixin": "weixin",
                   "helper": "filehelper"}  # helper 是 filehelper 被截尾 6 位后的样子


SELF_CHATS = {"filehelper"}


def is_self_chat(chat_id: str) -> bool:
    return str(chat_id or "").strip() in SELF_CHATS


def _label_for_uncached(target: str) -> str:
    """账里的 target（chat_id、工具名、"win:名字"）→ 给人看的名字；翻不出就返回空串。

    2026-10-24：账里 target 统一存 chat_id，显示名改成读取时解析——
    这样群改名之后历史账不会跟着失真（以前直接把显示名写进了账）。
    """
    t = _strip_wrap(str(target or "").strip())
    for pre in ("win:", "msg:"):
        if t.startswith(pre):
            t = t[len(pre):]
            break
    if not t:
        return ""
    try:
        db = _account_db()
        names = display_names(db)
    except Exception:  # noqa: BLE001 - 翻不出名字不算错
        return ""
    t = _LEGACY_LABELS.get(t, t)
    cid = t if t in names else (resolve_chat_id(t) or "")
    if not cid:
        try:
            from core.execution import chat_alias as _alias

            _k = _alias.key_of(t)          # 改名之后，旧名字也能反查到 id
            cid = _k if _k in names else ""
        except Exception:  # noqa: BLE001
            cid = ""
    if not cid:
        return ""
    _label = chat_label(db, cid)
    try:
        from core.execution import chat_alias as _alias2

        _alias2.learn(cid, _label, source="db")
    except Exception:  # noqa: BLE001
        pass
    return _label



def _remember_aliases(names: dict[str, str]) -> None:
    """把 contacts 里的名字抄进别名簿：改名之后还能凭旧名字反查到 chat_id。

    批量一次落盘（302 条逐个写会把 tick 拖住），失败一律静默——
    别名簿只是记忆的延伸，坏了也不能影响"她叫什么"。
    """
    try:
        from core.execution import chat_alias as _alias

        _alias.learn_many(names.items(), source="db")
    except Exception:  # noqa: BLE001
        pass


def display_names(db: str) -> dict[str, str]:
    """contacts 表：wxid → 备注名优先，其次昵称。同一个人在群里和私聊里同名。"""
    if db in _NAMES:
        return _NAMES[db]
    out: dict[str, str] = {}
    groups: set[str] = set()
    try:
        con = _connect(db)
        for r in con.execute(
                "select wxid, coalesce(nickname,''), coalesce(remark,''), coalesce(is_group,0) "
                "from contacts"):
            wxid = (r[0] or "").strip()
            name = (r[2] or "").strip() or (r[1] or "").strip()
            if not wxid:
                continue
            if wxid.endswith("@chatroom") or int(r[3] or 0) == 1:
                groups.add(wxid)
            if name:
                out[wxid] = name
        con.close()
    except Exception:  # noqa: BLE001
        warn("ports.display_names.Exception", err=repr(__import__("sys").exc_info()[1]))
    for _k, _v in _SYSTEM_NAMES.items():
        out.setdefault(_k, _v)
    _NAMES[db] = out
    _GROUP[db] = groups
    _remember_aliases(out)
    return out


def is_group_chat(db: str, chat_id: str) -> bool:
    if str(chat_id).endswith("@chatroom"):
        return True
    display_names(db)
    return chat_id in _GROUP.get(db, set())


def chat_label(db: str, chat_id: str) -> str:
    """会话的显示名。查不到就按 id 形状留个能认的占位，绝不编一个真名。

    注意：以前这里取 id 末 6 位，`filehelper` 会变成「未标注的会话…helper」，
    看着像报错。系统号先查 `_SYSTEM_NAMES`，群和私聊各自带前缀。
    """
    cid = str(chat_id or "").strip()
    name = display_names(db).get(cid)
    if name:
        return name
    if cid in _SYSTEM_NAMES:
        return _SYSTEM_NAMES[cid]
    if cid.endswith("@chatroom"):
        return "群 %s…" % cid[:6]
    if cid.startswith("wxid_"):
        return "私 %s" % cid[5:11]
    if cid:
        return "未标注的会话 %s" % cid[:12]
    return "未标注的会话"


def resolve_chat(db: str, ref: str) -> str | None:
    """把「P5」这种名字换成 chat_id。先精确、再包含、最后放弃。"""
    ref = (ref or "").strip()
    if not ref:
        return None
    names = display_names(db)
    if ref in names:
        return ref
    for wxid, name in names.items():
        if name == ref:
            return wxid
    for wxid, name in names.items():
        if ref and (ref in name or name in ref):
            return wxid
    return None


# ---------------------------------------------------------------- 人话

def to_human(kind: str, content: str) -> str:
    """把数据库里存的原文变成她能读的一行。"""
    c = (content or "").strip()
    kind = (kind or "text").strip().lower()
    if kind == "system" or c.startswith("<?xml"):
        m = _XML_CONTENT.search(c)
        inner = (m.group(1).strip() if m else "")
        if not inner and c and not c.startswith("<"):
            # 2026-10-25：很多 system 行本来就是人话（「以上是打招呼的消息」「P4发起了语音通话」
            # 「"Elaina" 撤回了一条消息」），直接照抄即可，别降级成「一条系统消息」。
            return "（系统）" + c
        if not inner:
            # 没有 <content> 的系统消息（入群/被踢/群公告之类）：至少把她能看懂的类别说出来，
            # 别再往她眼睛里塞一坨 XML，也别只给「一条系统消息」这种零信息。
            t = _SYSTEM_KIND.search(c)
            inner = _SYSTEM_LABELS.get(t.group(1), "") if t else ""
        return "（系统）" + (inner if inner else "一条系统消息")
    if kind == "sticker":
        return "[表情包]"
    if kind == "image":
        return "[图片]"
    if kind == "file":
        return "[文件]"
    if kind == "forward":
        return "[聊天记录]"
    if kind == "link":
        # 链接卡片偶尔存的是 XML，一样走清洗；纯文本就当正文
        if c.startswith("<?xml") or c.startswith("<msg"):
            m = _XML_CONTENT.search(c)
            return (m.group(1).strip() if m else "") or "[链接]"
        return c or "[链接]"
    if kind == "pat":
        # 2026-10-25：原来只回「（拍了拍）」，499 条拍一拍的「谁拍谁」全丢了。
        # 微信把拍一拍存成正文：`"Elaina" 拍了拍 "P5"`，照抄即可，别自己加工。
        flat = re.sub(r"\s+", " ", c).strip()
        return ("（拍了拍）" + flat) if flat else "（拍了拍）"
    return c


# ---------------------------------------------------------------- 世界

_SELECT = ("select id, chat_id, chat_type, coalesce(sender_name,'') as speaker, created_at, "
           "coalesce(content,'') as content, is_self, coalesce(msg_type_name,'text') as kind, "
           "coalesce(quote_reply_user,'') as quote_user, coalesce(media_path,'') as media_path, "\
           "coalesce(quote_reply_id,'') as quote_id "
           "from messages ")


_NOPIC = "（画面内容难以辨认）"
_TASKBOOK = ("30字以内", "30 字以内", "禁止项", "用户想要", "目标：", "目标:")


_QUOTED_IMG_RE = re.compile(r"(\[引用 [^\]]{1,24}: )\[图片\]")
_STICKER_RE = re.compile(r"\[表情包:([^\]]+)\]")
# 2026-10-27：引用一张还没描述过的图时，宁可多等几秒也让她真看见；每拍最多一张。
_QUOTE_WAIT_S = 6.0


def _img_text(base: str, media_path: str, descs: dict) -> str:
    """把 [图片] 换成 [图片: 描述]；描述像"任务书"就不用它（2026-10-27 踩过）。"""
    name = os.path.basename(str(media_path or "").replace("\\", "/")).strip().lower()
    d = " ".join(str((descs or {}).get(name) or "").split())
    if not d or any(k in d for k in _TASKBOOK) or d == _NOPIC:
        return base
    return "[图片: %s]" % d[:160]


def _quote_text(text: str, media_path: str, descs: dict) -> str:
    """`[引用 P6: [图片]]` → `[引用 P6: [图片: 描述]]`。

    2026-10-27：微信把被引用那张图的 media_path 存在**引用行自己**身上，
    所以不用去库里找原消息，直接拿这一行的 media_path 换描述即可。
    """
    if "[引用" not in (text or ""):
        return text
    if "[图片]" not in text and not media_ref.has_locator(text):
        return text
    return media_ref.render_media_locators(text, descs=descs or {}, path=media_path,
                                           mode="wait", budget_s=_QUOTE_WAIT_S)


def _sticker_text(content: str, descs: dict, mode: str = "prefetch") -> str:
    """`[表情包:<md5 或 base64 token>]` → `[表情包: 描述]`。

    2026-10-27：`to_human` 对 sticker 一律给裸「[表情包]」，把 md5 一起丢了 ——
    她引用自己发的表情包时，模型连 token 里那句「睡觉咯」都看不到。这里把 md5
    捡回来：先查表情包缓存 / 描述表，没有就现问一次视觉，再不行退回 token 里的字。
    """
    try:
        from core.preprocess.emoji_recognition import replace_emoji_descriptions
    except Exception:  # noqa: BLE001 - 表情包模块坏了不该拖垮感知
        replace_emoji_descriptions = None
    text = replace_emoji_descriptions(str(content or "")) if replace_emoji_descriptions else ""
    m = _STICKER_RE.search(text or "")
    tok = (m.group(1) if m else "").strip()
    if not tok:
        return "[表情包]"
    if re.fullmatch(r"[0-9a-fA-F]{32}", tok):
        d = media_ref.describe_sticker(tok.lower(), descs=descs or {}, mode=mode,
                                       budget_s=_QUOTE_WAIT_S)
        return ("[表情包: %s]" % d) if d else "[表情包]"
    return "[表情包: %s]" % tok


def sticker_text(content: str, descs: dict | None = None, mode: str = "prefetch") -> str:
    """表情包一行的人话（snapshot 复用：Row.text 已经被 to_human 抹成裸「[表情包]」）。

    ``mode`` 默认 prefetch：翻历史时不该为她等视觉模型，后台补上、下一拍就看得见。
    """
    return _sticker_text(content, descs or {}, mode)


_DESC = {"at": 0.0, "db": "", "map": {}}
_DESC_TTL_S = 30.0


def _descs(db: str) -> dict:
    """这一库里的图片描述（30 秒缓存：每拍最多查一次库）。"""
    now = time.time()
    if _DESC["db"] == db and now - float(_DESC["at"]) < _DESC_TTL_S:
        return _DESC["map"]
    m = media_descriptions(db, limit=2000)
    _DESC.update({"at": now, "db": db, "map": m})
    return m


def _rows(raw, descs: dict | None = None) -> list[Row]:
    out: list[Row] = []
    for r in raw:
        _mp = (r["media_path"] or "") if "media_path" in r.keys() else ""
        _text = to_human(r["kind"], r["content"])
        if r["kind"] in ("image", "quote_image"):
            _text = _img_text(_text, _mp, descs or {})
        elif r["kind"] in ("sticker", "emoji"):
            _text = _sticker_text(r["content"], descs or {})
        elif r["kind"] == "file" and media_ref.is_image_path(_mp):
            # 2026-10-27: 微信常把动图/图片当「文件」发，to_human 只看 kind 就塌成
            # 「[文件]」—— 本地文件明明在，她却被蒙在鼓里。按后缀认回图片来。
            _text = _img_text("[图片]", _mp, descs or {})
        elif "[引用" in (_text or ""):
            _text = _quote_text(_text, _mp, descs or {})
        out.append(Row(
            msg_id=int(r["id"]), chat_id=r["chat_id"] or "",
            chat_type=r["chat_type"] or "",
            speaker=(r["speaker"] or "").strip(),
            # 2026-10-25：留到秒；分钟粒度会让同一分钟内"谁先说"判不出来
            at=(r["created_at"] or "")[:19],
            text=_text,
            # 2026-10-24：发给自己用的会话（收藏夹）里，微信会把一部分消息记成 is_self=0
            # （sender 其实是 filehelper 自己），于是被当成"对方在跟我说话"。这里一律算自己的。
            is_self=(bool(r["is_self"]) or is_self_chat(r["chat_id"])),
            kind=(r["kind"] or "text"),
            raw=(r["content"] or ""), quote_user=(r["quote_user"] or ""),
            media_path=_mp,
            sender_id=(str(r["sender_id"] or "") if "sender_id" in r.keys() else ""),
            quote_id=(str(r["quote_id"] or "") if "quote_id" in r.keys() else ""),
            platform_msg_id=(str(r["platform_msg_id"] or "") if "platform_msg_id" in r.keys() else "")))
    return out


def sense_sync(db: str, *, since_hours: int = 168, limit: int = 400,
               chat_id: str = "") -> list[Row]:
    """她这一拍能看到的世界（读 data/world.db）。

    2026-10-28：唯一读口。旧路（直连 account.db 的 messages）已删除 —— 两套真相
    会让"谁在说话"出现两种答案。要回退请用 git/备份 ports.py.bak-rmoldpath。
    失败不抛：驱动层永远给一个列表，宁可这一拍看见得少，也不能让拍子炸掉。
    """
    try:
        from db import world_db as _wdb
        return _rows(_wdb.sense_rows(db, since_hours=since_hours, limit=limit, chat_id=chat_id), _descs(db))
    except Exception:  # noqa: BLE001 - 驱动层永远不抛
        warn("ports.sense_sync.Exception", err=repr(__import__("sys").exc_info()[1]))
        return []


async def sense(db: str, *, since_hours: int = 168, limit: int = 400) -> list[Row]:
    return sense_sync(db, since_hours=since_hours, limit=limit)


async def recent_in_chat(db: str, chat_id: str, *, limit: int = 20) -> list[Row]:
    """某个会话最近的几句话（回看用）。2026-10-28：也走 world.db，只留一个读口。"""
    try:
        from db import world_db as _wdb
        return _rows(_wdb.sense_rows(db, since_hours=24 * 365, limit=limit, chat_id=chat_id), _descs(db))
    except Exception:  # noqa: BLE001
        warn("ports.recent_in_chat.Exception", err=repr(__import__("sys").exc_info()[1]))
        return []


def media_descriptions(db: str, *, limit: int = 400) -> dict[str, str]:
    """这一库里已经存好的图片描述：{小写文件名: 描述}。

    描述由入站那一步的视觉模型写进 `message_media.description`（见
    core/ingestion/media_store.py::_store_row_description）。这里只读、永不抛：
    表不在或老库没有这一列，就给空表，`_text_of` 退回 OCR 或 [图片]。
    """
    try:
        con = _connect(db)
        raw = con.execute(
            "select local_path, description from message_media "
            "where description is not null and trim(description) <> '' "
            "order by id desc limit ?", (int(limit),)).fetchall()
        con.close()
    except Exception:  # noqa: BLE001 - 驱动层永远不抛
        return {}
    out: dict[str, str] = {}
    for r in raw:
        name = os.path.basename(str(r["local_path"] or "").replace("\\", "/")).strip().lower()
        if not name or name in out:
            continue
        out[name] = " ".join(str(r["description"]).split())[:160]
    return out


# ---------------------------------------------------------------- 能力

async def look(ref: str, *, db: str) -> Result:
    """展开一个 ref。win: 支持名字或 id；msg: 支持数据库 id。"""
    scope = current_read_scope()
    if scope is None or not all((scope.platform, scope.account_id, scope.chat_id)):
        return Result(False, error="missing_read_scope: 当前回合的会话或账号尚未确认")
    ref = (ref or "").strip()
    if ref.startswith("ref:"):          # 她可能照抄账里的写法
        ref = ref[4:].strip()
    if ref.isdigit():                   # 只给编号也认
        ref = "msg:" + ref
    try:
        if ref.startswith("win:"):
            target = ref[4:].strip()
            chat_id = resolve_chat(db, target) or target
            if chat_id != scope.chat_id:
                return Result(False, error="read_scope_denied: 当前回合不能展开其他会话")
            rows = await recent_in_chat(db, chat_id, limit=20)
            if not rows:
                return Result(False, error="这个会话里没有消息（名字或 id 可能不对）")
            lines = []
            for r in rows:
                who = "我" if r.is_self else (r.speaker or chat_label(db, chat_id))
                lines.append("%s %s: %s" % (r.at, who, r.text[:120]))
            head = "「%s」最近 %d 条：" % (chat_label(db, chat_id), len(rows))
            return Result(True, text=head + "\n" + "\n".join(lines),
                          data={"chat_id": chat_id, "rows": [r.__dict__ for r in rows]})
        if ref.startswith("msg:"):
            mid = ref[4:].strip()
            if not mid.isdigit():
                return Result(False, error="msg: 后面要给数字编号")
            con = _connect(db)
            raw = con.execute(_SELECT + "where id = ?", (int(mid),)).fetchall()
            con.close()
            if not raw:
                return Result(False, error="没有这条消息（编号可能不对）")
            if str(raw[0]["chat_id"] or "") != scope.chat_id:
                return Result(False, error="read_scope_denied: 消息不属于当前会话")
            r = _rows(raw)[0]
            who = "我" if r.is_self else (r.speaker or "?")
            return Result(True, text="%s %s: %s" % (r.at, who, r.text),
                          data={"row": r.__dict__})
        if ref.startswith("person:"):
            return await act("get_person_profile", {"person": ref[7:]})
        # 2026-11-07 P2：mem: 声明移除。原来这条路是「看起来查了、其实抛异常被吞」
        # （2026-10-26 发现是坏调用），方案点名要「接通或移除」。
        # 模型要查记忆用 read_memory / memory_recall / recall_memories，不走 ref。
        return Result(False, error="ref 形状不认识（要 win:/msg:/person:）")
    except Exception as exc:  # noqa: BLE001
        warn("ports.look.Exception", err=repr(exc))
        return Result(False, error=repr(exc))


ALLOWED_TASKS = {
    "create_reminder": "建提醒（什么时候、提醒什么）",
    "create_todo": "记一件待办（要做什么）",
    "list_todos": "看还有哪些待办没做",
    "complete_todo": "把某件待办标成做完",
    "get_current_time": "现在是几点、今天周几",
    "get_weather": "查天气（不填城市就用默认城市）",
    # 2026-10-25：她该会上网（用户要求）——查资料、读链接、记事
    "web_search": "查网上的实时消息（给关键词，返回标题/要点/链接）",
    "fetch_url": "给一个网址，读它的正文",
    # 2026-10-25：翻旧账 / 看人 / 看图（用户要求全接）
    "search_messages": "搜历史消息（关键词/某人/某个会话，可限时间范围）",
    # 2026-11-18：她终于有手读文档了。只读、只到 docs/ 与 output/（边界见 agents/tools/local_files.py）
    "read_file": "读项目里的一份文档/产出（只读；只到 docs/ 与 output/）",
    "memory_recall": "按问题查记忆；valid_at 查询指定有效时间的 Belief 事实",
    "read_memory": "按索引 key 读某条记忆全文（key 来自这一拍的[事件记忆·索引]）",
    "recall_thread": "按话题线程 id 召回整段讨论（id 来自 search_threads）",
    "get_contact_info": "查某人/某群：备注名、昵称、是否群聊、最近消息摘要",
    "analyze_image": "看图：文字、场景、物体、表情、情绪（不填 image_url 就用这一轮最新的图）",
    # 下面这些是"待人"要用的：发表情、读情绪、读态度、查画像、翻旧话题
    "read_emotion": "读某个人此刻的情绪（不加名字就是当前会话的人）",
    "get_stance_cards": "读我对某人的态度（亲近/疏远/该端着还是放开）",
    "search_threads": "按关键词把某件事的来龙去脉翻出来",
    "notify_user": "给主人推一条通知（不是聊天，是叫他）",
    # 2026-10-25：把注册表里她够不着、但确实用得到的三个接上（用户要求）
    "get_person_profile": "翻某个人的档案：我对他/她的观察、关系、记忆（person=wxid/昵称/备注）",
    "correct_emoji_desc": "给表情包写正确描述，下次就认得（md5 + correct_desc）",
    "decision_helper": "帮人拿主意：拆选项、列优缺点、给推荐",
    # 2026-10-25：再补两个只读工具（用户批准）：查 ACG 设定、看今日分析
    "acg_lookup": "查番剧/游戏角色的设定（性格、身世、萌点），比通用搜索准",
    "get_today_analysis": "看今日微信分析：谁找过我、群聊摘要、每日日报",
}


# 2026-10-25：动作不摊成一个个按钮，收进四族，按需翻目录（design_loop.html ③）
# 2026-10-25 晚：从 11 个扩到 14 个（加 web_search / fetch_url / write_note）
TASK_FAMILIES = {
    "提醒与待办": ["create_reminder", "create_todo", "list_todos", "complete_todo"],
    "认识人": ["get_contact_info", "get_person_profile"],
    "看人和态度": ["read_emotion", "get_stance_cards", "search_threads"],
    "猜表情": ["correct_emoji_desc"],
    "此刻的事实": ["get_current_time", "get_weather", "web_search", "fetch_url", "analyze_image", "acg_lookup"],
    "翻旧账": ["search_messages", "memory_recall", "read_memory", "recall_thread", "get_today_analysis"],
    "查项目文档": ["read_file"],
    "小动作": ["notify_user"],
    "给她看东西": ["show"],
    "帮我拿主意": ["decision_helper"],
}

TASK_ARGS = {
    "create_reminder": "content=提醒什么；remind_at=什么时候（可以写「明天早上九点」这种人话）",
    "create_todo": "title=要做什么",
    "list_todos": "（不用参数）",
    "complete_todo": "todo_id=哪一条（先 list_todos 看编号）",
    "get_current_time": "（不用参数）",
    "get_weather": "city=城市，可不填（默认城市）",
    "read_emotion": "who=谁，可不填（默认当前会话里的人）",
    "get_stance_cards": "who=谁，可不填",
    "search_threads": "query=关键词",
    "notify_user": "text=要告诉他什么",
    "show": "text=要给她看的那句话（可换行，一句一行，全张不超过 40 字最有分寸）；frames=2~5 表示会动（默认 1，静止）；target=发给谁（可留空＝当前会话）",
    "web_search": "query=要查什么（≤120 字，越具体越好）",
    "fetch_url": "url=网址（http/https）",
    "search_messages": "keyword=搜什么词；chat_id=哪个会话（可留空）；chat_type=private/group（可留空）；hours_back=往前找多少小时（默认 24，0=不限）",
    "read_file": "path=相对仓库根的路径（例如 docs/README.md）；max_chars=最多多少字（可留空）",
    "memory_recall": "query=问题；kinds=fact/episode/person/meme/quote（逗号分隔，可空）；valid_at=事实有效日期或日期时间（可空，提供时 kinds 只可 fact）；top_k=条数（默认5）",
    "read_memory": "keys=索引里的 key，多个用逗号（例如 m1,m3）",
    "recall_thread": "thread_id=话题线程 id（从 search_threads 拿）",
    "get_contact_info": "wxid=人名、群名或 wxid",
    "analyze_image": "image_url=图或留空；msg_id=指定某一条消息里的图（群里连发多张时用它）；question=想问它什么（可留空）",
    "get_person_profile": "person=谁（wxid/昵称/备注）；query=想了解什么（可选）；limit=条数（可选，默认 12）",
    "correct_emoji_desc": "md5=表情包 md5（32 位十六进制）；correct_desc=正确描述（2–6 个中文词）",
    "decision_helper": "question=在纠结什么；options=选项用 | 隔开（可空）；context=背景（可空）",
    "acg_lookup": "keyword=角色/条目名；work=作品名（可选，用来消歧）",
    "get_today_analysis": "days=看几天内的分析（默认 1）",
}


def catalog(family: str = "") -> Result:
    """翻工具目录。不填族：有哪几族；填了族：那一族每个动作要哪些参数。

    分族不是为了整齐，是因为十一个动作全摊在 do 的说明里她每拍都得读一遍；
    按族翻，省下的注意力留给「这一拍该不该开口」。容错：她只给动作名也认。
    """
    fam = (family or "").strip()
    if not fam:
        lines = ["[工具目录] 要动手先翻族，再用 do(name, args) 去办："]
        for _f, _names in TASK_FAMILIES.items():
            lines.append("- %s：%d 个 · %s" % (_f, len(_names), "、".join(_names)))
        lines.append("（要看某族每个动作的参数：catalog(family=\"" + list(TASK_FAMILIES)[0] + "\")）")
        return Result(True, text="\n".join(lines))
    names = TASK_FAMILIES.get(fam)
    if names is None:
        if fam in ALLOWED_TASKS:
            return Result(True, text="[%s] %s\n参数：%s"
                          % (fam, ALLOWED_TASKS[fam], TASK_ARGS.get(fam, "见描述")))
        return Result(False, error="没有这一族：%s（有：%s）" % (fam, "、".join(TASK_FAMILIES)))
    lines = ["[%s]" % fam]
    for _n in names:
        lines.append("- %s：%s\n  参数：%s" % (_n, ALLOWED_TASKS.get(_n, ""), TASK_ARGS.get(_n, "")))
    return Result(True, text="\n".join(lines))


@dataclass(frozen=True)
class ReadScope:
    platform: str
    account_id: str
    chat_id: str
    user_message: str = ""
    sender_id: str = ""
    db_path: str = ""
    visible_event_ids: tuple[str, ...] = ()

    @property
    def is_group(self) -> bool:
        return self.chat_id.endswith("@chatroom") or self.chat_id.startswith("group:")


from contextvars import ContextVar
_READ_SCOPE = ContextVar("loop_read_scope", default=None)


def current_read_scope() -> ReadScope | None:
    return _READ_SCOPE.get()


def _scoped_outbound_account(platform: str, chat_id: str) -> str:
    scope = current_read_scope()
    if scope and (scope.platform, scope.chat_id) == (platform, chat_id):
        return scope.account_id
    return ""


def turn_sender(rows, chat_id: str) -> str:
    """只有同一会话、唯一且非空的真实发言人可代表这批入站消息。"""
    peers = [r for r in rows if not getattr(r, "is_self", False)]
    if not peers or any(str(getattr(r, "chat_id", "") or "") != chat_id for r in peers):
        return ""
    senders = {str(getattr(r, "sender_id", "") or "").strip() for r in peers}
    return next(iter(senders)) if len(senders) == 1 and "" not in senders else ""


def scoped_turn(function):
    """回合入口提供读取身份；模型的参数、focus 和 look 结果不能修改它。"""
    import functools
    import inspect
    signature = inspect.signature(function)

    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        values = signature.bind(*args, **kwargs).arguments
        db = os.path.normpath(str(values.get("db") or ""))
        platform = str(values.get("platform") or "")
        parent = os.path.dirname(db)
        source_platform = os.path.basename(os.path.dirname(parent))
        account = os.path.basename(parent) if os.path.basename(db) == "account.db" and source_platform == platform else ""
        scope = ReadScope(platform, account, str(values.get("chat_id") or ""),
                          str(values.get("user_message") or ""), str(values.get("sender_id") or ""),
                          db_path=db,
                          visible_event_ids=tuple(values.get("visible_event_ids") or ()))
        token = _READ_SCOPE.set(scope)
        try:
            return await function(*args, **kwargs)
        finally:
            _READ_SCOPE.reset(token)
    return wrapped


async def act(name: str, args: dict) -> Result:
    """办事：把她的动作转给旧工具表里那几十个工具中的一个。

    只放行 ALLOWED_TASKS 里的少数几个——先让她会做最常见的几件事，
    不一次性把三十个按钮全摊开（那会把她的注意力吃掉）。
    """
    if name not in ALLOWED_TASKS:
        return Result(False, error="这个动作还没接上：%s。先翻目录看有哪几族：catalog()\n（现在能做的：%s）"
                      % (name, "、".join(ALLOWED_TASKS)))
    args = dict(args or {})
    args.pop("_chat", None)  # 旧的模型参数不能提供身份。
    scope = current_read_scope()
    if scope is None or not all((scope.platform, scope.account_id, scope.chat_id)):
        return Result(False, error="missing_read_scope: 当前回合的会话或账号尚未确认")
    chat_id = scope.chat_id
    for key, value in (("platform", scope.platform), ("account_id", scope.account_id)):
        if key in args:
            args[key] = value
    # 她的说法和工具的字段名不一定一样：这里做最小别名，别让她因为字段名失败
    if name == "get_person_profile" and "person" not in args and "name" in args:
        args["person"] = args.pop("name")
    if name == "search_threads" and "query" not in args and "keyword" in args:
        args["query"] = args.pop("keyword")
    if name == "web_search" and "query" not in args and "keyword" in args:
        args["query"] = args.pop("keyword")
    if name == "search_messages" and "keyword" not in args and "query" in args:
        args["keyword"] = args.pop("query")
    if name == "get_contact_info" and "wxid" not in args:
        for _k in ("name", "contact", "who"):
            if _k in args:
                args["wxid"] = args.pop(_k)
                break
    if name == "recall_thread" and "thread_id" not in args and "id" in args:
        args["thread_id"] = args.pop("id")
    if name == "read_memory" and "keys" not in args and "key" in args:
        args["keys"] = args.pop("key")
    try:
        from core.bootstrap import get_tools, init_singletons
        try:
            init_singletons(validate=False, allow_production=True)
        except Exception:
            warn("ports.act.Exception", err=repr(__import__("sys").exc_info()[1]))
        tools = get_tools()
        if callable(tools) and not hasattr(tools, "get"):
            tools = tools()
        tool = tools.get(name)
    except Exception as exc:
        warn("ports.act.Exception", err=repr(exc))
        return Result(False, error="工具表拿不到：%r" % (exc,))
    if tool is None:
        return Result(False, error="没有这个工具：%s" % name)
    try:
        from agents.base import AgentContext
        ctx = AgentContext(messages=[], extra={
            "chat_id": scope.chat_id, "platform": scope.platform, "is_group": scope.is_group,
            "account_id": scope.account_id, "user_message": scope.user_message,
            "sender_id": scope.sender_id,
        })
        from core.terminal.authority import active_context_principal
        principal = active_context_principal(ctx)
        if name == "read_file" and not principal.is_owner:
            return Result(False, error="项目文件只允许主人读取")
        if hasattr(tool, "set_context"):
            tool.set_context(ctx)
        res = await tool.execute(**args)
    except TypeError as exc:
        # 2026-10-25：不少工具用位置参数（如 correct_emoji_desc(md5, correct_desc)），
        # 她少给一个字段就 TypeError。原来只回一句「崩了：TypeError(...)」，她没法自己修。
        _msg = str(exc)
        if "required positional argument" in _msg or "unexpected keyword argument" in _msg:
            _need = list((getattr(tool, "parameters", {}) or {}).keys())
            warn("ports.act.missing_args", tool=name, err=_msg[:120])
            return Result(False, error="%s 缺参数或参数名不对：%s。这个动作要的参数：%s（先 catalog() 看一眼）"
                          % (name, _msg[:160], "、".join(_need) or "（无）"))
        warn("ports.act.TypeError", err=_msg[:160])
        return Result(False, error="%s 崩了：%r" % (name, exc))
    except Exception as exc:
        warn("ports.act.Exception", tool=name, err=repr(exc))
        return Result(False, error="%s 查询或执行失败：%r" % (name, exc))
    err = str(getattr(res, "error", "") or "")
    out = getattr(res, "output", None)
    if out is None:
        out = getattr(res, "content", "") or ""
    out = str(out)
    if err:
        return Result(False, error="%s 失败：%s" % (name, err))
    return Result(True, text="[%s] %s" % (name, out))



_ACC_DB: dict = {"path": "", "at": 0.0}
_ACC_DB_TTL_S = 30.0


def _account_db() -> str:
    """当前账号的账库。取消息最多的那个——就是鲸鲸本号。

    2026-10-25：缓存 30 秒。原来每调一次就 glob + 对每个库 count(*)，
    而 label_for()（账页每一行都要翻名）会调它 —— 一次请求撞几百次，接口因此慢到 4–9 秒。
    """
    import glob
    import time as _t

    scope = current_read_scope()
    if scope is not None and scope.db_path:
        return scope.db_path if os.path.isfile(scope.db_path) else ""
    _now = _t.time()
    if _ACC_DB["path"] and _now - float(_ACC_DB["at"]) < _ACC_DB_TTL_S:
        return str(_ACC_DB["path"])
    cands = glob.glob(os.path.join(ROOT, "data", "platforms", "wechat", "*", "account.db"))
    best, best_n = "", -1
    for c in cands:
        try:
            n = sqlite3.connect(c).execute("select count(*) from messages").fetchone()[0]
        except Exception:
            warn("ports._account_db.Exception", err=repr(__import__("sys").exc_info()[1]))
            continue
        if n > best_n:
            best, best_n = c, n
    _ACC_DB["path"], _ACC_DB["at"] = best, _t.time()
    return best


def _recent_duplicate(chat_id: str, text: str, *, window_s: int = 20) -> bool:
    """同会话、同内容、20 秒内已经发过就不再发。

    2026-10-28：窗口从 120 秒收到 20 秒 —— 它防的是**机器重发**（回车没生效又补一次、
    重试风暴），不是"她想把同一句再说一遍"。人的重复是表达，机器的重复才是事故。
    """
    try:
        con = sqlite3.connect(_account_db())
        row = con.execute(
            "select 1 from send_log where chat_id=? and content=? "
            "and created_at >= datetime('now','localtime',?) limit 1",
            (chat_id, text, f"-{int(window_s)} seconds"),
        ).fetchone()
        con.close()
        return bool(row)
    except Exception:
        warn("ports._recent_duplicate.Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def _recent_similar(chat_id: str, text: str, *, window_s: int = 180, threshold: float = 0.82) -> bool:
    """2026-10-28 起**停用**（主人明确要求：允许她像人一样连说几句，不拦"意思像"）。

    原逻辑（保留在此供回滚参考）：同会话 180 秒内 difflib 相似度 >= 0.82 就挡。
    停用原因：她讲一件事时本来就会连发两三句、用词高度相似，这个闸把"续讲"
    当成"重复刷屏"。真正该防的是**完全相同**的重复（那由 _recent_duplicate 管，
    而且它防的是机器重发，不是她的表达）。
    """
    return False


def _recent_similar_off(chat_id: str, text: str, *, window_s: int = 180, threshold: float = 0.82) -> bool:
    """（旧实现，已停用，仅留作参考）同会话说过"意思很像"的话就挡一下。

    2026-10-25 事故：她在两分钟里连说 5 遍"图我看不清"，因为原来只挡**完全相同**
    的字符串，换个字就穿透。这里用 difflib 比相似度。短于 8 字的不参与，
    避免把「啊？」这类正常反应也挡掉；token 级重叠也一起看，防止只换虚词。
    """
    t = str(text or "").strip()
    if len(t) < 8:
        return False
    # 2026-10-28：她讲一个故事/一件事时会连发几句，每句都跟上一句"很像"（同一主题、
    # 同样的词），原来 10 分钟窗 + 0.66 阈值会把续篇当成重复直接拦掉——22:30 那三条
    # 就是这么丢的。续讲的判据：多行（真的在分句说）或者跟上一条首 6 字完全不同。
    if t.count(chr(10)) >= 1:
        return False
    try:
        con = sqlite3.connect(_account_db())
        rows = con.execute(
            "select content from send_log where chat_id=? "
            "and created_at >= datetime('now','localtime',?) order by id desc limit 8",
            (chat_id, f"-{int(window_s)} seconds"),
        ).fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return False
    import difflib
    def _tok(z):
        return set(z.replace("，", " ").replace("。", " ").replace("？", " ").replace("！", " ").split())
    _tt = _tok(t)
    for (old,) in rows:
        old = str(old or "").strip()
        if len(old) < 8:
            continue
        if difflib.SequenceMatcher(None, old, t).ratio() >= threshold:
            return True
        _ot = _tok(old)
        if _tt and _ot and len(_tt & _ot) / float(len(_tt | _ot)) >= 0.6:
            return True
    return False


def _repeat_guard(chat_id: str, *, window_s: int = 600, mine_limit: int = 3) -> bool:
    """2026-10-28 起**停用**（允许刷屏；对方不回是对方的事，不是她该被消音的理由）。

    原逻辑：同会话 10 分钟内她已说 >= 3 句、而对方一个字都没打就挡住。
    """
    return False


def _repeat_guard_off(chat_id: str, *, window_s: int = 600, mine_limit: int = 3) -> bool:
    """（旧实现，已停用）同会话 10 分钟内我已经说了 >= 3 句、而对方**一个字都没打**（只发表情包/图片）就挡住。

    2026-10-25 事故：两分钟里连说 5 遍"图我看不清"。字符串相似度抓不住这种
    "换着说法重复同一件事"——中文同义改写后 bigram 重合度很低。真正稳定的信号是
    行为层面：**我一个人在说话，对方只丢表情**。这不是判断她该不该说（那交给她），
    而是防止同一件事被反复送往同一个不开口的人。
    """
    try:
        con = sqlite3.connect(_account_db())
        mine = con.execute(
            "select count(*) from send_log where chat_id=? "
            "and created_at >= datetime('now','localtime',?)",
            (chat_id, f"-{int(window_s)} seconds"),
        ).fetchone()[0]
        if mine < mine_limit:
            con.close()
            return False
        peer_txt = con.execute(
            "select count(*) from messages where chat_id=? and is_self=0 "
            "and msg_type_name in ('text','quote_reply','link','file','forward') "
            "and created_at >= datetime('now','localtime',?)",
            (chat_id, f"-{int(window_s)} seconds"),
        ).fetchone()[0]
        con.close()
        return int(peer_txt or 0) == 0
    except Exception:  # noqa: BLE001
        warn("ports._repeat_guard.Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def resolve_chat_id(name: str) -> str:
    """把「Elaina」「示例宿舍群」这样的名字翻成 chat_id。

    认不出就返回空串——绝不把名字当 id 发出去（2026-10-24：名字被当成 chat_id，
    守卫认不出是群，差点往一个不存在的会话发）。
    """
    name = ref_name(name)

    n = (name or "").strip()
    if not n:
        return ""
    # 2026-10-24：她会把视界里的 ref 原样抄回来（win:名字 / person:名字 / msg:…），
    # 先把 ref 前缀剥掉再认，否则永远认不出、账里就会存一堆打不开的 ref。
    for _pre in ("win:", "person:", "msg:"):
        if n.startswith(_pre):
            n = n[len(_pre):].strip()
            break
    if not n:
        return ""
    n = n.replace("（发给自己）", "").strip()
    n = _LEGACY_LABELS.get(n, n)
    if "@chatroom" in n or n.startswith("wxid_"):
        return n
    try:  # 本身就是 chat_id 的（filehelper 这类系统号也在这张表里）
        if n in display_names(_account_db()):
            return n
    except Exception:  # noqa: BLE001
        pass
    # 1) 反查显示名（和她在视界里看到的名字一致）
    try:
        for cid, label in display_names(_account_db()).items():
            if label == n or label.endswith(n) or n.endswith(label):
                return cid
    except Exception:  # noqa: BLE001
        warn("ports.resolve_chat_id.Exception", err=repr(__import__("sys").exc_info()[1]))
    # 2) 通讯录备注 / 昵称
    try:
        con = sqlite3.connect(_account_db())
        row = con.execute(
            "select wxid from contacts where (remark=? or nickname=?) "
            "and wxid is not null and wxid<>'' limit 1", (n, n)).fetchone()
        con.close()
        if row:
            return row[0]
    except Exception:  # noqa: BLE001
        warn("ports.resolve_chat_id.Exception", err=repr(__import__("sys").exc_info()[1]))
    return ""

def _say_target_allowed(chat_id: str) -> tuple[bool, str]:
    """说话前的白名单（2026-11-04「全 C」地基③）：这个会话真的有过往来吗。

    `resolve_say_target` 只保证"名字认得出、且不重名"；但认得出不等于该发。全 C 之后
    外发不再等点头，所以再补一条：微信账库里必须有这个会话的至少一条消息。否则就是
    往一个从没来往过的 id 发 —— 要么发到空气里，要么台账记成办成了（E-59）。
    白名单自己查不了时**一律不判通过**（fail-closed）。
    """
    try:
        from . import targets as _tg

        if _tg.allowed(chat_id):
            return True, ""
        return False, "这个会话在微信里一条消息都没有 —— 不敢主动发过去"
    except Exception:  # noqa: BLE001
        warn("ports.say_target_whitelist_failed",
             err=repr(__import__("sys").exc_info()[1])[:160])
        return False, "白名单查不了，这一句不发"


def resolve_say_target(name: str) -> tuple[str, str]:
    """说话前认人：只用「完全相等」，认不出或认重了都拒绝。返回 (chat_id, 拒绝原因)。

    BR-069（fail-closed）：`resolve_chat_id` 允许 label.endswith(n) 这种前后缀模糊匹配，
    且按字典顺序返回第一个命中——她说「鱼」可能落到「大肥鱼」或「小肥鱼」里先出现的那个，
    发错人一句提示都没有。说话这条路不走模糊匹配：认不出就拒，认重了让她自己说清。
    """
    raw = (name or "").strip()
    if not raw:
        return "", "不知道对谁说：target 为空"
    n = ref_name(raw)
    for _pre in ("win:", "person:", "msg:"):
        if n.startswith(_pre):
            n = n[len(_pre):].strip()
            break
    n = n.replace("（发给自己）", "").strip()
    if not n:
        return "", "不知道对谁说：target 为空"
    n = _LEGACY_LABELS.get(n, n)
    if "@chatroom" in n or n.startswith("wxid_"):
        _ok, _why = _say_target_allowed(n)
        if not _ok:
            return "", "「%s」%s" % (n[:24], _why)
        return n, ""
    hits = []
    try:
        for cid, label in display_names(_account_db()).items():
            if label == n or cid == n:
                hits.append(cid)
    except Exception:  # noqa: BLE001
        warn("ports_resolve_say_target_failed", err=repr(__import__("sys").exc_info()[1]))
    try:
        con = sqlite3.connect(_account_db())
        try:
            rows = con.execute(
                "select wxid from contacts where (remark=? or nickname=?) "
                "and wxid is not null and wxid<>''", (n, n)).fetchall()
        finally:
            con.close()
        for row in rows:
            hits.append(str(row[0]))
    except Exception:  # noqa: BLE001
        warn("ports_resolve_say_target_failed", err=repr(__import__("sys").exc_info()[1]))
    uniq = []
    for cid in hits:
        if cid and cid not in uniq:
            uniq.append(cid)
    if len(uniq) == 1:
        _ok, _why = _say_target_allowed(uniq[0])
        if not _ok:
            return "", "「%s」认得出，%s" % (n[:24], _why)
        return uniq[0], ""
    if not uniq:
        return "", "认不出「%s」。要用视界里显示的名字，或直接给 chat_id。" % n[:24]
    return "", "「%s」对得上 %d 个会话（%s），不敢猜——说清是哪一个。" % (n[:24], len(uniq), "、".join(uniq[:3]))

def _self_proof(chat_id: str, text: str, started: float) -> str:
    """旧参数缺账号及操作绑定；正文前缀不能生成发送凭据。"""
    return ""


def _self_proof_window(chat_id: str, at: float, *, back: int = 30, fwd: int = 180) -> str:
    """时间相近不能证明本次发送。"""
    return ""


def _maybe_voice(text: str, *, chat_id: str, platform: str) -> None:
    """文字已经发出去了，顺手问一句"要不要同时递段声音"。

    2026-10-27：主人要求把语音接进说话路径。默认 off（`YINYUE_VOICE_MODE`），
    而且微信/QQ 目前都**发不出语音条**（见 core/speech/voice_send.py 的 CAPABILITIES），
    所以这里永远只做"决策 + 合成 + 留痕"，绝不阻塞、绝不抛异常。
    """
    try:
        from . import voice_out

        if voice_out.mode() == "off":
            return
        card = {}
        try:
            from .selfcard import active_card

            card = active_card() or {}
        except Exception:  # noqa: BLE001
            card = {}
        voice_out.maybe_send(text, platform=platform, chat_id=chat_id, card=card)
    except Exception as exc:  # noqa: BLE001
        warn("ports._maybe_voice.Exception", err=repr(exc))


def _confirm_later(chat_id: str, text: str, turn_id: str, started: float,
                   delay_s: int | None = None) -> None:
    """无操作绑定不启动旧确认线程；可靠观察复用 desk 绑定链。"""
    warn("ports_confirmation_binding_missing", chat_id=chat_id, turn_id=turn_id)


def _strip_wrap(reason: str) -> str:
    """把"刚被拦过…：…"这种层层包起来的理由剥到最里面那层。"""
    r = (reason or "").strip()
    while r.startswith("刚被拦过"):
        i = r.find("：")
        if i < 0:
            break
        r = r[i + 1:].strip()
    return r


def split_bubbles(text: str, *, max_parts: int | None = None) -> list[str]:  # 2026-10-31：主人要求取消上限（原 max_parts=3）
    """把一句话拆成几个气泡（微信里连发几条，像人打字那样）。

    2026-10-25：实测她的「一口气」中位数 66 字，人的是 9 字——不是她话多，
    是她的对话是一次性的（一拍一次机会），只能把七口气压成一口。
    拆成 2~3 条、每条十几字，才是人打字的样子。
    分隔：空行，或单独一行的 ||。不额外切句子——切在哪她说了算。
    """
    t = str(text or "").replace("\r\n", "\n").strip()
    if not t:
        return []
    raw = []
    for chunk in t.split("\n\n"):
        for piece in chunk.split("\n||\n"):
            piece = piece.strip().strip("|").strip()
            if piece:
                raw.append(piece)
    if max_parts and len(raw) > max_parts:   # 多出来的并进最后一条，别发一串碎渣
        raw = raw[: max_parts - 1] + ["\n".join(raw[max_parts - 1:])]
    return raw


# ---------------------------------------------------------------- 发不出去时还能再试吗
_RETRY_HINTS = (
    "无法激活微信窗口", "激活微信窗口", "未能激活", "not_foreground",
    "搜索验证失败", "ocr", "OCR", "超时", "timeout", "Timed out",
    "窗口", "focus", "拒绝访问", "连接", "connect", "网络",
)
_NEVER_RETRY = (
    "no_row_in_db", "unconfirmed", "回读", "WECHAT_SEND", "WECHAT_UI",
    "=off", "被墙", "没有权限", "群里", "group",
)


def say_retryable(err: str) -> bool:
    """这句还能不能再试一次？

    2026-10-27：18:32:39 那拍三次 say 都因为"前台被抢"直接失败，三句话永久丢失 ——
    她以为说了、其实没有。这里只重试「动作没做完」的失败（窗口/OCR/连接/超时）；
    「可能已经发出去了」的失败（no_row_in_db、unconfirmed、回读）**绝不重试**，
    否则就会变成同一条发两遍。
    """
    e = str(err or "")
    if not e:
        return False
    low = e.lower()
    if any(k.lower() in low for k in _NEVER_RETRY):
        return False
    return any(k.lower() in low for k in _RETRY_HINTS)


#: 被说话白名单拦下的句子单独记一份，给控制台「最近被拦下的话」看（2026-11-05，主人「要」）。
BLOCKED_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "logs", "blocked.jsonl")


#: 旁证文件多大就轮一次：上限与保留份数**只在** `core/infra/log_rotate.py::DEFAULT_POLICIES`
#: 写一次（`blocked.jsonl` 那条），这里不再抄一份数字（抄两份就一定会漂）。
BLOCKED_LOG_NAME = "blocked.jsonl"
#: 这些原因属于「内容闸」——正文一个字都不许落盘：那正是闸要挡的东西。
BLOCKED_HIDDEN_PREFIXES = ("content:",)


def _log_blocked(chat_id: str, why: str, text: str, *, platform: str = "wechat",
                 turn_id: str = "", reason: str = "", ctx: dict | None = None) -> None:
    """记一行「这句话被一道闸挡下了」。

    为什么不写进 `outbound_queue`：那张表是"要发的东西"，被拦下的从来没打算发；
    写进去会让回读核对、重发、周报的凭据率全都算错。这里是**旁证**，不是队列。
    记不下来也不许影响"不发"这个结论（整个包在 try 里）。

    谁往这里写（2026-11-05 §40.6 第一条：一道闸一道闸地记，面板才分得清她为什么没说话）：
    白名单（``not_in_allowlist``）、内容闸（``content:*``）、去重（``duplicate`` / ``too_soon``）、
    自发闸（``spont:*``）。**内容闸那几类不记正文**（留空 + ``hidden=True``）。

    2026-11-05 §43.6 第三条：「只记结果」复盘不了。`turn_id` / `reason` / `ctx` 记下
    「这一拍为什么想说」（哪道闸、自发闸的 streak / 没有指向她时的 kind），
    但**仍然一个字都不记正文**（`content:*` 那条连 `text` 也置空）。
    """
    try:
        import json as _json
        import time as _time

        _why = str(why or "")
        _hidden = any(_why.startswith(_p) for _p in BLOCKED_HIDDEN_PREFIXES)
        rec = {"at": _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime()),
               "ts": _time.time(),
               "platform": str(platform or ""),
               "chat_id": str(chat_id or ""),
               "why": _why,
               "text": "" if _hidden else " ".join(str(text or "").split())[:120],
               "turn_id": str(turn_id or ""),
               "reason": str(reason or ""),
               "ctx": dict(ctx or {}),
               "hidden": _hidden}
        os.makedirs(os.path.dirname(BLOCKED_LOG), exist_ok=True)
        # 轮转交给 `core/infra/log_rotate`：它先试 rename、被占就报 busy（不截断）。
        # 这里**不自己 rename**：唯一写口那条纪律（`tests/pytest_style/test_jsonio_atomic.py`）
        # 只给「轮转/归档」开了白名单，而轮转的实现只有一个地方该有。
        try:
            from core.infra import log_rotate as _lr

            _how = _lr.rotate_if_needed(BLOCKED_LOG)
            if _how not in ("noop", "missing", "rename"):
                warn("ports.blocked_rotate", how=_how)
        except Exception as _rex:  # noqa: BLE001 - 轮不动就算了，不许因此丢掉这一行
            warn("ports.blocked_rotate_failed", err=repr(_rex)[:120])
        with open(BLOCKED_LOG, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as _exc:  # noqa: BLE001 - 见 docstring：记不下也不许改判
        warn("ports.blocked_log_failed", err=repr(_exc)[:120])


def policy_blocked(platform: str, chat_id: str) -> tuple[bool, str]:
    """这个会话能不能开口（说话白名单）。返回 (是否被拦, 原因)。

    2026-11-05 修：这道判定以前只装在 `say()` 里，而模型回话走的是 `say_many()`
    的「整组一次入队」快路（直接 `world_db.enqueue`）—— 于是控制台「律」里没勾的群
    她照样说话（示例宿舍群 18:04 那条就是这么出去的）。
    现在两条路都先过这里；**判定自己坏了按「不许说」处理**（fail-closed）：
    白名单读不了就放行，等于「配置一坏，哪里都能说」。
    """
    try:
        from loop import speak_policy as _sp

        _ok, _why = _sp.allowed(platform, str(chat_id or ""))
        return (not _ok), str(_why or "")
    except Exception as _exc:  # noqa: BLE001 - 见 docstring：失败必须收紧，不许放开
        warn("speak_policy.check_failed", err=repr(_exc)[:160])
        return True, "policy_check_failed"



async def say_many(parts, *, dry_run: bool = True, chat_id: str = "",
                   platform: str = "wechat", turn_id: str = "", wait_ack: bool = False,
                   gap: tuple = (0.35, 0.9), reason: str = "reply",
                   trial_group_id: str = "", trial_expires_at: float = 0) -> Result:
    """连发几个气泡：一条一条发，中间停 0.3~0.9 秒（像在一句一句打字；2026-10-27 提速）。

    单条失败就停——已经发出去的不回滚，也不重发（重复比漏发难收拾）。
    """
    import asyncio
    import random
    import time

    segs = [str(x).strip() for x in (parts or []) if str(x or "").strip()]
    if not segs:
        return Result(False, error="empty_text")
    _blocked, _pwhy = policy_blocked(platform, str(chat_id or ""))
    if _blocked:
        warn("speak_policy.blocked", chat=str(chat_id or "")[:40], why=_pwhy, bubbles=len(segs))
        _log_blocked(str(chat_id or ""), _pwhy, "\n".join(segs), platform=platform,
                     turn_id=turn_id, reason=reason,
                     ctx={"gate": "allowlist", "bubbles": len(segs)})
        return Result(False, error="not_in_allowlist 这个会话不在我的说话白名单里（%s）" % _pwhy,
                      data={"code": "not_in_allowlist", "why": _pwhy, "chat_id": chat_id})

    if trial_group_id and (dry_run or len(segs) != 1 or trial_expires_at <= time.time()
                           or not _scoped_outbound_account(platform, chat_id)):
        return Result(False, error="invalid_trial_send_scope")

    # 2026-10-28：整组一次入队 —— 组内 seq 决定顺序、TTL 决定全组成败，
    # desk 认领时用"组里还有更小的 seq 没走完就不许插队"来保序。
    if not dry_run:
        try:
            import os as _os

            from db import world_db as _w

            _chat = resolve_chat_id(str(chat_id or "")) or str(chat_id or "")
            # 2026-11-04（全 C）：同一句 120 秒不发第二遍 —— 整组按拼起来的正文判。
            try:
                from loop import outbound_gate as _og3
                _ok3, _why3 = _og3.check(_chat, "\n".join(segs), reason=reason)
                if not _ok3:
                    warn("outbound_gate.blocked", chat=_chat[:40], why=_why3, bubbles=len(segs))
                    _log_blocked(_chat, _why3, "\n".join(segs), platform=platform,
                                 turn_id=turn_id, reason=reason,
                                 ctx={"gate": "outbound", "bubbles": len(segs)})
                    return Result(True, text="\n\n".join(segs),
                                  data={"ack": "skipped", "why": _why3, "chat_id": _chat,
                                        "bubbles": len(segs)})
                _og3.remember(_chat, "\n".join(segs), reason=reason)
            except Exception as _exc3:  # noqa: BLE001 - 闸门坏了不能把话憋住
                warn("outbound_gate_failed", err=repr(_exc3)[:160])
            # 2026-11-01 修：这里原本写 `_time.time()`，而本函数只 `import time`、
            # 没有 `_time` 这个别名 —— 每次开口都抛 NameError，被下面 except 吞掉
            # 后静默退化成逐条发送。silent_warnings.log 里躺着 136 条这条错，
            # 最早一条是 2026-10-28 这个分支上线的当天：整组入队从来没成功过，
            # 组保序 / 整组 TTL / 整组成败 全都跟着一起没生效。
            if trial_group_id and not _scoped_outbound_account(platform, _chat):
                return Result(False, error="invalid_trial_send_scope")
            _gid = trial_group_id or "many:%s:%d" % (str(turn_id or "turn"), int(time.time() * 1000))
            _ttl = float(_text("YINYUE_QUEUE_TTL_S", "604800") or 604800)  # 2026-10-31：队列不再 10 分钟过期（原 600s）
            # 打字节奏：顺序由 claim_next 的 seq 保证，间隔靠 next_at 按 seq 错开。
            # 逐条发送那条老路是在循环里 sleep 出来的；整组入队没有循环，得在这里补。
            _gap_s = random.uniform(*gap) if len(segs) > 1 else 0.0
            _ids = _w.enqueue(_gid, _chat, [{"kind": "text", "text": x} for x in segs],
                              platform=platform, account_id=_scoped_outbound_account(platform, _chat), turn_id=str(turn_id or ""), ttl_s=_ttl,
                              stagger_s=_gap_s, **({"max_attempts": 1, "expires_at": trial_expires_at} if trial_group_id else {}))
            if trial_group_id and not _ids:
                return Result(False, error="owner_trial_window_already_used")
            return Result(True, text="\n\n".join(segs),
                          data={"bubbles": len(segs), "ack": "queued", "group_id": _gid,
                                "queue_ids": _ids, "chat_id": _chat, "ttl_s": _ttl})
        except Exception as _exc:  # noqa: BLE001
            warn("ports.say_many.enqueue.Exception", err=repr(_exc)[:200])
            if trial_group_id:
                return Result(False, error="trial_queue_outcome_unknown")
    sent = []
    ack = ""
    bubble_ms: list[int] = []
    gap_ms: list[int] = []
    last_timing = None
    # 2026-11-01：这条兜底分支是一条一条调 say()，每条各自入队、各有 group_id
    # （库里真实流量全是 `say:` 前缀，说明生产走的就是这里）。以前只把 ack 汇总
    # 出来，链接键全丢 —— 账本记着"她说了这句"，却再也扣不上队列里那几条，
    # 对账只能退到"5 分钟窗 + 文本前 14 字"去猜。这里把每条气泡的行号收集齐。
    _ids_all: list = []
    _gids_all: list = []
    for i, seg in enumerate(segs):
        _g = 0.0
        if i and not dry_run:
            _g = random.uniform(*gap)
            await asyncio.sleep(_g)
        gap_ms.append(int(_g * 1000))
        _t0 = time.time()
        r = await say(seg, dry_run=dry_run, chat_id=chat_id, platform=platform,
                      turn_id=turn_id, wait_ack=wait_ack)
        bubble_ms.append(int((time.time() - _t0) * 1000))  # 2026-10-27：每条气泡各几秒
        if not r.ok:
            # 半截失败也要把已经入队的行号带回去 —— 发出去的那几句得能对上账。
            return Result(False, error=str(r.error),
                          text="\n\n".join(sent + [seg]),
                          data={"bubbles_sent": len(sent), "failed_index": i,
                                "bubble_ms": bubble_ms, "gap_ms": gap_ms,
                                "queue_ids": _ids_all, "group_ids": _gids_all})
        sent.append(seg)
        _d = getattr(r, "data", None) or {}
        ack = str(_d.get("ack") or ack or "")
        if isinstance(_d, dict):
            for _qid in (_d.get("queue_ids") or []):
                _ids_all.append(_qid)
            if _d.get("group_id"):
                _gids_all.append(str(_d["group_id"]))
            if _d.get("timing"):
                last_timing = _d["timing"]
    return Result(True, text="\n\n".join(sent),
                  data={"bubbles": len(sent), "ack": ack or ("dry_run" if dry_run else "unconfirmed"),
                        "bubble_ms": bubble_ms, "gap_ms": gap_ms, "timing": last_timing,
                        # 链接键：队列行号最准（一笔一个 id），组号留着做交叉验证
                        "queue_ids": _ids_all, "group_ids": _gids_all})


async def show_file(target: str, path: str, *, note: str = "", dry_run: bool = False) -> Result:
    """把**已经在磁盘上的一个文件**发出去（照片、文档、压缩包都行）。

    2026-10-28：她原先只有 show（把字画成图），没有「发文件」这只手。
    安全边界（她不能说「发那个 /etc/passwd」）：
      1) 只允许发 ROOT 下面、且在这些目录里的文件：
         data/artifacts、data/persona、data/exports、docs、output；
      2) 单文件 <= 25 MB；
      3) 必须是普通文件、存在、可读。
    规则被挡时返回 ok=False + 明确原因，让她能如实说"这个我发不了"。
    """
    started = time.time()
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    allow_dirs = [os.path.join(root_dir, d) for d in
                  ("data/artifacts", "data/persona", "data/exports", "docs", "output")]
    raw = str(path or "").strip().strip('"')
    if not raw:
        return Result(False, error="no_path")
    full = raw if os.path.isabs(raw) else os.path.join(root_dir, raw)
    full = os.path.abspath(full)
    if not os.path.isfile(full):
        return Result(False, error="file_not_found: %s" % full)
    if not any(full.lower().startswith(os.path.abspath(d).lower()) for d in allow_dirs):
        return Result(False, error="path_not_allowed: 只能发 data/artifacts、data/persona、data/exports、docs、output 里的东西")
    try:
        size = os.path.getsize(full)
    except OSError as e:
        return Result(False, error="stat_failed: %s" % e)
    if size > 25 * 1024 * 1024:
        return Result(False, error="file_too_big: %.1f MB（上限 25 MB）" % (size / 1048576.0))
    chat_id = (resolve_chat_id(target) if target else "") or str(target or "")
    send_to = label_for(chat_id) or label_for(str(target or "")) or chat_id
    if dry_run:
        return Result(True, text=note, data={"path": full, "bytes": size, "send_to": send_to,
                                             "ack": "dry_run"})
    try:
        from core.execution.wx_sender import WxSender
        sender = WxSender.get_instance()
        out = await asyncio.to_thread(sender.send_file, send_to, full)
    except Exception as e:  # noqa: BLE001 - 发不出去就如实回，不崩这一拍
        return Result(False, error="send_file_failed: %s" % e)
    ok = bool((out or {}).get("ok"))
    return Result(ok, text=note,
                  error="" if ok else str((out or {}).get("error") or "send_file_failed"),
                  data={"path": full, "bytes": size, "send_to": send_to,
                        "ack": str((out or {}).get("ack") or "unconfirmed"),
                        "ms": int((time.time() - started) * 1000)})


async def show(target: str, text: str, *, frames: int = 1, footer: str = "", dry_run: bool = False) -> Result:
    """把一句话画成图发出去。

    frames>1 就是连着发几张，微信里看起来像在“长出来”——她说的“会动的”
    在微信里只能这么做：视频要转码、要审核、还会被压。

    2026-10-27：她说「那给你个会动的」时手上什么都没有。这条补的是手。
    """
    started = time.time()
    text = str(text or "").strip()
    if not text:
        return Result(False, error="nothing_to_draw")
    chat_id = (resolve_chat_id(target) if target else "") or str(target or "")
    # 发图走的是「搜名字 → 点进会话 → 文件对话框」这条路，微信的搜索框
    # 只认显示名，不认 chat_id。2026-10-27 实测：把 10000000001@chatroom
    # 打进搜索框，OCR 永远匹配不上，图根本发不出去。所以先翻成名字。
    send_to = label_for(chat_id) or label_for(str(target or "")) or chat_id
    frames = max(1, min(int(frames or 1), 5))
    try:
        from loop import draw as _draw
        paths = await asyncio.to_thread(_draw.render, text, frames=frames, footer=str(footer or ""))
    except Exception as e:  # noqa: BLE001 - 画不出来就是画不出来，别把一拍弄崩
        return Result(False, error="draw_failed: %s" % e)
    if dry_run:
        return Result(True, text=text, data={"files": paths, "frames": len(paths),
                                             "ack": "dry_run", "chat_id": chat_id})
    if not chat_id:
        return Result(False, error="no_target", data={"files": paths})
    try:
        from core.execution.wx_sender import WxSender
        sender = WxSender.get_instance()
    except Exception as e:  # noqa: BLE001
        return Result(False, error="no_sender: %s" % e)
    sent, errs = 0, []
    for p in paths:
        try:
            r = await asyncio.to_thread(sender.send_image, send_to, p)
        except Exception as e:  # noqa: BLE001
            errs.append("send_image: %s" % e)
            continue
        if isinstance(r, dict) and r.get("ok"):
            sent += 1
            try:
                from db import world_db as _wdb_img
                _wdb_img.record_outbound(chat_id, "\u56fe\uff1a%s" % os.path.basename(p),
                                         platform="wechat", method="wechat_image", ack="unconfirmed")
                _confirm_image_later(chat_id, "\u56fe\uff1a%s" % os.path.basename(p))
            except Exception:
                pass
        else:
            errs.append(str((r or {}).get("error") or "send_failed") if isinstance(r, dict) else "send_failed")
    ok = sent > 0 and sent == len(paths)
    return Result(ok, text=text,
                  data={"files": paths, "frames": len(paths), "chat_id": chat_id, "send_to": send_to,
                        "ack": "%d/%d" % (sent, len(paths)),
                        "send_ms": int((time.time() - started) * 1000)},
                  error="" if ok else "; ".join(errs[:2]))


async def say(text: str, *, dry_run: bool = True, chat_id: str = "",
              platform: str = "wechat", turn_id: str = "", wait_ack: bool = False, reason: str = "reply") -> Result:
    """真要开口：走旧出口（守卫 → 闸门 → 微信键鼠），一个字都不绕。

    故意不自己做键盘和窗口操作——那是有护栏的地方（账号/工具/内容三道守卫、
    输出闸门、send_log、回执）。新大脑只决定说什么，不碰怎么发。
    """
    # 单一出口的负缓存：刚被墙拦过的靶，30 分钟内不再尝试（省 token、防反复撞墙）
    try:
        from loop import ledger as _ledger
        _cid = resolve_chat_id(str(chat_id or "")) or str(chat_id or "")
        _blk = _ledger.blocked_targets()
        if _cid and _cid in _blk:
            return Result(False, error="blocked_recent:" + str(_blk[_cid])[:80])
    except Exception:  # noqa: BLE001 - 负缓存只是省钱，不能挡住正常发送
        warn("ports.say.Exception", err=repr(__import__("sys").exc_info()[1]))
    # 2026-11-05：白名单判定统一走 policy_blocked（say / say_many 两条路同一份口径）。
    _blocked, _pwhy = policy_blocked(platform, str(chat_id or ""))
    if _blocked:
        warn("speak_policy.blocked", chat=str(chat_id or "")[:40], why=_pwhy)
        _log_blocked(str(chat_id or ""), _pwhy, text, platform=platform,
                     turn_id=turn_id, reason=reason, ctx={"gate": "allowlist"})
        return Result(False, error="not_in_allowlist 这个会话不在我的说话白名单里（%s）" % _pwhy,
                      data={"code": "not_in_allowlist", "why": _pwhy, "chat_id": chat_id})
    if not text or not text.strip():
        return Result(False, error="empty_text")
    if dry_run:
        return Result(True, text=text, data={"dry_run": True, "chat_id": chat_id})
    # 2026-11-04（用户选「全 C」：不再停下来等点头）：只挡"发出去收不回还纯粹浪费"的那一类。
    # 判据在 loop/outbound_gate.py（同一句 120 秒不发第二遍；自发开口同一会话 300 秒一条）。
    try:
        from loop import outbound_gate as _og
        _gchat = resolve_chat_id(str(chat_id or "")) or str(chat_id or "")
        _ok, _gwhy = _og.check(_gchat, text, reason=reason)
        if not _ok:
            warn("outbound_gate.blocked", chat=_gchat[:40], why=_gwhy)
            _log_blocked(_gchat, _gwhy, text, platform=platform,
                         turn_id=turn_id, reason=reason, ctx={"gate": "outbound"})
            # 2026-11-04 ④ 主人抄送：被拦下的这类话（钱 / 地址 / 替人答应 / 时间承诺）
            # 她自己知道、主人不知道 —— 报一条。但**正文一个字都不许带**：那正是闸
            # 要挡的东西，抄送里再写一遍等于绕过去。
            try:
                from loop import owner_copy as _oc
                _oc.note("content_blocked", target=_gchat, detail=_gwhy)
            except Exception as _oexc:  # noqa: BLE001 - 抄送坏了不许影响主流程
                warn("owner_copy.content_blocked_failed", err=repr(_oexc)[:120])
            return Result(True, text=text,
                          data={"ack": "skipped", "why": _gwhy, "chat_id": _gchat})
        _og.remember(_gchat, text, reason=reason)
    except Exception as _gexc:  # noqa: BLE001 - 闸门坏了不能把话憋住
        warn("outbound_gate_failed", err=repr(_gexc)[:160])
    # 2026-10-28 动作队列第 4 步：say 只"排队"，不抓键鼠。
    # 键鼠线唯一入口是 loop/desk.py；回执语义因此从"已发出"变成"已排队"。
    try:
        import os as _os
        import time as _time

        from db import world_db as _w

        _chat = resolve_chat_id(str(chat_id or "")) or str(chat_id or "")
        _gid = "say:%s:%d" % (str(turn_id or "turn"), int(_time.time() * 1000))
        _ttl = float(_text("YINYUE_QUEUE_TTL_S", "604800") or 604800)  # 2026-10-31：队列不再 10 分钟过期（原 600s）
        _ids = _w.enqueue(_gid, _chat, [{"kind": "text", "text": str(text)}],
                          platform=platform, account_id=_scoped_outbound_account(platform, _chat), turn_id=str(turn_id or ""), ttl_s=_ttl)
        return Result(True, text=text,
                      data={"ack": "queued", "group_id": _gid, "queue_ids": _ids,
                            "chat_id": _chat, "ttl_s": _ttl})
    except Exception as _exc:  # noqa: BLE001
        # 队列坏了就退回老路：宁可慢，也不能因为新机制把话憋住
        warn("ports.say.enqueue.Exception", err=repr(_exc)[:200])
    try:
        from agents.base import AgentContext
        from core.speech.facade import get_speech_facade
        from core.speech.outbound import OutboundDraft, get_outbound_gateway
        from core.terminal.store_port import get_repo
        from core.speech.loop_turn import make_turn as _make_turn

        repo = get_repo()
        if chat_id and (_recent_duplicate(chat_id, text) or _recent_similar(chat_id, text)
                        or _repeat_guard(chat_id)):
            return Result(False, error="duplicate_within_120s 同一条两分钟内已经发过，不再重发")

        # 2026-10-25：不要把 who 换成显示名——`core/speech/guards.py:353` 的跨会话闸是
        # `draft.who != policy.target.chat_id` 就拦，喂人名会被当成"跨会话发送"直接挡掉
        # （凌晨那三条 say error「当前会话规则不允许跨会话发送」就是这么来的）。
        # 发送侧 `WxSender.send_text()` 自己会 `resolve_nickname(who)` 拿显示名去搜，这里给 id 就够。
        turn = _make_turn({
            "platform": platform, "chat_id": chat_id, "chat_type": "private",
            "content": text, "sender_name": "", "trigger_reason": "loop_say",
        })
        snapshot = get_speech_facade().resolve(turn, repo)
        draft = OutboundDraft(
            request_id=turn.request_id, kind="text", target=turn.target,
            account_choice="self", content=text, who=chat_id, mood_id="", emotion="",
        )
        ctx = AgentContext(messages=[], extra={
            "chat_id": chat_id, "is_group": False, "platform": platform,
            "user_message": text, "message_time": None,
        })
        import time
        started = time.time()
        res = await get_outbound_gateway().send_text(draft, snapshot, ctx)
        ok = bool(getattr(res, "ok", False))
        ack = ""
        data = getattr(res, "data", None) or {}
        _timing = data.get("timing") if isinstance(data, dict) else None
        if isinstance(data, dict):
            ack = str(data.get("ack") or "")
        # 关键约定：拿不到确认 ≠ 失败。发出去但没有回执，只能说 unconfirmed。
        if not ok:
            err = str(getattr(res, "error", "") or "")
            # 2026-10-27「冒了」事故：unconfirmed 这个词太宽，会把"根本没发出门"
            # 的提前返回也洗成成功。只认"确实按过回车"的证据。
            if "no_row_in_db" in err or "回读" in err:
                return Result(True, text=text, data={"ack": "unconfirmed", "note": err,
                                                     "send_ms": int((time.time() - started) * 1000)})
            return Result(False, error=err or "send_failed")
        # 先发、先记 unconfirmed；30 秒后由后台线程回查账库改成 confirmed。
        if ack == "confirmed":
            _maybe_voice(text, chat_id=chat_id, platform=platform)
            return Result(True, text=text, data={"ack": "confirmed",
                                                 "send_ms": int((time.time() - started) * 1000),
                                                 "timing": _timing})
        try:  # 2026-10-28 世界账本：按过回车就记一笔，事后用它判归属/追认
            from db import world_db as _wdb
            _wdb.record_outbound(chat_id, text, platform=platform,
                                 account_id=str(data.get("account_id") or "") if isinstance(data, dict) else "",
                                 turn_id=turn_id, method="wechat_gui", ack=str(ack or "unconfirmed"))
        except Exception:
            pass
        if turn_id:
            _confirm_later(chat_id, text, turn_id, started)
        _maybe_voice(text, chat_id=chat_id, platform=platform)
        return Result(True, text=text, data={"ack": ack or "unconfirmed",
                                             "send_ms": int((time.time() - started) * 1000),
                                             "timing": _timing})
    except Exception as exc:  # noqa: BLE001
        warn("ports.say.Exception", err=repr(exc))
        return Result(False, error=repr(exc))


def get_llm():
    """LLM 客户端（旧系统提供）。init_singletons 会碰 meta 库，必须声明服务入口。"""
    from core.bootstrap import get_llm as _get_llm
    from core.bootstrap import init_singletons
    init_singletons(validate=False, allow_production=True)
    return _get_llm()


# ---------------------------------------------------------------- 有没有人在叫我



_EMPTY_ALIASES_WARNED = {"n": 0}


def _warn_empty_aliases() -> None:
    """名单空着只提醒一次：这会让群里 @/拍/引用 全部认不出来（2026-10-27 起没兜底）。"""
    if _EMPTY_ALIASES_WARNED["n"] >= 1:
        return
    _EMPTY_ALIASES_WARNED["n"] += 1
    warn(
        "ports.me_aliases.empty",
        hint="data/persona/me_names.json 里没写名字：群里 @/拍/引用 都认不出来（★ 不会亮）",
        path=os.path.join(ROOT, "data", "persona", "me_names.json"),
    )


def me_aliases() -> tuple[str, ...]:
    """她在平台上的名字（别人怎么叫她）。

    "是不是在叫我"只认这个，**不看人设卡名**——卡名决定她怎么说话，
    这里的名字决定别人怎么喊她。取不到就退回旧硬编码，绝不让寻址瞎掉。
    """
    try:
        from core.identity.self_names import self_names as _sn

        got = tuple(_sn(pick_db()))
        if not got:
            _warn_empty_aliases()
        return got
    except Exception:  # noqa: BLE001 - 名单取不到不是致命错
        return ()


def toward_label(row) -> str:
    """这条消息"朝谁"——只给事实，不替她判断要不要回。

    2026-10-25 用户要求：群里不能"谁说话都算在叫我"，要标出朝谁；
    而且事实一律用"我"的视角写（「@ 了我」而不是「@ 了你」）。
    """
    from . import addressing

    al = me_aliases()
    why = addressing.named_reason(row, al)
    if why:
        return why.replace("你", "我")
    if addressing.mentions_self(row, al):
        return "这条提到了我的名字"
    return "这条没提到我的名字"


def address_tier(rows: list, is_private: bool) -> tuple:
    """有人需要她吗？只回答"有多少条同时满足"。

    私聊：最后一句是别人说的就算 ask（一对一本来就是对着说）。
    群聊（2026-10-25 用户定）：只有 @我 / 拍我 / 引用我的话 才亮 ★，
    只提到名字算 ·；其它一律不亮——她照样会被"世界变了"叫醒，
    只是不把别人的闲聊当成"有人在等我"。
    """
    peers = [r for r in rows if not r.is_self]
    if is_private:
        return ("ask", len(peers)) if peers else ("", 0)
    from . import addressing

    al = me_aliases()
    asks = sum(bool(addressing.named_reason(r, al)) for r in peers)
    if asks:
        return ("ask", asks)
    hits = sum(addressing.mentions_self(r, al) for r in peers)
    if hits:
        return ("mention", hits)
    return ("", 0)

_META_DB = {"path": "", "at": 0.0}
_META_DB_TTL_S = 60.0


def _meta_path() -> str:
    """元库（画像/身份/观察记录在这儿，账号库里没有）。

    2026-10-26：这个函数里原来引用了 `_META`——一个从来没定义过的名字，
    于是 `person:<名字>` 这条路每次都抛 NameError，被她自己接住当成"查不到"。
    """
    import time as _t
    if _META_DB["path"] and (_t.time() - float(_META_DB["at"])) < _META_DB_TTL_S:
        return str(_META_DB["path"])
    cand = os.path.join(ROOT, "data", "yinyue_meta.db")
    if not os.path.exists(cand):
        hits = sorted(glob.glob(os.path.join(ROOT, "data", "*", "yinyue_meta.db")))
        cand = hits[0] if hits else cand
    _META_DB["path"], _META_DB["at"] = cand, _t.time()
    return cand


def _meta():
    return sqlite3.connect("file:%s?mode=ro" % _meta_path().replace("\\", "/"), uri=True, timeout=5)


async def person_card(name: str) -> Result:
    """person:<名字>：她的画像还不在账号库里，在元库里（126 个人 / 137 条身份 / 观察记录）。"""
    who = (name or "").strip()
    if not who:
        return Result(False, error="person: 后面要给名字")
    try:
        con = _meta()
        ent = con.execute(
            "select person_id, display_name from person_entities "
            "where display_name = ? or display_name like ? order by length(display_name) limit 5",
            (who, "%" + who + "%")).fetchall()
        if not ent:
            con.close()
            return Result(True, text="[person:%s] 元库里没有这个人（可能只是微信联系人，还没被观察过）" % who,
                          data={"refs": []})
        pid, disp = ent[0][0], ent[0][1]
        ids = con.execute("select identity_type, identity_value, confidence from person_identities "
                          "where person_id = ? limit 8", (pid,)).fetchall()
        obs_n = 0
        try:
            obs_n = con.execute("select count(*) from person_observations where person_id = ?",
                                (pid,)).fetchone()[0]
        except Exception:  # noqa: BLE001
            warn("ports.person_card.Exception", err=repr(__import__("sys").exc_info()[1]))
        con.close()
        lines = ["[person:%s] 元库画像｜%s（person_id=%s）" % (who, disp, pid),
                 "- 观察记录：%d 条" % obs_n,
                 "- 身份线索：" + ("；".join("%s=%s" % (t, v) for t, v, _ in ids) if ids else "无")]
        if len(ent) > 1:
            lines.append("- 同名/近似：" + "、".join(e[1] for e in ent[1:4]))
        return Result(True, text="\n".join(lines), data={"person_id": pid})
    except Exception as exc:  # noqa: BLE001
        warn("ports.person_card.Exception", err=repr(exc))
        return Result(False, error=repr(exc))


# ---------------------------------------------------------------- 说话（真发）


def unanswered_streak(db: str, chat_id: str, *, limit: int = 30) -> int:
    """我连着说了几句、对方一句没接。

    从最近的消息往回数，数到对方（is_self=0）说话为止。
    返回 0 表示"最后说话的是对方"或"没说过话"；返回 N 表示我说了 N 句没被接。
    这是"一个人在这说"的机器判据——不是靠她自己想起来。
    """
    try:
        con = _connect(db)
        rows = con.execute(
            "select is_self from messages where chat_id=? order by created_at desc, id desc limit ?",
            (chat_id, limit),
        ).fetchall()
        con.close()
    except Exception:
        warn("ports.unanswered_streak.Exception", err=repr(__import__("sys").exc_info()[1]))
        return 0
    n = 0
    for r in rows:
        if int(r[0] or 0) == 1:
            n += 1
        else:
            break
    return n


def world_mtime() -> float:
    """世界最后一次「被动过」的时刻（看库文件与 -wal 的 mtime）。

    这个函数是节拍的成本闸：只有它变了，才值得去算整屏视界。
    比「每 0.4 秒重算一遍 30 个会话」便宜两个数量级。
    """
    newest = 0.0
    for db in db_paths():
        for suffix in ("", "-wal", "-shm"):
            try:
                newest = max(newest, os.path.getmtime(db + suffix))
            except FileNotFoundError:
                continue  # 库没有 -wal/-shm 是常态（这里原来每拍刷一条 warn）
            except OSError:
                warn("ports.world_mtime.Exception", err=repr(__import__("sys").exc_info()[1]))
                continue
    return newest


_STAMP = {"at": 0.0, "val": ()}
_STAMP_TTL_S = 0.3


def world_stamp() -> tuple:
    """世界「内容」的指纹：新消息、消息总数、联系人变更。

    2026-10-25 换掉的判据。原来用 db/-wal/-shm 的 mtime，可同步任务每
    config.sync.interval_minutes 分钟就会打开库写一下 WAL，mtime 被顶起来，
    于是一条新消息都没有也醒一拍（实测一天 217 拍里 185 拍是空转，正好 5 分钟一次）。
    现在只看内容：真有人说话才会变；2026-10-27 起把「已落库的媒体描述条数/总
    长度」也算进来——后台补上描述的那一拍也得叫醒她。
    """
    now = time.time()
    cached = _STAMP["val"]
    if cached and now - _STAMP["at"] < _STAMP_TTL_S:
        return cached
    try:
        db = str(pick_db())
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("?", "%3f"), uri=True, timeout=1.0)
        try:
            row = con.execute(
                "select coalesce(max(id),0), count(*), coalesce(max(created_at),'') from messages"
            ).fetchone()
            # 2026-10-25：只数联系人条数，**不看 updated_at** —— 同步任务每 5 分钟
            # 就会把 contacts 行重写一遍（updated_at 变），拿它当判据等于又回到 5 分钟一拍。
            crow = con.execute("select count(*) from contacts").fetchone()
            # 2026-10-27：图片/表情包的描述也是"世界内容"的一部分。以前它只在
            # message_media 里加一行，指纹不动，于是「后台刚看清的图」她永远等不到
            # 下一拍（要么等别人说话，要么根本看不见）。只数条数与总长度：
            # 同步任务重写同一行不会让它变。
            try:
                drow = con.execute(
                    "select coalesce(count(*),0), coalesce(sum(length(description)),0) "
                    "from message_media where description is not null "
                    "and trim(description) <> ''").fetchone()
                descs = (int(drow[0]), int(drow[1]))
            except Exception:  # noqa: BLE001 - 老库没有这张表，就当没有描述
                descs = (0, 0)
        finally:
            con.close()
        val = (int(row[0]), int(row[1]), int(crow[0])) + descs
    except Exception:  # noqa: BLE001 - 取不到就退回 mtime，别把节拍卡死
        warn("ports.world_stamp.Exception", err=repr(__import__("sys").exc_info()[1]))
        return ("mtime", int(world_mtime() * 1000))
    _STAMP["at"] = now
    _STAMP["val"] = val
    return val


def incoming_since(chat_id: str, ts: float) -> int:
    """这个人（这个会话）在我上次开口之后，又发了几条。事实，不参与任何拦截。"""
    try:
        import sqlite3
        from datetime import datetime as _dt
        cid = resolve_chat_id(chat_id) or chat_id
        # 2026-10-31 教训：调用方两次把毫秒喂进来（run.py 的 t0），fromtimestamp 抛
        # OSError(22)，这里静默返回 []，于是「有人说话」恒为假、她自己也不记得听见了。
        # 契约是 epoch 秒；看到明显是毫秒的值就地换算，别再让它悄悄变成"没人说话"。
        if not ts:
            since = "1970-01-01 00:00:00"
        else:
            _s = float(ts)
            if _s > 1e11:  # 毫秒（1e11 秒 ≈ 5138 年，正常 epoch 秒不可能到这）
                warn("ports.incoming_since.ms_argument", ts=ts, fixed=_s / 1000.0)
                _s = _s / 1000.0
            since = _dt.fromtimestamp(_s).strftime("%Y-%m-%d %H:%M:%S")
        con = sqlite3.connect(_account_db())
        try:
            row = con.execute(
                "select count(*) from messages where chat_id=? and is_self=0 and created_at > ?",
                (cid, since)).fetchone()
        finally:
            con.close()
        return int((row or [0])[0])
    except Exception:  # noqa: BLE001
        warn("ports.incoming_since.Exception", err=repr(__import__("sys").exc_info()[1]))
        return 0


def peer_events_since(ts: float, *, limit: int = 5) -> list[dict]:
    """自 ts 之后「别人说的」话（不含她自己）。

    2026-10-24 事故：世界指纹只看库 mtime，分不出「是她自己刚写库」还是「有人来找她」，
    于是她每发一句就把自己叫醒一次。这个函数把「到底有没有人说话」变成可查的事实，
    同时给她一个可验证的醒来理由。
    """
    try:
        import sqlite3
        from datetime import datetime as _dt
        # 2026-10-31 教训：调用方两次把毫秒喂进来（run.py 的 t0），fromtimestamp 抛
        # OSError(22)，这里静默返回 []，于是「有人说话」恒为假、她自己也不记得听见了。
        # 契约是 epoch 秒；看到明显是毫秒的值就地换算，别再让它悄悄变成"没人说话"。
        if not ts:
            since = "1970-01-01 00:00:00"
        else:
            _s = float(ts)
            if _s > 1e11:  # 毫秒（1e11 秒 ≈ 5138 年，正常 epoch 秒不可能到这）
                warn("ports.peer_events_since.ms_argument", ts=ts, fixed=_s / 1000.0)
                _s = _s / 1000.0
            since = _dt.fromtimestamp(_s).strftime("%Y-%m-%d %H:%M:%S")
        db = _account_db()
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            raw = con.execute(_SELECT + "where is_self=0 and created_at > ? "
                              "order by created_at desc limit ?", (since, int(limit))).fetchall()
        finally:
            con.close()
        out = []
        for r in _rows(raw, _descs(db)):
            if is_self_chat(r.chat_id):
                continue
            out.append({"chat": chat_label(db, r.chat_id), "who": r.speaker or "某人",
                        "at": r.at, "text": (r.text or "")[:120], "chat_id": r.chat_id})
        return out
    except Exception:  # noqa: BLE001
        warn("ports.peer_events_since.Exception", err=repr(__import__("sys").exc_info()[1]))
        return []



# ---------------------------------------------------------------- 显示名缓存
# 2026-10-25：账页/世界页每行都要把 target 翻成人名，原来每次都 glob + count(*) + 查 contacts，
# 实测 200 次 = 1.17 秒。翻名结果 60 秒内不变（改名要等下一轮），缓存掉。
_LABEL_CACHE: dict[str, tuple[float, str]] = {}
_LABEL_TTL_S = 60.0


_REF_NAME_RE = re.compile(r"[★·]?\s*\d*\s*(?:群|私)?\s*[「『]([^」』]+)[」』]")


def ref_name(target: str) -> str:
    """从"视界里抄回来的 ref"里抠出会话名。

    她 look 时会原样抄视界那一行，比如 `6 群「鲸鲸的测试群」`、`★3 私「Elaina」`、
    `202 群「示例作业群」`——前面的数字是"有几条在等我"，不是名字的一部分。
    以前没剥这层，于是账里/界面上出现 `6 群「鲸鲸的测试群」` 这种脏名字。
    """
    t = str(target or "").strip()
    if not t:
        return ""
    m = _REF_NAME_RE.search(t)
    if m:
        return m.group(1).strip()
    for pre in ("win:", "person:", "msg:"):
        if t.startswith(pre):
            t = t[len(pre):].strip()
            break
    return t


def label_for(target: str) -> str:
    """target（chat_id / 工具名 / "win:名字"）→ 给人看的名字；翻不出返回空串。带 60 秒缓存。"""
    target = ref_name(target)

    import time as _t

    key = str(target or "")
    now = _t.time()
    hit = _LABEL_CACHE.get(key)
    if hit is not None and now - hit[0] < _LABEL_TTL_S:
        return hit[1]
    try:
        out = _label_for_uncached(key)
    except Exception:  # noqa: BLE001 - 翻名失败不许抛给调用方
        out = ""
    if len(_LABEL_CACHE) > 500:
        _LABEL_CACHE.clear()
    _LABEL_CACHE[key] = (now, out)
    return out


# ── 世界还活着吗（2026-10-26 事故之后加的反向看护）────────────────────────────
# 事故：调度器 13:48 起就没再跑，同步停了五个小时，引擎照旧按旧视界和她自己的压力
# 开口（18:49 她还在说"憋了快一个钟头"，越过主人 18:34 的回答）。
# 她没法从视界里看出"世界停了"——旧消息和没消息长得一模一样。所以由这块说真话。
_SYNC_KICK = [0.0]         # 上次踢同步的时刻（一个引擎进程一份）
_SYNC_KICK_GAP_S = 180.0   # 两次踢之间至少隔 3 分钟，别把机器踢崩


def world_health() -> dict:
    """主程序（负责同步的那半个进程）还活着吗。

    口径只有一处：`core/infra/liveness.py`——心跳里的 `beat_ms` 由主程序自己声明（单位统一为毫秒，见 core/infra/duration.py）。
    这里以前写死 `_WORLD_STALE_S = 90.0`，注释还写着「它每 15 秒写一次」，
    而实际节拍早改成 10 秒了：写死的阈值一定会和现实脱钩（开发规矩 §2.11）。
    读不到 / 读坏 / 超期，一律算「世界停了」——宁可让她闭嘴，也不让她对着旧世界说话。
    """
    got = liveness.read("scheduler")
    if not got:
        return {"alive": False, "stale_ms": None, "note": "主程序没在跑（找不到心跳文件）"}
    st = liveness.state("scheduler")
    at = liveness.age_ms("scheduler")
    out = {"alive": st == "alive", "state": st, "stale_ms": at,
           "since_ms": got.get("started_at_ms") or 0, "job": got.get("job") or "",
           "pid": got.get("pid"), "note": ""}
    if st in ("stale", "gone"):
        out["note"] = liveness.why("scheduler")
    elif st == "unknown":
        # 不可判：不许对外宣称“世界停了”（那会让她永久闭嘴），也不许说“没事”。
        out["note"] = "心跳不符合契约（%s）——不改判，等生产者重启" % "、".join(
            liveness.contract("scheduler"))
    return out


def kick_sync(*, force: bool = False) -> bool:
    """踢一脚同步：后台子进程跑 `run.py sync`，把世界补到最新。

    限流是必须的：世界停了的时候每一拍都会走到这里，不拦的话会开出一串子进程。
    永远返回，不抛（这是 ports 的规矩）。
    """
    now = time.time()
    if not force and (now - _SYNC_KICK[0]) < _SYNC_KICK_GAP_S:
        return False
    _SYNC_KICK[0] = now
    try:
        subprocess.Popen([sys.executable or "python", os.path.join(ROOT, "run.py"), "sync"],
                         cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 | 0x00000200)
        warn("world_sync_kicked")
        return True
    except Exception as exc:  # noqa: BLE001
        warn("world_sync_kick_failed", error=str(exc)[:160])
        return False

def backfill_receipts(days: int = 1) -> dict:
    """只读报告旧未确认账目；没有操作绑定不能补造确认。"""
    import datetime as _dt
    import json as _json
    from pathlib import Path as _P
    from core.infra import actions as _actions

    done, checked, missing = [], 0, []
    for back in range(max(1, int(days))):
        day = (_dt.date.today() - _dt.timedelta(days=back)).isoformat()
        path = _P(_actions.path(day))
        if not path.exists():
            continue
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(_json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        for r in rows:
            if r.get("act") != "say" or (r.get("ack") or "") != "unconfirmed":
                continue
            text = str(r.get("text") or "")
            checked += 1
            missing.append({"turn_id": r.get("turn_id"), "at": r.get("at"),
                            "text": text[:24], "reason": "operation_binding_missing"})
    return {"ok": True, "checked": checked, "confirmed": len(done), "still_missing": len(missing),
            "done": done[:20], "missing": missing[:20]}


def _confirm_image_later(chat_id: str, label: str) -> None:
    """缺少媒体操作绑定，不按跨账号近期图片猜测确认。"""
    warn("ports_image_confirmation_binding_missing", chat_id=chat_id)
