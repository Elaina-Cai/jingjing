"""试衣间（P3）：拿真实一拍的 prompt 重跑，看她在别的温度/别的嘱咐下会说什么。

铁律（写在这里，也写在 docs/试衣间-P3.md 里）：
  1. 一句话都不发出去（say 只记账内回执）
  2. 一个账本行都不写（ledger 不参与）
  3. selfstate / memo 一律不动
  4. 不写 trace（试衣间不是"她的一拍"，不留痕）
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from . import brain, ports, trace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data", "console")
MAX_REPEATS = 4
MAX_ADDENDUM = 240
MAX_VARIANTS = 6
CURRENT_LABEL = "（当前）"


from core.infra import egress
from core.infra.jsonio import atomic_write_json


def _current_route() -> dict:
    """角色表里「说话」那张卡现在指向谁。"""
    try:
        from core import llm_roles
        role = (llm_roles.load() or {}).get("chat") or {}
        return {"provider": str(role.get("provider") or ""), "model": str(role.get("model") or "")}
    except Exception:  # noqa: BLE001
        return {"provider": "", "model": ""}


def _ollama_tags(timeout: float = 1.2) -> list[str]:
    """本机 ollama 装了哪些模型（连不上就当没有）。"""
    try:
        import urllib.request
        with egress.connect("127.0.0.1", 11434, timeout_ms=3_000) as f:
            data = json.loads(f.read().decode("utf-8"))
        return [str(m.get("name") or "") for m in (data.get("models") or []) if m.get("name")]
    except Exception:  # noqa: BLE001
        return []


def models() -> dict:
    """可切换的「provider / model」清单。只读：不改任何配置。"""
    cur = _current_route()
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(provider: str, model: str, *, kind: str = "", note: str = "") -> None:
        provider = str(provider or "").strip()
        model = str(model or "").strip()
        if not provider or not model or (provider, model) in seen:
            return
        seen.add((provider, model))
        items.append({
            "provider": provider, "model": model, "kind": kind, "note": note,
            "label": "%s / %s" % (provider, model),
            "current": provider == cur.get("provider") and model == cur.get("model"),
        })

    try:
        from llm.config import load_llm_config
        cfg = load_llm_config()
        for name, spec in sorted((cfg.endpoints or {}).items()):
            add(name, spec.model, kind=spec.kind, note=(spec.base_url or "")[:60])
    except Exception:  # noqa: BLE001
        pass
    # deepseek 官网常见两档：flash（默认/快）与 chat（稳）
    add("deepseek", "deepseek-flash", kind="openai_compat", note="DeepSeek 快档（默认）")
    add("deepseek", "deepseek-chat", kind="openai_compat", note="DeepSeek 稳档")
    for tag in _ollama_tags():
        if "embed" in tag.lower():     # 向量模型不能对话，不进候选
            continue
        add("ollama", tag, kind="openai_compat", note="本机 ollama")
    if cur.get("provider") and cur.get("model"):
        add(cur["provider"], cur["model"], note="角色表当前值")
    for it in items:
        if it["current"]:
            it["label"] += CURRENT_LABEL
    return {"ok": True, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "current": cur, "items": items, "n": len(items)}


def _messages_from(rec: dict) -> list[dict]:
    prompt = (rec or {}).get("prompt") or {}
    system = str(prompt.get("system") or "")
    parts = prompt.get("parts") or []
    user = "\n\n".join(str(p.get("text") or "") for p in parts)
    if not system or not user:
        raise ValueError("这条记录里没有完整的 prompt（可能被截断或太旧）")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _one(messages: list[dict], *, llm, db: str, addendum: str, temperature: float,
         model: str | None = None, provider: str | None = None) -> dict:
    t0 = time.perf_counter()
    receipts = asyncio.run(brain.run_loop(
        db, "", llm=llm, dry_run=True, rehearse=True,
        messages_override=messages, addendum=addendum, temperature=temperature,
        model=model, provider=provider))
    ms = (time.perf_counter() - t0) * 1000.0
    rec = brain.LAST_REHEARSAL or {}
    said = [r.get("text") or "" for r in receipts if r.get("act") == "say"]
    blocked = [{"act": r.get("act"), "reason": r.get("blocked_reason") or ""}
               for r in receipts if r.get("result") == "blocked" and r.get("act") != "say"]
    calls = rec.get("calls") or []
    tokens = 0
    for c in calls:
        try:
            tokens += int((c or {}).get("tokens") or 0)
        except Exception:  # noqa: BLE001
            pass
    outcome = rec.get("outcome") or {}
    return {
        "said": [t for t in said if t],
        "blocked": blocked,
        "silent": not any(said),
        "silent_why": outcome.get("silent_why") or "",
        "steps": [{"tool": s.get("tool"), "args": s.get("args")} for s in (rec.get("steps") or [])],
        "tokens": tokens,
        "ms": round(ms, 1),
        "model": (calls[0] or {}).get("model") if calls else "",
        "reasoning": "\n\n".join(str((c or {}).get("reasoning") or "") for c in calls).strip(),
    }


def run(turn_id: str, *, addendum: str = "", temperatures: list[float] | None = None,
        repeats: int = 1, by: str = "panel",
        models: list[dict] | None = None) -> dict:
    """按 turn_id 重跑。

    temperatures 给几个值就跑几个变体；models 给几个「provider/model」就跑几个（与温度做笛卡尔积，
    总数封顶 MAX_VARIANTS）；repeats 是同一档重复几次。models 为空 = 不指定模型，用当前路由。
    """
    rec = trace.read_turn(turn_id)
    if not rec:
        return {"ok": False, "error": "找不到这一拍的记录：%s" % turn_id}
    messages = _messages_from(rec)
    temps = [float(t) for t in (temperatures or [0.1])][:MAX_REPEATS]
    reps = max(1, min(int(repeats or 1), 2))
    picks: list[dict] = []
    for m in (models or []):
        if not isinstance(m, dict):
            continue
        _prov = str(m.get("provider") or "").strip()
        _mod = str(m.get("model") or "").strip()
        if _mod:
            picks.append({"provider": _prov, "model": _mod})
    if not picks:
        picks = [{"provider": None, "model": None}]   # 不给 = 用当前路由
    add = str(addendum or "")[:MAX_ADDENDUM].strip()
    # llm.toml 是相对路径：cwd 不在项目根时会静默退回 legacy 配置（deepseek-chat），
    # 于是记录里的 deepseek-flash 跑不出来、还会撞 auth 失败落到 ollama。必须先切根。
    _cwd = os.getcwd()
    try:
        os.chdir(ROOT)
        llm = ports.get_llm()
    finally:
        os.chdir(_cwd)
    db = str(rec.get("db") or "")
    variants: list[dict] = []
    for pick in picks:
        for t in temps:
            for k in range(reps):
                if len(variants) >= MAX_VARIANTS:
                    break
                try:
                    one = _one(messages, llm=llm, db=db, addendum=add, temperature=t,
                               model=pick["model"], provider=pick["provider"])
                except Exception as exc:  # noqa: BLE001
                    one = {"said": [], "blocked": [], "silent": True, "silent_why": "",
                           "steps": [], "tokens": 0, "ms": 0.0, "model": "", "reasoning": "",
                           "error": repr(exc)}
                one.update({"i": len(variants) + 1, "temperature": t, "repeat": k + 1,
                            "provider": pick["provider"] or "", "model_want": pick["model"] or ""})
                variants.append(one)
    out = {
        "ok": True,
        "turn_id": turn_id,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "by": by,
        "addendum": add,
        "scene": rec.get("scene") or "",
        "source_at": rec.get("at") or "",
        "models": [p for p in picks if p.get("model")],
        "variants": variants,
        "tokens_total": sum(v.get("tokens") or 0 for v in variants),
        "n": len(variants),
    }
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, "rehearsal-%s.json" % turn_id)
        atomic_write_json(path, out, indent=2)
    except Exception:  # noqa: BLE001 - 落盘失败不影响返回
        pass
    return out


def last(turn_id: str) -> dict:
    path = os.path.join(OUT_DIR, "rehearsal-%s.json" % turn_id)
    try:
        with open(path, encoding="utf-8") as f:
            return {"ok": True, "data": json.load(f)}
    except FileNotFoundError:
        return {"ok": False, "error": "这一拍还没试过"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": repr(exc)}
