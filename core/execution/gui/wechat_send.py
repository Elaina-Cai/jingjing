"""微信 GUI 单次发送与明确账号的本地观察。

本地同会话、逐字同文的唯一新增自发记录只表明本地记录出现，
不代表平台送达或已读；缺账号不读取默认快照，不因缺回执重发。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from core import features
from core.execution.gui.base import GuiError
from safelog import warn
from core.infra.envflags import text as _text

log = logging.getLogger(__name__)

STATUS_SENT = "sent"
STATUS_UNCONFIRMED = "unconfirmed"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
SEND_STATUSES = (STATUS_SENT, STATUS_UNCONFIRMED, STATUS_FAILED, STATUS_BLOCKED)

#: 回读节奏：8 次 × 0.4 s ≈ 3.2 s。本地落库通常 < 1 s，余量留给「网络慢但真发出去了」。
POLL_INTERVAL_S = 0.15
POLLS = 10
ROW_LIMIT = 20
DETAIL_MAX_CHARS = 200

#: 2026-10-27：structlog 的 wx_* 日志落不进 logs/yinyue.log，出问题只能靠猜。
#: 这里把每个返回点直接写纯文本，一行一个 json，方便事后统计分布。
_PROBE_LOG = Path(__file__).resolve().parents[3] / "data" / "loop" / "_send_probe.log"


def _probe(event: str, **kw: Any) -> None:
    """记录一次发送判定（永不抛异常，永不阻塞）。"""
    try:
        _PROBE_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": _stamp(), "event": event}
        row.update(kw)
        with _PROBE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 探针不许影响发送
        pass


def _mode() -> str:
    return features.mode(features.WECHAT_SEND, features.MODE_OFF)


def normalize_text(text: str) -> str:
    """只抹空白——不做同义改写。对账比的是我们填进去的那句原文。"""
    return " ".join(str(text or "").split())


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:16]


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(sep=" ", timespec="seconds")


@dataclass
class SendReceipt:
    """一次发送的账：成没成、凭什么说成了。"""

    status: str
    detail: str = ""
    chat: str = ""
    text_sha256: str = ""
    local_id: int = 0
    server_id: int = 0
    baseline: int = 0
    polls: int = 0
    at: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def is_delivered(self) -> bool:
        return self.status in (STATUS_SENT, STATUS_UNCONFIRMED)

    def as_receipt(self) -> dict[str, Any]:
        ack = "error"
        if self.status == STATUS_SENT and self.server_id:
            ack = "platform_id"
        elif self.status == STATUS_UNCONFIRMED:
            ack = "unconfirmed"
        elif self.status == STATUS_BLOCKED:
            ack = "none"
        return {
            "status": self.status,
            "kind": "text",
            "target": self.chat,
            "ack": ack,
            "reason": "" if self.status == STATUS_SENT else self.status,
            "platform_msg_id": str(self.server_id or ""),
            "client_msg_id": f"local:{self.local_id}" if self.local_id else "",
            "adapter": "wechat",
            "detail": (self.detail or "")[:DETAIL_MAX_CHARS],
            "ref": f"wechat#{self.local_id}" if self.local_id else "wechat#",
            "at": self.at,
        }


def _pick_row(rows: list[dict[str, Any]], fingerprint: str) -> dict[str, Any] | None:
    """新增行里找「我发的 + 内容就是我们那句」的最早一条。"""
    for row in rows:
        if not row.get("mine"):
            continue
        if text_fingerprint(str(row.get("text") or "")) == fingerprint:
            return row
    return None


def _record(store: Any, receipt: SendReceipt, *, turn_id: str) -> None:
    """落回执表；记账失败绝不影响回执（旁注不许阻断）。"""
    if store is None or not hasattr(store, "record_receipt"):
        return
    try:
        store.record_receipt(receipt.as_receipt(), turn_id=turn_id, chat_id=receipt.chat)
    except Exception as exc:
        log.warning("wechat_gui_receipt_record_failed", extra={"err": repr(exc)[:160]})
        return


def send_and_verify(
    chat: str,
    text: str,
    *,
    mention: str = "",
    db_path: str | Path | None = None,
    platform: str = "wechat",
    account_id: str = "",
    deps: Any = None,
    store: Any = None,
    turn_id: str = "",
    poll_interval_s: float = POLL_INTERVAL_S,
    polls: int = POLLS,
    sleep: Callable[[float], None] | None = None,
    refresh: Callable[[], Any] | None = None,
    now: datetime | None = None,
) -> SendReceipt:
    """填字 → 回车 → 回读对账。返回回执，**永不重发**。"""
    from core.execution.gui import wechat

    body = str(text or "").strip()
    fingerprint = text_fingerprint(body)
    stamp = _stamp(now)
    mode = _mode()

    def _blocked(detail: str) -> SendReceipt:
        _probe("blocked", chat=str(chat or ""), detail=detail, mode=mode)
        return SendReceipt(
            status=STATUS_BLOCKED, detail=detail, chat=str(chat or ""),
            text_sha256=fingerprint, at=stamp,
        )

    if mode == features.MODE_OFF:
        return _blocked("WECHAT_SEND=off：发送闸没开")
    if mode != features.MODE_ON:
        return _blocked(f"WECHAT_SEND={mode}：不可逆动作不进影子档，只做读侧判定")
    if not str(chat or "").strip():
        return _blocked("no_target：没给会话")
    if not body:
        return _blocked("unsupported：空文本不发送")

    from core.execution import local_text_receipt as local
    from db import world_db
    path = local.account_path(world_db.ROOT, platform, account_id) if account_id else None
    if platform != "wechat" or (account_id and path is None):
        return _blocked("observation_source_unavailable：指定账号来源无效或不可读")
    if db_path is not None and (path is None or Path(db_path).resolve() != path.resolve()):
        return _blocked("observation_source_mismatch：显式数据库与账号来源不符")
    baseline = 0
    observation_ready = False
    source_token = ""
    if path is not None:
        try:
            baseline, source_token = local.capture(path)
            observation_ready = baseline is not None
        except Exception as exc:
            warn("gui_observation_baseline_failed", chat=str(chat), account_id=account_id, error=repr(exc)[:160])
    if not observation_ready:
        warn("gui_observation_source_unavailable", chat=str(chat), platform=platform, account_id=account_id)

    impl = deps or wechat._default_deps()
    # 出口传进来的是地址（chat_id），微信搜索框只认名字。
    shown = wechat.display_name_for(chat)
    filled: Any = None
    pressed = False
    press_error = ""
    key_attempted = False
    t_fill = 0.0
    t_filled = 0.0
    t_enter = 0.0
    # 「填字 + 回车」是**一段**连续动作：手必须一直是我们的。中间放锁的话，别人
    # 能在这两百毫秒里切走会话，回车就把我们的话发给了别人。
    try:
        with wechat.device_guard(turn_id or "wechat"):
            try:
                t_fill = time.time()
                filled = wechat.fill_message(shown, body, mention=mention, deps=impl)
                t_filled = time.time()
            except GuiError as exc:
                warn("core/execution/gui/wechat_send.py:188 GuiError", err=repr(exc))
                return SendReceipt(
                    status=STATUS_FAILED,
                    detail=f"fill_failed {exc.code}：{exc.detail}"[:DETAIL_MAX_CHARS],
                    chat=str(chat), text_sha256=fingerprint, baseline=baseline, at=stamp,
                )

            if getattr(filled, "filled_verified", None) is False:
                _probe("fill_unverified", chat=str(chat), filled=repr(getattr(filled, "filled_verified", None)))
                # 输入框里读不到我们写的字 = 字没进去（或进错了地方），
                # 这时候按回车只会把「当时输入框里原本的东西」发出去。
                receipt = SendReceipt(
                    status=STATUS_FAILED,
                    detail="fill_unverified：输入框读不到我们写的字，不按回车"[:DETAIL_MAX_CHARS],
                    chat=str(chat), text_sha256=fingerprint, baseline=baseline,
                    evidence={"fill": getattr(filled, "text_sha256", "")}, at=stamp,
                )
                _record(store, receipt, turn_id=turn_id)
                return receipt

            try:
                key_attempted = True
                pressed = bool(impl.press_enter())
                t_enter = time.time()
            except Exception as exc:  # pragma: no cover - 驱动异常
                warn("core/execution/gui/wechat_send.py:218 Exception", err=repr(exc))
                pressed = False
                press_error = f"{type(exc).__name__}: {exc}"
            finally:
                # 真机 2026-10-21：往群里发完之后，回私聊那条被判「人还在群里」——
                # 复用记忆说的是上一个会话。发送一定改了会话状态，就地作废记忆，
                # 下一次发送必须重新搜索 + 顶栏验收（宁可慢 1.5 秒，也不发错人）。
                try:
                    from core.execution import wx_chat as _wx_chat

                    _wx_chat.invalidate_active_chat("after_send")
                except Exception:  # noqa: BLE001 - 作废失败不许影响发送
                    warn("core/execution/gui/wechat_send.py:216 Exception", err=repr(__import__("sys").exc_info()[1]))

    except (wechat.device_lease_mod.UiBusy,
            wechat.capability_probe.CapabilityMissing) as exc:
        warn("gui_device_guard_failed", chat=str(chat), key_attempted=key_attempted, error=repr(exc))
        if not key_attempted:
            return _blocked(f"blocked：{type(exc).__name__}: {exc}"[:DETAIL_MAX_CHARS])
        pressed = False
        press_error = f"guard_exit_failed: {type(exc).__name__}: {exc}"
    if not pressed:
        _probe("press_result_unknown", chat=str(chat), detail=press_error)
        receipt = SendReceipt(
            status=STATUS_UNCONFIRMED,
            detail=f"press_result_unknown 已调用发送按键，执行结果未知；不重发 {press_error}".strip()[:DETAIL_MAX_CHARS],
            chat=str(chat), text_sha256=fingerprint, baseline=baseline,
            evidence={"fill": getattr(filled, "text_sha256", ""),
                      "execution": "unknown", "platform": platform, "account_id": account_id}, at=stamp,
        )
        _record(store, receipt, turn_id=turn_id)
        return receipt

    wait = sleep or time.sleep
    tries = 0
    local_id = ""
    if observation_ready:
        for tries in range(1, max(1, int(polls)) + 1):
            try:
                local_id = local.observe(path, chat, body, baseline, source_token=source_token)
            except Exception as exc:
                warn("gui_observation_read_failed", chat=str(chat), account_id=account_id, error=repr(exc)[:160])
                break
            if local_id:
                break
            if tries < polls:
                wait(poll_interval_s)
    evidence = {"fill": getattr(filled, "text_sha256", ""), "platform": platform,
                "account_id": account_id, "evidence_kind": "local_outgoing_record" if local_id else "unknown"}
    receipt = SendReceipt(
        status=STATUS_UNCONFIRMED,
        detail=("本地记录中已出现这条消息；不代表平台送达或已读" if local_id else
                "已尝试发送，未取得可核对的本地记录；不重发"),
        chat=str(chat), text_sha256=fingerprint, local_id=int(local_id or 0),
        baseline=baseline, polls=tries, evidence=evidence, at=stamp,
    )
    _record(store, receipt, turn_id=turn_id)
    return receipt


# ---------------------------------------------------------------------------
# 2026-10-28：GUI 直发也要进世界账本（world_db.outbound_receipt）
# 背景：引擎走 ports.say 会记 outbound_receipt，但直接调 send_and_verify 的
#      路径（发图、补发、测试）以前一行账都不留，导致"这条到底是谁发的"判不出来。
# ---------------------------------------------------------------------------
_orig_send_and_verify = send_and_verify


def _gui_self_proof(text, wait_s=3.0, exclude=None):
    """兼容旧调用；无账号和发送操作绑定的同文不构成凭据。"""
    return ""


def _gui_self_proof_chat(chat: str, text: str, *, wait_s: float = 2.0) -> str:
    """会话名不能代替账号及发送操作绑定。"""
    return ""


def _gui_confirm_later(chat, text, *args, **kwargs):
    """无操作绑定不启动扫描或后台确认；可靠观察由 desk 绑定链处理。"""
    warn("gui_confirmation_binding_missing", chat=str(chat or ""))
    return {"ok": False, "reason": "operation_binding_missing"}


def send_and_verify(*args, **kwargs):
    _r = _orig_send_and_verify(*args, **kwargs)
    try:
        from db import world_db as _w
        _chat = str(kwargs.get("chat") or (args[0] if args else "") or "")
        _text = str(kwargs.get("text") or (args[1] if len(args) > 1 else "") or "")
        _ok = getattr(_r, "ok", None)
        if _ok is None:
            _ok = str(getattr(_r, "status", "") or "") in ("sent", "ok", "confirmed", "unconfirmed")
        _ack = str(getattr(_r, "ack", "") or "")
        if _chat and _text and _ok:
            _w.record_outbound(_chat, _text, platform="wechat", turn_id="",
                               method="wechat_gui", ack=_ack or "unconfirmed", account_id=str(kwargs.get("account_id") or ""))
    except Exception as exc:
        warn("wechat_gui_outbound_record_failed", err=repr(exc)[:160])
    return _r


