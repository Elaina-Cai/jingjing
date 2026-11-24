#!/usr/bin/env python
"""信息缺口看板：验证「她知道自己看到的是残缺的过去」这件事落地了没有、被用了没有。

背景（2026-10-11）
------------------
准入把候选池（``CANDIDATE_LIMIT`` 行）折成「A 留原文 / B 归一句话 / C 丢弃」，
被丢掉的行对模型**完全不可见**——它不知道自己看的是残缺的过去，于是会在信息不全
时照常自信作答。改法是一次**纯文本增量**：给 ``scene_text`` / ``fold`` 追加一句
「候选 N 行里，只有 M 行的原文给了你；其余 K 行没有给你」。

用法（只读，不写库、不改配置）
------------------------------
    python scripts/omitted_board.py --verify    # 离线：走真实 evaluate() 看装配产物（不用重启）
    python scripts/omitted_board.py --readout   # 生产：读事件流，看她有没有说出"我没看到那段"
    python scripts/omitted_board.py             # 两个都跑

怎么读结果（重启后才算数）
--------------------------
* 出现「我没看到 / 哪一段 / 再给我」  → 这个权她在用 → 可以进第二步（撤闸门）
* 一轮都没出现                        → 权给了没用 → 第二步别做，别在这上面继续加东西
* ``state_assembly``（而不是 ``_shadow``）事件开始出现 → STATE_ASSEMBLY=on 已生效
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 强信号：明确指向"我拿到的东西不全"（含指代上文/引用缺失内容）
STRONG = (
    # 收窄过一次（2026-10-11）：原来含「没给我」「再说一遍」，实测 571 条历史回复里
    # 那两条命中的全是误报（"省得我再说一遍"＝她说别人；"你还没给我报呢"＝别的意思）。
    # 现在只留明确指向**自己拿到的东西不全**的说法。
    "没看到", "没收到", "看不到", "只看到", "哪一段", "上面那段", "上面那句",
    "前文", "上文", "再发我", "没发我",
)
#: 弱信号：承认不确定/没听懂（容易误报，单独列，不算验收）
WEAK = (
    "不确定", "不清楚", "我猜", "可能是", "记不得", "不记得", "没印象",
)


def env_flags() -> dict[str, str]:
    """盘点两个开关的**实际取值**（.env 优先，其次 os.environ）。"""
    wanted = ("YINYUE_FEATURE_STATE_ASSEMBLY", "YINYUE_ADMISSION_MODE")
    found: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            body = line.strip()
            if not body or body.startswith("#") or "=" not in body:
                continue
            key, _, value = body.partition("=")
            if key.strip() in wanted:
                found[key.strip()] = value.strip()
    for key in wanted:
        found.setdefault(key, f"(未设 → 代码默认; env={os.environ.get(key, '')!r})")
    return found


# ---------------------------------------------------------------- 离线验证
class _FakeLLM:
    """只实现 evaluate() 用到的 ``chat``；不联网。"""

    def __init__(self, reply: str) -> None:
        self.reply, self.calls = reply, 0

    async def chat(self, **kwargs):  # noqa: ANN003
        # 与 tests/test_admission.py::FakeLLM 同形：judge 读的是**裸字符串**。
        self.calls += 1
        return self.reply


_FAKE_JUDGE = json.dumps(
    {"labels": {"1": "A", "2": "B", "3": "C"}, "scene": "群里在玩歪头狗梗，气氛轻松"},
    ensure_ascii=False,
)


def verify() -> bool:
    """走真实 evaluate() 路径，打印她实际会看到的那段文本。"""
    import core.context.admission as admission

    print("== 离线验证（不用重启）==")
    for key, value in env_flags().items():
        print(f"   {key} = {value}")
    print()

    rows = []
    base = datetime(2026, 10, 11, 14, 0)
    for i in range(6):
        rows.append(_wm(base + timedelta(minutes=3 * i), "P5", f"第{i + 1}句"))
    # 双向验：夹具必须覆盖"声明该出现"和"声明不该出现"两侧。
    # 门槛 OMITTED_NOTE_MAX_SHOWN 是照真实分布收窄过的（见 admission.py 常量注释）：
    # 候选 18 行、judge 判 A 的中位数是 2 → 常态**不该**念这句，否则每轮 45 字噪音。
    judge_empty_a = json.dumps({"labels": {"1": "B", "2": "C"}, "scene": "群里在聊天"},
                               ensure_ascii=False)
    judge_has_a = json.dumps({"labels": {"1": "A", "2": "B"}, "scene": "群里在聊天"},
                             ensure_ascii=False)

    # 准入的 enabled_for() 要求会话**在窗口白名单里**（与 chat_window 同源）；
    # 这几个环境变量必须钉死，否则 evaluate() 直接返回 None（= 准入没跑）。
    pins = {
        "YINYUE_ADMISSION_MODE": "shadow",
        "YINYUE_FEATURE_CHAT_WINDOW": "1",
        "YINYUE_CHAT_WINDOW_CHATS": "c@chatroom",
        "YINYUE_FEATURE_REPLY_INTENT": "shadow",
        "YINYUE_ADMISSION_CHATS": None,
    }
    old = {k: os.environ.get(k) for k in pins}
    for key, value in pins.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        result = asyncio.run(admission.evaluate(
            chat_id="c@chatroom", reason="mentioned_in_group",
            current="@鲸鲸 你看这个", sender_name="P5",
            now=base + timedelta(minutes=30), rows=rows, llm=_FakeLLM(judge_empty_a),
        ))
        result_a = asyncio.run(admission.evaluate(
            chat_id="c@chatroom", reason="mentioned_in_group",
            current="@鲸鲸 你看这个", sender_name="P5",
            now=base + timedelta(minutes=30), rows=rows, llm=_FakeLLM(judge_has_a),
        ))
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    if result is None:
        print("  !! evaluate() 返回 None（准入没跑）——先看 YINYUE_ADMISSION_MODE")
        return False

    print("  --- 轮次模式落地的 scene_text（＝她看到的系统块）---")
    print("\n".join("    " + line for line in str(result.scene_text or "").splitlines()) or "    (空)")
    print()
    print("  --- 非轮次模式落地的 text ---")
    print("\n".join("    " + line for line in str(result.text or "").splitlines()) or "    (空)")
    print()

    ok = True
    judge_used = str(getattr(result, "fallback", "")) == "judge"
    has_note = "原文没有给你" in str(result.scene_text or "")
    if not judge_used:
        print(f"  !! fallback={result.fallback!r}（不是 judge）→ 这次没走到那条路")
        ok = False
    elif not has_note:
        print("  !! 一行原文都没给时，scene_text 里应有那句声明，但没有")
        ok = False
    else:
        print("  ok 一行原文都没给 → scene_text 带了「原文没有给你」")

    a_text = str(getattr(result_a, "scene_text", "") or "")
    if "原文没有给你" in a_text:
        print(f"  !! 给了 A 行时不该念这句（门槛 {admission.OMITTED_NOTE_MAX_SHOWN}），"
              f"但出现了 → 会变成每轮噪音")
        ok = False
    else:
        print(f"  ok 给了 A 行 → 不念（门槛 OMITTED_NOTE_MAX_SHOWN="
              f"{admission.OMITTED_NOTE_MAX_SHOWN}）")

    total = admission.CANDIDATE_LIMIT
    empty_ok = admission.fold({}, "", ["x"] * 3) == ""
    print(f"  {'ok' if empty_ok else '!!'} judge 什么都没给时 fold() 仍返回空串（T2 兜底没被吃掉）"
          f"{'' if empty_ok else ' ← 这条破了会让兜底路径失效'}")
    ok = ok and empty_ok

    print(f"  ok 候选池上限 CANDIDATE_LIMIT={total} / A 堆上限 A_CAP={admission.A_CAP}")
    print()
    return ok


def _wm(when: datetime, speaker: str, content: str, is_self: bool = False):
    from core.context.chat_window import WindowMessage

    return WindowMessage(when, speaker, content, is_self=is_self)


# ---------------------------------------------------------------- 生产读数
def _db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("META_DB_PATH") or ROOT / "data" / "yinyue_meta.db")


def readout(days: int, db: str | None) -> bool:
    print("== 生产读数（只读）==")
    path = _db_path(db)
    if not path.exists():
        print(f"  !! 库不存在: {path}")
        return False
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = con.execute(
            "SELECT sequence, occurred_at, chat_type, content FROM terminal_events "
            "WHERE event_type='reply' AND content <> '' AND occurred_at >= ? "
            "ORDER BY sequence DESC",
            (since,),
        ).fetchall()
        base = con.execute(
            "SELECT COUNT(*) FROM terminal_events WHERE event_type='reply' AND content <> ''"
        ).fetchone()[0]
    finally:
        con.close()

    print(f"  {path}  |  近 {days} 天她的回复 {len(rows)} 条（历史累计 {base} 条）")
    print()

    strong_hits, weak_hits = [], []
    for seq, at, chat_type, content in rows:
        text = str(content or "")
        hit = next((w for w in STRONG if w in text), "")
        if hit:
            strong_hits.append((seq, at, chat_type, text, hit))
            continue
        weak = next((w for w in WEAK if w in text), "")
        if weak:
            weak_hits.append((seq, at, chat_type, text, weak))

    print(f"  【强信号】承认「我拿到的东西不全」：{len(strong_hits)} 条")
    for seq, at, chat_type, text, hit in strong_hits[:15]:
        print(f"    seq={seq} {at} {chat_type} 命中[{hit}] | {text[:80]}")
    if not strong_hits:
        print("    （无）→ 基线 0。出现 1 条就说明这个权她真的在用。")
    print()

    print(f"  【弱信号】承认不确定（会误报，只作参考）：{len(weak_hits)} 条")
    for seq, at, chat_type, text, hit in weak_hits[:5]:
        print(f"    seq={seq} {at} {chat_type} 命中[{hit}] | {text[:70]}")
    print()

    # 判"生效没有"的**权威**依据是启动时的 flag 全量转储（进程真正加载了什么），
    # 不是事件计数——事件只有走到那条路才会有，还没轮次时计数天然是 0。
    log = ROOT / "logs" / "yinyue.log"
    if not log.exists():
        print("  !! 没有 logs/yinyue.log")
        print()
        return True
    from collections import Counter

    counts: Counter = Counter()
    last_flag_dump = None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines()[-80000:]:
        body = line.strip()
        if not body.startswith("{"):
            continue
        try:
            rec = json.loads(body)
        except Exception:
            continue
        event = str(rec.get("event") or "")
        if event.startswith("state_assembly") or event in ("self_move_recorded", "state_store_failed"):
            counts[event] += 1
        if "flag_state_assembly" in rec:
            last_flag_dump = rec
    print("  logs/yinyue.log：")
    for name in ("state_assembly", "state_assembly_shadow", "state_assembly_failed",
                 "self_move_recorded", "state_store_failed"):
        print(f"    {name:<24} {counts.get(name, 0)}")
    if last_flag_dump is not None:
        live = str(last_flag_dump.get("flag_state_assembly"))
        when = str(last_flag_dump.get("timestamp", "?"))
        verdict = "已生效" if live == "on" else "还没生效（进程加载的是旧 .env）"
        print(f"    └ 最近一次启动转储 flag_state_assembly = {live!r} @ {when} → {verdict}")
        print(f"      同批：admission_mode={last_flag_dump.get('flag_admission_mode')!r}"
              f" addressee_guard={last_flag_dump.get('flag_addressee_guard')!r}"
              f" output_gate={last_flag_dump.get('flag_output_gate')!r}")
    print()
    return True


def replay(limit: int) -> bool:
    """用 ``logs/admission_trace.jsonl`` 里的**真实记录**重放整条链。

    为什么要有这个模式：真实群聊里 @ 她一次不便时，这是唯一能不经发消息就验证
    「scene_text → prompt 块」这一环的办法。数据不是我造的：``labels`` 与候选原文
    都是当时 judge 真实判出来的捕获记录。

    覆盖到的链路：真实候选原文 + 真实 labels → ``_scene_block()``（本次改动）→
    ``group_recent_context`` → ``build_persona_blocks()`` → system 块。
    **不覆盖**的一环：``turn_pipeline`` 里 ``group_recent_context = admitted.scene_text``
    那三行（已读码确认，但没有可执行的桩去驱动整个 turn）。
    """
    import core.context.admission as admission
    from core.terminal.persona_brain import build_persona_blocks
    from types import SimpleNamespace

    path = ROOT / "logs" / "admission_trace.jsonl"
    if not path.exists():
        print(f"  !! 没有 {path}")
        return False

    picked: list[tuple[int, int, dict]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        judgment = rec.get("judgment") or {}
        if judgment.get("fallback") != "judge":
            continue
        labels = judgment.get("labels") or {}
        total = sum(int(v) for v in labels.values())
        if total:
            picked.append((int(labels.get("A", 0)), total, rec))
    # A 越少 = 当时被裁得越狠 = 越能看出声明的价值
    picked.sort(key=lambda item: (item[0], -item[1]))
    print(f"== 真实记录重放（judge 成功记录 {len(picked)} 条，取裁得最狠的 {limit} 条）==")
    if not picked:
        print("  !! admission_trace.jsonl 里没有可用的 judge 记录")
        return False
    print()

    snap = SimpleNamespace(
        persona=SimpleNamespace(name="鲸鲸", identity={"self": "你是鲸鲸。"}, traits={}),
        turn=None,
        policy=None,
    )
    ok = True
    for a_count, total, rec in picked[:limit]:
        judgment = rec["judgment"]
        labels = judgment["labels"]
        scene = str(judgment.get("scene") or "")
        cands = (rec.get("evidence") or {}).get("candidates") or []
        shown_a = min(a_count, admission.A_CAP)

        print(f"  ── {rec.get('at')}  reason={judgment.get('reason')}  labels={labels}")
        print(f"     候选 {total} 行；判 A 的 {a_count} 行；实际给原文 {shown_a} 行"
              f"（A_CAP={admission.A_CAP}）")

        # 旧代码 = _scene_block(scene)：只有场面那一句，没有任何"我少了什么"
        old_text = "【场面备注】\n" + " ".join(scene.split())[:admission.SCENE_CAP]
        # 新代码 = 打过补丁的 _scene_block(scene, total=, shown=)
        new_text = admission._scene_block(scene, total=total, shown=shown_a)

        print("     旧（她当时实际拿到的系统块）：")
        print("        " + old_text.replace("\n", " / ")[:100])
        print("     新（现在会拿到的）：")
        for line in new_text.splitlines():
            print("        " + line[:110])

        joined = "\n".join(build_persona_blocks(
            snap, is_group=True, group_recent_context=new_text,
        ))
        landed = "原文没有给你" in joined
        # 断言是**门槛感知**的：给了 ≤ 门槛 行原文时声明该在，否则不该在。
        should = shown_a <= admission.OMITTED_NOTE_MAX_SHOWN
        good_note = landed if should else not landed
        mark = "ok" if good_note else "!!"
        why = ("该出现" if should else "按门槛不该出现") + f"（给原文 {shown_a} 行）"
        print(f"     {mark} 声明{why}，且确实{'在' if landed else '不在'} prompt 块里"
              f"{'' if good_note else ' ← 这一环断了'}")
        header = "[群聊最近消息·仅作上下文]" in joined
        print(f"     {'ok' if header else '!!'} 块名仍是 [群聊最近消息·仅作上下文]（群聊语义没变）")
        ok = ok and good_note and header
        print()
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="信息缺口看板（只读）")
    parser.add_argument("--verify", action="store_true", help="离线验证装配产物")
    parser.add_argument("--replay", action="store_true", help="用真实 admission 记录重放整条链")
    parser.add_argument("--readout", action="store_true", help="读生产事件流")
    parser.add_argument("--limit", type=int, default=2, help="重放几条（默认 2）")
    parser.add_argument("--days", type=int, default=7, help="读数回看天数（默认 7）")
    parser.add_argument("--db", default=None, help="meta 库路径（默认 data/yinyue_meta.db）")
    args = parser.parse_args()
    both = not (args.verify or args.readout or args.replay)
    ok = True
    if both or args.verify:
        ok = verify() and ok
    if both or args.replay:
        ok = replay(max(1, args.limit)) and ok
    if both or args.readout:
        ok = readout(max(1, args.days), args.db) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
