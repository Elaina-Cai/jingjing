"""把"她常犯的错"从反馈样本里聚出来（P3-1，只读）。

设计三条：
1. **只做计数与归类，不做评判** —— 归类规则全写在 `CODE_*` 与 `describe_code()`
   里，可被单测逐条钉住；绝不让模型自己总结自己（那样只会总结出她想成为的样子）。
2. **原始样本永远保留** —— 聚合只是索引，`ids` 指回 `feedback_samples.id`，
   任何结论都能一路查到原句。
3. **零写入** —— 本模块不碰数据库、不 import 任何 IO 模块，只吃 row/dict 列表；
   落不落盘、写不写规则，由 scripts 层与主人决定。

来源：`feedback_samples` 表。`source='repeat'`＝主人几乎又问了一遍（上次答败了）；
`source='correction'`＝主人明确说不对。这两个信号仓库早就在收（core/quality/feedback.py），
但一直没有"汇总成常犯的错"这一步（P3 之前 repeat 39 条一条都没被用过）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "CODE_EMPTY_REPLY",
    "CODE_REFUSED_NO_PATH",
    "CODE_REFUSED_WITH_PATH",
    "CODE_ANSWERED_STILL_REPEATED",
    "CODES",
    "Cluster",
    "build_digest",
    "classify_cluster",
    "describe_code",
    "sample_shape",
]

#: 她压根没回（reply_preview 空）。这是最贵的一种：主人连"她做不到"都没听到。
CODE_EMPTY_REPLY = "empty_reply"
#: 她回了「发不了/没有这个口」，但**没给替代路径** —— 主人只能自己再问一遍。
CODE_REFUSED_NO_PATH = "refused_no_path"
#: 她拒绝了，但给了替代（"你自己去发"/"得你用手机敲"）—— 这类重复是没听懂，不是没路。
CODE_REFUSED_WITH_PATH = "refused_with_path"
#: 她答了，主人还是又问一遍 —— 要么答偏了，要么答了但没落地。
CODE_ANSWERED_STILL_REPEATED = "answered_but_repeated"

#: 归类顺序即优先级：越靠前越该先修。
CODES = (
    CODE_EMPTY_REPLY,
    CODE_REFUSED_NO_PATH,
    CODE_REFUSED_WITH_PATH,
    CODE_ANSWERED_STILL_REPEATED,
)

_LABELS = {
    CODE_EMPTY_REPLY: "没回（连「我做不到」都没说）",
    CODE_REFUSED_NO_PATH: "拒绝了但没给路",
    CODE_REFUSED_WITH_PATH: "拒绝了但给了替代",
    CODE_ANSWERED_STILL_REPEATED: "答了，主人还是又问",
}

#: 「我做不到」的说法。只收生产里真出现过的，别凭想象加。
_REFUSE_MARKERS = (
    "发不了", "发不出去", "没有这个口", "没这个口", "做不到", "没法", "没有权限", "没权限", "干不了",
)
#: "替代路径"的说法。
_PATH_MARKERS = ("你自己", "你可以", "得你", "你自己去", "你来", "手动", "用手机", "敲一遍")

_CHOP = " \t\r\n，。！？!?；;、,:：·,.…~～-—_\"'“”‘’（）()[]【】｜|"


def _norm(text: str) -> str:
    """去掉空白与标点，只留可比较的骨架。"""
    src = str(text or "")
    out = []
    for ch in src:
        if ch in _CHOP or ch.isspace():
            continue
        out.append(ch)
    return "".join(out).lower()


def sample_shape(text: str, *, keep: int = 24) -> str:
    """把一句话压成一个可聚类的键：归一化后取前 keep 个字。

    取前缀而不是全串，是因为主人重问时常常多带一句"再试一次"；
    但也不能只取关键词 —— 那要靠词表，等于把判断藏在词表里。
    """
    return _norm(text)[: max(4, int(keep))]


def _flags(reply: str) -> tuple[bool, bool]:
    body = _norm(reply)
    empty = not body
    refused = any(_norm(m) in body for m in _REFUSE_MARKERS)
    has_path = any(_norm(m) in body for m in _PATH_MARKERS)
    return empty, (refused and not has_path), has_path


@dataclass
class Cluster:
    """同一个"形状"的一簇样本。"""

    shape: str
    source: str = ""
    count: int = 0
    ids: list[int] = field(default_factory=list)
    chats: list[str] = field(default_factory=list)
    first_at: str = ""
    last_at: str = ""
    code: str = CODE_EMPTY_REPLY
    example_text: str = ""
    example_reply: str = ""

    def as_dict(self) -> dict:
        return {
            "shape": self.shape,
            "source": self.source,
            "count": self.count,
            "code": self.code,
            "label": describe_code(self.code),
            "ids": list(self.ids),
            "chats": list(self.chats),
            "first_at": self.first_at,
            "last_at": self.last_at,
            "example_text": self.example_text,
            "example_reply": self.example_reply,
        }


def describe_code(code: str) -> str:
    return _LABELS.get(str(code or ""), "未归类")


def classify_cluster(cluster: Cluster) -> str:
    """给一簇样本贴一个码。顺序就是优先级（见 CODES）。"""
    empty, refused_no_path, refused_with_path = _flags(cluster.example_reply)
    if empty:
        return CODE_EMPTY_REPLY
    if refused_no_path:
        return CODE_REFUSED_NO_PATH
    if refused_with_path:
        return CODE_REFUSED_WITH_PATH
    return CODE_ANSWERED_STILL_REPEATED


def _field(row, name, default=""):
    if isinstance(row, dict):
        return row.get(name, default)
    try:
        return row[name]
    except Exception:
        return getattr(row, name, default)


def build_digest(samples, *, min_count: int = 2) -> dict:
    """把样本聚成"常犯的错"。纯函数：不给数据库、不给文件、不写任何东西。

    `samples` 是 dict/Row 列表，需要 `id / source / chat_id / sample_text /
    reply_preview / created_at` 六个字段（其余字段忽略）。
    """
    buckets: dict[tuple[str, str], Cluster] = {}
    by_source: dict[str, int] = {}
    for row in samples or ():
        source = str(_field(row, "source") or "")
        by_source[source] = by_source.get(source, 0) + 1
        text = str(_field(row, "sample_text") or "")
        shape = sample_shape(text)
        if not shape:
            continue
        key = (source, shape)
        cur = buckets.get(key)
        if cur is None:
            cur = Cluster(
                shape=shape,
                source=source,
                example_text=text,
                example_reply=str(_field(row, "reply_preview") or ""),
                first_at=str(_field(row, "created_at") or ""),
                last_at=str(_field(row, "created_at") or ""),
            )
            buckets[key] = cur
        cur.count += 1
        rid = _field(row, "id", None)
        if rid is not None:
            cur.ids.append(int(rid))
        chat = str(_field(row, "chat_id") or "")
        if chat and chat not in cur.chats:
            cur.chats.append(chat)
        at = str(_field(row, "created_at") or "")
        if at:
            if not cur.first_at or at < cur.first_at:
                cur.first_at = at
            if not cur.last_at or at > cur.last_at:
                cur.last_at = at

    clusters = [c for c in buckets.values() if c.count >= max(1, int(min_count))]
    for c in clusters:
        c.code = classify_cluster(c)
    clusters.sort(key=lambda c: (-c.count, CODES.index(c.code) if c.code in CODES else 99, c.shape))

    by_code: dict[str, int] = {}
    for c in clusters:
        by_code[c.code] = by_code.get(c.code, 0) + c.count

    return {
        "total": sum(by_source.values()),
        "by_source": by_source,
        "by_code": by_code,
        "clusters": [c.as_dict() for c in clusters],
        "top": [c.as_dict() for c in clusters[:3]],
        "notes": [
            "只统计出现 >= %d 次的形状；一次性的口误不算" % int(min_count),
            "计数是样本条数，不是人次数；同一个诉求重问三次算三条",
            "本表不改任何行为——要改，得另外有写入路径并留回滚",
        ],
    }
