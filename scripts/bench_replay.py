"""回放量表：用真实历史回合，量「每种信息通道值多少」。

背景与设计：docs/decisions/回放量表.md
- 每个条目 = 一个真实回合（某会话里某条真实消息）+ 一句非泄漏的追问 +
  机械判定的期望 token。
- 三个条件（都不含"看过答案再写"的泄漏）：
    prod  生产原样：真实 chat_window 文本进 _group_recent_context
    raw   +按**问题关键词**检索到的原文片段（检索不看答案）
    note  +由**与问题无关**的写手步骤从同一段原文写出的 0-3 条手记
- 打分：期望 token 命中率 / 是否承认不知道 / 回答里出现"整库都没有"的数字或文件名
  （= 编造候选）/ 长度。

子命令：
  build  造条目（写 bench/items.json）
  run    跑条目（写 .test_tmp/bench_<stamp>/）
  report 只看已有 run 的汇总

必须在隔离库下跑： powershell -NoProfile -ExecutionPolicy Bypass -File
  scripts\run_isolated.ps1 scripts\bench_replay.py build --n 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.infra import det  # noqa: E402 - 补完 sys.path 才能 import 项目模块

os.environ.setdefault("YINYUE_FEATURE_MEMORY_VECTOR_RECALL", "0")

ADMISSION_RE = re.compile(
    r"不知道|不记得|记不得|没听说|没记录|不清楚|没存|没有|查不到|翻不出|我哪知道|我这儿没|不在我手上"
)
DECLINED_RE = re.compile(r"不接话|没点名|不关我事|不关我的事|略过|没必要回|没点我|默认不接")

TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("term", re.compile(r"20\d{2}\s*[-–—]\s*20\d{2}(?:\s*[-_]\s*\d)?")),
    ("arxiv", re.compile(r"(?<!\d)\d{4}\.\d{4,5}(?!\d)")),
    ("file", re.compile(r"[\w\u4e00-\u9fff\-_.]{2,60}\.(?:pdf|xlsx|docx|zip|pptx|md|py)")),
    ("num", re.compile(r"(?<!\d)\d{6,}(?!\d)")),
    ("date", re.compile(r"\d{1,2}月\d{1,2}日")),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    (
        "place",
        re.compile(
            r"南宁|广州|深圳|北京|上海|海口|桂林|湛江|玉林|东莞|佛山|中山|惠州|珠海|长沙|武汉|成都|重庆|西安|南京|杭州|苏州|天津"
        ),
    ),
    (
        "topic",
        re.compile(
            r"(?:大学物理|物理实验|高数|线代|大物|毛概|思修|英语四级|英语六级|C语言|数据结构|操作系统|计算机网络|概率论|离散数学|数电|模电|选修课|通识课|奖学金|体测)"
            r"(?:实验)?(?:报告|作业|论文|考试|小测|课)?|(?:实验报告|选课通知|体测成绩)"
        ),
    ),
]

PLACE_VERB_RE = re.compile(r"看|去|回|到|来|帮|查|要|买|订|约|找|寄|发")

CONDITION_NAMES = ("prod", "raw", "oracle", "note", "note_oracle", "noise")

# 问题模板：按**原句关键词**门控，保证问法与事实语义一致，且机械上不含答案
TEMPLATES = [
    (re.compile(r"课表|学期|学年"), "term", "我课表是哪一学年的来着？"),
    (re.compile(r"课表|表格|xlsx"), "file", "我上次发的课表文件叫什么名字来着？"),
    (re.compile(r"论文|arxiv"), "arxiv", "你之前提的那篇论文编号是多少来着？"),
    (re.compile(r"编号|单号|卡号|订单|快递|工号|学号"), "num", "上次那个编号是多少来着？"),
    (re.compile(r"说好|定在|约定|改到"), "date", "我们最后定的是哪一天来着？"),
]

PLACEHOLDER_RE = re.compile(r"\[表情包|\[图片|\[SILENT|\[引用|\[语音|\[视频|\[文件|拍了拍|\[链接|\[位置")


_CN_DIGITS = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}


def _cn_int(text: str) -> str:
    if "十" not in text:
        return "".join(_CN_DIGITS.get(ch, "") for ch in text)
    head, _, tail = text.partition("十")
    tens = _CN_DIGITS.get(head, "1") if head else "1"
    ones = _CN_DIGITS.get(tail, "0") if tail else "0"
    return f"{tens}{ones}"


def _norm(text: str) -> str:
    s = re.sub(r"[\s_\-–—*#`]", "", str(text or ""))
    s = s.replace("号", "日")
    s = re.sub(r"[零一二三四五六七八九十]{1,3}月", lambda m: f"{int(_cn_int(m.group(0)[:-1]))}月", s)
    s = re.sub(r"[零一二三四五六七八九十]{1,3}日", lambda m: f"{int(_cn_int(m.group(0)[:-1]))}日", s)
    return s


def _read_rows(chat_id: str, limit: int = 6000) -> list[dict]:
    from sqlalchemy import text as sa_text

    from core.bootstrap import get_meta_db_path
    from db.write_guard import read_only_engine

    engine = read_only_engine(get_meta_db_path())
    with engine.connect() as conn:
        rows = conn.execute(
            sa_text(
                "select event_id, occurred_at, event_type, actor_person_id, content, chat_type "
                "from terminal_events where chat_id = :c "
                "and event_type in ('message','question','reply','request') "
                "and content is not null and content != '' "
                "order by occurred_at, sequence limit :n"
            ),
            {"c": chat_id, "n": limit},
        ).fetchall()
        names: dict[str, str] = {}
        try:
            for pid, ident, disp in conn.execute(
                sa_text(
                    "select i.person_id, i.identity_value, e.display_name "
                    "from person_identities i "
                    "left join person_entities e on e.id = i.person_id "
                    "where i.identity_type = 'wxid'"
                )
            ).fetchall():
                if pid:
                    names[str(pid)] = str(disp or ident or "")
        except Exception:
            names = {}
    out = []
    for event_id, occurred_at, event_type, actor, content, chat_type in rows:
        out.append(
            {
                "event_id": str(event_id),
                "at": str(occurred_at),
                "type": str(event_type),
                "actor": str(actor or ""),
                "name": names.get(str(actor or ""), "") or str(actor or "?"),
                "content": str(content or ""),
                "chat_type": str(chat_type or ""),
            }
        )
    return out


def _window_text(chat_id: str) -> str:
    from core.context.chat_window import build_window_parts

    try:
        return str(build_window_parts(chat_id).text_full or "")
    except Exception:
        return ""


def _pick_token(content: str) -> tuple[str, str] | None:
    for kind, pat in TOKEN_PATTERNS:
        m = pat.search(content)
        if not m:
            continue
        if kind == "place" and not PLACE_VERB_RE.search(content):
            continue
        if kind == "num":
            lo = content[max(0, m.start() - 6) : m.start()]
            hi = content[m.end() : m.end() + 6]
            if not re.search(r"[\u4e00-\u9fff]", lo + hi):
                continue
        return kind, m.group(0).strip()
    return None


def _question_for(kind: str, content: str) -> str:
    for pat, want, q in TEMPLATES:
        if want == kind and pat.search(content):
            return q
    return ""


QW_SYSTEM = (
    "你是这个对话里发消息的主人。下面给你一条**你自己发过**的消息。"
    "请照你自己的口吻写一句自然的追问，让对方回忆这条消息里的一个关键信息"
    "（编号/日期/文件名/数字/约定/地址）。硬性要求："
    "(1) 答案本身绝不能出现在问题里——这些字符串一个都不许出现：{expects}；"
    "(2) 不要出现「聊天记录」「上面那句」「这句话」这类元话术；"
    "(3) 不超过 30 个字；(4) 只输出这一个问句，不要引号不要解释。"
)


def _question_via_llm(agent, content: str, expects: list[str]) -> str:
    import asyncio as _asyncio

    system = QW_SYSTEM.format(expects=" / ".join(expects))
    try:
        q = _asyncio.run(
            agent.llm.chat(
                system=system,
                user=content[:300],
                agent_name="conversational",
                temperature=0.3,
                max_tokens=80,
            )
        )
    except Exception:  # noqa: BLE001
        return ""
    q = str(q or "").strip().strip('"').strip("「」")
    q = q.splitlines()[0].strip() if q else ""
    if not q or len(q) > 40:
        return ""
    if any(_norm(t) in _norm(q) for t in expects):
        return ""
    return q


QW_SYSTEM = (
    "你是这个对话里发消息的主人。下面给你一条**你自己发过**的消息。"
    "请照你自己的口吻写一句自然的追问，让对方回忆这条消息里的一个关键信息"
    "（编号/日期/文件名/数字/约定/地址）。硬性要求："
    "(1) 答案本身绝不能出现在问题里——这些字符串一个都不许出现：{expects}；"
    "(2) 不要出现「聊天记录」「上面那句」「这句话」这类元话术；"
    "(3) 不超过 30 个字；(4) 只输出这一个问句，不要引号不要解释。"
)


def _question_via_llm(agent, content: str, expects: list[str]) -> str:
    import asyncio as _asyncio

    system = QW_SYSTEM.format(expects=" / ".join(expects))
    try:
        q = _asyncio.run(
            agent.llm.chat(
                system=system,
                user=content[:300],
                agent_name="conversational",
                temperature=0.3,
                max_tokens=80,
            )
        )
    except Exception:  # noqa: BLE001
        return ""
    q = str(q or "").strip().strip('"').strip("「」")
    q = q.splitlines()[0].strip() if q else ""
    if not q or len(q) > 40:
        return ""
    if any(_norm(t) in _norm(q) for t in expects):
        return ""
    return q


def _expect_tokens(kind: str, token: str, content: str) -> list[str]:
    if kind == "term":
        base = re.sub(r"[\s_\-–—]", "", token)
        short = re.sub(r"(\d{4})[-–—]?(\d{4}).*", r"\1-\2", token).strip()
        return [token.strip(), short, base]
    if kind == "file":
        term = TOKEN_PATTERNS[0][1].search(token)
        out = [token.strip()]
        if term:
            out.append(term.group(0).strip())
        return out
    if kind == "date":
        m = re.match(r"(\d{1,2})月(\d{1,2})日", token)
        if m:
            return [token.strip(), f"{int(m.group(1))}月{int(m.group(2)):02d}日"]
        return [token.strip()]
    return [token.strip()]


def _build_items(chat_id: str, n: int, runs_placeholder: int, writer: str = "llm") -> list[dict]:
    rows = _read_rows(chat_id)
    writer_calls = 0
    writer_budget = 24
    agent = None
    if writer == "llm":
        try:
            from core.bootstrap import build_agent

            agent = build_agent()
        except Exception:  # noqa: BLE001
            agent = None
    window = _window_text(chat_id)
    window_norm = _norm(window)
    items: list[dict] = []
    seen_tokens: set[str] = set()
    recent_cut = max(0, len(rows) - 40)
    order = [(i, r) for i, r in enumerate(rows) if i >= recent_cut] + [
        (i, r) for i, r in enumerate(rows) if i < recent_cut
    ]
    quota_recent = max(1, n // 2)
    n_recent = 0
    for idx, row in order:
        if n_recent >= quota_recent and idx >= recent_cut:
            continue
        if idx >= recent_cut:
            n_recent += 1
        if len(items) >= n:
            break
        content = row["content"]
        if len(content) < 4 or len(content) > 600:
            continue
        picked = _pick_token(content)
        if not picked:
            continue
        kind, token = picked
        if _norm(token) in seen_tokens:
            continue
        expects = _expect_tokens(kind, token, content)
        # 唯一性：期望 token 在会话里只能出现在 1-3 行，否则答案不唯一
        hits = [r for r in rows if any(_norm(t) in _norm(r["content"]) for t in expects)]
        if not 1 <= len(hits) <= 6:
            continue
        if PLACEHOLDER_RE.search(content):
            continue
        if row["type"] == "reply" or row["name"] in ("self", "?"):
            continue
        if len(re.findall(r"[\u4e00-\u9fff]", content)) < 4:
            continue
        question = ""
        if agent is not None and writer_calls < writer_budget:
            writer_calls += 1
            question = _question_via_llm(agent, content, expects)
        if not question:
            question = ""
        if agent is not None:
            question = _question_via_llm(agent, content, expects)
        if not question:
            question = _question_for(kind, content)
        if not question:
            continue
        if any(_norm(t) in _norm(question) for t in expects):
            continue
        pos = content.find(token)
        head = content[max(0, pos - 8) : pos]
        if "@" in head and not re.search(r"[\s，。、！？~—]$", head):
            continue
        seen_tokens.add(_norm(token))
        in_window = any(_norm(t) in window_norm for t in expects)
        items.append(
            {
                "item_id": f"{chat_id}:{row['event_id']}",
                "chat_id": chat_id,
                "kind": kind,
                "token": token,
                "expect_tokens": expects,
                "question": question,
                "category": "in_window" if in_window else "out_of_window",
                "source": {
                    "at": row["at"],
                    "name": row["name"],
                    "type": row["type"],
                    "content": content,
                    "row_index": idx,
                },
                "rows_total": len(rows),
            "token_rows": len(hits),
                "window_chars": len(window),
            }
        )
    return items


def _window_items(chat_id: str, n: int, agent, budget: int = 6) -> list[dict]:
    """从**窗口文本里真的出现过**的 token 造条目（= 生产 prompt 自带的对照组）。"""
    rows = _read_rows(chat_id)
    window = _window_text(chat_id)
    items: list[dict] = []
    seen: set[str] = set()
    calls = 0
    for kind, pat in TOKEN_PATTERNS:
        if len(items) >= n:
            break
        for m in pat.finditer(window):
            if len(items) >= n or calls >= budget:
                break
            token = m.group(0).strip()
            if not token or _norm(token) in seen:
                continue
            src = next((r for r in rows if token in r["content"]), None)
            src_i = next((i for i, r in enumerate(rows) if token in r["content"]), 0)
            if src is None or PLACEHOLDER_RE.search(src["content"]):
                continue
            if src["type"] == "reply" or src["name"] in ("self", "?"):
                continue
            expects = _expect_tokens(kind, token, src["content"])
            q = ""
            if agent is not None and calls < budget:
                calls += 1
                q = _question_via_llm(agent, src["content"], expects)
            if not q:
                q = _question_for(kind, src["content"])
            if not q:
                continue
            seen.add(_norm(token))
            items.append(
                {
                    "item_id": f"{chat_id}:win:{src['event_id']}",
                    "chat_id": chat_id,
                    "kind": kind,
                    "token": token,
                    "expect_tokens": expects,
                    "question": q,
                    "category": "in_window",
                    "source": {
                        "at": src["at"],
                        "name": src["name"],
                        "type": src["type"],
                        "content": src["content"],
                        "row_index": src_i,
                    },
                    "rows_total": len(rows),
                    "token_rows": 0,
                    "window_chars": len(window),
                }
            )
    return items


def _keywords(question: str) -> list[str]:
    words = re.findall(r"[\u4e00-\u9fff]{2,4}", question)
    stop = {"上次", "那个", "什么", "哪个", "来着", "我们", "之前", "多少"}
    return [w for w in words if w not in stop] or ["那个"]


def _retrieve(rows: list[dict], question: str, span: int = 6) -> tuple[str, int]:
    kws = _keywords(question)
    best_i, best_score = -1, 0
    for i, r in enumerate(rows):
        c = _norm(r["content"])
        score = sum(c.count(k) for k in kws)
        if score > best_score:
            best_i, best_score = i, score
    if best_i < 0:
        return "", -1
    lo = max(0, best_i - span)
    hi = min(len(rows), best_i + span + 1)
    block = "[历史原文·仅作上下文]\n" + "\n".join(
        f"{r['at'][:16]} {r['name']}: {r['content']}" for r in rows[lo:hi]
    )
    return block, best_i


def _make_agent(chat_id: str, question: str, *, sender: str, owner: bool):
    from core.bootstrap import build_agent
    from core.config.settings import config
    from core.speech.turn import snapshot_from_context

    rows = _read_rows(chat_id)
    inbound = [r for r in rows if r["type"] in ("message", "question")]
    if not inbound:
        return None, None, rows
    last = inbound[-1]
    is_group = chat_id.endswith("@chatroom")
    agent = build_agent()
    sender_id = sender or last["actor"]
    extra = {
        "platform": "wechat",
        "account_id": config.wechat.yinyue_wxid,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "sender_name": last["name"],
        "user_message": question,
        "is_group": is_group,
        "group_id": chat_id if is_group else "",
    }
    context = SimpleNamespace(
        extra=extra,
        account_context=None,
        get=lambda key, default=None: extra.get(key, default),
    )
    snapshot = snapshot_from_context(
        context,
        chat_type="group" if is_group else "private",
        is_owner=owner,
        is_mentioned=True,
        group_recent_context=_window_text(chat_id),
    )
    agent._last_context = context
    agent._inbound_content = question
    agent._group_recent_context = ""
    agent._speech_snapshot = snapshot
    return agent, snapshot, rows


def _prompt_for(agent, snapshot, is_group: bool) -> str:
    return agent._build_system_prompt_from_snapshot(snapshot, is_group)


WRITER_SYSTEM = (
    "你是这个对话的长期记忆写手。给你一段聊天原文，挑出 0-3 条**值得长期记住的事实**"
    "（谁说了什么可核验的事、时间、编号、约定、承诺），每条写成一行短句，不超过 40 字。"
    "不要写感受和评论，不要写引语，不要解释。只输出以「- 」开头的条目；"
    "如果没有值得记的，只输出 NONE。"
)


async def _chat(
    agent, system: str, user: str, *, temperature: float | None = 0.4, max_tokens: int = 1000
) -> str:
    kwargs: dict = {
        "system": system,
        "user": user,
        "agent_name": "conversational",
        "max_tokens": max_tokens,
    }
    # 思考档不吃 temperature（传了会削思考质量，adapters/openai_compat.py:284 会自行剥掉）；
    # max_tokens 太小会被思考吃光 → 正文空（实测 300 → ''，1500 → 正常）。
    if temperature is not None:
        kwargs["temperature"] = temperature
    return str(await agent.llm.chat(**kwargs) or "")


def _grade(answer: str, item: dict, all_rows: list[dict]) -> dict:
    a_norm = _norm(answer)
    expect = item["expect_tokens"]
    hit = sum(1 for t in expect if _norm(t) in a_norm)
    corpus = _norm("".join(r["content"] for r in all_rows))
    nums = set(re.findall(r"(?<!\d)\d{4,}(?!\d)", answer))
    files = set(re.findall(r"[\w\u4e00-\u9fff\-_.]{2,60}\.(?:pdf|xlsx|docx|zip|pptx|md|py)", answer))
    missing = sorted(x for x in (nums | files) if _norm(x) not in corpus)
    return {
        "answer": answer,
        "token_hit": hit,
        "token_recall": hit / len(expect) if expect else 0.0,
        "exact": bool(expect) and hit == len(expect),
        "admission": bool(ADMISSION_RE.search(answer)),
        "declined": bool(DECLINED_RE.search(answer)),
        "halluc_tokens": missing,
        "chars": len(answer),
    }


def cmd_build(args) -> int:
    chats = [c for c in str(args.chats).split(",") if c]
    all_items: list[dict] = []
    agent = None
    if args.writer == "llm":
        try:
            from core.bootstrap import build_agent

            agent = build_agent()
        except Exception:  # noqa: BLE001
            agent = None
    for chat in chats:
        items = _build_items(chat, args.n, args.runs, args.writer)
        items += _window_items(chat, max(2, args.n // 4), agent, budget=4)
        print(f"{chat}: {len(items)} 条（in_window {sum(1 for i in items if i['category']=='in_window')}）")
        all_items.extend(items)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(all_items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"items -> {out}（共 {len(all_items)}）")
    return 0


def cmd_run(args) -> int:
    items = json.loads(Path(args.items).read_text(encoding="utf-8"))
    if args.limit:
        items = items[: args.limit]
    active = (
        [c for c in args.only_conditions.split(",") if c]
        if args.only_conditions
        else list(CONDITION_NAMES)
    )
    stamp = args.stamp or __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.outdir) / f"bench_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    row_cache: dict[str, list[dict]] = {}
    agent_cache: dict[str, object] = {}
    results: list[dict] = []
    for item in items:
        chat = item["chat_id"]
        if chat not in row_cache:
            row_cache[chat] = _read_rows(chat)
        rows = row_cache[chat]
        if chat not in agent_cache:
            agent, snapshot, _ = _make_agent(
                chat, item["question"], sender=args.sender, owner=bool(args.owner)
            )
            agent_cache[chat] = (agent, snapshot)
        agent, snapshot = agent_cache[chat]
        is_group = chat.endswith("@chatroom")
        window = _window_text(chat)
        agent._inbound_content = item["question"]
        agent._group_recent_context = window
        base = _prompt_for(agent, snapshot, is_group)

        raw_block, best_i = _retrieve(rows, item["question"], span=args.span)
        noise_block = ""
        if rows:
            rnd = det.rng(args.noise_seed, item["item_id"])
            for _ in range(12):
                j = rnd.randrange(len(rows))
                lo = max(0, j - args.span)
                hi = min(len(rows), j + args.span + 1)
                span_rows = rows[lo:hi]
                if any(
                    _norm(t) in _norm("".join(r["content"] for r in span_rows))
                    for t in item["expect_tokens"]
                ):
                    continue
                noise_block = "[历史原文·仅作上下文]\n" + "\n".join(
                    f"{r['at'][:16]} {r['name']}: {r['content']}" for r in span_rows
                )
                break
        note_block = ""
        if raw_block:
            span_text = raw_block.split("\n", 1)[1]
            note_text = asyncio.run(
                _chat(agent, WRITER_SYSTEM, span_text, temperature=None, max_tokens=1500)
            )
            note_block = f"[长期记忆·手记]（写手刚从一段原文里抄的）\n{note_text.strip()}"
        from agents.conversational import _append_prompt_block

        oracle_block = ""
        src_idx = int(item["source"].get("row_index") or 0)
        if 0 <= src_idx < len(rows):
            lo = max(0, src_idx - args.span)
            hi = min(len(rows), src_idx + args.span + 1)
            oracle_block = "[历史原文·仅作上下文]\n" + "\n".join(
                f"{r['at'][:16]} {r['name']}: {r['content']}" for r in rows[lo:hi]
            )
        note_oracle_block = ""
        if oracle_block and "note_oracle" in active:
            span_text2 = oracle_block.split("\n", 1)[1]
            note2 = asyncio.run(
                _chat(agent, WRITER_SYSTEM, span_text2, temperature=None, max_tokens=1500)
            )
            note_oracle_block = (
                f"[长期记忆·手记]（写手从那段原文里抄的）\n{note2.strip()}"
            )
        variants = {
            "prod": base,
            "raw": _append_prompt_block(base, raw_block) if raw_block else base,
            "oracle": _append_prompt_block(base, oracle_block) if oracle_block else base,
            "note": _append_prompt_block(base, note_block) if note_block else base,
            "note_oracle": _append_prompt_block(base, note_oracle_block) if note_oracle_block else base,
            "noise": _append_prompt_block(base, noise_block) if noise_block else base,
        }
        variants = {k: v for k, v in variants.items() if k in active}
        if args.dump_prompts:
            pd = out_dir / "prompts"
            pd.mkdir(exist_ok=True)
            for name, text in variants.items():
                (pd / f"{item['item_id'].replace(':','_')}.{name}.txt").write_text(text, encoding="utf-8")

        item_out = {
            "item_id": item["item_id"],
            "chat_id": chat,
            "kind": item["kind"],
            "category": item["category"],
            "question": item["question"],
            "expect_tokens": item["expect_tokens"],
            "source": item["source"],
            "fact_in_prod_prompt": all(_norm(t) in _norm(base) for t in item["expect_tokens"]),
            "retrieval_row_index": best_i,
            "oracle_row_index": int(item["source"].get("row_index") or 0),
            "note_text": note_block,
            "note_oracle_text": note_oracle_block,
            "conditions": {},
        }
        for name, prompt in variants.items():
            runs = []
            for _ in range(max(1, args.runs)):
                try:
                    ans = asyncio.run(_chat(agent, prompt, item["question"]))
                except Exception as exc:  # noqa: BLE001
                    ans = f"[ERROR] {type(exc).__name__}: {exc}"
                runs.append(_grade(ans, item, rows))
            item_out["conditions"][name] = runs
            print(
                f"[{item['category'][:3]}] {item['item_id'][-24:]} {name:4s} "
                + " | ".join(r["answer"][:55].replace("\n", " ") for r in runs)
            )
        results.append(item_out)

    agg: dict[str, dict] = {}
    for name in active:
        rs = [r for it in results for r in it["conditions"][name]]
        agg[name] = {
            "exact_rate": sum(1 for r in rs if r["exact"]) / len(rs) if rs else 0.0,
            "recall_mean": sum(r["token_recall"] for r in rs) / len(rs) if rs else 0.0,
            "admission_rate": sum(1 for r in rs if r["admission"]) / len(rs) if rs else 0.0,
            "declined_rate": sum(1 for r in rs if r.get("declined")) / len(rs) if rs else 0.0,
            "halluc_mean": sum(len(r["halluc_tokens"]) for r in rs) / len(rs) if rs else 0.0,
            "chars_mean": sum(r["chars"] for r in rs) / len(rs) if rs else 0.0,
            "n": len(rs),
        }
    by_cat: dict[str, dict] = {}
    for cat in ("in_window", "out_of_window"):
        sub = [it for it in results if it["category"] == cat]
        if not sub:
            continue
        by_cat[cat] = {
            name: {
                "exact_rate": (
                    sum(1 for it in sub for r in it["conditions"][name] if r["exact"])
                    / max(1, sum(len(it["conditions"][name]) for it in sub))
                ),
                "recall_mean": (
                    sum(r["token_recall"] for it in sub for r in it["conditions"][name])
                    / max(1, sum(len(it["conditions"][name]) for it in sub))
                ),
                "n_items": len(sub),
            }
            for name in active
        }
    report = {
        "stamp": stamp,
        "runs_per_condition": args.runs,
        "items": len(results),
        "aggregate": agg,
        "by_category": by_cat,
        "results": results,
    }
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 汇总（全体）===")
    for name, s in agg.items():
        print(
            f"{name:5s} 全对 {s['exact_rate']:.0%}  召回 {s['recall_mean']:.2f}  "
            f"承认不知道 {s['admission_rate']:.0%}  编造候选 {s['halluc_mean']:.2f}  n={s['n']}"
        )
    print("=== 分类 ===")
    for cat, s in by_cat.items():
        print(f"{cat}: " + "  ".join(f"{k} 全对{v['exact_rate']:.0%}/召回{v['recall_mean']:.2f}" for k, v in s.items()))
    print(f"report -> {out_dir / 'report.json'}")
    return 0


def cmd_report(args) -> int:
    path = Path(args.report)
    rep = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({"aggregate": rep["aggregate"], "by_category": rep["by_category"]}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("--chats", default="wxid_seed01,10000000003@chatroom,10000000001@chatroom")
    b.add_argument("--n", type=int, default=8, help="每个会话最多几条")
    b.add_argument("--runs", type=int, default=1)
    b.add_argument("--writer", default="llm", choices=["llm", "template"])
    b.add_argument("--out", default=str(ROOT / "bench" / "items.json"))
    b.set_defaults(func=cmd_build)

    r = sub.add_parser("run")
    r.add_argument("--items", default=str(ROOT / "bench" / "items.json"))
    r.add_argument("--runs", type=int, default=2)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--span", type=int, default=6)
    r.add_argument("--noise-seed", type=int, default=7)
    r.add_argument("--only-conditions", default="")
    r.add_argument("--sender", default="")
    r.add_argument("--owner", action="store_true")
    r.add_argument("--dump-prompts", action="store_true")
    r.add_argument("--outdir", default=str(ROOT / ".test_tmp"))
    r.add_argument("--stamp", default="")
    r.set_defaults(func=cmd_run)

    p = sub.add_parser("report")
    p.add_argument("report")
    p.set_defaults(func=cmd_report)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
