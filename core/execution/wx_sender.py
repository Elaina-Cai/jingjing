from __future__ import annotations

import os
import time

import structlog
from datetime import datetime
from threading import Lock, Thread

from core.infra import skips
from core.execution import wx_chat
from safelog import warn
from core.infra.envflags import text as _text

log = structlog.get_logger()

# Contact search and conversation opening live in wx_chat, which both this
# module and wx_sticker_sender depend on.  These are kept as module-level names
# because core/execution/__init__ re-exports this module.
set_account_id = wx_chat.set_account_id
resolve_nickname = wx_chat.resolve_nickname
ensure_foreground = wx_chat.ensure_foreground
# 打字只有 wx_chat.type_text 一个实现（SEC-006）；这里只是模块内短名。
type_text = wx_chat.type_text
press_enter = wx_chat.press_enter
press_ctrl_enter = wx_chat.press_ctrl_enter
press_escape = wx_chat.press_escape
get_ocr = wx_chat.get_ocr

# Backwards-compatible private aliases for callers written before the split.
_wechat_window = wx_chat._wechat_window
set_sender_account_id = wx_chat.set_account_id
_resolve_nickname = wx_chat.resolve_nickname
_ensure_wechat_visible = wx_chat.ensure_foreground
_press_enter = wx_chat.press_enter
_press_escape = wx_chat.press_escape
_get_ocr = wx_chat.get_ocr


# ---------------------------------------------------------------------------
# 发送回执（`docs/decisions/出口.md` §5.3）
#
# 旧 who/msg/baseline 接口缺账号及发送操作绑定，不能从历史同文生成回执。
# 现役 desk 的可靠本地观察由 local_text_receipt 与 world_db 的绑定提交负责。
# ---------------------------------------------------------------------------

_ACK_UNCONFIRMED = "unconfirmed"
_ACK_PLATFORM = "platform_id"
_ACK_PENDING = "pending"

#: 后台补账：最多盯 30 秒（微信落盘实测 26 秒），单条 1.5 秒一次。
BACKFILL_POLLS = 20
BACKFILL_INTERVAL_S = 1.5
#: 同时最多几个补账线程（她一分钟也就发几条，超过这个数说明出事了）。
BACKFILL_MAX_THREADS = 4


def _verify_title(display: str, who: str, *, tries: int = 3, pause: float = 0.4,
                  verify=None, reactivate=None) -> tuple[bool, str]:
    """连续读顶部标题。

    读不到（返回空串）时，先把窗口重新拿到前台再试下一次；读到但对不上立刻返回。
    后者的 fail-closed 是 2026-10-28「发进别人会话」之后的硬规则，不能放宽。
    """
    verify = verify or wx_chat.verify_active_chat_title
    for i in range(max(1, int(tries))):
        try:
            ok, seen = verify(display, raw_who=who)
        except Exception as exc:  # noqa: BLE001
            ok, seen = False, ""
            warn("wx_sender.py:verify_title_failed", err=repr(exc))
        if ok or seen:
            return bool(ok), seen
        if i + 1 < int(tries):
            if reactivate is not None:
                try:
                    reactivate()
                except Exception:  # noqa: BLE001 - 前台拉不起来就继续下一轮
                    from core.infra.warn_guard import once as _once
                    if _once("wx_sender_title_reactivate_failed"):
                        warn("wx_sender_title_reactivate_failed",
                             err=repr(__import__("sys").exc_info()[1]))
            time.sleep(pause)
    return False, ""


def ack_baseline(who: str) -> int | None:
    """旧接口没有账号/发送操作绑定；不从默认快照猜测游标。"""
    return None


def _lookup_ack(who: str, msg: str, baseline: int) -> dict | None:
    """兼容旧调用；裸游标不足以绑定账号和发送操作，不生成回执。"""
    log.info("wx_send_ack_binding_missing", who=who)
    return None


def ack_after_send(who: str, msg: str, baseline: int | None, *, polls: int = 1,
                   interval_s: float = BACKFILL_INTERVAL_S) -> dict:
    """回读对账，返回 `{ack, local_id, server_id}`。

    ``polls=1`` = 立刻看一次（默认，**不阻塞等待**）；要多等就传大一点。
    """
    if baseline is None:
        return {"ack": _ACK_UNCONFIRMED}
    rounds = max(1, int(polls))
    try:
        for i in range(rounds):
            got = _lookup_ack(who, msg, baseline)
            if got is not None:
                return got
            if i + 1 < rounds:
                time.sleep(max(0.1, float(interval_s)))
    except Exception:  # noqa: BLE001 - 读库失败只降级，不许改判「没发出去」
        log.warning("wx_send_ack_readback_failed", who=who)
    return {"ack": _ACK_UNCONFIRMED}


_backfill_lock = Lock()
_backfill_running = {"n": 0}


def _backfill_ack(who: str, msg: str, baseline: int) -> dict:
    """后台盯 30 秒，拿到平台消息号就把那行旁注补成真回执。"""
    got = {"ack": _ACK_UNCONFIRMED}
    try:
        for i in range(BACKFILL_POLLS):
            try:
                found = _lookup_ack(who, msg, baseline)
            except Exception as exc:  # noqa: BLE001
                # 2026-10-26：原来这里把异常整个吞掉，只打一句 backfill_failed，
                # 于是"没找到回执"和"查询崩了"在日志/账面长得一模一样。
                # 现在记下原因并退避重试（最常见的真因是 account.db 被别的线程写着 → database is locked）。
                log.warning("wx_send_ack_lookup_error", who=who, attempt=i + 1,
                            error=str(exc)[:200])
                time.sleep(min(1.0 * (i + 1), 5.0))
                continue
            if found is not None:
                got = found
                break
            time.sleep(BACKFILL_INTERVAL_S)
    except Exception as exc:  # noqa: BLE001
        log.warning("wx_send_ack_backfill_failed", who=who, error=str(exc)[:200])
    try:
        from core.terminal import delivery_receipts as _dr

        changed = _dr.update_pending_ack(
            who, ack=got.get("ack", _ACK_UNCONFIRMED),
            platform_msg_id=got.get("server_id", ""),
            client_msg_id=got.get("local_id", ""),
        )
        log.info(
            f"wx_send_ack_backfilled who={who} ack={got.get('ack')} "
            f"local_id={got.get('local_id', '')} server_id={got.get('server_id', '')} "
            f"receipts_updated={changed}"
        )
    except Exception:  # noqa: BLE001
        log.warning("wx_send_ack_backfill_write_failed", who=who)
    finally:
        with _backfill_lock:
            _backfill_running["n"] -= 1
    return got


def spawn_ack_backfill(who: str, msg: str, baseline: int | None) -> bool:
    """起一个后台补账线程；返回是否真起了（baseline 缺失/线程已满 → False）。"""
    if baseline is None:
        return False
    with _backfill_lock:
        if _backfill_running["n"] >= BACKFILL_MAX_THREADS:
            log.info("wx_send_ack_backfill_skipped reason=too_many who=%s", who)
            return False
        _backfill_running["n"] += 1
    try:
        t = Thread(target=_backfill_ack, args=(who, msg, baseline), daemon=True,
                   name="wx-ack-backfill")
        t.start()
        return True
    except Exception:  # noqa: BLE001
        with _backfill_lock:
            _backfill_running["n"] -= 1
        log.warning("wx_send_ack_backfill_thread_failed", who=who)
        return False


def _probe_reuse(display_name: str, who: str) -> tuple:
    """现在开着的会话能不能直接复用（省掉"搜索→回车→点输入框"）。

    返回 ``(reused, source)``：``source`` 只是日志用的 —— ``fingerprint`` 表示
    用的是**已验证过一次、且这一帧像素指纹仍然一致**的记忆（约 0.3 秒）；
    ``ocr`` 表示退回到读一次顶部标题（约 1.2–2.6 秒）；``ocr_miss`` 表示读到了但
    不是目标会话，调用方必须走完整路径。

    为什么指纹能当证据：``try_reuse_chat(allow_unverified=False)`` 只认
    **曾经 tag 成 title_verified** 的那条记忆，并且要过 identity / hwnd / pid /
    前台 / 窗口尺寸 / 标题区矩形 / 指纹七道；任何一道不成立它就自己返回 None，
    这里立刻退回 OCR。也就是说指纹只会让"本来就对"的情况更快，不会让"可能错"的
    情况少验一次 —— 2026-10-28 发错人的事故，撞的是"没验就打字"，不是"验过又验"。
    """
    try:
        bundle = wx_chat.try_reuse_chat(display_name, raw_who=who, allow_unverified=False)
    except Exception as exc:  # noqa: BLE001 - 复用判定失败就退回正常路径
        warn("wx_sender_probe_reuse_failed", err=repr(exc))
        bundle = None
    if bundle is not None:
        return True, "fingerprint"
    try:
        verified, _fatal = wx_chat.verify_active_chat_title(display_name, raw_who=who)
    except Exception as exc:  # noqa: BLE001 - 读不到标题不算致命，走正常路径再验一遍
        warn("wx_sender_reuse_probe_failed", err=repr(exc))
        return False, "ocr_error"
    return (bool(verified), "ocr" if verified else "ocr_miss")


def account_send_options(account_id: str, *, platform: str = "wechat", meta_repo=None) -> dict:
    """只读指定账号配置；不改共享发送器状态，不从其他账号兜底。"""
    if not account_id:
        return {}
    if meta_repo is None:
        from core.terminal.store_port import get_meta_repo
        meta_repo = get_meta_repo()
    row = meta_repo.account_registry.get(account_id)
    if row is None or str(row.platform) != platform or str(row.account_id) != account_id:
        raise ValueError("send_account_config_mismatch")
    import json
    config = json.loads(row.config_json or "{}")
    if not isinstance(config, dict):
        raise ValueError("send_account_config_invalid")
    if "send_key" not in config:
        return {}
    key = config["send_key"]
    if key not in ("enter", "ctrl_enter"):
        raise ValueError("send_key must be enter or ctrl_enter")
    return {"send_key": key}


class WxSender:
    _instance: WxSender | None = None
    _lock = Lock()

    def __init__(self, min_interval: float = 0.4, ocr_verify: bool = True, *, send_key: str = "enter"):
        if send_key not in ("enter", "ctrl_enter"):
            raise ValueError("send_key must be enter or ctrl_enter")
        self._send_key = send_key
        # 2026-10-25：2.0 → 0.4。这是"两条消息之间至少隔这么久"的防连发闸，
        # 而 say_many() 已经在气泡之间 sleep 1.2~2.4s 做人味间隔——两个闸叠加时
        # 发 3 条气泡光等待就要 8~9 秒。0.4 只防"同一瞬间连击"，人味交给 say_many。
        self._min_interval = min_interval
        self._last_send_at: float = 0.0
        self._send_lock = Lock()
        self._connected = False
        self._ocr_verify = ocr_verify

    @classmethod
    def get_instance(cls, **kwargs) -> WxSender:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(**kwargs)
        return cls._instance

    def _uipi_hint(self) -> str:
        """Explain the failure when WeChat outranks us in integrity.

        Windows UIPI silently discards synthesized input aimed at a
        higher-integrity window, so an unelevated 鲸鲸 can neither activate
        nor type into an elevated WeChat.  Without this hint the failure
        reads as a mysterious "text never reached the input box".
        """
        try:
            from core.execution import wx_vision

            win = _wechat_window()
            if win is not None and wx_vision.process_is_higher_integrity(win.pid):
                return ("（检测到微信以管理员权限运行，鲸鲸没有权限向它发送输入，"
                        "点击和按键都会被系统丢弃：请以管理员身份运行鲸鲸，"
                        "或让微信改用普通权限启动）")
        except Exception:
            warn("wx_sender_uipi_probe_failed", err=repr(__import__("sys").exc_info()[1]))
        return ""

    def connect(self) -> bool:
        win = _wechat_window(force=True)
        self._connected = win is not None
        if self._connected:
            log.info("wx_sender_connected", backend="keyboard_ocr",
                     hwnd=win.hwnd, exe=win.exe, size=(win.width, win.height))
            if self._uipi_hint():
                log.warning("wx_sender_uipi_blocked", who="",
                            hint="微信以管理员权限运行，鲸鲸无权向其注入输入；"
                                 "请以管理员身份运行鲸鲸，或让微信以普通权限运行")
        else:
            log.warning("wx_sender_connect_failed", error="微信窗口未找到", backend="keyboard_ocr")
        return self._connected

    @property
    def is_connected(self) -> bool:
        if not self._connected:
            return False
        try:
            return _wechat_window() is not None
        except Exception:
            warn("wx_sender_window_recheck_failed", err=repr(__import__("sys").exc_info()[1]))
            return False

    def _search_and_enter_chat(self, who: str, raw_who: str = "") -> dict:
        """Open a conversation; see wx_chat.open_chat."""
        result = wx_chat.open_chat(who, raw_who=raw_who, ocr_verify=self._ocr_verify)
        import os as _os  # 本模块没有顶层 import os，这里局部引入，避免 NameError
        _fast = str(_text("YINYUE_SEND_FAST", "1")).lower() not in ("0", "false", "off", "")
        if (result.get("ok") and self._ocr_verify and not _fast
                and not result.get("reused") and wx_chat.text_verify_on_open()):
            # One title OCR here upgrades the memory to title-verified, which
            # lets every later text/sticker send reuse the open conversation.
            # The first read can land during the open animation; one retry
            # keeps a transient unreadable header from costing a fresh contact
            # search on the next send.
            _verified, fatal = wx_chat.verify_active_chat_title(who, raw_who=raw_who)
            if fatal:
                return {"ok": False, "error": fatal, "not_executed": True}
            if not _verified:
                time.sleep(0.4)
                _verified, fatal = wx_chat.verify_active_chat_title(who, raw_who=raw_who)
                if fatal:
                    return {"ok": False, "error": fatal, "not_executed": True}
        return result

    def _pre_send_guard(self, display_name: str, who: str):
        """Abort before Enter when the open chat no longer matches the memory.

        guard_active_chat returns None when there is no memory to enforce; a
        False means the chat changed, the window geometry changed, or WeChat
        lost the foreground.  The send is aborted without touching the input
        field.
        """
        guard = wx_chat.guard_active_chat(display_name, raw_who=who)
        if guard is not False:
            return None
        log.warning("wx_send_chat_changed_aborted", who=who)
        return {"ok": False, "error": "发送前检测到聊天窗口已变化，已中止发送", "not_executed": True}

    @wx_chat.ui_locked
    def send_text(self, who: str, msg: str, *, send_key: str | None = None) -> dict:
        selected_key = self._send_key if send_key is None else send_key
        if selected_key not in ("enter", "ctrl_enter"):
            return {"ok": False, "error": "invalid send_key", "not_executed": True}
        from core import features as _features

        if _features.dry_run_send(str(who or "")):
            log.info("dry_run_send", platform="wechat", who=who, kind="text", chars=len(str(msg or "")))
            return {"ok": True, "dry_run": True}
        if not self.is_connected:
            if not self.connect():
                return {"ok": False, "error": "微信未连接，请确认微信PC客户端已打开并登录", "not_executed": True}

        display_name = resolve_nickname(who)
        _ph0 = time.time()
        _ph = []

        def _mk(_tag: str) -> None:
            _ph.append((_tag, int((time.time() - _ph0) * 1000)))

        with self._send_lock:
            elapsed = time.time() - self._last_send_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)

            try:
                hwnd = ensure_foreground()
                if not hwnd:
                    return {"ok": False, "error": "无法激活微信窗口" + self._uipi_hint(), "not_executed": True}
                _mk("foreground")

                # 快路径：当前会话已经就是目标，跳过"搜索→回车→点输入框"三件事。
                # 这三件事加起来 2~4 秒，而每次发送都要付一遍；命中时只花一次截图+OCR。
                # 复用判定：直接读"当前打开的会话标题"，不依赖 guard 的记忆。
                # guard_active_chat 是三态（None=没记忆/True=对得上/False=窗口变了），
                # 2026-10-24 我把它当布尔用，于是永远退回"搜索→回车→点输入框"。
                _t_enter = time.time()
                reused, _reuse_src = _probe_reuse(display_name, who)
                log.info("wx_send_reuse_probe", who=str(who), reused=bool(reused),
                         source=_reuse_src)
                _mk("reuse_probe")
                if reused:
                    log.info("wx_send_reused_chat", who=who)
                else:
                    search_result = self._search_and_enter_chat(display_name, raw_who=who)
                    # 2026-10-26：群里来的新消息会把微信弹回别的会话，于是「打开的聊天
                    # 与目标不符」。这里只重试"进对会话"这件事：先 Esc 清掉搜索浮层，
                    # 隔一会儿再搜一次，最多 3 次；仍不对就中止 —— 绝不发错人。
                    _enter_tries = 1
                    while not search_result["ok"] and _enter_tries < 3:
                        log.warning("wx_enter_chat_retry", who=who, attempt=_enter_tries)
                        try:
                            press_escape()
                        except Exception:
                            pass
                        time.sleep(0.35)
                        search_result = self._search_and_enter_chat(display_name, raw_who=who)
                        _enter_tries += 1
                    if not search_result["ok"]:
                        return search_result

                    # 打开会话不保证键盘焦点在输入框（搜索方式进入时焦点可能
                    # 还留在搜索浮层/会话列表上），粘贴前先点一下输入框。
                    if not wx_chat.focus_message_box():
                        log.warning("wx_send_input_focus_failed", who=who)

                # 2026-10-28 事故：send_text("文件传输助手") 实际发进了当时打开的P5。
                # 说明'进对会话'这一步可能静默失败，随后就照着当前焦点打字了。
                # 硬闸：打字之前必须读到顶部标题 == 目标（或它的别名），否则中止，不打字。
                _mk("switch_chat")
                # 2026-10-31 提速：reused=True 表示上面那次探测已经用 OCR 验过"顶部标题就是目标会话"
                # （同一个 window frame），再验一遍是重复付费（实测 1.2–7 秒）。
                _ok_title, _seen = (bool(reused), "")
                if not _ok_title:
                    # 2026-11-22：读不到标题（_seen 为空）时先把窗口重新拿到前台再试。
                    # 16:06 那次就是窗口被别的窗口盖着，三次都读到空标题，一条已经写好的
                    # 提醒被中止。读到标题但对不上仍然立刻中止，fail-closed 不变。
                    _ok_title, _seen = _verify_title(
                        display_name, who, reactivate=ensure_foreground)
                if not _ok_title:
                    log.warning("wx_send_wrong_chat_abort", who=str(who), seen=str(_seen))
                    try:
                        wx_chat.invalidate_active_chat("wrong_chat")
                    except Exception:
                        pass
                    return {"ok": False,
                            "not_executed": True,
                            "error": "打开的会话不是目标（顶部是「%s」），已中止，没打字" % _seen}
                _mk("title_verify")
                # 2026-10-28 五修（结构性）：发送前先看框里有没有存量字。
                # 有就停手报出去——存量字可能是上次没发出去的，也可能是主人自己打了一半的。
                try:
                    # 2026-10-31 提速：先用像素缓存判空（0.18s），比不出来才 OCR（2.4s）。
                    # 三态：True=空 / False=有字 / None=测不准（测不准就走老口径，别当成空）。
                    _empty = wx_chat.box_looks_empty()
                    _dirty = None if _empty is None else (not _empty)
                except Exception as exc:  # noqa: BLE001
                    _dirty = None
                    warn("wx_sender_dirty_check_failed", err=repr(exc))
                if _dirty:
                    log.warning("wx_send_box_not_empty", who=str(who))
                    return {"ok": False,
                            "not_executed": True,
                            "error": "输入框里已经有字（可能是上次没发出去的），"
                                     "我没有动它、也没按回车。请人工看一眼。"}
                _mk("dirty_check")
                _t_chat = time.time()
                _box_before = wx_chat.message_box_snapshot()
                _mk("box_before")
                pasted = type_text(msg)
                _mk("paste")
                time.sleep(0.2)
                # 2026-10-25 提速：粘贴自己报成功，就不再截屏+OCR 读一遍（省约 0.5s/条）。
                # 只有粘贴失败/说不清时才做 OCR 复核；发完之后仍有一次"输入框是否清空"校验兜底。
                # 2026-10-28 事故「耶什么」：type_text() 的返回值只代表剪贴板粘贴动作没抛异常，
                # 不代表字真的进了输入框（焦点不在输入框时粘贴会静默落空）。
                # 所以不论 pasted 是什么，都必须读一遍输入框再决定要不要回车。
                # 2026-10-28 四修：像素差分当主判据（字形是真的，OCR 会瞎：1~3 个字的
                # 短句经常整条读不出来）。OCR 只做兜底 —— 框里本来就有字、粘贴被吞时，
                # 差分看不出变化，这时才需要问 OCR 一句。
                wx_chat.invalidate_view()  # 粘贴后必须重新截一帧，不能拿粘贴前的帧比对
                _snap_pasted = wx_chat.message_box_snapshot()
                _ink = wx_chat.message_box_changed(_box_before, _snap_pasted)
                if _ink is True:
                    landed = True
                    log.info("wx_send_text_diff_confirmed", who=str(who))
                else:
                    landed = wx_chat.message_box_has_text()
                    log.info("wx_send_text_ocr_fallback", who=str(who), ink=_ink, ocr=landed)
                if not landed:
                    log.warning("wx_send_text_unverified", who=str(who), pasted=bool(pasted), ink=_ink)
                _t_typed = time.time()
                _mk("text_verify")
                if landed is False:
                    # 2026-10-28 事故「哦」：1~3 个字的短句经常读不出来，单次 OCR
                    # 说"框是空的"并不可信；复读一次再下结论（只读，不碰输入框）。
                    time.sleep(0.35)
                    try:
                        if wx_chat.message_box_has_text():
                            landed = True
                            log.info("wx_send_text_second_read", who=str(who))
                    except Exception as exc:  # noqa: BLE001
                        warn("wx_sender.py:second_read_failed", err=repr(exc))
                if landed is False:
                    # 2026-10-28 五修（结构性）：这里原来是
                    # 「focus → 清空输入框 → 重贴 → 再清空」。破坏性动作一律删掉：
                    # 粘贴过就算数，读不到就判失败、把状态报出去。
                    # 理由：只要发送路径还有「清空」能力，任何一次误判都会吃掉框里的字
                    # （1~2 个字的短句正是 OCR 最容易误判的），换个判据只能降低误判率，
                    # 不会让破坏本身消失。所以直接取消这个能力。
                    # 失败留下的字是证据：下一次发送会在前置校验里发现"框不空"并中止，不会覆盖。
                    log.warning(
                        "wx_send_text_missing", who=who,
                        backend="keyboard_ocr", pasted=pasted, ink=_ink,
                    )
                    wx_chat.invalidate_active_chat("send_failed")
                    return {"ok": False,
                            "error": "消息文本未进入输入框（我没有清空、也没按回车；"
                                     "框里若还有字，那是证据，请人工看一眼）" + self._uipi_hint()}

                aborted = self._pre_send_guard(display_name, who)
                if aborted is not None:
                    return aborted

                _mk("before_baseline")
                baseline = ack_baseline(who)
                _mk("ack_baseline")
                _t_enter = time.time()
                wx_chat.invalidate_view()  # 回车前丢掉旧帧：回车后的判空必须看新帧
                # 发送键在执行前确定，执行后观察不能触发第二次发送。
                if selected_key == "ctrl_enter":
                    press_ctrl_enter()
                else:
                    press_enter()
                time.sleep(0.12)
                # 2026-10-31 提速：先像素差分判"框回到发送前（空）的样子"，只有差分说
                # "还有东西"才叫 OCR（一次 OCR 0.7–5 秒）；差分判"没变"= 框空了 = 发出去了。
                wx_chat.invalidate_view()  # 回车后重新截：这一帧决定"发出去了没有"
                _snap_after = wx_chat.message_box_snapshot()
                _ink_after = wx_chat.message_box_changed(_box_before, _snap_after)
                remaining = False if _ink_after is False else wx_chat.message_box_has_text()
                if _ink_after is False and _snap_after is not None:
                    # 框现在是空的 —— 记下来，下一次判空就不用 OCR 了。
                    try:
                        import numpy as _np2
                        _k2 = _np2.asarray(_snap_after, dtype="uint8").shape
                        wx_chat._BOX_EMPTY[(int(_k2[0]), int(_k2[1]))] = _snap_after
                    except Exception as exc:  # noqa: BLE001 - 记不住只是下次慢一点
                        skips.skip("wx_sender.py:remember_empty_box", "degraded", repr(exc)[:120])
                log.info("wx_send_timing", who=str(who),
                         type_s=round(_t_typed - _t_chat, 2),
                         post_s=round(time.time() - _t_typed, 2))
                if remaining:
                    log.warning(
                        "wx_send_unverified", who=who,
                        backend="keyboard_ocr",
                    )
                    return {"ok": False, "error": "发送后输入框仍有文本，请检查微信焦点/输入法" + self._uipi_hint()}


                _mk("enter_and_verify")
                _t_verified = time.time()
                log.info("wx_send_phases", who=who, reused=reused,
                         phases=" ".join("%s=%d" % (t, ms) for t, ms in _ph))
                log.info(
                    "wx_send_timing", who=who, reused=reused,
                    ms_activate=int((_t_chat - _t_enter) * 1000) if not reused else 0,
                    ms_type=int((_t_typed - _t_chat) * 1000),
                    ms_verify=int((_t_verified - _t_typed) * 1000),
                    ms_total=int((_t_verified - _t_enter) * 1000),
                )
                self._last_send_at = time.time()
                log.info(
                    "wx_send_ok", who=who, msg_len=len(msg),
                    backend="keyboard_ocr", verified=remaining is not None,
                )
                # 回执不占主路径：微信落盘实测 26 秒，同步等它等于让每条回复慢 30 秒。
                # 先回 pending，后台线程补账补上平台消息号。
                spawn_ack_backfill(who, msg, baseline)
                return {
                    "ok": True,
                    "who": who,
                    "sent_at": datetime.now().isoformat(),
                    "ack": _ACK_PENDING if baseline is not None else _ACK_UNCONFIRMED,
                }
            except Exception as e:
                log.error("wx_send_failed", who=who, error=str(e), backend="keyboard_ocr")
                return {"ok": False, "error": str(e)}

    @wx_chat.ui_locked
    def send_file(self, who: str, filepath: str) -> dict:
        """发文件：走「文件」按钮的选择文件框（原先是把路径当文字发出去）。"""
        from core import features as _features

        if _features.dry_run_send(str(who or "")):
            log.info("dry_run_send", platform="wechat", who=who,
                     kind="file", filepath=filepath)
            return {"ok": True, "dry_run": True}
        if not filepath or not os.path.isfile(str(filepath)):
            return {"ok": False, "error": "文件不存在：%s" % (filepath or "")}
        if not self.is_connected:
            if not self.connect():
                return {"ok": False, "error": "微信未连接"}
        display_name = resolve_nickname(who)
        with self._send_lock:
            elapsed = time.time() - self._last_send_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            try:
                hwnd = ensure_foreground()
                if not hwnd:
                    return {"ok": False, "error": "无法激活微信窗口" + self._uipi_hint(), "not_executed": True}
                search_result = self._search_and_enter_chat(display_name, raw_who=who)
                if not search_result["ok"]:
                    return search_result
                res = self._send_file_via_dialog(str(filepath), who)
                if res.get("ok"):
                    self._last_send_at = time.time()
                return res
            except Exception as exc:  # noqa: BLE001
                log.error("wx_send_file_failed", who=who, error=str(exc))
                return {"ok": False, "error": str(exc)}
    # ---------- 2026-10-27：走「文件」按钮 ----------
    # Ctrl+V 位图在微信 4.x 里认不出来（用户实测只贴出文字），
    # 点输入框下方第三个图标（文件夹=文件）弹出的 #32770「选择文件」才稳。
    _FILE_DIALOG_TITLES = ("选择文件", "打开", "发送文件", "Open")

    @staticmethod
    def _visible_dialogs() -> list:
        import win32gui

        found = []

        def cb(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                if win32gui.GetClassName(hwnd) != "#32770":
                    return
                found.append((hwnd, win32gui.GetWindowText(hwnd) or ""))
            except Exception:
                pass

        win32gui.EnumWindows(cb, None)
        return found

    def _icon_shape_score(self, img, icon) -> tuple:
        """给工具条图标打个形状分。文件夹是唯一「比高宽 + 有横贯上下的边」的。"""
        try:
            x, y, w, h = int(icon.x), int(icon.y), int(icon.w), int(icon.h)
            patch = img.convert("L").crop((x, y, x + w, y + h))
            W, H = patch.size
            data = list(patch.getdata())
            rows = [0] * H
            for idx, v in enumerate(data):
                if v < 150:
                    rows[idx // W] += 1
            wide_rows = sum(1 for r in rows if r >= 0.8 * W)
            aspect = W / max(1, H)
            return (wide_rows, 1 if aspect >= 1.05 else 0, round(aspect, 2))
        except Exception:  # noqa: BLE001
            return (0, 0, 0.0)

    def _click_file_button(self) -> bool:
        """点输入框下方的「文件」按钮。

        2026-10-27：原来写死 lay.icons[2]（0=表情 1=盒子 2=文件夹 3=截图 4=语音），
        微信一改版就会点歪。现在改成「按形状排候选 → 点完必须看到文件对话框
        才算数 → 看不到就 Esc 关掉换下一个」，最多试 3 个。
        """
        from core.execution import wx_human as _wh
        from core.execution import wx_vision as _wv
        try:
            win = _wv.find_wechat_window()
            if not win:
                return False
            cap = _wv.capture_window(win)
            img = cap[0] if isinstance(cap, tuple) else cap
            lay = _wv.detect_layout(img)
            if not lay.icons or len(lay.icons) < 3:
                return False
            order = sorted(range(len(lay.icons)),
                           key=lambda i: self._icon_shape_score(img, lay.icons[i]),
                           reverse=True)
            for rank, idx in enumerate(order[:3]):
                icon = lay.icons[idx]
                _wh.click(int(win.left + icon.cx), int(win.top + icon.cy))
                time.sleep(0.45)
                if self._find_file_dialog(1.5):
                    log.info("wx_file_icon_hit", index=idx, rank=rank,
                             score=list(self._icon_shape_score(img, icon)))
                    return True
                _wh.key_press(_wh.VK_ESCAPE)
                time.sleep(0.3)
            log.warning("wx_file_icon_miss", icons=len(lay.icons))
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("wx_file_icon_click_failed", error=str(exc))
            return False
    def _find_file_dialog(self, timeout: float = 3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for hwnd, title in self._visible_dialogs():
                if any(t in title for t in self._FILE_DIALOG_TITLES):
                    return hwnd
            time.sleep(0.15)
        return None

    @staticmethod
    def _screen_rect() -> tuple:
        import ctypes
        u = ctypes.windll.user32
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
                u.GetSystemMetrics(78), u.GetSystemMetrics(79))

    def _ensure_window_on_screen(self) -> dict:
        """窗口没完整露在屏幕里就先摆正；摆不正就报错，绝不盲点。

        2026-10-27：第一版把 GetSystemMetrics（逻辑像素）和窗口 rect（物理像素）
        混在一起比，DPI 150% 下永远判定"不在屏内"，每次都要挪一次窗口。
        现在统一用 MonitorFromWindow + GetWindowRect（同一套物理坐标），且只挪不缩。
        """
        from core.execution import wx_vision as _wv
        try:
            win = _wv.find_wechat_window()
            if not win:
                return {"ok": False, "error": "找不到微信窗口"}
            import win32api
            import win32gui
            hwnd = int(win.hwnd)
            mi = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd, 2))
            mx, my, mr, mb = mi["Monitor"]
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            w, h = right - left, bottom - top
            inside = (left >= mx - 1 and top >= my - 1
                      and right <= mr + 1 and bottom <= mb + 1)
            if inside and w >= 640 and h >= 480:
                return {"ok": True, "moved": False, "rect": [left, top, right, bottom]}
            nx = min(max(mx, left), max(mx, mr - w))
            ny = min(max(my, top), max(my, mb - h))
            win32gui.SetWindowPos(hwnd, 0, nx, ny, 0, 0, 0x0001 | 0x0040)
            time.sleep(0.45)
            l2, t2, r2, b2 = win32gui.GetWindowRect(hwnd)
            ok2 = (l2 >= mx - 1 and t2 >= my - 1 and r2 <= mr + 1 and b2 <= mb + 1)
            log.info("wx_window_repositioned", before=[left, top, right, bottom],
                     after=[l2, t2, r2, b2], monitor=list(mi["Monitor"]))
            return {"ok": ok2, "moved": True, "rect": [l2, t2, r2, b2],
                    "error": "" if ok2 else "窗口摆不回屏幕内"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "窗口检查失败：%s" % exc}

    @staticmethod
    def _dialog_filename_edit(dlg):
        """找「选择文件」对话框里的「文件名」输入框——最靠下的那个 Edit。

        对话框里有两个 Edit：上面是地址栏（文字形如「地址: C:\\...」），
        下面的才是文件名框。按窗口 top 坐标取最大者即可，不必依赖标题文字。
        """
        import win32gui

        cands = []

        def _walk(hwnd, _param):
            try:
                if win32gui.GetClassName(hwnd).lower() != "edit":
                    return True
                r = win32gui.GetWindowRect(hwnd)
            except Exception:  # noqa: BLE001
                return True
            cands.append((int(r[1]), hwnd))
            return True

        try:
            win32gui.EnumChildWindows(dlg, _walk, None)
        except Exception:  # noqa: BLE001
            return 0
        if not cands:
            return 0
        cands.sort(reverse=True)
        return cands[0][1]

    def _read_dialog_path(self, dlg) -> str:
        """OCR 读「文件名」框里的路径（读不到返回空串）。

        2026-10-27 实测：这个 Vista 风格对话框的文件名框是 DirectUI 控件，
        GetWindowText / WM_GETTEXT / UI Automation 的 ValuePattern **全都返回空**，
        只有截屏 OCR 能读到里面的字。所以回读走视觉，和项目其它地方一致。
        """
        import win32gui
        from core.execution import wx_vision as _wv

        try:
            left, top, right, bottom = win32gui.GetWindowRect(dlg)
            if right - left < 200 or bottom - top < 120:
                return ""
            # 文件名那一行在对话框底部：x 取中间偏右，避开左侧「文件名(N):」标签和右侧按钮
            box = (left + int((right - left) * 0.28),
                   bottom - 125,
                   left + int((right - left) * 0.72),
                   bottom - 80)
            img = _wv.capture(box)
            items = _wv.ocr_items(img)
        except Exception as exc:  # noqa: BLE001
            log.warning("wx_file_dialog_ocr_failed", error=str(exc))
            return ""
        parts = [str(it[0]) for it in items if len(it) > 5 and it[5] >= 0.5]
        return " ".join(parts).strip().strip('"')

    def _set_dialog_path(self, dlg, path: str) -> bool:
        """把路径粘进「文件名」框。

        不点鼠标：对话框一弹出，焦点本来就在文件名框里（实测直接 Ctrl+V 就进去了；
        之前加的那一次点击因为 DPI 缩放坐标错位，反而把焦点点丢，粘贴全部落空）。
        """
        from core.execution import wx_human as _wh

        try:
            import win32gui as _w
            _w.SetForegroundWindow(dlg)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.12)
        return bool(_wh.paste_text(str(path)))

    @staticmethod
    def _squash(text: str) -> str:
        """去掉空白和分隔符再比，容忍 OCR 噪声。"""
        return "".join(ch for ch in str(text or "").lower()
                       if ch.isalnum() or ch in "._-")

    def _wait_dialog_path(self, dlg, expect: str, timeout: float = 2.0) -> bool:
        """等文件名框里出现 expect 的文件名（框窄会显示成尾巴，所以按末段比）。"""
        want = self._squash(os.path.basename(expect))[-14:]
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = self._squash(self._read_dialog_path(dlg))
            if want and want in got:
                return True
            time.sleep(0.2)
        return False

    def _send_file_via_dialog(self, filepath: str, who: str) -> dict:
        """点「文件」→ 粘路径 → 回车选上 → 回输入框回车发出。"""
        import win32gui
        from core.execution import wx_human as _wh

        win_state = self._ensure_window_on_screen()
        if not win_state.get("ok"):
            return {"ok": False, "error": win_state.get("error") or "微信窗口不在屏幕内"}
        if not self._click_file_button():
            return {"ok": False, "error": "点不到「文件」按钮"}
        dlg = self._find_file_dialog(3.0)
        _t_open = time.time()
        if not dlg:
            return {"ok": False, "error": "文件对话框没出现"}
        try:
            win32gui.SetForegroundWindow(dlg)
        except Exception:
            pass
        time.sleep(0.25)
        if not self._set_dialog_path(dlg, str(filepath)):
            _wh.key_press(_wh.VK_ESCAPE)
            return {"ok": False, "error": "路径写不进对话框"}
        expect = os.path.normcase(os.path.abspath(str(filepath)))
        # 2026-10-28：文件名框的 OCR 回读对纯 ASCII 名字极不可靠（实测
        # "..._filetest.txt" 被读成 "nnyuedatalartifactsdrawiletesl"），
        # 于是改成「以对话框是否关闭为准」：回车后框还必须存在，才算没被接受。
        if not self._wait_dialog_path(dlg, expect, 1.2):
            log.info("wx_file_dialog_path_lenient", who=who, filepath=filepath,
                     got=self._read_dialog_path(dlg))
        path_ms = int((time.time() - _t_open) * 1000)
        _wh.key_press(_wh.VK_RETURN)
        time.sleep(0.7)
        # 选文件的回车之后只读等待；窗口状态未知时不能再按回车。
        deadline = time.time() + 4.0
        while time.time() < deadline:
            if not win32gui.IsWindowVisible(dlg):
                break
            time.sleep(0.15)
        else:
            _wh.key_press(_wh.VK_ESCAPE)
            return {"ok": False, "error": "文件对话框未确认关闭，未执行后续发送"}
        time.sleep(0.9)  # 等附件出现在输入框
        display_name = resolve_nickname(who)
        title_ok, seen = wx_chat.verify_active_chat_title(display_name, raw_who=who)
        if not title_ok:
            return {"ok": False, "error": "文件发送前目标会话未确认：%s" % seen}
        if not wx_chat.focus_message_box():
            return {"ok": False, "error": "文件发送前输入框焦点未确认"}
        aborted = self._pre_send_guard(display_name, who)
        if aborted is not None:
            return aborted
        _wh.key_press(_wh.VK_RETURN)  # 发送
        log.info("wx_send_file_ok", who=who, filepath=filepath,
                 path_ms=path_ms, total_ms=int((time.time() - _t_open) * 1000))
        return {"ok": True, "who": who, "kind": "file", "filepath": filepath,
                "path_ms": path_ms, "total_ms": int((time.time() - _t_open) * 1000),
                "sent_at": datetime.now().isoformat(), "ack": "unconfirmed"}

    def send_image(self, who: str, filepath: str) -> dict:
        """发图片：走「文件」按钮的文件对话框（微信 4.x 不吃剪贴板位图）。"""
        from core import features as _features

        if _features.dry_run_send(str(who or "")):
            log.info("dry_run_send", platform="wechat", who=who,
                     kind="image", filepath=filepath)
            return {"ok": True, "dry_run": True}
        if not filepath or not os.path.isfile(str(filepath)):
            return {"ok": False, "error": "图片不存在：%s" % (filepath or "")}
        if not self.is_connected:
            if not self.connect():
                return {"ok": False, "error": "微信未连接"}
        display_name = resolve_nickname(who)
        with self._send_lock:
            elapsed = time.time() - self._last_send_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            try:
                hwnd = ensure_foreground()
                if not hwnd:
                    return {"ok": False, "error": "无法激活微信窗口" + self._uipi_hint(), "not_executed": True}
                search_result = self._search_and_enter_chat(display_name, raw_who=who)
                if not search_result["ok"]:
                    return search_result
                res = self._send_file_via_dialog(str(filepath), who)
                if res.get("ok"):
                    self._last_send_at = time.time()
                return res
            except Exception as exc:  # noqa: BLE001
                log.error("wx_send_image_failed", who=who, error=str(exc))
                return {"ok": False, "error": str(exc)}
    def get_chat_list(self) -> list[str]:
        return []

    def disconnect(self):
        self._connected = False
        log.info("wx_sender_disconnected", backend="keyboard_ocr")


# ---------------------------------------------------------------------------
# 2026-10-28：发文件也要进世界账本（world_db.outbound_receipt）
# 之前只有「引擎说话」「GUI 直发文字」「画图发图」记账，文件发送完全漏掉。
# ---------------------------------------------------------------------------
_orig_send_file = WxSender.send_file


def _send_file_accounted(self, who, filepath):
    # 2026-10-28：文件名框回读对短相对路径很脆（实测 "data\\artifacts\\draw\\_filetest.txt"
    # 被 OCR 成 ":datalartifactsldraw filetestAl"）。发文件一律转绝对路径，
    # 这样和发图那条路一致，回读的是长路径，命中率明显更高。
    try:
        if filepath and not os.path.isabs(str(filepath)):
            filepath = os.path.abspath(str(filepath))
    except Exception:
        pass
    _r = _orig_send_file(self, who, filepath)
    if isinstance(_r, dict) and _r.get("dry_run"):
        return _r  # 预演没有发送事实，不落账、不启动追认。
    try:
        from db import world_db as _w
        _chat = str(who or "")
        if _chat and isinstance(_r, dict) and _r.get("ok"):
            _label = "\u6587\u4ef6\uff1a%s" % os.path.basename(str(filepath or ""))
            _w.record_outbound(_chat, _label, platform="wechat", method="wechat_file", ack="unconfirmed")
            # 只有发送尝试；同名文件/时间窗口不足以确认本次发送。
    except Exception:
        pass
    return _r


WxSender.send_file = _send_file_accounted


def _confirm_file_later(who: str, label: str, *args, **kwargs):
    """兼容旧调用；没有来源完整且唯一关联的文件凭据时不能确认。"""
    return {"confirmed": False, "why": "missing_file_delivery_evidence"}
