from __future__ import annotations

import json
import re

from datetime import datetime

import structlog

from agents.tools.base import BaseTool, ToolResult
from safelog import warn

log = structlog.get_logger()


#: 非主人待提醒上限（``pending >= cap`` 即拒）。**用途只剩"代码兜底"**：
#: N4 规则引擎（``core/rules/engine.py``）在 ``policy_rules`` 里找不到
#: ``create_reminder`` 的规则数据行时，判据回落成这一个常数——于是
#: ① ``YINYUE_FEATURE_RULES=off`` 时行为与接线前**逐字节一致**（同一个数）；
#: ② 主人想在 Web 上改配额，写一行数据就生效（``to_rule()`` 是形状适配，
#:    判据本体始终在引擎里）。
NON_OWNER_PENDING_CAP = 5
_MENTION_OPT_OUT = {"不@", "不@任何人", "none", "无"}


def _pending_cap_rule():
    """把 :data:`NON_OWNER_PENDING_CAP` 折成引擎的一条 ``quota`` 规则（兜底值）。

    只在"数据面上没有 ``create_reminder`` 规则"时用；有数据行时**数据行说了算**
    （这正是「改一行数据即生效」要的效果）。

    ``effect`` 必须是 ``allow``："配额用尽才拒"由 ``decide()`` 的 quota 维说，
    写成 ``deny`` 会变成"无条件拒"（配额还有余也拦）。
    """
    from core.rules.engine import Quota, Rule, RuleAction, Target

    return Rule(
        key="builtin:create_reminder.non_owner_pending_cap",
        name="非主人待提醒上限（代码兜底）",
        target=Target(role="member"),
        # ``quota`` 是引擎的"次数"维；这里的事实来源是 reminders 表的待提醒条数
        # （``count_pending_for_requester``），由调用点作为 ``QuotaCounts`` 传进来
        # ——**判据在引擎，事实在调用点**（两本账才是要防的东西）。
        quota=Quota(per_day=NON_OWNER_PENDING_CAP),
        action=RuleAction(action_id="create_reminder", effect="allow"),
        priority=10_000,
    )


def _data_rule(action_id: str):
    """数据面上的第一条同名动作规则（没有 → ``None``，调用点用内置兜底）。"""
    try:
        from core.rules import engine as rules_engine

        for rule in rules_engine.rules():
            if rule.action.action_id == action_id:
                return rule
    except Exception:  # noqa: BLE001 - 引擎不可用就只走兜底
        warn("agents/tools/reminder.py:57 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None
    return None


def _reminder_role_is_owner(tool) -> bool:
    """"委托人是不是主人"的**唯一**判定（N4 收编点 ⑥，G1-13）。

    转发到 ``core/rules/engine.py::is_owner``（``BaseTool.authoritative_is_owner``
    也只是它的转发壳）——本工具不再自己写 ``bool(...)`` 兜底。
    """
    try:
        return bool(tool.authoritative_is_owner())
    except Exception:  # noqa: BLE001 - 身份不可得 = 不是主人（fail closed）
        warn("agents/tools/reminder.py:70 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False


def _pending_cap_exceeded(source: dict, pending: int) -> str | None:
    """待提醒条数超上限 → 回绝文案；没超 → ``None``（N4 收编点 ⑥）。

    **判据本体在引擎**：有数据行就用数据行（主人可在 Web 上改配额），没有就用
    :func:`_pending_cap_rule` 的兜底常数（同一个 ``NON_OWNER_PENDING_CAP``，
    所以 ``RULES=off`` 与接线前逐字节一致）。事实（``pending``）由本工具提供，
    所以两边永远是同一个数、不会漂。

    走的是**完整判定**（``quota`` + ``action`` 两维），不是只看配额：判定
    "不能做"由规则自己的 ``effect=deny`` 说出来，判据只有那一处。

    **fail open**：引擎/读通道不可用时返回 ``None``（照旧放行），判据故障绝不
    变成"多拒一条提醒"——同 ``output_gate`` 读不到数据行的口径。

    ``RULES=off``（默认）走**同一条判据的 off 分支**：引擎在 off 档一律放行，
    所以这里显式落到 :data:`NON_OWNER_PENDING_CAP` 那一个常数——与接线前
    **逐字节一致**（同一个数、同一句文案），想改配额就把引擎打开再写数据行。
    """
    try:
        from core.rules import engine as rules_engine

        if rules_engine.rules_mode() == "off":
            if pending >= NON_OWNER_PENDING_CAP:
                return (
                    f"对方已有 {pending} 条待提醒，到上限了：先回绝，等这些到点再说"
                )
            return None
        data_rule = _data_rule("create_reminder")
        rows = [data_rule] if data_rule is not None else [_pending_cap_rule()]
        ctx = rules_engine.ActionContext(
            action_id="create_reminder",
            actor_id=str(source.get("requester_id") or source.get("chat_id") or ""),
            chat_id=str(source.get("chat_id") or ""),
            chat_type=str(source.get("chat_type") or ""),
            platform=str(source.get("platform") or ""),
            account_id=str(source.get("account_id") or ""),
            is_owner=False,
            is_group=str(source.get("chat_type") or "") == "group",
            quota_counts=rules_engine.QuotaCounts(counts={"day": int(pending)}),
        )
        decision = rules_engine.get_engine().decide_with_rows(ctx, rows)
    except Exception as exc:  # noqa: BLE001 - 判据故障不挡创建
        log.warning("reminder_quota_check_failed", error=str(exc)[:160])
        return None
    if decision.allowed:
        return None
    # 文案与 off 分支同前缀（可读性不变），括号里补引擎给的可读判据。
    return (
        f"对方已有 {pending} 条待提醒，到上限了：先回绝，等这些到点再说"
        f"（{decision.explanation}）"
    )


def _normalize_mention(raw):
    """把模型给的 mention 归一成名字列表；None=没给，[]=明确不@。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = [p for p in re.split(r"[,，、;；\s]+", raw) if p]
    else:
        try:
            parts = [str(p).strip() for p in raw if str(p).strip()]
        except TypeError:
            warn("agents/tools/reminder.py:136 TypeError", err=repr(__import__("sys").exc_info()[1]))
            parts = [str(raw).strip()]
    if len(parts) == 1 and parts[0] in _MENTION_OPT_OUT:
        return []
    return [p for p in parts if p not in _MENTION_OPT_OUT]


class CreateReminderTool(BaseTool):
    name = "create_reminder"
    description = (
        "创建一个定时提醒，到时间后鲸鲸会在来源会话里提醒。"
        "群里任何人（不只是主人）明确请你建提醒都算数：先调用本工具落库、再回话；"
        "只要回话说接了，就必须已经落过库（不许只在嘴上答应）；不接就明说不接。"
        "非主人委托只接一次性：对方要循环（每天/每周/间隔）时必须明确回绝；"
        "群聊创建时必须定好到点@谁（可多选）：mention 传名字列表，"
        '提醒请求人本人传["我"]，不需要@传[]；缺了就先问，不要猜。'
        "purpose=act（到点去干活再回报）的执行器还没接，目前不要答应。"
    )
    parameters = {
        "content": {"type": "string", "description": "提醒内容，例如'该开会了'", "required": True},
        "remind_at": {"type": "string", "description": "提醒时间，格式 YYYY-MM-DD HH:MM，例如 2025-09-03 14:00", "required": True},
        "repeat": {"type": "string", "description": "重复规则: daily/每天, weekly/每周, monthly/每月, 或如 8h/2d 等间隔；只有主人可用"},
        "mention": {
            "type": "array",
            "items": {"type": "string"},
            "description": '群聊到点@的人名列表，可多选；["我"]=@请求人；[]=不@；私聊忽略',
        },
        "purpose": {
            "type": "string",
            "enum": ["remind", "act"],
            "description": "remind=到点说一句提醒（默认）；act=到点去做成这件事再回报（执行器未接，禁止答应）",
        },
    }

    def __init__(self, repo, meta_repo=None):
        self.repo = repo
        self.meta_repo = meta_repo

    def _resolve_source(self) -> dict:
        """把这轮对话的送达地址、委托人和群@对象候选一并固化。

        提醒的兑现责任是把话送回"被要求时所在的那个会话"，所以地址必须
        跟消息同一轮抓取，而不是到点再猜。运行时 AccountContext 是首选
        （scheduler/router 都挂了）；老调用方退化读 extra。取不到 chat_id
        时宁可不建：没有地址的提醒就是对用户的空头承诺。
        """
        ctx = self.ctx
        extra = (getattr(ctx, "extra", None) or {}) if ctx is not None else {}
        platform = str(extra.get("platform") or "").strip()
        account_id = str(extra.get("account_id") or "").strip()

        account_ctx = getattr(ctx, "account_context", None) if ctx is not None else None
        if account_ctx is not None:
            platform = str(getattr(account_ctx, "platform", "") or platform).strip()
            account_id = str(
                getattr(account_ctx, "account_id", "") or account_id
            ).strip()

        # extra 里的 account_id 在部分老路径上是 "self"/"owner" 选择词而不是
        # wxid；那不是地址，退回账号表解析。
        if platform and account_id in ("", "self"):
            accounts = (
                (getattr(ctx, "accounts", None) or {}) if ctx is not None else {}
            )
            account_id = str(accounts.get(platform) or "").strip()
        elif platform and account_id == "owner":
            owner_accounts = (
                (getattr(ctx, "owner_accounts", None) or {}) if ctx is not None else {}
            )
            ids = owner_accounts.get(platform) or []
            account_id = str(ids[0]) if ids else ""

        return {
            "platform": platform,
            "account_id": account_id,
            "chat_id": str(extra.get("chat_id") or "").strip(),
            "chat_type": "group" if extra.get("is_group") else "private",
            "requester_id": str(extra.get("sender_id") or "").strip(),
            "requester_name": str(extra.get("sender_name") or "").strip(),
            "source_ref": str(extra.get("parent_event_id") or "").strip(),
        }

    def _source_authorized(self, source: dict) -> bool:
        """非主人私聊委托门槛：该会话在 proactive_targets 里且 enabled。

        直接查库，不用 principal 的缓存 capability——白名单可能是主人刚
        在 Web 上写的一行，缓存未必已经刷到。

        只对**私聊**生效：群聊是公共委托位（2026-10 主人拍板「群里任何人
        都可以让她建提醒」），谁在群里明确开口都算数，不查白名单。
        """
        meta = self.meta_repo
        if meta is None:
            return False
        try:
            row = meta.proactive.get_target(
                source["platform"], source["account_id"], source["chat_id"],
            )
        except Exception:
            warn("agents/tools/reminder.py:233 Exception", err=repr(__import__("sys").exc_info()[1]))
            return False
        return bool(row is not None and int(getattr(row, "enabled", 0) or 0))

    def _pending_for_requester(self, source: dict) -> int:
        key = source["requester_id"] or source["chat_id"]
        if not key:
            return 0
        return int(self.repo.reminders.count_pending_for_requester(key))

    def _find_pending_dedup(self, source: dict, remind_at: datetime, content: str,
                            pid: int):
        """同一件事是否已经在册（幂等键 = ``(source_chat_id, remind_at, normalize_dedup_text(content))``）。

        唯一实现点在 ``db/repo_reminder.py::find_pending_dedup``；这里只做**创建点
        的形状适配**，绝不自己写第二份判据（两份判据 = 等着漂移）。

        替身 repo 没有这个能力时（``tests/test_reminder_delivery.py`` /
        ``tests/test_work_item_p1.py`` 的 ``_FakeReminders`` 只有 ``add``）返回
        ``None`` ⇒ 照旧新建：老替身的行为**逐字节不变**，而生产 repo 两个 manager
        （``db/repository.py`` / ``db/meta_repo.py``）挂的都是真 ``ReminderRepository``。
        """
        from db.repo_reminder import normalize_dedup_text

        reminders = getattr(self.repo, "reminders", None)
        finder = getattr(reminders, "find_pending_dedup", None)
        if finder is None:
            return None
        return finder(
            persona_id=pid,
            chat_id=source["chat_id"],
            due_at=remind_at,
            content=normalize_dedup_text(content),
        )

    async def execute(self, **kwargs) -> ToolResult:
        content = kwargs.get("content", "")
        remind_at_str = kwargs.get("remind_at", "")
        repeat_rule = kwargs.get("repeat", "")
        purpose = str(kwargs.get("purpose") or "remind").strip().lower()
        if purpose not in ("remind", "act"):
            purpose = "remind"

        if not content or not remind_at_str:
            return ToolResult("", error="content 和 remind_at 不能为空")

        try:
            remind_at = datetime.strptime(remind_at_str.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            warn("agents/tools/reminder.py:281 ValueError", err=repr(__import__("sys").exc_info()[1]))
            try:
                remind_at = datetime.strptime(remind_at_str.strip(), "%Y-%m-%d")
                remind_at = remind_at.replace(hour=9, minute=0)
            except ValueError:
                warn("agents/tools/reminder.py:285 ValueError", err=repr(__import__("sys").exc_info()[1]))
                return ToolResult("", error=f"时间格式错误: {remind_at_str}，请用 YYYY-MM-DD HH:MM")

        if remind_at < datetime.now():
            return ToolResult("", error="提醒时间已经过了")

        if purpose == "act":
            # 字段先留、执行器后接：现在说"到点我替你去办"就是空头承诺。
            return ToolResult(
                "",
                error="到点去干活再回报的执行器还没接：直接说明做不了，不要创建",
            )

        source = self._resolve_source()
        if not source["chat_id"]:
            # 不能假装记下：到点没有会话可以送达，宁可不建
            # （2026-09-28 修复：承诺与能力对齐）。
            return ToolResult(
                "",
                error="此刻无法确定要提醒到哪个会话，所以没有创建；请在目标会话里再说一次",
            )

        owner = _reminder_role_is_owner(self)
        if not owner:
            # 2026-09-28 权限收口 / 2026-10 群聊放宽：非主人只接一次性。
            # 群聊例外：主人拍板「群里任何人都可以让她建提醒」——群是公共
            # 委托位，群友明确开口就要真的建（先落库再回话），不再要求
            # proactive_targets 白名单；私聊仍是私人通道，没授权不接。
            # 拒绝文案直接写清该怎么回，保证言行一致。
            #
            # N4 收编点 ⑥（G1-13）：上面这条"非主人能不能建 / 能建几条"的判据
            # 由 ``core/rules/engine.py`` 判（``target.role`` + ``quota`` 两维），
            # 本工具只提供业务事实（待提醒条数）与文案。
            if str(repeat_rule or "").strip():
                return ToolResult(
                    "",
                    error="对方要的是循环提醒：非主人只能一次性，直接回绝，不要创建",
                )
            if source["chat_type"] != "group" and not self._source_authorized(source):
                return ToolResult(
                    "",
                    error="这个会话没有主人的提醒委托授权：回绝对方，不要创建",
                )
            pending = self._pending_for_requester(source)
            blocked = _pending_cap_exceeded(source, pending)
            if blocked is not None:
                return ToolResult("", error=blocked)

        mention_names = _normalize_mention(kwargs.get("mention"))
        if source["chat_type"] == "group":
            if mention_names is None:
                return ToolResult(
                    "",
                    error="群提醒缺@对象：先问 ta 到点要@谁（可多选，也可以不@），问清再创建",
                )
            resolved = []
            for name in mention_names:
                if name == "我":
                    if not source["requester_name"]:
                        return ToolResult(
                            "", error="不知道请求人在群里的称呼：先问清 ta 的名字，再创建",
                        )
                    name = source["requester_name"]
                resolved.append(name)
            mention_names = list(dict.fromkeys(resolved))
        else:
            mention_names = []

        from db.models import Reminder
        active = self.repo.personas.list_active()
        pid = active[0].id if active else 1

        # 幂等（2026-10-02 事故「同一句话她说了两遍」）：同一会话 + 同一到点时刻 +
        # 同一段（只抹空白的）文本，且那条**仍 pending** ⇒ 复用，不新建。
        # 判据 owner 在 db/repo_reminder.py::find_pending_dedup。
        saved = self._find_pending_dedup(source, remind_at, content, pid)
        if saved is not None:
            from db.repo_reminder import normalize_dedup_text

            log.info(
                "reminder_dedup_reused",
                reminder_id=saved.id,
                chat_id=source["chat_id"],
                due_at=remind_at.isoformat(),
                normalized_text=normalize_dedup_text(content),
            )
            # 复用既有那条：id 原样返回，投递机会仍是 1 次（不再新落一行）。
            content = str(saved.content or content)

        if saved is None:
            reminder = Reminder(
                persona_id=pid,
                content=content,
                remind_at=remind_at,
                status="pending",
                repeat_rule=repeat_rule or None,
                source_platform=source["platform"] or None,
                source_account_id=source["account_id"] or None,
                source_chat_id=source["chat_id"],
                source_chat_type=source["chat_type"],
                requester_id=(source["requester_id"] or source["chat_id"]),
                requester_name=source["requester_name"] or None,
                requester_is_owner=1 if owner else 0,
                mention_name=None,
                mention_names=(
                    json.dumps(mention_names, ensure_ascii=False) if mention_names else None
                ),
                purpose=purpose,
                attempts=0,
                created_at=datetime.now(),
            )
            saved = self.repo.reminders.add(reminder)

            # P1 双写投影（docs/任务模型.md §7.1 ①）：reminders 仍是唯一执行权威，
            # work_items 只是它的单向读模型（reminders → work_items，绝不反向）。
            # 整体 best-effort：投影失败绝不影响创建结果——下面的 ToolResult 逐字不变，
            # 这里只留一条告警。
            try:
                from core.tasks import store as work_item_store

                work_item_store.record_created(
                    self.repo,
                    saved,
                    extra=(getattr(self.ctx, "extra", None) or {}),
                    messages=list(getattr(self.ctx, "messages", None) or []),
                )
            except Exception as exc:  # noqa: BLE001 - 观测失败不影响创建结果
                log.warning("work_item_write_failed", error=str(exc)[:160])

            # 群聊v2 Step 12（交接 §5 TODO 1）：同一行再登记一条世界模型意图，供
            # 调度 tick 到点执行。INTENTION_TICK=off（默认）时本调用 no-op，
            # 创建结果与接线前逐字不变；登记失败也绝不影响创建。
            try:
                from core.context.intention_bridge import open_reminder_intention

                open_reminder_intention(
                    chat_id=source["chat_id"],
                    reminder_id=getattr(saved, "id", None),
                    due_at=remind_at,
                    evidence=content,
                    at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                    scope={
                        "platform": source.get("platform", ""),
                        "account_id": source.get("account_id", ""),
                        "chat_id": source.get("chat_id", ""),
                        "chat_type": source.get("chat_type", ""),
                        "group_id": (
                            source.get("chat_id", "")
                            if source.get("chat_type") == "group"
                            else ""
                        ),
                    },
                    source_refs=(
                        (source["source_ref"],) if source.get("source_ref") else ()
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - 意图登记失败不影响创建结果
                log.warning("intention_bridge_failed", error=str(exc)[:160])

        repeat_str = f"（每{repeat_rule}重复）" if repeat_rule else ""
        where = "在群里" if source["chat_type"] == "group" else "在这个会话里"
        mention_str = "，到点@ " + "、".join(mention_names) if mention_names else ""
        one_shot = "" if owner else "（一次性）"
        return ToolResult(
            f"已创建提醒{one_shot}: {content}，将在 "
            f"{remind_at.strftime('%Y年%m月%d日 %H:%M')} {where}提醒"
            f"{mention_str}{repeat_str}",
            data={
                "id": saved.id,
                "remind_at": remind_at.isoformat(),
                "one_shot": not owner,
                "mention": mention_names or None,
                "purpose": purpose,
            },
        )
