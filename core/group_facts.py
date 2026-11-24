"""媒体/开关的判据（从退役的 group_state 抽出来的那一小块）。

2026-10-24：`core/group_state.py`（2892 行旧群聊状态机）整块退役搬进 `legacy_brain/`。
但它里面还有 5 个**真在用**的小东西——媒体种类判据、占位符表、两个开关读数——
采集侧（media_store）、事件库（event_store_db）、调度器（pat_grace）都在调。
所以这些留在 core/ 里，只搬走状态机本体。默认值全部不变。
"""
from __future__ import annotations

import re
from safelog import warn

#: 内容形式里的**媒体**（规格 §3.2 表 B 的 C2–C5：图 / 表情（贴纸）/ 语音 / 文件；
#: ``video`` 在微信侧与文件同一渲染，一并算）。P1b（2026-10-06）把它们接进**同一个**
#: 单元：媒体是**内容**（§3.1），与文字 / 拍 / ``@`` / 引用同在一个单元、同一套封口
#: 四条；窗口仍**只有一条** Z1（:meth:`SemanticUnit.silence_window_seconds`），只是
#: "媒体已到、话还没说"的那一段读专用窗口（:func:`media_followup_seconds`，见下）。
#:
#: **媒体不能让单元提前成熟**（P1b 的关键判据，见 :meth:`SemanticUnit.seal_reason`）：
#: 一张图 / 一个表情包**不能替他把话说出来**，所以它不满足 Z4 的"有话可说"。否则
#: 「图 + 拍 + 一张要她认人的话」会在图那一格就封口，后面的文字另成一个回合——
#: 那正是 G3（"一件事被拆成两件"）的病，也正是主人 2026-10-01 撞到的现场。
MEDIA_KIND_IMAGE = "image"
MEDIA_KIND_STICKER = "sticker"
MEDIA_KIND_VOICE = "voice"
MEDIA_KIND_FILE = "file"
MEDIA_KIND_VIDEO = "video"
MEDIA_KINDS = (
    MEDIA_KIND_IMAGE, MEDIA_KIND_STICKER, MEDIA_KIND_VOICE,
    MEDIA_KIND_FILE, MEDIA_KIND_VIDEO,
)

#: 采集侧渲染出来的媒体**占位符** → 媒体种类（结构化前缀，与
#: ``GroupState.NON_TEXT_PREFIXES`` / ``core.conversation.media.is_media_message``
#: 同一口径）。它判的是"这条消息是不是媒体"，**不是**"他在不在叫她"——呼唤形式
#: 仍然只由已有解析器给（规格 §8 第 5 条）。
MEDIA_PLACEHOLDER_KINDS: tuple[tuple[str, str], ...] = (
    ("[图片]", MEDIA_KIND_IMAGE),
    ("[表情包]", MEDIA_KIND_STICKER),
    ("[表情包:", MEDIA_KIND_STICKER),
    ("[语音]", MEDIA_KIND_VOICE),
    ("[文件]", MEDIA_KIND_FILE),
    ("[视频]", MEDIA_KIND_VIDEO),
)

#: 媒体占位符整段（含 ``[图片: 描述]`` 那种带描述的渲染）——判"有没有**可判文本**"
#: 时整段剥掉：描述是媒体自己的渲染，不是他打出来的字。
_MEDIA_MARKER_RE = re.compile(r"\[(图片|表情包|语音|文件|视频)[^\]]*\]")

#: 媒体种类 → 采集侧那套占位符（内容字符串为空时用它兜：媒体成员在单元里**永远
#: 有一个非空的 ``media_ref``，渲染与叙述不会因为"文件还没解出来"而丢掉这条信号）。
_MEDIA_KIND_MARKERS: dict[str, str] = {
    MEDIA_KIND_IMAGE: "[图片]",
    MEDIA_KIND_STICKER: "[表情包]",
    MEDIA_KIND_VOICE: "[语音]",
    MEDIA_KIND_FILE: "[文件]",
    MEDIA_KIND_VIDEO: "[视频]",
}


def semantic_unit_enabled() -> bool:
    """``YINYUE_FEATURE_SEMANTIC_UNIT``：默认关（三态读法见 ``core.features.mode``）。

    关 = **一行都不走**装配器，coalesce + 拍宽限两条老路原样（回滚位，逐字节一致）。
    """
    try:
        from core import features

        return features.mode(features.SEMANTIC_UNIT, features.MODE_OFF) != features.MODE_OFF
    except Exception:  # noqa: BLE001 - 读不到开关=不改变现状
        warn("core/group_facts.py:69 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def unit_media_enabled() -> bool:
    """``YINYUE_FEATURE_UNIT_MEDIA``：媒体进单元 + ``pat→media`` 登记退役（默认关）。

    **前置条件是 ``SEMANTIC_UNIT`` 也开着**：媒体成员只存在于装配器里，没有装配器
    就没有这条路径的 owner，那时退役旧登记会直接造成**覆盖缺口**。所以读法是
    "``SEMANTIC_UNIT`` **且** ``UNIT_MEDIA``"——两个都关（默认）时为假，与接线前
    **逐字节一致**（回滚位）。三态读法见 ``core.features.mode``。
    """
    if not semantic_unit_enabled():
        return False
    try:
        from core import features

        return features.mode(features.UNIT_MEDIA, features.MODE_OFF) != features.MODE_OFF
    except Exception:  # noqa: BLE001 - 读不到开关=不改变现状
        warn("core/group_facts.py:87 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def pat_grace_enabled() -> bool:
    """``YINYUE_FEATURE_PAT_GRACE``：默认关（三态读法见 core.features.mode）。"""
    try:
        from core import features

        return features.mode(features.PAT_GRACE, features.MODE_OFF) != features.MODE_OFF
    except Exception:  # noqa: BLE001 - 读不到开关=不改变现状
        warn("core/group_facts.py:97 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def media_kind_of(content: str, msg_kind: str = "") -> str:
    """这条消息的媒体种类（``""`` = 不是媒体）。

    优先读采集侧给的 ``msg_type``（``image`` / ``sticker`` / ``voice`` / ``file`` /
    ``video``；``emoji`` 是表情通道的历史别名）；它缺失或对不上时退回**占位符**判据
    （``[图片]`` / ``[表情包: …]`` …）——与 ``core.conversation.media.is_media_message``
    同一口径：**文件还没解出来**（``media_path`` 为空）也算媒体。
    """
    kind = str(msg_kind or "").strip().lower()
    if kind in MEDIA_KINDS:
        return kind
    if kind == "emoji":
        return MEDIA_KIND_STICKER
    text = str(content or "").strip()
    for prefix, mapped in MEDIA_PLACEHOLDER_KINDS:
        if text.startswith(prefix):
            return mapped
    return ""


def media_store_row_ref(content: str, msg_kind: str = "") -> str:
    """媒体件在 ``message_media`` 里的 kind（image|file|video|voice|emoji）。

    采集侧判据（``media_kind_of``）用 ``sticker``，表里的历史别名是 ``emoji``；
    这个薄映射让两侧口径只在这里对一次（task-26）。
    """
    kind = media_kind_of(content, msg_kind)
    return "emoji" if kind == MEDIA_KIND_STICKER else kind
