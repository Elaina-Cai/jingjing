"""Assemble scoped person context for a prompt.

The builder is deliberately storage-agnostic: it depends on the two ports and
pure visibility rules.  Wiring it into WeChat, QQ or the Web only requires an
adapter repository and a request describing the current conversation.
"""

from __future__ import annotations

from dataclasses import replace

from core import temporal
from core.contracts.events import ChatType
from core.contracts.person_memory import (
    PersonContext,
    PersonContextRequest,
    PersonObservation,
    PersonProfileSnapshot,
    ProfileScopeType,
)
from core.people.ports import ObservationRepository, ProfileRepository
from core.people.visibility import (
    VisibleScope,
    profile_allowed,
    filter_visible_observations,
    rank_observations,
)
from safelog import warn


def _scope_type_for(chat_type: ChatType) -> ProfileScopeType:
    if chat_type in (ChatType.GROUP, ChatType.CHANNEL):
        return ProfileScopeType.GROUP
    if chat_type == ChatType.PRIVATE:
        return ProfileScopeType.PRIVATE
    return ProfileScopeType.GLOBAL


def _format_confidence(value: float) -> str:
    try:
        return f"{max(0.0, min(1.0, float(value))) * 100:.0f}%"
    except (TypeError, ValueError):
        warn("core/people/context.py:38 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return "unknown"


def _label_for(observation: PersonObservation) -> str:
    if observation.is_group_scope():
        return f"群聊 {observation.occurred_scope_id}"
    if observation.is_private_scope():
        return "私聊"
    return "其他"


class PersonContextBuilder:
    """Build a prompt-ready context for one person in one conversation."""

    def __init__(
        self,
        observations: ObservationRepository,
        profiles: ProfileRepository,
    ) -> None:
        self._observations = observations
        self._profiles = profiles

    def build(
        self,
        request: PersonContextRequest,
        *,
        owner_cross_account: bool = False,
    ) -> PersonContext:
        if not all((request.target_person_id, request.platform, request.account_id, request.chat_id)):
            return PersonContext(request=request, prompt_block="")

        if request.viewer_is_owner and request.chat_type != ChatType.PRIVATE:
            request = replace(request, viewer_is_owner=False)
        scoped_profile = self._profiles.get_profile(
            request.target_person_id,
            platform=request.platform,
            account_id=request.account_id,
            scope_type=_scope_type_for(request.chat_type),
            scope_id=request.chat_id,
        )
        global_profile = self._profiles.get_profile(
            request.target_person_id,
            platform=request.platform,
            account_id=request.account_id,
            scope_type=ProfileScopeType.GLOBAL,
            scope_id="",
        ) if request.viewer_is_owner and request.chat_type == ChatType.PRIVATE else None
        profile = next((p for p in (scoped_profile, global_profile) if profile_allowed(p, request)), None)

        # R-I3：范围过滤同时发生在数据层（list_for_person 的行级条件）与这里
        # 的纯函数层；两层的 scope 必须来自同一个 request，缺 scope 时仓储
        # 直接拒读（返回空），不会退回"按 person_id 全取"。
        candidates = self._observations.list_for_person(
            request.target_person_id,
            scope=VisibleScope(
                platform=request.platform,
                account_id=request.account_id,
                requester_person_id=(
                    request.viewer_person_id
                ),
                chat_type=request.chat_type,
                chat_id=request.chat_id,
                viewer_is_owner=request.viewer_is_owner,
            ),
            limit=max(50, int(request.max_observations or 12) * 8),
        )
        visible = filter_visible_observations(
            candidates,
            platform=request.platform,
            account_id=request.account_id,
            chat_type=request.chat_type,
            chat_id=request.chat_id,
            viewer_person_id=request.viewer_person_id,
            viewer_is_owner=request.viewer_is_owner,
            owner_cross_account=owner_cross_account,
        )
        ranked = rank_observations(
            visible,
            query=request.query,
            current_scope_type=request.chat_type,
            current_scope_id=request.chat_id,
        )
        selected = tuple(ranked[: max(0, int(request.max_observations or 12))])

        prompt_block = self._render(request, profile, selected)
        evidence_ids: list[str] = []
        for observation in selected:
            for evidence_id in (observation.source_evidence_id, *observation.evidence_ids):
                evidence_id = str(evidence_id or "").strip()
                if evidence_id and evidence_id not in evidence_ids:
                    evidence_ids.append(evidence_id)
        return PersonContext(
            request=request,
            profile=profile,
            observations=selected,
            prompt_block=prompt_block,
            source_evidence_ids=tuple(evidence_ids),
        )

    @staticmethod
    def _render(
        request: PersonContextRequest,
        profile: PersonProfileSnapshot | None,
        observations: tuple[PersonObservation, ...],
    ) -> str:
        lines: list[str] = ["[当前联系人]", f"- 内部标识: {request.target_person_id}"]
        if profile is not None:
            if profile.summary:
                lines.append(f"- 画像摘要: {profile.summary}")
            if profile.style:
                lines.append(f"- 说话风格: {profile.style}")
            if profile.topics:
                lines.append(f"- 常聊话题: {'、'.join(profile.topics[:6])}")
            if profile.emotion_tendency:
                lines.append(f"- 情绪倾向: {profile.emotion_tendency}")
            if profile.relationship_tone:
                lines.append(f"- 互动语气: {profile.relationship_tone}")

        lines.append("[相关记忆]")
        if not observations:
            lines.append("- 暂无已确认的联系人记忆")
        else:
            for observation in observations:
                content = (observation.content or observation.raw_quote or "").strip()
                if not content:
                    continue
                lines.append(
                    f"- [{_label_for(observation)}] {content}"
                    f"（{temporal.format_stamp(observation.observed_at)}"
                    f"{'｜' if temporal.format_stamp(observation.observed_at) else ''}"
                    f"置信度 {_format_confidence(observation.confidence)}）"
                )
        return "\n".join(lines)
