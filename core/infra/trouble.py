"""她「身上哪儿坏了」的台账：一个写口、一个读口、一句话（CB-133）。

为什么要单独一层：
- 语音这类部件的失败原来只有糊在一起的事件名（`cv3_request_failed`），日志里看得出
  「失败了」，看不出「哪个部件、为什么、已经多久了」；
- 而她本人看不到日志。她要「知道自己哪里出了问题」，只能靠进上下文的一段事实。

所以这里只做三件事：
`note(area, reason, detail)` 记一次失败（同一部件同一原因反复失败只累加次数，不刷屏）；
`clear(area)` 好了就删（不删的话她三天后还认为自己坏的）；
`render()` 把「现在的坏」说成一句第一人称的事实，没坏就返回空串。

口吻只在 `_SAY` / `REASONS` 两张表里写一处，别把句子散到调用点去。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from core.infra.jsonio import atomic_write_json, read_json_checked

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "data" / "loop" / "trouble.json"
DETAIL_MAX = 160

# 加新部件：登记一句「我这会儿怎么了」的第一人称说法（没有就退回通用句）。
_SAY = {"voice": "我这会儿发不出语音（打字不受影响，照常能发消息）"}
AREAS = {"voice": "语音合成（CosyVoice）"}

# 失败分类。调用点只许从这里选，不许自己编词——分类一散，日志就又看不懂了。
REASONS = {
    "connect_refused": "连不上服务，引擎大概没起",
    "spawn_failed": "引擎进程起不来",
    "boot_timeout": "引擎起了，但一直不应答",
    "synth_timeout": "合成超时",
    "server_error": "服务报错了",
    "synth_no_file": "合成了但没拿到音频文件",
    "not_installed": "模型文件不全",
    "autostart_off": "语音合成的自动拉起被关掉了（跟发文字无关）",
}


def _blank() -> dict:
    return {"items": {}}


def _load(path=None) -> dict:
    raw, _state = read_json_checked(path or LEDGER, default=None)
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), dict):
        return _blank()
    return raw


def note(area: str, reason: str, detail: Any = "", *, path=None, now=None) -> bool:
    """记一次失败。写不进去只返回 False，不抛——台账坏了不该让合成变成崩溃。"""
    if area not in AREAS:
        raise ValueError("没登记的部件：%r（先加到 trouble.AREAS）" % (area,))
    if reason not in REASONS:
        raise ValueError("没登记的原因：%r（先加到 trouble.REASONS）" % (reason,))
    t = float(time.time() if now is None else now)
    d = _load(path)
    items = d.setdefault("items", {})
    prev = items.get(area) or {}
    same = prev.get("reason") == reason
    items[area] = {
        "reason": reason,
        "detail": str(detail or "")[:DETAIL_MAX],
        "at": t,
        "since": (float(prev.get("since") or t) if same else t),
        "tries": (int(prev.get("tries") or 0) + 1 if same else 1),
    }
    return atomic_write_json(path or LEDGER, d)


def clear(area: str, *, path=None) -> bool:
    """好了就删。不删的话她长时间认为自己还坏着，比坏着更糟。"""
    d = _load(path)
    items = d.get("items") or {}
    if area not in items:
        return True
    items.pop(area, None)
    d["items"] = items
    return atomic_write_json(path or LEDGER, d)


def snapshot(*, path=None) -> dict:
    return dict(_load(path).get("items") or {})


def _age(item: dict, now: float) -> str:
    at = float(item.get("since") or item.get("at") or 0.0)
    if not at:
        return "刚才"
    left = max(0.0, now - at)
    clock = time.strftime("%H:%M", time.localtime(at))
    if left < 90:
        return "%s 起的" % clock
    if left < 3600:
        return "%s 起，%d 分钟了" % (clock, int(left // 60))
    return "%s 起，%d 小时了" % (clock, int(left // 3600))


def render(*, path=None, now=None) -> str:
    """现在的坏 → 一段第一人称事实；没坏返回空串（调用方据此决定整段不插）。"""
    items = snapshot(path=path)
    if not items:
        return ""
    t = float(time.time() if now is None else now)
    lines = ["[我身上的状况]（这是系统告诉我的事实，不是我自己想出来的；要不要说、怎么说，我自己定）"]
    for area in AREAS:
        item = items.get(area)
        if not item:
            continue
        say = _SAY.get(area, "我这会儿%s不太对" % AREAS[area])
        line = "- %s：%s（%s，已经试过 %d 次）" % (
            say, REASONS.get(str(item.get("reason")), "原因不明"),
            _age(item, t), int(item.get("tries") or 1))
        det = str(item.get("detail") or "").strip()
        if det:
            line += "\n  最近一次的报错：%s" % det[:100]
        lines.append(line)
    return "\n".join(lines) if len(lines) > 1 else ""


__all__ = ["AREAS", "REASONS", "LEDGER", "note", "clear", "snapshot", "render"]
