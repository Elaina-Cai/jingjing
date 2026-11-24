"""群成员名单与「@ 的是谁」的合法/合理判定（task-47，只读、纯判定）。

说话人 / 受话人由 ``core.conversation.addressing`` 判定；出站 ``@`` 的文字由
模型自己写在正文里（出口不再有按归属结论二次渲染的环节）。

合法性来源（任一即可，全部是**事实**，不猜）：
``group_memberships``（群名片 + wxid，canonical ``known_member_keys`` 同源）、
本轮触发者、本轮 inbound 里明确 @ 到的人、本窗口近期说过话的人。

合理性（plausibility）是**另一件事**：名字在名单里只证明「这个人存在」，证不了
「她该跟他说」。所以「@ 了名单里但近期没出现过的人」只记 ``unlikely``，不拦。

纪律：不新造 @ 正则（用 ``core.speech.resolver._address_span``）、不做昵称模糊
匹配、不改 ``group_memberships`` 语义、任何异常都退回「没有这份证据」。
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
from dataclasses import dataclass
from typing import Any
from safelog import warn

from core.infra.envflags import text

#: 名单来源标签（写进 shadow 日志，便于以后分列读数）
ROSTER_SOURCE_MEMBERSHIP = "group_memberships"
ROSTER_SOURCE_TRIGGER = "trigger"
ROSTER_SOURCE_INBOUND = "inbound"
ROSTER_SOURCE_WINDOW = "window"

#: 合理性判据里「近期」的窗口大小（卡面：N=20，可配置）
DEFAULT_RECENT_LIMIT = 20
#: 合理性判据：近 N 小时内在该群说过话（lead 2026-10-17 决定；`N=20` 条窗口实测
#: 只覆盖 82/166，24h 活跃覆盖 166/166）。``YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS``
#: 可配，钳 1–168；只影响 ``unlikely``，不影响合法性判定。
DEFAULT_ACTIVE_HOURS = 24
MAX_ACTIVE_HOURS = 168
ACTIVE_HOURS_ENV = "YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS"
ROSTER_TTL_SECONDS = 300.0
RECENT_TTL_SECONDS = 60.0
_CACHE_MAX = 64

#: 名字边界上的标点（模型常写 ``@P6.`` / ``@fleurira.``，群名片里也可能带点）。
#: 只做**边界**归一，不做模糊匹配：去尾标点 + casefold，别的一律不动。
_ADDRESS_PREFIX_CHARS = " \t\u3000\u2005"
_NAME_TAIL = "。.．·,，、;；:：!！?？~～\u3000 "

_ROSTER_CACHE: dict[str, tuple[float, "RosterSnapshot"]] = {}
_RECENT_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class RosterVerdict:
    """一次 @ 目标的判定结论（只描述事实，不描述该怎么改）。"""

    name: str = ""
    person_id: str = ""
    legal: bool = False
    ambiguous: bool = False
    plausible: bool = False
    reason: str = ""
    roster_source: str = ""
    in_roster: bool = False
    recent: bool = False
    stripped_text: str = ""

    @property
    def unlikely(self) -> bool:
        """合法但「近期没出现过」——只记录，不拦。"""
        return bool(self.legal and not self.plausible)

    def as_dict(self) -> dict[str, Any]:
        return {
            "at_name": self.name,
            "person_id": self.person_id,
            "legal": self.legal,
            "ambiguous": self.ambiguous,
            "plausible": self.plausible,
            "unlikely": self.unlikely,
            "reason": self.reason,
            "roster_source": self.roster_source,
            "in_roster": self.in_roster,
            "recent": self.recent,
        }


def _name_key(name: str) -> str:
    """名字的比较键：去首尾空白 + 去尾标点 + casefold（不是模糊匹配）。"""
    return str(name or "").strip().rstrip(_NAME_TAIL).casefold()


def _active_hours_default() -> int:
    return active_hours()


def active_hours() -> int:
    """合理性判据的小时数（默认 24，钳 1–168）。非法值回落默认、不抛。"""
    raw = text("YINYUE_ADDRESSEE_ROSTER_ACTIVE_HOURS")
    if raw is None or not str(raw).strip():
        return DEFAULT_ACTIVE_HOURS
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        warn("core/context/group_members.py:105 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return DEFAULT_ACTIVE_HOURS
    return max(1, min(MAX_ACTIVE_HOURS, value))


def _meta_repo(meta_repo: Any = None) -> Any:
    if meta_repo is not None:
        return meta_repo
    from core.terminal.store_port import get_meta_repo

    return get_meta_repo()


def _roster_rows(chat_id: str, *, meta_repo: Any = None) -> list[tuple[str, str, str]]:
    """(群名片, person_id, wxid) 行；读不到返回空列表（没有名单 = 没有证据）。

    必须同时带出 wxid：``terminal_events.actor_person_id`` 里放的是**账号 id
    （wxid）**，只拿 meta 人 id 去比「近期说过话」会 100% 对不上（实测 0/166）。
    """
    try:
        from sqlalchemy import text as _sa_text

        repo = _meta_repo(meta_repo)
        with repo.engine.connect() as conn:
            rows = conn.execute(
                _sa_text(
                    "select gm.group_nickname, gm.person_id, pi.identity_value "
                    "from group_memberships gm "
                    "left join person_identities pi "
                    "  on pi.person_id = gm.person_id "
                    " and pi.platform = gm.platform "
                    " and pi.identity_type = 'wxid' "
                    "where gm.chat_id = :chat and gm.group_nickname != ''"
                ),
                {"chat": str(chat_id)},
            ).fetchall()
    except Exception:  # noqa: BLE001 - 读不到名单就当没有这份证据
        warn("core/context/group_members.py:141 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []
    out: list[tuple[str, str, str]] = []
    for row in rows:
        values = list(row) + ["", "", ""]
        name = str(values[0] or "").strip()
        pid = str(values[1] or "").strip()
        wxid = str(values[2] or "").strip()
        if name:
            out.append((name, pid, wxid))
    return out


def _identity_maps(conn: Any) -> tuple[dict, dict, set[str]]:
    """``person_identities`` → 两套映射 + 全部 person id（task-49 双身份）。

    * ``by_account[(platform, account_id, value)] -> person_id``
    * ``by_platform[(platform, value)] -> person_id``
    * 同一个键对应多个 person 时记 ``None``（歧义 ⇒ 不当成同一个人）。

    ``terminal_events.actor_person_id`` 在 on 档写 meta 人 id、off 档（以及存量行）
    写平台号，所以读侧必须两套都能落到同一个人身上。
    """
    by_account: dict[tuple[str, str, str], str | None] = {}
    by_platform: dict[tuple[str, str], str | None] = {}
    persons: set[str] = set()
    try:
        from sqlalchemy import text as _sa_text

        rows = conn.execute(
            _sa_text(
                "select platform, account_id, identity_value, person_id "
                "from person_identities"
            )
        ).fetchall()
    except Exception:  # noqa: BLE001 - 读不到身份表就只按原值去重
        warn("core/context/group_members.py:176 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}, {}, set()
    for row in rows:
        values = list(row) + ["", "", "", ""]
        platform = str(values[0] or "").strip()
        account_id = str(values[1] or "").strip()
        value = str(values[2] or "").strip()
        person = str(values[3] or "").strip()
        if not value or not person:
            continue
        persons.add(person)
        scoped = (platform, account_id, value)
        if scoped in by_account and by_account[scoped] != person:
            by_account[scoped] = None
        else:
            by_account.setdefault(scoped, person)
        wide = (platform, value)
        if wide in by_platform and by_platform[wide] != person:
            by_platform[wide] = None
        else:
            by_platform.setdefault(wide, person)
    return by_account, by_platform, persons


def _canonical_speaker_ids(conn: Any, rows: Any) -> list[str]:
    """把「谁说过话」的行归一到 person id；解析不出保留原值（不猜、不丢）。

    行形如 ``(actor_person_id, platform, bot_account_id)``。on 档写的是 person id
    （原样保留），存量/off 档写的是 wxid（映射到 person id）。两者落到同一个人时
    只算一个人 —— 这就是「人数在 off/on 两档相等」的保证。
    """
    by_account, by_platform, persons = _identity_maps(conn)
    out: list[str] = []
    for row in rows:
        values = list(row) + ["", ""]
        raw = str(values[0] or "").strip()
        if not raw:
            continue
        platform = str(values[1] or "").strip()
        account_id = str(values[2] or "").strip()
        if raw in persons:
            person = raw
        else:
            person = ""
            if account_id:
                key = (platform, account_id, raw)
                person = by_account.get(key) or ""
                if not person and key in by_account:
                    person = ""
            if not person:
                person = by_platform.get((platform, raw)) or ""
        value = person or raw
        if value not in out:
            out.append(value)
    return out


@dataclass(frozen=True)
class RosterSnapshot:
    """一个群的名单快照：名字 → 人 id 集合 + 人 id → 身份 id 集合。"""

    by_name: dict[str, frozenset[str]]
    identities: dict[str, frozenset[str]]

    @property
    def all_ids(self) -> frozenset[str]:
        out: set[str] = set()
        for value in self.identities.values():
            out.update(value)
        return frozenset(out)

    def ids_of(self, person_id: str) -> frozenset[str]:
        return self.identities.get(str(person_id or ""), frozenset({str(person_id or "")}))


def roster_snapshot(chat_id: str, *, meta_repo: Any = None,
                    ttl: float = ROSTER_TTL_SECONDS) -> RosterSnapshot:
    """群名单快照（带 TTL 缓存）；读不到就是空快照（= 没有这份证据）。"""
    chat = str(chat_id or "").strip()
    if not chat:
        return RosterSnapshot(by_name={}, identities={})
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _ROSTER_CACHE.get(chat)
        if hit is not None and ttl >= 0 and (now - hit[0]) <= ttl:
            return hit[1]
    by_name: dict[str, set[str]] = {}
    identities: dict[str, set[str]] = {}
    for name, person_id, wxid in _roster_rows(chat, meta_repo=meta_repo):
        key = _name_key(name)
        if not key:
            continue
        if person_id:
            by_name.setdefault(key, set()).add(person_id)
            bucket = identities.setdefault(person_id, {person_id})
            if wxid:
                bucket.add(wxid)
        elif wxid:
            by_name.setdefault(key, set()).add(wxid)
            identities.setdefault(wxid, {wxid})
    snapshot = RosterSnapshot(
        by_name={key: frozenset(values) for key, values in by_name.items()},
        identities={key: frozenset(values) for key, values in identities.items()},
    )
    with _CACHE_LOCK:
        _ROSTER_CACHE[chat] = (now, snapshot)
        if len(_ROSTER_CACHE) > _CACHE_MAX:
            oldest = sorted(_ROSTER_CACHE.items(), key=lambda item: item[1][0])[
                : len(_ROSTER_CACHE) - _CACHE_MAX
            ]
            for key, _value in oldest:
                _ROSTER_CACHE.pop(key, None)
    return snapshot


def member_index(chat_id: str, *, meta_repo: Any = None,
                 ttl: float = ROSTER_TTL_SECONDS) -> dict[str, frozenset[str]]:
    """群名片（边界归一后）→ 可能的人 id 集合（兼容读口）。"""
    return dict(roster_snapshot(chat_id, meta_repo=meta_repo, ttl=ttl).by_name)


def recent_speaker_ids(chat_id: str, *, limit: int = DEFAULT_RECENT_LIMIT,
                       hours: int = 0, now: "dt.datetime | None" = None,
                       meta_repo: Any = None,
                       ttl: float = RECENT_TTL_SECONDS) -> tuple[str, ...]:
    """近期说话人（去重）。

    ``hours > 0`` ⇒ 「近 ``hours`` 小时在该群说过话」（合理性判据，见
    :func:`active_hours`）；否则退回「最近 ``limit`` 条事件」（旧口径，保留给对账）。
    ``now`` 供回放把「当时」当判据（生产传 ``None`` = 现在）。

    task-49：返回值是 **person id 空间**（``_canonical_speaker_ids``）。存量行是
    wxid、on 档新行是 person id，归一到同一空间后「人数」在两档相等；解析不出的
    占位符保留原值，不丢行。
    """
    chat = str(chat_id or "").strip()
    if not chat:
        return ()
    if hours > 0:
        hours = max(1, min(MAX_ACTIVE_HOURS, int(hours)))
        anchor = now or dt.datetime.now()
        since = (anchor - dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        if limit <= 0:
            return ()
        since = ""
    key = f"{chat}|{hours}h|{since}|{limit}"
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _RECENT_CACHE.get(key)
        if hit is not None and ttl >= 0 and (now - hit[0]) <= ttl:
            return hit[1]
    try:
        from sqlalchemy import text as _sa_text

        repo = _meta_repo(meta_repo)
        with repo.engine.connect() as conn:
            if hours > 0:
                rows = conn.execute(
                    _sa_text(
                        "select distinct actor_person_id, platform, bot_account_id "
                        "from terminal_events "
                        "where chat_id = :chat and actor_person_id != '' "
                        "and occurred_at >= :since"
                    ),
                    {"chat": chat, "since": since},
                ).fetchall()
            else:
                rows = conn.execute(
                    _sa_text(
                        "select actor_person_id, platform, bot_account_id "
                        "from terminal_events "
                        "where chat_id = :chat and actor_person_id != '' "
                        "order by occurred_at desc limit :lim"
                    ),
                    {"chat": chat, "lim": int(limit)},
                ).fetchall()
            rows = _canonical_speaker_ids(conn, rows)
    except Exception:  # noqa: BLE001 - 读不到近期说话人 = 没有这份证据
        warn("core/context/group_members.py:354 Exception", err=repr(__import__("sys").exc_info()[1]))
        rows = []
    out: list[str] = []
    for person_id in rows:
        value = str(person_id or "").strip()
        if value and value not in out:
            out.append(value)
    keys = tuple(out)
    with _CACHE_LOCK:
        _RECENT_CACHE[key] = (now, keys)
        if len(_RECENT_CACHE) > _CACHE_MAX:
            oldest = sorted(_RECENT_CACHE.items(), key=lambda item: item[1][0])[
                : len(_RECENT_CACHE) - _CACHE_MAX
            ]
            for name, _value in oldest:
                _RECENT_CACHE.pop(name, None)
    return keys


def _person_ids_of_name(name: str, *, platform: str,
                        meta_repo: Any = None) -> frozenset[str]:
    """canonical 人名 → person_id 集合（``PeopleRepository.find_persons``）。"""
    value = str(name or "").strip()
    if not value:
        return frozenset()
    try:
        repo = _meta_repo(meta_repo)
        people = getattr(repo, "people", None)
        rows = list(people.find_persons(value, platform=str(platform or ""),
                                        limit=5) or ())
    except Exception:  # noqa: BLE001 - 认不出人就按没有这份证据
        warn("core/context/group_members.py:384 Exception", err=repr(__import__("sys").exc_info()[1]))
        return frozenset()
    found = {str(getattr(row, "person_id", "") or "").strip() for row in rows}
    found.discard("")
    return frozenset(found)


def _inbound_named(name_map: dict[str, set[str]], inbound: str, *,
                   platform: str, meta_repo: Any = None,
                   roster_ids: "set[str] | None" = None) -> set[str]:
    """本轮 inbound 里点名的人 → 人 id（用于 ``legal_ids`` 扩容）。

    只有**本群成员**才登记进 ``name_map``：``find_persons`` 是全局名字表，
    把全局同名并进本群的名字表会凭空造出 ``ambiguous``（实测 ``@Elaina`` 在
    ``person_entities`` 有 3 个 person、本群名单只有 1 个，却被判重名）。
    """
    try:
        from core.conversation.addressing import mentioned_names

        names = mentioned_names(str(inbound or ""))
    except Exception:  # noqa: BLE001 - 解析不出就只按已有名单判
        warn("core/context/group_members.py:404 Exception", err=repr(__import__("sys").exc_info()[1]))
        return set()
    ids: set[str] = set()
    for name in names:
        found = _person_ids_of_name(name, platform=platform, meta_repo=meta_repo)
        if not found:
            continue
        ids.update(found)
        if roster_ids is None:
            name_map.setdefault(_name_key(name), set()).update(found)
            continue
        in_group = set(found) & set(roster_ids)
        if in_group:
            name_map.setdefault(_name_key(name), set()).update(in_group)
    return ids


def judge_address(
    text: str,
    *,
    chat_id: str,
    platform: str = "",
    trigger_name: str = "",
    trigger_id: str = "",
    inbound: str = "",
    recent_limit: int = DEFAULT_RECENT_LIMIT,
    active_hours: int = 0,
    now: "dt.datetime | None" = None,
    meta_repo: Any = None,
    person_id_of: Any = None,
    known_names: tuple[str, ...] = (),
) -> RosterVerdict | None:
    """正文里首个招呼位 ``@name`` 的合法/合理判定；没有 @ / 广播 → ``None``。

    ``known_names`` = 本轮「允许集合」里已有的事实名字（触发者 / 被引用作者 /
    呼唤发出者）；名字直接对得上就是合法，不再要求名单里有它 —— 这三类是本轮
    确定的事实，比群名片更硬。``person_id_of`` 可注入（canonical 解析器，测试可替身）。

    身份 id 空间：名单里的人有 **meta 人 id** 和 **wxid** 两个身份；
    ``terminal_events.actor_person_id`` 放的是 wxid。合法性与合理性都在
    「这个人的所有身份 id」上做交，不能只比 meta 人 id（实测会 100% 对不上）。
    """
    from core.speech.resolver import _address_span, _is_broadcast_mention

    raw = str(text or "")
    if not raw.strip():
        return None
    chat = str(chat_id or "").strip()
    roster = roster_snapshot(chat, meta_repo=meta_repo)
    span = _address_span(raw)
    if span is None:
        return None
    name, span_start, span_end = span
    extended = _extend_span_with_roster(raw, roster, span_start, span_end)
    if extended is not None:
        name, span_start, span_end = extended
    if _is_broadcast_mention(name):
        return None
    key = _name_key(name)
    stripped = _strip_span(raw, span_start, span_end)
    known = {_name_key(item) for item in known_names if _name_key(item)}
    if key in known:
        return RosterVerdict(
            name=name, legal=True, plausible=True, reason="allowed",
            roster_source=ROSTER_SOURCE_TRIGGER
            if key == _name_key(trigger_name)
            else ROSTER_SOURCE_INBOUND,
        )
    name_map: dict[str, set[str]] = {
        alias: set(ids) for alias, ids in roster.by_name.items()
    }
    trigger = str(trigger_id or "").strip()
    trigger_label = str(trigger_name or "").strip()
    if trigger and trigger_label:
        name_map.setdefault(_name_key(trigger_label), set()).add(trigger)
    roster_ids = {pid for ids in roster.by_name.values() for pid in ids}
    inbound_ids = _inbound_named(name_map, inbound, platform=platform,
                                meta_repo=meta_repo, roster_ids=roster_ids)
    hours = active_hours if active_hours > 0 else _active_hours_default()
    recent = recent_speaker_ids(
        chat, limit=recent_limit, hours=hours, now=now, meta_repo=meta_repo)
    legal_ids = set(roster_ids) | set(recent) | set(inbound_ids) | set(roster.all_ids)
    if trigger:
        legal_ids.add(trigger)
    elif callable(person_id_of) and trigger_label:
        try:
            trigger_alias = str(
                person_id_of(trigger_label, platform=platform) or "").strip()
        except Exception:  # noqa: BLE001 - 认不出就当没有这条事实
            warn("core/context/group_members.py:492 Exception", err=repr(__import__("sys").exc_info()[1]))
            trigger_alias = ""
        if trigger_alias:
            legal_ids.add(trigger_alias)
            legal_ids.update(roster.ids_of(trigger_alias))

    ids = name_map.get(key, set())
    in_roster = bool(ids & roster_ids)
    resolved_in_group = False
    if len(ids) > 1:
        # 全局名字表里同名多人时，**只用这个群的证据收窄**：本群名单 / 近期说话人 /
        # 本轮 inbound 点名 / 触发者。收窄到唯一一个就用它（仍记 ambiguous 供观测），
        # 收窄后仍多于一个才判重名 —— 绝不退回全局名字表去猜。
        # 证据分层：第一层「这个群的证据」（本群名单 / 近期说话人 / inbound 点名 /
        # 触发者）只用来排除「全局同名但不在本群」的人；如果剩多个，第二层用
        # 「本轮证据」（近期说话 / inbound 点名 / 触发者）再收窄 —— 两个本群成员
        # 同名时，只有这一轮真的在场/被点名的那个人才算。
        group_evidence = set(roster_ids) | set(recent) | set(inbound_ids)
        if trigger:
            group_evidence.add(trigger)
        candidates = {
            pid for pid in ids
            if (set(roster.ids_of(pid)) | {pid}) & group_evidence
        }
        if len(candidates) > 1:
            turn_evidence = set(recent) | (set(inbound_ids) & set(roster_ids))
            if trigger:
                turn_evidence.add(trigger)
            narrowed = {
                pid for pid in candidates
                if (set(roster.ids_of(pid)) | {pid}) & turn_evidence
            }
            if narrowed:
                candidates = narrowed
        if len(candidates) == 1:
            ids = candidates
            resolved_in_group = True
    if len(ids) > 1:
        return RosterVerdict(name=name, legal=False, ambiguous=True,
                             reason="ambiguous", in_roster=in_roster,
                             stripped_text=stripped)
    person_id = next(iter(ids)) if ids else ""
    if not person_id and callable(person_id_of):
        try:
            person_id = str(person_id_of(name, platform=platform) or "").strip()
        except Exception:  # noqa: BLE001 - 认人失败只按名字比
            warn("core/context/group_members.py:537 Exception", err=repr(__import__("sys").exc_info()[1]))
            person_id = ""
    if not person_id:
        person_id = next(iter(_person_ids_of_name(
            name, platform=platform, meta_repo=meta_repo)), "")
    if not person_id:
        return RosterVerdict(name=name, legal=False, reason="unresolved",
                             stripped_text=stripped)
    identity_ids = set(roster.ids_of(person_id)) | {person_id}
    legal = bool(identity_ids & legal_ids)
    source = ""
    if in_roster:
        source = ROSTER_SOURCE_MEMBERSHIP
    elif trigger and trigger in identity_ids:
        source = ROSTER_SOURCE_TRIGGER
    elif identity_ids & set(inbound_ids):
        source = ROSTER_SOURCE_INBOUND
    elif identity_ids & set(recent):
        source = ROSTER_SOURCE_WINDOW
    recent_hit = bool(identity_ids & set(recent))
    plausible = recent_hit or bool(identity_ids & set(inbound_ids)) or (
        bool(trigger) and trigger in identity_ids)
    if not legal:
        return RosterVerdict(name=name, person_id=person_id, legal=False,
                             ambiguous=resolved_in_group,
                             reason="not_member", roster_source=source,
                             in_roster=in_roster, recent=recent_hit,
                             stripped_text=stripped)
    reason = "allowed" if plausible else "unlikely"
    if resolved_in_group:
        reason = "ambiguous_resolved_in_group"
    return RosterVerdict(name=name, person_id=person_id, legal=True,
                         ambiguous=resolved_in_group,
                         plausible=plausible, reason=reason,
                         roster_source=source, in_roster=in_roster,
                         recent=recent_hit)


def _extend_span_with_roster(
    text: str, roster: RosterSnapshot, start: int, end: int,
) -> tuple[str, int, int] | None:
    """把 canonical 切出来的 `@名字` 延长成**整串等于群名片**的那个名字。

    不是模糊匹配：候选必须与群名片逐字相等（casefold 后），只是允许名字本身带
    边界标点（实测群名片 ``Aaa.很傻的猪呗``，按标点切只得到 ``Aaa`` → 21 条
    合法成员被误判成 ``unresolved``）。招呼位的判定仍由 canonical
    ``_address_span`` 负责，这里只扩展长度，不新增「算不算点名」的规则。
    """
    raw = str(text or "")
    if not raw or start < 0 or start >= len(raw):
        return None
    lowered = raw.casefold()
    if len(lowered) != len(raw):
        lowered = raw.lower()
    best: tuple[str, int] | None = None
    for key in roster.by_name:
        if not key:
            continue
        candidate_end = start + 1 + len(key)
        if candidate_end <= end or candidate_end > len(raw):
            continue
        if lowered[start:candidate_end] != "@" + key:
            continue
        if best is None or candidate_end > best[1]:
            best = (raw[start + 1:candidate_end], candidate_end)
    if best is None:
        return None
    return best[0], start, best[1]


def _strip_span(text: str, start: int, end: int) -> str:
    """剥掉点名：用 canonical ``_drop_address``（连名字和后随空白一起去）。"""
    from core.speech.resolver import _drop_address

    return _drop_address(str(text or ""), start, end)


def _strip(text: str, name: str) -> str:
    """去掉首个招呼位的 ``@name``（连名字一起去）。"""
    from core.speech.resolver import _address_span, _drop_address

    span = _address_span(text)
    if span is None or span[0] != name:
        return str(text or "")
    _n, start, end = span
    return _drop_address(str(text or ""), start, end)
