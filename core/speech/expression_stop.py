"""表达层的机械减法：套话、残留尾、行首时间戳。

**只做减法**：命中就删那一段，不判断内容、不改写、不新增、不调模型。这与输出闸门
（``core.speech.guards``）是两件事——闸门只观测、一个字都不动；这里反而**必须**动
文本，因为它的目的是把「模型自己追加的那半句」拿掉。

三个来源都是实测事故，不是口味偏好：

* **套话**（2026-09-26）：群聊真人接情绪 24 条里问句 0、安抚词 0、建议词 1——
  「倾听姿态/劝诫」在真人语料里几乎不出现，却是我方高频输出。
* **残留尾**（2026-09-27）：探针 72 轮实测出「示范句 + 追加尾巴」机制（模型照正面
  示范说第一句，再自行追加一句反问/旧账，t_poke 5/6）。prompt 收到第 4 版仍压不住。
  2026-10-10 主人裁定：**只删「赌气罢言」**（人设明令不冷战），其余 10 类改为只检测
  ——那些是她的记账与抬杠，是人味来源，不是 AI 腔。
* **行首时间戳**（2026-09-28 S2 对照）：轮次内容带 ``[HH:MM]`` 后草稿 5/6 条模仿出
  前缀（旧载体 0/6），出口只删行首那一个。

历史 import 路径（``core.speech.guards``）仍可用：guards 里保留同名再导出。
"""

from __future__ import annotations

import re

__all__ = [
    "detect_canned_phrases",
    "detect_residual_tails",
    "strip_canned_phrases",
    "strip_leading_timestamp",
    "strip_residual_tails",
    "watch_canned_phrases",
]

# ---------------------------------------------------------------------------
# AI 腔套话止损（批C 表达层，2026-09-26）
# 实测依据：群聊真人接情绪 24 条里，问句 0、安抚词 0、建议词 1——「倾听姿态/劝诫」
# 类句子在真人语料里几乎不出现，却是我方高频输出。这里只做机械减法：命中就删掉
# 命中子句；不判断内容、不改写、不新增、不再调模型。硬名单删；观察名单只记日志。
_CANNED_STRIP: tuple[str, ...] = (
    "跟我说说", "我听着呢",
    "心疼你", "摸摸头", "抱抱你", "我会一直陪着你", "我会陪着你的",
)
_CANNED_WATCH: tuple[str, ...] = (
    "先歇会儿", "早点休息", "辛苦了", "加油哦", "别想太多", "别难过",
    "我在这儿守着",
    # 2026-09-27 去凶：关心句不再硬删（硬删会让句子变冷），转观察名单只记。
    "别硬撑", "注意身体", "照顾好自己",
    # 2026-09-27i 观察：退场带气字眼只记不删。
    "我闭嘴",
    # 2026-09-27 观察：「我闭嘴」的变体（不插嘴了），同样只记不删。
    "不插嘴了",
    # 批2（2026-09-26）：批1 探针里出现的「倾听腔」变体，先只记日志。
    "说给我听听", "说来我听听",
)
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[，。！？；;\n])")

# ---------------------------------------------------------------------------
# 语气残留止损（2026-09-27 收口）：探针 72 轮实测出「示范句 + 追加尾巴」机制——
# 模型先照正面示范说第一句，再自行追加一句反问/旧账（t_poke 5/6、t_how_you 4/6）。
# prompt 收敛到第 4 收仍压不住，这里只做机械兜底：删命中子句，不改写、不新增。
_RESIDUAL_TAIL_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("反查对方", re.compile(r"查岗")),
    ("反将一军", re.compile(r"倒是你")),
    ("旧账菜名", re.compile(
        r"(?:被使唤|被点名|刚才|刚刚|之前)[^。！？；;\n]{0,10}报菜名"
        r"|报菜名[^。！？；;\n]{0,6}报出"
        r"|菜名[^。！？；;\n]{0,12}(?:没漏|不报)"
    )),
    ("算旧账", re.compile(r"(?:刚才|刚刚|之前)[^。！？；;\n]{0,12}你先")),
    ("旧账没漏", re.compile(r"没漏")),
    ("旧账报完", re.compile(r"报完了")),
    ("算账扯平", re.compile(r"扯平")),
    ("回敬话头", re.compile(r"别接我话头")),
    ("反套话", re.compile(r"套我话|套我的话")),
    ("追问归因", re.compile(r"谁惹你了|谁惹的|谁气的")),
    ("赌气罢言", re.compile(r"不说了")),
)

# 命中这些标签时，紧随其后的子句通常属同一句尾巴，一并删（避免留半句）。
_RESIDUAL_FOLLOW_LABELS = frozenset({"反将一军", "赌气罢言"})


# 2026-10-10（按主人裁定）：**只删「赌气罢言」**——人设明令「不冷战、不赌气、不撂挑子」。
# 其余 10 类（反将一军 / 算旧账 / 旧账菜名 / 追问归因 / 反查对方 / 反套话 …）改为**只检测、不删**。
# 依据：表达层基线（scripts/expr_signal_report.py，532 条真实回复）里命中 9 条，人读全部合格，例：
#   「摸鱼？我这叫待机省电，你懂什么。倒是你，大中午的盯着我打卡，自己不也没干正事。」
#   「行，那我闭嘴。反正菜名我一个没漏。」
# 这些是她的**人味来源**（记账、抬杠、反击），不是 AI 腔；旧写法把它们整句删了。
# 检测口保留在 detect_residual_tails()，报告照常统计命中分布。
_RESIDUAL_STRIP_LABELS = frozenset({"赌气罢言"})

# 2026-09-28 S2 对照实验：轮次内容带 [HH:MM]，草稿 5/6 条模仿出前缀（旧载体 0/6）。
# 出口只做减法：删行首那一个时间戳，不动其余内容。
_LEADING_TIMESTAMP_RE = re.compile(r"^\s*\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*")


def strip_leading_timestamp(text: str) -> tuple[str, bool]:
    """去掉行首 [HH:MM]/[HH:MM:SS] 前缀；返回 (文本, 是否命中)。

    只删行首一个前缀；删完为空则返回空串（交给空稿处理）。
    """
    raw = str(text or "")
    if not _LEADING_TIMESTAMP_RE.match(raw):
        return raw, False
    cleaned = _LEADING_TIMESTAMP_RE.sub("", raw, count=1).lstrip()
    return cleaned, True


def detect_residual_tails(text: str) -> list[str]:
    """语气残留子句命中（纯匹配，供日志与验收统计）。"""
    value = str(text or "")
    if not value:
        return []
    hits: list[str] = []
    for clause in _CLAUSE_SPLIT_RE.split(value):
        for label, rx in _RESIDUAL_TAIL_RES:
            if rx.search(clause):
                hits.append(label)
                break
    return hits


def strip_residual_tails(text: str) -> tuple[str, list[str]]:
    """删掉命中「必删」标签的子句（当前只有「赌气罢言」）；其余标签只算不删。

    机械减法：只删命中子句，不改写、不新增；删空/删到没内容时原样返回。
    返回的 hits 只含**真的被删掉**的标签；要看全部命中（含只观察的），用
    detect_residual_tails()——表达层基线报告走的是那一条路。
    """
    value = str(text or "").strip()
    if not value:
        return value, []
    clauses = _CLAUSE_SPLIT_RE.split(value)
    drop: set[int] = set()
    hit_label: dict[int, str] = {}
    hits: list[str] = []
    for i, clause in enumerate(clauses):
        for label, rx in _RESIDUAL_TAIL_RES:
            if rx.search(clause):
                if label in _RESIDUAL_STRIP_LABELS:
                    drop.add(i)
                    hit_label[i] = label
                    hits.append(label)
                break
    if not drop:
        return value, []
    # 反将/赌气尾巴：后面紧跟的子句通常属同一句，一并删（避免留半句）。
    for i in sorted(drop):
        if hit_label.get(i) not in _RESIDUAL_FOLLOW_LABELS:
            continue
        if i + 1 < len(clauses) and (i + 1) not in drop:
            drop.add(i + 1)
    kept = [c for i, c in enumerate(clauses) if i not in drop]
    result = "".join(kept).strip().strip("，,、；; \n")
    if len(result) < 2 or not any(ch.isalnum() for ch in result):
        return value, []
    return result, hits


def detect_canned_phrases(text: str) -> list[str]:
    """硬名单命中（纯字符串匹配，供日志与验收统计）。"""
    value = str(text or "")
    return [p for p in _CANNED_STRIP if p in value]


def watch_canned_phrases(text: str) -> list[str]:
    """观察名单命中：只记录，不删。"""
    value = str(text or "")
    return [p for p in _CANNED_WATCH if p in value]


def strip_canned_phrases(text: str) -> tuple[str, list[str]]:
    """删掉命中硬名单的子句；返回 (text, hits)。

    机械减法：只删命中子句，不改写、不新增；删空/删到没内容时原样返回。
    """
    value = str(text or "")
    hits = detect_canned_phrases(value)
    if not value or not hits:
        return value, []
    kept = [
        clause for clause in _CLAUSE_SPLIT_RE.split(value)
        if not any(p in clause for p in hits)
    ]
    result = "".join(kept).strip().strip("，,、；; \n")
    if len(result) < 2 or not any(ch.isalnum() for ch in result):
        return value, []
    return result, hits
