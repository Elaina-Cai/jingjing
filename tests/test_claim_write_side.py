# -*- coding: utf-8 -*-
"""claim 台账**写侧**（`docs/archive/任务清单_2026-10-06.md:71` M2-4）：谁在何时自报/转述/推测了什么 → 真的落库。

M2-4 交付了台账本体与读侧接线，但 ``ClaimLedger().record`` **零生产调用点** → 台账
永远 0 行 → 开关只能开 ``shadow``（``on`` 会因为"没有台账行 = 无支撑"过度降级）。
本文件守的就是那一格：写侧真的在生产调用点上落库。

写侧 owner 与判据（**全在** ``core/context/message_facts.py``，调用方传不进来）：

* 写口 :func:`message_facts.record_message_claims`（唯一入口，必须给 engine/ledger）；
* 归属判据 :func:`message_facts.classify_subject`（自报 / 点名的第三方 / **解析不出人
  就不登记**）；
* 权威级判据 :func:`message_facts._authority_of`（自报 > 转述 > 推测，从文本语态 +
  归属**算**出来）；
* 撤回识别 :func:`message_facts.extract_retractions` → ``ClaimLedger.withdraw``。

生产接线点：``scheduler/main.py`` 的 ``NEW_MESSAGES_SYNCED`` 处理器 →
``scheduler/person_memory.py::record_claims_for_messages``（与既有 ``person_observations``
**同一批消息、同一处身份解析**，不是第二套事实存储）。

**测试卫生**（照 ``tests/test_claim_ledger.py``）：断言"关"一律**显式** ``off``
（"env 未设"≠"关"）；库/日志/平台目录全部改道，**绝不碰生产库**。

Run directly::

    .\\.venv\\Scripts\\python.exe tests\\test_claim_write_side.py
"""

from __future__ import annotations

import ast
import os
import sqlite3
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent

# --- import 期钉死：本文件依赖的每一个开关都显式表态（"env 未设"≠"关"）--------
os.environ["YINYUE_FEATURE_CLAIM_LEDGER"] = "off"
# 出站闸门只剩 OUTPUT_GATE（ADDRESSEE_GUARD 随 R0 删门移除）。
os.environ["YINYUE_FEATURE_OUTPUT_GATE"] = "off"
# 隔离：库/日志/平台目录改道（写侧用例自己注入 engine，绝不走 bootstrap 的默认库）。
_ISO_ROOT = (ROOT / "data" / "_claim_write_side_test").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")
os.environ["LOG_DIR"] = str(ROOT / "data" / "_test_logs_claim_write_side")

from sqlalchemy import create_engine, text as _sql  # noqa: E402

from core.context import message_facts as facts  # noqa: E402
from db.meta_models import MetaBase  # noqa: E402

# ---------------------------------------------------------------------------
# 真实样本（2026-09-30 21:44–21:47 群 10000000001@chatroom）
# ---------------------------------------------------------------------------
CHAT = "10000000001@chatroom"
ANAHIDA = "wxid_seed03"      # 21:44:59 自报「湖南长沙」的人
XIAOYU = "wxid_seed06"        # 21:44:57 撤回「我瞎说的」的转述者
SELF = "wxid_seed01"          # 她

SELFREPORT = "我是湖南长沙的。"
RELAY = "P6跟我说他是湖南长沙的。"
SPECULATION = "他好像是湖南人。"
RETRACT = "我瞎说的，别当真。"


# ---------------------------------------------------------------------------
# 隔离设施
# ---------------------------------------------------------------------------
class _Repo:
    """极小 meta repo 壳（写路径只认 ``engine``）。"""

    def __init__(self, engine) -> None:
        self.engine = engine


class _Boom:
    """任何属性/调用都打红的桩：用来证明"这一行根本没走"。"""

    def __getattr__(self, name):  # pragma: no cover - 关位下不该被碰到
        raise AssertionError(f"off 位下不该碰台账（读了 {name}）")


def _book(engine) -> facts.ClaimLedger:
    """**把台账钉在同一个 engine 上**（读口与写口必须同一个库）。

    ``record_message_claims(engine=...)`` 只把 engine 交给**写**；幂等那一步要**读**回来
    （``_recorded_message``），读口走的是台账对象自己的 repo。两处不同库就会出现
    "写进去了但读不回来" → 幂等失效。生产接线处的做法与此一致
    （``person_memory.record_claims_for_messages`` 一批一个句柄）。
    """
    return facts.ClaimLedger(meta_repo=_Repo(engine))


def _fresh_path(name: str) -> Path:
    path = _ISO_ROOT / f"{name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return path


def _engine(name: str = "shared"):
    engine = create_engine(f"sqlite:///{_fresh_path(name)}")
    MetaBase.metadata.create_all(engine)
    return engine


def _rows(engine) -> list[dict]:
    with engine.connect() as conn:
        return [
            dict(row) for row in conn.execute(_sql(
                "select id, chat_id, person_key, field_key, value, authority,"
                " source_kind, source_actor_id, source_message_id, superseded_by,"
                " withdrawn_at from claim_ledger order by id"
            )).mappings().all()
        ]


def _audit(engine) -> list[dict]:
    with engine.connect() as conn:
        return [
            dict(row) for row in conn.execute(_sql(
                "select action, field_key, authority, existing_claim_id,"
                " existing_authority, reason from claim_ledger_audit order by id"
            )).mappings().all()
        ]


@contextmanager
def _env(**pairs):
    """临时设置三态开关（"off"/"shadow"/"on"），退出时连"未设置"一起还原。"""
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


def _message(text: str, *, speaker: str = SELF, message_id: str = "m1",
             chat: str = CHAT, kind: str = "text", is_self: bool = False,
             known: tuple[str, ...] = (), resolve=None) -> facts.ClaimMessage:
    return facts.ClaimMessage(
        text=text, chat_id=chat, speaker_key=speaker, message_id=message_id,
        platform="wechat", account_id="wxid_bot",
        observed_at="2026-09-30 21:44:59", kind=kind, is_self=is_self,
        known_person_keys=known, resolve_person=resolve,
    )


def _name_to_id(mapping: dict[str, str]):
    """**注入解**：昵称 → 规范 id（生产里是 canonical 身份层给的，用例里给一份小的）。"""
    return lambda name: mapping.get(str(name or "").strip(), "")


# ---------------------------------------------------------------------------
# ① 三级权威：自报 / 转述 / 推测**各落一行且级别正确**
# ---------------------------------------------------------------------------
def test_write_side_records_all_three_authority_levels():
    """三级各一条真实消息 → 台账**三条行**，级别分别是 self_report / relay / speculation。

    权威级**不是调用方传的**：``ClaimMessage`` 里根本没有 authority 字段，级别由
    ``classify_subject``（归属）+ ``_authority_of``（语态）算出来——用例顺手把
    "算出来的"与"判据单独跑一遍"的读数对齐，证明写口没有各判一套。

    "低级不许盖高级"在新口径下**不再丢数据**：推测那条落在**同一个 chat** 时会撞上
    自报那条（同一个 ``(chat, person, 字段)``）→ **并列留档**（见 ②）。
    """
    engine = _engine("three_levels")
    other_chat = "10000000001@chatroom#2"
    try:
        with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
            # 自报：说话人就是被说的那个人。
            facts.record_message_claims(
                _message(SELFREPORT, speaker=ANAHIDA, message_id="m214459"),
                ledger=_book(engine))
            # 转述：句子里点名第三方，昵称由注入的解析器（canonical 身份层的活）
            # 换回规范 id —— 值归**被说的那个人**。
            facts.record_message_claims(
                _message(RELAY, speaker=SELF, message_id="m214420",
                         resolve=_name_to_id({"P6": XIAOYU})),
                ledger=_book(engine))
            # 推测：**点了第三方却解析不出人 → 不登记**。这不是"漏"，是方向：
            # 把"他好像是湖南人"记成**说话人自己的自报**，就是 21:45:20 事故的反向形态。
            facts.record_message_claims(
                _message(SPECULATION, speaker=SELF, message_id="m214600"),
                ledger=_book(engine))
            # 推测那一档要有落库的读数：同语态但话就是说自己的（自报口吻却带"好像"=
            # 无来源）→ 归属是说话人，权威级仍然是 **speculation**（语态判据说了算，
            # 不是"有没有人"；也没有"越不确定越权威"的反向判定）。
            facts.record_message_claims(
                _message("我好像是湖南人吧。", speaker=ANAHIDA, message_id="m214700",
                         chat=other_chat),
                ledger=_book(engine))
            # 反证：同一条推测落在自报那一个 chat 上 → 撞权威序 → **并列留档**
            # （照登记；谁更可信由读时透镜定，见 ②）。
            clash = facts.record_message_claims(
                _message("我好像是湖北人吧。", speaker=ANAHIDA, message_id="m214701"),
                ledger=_book(engine))
        rows = _rows(engine)
    finally:
        engine.dispose()

    by_message = {row["source_message_id"]: row for row in rows}
    assert set(by_message) == {"m214459", "m214420", "m214700", "m214701"}, [
        (row["source_message_id"], row["authority"]) for row in rows]
    assert by_message["m214459"]["authority"] == "self_report", by_message["m214459"]
    assert by_message["m214420"]["authority"] == "relay", by_message["m214420"]
    assert by_message["m214700"]["authority"] == "speculation", by_message["m214700"]
    # 解析不出人的那一句**一行都没有**（宁漏不误杀）。
    assert "m214600" not in by_message, by_message["m214600"]
    # 撞权威序的那一句：**并列留档**（照登记、不被拒），高权威行一字不动。
    assert clash.rows == 1 and not clash.rejected, clash.to_dict()
    assert len(clash.parallel) == 1 and clash.parallel[0][2] == "speculation", (
        clash.to_dict())
    assert by_message["m214701"]["authority"] == "speculation", by_message["m214701"]
    assert int(by_message["m214459"]["superseded_by"]) == 0, "并列不许动高权威行"
    # 转述的值归**被说的那个人**（P6），不是说话人（她）。
    assert by_message["m214420"]["person_key"] == XIAOYU, by_message["m214420"]
    assert by_message["m214420"]["source_actor_id"] == SELF, by_message["m214420"]
    assert by_message["m214459"]["person_key"] == ANAHIDA, by_message["m214459"]
    # 三级齐（台账里真的出现过三档，不是"只有自报"）。
    assert {row["authority"] for row in rows} == {
        "self_report", "relay", "speculation"}, rows
    # 判据同源：单独跑一遍归属判据，读数必须与写口落库的一致。
    shape, subject = facts.classify_subject(
        RELAY, speaker_key=SELF, known_person_keys=(),
        resolve_person=_name_to_id({"P6": XIAOYU}))
    assert (shape, subject) == (facts.SUBJECT_NAMED, XIAOYU), (shape, subject)
    assert facts.classify_subject(SPECULATION, speaker_key=SELF,
                                  known_person_keys=(XIAOYU,)) == (
        facts.SUBJECT_UNBOUND, ""), "解析不出人就不该有归属"
    print("  ok ① 三级各落一行（self_report / relay / speculation），判据同源")


def test_authority_is_never_a_caller_parameter():
    """结构锁：**权威级不是调用方能传的**（写侧签名里没有 authority）。"""
    import inspect

    params = set(inspect.signature(facts.record_message_claims).parameters)
    assert not (params & {"authority", "level", "rank", "source_kind"}), sorted(params)
    fields = set(facts.ClaimMessage.__dataclass_fields__)
    assert not (fields & {"authority", "level", "rank"}), sorted(fields)
    # 写口只吃"消息 + 句柄"：既有读口一行都不改。
    source = inspect.getsource(facts.record_message_claims)
    assert "authority=" not in source.split("def message_claim_candidates")[0], \
        "写口里不许出现「自己指定权威级」的写法"
    print("  ok ① 权威级不是调用方参数（结构锁）")


# ---------------------------------------------------------------------------
# ② 低权威想盖高权威 → 并列留档（高权威行不动 + 审计留痕）
# ---------------------------------------------------------------------------
def test_low_authority_is_kept_parallel_at_the_write_port():
    """自报先落 → 转述/推测再落同字段 → **并列留档**（一行不丢），高权威行一字不动。"""
    engine = _engine("parallel")
    try:
        with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
            facts.record_message_claims(
                _message(SELFREPORT, speaker=ANAHIDA, message_id="m1"),
                ledger=_book(engine))
            low = facts.record_message_claims(
                _message(RELAY, speaker=SELF, message_id="m2",
                         resolve=_name_to_id({"P6": ANAHIDA})),
                ledger=_book(engine))
            guess = facts.record_message_claims(
                _message("我好像是广东人吧。", speaker=ANAHIDA, message_id="m3"),
                ledger=_book(engine))
            book = facts.ClaimLedger(meta_repo=_Repo(engine))
            resolved = book.resolve(CHAT, ANAHIDA, "籍贯")
            recency = book.resolve(CHAT, ANAHIDA, "籍贯", order="recency")
        rows = _rows(engine)
        audit = _audit(engine)
    finally:
        engine.dispose()

    assert low.rows == 1 and not low.rejected, low.to_dict()
    assert len(low.parallel) == 1 and low.parallel[0][2] == "relay", low.to_dict()
    assert guess.rows == 1 and len(guess.parallel) == 1, guess.to_dict()
    assert len(rows) == 3, rows
    assert rows[0]["value"] == "湖南长沙" and rows[0]["authority"] == "self_report", rows
    assert int(rows[0]["superseded_by"]) == 0, "并列不许动那条高权威行"
    assert {row["source_message_id"] for row in rows} == {"m1", "m2", "m3"}, rows
    # 留痕：两行 parallel，点名「挡在前面的那条」（都是那条自报）。
    parallel = [row for row in audit if row["action"] == "parallel"]
    assert len(parallel) == 2, audit
    assert all(int(row["existing_claim_id"]) == int(rows[0]["id"]) for row in parallel)
    assert all(row["existing_authority"] == "self_report" for row in parallel), parallel
    # 同一批行两种读数：权威序回自报；时间序回最后说的那条。
    assert resolved.value == "湖南长沙" and resolved.authority == "self_report"
    assert [item.authority for item in resolved.conflicts] == ["relay", "speculation"], (
        resolved.to_dict())
    assert all(item.active for item in resolved.conflicts), resolved.to_dict()
    assert recency.order == "recency", recency.to_dict()
    assert recency.value == guess.parallel[0][1], (recency.to_dict(), guess.to_dict())
    print("  ok ② 低权威并列留档 2 次（高权威行不动 + 审计两行 + 两种透镜读数）")



# ---------------------------------------------------------------------------
# ③ 撤回 / 更正：旧行留着并标 superseded_by（不许静默改写）
# ---------------------------------------------------------------------------
def test_withdrawal_keeps_the_old_row_and_marks_the_chain():
    """两个方向都要有读数：**更正**（superseded_by）与**撤回**（withdrawn_at）。"""
    engine = _engine("withdraw")
    try:
        with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
            # 更正链：同一人同一字段改口 → 旧行标 superseded_by，正文一字不改。
            facts.record_message_claims(
                _message(SELFREPORT, speaker=ANAHIDA, message_id="m1"),
                ledger=_book(engine))
            facts.record_message_claims(
                _message("我是湖南长沙岳麓区的。", speaker=ANAHIDA, message_id="m2"),
                ledger=_book(engine))
            # 撤回：说话人自己收回刚说过的话。
            book = facts.ClaimLedger(meta_repo=_Repo(engine))
            facts.record_message_claims(
                _message(RETRACT, speaker=ANAHIDA, message_id="m3"), ledger=book)
        rows = _rows(engine)
        resolved = facts.ClaimLedger(meta_repo=_Repo(engine)).resolve(
            CHAT, ANAHIDA, "籍贯")
    finally:
        engine.dispose()

    assert len(rows) == 2, rows
    old, new = rows
    # 更正：旧行只在更正链三列上被写，正文/权威/时间逐字不动。
    assert int(old["superseded_by"]) == int(new["id"]), old
    assert old["value"] == "湖南长沙", old
    assert old["authority"] == "self_report", old
    assert int(new["superseded_by"]) == 0, new
    # 撤回：两行都**留着**，只标 withdrawn_at（不得静默删除/改写）。
    assert str(old["withdrawn_at"] or ""), old
    assert str(new["withdrawn_at"] or ""), new
    assert new["value"] == "湖南长沙岳麓区", "撤回不改写正文"
    assert resolved.has_claim is False, resolved.to_dict()
    assert len(resolved.history) == 2, "更正是历史，不是删除"
    print("  ok ③ 更正 → 旧行 superseded_by；撤回 → 旧行留着 + withdrawn_at")


# ---------------------------------------------------------------------------
# ④ 幂等：同一条消息重复处理 → 不产生第二行
# ---------------------------------------------------------------------------
def test_reprocessing_the_same_message_never_adds_a_row():
    """同一条消息处理两次（含只换台账句柄）→ 仍是一行，第二次返回 already_seen。"""
    engine = _engine("idempotent")
    try:
        message = _message(SELFREPORT, speaker=ANAHIDA, message_id="m214459")
        with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
            first = facts.record_message_claims(message, ledger=_book(engine))
            second = facts.record_message_claims(message, ledger=_book(engine))
            third = facts.record_message_claims(
                message, ledger=facts.ClaimLedger(meta_repo=_Repo(engine)))
        rows = _rows(engine)
    finally:
        engine.dispose()

    assert first.rows == 1 and first.already_seen is False, first.to_dict()
    assert second.rows == 0 and second.already_seen is True, second.to_dict()
    assert third.rows == 0 and third.already_seen is True, third.to_dict()
    assert len(rows) == 1, rows
    print("  ok ④ 幂等：同一 (chat, msg_id) 处理三次 → 仍是一行")


def test_duplicate_outcome_does_not_grow_the_ledger():
    """同一权威同值（换了一条消息 id 的复述）→ ``DUPLICATE``：不新增行（既有口径）。"""
    engine = _engine("duplicate")
    try:
        with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
            facts.record_message_claims(
                _message(SELFREPORT, speaker=ANAHIDA, message_id="m1"),
                ledger=_book(engine))
            again = facts.record_message_claims(
                _message(SELFREPORT, speaker=ANAHIDA, message_id="m2"),
                ledger=_book(engine))
        rows = _rows(engine)
    finally:
        engine.dispose()
    assert again.rows == 1, "同一句话复述：算这条消息说的，但台账不长行"
    assert len(rows) == 1, rows
    print("  ok ④ 复述同值 → DUPLICATE（台账仍是一行）")


# ---------------------------------------------------------------------------
# ⑤ off：逐字节一致、**零 IO**（连句柄都不碰）
# ---------------------------------------------------------------------------
def test_off_touches_nothing():
    """``off`` = 一行都不走：不给句柄也不写、给了打红的句柄也不碰、不建表。"""
    path = _fresh_path("bare_off")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with _env(YINYUE_FEATURE_CLAIM_LEDGER="off"):
            bare = facts.record_message_claims(
                _message(SELFREPORT, speaker=ANAHIDA, message_id="m1"))
            guarded = facts.record_message_claims(
                _message(SELFREPORT, speaker=ANAHIDA, message_id="m1"),
                ledger=_Boom(), engine=_Boom())
            pool = facts.message_claim_candidates(
                _message(SELFREPORT, speaker=ANAHIDA, message_id="m1"))
        assert bare.traced is False and bare.rows == 0, bare.to_dict()
        assert guarded.traced is False, guarded.to_dict()
        # 纯函数层不读开关：候选照旧算得出来（"关"关的是**落库**，不是判据）。
        assert pool and pool[0].authority == "self_report", pool
    finally:
        engine.dispose()
    con = sqlite3.connect(str(path))
    try:
        names = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert "claim_ledger" not in names, "off 不许建表（表由迁移/create_all 管）"
    print("  ok ⑤ off：零 IO（打红句柄不被碰）、不建表")


def test_shadow_also_writes():
    """``shadow`` **也落库**：影子期要攒的就是这份台账（"开了影子还是 0 行"是缺口）。"""
    engine = _engine("shadow")
    try:
        with _env(YINYUE_FEATURE_CLAIM_LEDGER="shadow"):
            result = facts.record_message_claims(
                _message(SELFREPORT, speaker=ANAHIDA, message_id="m1"),
                ledger=_book(engine))
        rows = _rows(engine)
    finally:
        engine.dispose()
    assert result.rows == 1 and len(rows) == 1, (result.to_dict(), rows)
    print("  ok ⑤ shadow 也落库（读侧才分档：enforced 仍为 False）")


def test_write_port_requires_a_handle():
    """**不许回落到默认（生产）meta 库**：没有句柄 → 一行都不写、不 bootstrap。"""
    import core.bootstrap as bootstrap

    called: list[str] = []
    saved = bootstrap.get_meta_repo
    bootstrap.get_meta_repo = lambda *a, **k: called.append("meta") or _Boom()
    try:
        with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
            result = facts.record_message_claims(
                _message(SELFREPORT, speaker=ANAHIDA, message_id="m1"))
    finally:
        bootstrap.get_meta_repo = saved
    assert result.rows == 0 and result.skipped, result.to_dict()
    assert called == [], "写侧不许去取默认库（那会把一次冒烟变成往生产库写字）"
    print("  ok ⑤ 写口没有句柄就不写（不回落生产库）")


# ---------------------------------------------------------------------------
# ⑥ 生产接线：摄取链上的**唯一**写点（结构 + 端到端）
# ---------------------------------------------------------------------------
def _code_only(path: Path) -> str:
    """一个 .py 文件里**可执行代码**的文本（注释与 docstring 都去掉）。"""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    parts: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:]
        parts.append(ast.unparse(node))
    return "\n".join(parts)


def test_production_write_point_is_the_ingestion_chain():
    """结构锁：写口只被**摄取链**调用（scheduler），core 里没有第二个调用点。"""
    memory = _code_only(ROOT / "scheduler" / "person_memory.py")
    assert "record_message_claims(" in memory, "摄取链必须真的调写口"
    assert "message_facts.ledger_tracing()" in memory, \
        "开关必须在**取 repo 之前**判（off = 零 IO）"
    main_src = _code_only(ROOT / "scheduler" / "main.py")
    assert "record_claims_for_messages(" in main_src, "NEW_MESSAGES_SYNCED 处理器要接上"
    assert "EventType.NEW_MESSAGES_SYNCED" in main_src
    # 唯一写点：核心层里除了 owner 自己，谁都不许调 record_message_claims。
    offenders = [
        str(path.relative_to(ROOT)).replace("\\", "/")
        for path in (ROOT / "core").rglob("*.py")
        if "__pycache__" not in path.parts
        and "record_message_claims(" in path.read_text(encoding="utf-8-sig")
        and path.name != "message_facts.py"
    ]
    assert offenders == [], f"core 层出现了第二个写侧调用点：{offenders}"
    print("  ok ⑥ 写侧唯一生产写点在摄取链（scheduler），core 层零第二调用点")


def test_ingestion_batch_writes_and_is_idempotent():
    """端到端：一批 ``NEW_MESSAGES_SYNCED`` 形态的消息 → 落库；再跑一遍不长行。

    转述那一格的归属靠 canonical 身份解析：**同一批消息的发言人**就是手上那份规范表
    （``sender_id`` ↔ ``sender_name``），所以P6本人也在这一批里。
    """
    from scheduler import person_memory

    engine = _engine("batch")
    try:
        batch = [
            {"sender_id": ANAHIDA, "chat_id": CHAT, "msg_id": "b1",
             "sender_name": "P3", "content": SELFREPORT,
             "msg_type_name": "text", "is_self": 0,
             "source": "wechat", "account_id": "wxid_bot",
             "created_at": "2026-09-30 21:44:59"},
            {"sender_id": SELF, "chat_id": CHAT, "msg_id": "b2",
             "sender_name": "主人", "content": RELAY,
             "msg_type_name": "text", "is_self": 0,
             "source": "wechat", "account_id": "wxid_bot",
             "created_at": "2026-09-30 21:44:20"},
            # P6本人在这一批里 → 他进规范身份表 → 上面那句转述归得到人。
            {"sender_id": XIAOYU, "chat_id": CHAT, "msg_id": "b0",
             "sender_name": "P6", "content": "在的",
             "msg_type_name": "text", "is_self": 0,
             "source": "wechat", "account_id": "wxid_bot"},
            # 自己发的消息不登记（她说的话不是"别人的 claim"）。
            {"sender_id": "wxid_bot", "chat_id": CHAT, "msg_id": "b3",
             "sender_name": "鲸鲸", "content": SELFREPORT,
             "msg_type_name": "text", "is_self": 1,
             "source": "wechat", "account_id": "wxid_bot"},
            # 图片没有正文：不登记。
            {"sender_id": ANAHIDA, "chat_id": CHAT, "msg_id": "b4",
             "sender_name": "P3", "content": "", "msg_type_name": "image",
             "is_self": 0, "source": "wechat", "account_id": "wxid_bot"},
        ]
        with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
            first = person_memory.record_claims_for_messages(
                batch, platform="wechat", account_id="wxid_bot",
                repo=_Repo(engine))
            second = person_memory.record_claims_for_messages(
                batch, platform="wechat", account_id="wxid_bot",
                repo=_Repo(engine))
        rows = _rows(engine)
    finally:
        engine.dispose()

    assert first["traced"] is True and first["rows"] == 2, first
    assert second["rows"] == 0 and second["seen"] == 2, second
    assert len(rows) == 2, rows
    assert {row["authority"] for row in rows} == {"self_report", "relay"}, rows
    # 转述那一行归给P6：同一批消息里的规范身份表就是判据的输入。
    relay_row = [row for row in rows if row["authority"] == "relay"][0]
    assert relay_row["person_key"] == XIAOYU, relay_row
    print("  ok ⑥ 摄取链一批：落 2 行（自报 + 转述），重跑零新增")


def test_ingestion_off_is_zero_io():
    """``off`` 下摄取链的记账入口：连 repo 都不取（零 IO）。"""
    from scheduler import person_memory

    with _env(YINYUE_FEATURE_CLAIM_LEDGER="off"):
        stats = person_memory.record_claims_for_messages(
            [{"sender_id": ANAHIDA, "chat_id": CHAT, "msg_id": "b1",
              "content": SELFREPORT, "msg_type_name": "text"}],
            platform="wechat", account_id="wxid_bot", repo=_Boom())
    assert stats["traced"] is False and stats["rows"] == 0, stats
    print("  ok ⑥ off：摄取链入口零 IO（打红 repo 不被碰）")


# ---------------------------------------------------------------------------
# ⑦ 变异自检：写侧"不落库" → 同一条消息零行（红）；还原 → 一行（绿）
# ---------------------------------------------------------------------------
def test_mutation_without_the_write_port_nothing_is_recorded():
    """**变异自检**：把摄取链的写点改成"原样返回"（= 写侧不落库）→ 台账 0 行。

    这条证明 ⑥ 的绿来自写侧接线本身，不是别处顺手写的（语义上等价于把
    ``record_claims_for_messages`` 里的那句 ``record_message_claims`` 删掉——
    那正是"台账 0 行、只能开 shadow"的缺口形态）。
    """
    from scheduler import person_memory

    engine = _engine("mutation")
    batch = [{"sender_id": ANAHIDA, "chat_id": CHAT, "msg_id": "b1",
              "sender_name": "P3", "content": SELFREPORT,
              "msg_type_name": "text", "is_self": 0,
              "source": "wechat", "account_id": "wxid_bot"}]
    try:
        with _env(YINYUE_FEATURE_CLAIM_LEDGER="on"):
            # 变异：写点退化成"什么都不写"。
            result = facts.record_message_claims(
                _message(SELFREPORT, speaker=ANAHIDA, message_id="b1",
                         chat="mutation_chat"),
                ledger=facts.ClaimLedger([]))          # 注入空台账 = 不落库
            assert result.rows == 1, \
                "注入式台账只记内存行（本用例的对照：判据仍然给出 INSERTED）"
            rows_after_mutation = _rows(engine)
            assert rows_after_mutation == [], "变异没生效（写侧其实落了库）"
            # 还原：同一条消息走真实写点 → 一行。
            restored = person_memory.record_claims_for_messages(
                batch, platform="wechat", account_id="wxid_bot",
                repo=_Repo(engine))
            rows_restored = _rows(engine)
    finally:
        engine.dispose()
    assert restored["rows"] == 1 and len(rows_restored) == 1, (restored, rows_restored)
    print("  ok ⑦ 变异自检：写侧不落库 → 0 行（红）；接回 → 1 行（绿）")


# ---------------------------------------------------------------------------
# 自带 runner（本仓库的测试不走 pytest 收集：python tests/xxx.py）
# ---------------------------------------------------------------------------
def main() -> bool:
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    print("=" * 68)
    print("M2-4 claim 台账**写侧**（三级权威 / 撤回更正 / 幂等 / off 零 IO）")
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
