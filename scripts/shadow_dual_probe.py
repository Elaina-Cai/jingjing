#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""影子双轨探针 v1：同一批真实输入，A=当前口径 / B=候选口径，只读 + 只调 LLM。

为什么（《从Cortico学什么》§六 第 0 项）
========================================
裁定 1 之后，判定/表达层的唯一裁判是「效果好不好」，而效果必须可测。
本探针给的是**反事实读数**：同一批真实回合、同一个 ask，分别按当前口径与候选口径
重新生成回复并比较；她当时真发出去的那条额外作为参考列（零成本，来自 terminal_events）。
它回答「换成候选会变成什么样」，**不回答「哪条更好」**——后者由人判。

纪律
====
* 只读：meta 库以 ``mode=ro`` 打开；不发送、不写任何库/记忆/事件；
* 成本：每回合最多 2 次 LLM 调用（baseline.mode=real 时 A 轨省掉）；报告写清调用数；
* 分母为 0 → ``n/a``（与 scripts/expr_signal_report.py 同一口径，不是 0.0）；
* 确定性等距抽样；报告带 spec 哈希与调用参数，便于同 spec 复跑对照。

候选 spec（见 configs/shadow_variants/example_null.json）
=========================================================
{
  "name": "candidate_x",
  "notes": "为什么测这个候选",
  "baseline":  {"context": "current", "system_mode": "online", "mode": "regen",
                "temperature": 0.7, "max_tokens": 180},
  "candidate": {"context": "window", "system_mode": "online_append",
                "system_file": "configs/prompts/xxx.txt",
                "temperature": 0.7, "max_tokens": 180},
  "extras": {"<chat_id>": {"group_baseline": "...", "stance": "...",
                           "person_notes": {"昵称": "备注"}}}
}

* ``context``: ``current``（现状：最近 30 条全文 + 6 条流水召回）|
  ``window``（新窗口：最近 40 条全文 + 更早压缩 + 场外补充）；
* ``system_mode``: ``online``（线上人设）| ``file``（整段替换，读 system_file）|
  ``online_append``（线上人设后追加，读 system_file）；
* ``baseline.mode``: ``regen``（默认，重生成）| ``real``（直接取她当时真发的，
  省一次调用，但 A 的历史口径与 B 不一致——只适合粗筛）；
* ``extras`` 仅 window 轨使用（与 scripts/ab_replay_window.py 同源口径），可省略。

v1 边界
=======
只覆盖「上下文构造 + system prompt + 采样参数」候选轴；**不覆盖闸门内部逻辑**
（如 repeat guard / output gate 的去留）——那需要在生成链上做 dry-run 影子模式，另立项。
LLM 本身有随机性：先跑 example_null（A/B 同口径）量出噪声地板，候选差异要超过它才算数。

Run:
    python scripts/shadow_dual_probe.py --spec configs/shadow_variants/example_null.json --dry-run
    python scripts/shadow_dual_probe.py --spec configs/shadow_variants/example_null.json --limit 3
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 独立运行时 core.* 可导
    sys.path.insert(0, str(ROOT))
META = ROOT / "data" / "yinyue_meta.db"
TEST_PARENT = re.compile(r"^m\d+$")
TRIGGER_TYPES = ("message", "question", "request")

#: 与 ab_replay_window.py 同源的参考窗口（单文件纯函数，不复制口径）。
_spec = importlib.util.spec_from_file_location(
    "preview_chat_window", ROOT / "scripts" / "preview_chat_window.py"
)
_pw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pw)  # type: ignore[union-attr]

CONTEXTS = ("current", "window")
SYSTEM_MODES = ("online", "file", "online_append")
BASELINE_MODES = ("regen", "real")
DEFAULT_MODEL = "deepseek-chat"
DIRECTION_TOLERANCE = 2


# ------------------------------------------------------------------ spec

def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (ROOT / p)


def _check_track(track: dict, label: str) -> None:
    ctx = str(track.get("context", "current"))
    if ctx not in CONTEXTS:
        raise ValueError(f"{label}.context 只支持 {CONTEXTS}，收到 {ctx!r}")
    mode = str(track.get("system_mode", "online"))
    if mode not in SYSTEM_MODES:
        raise ValueError(f"{label}.system_mode 只支持 {SYSTEM_MODES}，收到 {mode!r}")
    if mode in ("file", "online_append"):
        raw = str(track.get("system_file") or "")
        if not raw:
            raise ValueError(f"{label}.system_mode={mode} 必须给 system_file")
        if not _resolve(raw).exists():
            raise ValueError(f"{label}.system_file 不存在：{_resolve(raw)}")
    for key in ("temperature", "max_tokens"):
        if key in track:
            try:
                float(track[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label}.{key} 不是数字：{track[key]!r}") from exc


def load_spec(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise ValueError(f"spec 不存在：{p}")
    try:
        spec = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"spec 不是合法 JSON：{exc}") from exc
    if not isinstance(spec, dict):
        raise ValueError("spec 顶层必须是对象")
    if not str(spec.get("name") or "").strip():
        raise ValueError("spec.name 不能为空")
    for key in ("baseline", "candidate"):
        track = spec.get(key)
        if not isinstance(track, dict):
            raise ValueError(f"spec.{key} 必须是对象")
        _check_track(track, key)
    bmode = str(spec.get("baseline", {}).get("mode", "regen"))
    if bmode not in BASELINE_MODES:
        raise ValueError(f"baseline.mode 只支持 {BASELINE_MODES}，收到 {bmode!r}")
    spec["_path"] = str(p)
    spec["_hash"] = hashlib.sha1(
        json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return spec


def _read_text(path_str: str) -> str:
    return _resolve(path_str).read_text(encoding="utf-8-sig").strip()


# ------------------------------------------------------------------ 真实回合

def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{META}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_persona() -> str:
    con = connect()
    try:
        row = con.execute("select system_prompt from personas where id=1").fetchone()
        return str((row["system_prompt"] if row else "") or "")
    finally:
        con.close()


def _even_sample(rows: list[dict], n: int) -> list[dict]:
    """确定性等距抽样：同输入同输出，无随机。"""
    if n <= 0 or not rows:
        return []
    if len(rows) <= n:
        return list(rows)
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def pick_turns(limit: int, chats: list[str] | None = None, since: str = "") -> list[dict]:
    """真实回合：``reply`` 行 + 同会话里它前面最近的一条**别人**触发消息。"""
    chat_filter = [c for c in (chats or []) if c]
    marks = ",".join("?" * len(TRIGGER_TYPES))
    con = connect()
    try:
        sql = (
            "select id, chat_id, occurred_at, content, metadata_json from terminal_events "
            "where event_type='reply' and content is not null and trim(content)<>''"
        )
        params: list = []
        if chat_filter:
            sql += f" and chat_id in ({','.join('?' for _ in chat_filter)})"
            params.extend(chat_filter)
        sql += " order by occurred_at, id"
        replies = []
        for row in con.execute(sql, params):
            try:
                meta = json.loads(row["metadata_json"] or "{}")
            except Exception:  # noqa: BLE001
                meta = {}
            if TEST_PARENT.match(str(meta.get("parent_msg_id") or "")):
                continue
            if since and str(row["occurred_at"]) < since:
                continue
            replies.append(dict(row))

        turns: list[dict] = []
        for row in replies:
            trig = con.execute(
                "select content from terminal_events "
                "where chat_id=? and occurred_at<? "
                f"and event_type in ({marks}) "
                "and coalesce(json_extract(metadata_json,'$.is_self'),0)=0 "
                "and content is not null and trim(content)<>'' "
                "and content not like '<?xml%' and content not like '<sysmsg%' "
                "order by occurred_at desc limit 1",
                (row["chat_id"], row["occurred_at"], *TRIGGER_TYPES),
            ).fetchone()
            if not trig:
                continue
            chat_id = str(row["chat_id"])
            turns.append({
                "chat_id": chat_id,
                "at": str(row["occurred_at"])[:16],
                "trigger": str(trig["content"]).strip()[:200],
                "reply": str(row["content"]).strip(),
                "is_group": chat_id.endswith("@chatroom"),
            })
    finally:
        con.close()
    return _even_sample(turns, limit)


# ------------------------------------------------------------------ 上下文构造

def _rules(chat_id: str) -> str:
    if chat_id.endswith("@chatroom"):
        return ("[群聊规则]\n- 群里 1~3 句，@ 开头，可以接梗吐槽，"
                "不暴露私聊与真实授权关系。")
    return "[私聊规则]\n- 1~3 句，叫「主人」，亲昵自然，不 @ 前缀。"


def _upto(rows: list[dict], at: str) -> list[dict]:
    return [r for r in rows if r.get("dt") and str(r.get("occurred_at", "")) < at]


def build_current_context(chat_id: str, at: str, persona: str, rows: list[dict]) -> str:
    """现状口径：最近 30 条全文 + 6 条「流水」式召回（复现现状失败模式）。"""
    upto = _upto(rows, at)
    recent30 = [_pw.fmt_full(r) for r in upto[-30:]]
    recall6 = [_pw.fmt_full(r) for r in upto[-62:-56]]
    return "\n\n".join([
        persona,
        "[最近消息·仅作上下文]\n" + "\n".join(recent30),
        "[事件记忆·仅作当前对话参考]\n" + "\n".join(recall6),
        _rules(chat_id),
    ]).strip()


def build_window_context(chat_id: str, at: str, persona: str, rows: list[dict],
                         extras: dict | None = None) -> str:
    """新窗口口径：最近 40 条全文 + 更早压缩 + 场外补充（同 ab_replay_window B 轨）。"""
    extras = extras or {}
    upto = _upto(rows, at)
    full = upto[-40:]
    older = upto[: max(0, len(upto) - 40)]
    lines_full = [_pw.fmt_full(r) for r in full]
    lines_older, _mode = _pw.shrink(older, 900) if older else ([], "")
    ext: list[str] = []
    if extras.get("group_baseline"):
        ext.append("[群基线]\n" + str(extras["group_baseline"]))
    notes = extras.get("person_notes") or {}
    speakers = [r.get("speaker") for r in upto[-20:]]
    card_lines = [f"- {name}：{notes[name]}"
                  for name in dict.fromkeys(speakers) if name in notes]
    if card_lines:
        ext.append("[在场人]\n" + "\n".join(card_lines))
    if extras.get("stance"):
        ext.append("[她的立场]\n" + str(extras["stance"]))
    blocks = [persona]
    if ext:
        blocks.append("\n".join(ext))
    blocks.append("[最近对话·原文]\n" + "\n".join(lines_full))
    if lines_older:
        blocks.append("[今天更早·缩略]\n" + "\n".join(lines_older))
    blocks.append(_rules(chat_id))
    return "\n\n".join(blocks).strip()


ASK_TEMPLATE = "轮到你了，请用鲸鲸的口吻回一条（1~3 句）。刚才这条是：{trigger}"


def resolve_system(track: dict, persona: str) -> str:
    mode = str(track.get("system_mode", "online"))
    if mode == "online":
        return persona
    if mode == "file":
        return _read_text(str(track["system_file"]))
    return (persona + "\n\n" + _read_text(str(track["system_file"]))).strip()


def compose_track(track: dict, turn: dict, persona: str, rows: list[dict],
                  extras: dict | None) -> list[dict]:
    system_text = resolve_system(track, persona)
    if str(track.get("context", "current")) == "window":
        ctx = build_window_context(turn["chat_id"], turn["at"], system_text, rows, extras)
    else:
        ctx = build_current_context(turn["chat_id"], turn["at"], system_text, rows)
    return [
        {"role": "system", "content": ctx},
        {"role": "user", "content": ASK_TEMPLATE.format(trigger=turn["trigger"])},
    ]


# ------------------------------------------------------------------ 双轨执行

def _track_params(track: dict, model: str) -> dict:
    return {
        "model": str(track.get("model") or model),
        "temperature": float(track.get("temperature", 0.7)),
        "max_tokens": int(track.get("max_tokens", 180)),
    }


def _safe_call(complete, messages: list[dict], params: dict) -> tuple[str, str | None]:
    try:
        text = complete(messages, **params)
        return str(text or "").strip(), None
    except Exception as exc:  # noqa: BLE001 - 探针不能因单次调用失败整体崩
        return "", f"<调用失败：{exc}>"


def run_probe(turns: list[dict], spec: dict, persona: str, complete,
              model: str = DEFAULT_MODEL, dry_run: bool = False,
              rows_provider=None) -> list[dict]:
    """跑双轨。``rows_provider(chat_id)`` 仅测试注入；生产默认读 chat_window。"""
    baseline = spec["baseline"]
    candidate = spec["candidate"]
    bmode = str(baseline.get("mode", "regen"))
    extras_by_chat = spec.get("extras") or {}
    rows_cache: dict[str, list[dict]] = {}
    load_rows = rows_provider or (lambda cid: _pw.load_rows(cid, days=7))
    results: list[dict] = []
    for index, turn in enumerate(turns, 1):
        chat_id = turn["chat_id"]
        if chat_id not in rows_cache:
            rows_cache[chat_id] = load_rows(chat_id)
        rows = rows_cache[chat_id]
        extras = extras_by_chat.get(chat_id) or {}
        a_msgs = compose_track(baseline, turn, persona, rows, extras)
        b_msgs = compose_track(candidate, turn, persona, rows, extras)
        item = {
            **turn,
            "index": index,
            "a_msgs": a_msgs,
            "b_msgs": b_msgs,
            "a_text": "",
            "b_text": "",
            "a_error": None,
            "b_error": None,
            "a_source": "real" if bmode == "real" else "regen",
        }
        if dry_run:
            results.append(item)
            continue
        if bmode == "real":
            item["a_text"], item["a_error"] = turn["reply"], None
        else:
            item["a_text"], item["a_error"] = _safe_call(
                complete, a_msgs, _track_params(baseline, model))
        item["b_text"], item["b_error"] = _safe_call(
            complete, b_msgs, _track_params(candidate, model))
        results.append(item)
        print(f"  {index}/{len(turns)} A={len(item['a_text'])}字 B={len(item['b_text'])}字")
    return results


# ------------------------------------------------------------------ 读数

def _pct(numerator: int, denominator: int) -> str:
    return "n/a" if denominator <= 0 else f"{numerator * 100.0 / denominator:.1f}%"


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _mean(values: list[int]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _fmt_num(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _direction(a: str, b: str) -> str:
    """B 相对 A 的方向。±2 字内视为同长，避免把空白差异报成改写。"""
    la, lb = len(a.strip()), len(b.strip())
    if la == 0 and lb == 0:
        return "both_empty"
    if la == 0:
        return "longer"
    if lb == 0:
        return "shorter"
    if abs(lb - la) <= DIRECTION_TOLERANCE:
        return "same"
    return "shorter" if lb < la else "longer"


def _canned_hits(text: str) -> list[str]:
    from core.speech.expression_stop import detect_canned_phrases

    return list(detect_canned_phrases(text))


def _residual_hits(text: str) -> list[str]:
    from core.speech.expression_stop import detect_residual_tails

    return list(detect_residual_tails(text))


def compute_metrics(results: list[dict]) -> dict:
    both = [r for r in results
            if not r["a_error"] and not r["b_error"]
            and r["a_text"].strip() and r["b_text"].strip()]
    directions = [_direction(r["a_text"], r["b_text"]) for r in both]
    changed = sum(1 for r in both if r["a_text"].strip() != r["b_text"].strip())
    a_len = [len(r["a_text"].strip()) for r in both]
    b_len = [len(r["b_text"].strip()) for r in both]
    real_dirs = [_direction(r["reply"], r["b_text"]) for r in both if r["reply"].strip()]
    return {
        "n_total": len(results),
        "n_both_ok": len(both),
        "n_errors": sum(1 for r in results if r["a_error"] or r["b_error"]),
        "len_a_median": _median(a_len),
        "len_b_median": _median(b_len),
        "len_a_mean": _mean(a_len),
        "len_b_mean": _mean(b_len),
        "dir_shorter": directions.count("shorter"),
        "dir_same": directions.count("same"),
        "dir_longer": directions.count("longer"),
        "changed": changed,
        "canned_a": sum(1 for r in both if _canned_hits(r["a_text"])),
        "canned_b": sum(1 for r in both if _canned_hits(r["b_text"])),
        "residual_a": sum(1 for r in both if _residual_hits(r["a_text"])),
        "residual_b": sum(1 for r in both if _residual_hits(r["b_text"])),
        "real_dir_shorter": real_dirs.count("shorter"),
        "real_dir_same": real_dirs.count("same"),
        "real_dir_longer": real_dirs.count("longer"),
        "real_n": len(real_dirs),
    }


def build_report(results: list[dict], metrics: dict, spec: dict,
                 model: str, calls: int) -> str:
    m = metrics
    out = [
        f"# 影子双轨探针：{spec['name']}",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- spec：`{spec['_path']}`（哈希 `{spec['_hash']}`）",
        f"- 候选说明：{spec.get('notes') or '（未写）'}",
        f"- 模型/参数：{model} · A 轨 temp={spec['baseline'].get('temperature', 0.7)} / "
        f"B 轨 temp={spec['candidate'].get('temperature', 0.7)}",
        f"- 样本：{m['n_total']} 回合（双侧有效 {m['n_both_ok']}，调用失败 {m['n_errors']}）；"
        f"LLM 调用 {calls} 次",
        f"- 数据源：`data/yinyue_meta.db` terminal_events（只读；A={results[0]['a_source'] if results else 'n/a'}）",
        "",
        "## 一、逐项读数（A=当前口径，B=候选口径）",
        "",
        "| 指标 | A | B | 说明 |",
        "|---|---|---|---|",
        f"| 长度中位数 | {_fmt_num(m['len_a_median'])} 字 | {_fmt_num(m['len_b_median'])} 字 | 双侧有效 {m['n_both_ok']} 条 |",
        f"| 长度均值 | {_fmt_num(m['len_a_mean'])} 字 | {_fmt_num(m['len_b_mean'])} 字 | — |",
        f"| 套话率 | {_pct(m['canned_a'], m['n_both_ok'])} | {_pct(m['canned_b'], m['n_both_ok'])} | `guards.detect_canned_phrases` |",
        f"| 残留尾率 | {_pct(m['residual_a'], m['n_both_ok'])} | {_pct(m['residual_b'], m['n_both_ok'])} | `guards.detect_residual_tails` |",
        f"| B 变化率 | — | {_pct(m['changed'], m['n_both_ok'])} | B 文本 ≠ A 文本（含标点） |",
        f"| B 更短 / 同长 / 更长 | — | {m['dir_shorter']} / {m['dir_same']} / {m['dir_longer']} | ±{DIRECTION_TOLERANCE} 字内算同长 |",
        f"| B 相对**真实回复**更短/同/长 | — | {m['real_dir_shorter']} / {m['real_dir_same']} / {m['real_dir_longer']}（n={m['real_n']}） | 参考列，非受控比较 |",
        "",
        "## 二、逐回合样例",
        "",
    ]
    for r in results:
        out += [
            f"### {r['index']}. [{r['at']}] {r['chat_id']}"
            + ("（群聊）" if r["is_group"] else "（私聊）"),
            "",
            f"- **触发**：{r['trigger']}",
            f"- **真实回复**（参考，{len(r['reply'])} 字）：{r['reply']}",
            f"- **A 当前口径**（{r['a_source']}，{len(r['a_text'])} 字）"
            + (f" ⚠️ {r['a_error']}" if r["a_error"] else "")
            + f"：{r['a_text']}",
            f"- **B 候选口径**（{len(r['b_text'])} 字）"
            + (f" ⚠️ {r['b_error']}" if r["b_error"] else "")
            + f"：{r['b_text']}",
            "",
        ]
    out += [
        "## 三、边界（读数前必读）",
        "",
        "- 样本小、LLM 有随机性：**先用 example_null（A/B 同口径）量噪声地板**，"
        "候选差异要超过地板才算信号；",
        "- 本探针只覆盖「上下文构造 + system prompt + 采样参数」；闸门内部逻辑"
        "（repeat guard / output gate 等）不在此列，需要生成链 dry-run 影子模式，另立项；",
        "- 「哪条更好」不在数字里——分母为 0 一律 `n/a`，取样例由人判。",
        "",
    ]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ CLI

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--spec", required=True, help="候选 spec JSON（见 configs/shadow_variants/）")
    ap.add_argument("--limit", type=int, default=3, help="回合数（每回合最多 2 次调用）")
    ap.add_argument("--chats", default="", help="逗号分隔的 chat_id 过滤；空=全部")
    ap.add_argument("--since", default="", help="只取该时刻之后的回合（字符串比较）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true", help="只选回合+拼 prompt，不调 LLM、不落盘")
    ap.add_argument("--out", default="", help="报告路径；默认 output/shadow_dual_<name>_<ts>.md")
    args = ap.parse_args()

    try:
        spec = load_spec(args.spec)
    except ValueError as exc:
        print(f"spec 错误：{exc}")
        return 2

    turns = pick_turns(args.limit, [c.strip() for c in args.chats.split(",")],
                       since=args.since)
    groups = sum(1 for t in turns if t["is_group"])
    print(f"回合：{len(turns)} 条（群聊 {groups} / 私聊 {len(turns) - groups}）"
          f" | 候选：{spec['name']} | baseline.mode={spec['baseline'].get('mode', 'regen')}")
    if not turns:
        print("没有可用回合。")
        return 1
    persona = load_persona()
    print(f"人设：{'已加载 ' + str(len(persona)) + ' 字' if persona else '读不到（候选若为 online 会是空 system）'}")

    if args.dry_run:
        results = run_probe(turns, spec, persona, complete=lambda *a, **k: "", dry_run=True)
        for r in results:
            print(f"  {r['index']}. [{r['at']}] {r['chat_id']} "
                  f"A_prompt={sum(len(m['content']) for m in r['a_msgs'])}字 "
                  f"B_prompt={sum(len(m['content']) for m in r['b_msgs'])}字")
        print("dry-run：未调 LLM、未落盘。去掉 --dry-run 才会真跑。")
        return 0

    from openai import OpenAI

    key = ""
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            key = line.split("=", 1)[1].strip()
    if not key:
        print("没找到 DEEPSEEK_API_KEY。")
        return 1
    client = OpenAI(api_key=key, base_url="https://api.deepseek.com/v1")

    def complete(messages: list[dict], **params) -> str:
        resp = client.chat.completions.create(messages=messages, **params)
        return (resp.choices[0].message.content or "").strip()

    calls = 2 * len(turns) if spec["baseline"].get("mode", "regen") == "regen" else len(turns)
    results = run_probe(turns, spec, persona, complete, model=args.model)
    metrics = compute_metrics(results)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = Path(args.out) if args.out else (
        ROOT / "output" / f"shadow_dual_{spec['name']}_{stamp}.md")
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(build_report(results, metrics, spec, args.model, calls), encoding="utf-8")
    sidecar = base.with_suffix(".json")
    sidecar.write_text(json.dumps(
        {"spec": {k: v for k, v in spec.items() if not k.startswith("_")},
         "spec_hash": spec["_hash"], "metrics": metrics, "results": results},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告：{base}\n原始数据：{sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
