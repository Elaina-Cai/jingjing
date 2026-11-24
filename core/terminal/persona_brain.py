"""Phase 3 PersonaBrain prompt boundary.

The legacy system prompt was assembled from many independent blocks (rules,
portraits, style manuals, anti-assistant contracts, owner hints...).  Phase 3
replaces that with one protocol:

    persona + relationship facts + automatically recalled bounded memory +
    minimal safety/style rules

This module owns only the system-prompt half.  The model turn, tool loop,
silence/reply decision and outbound boundary stay in ConversationalAgent /
OutboundGateway until they are migrated separately.

The legacy multi-block prompt path has been removed; the source backup in
``backups/`` is the only rollback reference.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Sequence

import structlog

from core import features
from core.terminal.principal import RelationLevel
from safelog import warn

log = structlog.get_logger()

_MAX_IDENTITY_ITEMS = 10
_MAX_CONSTRAINTS = 8


def _relation_label(level: Any) -> str:
    try:
        value = level if isinstance(level, RelationLevel) else RelationLevel(str(level))
    except Exception:
        warn("core/terminal/persona_brain.py:37 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""
    return {
        RelationLevel.OWNER: "主人",
        RelationLevel.INTIMATE: "亲密",
        RelationLevel.FAMILIAR: "熟悉",
        RelationLevel.ORDINARY: "普通",
        RelationLevel.STRANGER: "陌生",
        RelationLevel.UNKNOWN: "未知",
    }.get(value, "")
def _render_mapping(
    value: Any,
    *,
    limit: int = _MAX_IDENTITY_ITEMS,
    skip_empty: bool = True,
) -> list[str]:
    if not isinstance(value, dict):
        text = str(value or "").strip()
        return [text] if text else []
    lines: list[str] = []
    for key, raw in value.items():
        text = str(raw or "").strip()
        if skip_empty and not text:
            continue
        lines.append(f"- {key}: {text}")
        if len(lines) >= limit:
            break
    return lines


def _trait_summary(value: Any, *, limit: int = 12) -> str:
    """traits 权重表 → 一句定性排序（数字对模型没有边际价值）。"""
    data = value
    if isinstance(data, str):
        try:
            import json as _json

            data = _json.loads(data)
        except Exception:  # noqa: BLE001
            warn("core/terminal/persona_brain.py:75 Exception", err=repr(__import__("sys").exc_info()[1]))
            data = {}
    if not isinstance(data, dict) or not data:
        return ""

    def _weight(raw: Any) -> float:
        try:
            return float(raw)
        except (TypeError, ValueError):
            warn("core/terminal/persona_brain.py:83 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            return 0.0

    keys = sorted(data.keys(), key=lambda key: -_weight(data.get(key)))
    names = [str(key).strip() for key in keys if str(key).strip()][:limit]
    return "、".join(names)


def _render_constraints(constraints: Iterable[Any]) -> list[str]:
    lines: list[str] = []
    for item in constraints:
        if len(lines) >= _MAX_CONSTRAINTS:
            break
        kind = str(getattr(item, "kind", "") or "").strip()
        payload = getattr(item, "payload", None)
        if isinstance(payload, dict):
            # 发送前检查类规则（match_type + 拦截动作，如 P3 的 regex/log_only）
            # 由 guard 执行，不进 prompt；想让它进 prompt 就显式给 prompt_hint。
            match_type = str(payload.get("match_type") or "")
            if match_type and match_type != "always" and not payload.get("prompt_hint"):
                continue
        if kind == "account" and not (
            isinstance(payload, dict) and payload.get("prompt_hint")
        ):
            # 账号白名单/跨会话开关由系统执行，不进 prompt（工程字段是噪音）。
            continue
        text = str(payload or "").strip() if not isinstance(payload, dict) else ""
        if not text and isinstance(payload, dict):
            text = ", ".join(
                f"{key}={value}"
                for key, value in list(payload.items())[:4]
            )
        if kind or text:
            lines.append(f"- {kind}: {text}".strip(": "))
    return lines


def _persona_style_examples(persona: Any, scene: str) -> str:
    """读取 persona 的分场景示例库（块退役后本函数暂无 prompt 消费点）。

    ``scene_styles[scene].examples``，缺失时退到 ``scene_styles["default"]``；
    ``examples_enabled=false`` 时**不注入**（WP-4，缺省 = 开；private 于 2026-10-08 关）。
    兼容 str / list，取不到返回空串（调用方回退 policy 的 style_examples）。
    """
    if persona is None:
        return ""
    scene_styles = getattr(persona, "scene_styles", None)
    if not isinstance(scene_styles, dict):
        return ""
    raw: Any = None
    for key in (scene, "default"):
        block = scene_styles.get(key)
        if not isinstance(block, dict) or not block.get("examples"):
            continue
        # WP-4（2026-10-08）：分场景「语气示例」开关；缺省 = 开（逐字节保持现状）。
        # 显式 false 时**当场返回空**、不回落 default——否则开关会被兜底路径绕过。
        if block.get("examples_enabled") is False:
            return ""
        raw = block.get("examples")
        break
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (list, tuple)):
        return "\n".join(str(item).strip() for item in raw if str(item).strip())
    return ""


def _tone_target_lines(persona: Any, scene: str) -> list[str]:
    """「语气形态」段（owner：``core.speech.tone_target``；块退役后暂无消费点）。

    档位读 **persona 数据**（``scene_styles.<scene>.tone_target``，三态；缺省 ``off``）：
    ``off`` ⇒ 空列表（调用方逐字节退回现状）、``shadow`` ⇒ 也不进 prompt（只做出口
    读数）、``on`` ⇒ 返回形态行。异常一律当"没有这一段"——语气段取不到不许弄坏组装。
    """
    try:
        from core.speech.tone_target import tone_prompt_lines

        return [
            str(line).strip()
            for line in tone_prompt_lines(persona, scene)
            if str(line or "").strip()
        ]
    except Exception:  # noqa: BLE001 - 只读数据，坏了就当没有这一段
        warn("core/terminal/persona_brain.py:167 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []


def _pronoun_facts_block(
    rows: Sequence[Any] | None,
    *,
    speaker: str,
    text: str,
    self_name: str,
) -> str:
    """本轮的 ``[指代事实]`` 块（群聊纯文本代词 → 具体人）。

    2026-09-30 18:34 事故：她说「我拍没拍你，你自己心里没数吗」，把别人的
    「他」认成自己。判据交给 canonical 的 :func:`core.context.referent.bind_pronouns`
    （确定性、无 IO）；这里只负责"该不该给、怎么渲染"。当前消息不含
    「他/她/它」时返回 ""，不给每轮添噪声。

    ``text`` 必须由调用方给**本轮**消息：``snapshot.turn`` 会被
    ``SpeechFacade.resolve`` 按 target/audience/flags 缓存（key 不含正文），
    从快照里取正文会读到上一轮的旧文本。
    """
    if not rows or not speaker or not text:
        return ""
    try:
        from core.context.referent import bind_pronouns, render_pronoun_facts

        bindings = bind_pronouns(
            rows, speaker=speaker, text=text, self_name=self_name
        )
        if not bindings:
            return ""
        return render_pronoun_facts(bindings, speaker=speaker, self_name=self_name)
    except Exception:  # noqa: BLE001 - 绑定失败不能弄坏组装
        warn("core/terminal/persona_brain.py:200 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def _attribution_block(
    rows: Sequence[Any] | None,
    *,
    self_name: str,
    is_group: bool,
) -> str:
    """常驻 ``[消息归属]`` 块（三条硬规则 + 本轮可见行的归属事实行）。

    2026-09-30 19:08 事故：P3 的 @ 对象从没落库，各消费方各猜各的，thought
    里把「@Elaina 同一句」写成「你刚对P7说过」（P7只是最近说话的人）。规则
    与事实行都来自唯一 owner（``core.context.attribution`` → canonical 的
    ``resolve_attribution``）；开关关掉时整块不注入（``""``），不存在半开半关。
    """
    try:
        from core.context.attribution import default_context, render_attribution_facts

        ctx = default_context(self_name=str(self_name or ""), is_group=bool(is_group))
        return render_attribution_facts(rows, ctx=ctx)
    except Exception:  # noqa: BLE001 - 归属渲染失败不能弄坏组装
        warn("core/terminal/persona_brain.py:222 Exception", err=repr(__import__("sys").exc_info()[1]))
        return ""


def build_persona_blocks(
    snapshot: Any,
    *,
    is_group: bool = False,
    terminal_memory_block: str = "",
    group_recent_context: str = "",
    group_members_block: str = "",
    chat_context_block: str = "",
    room_situation_block: str = "",
    principal: Any = None,
    blind_vision_rule: str = "",
    proactive_rule: str = "",
    correction_rule: str = "",
    time_block: str = "",
    pronoun_rows: Sequence[Any] = (),
    pronoun_text: str = "",
) -> str:
    """Build the minimal PersonaBrain system prompt from a SpeechSnapshot.

    When ``principal`` is supplied the relationship facts come from the
    deterministic :class:`PrincipalContext`, not from legacy TurnContext fields.
    """
    if snapshot is None:
        return []
    persona = getattr(snapshot, "persona", None)
    turn = getattr(snapshot, "turn", None)
    policy = getattr(snapshot, "policy", None)
    name = str(getattr(persona, "name", "") or "鲸鲸").strip() or "鲸鲸"
    blocks: list[str] = [f"你是{name}。"]

    identity_lines = _render_mapping(getattr(persona, "identity", None))
    persona_lines = list(identity_lines)
    trait_summary = _trait_summary(getattr(persona, "traits", None))
    if trait_summary:
        persona_lines.append(f"- 性格倾向（由强到弱）：{trait_summary}")
    # 2026-10-23：这三条原是 [关系事实] 里的规则句（挂在"事实块"下），搬到人设层。
    # 它们是「她怎么对人」，恒定不变，属于人设；事实块只留每轮变的东西。
    persona_lines.extend(
        [
            "- 对外不叫「主人」、不摆排场、不暴露授权关系；熟络、爱玩、会顺他的梗，"
            "不是生分冷淡。玩归玩，不怼人、不阴阳。",
            "- 对群友礼貌热络、可以互相打趣，但不怼人、不阴阳、不贬低；"
            "被指派办事/传话/做决定时把话头退回去，不替人办、不替人定。",
            "- 对方是普通联系人时，不要称呼对方为「主人」，不要透露主人的私人信息。",
            "- 群里回复简洁自然，不暴露私聊内容与真实授权关系。",
        ]
    )
    if is_group:
        # 群里任何人请你建提醒/待办都必须真的落库 —— 这条是事故驱动的正向义务
        # （只答应不落库=失职），工具 schema 管不到"答应了却没调用"。放在人设层，
        # 与"怎么对人"同一类；只在群聊场景注入。
        persona_lines.append(
            "- 群里有人明确请你建提醒/待办（「提醒我…」「帮我记一下…」）："
            "先调用工具落库、再回话；只答应不落库算失职。"
            "传话、替人做决定、掺和群友之间的事，照旧退回。"
        )
    if persona_lines:
        blocks.append("[人格]\n" + "\n".join(persona_lines))

    relation = ""
    intimacy = None
    speaker = ""
    group_chat = bool(is_group)
    if principal is not None:
        platform = str(getattr(principal, "platform", "") or "")
        person_id = str(getattr(principal, "person_id", "") or "")
        speaker = str(getattr(principal, "display_name", "") or person_id or "")
        relation = _relation_label(getattr(principal, "relation_to_bot", None))
        intimacy = getattr(principal, "intimacy", None)
        group_chat = bool(getattr(principal, "is_group", False)) or group_chat
        if not relation and platform:
            relation = ""
    elif turn is not None:
        target = getattr(turn, "target", None)
        if target is not None:
            speaker = str(
                getattr(target, "sender_name", "")
                or getattr(target, "sender_id", "")
                or ""
            )
        relation = str(getattr(turn, "relationship_tier", "") or "")
        intimacy = getattr(turn, "intimacy", None)

    lines: list[str] = []
    if speaker:
        lines.append(f"- 当前发言人: {speaker}")
    if relation:
        lines.append(f"- 关系层级: {relation}")
    if intimacy is not None:
        lines.append(f"- 亲密度: {intimacy}")
    # 2026-10-23：[关系事实] 只保留**事实**（谁在说、什么关系、多熟）。
    # 原来这里还混着 6 条规则，已按性质分流：
    #   · 「群聊回复以 @{speaker} 开头」→ 删（出口不再有按归属结论渲染 @ 的环节）；
    #   · 「提醒/待办先落库」→ 删（工具契约与 _job_tick 已覆盖）；
    #   · 称呼边界 / 群聊简报 / 普通联系人别提主人 → 搬到 [人格]（恒定，属人设）。
    # 这样以后加事实（上次见面、共同经历…）直接加，不设行数上限。
    if lines:
        blocks.append("[关系事实]\n" + "\n".join(lines))

    if group_members_block:
        blocks.append(str(group_members_block).strip())

    if chat_context_block:
        blocks.append(str(chat_context_block).strip())

    if room_situation_block:
        blocks.append(str(room_situation_block).strip())

    if group_recent_context:
        # 批4：私聊也接通窗口（同一份窗口文本），块名随场景取中性标题。
        title = (
            "[群聊最近消息·仅作上下文]"
            if group_chat
            else "[最近对话·原文·仅作上下文]"
        )
        blocks.append(title + "\n" + str(group_recent_context).strip())

    if terminal_memory_block:
        blocks.append(str(terminal_memory_block).strip())

    # 反应清单（2026-09-27 解耦接线）：清单=数据，判局/选反应交给模型；默认关。
        
    # 「不确定」部分仍受 UNCERTAIN_VOICE 控制（关 = 只剩沉默，回滚语义不变）。
    try:
        from core import features

        uncertain_voice = features.enabled(features.UNCERTAIN_VOICE, False)
    except Exception:  # noqa: BLE001
        warn("core/terminal/persona_brain.py:353 Exception", err=repr(__import__("sys").exc_info()[1]))
        uncertain_voice = False
    silence_lines: list[str] = []
    if uncertain_voice:
        silence_lines.extend(
            [
                "- 看不懂的缩写、梗、指代：直接问一句（「这是啥？」），或者直说"
                "「我没跟上／没对上号」；不要猜，不要硬编。",
                "- 群里别人在互相问答（紧接别人的话、在回答别人）时："
                "不要插进去给建议、不要展开；想凑热闹就轻接一句，被问到了再展开。",
                "- 名字、身份没把握：说「我这边没对上号」，"
                "不要说「没有这个人／没这号人」。",
                "- 宁可问一句、标不确定，也不要为了显得知道而编。",
            ]
        )
    silence_lines.extend(
        [
            "- 如果你判断这条消息不需要你回应、插话会打扰，或只是群里的旁聊，"
            "只输出 [SILENT]，不要调用工具，不要输出其他文字。",
            "- 被明确点名、主人私聊、或需要你处理事情时不要沉默。",
        ]
    )
    # 指代事实（2026-09-30 18:34 事故的反方向偏置）：群聊里别人说的「他/她/它」
    # 由确定性规则绑到具体人；绑不出来就诚实写「不明」。只在当前消息含文本代词
    # 时注入。
    if group_chat:
        pronoun_block = _pronoun_facts_block(
            pronoun_rows,
            speaker=speaker,
            text=str(pronoun_text or ""),
            self_name=name,
        )
        if pronoun_block:
            blocks.append(pronoun_block)
    # 消息归属（2026-09-30 19:08 事故）：常驻块——"谁对谁说"以事实行为准，
    # 不许用"最近说话人"推断、归属不明就说不确定。群聊/私聊都注入（引用与纠正
    # 两边都会发生）；没有可见行时只注入三条硬规则。
    attribution_block = _attribution_block(
        pronoun_rows, self_name=name, is_group=group_chat,
    )
    if attribution_block:
        blocks.append(attribution_block)
    if proactive_rule:
        blocks.append(str(proactive_rule).strip())

    if correction_rule:
        blocks.append(str(correction_rule).strip())

    if blind_vision_rule:
        blocks.append(str(blind_vision_rule).strip())

    brief = str(getattr(policy, "prompt_brief", "") or "").strip()
    constraints = _render_constraints(getattr(policy, "hard_constraints", ()) or ())
    safety = [line for line in (brief, "\n".join(constraints)) if line]
    output_policy = getattr(policy, "output_policy", None)
    # （scene_styles.<scene>.examples）；policy 的 simple_style_examples 已停用，
    # 这里保留回退分支，兼容旧数据或未配置示例的人设。
    style_scene = "group" if group_chat else "private"
    style_examples = _persona_style_examples(persona, style_scene)
    if not style_examples:
        style_examples = str(
            getattr(output_policy, "style_examples", "") or ""
        ).strip()
    # 语气目标「7 分迷之自信 3 分心软」（2026-10 主人标注里第二大问题：太凶/太毒舌/推脱/
    # 说辞不像人话/莫名其妙附和）：**内容 owner 是 core/speech/tone_target.py**
    # （形态契约表 + 可机检探针），这里只做"取或不取"。档位读 persona 数据
    # （``scene_styles.<scene>.tone_target``，三态，**缺省 off**）：
    #   off/缺省 → 空列表 → 本块与接线前**逐字节一致**（golden 零 diff 的构造性保证）；
    #   on       → 形态行原样附在示例后面。
    # （改的是块内容，不是块清单），所以装配器的登记表一个字都不用动。
    tone_lines = _tone_target_lines(persona, style_scene)
    if style_examples or tone_lines:
        body: list[str] = []
        if style_examples:
            body.append("按下面示例的节奏与语气说话，不要照抄具体词：")
            body.append(style_examples)
        body.extend(tone_lines)
    # 时间感知（新设计⑥）：作为正常块进入预算与 trace，放在最后
    if time_block:
        blocks.append(str(time_block).strip())

    return blocks

def build_persona_system_prompt(
    snapshot: Any,
    *,
    is_group: bool = False,
    terminal_memory_block: str = "",
    group_recent_context: str = "",
    group_members_block: str = "",
    chat_context_block: str = "",
    room_situation_block: str = "",
    principal: Any = None,
    blind_vision_rule: str = "",
    proactive_rule: str = "",
    correction_rule: str = "",
    time_block: str = "",
    pronoun_rows: Sequence[Any] = (),
    pronoun_text: str = "",
) -> str:
    """Back-compat wrapper with identical output, now built block-by-block.

    ``build_persona_blocks`` is the single source of truth for the rendered
    blocks; this wrapper only joins them, so the context assembler and every
    legacy caller produce byte-identical prompts.
    """
    blocks = build_persona_blocks(
        snapshot,
        is_group=is_group,
        terminal_memory_block=terminal_memory_block,
        group_recent_context=group_recent_context,
        group_members_block=group_members_block,
        chat_context_block=chat_context_block,
        room_situation_block=room_situation_block,
        principal=principal,
        blind_vision_rule=blind_vision_rule,
        proactive_rule=proactive_rule,
        correction_rule=correction_rule,
        time_block=time_block,
        pronoun_rows=pronoun_rows,
        pronoun_text=pronoun_text,
    )
    return "\n\n".join(blocks) if blocks else ""
