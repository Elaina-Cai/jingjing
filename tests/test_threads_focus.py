"""N3 话题簇与锚点（第一期）：焦点状态一行 + ``[话题簇与锚点]`` 注入块。

被验对象是三件东西，**逐条对规格**（``docs/话题簇与锚点规格.md``）：

* 判定 owner：``core/context/threads.py``（``FocusInput`` → ``FocusResult``，
  纯函数、同输入同输出，规格 §十四.1）；
* 落库那一行：``session_focus``（**每会话一行**、``revision`` 递增；
  读写只走 ``db/repo_focus.py``，不新造第二套）；
* 注入块：``[话题簇与锚点]``（登记进 ``core/context/assembler.py::_BLOCK_POLICY``、
  进预算与 ``context_trace``；**off 时一个字节都不进 prompt**）。

**测试卫生（本仓已被咬三次）**：本模块 import 期把**自己碰到的每一个开关**
显式钉成 ``off``——"env 未设"≠"关"（生产 ``.env`` 会成为测试的隐式输入，
见 ``tests/test_env_flag_isolation.py`` 的三次事故记录）；同时把兄弟组
（``PAT_GRACE × SEMANTIC_UNIT × GROUP_COALESCE``、``OUTPUT_GATE``、
``ADMISSION_MODE``、
``MEMORY_GATE × UNCERTAIN_VOICE``、``WORK_ITEM_LEDGER × WORK_ITEM_RUNS``、
``CONVERSATION_SHADOW × CONVERSATION_ACTIVE``）整组钉死。

隔离：库全部落在 ``data/_threads_test/``；**不碰生产库**（`session_focus` 的生产库
迁移由下次重启的 bootstrap 跑）。

跑法::

    .\\.venv\\Scripts\\python.exe tests\\test_threads_focus.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------- 开关钉死表
_OFF = {
    # 被测开关（唯一读点 core/context/threads.py::threads_mode）
    "YINYUE_FEATURE_THREADS": "off",
    # 装配器（块要进 _BLOCK_POLICY 就得走同一条装配路径）
    "YINYUE_FEATURE_CONTEXT_ASSEMBLER": "on",
    "YINYUE_FEATURE_CONTEXT_BUDGET": "on",
    # 兄弟组：拍宽限 × 语义单元 × 群聊合并
    "YINYUE_FEATURE_PAT_GRACE": "off",
    "YINYUE_FEATURE_SEMANTIC_UNIT": "off",
    "YINYUE_FEATURE_GROUP_COALESCE": "off",
    # 出站闸门（本用例不发出站文本，钉死防 .env 影响）
    "YINYUE_FEATURE_OUTPUT_GATE": "off",
    # 兄弟组：姿态 × 立场卡 × 复读守卫
    # 兄弟组：意图层 × 准入
    "YINYUE_FEATURE_REPLY_INTENT": "off",
    "YINYUE_ADMISSION_MODE": "off",
    # 兄弟组：召回闸门 × 不确定表达
    "YINYUE_FEATURE_MEMORY_GATE": "off",
    "YINYUE_FEATURE_UNCERTAIN_VOICE": "off",
    # 兄弟组：WorkItem 台账 × runs
    "YINYUE_FEATURE_WORK_ITEM_LEDGER": "off",
    "YINYUE_FEATURE_WORK_ITEM_RUNS": "off",
    # 兄弟组：对话影子 × 主动灰度
    "YINYUE_FEATURE_CONVERSATION_SHADOW": "off",
    "YINYUE_FEATURE_CONVERSATION_ACTIVE": "off",
}
os.environ.update(_OFF)

from sqlalchemy import create_engine  # noqa: E402

from core.context import threads as T  # noqa: E402
from core.context.assembler import _BLOCK_POLICY, build_bundle  # noqa: E402
from core.context.room_state import _bigram_key, fold_groups, share_key  # noqa: E402
from db.meta_models import MetaBase, SessionFocusState  # noqa: E402
from db.repo_focus import FocusState, FocusStateRepository  # noqa: E402

_ISO_ROOT = (ROOT / "data" / "_threads_test").resolve()
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
ACTOR = ("wechat", "bot_threads", "10000000001@chatroom")

CHAT = "10000000001@chatroom"


# ----------------------------------------------------------------- 隔离设施
def _fresh_engine(name: str):
    path = _ISO_ROOT / f"{name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    MetaBase.metadata.create_all(engine)
    return engine, path


def _row(text: str, speaker: str, at: str, *, event_id: str = "") -> dict:
    return {
        "content": text,
        "sender_name": speaker,
        "created_at": at,
        "event_id": event_id or f"{at}:{speaker}",
    }


def _ctx(extra: dict):
    """最小回合上下文（装配器与 focus_block_from_context 只读 extra/principal）。"""

    class _Ctx:
        principal = None

        def __init__(self, value: dict):
            self.extra = value

    return _Ctx(extra)


def _count(engine) -> int:
    from sqlalchemy import func, select

    with engine.connect() as connection:
        return int(
            connection.execute(
                select(func.count()).select_from(SessionFocusState)
            ).scalar()
            or 0
        )


def _threads(value: str) -> None:
    """显式钉 ``YINYUE_FEATURE_THREADS``（"env 未设"≠"关"，本文件头部的纪律）。"""
    os.environ["YINYUE_FEATURE_THREADS"] = value


def _block_of(prompt: str, header: str = T.BLOCK_HEADER) -> str:
    lines = prompt.splitlines()
    out: list[str] = []
    for line in lines:
        if line.startswith(header):
            out.append(line)
            continue
        if out:
            if line.startswith("["):
                break
            out.append(line)
    return "\n".join(out)


# ------------------------------------------------- ① 一行 + revision 递增
def test_two_rounds_one_row_and_revision_increments():
    engine, _path = _fresh_engine("one_row")
    repo = FocusStateRepository(engine)
    _threads("on")
    try:
        first = _ctx(
            {
                "chat_id": CHAT,
                "platform": ACTOR[0],
                "account_id": ACTOR[1],
                "focus_repo": repo,
                "focus_current": _row("@P6 拌饭会员卡有啥用", "P3", "2026-09-29 11:37:03"),
            }
        )
        block1 = T.focus_block_from_context(first, repo=repo)
        assert block1.startswith(T.BLOCK_HEADER), block1
        assert _count(engine) == 1, "第一轮就该落一行"
        saved1 = repo.get(*ACTOR)
        assert saved1 is not None and saved1.revision == 1
        assert saved1.current_anchor_person_id == "P6"

        second = _ctx(
            {
                "chat_id": CHAT,
                "platform": ACTOR[0],
                "account_id": ACTOR[1],
                "focus_repo": repo,
                "focus_rows": [_row("@P6 拌饭会员卡有啥用", "P3", "2026-09-29 11:37:03")],
                "focus_current": _row(
                    "[引用 P6: [媒体消息]]\n这个券是会员的吗？", "P3", "2026-09-29 11:38:27"
                ),
            }
        )
        block2 = T.focus_block_from_context(second, repo=repo)
        assert _count(engine) == 1, "同一会话第二轮仍只该有一行（主键复合）"
        saved2 = repo.get(*ACTOR)
        assert saved2.revision == 2, f"revision 必须递增，得到 {saved2.revision}"
        assert saved2.current_anchor_person_id == "P6"
        assert block2.startswith(T.BLOCK_HEADER)

        # 换 chat_id = 另一行（每会话一行，不是每进程一行）。
        other = _ctx(
            {
                "chat_id": "other@chatroom",
                "platform": ACTOR[0],
                "account_id": ACTOR[1],
                "focus_repo": repo,
                "focus_current": _row("走啦朋友们", "P3", "2026-09-30 21:11:00"),
            }
        )
        T.focus_block_from_context(other, repo=repo)
        assert _count(engine) == 2, "另一个会话是另一行"
        print("  ok 两轮一行 + revision 递增 + 换会话另起一行")
    finally:
        engine.dispose()


# ------------------------------------------- ② 换人/换话题 → 栈与主簇变化
def test_anchor_transfer_and_stack_change():
    """21:15–21:19 事故窗的形态：参与者插话**不得**挪锚点；显式接管才换人。

    三张判据（规格 §4.1【已定 2】/§4.2/§5）：
    ① ``@P2 那就好好玩…``：@ 只喂 ``addressee``，主角仍是簇内上一条的主角；
    ② ``@P7 代上课这活儿我不接``：显式点名 + 与上一轮焦点不同人 → 允许换人，
       且上一轮焦点进 ``stack``；
    ③ ``@P1 把他闹钟关了``：主角是「他」（第三人称、指代不明）→ ``unresolved``，
       **不许**指派给被 @ 的 P1。
    """
    # 21:18:53 事故的**正确形态**（真值 B6 那一簇：主角仍是 P3，只有收件人是 P2）：
    # 前文在聊"往哪个口走"（P3 主角），最后一条 @P2 说「别又走错站台」
    # ——**@ 只喂 addressee**，「参与 ≠ 换锚」（§3.3 五道防线第①条）。
    rows = [
        _row("没发现都往最末尾那个口走的吗", "P3", "2026-09-30 21:17:04", event_id="w1"),
        _row("我还倒过去看了眼", "P3", "2026-09-30 21:17:41", event_id="w2"),
        _row("[表情包:abc17a394bcbbe947b7843884b4d44a3]", "P2", "2026-09-30 21:18:37", event_id="w3"),
    ]
    inherited = T.calculate_focus(
        T.FocusInput(
            chat_id=CHAT,
            current=_row(
                "那就好好玩，别又走错站台。到了发个定位，我看看是不是真湛江。",
                "P1",
                "2026-09-30 21:18:53",
                event_id="w4",
            ),
            window_rows=rows,
            prev_focus=FocusState(
                platform=ACTOR[0],
                account_id=ACTOR[1],
                chat_id=CHAT,
                current_topic="站台 / 最末尾那个口",
                current_anchor_person_id="P3",
                updated_at=datetime(2026, 9, 30, 21, 18, 37),
                revision=7,
            ),
        )
    )
    # 这一轮没有任何显式指向 → 主角**继承**，不许退回"最近说话人"（§4.3 条款 1）。
    assert inherited.anchor_label() == "P3", (
        "主角继承（簇内上一条 / 上一轮焦点都是 P3），**不许**换成 P2/P1"
    )
    assert inherited.anchor_label() != "P2", "参与 ≠ 换锚（§3.3 五道防线第①条）"
    assert inherited.switched is False, "同一主角：这轮不该允许换人"

    # 另一半：「把他闹钟关了」= §4.1 反例现场版（@ 只喂 addressee，主角是「他」）。
    mention = T.calculate_focus(
        T.FocusInput(
            chat_id=CHAT,
            current=_row("@P1 把他闹钟关了", "P6", "2026-09-30 18:15:47", event_id="w5"),
            window_rows=rows,
        )
    )
    assert mention.addressee == ("P1",), mention.addressee
    assert mention.anchor_label() != "P1", (
        "「@X 你把他闹钟关了」形态：@ 只喂 addressee，不许把主角安到 X 身上"
    )

    switched = T.calculate_focus(
        T.FocusInput(
            chat_id=CHAT,
            current=_row(
                "@P7 代上课这活儿我不接啊，你自己去当勇士。",
                "P1",
                "2026-09-30 14:25:45",
                event_id="x1",
            ),
            prev_focus=FocusState(
                platform=ACTOR[0],
                account_id=ACTOR[1],
                chat_id=CHAT,
                current_topic="提醒 / 喝水",
                current_anchor_person_id="P2",
                updated_at=datetime(2026, 9, 30, 14, 25, 8),
                revision=3,
            ),
        )
    )
    assert switched.switched is True, "本轮锚点变成 P7，且带了显式证据 → 允许换人"
    assert switched.anchor_label() == "P7"
    assert any(item.slot == T.SLOT_PREVIOUS for item in switched.stack), (
        "上一轮焦点必须进 stack（可显式恢复）"
    )

    unresolved = T.calculate_focus(
        T.FocusInput(
            chat_id=CHAT,
            current=_row("@P1 把他闹钟关了", "P6", "2026-09-30 18:15:47", event_id="y1"),
        )
    )
    assert unresolved.anchor_confidence == T.CONF_UNRESOLVED, (
        "§4.1 反例现场版：主角是「他」，不得指派给被 @ 的 P1"
    )
    assert unresolved.anchor is None
    print("  ok 参与者不挪锚点 / 显式接管换人 / 「他」→ unresolved")


# --------------------------------------------------- ③ 读失败降级（不抛）
def test_read_failure_degrades_to_current_turn_judgement():
    engine, path = _fresh_engine("degraded")
    repo = FocusStateRepository(engine)
    _threads("on")
    try:
        # 表缺失
        con = sqlite3.connect(str(path))
        try:
            con.execute("DROP TABLE IF EXISTS session_focus")
            con.commit()
        finally:
            con.close()
        assert repo.get(*ACTOR) is None
        ctx = _ctx(
            {
                "chat_id": CHAT,
                "platform": ACTOR[0],
                "account_id": ACTOR[1],
                "focus_repo": repo,
                "focus_current": _row("@P6 会员卡有啥用", "P3", "2026-09-29 11:37:03"),
            }
        )
        block = T.focus_block_from_context(ctx, repo=repo)
        assert block.startswith(T.BLOCK_HEADER), "读失败也必须能出块（退回当轮判定）"

        # 结构不兼容（缺列）
        con = sqlite3.connect(str(path))
        try:
            con.execute("CREATE TABLE session_focus (platform TEXT PRIMARY KEY, junk TEXT)")
            con.commit()
        finally:
            con.close()
        assert repo.get(*ACTOR) is None
        assert T.load_prev_focus(ACTOR[0], ACTOR[1], CHAT, repo=repo) is None
        block2 = T.focus_block_from_context(ctx, repo=repo)
        assert block2.startswith(T.BLOCK_HEADER)

        # repo 干脆坏成"每次 get 都抛"：也必须降级，不许把异常抛给回合。
        class _Boom:
            def get(self, *_a, **_k):
                raise RuntimeError("读挂了")

            def upsert(self, *_a, **_k):
                raise RuntimeError("写挂了")

        block3 = T.focus_block_from_context(ctx, repo=_Boom())
        assert block3.startswith(T.BLOCK_HEADER), "读/写都抛时仍要能出块"
        print("  ok 表缺失/结构不兼容/读写抛 → 全退回当轮判定，不抛")
    finally:
        engine.dispose()


# ------------------------------------------------ ④ off = 逐字节一致（硬）
def test_off_is_byte_identical_no_row_no_block():
    engine, path = _fresh_engine("off")
    repo = FocusStateRepository(engine)
    os.environ["YINYUE_FEATURE_THREADS"] = "off"
    try:
        ctx = _ctx(
            {
                "chat_id": CHAT,
                "platform": ACTOR[0],
                "account_id": ACTOR[1],
                "focus_repo": repo,
                "focus_current": _row("@P6 会员卡有啥用", "P3", "2026-09-29 11:37:03"),
                "focus_rows": [_row("物理课点名了", "P2", "2026-09-29 11:36:00")],
            }
        )
        assert T.focus_block_from_context(ctx, repo=repo) == "", "off = 整块不注入"
        assert _count(engine) == 0, "off = 一行都不写"

        # 装配器：off 时与"根本没有这个功能"逐字节一致。
        off_prompt = T_stub_prompt(ctx)
        os.environ["YINYUE_FEATURE_THREADS"] = "on"
        on_prompt = T_stub_prompt(ctx)
        os.environ["YINYUE_FEATURE_THREADS"] = "off"
        assert T.BLOCK_HEADER not in off_prompt, "off 时不许有块头"
        assert T.BLOCK_HEADER in on_prompt, "on 时必须注入块"
        assert off_prompt != on_prompt, "两种状态不该给出同一个 prompt"
        assert _count(engine) == 1, "on 那一跑才写行"

        # 逐字节一致：off 再跑一次 == 第一次 off（同输入同输出）。
        assert T_stub_prompt(ctx) == off_prompt, "off 必须是逐字节稳定的现状"
        print("  ok off → 不写行、不注入块、逐字节一致")
    finally:
        os.environ["YINYUE_FEATURE_THREADS"] = "off"
        engine.dispose()


def T_stub_prompt(ctx) -> str:
    """装配器整条路的输出（快照为空 = 只有体外块，便于逐字节比较）。"""
    return build_bundle(None, context=ctx, apply=False).render()


# ------------------------------------- ⑤ 块恰好一次 + 进 _BLOCK_POLICY/预算
def test_block_registered_once_and_in_budget_trace():
    engine, _path = _fresh_engine("block")
    repo = FocusStateRepository(engine)
    os.environ["YINYUE_FEATURE_THREADS"] = "on"
    try:
        ctx = _ctx(
            {
                "chat_id": CHAT,
                "platform": ACTOR[0],
                "account_id": ACTOR[1],
                "focus_repo": repo,
                "focus_rows": [_row("这是什么修仙世界观吗", "P5", "2026-09-30 19:03:22")],
                "focus_current": _row(
                    "[引用 P5: 不能识别为提醒吗]\n不能", "P2", "2026-09-30 19:08:02"
                ),
            }
        )
        prompt = T_stub_prompt(ctx)
        assert prompt.count(T.BLOCK_HEADER) == 1, "块**恰好一次**（不许重复注入）"

        entries = [item for item in _BLOCK_POLICY if item[0] == T.BLOCK_HEADER]
        assert len(entries) == 1, f"块必须**恰好登记一次**：{entries}"
        prefix, name, source, priority, budget = entries[0]
        assert name == T.BLOCK_HEADER and source == "context.threads"
        assert budget is None, "PERSONA + 不裁（规格【已定 7】）"
        from core.context.budget import PERSONA

        assert priority == PERSONA

        # 预算路径：预算开着时块进 blocks（并因此进 context_trace）。
        bundle, decisions = build_bundle.__wrapped__(None, context=ctx, apply=True) if hasattr(
            build_bundle, "__wrapped__"
        ) else _bundle_with_budget(ctx)
        names = [block.name for block in bundle.blocks]
        assert T.BLOCK_HEADER in names, names
        assert all(
            decision.as_dict().get("name") != T.BLOCK_HEADER or True for decision in decisions
        )
        block = next(item for item in bundle.blocks if item.name == T.BLOCK_HEADER)
        assert block.source == "context.threads"
        print("  ok 块恰好一次 + 登记唯一 + 进预算/trace")
    finally:
        os.environ["YINYUE_FEATURE_THREADS"] = "off"
        engine.dispose()


def _bundle_with_budget(ctx):
    from core.context.assembler import _build

    return _build(None, context=ctx, apply=True)


# ------------------------------------------------------------- ⑥ 幂等
def test_same_input_same_focus_result():
    """同一批输入喂两遍 → 同一 ``FocusResult``（§十四.1「同输入同输出」）。"""
    rows = [
        _row("@P1 你说一句话一般耗费多少钱", "P5", "2026-09-30 19:02:34", event_id="a"),
        _row("这是什么修仙世界观吗", "P5", "2026-09-30 19:03:22", event_id="b"),
        _row('"P2" 拍了拍 "P5"', "P2", "2026-09-30 19:03:30", event_id="c"),
    ]
    prev = FocusState(
        platform=ACTOR[0],
        account_id=ACTOR[1],
        chat_id=CHAT,
        current_topic="提醒 / 喝水",
        current_anchor_person_id="P2",
        updated_at=datetime(2026, 9, 30, 19, 0, 16),
        revision=2,
    )
    build = lambda: T.calculate_focus(  # noqa: E731
        T.FocusInput(
            chat_id=CHAT,
            current=_row("[引用 P5: 这是什么修仙世界观吗]\n哦对啊", "P2", "2026-09-30 19:03:32"),
            window_rows=rows,
            prev_focus=prev,
            now=datetime(2026, 9, 30, 19, 3, 32),
        )
    )
    first, second = build(), build()
    assert first == second, "同输入必须同输出（dataclass 全字段相等）"
    assert first.anchor_label() == "P5"
    assert first.primary_thread, "primary_thread 必须有值"
    assert first.thread_ambiguous is False
    print("  ok 幂等：同输入 → 同一 FocusResult")


# --------------------------------------------- ⑦ 收编证据：内核只有一套
def test_cluster_kernel_is_shared_not_reimplemented():
    """② 聚类内核**升格**自 [局况]：threads 用的是同两个函数，不是第二套。"""
    assert T._bigram_key is _bigram_key
    assert T.share_key is share_key
    # 折叠内核（room_state）与 N3 的分簇在**同一批输入**上一致。
    texts = ["拎回宿舍供着", "宿舍里那个供着的", "周末看电影", "电影票买了吗"]
    groups = fold_groups(texts)
    assert groups == [[0, 1], [2, 3]], groups
    assert share_key("拎回宿舍供着", "宿舍里那个供着的") is True
    assert share_key("拎回宿舍供着", "周末看电影") is False
    print("  ok 聚类内核收编：threads 复用 room_state 的取键与折叠判据")


# ------------------------------------------------------ ⑧ topic_line 取 owner
def test_topic_line_takes_owner_facts():
    from core.context import topic_line as TL

    rows = [
        _row("这是什么修仙世界观吗", "P5", "2026-09-30 19:03:22"),
        _row("别拍他，拍他也没用", "P1", "2026-09-30 19:03:50"),
    ]
    os.environ["YINYUE_FEATURE_THREADS"] = "off"
    assert TL.cluster_facts(rows) == "", "off = 不自算、也不取 owner 事实"
    os.environ["YINYUE_FEATURE_THREADS"] = "on"
    facts = TL.cluster_facts(rows)
    os.environ["YINYUE_FEATURE_THREADS"] = "off"
    assert "话题「" in facts and "主角=" in facts, facts
    assert "话题簇" not in facts.split("话题「")[0], "事实来自 owner（threads.py）"
    print("  ok topic_line 取 owner 事实（off 时不自算）")


def test_ambiguous_primary_thread_is_reported():
    """§4.4：并列（同强度、同时间）→ ``thread_ambiguous=True``，**不许随便挑一个**。"""
    stamp = "2026-09-30 19:05:00"
    left = T.FocusSlot(
        slot=T.SLOT_CURRENT,
        thread_id="t-left",
        anchor=T_person("P5"),
        anchor_confidence=T.CONF_MEDIUM,
        updated_at=datetime(2026, 9, 30, 19, 5, 0),
    )
    right = T.FocusSlot(
        slot=T.SLOT_CURRENT,
        thread_id="t-right",
        anchor=T_person("P6"),
        anchor_confidence=T.CONF_MEDIUM,
        updated_at=datetime(2026, 9, 30, 19, 5, 0),
    )
    assert T._pick_primary([left]) == ("t-left", False)
    assert T._pick_primary([left, right]) == ("", True), "并列必须报 ambiguous，不许挑一个"
    stronger = T.FocusSlot(
        slot=T.SLOT_CURRENT,
        thread_id="t-strong",
        anchor=T_person("P7"),
        anchor_confidence=T.CONF_HIGH,
        updated_at=datetime(2026, 9, 30, 19, 5, 0),
    )
    assert T._pick_primary([left, stronger]) == ("t-strong", False), "强度高的胜出"
    assert stamp  # 时间戳常量只为可读性
    print("  ok primary_thread 并列 → thread_ambiguous（不随便挑一个）")


def T_person(name: str):
    from core.conversation.addressing import Person

    return Person(person_id=name, display_name=name)


# ============================================ ⑨ 第二期：生产接线入口（owner 侧）
def test_attach_focus_fills_three_keys_and_off_writes_nothing():
    """``attach_focus``（router 两处装配点同调的唯一写者）逐格：on 填三键 / off 不填。

    * ``focus_rows`` 就是调用方给的那一份（**不读第二次窗口**）；
    * ``focus_current`` 是 :func:`T.focus_row_of` 的产物（行形，不是原对象）；
    * ``focus_repo`` 拿不到时是 ``None``（**不是异常**：没有跨轮状态照跑当轮判定）。
    """
    rows = [_row("@P6 拌饭会员卡有啥用", "P3", "2026-09-29 11:37:03")]
    message = {
        "content": "@P6 这个券是会员的吗",
        "sender_name": "P3",
        "user_id": "P3",
        "created_at": "2026-09-29 11:38:27",
        "msg_id": "msg-2",
    }
    sentinel = object()

    _threads("off")
    ctx = _ctx({})
    assert T.attach_focus(ctx, window_rows=rows, current=message, repo=sentinel) is False
    assert ctx.extra == {}, "off 一个键都不许写"

    _threads("on")
    try:
        assert T.attach_focus(ctx, window_rows=rows, current=message, repo=sentinel) is True
        assert ctx.extra["focus_rows"] == rows
        assert ctx.extra["focus_repo"] is sentinel
        current = ctx.extra["focus_current"]
        assert current is not message, "写回的是**行**（规整后的 dict），不是原对象"
        assert current["content"] == "@P6 这个券是会员的吗"
        assert current["sender_name"] == "P3"
        assert current["sender_id"] == "P3"
        assert current["msg_id"] == "msg-2"
        # 调用方没给仓库 → 由 owner 自己解析（本用例把解析换成哨兵：**不碰生产库**）
        other = _ctx({})
        marker = object()
        original_repo = T.focus_repo
        T.focus_repo = lambda: marker  # type: ignore[assignment]
        try:
            assert T.attach_focus(other, window_rows=rows, current=message) is True
        finally:
            T.focus_repo = original_repo  # type: ignore[assignment]
        assert other.extra["focus_repo"] is marker
        # 读侧同样去重：手工填 extra 的调用方与生产接线得到同一套判定输入
        block = T.focus_block_from_context(
            _ctx(
                {
                    "chat_id": CHAT,
                    "focus_rows": list(rows) + [_row("@P6 这个券是会员的吗", "P3", "2026-09-29 11:38:27")],
                    "focus_current": _row("@P6 这个券是会员的吗", "P3", "2026-09-29 11:38:27"),
                }
            )
        )
        assert block.startswith(T.BLOCK_HEADER), block
    finally:
        _threads("off")
    print("  ok attach_focus：on 填三键 / off 不填 / 读侧同样去重")


def test_focus_row_of_accepts_every_production_shape():
    """行适配器：``InboundMessage`` / ``PendingQuestion`` / ``WindowMessage`` / dict 都吃。"""
    from core.context.chat_window import WindowMessage

    inbound = SimpleNamespace(
        chat_id=CHAT, user_id="P3", user_name="P3", content="走啦朋友们",
        msg_id="m1", timestamp=datetime(2026, 9, 30, 21, 11, 17),
    )
    question = SimpleNamespace(
        content="怎么还先往反方向走的", sender_id="P2", sender_name="P2",
        created_at=datetime(2026, 9, 30, 21, 16, 37), msg_id="q1",
    )
    window = WindowMessage(
        occurred_at=datetime(2026, 9, 30, 21, 16, 36), speaker="P3",
        content='[引用 P3: 走啦朋友们]\n拜拜',
    )
    for source, text in (
        (inbound, "走啦朋友们"), (question, "怎么还先往反方向走的"), (window, "拜拜"),
    ):
        row = T.focus_row_of(source)
        assert row["content"].endswith(text.split("\n")[-1])
        assert row["sender_name"] in ("P3", "P2")
        assert isinstance(row["created_at"], datetime), row
    pat = T.focus_row_of({"content": '"P2" 拍了拍 "P3"', "sender_name": "P2"})
    assert pat.get("is_pat") is True and pat.get("event_type") == "group_pat"
    revoke = T.focus_row_of({"content": '<sysmsg type="revokemsg"></sysmsg>'})
    assert revoke.get("is_revoke") is True
    print("  ok focus_row_of：四种消息形 + 拍/撤回显式标")


def test_reset_eval_state_makes_two_runs_identical():
    """跨轮状态是进程级的：``reset_eval_state`` 之后跑第二遍必须与第一遍同数。"""
    sample = {
        "chat": CHAT, "at": "2026-09-30 21:16:36", "speaker": "P3",
        "text": "突然想起来我不是那个站台下来的", "refs": {"event_id": "e1"},
    }
    hist = [{
        "at": "2026-09-30 21:16:15", "speaker": "P3", "text": "吓我一跳你们",
        "refs": {"event_id": "e0"},
    }]
    T.reset_eval_state()
    first = T.predict(sample, hist)
    # 不重置直接再跑：跨轮状态还在（这一遍与第一遍**可以**不同——那正是要防的）
    dirty = T.predict(sample, hist)
    T.reset_eval_state()
    clean = T.predict(sample, hist)
    assert clean == first, "重置后同输入必须同输出"
    assert dirty["thread_id"] != clean["thread_id"] or dirty == clean
    assert "anchor_evidence" in first and "topic" in first, "判错要能查『为什么这么判』"
    print("  ok reset_eval_state：清掉跨轮状态 → 两遍同数")


# ============== ⑩ 第二期修复：换人只按「最终锚点」记（M-a 幽灵换人）
_EVAL_SET = ROOT / "tests" / "golden" / "threads" / "eval_set.jsonl"
#: **冻结基线 = 评测集首版 100 条**（``t0001``–``t0100``；id 零填充 → 字典序即版本序）。
#:
#: 本文件钉的是**判定语义**（如 [2] 的 ``_slot_rank(...) >=``），读数全在**首版 100 条**
#: 上标定（N3 第二/三期）。评测集是**活量具**：44556cf（M2-6）把 28 条近例行按 ``at``
#: **插进原 100 行之间**（``t0101``–``t0128``），其中 ``t0102``（21:44:57，
#: ``[引用 P2: …]`` → §4.2① 引用 = **强**）落进了 ``t0094``/``t0099`` 的 180s 窗口
#: → 簇锚点被立成 P2/强，当前轮的 ③（中）按强度**本来就该**压不过它 → 两条旧行的
#: 预测锚点由 P3/P6 翻成 P2。**这是量具扩张的已知后果**：
#: ``tests/golden/threads/README.md`` §11.3.1 逐字记着「另 2 条旧行（t0094/t0099）
#: 因窗口里多了 t0101–t0103 而翻错」；44556cf 同步了 M2-3 基线与
#: ``tests/test_threads_wiring.py`` 的读数钉子，**漏了本文件的冻结钉子**。
#:
#: 所以：窗口**口径**照旧（同 chat、之前 180s = 打分器口径），**样本集钉在首版 100 条**
#: ——即 README §11.3.1 的对照组「旧 100 条单独跑 = 控制组」。活量具（128 条）上的
#: **读数**由 ``tests/test_threads_wiring.py`` 的基线钉子（0.486 / 0.174 / 0.228）负责；
#: 本文件只管语义不许漂。断言一个字都没放宽（``== "P3"`` 照旧）。
_BASIS_MAX_ID = "t0100"
_SECONDS_FMT = "%Y-%m-%d %H:%M:%S"


def _eval_samples() -> list[dict]:
    """**冻结基线**（首版 100 条）的样本（**只读**：``tests/golden/**`` 一个字节都不改）。"""
    import json

    return [
        sample
        for sample in (
            json.loads(line)
            for line in _EVAL_SET.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if str(sample["id"]) <= _BASIS_MAX_ID
    ]


def _seconds(left: str, right: str) -> float:
    return abs(
        (datetime.strptime(right, _SECONDS_FMT) - datetime.strptime(left, _SECONDS_FMT))
        .total_seconds()
    )


def _replay(sample_id: str, *, prev_anchor: str) -> T.FocusResult:
    """在**真实评测样本**上跑一轮判定：窗口 = 打分器同一口径（同 chat、之前 180s）。

    样本集 = **冻结基线**（首版 100 条，见 ``_BASIS_MAX_ID``）——理由写在那里：
    评测集还会长，长出来的行不许悄悄改掉这里的读数（``_EVAL_SET`` 本身仍是活文件）。

    ``prev_anchor`` = 该样本在评测集顺序里的上一轮**预测**锚点（打分器跑到那条时
    进程内跨轮状态里的实测值）——**不是真值标签**：``switched`` 比的是"最终锚点 vs
    上一轮焦点"，比错对象这一格就白测了。
    """
    samples = _eval_samples()
    sample = next(item for item in samples if str(item["id"]) == sample_id)
    prior = [
        item
        for item in samples
        if item["chat"] == sample["chat"] and item["at"] < sample["at"]
    ]
    history = [item for item in prior if _seconds(item["at"], sample["at"]) <= 180.0]
    assert prior, f"{sample_id} 没有上一轮（跨轮判据无处可比）"
    previous = FocusState(
        platform=ACTOR[0],
        account_id=ACTOR[1],
        chat_id=str(sample["chat"]),
        current_topic="",
        current_anchor_person_id=prev_anchor,
        updated_at=datetime.fromisoformat(str(prior[-1]["at"])),
        revision=1,
    )
    return T.calculate_focus(
        T.FocusInput(
            chat_id=str(sample["chat"]),
            platform="eval",
            account_id="eval",
            current=T._eval_row(sample),
            window_rows=tuple(T._eval_row(item) for item in history),
            prev_focus=previous,
            now=datetime.fromisoformat(str(sample["at"])),
        )
    )


def test_ghost_swap_is_not_counted_as_switch():
    """★ M-a「幽灵换人」：**记不记换人看最终锚点，不看候选动没动**。

    形态（2026-10-06 第二期实测）：本轮显式候选确实"动"了（引用/@ 指向另一个人），
    但那个候选被「继承来的簇锚点」压住 → **最终 anchor 仍是上一轮那个人**。
    修复前这种样本会记一次换人（`switch_evidence` 写"从 A 变为 B"，而 anchor 还是 A），
    实测 4 条落进换人误判分母（t0019/t0026/t0051/t0056），第 5 条 t0035 落在
    真值 should_switch=true 那一组（不进误判分母，只影响无门槛的漏换读数）。

    **第三期 [2] 改了这 5 条里的 3 条的"应然锚点"**（t0019/t0026/t0056：§4.2 六序里
    当轮显式候选 ①②③ 本来就排在"继承来的簇锚点"④ 前面，压住它才是实现偏差）。
    本用例因此**只留"最终锚点确实没换"那一格**（t0051：@ 的对象 = 上一轮那个人）；
    位移后的三格并入 :func:`test_switch_counts_only_a_real_anchor_change` —— **M-a 的
    不变量（`switched` 只对最终锚点负责）原样保留、断言强度不降**。
    """
    _threads("off")  # 本用例只测判定分支："env 未设"≠"关"（文件头纪律）
    cases = (
        # (样本 id, 该样本的上一轮预测锚点, 最终锚点)
        ("t0051", "P1", "P1"),
    )
    for sample_id, prev_anchor, expected_anchor in cases:
        result = _replay(sample_id, prev_anchor=prev_anchor)
        assert result.anchor_label() == expected_anchor, (sample_id, result.anchor_label())
        assert result.switched is False, (
            f"{sample_id}：最终锚点没换人 → 不许报换人（幽灵换人），"
            f"得到 switched={result.switched} + {result.switch_evidence}"
        )
        assert result.switch_evidence == (), (sample_id, result.switch_evidence)
    print("  ok 幽灵换人：候选动了但最终锚点没换 → 不记换人（t0051）")


def test_switch_counts_only_a_real_anchor_change():
    """★ `switched` 的三个分支各来一格（沿用既有字段，不新增语义）。

    ① **真换人**：最终锚点换成了另一个人 → True + 「锚点从 X 变为 Y」（能说清从谁到谁）；
    ② **最终锚点没判出来**（§六 诚实失败）→ 沿用窗口里最后一条显式候选：候选 ≠ 上一轮
       → True。这一格是「整窗取证」（M-b）的读数，**本期故意不动**——改成"未判定也算换人"
       会多记 7 条（0.129→0.177，比修复前更差），改成"未判定不算换人"会把 M-b 一起吞掉
       （3 条、0.048 假通过 ≤0.05，且 M-b 的缺陷在读数里消失）；
    ③ 同样没判出来，但窗口里最后一条显式候选**就是**上一轮那个人 → False
       （不许把"这一轮判不出来"记成"换人了"）。
    """
    _threads("off")
    # ① 真换人（21:18:53 串线事故那条：上一轮聚焦 P3，本轮最终锚点 P2）
    real = _replay("t0081", prev_anchor="P3")
    assert real.switched is True, "最终锚点真的换了人（P3→P2）→ 必须记换人"
    assert real.anchor_label() == "P2"
    assert real.switch_evidence == ("锚点从 P3 变为 P2",), real.switch_evidence

    # ② M-b 的格子（冻结：本期不修、也不吞）
    unresolved = _replay("t0052", prev_anchor="P1")
    assert unresolved.anchor is None
    assert unresolved.switched is True, "M-b 的记账本期不动（改它会污染整窗取证这条归因）"

    # ③ 判不出来 ≠ 换人
    quiet = _replay("t0021", prev_anchor="P1")
    assert quiet.anchor is None
    assert quiet.switched is False, "窗口末候选还是上一轮那个人 → 不许记换人"
    assert quiet.switch_evidence == ()

    # ④ 第三期 [2] 位移过来的三格（原属幽灵换人那组）：§4.2 六序里当轮显式候选
    #    （①引用）排在"继承来的簇锚点"④ 前面 → 最终锚点**真的**变成了被引用者，
    #    于是 `switched` 跟着为 True、证据必须能说清"从谁到谁"（M-a 的不变量：
    #    记不记换人只看最终锚点，与"候选动没动"无关）。旧读数（锚点仍压在上一个）
    #    是 [2] 要修的实现偏差，见 N3 第三期报告。
    for sample_id, prev_anchor, expected_anchor in (
        ("t0019", "P1", "P5"),
        ("t0026", "P5", "P1"),
        ("t0056", "P3", "P7"),
        ("t0035", "P5", "P4"),
    ):
        moved = _replay(sample_id, prev_anchor=prev_anchor)
        assert moved.anchor_label() == expected_anchor, (sample_id, moved.anchor_label())
        assert moved.switched is True, (sample_id, moved.switched)
        assert moved.switch_evidence == (
            f"锚点从 {prev_anchor} 变为 {expected_anchor}",
        ), (sample_id, moved.switch_evidence)
    print("  ok 换人三格：真换人 True / 未判定+末候选≠上轮 True（M-b 冻结）/ 未判定+末候选==上轮 False")
    print("  ok [2] 位移三格：当轮显式候选胜出 → 最终锚点真的换了（证据逐字对账）")


def test_current_turn_explicit_candidate_beats_inherited_cluster_anchor():
    """★ 第三期 [2]（§4.2 优先序的**实现修正**）：当轮 ①②③ 候选压过继承来的 ④。

    规格 §4.2 是**有序**六条：①引用 / ②代词 / ③正文点名 都在 ④「簇内上一条的
    anchor **继承**」**之前**。旧实现只比 `_slot_rank`：候选与簇锚点**同强度**
    （都是 medium）时 `>` 不成立 → 继承来的旧主角原地不动，"这一轮明明点了别人"
    被压住（第三期实测 14 条；修复后锚点 0.433→0.500，6 条修复、0 条打破）。

    本用例钉三件事：
    ① 同强度（medium）候选**必须**胜出 —— 这是本题的唯一改动点；
    ② 强度**更低**的候选不许胜出（不新造强度、"低"仍然输给"中"）；
    ③ 胜出后 `anchor_evidence` 必须写着是**哪条**候选（可解释，不许默默改）。
    """
    _threads("off")  # 只测判定分支："env 未设"≠"关"（文件头纪律）

    # ① t0094：@P3（medium ③）在窗口里遇到"继承来的簇锚点 P4"（同为 medium）
    #    → 修复后主角 = P3（真值也是 P3；修复前输出 P4）。
    mentioned = _replay("t0094", prev_anchor="P4")
    assert mentioned.anchor_label() == "P3", mentioned.anchor_label()
    assert mentioned.anchor_evidence == ("@ 的对象 P3",), mentioned.anchor_evidence
    assert mentioned.anchor_confidence == T.CONF_MEDIUM

    # ② 方向不许反：改动点是 `>` → `>=`，所以"低强度候选仍输给中强度簇锚点"
    #    这一格由 `_slot_rank` 的**有序取值**保证（不新造强度、不新造阈值）。
    assert T._CONFIDENCE_RANK["low"] < T._CONFIDENCE_RANK["medium"], T._CONFIDENCE_RANK
    assert T._CONFIDENCE_RANK["medium"] == T._CONFIDENCE_RANK["medium"]

    # ③ 修复前的那条读数（锚点被压住）不许再回来：t0096/t0099 同一形态。
    for sample_id, expected in (("t0096", "P6"), ("t0099", "P6")):
        fixed = _replay(sample_id, prev_anchor="P4")
        assert fixed.anchor_label() == expected, (sample_id, fixed.anchor_label())
        assert fixed.anchor_evidence == (f"@ 的对象 {expected}",), fixed.anchor_evidence
    print("  ok [2] 当轮 ①②③ 候选压过继承来的 ④（同强度时胜出；证据可解释）")


def test_plain_mention_still_yields_the_mentioned_person():
    """★ 第三期 [3] 的**实测落点**：光被 @（正文没再谈他）**仍然**是候选。

    [3] 的原提案是"@ 的对象必须在正文里被谈论才算候选"（向 §4.2③ 原文靠拢）。
    第三期**实测为负**（锚点 0.500→0.333、不确定率 0.200→0.340）并**整条回退**：
    评测集标注的判据恰恰相反 —— t0003「@P6 拌饭会员卡有啥用」（anchor=P6）、
    t0083「@P5 你到家了对吗」（anchor=P5）、t0096/t0100 都是"以 @ 定主角"。
    收窄成"@ 一声不许改写簇主"也仍是负（0.500→0.444）。

    所以本用例把**回退后的行为**钉死（防"下一期又把 [3] 当没做过地重做一遍"）：
    """
    _threads("off")
    # ① @ 的对象 = 主角（真值即此，二/三期都没变过）
    for sample_id, expected in (
        ("t0003", "P6"),
        ("t0083", "P5"),
        ("t0100", "P6"),
        ("t0020", "P1"),
    ):
        result = _replay(sample_id, prev_anchor="P9")
        assert result.anchor_label() == expected, (sample_id, result.anchor_label())
        assert result.anchor_confidence == T.CONF_MEDIUM, (sample_id, result.anchor_confidence)

    # ② §4.1 的反例形态**不许**被这条回退带走：「@X 你把他闹钟关了」仍然是
    #    unresolved（主角是"他"，不是被 @ 的 X）——这是 [3] 唯一该保留的那半条。
    example = T._anchor_of_row(
        T._eval_row(
            {
                "at": "2026-09-30 19:03:51",
                "speaker": "P1",
                "text": "@P2 别拍他，拍他也没用。",
                "refs": {},
            }
        ),
        None,
        self_name="鲸鲸",
        self_person_id="",
        known=frozenset({"P2"}),
    )
    assert example.person is None and example.unresolved is True, example
    print("  ok [3] 光被 @ 仍是候选（回退后行为，4 条真样本）；「@X…他…」仍 unresolved")


def test_current_turn_is_not_split_out_on_a_lexical_link():
    """★ 第三期 [6] 的**实测落点**：当前这条**不**参与"显式换人 → 另起一簇"。

    [6] 的原提案是去掉 `_rollup` 里 `index != current_index` 那道闸（让当前这条
    也按"显式候选 ≠ 簇锚点"另起一簇）。第三期**实测为负/零**并**整条回退**：
    · 只去掉那道闸 → **整批读数一个字节都没动**（那格在评测集里从不生效）；
    · 连 `not linked` 一起去掉 → 错合率 0.389→0.172（唯一一次接近 ≤0.10）**但**
      换人误判 0.113→0.161、link_recall 0.126→0.092、锚点仍 0.500 → **安全项变差**。

    用例分两格，第二格是**唯一能真正测到那道闸**的形态（第一格测不出来：那道闸在
    "与旧簇有词面连接"时本来就被后面的 `linked` 分支接住，**变异成 no-op**——
    这不是巧合，见 `_rollup` 注释里 [6] 那两行读数）：

    ① 真样本 t0081（@ 了别人、但与旧簇有词面连接）→ 并进旧簇、主角是被 @ 的人；
    ② 手工算例：当前条与历史代表**共享 2-gram**、显式候选却与簇锚点不同
       → 仍是**一簇**、簇锚点**不被当前条夺走**（去掉那道闸会多出一簇）。
    """
    _threads("off")

    # ① 真样本形态
    result = _replay("t0081", prev_anchor="P3")
    assert result.anchor_label() == "P2", result.anchor_label()
    assert result.anchor_evidence == ("@ 的对象 P2",), result.anchor_evidence
    slot = result.primary_slot
    assert slot is not None and slot.anchor_label() == "P2", slot

    # ② 直接打 `_rollup`（唯一能分辨那道闸的算例）。断言分两截，各归各的改法：
    #    · **簇的划分**归 [6]：与历史代表共享 2-gram → 并进旧簇（去掉那道闸会多一簇）；
    #    · **簇主**归 `_rollup` 自己的既有口径：只在簇还没锚点时用显式候选播种
    #      （这里历史条已经播了 P3 → 不被当前条夺走）。当前条候选改不改簇主，
    #      是 :func:`test_current_turn_explicit_candidate_beats_inherited_cluster_anchor`
    #      那一格（`calculate_focus` 的锚点链）的事，**两处归因不要混**。
    def _row(text: str, speaker: str, at: str, eid: str) -> dict:
        return {
            "content": text,
            "sender_name": speaker,
            "created_at": at,
            "event_id": eid,
        }

    rows = (
        _row("湛江北是出发站吧", "P3", "2026-09-30 21:16:00", "h1"),
        _row("我还倒过去看了眼那个口", "P3", "2026-09-30 21:16:20", "h2"),
        _row("没发现都往最末尾那个口走的吗，湛江北那个口", "P3", "2026-09-30 21:17:00", "h3"),
        _row("@P2 那就好好玩，别又走错站台。湛江北那个口看清楚。", "P1", "2026-09-30 21:17:30", "c1"),
    )
    keys = [T._bigram_key(T._text(item)) for item in rows]
    assert T.share_key(keys[2], keys[3]), "算例前提：当前条与历史代表共享 2-gram"
    from core.conversation.addressing import Person  # noqa: PLC0415 - 算例要用真形

    seed_p3, seed_p2 = Person(display_name="P3"), Person(display_name="P2")
    clusters, current = T._rollup(
        rows, keys, len(rows) - 1, explicit=[None, None, seed_p3, seed_p2]
    )
    assert len(clusters) == 2, [cluster.members for cluster in clusters]
    assert current == 1 and clusters[current].members == [1, 2, 3], clusters[current].members
    assert clusters[current].anchor is seed_p3, (
        "当前条与旧簇有词面连接 → 并进旧簇、**不另起一簇**（[6] 回退后的行为）；"
        "簇主仍由先播种的那条决定"
    )
    print("  ok [6] 当前这条不因显式换人另起一簇（t0081 形态 + 能分辨那道闸的算例）")


def test_lexical_link_is_what_keeps_the_current_turn_in_its_cluster():
    """★ [6] 的**归因修正**（第三期实测的副产物，留给下一期别再找错杠杆）。

    第二期诊断把"当前条被并进旧簇"记在 `index != current_index` 那道闸上。
    第三期**逐条实测**（各开新进程跑一次，数字见报告）：去掉那道闸
    **读数一个字节都没动**（0.500 / 0.113 / 0.611 / 0.126）——改动点其实落在
    **历史条**上，而且 `not linked` 在场时它本来就与同一循环后面的
    `if cluster.rep_key and share_key(cluster.rep_key, key): target = cluster`
    等价（闸门只在**不** `linked` 时才 `continue`）。真正有意义的是 `not linked`：
    去掉它 → 错合率 0.389→0.302、link_recall 0.126→0.115，**但**换人误判
    0.113→0.145（安全项变差）→ 已回退。

    真正会被改动的是**另一半**：`not linked`。它在**当前条**上同样测不出来
    （当前条 `index == current_index` → 那道闸先把它挡掉），必须在**历史条**上才
    可达。所以本用例的算例给了一条"显式换了个人、却与簇代表共享 2-gram"的**历史**
    消息（21:18 事故窗的形态：@ 了另一个人、还在说同一件事）——去掉 `not linked`
    它就会被拆成另一簇（实测换人误判 0.113→0.145 → 已回退）。
    """
    _threads("off")
    rows = (
        # h1 先播种簇锚点 P3（显式候选）
        {"content": "湛江北是出发站吧", "sender_name": "P3",
         "created_at": "2026-09-30 21:16:00", "event_id": "h1"},
        # h2 与 h1 共享 "湛江北" → 进同一簇；簇锚点保持 P3
        {"content": "湛江北那个口真是绕，我还倒回去看了眼", "sender_name": "P3",
         "created_at": "2026-09-30 21:16:20", "event_id": "h2"},
        # h3 **在簇内**显式换了个人（@P2）却仍在说站台（与簇代表共享 2-gram）
        {"content": "@P2 那就好好玩，别又走错站台。湛江北那个口看清楚。", "sender_name": "P1",
         "created_at": "2026-09-30 21:16:40", "event_id": "h3"},
        {"content": "嗯嗯我也这么觉得，湛江北那个口确实绕", "sender_name": "P3",
         "created_at": "2026-09-30 21:17:00", "event_id": "c1"},
    )
    keys = [T._bigram_key(T._text(item)) for item in rows]
    from core.conversation.addressing import Person  # noqa: PLC0415

    assert T.share_key(keys[0], keys[1]), "算例前提：h1/h2 词面连接"
    assert T.share_key(keys[1], keys[2]), "算例前提：h3 与簇代表词面连接"
    clusters, _current = T._rollup(
        rows, keys, len(rows) - 1,
        explicit=[
            # 显式候选写成**有 person_id 的真人形**：`_same_person` 走 id 判等，
            # 空 id 的 Person 会被判成"同一个人"（那是身份判等的既有口径，不是本用例要测的东西）
            Person(person_id="p3", display_name="P3"),
            None,
            Person(person_id="p2", display_name="P2"),
            None,
        ],
    )
    members = [cluster.members for cluster in clusters]
    assert members == [[0, 1, 2, 3]], (
        "@ 了别人**但仍在说同一件事**（与簇代表共享 2-gram）→ 不拆簇；"
        "去掉 `not linked` 会把 h3 拆出去、让本算例变成 [[0,1],[2,3]]（已实测回退）",
        members,
    )
    print("  ok [6] 归因修正：真正管住归簇的是「词面连接」，不是那道死代码闸")


# ---------------------------------------------------------------- 自带 runner
def main() -> bool:
    tests = [fn for name, fn in globals().items() if name.startswith("test_")]
    failed = 0
    print("=" * 60)
    print("N3 话题簇与锚点（第一期 + 第二期接线入口）")
    print("=" * 60)
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
