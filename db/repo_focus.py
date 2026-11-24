"""N3 焦点状态存储：``session_focus`` 每会话一行的读写 API（P0-WP3）。

**这是全系统唯一允许落库的判定状态。**

``docs/话题簇与锚点规格.md`` §十二【已定 1】经 **R-T5** 修订为「**除焦点状态外
仍不落库**」：

* 话题簇 / 锚点 / 归属判定一律**读时派生**（canonical owner 是
  ``core/context/threads.py``，P1 落地），**不写库**；
* 只有"这一轮在聊哪件事（``current_topic``）、主角是谁
  （``current_anchor_person_id``）"这一行最小状态允许落库，且**每会话一行**
  （主键 ``(platform, account_id, chat_id)``）。

**降级契约（硬约束，调用方必须照此实现）**：**丢行只损失"一句确认"**。
本表缺失 / 表结构损坏 / 任何读失败 → :meth:`FocusStateRepository.get` 返回
``None``，**不抛异常**；调用方拿到 ``None`` 就退回"当轮判定"，**不得**因为状态
缺失而拒绝服务、也不得把缺失当成错误上报。写路径（``upsert``）失败照旧抛——
写不进去是配置/磁盘问题，需要被看见。

**这里的 ``revision`` 与"缓存失效"**：照 ``core/speech/store.py`` 的 revision
模式，``revision`` 是**单调递增计数**（不是 uuids）。读侧缓存自己的
``revision``，发现变了才重查；:meth:`FocusStateRepository.bump_revision` 只动
``revision``（**不动** ``updated_at``，两者语义不同：一个是"缓存该失效了"，
一个是"状态内容最后变更时间"）。``upsert`` 顺带把 ``revision`` +1。

时间语义：一律 **naive 本地墙钟**（与 ``work_items`` 同口径），naive/aware
混用会读出不同类型的对象，写入前统一折算成本地 naive。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from core import temporal
from db.meta_models import SessionFocusState
from db.repo_base import BaseRepository
from safelog import warn

log = structlog.get_logger()

__all__ = ["FocusState", "FocusStateRepository", "PARSE_FAILED"]

#: ``suspended_json`` 解析失败时的哨兵（调用方据此知道"挂起清单读不出来"）。
PARSE_FAILED = object()


def _now() -> datetime:
    """本地 naive 墙钟（与 ``work_items`` 的时间列同口径）。"""
    return datetime.now()


def _to_local_naive(value: datetime | None) -> datetime | None:
    """aware → 本地 naive；naive 原样透传；``None`` 透传。

    数据库列是 naive ``DATETIME``，塞 aware 进去会读回一个 aware 对象，
    于是同一个字段在不同路径上类型不同——这正是要在这里掐掉的漂移。
    """
    # 走唯一 owner（``core.temporal.to_naive_local``）——本文件**不许**再自写
    # ``astimezone().replace(tzinfo=None)``：`tests/test_time_convention.py` 的 AST 守卫
    # 会把它判红，且两份实现迟早漂移（`db/repo_commands.py:44` 是同一先例）。
    return temporal.to_naive_local(value)


@dataclass
class FocusState:
    """一行 ``session_focus`` 的**快照**（不是 ORM 行，脱离 Session 生命周期）。"""

    platform: str = ""
    account_id: str = ""
    chat_id: str = ""
    #: 当前焦点：一句话 / 关键词组（"在聊哪件事"）。
    current_topic: str = ""
    #: 当前锚点人（这一轮话题的主角；**不是**最近说话人）。可为空串 = unresolved。
    current_anchor_person_id: str = ""
    #: 上一个焦点（换话题时把旧焦点压到这里）。
    previous_topic: str = ""
    #: 挂起话题数组（原始 JSON 文本，与库内同形）。
    suspended_json: str = "[]"
    #: 当前焦点自何时起（naive 本地墙钟）。
    topic_since: datetime | None = None
    #: 状态内容最后变更时间（``bump_revision`` **不**改它）。
    updated_at: datetime | None = None
    #: 单调递增版本号，供读侧缓存失效。
    revision: int = 1

    @property
    def suspended(self) -> list[Any]:
        """解析后的挂起话题数组；JSON 坏 / 不是数组 → ``[]``（**不抛**）。"""
        parsed = self.suspended_parsed()
        return [] if parsed is PARSE_FAILED else parsed

    def suspended_parsed(self) -> list[Any]:
        """同 :attr:`suspended`，但解析失败返回 :data:`PARSE_FAILED` 哨兵。

        需要区分"确实没有挂起话题"与"挂起清单读坏了"的调用方用这个。
        """
        raw = (self.suspended_json or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            warn("db/repo_focus.py:108 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            return PARSE_FAILED
        return parsed if isinstance(parsed, list) else PARSE_FAILED


def normalize_suspended(value: Any) -> str:
    """把挂起话题列表序列化成库内文本（``None`` / 非列表 → ``[]``）。"""
    if value is None:
        return "[]"
    if isinstance(value, str):
        # 已经是 JSON 文本：能解析成数组就规范化，否则当单个话题文本处理。
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            warn("db/repo_focus.py:121 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            parsed = [value]
        if isinstance(parsed, list):
            return json.dumps(parsed, ensure_ascii=False)
        return json.dumps([parsed], ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False)
    return json.dumps([value], ensure_ascii=False)


class FocusStateRepository(BaseRepository):
    """``session_focus`` 的读写入口（构造见 :func:`get_focus_repo`）。

    * :meth:`get` —— **永不抛**（任何读失败 → ``None``；见模块 docstring 的降级契约）
    * :meth:`upsert` —— 每会话一行；新建 revision=1，更新 revision+1
    * :meth:`clear` —— 丢弃该会话的焦点行（丢行只损失"一句确认"）
    * :meth:`bump_revision` —— 只推 revision，供读侧缓存失效
    """

    # ------------------------------------------------------------------ 读
    def get(self, platform: str, account_id: str, chat_id: str) -> FocusState | None:
        """读一行焦点状态；**读不到返回 ``None``，绝不抛**。

        降级行为是本模块的硬约束：表不存在（老库 / 迁移未跑）、文件损坏、
        磁盘错误……一律视为"没有焦点状态"，调用方退回**当轮判定**。
        **不得**因缺失而拒绝服务。
        """
        try:
            with self._session() as session:
                row = session.get(
                    SessionFocusState, (platform or "", account_id or "", chat_id or "")
                )
                if row is None:
                    return None
                return self._snapshot(row)
        except SQLAlchemyError as exc:
            # 只在 debug 落地：状态缺失是**预期内的降级**，不是每轮都该报警的错。
            log.debug(
                "focus_state_unavailable",
                platform=platform,
                account_id=account_id,
                chat_id=chat_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None

    @staticmethod
    def _snapshot(row: SessionFocusState) -> FocusState:
        return FocusState(
            platform=row.platform or "",
            account_id=row.account_id or "",
            chat_id=row.chat_id or "",
            current_topic=row.current_topic or "",
            current_anchor_person_id=row.current_anchor_person_id or "",
            previous_topic=row.previous_topic or "",
            suspended_json=row.suspended_json or "[]",
            topic_since=row.topic_since,
            updated_at=row.updated_at,
            revision=int(row.revision or 0),
        )

    # ------------------------------------------------------------------ 写
    def upsert(
        self,
        platform: str,
        account_id: str,
        chat_id: str,
        *,
        current_topic: str = "",
        current_anchor_person_id: str = "",
        previous_topic: str | None = None,
        suspended: Any = None,
        topic_since: datetime | None = None,
        now: datetime | None = None,
        bump: bool = True,
    ) -> FocusState:
        """写一行焦点状态（存在则更新）。

        没传的字段**保持原值**（``None`` = "不动"）：``previous_topic`` /
        ``suspended`` / ``topic_since`` 都是这个口径，避免"只想换话题"的调用方
        把挂起清单顺手清空。要显式清空就传 ``""`` / ``[]``。

        ``suspended`` 接受列表（或已经是 JSON 文本的 ``str``）；落库统一走
        :func:`normalize_suspended`。``bump`` 只对**更新**有效（新建行恒为
        ``revision=1``）：默认 +1——写状态就是要让读侧缓存失效；批量导入这类
        "不需要每个中间态都让缓存失效"的场景传 ``bump=False``。
        """
        now = _to_local_naive(now) or _now()
        topic_since = _to_local_naive(topic_since)
        key = (platform or "", account_id or "", chat_id or "")
        with self._session() as session:
            row = session.get(SessionFocusState, key)
            if row is None:
                row = SessionFocusState(
                    platform=key[0],
                    account_id=key[1],
                    chat_id=key[2],
                    current_topic=current_topic or "",
                    current_anchor_person_id=current_anchor_person_id or "",
                    previous_topic=previous_topic or "",
                    suspended_json=normalize_suspended(suspended),
                    topic_since=topic_since or now,
                    updated_at=now,
                    revision=1,
                )
                session.add(row)
            else:
                row.current_topic = current_topic or ""
                row.current_anchor_person_id = current_anchor_person_id or ""
                if previous_topic is not None:
                    row.previous_topic = previous_topic
                if suspended is not None:
                    row.suspended_json = normalize_suspended(suspended)
                if topic_since is not None:
                    row.topic_since = topic_since
                row.updated_at = now
                if bump:
                    row.revision = int(row.revision or 0) + 1
            session.commit()
            session.refresh(row)
            return self._snapshot(row)

    def clear(self, platform: str, account_id: str, chat_id: str) -> bool:
        """丢弃该会话的焦点行；**返回是否真的删掉了一行，失败不抛**（同 get 口径）。

        下一次 :meth:`get` 返回 ``None`` → 调用方走"当轮判定"，行为与"从没有过
        焦点状态"完全一致（R-T5 的回滚面：焦点状态行保留但停读即退回当轮判定）。
        """
        try:
            with self._session() as session:
                result = session.execute(
                    delete(SessionFocusState).where(
                        SessionFocusState.platform == (platform or ""),
                        SessionFocusState.account_id == (account_id or ""),
                        SessionFocusState.chat_id == (chat_id or ""),
                    )
                )
                session.commit()
                return bool(result.rowcount)
        except SQLAlchemyError as exc:
            log.debug(
                "focus_state_clear_unavailable",
                platform=platform,
                account_id=account_id,
                chat_id=chat_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False

    def bump_revision(self, platform: str, account_id: str, chat_id: str) -> int:
        """只把 ``revision`` +1（**不动** ``updated_at``）；返回新值，无行返回 0，失败不抛。

        给读侧缓存失效用：缓存里存住上次见到的 ``revision``，发现变大就重读。
        "读不到 → 0" 与 :meth:`get` 的 ``None`` 同义（没有状态）。
        """
        try:
            with self._session() as session:
                session.execute(
                    update(SessionFocusState)
                    .where(
                        SessionFocusState.platform == (platform or ""),
                        SessionFocusState.account_id == (account_id or ""),
                        SessionFocusState.chat_id == (chat_id or ""),
                    )
                    .values(revision=SessionFocusState.revision + 1)
                )
                session.commit()
                value = session.execute(
                    select(SessionFocusState.revision).where(
                        SessionFocusState.platform == (platform or ""),
                        SessionFocusState.account_id == (account_id or ""),
                        SessionFocusState.chat_id == (chat_id or ""),
                    )
                ).scalar()
                return int(value or 0)
        except SQLAlchemyError as exc:
            log.debug(
                "focus_state_bump_unavailable",
                platform=platform,
                account_id=account_id,
                chat_id=chat_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return 0

    # ------------------------------------------------------------- 维护辅助
    def count(self, platform: str | None = None) -> int:
        """行数（运维/用例用；读失败同样不抛，返回 0）。"""
        try:
            with self._session() as session:
                query = select(func.count()).select_from(SessionFocusState)
                if platform is not None:
                    query = query.where(SessionFocusState.platform == platform)
                return int(session.execute(query).scalar() or 0)
        except SQLAlchemyError:
            warn("db/repo_focus.py:315 SQLAlchemyError", err=repr(__import__("sys").exc_info()[1]))
            return 0

    def list_since(self, since: datetime, limit: int = 100) -> Sequence[FocusState]:
        """``updated_at >= since`` 的焦点行（按更新时间倒序）；读失败返回空列表。"""
        try:
            with self._session() as session:
                rows = session.execute(
                    select(SessionFocusState)
                    .where(SessionFocusState.updated_at >= _to_local_naive(since))
                    .order_by(SessionFocusState.updated_at.desc())
                    .limit(limit)
                ).scalars().all()
                return [self._snapshot(row) for row in rows]
        except SQLAlchemyError:
            warn("db/repo_focus.py:329 SQLAlchemyError", err=repr(__import__("sys").exc_info()[1]))
            return []


def get_focus_repo(engine=None) -> FocusStateRepository:
    """取焦点状态仓库：**必须显式给 engine**（META 库的 engine）。

    P0-WP3 只落"状态载体"，**没有**消费方，所以这里不做隐式单例解析：
    ``db/`` 既不能反向 import ``core/``（`scripts/check_layering.py` 的 db→core
    禁令 + 避免 import 环），也不该按 env 路径偷偷新建一个库（会绕过
    ``core.bootstrap`` 的迁移顺序，正是 ENG-19 那类事故）。P1 接消费方时把
    META engine 传进来即可（``SpeechStore`` 的 ``get_speech_store(engine)`` 同形）。
    """
    if engine is None:
        raise RuntimeError(
            "focus repo needs an explicit engine (the META engine); "
            "P0-WP3 ships the state carrier only"
        )
    return FocusStateRepository(engine)
