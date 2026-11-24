"""渠道约定：要不要 @、能不能发语音、隔多久再发、要不要分段。

这些是「渠道的物理规则」，不是她的性格：一条十几个字、群里不发语音、
另一个平台能说得长一点。她的性格在 data/persona/cards/*.md，别混进这里。

2026-10-30（施工 Step 3）：这些数字原来写死在 loop/brain.py 的提示词字符串里，
改一句措辞要动代码；现在一个平台一行，render() 是唯一出口。

2026-10-31（施工 Step 8 · D1 面板）：上面那张表是**出厂默认**；面板改的值落在
data/prompt/channel.json（扁平点号键，例如 "wechat_group.delay_ms"），
rules() 读的时候盖在默认上——出厂表不再被改动，随时能回到默认（删掉那个文件即可）。
注意：条数与字数上限已按主人要求取消，这里不再有 max_msgs / chars_per_msg。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace

from core.contracts import limits

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OVERRIDE_PATH = os.path.join(ROOT, "data", "prompt", "channel.json")

# 面板能改的字段（D1 渠道约定的白名单，与 console/executor.py 的 SCHEMA 一一对应）
EDITABLE = (
    "blank_line",
    "allow_voice",
    "allow_markdown",
    "mention_required",
    "delay_ms",
    "owner_always_reply",
    "ask_hint",
)


@dataclass(frozen=True)
class ChannelRules:
    platform: str = ""
    blank_line: bool = False
    allow_voice: bool = True
    allow_markdown: bool = False
    mention_required: bool = False
    delay_ms: int = 0
    # 2026-11-17 表达改造：不再用「7% 带问号」这种语料统计当表达目标。
    ask_hint: str = "需要了解的信息确实会改变回答时再问；不必在每次回复末尾追加问题。"
    owner_always_reply: bool = True
    file_dirs: tuple = ("data/artifacts", "data/persona", "data/exports", "docs", "output")
    file_max_mb: int = 100000   # 2026-10-31 取消上限（原来 25MB）


DEFAULT = ChannelRules()

_PLATFORMS = {
    # 2026-10-31：微信只有一个平台。"私聊/群"是会话形态，不是平台——
    # 群里的额外规矩见 _GROUP_RULES，由 rules(platform, chat_id) 按 chat_id 叠加。
    "wechat": replace(DEFAULT, platform="wechat"),
    "qq": replace(DEFAULT, platform="qq", blank_line=True, delay_ms=300),
}

# 会话形态 → 额外规矩。面板只配"平台级的底"，这几条是群的固有属性（不单独开开关）：
# 群里必须 @ 到人、不发语音、发完慢一点，否则一连串短句会刷屏。
_GROUP_RULES = {
    "wechat": {"mention_required": True, "allow_voice": False, "delay_ms": 1500},
    "qq": {"mention_required": True, "allow_voice": False, "delay_ms": 800},
}
_GROUP_SUFFIX = "@chatroom"          # 微信群
_GROUP_PREFIX = "group:"             # QQ 群（历史 chat_id 形态）


def is_group(chat_id: str) -> bool:
    """这个会话是不是群。只认 chat_id：微信群 …@chatroom，QQ 群 group:…。"""
    cid = str(chat_id or "").strip().lower()
    return cid.endswith(_GROUP_SUFFIX) or cid.startswith(_GROUP_PREFIX)


_cache: dict = {"at": None, "data": {}}


def _overrides() -> dict:
    """读面板改过的值（data/prompt/channel.json）。按 mtime 缓存，文件不在就是空。"""
    try:
        at = os.path.getmtime(OVERRIDE_PATH)
    except OSError:
        _cache["at"], _cache["data"] = None, {}
        return {}
    if _cache["at"] == at:
        return _cache["data"]
    data = {}
    try:
        with open(OVERRIDE_PATH, encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            data = got
    except Exception:  # noqa: BLE001 —— 文件写一半/不是 JSON 都当没有，绝不因此让她说不出话
        data = {}
    _cache["at"], _cache["data"] = at, data
    return data


def _coerce(key: str, value):
    if key == "delay_ms":
        try:
            return max(0, int(value))
        except Exception:  # noqa: BLE001
            return 0
    if key == "ask_hint":
        return str(value or "")
    return bool(value)


def overrides_for(platform: str) -> dict:
    """这个平台被面板改过的字段（没改就是空 dict）。"""
    p = (platform or "").strip().lower()
    ov = _overrides()
    return {k: _coerce(k, ov["%s.%s" % (p, k)])
            for k in EDITABLE if "%s.%s" % (p, k) in ov}


def rules(platform: str, chat_id: str = "") -> ChannelRules:
    """按平台 + 会话形态给一套约定。

    顺序：出厂默认 → 面板改过的值 → 群里的固有规矩（面板显式改过的字段优先，
    因为那是主人亲手设的）。不认识的平台用 DEFAULT。
    """
    p = (platform or "").strip().lower()
    base = _PLATFORMS.get(p, DEFAULT)
    patch = overrides_for(p)
    r = replace(base, **patch) if patch else base
    if is_group(chat_id):
        # 平台判不出来（cells 没写 platform）时也要拿到群里那两条硬规矩：
        # 必须先 @ 对方、不发语音。delay 只在知道平台时才加，免得给 QQ 群套微信的 1.5 秒。
        _gr = _GROUP_RULES.get(p)
        if _gr is None:
            _gr = {"mention_required": True, "allow_voice": False}
        extra = {k: v for k, v in _gr.items() if k not in patch}
        if extra:
            r = replace(r, **extra)
    return r


def group_extra(platform: str) -> dict:
    """面板显示用：这个平台在群里会额外变成什么（不含面板改过的字段）。"""
    p = (platform or "").strip().lower()
    patch = overrides_for(p)
    return {k: v for k, v in (_GROUP_RULES.get(p) or {}).items() if k not in patch}


def factory(platform: str) -> dict:
    """出厂默认值（面板用来显示"恢复到默认"该是多少）。"""
    r = _PLATFORMS.get((platform or "").strip().lower(), DEFAULT)
    return {k: getattr(r, k) for k in EDITABLE}


def effective(platform: str) -> dict:
    """面板显示用：这个平台此刻真正生效的可改字段。"""
    r = rules(platform)
    return {k: getattr(r, k) for k in EDITABLE}


def render(r: ChannelRules | None = None) -> str:
    """把一套约定渲染成提示词里的一段话。改平台只改上面的表，不改这里。"""
    r = r or DEFAULT
    bits = [
        "[怎么开口] 想说话就用 say，target 写会话名（拿不准就先 look 看一眼）。",
        # 2026-10-31：主人要求取消上限——不再规定条数与字数（原「一次最多 %d 条，每条 %d~%d 个字」）
        "打字的样子：想发几条就几条，想写多长就多长（对方说话短就短一点，正经聊就写开）；%s"
        % ("可以空行分段" if r.blank_line else "一条说完再发下一条",)
    ]
    if r.mention_required:
        bits.append("群里的公开场合要先 @ 对方再说话。")
    if not r.allow_voice:
        bits.append("不发语音，只发字。")
    if r.delay_ms > 0:
        bits.append("发完一条停 %.1f 秒左右再发下一条。" % (r.delay_ms / 1000.0))
    if not r.allow_markdown:
        bits.append("不用 markdown、不用列表。")
    if r.owner_always_reply:
        bits.append("对着主人的时候要接话：他一句话我基本都回。")
    bits.append(r.ask_hint)
    # 2026-11-03 P3-2：做不到的事一次说绝。平台无关（不改上面任何字段表），
    # 文案的单一来源在 core/contracts/limits.py；见方案文档 §14 / E-29。
    bits.append(limits.refusal_text())
    # 2026-11-03 P3-3：收到表情包/图片就沉默是第二类高频错（digest 4+12 次）。
    # 仍放这里：每轮必在、平台无关、自带可见标签（标题不渲染，见 E-16）。
    bits.append(limits.no_blank_text())
    return "\n".join(bits)
