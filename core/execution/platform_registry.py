from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class Sense:
    """一条能力：这个平台，她能看见什么 / 能动手做什么。

    为什么不是两个布尔
    ------------------
    `perceive: bool, act: bool` 一旦这么写，以后「能看图但不能看视频」就得重构。
    这里一格一条，两轴只是**显示时的聚合视图**（`core/infra/senses.py`）。

    两条轴的差别决定了怎么知道它坏了
    --------------------------------
    - `axis="act"`（动得了手）：坏了是响亮的 —— 抛错、被拒、没有回执，看回执就行。
    - `axis="perceive"`（看得见）：坏了是静默的 —— 和「没人说话」长得一模一样，
      所以必须由「这条通道自己的心跳」来证明（`proof="beat"`）。
    """

    key: str                       # read_chat / send_image / like_post …
    axis: str                      # "perceive" | "act"
    says: str                      # 人话：能看见聊天内容
    proof: str                     # "beat"（感知，要心跳）| "ack"（操作，要回执）
    source: str = ""               # proof="beat" 时指向具体数据源；ack 留空
    beat_ms: int | None = None     # 证据多久不新鲜就算它停了（只有 beat 才有）

    def __post_init__(self) -> None:
        if self.axis not in ("perceive", "act"):
            raise ValueError("Sense.axis 只能是 perceive/act，收到 %r" % (self.axis,))
        if self.proof not in ("beat", "ack"):
            raise ValueError("Sense.proof 只能是 beat/ack，收到 %r" % (self.proof,))
        if self.proof == "beat":
            if not self.source:
                raise ValueError("感知类能力必须写 source（证据从哪来）：%s" % self.key)
            if not (self.beat_ms and self.beat_ms > 0):
                raise ValueError("感知类能力必须写 beat_ms：%s" % self.key)
        elif self.beat_ms is not None:
            raise ValueError("操作类能力不该有 beat_ms（够不够快看回执，不看小时钟）：%s" % self.key)
        if "_" in self.says or not self.says:
            raise ValueError("Sense.says 必须是给人看的话（别写下划线/别空着）：%r" % (self.says,))


@dataclass(frozen=True)
class PlatformDef:
    key: str
    display: str
    source: str
    send_extra_cls: type[BaseModel] | None = None
    action_extra_cls: type[BaseModel] | None = None
    # 这个平台她「应该有什么能力」。空 = 没接（明确写出来，不留空白）。
    senses: tuple[Sense, ...] = ()


class WechatSendExtra(BaseModel):
    msg_type_detail: str | None = None
    at_list: list[str] | None = None
    media_path: str | None = None


class WechatActionExtra(BaseModel):
    contact_wxid: str | None = None
    group_name: str | None = None


class BilibiliSendExtra(BaseModel):
    video_id: str | None = None
    comment_id: str | None = None
    danmaku_id: int | None = None
    dynamic_id: str | None = None


class BilibiliActionExtra(BaseModel):
    video_id: str | None = None
    action_type: str | None = None


class DouyinSendExtra(BaseModel):
    video_id: str | None = None
    comment_id: str | None = None
    live_room_id: str | None = None


class QQSendExtra(BaseModel):
    group_id: str | None = None
    at_list: list[str] | None = None


class TelegramSendExtra(BaseModel):
    reply_to_message_id: int | None = None
    parse_mode: str | None = None


_REGISTRY: dict[str, PlatformDef] = {}


def register_platform(defn: PlatformDef) -> None:
    _REGISTRY[defn.key] = defn


def get_platform(key: str) -> PlatformDef | None:
    return _REGISTRY.get(key)


def all_platforms() -> dict[str, PlatformDef]:
    return dict(_REGISTRY)


def platform_keys() -> list[str]:
    return list(_REGISTRY.keys())


def make_platform_enum() -> type[Enum]:
    members = {k.upper(): k for k in _REGISTRY}
    return Enum("Platform", members)


def make_source_enum() -> type[StrEnum]:
    members = {k.upper(): d.source for k, d in _REGISTRY.items()}
    return StrEnum("MessageSource", members)


def get_extra_cls(platform: str, log_type: str = "send") -> type[BaseModel] | None:
    d = _REGISTRY.get(platform)
    if not d:
        return None
    if log_type == "send":
        return d.send_extra_cls
    if log_type == "action":
        return d.action_extra_cls
    return None


# ---------------------------------------------------------------------------
# 她的两只手和一双眼睛（声明，不是给告警用的）
#
# 规矩：
#   1. 只声明**已经有实现**的能力。想接但没接的，senses 留空，别提前许诺。
#   2. 感知（perceive）要给 source + beat_ms：证明「这条通道还在工作」；
#      操作（act）只要回执，不给 beat —— 快慢是回执的事，不是小时钟的事。
#   3. 额度/权限不是新的一轴，挂在具体能力上（PIT-040 那套）。
# ---------------------------------------------------------------------------
WECHAT_SENSES: tuple[Sense, ...] = (
    # 临时口径：解密进程还没有自己的心跳，先用「解密库最新 mtime」顶替。
    # 真修法是让它像 QQ 那样写 liveness("wechat_decrypt")，然后这里改走心跳。
    Sense("read_chat", "perceive", "能看见聊天内容", "beat", "decrypted", 900_000),
    Sense("read_image", "perceive", "能看见发来的图和表情", "beat", "decrypted", 900_000),
    Sense("send_text", "act", "能打字回人", "ack"),
    Sense("send_image", "act", "能发图", "ack"),
    Sense("send_sticker", "act", "能发表情", "ack"),
    Sense("recall", "act", "能撤回自己发错的话", "ack"),
)

QQ_SENSES: tuple[Sense, ...] = (
    Sense("read_messages", "perceive", "能看见 QQ 消息和群消息", "beat", "peer_push", 60_000),
    Sense("send_text", "act", "能打字回人", "ack"),
    Sense("send_image", "act", "能发图", "ack"),
)

register_platform(PlatformDef(
    key="wechat", display="微信", source="wechat",
    send_extra_cls=WechatSendExtra, action_extra_cls=WechatActionExtra,
    senses=WECHAT_SENSES,
))
register_platform(PlatformDef(
    key="qq", display="QQ", source="qq",
    send_extra_cls=QQSendExtra, action_extra_cls=QQSendExtra,
    senses=QQ_SENSES,
))
register_platform(PlatformDef(
    key="telegram", display="Telegram", source="telegram",
    send_extra_cls=TelegramSendExtra, action_extra_cls=TelegramSendExtra,
    senses=(),  # 没接：明确留空，不提前许诺能力
))
register_platform(PlatformDef(
    key="discord", display="Discord", source="discord",
    senses=(),  # 没接：明确留空，不提前许诺能力
))
register_platform(PlatformDef(
    key="bilibili", display="B站", source="bilibili",
    send_extra_cls=BilibiliSendExtra, action_extra_cls=BilibiliActionExtra,
    senses=(),  # 没接：明确留空，不提前许诺能力
))
register_platform(PlatformDef(
    key="douyin", display="抖音", source="douyin",
    send_extra_cls=DouyinSendExtra, action_extra_cls=DouyinSendExtra,
    senses=(),  # 没接：明确留空，不提前许诺能力
))
register_platform(PlatformDef(
    key="dingtalk", display="钉钉", source="dingtalk",
    senses=(),  # 没接：明确留空，不提前许诺能力
))
register_platform(PlatformDef(
    key="feishu", display="飞书", source="feishu",
    senses=(),  # 没接：明确留空，不提前许诺能力
))
register_platform(PlatformDef(
    key="web", display="Web", source="web",
    senses=(),  # 没接：明确留空，不提前许诺能力
))
