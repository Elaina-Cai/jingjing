"""消息事实层：**claim 台账**（事实权威序 + 更正历史 + 复述抑制）。

M2-4（`docs/archive/任务清单_2026-10-06.md:71`）的落地件。落点选在这里，理由就是
``docs/decisions/回答架构评审.md:156`` 那一格：**「0 结构」层的 `claims`（谁自报/断言了什么、
来源是自报还是转述）本来就标着「新建 `core/context/message_facts.py`；读侧消费」**，
而这一层另外三项（speaker / addressees）的 canonical owner 是
``core.conversation.addressing.py``——本模块**只消费**它给出的说话人，**自带第二套
归属/说话人解析**（那是 ``tests/test_message_attribution.py`` 静态守卫⑦明令禁止的）。

权威序边界（与既有三份存储的分工，**不重叠**）
--------------------------------------------

本模块是**权威序（authority order）的 owner**，不是第四份事实仓库：

* ``person_notes``（迁移 48）——主人手写的**半长期备注**（群基线/她的立场），
  溯源列是 ``source_proposal_id`` / ``evidence_json``；它回答"这个群该怎么对待"。
* ``person_observations``——**从消息里抽取的观察**（写侧在 scheduler），回答
  "这个人做过什么"。
* ``memory_projections``（``source_event_ids``）——**事件投影**，回答"发生过什么事"。
* ``claim_ledger``（本模块，迁移 56 + 59）——**谁在何时自报 / 转述 / 推测了哪个字段的
  哪个值 + 权威级 + 更正链 + 原话与 span**，回答的是"**这条事实该信谁**"。

一句话：那三份各管各的"内容"，本表只管"内容之间的**效力次序**"。本表另存**它自己的证据**：``source_text``（有上限的原话副本）+
``evidence_start`` / ``evidence_end``（命中片段在原文里的半开位置）——真相仍在消息表，
用 ``source_message_id`` + span 回到现场。

三级权威（**自报 > 转述 > 推测**）
----------------------------------

* ``self_report``（自报）：说话人**就是**被说的那个人（``我说我是湖南长沙的``）；
* ``relay``（转述）：说话人引用**第三方**（``他跟我说他是湖南长沙的``）；
* ``speculation``（推测）：无来源（``他好像是湖南人吧`` / ``我猜``）。

同一 ``(platform, account_id, chat_id, person_key, field_key)`` 上**低权威不覆盖高权威**
（2026-10-16「写时不承诺」收口）：**低权威写入不再被拒** —— 转述/推测也是"有人这么
说过"，一律留档（``RecordOutcome.PARALLEL``，并列在案并记进 ``claim_ledger_audit``）。
**谁更可信改成读时透镜**：:meth:`ClaimLedger.resolve` 的 ``order`` 在同一批行上换排序
（``authority`` / ``recency``）。只有"字段/值为空"这种无法登记的行才 ``REJECTED``。
**降级不是拒绝**：同一权威级的重复写入是 ``DUPLICATE``，自己更新自己是
``SUPERSEDED``（人本来就会改口）；``off`` 开关下仍是旧口径（低权威被拒，回滚位）。

更正历史（**不得静默改写**）
-----------------------------

任何一次更新都不 UPDATE 旧行的 ``value``：旧行只被写 ``superseded_by`` /
``supersede_reason`` / ``superseded_at`` 三列，**正文一字不动**。撤回（
``21:44:57`` 转述者P6「我瞎说的」）走 :meth:`ClaimLedger.withdraw`：说话人那条
claim 置 ``withdrawn_at``，**并且把以它为源的下游 claim 一并作废**
（``supersede_reason='source_withdrawn'``）——这正是"她 21:44:20 的登记必须随之
作废"那一格。

复述 / 近重复读数（2026-10-24 退役）
------------------------------------

出站侧「近重复 → 抑制」与「复述计数」随 ``repeat_guard`` / ``assertion_guard`` /
``guards`` 的复读指纹一起下线：那三项都在替模型判"这句话和她刚说的像不像"。
本模块只保留 ``content_key``（字面归一化）做**台账内部的同一性归并**，不再提供
"近 N 条里第几次"的读数，也不算任何相似度。

开关 ``YINYUE_FEATURE_CLAIM_LEDGER``（三态，**默认 off**）
----------------------------------------------------------

* ``off``  ← **默认**：本模块**一行都不走**——不建表相关的读写、不查库、不参与闸门，
  与接线前**逐字节一致**（回滚位）。表本身由 ``create_all`` / 迁移 56 建，
  **与开关无关**（同 ``person_notes``：schema 是 schema，行为是行为）；
* ``shadow`` → 判定 + 留痕（写台账、算权威序），但闸门只记不动；
* ``on``   → 台账结论生效：无支撑的断言降级为"没把握"（或按配置拦下）。

**唯一读点**是 :func:`claim_ledger_mode`，其余地方一律读 :func:`ledger_enabled` /
:func:`ledger_tracing` / :func:`ledger_enforcing` 三个布尔。

写侧（**谁在何时自报/转述/推测了什么 → 落库**）
-----------------------------------------------

写侧**唯一入口**是 :func:`record_message_claims`，生产调用点在**摄取链**
（``scheduler/main.py`` 的 ``NEW_MESSAGES_SYNCED`` → ``scheduler/person_memory.py``
``record_claims_for_messages``，与既有 ``person_observations`` **同一批消息、同一处
identity 解析**，所以不需要第二套事实存储，也不需要第二套归属解析）。

三条口径都由本模块的判据给，调用方传不进来：

* **归属** :func:`classify_subject` —— 自报（说话人本人）/ 转述（点名的第三方，
  必须在 canonical 身份解析出的 ``known_person_keys`` 里）/ 解析不出人 → **不登记**；
* **权威级** :func:`_authority_of` —— 自报 > 转述 > 推测，从文本语态 + 归属算出来；
* **幂等** —— 同一个 ``(chat, source_message_id)`` 第二次走到写口，一行都不写
  （:func:`_recorded_message`，幂等键就是台账自己的溯源列）。

``withdraw`` 走的是**同一条**写路径：说话人说"我瞎说的"/"记错了" → 撤回自己那条，
并把以它为源的下游一并作废；旧行留着、正文一字不动（更正历史，不是删除）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable, Sequence

from core import features
from core.contracts.events import EvidenceRef  # P0-2：断言=原话+span
from safelog import warn

try:  # structlog 与仓库其它模块同口径；导入失败也不让本模块炸（读侧要能降级）
    import structlog

    log = structlog.get_logger()
except Exception:  # noqa: BLE001 - 见上
    warn("core/context/message_facts.py:108 Exception", err=repr(__import__("sys").exc_info()[1]))
    import logging

    log = logging.getLogger(__name__)

__all__ = [
    "AUTHORITY_LEVELS",
    "AUTHORITY_RANK",
    "CLAIM_LEDGER_TABLE",
    "SUBJECT_NAMED",
    "SUBJECT_SPEAKER",
    "SUBJECT_UNBOUND",
    "Authority",
    "ClaimCandidate",
    "ClaimLedger",
    "ClaimMessage",
    "ClaimRecord",
    "ClaimResolution",
    "MessageClaimResult",
    "RecordOutcome",
    "RetractionCandidate",
    "claim_ledger_mode",
    "classify_subject",
    "extract_claims",
    "extract_retractions",
    "ledger_enabled",
    "ledger_enforcing",
    "ledger_tracing",
    "message_claim_candidates",
    "record_message_claims",
]

CLAIM_LEDGER_TABLE = "claim_ledger"


# ---------------------------------------------------------------------------
# 开关（三态，默认 off；唯一读点在这里）
# ---------------------------------------------------------------------------
def claim_ledger_mode() -> str:
    """三态读法（**唯一读点**；别用 ``features.enabled``——它会把 shadow 读成 False）。

    默认 ``off``：未设置 = 关。本模块在测试进程里被 import 时也会显式钉死
    （``tests/test_claim_ledger.py`` 的 import 期钉表），因为"env 未设"≠"关"。
    """
    return features.mode(features.CLAIM_LEDGER, features.MODE_OFF)


def ledger_tracing() -> bool:
    """``shadow`` / ``on``：要不要**判定 + 留痕 + 写台账**。"""
    return claim_ledger_mode() != features.MODE_OFF


def ledger_enforcing() -> bool:
    """``on``：台账结论要不要**改这一轮对外说的话**（降级/拦）。"""
    return claim_ledger_mode() == features.MODE_ON


def ledger_enabled() -> bool:
    """历史别名（= :func:`ledger_tracing`，与其它模块的 ``*_enabled`` 口径一致）。"""
    return ledger_tracing()


# ---------------------------------------------------------------------------
# 三级权威（唯一枚举；秩只在 AUTHORITY_RANK 里给一次）
# ---------------------------------------------------------------------------
class Authority(StrEnum):
    """claim 的权威级：**自报 > 转述 > 推测**（规格原文的三级，不许再加第四级）。"""

    SELF_REPORT = "self_report"   # 自报：说话人就是被说的那个人
    RELAY = "relay"               # 转述：说话人引用第三方（"他跟我说…"）
    SPECULATION = "speculation"   # 推测：无来源（"好像是…" / "我猜…"）


AUTHORITY_RANK: dict[str, int] = {
    Authority.SELF_REPORT.value: 3,
    Authority.RELAY.value: 2,
    Authority.SPECULATION.value: 1,
}
AUTHORITY_LEVELS: tuple[str, ...] = (
    Authority.SELF_REPORT.value, Authority.RELAY.value, Authority.SPECULATION.value,
)


def rank_of(authority: str) -> int:
    """权威级的秩；未知值按最低（推测）处理——读侧永远不炸，只是更保守。"""
    return AUTHORITY_RANK.get(str(authority or "").strip().lower(), 0)


# ---------------------------------------------------------------------------
# 字段词表（**有限、可校验**的那一半：把"像不像编造"换成"有没有 claim 支撑"）
# ---------------------------------------------------------------------------
# 每条 = (field_key, 取值正则, 占位模板)。值正则必须**只取一个值**（不贪婪吃整句）。
# 字段词表是**有限集合**——这正是本方案相对"词表枚举形态"的改进：形态无穷，
# 而可登记的字段是可数的，没登记进词表的断言不参与权威序判定（宁漏不误杀）。
#
# 行政/身份后缀表（**事实**性质的少量常量，不是"编造形态"的词表）：判断"这一段是不是
# 一个地名"。它只被用来**收窄**（收不窄就不登记），本身不参与任何"像不像编造"的判断。
_PLACE_SUFFIXES: tuple[str, ...] = ("省", "市", "县", "区", "州", "盟", "旗")

# 省级行政区 + 常见地级市（**有限闭集**，与"字段词表"同一性质：可数、可校验）：
# 用途只有一个——把"我是X"这种**没有后缀提示**的口吻收窄成"X 里确实有个地名"。
# 收不窄就不登记（宁漏不误杀）："我是认真的"里没有任何地名，所以不是籍贯。
_PLACE_NAMES: frozenset[str] = frozenset({
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东",
    "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾",
    "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
    "石家庄", "太原", "沈阳", "长春", "哈尔滨", "南京", "杭州", "合肥", "福州",
    "南昌", "济南", "郑州", "武汉", "长沙", "广州", "南宁", "海口", "成都", "贵阳",
    "昆明", "西安", "兰州", "西宁", "台北", "苏州", "无锡", "宁波", "温州", "厦门",
    "青岛", "大连", "深圳", "珠海", "佛山", "东莞", "株洲", "湘潭", "岳阳", "衡阳",
    "常德", "湛江", "汕头", "洛阳", "开封", "绵阳", "桂林", "三亚", "保定", "唐山",
})

#: 取值里出现就一律不算地名/身份的虚词与情态词（**收窄**，不参与真假判断）。
#: 来源：2026-10-16 台账污染样本实测——"才不像人 / 这就是克劳德大人 / 那时候打架不会切人 / 学委大人"
#: 全被当成了籍贯取值。真地名里不会出现这些字。
_PLACE_VALUE_NOISE: frozenset[str] = frozenset({
    "不", "才", "哪", "啥", "什", "么", "怎", "真", "太", "很",
    "我", "你", "他", "她", "它", "们", "这", "那", "的", "了",
    "吗", "吧", "呢", "啊", "被", "让", "把", "和", "跟", "吗",
})

#: "湖南人" 这种形态里，地名之后允许出现的属后缀（空串 = 就止于地名）。
_PLACE_TAIL_OK: frozenset[str] = frozenset({"", "人", "市", "省", "县", "区", "人士"})

#: 句末收尾（标点 + **句末语气词**）：值正则以它作 lookahead，保证"取到的是**一个值**"
#: 而不是把后面半句吃进来。语气词必须收（"你是广东人吧"正是 21:45:20 事故原句的形态）。
_SENTENCE_TAIL = r"[，。！？；、：\s吧啊呢哦呀嘛啦哈]|$"

_FIELD_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # 籍贯：①"籍贯是/：X（人）"；②"…人"（句读收尾）；③"我是X"（自报口吻，带地名后缀校验）
    ("籍贯", re.compile(r"籍贯[^。！？；\n]{0,8}?[:：是为]\s*"
                        r"([\u4e00-\u9fff]{2,12}?)(?=" + _SENTENCE_TAIL + r")"), "{v}"),
    # "…人"（句读收尾）。**不做位置/前缀排除**：形态正则永远会漏（"他好像是湖南人"
    # 里"是湖南人"照样匹配），该由 :func:`_looks_like_place` 在**取值**上收窄——
    # "好象是湖南人"不是地名，"湖南人"是。这是"有限闭集"那条纪律的又一次应用。
    ("籍贯", re.compile(r"([\u4e00-\u9fff]{2,8}人)(?=" + _SENTENCE_TAIL + r")"), "{v}"),
    # 只在**句子就此结束**（或后接标点/语气词）时才算——"我是湖南长沙的吃法"不算。
    ("籍贯", re.compile(r"我(?:也)?是\s*([\u4e00-\u9fff]{2,10}?)(?:的)?"
                        r"(?=" + _SENTENCE_TAIL + r")"), "{v}"),
    # ③转述口吻（"他跟我说他是湖南长沙的" / "她说她是广东的"）：值归给被转述的人。
    ("籍贯", re.compile(r"(?:说|讲|告诉|自称)[^。！？；\n]{0,6}?"
                        r"(?:他|她|人家)\s*是\s*([\u4e00-\u9fff]{2,10}?)(?:的)?"
                        r"(?=" + _SENTENCE_TAIL + r")"), "{v}"),
    # ④第二人称身份断言（"你是广东人吧" = 21:45:20 事故原句的形态）：值归给**对方**，
    #   字段由上下文（前面那句"报的籍贯"）给。同样带地名后缀校验——"你说的"不是籍贯。
    ("籍贯", re.compile(r"(?:^|[，。！？；、\s])你(?:是|也是)\s*"
                        r"([\u4e00-\u9fff]{2,8}?(?:人|的))"
                        r"(?=" + _SENTENCE_TAIL + r")"), "{v}"),
    ("所在地", re.compile(r"(?:我|他|她|你)(?:现在|最近|目前)?在\s*"
                          r"([\u4e00-\u9fff]{2,8}?(?:省|市|县|区)?)"
                          r"(?:上学|读书|上班|工作|住|待|呆)"), "在{v}"),
    ("年龄", re.compile(r"(?:我|他|她|你)(?:今年)?\s*(\d{1,2})\s*(?:岁|周岁)"), "{v}岁"),
    ("学校", re.compile(r"(?:我|他|她|你)(?:在|读|上)\s*"
                        r"([\u4e00-\u9fffA-Za-z0-9]{2,14}?(?:大学|学院|学校|中学|高中))"),
     "{v}"),
    # 花费：**具体数字 + 时间量**（今晚 18:01 的活体样本）。这里登记的是"一天的
    # 花费"这个**字段**，"五块三"这个**值**一个字都不进代码——判据是台账里有没有
    # 支撑它的 claim，不是"五块三"像不像编的。
    # 钱数的口语写法（"五块三" = 5.3 元）也收：`块|元` 后面可以跟一位角数。
    ("花费", re.compile(r"一天\s*((?:\d+(?:\.\d+)?|"
                        r"[一二三四五六七八九十百千万两]+)(?:块|元)"
                        r"(?:[一二三四五六七八九\d](?:毛|角)?)?(?:钱)?)"), "{v}/天"),
    ("花费", re.compile(r"((?:\d+(?:\.\d+)?|[一二三四五六七八九十百千万两]+)"
                        r"(?:块|元|毛|角)钱?)/?(?:一天|每天|一天到晚)"), "{v}/天"),
)

#: 需要**地名后缀校验**的取值口径（"我是X" / "籍贯是X" 这种没有后缀提示的口吻）：
#: ``(字段名, 该字段第几条 pattern)``。收不窄就不登记（宁漏不误杀——"我是认真的"
#: 不是籍贯）。这是**收窄**规则，不是"编造形态"词表：它不参与任何真假判断。
_PLACE_CHECKED: frozenset[tuple[str, int]] = frozenset({
    ("籍贯", 0),          # "籍贯是X"
    ("籍贯", 1),          # "…人"
    ("籍贯", 2),          # "我是X"
    ("籍贯", 3),          # "他说他是X"
    ("籍贯", 4),          # "你是X人"（第二人称身份断言）
})


def _trim_value(value: str) -> str:
    """去掉取值两端被正则吃进来的虚词/句读（"我是湖南长沙的" → "湖南长沙"）。

    **只做减法、不做改写**：这里改的是"登记成什么值"，不是原文；原文永远原样保留在
    ``evidence`` 里（事后可回查"她当时到底怎么说的"）。
    """
    text = str(value or "").strip()
    while text and text[0] in "是叫为在的了我你他她它":
        text = text[1:]
    for noise in ("好像是", "似乎是", "大概是", "应该是", "可能是", "也许是"):
        if text.startswith(noise):
            text = text[len(noise):]
            break
    while text and text[-1] in "的了吧啊呢哦呀嘛啦":
        text = text[:-1]
    return text.strip()


def _looks_like_place(value: str) -> bool:
    """这一段像不像一个地名/身份（用于**没有后缀提示**或**前缀容易被吃进值里**的口径）。

    判据 = ①以行政/身份后缀收尾（``_PLACE_SUFFIXES``），或 ②值里含一个已知地名
    （``_PLACE_NAMES``，有限闭集）。两条都是**收窄**，不是"编造形态"词表：

    * "我是认真的"两条都不满足（后缀表里没有"的"，地名表里没有"认真"）→ 不是籍贯；
    * "他好像是湖南人"这段文本里的取值是"是湖南人"——"湖南"在地名表里 → 收下（这条
      事实确实被说出来了，只是**权威级**是推测，由 :func:`_authority_of` 判）。
    """
    text = _trim_value(value)
    if len(text) < 2:
        return False
    for noise in _PLACE_VALUE_NOISE:
        if noise in text:
            return False
    if text.endswith("人"):
        # "省/市/县/区/州/盟/旗" 收尾是行政后缀，不答辩；
        # 但 "人" 不能："才不像人 / 人为什么是人 / 学委大人" 都以它收尾。
        # 只有前面要么是已知地名（湖南人）、要么全是数量字（三人）才算。
        stem = text[:-1]
        if re.fullmatch(r"[一二两三四五六七八九十百千万几多]{1,3}", stem):
            return True
        return _starts_with_place(stem)
    if text.endswith(_PLACE_SUFFIXES):
        return True
    return _contains_place(text)


def _starts_with_place(text: str) -> bool:
    """这段文字是否**以地名开头**（后面只能跟无关属后缀）。"""
    rest = str(text or "")
    for size in (4, 3, 2):
        if len(rest) >= size and rest[:size] in _PLACE_NAMES:
            tail = rest[size:]
            return not tail or tail in _PLACE_TAIL_OK
    return False


def _contains_place(text: str) -> bool:
    for size in (4, 3, 2):
        for start in range(0, max(0, len(text) - size + 1)):
            if text[start:start + size] in _PLACE_NAMES:
                return True
    return False


def _field_patterns_for(field_key: str) -> tuple[tuple[str, re.Pattern[str], str], ...]:
    key = str(field_key or "").strip()
    return tuple(item for item in _FIELD_PATTERNS if item[0] == key)


def field_keys() -> tuple[str, ...]:
    """已登记的字段名（顺序稳定、去重）——字段词表是**有限**集合。"""
    seen: list[str] = []
    for key, _rx, _tpl in _FIELD_PATTERNS:
        if key not in seen:
            seen.append(key)
    return tuple(seen)


# --- 归属语态（谁在说谁）---------------------------------------------------
# 说话人由调用方给（canonical = core.conversation.addressing.resolve_attribution）；
# 本模块只判"这句话把哪个字段归给了谁"。
_SELF_MARK = re.compile(r"(?:^|[，。！？；\s])我(?:自己|本人)?(?=[是为在的]|今年|现在|最近|目前)")
# 转述标记：说话人引用第三方。**"是"只在没有推测状语时才算**——`(?<![好像似乎大概
# 应该可能也许])是` 让"他好像是湖南人"落进**推测**（它确实没有来源），而"他说他是
# 长沙人"照旧是转述。少了这个 lookbehind 就会出现"越不确定越权威"的反向判定。
#
# **不带 `@` 昵称捕获**：`@?[\w…]{1,16}` 那种写法会被
# ``tests/test_message_attribution.py`` 的静态守卫⑦判成"第二套归属正则"——它确实是。
# 本模块**不做归属解析**：说话人与引用对象由 canonical
# ``core.conversation.addressing.resolve_attribution`` 给；这里只判"这句话的**语态**
# 是不是转述"，用的是一张**代词/称谓**短表（他/她/人家/对方/那位/这位），不碰 `@`。
_THIRD_MARK = re.compile(
    r"(?:他|她|人家|对方|那位|这位)"
    r"(?:说|讲|告诉|跟(?:我|你|他|她)?说|自称|提过|发过"
    r"|(?<![好像似乎大概应该可能也许])是)")
_SPECULATION_MARK = re.compile(r"(?:好像|似乎|大概|估计|可能|也许|应该|猜|听说是|记不清|不确定)")
# 第三人称代词（**只用来收窄，不用来归属**）：句子里点名了别人、而归归属又解不出来时，
# **不许**把这句话算成"说话人自己说的"（那正是事故 21:45:20 的反向形态：
# 一句"他好像是湖南人"被记成她的自报）。收不窄就不登记 —— 宁漏不误杀。
_THIRD_PERSON = re.compile(r"(?:^|[，。！？；、：\s])(?:他|她|人家|对方|那位|这位)")
#: 转述句里**点到的第三方名字**（严格口径下交给 ``resolve_person`` 解析）：
#: 就是 ``_guess_person`` 的那一条正则——**不新增第二张形态表**。
_NAMED_SUBJECT = re.compile(r"([\u4e00-\u9fffA-Za-z0-9_]{2,16}?)"
                            r"(?:说|讲|告诉|自称|跟我|跟你|说过的)")


def _third_party_tokens(text: str) -> list[str]:
    """转述句里点到的第三方候选名（顺序稳定、去重；严格口径的解析输入）。"""
    out: list[str] = []
    for match in _NAMED_SUBJECT.finditer(str(text or "")):
        token = str(match.group(1) or "").strip()
        if token and token not in out:
            out.append(token)
    return out

# 撤回（更正历史入口）：说话人自己收回刚说过的话。
_RETRACTION_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("自己声明是瞎说", re.compile(r"(?:我)?(?:是)?(?:瞎说|胡说的|乱说的|随便说的|编的|开玩笑的)")),
    ("撤回前话", re.compile(r"(?:当我|就当我)(?:没说|没说过)|收回(?:刚才|前面|上面)?的话")),
    ("自我更正", re.compile(r"我(?:记错|说错|搞错|弄错)了|(?:刚才|前面|上面)(?:那句|那条)不算")),
)


#: 原话在台账里的**上限**（超出只存前 N 字；真相仍在消息表，用 ``source_message_id``
#: + span 回到现场）。限的是"正文副本"，**位置**不设限。
SOURCE_TEXT_MAX: int = 2000

#: 命中片段入列的**上限**（片段只是指路的，位置才是基质：超长片段仍能用 span 切原文）。
EVIDENCE_TEXT_MAX: int = 200


def _offset(value: Any, default: int = -1) -> int:
    """偏移收敛：``None`` / 垃圾 / 负数 → ``default``（**``0`` 是合法偏移**，不许被 or 吃掉）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        warn("core/context/message_facts.py:424 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return default
    return number if number >= 0 else default


@dataclass(frozen=True)
class ClaimCandidate:
    """从一条消息里抽出的**待登记** claim（纯数据；落库与否由 :meth:`ClaimLedger.record` 定）。"""

    field_key: str
    value: str
    authority: str = Authority.SPECULATION.value
    person_key: str = ""
    evidence: str = ""
    #: 证据在**原文里的位置**（半开区间；``-1`` = 没记到）。引文可以截断，位置不会：
    #: ``source_message_id`` + ``[evidence_start:evidence_end]`` 就能回到现场。
    evidence_start: int = -1
    evidence_end: int = -1
    #: 原文（有上限的副本）；字段抽取是**可重建派生缓存**，见 :meth:`ClaimRecord.rebuild`。
    source_text: str = ""
    source_chat_id: str = ""
    source_message_id: str = ""
    source_actor_id: str = ""
    observed_at: Any = None
    source_claim_id: int = 0
    template: str = "{v}"
    #: 作用域（与 ``source_chat_id`` 一样是**指向原始证据**的溯源列，不是第二套事实）：
    #: 写侧从摄取链的消息行带进来，读侧 ``for_chat`` 的平台/账号过滤吃它。
    platform: str = ""
    account_id: str = ""
    #: 这条 claim 所在消息的 content_key（字面归一化，台账内部同一性归并用）
    #: （复述抑制的归并键；留空时落库只按值算键，不自己造第二套指纹）。
    source_content_key: str = ""

    @property
    def rank(self) -> int:
        return rank_of(self.authority)

    def with_person(self, person_key: str) -> "ClaimCandidate":
        return (self if self.person_key == str(person_key or "")
                else ClaimCandidate(**{**self.__dict__, "person_key": str(person_key or "")}))


@dataclass(frozen=True)
class RetractionCandidate:
    """从一条消息里抽出的**撤回**（说话人收回自己刚说的话）。"""

    actor_id: str = ""
    chat_id: str = ""
    reason: str = ""
    evidence: str = ""
    observed_at: Any = None


# ---------------------------------------------------------------------------
# 抽取（确定性、无 IO、无模型）
# ---------------------------------------------------------------------------
def _authority_of(text: str, *, subject_is_speaker: bool) -> str:
    """这一步只判权威级（三级），不判真假。

    顺序有意：**自报优先于转述**——"我自己说的"和"他跟我说"同时出现时，若被说的
    就是说话人自己，那是自报（转述标记只是句法上的搭头）。推测标记只在没有前两者
    时才算：带来源的话不该被一个"可能"降级（那会让低权威反过来压高权威）。
    """
    if subject_is_speaker and _SELF_MARK.search(text):
        return Authority.SELF_REPORT.value
    if _THIRD_MARK.search(text):
        return Authority.RELAY.value
    if _SPECULATION_MARK.search(text):
        return Authority.SPECULATION.value
    if _SELF_MARK.search(text):
        # 第一人称但被说的不是说话人（"我记着他家在长沙"）：那是转述口吻，不是自报。
        return Authority.RELAY.value
    return Authority.SPECULATION.value


def _guess_person(text: str, *, speaker_key: str) -> str:
    """值所属的人：说话人自己，或句子里点到的第三方（取第一个命中的人名片段）。

    **不做归属解析**（那是 ``core.conversation.addressing`` 的活）：这里只在
    "值归给谁"这一格上给一个候选，调用方可以（也应该）传 ``person_key`` 覆盖。
    """
    for match in re.finditer(r"([\u4e00-\u9fffA-Za-z0-9_]{2,16}?)"
                             r"(?:说|讲|告诉|自称|跟我|跟你|说过的)", text):
        token = match.group(1).strip()
        if token and token != speaker_key:
            return token
    return str(speaker_key or "")


def _field_matches_with_spans(text: str) -> list[tuple[str, str, str, int, int]]:
    """字段词表命中 + **原文偏移**：``[(字段名, 取值, 命中片段, start, end)]``。

    **唯一**的"什么算一个可登记的事实"判定处：:func:`extract_claims`（写侧）与
    :func:`assertion_hits`（闸门读侧）共用它——两处各写一份就是"各算一套"。

    本函数**只回答"哪个字段取了什么值"**，不看归属语态；归属语态在
    :func:`_third_party_subject` 里另判一格（两处都是本模块的判据，不是两层判据）。
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    out: list[tuple[str, str, str, int, int]] = []
    seen: set[tuple[str, str]] = set()
    counters: dict[str, int] = {}
    for field_key, pattern, _template in _FIELD_PATTERNS:
        index = counters.get(field_key, 0)
        counters[field_key] = index + 1
        check_place = (field_key, index) in _PLACE_CHECKED
        for match in pattern.finditer(raw):
            value = (match.group(1) or "").strip()
            if not value:
                continue
            if check_place and not _looks_like_place(value):
                continue
            value = _trim_value(value)
            key = (field_key, value)
            if key in seen:
                continue
            seen.add(key)
            out.append((field_key, value, match.group(0), match.start(), match.end()))
    return out


def _field_matches(text: str) -> list[tuple[str, str, str]]:
    """字段词表命中的**三列视图**（写侧/闸门读侧的历史口径；偏移由 spans 版给）。

    与 :func:`_field_matches_with_spans` 是**同一份**实现：这一层只是把 start/end
    丢掉，保证"判的字段"与"记的字段"永远出自同一个循环。
    """
    return [(field_key, value, snippet)
            for field_key, value, snippet, _start, _end
            in _field_matches_with_spans(text)]


#: 第三条路（"**这条 claim 到底是谁的**"）的取值：**判据在写侧，不在调用方**。
SUBJECT_SPEAKER = "speaker"            # 说话人本人（自报那一格）
SUBJECT_NAMED = "named"                # 句子里点名、且解析得出规范 id 的第三方
SUBJECT_UNBOUND = "unbound"            # 点了第三方但解析不出人 → **不登记**（宁漏不误杀）


def classify_subject(text: str, *, speaker_key: str = "",
                     known_person_keys: Iterable[str] | None = None,
                     resolve_person: Any = None) -> tuple[str, str]:
    """这条消息里的 claim 归给谁：``(归属形态, person_key)``。

    这是**判据**（写侧三级权威的输入），不是调用方随手传的参数——所以它住在本模块，
    与 :func:`_authority_of` 同一处：

    * :data:`SUBJECT_SPEAKER` —— 语态是自报（``我是…``）且说话人可识别；
    * :data:`SUBJECT_NAMED` —— 点了第三方（他/她/人家/对方/那位/这位）**且**那个第三方
      的规范 id 解析得出来：先看 ``known_person_keys``（canonical 身份解析的结果，
      如 ``@`` 提及、名单）里有没有直接出现在文本里的 id，再让
      ``resolve_person(文本里点到的名字)`` 给一次（**归属解析的 owner 是调用方**，
      见下）；值归**被说的那个人**；
    * :data:`SUBJECT_UNBOUND` —— 点了第三方却解析不出人（"他好像是湖南人"里的"他"）。
      **这一格不登记**：拿它当"说话人自己说的"登记，就会把一句空话变成她的自报
      （事故 21:45:20 的反向形态）；跳过是"宁漏不误杀"的方向。

    ``resolve_person`` 是**注入解，不是第二套归属解析**：昵称 → 规范 id 的解析是
    canonical 身份层的活（``core.people`` / 群名单），本模块只**问一次**、拿回来的是
    一个 id 或空串。取不到人就不登记（与上面那条同一个方向）。``None`` = 调用方没有
    解析器（旧口径，只认文本里直接出现的 id）；生产写侧给的是摄取链手上的那一份。

    代词/称谓表就是 :data:`_THIRD_MARK` 那一张（**不新增第二张**）：本函数只回答
    "这句话的语态是不是转述"，**不做归属解析**（`@` 与昵称的解析是 canonical
    ``core.conversation.addressing.resolve_attribution`` 的活，见本模块文件头）。
    """
    raw = str(text or "")
    speaker = str(speaker_key or "")
    if not raw.strip():
        return SUBJECT_UNBOUND, ""
    explicit = known_person_keys is not None
    known = [
        str(item).strip() for item in (known_person_keys or ())
        if str(item or "").strip() and str(item).strip() != speaker
    ]

    def _legacy() -> tuple[str, str]:
        """**旧口径**（``known_person_keys=None``）：只换归属形状，不换"要不要登记"。

        这是既有的抽取行为（``extract_claims`` 的历史契约）：点到的名字直接当第三方，
        把"他/她"这种代词也当名字用。**保持逐字不变**——生产写侧不走这一格。
        """
        if _THIRD_MARK.search(raw):
            subject = _guess_person(raw, speaker_key=speaker)
            return (SUBJECT_SPEAKER, speaker) if subject == speaker else (
                SUBJECT_NAMED, subject)
        return (SUBJECT_SPEAKER, speaker) if speaker else (SUBJECT_UNBOUND, "")

    if explicit:
        # **严格口径**（生产写侧）：先看能不能把点到的第三方解析成规范 id。
        for key in known:
            if key in raw:
                return SUBJECT_NAMED, key
        for token in _third_party_tokens(raw):
            if not token or token == speaker:
                continue
            if token in known:
                return SUBJECT_NAMED, token
            if callable(resolve_person):
                try:
                    resolved = str(resolve_person(token) or "").strip()
                except Exception:  # noqa: BLE001 - 解析器坏了 = 解析不出人
                    warn("core/context/message_facts.py:627 Exception", err=repr(__import__("sys").exc_info()[1]))
                    resolved = ""
                if resolved and resolved != speaker:
                    return SUBJECT_NAMED, resolved
        if _THIRD_MARK.search(raw) or _THIRD_PERSON.search(raw):
            # 点了别人却解析不出人：**不登记**（宁漏不误杀）。
            return SUBJECT_UNBOUND, ""
        return (SUBJECT_SPEAKER, speaker) if speaker else (SUBJECT_UNBOUND, "")
    return _legacy()


def extract_claims(
    text: str,
    *,
    speaker_key: str = "",
    person_key: str = "",
    chat_id: str = "",
    message_id: str = "",
    actor_id: str = "",
    observed_at: Any = None,
    platform: str = "",
    account_id: str = "",
    known_person_keys: Iterable[str] | None = None,
    subject_is_speaker: bool | None = None,
    resolve_person: Any = None,
) -> list[ClaimCandidate]:
    """从一条消息里抽出待登记 claim（**纯函数**：同输入同输出、不查库、不调模型）。

    ``person_key`` 非空时值一律归给它（调用方已解析好归属）；为空时退回
    :func:`classify_subject`（**判据在本模块**，见它的 docstring）：自报归说话人，
    点名的第三方归那个第三方，**解析不出人就一条都不登记**。

    ``subject_is_speaker`` 只在调用方**已经**按 canonical 归属解析出"值归说话人自己"
    时显式传 ``True``（写侧的摄取链就是这一格）；``None`` = 由 :func:`classify_subject`
    判。它**不是**权威级：权威级一律由 :func:`_authority_of` 从文本语态 + 归属判出来，
    调用方传不进来。
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    speaker = str(speaker_key or actor_id or "")
    if person_key:
        subject = str(person_key or "")
        if subject_is_speaker is None:
            subject_is_speaker = bool(speaker) and subject == speaker
    else:
        shape, subject = classify_subject(
            raw, speaker_key=speaker, known_person_keys=known_person_keys,
            resolve_person=resolve_person,
        )
        if shape == SUBJECT_UNBOUND:
            # 点了第三方却解析不出人：**不登记**（宁漏不误杀——见 classify_subject）。
            return []
        subject_is_speaker = shape == SUBJECT_SPEAKER
    authority = _authority_of(raw, subject_is_speaker=bool(subject_is_speaker))
    out: list[ClaimCandidate] = []
    templates = {key: template for key, _rx, template in _FIELD_PATTERNS}
    source_text = raw[:SOURCE_TEXT_MAX]
    for field_key, value, snippet, start, end in _field_matches_with_spans(raw):
        out.append(ClaimCandidate(
            field_key=field_key,
            value=value,
            authority=authority,
            person_key=subject,
            evidence=snippet,
            evidence_start=int(start),
            evidence_end=int(end),
            source_text=source_text,
            source_chat_id=str(chat_id or ""),
            source_message_id=str(message_id or ""),
            source_actor_id=speaker,
            observed_at=observed_at,
            template=templates.get(field_key, "{v}"),
            platform=str(platform or ""),
            account_id=str(account_id or ""),
        ))
    return out


def extract_retractions(
    text: str,
    *,
    actor_id: str = "",
    speaker_key: str = "",
    chat_id: str = "",
    observed_at: Any = None,
) -> list[RetractionCandidate]:
    """从一条消息里抽出撤回声明（纯函数）。第一条命中即返回（一句话只撤回一次）。"""
    raw = str(text or "")
    for reason, pattern in _RETRACTION_RES:
        match = pattern.search(raw)
        if match:
            return [RetractionCandidate(
                actor_id=str(actor_id or speaker_key or ""),
                chat_id=str(chat_id or ""),
                reason=reason,
                evidence=match.group(0),
                observed_at=observed_at,
            )]
    return []


# ---------------------------------------------------------------------------
# 写侧（**唯一生产写口**：摄取链 → 台账；读侧在 :func:`evaluate_claims`）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ClaimMessage:
    """摄取链上的一条消息（**纯数据**：写侧只吃这几个字段，不查库、不调模型）。

    字段名与 ``NEW_MESSAGES_SYNCED`` 载荷逐字对应（``sender_id`` / ``chat_id`` /
    ``msg_id`` / ``is_self`` / ``msg_type_name``），所以接线处只做一次搬运、不做解析。
    ``known_person_keys`` 是这一批消息**已解析出的规范身份 id**（canonical 解析的结果，
    不是本模块解析的）：句子里点名第三方时，只有它里面的人才会被登记（见
    :func:`classify_subject`）。
    """

    text: str = ""
    chat_id: str = ""
    speaker_key: str = ""
    message_id: str = ""
    platform: str = ""
    account_id: str = ""
    observed_at: Any = None
    kind: str = "text"
    is_self: bool = False
    known_person_keys: tuple[str, ...] = ()
    #: 昵称 → 规范 id 的**解析器**（canonical 身份层的活；本模块只问一次）。
    #: ``None`` = 没有解析器（只认文本里直接出现的 id）——见 :func:`classify_subject`。
    resolve_person: Any = None


@dataclass(frozen=True)
class MessageClaimResult:
    """一条消息的**写侧读数**（每一种结果都显式；没有"静默丢弃"这一格）。"""

    traced: bool = False
    recorded: tuple[tuple[str, str, str, int], ...] = ()
    #: 被台账拒掉的那些（收口后只剩"无法登记的行"：字段/值为空；``off`` 下低权威也走这格）
    rejected: tuple[tuple[str, str, str, int], ...] = ()
    #: 与更高权威**并列**登记下来的（新行在案，但它不是当前权威）：
    #: ``(字段, 取值, 权威级, 新行 id)`` —— 低权威不再被拒写（写时不承诺）。
    parallel: tuple[tuple[str, str, str, int], ...] = ()
    skipped: tuple[str, ...] = ()
    already_seen: bool = False
    withdrawn: tuple[int, int] = (0, 0)

    @property
    def rows(self) -> int:
        """这一条消息**新增**了几行（幂等回放时恒 0）。"""
        return len(self.recorded)

    def to_dict(self) -> dict[str, Any]:
        return {
            "traced": bool(self.traced),
            "recorded": [list(item) for item in self.recorded],
            "rejected": [list(item) for item in self.rejected],
            "parallel": [list(item) for item in self.parallel],
            "skipped": list(self.skipped),
            "already_seen": bool(self.already_seen),
            "withdrawn": list(self.withdrawn),
        }


def _is_text_message(message: ClaimMessage) -> bool:
    """只登记**纯文本**消息（富媒体/系统消息的正文不是"她说的话"，宁漏不误杀）。"""
    kind = str(message.kind or "").strip().lower()
    return kind in ("", "text")


def message_claim_candidates(message: ClaimMessage) -> list[ClaimCandidate]:
    """一条消息 → 待登记 claim（**纯函数**；权威级在这里由判据给）。

    判据链（**全在本模块**，调用方一个都传不进来）：

    ① :func:`classify_subject` 判"值归给谁"→ 自报 = 说话人本人 / 转述 = 点名的第三方
       / 解析不出人 = **不登记**；
    ② :func:`_authority_of` 从文本语态（自报标记 > 转述标记 > 推测标记）+
       ①的归属判出权威级；三级是**算出来的**，不是传进来的；
    ③ :func:`_field_matches` 判"哪个字段取了什么值"（与闸门读侧同一份字段词表）。
    """
    if message.is_self or not _is_text_message(message):
        return []
    speaker = str(message.speaker_key or "").strip()
    text = str(message.text or "")
    if not speaker or not text.strip():
        return []
    return extract_claims(
        text,
        speaker_key=speaker,
        chat_id=str(message.chat_id or ""),
        message_id=str(message.message_id or ""),
        actor_id=speaker,
        observed_at=message.observed_at,
        platform=str(message.platform or ""),
        account_id=str(message.account_id or ""),
        known_person_keys=tuple(message.known_person_keys or ()),
        resolve_person=message.resolve_person,
    )


def _message_content_key(message: ClaimMessage) -> str:
    """这条消息的 ``content_key``（字面归一化；台账内部同一性归并用）。"""
    key = content_key(message.text)
    return key or str(message.message_id or "")


def _recorded_message(message: ClaimMessage, ledger: ClaimLedger) -> bool:
    """这条消息**已经登记过**了吗（幂等读口；幂等键 = 会话 + 消息 id）。

    读的是台账自己的溯源列（``source_message_id``）——同一个 ``(chat, msg_id)`` 第二次
    走到写口时返回真，写口据此**一行都不写**。没有消息 id 时返回假：那一格回落到
    ``ClaimLedger.record`` 自己的同权威同值幂等（它已经管这一半）。
    """
    message_id = str(message.message_id or "").strip()
    if not message_id:
        return False
    return any(
        str(row.source_message_id or "") == message_id
        for row in ledger.for_chat(str(message.chat_id or ""))
    )


def record_message_claims(
    message: ClaimMessage, *, ledger: ClaimLedger | None = None,
    engine: Any = None, now: Any = None,
) -> MessageClaimResult:
    """**写侧唯一入口**：把"谁在何时自报/转述/推测了什么"真的落到 ``claim_ledger``。

    开关 ``YINYUE_FEATURE_CLAIM_LEDGER``（三态，唯一读点 :func:`claim_ledger_mode`）
    门控**整条写路径**：

    * ``off``（默认）→ **逐字节一致 + 零 IO**：不构造台账、不查库、不建对象，
      直接返回 ``traced=False`` 的空读数（回滚位）；
    * ``shadow`` / ``on`` → 判定 + 落库。*写侧不区分 shadow/on*：影子期要攒的就是
      这份台账本身（"开了影子也还是 0 行"正是本次要修的缺口），而**对外行为**由读侧
      （:func:`ledger_enforcing`）分档。

    三条口径：

    * **权威级由判据给**：见 :func:`message_claim_candidates`，调用方没有权威级参数；
    * **幂等**：同一个 ``(chat, msg_id)`` 第二次进来 → ``already_seen=True``、零写入；
    * **撤回**：说话人自己收回（"我瞎说的"/"记错了"）→ 走 :meth:`ClaimLedger.withdraw`，
      旧行**留着**、只写 ``withdrawn_at``；被它更新掉的旧行同样作废（不得静默改写）。

    **必须给 ``engine`` 或 ``ledger``**（两者都没有 → ``traced=False``、一行都不写）：
    写侧不许靠"默认 meta 库"兜底——那条路会把一个没接上 repo 的调用点变成**往生产库
    写字**（读侧 :func:`evaluate_claims` 可以兜底，因为读错最多是"没有台账"；写错是
    事实污染，方向不同）。
    """
    if not ledger_tracing():
        return MessageClaimResult(traced=False)
    if message.is_self or not str(message.speaker_key or "").strip():
        return MessageClaimResult(traced=False)
    if ledger is None and engine is None:
        # 没有可写的台账句柄：宁可不写，也不许回落到 bootstrap 的默认（生产）meta 库。
        return MessageClaimResult(traced=False, skipped=("写侧没拿到台账句柄",))
    book = ledger if ledger is not None else ClaimLedger()
    if not _is_text_message(message):
        return MessageClaimResult(traced=True, skipped=("非文本消息不登记",))
    if _recorded_message(message, book):
        # 幂等：同一条消息重复处理 → **不产生第二行**。
        return MessageClaimResult(traced=True, already_seen=True)

    recorded: list[tuple[str, str, str, int]] = []
    rejected: list[tuple[str, str, str, int]] = []
    parallel: list[tuple[str, str, str, int]] = []
    for candidate in message_claim_candidates(message):
        outcome, claim_id = book.record(candidate, engine=engine, now=now)
        row = (candidate.field_key, candidate.value, candidate.authority, int(claim_id))
        if outcome in (RecordOutcome.INSERTED, RecordOutcome.SUPERSEDED):
            recorded.append(row)
        elif outcome == RecordOutcome.DUPLICATE:
            # 同一权威同值：幂等（不新增行），但那条 claim 确实是这条消息说的。
            recorded.append(row)
        elif outcome == RecordOutcome.PARALLEL:
            # **低权威也留档**：新行确实写下去了（rows 要如实计数），但它是"并列说法"，
            # 读侧默认透镜不会选它 —— 两条读数都记下来，谁说的都不丢。
            recorded.append(row)
            parallel.append(row)
        else:
            rejected.append(row)
    withdrawn = (0, 0)
    for retraction in extract_retractions(
        message.text, actor_id=str(message.speaker_key or ""),
        chat_id=str(message.chat_id or ""), observed_at=message.observed_at,
    ):
        withdrawn = book.withdraw(
            chat_id=retraction.chat_id, actor_id=retraction.actor_id,
            reason=retraction.reason, evidence=retraction.evidence,
            engine=engine, now=now,
        )
    if not recorded and not rejected and not any(withdrawn):
        return MessageClaimResult(traced=True, skipped=("这条消息里没有可归属的 claim",))
    return MessageClaimResult(
        traced=True, recorded=tuple(recorded), rejected=tuple(rejected),
        parallel=tuple(parallel),
        withdrawn=(int(withdrawn[0]), int(withdrawn[1])),
    )


# ---------------------------------------------------------------------------
# 归一化（content_key / value_key 共用这一份字面归一化）
# ---------------------------------------------------------------------------
def _normalise_key(text: str) -> str:
    """字面归一化：去掉非文字符号后 casefold（台账内部的同一性归并用）。

    2026-10-24 起不再借用 ``core.speech.guards`` 的指纹函数——那个指纹随复读闸
    一起退役；本模块自带这一份最小归一化，**不依赖闸门模块**，也不再有两套口径。
    """
    return re.sub(r"[\W_]+", "", str(text or ""), flags=re.UNICODE).casefold()


def content_key(text: str) -> str:
    """一条消息的 ``content_key``：去空白/标点折叠后的字面指纹（台账内部用）。"""
    return _normalise_key(text)


def value_key(value: str) -> str:
    """字段取值的比较键（"湖南长沙" vs "广东" → 不同键；"长沙" ⊂ "湖南长沙" 见下）。"""
    return _normalise_key(value)


# ---------------------------------------------------------------------------
# 台账记录（读侧结构；写侧在 :class:`ClaimLedger`）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ClaimRecord:
    """一行 claim 的只读投影（**原文列一律不改**：更正只写 superseded_* 三列）。"""

    id: int = 0
    platform: str = ""
    account_id: str = ""
    chat_id: str = ""
    person_key: str = ""
    field_key: str = ""
    value: str = ""
    content_key_value: str = ""
    authority: str = Authority.SPECULATION.value
    source_kind: str = ""
    source_actor_id: str = ""
    source_chat_id: str = ""
    source_message_id: str = ""
    source_claim_id: int = 0
    observed_at: str = ""
    superseded_by: int = 0
    supersede_reason: str = ""
    superseded_at: str = ""
    withdrawn_at: str = ""
    created_at: str = ""
    # P0-2-B：读时透镜
    #: 原文的有上限副本；真相仍在消息表，用 ``source_message_id`` + span 回到现场。
    source_text: str = ""
    #: 命中片段（只是指路的；位置见下）
    evidence_text: str = ""
    #: 片段在**原文**里的半开区间（``-1`` = 没记到）
    evidence_start: int = -1
    evidence_end: int = -1

    @property
    def rank(self) -> int:
        return rank_of(self.authority)

    @property
    def active(self) -> bool:
        """还能不能被采用：没被更新掉、也没被撤回。"""
        return not self.superseded_by and not str(self.withdrawn_at or "").strip()

    @property
    def span(self) -> tuple[int, int]:
        """命中片段在原文里的半开区间；没记到就是 ``(-1, -1)``。"""
        return (_offset(self.evidence_start), _offset(self.evidence_end))

    @property
    def has_span(self) -> bool:
        start, end = self.span
        return start >= 0 and end > start

    @property
    def quote(self) -> str:
        """原话里那一段（span 切得到就切原文；切不到退回入列时的片段）。"""
        if self.has_span and self.source_text:
            sliced = self.source_text[self.span[0]:self.span[1]]
            if sliced:
                return sliced
        return self.evidence_text

    @property
    def evidence_ref(self) -> EvidenceRef:
        """通用 span 视图（P1 复用同一个类型，不另造）：位置为主，引文只是指路。"""
        return EvidenceRef(
            source=self.source_kind or CLAIM_LEDGER_TABLE,
            message_id=self.source_message_id,
            account_id=self.account_id,
            chat_id=self.source_chat_id,
            quoted_text=self.evidence_text or self.quote,
            start=self.evidence_start,
            end=self.evidence_end,
        )

    def rebuild(self) -> dict[str, Any]:
        """拿 ``source_text`` 重跑一遍字段抽取（**只读**：不碰任何库、不改任何行）。

        这一格就是"字段/值/span 是**可重建派生缓存**"的落点：真相是原话 + 位置，
        抽取随时可以重算；``matches_stored`` 只是自检（能不能用同一份词表重建出当初
        登记的那一条）。
        """
        spans = _field_matches_with_spans(self.source_text) if self.source_text else []
        rebuilt = [{"field_key": key, "value": value, "evidence": snippet,
                    "start": int(start), "end": int(end)}
                   for key, value, snippet, start, end in spans]
        mine = value_key(self.value)
        matches_stored = any(
            item["field_key"] == self.field_key
            and (value_key(item["value"]) == mine
                 or (item["start"] == _offset(self.evidence_start)
                     and item["end"] == _offset(self.evidence_end)))
            for item in rebuilt
        )
        return {"source_message_id": self.source_message_id,
                "span": self.span, "rebuilt": rebuilt,
                "matches_stored": bool(matches_stored)}

    def to_dict(self) -> dict[str, Any]:
        """读侧透镜要的全量行：值 / 权威 / 来源 / 时间 / span / 原话片段 / 是否仍有效。"""
        return {
            "id": self.id,
            "person_key": self.person_key,
            "field_key": self.field_key,
            "value": self.value,
            "authority": self.authority,
            "source_kind": self.source_kind,
            "source_actor_id": self.source_actor_id,
            "source_message_id": self.source_message_id,
            "observed_at": self.observed_at,
            "evidence_text": self.evidence_text,
            "span": self.span,
            "source_text_len": len(self.source_text),
            "superseded_by": self.superseded_by,
            "supersede_reason": self.supersede_reason,
            "withdrawn_at": self.withdrawn_at,
            "active": self.active,
        }


class RecordOutcome(StrEnum):
    """一次写入的结果（**每一种都显式**，没有"静默改写"这一格）。"""

    INSERTED = "inserted"           # 新登记一条
    DUPLICATE = "duplicate"         # 同一权威级、同一个值：幂等，不新增行
    SUPERSEDED = "superseded"       # 同一权威级的新值：旧行标 superseded_by，新行插入
    REJECTED = "rejected"           # 无法登记的行（字段/值为空）；``off`` 下低权威也走这格
    PARALLEL = "parallel"           # 低权威**不再被拒**：新行在案，但它不是当前权威
    WITHDRAWN = "withdrawn"         # 撤回（自身或下游作废）


@dataclass(frozen=True)
class ClaimResolution:
    """某个 ``(chat, person, field)`` 上的**读时读数**（结论 + 全部并列说法，一次读全）。"""

    chat_id: str = ""
    person_key: str = ""
    field_key: str = ""
    authoritative: ClaimRecord | None = None
    #: **并列说法**：整条链上非赢家的全部行（含被更新/被撤回的）—— 不再只收一个当前值。
    conflicts: tuple[ClaimRecord, ...] = ()
    history: tuple[ClaimRecord, ...] = ()
    #: 读时**透镜**（同一批行换排序）：``authority``（默认）/ ``recency``。
    order: str = "authority"

    @property
    def has_claim(self) -> bool:
        return self.authoritative is not None

    @property
    def authority(self) -> str:
        return self.authoritative.authority if self.authoritative else ""

    @property
    def value(self) -> str:
        return self.authoritative.value if self.authoritative else ""

    def supports(self, value: str) -> bool:
        """``value`` 与权威值**相容**吗（相等 / 一方包含另一方 —— 与复述同一份宽容度）。"""
        mine, theirs = value_key(self.value), value_key(value)
        if not mine or not theirs:
            return False
        return mine == theirs or mine in theirs or theirs in mine

    def conflicts_with(self, value: str) -> bool:
        return self.has_claim and not self.supports(value)

    @property
    def active_conflicts(self) -> tuple[ClaimRecord, ...]:
        """仍然有效的其它说法（``conflicts`` 里已被更新/撤回的那些除外）。"""
        return tuple(row for row in self.conflicts if row.active)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "person_key": self.person_key,
            "field_key": self.field_key,
            #: 读法（透镜）名，跟结论一起吐出去：同一批行可以是不同的读数。
            "order": self.order,
            "authoritative": (self.authoritative.value if self.authoritative else ""),
            "authority": self.authority,
            "authoritative_record": (self.authoritative.to_dict()
                                    if self.authoritative else None),
            #: 每条都带值 / 权威 / 来源 / 时间 / span —— 不再只剩一个值或一个 id。
            "conflicts": [item.to_dict() for item in self.conflicts],
            "history": [item.to_dict() for item in self.history],
        }


# ---------------------------------------------------------------------------
# 台账本体
# ---------------------------------------------------------------------------
def _stamp(value: Any) -> str:
    """时间列一律 naive 本地墙钟字符串（与 ``work_items`` / ``session_focus`` 同口径）。"""
    if value is None:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


class ClaimLedger:
    """claim 台账：**权威序的 owner**。

    ``records`` 可以直接注入（测试 / 回放）；不注入时从 meta 库读
    （``records=None`` → :meth:`_load`）。任何读失败都降级成"没有台账"，
    绝不因为台账读不到而拒绝服务（同 ``output_gate`` 的兜底口径）。
    """

    def __init__(self, records: Sequence[ClaimRecord] | None = None,
                 *, meta_repo: Any = None) -> None:
        self._injected = records is not None
        self._records: list[ClaimRecord] = list(records or ())
        self._meta_repo = meta_repo

    # ---- 读 ---------------------------------------------------------------
    def _rows(self) -> list[ClaimRecord]:
        if self._injected:
            return list(self._records)
        return self._load()

    def _load_read_only(self, reason: str) -> list[ClaimRecord] | None:
        """可写引擎拿不到时的**只读**兵底（拿不到就返回 ``None``）。

        为什么需要这一格：警卫拒绝给“可写的 meta engine”时（没声明写生产库的
        进程，如 Web 进程 / 只读诊断），**读台账本来就不该被写警卫连坐**——这正是
        ``db.write_guard.read_only_engine`` 那条口径（“只读不经过本模块”）。没有它，
        ``claim_ledger_read_failed`` 会把“进程没声明写权”误报成“台账没数据”。

        ``reason`` 只用于日志：原因丢了就永远不知道过去患过什么。
        """
        try:
            from sqlalchemy import text as _sql

            from core.bootstrap import get_meta_db_path
            from db.write_guard import read_only_engine

            engine = read_only_engine(get_meta_db_path())
            try:
                with engine.connect() as conn:
                    rows = conn.execute(_sql(
                        "select * from claim_ledger order by id"
                    )).mappings().all()
                return [_record_from_row(dict(row)) for row in rows]
            finally:
                engine.dispose()
        except Exception as exc:  # noqa: BLE001 - 只读也读不到 = 真的没读到
            log.warning("claim_ledger_read_failed", error=str(exc), writable_error=reason[:200])
            return None

    def _load(self) -> list[ClaimRecord]:
        try:
            from sqlalchemy import text as _sql

            repo = self._meta_repo
            if repo is None:
                from core.terminal.store_port import get_meta_repo

                repo = get_meta_repo()
            engine = repo.engine
            with engine.connect() as conn:
                rows = conn.execute(_sql(
                    "select * from claim_ledger order by id"
                )).mappings().all()
            return [_record_from_row(dict(row)) for row in rows]
        except Exception as exc:  # noqa: BLE001 - 台账读不到 = 没有台账，绝不拒绝服务
            warn("core/context/message_facts.py:1216 Exception", err=repr(exc))
            rows = self._load_read_only(str(exc))
            if rows is not None:
                return rows
            return []

    def for_chat(self, chat_id: str, *, platform: str = "",
                 account_id: str = "") -> list[ClaimRecord]:
        chat = str(chat_id or "")
        rows = [
            row for row in self._rows()
            if row.chat_id == chat
            and (not platform or row.platform in ("", platform))
            and (not account_id or row.account_id in ("", account_id))
        ]
        return rows

    def history(self, chat_id: str, person_key: str,
                field_key: str) -> tuple[ClaimRecord, ...]:
        """``(chat, person, field)`` 上的**更正链**（按 id 升序 = 时间序）。"""
        return tuple(
            row for row in self.for_chat(chat_id)
            if row.person_key == str(person_key or "")
            and row.field_key == str(field_key or "")
        )

    def resolve(self, chat_id: str, person_key: str, field_key: str, *,
                order: str = "authority") -> ClaimResolution:
        """**读时透镜**：同一 ``(chat, person, field)`` 上的当前读数。

        ``order`` 是**读法**，不是写死的世界模型（同一批行可以换着读）：

        * ``authority``（默认，逐字旧口径）：只取**仍有效**的行，权威序优先、同级取最新；
        * ``recency``：同一批仍有效的行，**时间序**优先（谁最后说的）——权威级只当同刻次序。

        两种读法都**不写库、不改任何行**。``conflicts`` 是**整条链上非赢家的全部说法**
        （含被更新掉的与被撤回的）：低权威不再被拒写（见 :meth:`record`），读侧自然要
        把"还有别人说过什么"一起交出来 —— 这就是并列说法的落点。
        """
        chain = self.history(chat_id, person_key, field_key)
        active = [row for row in chain if row.active]
        winner: ClaimRecord | None = None
        if str(order or "authority") == "recency":
            for row in active:
                if winner is None or int(row.id) > int(winner.id):
                    winner = row
        else:
            for row in active:
                if winner is None:
                    winner = row
                    continue
                if row.rank > winner.rank:
                    winner = row
                elif row.rank == winner.rank and int(row.id) > int(winner.id):
                    winner = row
        conflicts = tuple(
            row for row in chain if winner is not None and row.id != winner.id
        )
        return ClaimResolution(
            chat_id=str(chat_id or ""), person_key=str(person_key or ""),
            field_key=str(field_key or ""), authoritative=winner,
            conflicts=conflicts, history=chain, order=str(order or "authority"),
        )

    def claims_in_chat(self, chat_id: str, *, platform: str = "",
                       account_id: str = "", limit: int = 40) -> list[ClaimRecord]:
        """这个会话里**仍有效**的 claim（最近先）。

        与 :meth:`resolve` 同一个“仍有效”口径（``superseded_by = 0 and withdrawn_at = ''``）：
        给回忆读侧用——读到的话必须是当前还算数的那一份。读不到台账返空，不抛。
        """
        rows = [row for row in self.for_chat(
            chat_id, platform=platform, account_id=account_id,
        ) if row.active]
        rows.sort(key=lambda row: int(row.id), reverse=True)
        return rows[: int(limit)] if limit and int(limit) > 0 else rows

    def in_window(self, chat_id: str, since: Any = None, *,
                  limit: int = 0) -> list[ClaimRecord]:
        """按会话读最近登记（``since`` 为 naive 墙钟字符串；``limit`` 0 = 不限）。"""
        rows = self.for_chat(chat_id)
        if since:
            bound = _stamp(since)
            rows = [row for row in rows if str(row.created_at or "") >= bound]
        rows.sort(key=lambda row: int(row.id), reverse=bool(limit))
        return rows[: int(limit)] if limit and int(limit) > 0 else rows

    # ---- 写 ---------------------------------------------------------------
    def record(self, candidate: ClaimCandidate, *,
               engine: Any = None, now: Any = None) -> tuple[RecordOutcome, int]:
        """登记一条 claim（**一次调用 = 至多一行**；重/更/并列/无法登记四种结果都显式）。

        判定次序（照规格，一步都不许跳）：

        1. 同一 ``(chat, person, field)`` 上已有**仍有效**的同级同值 → ``DUPLICATE``
           （幂等，不新增行：同一句话被复述不该在台账里长成两行）；
        2. 已有**更高权威**的有效行 → **``PARALLEL``**（2026-10-16「写时不承诺」收口）：
           新行**照登记**（转述/推测也是"有人这么说过"），高权威旧行**一字不动**，
           另行记一行 ``claim_ledger_audit``。谁更可信由**读时透镜**定
           （:meth:`resolve` 的 ``order`` 在同一批行上换排序）；
           只有 ``CLAIM_LEDGER=off`` 才回到旧口径（早退 ``REJECTED``，旧行一字不动）；
        3. 已有**同级**的有效行且值不同 → ``SUPERSEDED``：旧行只写 ``superseded_by`` /
           ``supersede_reason`` / ``superseded_at``，**正文不改**；
        4. 已有**更低权威**的有效行 → ``SUPERSEDED``（高权威覆盖低权威，这是合法方向）；
        5. 没有有效行 → ``INSERTED``。

        **注入式台账（``records`` 非 None）全在内存里**：读是它、写也必须是它——
        否则"读空、写库"会让幂等与权威序在同一个对象上各看一个地方（离线用例/回放
        就会发现写进去了却读不回来）。这一格**零 IO**，也不碰默认（生产）库。
        """
        person = str(candidate.person_key or "")
        field_key = str(candidate.field_key or "")
        value = str(candidate.value or "")
        authority = str(candidate.authority or Authority.SPECULATION.value)
        if not field_key or not value:
            return RecordOutcome.REJECTED, 0
        chat = str(candidate.source_chat_id or "")
        rows = self.history(chat, person, field_key)
        active = [row for row in rows if row.active]
        incoming = rank_of(authority)
        #: 开关三态：``off`` = 旧口径（低权威被拒，回滚位）；``shadow`` / ``on`` =
        #: 「写时不承诺」（低权威也留档，权威序降级成读时透镜）。
        relaxed = ledger_enabled()
        same_value = [row for row in active
                      if value_key(row.value) == value_key(value)]
        if same_value:
            best = max(same_value, key=lambda row: row.rank)
            if rank_of(best.authority) == incoming:
                self._audit_row(
                    engine, action="duplicate", record=best, candidate=candidate,
                    reason="同权威同值（幂等）")
                return RecordOutcome.DUPLICATE, int(best.id)
            if not relaxed and rank_of(best.authority) > incoming:
                self._audit_row(
                    engine, action="rejected", record=best, candidate=candidate,
                    reason="低权威不得覆盖高权威（同值）")
                return RecordOutcome.REJECTED, int(best.id)
        blockers = [row for row in active if row.rank > incoming]
        if blockers and not relaxed:
            best = max(blockers, key=lambda row: (row.rank, int(row.id)))
            self._audit_row(
                engine, action="rejected", record=best, candidate=candidate,
                reason=f"{authority} 不得覆盖 {best.authority}")
            log.info("claim_ledger_rejected", person=person, field=field_key,
                     incoming=authority, existing=best.authority)
            return RecordOutcome.REJECTED, int(best.id)

        new_id = self._insert(candidate, engine=engine, now=now)
        if new_id <= 0:
            return RecordOutcome.REJECTED, 0
        # 旧行（同级或更低、仍有效）**只标更正链**，正文一字不动；
        # 更高权威的行**一字不动**（那是并列说法，不是被推翻）。
        superseded_ids: list[int] = []
        for row in active:
            if int(row.rank) > incoming:
                continue
            if self._injected:
                self._supersede_row(int(row.id), new_id,
                                    reason=f"superseded_by:{authority}")
            else:
                _mark_superseded(engine or self._meta_repo, int(row.id), new_id,
                                 reason=f"superseded_by:{authority}", now=now)
            superseded_ids.append(int(row.id))
        if blockers:
            # 并列留档：新行在案，旧的高权威行仍在案 —— 谁更可信读时再定。
            best = max(blockers, key=lambda row: (row.rank, int(row.id)))
            self._audit_row(
                engine, action="parallel", record=best, candidate=candidate,
                reason=", ".join(str(row.id) for row in blockers))
            log.info("claim_ledger_parallel", person=person, field=field_key,
                     incoming=authority, existing=best.authority)
            return RecordOutcome.PARALLEL, new_id
        outcome = (RecordOutcome.SUPERSEDED if superseded_ids
                   else RecordOutcome.INSERTED)
        self._audit_row(engine, action=str(outcome), record=None,
                        candidate=candidate,
                        reason=", ".join(str(i) for i in superseded_ids))
        return outcome, new_id

    # ---- 内部：注入式台账（纯内存，零 IO）---------------------------------
    def _audit_row(self, engine: Any, *, action: str, record: ClaimRecord | None,
                   candidate: ClaimCandidate, reason: str) -> None:
        """留痕：注入式台账只写日志（不碰任何库），否则落 ``claim_ledger_audit``。"""
        if self._injected:
            log.info("claim_ledger_audit_memory", action=str(action),
                     field=str(candidate.field_key), reason=str(reason))
            return
        _audit(engine or self._meta_repo, action=action, record=record,
               candidate=candidate, reason=reason)

    def _supersede_row(self, old_id: int, new_id: int, *, reason: str) -> bool:
        """注入式台账的更正链：只改内存里那一行的 ``superseded_by`` 三列（正文不动）。"""
        for index, row in enumerate(self._records):
            if int(row.id) != int(old_id) or not row.active:
                continue
            self._records[index] = replace(
                row, superseded_by=int(new_id), supersede_reason=str(reason),
                superseded_at=_stamp(None),
            )
            return True
        return False

    def _insert_memory(self, candidate: ClaimCandidate, *,
                       now: Any = None) -> int:
        """注入式台账的落库替身：往内存那份里追加一行（**零 IO**）。"""
        new_id = max((int(row.id) for row in self._records), default=0) + 1
        stamp = _stamp(now or candidate.observed_at)
        self._records.append(ClaimRecord(
            id=new_id,
            platform=str(candidate.platform or ""),
            account_id=str(candidate.account_id or ""),
            chat_id=str(candidate.source_chat_id or ""),
            person_key=str(candidate.person_key or ""),
            field_key=str(candidate.field_key or ""),
            value=str(candidate.value or ""),
            content_key_value=str(candidate.source_content_key or "")
            or content_key(candidate.evidence or candidate.value),
            authority=str(candidate.authority or Authority.SPECULATION.value),
            source_kind=_source_kind(str(candidate.authority)),
            source_actor_id=str(candidate.source_actor_id
                                or candidate.person_key or ""),
            source_chat_id=str(candidate.source_chat_id or ""),
            source_message_id=str(candidate.source_message_id or ""),
            source_claim_id=int(candidate.source_claim_id or 0),
            observed_at=stamp,
            created_at=stamp,
            source_text=str(candidate.source_text or "")[:SOURCE_TEXT_MAX],
            evidence_text=str(candidate.evidence or "")[:EVIDENCE_TEXT_MAX],
            evidence_start=_offset(candidate.evidence_start),
            evidence_end=_offset(candidate.evidence_end),
        ))
        return new_id

    def withdraw(self, *, chat_id: str, actor_id: str, reason: str = "",
                 evidence: str = "", engine: Any = None,
                 now: Any = None) -> tuple[int, int]:
        """撤回说话人的 claim，**并把它下游的 claim 一并作废**（更正历史硬条款）。

        返回 ``(直接撤回数, 下游作废数)``。

        三条口径（顺序有意，都是"宁多撤不漏撤"——静默忽略正是要修的那个缺口）：

        1. **直接**：仍有效的行里 ``source_actor_id`` 是这个人的（自报时 actor 就是本人，
           见 :meth:`_insert` 的兜底）；
        2. **被同一人更新掉的旧行**：``supersede_reason`` 指向本会话里的这个说话人
           （"他改口了"——旧说法随新说法一起作废，不静默留着）；
        3. **下游**：``source_claim_id`` 指向被撤 claim 的那些行，**递归**（链式撤回）。

        事故对应：21:44:57 P6撤回「我瞎说的」→ 她 21:44:20 那条转述登记
        （``source_claim_id`` = P6那条）**随之作废**，而 21:44:59 P3 的自报
        与前两步无关，照旧有效。
        """
        repo = engine or self._meta_repo
        rows = self.for_chat(chat_id)
        actor = str(actor_id or "")
        # 直接撤回目标 = ①这个人仍有效的行 + ②被这个人"改口"更新掉的旧行。
        mine = [row for row in rows
                if row.active and self._is_actor(row, actor)]
        replaced = {int(row.id) for row in rows
                    if not row.active
                    and self._superseded_by_actor(row, rows, actor)}
        stamp = _stamp(now)
        direct = 0
        for row in mine:
            if _mark_withdrawn(repo, int(row.id), reason=reason or "retracted",
                               evidence=evidence, now=stamp):
                direct += 1
        # 被这个说话人"改口"更新掉的旧行：一起作废（新说法撤了，旧说法不能复活成
        # "从没被改过"的样子）。
        for claim_id in sorted(replaced):
            if _mark_withdrawn(repo, claim_id, reason="replaced_then_retracted",
                               evidence=evidence, now=stamp):
                direct += 1
        upstream = {int(row.id) for row in mine} | replaced
        downstream = 0
        changed = True
        while changed:                       # 递归：下游的下游也要作废（链式撤回）
            changed = False
            for row in self.for_chat(chat_id):
                if not row.active or int(row.source_claim_id or 0) not in upstream:
                    continue
                if _mark_withdrawn(repo, int(row.id),
                                   reason="source_withdrawn", evidence=evidence,
                                   now=stamp):
                    upstream.add(int(row.id))
                    downstream += 1
                    changed = True
        log.info("claim_ledger_withdrawn", chat=chat_id, actor=actor,
                 direct=direct, downstream=downstream, reason=reason)
        return direct, downstream

    def _is_actor(self, row: ClaimRecord, actor_id: str) -> bool:
        """这一行是不是"这个人说的"（来源人；自报行的来源人就是本人）。"""
        actor = str(actor_id or "")
        if not actor:
            return False
        return actor in (str(row.source_actor_id or ""),)

    def _superseded_by_actor(self, row: ClaimRecord, rows: Sequence[ClaimRecord],
                             actor_id: str) -> bool:
        """``row`` 是不是**正在撤回的这个说话人**自己写下、但已被更新掉的那条。

        判据 = 这条的 ``source_actor_id`` 就是 ``actor_id``（**是他说的话**），且它
        已经被后来的行更新掉（``superseded_by`` 非 0）。

        为什么必须把它一起撤：他改口后又说"我瞎说的"，那么"他最初那句话"不能复活
        成一条有效 claim —— 让人以为它从没被改过就是**静默改写**。判据只看"这条是
        谁写的"，不看"谁把它更新掉的"：她的登记（SELF 写）不该因为别人撤回而消失。
        """
        if not row.superseded_by:
            return False
        return self._is_actor(row, actor_id)

    # ---- 内部：落库 -------------------------------------------------------
    def _insert(self, candidate: ClaimCandidate, *, engine: Any,
                now: Any) -> int:
        if self._injected:
            # 注入式台账：**只往内存里追加**（读是它、写也是它，零 IO）。
            return self._insert_memory(candidate, now=now)
        try:
            from sqlalchemy import text as _sql

            repo = engine or self._meta_repo
            if repo is None:
                from core.terminal.store_port import get_meta_repo

                repo = get_meta_repo()
            stamp = _stamp(now or candidate.observed_at)
            with repo.engine.begin() as conn:
                result = conn.execute(_sql(
                    "insert into claim_ledger ("
                    " platform, account_id, chat_id, person_key, field_key, value,"
                    " content_key, authority, source_kind, source_actor_id,"
                    " source_chat_id, source_message_id, source_claim_id,"
                    " observed_at, created_at, source_text, evidence_text,"
                    " evidence_start, evidence_end) values ("
                    " :platform, :account_id, :chat_id, :person_key, :field_key, :value,"
                    " :content_key, :authority, :source_kind, :source_actor_id,"
                    " :source_chat_id, :source_message_id, :source_claim_id,"
                    " :observed_at, :created_at, :source_text, :evidence_text,"
                    " :evidence_start, :evidence_end)"
                ), {
                    "platform": str(getattr(candidate, "platform", "") or ""),
                    "account_id": str(getattr(candidate, "account_id", "") or ""),
                    "chat_id": str(candidate.source_chat_id or ""),
                    "person_key": str(candidate.person_key or ""),
                    "field_key": str(candidate.field_key or ""),
                    "value": str(candidate.value or ""),
                    "content_key": str(candidate.source_content_key or "")
                    or content_key(candidate.evidence or candidate.value),
                    "authority": str(candidate.authority
                                     or Authority.SPECULATION.value),
                    "source_kind": _source_kind(str(candidate.authority)),
                    # 没给"谁说的"就按"这条 claim 说的人"算（自报时两者本来就是同一个）
                    # ——不这样，"P6撤回我瞎说的"就撤不到P6自己那条。
                    "source_actor_id": str(candidate.source_actor_id
                                            or candidate.person_key or ""),
                    "source_chat_id": str(candidate.source_chat_id or ""),
                    "source_message_id": str(candidate.source_message_id or ""),
                    "source_claim_id": int(candidate.source_claim_id or 0),
                    "observed_at": stamp,
                    "created_at": stamp,
                    # 原话/位置**随行落库**：字段抽取是可重建派生缓存（rebuild() 自检）。
                    "source_text": str(getattr(candidate, "source_text", "") or ""
                                       )[:SOURCE_TEXT_MAX],
                    "evidence_text": str(candidate.evidence or ""
                                        )[:EVIDENCE_TEXT_MAX],
                    "evidence_start": _offset(candidate.evidence_start),
                    "evidence_end": _offset(candidate.evidence_end),
                })
                return int(result.lastrowid or 0)
        except Exception as exc:  # noqa: BLE001 - 写失败必须被看见（绝不静默丢写）
            log.warning("claim_ledger_write_failed", error=str(exc))
            raise


def _source_kind(authority: str) -> str:
    return {
        Authority.SELF_REPORT.value: "self",
        Authority.RELAY.value: "relay",
        Authority.SPECULATION.value: "guess",
    }.get(str(authority or "").lower(), "guess")


def _mark_superseded(repo: Any, old_id: int, new_id: int, *, reason: str,
                     now: Any = None) -> bool:
    """旧行只写更正链三列（``superseded_by`` / ``supersede_reason`` / ``superseded_at``）。"""
    try:
        from sqlalchemy import text as _sql

        if repo is None:
            from core.terminal.store_port import get_meta_repo

            repo = get_meta_repo()
        with repo.engine.begin() as conn:
            result = conn.execute(_sql(
                "update claim_ledger set superseded_by = :new,"
                " supersede_reason = :reason, superseded_at = :now"
                " where id = :old and superseded_by = 0"
                " and (withdrawn_at is null or withdrawn_at = '')"
            ), {"new": int(new_id), "reason": str(reason or ""),
                "now": _stamp(now), "old": int(old_id)})
            return bool(result.rowcount)
    except Exception as exc:  # noqa: BLE001
        log.warning("claim_ledger_supersede_failed", error=str(exc), old=old_id)
        return False


def _mark_withdrawn(repo: Any, claim_id: int, *, reason: str, evidence: str,
                    now: Any = None) -> bool:
    """撤回：只写 ``withdrawn_at`` / ``withdrawn_reason`` / ``withdrawn_evidence``。"""
    try:
        from sqlalchemy import text as _sql

        if repo is None:
            from core.terminal.store_port import get_meta_repo

            repo = get_meta_repo()
        with repo.engine.begin() as conn:
            result = conn.execute(_sql(
                "update claim_ledger set withdrawn_at = :now,"
                " withdrawn_reason = :reason, withdrawn_evidence = :evidence"
                " where id = :cid and (withdrawn_at is null or withdrawn_at = '')"
            ), {"now": _stamp(now), "reason": str(reason or ""),
                "evidence": str(evidence or ""), "cid": int(claim_id)})
            return bool(result.rowcount)
    except Exception as exc:  # noqa: BLE001
        log.warning("claim_ledger_withdraw_failed", error=str(exc), id=claim_id)
        return False


def _audit(repo: Any, *, action: str, record: ClaimRecord | None,
           candidate: ClaimCandidate, reason: str) -> None:
    """拒绝/重复/覆盖都留一行审计（**拒绝不是静默**——这正是"不得静默改写"的边界）。"""
    if not ledger_tracing():
        return
    try:
        from sqlalchemy import text as _sql

        if repo is None:
            from core.terminal.store_port import get_meta_repo

            repo = get_meta_repo()
        with repo.engine.begin() as conn:
            conn.execute(_sql(
                "insert into claim_ledger_audit ("
                " action, chat_id, person_key, field_key, value, authority,"
                " existing_claim_id, existing_authority, reason, created_at)"
                " values (:action, :chat_id, :person_key, :field_key, :value,"
                " :authority, :existing_claim_id, :existing_authority, :reason, :now)"
            ), {
                "action": str(action),
                "chat_id": str(candidate.source_chat_id or ""),
                "person_key": str(candidate.person_key or ""),
                "field_key": str(candidate.field_key or ""),
                "value": str(candidate.value or ""),
                "authority": str(candidate.authority or ""),
                "existing_claim_id": int(record.id) if record else 0,
                "existing_authority": str(record.authority) if record else "",
                "reason": str(reason or ""),
                "now": _stamp(None),
            })
    except Exception as exc:  # noqa: BLE001 - 审计写不进去不许影响判定
        log.warning("claim_ledger_audit_failed", error=str(exc), action=action)


def _record_from_row(row: dict) -> ClaimRecord:
    return ClaimRecord(
        id=int(row.get("id") or 0),
        platform=str(row.get("platform") or ""),
        account_id=str(row.get("account_id") or ""),
        chat_id=str(row.get("chat_id") or ""),
        person_key=str(row.get("person_key") or ""),
        field_key=str(row.get("field_key") or ""),
        value=str(row.get("value") or ""),
        content_key_value=str(row.get("content_key") or ""),
        authority=str(row.get("authority") or Authority.SPECULATION.value),
        source_kind=str(row.get("source_kind") or ""),
        source_actor_id=str(row.get("source_actor_id") or "") or str(
            row.get("person_key") or ""),
        source_chat_id=str(row.get("source_chat_id") or ""),
        source_message_id=str(row.get("source_message_id") or ""),
        source_claim_id=int(row.get("source_claim_id") or 0),
        observed_at=str(row.get("observed_at") or ""),
        superseded_by=int(row.get("superseded_by") or 0),
        supersede_reason=str(row.get("supersede_reason") or ""),
        superseded_at=str(row.get("superseded_at") or ""),
        withdrawn_at=str(row.get("withdrawn_at") or ""),
        created_at=str(row.get("created_at") or ""),
        source_text=str(row.get("source_text") or ""),
        evidence_text=str(row.get("evidence_text") or ""),
        evidence_start=_offset(row.get("evidence_start")),
        evidence_end=_offset(row.get("evidence_end")),
    )


# ---------------------------------------------------------------------------
# 闸门接缝（**唯一 IO 点**；判定本身仍在 guards 里，纯函数）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ClaimAssertion:
    """本回合文本里的**人-事实断言**（待与台账比对的那一半）。"""

    field_key: str
    value: str
    evidence: str = ""
    person_key: str = ""
    #: 断言在**本回合原文**里的半开区间（``None`` = 没记到；加法式，老调用不受影响）。
    span: tuple[int, int] | None = None


@dataclass(frozen=True)
class ClaimVerdict:
    """一条断言的台账结论（**可拦**的那一半）。"""

    assertion: ClaimAssertion
    supported: bool = False
    resolution: ClaimResolution | None = None
    reason: str = ""

    @property
    def field_key(self) -> str:
        return self.assertion.field_key


@dataclass
class ClaimLedgerInputs:
    """交给闸门的台账输入（**纯数据**；闸门自己不查库，IO 只在本模块）。"""

    enabled: bool = False
    enforced: bool = False
    recent_keys: tuple[str, ...] = ()
    assertions: tuple[ClaimAssertion, ...] = ()
    verdicts: tuple[ClaimVerdict, ...] = ()
    content_key_value: str = ""
    notes: tuple[str, ...] = field(default=())

    @property
    def unsupported(self) -> tuple[ClaimVerdict, ...]:
        return tuple(item for item in self.verdicts if not item.supported)


def assertion_hits(text: str) -> list[ClaimAssertion]:
    """本回合文本里**结构上可校验**的人-事实断言（字段词表内的那些）。

    与 ``guards`` 的两条形态检测合起来构成 `unfounded_claim` 的完整判据：
    形态检测（有限）负责"这句话像不像在说一件事实"，台账负责"这件事实有没有来源"。

    命中口径**与写侧共用** :func:`_field_matches_with_spans`：闸门判的字段与台账登记的
    字段必然是同一批——两处各写一份就是"判的是一件事、记的是另一件事"。
    """
    return [ClaimAssertion(field_key=field_key, value=value, evidence=snippet,
                           span=(int(start), int(end)))
            for field_key, value, snippet, start, end
            in _field_matches_with_spans(text)]


def evaluate_claims(
    text: str, *, chat_id: str, person_keys: Iterable[str] = (),
    ledger: ClaimLedger | None = None,
    recent_keys: Iterable[str] = (),
    content_key_value: str = "",
) -> ClaimLedgerInputs:
    """**唯一接缝**：读台账，给出本回合的 claim 判据（IO 只在这里）。

    ``enabled=False``（flag ``off``）时**一行都不走**：不建台账对象、不查库、
    ``recent_keys`` 照原样透传（复述读数仍由 M2-1 那一份给）。
    """
    if not ledger_tracing():
        return ClaimLedgerInputs(
            enabled=False, enforced=False,
            recent_keys=tuple(recent_keys or ()),
            content_key_value=str(content_key_value or ""),
        )
    claims = ledger if ledger is not None else ClaimLedger()
    assertions = assertion_hits(text)
    people = [str(item or "") for item in (person_keys or ()) if str(item or "")]
    verdicts: list[ClaimVerdict] = []
    for assertion in assertions:
        target = assertion.person_key or (people[0] if people else "")
        if not target:
            verdicts.append(ClaimVerdict(
                assertion=assertion, supported=False,
                reason="没有可判定的人（归属未知，按无支撑处理）"))
            continue
        resolution = claims.resolve(str(chat_id or ""), target, assertion.field_key)
        if resolution.has_claim and resolution.supports(assertion.value):
            verdicts.append(ClaimVerdict(assertion=assertion, supported=True,
                                         resolution=resolution,
                                         reason=f"有 {resolution.authority} 支撑"))
        elif resolution.has_claim:
            verdicts.append(ClaimVerdict(
                assertion=assertion, supported=False, resolution=resolution,
                reason=(f"与 {resolution.authority}「{resolution.value}」冲突")))
        else:
            verdicts.append(ClaimVerdict(
                assertion=assertion, supported=False, resolution=resolution,
                reason="本回合没有任何 claim 支撑这个字段"))
    key = str(content_key_value or content_key(text))
    window = tuple(str(item or "") for item in (recent_keys or ()))
    return ClaimLedgerInputs(
        enabled=True, enforced=ledger_enforcing(), recent_keys=window,
        verdicts=tuple(verdicts), content_key_value=key,
    )
