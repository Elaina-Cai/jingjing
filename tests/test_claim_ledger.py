# -*- coding: utf-8 -*-
"""M2-4 claim 台账（事实权威）：**三级权威 / 更正历史 / 复述抑制 / unfounded_claim 可拦**。

对应 `docs/archive/任务清单_2026-10-06.md:71`（M2-4 的六条硬要求）与 `docs/decisions/回答架构评审.md:156`
（落点 `core/context/message_facts.py`）与 `docs/decisions/回答架构评审.md:179`（M2-1 ④ 的改期
条款："台账落地后把 ``unfounded_claim`` 从**只检测**升级为**可拦**"）。

**判据换了**：从"像不像编造"（形态无穷，只能枚举词表）换成"**有没有 claim 支撑**"
（有限、可校验 = 字段词表 + 三级权威 + 更正链）。

样本全部取自 **2026-09-30 21:44–21:47 群 10000000001@chatroom 的真实事故**
（`docs/archive/任务清单_2026-10-06.md:71` 的"直接缺口"三条）与 **18:01 的活体样本**（"五块三"）：

* 21:44:20 她（转述）：「刚刚@我那位报的籍贯我记着了：长沙、株洲、湘潭。」
* 21:44:57 P6（转述者）撤回：「我瞎说的」→ 她 21:44:20 的登记**必须随之作废**
* 21:44:59 P3（**自报**）：「湖南长沙」
* 21:45:20 她：「P3 你是广东人吧」→ **必须被 21:44:59 的自报打掉**
* 21:46:55 她：「籍贯我记着了：长沙、株洲、湘潭、岳阳」→ **不得再发等价名单**
* 18:01:32 她：「不过一天五块三的吃法你是真敢」→ 本轮**没有任何来源**（
  ``llm_tool_call calls=0``，出口闸门 ``gate rule=-`` 连命中都没有）→ 之后必须判
  **"无 claim 支撑"**，且**不许靠词表枚举形态**。

**import 期钉死**（本文件 §纪律：断言"关"必须显式 ``off``，且钉了兄弟组一个开关就得
钉死整组——出站闸门现在只剩 ``YINYUE_FEATURE_OUTPUT_GATE`` 一个开关）。

Run directly:
    python tests/test_claim_ledger.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# --- import 期钉死：每一个本文件依赖的开关都显式表态（"env 未设"≠"关"）--------
# 台账三态：off（默认，回滚位）——绝大多数用例自己再按需覆盖。
os.environ["YINYUE_FEATURE_CLAIM_LEDGER"] = "off"
# 出站闸门只剩 OUTPUT_GATE（ADDRESSEE_GUARD 随 R0 删门移除；tests/test_env_flag_isolation.py §③）：
# 钉一个就得钉整组，否则生产 .env 会改写"关 = 旧行为"的断言。
os.environ["YINYUE_FEATURE_OUTPUT_GATE"] = "off"
# 隔离：库/日志/平台目录都改道，绝不碰生产库（新守卫 assert_migrations_allowed
# 撞到就是提醒在用生产路径——本文件根本不走 bootstrap 的迁移入口）。
ROOT = Path(__file__).resolve().parent.parent
_ISO_ROOT = (ROOT / "data" / "_claim_ledger_test").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("LOG_DIR", str(ROOT / "data" / "_test_logs_claim_ledger"))

from sqlalchemy import create_engine  # noqa: E402

from core import features  # noqa: E402
from core.context import message_facts as facts  # noqa: E402
from core.speech import guards  # noqa: E402
from core.speech.guards import (  # noqa: E402
    GATE_MODE_OFF,
    GATE_MODE_ON,
    GATE_MODE_SHADOW,
    GATE_SEVERITY_BLOCK,
    GATE_SEVERITY_WATCH,
    GateConfig,
    claim_ledger_inputs,
    run_output_gate,
)
from db import migrations  # noqa: E402
from db.meta_models import (  # noqa: E402
    ClaimLedgerAuditRow,
    ClaimLedgerRow,
    MetaBase,
)

# ---------------------------------------------------------------------------
# 真实样本（2026-09-30 21:44–21:47 + 18:01）
# ---------------------------------------------------------------------------
CHAT = "10000000001@chatroom"
PURE_CHAT = "claim_ledger_pure@chatroom"      # 纯函数用例专用（熔断按 chat_id 计数）
ANAHIDA = "wxid_seed03"                      # 21:44:59 自报「湖南长沙」的人
XIAOYU = "wxid_seed06"                        # 21:44:57 撤回「我瞎说的」的转述者
SELF = "wxid_seed01"                          # 她

RELAY_2144 = "刚刚@我那位报的籍贯我记着了：长沙、株洲、湘潭。"
RETRACT_214457 = "……我瞎说的，别当真。"
SELFREPORT_214459 = "我是湖南长沙的。"
WRONG_2145 = "P3 你是广东人吧，刚才报的籍贯里没有你。"
REPEAT_2146 = "籍贯我记着了：长沙、株洲、湘潭、岳阳。"
LIVE_1801 = "不过一天五块三的吃法你是真敢，@Elaina"
LIVE_1802 = "你记错了吧，不是我说的5.3一天"
LIVE_1802_QUOTE = "你原话是「一天十几块怎么吃饭堂」，五块三是我自己脑补的。"


# ---------------------------------------------------------------------------
# 隔离设施
# ---------------------------------------------------------------------------
def _fresh_path(name: str) -> Path:
    path = _ISO_ROOT / f"{name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return path


def _create_all_engine(path: Path):
    engine = create_engine(f"sqlite:///{path}")
    MetaBase.metadata.create_all(engine)
    return engine


def _table_shape(path: Path, table: str):
    con = sqlite3.connect(str(path))
    try:
        columns = [
            (row[1], str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
            for row in con.execute(f"PRAGMA table_info({table})")
        ]
        named, shapes = [], []
        for row in con.execute(f"PRAGMA index_list({table})"):
            name, unique = row[1], int(row[2])
            cols = tuple(info[2] for info in con.execute(f"PRAGMA index_info({name})")
                         if info[2] is not None)
            shapes.append((unique, cols))
            if not name.startswith("sqlite_autoindex"):
                named.append(name)
        return columns, sorted(named), sorted(shapes)
    finally:
        con.close()


def _tables(path: Path) -> set[str]:
    con = sqlite3.connect(str(path))
    try:
        return {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _stamped(path: Path) -> set[int]:
    con = sqlite3.connect(str(path))
    try:
        return {row[0] for row in con.execute("SELECT version FROM _schema_version")}
    finally:
        con.close()


def _legacy_library(name: str, baseline: int) -> Path:
    """老库 = create_all 建好、抹掉两张 claim 表、把版本压回 ``baseline``。"""
    path = _fresh_path(name)
    engine = _create_all_engine(path)
    engine.dispose()
    con = sqlite3.connect(str(path))
    try:
        con.execute("DROP TABLE IF EXISTS claim_ledger")
        con.execute("DROP TABLE IF EXISTS claim_ledger_audit")
        con.execute(
            "CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER PRIMARY KEY, "
            "description TEXT, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
        con.execute("DELETE FROM _schema_version")
        con.execute("INSERT OR REPLACE INTO _schema_version (version, description) "
                    "VALUES (?, ?)", (baseline, "test baseline"))
        con.commit()
    finally:
        con.close()
    return path


@contextmanager
def _env(**pairs):
    """临时设置三态开关（值 "off"/"shadow"/"on"），退出时连"未设置"状态一起还原。"""
    saved = {key: os.environ.get(key) for key in pairs}
    try:
        for key, value in pairs.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _ledger() -> facts.ClaimLedger:
    """**写路径**用的台账：显式绑定当前隔离库（读侧也走同一个库，不回落生产库）。"""
    return facts.ClaimLedger(meta_repo=_Repo(_db()))


_DB_ENGINE = None


def _db():
    """当前隔离库 engine（**每个写路径用例先调 :func:`_reset_db`**）。"""
    global _DB_ENGINE
    if _DB_ENGINE is None:
        _DB_ENGINE = _create_all_engine(_fresh_path("shared"))
    return _DB_ENGINE


def _reset_db():
    """每个"写路径"用例自己一张干净库（PRAGMA 之外不共享状态）。

    **必须**在用例开头调用：``create_all`` 不会删掉上一个用例插入的行，而权威序
    判定完全依赖"这个 (chat, person, field) 上现在有什么"——不重置就是脏读。
    """
    global _DB_ENGINE
    if _DB_ENGINE is not None:
        _DB_ENGINE.dispose()
    _DB_ENGINE = _create_all_engine(_fresh_path("shared"))
    return _DB_ENGINE


def _records(**rows) -> facts.ClaimLedger:
    """注入式台账（读侧用例：不碰库、不依赖迁移）。"""
    out = []
    for index, row in enumerate(rows.get("items", ()), start=1):
        out.append(facts.ClaimRecord(
            id=index, platform="wechat", account_id="bot",
            chat_id=row.get("chat_id", CHAT), person_key=row["person"],
            field_key=row["field"], value=row["value"],
            authority=row["authority"], source_actor_id=row.get("actor", ""),
            source_claim_id=int(row.get("source_claim_id", 0)),
            superseded_by=int(row.get("superseded_by", 0)),
            withdrawn_at=str(row.get("withdrawn_at", "")),
            created_at=row.get("at", "2026-09-30 21:44:20"),
        ))
    return facts.ClaimLedger(out)


def _claim(value: str, authority: str, *, person: str = ANAHIDA,
           field_key: str = "籍贯", actor: str = "") -> facts.ClaimCandidate:
    return facts.ClaimCandidate(
        field_key=field_key, value=value, authority=authority,
        person_key=person, evidence=f"籍贯是{value}",
        source_chat_id=CHAT, source_message_id="m1",
        source_actor_id=actor or person)


class _Repo:
    """极小 meta repo 壳（只有 ``engine``）——给"裸库"用例用。"""

    def __init__(self, engine) -> None:
        self.engine = engine


def _guard_inputs(text: str, ledger: facts.ClaimLedger,
                  *, chat_id: str = PURE_CHAT):
    """把注入式台账折进闸门 config（绕开库，只测判定链）。"""
    probe = facts.evaluate_claims(text, chat_id=chat_id, person_keys=(ANAHIDA,),
                                  ledger=ledger)
    unsupported = tuple({
        "field": verdict.field_key,
        "value": verdict.assertion.value,
        "evidence": verdict.assertion.evidence,
        "reason": verdict.reason,
    } for verdict in probe.unsupported)
    # 本壳模拟**真接缝在台账 on 时**的产物：除了 findings 还要给处置许可
    # （``claim_ledger_enforce``，2026-10-10 新增）。shadow / 手工注入"只记不动"
    # 的回归见 test_shadow_ledger_finding_is_recorded_but_never_rewrites。
    return GateConfig(unsupported_claims=unsupported,
                      claim_ledger_enforce=True)


def _code_only(path) -> str:
    """一个 .py 文件里**可执行代码**的文本（注释与 docstring 都去掉）。

    结构守卫只该盯代码：注释里必须留着事故原句（"五块三"）给后人看，断言不能逼着
    后人删掉那段记录。
    """
    import ast

    tree = ast.parse(Path(path).read_text(encoding="utf-8-sig"))
    parts: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:]
        parts.append(ast.unparse(node))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# ① 三级权威：自报 > 转述 > 推测（三格 + 低权威试图覆盖高权威 → 拒绝）
# ---------------------------------------------------------------------------
def test_authority_order_is_self_report_then_relay_then_speculation():
    """三格读数：同一 ``(chat, person, field)`` 上，权威序 = 自报 > 转述 > 推测。"""
    assert facts.AUTHORITY_LEVELS == ("self_report", "relay", "speculation")
    assert (facts.rank_of("self_report") > facts.rank_of("relay")
            > facts.rank_of("speculation")), facts.AUTHORITY_RANK

    # 三格：各自单独存在时都能成为权威值。
    for authority, value in (("self_report", "湖南长沙"), ("relay", "湖南长沙"),
                             ("speculation", "湖南长沙")):
        ledger = _records(items=[{"person": ANAHIDA, "field": "籍贯",
                                  "value": value, "authority": authority}])
        resolved = ledger.resolve(CHAT, ANAHIDA, "籍贯")
        assert resolved.has_claim and resolved.authority == authority, resolved

    # 三格同时在 → 自报胜（哪怕它最早写下：权威序不是"最新的赢"）。
    ledger = _records(items=[
        {"person": ANAHIDA, "field": "籍贯", "value": "湖南长沙",
         "authority": "self_report", "at": "2026-09-30 21:44:59"},
        {"person": ANAHIDA, "field": "籍贯", "value": "广东",
         "authority": "relay", "at": "2026-09-30 21:45:20"},
        {"person": ANAHIDA, "field": "籍贯", "value": "湖北",
         "authority": "speculation", "at": "2026-09-30 21:46:00"},
    ])
    resolved = ledger.resolve(CHAT, ANAHIDA, "籍贯")
    assert resolved.authority == "self_report", resolved.to_dict()
    assert resolved.value == "湖南长沙", resolved.to_dict()
    assert {item.authority for item in resolved.conflicts} == {"relay", "speculation"}
    print("  ok 三级权威三格 + 自报胜出（权威序 ≠ 最新序）")


def test_low_authority_is_kept_as_parallel_not_rejected():
    """**低权威不再被拒**（2026-10-16「写时不承诺」）：并列留档 + 审计一行 ``parallel``。

    旧口径把「低权威想覆盖高权威」整条丢掉 —— 于是「谁在什么时候转述过什么」永久消失。
    新口径：**都留着**，读时再排序（权威序只是默认透镜，不是写入许可）。
    ``off`` 档（回滚位）仍是旧口径，见下一条用例。
    """
    engine = _reset_db()
    with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
        outcome, claim_id = _ledger().record(
            _claim("湖南长沙", "self_report"), engine=engine)
        assert outcome == facts.RecordOutcome.INSERTED, outcome
        assert claim_id > 0

        # 转述（21:45:20 那一格的形态）想盖掉自报 → **并列**在案。
        outcome, relay_id = _ledger().record(_claim("广东", "relay"), engine=engine)
        assert outcome == facts.RecordOutcome.PARALLEL, outcome
        assert relay_id > claim_id, "返回的是**新行**的 id（它真的落库了）"

        resolved = _ledger().resolve(CHAT, ANAHIDA, "籍贯")
        assert resolved.value == "湖南长沙" and resolved.authority == "self_report", (
            resolved.to_dict())
        # 并列的说法读得到（不再是一个标量说了算）。
        assert [item.value for item in resolved.conflicts] == ["广东"], resolved.to_dict()
        assert [item.authority for item in resolved.conflicts] == ["relay"]
        assert resolved.conflicts[0].active, "并列的那行仍然有效（不是被更新掉）"
        assert resolved.active_conflicts == tuple(resolved.conflicts)

        # 推测更不行 → 同样并列留档。
        outcome, guess_id = _ledger().record(_claim("湖北", "speculation"), engine=engine)
        assert outcome == facts.RecordOutcome.PARALLEL, outcome
        assert guess_id > relay_id

        # 权威序 = 默认透镜（旧读数逐字不变）；换一种排序 = 另一种读数（写入侧没变）。
        assert _ledger().resolve(CHAT, ANAHIDA, "籍贯").value == "湖南长沙"
        recency = _ledger().resolve(CHAT, ANAHIDA, "籍贯", order="recency")
        assert recency.order == "recency" and recency.value == "湖北", recency.to_dict()

    with engine.connect() as conn:
        from sqlalchemy import text as _sql

        audit = conn.execute(_sql(
            "select action, existing_claim_id, existing_authority, reason"
            " from claim_ledger_audit where action = 'parallel' order by id"
        )).mappings().all()
        total = conn.execute(_sql("select count(*) as n from claim_ledger")).scalar()
    assert len(audit) == 2, audit
    assert all(int(row["existing_claim_id"]) == claim_id for row in audit), audit
    assert all(row["existing_authority"] == "self_report" for row in audit), audit
    assert total == 3, "并列也落库：谁说过什么不丢"
    print(f"  ok 低权威并列留档 2 次（权威透镜回自报 / recency 回最新，审计 {len(audit)} 行）")


def test_off_mode_still_rejects_low_authority():
    """``off``（默认档 = 回滚位）= 逐字旧行为：低权威仍早退 ``REJECTED``、不落库、不并列。"""
    engine = _reset_db()
    with _env(YINYUE_FEATURE_CLAIM_LEDGER="off"):
        outcome, claim_id = _ledger().record(
            _claim("湖南长沙", "self_report"), engine=engine)
        assert outcome == facts.RecordOutcome.INSERTED, outcome
        outcome, kept = _ledger().record(_claim("广东", "relay"), engine=engine)
        assert outcome == facts.RecordOutcome.REJECTED, outcome
        assert kept == claim_id, "旧口径返回的是**仍然有效的那条**的 id"
    with engine.connect() as conn:
        from sqlalchemy import text as _sql

        total = conn.execute(_sql("select count(*) as n from claim_ledger")).scalar()
        rejected = conn.execute(_sql(
            "select count(*) as n from claim_ledger_audit where action = 'rejected'"
        )).scalar()
    # off = 零 IO：拒绝不落库、**也不留痕**（``_audit`` 在非 tracing 时直接 return）。
    assert total == 1 and rejected == 0, (total, rejected)
    print("  ok off：低权威仍被拒（1 行台账、零留痕）")



def test_higher_authority_does_override_lower_authority():
    """合法方向：**高权威覆盖低权威** = SUPERSEDED（不是拒绝）。"""
    engine = _reset_db()
    with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
        outcome, low_id = _ledger().record(_claim("湖北", "speculation"), engine=engine)
        assert outcome == facts.RecordOutcome.INSERTED, outcome
        outcome, high_id = _ledger().record(_claim("湖南长沙", "self_report"),
                                            engine=engine)
        assert outcome == facts.RecordOutcome.SUPERSEDED, outcome
        assert high_id != low_id

        history = _ledger().history(CHAT, ANAHIDA, "籍贯")
        old = [row for row in history if row.id == low_id][0]
        assert old.superseded_by == high_id, old
        assert old.value == "湖北", "**旧正文不许被改写**"
        assert old.active is False
        assert _ledger().resolve(CHAT, ANAHIDA, "籍贯").value == "湖南长沙"

        # 同权威同值 → 幂等（同一句话复述不该在台账里长成两行）。
        outcome, same = _ledger().record(_claim("湖南长沙", "self_report"),
                                         engine=engine)
        assert outcome == facts.RecordOutcome.DUPLICATE, outcome
        assert same == high_id, (same, high_id)
    print("  ok 高权威覆盖低权威 = SUPERSEDED（低权威那一侧改由并列留档承接）")


# ---------------------------------------------------------------------------
# ② 更正历史：旧 claim 标 superseded_by、原文不被改写、更正链可回查
# ---------------------------------------------------------------------------
def test_correction_history_is_explicit_and_never_rewrites_text():
    """每条 claim 带来源与时间；被更新的旧 claim 显式标 ``superseded_by``。"""
    engine = _reset_db()
    with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
        candidate = _claim("湖南长沙", "self_report")
        _, first = _ledger().record(candidate, engine=engine,
                                    now="2026-09-30 21:44:59")
        _, second = _ledger().record(
            _claim("湖南", "self_report"), engine=engine, now="2026-09-30 22:10:00")

    with engine.connect() as conn:
        from sqlalchemy import text as _sql

        rows = conn.execute(_sql(
            "select id, value, authority, observed_at, created_at, superseded_by,"
            " supersede_reason, superseded_at from claim_ledger order by id"
        )).mappings().all()
    assert len(rows) == 2, rows
    old, new = rows
    assert int(old["id"]) == first and int(new["id"]) == second
    # 更正链：旧行显式指向新行 + 原因 + 时间（三列齐）。
    assert int(old["superseded_by"]) == second, old
    assert old["supersede_reason"], old
    assert str(old["superseded_at"] or ""), old
    # **原文不被改写**：旧行的值/权威/来源时间与写入时逐字相同。
    assert old["value"] == "湖南长沙", old
    assert old["authority"] == "self_report", old
    assert str(old["observed_at"]) == "2026-09-30 21:44:59", old
    # 新行没被反向标记（更正链是有向的：旧 → 新）。
    assert int(new["superseded_by"]) == 0, new
    print(f"  ok 更正历史：#{first} superseded_by=#{second}，旧正文逐字不变")


def test_retraction_withdraws_own_claim_and_downstream():
    """21:44:57 转述者P6撤回「我瞎说的」→ 她 21:44:20 的登记**随之作废**。"""
    engine = _reset_db()
    with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
        # P6 21:44:20 的转述（她替P6登记的那条，来源 = P6那句）。
        _, upstream = _ledger().record(facts.ClaimCandidate(
            field_key="籍贯", value="长沙、株洲、湘潭", authority="relay",
            person_key=ANAHIDA, evidence="他跟我说他是长沙人",
            source_chat_id=CHAT, source_message_id="m2144",
            source_actor_id=XIAOYU), engine=engine, now="2026-09-30 21:44:20")
        # 21:44:20 她的登记（下游：来源 claim = 上面那条）。
        _, downstream = _ledger().record(facts.ClaimCandidate(
            field_key="籍贯", value="长沙", authority="relay",
            person_key=ANAHIDA, evidence="刚刚@我那位报的籍贯我记着了：长沙",
            source_chat_id=CHAT, source_message_id="m2144b",
            source_actor_id=SELF, source_claim_id=upstream),
            engine=engine, now="2026-09-30 21:44:20")
        assert _ledger().resolve(CHAT, ANAHIDA, "籍贯").has_claim

        direct, cascaded = _ledger().withdraw(
            chat_id=CHAT, actor_id=XIAOYU, reason="自己声明是瞎说",
            evidence=RETRACT_214457, engine=engine, now="2026-09-30 21:44:57")
        assert direct == 1, (direct, cascaded)
        assert cascaded == 1, "下游那条必须一并作废（不是静默忽略）"
    history = {row.id: row for row in _ledger().history(CHAT, ANAHIDA, "籍贯")}
    assert history[upstream].withdrawn_at, history[upstream]
    assert history[downstream].withdrawn_at, history[downstream]
    assert history[downstream].value == "长沙", "撤回不改写正文"
    # 作废后：这个 (chat, person, field) 上**没有**仍有效的 claim。
    resolved = _ledger().resolve(CHAT, ANAHIDA, "籍贯")
    assert resolved.has_claim is False, resolved.to_dict()
    assert len(resolved.history) == 2, "更正是历史，不是删除：两行都还在"
    print(f"  ok 撤回：直接 {direct} 条 + 下游 {cascaded} 条作废（链可回查）")


def test_retraction_is_detected_from_the_real_message():
    """撤回的**识别**：21:44:57 那句必须被抽出来（不靠人手工标）。"""
    hits = facts.extract_retractions(RETRACT_214457, actor_id=XIAOYU,
                                     chat_id=CHAT)
    assert hits and hits[0].actor_id == XIAOYU, hits
    assert hits[0].reason, hits
    for clean in ("我瞎说的？", LIVE_1802, "籍贯我记着了：长沙、株洲、湘潭。"):
        assert facts.extract_retractions(clean, actor_id=SELF) == [] or clean.endswith("？"), clean
    print("  ok 撤回识别（21:44:57 命中，正常叙述不命中）")


def test_live_fixture_owner_denial_is_not_punished():
    """18:01:57 主人的更正：「你记错了吧，不是我说的5.3一天」不是无支撑断言。"""
    ledger = _records(items=[])
    with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
        config = _guard_inputs(LIVE_1802, ledger)
    assert not config.unsupported_claims or all(
        "5.3" not in str(item) for item in config.unsupported_claims), \
        config.unsupported_claims
    print("  ok 主人的否认句不被当成她自己的无支撑断言")


# ---------------------------------------------------------------------------
# ⑥ 三个近例回归（21:45:20 / 21:46:55 / 21:44:57）
# ---------------------------------------------------------------------------
def test_off_is_byte_identical_and_touches_nothing():
    """``off`` = 逐字节一致：接缝一行不走（不 import 台账、不查库、不改 config）。"""
    base = GateConfig()
    with _env(YINYUE_FEATURE_CLAIM_LEDGER="off"):
        config = claim_ledger_inputs(LIVE_1801, chat_id=CHAT)
    assert config is base or config == base, config
    assert config.unsupported_claims == ()

    # 台账关着时，闸门读到的 config 与手工 GateConfig() 完全一致 → 判定逐字节相同。
    with _env(YINYUE_FEATURE_CLAIM_LEDGER="off"):
        off_config = claim_ledger_inputs(LIVE_1801, chat_id=PURE_CHAT)
    for text in (LIVE_1801, WRONG_2145, REPEAT_2146, "在的。"):
        a = run_output_gate(text, chat_type="group", chat_id=PURE_CHAT,
                            config=off_config, mode=GATE_MODE_ON)
        b = run_output_gate(text, chat_type="group", chat_id=PURE_CHAT,
                            config=GateConfig(), mode=GATE_MODE_ON)
        assert a == b, (text, a, b)
        assert a.applied_text == text, (text, a.applied_text)

    # off 时不建表、不读写：裸库（**一张表都没有**）当 meta repo，接缝照旧不碰它。
    bare = _fresh_path("bare_off")
    engine = create_engine(f"sqlite:///{bare}")
    try:
        with _env(YINYUE_FEATURE_CLAIM_LEDGER="off"):
            config = claim_ledger_inputs(LIVE_1801, chat_id=CHAT)
            assert config.unsupported_claims == ()
            # 注入式台账（**零 IO**）：证明 off 下"读台账"这回事根本没发生——
            # 如果哪天有人在 off 路径上查库，这里不会红，但接缝那一行会（见上）。
            assert facts.ClaimLedger([]).for_chat(CHAT) == []
    finally:
        engine.dispose()
    con = sqlite3.connect(str(bare))
    try:
        names = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert "claim_ledger" not in names, "off 不许建表（表由迁移/create_all 管，不由 flag 管）"
    print("  ok off：接缝一行不走、判定逐字节一致、不建表")


def test_off_ledger_read_does_not_touch_production_path():
    """``off`` 下 ``evaluate_claims`` 早退：**不构造默认台账**（不查库）。"""
    calls: list[str] = []

    class _Boom:
        def resolve(self, *_args, **_kwargs):        # pragma: no cover - 不该被调到
            calls.append("resolve")
            raise AssertionError("off 下不该查台账")

    with _env(YINYUE_FEATURE_CLAIM_LEDGER="off"):
        out = facts.evaluate_claims(LIVE_1801, chat_id=CHAT,
                                    ledger=_Boom(), recent_keys=("k",))
        seam = claim_ledger_inputs(LIVE_1801, chat_id=CHAT)
    assert out.enabled is False and out.verdicts == (), out
    assert calls == [], calls
    assert seam.unsupported_claims == ()

    # shadow / on 才真的判定（同一份输入，两种档位的差别只在 enforced）。
    ledger = _records(items=[])
    for mode, enforced in (("shadow", False), ("on", True)):
        with _env(YINYUE_FEATURE_CLAIM_LEDGER=mode):
            out = facts.evaluate_claims(LIVE_1801, chat_id=CHAT, ledger=ledger)
        assert out.enabled is True, mode
        assert out.enforced is enforced, (mode, out.enforced)
        assert out.unsupported, mode
    print("  ok off 早退不查库；shadow/on 才判定（enforced 分档）")


# ---------------------------------------------------------------------------
# ⑧ 迁移 56：幂等 + parity + 注册位置
# ---------------------------------------------------------------------------
def test_migration_56_is_idempotent():
    path = _fresh_path("idem")
    engine = _create_all_engine(path)
    engine.dispose()
    con = sqlite3.connect(str(path))
    try:
        con.execute("DROP TABLE IF EXISTS claim_ledger")
        con.execute("DROP TABLE IF EXISTS claim_ledger_audit")
        con.commit()
    finally:
        con.close()

    migrations.run_migrations(str(path))
    first = (_table_shape(path, "claim_ledger"),
             _table_shape(path, "claim_ledger_audit"))
    migrations.run_migrations(str(path))
    second = (_table_shape(path, "claim_ledger"),
              _table_shape(path, "claim_ledger_audit"))
    assert first == second, f"迁移 56 不幂等：\n{first}\n{second}"
    assert 56 in _stamped(path)
    assert {column[0] for column in first[0][0]} == {
        column.name for column in ClaimLedgerRow.__table__.columns}
    assert {column[0] for column in first[1][0]} == {
        column.name for column in ClaimLedgerAuditRow.__table__.columns}
    print("  ok 迁移 56 幂等（两张表跑两次 PRAGMA 全等）")


def test_migration_56_parity_and_registration():
    fresh = _fresh_path("parity_fresh")
    engine = _create_all_engine(fresh)
    engine.dispose()

    legacy = _legacy_library("parity_legacy", 55)
    migrations.run_migrations(str(legacy))
    for table in ("claim_ledger", "claim_ledger_audit"):
        assert table in _tables(legacy), table
        assert _table_shape(fresh, table) == _table_shape(legacy, table), \
            f"create_all vs 迁移 56 的建表漂移：{table}"

    stamped = _stamped(legacy)
    assert 56 in stamped and max(stamped) >= 56
    # 两个坑：56 不在两个注册元组里（进哪个都会「哪里都不执行」）。
    assert 56 not in migrations._META_MIGRATION_VERSIONS, "进了元链 → 40–55 永久跳过"
    assert 56 not in migrations._ACCOUNT_SKIP_MIGRATION_VERSIONS, "进了 skip 集 → 哪里都不执行"
    assert any(m.version == 56 for m in migrations._MIGRATIONS), "56 必须注册"

    # NOT NULL 列全带 server_default（仓库既有教训）。主键 `id` 是 AUTOINCREMENT，
    # 与 SessionFocusState / RuleUseLedger 同口径，不在此列。
    for model in (ClaimLedgerRow, ClaimLedgerAuditRow):
        for column in model.__table__.columns:
            if column.nullable or column.primary_key:
                continue
            assert column.server_default is not None, \
                f"{model.__name__}.{column.name} 缺 server_default"

    # 非应用库（连 terminal_events 都没有）：守卫 return，不建表。
    alien = _fresh_path("alien")
    con = sqlite3.connect(str(alien))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER "
                    "PRIMARY KEY, description TEXT, applied_at DATETIME)")
        con.execute("INSERT INTO _schema_version (version, description) VALUES (55,'t')")
        con.commit()
    finally:
        con.close()
    migrations.run_migrations(str(alien))
    assert "claim_ledger" not in _tables(alien), "非应用库不该被塞进应用表"
    print("  ok 56 parity + 注册位置 + NOT NULL 全带 server_default")


def test_schema_shape_is_a_ledger_not_a_rewritable_store():
    """结构锁：台账里**没有**"把旧正文改掉"的形状（更正只走 superseded_* 列）。"""
    columns = {column.name for column in ClaimLedgerRow.__table__.columns}
    for required in ("authority", "superseded_by", "supersede_reason",
                     "superseded_at", "withdrawn_at", "source_actor_id",
                     "source_chat_id", "source_message_id", "source_claim_id",
                     "content_key", "observed_at", "created_at"):
        assert required in columns, required
    # 溯源列齐 + 权威级默认最低（推测）：没有来源的写入不会被当成自报。
    assert ClaimLedgerRow.__table__.columns["authority"].server_default is not None
    assert "'speculation'" in str(
        ClaimLedgerRow.__table__.columns["authority"].server_default.arg)
    assert set(facts.AUTHORITY_LEVELS) == {"self_report", "relay", "speculation"}
    print("  ok 台账形状（溯源 + 更正链列齐，权威级默认最低）")


# ---------------------------------------------------------------------------
# ⑨ 抽取：字段词表是**有限**的，值不进词表
# ---------------------------------------------------------------------------
def test_extraction_classifies_authority_from_real_messages():
    """三级权威的**识别**：自报 / 转述 / 推测各一条真实样本。"""
    self_hits = facts.extract_claims(SELFREPORT_214459, speaker_key=ANAHIDA)
    assert self_hits and self_hits[0].authority == "self_report", self_hits
    assert self_hits[0].person_key == ANAHIDA, self_hits[0]

    relay_hits = facts.extract_claims("他跟我说他是湖南长沙的。", speaker_key=SELF)
    assert relay_hits and relay_hits[0].authority == "relay", relay_hits

    guess_hits = facts.extract_claims("他好像是湖南人。", speaker_key=SELF)
    assert guess_hits and guess_hits[0].authority == "speculation", guess_hits

    # 花费：18:01 的活体样本只被登记成"一天的**字段**"（值不进代码）。
    spend = facts.extract_claims(LIVE_1801, speaker_key=SELF)
    assert spend and spend[0].field_key == "花费", spend
    assert spend[0].value == "五块三", spend

    assert facts.field_keys() == ("籍贯", "所在地", "年龄", "学校", "花费"), \
        facts.field_keys()
    print("  ok 抽取：自报 / 转述 / 推测三格识别正确")


def test_parallel_claims_keep_quote_and_span():
    """**说法带原话 + span 落库**（迁移 59）：读侧能回原文，不必猜。

    原话 + 位置 = 基质；``value``/``field_key`` 是**可重建派生缓存**（``rebuild()``
    用同一份词表重跑一遍就能重建出来）。老行（没有 span）照样读得到值，只是位置未知。
    """
    engine = _reset_db()
    with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
        self_hits = facts.extract_claims(
            SELFREPORT_214459, speaker_key=ANAHIDA, chat_id=CHAT,
            message_id="m1", actor_id=ANAHIDA)
        assert self_hits, "样本必须抽得出 claim"
        first = self_hits[0]
        assert first.source_text == SELFREPORT_214459, first
        assert first.evidence_start >= 0, (first.evidence_start, first.evidence_end)
        assert first.evidence_end > first.evidence_start
        assert first.source_text[first.evidence_start:first.evidence_end] == first.evidence

        relay_hits = facts.extract_claims(
            "他跟我说他是广东人。", speaker_key=SELF, person_key=ANAHIDA,
            chat_id=CHAT, message_id="m2", actor_id=SELF)
        assert relay_hits and relay_hits[0].authority == "relay", relay_hits
        relay = relay_hits[0]
        assert relay.source_text[relay.evidence_start:relay.evidence_end] == relay.evidence

        outcome, _ = _ledger().record(first, engine=engine)
        assert outcome == facts.RecordOutcome.INSERTED, outcome
        outcome, low_id = _ledger().record(relay, engine=engine)
        assert outcome == facts.RecordOutcome.PARALLEL, outcome

        resolved = _ledger().resolve(CHAT, ANAHIDA, "籍贯")
        picked = [row for row in resolved.conflicts if row.id == low_id]
        assert len(picked) == 1, resolved.to_dict()
        row = picked[0]

    assert row.value == relay.value
    assert row.source_text == "他跟我说他是广东人。", row.source_text
    assert row.span == (relay.evidence_start, relay.evidence_end), row.span
    assert row.source_text[row.span[0]:row.span[1]] == row.evidence_text
    assert row.quote == row.evidence_text, "有 span → 引文就是原文那一段"
    assert row.source_actor_id == SELF, "转述人（谁说的）也读得到"
    assert row.source_message_id == "m2" and row.authority == "relay", row.to_dict()
    # 通用 span 类型（与 P1 共用同一个 EvidenceRef，不另造）。
    ref = row.evidence_ref
    assert ref.message_id == "m2" and ref.has_span, ref
    assert ref.slice_from(row.source_text) == row.evidence_text, (ref, row.evidence_text)
    # 真相是原话 + 位置：字段/值能拿原话重跑出来。
    rebuilt = row.rebuild()
    assert rebuilt["matches_stored"] is True, rebuilt
    assert rebuilt["span"] == row.span, rebuilt
    assert any(item["value"] == row.value for item in rebuilt["rebuilt"]), rebuilt

    # 老行（迁移 59 之前 / 没有 span）：值读得到，位置未知 —— 不假装知道。
    legacy = _records(items=[{"person": ANAHIDA, "field": "籍贯",
                              "value": "湖南长沙", "authority": "self_report",
                              "at": "2026-09-30 21:44:59"}])
    old = legacy.resolve(CHAT, ANAHIDA, "籍贯").authoritative
    assert old.has_span is False and old.span == (-1, -1), old.span
    assert old.source_text == "" and old.evidence_text == "", old.to_dict()
    assert old.quote == "", "没有原话也没有位置 → 空（不编）"
    assert old.evidence_ref.has_span is False, old.evidence_ref
    assert old.rebuild()["matches_stored"] is False, old.rebuild()
    print("  ok 并列说法带原话 + span（可回原文、字段可重建；老行不假装有位置）")


def test_authority_labels_cover_every_level():
    """**三级权威在渲染层都在**：标签表的 key 必须覆盖 ``AUTHORITY_LEVELS``。

    2026-10-16 收口：key 原写成 ``relayed``，而生产写侧发的是 ``relay`` —— 真流量的
    转述行全部落到兜底 ``[听说]``，「自报 > 转述 > 推测」在渲染层**退化成两级**。
    """
    from types import SimpleNamespace

    from core.memory import providers as providers_mod

    missing = set(facts.AUTHORITY_LEVELS) - set(providers_mod._AUTHORITY_LABEL)
    assert not missing, f"标签表缺 key：{sorted(missing)}"
    for authority, label in (("self_report", "[他自报]"), ("relay", "[别人说]"),
                             ("speculation", "[我猜的]"), ("relayed", "[别人说]")):
        row = SimpleNamespace(person_key=ANAHIDA, field_key="籍贯", value="湖南长沙",
                              authority=authority, observed_at="2026-09-30 21:44:59")
        text = providers_mod.ClaimMemoryProvider._render(row, {})
        assert label in text, (authority, text)
        assert "[听说]" not in text, f"{authority} 落到兜底了：{text}"

    # 有原话/位置的行 → 读侧看得到出处；老行（没有这两列）输出与旧版逐字相同。
    fresh = SimpleNamespace(person_key=ANAHIDA, field_key="籍贯", value="湖南长沙",
                            authority="relay", observed_at="2026-09-30 21:44:59",
                            source_text="他跟我说他是广东人。", evidence_start=0,
                            evidence_end=9, evidence_text="他跟我说他是广东人")
    rendered = providers_mod.ClaimMemoryProvider._render(fresh, {})
    assert "（原话：他跟我说他是广东人）" in rendered, rendered
    old = SimpleNamespace(person_key=ANAHIDA, field_key="籍贯", value="湖南长沙",
                          authority="relay", observed_at="2026-09-30")
    assert providers_mod.ClaimMemoryProvider._render(old, {}) == (
        "[别人说]【籍贯】有人 = 湖南长沙（群里 2026-09-30）")
    print("  ok 三级权威标签齐（relay 不再落兜底 [听说]，出处可读）")


def test_migration_59_is_registered_and_adds_span_columns():
    """迁移 59：给台账加 span/原话 4 列（幂等、与模型同形、不在两个版本元组里）。"""
    path = _fresh_path("m59")
    engine = _create_all_engine(path)
    engine.dispose()
    con = sqlite3.connect(str(path))
    try:
        con.execute("DROP TABLE IF EXISTS claim_ledger")
        con.execute("DROP TABLE IF EXISTS claim_ledger_audit")
        con.commit()
    finally:
        con.close()

    migrations.run_migrations(str(path))
    first = _table_shape(path, "claim_ledger")
    migrations.run_migrations(str(path))
    second = _table_shape(path, "claim_ledger")
    assert first == second, "迁移 59 不幂等"
    assert 59 in _stamped(path)
    columns = {column[0]: column for column in first[0]}
    for name, ddl_type in (("source_text", "TEXT"), ("evidence_text", "TEXT"),
                           ("evidence_start", "INTEGER"), ("evidence_end", "INTEGER")):
        assert name in columns, sorted(columns)
        column = columns[name]
        assert column[1] == ddl_type, column
        assert int(column[2]) == 1, f"{name} 必须 NOT NULL（老库 ALTER 要带默认值）"
        assert column[3] is not None, f"{name} 缺 server_default"
    assert {column[0] for column in first[0]} == {
        column.name for column in ClaimLedgerRow.__table__.columns}, "迁移 vs 模型不同形"
    assert 59 not in migrations._META_MIGRATION_VERSIONS, "进了元链 → 40-55 永久跳过"
    assert 59 not in migrations._ACCOUNT_SKIP_MIGRATION_VERSIONS, "进了 skip 集 → 哪里都不执行"
    assert any(m.version == 59 for m in migrations._MIGRATIONS), "59 必须注册"
    print("  ok 迁移 59 幂等 + 4 列 + 不在两个版本元组 + 与模型同形")


# ---------------------------------------------------------------------------
# 自带 runner
# ---------------------------------------------------------------------------
def main() -> bool:
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    print("=" * 68)
    print("M2-4 claim 台账（事实权威 / 更正历史 / 复述抑制 / unfounded_claim 可拦）")
    print("=" * 68)
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)

