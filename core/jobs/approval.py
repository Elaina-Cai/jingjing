"""审批入口：人回一句「可以」→ `Approval` 落库 → 写操作解冻。

这张板子要成立，靠的不是「她猜主人同意了」，而是**主人的那句话本身成为一行证据**。
所以这个模块只做一件很窄的事：把一条明确、短、来自私聊本人的批复，
换成 `Approval(job_id, step_n, action_hash, who, channel)` 写进 `job_approvals`，
再追加一条 `approved` 事件让「在等」解开（`store._apply_projection` 里那一支）。

四条纪律（全部为了**判错方向是「不办」而不是「办错」**）：

1. **只在私聊**（`is_private=True`）才算授权。群里第三人说「订吧」不是授权，
   `docs/decisions/办_在手的事.md` §12 第二条红线。
2. **否定优先**：句子里只要出现「不用 / 别 / 算了 / 取消 / 不行」，一律不批，
   哪怕同时出现「可以」——「可以，但是先别订」是人话里的常见反悔。
3. **一句话只批一件事**：同一会话有两件以上在等，`apply_approval` 直接放弃并记日志。
   宁可多问一次，不许把 A 的批准记到 B 头上。
4. **批的是动作不是句子**：`action_hash = step_action_hash(job_id, step)`。
   步骤一推进，旧批准自动失效（`is_approved` 按 (job, step, hash) 查）。

`JOB_LEDGER` 三档照旧：`off`/`shadow` 一行都不写，只有 `on` 才落库。
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from core.contracts.plan import Approval, Job, Policy, Step
from safelog import warn

log = logging.getLogger(__name__)

#: 一句批复的最长长度。更长的多半是新话题（"可以是可以，不过我下周三才放假"），
#: 交给判去理解，不由这里替人点头。
MAX_APPROVAL_CHARS = 40

#: 明确同意。只收「一句话就能盖章」的说法，不收「也许 / 应该 / 你说呢」。
APPROVE_TOKENS = (
    "可以",
    "批准",
    "同意",
    "确认",
    "没问题",
    "就这么办",
    "就这个",
    "订吧",
    "办吧",
    "去吧",
    "买吧",
    "发吧",
    "好",
    "行",
    "嗯",
    "准了",
)

#: 明确否定（优先于同意）。含「别」的单字也要挡——"别订" 里没有别的否定词。
DENY_TOKENS = (
    "不用",
    "不要",
    "别",
    "算了",
    "取消",
    "不行",
    "不批",
    "先不",
    "再想",
    "等等",
    "改成",
    "换一个",
    "否",
)


#: 句末要剥掉的标点（含中文引号；用 chr() 拼，免得源码里引号打架）
_TRAILING = "。．.!！?？~～,，、;；:：" + chr(0x201C) + chr(0x201D) + chr(0x2018) + chr(0x2019) + chr(34) + "'"


def _norm(text: str) -> str:
    """去空白与句末标点；不做同义词替换（那会变成分类器）。"""
    raw = "".join(str(text or "").split())
    return raw.strip(_TRAILING)


def _mode() -> str:
    from core import features

    return features.mode(features.JOB_LEDGER, features.MODE_OFF)


def parse_approval(
    text: str,
    job: Job,
    *,
    is_private: bool,
    speaker_id: str = "",
    now: datetime | None = None,
) -> Approval | None:
    """把一句话判成批准；判不出来就返回 `None`（**永远不许猜**）。"""
    if not is_private or not job.job_id:
        return None
    if job.state != "awaiting" or job.awaiting.kind != "approval":
        return None
    owner = job.owner_person_id
    if owner and speaker_id and owner != speaker_id:
        return None
    line = _norm(text)
    if not line or len(line) > MAX_APPROVAL_CHARS:
        return None
    if any(tok in line for tok in DENY_TOKENS):
        return None
    if not any(tok in line for tok in APPROVE_TOKENS):
        return None
    step = _awaiting_step(job)
    if step is None:
        return None
    from core.jobs.runner import step_action_hash
    from core.jobs.store import approval_ttl_expiry

    base = now or datetime.now()
    return Approval(
        job_id=job.job_id,
        step_n=step.n,
        action_hash=step_action_hash(job.job_id, step),
        who=speaker_id or owner or "owner",
        channel="private",
        at=base.isoformat(sep=" ", timespec="seconds"),
        expires_at=approval_ttl_expiry(base),
    )


#: 长期授权的说法。与「这次可以」分开：一次性点头叫 `Approval`，一类事不用再问叫 `grant`。
GRANT_TOKENS = (
    "以后",
    "今后",
    "往后",
    "这类事",
    "这种事",
    "不用问",
    "别问",
    "自己办",
    "自己做主",
    "自己决定",
    "直接办",
    "直接做",
    "长期",
)

#: 一句授权的最长长度（更长多半是新话题，交给判去理解）。
MAX_GRANT_CHARS = 40

#: 长期授权默认 30 天过期（`revocable=True`，随时可以再说一句撤回）。
GRANT_TTL_DAYS = 30


def parse_grant(
    text: str,
    job: Job,
    *,
    is_private: bool,
    speaker_id: str = "",
    now: datetime | None = None,
) -> Policy | None:
    """把一句话判成「这一类事以后不用再问我」；判不出来返 `None`。

    只认三种情况，其余一律 `None`（判错方向仍然是「不办」）：

    1. 私聊、本人说（群里第三人说的「以后你自己办」不算）；
    2. 句子里有明确的长期语义（`GRANT_TOKENS`），且没有否定词（否定优先）；
    3. **在等的那一步本来就属于 `grant` 档**（`write_local` 这类只动我们这一侧的事）。
       `confirm` 档的事（动别人的界面 / 对外说话）不接受一次性升级，
       每次都要点头——授权只决定「要不要问」，不许把一个 `confirm` 变成 `auto`。
    """
    if not is_private or not job.job_id:
        return None
    owner = job.owner_person_id
    if owner and speaker_id and owner != speaker_id:
        return None
    line = _norm(text)
    if not line or len(line) > MAX_GRANT_CHARS:
        return None
    if any(tok in line for tok in DENY_TOKENS):
        return None
    if not any(tok in line for tok in GRANT_TOKENS):
        return None
    step = _awaiting_step(job)
    if step is None:
        return None
    from core.jobs import policy as policy_mod

    effect = str(getattr(step, "effects", "") or "")
    if policy_mod.effect_tier(effect) != policy_mod.TIER_GRANT:
        return None
    base = now or datetime.now()
    expires = base + timedelta(days=GRANT_TTL_DAYS)
    return Policy(
        scope=effect,
        condition="",
        max_amount=None,
        expires_at=expires.isoformat(sep=" ", timespec="seconds"),
        revocable=True,
        approved_by=f"私聊:{speaker_id or owner or 'owner'}",
    )


def record_grant(job: Job, granted: Policy, *, store: Any = None) -> bool:
    """落一行长期授权（`granted` 事件 → 投影写进 `jobs.policy_json`）。"""
    if _mode() != "on":
        return False
    if store is None:
        from core.jobs.store import default_store

        store = default_store()
    if not getattr(store, "enabled", lambda: False)():
        return False
    try:
        store.append(
            job.job_id,
            "granted",
            payload={"policy": asdict(granted), "who": granted.approved_by},
            actor="human",
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 授权落库失败不许拖垮回合
        log.warning("grant_record_failed job_id=%s error=%r", job.job_id, exc)
        return False


def apply_grant(
    text: str,
    *,
    chat_id: str,
    is_private: bool,
    speaker_id: str = "",
    store: Any = None,
    now: datetime | None = None,
) -> list[Policy]:
    """这一句是不是长期授权；是就落库（同一句不许同时给两件事授权）。"""
    if not chat_id or not str(text or "").strip():
        return []
    if _mode() != "on":
        return []
    if store is None:
        from core.jobs.store import default_store

        store = default_store()
    if not getattr(store, "enabled", lambda: False)():
        return []
    from core.jobs import policy as policy_mod

    try:
        candidates = []
        for job in store.open_jobs(chat_id):
            step = _awaiting_step(job)
            if step is None:
                continue
            effect = str(getattr(step, "effects", "") or "")
            if policy_mod.effect_tier(effect) == policy_mod.TIER_GRANT:
                candidates.append(job)
    except Exception as exc:  # noqa: BLE001
        log.warning("grant_scan_failed chat_id=%s error=%r", chat_id, exc)
        return []
    if len(candidates) > 1:
        log.info("grant_ambiguous chat_id=%s candidates=%d", chat_id, len(candidates))
        return []
    if not candidates:
        return []
    granted = parse_grant(
        text, candidates[0], is_private=is_private, speaker_id=speaker_id, now=now
    )
    if granted is None:
        return []
    job = candidates[0]
    if not record_grant(job, granted, store=store):
        return []
    # 一句话也是对这一句的点头：人正被问着「要不要写这个文件」，
    # 他说「这类事以后你自己办」= 这次也办 + 以后不用问。
    # 所以要同时落一行 `Approval` 把他从「在等」里解开，否则授权到手却还卡在原地。
    if job.state == "awaiting" and job.awaiting.kind == "approval":
        step = _awaiting_step(job)
        if step is not None:
            from core.jobs.runner import step_action_hash
            from core.jobs.store import approval_ttl_expiry

            base = now or datetime.now()
            record_approval(
                job,
                Approval(
                    job_id=job.job_id,
                    step_n=step.n,
                    action_hash=step_action_hash(job.job_id, step),
                    who=speaker_id or job.owner_person_id or "owner",
                    channel="private",
                    at=base.isoformat(sep=" ", timespec="seconds"),
                    expires_at=approval_ttl_expiry(base),
                ),
                store=store,
            )
    log.info("grant_recorded job_id=%s scope=%s", job.job_id, granted.scope)
    return [granted]

#: 「把授权收回来」的说法。与 `GRANT_TOKENS` 方向相反：那类要「以后自己办」，这类要「以后都问我」。
REVOKE_TOKENS = (
    "撤回授权",
    "撤销授权",
    "取消授权",
    "收回授权",
    "授权撤回",
    "授权撤销",
    "授权取消",
    "收回权限",
    "取消权限",
    "别自己办",
    "不要自己办",
    "不用自己办",
    "别自己做主",
    "不要自己做主",
    "还是问我",
    "都问我",
    "先问我",
    "每次问我",
)

#: 「不是要撤回」——**否定优先**：「不用撤回授权了」不许把授权撤掉。
KEEP_TOKENS = (
    "不用撤",
    "不要撤",
    "别撤",
    "不是撤",
    "没让你撤",
    "不用收回",
    "不要收回",
    "别收回",
    "不用取消",
    "不要取消",
    "别取消",
)

#: 一句撤回的最长长度。
MAX_REVOKE_CHARS = 40


def live_grant(job: Job, *, now: datetime | None = None) -> Policy | None:
    """这件事手上**还有效**的长期授权；没有就 `None`。

    「有效」= `approved_by` 非空 + `scope` 非空 + 没过期。
    与 `policy.grant_allows()` 的判据同源（那边是「能不能覆盖这一步」，这边是「有没有这么张票」）。
    """
    policy = getattr(job, "policy", None)
    if policy is None:
        return None
    if not str(getattr(policy, "approved_by", "") or "").strip():
        return None
    if not str(getattr(policy, "scope", "") or "").strip():
        return None
    raw = str(getattr(policy, "expires_at", "") or "").strip()
    if not raw:
        return policy
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        warn("core/jobs/approval.py:361 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return None
    base = now or datetime.now()
    if expires.tzinfo is None and base.tzinfo is not None:
        expires = expires.replace(tzinfo=base.tzinfo)
    if base > expires:
        return None
    return policy


def parse_revoke_grant(
    text: str,
    *,
    is_private: bool,
    speaker_id: str = "",
    owner: str = "",
    job: Job | None = None,
    now: datetime | None = None,
) -> str:
    """把一句话判成「把授权收回来」；判不出来返空串（返回的是命中的说法，只用于留痕）。

    五道闸，任何一条不成立都返空串（判不出来就什么都不做）：

    1. 私聊、本人说（群里第三人说「别让他自己订」不算）；
    2. 不超过 40 字（更长多半是新话题，交给判）；
    3. **否定优先**：句子里出现 `KEEP_TOKENS`（「不用撤回」）→ 不撤；
    4. 必须有明确的收回语义（`REVOKE_TOKENS`）；
    5. 给了 `job` 时，那件事上必须**确实还有**一张有效的授权（撤一张没有的票没有意义）。
    """
    if not is_private:
        return ""
    if owner and speaker_id and owner != speaker_id:
        return ""
    line = _norm(text)
    if not line or len(line) > MAX_REVOKE_CHARS:
        return ""
    if any(tok in line for tok in KEEP_TOKENS):
        return ""
    hit = next((tok for tok in REVOKE_TOKENS if tok in line), "")
    if not hit:
        return ""
    if job is not None and live_grant(job, now=now) is None:
        return ""
    return hit


def _expired_now(granted: Policy, *, now: datetime | None = None) -> dict[str, Any]:
    """把一张授权做成「已过期」的样子（早一秒，保证 `>` 判定成立）。"""
    base = now or datetime.now()
    return asdict(
        Policy(
            scope=granted.scope,
            condition=granted.condition,
            max_amount=granted.max_amount,
            expires_at=(base - timedelta(seconds=1)).isoformat(sep=" ", timespec="seconds"),
            revocable=granted.revocable,
            approved_by=granted.approved_by,
        )
    )


def record_revoke(
    job: Job,
    *,
    store: Any = None,
    who: str = "",
    reason: str = "",
    now: datetime | None = None,
) -> bool:
    """落一行 `grant_revoked`（投影把 policy 钉成过期）。"""
    if _mode() != "on":
        return False
    if store is None:
        from core.jobs.store import default_store

        store = default_store()
    if not getattr(store, "enabled", lambda: False)():
        return False
    granted = live_grant(job, now=now)
    if granted is None:
        return False
    try:
        store.append(
            job.job_id,
            "grant_revoked",
            payload={
                "policy": asdict(granted),
                "revoked_policy": _expired_now(granted, now=now),
                "who": who or granted.approved_by,
                "reason": reason,
            },
            actor="human",
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 撤权落库失败不许拖垮回合
        log.warning("grant_revoke_failed job_id=%s error=%r", job.job_id, exc)
        return False


def apply_revoke_grant(
    text: str,
    *,
    chat_id: str,
    is_private: bool,
    speaker_id: str = "",
    store: Any = None,
    now: datetime | None = None,
) -> list[Policy]:
    """这一句是不是「把授权收回来」；是就落一行并让撤权立刻生效。

    同一句不许同时撤两件事的授权（两件都还有效 → 一句都不撤，回空）。
    """
    if not chat_id or not str(text or "").strip():
        return []
    if _mode() != "on":
        return []
    if store is None:
        from core.jobs.store import default_store

        store = default_store()
    if not getattr(store, "enabled", lambda: False)():
        return []
    try:
        candidates = [job for job in store.open_jobs(chat_id) if live_grant(job, now=now) is not None]
    except Exception as exc:  # noqa: BLE001
        log.warning("grant_revoke_scan_failed chat_id=%s error=%r", chat_id, exc)
        return []
    if len(candidates) > 1:
        log.info("grant_revoke_ambiguous chat_id=%s candidates=%d", chat_id, len(candidates))
        return []
    if not candidates:
        return []
    job = candidates[0]
    reason = parse_revoke_grant(
        text,
        is_private=is_private,
        speaker_id=speaker_id,
        owner=job.owner_person_id,
        job=job,
        now=now,
    )
    if not reason:
        return []
    granted = live_grant(job, now=now)
    if granted is None:
        return []
    if not record_revoke(job, store=store, who=speaker_id or job.owner_person_id or "owner", reason=reason, now=now):
        return []
    log.info("grant_revoked job_id=%s scope=%s reason=%s", job.job_id, granted.scope, reason)
    return [granted]


def _awaiting_step(job: Job) -> Step | None:
    """在等的那一步：`next_step()` 优先，退一步取 `step_n` 之后的第一条。"""
    step = job.next_step()
    if step is not None:
        return step
    for candidate in job.steps:
        if candidate.n > job.step_n:
            return candidate
    return None


def record_approval(job: Job, approval: Approval, *, store: Any = None) -> bool:
    """落一行批准 + 追加 `approved` 事件（后者才解开「在等」）。"""
    if _mode() != "on":
        return False
    if store is None:
        from core.jobs.store import default_store

        store = default_store()
    if not getattr(store, "enabled", lambda: False)():
        return False
    try:
        # `approve()` 自己会追加 `approved` 事件（解开「在等」靠的就是那一条），
        # 这里再追一条就是同一件事写两行账——审计时数不清批了几次。
        return bool(store.approve(approval))
    except Exception as exc:  # noqa: BLE001 - 审批落库失败不许拖垮回合
        log.warning(
            "approval_record_failed job_id=%s step=%s error=%r",
            approval.job_id,
            approval.step_n,
            exc,
        )
        return False


def _miss(reason: str, **fields: Any) -> list:
    """静默失败也要留一行：**返回空列表不许无声**。

    真机教训（2026-10-21）：主人回了「可以」，账上一行没有，日志也一条没有——
    ``return []`` 全是哑的，于是谁都说不出为什么没生效。
    """
    parts = " ".join(f"{k}={v!r}" for k, v in fields.items())
    log.info("approval_miss reason=%s %s", reason, parts)
    return []


def apply_approval(
    text: str,
    *,
    chat_id: str,
    is_private: bool,
    speaker_id: str = "",
    store: Any = None,
    now: datetime | None = None,
) -> list[Approval]:
    """这一句话是不是对某一件在等的事的批准；是就落库并解开它。"""
    if not chat_id or not str(text or "").strip():
        return _miss("empty", chat_id=chat_id, text=text)
    mode = _mode()
    if mode != "on":
        return _miss("mode_off", mode=mode)
    if store is None:
        from core.jobs.store import default_store

        store = default_store()
    if not getattr(store, "enabled", lambda: False)():
        return _miss("store_disabled", chat_id=chat_id)
    try:
        waiting = [
            job
            for job in store.open_jobs(chat_id)
            if job.state == "awaiting" and job.awaiting.kind == "approval"
        ]
    except Exception as exc:  # noqa: BLE001
        log.warning("approval_scan_failed chat_id=%s error=%r", chat_id, exc)
        return []
    if len(waiting) > 1:
        # 一句话不许同时给两件事盖章——分不清她在批哪一件。
        log.info("approval_ambiguous chat_id=%s waiting=%d", chat_id, len(waiting))
        return []
    if not waiting:
        return _miss("no_waiting", chat_id=chat_id, text=text)
    approval = parse_approval(
        text, waiting[0], is_private=is_private, speaker_id=speaker_id, now=now
    )
    if approval is None:
        return _miss("no_token", chat_id=chat_id, text=text, is_private=is_private)
    if not record_approval(waiting[0], approval, store=store):
        return _miss("record_failed", job_id=waiting[0].job_id)
    log.info(
        "approval_granted chat_id=%s job_id=%s step=%d",
        chat_id,
        approval.job_id,
        approval.step_n,
    )
    return [approval]
