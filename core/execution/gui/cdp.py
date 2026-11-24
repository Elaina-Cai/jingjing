# -*- coding: utf-8 -*-
"""CDP 后端：把 Chrome / Edge / 任何 Electron 系 App 的界面读成文本。

出处：`docs/decisions/软件适配器.md` §18。为什么先做 CDP：

| 通道 | 覆盖面 | 可靠性 | 为什么排这个序 |
|---|---|---|---|
| CDP | 浏览器 + 全部 Electron 系 App（QQ NT / 飞书 / Slack / VSCode） | 高 | 有 DOM，读到的是结构不是像素 |
| ADB | 安卓全 App（一个驱动通吃） | 高 | 下一步，性价比最高 |
| UIA | Win32 / UWP（微信 PC、Office） | 中 | 控件树不稳，且会抢焦点 |
| Vision | 截图 + OCR + 坐标 | 低 | 只做兜底，且必须先有人确认 |

三条纪律：

1. **只连本机回环**（`127.0.0.1`）。远端调试端口一旦暴露，等于把浏览器交出去。
2. **关着就 blocked，不是 not_connected**：开关默认 `off`（`features.GUI_CHANNEL`），
   没开会话时连端口都不摸——免得一个「顺手读一下」变成后台扫描。
3. **只读**。这一片没有任何 `act`：点/输要等状态差异与授权链一起到位。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from core import features
from core.execution.gui.base import (
    ClickResult,
    GuiError,
    GuiMotion,
    GuiSnapshot,
    SNAPSHOT_MAX_CHARS,
    TypeResult,
    check_nav_url,
    check_selector,
    check_text,
    count_changed_pixels,
    diff_snapshots,
    expect_found,
    foreground_window_title,
    normalize_text,
    window_title_matches,
)
from safelog import warn

#: 默认调试端口（Edge/Chrome 用 `--remote-debugging-port=9222` 起）。
DEFAULT_ENDPOINT: str = "http://127.0.0.1:9222"

#: 连不上端口的判定时长；命令回来的判定时长。
CONNECT_TIMEOUT_S: float = 5.0
COMMAND_TIMEOUT_S: float = 8.0

#: 跳转后等「我要看到的那句话」出现：重读几次、每次间隔多久。
NAV_POLLS: int = 3
NAV_POLL_INTERVAL_S: float = 0.2

#: 证据截图放哪：`工具包.md` §4.1 第 3 条要求动作前后各一张，落在 `data/evidence/`。
EVIDENCE_DIR: str = "data/evidence"

#: 点一个元素前，先问它「你在屏幕上的什么位置」（只取矩形中心，不点）。
FOCUS_EXPRESSION = (
    "(function(s){var e=document.querySelector(s);"
    "if(!e){return false;}"
    "if(e.focus){e.focus();}"
    "if(e.select){e.select();}"
    "else if(e.setSelectionRange){try{e.setSelectionRange(0,(e.value||'').length);}catch(err){}}"
    "return document.activeElement===e;"
    "})(%s)"
)

VALUE_EXPRESSION = (
    "(function(s){var e=document.querySelector(s);"
    "if(!e){return null;}"
    "var v=(typeof e.value==='string')?e.value:(e.innerText||'');"
    "return {value:v,tag:e.tagName||''};"
    "})(%s)"
)

RECT_EXPRESSION: str = (
    "(function(s){var e=document.querySelector(s);"
    "if(!e){return null;}var r=e.getBoundingClientRect();"
    "var x=r.left+r.width/2,y=r.top+r.height/2;"
    "return {x:x,y:y,w:r.width,h:r.height};})(%s)"
)

#: 读一屏的表达式：只取 `title / url / innerText`，不注入任何东西、不点任何东西。
SNAPSHOT_EXPRESSION: str = (
    "(function(){var b=document.body?document.body.innerText:'';"
    "return JSON.stringify({title:document.title||'',url:location.href||'',text:b});})()"
)

#: 允许读的 URL 协议（`file:` / `chrome:` 之类一律不读）。
ALLOWED_SCHEMES: tuple[str, ...] = ("http://", "https://")


from core.infra import egress, duration


def _mode() -> str:
    """唯一读开关的地方（`GUI_CHANNEL`，三档，默认 `off`）。"""
    return features.mode(features.GUI_CHANNEL, features.MODE_OFF)


def _http_json(url: str, *, timeout: float, http_get: Callable[[str], Any] | None = None) -> Any:
    if http_get is not None:
        return http_get(url)
    rep = egress.get(url, allow_private=True,
                     timeout_ms=duration.to_ms(f'{timeout}s'))
    return json.loads(rep.text)


def list_targets(
    endpoint: str = DEFAULT_ENDPOINT,
    *,
    timeout: float = CONNECT_TIMEOUT_S,
    http_get: Callable[[str], Any] | None = None,
) -> list[dict[str, str]]:
    """列出可读的页面（只要 `type == "page"` 且是 http(s)）。"""
    base = str(endpoint or DEFAULT_ENDPOINT).rstrip("/")
    if not base.startswith(ALLOWED_SCHEMES):
        raise GuiError("blocked", f"只连本机 http(s) 调试端口：{base}")
    try:
        data = _http_json(base + "/json", timeout=timeout, http_get=http_get)
    except GuiError:
        raise
    except Exception as exc:  # noqa: BLE001 - 端口没开、被拒、不是 JSON 都算连不上
        raise GuiError("not_connected", f"{base}: {exc}") from None
    if not isinstance(data, list):
        raise GuiError("not_connected", f"{base}/json 没有返回列表")
    out: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict) or str(item.get("type") or "") != "page":
            continue
        url = str(item.get("url") or "")
        if not url.lower().startswith(ALLOWED_SCHEMES):
            continue
        out.append(
            {
                "id": str(item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "url": url,
                "ws": str(item.get("webSocketDebuggerUrl") or ""),
            }
        )
    return out


def pick_target(targets: list[dict[str, str]], target: str = "") -> dict[str, str]:
    """选页面：空 = 第一个；纯数字 = 下标；其它 = URL/标题子串。"""
    if not targets:
        raise GuiError("no_target", "没有可读的页面")
    key = str(target or "").strip()
    if not key:
        return targets[0]
    if key.isdigit():
        idx = int(key)
        if 0 <= idx < len(targets):
            return targets[idx]
        raise GuiError("no_target", f"第 {idx} 个页面不存在（共 {len(targets)} 个）")
    for item in targets:
        if key in item["url"] or key in item["title"]:
            return item
    raise GuiError("no_target", key[:120])


class _SyncSession:
    """最小的同步 CDP 会话：send(method, params) → result。"""

    def __init__(self, ws_url: str, *, timeout: float = COMMAND_TIMEOUT_S) -> None:
        self._ws_url = ws_url
        self._timeout = timeout
        self._next_id = 0
        self._ws: Any = None

    def __enter__(self) -> "_SyncSession":
        try:
            from websockets.sync.client import connect
        except Exception as exc:  # noqa: BLE001
            raise GuiError("unsupported", f"没有 websockets 库：{exc}") from None
        try:
            self._ws = connect(self._ws_url, open_timeout=self._timeout).__enter__()
        except Exception as exc:  # noqa: BLE001
            raise GuiError("not_connected", f"{self._ws_url}: {exc}") from None
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001 - 关不掉不影响结果
                warn("core/execution/gui/cdp.py:187 Exception", err=repr(__import__("sys").exc_info()[1]))
            self._ws = None

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._ws is None:
            raise GuiError("not_connected", "会话没打开")
        self._next_id += 1
        msg_id = self._next_id
        try:
            self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            while True:
                raw = self._ws.recv(timeout=self._timeout)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                payload = json.loads(raw)
                if isinstance(payload, dict) and payload.get("id") == msg_id:
                    break
        except GuiError:
            raise
        except Exception as exc:  # noqa: BLE001 - 命令没回来
            raise GuiError("timeout", f"{method}: {exc}") from None
        if not isinstance(payload, dict):
            raise GuiError("unsupported", f"{method}: 回包不是对象")
        error = payload.get("error")
        if isinstance(error, dict):
            raise GuiError("unsupported", f"{method}: {error.get('message') or error}")
        result = payload.get("result")
        return result if isinstance(result, dict) else {}


class CdpReader:
    """只读驱动：`snapshot()` 是它唯一的动作。"""

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        connect: Callable[[str], Any] | None = None,
        http_get: Callable[[str], Any] | None = None,
        timeout: float = COMMAND_TIMEOUT_S,
        taken_at: Callable[[], str] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._connect = connect or (lambda ws: _SyncSession(ws, timeout=timeout))
        self._http_get = http_get
        self._timeout = timeout
        self._taken_at = taken_at

    def _stamp(self) -> str:
        if self._taken_at is not None:
            return str(self._taken_at())
        from datetime import datetime

        return datetime.now().isoformat(sep=" ", timespec="seconds")

    def _open(self, target: str = "") -> dict[str, str]:
        """选页面（顺带校验：不能没有 ws 地址，那说明没开调试端口）。"""
        page = pick_target(list_targets(self.endpoint, http_get=self._http_get), target)
        if not page["ws"]:
            raise GuiError("not_connected", "页面没有 webSocketDebuggerUrl（缺 --remote-debugging-port）")
        return page

    def _call(self, page: dict[str, str], method: str, params: dict[str, Any]) -> Any:
        """一次 CDP 命令：开会话 → send → 关会话。驱动层只往外抛封闭错误码。"""
        session = self._connect(page["ws"])
        enter = getattr(session, "__enter__", None)
        if enter is not None:
            enter()
        try:
            try:
                return session.send(method, params)
            except GuiError:
                raise
            except Exception as exc:  # noqa: BLE001 - 驱动层只往外抛封闭错误码
                raise GuiError("timeout", f"{method}: {exc}") from None
        finally:
            leave = getattr(session, "__exit__", None)
            if leave is not None:
                try:
                    leave(None, None, None)
                except Exception:  # noqa: BLE001
                    warn("core/execution/gui/cdp.py:268 Exception", err=repr(__import__("sys").exc_info()[1]))

    def _read_page(self, page: dict[str, str], *, max_chars: int = SNAPSHOT_MAX_CHARS) -> GuiSnapshot:
        """读**这一个**页面（选页由调用方做，读两次才算差异时不必再选一次）。"""
        result = self._call(
            page,
            "Runtime.evaluate",
            {
                "expression": SNAPSHOT_EXPRESSION,
                "returnByValue": True,
                "awaitPromise": False,
            },
        )
        if not isinstance(result, dict):
            raise GuiError("unsupported", "Runtime.evaluate 没有返回对象")
        if result.get("exceptionDetails"):
            raise GuiError("unsupported", str(result.get("exceptionDetails"))[:200])
        value = (result.get("result") or {}).get("value") if isinstance(result.get("result"), dict) else None
        if not isinstance(value, str):
            raise GuiError("unsupported", "Runtime.evaluate 没给字符串")
        try:
            payload = json.loads(value)
        except ValueError:
            raise GuiError("unsupported", "页面返回值不是 JSON") from None
        if not isinstance(payload, dict):
            raise GuiError("unsupported", "页面返回值不是对象")
        text, truncated = normalize_text(payload.get("text"), max_chars=max_chars)
        return GuiSnapshot(
            target=page["id"],
            url=str(payload.get("url") or page["url"]),
            title=str(payload.get("title") or page["title"]),
            text=text,
            taken_at=self._stamp(),
            truncated=truncated,
        )

    def snapshot(
        self, target: str = "", *, max_chars: int = SNAPSHOT_MAX_CHARS
    ) -> GuiSnapshot:
        """读一屏。**开关没开就 blocked，连端口都不摸。**"""
        if _mode() == features.MODE_OFF:
            raise GuiError("blocked", "GUI_CHANNEL=off")
        return self._read_page(self._open(target), max_chars=max_chars)

    def _screenshot(self, page: dict[str, str]) -> bytes:
        """要一张 PNG（base64 回来，自己解码）。"""
        raw = self._call(page, "Page.captureScreenshot", {"format": "png"})
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, str) or not data:
            raise GuiError("unsupported", "Page.captureScreenshot 没给 data")
        import base64

        try:
            return base64.b64decode(data)
        except Exception as exc:  # noqa: BLE001
            raise GuiError("unsupported", f"截图解不开：{exc}") from None

    def _write_evidence(self, png: bytes, evidence_dir: str, turn_id: str, label: str = "after") -> str:
        """把一张截图落到磁盘，返回路径；写不了就返回空串（不阻断动作）。

        名字是**定死的**：``<turn_id>_click_<before|after>.png``。
        为什么不用自增序号：序号随进程历史漂移，同一件事重跑一次就对不上上一次的证据，
        而「这个动作当时到底长什么样」是全套回执里唯一不可重新推导的东西。
        同一个 turn 里重复写同名文件时，加 ``-2`` / ``-3`` 后缀，绝不覆盖。
        """
        if not png or not str(evidence_dir or "").strip():
            return ""
        try:
            from pathlib import Path

            out = Path(str(evidence_dir))
            out.mkdir(parents=True, exist_ok=True)
            stem = f"{turn_id or 'click'}_click_{label}"
            path = out / f"{stem}.png"
            n = 2
            while path.exists():
                path = out / f"{stem}-{n}.png"
                n += 1
            path.write_bytes(png)
            return str(path)
        except Exception:  # noqa: BLE001 - 证据写不下不影响「动作已经发生」这个事实
            warn("core/execution/gui/cdp.py:349 Exception", err=repr(__import__("sys").exc_info()[1]))
            return ""

    def click(
        self,
        selector: object,
        *,
        target: str = "",
        window_title: str = "",
        expect: str = "",
        max_chars: int = SNAPSHOT_MAX_CHARS,
        evidence_dir: str = EVIDENCE_DIR,
        turn_id: str = "",
        polls: int = NAV_POLLS,
        pre_gate: Any = None,
    ) -> ClickResult:
        """**真的点一下**：先验两条判据（前台 / 标题），再派发真鼠标事件，最后量像素差。

        三条判据的分工：前两条（前台 / 标题）在动手**之前**判——不成立就一步不动；
        第三条（≥100 像素变化）只能事后量，量不到就当失败（返回的 `criteria()` 里为 False，
        由调用方交给 `can_ui_control()` 做唯一裁决）。
        """
        if _mode() == features.MODE_OFF:
            raise GuiError("blocked", "GUI_CHANNEL=off")
        sel = check_selector(selector)
        page = self._open(target)
        before = self._read_page(page, max_chars=max_chars)

        fg_title = foreground_window_title()
        foreground_ok = window_title_matches(window_title, fg_title)
        title_ok = foreground_ok and bool(str(before.title or "").strip())
        if pre_gate is not None:
            allowed, reason = pre_gate(foreground_ok, title_ok)
            if not allowed:
                raise GuiError(
                    "blocked",
                    f"{reason or 'ui_blocked'}（前台={foreground_ok} 标题={title_ok}）",
                )

        rect = self._call(
            page,
            "Runtime.evaluate",
            {
                "expression": RECT_EXPRESSION % __import__("json").dumps(sel),
                "returnByValue": True,
                "awaitPromise": False,
            },
        )
        box = rect.get("result", {}).get("value") if isinstance(rect, dict) else None
        if not isinstance(box, dict) or "x" not in box or "y" not in box:
            raise GuiError("no_target", f"选择器没匹配到可见元素：{sel[:80]}")

        x, y = float(box["x"]), float(box["y"])
        shot_before = self._screenshot(page)
        evidence_before = self._write_evidence(shot_before, evidence_dir, turn_id, "before")
        move = {"x": x, "y": y, "button": "left", "clickCount": 1}
        self._call(page, "Input.dispatchMouseEvent", dict(move, type="mouseMoved"))
        self._call(page, "Input.dispatchMouseEvent", dict(move, type="mousePressed"))
        self._call(page, "Input.dispatchMouseEvent", dict(move, type="mouseReleased"))
        shot_after = self._screenshot(page)
        pixels = count_changed_pixels(shot_before, shot_after)

        after = self._read_page(page, max_chars=max_chars)
        tries = 0
        while expect and not expect_found(after, expect) and tries < max(0, int(polls)):
            import time

            time.sleep(NAV_POLL_INTERVAL_S)
            after = self._read_page(page, max_chars=max_chars)
            tries += 1
        return ClickResult(
            target=page["id"],
            selector=sel,
            url_before=before.url,
            url_after=after.url,
            title_after=after.title,
            state_before=before.digest(),
            state_after=after.digest(),
            changed=diff_snapshots(before, after),
            foreground_title=fg_title,
            foreground_ok=foreground_ok,
            title_ok=title_ok,
            pixels_changed=int(pixels),
            evidence=self._write_evidence(shot_after, evidence_dir, turn_id, "after"),
            evidence_before=evidence_before,
            expect_found=expect_found(after, expect),
            at=self._stamp(),
        )

    def _field_value(self, page: dict[str, object], selector: str) -> str | None:
        """读输入框的当前值；元素不存在给 None（不去猜）。"""
        out = self._call(
            page,
            "Runtime.evaluate",
            {
                "expression": VALUE_EXPRESSION % __import__("json").dumps(selector),
                "returnByValue": True,
                "awaitPromise": False,
            },
        )
        value = out.get("result", {}).get("value") if isinstance(out, dict) else None
        if not isinstance(value, dict):
            return None
        return str(value.get("value") or "")

    def insert_text(
        self,
        selector: object,
        text: object,
        *,
        target: str = "",
        window_title: str = "",
        expect: str = "",
        max_chars: int = SNAPSHOT_MAX_CHARS,
        polls: int = NAV_POLLS,
        pre_gate: Any = None,
    ) -> TypeResult:
        """在网页里打字：聚焦 -> 选中原值 -> Input.insertText -> 读回输入框的值。

        与 click 的三点不同：

        1. 判据是输入框的值变了没有，不是像素（打一个字只变十几个像素）；
        2. 前两条判据（前台 / 标题）照旧在动手之前判，不过就一步不动；
        3. 用 Input.insertText（页面内合成事件：不抢系统焦点、不动剪贴板），
           代价是 React/Vue 那种受控输入可能不更新框架 state——DOM 上的值是对的，
           但页面若把值存在自己的 state 里，提交时可能读不到。这条限制写在文档里。
        """
        if _mode() == features.MODE_OFF:
            raise GuiError("blocked", "GUI_CHANNEL=off")
        sel = check_selector(selector)
        body = check_text(text)
        page = self._open(target)
        before = self._read_page(page, max_chars=max_chars)

        fg_title = foreground_window_title()
        foreground_ok = window_title_matches(window_title, fg_title)
        title_ok = foreground_ok and bool(str(before.title or "").strip())
        if pre_gate is not None:
            allowed, reason = pre_gate(foreground_ok, title_ok)
            if not allowed:
                raise GuiError(
                    "blocked",
                    f"{reason or 'ui_blocked'}(前台={foreground_ok} 标题={title_ok})",
                )

        value_before = self._field_value(page, sel)
        if value_before is None:
            raise GuiError("no_target", f"选择器没匹配到输入框：{sel[:80]}")
        focus = self._call(
            page,
            "Runtime.evaluate",
            {
                "expression": FOCUS_EXPRESSION % __import__("json").dumps(sel),
                "returnByValue": True,
                "awaitPromise": False,
            },
        )
        focused = focus.get("result", {}).get("value") if isinstance(focus, dict) else None
        if not focused:
            raise GuiError("no_target", f"输入框聚焦失败：{sel[:80]}")
        self._call(page, "Input.insertText", {"text": body})
        value_after = self._field_value(page, sel)
        if value_after is None:
            raise GuiError("no_target", f"打完字输入框不见了：{sel[:80]}")

        after = self._read_page(page, max_chars=max_chars)
        tries = 0
        while expect and not expect_found(after, expect) and tries < max(0, int(polls)):
            import time

            time.sleep(NAV_POLL_INTERVAL_S)
            after = self._read_page(page, max_chars=max_chars)
            tries += 1
        changed: dict[str, object] = {}
        if value_after != value_before:
            changed = {
                "value_sha256": __import__("hashlib").sha256(
                    value_after.encode("utf-8", "replace")
                ).hexdigest()[:16],
                "value_len": len(value_after),
            }
        return TypeResult(
            target=str(page.get("id") or ""),
            selector=sel,
            url_before=before.url,
            url_after=after.url,
            title_after=after.title,
            state_before=before.digest(),
            state_after=after.digest(),
            changed=changed,
            value_before=value_before,
            value_after=value_after,
            expect_found=expect_found(after, expect),
            at=self._stamp(),
        )
    def navigate(
        self,
        url: object,
        *,
        target: str = "",
        expect: str = "",
        max_chars: int = SNAPSHOT_MAX_CHARS,
        polls: int = NAV_POLLS,
    ) -> GuiMotion:
        """**第一个写动作**：只跳转，不点不输。返回状态差异式回执。

        为什么先做它：跳转是唯一「可逆、幂等性无所谓、失败也能看出来」的写动作
        （地址变了没有一眼可判），适合用来把 act 的证据链与授权链接通，再碰点击。
        """
        if _mode() == features.MODE_OFF:
            raise GuiError("blocked", "GUI_CHANNEL=off")
        dest = check_nav_url(url)
        page = self._open(target)
        before = self._read_page(page, max_chars=max_chars)
        self._call(page, "Page.navigate", {"url": dest})
        after = self._read_page(page, max_chars=max_chars)
        tries = 0
        while expect and not expect_found(after, expect) and tries < max(0, int(polls)):
            import time

            time.sleep(NAV_POLL_INTERVAL_S)
            after = self._read_page(page, max_chars=max_chars)
            tries += 1
        return GuiMotion(
            target=page["id"],
            action="navigate",
            url_before=before.url,
            url_after=after.url,
            title_after=after.title,
            state_before=before.digest(),
            state_after=after.digest(),
            changed=diff_snapshots(before, after),
            expect_found=expect_found(after, expect),
            at=self._stamp(),
        )
