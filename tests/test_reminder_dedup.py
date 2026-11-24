"""提醒幂等：同一件事说两遍，不许变成两条提醒（2026-10-02 live 事故）。

事故（Lead 从生产日志逐行取证）
------------------------------
``logs/yinyue.log`` 里 2026-10-01T14:50:51Z 与 16:42:20Z 各**一次**
``agent_tool_call{tool: create_reminder}``（两次独立请求：22:50 那条
``@鲸鲸 明天八点叫我起床``、00:42 那条 —— ``fast_path_check`` 原话都在日志里），
两条都写 ``remind_at = 2026-10-02 08:00``；``work_item_created`` 给出
``legacy_reminder_id`` **5 / 6**。08:00 一到，``run_reminder_delivery`` 扫出**两条**
pending，各投一次（03:46:14 / 03:46:19，两个不同 id）⇒ 主人听到同一句两遍。

⇒ 不是发送层重投（两个 id 两条记录），是**创建层没有幂等**。

本文件锁的三件事
----------------
1. **创建点幂等**（``agents/tools/reminder.py::CreateReminderTool.execute``）：
   幂等键 = ``(source_chat_id, remind_at, normalize_dedup_text(content))``，
   仅当同键那条**仍 pending** 时复用（返回既有 id），不新建；
2. **不许错并**：不同 ``remind_at`` / 不同文本各自保留（不跨时刻、不做语义匹配）；
3. **不回归**：``YINYUE_FEATURE_REMINDER_DEDUP=off`` 时与接线前**逐字节一致**
   （两条照旧、投递两次）；替身 repo 没有查找能力时同理。

隔离：库是 ``create_all`` 出来的临时 meta 库，日志进 ``data/_test_logs_*``，
**绝不碰生产库**（``data/yinyue_meta.db``）。

变异自检（2026-10-02 实测，`.venv` 直跑本文件；每步都还原复绿）
----------------------------------------------------------------
1. ``agents/tools/reminder.py`` 里 ``dedup_mode = features.mode(...)`` 改成常量
   ``features.MODE_OFF``（= 去掉幂等判定）→ 红，**5** 条：
   ``test_same_request_twice_lands_one_row_and_one_delivery``（``[1, 2]``，正是事故里
   "5 / 6" 的形状）+ ``test_whitespace_only_difference_is_same_request`` +
   ``test_default_is_on_when_env_unset`` + ``test_shadow_audits_but_still_creates`` +
   ``test_reuse_does_not_write_a_second_projection``（``[1, 2]``）；
2. 键放宽成"只用文本（忽略 chat / due）"——
   ``db/repo_reminder.py::find_pending_dedup`` 的查询去掉会话条件、
   创建点传 ``chat_id=""`` → 红，**正好 1** 条：
   ``test_different_chat_is_not_merged``（跨会话错并）；
   还原 → 13/13 复绿。

Run directly:
    python tests/test_reminder_dedup.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
_TEST_LOG_DIR = str(Path("data") / "_test_logs_reminder_dedup")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = _TEST_LOG_DIR
_ISO_ROOT = (ROOT / "data" / "_reminder_dedup_test" / "_iso").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")

# ---------------------------------------------------------------------------
# 开关钉死（守卫 tests/test_env_flag_isolation.py §③ / 本仓库"已咬三次"的坑）
# ---------------------------------------------------------------------------
# 本应用里"env 未设" ≠ "关"（``.env`` 里开着多个开关，且 ``core.features.mode`` 的
# 未设值会落到**调用方默认值**）。所以每个被本文件触及的开关都在**导入期**显式表态：
#   * ``WORK_ITEM_LEDGER`` / ``WORK_ITEM_RUNS``：同一枚 work_item_write_failed 告警
#     的来源，开不开都不改写本文件的断言，但显式关掉才叫"与投影无关"；
#   * ``SPEAK_CONTRACT`` / ``META_GATE`` 等出稿闸门不在本文件的
#     判定链上（本文件不跑生成），不钉——钉了反而让人以为它们参与判定。
os.environ["YINYUE_FEATURE_WORK_ITEM_LEDGER"] = "off"
os.environ["YINYUE_FEATURE_WORK_ITEM_RUNS"] = "off"

# scheduler.main 导入期就会初始化日志：先拦掉，日志只进隔离目录。
import core.infra.logging as _logging  # noqa: E402

_logging.init_logging = lambda *args, **kwargs: None

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

import agents.reminder_trigger as rt  # noqa: E402
import scheduler.main as sm  # noqa: E402
from agents.tools.reminder import CreateReminderTool  # noqa: E402
from core import features  # noqa: E402
from db.meta_models import MetaBase, Reminder as ReminderRow  # noqa: E402
from db.repo_reminder import (  # noqa: E402
    PENDING_STATUS,
    ReminderRepository,
    normalize_dedup_text,
)

CHAT = "10000000001@chatroom"
PERSONA_ID = 7


def _next_due(base: datetime | None = None) -> datetime:
    """下一个 08:00（保证在未来）——事故那两条的到点时刻形状。"""
    now = base or datetime.now()
    due = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if due <= now:
        due += timedelta(days=1)
    return due


DUE = _next_due()
DUE_SHIFTED = DUE + timedelta(hours=1)         # 不同时刻：另一件合法的事
TEXT = "起床啦，八点整了"
TEXT_OTHER = "叫 Elaina 起床"                  # 事故里第二条的原文（语义同、文本不同）


# --------------------------------------------------------------------- 替身
class _FakePersonas:
    def __init__(self, pid: int = PERSONA_ID):
        self._pid = pid

    def list_active(self):
        return [SimpleNamespace(id=self._pid)]


class _NoFinderReminders:
    """照 ``tests/test_reminder_delivery.py::_FakeReminders`` 的极简替身：

    只有 ``add`` / ``count_pending_for_requester``，**没有** ``find_pending_dedup``
    ——老替身的行为必须逐字节不变（幂等只在真仓储上生效）。
    """

    def __init__(self):
        self.added = []

    def count_pending_for_requester(self, requester_id, status="pending"):
        return 0

    def add(self, reminder):
        reminder.id = len(self.added) + 1
        self.added.append(reminder)
        return reminder


class _Repo:
    """``reminders`` 挂真仓储（真 SQL 判定），``personas`` 是替身。"""

    def __init__(self, engine, reminders=None, pid: int = PERSONA_ID):
        self.personas = _FakePersonas(pid)
        self.reminders = reminders if reminders is not None else ReminderRepository(engine)


def _fresh_engine(name: str):
    path = _ISO_ROOT / f"{name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    MetaBase.metadata.create_all(engine)
    return engine


def _ctx(**extra_overrides):
    extra = {
        "platform": "wechat",
        "account_id": "wxid_seed02",
        "chat_id": CHAT,
        "is_group": True,
        "sender_id": "wxid_seed01",
        "sender_name": "Elaina",
    }
    extra.update(extra_overrides)
    return SimpleNamespace(
        extra=extra,
        messages=[],
        account_context=None,
        accounts={"wechat": "wxid_seed02"},
        owner_accounts={},
    )


def _tool(repo, ctx=None) -> CreateReminderTool:
    tool = CreateReminderTool(repo)
    tool.authoritative_is_owner = lambda: True      # 主人请求：不查白名单/配额
    tool.set_context(ctx or _ctx())
    return tool


def _create(tool: CreateReminderTool, **kwargs):
    kwargs.setdefault("content", TEXT)
    kwargs.setdefault("remind_at", DUE.strftime("%Y-%m-%d %H:%M"))
    kwargs.setdefault("mention", ["我"])           # 群提醒必须定好到点@谁（事故那两条都是"我"）
    return asyncio.run(tool.execute(**kwargs))


def _rows(engine, status: str = "") -> list[ReminderRow]:
    with Session(engine) as s:
        q = s.query(ReminderRow)
        if status:
            q = q.filter(ReminderRow.status == status)
        return list(q.order_by(ReminderRow.id).all())


def created_row_persona(engine, reminder_id: int) -> int:
    """从落库那行取回真实 ``persona_id``（不猜常量）。"""
    with Session(engine) as s:
        return int(s.get(ReminderRow, reminder_id).persona_id)


def _due_row(row_id: int = 1, *, content=None, remind_at=None, status=None):
    """一条"到点"的行（供投递扫描替身用）。"""
    return SimpleNamespace(
        id=row_id,
        content=content if content is not None else TEXT,
        remind_at=remind_at or (datetime.now() - timedelta(minutes=1)),
        status=status or PENDING_STATUS,
        repeat_rule=None,
        source_platform="wechat",
        source_account_id="wxid_seed02",
        source_chat_id=CHAT,
        source_chat_type="group",
        requester_id="wxid_seed01",
        requester_name="Elaina",
        requester_is_owner=1,
        mention_name=None,
        mention_names=json.dumps(["Elaina"], ensure_ascii=False),
        purpose="remind",
    )


class _ScanRepo:
    """``run_reminder_delivery`` 的替身：只喂 due 行，记下每条被投了几次。

    ``list_pending()`` 是**权威扫描口**（``db/repo_reminder.py``）：一条 pending 行
    = 一次投递机会。所以"只落 1 条"在这里的直接读数是"只有 1 次投递机会"。
    """

    def __init__(self, due):
        self._due = list(due)
        self.attempts: dict[int, int] = {}
        self.delivered: list[int] = []
        self.reminders = SimpleNamespace(
            list_pending=self.list_pending,
            record_attempt=self.record_attempt,
            mark_done=self.mark_done,
            mark_failed=lambda reminder_id: None,
        )

    def list_pending(self):
        return list(self._due)

    def record_attempt(self, reminder_id: int) -> int:
        self.attempts[reminder_id] = self.attempts.get(reminder_id, 0) + 1
        return self.attempts[reminder_id]

    def mark_done(self, reminder_id: int):
        self.delivered.append(reminder_id)
        self._due = [r for r in self._due if getattr(r, "id", None) != reminder_id]


def _deliver_twice_blocking():
    """投递扫描：每条到点行都会被投一次（与生产 ``run_reminder_delivery`` 同形）。"""

    async def fake_turn(**kwargs):
        return rt.ProactiveTurnResult(True, sent=True, reason="sent")

    async def run(repo):
        original = rt.run_reminder_turn
        rt.run_reminder_turn = fake_turn
        try:
            with _patch(sm, "get_repo", lambda: repo):
                await sm.run_reminder_delivery()
                await sm.run_reminder_delivery()
        finally:
            rt.run_reminder_turn = original

    return run


@contextmanager
def _patch(obj, name, value):
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)


# ------------------------------------------------------------------ ① 幂等
def test_same_request_twice_lands_one_row_and_one_delivery():
    """① 同一请求连续两次 → 只落 1 条，且只有 1 次投递机会（事故的正面）。"""
    engine = _fresh_engine("idempotent")
    try:
        repo = _Repo(engine)
        tool = _tool(repo)
        with capture_logs() as events:
            first = _create(tool)
            second = _create(tool)
        assert not first.is_error, first.error
        assert not second.is_error, second.error

        rows = _rows(engine)
        assert len(rows) == 1, [r.id for r in rows]        # 不许出现事故里的 5 / 6
        assert rows[0].status == PENDING_STATUS
        assert rows[0].content == TEXT                     # 存的是原文，不改写
        assert rows[0].remind_at == DUE
        assert first.data["id"] == second.data["id"] == rows[0].id

        # 留痕：含既有 id + 键的三元组
        reused = [e for e in events if e.get("event") == "reminder_dedup_reused"]
        assert len(reused) == 1, events
        ev = reused[0]
        assert ev["reminder_id"] == rows[0].id
        assert ev["chat_id"] == CHAT
        assert ev["due_at"] == DUE.isoformat()
        assert ev["normalized_text"] == TEXT

        # 投递机会 = 1（第二条没有成为新行 ⇒ 08:00 不会再说第二遍）
        scanned = _ScanRepo([_due_row(rows[0].id, remind_at=rows[0].remind_at)])
        assert len(scanned.list_pending()) == 1
        asyncio.run(_deliver_twice_blocking()(scanned))
        assert scanned.delivered == [rows[0].id]
        assert scanned.attempts == {rows[0].id: 1}
    finally:
        engine.dispose()


def test_whitespace_only_difference_is_same_request():
    """①b 只差**首尾**空白 = 同一件事（规范化去首尾 + 合并内部空白），不新建第二条。"""
    engine = _fresh_engine("whitespace")
    try:
        repo = _Repo(engine)
        tool = _tool(repo)
        assert not _create(tool, content=TEXT).is_error
        assert not _create(tool, content=f"  {TEXT}  ").is_error
        assert not _create(tool, content=f"{TEXT} ").is_error
        rows = _rows(engine)
        assert len(rows) == 1, [r.content for r in rows]
        assert rows[0].content == TEXT                     # 第一条的原文原样（不改写）
    finally:
        engine.dispose()


def test_internal_whitespace_difference_stays_two_rows():
    """①c 内部空白的差异**是文本差异**：两条都留（幂等键不许在这里猜"其实同一句"）。

    规范化只合并空白，不把「起床啦，八点整了」和「起床啦， 八点整了」判成同一件事
    ——后者可能真是一句不同的话，合并它是"错并"，比重复更糟（②的纪律）。
    """
    engine = _fresh_engine("inner_space")
    try:
        repo = _Repo(engine)
        tool = _tool(repo)
        assert not _create(tool, content=TEXT).is_error
        assert not _create(tool, content="起床啦， 八点整了").is_error
        rows = _rows(engine)
        assert len(rows) == 2, [r.content for r in rows]
        assert [r.content for r in rows] == [TEXT, "起床啦， 八点整了"]
    finally:
        engine.dispose()


# --------------------------------------------------------------- ② 不许错并
def test_different_due_or_text_are_not_merged():
    """② 不同 ``due_at`` / 不同文本 → 各自保留（不跨时刻、不做语义匹配）。"""
    engine = _fresh_engine("distinct")
    try:
        repo = _Repo(engine)
        tool = _tool(repo)
        a = _create(tool, content=TEXT)
        b = _create(tool, content=TEXT, remind_at=DUE_SHIFTED.strftime("%Y-%m-%d %H:%M"))
        c = _create(tool, content=TEXT_OTHER)              # 同一时刻、不同文本
        d = _create(tool, content=TEXT_OTHER, remind_at=DUE_SHIFTED.strftime("%Y-%m-%d %H:%M"))
        for result in (a, b, c, d):
            assert not result.is_error, result.error

        rows = _rows(engine)
        assert len(rows) == 4, [(r.id, r.content, r.remind_at) for r in rows]
        assert {r.id for r in rows} == {a.data["id"], b.data["id"], c.data["id"], d.data["id"]}
        assert [r.remind_at for r in rows].count(DUE) == 2   # 同一时刻两条不同文本
        assert [r.content for r in rows].count(TEXT) == 2    # 同一文本两个不同时刻
    finally:
        engine.dispose()


def test_different_chat_is_not_merged():
    """②b 跨会话不并：同一个到点时刻、同一段文本，两个会话各留一条。"""
    engine = _fresh_engine("cross_chat")
    try:
        repo = _Repo(engine)
        in_group = _tool(repo)
        in_private = _tool(repo, _ctx(chat_id="wxid_seed01", is_group=False))
        a = _create(in_group)
        b = _create(in_private)
        assert not a.is_error and not b.is_error
        rows = _rows(engine)
        assert len(rows) == 2, [(r.id, r.source_chat_id) for r in rows]
        assert {r.source_chat_id for r in rows} == {CHAT, "wxid_seed01"}
    finally:
        engine.dispose()


# ------------------------------------------------- ③ 已投递/已取消不参与复用
def test_delivered_or_cancelled_rows_are_not_reused():
    """③ 已投递（done）/ 已取消（failed）的行不参与复用 → 新建。"""
    engine = _fresh_engine("not_pending")
    try:
        repo = _Repo(engine)
        tool = _tool(repo)
        first = _create(tool)
        assert not first.is_error, first.error
        with Session(engine) as s:
            row = s.get(ReminderRow, first.data["id"])
            row.status = "done"                     # 话已经送出去了
            s.commit()

        again = _create(tool)
        assert not again.is_error, again.error
        assert again.data["id"] != first.data["id"]          # 不返回已投递那条
        assert [r.status for r in _rows(engine)] == ["done", PENDING_STATUS]

        with Session(engine) as s:                  # 已取消（failed）同样不复用
            row = s.get(ReminderRow, again.data["id"])
            row.status = "failed"
            s.commit()
        third = _create(tool)
        assert not third.is_error, third.error
        assert third.data["id"] not in (first.data["id"], again.data["id"])
        assert len(_rows(engine)) == 3
        # 只有最后那条还能被扫到（前两条已出局）
        assert [r.id for r in _rows(engine, PENDING_STATUS)] == [third.data["id"]]
    finally:
        engine.dispose()


def test_finder_only_matches_pending_status():
    """③b 查找判据本身：同键但非 pending 时 ``find_pending_dedup`` 返回 None。"""
    engine = _fresh_engine("finder_status")
    try:
        repo = _Repo(engine)
        tool = _tool(repo)
        created = _create(tool)
        reminders = repo.reminders
        hit = reminders.find_pending_dedup(
            created_row_persona(engine, created.data["id"]), CHAT, DUE, TEXT,
        )
        assert hit is not None and hit.id == created.data["id"]
        with Session(engine) as s:
            s.get(ReminderRow, created.data["id"]).status = "done"
            s.commit()
        assert reminders.find_pending_dedup(
            created_row_persona(engine, created.data["id"]), CHAT, DUE, TEXT,
        ) is None
    finally:
        engine.dispose()

def test_missing_finder_behaves_like_off():
    """替身 repo 没有查找能力（老 ``_FakeReminders``）→ 照旧新建第二条。"""
    reminders = _NoFinderReminders()
    repo = SimpleNamespace(personas=_FakePersonas(), reminders=reminders)
    tool = _tool(repo)
    first = _create(tool)
    second = _create(tool)
    assert not first.is_error and not second.is_error
    assert len(reminders.added) == 2
    assert first.data["id"] == 1 and second.data["id"] == 2




# --------------------------------------------------- ⑥ 复用不写第二份投影
def test_reuse_does_not_write_a_second_projection():
    """复用路径**不**再跑一遍 work_items 投影：一条提醒一个台账行。"""
    engine = _fresh_engine("projection")
    try:
        import core.tasks.store as work_item_store

        calls: list[int] = []
        original = work_item_store.record_created

        def spy(repo, reminder, **kwargs):
            calls.append(getattr(reminder, "id", None))
            return original(repo, reminder, **kwargs)

        repo = _Repo(engine)
        tool = _tool(repo)
        work_item_store.record_created = spy
        try:
            assert not _create(tool).is_error
            assert not _create(tool).is_error
        finally:
            work_item_store.record_created = original
        assert len(calls) == 1, calls
    finally:
        engine.dispose()


# ------------------------------------------------------------ 规范化函数本身
def test_normalizer_only_collapses_whitespace():
    assert normalize_dedup_text("  起床啦，八点整了 ") == "起床啦，八点整了"
    assert normalize_dedup_text("起床啦，\n八点整了") == "起床啦， 八点整了"
    assert normalize_dedup_text(None) == ""
    # 语义相同但文本不同 → 键不同（本函数**不做**语义/模糊匹配）
    assert normalize_dedup_text(TEXT) != normalize_dedup_text(TEXT_OTHER)
    # 内部空白差异保留（只合并、不删除）——空 key 不参与复用
    assert normalize_dedup_text("开 会") == "开 会"
    assert normalize_dedup_text("   ") == ""


def main() -> bool:
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
