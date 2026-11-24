"""鲸鲸反应清单（react_playbook）：数据文件只读加载 + 渲染。

一期定位：内容=数据（``configs/speech_seed/react_playbook.json``），本模块只读、无副作用，
不接生产行为；接线/影子/切换见 ``docs/鲸鲸反应清单_接线设计_最解耦版.md``。

分工：判局与选反应由模型做；这里只把数据渲染成文本块，按调用方给定的场景 id / 场景类型
（private / group）过滤（纯确定性，不做语义判断）；清单条目只写"反应类型"，不放示范句。

WP-2A（2026-10-08，语气线归位）起，本文件同时承载**选择层的唯一数据源**：
* ``scenes``：16 幕（signals / stance / options / forbidden / 生效场景）；
* ``positions``：8 条「群聊位置 → 默认姿态」映射（原 ``core/context/stance.py`` 的代码常量迁入）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from safelog import warn

ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = ROOT / "configs" / "speech_seed" / "react_playbook.json"
BLOCK_HEADER = "[遇事怎么反应]"
_DEFAULT_SCENES = ("private", "group")


@dataclass(frozen=True)
class PlaybookScene:
    """一条场景：遇到什么 → 可选怎么反应 / 明确不要什么。"""

    id: str = ""
    name: str = ""
    scenes: tuple[str, ...] = _DEFAULT_SCENES
    stance: str = ""
    signals: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    enabled: bool = True


@dataclass(frozen=True)
class PositionStance:
    """一条「位置 → 默认姿态」映射（原 stance.py 代码常量，WP-2A 迁入数据）。"""

    position: str = ""
    stance: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ReactPlaybook:
    """整份清单（只读视图）。"""

    version: str = ""
    usage: tuple[str, ...] = ()
    common: tuple[str, ...] = ()
    scenes: tuple[PlaybookScene, ...] = ()
    positions: tuple[PositionStance, ...] = ()

    def enabled_scenes(self) -> tuple[PlaybookScene, ...]:
        return tuple(scene for scene in self.scenes if scene.enabled)

    def scene(self, scene_id: str) -> PlaybookScene | None:
        for scene in self.scenes:
            if scene.id == scene_id:
                return scene
        return None


def _as_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item or "").strip())


def load_playbook(path: str | Path | None = None) -> ReactPlaybook:
    """读取清单数据；文件缺失/损坏时返回空清单（不抛错、不阻断调用方）。"""
    target = Path(path) if path else SEED_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 只读数据，坏了就当没有
        warn("core/speech/react_playbook.py:82 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ReactPlaybook()
    if not isinstance(raw, dict):
        return ReactPlaybook()
    scenes: list[PlaybookScene] = []
    for item in raw.get("scenes") or []:
        if not isinstance(item, dict):
            continue
        scenes.append(
            PlaybookScene(
                id=str(item.get("id") or "").strip(),
                name=str(item.get("name") or "").strip(),
                scenes=_as_tuple(item.get("scenes")) or _DEFAULT_SCENES,
                stance=str(item.get("stance") or "").strip(),
                signals=_as_tuple(item.get("signals")),
                options=_as_tuple(item.get("options")),
                forbidden=_as_tuple(item.get("forbidden")),
                enabled=bool(item.get("enabled", True)),
            )
        )
    positions: list[PositionStance] = []
    raw_positions = raw.get("positions") or {}
    if isinstance(raw_positions, dict):
        for key, value in raw_positions.items():
            if not isinstance(value, dict):
                continue
            positions.append(
                PositionStance(
                    position=str(key).strip(),
                    stance=str(value.get("stance") or "").strip(),
                    reason=str(value.get("reason") or "").strip(),
                )
            )
    return ReactPlaybook(
        version=str(raw.get("version") or ""),
        usage=_as_tuple(raw.get("usage")),
        common=_as_tuple(raw.get("common")),
        scenes=tuple(scene for scene in scenes if scene.id and scene.name),
        positions=tuple(item for item in positions if item.position and item.stance),
    )


def position_map(playbook: ReactPlaybook | None = None) -> dict[str, tuple[str, str]]:
    """位置 → ``(默认姿态, 白话理由)``；数据缺失时返回空 dict（调用方按"没有姿态"处理）。"""
    book = playbook if playbook is not None else load_playbook()
    return {item.position: (item.stance, item.reason) for item in book.positions}


def render_scene(scene: PlaybookScene) -> str:
    """渲染单条场景（信号 / 可选 / 不要）。"""
    lines = [f"【{scene.name}】"]
    if scene.signals:
        lines.append("- 信号：" + "；".join(scene.signals))
    if scene.options:
        lines.append("- 可选：" + "；".join(scene.options))
    if scene.forbidden:
        lines.append("- 不要：" + "；".join(scene.forbidden))
    return "\n".join(lines)


def render_playbook_block(
    playbook: ReactPlaybook | None = None,
    *,
    scene_ids: list[str] | tuple[str, ...] | None = None,
    audience: str = "",
) -> str:
    """渲染成给模型的文本块；可按场景 id / 生效场景（``private`` / ``group``）过滤。

    过滤是纯确定性匹配，不做语义判断；``audience`` 为空 = 不过滤（与接线前逐字节一致，
    WP-2A 的等价判据）。无内容时返回 ""（调用方按"没有这个块"处理）。
    """
    book = playbook if playbook is not None else load_playbook()
    scenes = list(book.enabled_scenes())
    if scene_ids is not None:
        wanted = {str(item) for item in scene_ids}
        scenes = [scene for scene in scenes if scene.id in wanted]
    if audience:
        wanted_audience = str(audience)
        scenes = [
            scene
            for scene in scenes
            if wanted_audience in (scene.scenes or _DEFAULT_SCENES)
        ]
    if not scenes and not book.common:
        return ""
    lines = [BLOCK_HEADER]
    if book.usage:
        lines.append("用法：" + "；".join(book.usage) + "。")
    if book.common:
        lines.append("通用：")
        lines.extend(f"- {line}" for line in book.common)
    for scene in scenes:
        lines.append(render_scene(scene))
    return "\n".join(lines)
