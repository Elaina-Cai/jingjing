"""观察报告：把「这几处改动到底有没有在真实回合里发生」变成一条命令。

只读，不调模型、不外发。用法：

    python scripts/observe_cognition.py --hours 24

看三件事，分开说：
1) 真实动作与回执（账本）：say 成功/失败、只读拦截、必答卡住、评价提交；
2) 认知状态（世界库 + self_state）：评价四件套填了几条、各会话心境；
3) 提示词痕迹（data/prompt）：最近的回合里有没有「以前的事 / 还没放下 / 心境 / 只读」。

这些读数是「真实模型行为」的证据入口；它们为 0 也可能只是没有回合，不自动等于坏。
"""
from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.infra import skips  # noqa: E402 - 先保证仓库根在 sys.path


def _local_cutoff(hours: float) -> str:
    return (datetime.now() - timedelta(hours=float(hours))).strftime("%Y-%m-%d %H:%M:%S")


def _utc_cutoff(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=float(hours))).isoformat(timespec="seconds")


def ledger_rows(ledger_dir: Path, *, hours: float) -> list[dict]:
    cutoff = _local_cutoff(hours)
    rows: list[dict] = []
    for path in sorted(glob.glob(str(ledger_dir / "ledger-*.jsonl"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError as exc:
                        skips.skip("scripts/observe_cognition.py:ledger_rows", "malformed", repr(exc)[:80])
                        continue
                    if str(item.get("at") or "") >= cutoff:
                        rows.append(item)
        except OSError as exc:
            skips.skip("scripts/observe_cognition.py:ledger_rows", "absent", repr(exc)[:80])
            continue
    return rows


def summarize_ledger(rows: list[dict]) -> dict:
    acts: dict[str, int] = {}
    say_ok = say_failed = 0
    remember_ok = remember_failed = 0
    blocked: dict[str, int] = {}
    appraisals = 0
    for row in rows:
        act = str(row.get("act") or "")
        result = str(row.get("result") or "")
        acts[act] = acts.get(act, 0) + 1
        if act == "say":
            if result in {"sent", "queued", "confirmed"}:
                say_ok += 1
            elif result in {"failed", "blocked", "error"}:
                say_failed += 1
        if result in {"failed", "blocked", "error", "skipped"}:
            reason = str(row.get("blocked_reason") or "")[:40]
            if reason:
                blocked[reason] = blocked.get(reason, 0) + 1
        if act == "evaluate_experience" and result in {"committed", "dry"}:
            appraisals += 1
        if act == "remember":
            if result == "ok":
                remember_ok += 1
            elif result == "failed":
                remember_failed += 1
    return {"rows": len(rows), "acts": acts, "say_ok": say_ok, "say_failed": say_failed,
            "remember_ok": remember_ok, "remember_failed": remember_failed,
            "blocked": blocked, "appraisals": appraisals}


def delivery_stats(db: Path, *, hours: float) -> dict:
    """窗口内的投递确认：confirmed / unconfirmed / 其它，再加 attempt 失败数。"""
    out = {"confirmed": 0, "unconfirmed": 0, "other": 0, "attempt_failed": 0}
    if not Path(db).is_file():
        return out
    cutoff = time.time() - float(hours) * 3600
    conn = sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True)
    try:
        for ack, n in conn.execute(
            "select coalesce(r.ack,''), count(*) from outbound_receipt r "
            "join outbound_queue q on r.turn_id = 'queue:' || q.id "
            "where q.created_ts >= ? group by coalesce(r.ack,'')", (cutoff,)):
            key = str(ack or "")
            if key == "confirmed":
                out["confirmed"] += int(n)
            elif key in ("", "unconfirmed"):
                out["unconfirmed"] += int(n)
            else:
                out["other"] += int(n)
        out["attempt_failed"] = int(conn.execute(
            "select count(*) from outbound_attempt where started_ts >= ? and result in ('failed','not_executed')",
            (cutoff,)).fetchone()[0] or 0)
    finally:
        conn.close()
    return out


def appraisal_stats(db: Path, *, hours: float) -> dict:
    out = {"total": 0, "fields": {}, "chats": {}}
    if not Path(db).is_file():
        return out
    conn = sqlite3.connect(Path(db).as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT scope, payload FROM cognitive_events "
            "WHERE operation='appraise' AND committed_at>=? ORDER BY seq",
            (_utc_cutoff(hours),),
        ).fetchall()
    finally:
        conn.close()
    fields = {"meaning": 0, "feeling_text": 0, "tendency": 0, "unresolved_condition": 0, "next_move": 0}
    chats: dict[str, int] = {}
    for scope_text, payload_text in rows:
        try:
            payload = json.loads(payload_text or "{}")
            scope = json.loads(scope_text or "{}")
        except ValueError as exc:
            skips.skip("scripts/observe_cognition.py:appraisal_stats", "malformed", repr(exc)[:80])
            continue
        out["total"] += 1
        for key in fields:
            if str(payload.get(key) or "").strip():
                fields[key] += 1
        chat = str(scope.get("chat_id") or "?")
        chats[chat] = chats.get(chat, 0) + 1
    out["fields"] = fields
    out["chats"] = chats
    return out


def mood_stats(state_path: Path) -> dict:
    out = {"global": "", "global_age_h": None, "scoped": []}
    try:
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        skips.skip("scripts/observe_cognition.py:mood_stats", "absent", repr(exc)[:80])
        return out
    now = time.time()

    def age(item):
        try:
            return round((now - float(item.get("at") or 0)) / 3600.0, 1)
        except (TypeError, ValueError):
            return None

    mood = data.get("mood") or {}
    if str(mood.get("text") or "").strip():
        out["global"] = str(mood["text"])
        out["global_age_h"] = age(mood)
    for key, item in (data.get("moods") or {}).items():
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        chat = key
        try:
            parsed = json.loads(key)
            if isinstance(parsed, list) and len(parsed) == 3:
                chat = str(parsed[2])
        except ValueError as exc:
            skips.skip("scripts/observe_cognition.py:mood_stats", "malformed", repr(exc)[:80])
        out["scoped"].append({"chat": chat, "text": str(item["text"]), "age_h": age(item)})
    return out


def _literal(value) -> dict:
    """trace 里 wake/outcome/steps 写的是 Python repr（单引号），按字面量读。"""
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _literal_any(value):
    if isinstance(value, (dict, list)):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None


def turn_stats(trace_dir: Path, *, hours: float) -> dict:
    """该回的回没回：必答回合里有多少最终开口，只读回合有没有还在试图 say。"""
    cutoff = _local_cutoff(hours)
    out = {"turns": 0, "addressed": 0, "must_answer": 0, "must_answer_silent": 0,
           "read_only": 0, "read_only_say": 0, "spoke": 0, "rested": 0,
           "group_turns": 0, "group_smalltalk_speech": 0}
    if not Path(trace_dir).is_dir():
        return out
    for path in sorted(glob.glob(str(Path(trace_dir) / "trace-*.jsonl"))):
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError as exc:
            skips.skip("scripts/observe_cognition.py:turn_stats", "absent", repr(exc)[:80])
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    skips.skip("scripts/observe_cognition.py:turn_stats", "malformed", repr(exc)[:80])
                    continue
                if str(row.get("at") or "") < cutoff:
                    continue
                out["turns"] += 1
                wake = _literal(row.get("wake"))
                ad = wake.get("addressed") or {}
                if not isinstance(ad, dict):
                    ad = {}
                outcome = _literal(row.get("outcome"))
                said = outcome.get("said") or ()
                steps = _literal_any(row.get("steps"))
                listed = [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []
                tools = {str(step.get("tool") or "") for step in listed}
                responded = bool(said) or bool(tools & {"sticker", "show"})
                if responded:
                    out["spoke"] += 1
                if "rest" in tools:
                    out["rested"] += 1
                if str(ad.get("kind") or "").strip():
                    out["addressed"] += 1
                if str(row.get("scene") or "") == "group":
                    out["group_turns"] += 1
                    if not str(ad.get("kind") or "").strip() and responded:
                        out["group_smalltalk_speech"] += 1
                if ad.get("must_answer"):
                    out["must_answer"] += 1
                    if not responded:
                        out["must_answer_silent"] += 1
                if ad.get("read_only"):
                    out["read_only"] += 1
                    if "say" in tools:
                        out["read_only_say"] += 1
    return out


_PROMPT_MARKS = ("以前的事", "还没放下", "心境：", "之前记下的心情", "只读，不是没听见")


def prompt_stats(prompt_dir: Path, *, limit: int = 40) -> dict:
    out = {"files": 0, "marks": {mark: 0 for mark in _PROMPT_MARKS}}
    root = Path(prompt_dir)
    if not root.is_dir():
        return out
    try:
        # 真实现场是 data/prompt/<turn_id>/{request.json,parts.json}；也兼容平铺的 *.json。
        entries = [p for p in root.iterdir() if p.is_dir() or p.suffix == ".json"]
        entries = sorted(entries, key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, int(limit))]
    except OSError:
        return out
    for path in entries:
        text = ""
        try:
            if path.is_dir():
                request = path / "request.json"
                if request.is_file():
                    # 只数发给模型的 messages；request.json 里还有 tools/schema 文本，
                    # 那些描述里本来就有「还没放下」之类的词，会把读数灌成假阳性。
                    try:
                        payload = json.loads(request.read_text(encoding="utf-8", errors="replace"))
                        texts = []
                        if isinstance(payload, dict):
                            rounds = payload.get("rounds")
                            if isinstance(rounds, list):
                                for item in rounds:
                                    messages = item.get("messages") if isinstance(item, dict) else None
                                    if isinstance(messages, list):
                                        texts += [str(m.get("content") or "")
                                                  for m in messages if isinstance(m, dict)]
                            elif isinstance(payload.get("messages"), list):
                                texts += [str(m.get("content") or "")
                                          for m in payload["messages"] if isinstance(m, dict)]
                        text += "\n".join(texts) if texts else request.read_text(encoding="utf-8", errors="replace")
                    except ValueError:
                        text += request.read_text(encoding="utf-8", errors="replace")
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            skips.skip("scripts/observe_cognition.py:prompt_stats", "absent", repr(exc)[:80])
            continue
        out["files"] += 1
        for mark in _PROMPT_MARKS:
            if mark in text:
                out["marks"][mark] += 1
    return out


def render_report(*, hours: float, ledger: dict, appraisals: dict, moods: dict, prompts: dict,
                  turns: dict | None = None, delivery: dict | None = None) -> str:
    lines = [f"# 鲸鲸观察报告（最近 {hours:g} 小时）", ""]
    if turns is not None:
        lines.append("## 0. 该回的回没回（回合 trace）")
        lines.append(f"- 回合 {turns['turns']}；开口 {turns['spoke']}；选 rest {turns['rested']}")
        lines.append(f"- 有人在跟她说：{turns['addressed']}；其中必答 {turns['must_answer']}")
        if turns["must_answer"]:
            answered = turns["must_answer"] - turns["must_answer_silent"]
            lines.append(f"- 必答回合：回 {answered} / 没回 {turns['must_answer_silent']}")
        if turns["read_only"]:
            lines.append(f"- 只读回合 {turns['read_only']}；还在试图 say 的 {turns['read_only_say']}")
        if turns.get("group_turns"):
            lines.append(f"- 群回合 {turns['group_turns']}；其中没人点名时她开口 "
                         f"{turns.get('group_smalltalk_speech', 0)} 次（只作观察，不自动判错）")
        lines.append("")
    lines.append("## 1. 真实动作与回执（账本）")
    lines.append(f"- 行数：{ledger['rows']}；say 成功 {ledger['say_ok']} / 失败 {ledger['say_failed']}")
    lines.append(f"- 评价提交：{ledger['appraisals']}")
    if delivery is not None:
        lines.append(f"- 投递：确认 {delivery['confirmed']} / 未确认 {delivery['unconfirmed']} / "
                     f"其它 {delivery['other']} / 执行失败 {delivery['attempt_failed']}")
    if ledger.get("remember_ok") or ledger.get("remember_failed"):
        lines.append(f"- remember：成功 {ledger.get('remember_ok', 0)} / 失败 {ledger.get('remember_failed', 0)}")
    if ledger["blocked"]:
        for reason, count in sorted(ledger["blocked"].items(), key=lambda kv: -kv[1])[:6]:
            lines.append(f"- 拦截：{count} × {reason}")
    lines.append("")
    lines.append("## 2. 认知状态")
    fields = appraisals["fields"]
    lines.append(f"- 评价 {appraisals['total']} 条："
                 + "、".join(f"{key} {fields.get(key, 0)}" for key in ("meaning", "feeling_text", "tendency", "unresolved_condition", "next_move")))
    if appraisals["chats"]:
        lines.append("- 评价分布：" + "、".join(f"{chat} {n}" for chat, n in sorted(appraisals["chats"].items(), key=lambda kv: -kv[1])[:5]))
    if moods["global"]:
        lines.append(f"- 旧全局心境：{moods['global']}（{moods['global_age_h']} 小时前）")
    for item in moods["scoped"][:5]:
        lines.append(f"- 会话心境 {item['chat']}：{item['text']}（{item['age_h']} 小时前）")
    if not moods["scoped"] and not moods["global"]:
        lines.append("- 心境：空")
    lines.append("")
    lines.append("## 3. 提示词痕迹（最近 %d 个回合文件）" % prompts["files"])
    for mark, count in prompts["marks"].items():
        lines.append(f"- {mark}：{count}")
    lines.append("")
    lines.append("读法：这里只说明「这件事在真实回合里发生过没有」；"
                 "为 0 不自动等于坏，先看这段时间有没有对应回合。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--ledger-dir", default=str(ROOT / "data" / "loop"))
    parser.add_argument("--db", default=str(ROOT / "data" / "world.db"))
    parser.add_argument("--state", default=str(ROOT / "data" / "loop" / "self_state.json"))
    parser.add_argument("--prompt-dir", default=str(ROOT / "data" / "prompt"))
    parser.add_argument("--trace-dir", default=str(ROOT / "data" / "loop"))
    parser.add_argument("--prompt-limit", type=int, default=40)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    report = render_report(
        hours=args.hours,
        ledger=summarize_ledger(ledger_rows(Path(args.ledger_dir), hours=args.hours)),
        appraisals=appraisal_stats(Path(args.db), hours=args.hours),
        moods=mood_stats(Path(args.state)),
        prompts=prompt_stats(Path(args.prompt_dir), limit=args.prompt_limit),
        turns=turn_stats(Path(args.trace_dir), hours=args.hours),
        delivery=delivery_stats(Path(args.db), hours=args.hours),
    )
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
