#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""工具规划探针（只读）——『尺子』，不是『读数』。

口径（配套 configs/tool_planning_cases.json）：
  * 固化：用例原话 / 允许工具集 / 机械硬约束(hard) / 警示项(warn)
  * 不固化：模型每次实际选了哪个工具、什么参数——那是读数，记入报告、不写死
  * 只调 LLM：**不执行任何工具、不写任何数据库**；报告落 output/tool_planning/
  * 模型 / 提示词 / 工具变化后应重跑；本探针是手动体检项，不进 CI 门禁

用法：
  python scripts/tool_planning_probe.py                  # 全部用例 ×3 轮
  python scripts/tool_planning_probe.py --runs 1
  python scripts/tool_planning_probe.py --case bili_role_analysis --runs 5
  python scripts/tool_planning_probe.py --dry-run        # 只看用例与约束，不调模型

评分：hard 全过 = 该轮通过；warn 只提示不改判。跳过的参数检查（未用该工具）按通过计，
但在报告里标注 skip。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

CASE_FILE = ROOT / "configs" / "tool_planning_cases.json"
REPORT_DIR = ROOT / "output" / "tool_planning"

# 会改变外部世界/库的工具；探针本身不执行它们，但用来判"该不该走到写"
WRITE_GROUP = {
    "send_message", "send_sticker", "notify_user",
    "create_reminder", "create_todo", "complete_todo",
    "remember", "emit_event", "correct_emoji_desc",
}


def _load_cases() -> list[dict]:
    data = json.loads(CASE_FILE.read_text(encoding="utf-8"))
    return list(data.get("cases") or [])


def _check_label(chk: dict) -> str:
    t = chk.get("type")
    if t == "required_any_tool":
        return "需要工具家族:" + "/".join(chk.get("tools") or [])
    if t == "required_tool":
        return "需要工具:" + str(chk.get("tool"))
    if t == "forbid_group":
        return "禁止组:" + str(chk.get("group") or chk.get("tools"))
    if t == "no_tool_calls":
        return "必须零调用"
    if t == "arg_any":
        return f"{chk.get('tool')}.{chk.get('arg')} 含 {chk.get('values')}"
    if t == "arg_date_offset":
        try:
            off = int(chk.get("days") or 0)
        except Exception:  # noqa: BLE001
            off = 0
        return f"{chk.get('tool')}.{chk.get('arg')}=今天{off:+d}天"
    return str(t)


def _fill_templates(text: str, now: datetime) -> str:
    mapping = {
        "{now}": now.strftime("%Y-%m-%d %H:%M"),
        "{weekday}": "周" + "一二三四五六日"[now.weekday()],
        "{today}": now.strftime("%Y-%m-%d"),
        "{today+1}": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
        "{today+2}": (now + timedelta(days=2)).strftime("%Y-%m-%d"),
        "{today-1}": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
    }
    out = str(text or "")
    for key, value in mapping.items():
        out = out.replace(key, value)
    return out


def _norm_calls(calls: list) -> list[dict]:
    out: list[dict] = []
    for c in calls or []:
        args = c.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:  # noqa: BLE001
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {"_raw": str(args)}
        out.append({
            "name": str(c.get("name") or ""),
            "arguments": args,
            "id": str(c.get("id") or ""),
        })
    return out


def _hops_of(run: dict) -> dict[int, list[dict]]:
    hops: dict[int, list[dict]] = {1: list(run.get("hop1") or [])}
    hop2 = run.get("hop2") or {}
    if hop2.get("calls") is not None:
        hops[2] = list(hop2.get("calls") or [])
    return hops


def _calls_in(run: dict, spec: dict) -> list[dict]:
    hops = _hops_of(run)
    want = spec.get("hops")
    if not want:
        return [c for h in sorted(hops) for c in hops[h]]
    out: list[dict] = []
    for h in want:
        out.extend(hops.get(int(h), []))
    return out


def _arg_str(call: dict, arg: str) -> str:
    args = call.get("arguments") or {}
    if arg not in args:
        return ""
    return str(args.get(arg))


def _eval_check(check: dict, run: dict, now: datetime) -> dict:
    t = check.get("type")
    calls = _calls_in(run, check)

    if t == "no_tool_calls":
        bad = [c.get("name") for c in calls]
        return {"status": "fail", "detail": f"出现调用 {bad}"} if bad else {"status": "pass", "detail": "零调用"}

    if t == "required_any_tool":
        want = set(check.get("tools") or [])
        hit = [c.get("name") for c in calls if c.get("name") in want]
        if hit:
            return {"status": "pass", "detail": "命中 " + ",".join(sorted(set(hit)))}
        return {"status": "fail", "detail": "未调用期望家族 " + "/".join(sorted(want))}

    if t == "required_tool":
        want = str(check.get("tool") or "")
        if any(c.get("name") == want for c in calls):
            return {"status": "pass", "detail": f"调用了 {want}"}
        return {"status": "fail", "detail": f"未调用 {want}"}

    if t == "forbid_group":
        if check.get("group") == "write":
            group = WRITE_GROUP
        else:
            group = set(check.get("tools") or [])
        bad = [c.get("name") for c in calls if c.get("name") in group]
        if bad:
            return {"status": "fail", "detail": f"触了禁止组 {bad}"}
        return {"status": "pass", "detail": "未触禁止组"}

    if t in ("arg_any", "arg_date_offset"):
        tool = str(check.get("tool") or "")
        used = [c for c in calls if c.get("name") == tool]
        if not used:
            if check.get("on_missing_tool") == "fail":
                return {"status": "fail", "detail": f"未调用 {tool}（该约束要求必须用）"}
            return {"status": "skip", "detail": f"未调用 {tool}，参数检查跳过"}
        if t == "arg_any":
            values = [str(v) for v in (check.get("values") or [])]
            for c in used:
                arg_val = _arg_str(c, str(check.get("arg") or ""))
                if any(v in arg_val for v in values):
                    return {"status": "pass", "detail": f"{tool}.{check.get('arg')} 命中「{arg_val}」"}
            got = "; ".join(_arg_str(c, str(check.get("arg") or "")) for c in used)
            return {"status": "fail", "detail": f"{tool}.{check.get('arg')} 未命中 {values}（实际：{got}）"}
        days = int(check.get("days") or 0)
        want_date = (now + timedelta(days=days)).strftime("%Y-%m-%d")
        for c in used:
            if want_date in _arg_str(c, str(check.get("arg") or "")):
                return {"status": "pass", "detail": f"{tool}.{check.get('arg')} 含 {want_date}"}
        got = "; ".join(_arg_str(c, str(check.get("arg") or "")) for c in used)
        return {"status": "fail", "detail": f"{tool}.{check.get('arg')} 不含 {want_date}（实际：{got}）"}

    return {"status": "skip", "detail": f"未知检查类型 {t}"}


def _plan_signature(run: dict) -> str:
    sig = []
    for c in (run.get("hop1") or []):
        sig.append([c.get("name"), json.dumps(c.get("arguments") or {}, ensure_ascii=False, sort_keys=True)])
    return json.dumps(sig, ensure_ascii=False, sort_keys=True)


async def _run_one(case: dict, agent, llm, schemas: list, system: str, now: datetime) -> dict:
    started = time.monotonic()
    rec: dict = {"hop1": [], "hop2": None, "error": ""}
    try:
        try:
            user = agent._build_research_prompt(case.get("message") or "", "", "")
        except Exception:  # noqa: BLE001
            user = f'用户现在说: "{case.get("message") or ""}"'
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        resp = await llm.chat_tools(
            messages=messages, tools=schemas, tool_choice="auto",
            agent_name="tool_planning_probe", temperature=0.1, max_tokens=500,
        )
        if resp.get("error"):
            rec["error"] = str(resp.get("error"))[:300]
        rec["hop1"] = _norm_calls(resp.get("tool_calls") or [])
        rec["hop1_content"] = str(resp.get("content") or "")[:400]

        fake = case.get("fake_results") or {}
        if fake and rec["hop1"]:
            assistant_calls = [{
                "id": c["id"] or f"call_{i}",
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c["arguments"] or {}, ensure_ascii=False)},
            } for i, c in enumerate(rec["hop1"])]
            messages.append({"role": "assistant", "content": rec["hop1_content"], "tool_calls": assistant_calls})
            for c in assistant_calls:
                name = c["function"]["name"]
                fake_text = str(fake.get(name) or "（探针未提供该工具的假结果）")
                messages.append({
                    "role": "tool",
                    "tool_call_id": c["id"],
                    "content": _fill_templates(fake_text, now),
                })
            resp2 = await llm.chat_tools(
                messages=messages, tools=schemas, tool_choice="auto",
                agent_name="tool_planning_probe", temperature=0.1, max_tokens=500,
            )
            rec["hop2"] = {
                "calls": _norm_calls(resp2.get("tool_calls") or []),
                "content": str(resp2.get("content") or "")[:600],
                "error": str(resp2.get("error") or "")[:300],
            }
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"[:300]

    rec["seconds"] = round(time.monotonic() - started, 2)
    hard_spec = case.get("hard") or []
    warn_spec = case.get("warn") or []
    rec["hard"] = []
    for chk in hard_spec:
        item = _eval_check(chk, rec, now)
        item["check"] = _check_label(chk)
        rec["hard"].append(item)
    rec["warn"] = []
    for chk in warn_spec:
        item = _eval_check(chk, rec, now)
        item["check"] = _check_label(chk)
        rec["warn"].append(item)
    if rec["error"]:
        rec["hard"] = [{"check": "llm", "status": "fail", "detail": rec["error"]}]
    rec["hard_pass"] = all(x.get("status") != "fail" for x in rec["hard"])
    rec["signature"] = _plan_signature(rec)
    return rec


async def _main_async(args, cases: list[dict]) -> int:
    from core.bootstrap import get_tools, get_llm  # 延迟导入：--dry-run 不初始化

    tools = get_tools().all_tools()
    schemas = [t.schema() for t in tools.values()]
    agent_mod = __import__("agents.conversational", fromlist=["ConversationalAgent"])
    agent = agent_mod.ConversationalAgent()
    try:
        system = agent._build_system_prompt(False)
    except Exception as exc:  # noqa: BLE001
        system = ""
        print(f"!! system prompt 构建失败，退化用空串：{exc}")
    llm = get_llm()

    now = datetime.now()
    model_name = ""
    try:
        from core.config.settings import config
        model_name = str(getattr(config.llm, "deepseek_model", "") or "")
    except Exception:  # noqa: BLE001
        pass

    print(f"=== 工具规划探针 === model={model_name or '(未知)'} tools={len(schemas)} "
          f"system_chars={len(system)} runs={args.runs}")
    report: dict = {
        "generated_at": now.isoformat(timespec="seconds"),
        "model": model_name,
        "tool_count": len(schemas),
        "system_prompt_chars": len(system),
        "runs_per_case": args.runs,
        "note": "只记录调用、不执行工具、不写库；hard 全过=该轮通过；warn 只提示。",
        "cases": [],
    }
    overall_total = 0
    overall_pass = 0
    for case in cases:
        entry = {
            "id": case.get("id"),
            "message": case.get("message"),
            "why": case.get("why"),
            "notes": case.get("notes"),
            "hard_spec": [_check_label(c) for c in (case.get("hard") or [])],
            "runs": [],
        }
        print(f"\n[{case.get('id')}] {case.get('message')}")
        for i in range(args.runs):
            rec = await _run_one(case, agent, llm, schemas, system, now)
            rec["run"] = i + 1
            entry["runs"].append(rec)
            overall_total += 1
            overall_pass += 1 if rec["hard_pass"] else 0
            tag = "PASS" if rec["hard_pass"] else "FAIL"
            hop1 = ", ".join(f"{c['name']}({json.dumps(c['arguments'], ensure_ascii=False)[:60]})"
                             for c in rec["hop1"]) or "-"
            hop2 = ""
            if rec.get("hop2") is not None:
                calls2 = rec["hop2"].get("calls") or []
                hop2 = " | hop2=" + (", ".join(c["name"] for c in calls2) if calls2 else "收工")
            print(f"   run{rec['run']} [{tag}] hop1: {hop1}{hop2}  ({rec['seconds']}s)")
            for item in rec["hard"]:
                if item.get("status") == "fail":
                    print(f"        ✗ {item['check']}: {item['detail']}")
                elif item.get("status") == "skip":
                    print(f"        - {item['check']}: {item['detail']}")
            for item in rec["warn"]:
                if item.get("status") == "fail":
                    print(f"        ! warn {item['check']}: {item['detail']}")
            if rec.get("error"):
                print(f"        !! {rec['error']}")
        report["cases"].append(entry)

    # 稳定性：每例不同的 hop1 签名数
    print("\n=== 汇总 ===")
    print(f"硬约束：{overall_pass}/{overall_total} 轮通过")
    for entry in report["cases"]:
        sigs = {r.get("signature") for r in entry["runs"]}
        warns = sum(1 for r in entry["runs"] for w in r.get("warn") or [] if w.get("status") == "fail")
        rate = sum(1 for r in entry["runs"] if r.get("hard_pass"))
        print(f"  {entry['id']}: 通过 {rate}/{len(entry['runs'])}；计划形态 {len(sigs)} 种"
              + (f"；warn {warns}" if warns else ""))

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.json) if args.json else REPORT_DIR / f"planning_report_{now.strftime('%Y%m%d_%H%M%S')}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{out_path}")
    return 1 if overall_pass < overall_total else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="工具规划探针（只读，不执行工具）")
    parser.add_argument("--runs", type=int, default=3, help="每条用例跑几轮（默认 3）")
    parser.add_argument("--case", action="append", default=[], help="只跑指定 id（可多次）")
    parser.add_argument("--json", default="", help="报告输出路径（默认 output/tool_planning/）")
    parser.add_argument("--dry-run", action="store_true", help="只列用例与约束，不调模型")
    args = parser.parse_args()

    cases = _load_cases()
    if args.case:
        want = set(args.case)
        cases = [c for c in cases if c.get("id") in want]
    if not cases:
        print("没有匹配的用例")
        return 2

    if args.dry_run:
        print(f"用例文件：{CASE_FILE}（{len(cases)} 条）")
        for c in cases:
            print(f"\n[{c.get('id')}] {c.get('message')}")
            print(f"  why: {c.get('why')}")
            print(f"  允许: {', '.join(c.get('allowed_tools') or []) or '-'}")
            for chk in c.get("hard") or []:
                print(f"  hard: {_check_label(chk)}")
            for chk in c.get("warn") or []:
                print(f"  warn: {_check_label(chk)}")
            if c.get("notes"):
                print(f"  notes: {c.get('notes')}")
        return 0

    return asyncio.run(_main_async(args, cases))


if __name__ == "__main__":
    raise SystemExit(main())
