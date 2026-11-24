# -*- coding: utf-8 -*-
"""记忆范围**数据层强制**守卫（R-I1/R-I2/R-I3 硬验收项：越界必须被拒）。

宪法级不变量（docs/需求契约.md §三）：查记忆只允许「该动作发起人 + 当前会话」——
**不可配置、不可修改、不在 UI 出现、不被任何规则覆盖**。契约把 R-I3 列为硬验收项
（"必须有『越界必须被拒』的守卫测试"），本文件就是那条用例。

覆盖：
① 同会话同人 → 命中；
② **别的群**里的观察 → 不返回；
③ **他人私聊**里的观察 → 不返回（哪怕那条私聊里提到了当事人）；
④ 缺 scope / 非法 scope → fail-closed（空 + warning），**不退回无过滤**；
⑤ 绕过 provider 直接调 repo 层也不能越界（行级条件在 SQL 里，不是调用方约定）；
⑥ parity：SQL 行级过滤与 ``core.people.visibility.observation_visible_to`` 对同一批
   夹具给出同一答案（含 owner 分支、sensitivity、作废状态、群→当事人私聊单向投递）。

Run directly:
    python tests/test_memory_scope.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
# 隔离前导（照 tests/test_reminder_mapping.py:34-41）：本文件只用显式 scratch 库，
# 但保持同一纪律——万一以后有断言走 bootstrap，也不会碰到生产库。
_TEST_LOG_DIR = str(Path("data") / "_test_logs_memory_scope")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_DIR", _TEST_LOG_DIR)
_ISO_ROOT = (ROOT / "data" / "_memory_scope_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(_ISO_ROOT / "platforms"))
os.environ.setdefault("DATA_DIR", str(_ISO_ROOT / "data"))

from core.contracts.events import ChatType  # noqa: E402
from core.contracts.person_memory import (  # noqa: E402
    ObservationKind,
    ObservationStatus,
    ObservationVisibility,
    PersonObservation,
)
from core.people.visibility import (  # noqa: E402
    VisibleScope,
    observation_in_scope,
    observation_visible_to,
)
from core.memory.providers import PersonMemoryProvider  # noqa: E402
from db.meta_models import init_meta_db  # noqa: E402
from db.repo_person_memory import PersonMemoryRepository  # noqa: E402

DB_PATH = ROOT / "data" / "_memory_scope_test" / "scope.db"
PRODUCTION_META_DB = (ROOT / "data" / "yinyue_meta.db").resolve()
ENV_MEMORY = "YINYUE_FEATURE_PERSON_MEMORY"
ENV_RECALL = "YINYUE_FEATURE_PERSON_RECALL"
assert Path(os.environ["META_DB_PATH"]).resolve() != PRODUCTION_META_DB, (
    "测试隔离失效：META_DB_PATH 指到了生产库"
)

NOW = datetime(2026, 10, 1, 12, 0)
TENANT = {"platform": "qq", "account_id": "bot_10001"}


# --------------------------------------------------------------------- fixtures
def _obs(
    observation_id: str,
    *,
    person_id: str = "qq_p",
    chat_type: ChatType,
    chat_id: str,
    visibility: ObservationVisibility = ObservationVisibility.GROUP_SHARED,
    content: str,
    speaker_person_id: str = "",
    mentioned: tuple[str, ...] = (),
    sensitivity: str = "normal",
    status: ObservationStatus = ObservationStatus.ACTIVE,
) -> PersonObservation:
    return PersonObservation(
        observation_id=observation_id,
        person_id=person_id,
        platform=TENANT["platform"],
        account_id=TENANT["account_id"],
        occurred_scope_type=chat_type,
        occurred_scope_id=chat_id,
        speaker_person_id=speaker_person_id,
        mentioned_person_ids=mentioned,
        source_evidence_id=f"ev_{observation_id}",
        source_message_id=f"msg_{observation_id}",
        observed_at=NOW,
        kind=ObservationKind.FACT,
        content=content,
        confidence=0.9,
        importance=0.5,
        visibility=visibility,
        sensitivity=sensitivity,
        status=status,
    )


def _fixtures() -> list[PersonObservation]:
    return [
        # ① 同会话同人：P 在本群说的
        _obs("obs_same_group", chat_type=ChatType.GROUP, chat_id="qq_group_1",
             content="P6在群里说明天有课", speaker_person_id="qq_p"),
        # ② 别的群里的观察（同一租户，另一个会话）
        _obs("obs_other_group", chat_type=ChatType.GROUP, chat_id="qq_group_2",
             content="P6在别的群说要去打球", speaker_person_id="qq_q"),
        # ③ 他人私聊里的观察：Q 私聊提到 P —— 正是绝不能外泄的那一类
        _obs("obs_other_private", chat_type=ChatType.PRIVATE, chat_id="qq_q",
             content="P6在 Q 的私聊里被提到", speaker_person_id="qq_q",
             mentioned=("qq_p",), visibility=ObservationVisibility.PRIVATE_ONLY),
        # 当事人自己私聊里说的（私聊会话内可见）
        _obs("obs_own_private", chat_type=ChatType.PRIVATE, chat_id="qq_p",
             content="P6私聊说喜欢冰美式", speaker_person_id="qq_p",
             visibility=ObservationVisibility.PRIVATE_ONLY),
        # 只给 owner 看的 / 敏感的：非 owner 一律看不到
        _obs("obs_owner_only", chat_type=ChatType.GROUP, chat_id="qq_group_1",
             content="P6的 owner_only 观察", speaker_person_id="qq_p",
             visibility=ObservationVisibility.OWNER_ONLY),
        _obs("obs_sensitive", chat_type=ChatType.GROUP, chat_id="qq_group_1",
             content="P6的敏感观察", speaker_person_id="qq_p", sensitivity="sensitive"),
        # 作废的观察：对谁都不可见
        _obs("obs_superseded", chat_type=ChatType.GROUP, chat_id="qq_group_1",
             content="P6的旧观察（已作废）", speaker_person_id="qq_p",
             status=ObservationStatus.SUPERSEDED),
        # 另一租户（同 platform、不同 account）：连租户都不匹配
        PersonObservation(
            observation_id="obs_other_tenant",
            person_id="qq_p",
            platform="qq",
            account_id="bot_99999",
            occurred_scope_type=ChatType.GROUP,
            occurred_scope_id="qq_group_1",
            speaker_person_id="qq_p",
            observed_at=NOW,
            kind=ObservationKind.FACT,
            content="P6在另一个账号的群里说",
            visibility=ObservationVisibility.GROUP_SHARED,
        ),
    ]


def _fresh_repo() -> tuple[object, PersonMemoryRepository]:
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    engine = init_meta_db(str(DB_PATH))
    repo = PersonMemoryRepository(engine)
    repo.save_many(_fixtures())
    return engine, repo


def _close(engine) -> None:
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)


def _ids(rows) -> set[str]:
    return {str(getattr(row, "observation_id", "")) for row in rows}


def p_private_scope() -> VisibleScope:
    return VisibleScope(
        platform="qq", account_id="bot_10001", requester_person_id="qq_p",
        chat_type=ChatType.PRIVATE, chat_id="qq_p",
    )


def p_group_scope(chat_id: str = "qq_group_1") -> VisibleScope:
    return VisibleScope(
        platform="qq", account_id="bot_10001", requester_person_id="qq_p",
        chat_type=ChatType.GROUP, chat_id=chat_id,
    )


class _RecordingLog:
    """Records warning calls so "空 + 日志" can be asserted, not assumed."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def warning(self, event: str = "", **fields) -> None:
        self.calls.append({"event": event, **fields})

    def info(self, event: str = "", **fields) -> None:
        self.calls.append({"event": event, **fields})

    def reasons(self) -> list[str]:
        return [str(call.get("reason") or "") for call in self.calls]


def _principal(person_id: str, *, chat_type: ChatType, chat_id: str,
               is_group: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        person_id=person_id,
        platform=TENANT["platform"],
        bot_account_id=TENANT["account_id"],
        chat_id=chat_id,
        chat_type=chat_type,
        is_group=is_group,
    )


def _flags():
    # 兄弟开关一并显式表态（守卫 tests/test_env_flag_isolation.py §③）：本文件测的是
    # "写侧开 × 读侧开"下的可见范围，抽取（LLM）与聚合不参与——钉在缺省位，不吃 .env。
    return patch.dict(os.environ, {
        ENV_MEMORY: "1",
        ENV_RECALL: "1",
        "YINYUE_FEATURE_PERSON_LLM": "0",
        "YINYUE_FEATURE_PERSON_PROFILE_AGGREGATE": "0",
    }, clear=False)


# ------------------------------------------------------------------ ①②③ 越界
def test_same_scope_hits_and_other_scopes_never_return() -> None:
    """① 同会话同人命中；② 别的群不返回；③ 他人私聊不返回（provider 层）。

    口径说明：``GROUP_SHARED`` 的"群 → **当事人**私聊"单向投递是契约明写的
    （``core/contracts/person_memory.py:42-45``：可在发生它的群里用、可注入当事人
    私聊、**绝不许漏进别的群或别人的私聊**）。所以"别的群"的越界断言落在**群视图**
    上；私聊视图里那两条群行是契约允许的投递，不是本次要堵的越界。
    """
    engine, repo = _fresh_repo()
    provider = PersonMemoryProvider(repository=repo)
    try:
        with _flags():
            private_items = provider.recall(
                _principal("qq_p", chat_type=ChatType.PRIVATE, chat_id="qq_p"),
                "P6",
                limit=20,
            )
            group_items = provider.recall(
                _principal("qq_p", chat_type=ChatType.GROUP, chat_id="qq_group_1",
                           is_group=True),
                "P6",
                limit=20,
            )
            other_group_items = provider.recall(
                _principal("qq_p", chat_type=ChatType.GROUP, chat_id="qq_group_2",
                           is_group=True),
                "P6",
                limit=20,
            )
    finally:
        _close(engine)

    private_ids = {item.memory_id for item in private_items}
    group_ids = {item.memory_id for item in group_items}
    other_group_ids = {item.memory_id for item in other_group_items}

    # ① 命中：本群那条；群里只许出现本群发生的行（② 跨群必须被拒）
    assert "person:obs_same_group" in private_ids, private_ids
    assert group_ids == {"person:obs_same_group"}, group_ids
    assert other_group_ids == {"person:obs_other_group"}, other_group_ids
    assert "person:obs_same_group" not in other_group_ids
    assert "person:obs_other_group" not in group_ids

    # ③ 他人私聊：Q 私聊里提到 P，任何视图都不许出现
    assert "person:obs_other_private" not in private_ids, private_ids
    assert "person:obs_other_private" not in group_ids, group_ids
    assert "person:obs_other_private" not in other_group_ids, other_group_ids
    # 当事人自己私聊那条只在私聊视图里（契约的单向投递方向）
    assert "person:obs_own_private" in private_ids, private_ids
    assert "person:obs_own_private" not in group_ids, group_ids

    # owner_only / 敏感 / 作废 / 别租户：非 owner 的任何视图都看不到
    for forbidden in ("person:obs_owner_only", "person:obs_sensitive",
                      "person:obs_superseded", "person:obs_other_tenant"):
        assert forbidden not in private_ids, (forbidden, private_ids)
        assert forbidden not in group_ids, (forbidden, group_ids)
        assert forbidden not in other_group_ids, (forbidden, other_group_ids)

    # 返回项必须带可见性（不许留空 scope 给下游权限门）
    for item in private_items:
        assert item.visibility_scope, item


def test_direct_repository_access_cannot_cross_scopes() -> None:
    """⑤ 绕过 provider 直接调 repo 层：行级过滤仍在 SQL 里生效。"""
    engine, repo = _fresh_repo()
    try:
        private_rows = repo.list_for_person("qq_p", scope=p_private_scope())
        group_rows = repo.list_for_person("qq_p", scope=p_group_scope())
        other_group_rows = repo.list_for_person("qq_p", scope=p_group_scope("qq_group_2"))
    finally:
        _close(engine)

    # 群视图：只本群行，一行都不许串（② 跨群在 SQL 层被排除）
    assert _ids(group_rows) == {"obs_same_group"}
    assert _ids(other_group_rows) == {"obs_other_group"}
    # 私聊视图：当事人自己私聊 + 契约允许的"本人相关 group_shared"投递
    assert _ids(private_rows) == {"obs_same_group", "obs_other_group", "obs_own_private"}
    for rows in (private_rows, group_rows, other_group_rows):
        assert "obs_other_private" not in _ids(rows), _ids(rows)
        assert "obs_other_tenant" not in _ids(rows), _ids(rows)
        assert "obs_owner_only" not in _ids(rows), _ids(rows)
        assert "obs_superseded" not in _ids(rows), _ids(rows)


def test_requester_dimension_is_enforced_not_just_chat() -> None:
    """同一间私聊里，换发起人也不能读出"不是他的"观察（发起人维度）。"""
    engine, repo = _fresh_repo()
    try:
        q_in_own_chat = repo.list_for_person(
            "qq_p",
            scope=VisibleScope(
                platform="qq", account_id="bot_10001", requester_person_id="qq_q",
                chat_type=ChatType.PRIVATE, chat_id="qq_q",
            ),
        )
        stranger_in_group = repo.list_for_person(
            "qq_p",
            scope=VisibleScope(
                platform="qq", account_id="bot_10001", requester_person_id="qq_r",
                chat_type=ChatType.GROUP, chat_id="qq_group_1",
            ),
        )
    finally:
        _close(engine)
    # Q 在自己私聊里问 P：P 的私聊（qq_p 那一间）不许出现，只许"与 Q 相关"的群行
    assert "obs_own_private" not in _ids(q_in_own_chat), _ids(q_in_own_chat)
    # 群里问的是本群发生的行（本群行对在场的人可见），别的会话一律没有
    assert _ids(stranger_in_group) == {"obs_same_group"}, _ids(stranger_in_group)


# ------------------------------------------------------------- ④ fail-closed
def test_missing_scope_fails_closed_with_warning() -> None:
    """④ 缺 scope → 空 + warning（不许退回无过滤）。"""
    import db.repo_person_memory as repo_mod

    engine, repo = _fresh_repo()
    recorder = _RecordingLog()
    try:
        with patch.object(repo_mod, "log", recorder):
            assert repo.list_for_person("qq_p") == []
            assert repo.list_for_person("qq_p", scope=None) == []
    finally:
        _close(engine)
    assert recorder.reasons() == ["missing_scope", "missing_scope"], recorder.calls


def test_invalid_scope_fails_closed_with_warning() -> None:
    """④ 非法 scope（缺租户/缺发起人/群没有会话键/不是 scope 类型）→ 空 + warning。"""
    import db.repo_person_memory as repo_mod

    engine, repo = _fresh_repo()
    recorder = _RecordingLog()
    bad_scopes = [
        VisibleScope(platform="", account_id="bot_10001", requester_person_id="qq_p"),
        VisibleScope(platform="qq", account_id="", requester_person_id="qq_p"),
        VisibleScope(platform="qq", account_id="bot_10001", requester_person_id=""),
        VisibleScope(platform="qq", account_id="bot_10001", requester_person_id="qq_p",
                     chat_type=ChatType.GROUP, chat_id=""),
        VisibleScope(platform="qq", account_id="bot_10001", requester_person_id="qq_p",
                     chat_type=ChatType.UNKNOWN, chat_id="qq_group_1"),
        {"platform": "qq", "account_id": "bot_10001"},
    ]
    try:
        with patch.object(repo_mod, "log", recorder):
            results = [repo.list_for_person("qq_p", scope=scope) for scope in bad_scopes]
    finally:
        _close(engine)
    assert results == [[] for _ in bad_scopes], results
    assert recorder.reasons() == [
        "missing_platform", "missing_account_id", "missing_requester_person_id",
        "missing_group_chat_id", "unknown_chat_type", "invalid_scope_type",
    ], recorder.calls


def test_provider_without_usable_scope_returns_empty_and_logs() -> None:
    """④ provider 层同样 fail-closed：principal 缺 platform/account/发起人就拒读。"""
    import core.memory.providers as providers_mod

    engine, repo = _fresh_repo()
    recorder = _RecordingLog()
    provider = PersonMemoryProvider(repository=repo)
    bad_principals = [
        SimpleNamespace(person_id="qq_p"),  # 没有 platform / account
        SimpleNamespace(person_id="qq_p", platform="qq"),  # 没有 account
        SimpleNamespace(platform="qq", bot_account_id="bot_10001"),  # 没有发起人
        SimpleNamespace(person_id="qq_p", platform="qq", bot_account_id="bot_10001",
                        chat_id="", group_id="", chat_type=ChatType.GROUP),  # 群没会话键
    ]
    try:
        with _flags(), patch.object(providers_mod, "log", recorder):
            results = [provider.recall(principal, "P6", limit=20)
                       for principal in bad_principals]
    finally:
        _close(engine)
    assert results == [[] for _ in bad_principals], results
    # 没有 person_id 的那个在进入 scope 之前就返回空（本来就没有可查的人），
    # 所以只有 3 条告警；其余每个未通过 scope 校验的 principal 各一条。
    assert recorder.reasons() == [
        "missing_scope", "missing_scope", "missing_group_chat_id",
    ], recorder.calls


# ------------------------------------------------------------------ ⑥ parity
def _parity_scopes() -> list[VisibleScope]:
    return [
        p_private_scope(),
        p_group_scope(),
        p_group_scope("qq_group_2"),
        VisibleScope(platform="qq", account_id="bot_10001", requester_person_id="qq_q",
                     chat_type=ChatType.PRIVATE, chat_id="qq_q"),
        VisibleScope(platform="qq", account_id="bot_10001", requester_person_id="qq_p",
                     chat_type=ChatType.CHANNEL, chat_id="qq_channel_1"),
        VisibleScope(platform="qq", account_id="bot_10001", requester_person_id="qq_p",
                     chat_type=ChatType.PRIVATE, chat_id="qq_p", viewer_is_owner=True),
    ]


def test_sql_row_filter_matches_the_pure_visibility_rule() -> None:
    """⑥ 行级 SQL 与 core.people.visibility 同一答案（防止两套口径漂移）。"""
    engine, repo = _fresh_repo()
    try:
        for scope in _parity_scopes():
            rows = repo.list_for_person("qq_p", scope=scope)
            got = _ids(rows)
            for observation in _fixtures():
                if observation.person_id != "qq_p":
                    continue
                expected = observation_in_scope(observation, scope)
                assert expected == (observation.observation_id in got), (
                    scope, observation.observation_id, expected, sorted(got)
                )
    finally:
        _close(engine)


def test_pure_rule_and_scope_agree_on_both_axes() -> None:
    """⑥ 抽查：同一个观察在两个会话里的答案，纯函数与 VisibleScope 包装一致。"""
    by_id = {row.observation_id: row for row in _fixtures()}
    scope = p_private_scope()
    for observation_id in ("obs_same_group", "obs_other_group", "obs_other_private"):
        observation = by_id[observation_id]
        direct = observation_visible_to(
            observation, platform=scope.platform, account_id=scope.account_id,
            chat_type=scope.chat_type, chat_id=scope.effective_chat_id(),
            viewer_person_id=scope.requester_person_id,
        )
        assert direct == observation_in_scope(observation, scope), observation_id
    assert observation_in_scope(by_id["obs_same_group"], None) is False


def main() -> int:
    tests = [
        test_same_scope_hits_and_other_scopes_never_return,
        test_direct_repository_access_cannot_cross_scopes,
        test_requester_dimension_is_enforced_not_just_chat,
        test_missing_scope_fails_closed_with_warning,
        test_invalid_scope_fails_closed_with_warning,
        test_provider_without_usable_scope_returns_empty_and_logs,
        test_sql_row_filter_matches_the_pure_visibility_rule,
        test_pure_rule_and_scope_agree_on_both_axes,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            import traceback

            print(f"  FAIL {test.__name__}: {exc!r}")
            traceback.print_exc()
    print(f"memory scope checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
