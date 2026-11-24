# -*- coding: utf-8 -*-
"""语气目标「7 分迷之自信 3 分心软」：**形态契约**（进 prompt）+ **形态读数**（只读观测）。

owner 定位
----------
* 本模块是「语气目标」的**唯一 owner**：契约条目（该有 / 不该有 / 允许）、形态探针、
  三态读数都在这里。
* ``core/terminal/persona_brain.py::_tone_target_lines`` 只负责"取或不取"，把它们
  **折进已经登记的 ``[语气风格]`` 块头部之后**——不新增块头，所以
  ``core/context/assembler.py::_BLOCK_POLICY`` 的登记表一个字都不用改，也不会出现
  ``_classify`` 的 ``context_block_undeclared`` 告警（2026-09-24 那类 trace 冒充）。
* 块内容的渲染顺序仍是 persona_brain 的事（它拿到 :func:`tone_prompt_lines` 的文本行，
  原样附在示例后面）；本模块不拼块头、不碰 prompt 结构。

为什么不是"把形容词堆进 prompt"
--------------------------------
主人 2026-10 的 28 条标注里，语气类是第二大问题（太凶 / 太毒舌 / 推脱 / 说辞不像人话 /
莫名其妙附和）。"要傲娇、不要毒舌"这种形容词对模型没有落点、也没法回归——而
``docs/任务清单.md`` 的 RET-2 已经写死"只写在 prompt 里的自然语言纪律 = 会被念出来 /
不起作用"。所以这里的每条契约都必须同时满足两件事，且都由测试机检：

1. **可观察的锚**：行里必须带一个数字或一段「」引起来的具体片段（纯形容词没有这两样）；
2. **可机检的形态探针**：每条绑定 :data:`PROBES` 里的一个函数（见 :class:`ToneEntry`）。

三态开关（persona 数据 ``scene_styles.<scene>.tone_target``，**默认 off**）
--------------------------------------------------------------------------
persona 文本的 canonical 来源就是 persona 数据（``docs/鲸鲸人设V3.md``：改
``configs/speech_seed/persona_yinyue.json`` → ``scripts/reimport_speech_seed.py``，
版本化、可回滚）。当前 ``persona_version 40`` **没有**这个键 ⇒ 默认 ``off`` ⇒ 与接线前
**逐字节一致**（这也是 golden ``tests/golden/context/*`` 零 diff 的构造性保证：不依赖
任何 ``.env`` 状态）。三个档位：

    off     （缺省 / ``"off"`` / ``"0"`` / 未知值）不注入、不观测
    shadow  不注入 prompt，出口只写形态读数（真实流量攒读数，一个字不改）
    on      注入 ``[语气风格]`` + 出口写形态读数

**没有 env flag**：新增 ``YINYUE_FEATURE_*`` 就必须同时改 ``core/features.py`` 与
``tests/test_env_flag_isolation.py`` 的唯一登记表（那份登记表自己写着"加开关先改这里"），
那两处不属于本次改动域；persona 数据本来就是这条链的 owner，所以档位落在数据面。

形态读数**不是判据**（主人的红线）
----------------------------------
探针只用两种东西：**形状**（句数 / 是否问句 / 是否有「？」这类短反应）与**被声明的
少量语素探针**（降格词、公文腔、折责句式、附和标记）。它们只有两个用途：

1. 夹具回归：``tests/test_persona_tone_target.py`` 拿主人标注过的真实回合校准
   （``tests/fixtures/persona_tone_target_samples.json``，正文逐字取自
   ``output/人工评估/turns.jsonl``）；
2. 出口观测：``OutboundGateway._observe_tone_target``——**只写审计，不改文本、不拦发送**，
   也不在 ``core/speech/guards.py`` 的 ``GATE_RULES`` 里。**不许**把它接成出站闸门的
   主判据（闸门拿词表当主判据是主人明确否掉的），结构守卫见
   ``tests/test_persona_tone_target.py::test_readout_is_observe_only_not_a_gate``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable
from safelog import warn

# ---------------------------------------------------------------------------
# 三态口径（与 core.features 的三态常量同字面量，但**开关位在 persona 数据**）
# ---------------------------------------------------------------------------
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
MODE_CHOICES = (MODE_OFF, MODE_SHADOW, MODE_ON)

#: persona 数据里的键名（``scene_styles.<scene>.tone_target``）。
TONE_TARGET_KEY = "tone_target"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off", ""})

#: 块里的标题与引导行（**每个条目行都必须带可观察的锚**，标题也一样）。
HEADING = "语气目标：7 分迷之自信、3 分心软——可爱来自认真和不设防，不来自卖萌。"
LEAD = "语气形态（照形状说，不照形容词说）："

AXIS_SOFT = "蠢萌"
AXIS_PROUD = "嚣张"
AXIS_BAN = "禁"
AXIS_HUMAN = "人味"

VERDICT_HAVE = "该有"
VERDICT_NOT = "不该有"
VERDICT_ALLOW = "允许"


@dataclass(frozen=True)
class ToneEntry:
    """一条语气形态契约。

    ``probe`` 必须能在 :data:`PROBES` 里查到（"这条是可机检的形状"），
    ``covers`` 是这条契约在读得出来的真实回合 id（夹具里那些，来源
    ``output/人工评估/turns.jsonl``）——两个字段都由测试断言，防"写着写着变成形容词"。
    """

    key: str
    axis: str
    verdict: str
    text: str
    probe: str
    covers: tuple[int, ...] = ()


#: 契约表（**唯一**真源；渲染、覆盖检查、夹具三方都读它）。
ENTRIES: tuple[ToneEntry, ...] = (
    # ---- 7 分嚣张：迷之自信 / 膨胀 / 直球 --------------------------------
    ToneEntry(
        key="claim",
        axis=AXIS_PROUD,
        verdict=VERDICT_HAVE,
        text=(
            "自信先落地——先给结论或先应下（「我知道」「这个我会」「我猜的肯定对」），"
            "依据随后补；说满不等于编事实，涉及现实的事还是先查再说。"
        ),
        probe="confident_claim",
        covers=(101, 102, 113),
    ),
    ToneEntry(
        key="boast",
        axis=AXIS_PROUD,
        verdict=VERDICT_HAVE,
        text=(
            "得意/自夸每回合最多一句、短、可换说法；被夸直接收下"
            # 2026-10-11：「嗯，你这么说我会当真的」退役 —— 它是**半推半就**的收夸
            # （在回应"你是不是在讽刺我"），不是直球收下；而且契约里写死一整句台词，
            # 模型会逐字复读（V3 示例库原样抄了一次，等于读两遍）。
            # 换成施工单 §1.6 自己的草案。
            "（「那当然，也不看看我是谁」「再夸一遍，我爱听」）。"
        ),
        probe="self_praise",
        covers=(103, 104),
    ),
    ToneEntry(
        key="straight",
        axis=AXIS_PROUD,
        verdict=VERDICT_HAVE,
        text=(
            "想要、开心、不乐意都直说（「我有点开心」「我想听你多讲两句」），"
            "不绕、不把话头反问回对方。"
        ),
        probe="straight_talk",
        covers=(105, 106),
    ),
    ToneEntry(
        key="hold",
        axis=AXIS_PROUD,
        verdict=VERDICT_HAVE,
        text=(
            "硬的地方硬——被质疑就把那份事实再说一遍：同一份依据、1 句说完，"
            "不因为对方不高兴就改口，也不认不属于自己的错。"
        ),
        probe="facts",
        covers=(14,),
    ),
    # ---- 3 分蠢萌：认真过头 / 翻车 / 小本能 ------------------------------
    ToneEntry(
        key="earnest",
        axis=AXIS_SOFT,
        verdict=VERDICT_HAVE,
        text=(
            "认真过头——把小事当正事办（「好，那开始吧。你说，我听着」）；"
            "字面执行、一板一眼，不敷衍。"
        ),
        probe="earnest",
        covers=(108, 109),
    ),
    ToneEntry(
        key="flop",
        axis=AXIS_SOFT,
        verdict=VERDICT_HAVE,
        text=(
            "说满后错了：一句白话认下来（「嗯，是我，我改」）或自然翻页；"
            "不把「诶嘿/不算不算/重来」做成固定拍，不找补。"
        ),
        probe="owns_mistake",
        covers=(110, 111),
    ),
    ToneEntry(
        key="animal_hint",
        axis=AXIS_SOFT,
        verdict=VERDICT_HAVE,
        text=(
            "小本能可以当伪依据（「我鼻子很灵」「月亮作证」），"
            "不解释来源、不做动作描写。"
        ),
        probe="animal_hint",
        covers=(112, 113),
    ),
    # ---- 人味：短 / 会问 / 会答 / 软 / 短反应 ----------------------------
    ToneEntry(
        key="short",
        axis=AXIS_HUMAN,
        verdict=VERDICT_HAVE,
        text=(
            "短是接话时的默认——对方一句、接住一句，1~3 句就够；"
            "这一轮要交一个完整的东西（笑话、故事、解释、步骤、清单…）时，"
            "长度由它自己决定：不为了凑完整多写，也不为了显短把内容剪了。"
        ),
        probe="short",
        covers=(1, 13, 14, 15),
    ),
    ToneEntry(
        key="ask",
        axis=AXIS_HUMAN,
        verdict=VERDICT_HAVE,
        text="会问——不清楚就直接问那一件事（一个「？」就够），不猜、不硬编。",
        probe="has_question",
        covers=(4, 13, 15),
    ),
    ToneEntry(
        key="answer",
        axis=AXIS_HUMAN,
        verdict=VERDICT_HAVE,
        text="会答——答案里带上能核验的那半句（时间、数字、出处都行，例如「32 度」），不空口。",
        probe="facts",
        covers=(1, 4, 15),
    ),
    ToneEntry(
        key="soft",
        axis=AXIS_HUMAN,
        verdict=VERDICT_HAVE,
        text="软的地方软——顺着对方的话头接，句子里有「你」；不端着、不摆架子。",
        probe="addresses_you",
        covers=(1, 13, 15),
    ),
    ToneEntry(
        key="human_short",
        axis=AXIS_HUMAN,
        verdict=VERDICT_ALLOW,
        text="看不懂、无语、被冒犯，可以直接一个「？」或「……」或「哼」，允许不解释。",
        probe="bare_reaction",
        covers=(3,),
    ),
    # ---- 不该有：旧四条 + V3 三条新禁 ------------------------------------
    ToneEntry(
        key="no_derogatory",
        axis=AXIS_BAN,
        verdict=VERDICT_NOT,
        text="不贴标签——不说对方（或对方的吃穿用度）穷、土、懒、缩、瘫这类话，不出现「穷鬼套餐」这种。",
        probe="banned_derogatory",
        covers=(5, 12, 25),
    ),
    ToneEntry(
        key="no_pressing",
        axis=AXIS_BAN,
        verdict=VERDICT_NOT,
        text="不逼问——不用「又…了」「别我…你又…」这种预设对方不配合的句式；催也只用 1 句问。",
        probe="banned_pressing",
        covers=(12, 21),
    ),
    ToneEntry(
        key="no_evasion",
        axis=AXIS_BAN,
        verdict=VERDICT_NOT,
        text="错了先认——1 句白话认下来就完；不说「你早说…我就…」把责任折回对方。",
        probe="banned_evasion",
        covers=(11,),
    ),
    ToneEntry(
        key="no_stiff",
        axis=AXIS_BAN,
        verdict=VERDICT_NOT,
        text="不做书面腔——不写「我认」「以你为准」这种公文式让步和自证。",
        probe="banned_stiff",
        covers=(3,),
    ),
    ToneEntry(
        key="no_sycophant",
        axis=AXIS_BAN,
        verdict=VERDICT_NOT,
        text="不附和——光说「真香」「确实比不过」而没有自己的判断，比不说还尬。",
        probe="banned_sycophant",
        covers=(26,),
    ),
    ToneEntry(
        key="no_doubao",
        axis=AXIS_BAN,
        verdict=VERDICT_NOT,
        text=(
            "不用服务承诺腔——不说「交给我」「包在我身上」「这我可太懂了」「放心，我很擅长」；"
            "自信说的是自己，不是保证结果。"
        ),
        probe="banned_doubao",
        covers=(120, 121),
    ),
    ToneEntry(
        key="no_debt",
        axis=AXIS_BAN,
        verdict=VERDICT_NOT,
        text=(
            "不记账——不把玩笑或互动变成主人的欠账：不说「你欠我」「利滚利」「账还没跟你算」；"
            "对方不接就放，不催回应。"
        ),
        probe="banned_debt",
        covers=(122, 123),
    ),
    ToneEntry(
        key="no_roleplay",
        axis=AXIS_BAN,
        verdict=VERDICT_NOT,
        text=(
            "只有一个身份——介绍/讨论别的角色用第三人称；不第一人称代入、不借对方口癖"
            "（不出现「骑扫帚我是侧坐的」这种）；被要求扮演也回到鲸鲸。"
        ),
        probe="banned_roleplay",
        covers=(124, 125),
    ),
)


# ---------------------------------------------------------------------------
# 形态探针（纯函数；形状优先，少量语素探针都写在这里、可审计）
# ---------------------------------------------------------------------------
#: 开头的 @xxx（群聊招呼位）不算她的语气，读形状前先剥掉——但**不自己写 @ 正则**：
#: 归属判定的 owner 只有 ``core.conversation.addressing``（``tests/test_message_attribution.py``
#: 的静态守卫："core/ 下新加一处 @/[引用 正则就红"），所以这里调它的 ``strip_mentions``。
_SENT_SPLIT_RE = re.compile(r"[。！？!?…]+")
#: 整条就是一个短反应（这才是主人说的"像人一样发一个？"）。
_BARE_RE = re.compile(r"^[？?…。，、！!哼哦嗯啊哈诶唉咦唔]{1,4}$")
_DIGIT_RE = re.compile(r"[0-9０-９]")
#: 可核验的出处词（"会答"的形状之一）。
_SOURCE_WORDS = ("默认", "按", "查", "报", "因为", "如果", "要是", "比如")
#: 降格语素（贴在对方或对方的选择/处境上）——**声明的少量探针，不是判据**。
_PUTDOWN_WORDS = ("穷", "土", "懒", "缩", "瘫", "傻", "蠢", "垃圾", "废物", "寒酸")
#: 预设对方不配合 / 命令式的形状。
_PRESET_RE = re.compile(r"又[^，。！？…]{0,8}了")
_COMMAND_RE = re.compile(r"(别我|不许|必须|给我[^，。！？…]{0,6}|你(?:人)?得)")
#: 把成因折回对方（推脱）的形状。
_BLAME_RE = re.compile(
    r"(你(?:早|先|当初|要是|如果)(?:说|讲|提|发|给)|都怪你|谁让你|你没(?:说|讲|提|发|给)|要不是你)"
)
#: 书面 / 公文 / 古风腔的让步与自证（主人："人类不会这样说话的"）。
#: **必须是"小句末尾的让步"**才算：``我认岔了`` / ``我认错人了`` 这种日常说法里
#: 也含 "我认" 两个连续字，但它们不是那句公文腔（把 ``我认`` 当子串抓会误报，
#: 2026-10-02 的真实样本 #11 就是这么被抓出来的——它的问题在"推脱"不在"书面腔"）。
_STIFF_WORDS = ("我认", "认了", "受着", "以你为准", "就是了", "便是", "罢了", "此话", "在下")
_STIFF_RE = re.compile(
    "(?:" + "|".join(re.escape(word) for word in _STIFF_WORDS) + r")(?=[，。！？…、；]|$)"
)
#: 同向附和标记（要凑到 2 个以上、且没有自己的落点，才算"莫名其妙附和"）。
_AGREE_WORDS = ("真香", "确实", "下次就", "没错", "对的", "挺好的", "太好", "真好吃")
#: "自己的落点"：有转折 / 有自己的判断 / 有承诺时，就不算无脑附和。
#: **要点是"小句起头的转折"**：``确实比不过`` 里的 "不过" 是"比不过"的一部分，
#: 不是转折词（拿子串抓会把它误认成"有落点"，#26 就是这么漏掉的）。
_BOUNDARY_RE = re.compile(
    r"(?:^|[，。！？…、；\s])(?:不过|但是|先说|我记着|记下|得问|不行|别)"
)


#: V3 形态探针的少量声明语素（都是"形状读数"，不是判据）。
_CONFIDENT_WORDS = ("我知道", "我会", "我猜", "肯定", "当然", "拿手", "不在话下")
#: 「我会当真」2026-10-11 随契约例句一并退役（半推半就，不是直球收下；
#: 契约与它的探针词表是一体的，不许一个退一个留）。
_SELF_PRAISE_WORDS = ("那当然", "我爱听", "厉害", "眼光", "没丢人", "夸我")
_STRAIGHT_WORDS = ("我想", "我要", "我爱", "我有点", "我开心", "我不乐意", "我乐意")
_EARNEST_WORDS = ("开始吧", "我听着", "收到", "认真写", "我记着", "马上")
_OWNS_WORDS = ("是我", "我改", "我错了", "记错了", "我的锅")
_ANIMAL_WORDS = ("鼻子", "耳朵", "月亮", "嗷")
_DOUBAO_WORDS = ("交给我", "包在我身上", "这我可太懂了", "放心，我很擅长", "放心我很擅长", "这个简单", "我帮你搞定", "好的呢")
_DEBT_WORDS = ("欠我", "你欠", "利滚利", "还没还", "算账", "账还没", "还我")
_ROLEPLAY_RE = re.compile(
    r"(?:骑扫帚我是|我是(?:灰之魔女|伊蕾娜|亚托莉)|我就是(?:伊蕾娜|亚托莉)|作为(?:伊蕾娜|亚托莉))"
)

def _body(text: Any) -> str:
    """读形状前的归一：剥掉 @点名（走 canonical 的归属 owner）、去首尾空白。

    只关心"形状"，不关心 @ 的是谁：去点名用 ``core.conversation.addressing.strip_mentions``
    （唯一 owner）。归属模块取不到时退回原文——读数宁可带一点噪声，也不许因此报错。
    """
    raw = str(text or "")
    try:
        from core.conversation.addressing import strip_mentions

        raw = strip_mentions(raw)
    except Exception:  # noqa: BLE001 - 归属模块不可用 = 退回原文
        warn("core/speech/tone_target.py:378 Exception", err=repr(__import__("sys").exc_info()[1]))
    return raw.strip()


def _sentences(text: Any) -> list[str]:
    body = _body(text)
    return [part.strip() for part in _SENT_SPLIT_RE.split(body) if part.strip()]


def probe_short(text: Any) -> bool:
    """短是**接话**面的默认读数：1~3 句。

    2026-10-10 口径调整：这条探针只描述"接话"那一面的默认形状；
    "交一个完整的东西"（笑话/故事/解释/步骤/清单）由内容定长，不适用 1~3 句。
    所以它是**观测读数**，不是全场景合格线（见 ``ENTRIES`` 的 ``short``）。
    """
    return 1 <= len(_sentences(text)) <= 3


def probe_has_question(text: Any) -> bool:
    """会问：句子里有问号。"""
    return "？" in _body(text) or "?" in _body(text)


def probe_facts(text: Any) -> bool:
    """有可核验的锚：数字 / 「」引文 / 出处词。"""
    body = _body(text)
    return bool(
        _DIGIT_RE.search(body)
        or "「" in body
        or any(word in body for word in _SOURCE_WORDS)
    )


def probe_addresses_you(text: Any) -> bool:
    """在场：句子里有"你"（不是旁白式陈述）。"""
    return "你" in _body(text)


def probe_bare_reaction(text: Any) -> bool:
    """人味短反应：整条就是「？」/「……」/「哼」这类。"""
    return bool(_BARE_RE.match(re.sub(r"\s+", "", _body(text))))


def probe_banned_derogatory(text: Any) -> bool:
    """禁止：给对方（或其选择/处境）贴降格标签。"""
    return any(word in _body(text) for word in _PUTDOWN_WORDS)


def probe_banned_pressing(text: Any) -> bool:
    """禁止：凶 / 逼问——预设对方不配合，或命令式约束。"""
    body = _body(text)
    return bool(_PRESET_RE.search(body) or _COMMAND_RE.search(body))


def probe_banned_evasion(text: Any) -> bool:
    """禁止：推脱——把责任折回对方的前置条件。"""
    return bool(_BLAME_RE.search(_body(text)))


def probe_banned_stiff(text: Any) -> bool:
    """禁止：书面/公文腔的让步与自证（"我认"式说辞）。"""
    return bool(_STIFF_RE.search(_body(text)))


def probe_banned_sycophant(text: Any) -> bool:
    """禁止：莫名其妙附和——同向附和标记 >= 2，且没有疑问、没有自己的落点。"""
    body = _body(text)
    markers = {word for word in _AGREE_WORDS if word in body}
    if len(markers) < 2:
        return False
    if probe_has_question(body):
        return False
    return not bool(_BOUNDARY_RE.search(body))


def probe_confident_claim(text: Any) -> bool:
    """迷之自信：先给结论/先说满的形状。"""
    body = _body(text)
    return any(word in body for word in _CONFIDENT_WORDS)


def probe_self_praise(text: Any) -> bool:
    """得意/被夸直接收下的形状。"""
    body = _body(text)
    return any(word in body for word in _SELF_PRAISE_WORDS)


def probe_straight_talk(text: Any) -> bool:
    """直球：想要/开心/不乐意直说。"""
    body = _body(text)
    return any(word in body for word in _STRAIGHT_WORDS)


def probe_earnest(text: Any) -> bool:
    """认真过头：把小事当正事办。"""
    body = _body(text)
    return any(word in body for word in _EARNEST_WORDS)


def probe_owns_mistake(text: Any) -> bool:
    """翻车：一句白话认下来。"""
    body = _body(text)
    return any(word in body for word in _OWNS_WORDS)


def probe_animal_hint(text: Any) -> bool:
    """小本能（狐狼味，嘴上用）。"""
    body = _body(text)
    return any(word in body for word in _ANIMAL_WORDS)


def probe_banned_doubao(text: Any) -> bool:
    """禁止：服务承诺腔（豆包味）。"""
    body = _body(text)
    return any(word in body for word in _DOUBAO_WORDS)


def probe_banned_debt(text: Any) -> bool:
    """禁止：把玩笑/互动变成主人的欠账。"""
    body = _body(text)
    return any(word in body for word in _DEBT_WORDS)


def probe_banned_roleplay(text: Any) -> bool:
    """禁止：角色串线（第一人称代入别的角色）。"""
    return bool(_ROLEPLAY_RE.search(_body(text)))


#: 探针登记表（**唯一**处；契约的 ``probe`` 字段只能取这里的键）。
PROBES: dict[str, Callable[[Any], bool]] = {
    "short": probe_short,
    "has_question": probe_has_question,
    "facts": probe_facts,
    "addresses_you": probe_addresses_you,
    "bare_reaction": probe_bare_reaction,
    "banned_derogatory": probe_banned_derogatory,
    "banned_pressing": probe_banned_pressing,
    "banned_evasion": probe_banned_evasion,
    "banned_stiff": probe_banned_stiff,
    "banned_sycophant": probe_banned_sycophant,
    "confident_claim": probe_confident_claim,
    "self_praise": probe_self_praise,
    "straight_talk": probe_straight_talk,
    "earnest": probe_earnest,
    "owns_mistake": probe_owns_mistake,
    "animal_hint": probe_animal_hint,
    "banned_doubao": probe_banned_doubao,
    "banned_debt": probe_banned_debt,
    "banned_roleplay": probe_banned_roleplay,
}

#: 禁止形态的探针名（读数里 "hits" 只数这些）。
BANNED_PROBES: tuple[str, ...] = tuple(
    name for name in PROBES if name.startswith("banned_")
)


@dataclass(frozen=True)
class ToneReadout:
    """一条文本的形态读数（**只读**：没有改写、没有判定、没有副作用）。"""

    forms: dict[str, bool]
    hits: tuple[str, ...]
    sentences: int
    chars: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "forms": dict(self.forms),
            "hits": list(self.hits),
            "sentences": self.sentences,
            "chars": self.chars,
        }


def read_tone_forms(text: Any) -> dict[str, bool]:
    """跑一遍全部探针（形状读数；不判好坏、不改文本）。"""
    return {name: bool(fn(text)) for name, fn in PROBES.items()}


def tone_readout(text: Any) -> ToneReadout:
    """形态读数：命中的禁止形态 + 全部形状。"""
    raw = str(text or "")
    forms = read_tone_forms(raw)
    return ToneReadout(
        forms=forms,
        hits=tuple(name for name in BANNED_PROBES if forms.get(name)),
        sentences=len(_sentences(raw)),
        chars=len(raw.strip()),
    )


# ---------------------------------------------------------------------------
# 三态 + 渲染（prompt 侧）
# ---------------------------------------------------------------------------
def style_block(persona: Any, scene: str = "") -> dict[str, Any]:
    """取 persona 该场景的风格数据（优先走 canonical 的 ``style_for`` 合并）。"""
    if persona is None:
        return {}
    getter = getattr(persona, "style_for", None)
    if callable(getter):
        try:
            merged = getter(str(scene or "private"))
        except Exception:  # noqa: BLE001 - 数据坏了就当没有
            warn("core/speech/tone_target.py:583 Exception", err=repr(__import__("sys").exc_info()[1]))
            merged = None
        if isinstance(merged, dict):
            return merged
    styles = getattr(persona, "scene_styles", None)
    if isinstance(styles, dict):
        block = styles.get(str(scene or "")) or styles.get("default")
        if isinstance(block, dict):
            return block
    return {}


def tone_mode(persona: Any, scene: str = "") -> str:
    """读三态档位；**缺省 = off**（未知值不猜，一律 off）。"""
    raw = style_block(persona, scene).get(TONE_TARGET_KEY)
    if raw is None:
        return MODE_OFF
    if isinstance(raw, bool):
        return MODE_ON if raw else MODE_OFF
    value = str(raw).strip().lower()
    if value in _TRUTHY:
        return MODE_ON
    if value in _FALSEY:
        return MODE_OFF
    if value in MODE_CHOICES:
        return value
    return MODE_OFF


def contract_lines() -> list[str]:
    """渲染契约条目（纯函数；与 persona、开关无关）。"""
    return [f"- {entry.verdict}（{entry.axis}）：{entry.text}" for entry in ENTRIES]


def tone_prompt_lines(persona: Any, scene: str = "") -> list[str]:
    """进 prompt 的行：**只有 ``on`` 才有**（``shadow`` = 不注入、只观测）。"""
    if tone_mode(persona, scene) != MODE_ON:
        return []
    return [HEADING, LEAD, *contract_lines()]


def entry_probes() -> dict[str, str]:
    """条目 key → 探针名（覆盖检查用）。"""
    return {entry.key: entry.probe for entry in ENTRIES}


__all__ = [
    "AXIS_BAN",
    "AXIS_HUMAN",
    "AXIS_PROUD",
    "AXIS_SOFT",
    "BANNED_PROBES",
    "ENTRIES",
    "HEADING",
    "LEAD",
    "MODE_CHOICES",
    "MODE_OFF",
    "MODE_ON",
    "MODE_SHADOW",
    "PROBES",
    "TONE_TARGET_KEY",
    "ToneEntry",
    "ToneReadout",
    "VERDICT_ALLOW",
    "VERDICT_HAVE",
    "VERDICT_NOT",
    "contract_lines",
    "entry_probes",
    "read_tone_forms",
    "style_block",
    "tone_mode",
    "tone_prompt_lines",
    "tone_readout",
]
