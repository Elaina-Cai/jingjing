"""WeChat/scheduler adapter for asking a person profile question.

Core logic is ``core.people.reader.PersonProfileReader``.  This tool only:

* resolves the queried nickname/wxid to a ``person_id``,
* derives viewer/owner/context from the current AgentContext,
* calls the core reader and formats a tool result.
"""

from __future__ import annotations

from typing import Any

from agents.tools.base import BaseTool, ToolResult
from core.contracts.events import ChatType
from core.contracts.person_memory import PersonContextRequest
from core.identity.platforms import identity_type_for
from core.people.reader import PersonProfileReader
from db.repo_person_memory import PersonMemoryRepository
from safelog import warn


class GetPersonProfileTool(BaseTool):
    name = "get_person_profile"
    description = (
        "查询某个联系人的长期画像、说过的话、做过的事、偏好和关系。"
        "当用户问“某人是什么样的人 / 你还记得某人吗 / 某人说过什么 / "
        "某人和你聊过什么 / 某人最近怎么样 / 某人的画像”时，必须优先调用本工具，"
        "不要用 search_messages 逐条翻历史。"
        "用户用“他/她/它/这个人/那个人”时，先从最近对话里找到具体人名，再传给 person。"
        "可传 wxid、昵称或备注名；owner 私聊可查完整画像，"
        "普通联系人只能看到自己有权限的范围。"
    )
    parameters = {
        "person": {
            "type": "string",
            "description": "联系人的 wxid、昵称或备注名",
            "required": True,
        },
        "query": {
            "type": "string",
            "description": "本次想了解的问题，用于按相关性排序记忆",
        },
        "limit": {
            "type": "integer",
            "description": "返回观察条数，默认 12",
        },
    }

    def __init__(self, meta_repo: Any = None) -> None:
        self._meta_repo = meta_repo

    # ------------------------------------------------------------------ helpers
    def _meta(self) -> Any:
        meta = self._meta_repo
        if meta is not None and hasattr(meta, "engine") and hasattr(meta, "people"):
            return meta
        from core.terminal.store_port import get_meta_repo
        return get_meta_repo()

    def resolve_target_from_text(
        self,
        text: str,
        *,
        platform: str = "",
        account_id: str = "",
    ) -> str:
        """Find the most likely person display name mentioned in text.

        This is intentionally simple and auditable: longest known display name
        wins.  It is only used by the deterministic profile-query fast path;
        normal tool calls still pass an explicit ``person`` argument.
        """

        text = str(text or "")
        if not text:
            return ""
        try:
            meta = self._meta()
            candidates = meta.people.list_persons(
                platform=platform,
                account_id=account_id,
                limit=500,
            )
        except Exception:
            warn("agents/tools/person_profile.py:84 Exception", err=repr(__import__("sys").exc_info()[1]))
            return ""
        for person in sorted(
            candidates,
            key=lambda p: len(str(getattr(p, "display_name", "") or "")),
            reverse=True,
        ):
            name = str(getattr(person, "display_name", "") or "").strip()
            if len(name) >= 2 and name in text:
                return name
        return ""

    def _context_facts(self) -> dict[str, Any]:
        extra = getattr(self.ctx, "extra", None) or {}
        return {
            "sender_id": str(extra.get("sender_id") or ""),
            "sender_name": str(extra.get("sender_name") or ""),
            "chat_id": str(extra.get("chat_id") or ""),
            "is_group": bool(extra.get("is_group")),
            "platform": str(extra.get("platform") or ""),
            "account_id": str(extra.get("account_id") or ""),
        }

    def _resolve_via_roster(self, people: Any, query: str, *, chat_id: str,
                            platform: str, account_id: str) -> Any:
        """群里她用的名字常是群昵称；people 表里没有时，按当前群名单换成 wxid 再查一次。

        2026-11-24：账本里 3 次 `get_person_profile 失败，找不到联系人: P5`——名字是群昵称，
        people 表只认 wxid/备注。宁可多一次本会话名单查询，也不让她把「认识这个人」做成失败。
        """
        text = str(query or "").strip()
        if not text or not str(chat_id or "").strip():
            return None
        try:
            from loop import brief as _brief
            for row in _brief._roster_rows(str(chat_id)) or ():
                wxid = str(row[0] or "").strip()
                nick = str(row[1] or "").strip()
                if wxid and nick and nick == text:
                    found = people.find_persons(wxid, platform=platform,
                                                account_id=account_id, limit=5)
                    return found[0] if found else None
        except Exception as exc:  # noqa: BLE001 - 名单查不到就维持原来的「找不到」
            warn("person_profile_roster_resolve_failed", err=repr(exc)[:120])
        return None

    # ------------------------------------------------------------------ execute
    async def execute(self, **kwargs) -> ToolResult:
        person_query = str(kwargs.get("person") or kwargs.get("wxid") or "").strip()
        if not person_query:
            return ToolResult("", error="person 不能为空")

        facts = self._context_facts()
        platform, account_id = self._resolve_account()
        platform = str(facts["platform"] or platform or "")
        account_id = str(facts["account_id"] or account_id or "")
        if not all((platform, account_id, facts["chat_id"])):
            return ToolResult("", error="profile_scope_missing: 当前读取会话或账号不完整")
        if any(kwargs.get(key) and str(kwargs[key]) != current
               for key, current in (("platform", platform), ("account_id", account_id))):
            return ToolResult("", error="profile_scope_denied: 参数不能更换读取账号")

        meta = self._meta()
        people = meta.people

        target = None
        try:
            results = people.find_persons(
                person_query,
                platform=platform,
                account_id=account_id,
                limit=5,
            )
        except Exception as exc:
            warn("agents/tools/person_profile.py:129 Exception", err=repr(exc))
            return ToolResult("", error=f"查询联系人失败: {str(exc)[:120]}")
        if results:
            target = results[0]
        if target is None:
            target = self._resolve_via_roster(people, person_query, chat_id=facts["chat_id"],
                                              platform=platform, account_id=account_id)
        if target is None:
            return ToolResult("", error=f"找不到联系人: {person_query}")

        target_person_id = str(getattr(target, "person_id", "") or "")
        display = str(getattr(target, "display_name", "") or person_query)
        target_platform = str(getattr(target, "platform", "") or platform)
        target_account_id = str(getattr(target, "account_id", "") or account_id)

        if (target_platform, target_account_id) != (platform, account_id):
            return ToolResult("", error="profile_scope_denied: 联系人不属于当前账号")

        viewer_person_id = ""
        try:
            if facts["sender_id"]:
                viewer = people.get_identity(
                    platform, account_id, identity_type_for(platform, ""), facts["sender_id"],
                    facts["chat_id"] if facts["is_group"] else "",
                )
                viewer_person_id = str(getattr(viewer, "person_id", "") or "")
        except Exception:
            warn("agents/tools/person_profile.py:158 Exception", err=repr(__import__("sys").exc_info()[1]))
            viewer_person_id = ""

        # Authority is always active and fails closed; config owner-id sets and
        # model-supplied names are never used to unlock owner-only fields.
        # N4 收编点 ⑤（G1-13）：身份判定只有一处 owner（``core/rules/engine.py``
        # 的 ``is_owner``；``BaseTool.authoritative_is_owner`` 只是它的转发壳）
        # ——本工具不再自己 import 身份服务。
        viewer_is_owner = bool(self.authoritative_is_owner())
        principal = self.principal
        principal_matches = principal is not None and (
            principal.platform, principal.bot_account_id, principal.chat_id,
            str(getattr(principal.chat_type, "value", principal.chat_type)),
        ) == (platform, account_id, facts["chat_id"], "group" if facts["is_group"] else "private")
        if principal_matches:
            viewer_person_id = str(principal.person_id or viewer_person_id)
        viewer_is_owner = bool(viewer_is_owner and principal_matches)

        # Owner superpower is only honoured in 1:1/Web contexts; never leak
        # another person's private observations into a group answer.
        # 这不是动作前置条件（是"哪些字段可见"的数据面判据），所以留在本地：
        # 引擎的 ``target.role`` 维管"能不能做"，不管"字段级可见性"。
        owner_override = bool(viewer_is_owner and not facts["is_group"])

        request = PersonContextRequest(
            platform=target_platform or platform,
            account_id=target_account_id or account_id,
            chat_type=ChatType.GROUP if facts["is_group"] else ChatType.PRIVATE,
            chat_id=facts["chat_id"],
            target_person_id=target_person_id,
            viewer_person_id=viewer_person_id,
            viewer_is_owner=owner_override,
            query=str(kwargs.get("query") or f"{display} 的画像"),
            max_observations=max(1, min(50, int(kwargs.get("limit") or 12))),
        )
        memory = PersonMemoryRepository(meta.engine)
        reader = PersonProfileReader(memory, memory)
        context = reader.read(request)

        if not context.observations and context.profile is None:
            return ToolResult(f"暂无关于 {display} 的画像记忆")

        text = reader.render_text(context)
        output = f"【{display} 的画像】\n{text}"
        return ToolResult(
            output,
            data={
                "person_id": target_person_id,
                "display_name": display,
                "scope": request.chat_type.value,
                "observation_count": len(context.observations),
                "evidence_ids": list(context.source_evidence_ids[:20]),
                "owner_override": owner_override,
            },
        )
