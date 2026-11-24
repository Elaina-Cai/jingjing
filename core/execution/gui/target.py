"""目标（CurrentTarget）：此刻动的到底是哪一个窗口/哪一个会话。

为什么需要它
------------
屏幕上只有「当前」这一个活动对象，而我们的动作是分几秒完成的：切会话 → 聚焦 → 粘贴 →
回车。中途用户可能切走、窗口可能被最小化、另一个任务可能把会话换掉。**动作前后比对
目标**是唯一能在动手第 100 毫秒就发现「我被换了台」的办法——发现不一致就停手，
零副作用，而不是把话发到别人的会话里。

代次（generation）是乐观并发
--------------------------
每成功切换一次 +1。动作开始时记住代次，动手前比对；不一致 → `WindowChanged`。
它不需要真锁，也不防恶意；它只保证「我以为我在 A，其实已经在 B」这件事不会发生。

四条纪律
--------
1. **目标必须由证据决定**：`explicit(1.0) > anchor(0.9) > ocr(0.7) > recent_db(0.3) > none`。
   `recent_db` 只说明「最近有消息」，**不说明**屏幕上是哪个会话。
2. 判定失败一律**零副作用**：`check()` 只读，不做任何补救动作（补救由调用方决定）。
3. 锁屏/输入桌面不可写属于「环境不具备」，抛 `CapabilityMissing`（与租约同一种错），
   不是「目标变了」——两者的处置完全不同。
4. `check()` 不缓存探测结果：每次都必须重新探（一个过期的指纹比没有指纹更糟）。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from core.execution.gui.lease import CapabilityMissing
from safelog import warn

log = logging.getLogger(__name__)

PLATFORM_WECHAT = "wechat"
PLATFORM_CHROMIUM = "chromium"
PLATFORMS = (PLATFORM_WECHAT, PLATFORM_CHROMIUM)

EVIDENCE_CONFIDENCE: dict[str, float] = {
    "explicit": 1.0,
    "anchor": 0.9,
    "ocr": 0.7,
    "ambiguous": 0.5,
    "recent_db": 0.3,
    "none": 0.0,
}

Rect = tuple[int, int, int, int]


class TargetError(RuntimeError):
    """目标相关的错误基类。"""


class WindowChanged(TargetError):
    """窗口/会话指纹与记忆中不一致（或代次被别的任务推进了）。"""

    def __init__(self, reason: str, *, expected: Any = None, seen: Any = None) -> None:
        self.reason = reason
        self.expected = expected
        self.seen = seen
        super().__init__(f"目标变了（{reason}）：预期 {expected!r}，实际 {seen!r}")


class WindowGone(TargetError):
    """窗口不存在了（关掉、最小化到托盘、进程退出）。"""

    def __init__(self, handle: int = 0, reason: str = "") -> None:
        self.handle = int(handle or 0)
        self.reason = reason or "窗口不在了"
        super().__init__(f"{self.reason}（handle={self.handle}）")


@dataclass(frozen=True)
class CurrentTarget:
    """「此刻正在动的那一个」的完整快照。字段与设计文档 §5.1 一一对应。"""

    platform: str = PLATFORM_WECHAT
    handle: int = 0
    pid: int = 0
    exe: str = ""
    title: str = ""
    rect: Rect = (0, 0, 0, 0)
    identity: str = ""
    fingerprint: str = ""
    generation: int = 0
    owner: str = ""
    verb: str = "input"
    since: float = 0.0
    evidence: str = "explicit"
    confidence: float = 1.0

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.platform, int(self.handle), self.identity)

    @property
    def usable(self) -> bool:
        return bool(self.handle) and self.confidence > 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "handle": self.handle,
            "pid": self.pid,
            "exe": self.exe,
            "title": self.title,
            "rect": list(self.rect),
            "identity": self.identity,
            "fingerprint": self.fingerprint,
            "generation": self.generation,
            "owner": self.owner,
            "verb": self.verb,
            "since": self.since,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }


_GENERATIONS: dict[tuple[str, int, str], int] = {}


def reset_generations() -> None:
    """只给测试用：清掉代次计数。"""
    _GENERATIONS.clear()


def _fingerprint(*parts: str) -> str:
    joined = "|".join(p or "" for p in parts)
    return hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()[:16]


def _rect(value: Any) -> Rect:
    try:
        left, top, right, bottom = (int(v) for v in value)
    except Exception:
        warn("core/execution/gui/target.py:138 Exception", err=repr(__import__("sys").exc_info()[1]))
        return (0, 0, 0, 0)
    return (left, top, right, bottom)


# ---------------------------------------------------------------- 探针
def probe_wechat(*, chat: str = "") -> dict[str, Any]:
    """真机探针：微信的当前窗口 + 当前会话身份。

    任何异常都返回空 dict（让调用方按「看不见」处理，而不是炸在动作中间）。
    """
    out: dict[str, Any] = {"platform": PLATFORM_WECHAT}
    try:
        from core.execution import wx_chat, wx_vision

        win = wx_chat._wechat_window()
        if win is None:
            return {}
        out.update(
            {
                "handle": int(getattr(win, "hwnd", 0) or 0),
                "pid": int(getattr(win, "pid", 0) or 0),
                "exe": str(getattr(win, "exe", "") or ""),
                "title": str(getattr(win, "title", "") or ""),
                "rect": _rect(getattr(win, "rect", (0, 0, 0, 0))),
            }
        )
        identity = ""
        try:
            identity = str(wx_chat.active_chat_id() or "")
        except Exception:  # noqa: BLE001 - 身份读不到不算致命
            warn("core/execution/gui/target.py:168 Exception", err=repr(__import__("sys").exc_info()[1]))
            identity = ""
        out["identity"] = identity or str(chat or "")
        out["fingerprint"] = _fingerprint(out["title"], out["identity"], str(out["rect"]))
        out["session_active"] = True
    except Exception as exc:  # noqa: BLE001
        log.warning("target_probe_wechat_failed error=%r", exc)
        return {}
    return out


def probe_chromium(*, target: str = "") -> dict[str, Any]:
    """真机探针：CDP 当前目标页（读屏不需要独占，但需要知道「哪一页」）。"""
    out: dict[str, Any] = {"platform": PLATFORM_CHROMIUM}
    try:
        from core.execution.gui import cdp

        targets = cdp.list_targets() or []
        pick = targets[0] if targets else {}
        if not pick:
            return {}
        out.update(
            {
                "handle": int(pick.get("id_num") or 0),
                "pid": 0,
                "exe": "msedge",
                "title": str(pick.get("title") or ""),
                "rect": (0, 0, 0, 0),
                "identity": str(pick.get("url") or target or ""),
            }
        )
        out["fingerprint"] = _fingerprint(out["title"], out["identity"])
        out["session_active"] = True
    except Exception as exc:  # noqa: BLE001
        log.warning("target_probe_chromium_failed error=%r", exc)
        return {}
    return out


ProbeFn = Callable[..., dict[str, Any]]


def _call_probe(platform: str, probe_fn: ProbeFn | None, *, chat: str = "") -> dict[str, Any]:
    if probe_fn is not None:
        try:
            return probe_fn(chat=chat) or {}
        except TypeError:
            warn("core/execution/gui/target.py:214 TypeError", err=repr(__import__("sys").exc_info()[1]))
            return probe_fn() or {}
    if platform == PLATFORM_CHROMIUM:
        return probe_chromium(target=chat)
    return probe_wechat(chat=chat)


# ---------------------------------------------------------------- 取 / 验 / 推进
def acquire(
    platform: str = PLATFORM_WECHAT,
    *,
    chat: str = "",
    owner: str = "",
    verb: str = "input",
    evidence: str = "explicit",
    probe_fn: ProbeFn | None = None,
) -> CurrentTarget:
    """探一次，登记代次，返回目标快照。看不到窗口抛 `WindowGone`。"""
    if platform not in PLATFORMS:
        raise ValueError(f"未知平台 {platform!r}；只接受 {PLATFORMS}")
    if evidence not in EVIDENCE_CONFIDENCE:
        raise ValueError(f"未知证据档 {evidence!r}")
    seen = _call_probe(platform, probe_fn, chat=chat)
    handle = int(seen.get("handle") or 0)
    if not handle:
        raise WindowGone(0, reason="探针看不到窗口")
    identity = str(seen.get("identity") or chat or "")
    target = CurrentTarget(
        platform=platform,
        handle=handle,
        pid=int(seen.get("pid") or 0),
        exe=str(seen.get("exe") or ""),
        title=str(seen.get("title") or ""),
        rect=_rect(seen.get("rect") or (0, 0, 0, 0)),
        identity=identity,
        fingerprint=str(seen.get("fingerprint") or _fingerprint(identity, str(handle))),
        generation=_GENERATIONS.get((platform, handle, identity), 0) + 1,
        owner=str(owner or ""),
        verb=str(verb or "input"),
        since=time.time(),
        evidence=evidence,
        confidence=EVIDENCE_CONFIDENCE[evidence],
    )
    _GENERATIONS[target.key] = target.generation
    return target


def check(
    target: CurrentTarget,
    *,
    probe_fn: ProbeFn | None = None,
    now: float | None = None,
) -> None:
    """动手前的最后一道判定。只读、零副作用；不一致就抛。

    依次判：窗口还在 → 没锁屏 → 句柄没换 → 身份/指纹没变 → 代次没被别人推进。
    任何一条不过都抛，**不做补救**（补救是调用方的决定）。
    """
    seen = _call_probe(target.platform, probe_fn, chat=target.identity)
    if not seen:
        raise WindowGone(target.handle, reason="探针看不到窗口（关掉/最小化到托盘/进程退出）")
    if seen.get("session_active") is False:
        raise CapabilityMissing("session_locked")

    handle = int(seen.get("handle") or 0)
    if not handle:
        raise WindowGone(target.handle, reason="句柄为空")
    if handle != target.handle:
        raise WindowChanged("handle", expected=target.handle, seen=handle)

    identity = str(seen.get("identity") or "")
    if target.identity and identity and identity != target.identity:
        raise WindowChanged("identity", expected=target.identity, seen=identity)

    fingerprint = str(seen.get("fingerprint") or "")
    if target.fingerprint and fingerprint and fingerprint != target.fingerprint:
        raise WindowChanged("fingerprint", expected=target.fingerprint, seen=fingerprint)

    current = _GENERATIONS.get(target.key, 0)
    if target.generation and current != target.generation:
        raise WindowChanged("generation", expected=target.generation, seen=current)


def advance(
    target: CurrentTarget,
    *,
    identity: str | None = None,
    fingerprint: str | None = None,
    owner: str | None = None,
) -> CurrentTarget:
    """确认一次「成功切换」：代次 +1，返回新目标（旧目标从此作废）。"""
    new_identity = target.identity if identity is None else str(identity)
    new_fingerprint = (
        target.fingerprint if fingerprint is None else str(fingerprint)
    ) or _fingerprint(new_identity, str(target.handle))
    updated = replace(
        target,
        identity=new_identity,
        fingerprint=new_fingerprint,
        generation=target.generation + 1,
        owner=target.owner if owner is None else str(owner),
        since=time.time(),
    )
    _GENERATIONS[updated.key] = updated.generation
    if updated.key != target.key:
        _GENERATIONS.setdefault(target.key, target.generation)
    return updated


def require_confidence(target: CurrentTarget, minimum: float = 0.9) -> None:
    """不可逆动作要求高置信度证据。"""
    if target.confidence + 1e-9 < float(minimum):
        raise CapabilityMissing(
            f"target_evidence_weak:{target.evidence}<{minimum:g}"
        )
