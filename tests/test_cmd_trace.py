# -*- coding: utf-8 -*-
"""A1-d 命令链接线：``record_execution`` 接到**真实执行落点**的回归用例。

对应 `docs/archive/任务清单_2026-10-06.md:43`：「把 ``record_execution`` 接到真实执行落点，让
『谁下达 → 执行到哪』在真实流量上可查」，以及它给的三条判据：

  ① ``authorization_records`` 有行
  ② ``command_records.execution_ids`` 非空
  ③ ``terminal_events.authorization_id`` 非空

接线落点是 ``agents/base.py::BaseAgent.act`` 里那次 ``await tool.execute(...)``
（工具真正被跑的那一处），成功与失败两条路径都经过新增的
``_trace_command_execution``；判定与落库全在 ``core/terminal/cmd_trace.py``。

本文件覆盖五件事：

  ① **成功执行** → 三条判据**同时**成立（逐条断言，不是只测一个）；
  ② **失败执行** → 同样可查（"执行到哪" = 到失败那一步 + 原因）；
  ③ **幂等** → 重复记录不产生第二行授权 / 不换 execution_id / 不插第二条事件行；
  ④ ``YINYUE_FEATURE_CMD_TRACE=off`` → 与接线前**逐字节一致**
     （``record_execution`` / ``record_authorization`` 零调用，库里一个字节都不多）；
  ⑤ 既有 ``test_event_chain_links`` 的语义不回归（口径由那个文件自己守，这里守
     接线后**同一条链**仍然按老板 → 命令 → 执行三张表 join 得上）。

测试卫生：**关**一律显式写 ``off``（"env 未设" ≠ "关"）；本开关在 import 期钉死，
每个用例自己再钉一次；所有库路径在 import 前隔离，绝不碰生产库。

隔离走**唯一入口** ``tests/_tempdirs.py``（``temp_base()`` / ``make_db_dir()``，照
``tests/test_work_item_p1.py`` / ``tests/test_hard_rules_gate.py`` 的写法）：本文件此前
自建 ``data/_cmd_trace_test`` 一套（在生产 ``data/`` 下建 scratch，属于第三套口径），
现在 meta 库、账号库目录（``PLATFORMS_DIR``）、``DATA_DIR``、日志目录全部落在基座下；
本用例链路真正写的那张库是**每条用例自带的 meta 库**（``authorization_records`` /
``command_records`` / ``terminal_events`` 都是 meta schema 的表，见
``db/meta_models.py``），由 ``run_migrations`` 在那个临时文件上建出来。

Run directly:
    python tests/test_cmd_trace.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent

# 临时目录的唯一入口（见 tests/_tempdirs.py）：导入即把本进程的
# TEMP/TMP/TMPDIR/SQLITE_TMPDIR 钉到基座——SQLite 的 GetTempPath 与 jieba 的缓存
# 都按它走，只换个地方建目录是治标。
from _tempdirs import make_db_dir, temp_base  # noqa: E402

# 日志目录也要隔离：core.infra.logging.init_logging 会真写盘（同 test_work_item_p1）。
_TEST_LOG_DIR = str(temp_base() / "cmd_trace_logs")
Path(_TEST_LOG_DIR).mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = _TEST_LOG_DIR
# 数据库隔离（照 tests/test_work_item_p1.py）：任何一处走
# core.bootstrap.init_singletons() 都会在**真实** meta 库上跑 init_meta_db +
# run_migrations，账号库还会被 AccountDBManager.sync_to_registry → register 迁移
# （见 db/account_db_manager.py:32-37）。所以三处一起、在 import core.* 之前指到
# 隔离根：meta 库、账号库目录（PLATFORMS_DIR 决定 `get_account_db_path`）、DATA_DIR。
_ISO_ROOT = temp_base() / "cmd_trace_iso"
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")

# ---------------------------------------------------------------------------
# 开关纪律：**关**必须显式 ``off``，且依赖的每一个开关都要在 import 期钉死。
# ---------------------------------------------------------------------------
CMD_TRACE_ENV = "YINYUE_FEATURE_CMD_TRACE"
#: 本文件显式钉死的开关（"关"= 显式 off，不靠"env 未设"）。
_PINNED_OFF = (
    CMD_TRACE_ENV,
    # 事件流是这三条判据的载体，必须确定开着（默认 on，这里写明）。
)
os.environ[CMD_TRACE_ENV] = "off"
#: 事件流已无开关（恒开）：本文件的三条判据都要有事件行。

import core.infra.logging as _logging  # noqa: E402

_logging.init_logging = lambda *args, **kwargs: None

from sqlalchemy import text  # noqa: E402

from agents.base import AgentContext, BaseAgent  # noqa: E402
from agents.conversational import ConversationalAgent  # noqa: E402
from agents.tools.base import BaseTool, ToolRegistry  # noqa: E402
from core import features  # noqa: E402
from core.config.settings import config as _config  # noqa: E402
from core.terminal import cmd_trace as ct  # noqa: E402
from core.terminal.command_ledger import CommandStatus  # noqa: E402
from core.terminal.command_runtime import (  # noqa: E402
    install_terminal_command_service,
    reset_terminal_command_service,
)
from core.terminal.command_service import (  # noqa: E402
    CommandService,
    command_recorder_for_service,
)
from core.terminal.event_model import EventType  # noqa: E402
from core.terminal.event_runtime import (  # noqa: E402
    install_terminal_event_store,
    reset_terminal_event_store,
)
from core.terminal.event_store_db import TerminalEventStore  # noqa: E402
from core.terminal.identity_service import IdentityService  # noqa: E402
from core.terminal.principal import RelationLevel, RelationshipState  # noqa: E402
from core.terminal.shadow import record_message_events  # noqa: E402
from db.meta_repo import MetaRepositoryManager  # noqa: E402
from db.migrations import run_migrations  # noqa: E402

W = "wechat"
OWNER_CHAT = "owner_raw"
OWNER_MSG_ID = "m_cmd_trace_1"
OWNER_TEXT = "帮我订明天上午的会议室"

# 守卫：env 只在 core.config.settings 首次导入前设好才生效。隔离失效时硬失败，
# 绝不静默把测试写进生产库（同 tests/test_agent_tool_calling.py / test_work_item_p1.py）。
# 三条都断言：本链路会经 init_singletons() 碰 meta 库，也会经 AccountDBManager 注册
# 账号库——只守 meta 一条，账号库仍可能落到生产 `data/platforms/**/account.db`。
assert os.path.realpath(str(_config.paths.meta_db_path)) != os.path.realpath(
    str(ROOT / "data" / "yinyue_meta.db")
), f"测试数据库隔离失效：meta_db_path={_config.paths.meta_db_path}"
assert os.path.realpath(str(_config.paths.platforms_dir)) != os.path.realpath(
    str(ROOT / "data" / "platforms")
), f"测试账号库目录隔离失效：platforms_dir={_config.paths.platforms_dir}"
for _ac in _config.all_account_configs():
    _ac_db = Path(str(_ac.db_path)).resolve()
    assert _ac_db.is_relative_to(_ISO_ROOT), (
        f"账号库 {_ac.platform}/{_ac.account_id} 不在隔离根内：{_ac_db}（根 {_ISO_ROOT}）"
    )


# --------------------------------------------------------------------- helpers
class _FakeTool(BaseTool):
    """真执行、可控成功/失败的工具（不碰任何外部系统）。"""

    name = "book_room"
    description = "订会议室"
    parameters = {"room": {"type": "string", "description": "会议室", "required": True}}

    def __init__(self):
        self.calls = 0
        self.fail_flag = False
        self.error_text = ""

    async def execute(self, **kwargs):
        from agents.tools.base import ToolResult

        self.calls += 1
        if self.fail_flag:
            return ToolResult("", error=self.error_text)
        return ToolResult("已订好 3 楼会议室", data={"room": kwargs.get("room")})

    def make_failing(self, error: str = "会议室已被占用") -> "_FakeTool":
        self.fail_flag = True
        self.error_text = error
        return self


class _RaisingTool(_FakeTool):
    """工具**抛异常**的路径（与"返回 error"是两条不同的分支）。"""

    name = "notify_group"

    async def execute(self, **kwargs):
        self.calls += 1
        raise RuntimeError("上游 503")


def _make_temp_dir() -> Path:
    """每条用例一个新鲜目录（唯一入口的 ``make_db_dir``）。

    不再在 ``data/`` 下自建 scratch：那是第三套口径，也正是"临时目录回落进仓库"
    那条老路的形状（见 ``tests/_tempdirs.py`` 的文件头）。
    """
    return make_db_dir("cmd_trace_")


def _owner_identity() -> IdentityService:
    return IdentityService(
        relationships={
            (W, "bot_a", "p_owner"): RelationshipState(
                W, "bot_a", "p_owner", RelationLevel.OWNER, 1.0, "seed"
            ),
        },
        person_identities={(W, "owner_raw"): "p_owner"},
    )


def _scalar(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


class _Harness:
    """一个隔离库上的完整链路：采集 → 命令投影 → 真实工具执行。"""

    def __init__(self, tmp: Path, *, text: str = OWNER_TEXT, msg_id: str = OWNER_MSG_ID):
        self.tmp = tmp
        self.db_path = tmp / "cmd_trace.db"
        self.meta = MetaRepositoryManager(str(self.db_path))
        run_migrations(str(self.db_path))
        self.engine = self.meta.engine
        self.identity = _owner_identity()
        self.service = CommandService(
            self.identity, repository=_command_repo(self.engine)
        )
        self.store = TerminalEventStore(self.engine)
        install_terminal_command_service(self.service)
        install_terminal_event_store(self.store)
        self.msg_id = msg_id
        self.text = text
        self.event_id = ""
        self.turn_id = ""
        self.command_id = ""

    # -------------------------------------------------------------- 采集侧
    def ingest(self, *, require_command: bool = True) -> str:
        """一条主人消息进事件流 + 命令投影；返回命令 id（普通消息返回 ""）。"""
        raw = {
            "msg_id": self.msg_id,
            "chat_id": OWNER_CHAT,
            "chat_type": "private",
            "sender_id": OWNER_CHAT,
            "sender_name": "老板",
            "content": self.text,
            "created_at": "2026-01-01T10:00:00",
            "source": W,
        }
        events = record_message_events(
            [raw],
            platform=W,
            account_id="bot_a",
            identity_service=self.identity,
            store=self.store,
            command_recorder=command_recorder_for_service(
                self.service, self.identity, path=None
            ),
            return_events=True,
        )
        assert len(events) == 1, events
        self.event_id = str(events[0].event_id)
        # 权威 turn_id / command_id 都从库里读回来（不猜、不从进程内变量取）
        stored = self.store.get(self.event_id)
        self.turn_id = str(getattr(stored, "turn_id", "") or "")
        self.command_id = str(getattr(stored, "command_id", "") or "")
        assert self.event_id and self.turn_id, (self.event_id, self.turn_id)
        if require_command:
            assert self.command_id, (
                f"这条消息没有被命令投影收录（{self.text!r}）：command_id 为空"
            )
        else:
            assert not self.command_id, self.command_id
        return self.command_id

    # ------------------------------------------------------------ 执行侧环境
    def context(self) -> AgentContext:
        """照着 scheduler 那一份 extra 造（A1-c 的两个键就是这一回合的链接手柄）。"""
        return AgentContext(
            extra={
                "user_message": self.text,
                "platform": W,
                "account_id": "bot_a",
                "chat_id": OWNER_CHAT,
                "sender_id": OWNER_CHAT,
                "sender_name": "老板",
                "is_group": False,
                "turn_id": self.turn_id,
                "parent_event_id": self.event_id,
            },
            trigger="user_command",
        )

    def agent(self, *tools: BaseTool) -> ConversationalAgent:
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        agent = ConversationalAgent()
        agent.set_tool_registry(registry)
        return agent

    def run_tool(self, tool: BaseTool, ctx: AgentContext, **args):
        agent = self.agent(tool)
        return asyncio.run(agent.act(tool.name, args or {"room": "3楼"}, ctx))

    # -------------------------------------------------------------- 判据读数
    def auth_rows(self, command_id: str) -> int:
        return int(
            _scalar(
                self.engine,
                "SELECT COUNT(*) FROM authorization_records WHERE command_id = :cid",
                cid=command_id,
            )
            or 0
        )

    def execution_ids_in_db(self, command_id: str) -> tuple[str, ...]:
        import json

        raw = _scalar(
            self.engine,
            "SELECT execution_ids_json FROM command_records WHERE command_id = :cid",
            cid=command_id,
        )
        return tuple(json.loads(raw or "[]") or ())

    def event_rows(self, command_id: str) -> int:
        return int(
            _scalar(
                self.engine,
                "SELECT COUNT(*) FROM terminal_events WHERE command_id = :cid",
                cid=command_id,
            )
            or 0
        )

    def auth_id_on_events(self, command_id: str) -> str:
        return str(
            _scalar(
                self.engine,
                "SELECT authorization_id FROM terminal_events "
                " WHERE command_id = :cid AND authorization_id != '' LIMIT 1",
                cid=command_id,
            )
            or ""
        )

    def close(self) -> None:
        reset_terminal_command_service()
        reset_terminal_event_store()
        try:
            self.meta.engine.dispose()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                self.db_path.with_name(self.db_path.name + suffix).unlink(
                    missing_ok=True
                )
            except PermissionError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)


def _command_repo(engine):
    from db.repo_commands import CommandRepository

    return CommandRepository(engine)


def _assert_three_criteria(
    h: _Harness, command_id: str, *, label: str
) -> tuple[str, tuple[str, ...]]:
    """逐条断言三条判据（**三条都要读，不许只测一个**），返回实测读数。"""
    # 前提：命令是主人下达的（否则不许有授权行）。
    issuer_role = str(
        _scalar(
            h.engine,
            "SELECT issuer_role FROM command_records WHERE command_id = :cid",
            cid=command_id,
        )
        or ""
    )
    assert issuer_role == "master", (label, issuer_role)

    # ① authorization_records 有行
    auth_rows = h.auth_rows(command_id)
    assert auth_rows == 1, f"{label} 判据①：authorization_records 行数={auth_rows}"

    # ② command_records.execution_ids 非空
    execution_ids = h.execution_ids_in_db(command_id)
    assert len(execution_ids) >= 1, f"{label} 判据②：execution_ids={execution_ids!r}"

    # ③ terminal_events.authorization_id 非空
    auth_id = h.auth_id_on_events(command_id)
    assert auth_id, f"{label} 判据③：terminal_events.authorization_id 为空"

    # 三条判据必须指向**同一个**授权（不是三套 id）
    db_auth_id = str(
        _scalar(
            h.engine,
            "SELECT authorization_id FROM authorization_records "
            " WHERE command_id = :cid LIMIT 1",
            cid=command_id,
        )
        or ""
    )
    assert auth_id == db_auth_id, (label, auth_id, db_auth_id)
    assert auth_id == ct.authorization_id_for(command_id), label

    # 执行 id 指向的事件行确实存在、且落在同一条命令上
    for event_id in execution_ids:
        event = h.store.get(event_id)
        assert event is not None, f"{label} 执行事件行不存在：{event_id}"
        assert event.command_id == command_id, (label, event_id, event.command_id)
        assert event.authorization_id == auth_id, (label, event_id)
    return auth_id, execution_ids


# ------------------------------------------------------------------ ① 成功执行
def test_success_execution_satisfies_three_criteria() -> None:
    """①成功执行 → 三条判据同时成立（逐条断言）。"""
    os.environ[CMD_TRACE_ENV] = "on"
    tmp = _make_temp_dir()
    h = _Harness(tmp)
    try:
        command_id = h.ingest()
        # 接线前：三条判据一条都不成立
        assert h.auth_rows(command_id) == 0
        assert h.execution_ids_in_db(command_id) == ()
        assert h.auth_id_on_events(command_id) == ""

        tool = _FakeTool()
        result = h.run_tool(tool, h.context(), room="3楼")
        assert not result.is_error and "已订好" in result.output, result
        assert tool.calls == 1

        auth_id, execution_ids = _assert_three_criteria(h, command_id, label="①成功")

        # 复用而**不是**补写：接线点找到的 execution_id 就是既有 owner
        # （``record_action_event``）写的那条 action 行——同一件事不说两遍。
        # 用 ⑤ 那条链的行数口径钉死：命令行上只有"触发消息行 + 那条 action 行"。
        assert len(execution_ids) == 1
        assert h.event_rows(command_id) == 2, h.event_rows(command_id)
        assert int(
            _scalar(
                h.engine,
                "SELECT COUNT(*) FROM terminal_events "
                " WHERE action_payload_json LIKE '%\"cmd_trace\": true%'",
            )
            or 0
        ) == 0, "接线补写了第二条执行行（应当复用既有 action 行）"

        # 「谁下达」= 命令行 + 触发消息行；「执行到哪」= 执行事件行（按 command_id join）
        joined = int(
            _scalar(
                h.engine,
                "SELECT COUNT(*) FROM terminal_events e "
                "  JOIN command_records c ON c.command_id = e.command_id "
                "  JOIN authorization_records a ON a.authorization_id = e.authorization_id "
                " WHERE c.command_id = :cid",
                cid=command_id,
            )
            or 0
        )
        assert joined >= 2, joined          # 触发消息行 + 执行行
        assert h.event_rows(command_id) >= 2

        # 命令状态推进到「执行过」
        command = CommandService(
            h.identity, repository=_command_repo(h.engine)
        ).get(command_id)
        assert command is not None and command.status == CommandStatus.EXECUTING
        assert command.authorization_id == auth_id
        print(f"    [①成功] auth_rows=1 execution_ids={len(execution_ids)} "
              f"events_with_auth={h.event_rows(command_id)} join={joined} "
              f"reused=True appended=0")
    finally:
        h.close()


# ------------------------------------------------------------------ ② 失败执行
def test_failed_execution_is_traceable_with_reason() -> None:
    """②失败执行 → 同样可查（到失败那一步 + 原因）。"""
    os.environ[CMD_TRACE_ENV] = "on"
    tmp = _make_temp_dir()
    h = _Harness(tmp, text="帮我把通知发到群里", msg_id="m_cmd_trace_fail")
    try:
        command_id = h.ingest()
        tool = _FakeTool().make_failing("会议室已被占用")
        result = h.run_tool(tool, h.context(), room="3楼")
        assert result.is_error and result.error == "会议室已被占用", result

        # 三条判据在**失败**路径上照样成立
        auth_id, execution_ids = _assert_three_criteria(h, command_id, label="②失败")

        # 「执行到哪」= 到失败那一步 + 原因：执行事件行上带着 ok=False / error
        execution = h.store.get(execution_ids[0])
        payload = dict(getattr(execution, "action_payload", {}) or {})
        assert payload.get("ok") is False, payload
        assert "会议室已被占用" in str(payload.get("error") or ""), payload
        assert execution.event_type == EventType.ACTION, execution.event_type

        # 命令状态如实推进到 FAILED（不是假装成功）
        command = CommandService(
            h.identity, repository=_command_repo(h.engine)
        ).get(command_id)
        assert command is not None and command.status == CommandStatus.FAILED
        assert command.authorization_id == auth_id
        print(f"    [②失败] status=failed ok={payload.get('ok')} "
              f"error={payload.get('error')!r} execution_ids={len(execution_ids)}")

        # 抛异常那条路径（工具 raise，不是返回 error）同样留痕
        h2_tmp = _make_temp_dir()
        h2 = _Harness(h2_tmp, text="帮我通知一下群", msg_id="m_cmd_trace_raise")
        try:
            cid2 = h2.ingest()
            raising = _RaisingTool()
            res2 = h2.run_tool(raising, h2.context(), room="3楼")
            assert res2.is_error and "503" in res2.error, res2
            _assert_three_criteria(h2, cid2, label="②异常")
            cid2_command = CommandService(
                h2.identity, repository=_command_repo(h2.engine)
            ).get(cid2)
            assert cid2_command is not None
            assert cid2_command.status == CommandStatus.FAILED
            print("    [②异常] status=failed 已留痕")
        finally:
            h2.close()
    finally:
        h.close()


# --------------------------------------------------------------------- ③ 幂等
def test_repeated_recording_is_idempotent() -> None:
    """③幂等：重复记录 → 不产生第二行 / 不覆盖既有 execution_id。"""
    os.environ[CMD_TRACE_ENV] = "on"
    tmp = _make_temp_dir()
    h = _Harness(tmp, text="帮我订明天下午的会议室", msg_id="m_cmd_trace_idem")
    try:
        command_id = h.ingest()
        ctx = h.context()
        tool = _FakeTool()
        h.run_tool(tool, ctx, room="3楼")
        auth_id, execution_ids = _assert_three_criteria(h, command_id, label="③首次")
        auth_rows_1 = h.auth_rows(command_id)
        events_1 = h.event_rows(command_id)

        # 用**另一个** CommandService 实例（从库里 reload）重复登记同一次执行：
        # 这正是"重放 / 重投 / 重跑"的形状，而不是同一个内存对象的空转。
        reloaded = CommandService(h.identity, repository=_command_repo(h.engine))
        install_terminal_command_service(reloaded)
        for _ in range(3):
            outcome = ct.record_tool_execution(
                context=ctx, action="book_room", ok=True, duration_ms=5
            )
            assert outcome.reason == "ok", outcome
            assert outcome.command_id == command_id, outcome
            assert outcome.authorization_id == auth_id, outcome
            assert outcome.wrote_authorization is False, outcome
            assert outcome.wrote_execution is True, outcome
            assert outcome.execution_ids == execution_ids, outcome
            # 重放时链接列已经同值 → link_event 一个字都不写（幂等的直接读数）
            assert outcome.linked_event_rows == 0, outcome

        # 不产生第二行授权 / 不换 execution_id / 不多出事件行
        assert h.auth_rows(command_id) == auth_rows_1 == 1, (
            h.auth_rows(command_id), auth_rows_1,
        )
        assert h.execution_ids_in_db(command_id) == execution_ids, (
            h.execution_ids_in_db(command_id), execution_ids,
        )
        assert h.event_rows(command_id) == events_1, (
            h.event_rows(command_id), events_1,
        )
        assert str(
            _scalar(
                h.engine,
                "SELECT COUNT(*) FROM authorization_records WHERE authorization_id = :aid",
                aid=auth_id,
            )
        ) == "1"
        assert ct.authorization_id_for(command_id) == auth_id

        # 确定性 id 的两个性质（本用例的幂等地基）：
        # ① 同样输入恒等（重复调用不换 id）；
        # ② 换一个 actions / 触发消息 → 另一个 id（不同执行不互相顶替）。
        assert ct.execution_event_id_for(command_id, "book_room", h.event_id) == \
            ct.execution_event_id_for(command_id, "book_room", h.event_id)
        assert ct.execution_event_id_for(command_id, "book_room", h.event_id) != \
            ct.execution_event_id_for(command_id, "send_message", h.event_id)
        assert ct.execution_event_id_for(command_id, "book_room", h.event_id) != \
            ct.execution_event_id_for(command_id, "book_room", "other-event")
        print(f"    [③幂等] 重复 3 次后 auth_rows={auth_rows_1} "
              f"execution_ids={len(execution_ids)} events={events_1}")
    finally:
        h.close()


# ------------------------------------------------------------ ④ off = 逐字节一致
def test_off_records_nothing_and_never_calls_record_execution() -> None:
    """④``off`` → 与接线前**逐字节一致**：零调用、库零新增。

    三条证据，缺一不可：

    1. **零调用**：把 ``core/terminal/cmd_trace._trace``（= 过了门之后**唯一**会碰
       仓库与事件流的那一段）换成计数器 → ``off`` 下必须 0 次。接线前那段代码不存在，
       所以"0 次"就是"与接线前逐字节一致"的直接取证；
    2. **两个 owner 零调用**：``CommandService.record_execution`` /
       ``record_authorization`` 直接计数（判据 ② 靠前者、判据 ① 靠后者）；
    3. **库零新增**：命令行上三个判据的读数全为空，且命令状态仍是 ``received``
       （没被接线推进过）。

    另外：只有**成功**路径写东西是不够的——失败路径（返回 error 与抛异常两种）
    在 ``off`` 下也必须一个字都不写。
    """
    os.environ[CMD_TRACE_ENV] = "off"
    tmp = _make_temp_dir()
    h = _Harness(tmp, text="帮我订明天的会议室并通知大家", msg_id="m_cmd_trace_off")
    try:
        assert ct.cmd_trace_mode() == features.MODE_OFF
        assert ct.cmd_trace_enabled() is False

        command_id = h.ingest()

        calls: list[str] = []
        trace_calls: list[str] = []
        real_record_execution = CommandService.record_execution
        real_record_authorization = CommandService.record_authorization
        real_trace = ct._trace

        def _spy_execution(self, *args, **kwargs):
            calls.append("record_execution")
            return real_record_execution(self, *args, **kwargs)

        def _spy_authorization(self, *args, **kwargs):
            calls.append("record_authorization")
            return real_record_authorization(self, *args, **kwargs)

        def _spy_trace(*args, **kwargs):
            trace_calls.append("_trace")
            return real_trace(*args, **kwargs)

        CommandService.record_execution = _spy_execution
        CommandService.record_authorization = _spy_authorization
        ct._trace = _spy_trace
        try:
            events_before = h.event_rows(command_id)
            tool = _FakeTool()
            result = h.run_tool(tool, h.context(), room="3楼")
            assert not result.is_error and tool.calls == 1, result

            # 失败路径（返回 error / 抛异常）在 off 下同样一个字都不写
            failing = _FakeTool().make_failing("订不到")
            assert h.run_tool(failing, h.context(), room="4楼").is_error
            assert h.run_tool(_RaisingTool(), h.context(), room="5楼").is_error

            # 直接用落点函数调：off 时**第一行就返回**，连 store / 判定都不进
            outcome = ct.record_tool_execution(
                context=h.context(), action="book_room", ok=True, duration_ms=1
            )
            assert outcome.mode == features.MODE_OFF and outcome.reason == "off"
            assert outcome.traced is False and outcome.execution_event_id == ""
            assert outcome.execution_ids == () and outcome.authorization_id == ""
        finally:
            CommandService.record_execution = real_record_execution
            CommandService.record_authorization = real_record_authorization
            ct._trace = real_trace

        assert trace_calls == [], f"off 时接线逻辑仍然跑了：{trace_calls}"
        assert calls == [], f"off 时 owner 被调用了：{calls}"

        # 库里与接线前逐字节一致：三条判据一条都不成立，命令状态没被推进
        assert h.auth_rows(command_id) == 0
        assert h.execution_ids_in_db(command_id) == ()
        assert h.auth_id_on_events(command_id) == ""
        # off 下**接线一个字节都没写**：命令行上仍然只有那条触发消息行
        # （三次执行各留的 action 行是既有 owner 写的，`command_id` 仍为空）。
        assert h.event_rows(command_id) == events_before == 1, h.event_rows(command_id)
        action_rows = int(
            _scalar(
                h.engine,
                "SELECT COUNT(*) FROM terminal_events "
                " WHERE command_id = '' AND event_type = 'action'",
            )
            or 0
        )
        assert action_rows == 3, f"三次执行各有 1 条既有 action 行，实得 {action_rows}"
        status = str(
            _scalar(
                h.engine,
                "SELECT status FROM command_records WHERE command_id = :cid",
                cid=command_id,
            )
            or ""
        )
        assert status == CommandStatus.RECEIVED.value, status
        # 事件流这一段与接线前同形：三次执行各留一条 action 行（既有 owner 写的），
        # 一条 cmd_trace 行都没有——证明多出来的只有"没写"。
        traced_rows = int(
            _scalar(
                h.engine,
                "SELECT COUNT(*) FROM terminal_events "
                " WHERE action_payload_json LIKE '%\"cmd_trace\": true%'",
            )
            or 0
        )
        assert traced_rows == 0, traced_rows
        print(f"    [④off] _trace×0 record_execution×0 record_authorization×0 "
              f"auth_rows=0 execution_ids=0 cmd_trace_rows=0 status=received "
              f"events={events_before}")
    finally:
        h.close()


def test_off_is_the_default_when_env_is_unset() -> None:
    """④b 默认值：``YINYUE_FEATURE_CMD_TRACE`` **未设**时也是 ``off``（回滚位）。"""
    env = {k: v for k, v in os.environ.items() if k != CMD_TRACE_ENV}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    probe = (
        "import sys; sys.path.insert(0, r'%s');"
        "from core.terminal.cmd_trace import cmd_trace_mode, cmd_trace_enabled;"
        "print('PROBE', cmd_trace_mode(), cmd_trace_enabled())" % ROOT
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE ")]
    assert line, proc.stdout
    assert line[-1].strip() == "PROBE off False", line[-1]


# ------------------------------------------------- ⑤ 既有链语义不回归（同库口径）
def test_existing_command_chain_semantics_still_hold() -> None:
    """⑤接线后同一条链仍然按 消息 → 命令 → 授权 → 执行 三张表 join 得上。"""
    os.environ[CMD_TRACE_ENV] = "on"
    tmp = _make_temp_dir()
    h = _Harness(tmp, text="帮我查一下明天的日程", msg_id="m_cmd_trace_chain")
    try:
        command_id = h.ingest()
        # A1-a：命令投影在采集侧就把 command_id 回填到了消息事件行
        message_event = h.store.get(h.event_id)
        assert message_event is not None
        assert message_event.command_id == command_id
        assert message_event.turn_id == h.turn_id

        tool = _FakeTool()
        h.run_tool(tool, h.context(), room="3楼")
        _assert_three_criteria(h, command_id, label="⑤同链")

        # 执行事件行与触发消息行在同一回合（A1-c 的链没被接线改坏）
        execution_ids = h.execution_ids_in_db(command_id)
        execution = h.store.get(execution_ids[0])
        assert execution.parent_event_id == h.event_id
        assert execution.turn_id == h.turn_id

        # query(command_id=…) 命中「谁下达」与「执行到哪」两类行
        rows = h.store.query(command_id=command_id)
        kinds = {row.event_type for row in rows}
        assert EventType.REQUEST in kinds or EventType.COMMAND in kinds, kinds
        assert EventType.ACTION in kinds, kinds
        print(f"    [⑤同链] rows={len(rows)} kinds={sorted(k.value for k in kinds)}")
    finally:
        h.close()


def test_no_command_means_no_trace_and_no_error() -> None:
    """边界：没有命令投影的回合（普通闲聊）→ 不接线、不报错、零写入。

    "谁下达"都没有的时候不许凭空造一条命令（那才是第二套 id 体系）——
    如实返回 ``no_command_id`` 并留一条 action 行（既有 owner 写的，与接线无关）。
    """
    os.environ[CMD_TRACE_ENV] = "on"
    tmp = _make_temp_dir()
    h = _Harness(tmp, text="今天天气不错啊", msg_id="m_cmd_trace_plain")
    try:
        assert h.ingest(require_command=False) == ""
        assert str(
            _scalar(h.engine, "SELECT COUNT(*) FROM command_records")
        ) == "0"
        tool = _FakeTool()
        ctx = h.context()
        result = h.run_tool(tool, ctx, room="3楼")
        assert not result.is_error and tool.calls == 1, result
        outcome = ct.record_tool_execution(
            context=ctx, action="book_room", ok=True, duration_ms=1
        )
        assert outcome.reason == "no_command_id", outcome
        assert outcome.command_id == "" and outcome.traced is False
        assert str(
            _scalar(h.engine, "SELECT COUNT(*) FROM command_records")
        ) == "0"
        assert str(_scalar(h.engine, "SELECT COUNT(*) FROM authorization_records")) == "0"
        # 事件流里只有入站消息行 + 既有 owner 写的 action 行；没有 cmd_trace 行
        assert str(
            _scalar(
                h.engine,
                "SELECT COUNT(*) FROM terminal_events "
                " WHERE action_payload_json LIKE '%\"cmd_trace\": true%'",
            )
        ) == "0"
        print("    [边界] 无命令 → reason=no_command_id 命令行 0 授权行 0")
    finally:
        h.close()
        os.environ[CMD_TRACE_ENV] = "off"


def test_shadow_mode_leaves_repository_untouched() -> None:
    """影子档：只判定 + 只留痕，库一个字节都不动（三态的第二档）。"""
    os.environ[CMD_TRACE_ENV] = "shadow"
    tmp = _make_temp_dir()
    h = _Harness(tmp, text="帮我把会议室订了", msg_id="m_cmd_trace_shadow")
    try:
        assert ct.cmd_trace_mode() == features.MODE_SHADOW
        assert ct.cmd_trace_enabled() is False
        command_id = h.ingest()
        events_before = h.event_rows(command_id)
        outcome = ct.record_tool_execution(
            context=h.context(), action="book_room", ok=True, duration_ms=1
        )
        assert outcome.mode == features.MODE_SHADOW, outcome
        assert outcome.reason == "shadow", outcome
        assert outcome.command_id == command_id, outcome
        assert outcome.wrote_authorization is False and outcome.wrote_execution is False
        assert h.auth_rows(command_id) == 0
        assert h.execution_ids_in_db(command_id) == ()
        assert h.event_rows(command_id) == events_before
    finally:
        h.close()
        os.environ[CMD_TRACE_ENV] = "off"


def main() -> bool:
    tests = [
        test_success_execution_satisfies_three_criteria,
        test_failed_execution_is_traceable_with_reason,
        test_repeated_recording_is_idempotent,
        test_off_records_nothing_and_never_calls_record_execution,
        test_off_is_the_default_when_env_is_unset,
        test_existing_command_chain_semantics_still_hold,
        test_no_command_means_no_trace_and_no_error,
        test_shadow_mode_leaves_repository_untouched,
    ]
    failed: list[str] = []
    for test in tests:
        os.environ[CMD_TRACE_ENV] = "off"
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            import traceback as _tb

            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
            _tb.print_exc()
        finally:
            os.environ[CMD_TRACE_ENV] = "off"
    print(f"cmd trace checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
