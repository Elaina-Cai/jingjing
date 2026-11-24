"""P1 记录层：把每一拍摊平成一行 JSON，事后还知道她为什么那样判断。

账本（ledger）只记"她做了什么"（act / why / result / ack）；
payloads 只记"她收到什么"（system + user 原文，每次调用覆盖）。
这一层补中间那段：模型怎么回的、调了哪几个工具、工具回了什么、最后说没说、为什么没说。

落盘：data/loop/trace-YYYY-MM-DD.jsonl，一行一拍。
纪律：
  * 只在 loop 内部 import，不碰 core/agents（CONTRACT.md 的边界）；
  * 任何异常都吞掉——记录层坏了不许影响她说话；
  * 单个文件超过 64MB 就不再写（宁可缺记录，不许把磁盘写满）。
"""
from __future__ import annotations

import json
import hashlib
import os
import time
from datetime import datetime
from core.infra.envflags import text as _text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACE_DIR = os.path.join(ROOT, "data", "loop")
MAX_FILE_BYTES = 64 * 1024 * 1024

# 每段文字的截断上限（字符）。够回放，又不至于一天写几百 MB。
_CAP_SYSTEM = 16000
_CAP_PART = 6000
_CAP_CONTENT = 4000
_CAP_REASONING = 3000
_CAP_OBS = 1600
_CAP_ARG = 900
_CAP_WHY = 600


def _clip(text, cap: int) -> str:
    s = "" if text is None else str(text)
    if len(s) <= cap:
        return s
    keep = max(0, cap - 60)
    return s[:keep] + "\n…[截断 %d 字]…\n" % (len(s) - keep) + s[-40:]


def _now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def trace_path(when: float | None = None) -> str:
    day = datetime.fromtimestamp(when or time.time()).strftime("%Y-%m-%d")
    return os.path.join(TRACE_DIR, "trace-%s.jsonl" % day)


class TurnTrace:
    """一拍一张记录。构造后逐步 add，最后 write() 落盘一次。"""

    def __init__(self, turn_id: str, *, scene: str = "", db: str = "",
                 dry_run: bool = True, wake: dict | None = None,
                 user_message: str = "", scope: dict | None = None, visible_event_ids: tuple[str, ...] = ()):
        self.rec: dict = {
            "turn_id": turn_id,
            "scope": dict(scope or {}),
            "at": _now_stamp(),
            "ts": time.time(),
            "scene": scene or "",
            "db": os.path.basename(db or ""),
            "dry_run": bool(dry_run),
            "wake": _wake_facts(wake or {}),
            "trigger_message": _clip(user_message, 400),
            "visible_event_ids": list(visible_event_ids),
            "prompt": {"system": "", "parts": [], "chars": 0},
            "calls": [],
            "steps": [],
            "outcome": {},
        }
        self._t0 = time.perf_counter()

    # ---- 记录 -------------------------------------------------------------
    def prompt(self, *, system: str, parts, messages: list | None = None) -> None:
        items = []
        for name, text in parts or []:
            items.append({"name": str(name), "chars": len(text or ""),
                          "text": _clip(text, _CAP_PART)})
        self.rec["prompt"] = {
            "system": _clip(system, _CAP_SYSTEM),
            "parts": items,
            "chars": len(system or "") + sum(i["chars"] for i in items),
            "hash_basis": "initial_messages" if messages is not None else "assembled_parts",
            "sha256": hashlib.sha256(json.dumps(messages if messages is not None else {"system":system,"parts":parts},ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
        }

    def call(self, *, i: int, ms: float, envelope: dict | None) -> None:
        env = envelope or {}
        calls = []
        for c in (env.get("tool_calls") or []):
            calls.append({"name": c.get("name") or "", "arguments": c.get("arguments")})
        self.rec["calls"].append({
            "i": i,
            "ms": round(float(ms or 0.0), 1),
            "model": str(env.get("model") or ""),
            "provider": str(env.get("provider") or ""),
            "tokens": int(env.get("tokens") or 0),
            "content": _clip(env.get("content") or "", _CAP_CONTENT),
            "reasoning": _clip(env.get("reasoning") or "", _CAP_REASONING),
            "tool_calls": calls,
            "error": str(env.get("error") or "")[:300],
        })

    def step(self, *, i: int, tool: str, args: dict | None, obs: str,
             receipt: dict | None, ms: float) -> None:
        r = receipt or {}
        recorded = {
            "act": r.get("act") or "",
            "result": r.get("result") or "",
            "ack": r.get("ack") or "",
            "target": r.get("target") or "",
            "action_ref": r.get("action_ref") or "",
            "action_refs": list(r.get("action_refs") or ()),
            "why": _clip(r.get("why") or "", _CAP_WHY),
            "blocked_reason": _clip(r.get("blocked_reason") or "", 300),
        }
        # args 保留模型拟稿，回执正文记录交给投递口的文本。
        actual = r.get("text")
        if r.get("act") == "say" and isinstance(actual, str):
            recorded["text"] = _clip(actual, _CAP_CONTENT)
            recorded["text_truncated"] = len(actual) > _CAP_CONTENT
        self.rec["steps"].append({
            "i": i,
            "tool": str(tool or ""),
            "args": _clip(json.dumps(args or {}, ensure_ascii=False), _CAP_ARG),
            "ms": round(float(ms or 0.0), 1),
            "obs": _clip(obs or "", _CAP_OBS),
            "receipt": recorded,
        })

    def silent(self, text: str, reason: str = "") -> None:
        self.rec["silent_text"] = _clip(text or "", _CAP_CONTENT)
        if reason:
            self.rec["silent_reason"] = reason

    def note(self, key: str, value) -> None:
        self.rec.setdefault("notes", {})[key] = _clip(value, 500)

    def outcome(self, *, receipts, ms_total: float | None = None, completed: bool | None = None) -> None:
        said, blocked, silent_why = [], [], ""
        # 正文和实际投递状态分别取自回执的 text 与 ack。
        for r in receipts or []:
            act = r.get("act") or ""
            if act == "say":
                if (r.get("result") or "") == "sent":
                    _t = r.get("text")
                    _t = "" if _t is None else str(_t).strip()
                    said.append({"target": r.get("target") or "",
                                 "ack": r.get("ack") or "",
                                 "text": _clip(_t, 1200),
                                 "why": _clip(r.get("why") or "", _CAP_WHY)})
                else:
                    blocked.append({"target": r.get("target") or "",
                                    "result": r.get("result") or "",
                                    "reason": _clip(r.get("blocked_reason") or "", 300)})
            elif act == "silent":
                silent_why = _clip(r.get("why") or "", _CAP_WHY)
        self.rec["outcome"] = {
            "said": said,
            "blocked": blocked,
            "silent": (not said),
            "silent_why": silent_why,
            "n_receipts": len(receipts or []),
            "completed": completed,
            "ms": round(float(ms_total if ms_total is not None
                              else (time.perf_counter() - self._t0) * 1000.0), 1),
        }

    # ---- 落盘 -------------------------------------------------------------
    def write(self) -> str:
        path = trace_path(self.rec.get("ts"))
        try:
            if os.path.exists(path) and os.path.getsize(path) > MAX_FILE_BYTES:
                return ""
            os.makedirs(TRACE_DIR, exist_ok=True)
            line = json.dumps(self.rec, ensure_ascii=False, separators=(",", ":"))
            if len(line) > 200_000:  # 兜底：极端情况下也别写畸形大行
                self.rec["prompt"]["system"] = _clip(self.rec["prompt"]["system"], 2000)
                line = json.dumps(self.rec, ensure_ascii=False, separators=(",", ":"))
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            return path
        except Exception:  # noqa: BLE001 - 记录层失败不许影响这一拍
            return ""


def _wake_facts(wake: dict) -> dict:
    """只挑几件对回放有用的事实，别把整页 snapshot 的副本再存一遍。"""
    keep = {}
    for k in ("n_peers", "world_stale_ms", "world_sync_kicked",
              "world_stale_note", "reason", "chat_id"):
        v = wake.get(k)
        if v not in (None, "", False):
            keep[k] = _clip(v, 300)
    # 2026-10-27 体检要判断"夜里是不是她自己开口"：这两个布尔必须每次都写（False 也要写）
    keep["spontaneous"] = bool(wake.get("spontaneous"))
    keep["changed"] = bool(wake.get("changed"))
    if "inbound_row_ids" in wake:
        keep["inbound_row_ids"] = list(wake["inbound_row_ids"])
    addressed = wake.get("addressed") or {}
    if addressed:
        request_text = str(addressed.get("request_text") or "")
        keep["addressed"] = {
            key: _clip(addressed.get(key), 300)
            for key in ("chat_id", "kind", "who", "request_row_id")
            if addressed.get(key) not in (None, "", False)
        }
        keep["addressed"]["must_answer"] = bool(addressed.get("must_answer"))
        if request_text:
            keep["addressed"]["request_chars"] = len(request_text)
            keep["addressed"]["request_sha256"] = hashlib.sha256(
                request_text.encode("utf-8")
            ).hexdigest()
    return keep


def read_turn(turn_id: str) -> dict | None:
    """按 turn_id 回查最近两天的记录（调试用）。"""
    import glob
    for path in sorted(glob.glob(os.path.join(TRACE_DIR, "trace-*.jsonl")))[-2:]:
        try:
            for line in open(path, encoding="utf-8"):
                if turn_id in line[:200]:
                    rec = json.loads(line)
                    if rec.get("turn_id") == turn_id:
                        return rec
        except Exception:  # noqa: BLE001
            continue
    return None
