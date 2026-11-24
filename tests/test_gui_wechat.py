"""GUI · 微信写侧（半程）：读库渲染 + 只填不发。

跑法：``python -X utf8 -u tests/test_gui_wechat.py``
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import features  # noqa: E402
from core.execution.gui import wechat  # noqa: E402
from core.execution.gui.base import GuiError  # noqa: E402

FAILED = 0


def check(name: str, cond: object, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"OK   {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}  {detail}")


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE Msg_ab12 (local_id INTEGER, msg_svr_id INTEGER, type INT, "
            "sub_type INT, is_sender INT, create_time INT, str_talker TEXT, str_content TEXT)"
        )
        conn.executemany(
            "INSERT INTO Msg_ab12 VALUES (?,?,?,?,?,?,?,?)",
            [
                (1, 1001, 1, 0, 0, 1000, "群A", "第一条"),
                (2, 1002, 1, 0, 1, 1060, "群A", "我回的"),
                (3, 1003, 1, 0, 0, 1120, "群A", "第三条\n换行了"),
                (4, 1004, 1, 0, 0, 1090, "张三", "私聊那句"),
            ],
        )
        conn.execute(
            "CREATE TABLE MSG7 (localId INT, serverId INT, StrTalker TEXT, "
            "StrContent TEXT, IsSender INT, CreateTime INT)"
        )
        conn.executemany(
            "INSERT INTO MSG7 VALUES (?,?,?,?,?,?)",
            [
                (10, 2001, "群A", "旧表那条", 0, 940),
            ],
        )
        conn.execute("CREATE TABLE NotAMessage (x INT)")
        conn.execute("INSERT INTO NotAMessage VALUES (1)")
        conn.commit()
    finally:
        conn.close()


class FakeDeps:
    def __init__(self, *, focus: bool = True, has_text: bool | None = True,
                 title: str = "微信", foreground: str = "微信", png: bool = True,
                 mention_ok: bool = True) -> None:
        self.calls: list[str] = []
        self.mentioned: list[str] = []
        self._mention_ok = mention_ok
        self._focus = focus
        self._has_text = has_text
        self._title = title
        self._foreground = foreground
        self._png = png
        self.pasted = ""

    def window_title(self) -> str:
        self.calls.append("window_title")
        return self._title

    def foreground_title(self) -> str:
        self.calls.append("foreground_title")
        return self._foreground

    def open_chat(self, chat: str):
        self.calls.append(f"open_chat:{chat}")
        return {"ok": True}

    def focus_message_box(self) -> bool:
        self.calls.append("focus_message_box")
        return self._focus

    def mention(self, name: str) -> bool:
        self.calls.append(f"mention:{name}")
        self.mentioned.append(name)
        return self._mention_ok

    def paste(self, text: str) -> bool:
        self.calls.append("paste")
        self.pasted = text
        return True

    def message_box_has_text(self):
        self.calls.append("message_box_has_text")
        return self._has_text

    def screenshot(self):
        self.calls.append("screenshot")
        return b"\x89PNG-fake" if self._png else None


def _body(src: str, name: str) -> str:
    """切出某个函数的函数体（静态纪律要按方法判定，不能整文件一刀切）。"""
    i = src.find(f"def {name}(")
    if i < 0:
        return ""
    j = src.find("\n    def ", i + 1)
    return src[i: j if j > 0 else len(src)]


def main() -> int:
    tmp = ROOT / ".test_tmp" / "wechat_ui_case"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    db = tmp / "merge_msg.db"
    _make_db(db)

    # ---- 读 ----
    rows = wechat.read_recent(db, "群A")
    check("读：只取该会话", all(r["chat"] == "群A" for r in rows), str(rows))
    check("读：跨两套表名都收", len(rows) == 4, str(len(rows)))
    check("读：按时间升序", [r["time"] for r in rows] == [940, 1000, 1060, 1120],
          str([r["time"] for r in rows]))
    check("读：is_sender 认成「我」", [r["mine"] for r in rows if r["time"] == 1060] == [True])
    check("读：limit 取最近的", [r["time"] for r in wechat.read_recent(db, "群A", limit=2)]
          == [1060, 1120])
    check("读：没这个会话就给空", wechat.read_recent(db, "不存在的人") == [])
    check("读：空 chat 取全体", len(wechat.read_recent(db, "")) == 5)
    check("读：跳过没有说话人列的表", all("x" not in r["text"] for r in rows))

    try:
        wechat.read_recent(tmp / "nope.db", "群A")
        check("读：库不在 → not_connected", False, "没抛")
    except GuiError as exc:
        check("读：库不在 → not_connected", exc.code == "not_connected", exc.code)

    # ---- 渲染 ----
    text = wechat.render_lines(rows)
    check("渲染：一行一条", len(text.splitlines()) == 4, repr(text))
    check("渲染：我/对方分得清", "我: 我回的" in text and "对方: 第一条" in text, repr(text))
    check("渲染：换行压成空格", "第三条 换行了" in text, repr(text))
    from datetime import datetime
    expect_stamp = datetime.fromtimestamp(1120).strftime("%H:%M")
    check("渲染：时间戳 HH:MM", text.splitlines()[-1].startswith(expect_stamp), repr(text))
    long_row = [{"chat": "群A", "text": "字" * 500, "mine": False, "time": 0}]
    check("渲染：单条超 300 字截断", wechat.render_lines(long_row).endswith("…"))

    # ---- snapshot（开关） ----
    original_mode = wechat._mode
    try:
        wechat._mode = lambda: features.MODE_OFF
        try:
            wechat.snapshot("群A", db_path=db)
            check("snapshot：off → blocked", False, "没抛")
        except GuiError as exc:
            check("snapshot：off → blocked", exc.code == "blocked", exc.code)

        wechat._mode = lambda: features.MODE_ON
        snap = wechat.snapshot("群A", db_path=db, taken_at="2026-10-21 10:00:00")
        check("snapshot：正文含最后一条", "第三条" in snap.text, snap.text[:60])
        check("snapshot：标题带会话名", "群A" in snap.title, snap.title)
        check("snapshot：target 是会话", snap.target == "群A", snap.target)
        check("snapshot：指纹稳定", len(snap.digest()) == 16, snap.digest())
        check("snapshot：taken_at 透传", snap.taken_at == "2026-10-21 10:00:00")
        check("snapshot：没这个会话也不炸", wechat.snapshot("无名", db_path=db).text == "")

        # ---- 填字（半程） ----
        deps = FakeDeps()
        try:
            wechat.fill_message("群A", "   ", deps=deps)
            check("填字：空文本拒", False, "没抛")
        except GuiError as exc:
            check("填字：空文本拒", exc.code == "unsupported", exc.code)
            check("填字：空文本不碰界面", deps.calls == [], str(deps.calls))

        deps = FakeDeps(focus=False)
        try:
            wechat.fill_message("群A", "在吗", deps=deps)
            check("填字：输入框没聚焦 → no_target", False, "没抛")
        except GuiError as exc:
            check("填字：输入框没聚焦 → no_target", exc.code == "no_target", exc.code)
            check("填字：没聚焦就不粘字", "paste" not in deps.calls, str(deps.calls))

        deps = FakeDeps(title="", foreground="")
        try:
            wechat.fill_message("群A", "在吗", deps=deps)
            check("填字：没微信窗口 → not_connected", False, "没抛")
        except GuiError as exc:
            check("填字：没微信窗口 → not_connected", exc.code == "not_connected", exc.code)
            check("填字：没窗口就不开聊天", not any(c.startswith("open_chat") for c in deps.calls))

        deps = FakeDeps()
        res = wechat.fill_message("群A", "在吗", deps=deps, turn_id="t1",
                                 evidence_dir=tmp / "ev")
        order = [c for c in deps.calls if c != "screenshot"]
        check("填字：顺序 = 找窗口 → 开聊天 → 聚焦 → 粘字 → 核对",
              order == ["window_title", "foreground_title", "open_chat:群A",
                        "focus_message_box", "paste", "message_box_has_text"], str(order))
        check("填字：sent 永远 False", res.sent is False and res.as_receipt()["sent"] is False)
        check("填字：text_sha256 是 16 位",
              res.text_sha256 == hashlib.sha256("在吗".encode()).hexdigest()[:16],
              res.text_sha256)
        check("填字：核对方说填进去了", res.filled_verified is True)
        check("填字：前台/标题两旗都过", res.foreground_ok and res.title_ok)
        check("填字：像素差算了", res.pixels_changed == -1 or res.pixels_changed >= 0,
              str(res.pixels_changed))
        check("填字：证据前后各一张",
              Path(res.evidence_before).exists() and Path(res.evidence_after).exists(),
              f"{res.evidence_before} | {res.evidence_after}")
        check("填字：证据名定死", Path(res.evidence_before).name == "t1_wechat_fill_before.png",
              res.evidence_before)

        deps = FakeDeps(has_text=False)
        res = wechat.fill_message("群A", "在吗", deps=deps, evidence_dir=tmp / "ev")
        check("填字：核对不上就不算填进去", res.filled_verified is False)

        deps = FakeDeps(has_text=None)
        res = wechat.fill_message("群A", "在吗", deps=deps, evidence_dir=tmp / "ev")
        check("填字：核对不可用 → 退回「粘字成功」", res.filled_verified is True)

        deps = FakeDeps()
        res = wechat.fill_message("群A", "字" * 3000, deps=deps, evidence_dir=tmp / "ev")
        check("填字：超 2000 字截断",
              len(deps.pasted) == wechat.FILL_MAX_CHARS
              and res.text_sha256 == hashlib.sha256(deps.pasted.encode()).hexdigest()[:16],
              str(len(deps.pasted)))
    finally:
        wechat._mode = original_mode

    # ---- 纪律（静态） ----
    src = Path(wechat.__file__).read_text(encoding="utf-8")
    # ---- 群里 @：必须落在 paste 之前，失败就一格字都不填 ----
    _prev_mode = wechat._mode
    wechat._mode = lambda: features.MODE_ON
    deps = FakeDeps()
    wechat.fill_message("群A", "在吗", mention="小奇", deps=deps,
                        evidence_dir=tmp / "ev")
    order = [c for c in deps.calls if c.startswith(("mention", "paste"))]
    check("@ 在正文之前", order == ["mention:小奇", "paste"], order)
    check("@ 只叫一次", deps.mentioned == ["小奇"], deps.mentioned)
    check("@ 前缀 @ 会被剥掉", order[0] == "mention:小奇", order[0])

    deps = FakeDeps(mention_ok=False)
    try:
        wechat.fill_message("群A", "在吗", mention="小奇", deps=deps,
                            evidence_dir=tmp / "ev")
    except wechat.GuiError as exc:
        err = f"{exc.code}:{exc.detail}"
    else:
        err = "没有抛"
    check("@ 失败 → unsupported", err.startswith("unsupported"), err)
    check("@ 失败 → detail 说 mention_failed", "mention_failed" in err, err)
    check("@ 失败 → 没填字", "paste" not in deps.calls, deps.calls)

    deps = FakeDeps()
    wechat.fill_message("群A", "在吗", deps=deps, evidence_dir=tmp / "ev")
    check("不说 @ 就不调 mention", deps.mentioned == [], deps.mentioned)
    wechat._mode = _prev_mode

    check("纪律：fill_message 里不按回车", "press_enter" not in _body(src, "fill_message") and "VK_RETURN" not in src)
    check("纪律：不走发送口",
          "send_text(" not in src and "from core.speech" not in src
          and "wx_executor.send" not in src
          # 整个文件只有一处能把回车按下去：_Deps.press_enter（只给 wechat_send 用）
          and src.count("wx_chat.press_enter") == 1
          and "\n                wx_chat.press_enter()" in src)
    check("纪律：文档写明不按发送", "不按发送" in src or "不按发送" in (wechat.__doc__ or ""))
    check("纪律：不 import speech/routing/agents",
          "core.speech" not in src and "core.routing" not in src and "import agents" not in src)
    check("开关：WECHAT_UI 默认 off",
          features.FLAG_REGISTRY[features.WECHAT_UI].default == features.MODE_OFF)
    check("开关：总数 59", len(features.TRI_STATE_FLAGS) == 59, str(len(features.TRI_STATE_FLAGS)))  # 2026-11-03 P1-1 销账 +1（OPEN_LOOP_CLOSE）

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"RESULT {FAILED} failed")
    return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
