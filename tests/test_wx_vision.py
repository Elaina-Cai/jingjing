"""Regression tests for the mouse-driven WeChat perception layer.

These run entirely offline against screenshots that were captured from a real
WeChat 4.x window, so they guard the two things that actually broke before:

* coordinates must be derived from the frame, never from a stored offset, so
  the window can be moved or resized freely;
* the sticker match must be specific enough to pick the right cell and to
  refuse when the wanted sticker is not on screen.

The fixtures are dev artefacts.  If they are missing the tests report SKIP and
return True rather than failing, so the suite stays usable on a clean checkout.

Isolation
---------
This file used to have **no** isolation entry at all.  Run directly, its
send_sticker case reaches ``SendStickerTool`` → ``core.speech.turn.
snapshot_from_context`` → ``core.bootstrap.init_singletons()``, which resolves
``PathSettings`` defaults (``./data/yinyue_meta.db`` / ``./data/platforms``) and
ran ``init_meta_db`` + ``run_migrations`` **on the production meta database**
(since 2026-10-02 that is refused by ``core/bootstrap.py::
assert_migrations_allowed`` — the refusal was the red cell, and it is what this
file was missing).  Everything now goes through the single entry
``tests/_tempdirs.py``, and the two things the case legitimately needs from the
repo are materialised into the isolated root instead of being read from the
production trees (see ``_materialise_dev_emoji`` / ``_ensure_active_persona``).
"""

from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pathlib import Path

# 临时目录的唯一入口（见 tests/_tempdirs.py）：导入即把本进程的 TEMP/TMP/
# TMPDIR/SQLITE_TMPDIR 钉到基座。**必须在 import core.* 之前接上**——
# core.config.settings 一被导入就把 META_DB_PATH / PLATFORMS_DIR / DATA_DIR 解析死。
from _tempdirs import temp_base  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------- 隔离根
_ISO_ROOT = temp_base() / "wx_vision_iso"
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")
os.environ["LOG_DIR"] = str(_ISO_ROOT / "logs")

#: 本文件的表情库素材属于该账号（见文件头：dev artefact）。
_DEV_PLATFORM, _DEV_ACCOUNT_ID = "wechat", "wxid_seed02"
_DEV_EMOJI_SRC = (ROOT / "data" / "platforms" / _DEV_PLATFORM / _DEV_ACCOUNT_ID
                  / "emoji")
_DEV_EMOJI_DST = (Path(os.environ["PLATFORMS_DIR"]) / _DEV_PLATFORM
                  / _DEV_ACCOUNT_ID / "emoji")


def _materialise_dev_emoji() -> None:
    """把仓库里那份表情库**只读复制**进隔离根（offline 发送流程要它）。

    ``db.sticker_store.StickerStore`` 的 root 是 ``config.paths.platforms_dir``；
    ``PLATFORMS_DIR`` 改道以后，``registry.json`` 与 ``templates/*.png`` 得在隔离根
    里存在，offline 发送才解析得到表情（否则 send_sticker 直接死在"未找到表情包
    配置"）。刻意每次覆盖复制：仓库里的库重新整理过时，隔离根跟着更新——这正是
    ``_live_panel_mood()`` 说的"绑 LIVE registry 而不是某一次修订"。素材缺失时
    什么都不做，用例照旧 SKIP。
    """
    if not _DEV_EMOJI_SRC.is_dir():
        return
    _DEV_EMOJI_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_DEV_EMOJI_SRC, _DEV_EMOJI_DST, dirs_exist_ok=True)


_materialise_dev_emoji()

from core.execution import wx_vision  # noqa: E402

REFERENCE = ROOT / "_debug_click.png"                      # 1299x1537, panel closed
PANEL_OPEN = ROOT / "data" / "wechat" / "match_debug_live" / "panel_detection.png"  # 1618x1537
TEMPLATES = ROOT / "data" / "wechat" / "wxid_seed02" / "sticker_templates"

LIST_BG = (228, 228, 228)
CHAT_BG = (249, 249, 249)

# Template prefixes visible on the panel fixture (see the truth table in
# test_thumbnail_binding_assignment).  The offline send flow resolves its mood
# against the account's LIVE registry, so a hardcoded mood breaks every time
# the library is re-curated; picking a panel-visible mood keeps the test
# pinned to the flow instead of one library revision.
PANEL_TEMPLATE_PREFIXES = (
    "25db4471", "3b063de8", "5df1cdaa", "c97c6c9b",
    "15b2bff3", "e302850b", "f56e06c2", "44399fbd",
)


def _live_panel_mood() -> str:
    registry = (ROOT / "data" / "platforms" / "wechat" / "wxid_seed02"
                / "emoji" / "registry.json")
    try:
        import json

        raw = json.loads(registry.read_text(encoding="utf-8"))
    except Exception:
        return "调皮"
    for mood, entry in (raw or {}).items():
        template = str((entry or {}).get("template") or "")
        if any(prefix in template for prefix in PANEL_TEMPLATE_PREFIXES):
            return mood
    return "调皮"


def _load(path):
    from PIL import Image

    if not path.exists():
        return None
    with Image.open(str(path)) as handle:
        return handle.convert("RGB")


def _insert_cols(img, x, count, colour):
    from PIL import Image

    out = Image.new("RGB", (img.size[0] + count, img.size[1]), colour)
    out.paste(img.crop((0, 0, x, img.size[1])), (0, 0))
    out.paste(img.crop((x, 0, img.size[0], img.size[1])), (x + count, 0))
    return out


def _remove_cols(img, x, count):
    from PIL import Image

    out = Image.new("RGB", (img.size[0] - count, img.size[1]))
    out.paste(img.crop((0, 0, x, img.size[1])), (0, 0))
    out.paste(img.crop((x + count, 0, img.size[0], img.size[1])), (x, 0))
    return out


def _remove_rows(img, y, count):
    from PIL import Image

    out = Image.new("RGB", (img.size[0], img.size[1] - count))
    out.paste(img.crop((0, 0, img.size[0], y)), (0, 0))
    out.paste(img.crop((0, y + count, img.size[0], img.size[1])), (0, y))
    return out


def test_layout_on_real_captures():
    """The pane edge and the emoji button are read off the real frames."""
    print("\n[真实截图上的布局识别]")
    ok = True
    expectations = [
        (REFERENCE, 526, (571, 1475)),
        (PANEL_OPEN, 469, (514, 1475)),
    ]
    for path, want_left, want_emoji in expectations:
        image = _load(path)
        if image is None:
            print("  SKIP (缺少 %s)" % path.name)
            continue
        layout = wx_vision.detect_layout(image)
        point = layout.emoji_point()
        good = (abs(layout.chat_left - want_left) <= 6 and point is not None
                and abs(point[0] - want_emoji[0]) <= 4
                and abs(point[1] - want_emoji[1]) <= 4)
        ok &= good
        print("  %s %s chat_left=%s emoji=%s" % (
            "✓" if good else "✗", path.name, layout.chat_left, point))
    return ok


def test_window_geometry_variations():
    """A moved, widened, narrowed or shortened window still resolves correctly.

    Real resizes keep the chrome at a fixed pixel size and stretch the flexible
    pane, so the variants insert or remove whole background bands rather than
    rescaling the image.
    """
    print("\n[窗口几何变化]")
    base = _load(REFERENCE)
    if base is None:
        print("  SKIP (缺少 %s)" % REFERENCE.name)
        return True

    cases = [
        ("原样", lambda: base.copy(), 526, (571, 1475)),
        ("窗口加宽 300px", lambda: _insert_cols(base, 1000, 300, CHAT_BG), 526, (571, 1475)),
        ("会话列表拖宽 120px", lambda: _insert_cols(base, 400, 120, LIST_BG), 646, (691, 1475)),
        ("会话列表拖窄 120px", lambda: _remove_cols(base, 400, 120), 406, (451, 1475)),
        ("窗口收窄 260px", lambda: _remove_cols(base, 900, 260), 526, (571, 1475)),
        ("窗口变矮 200px", lambda: _remove_rows(base, 600, 200), 526, (571, 1275)),
    ]
    ok = True
    for name, build, want_left, want_emoji in cases:
        layout = wx_vision.detect_layout(build())
        point = layout.emoji_point()
        good = (abs(layout.chat_left - want_left) <= 6 and point is not None
                and abs(point[0] - want_emoji[0]) <= 5
                and abs(point[1] - want_emoji[1]) <= 5)
        ok &= good
        print("  %s %-18s chat_left=%-5s emoji=%s" % (
            "✓" if good else "✗", name, layout.chat_left, point))
    return ok


def test_panel_structure():
    """The popup, its tab bar and the sticker lattice are all recovered."""
    print("\n[表情面板结构]")
    image = _load(PANEL_OPEN)
    if image is None:
        print("  SKIP (缺少 %s)" % PANEL_OPEN.name)
        return True

    layout = wx_vision.detect_layout(image)
    panel = wx_vision.detect_panel(image, layout)
    if panel is None:
        print("  ✗ 未检测到面板")
        return False

    cells, meta = wx_vision.detect_sticker_cells(wx_vision._as_rgb(image), panel)
    checks = [
        ("面板矩形", (panel.x0, panel.y0, panel.x1, panel.y1) == (166, 784, 863, 1447)),
        ("页签数量 = 3", len(panel.tabs) == 3),
        ("选中页签 = 收藏", panel.active_tab == 2),
        ("格子数量 = 8", len(cells) == 8),
        ("列数 = 5", meta.get("cols") == 5),
    ]
    ok = True
    for label, good in checks:
        ok &= good
        print("  %s %s" % ("✓" if good else "✗", label))

    closed = _load(REFERENCE)
    if closed is not None:
        none_found = wx_vision.detect_panel(closed, wx_vision.detect_layout(closed)) is None
        ok &= none_found
        print("  %s 面板关闭时不误报" % ("✓" if none_found else "✗"))
    return ok


def test_template_match_round_trip():
    """A rendered thumbnail is found exactly where it was placed."""
    print("\n[模板匹配往返]")
    if not TEMPLATES.exists():
        print("  SKIP (缺少模板目录)")
        return True

    import numpy as np
    import cv2

    paths = sorted(TEMPLATES.glob("*.png"))[:8]
    if len(paths) < 4:
        print("  SKIP (模板太少)")
        return True

    canvas = np.full((560, 700, 3), 255, np.uint8)
    placed = {}
    for index, path in enumerate(paths):
        template = wx_vision.load_template_rgb(path)
        if template is None:
            continue
        row, col = divmod(index, 5)
        thumb = cv2.resize(template, (106, 106), interpolation=cv2.INTER_AREA)
        cx, cy = 90 + col * 132, 90 + row * 132
        canvas[cy - 53:cy + 53, cx - 53:cx + 53] = thumb
        placed[path.name] = (cx, cy)

    panel = wx_vision.Panel(x0=0, y0=0, x1=700, y1=560,
                            content=(0, 0, 700, 500), source="synthetic")
    cells, meta = wx_vision.detect_sticker_cells(canvas, panel)

    ok = True
    for name, (want_x, want_y) in placed.items():
        match = wx_vision.find_sticker_in_panel(canvas, panel, [str(TEMPLATES / name)],
                                                cells=cells, grid_meta=meta, min_score=0.5)
        if match is None:
            ok = False
            print("  ✗ %s 未匹配" % name[:12])
            continue
        error = ((match.point[0] - want_x) ** 2 + (match.point[1] - want_y) ** 2) ** 0.5
        good = error <= 6
        ok &= good
        print("  %s %s score=%.3f err=%.1fpx" % ("✓" if good else "✗", name[:12], match.score, error))
    return ok


def test_match_rejects_when_absent():
    """A sticker that is not on screen must not be confidently matched."""
    print("\n[面板里没有目标时应拒绝]")
    image = _load(PANEL_OPEN)
    if image is None or not TEMPLATES.exists():
        print("  SKIP (缺少素材)")
        return True

    layout = wx_vision.detect_layout(image)
    panel = wx_vision.detect_panel(image, layout)
    cells, meta = wx_vision.detect_sticker_cells(wx_vision._as_rgb(image), panel)

    # A blank white image shares no structure with any sticker on the panel.
    import numpy as np

    blank = np.full((240, 240, 3), 255, np.uint8)
    from PIL import Image as PILImage

    scratch = ROOT / "data" / "wechat" / "_test_blank.png"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    PILImage.fromarray(blank).save(str(scratch))
    try:
        match = wx_vision.find_sticker_in_panel(wx_vision._as_rgb(image), panel, [str(scratch)],
                                                cells=cells, grid_meta=meta, min_score=0.45)
    finally:
        try:
            scratch.unlink()
        except Exception:
            pass

    good = match is None or match.score < 0.45
    print("  %s 空白模板得分 %s（应低于 0.45）" % (
        "✓" if good else "✗", "无" if match is None else round(match.score, 3)))
    return good


def test_frame_plausibility_rejects_non_wechat():
    """A grab that is not WeChat must never be treated as one.

    This is the guard that stops the automation typing a contact name into, or
    clicking inside, whatever application happens to be painted over WeChat's
    window rectangle.
    """
    print("\n[画面真伪判据]")
    image = _load(REFERENCE)
    if image is None:
        print("  SKIP (缺少 %s)" % REFERENCE.name)
        return True

    ok = True

    good = wx_vision.detect_layout(image)
    looks = wx_vision.frame_looks_like_wechat(good)
    ok &= looks
    print("  %s 真实微信截图判为真" % ("✓" if looks else "✗"))

    # Erasing the input toolbar removes the emoji anchor.
    from PIL import ImageDraw

    no_toolbar = image.copy()
    ImageDraw.Draw(no_toolbar).rectangle(
        [0, int(image.size[1] * 0.90), image.size[0], image.size[1]], fill=(247, 247, 247))
    layout = wx_vision.detect_layout(no_toolbar)
    rejected = not wx_vision.frame_looks_like_wechat(layout)
    ok &= rejected
    print("  %s 抹掉工具条后判为假 (emoji=%s)" % ("✓" if rejected else "✗", layout.emoji_point()))

    # Images that are definitely not a WeChat window.
    negatives = [
        ROOT / "data" / "wechat" / "match_debug_live" / "panel_last.png",
        ROOT / "data" / "wechat" / "debug_capture" / "panel_page0.png",
        ROOT / "data" / "wechat" / "debug_capture" / "grid_page0.png",
    ]
    for path in negatives:
        candidate = _load(path)
        if candidate is None:
            continue
        verdict = wx_vision.frame_looks_like_wechat(wx_vision.detect_layout(candidate))
        ok &= not verdict
        print("  %s %s 判为%s" % ("✓" if not verdict else "✗", path.name,
                                  "真" if verdict else "假"))
    return ok


def test_thumbnail_binding_assignment():
    """Each mood is paired with its own panel crop, never a shared one.

    Ground truth was established visually: the eight stickers on the reference
    panel correspond one-to-one with the eight source templates in the account
    index, in this order.
    """
    print("\n[缩略图绑定分配]")
    image = _load(PANEL_OPEN)
    if image is None or not TEMPLATES.exists():
        print("  SKIP (缺少素材)")
        return True

    from core.execution.wx_sticker_sender import assign_thumbs

    layout = wx_vision.detect_layout(image)
    panel = wx_vision.detect_panel(image, layout)
    cells, _meta = wx_vision.detect_sticker_cells(wx_vision._as_rgb(image), panel)

    array = wx_vision._as_rgb(image)
    gallery = []
    for cell in cells:
        gallery.append({"path": "cell_r%dc%d" % (cell.row, cell.col),
                        "_array": array[cell.y0:cell.y1, cell.x0:cell.x1]})

    # (row, col) -> template prefix, taken from the verified match matrix
    truth = {
        (0, 0): "25db4471", (0, 1): "3b063de8", (0, 2): "5df1cdaa", (0, 3): "c97c6c9b",
        (0, 4): "15b2bff3", (1, 0): "e302850b", (1, 1): "f56e06c2", (1, 2): "44399fbd",
    }

    index = {}
    for path in sorted(TEMPLATES.glob("*.png")):
        if path.name[:8] in truth.values():
            index[path.stem] = {"template": str(path), "tab": "自定义"}

    assignment = assign_thumbs(index, gallery, min_score=0.5)
    ok = True
    if len(assignment) != len(truth):
        print("  ✗ 只绑定了 %d/%d 个" % (len(assignment), len(truth)))
        ok = False

    claimed = set()
    for mood, item in sorted(assignment.items()):
        cell = cells[item["gallery"]]
        want = truth.get((cell.row, cell.col))
        good = want is not None and mood.startswith(want)
        unique = item["gallery"] not in claimed
        claimed.add(item["gallery"])
        ok &= good and unique
        print("  %s %s -> (r%dc%d) 期望 %s  score=%.3f" % (
            "✓" if good and unique else "✗", mood[:8], cell.row, cell.col, want, item["score"]))
    return ok


def test_send_sticker_flow_offline():
    """The whole send_sticker flow executes against synthetic frames."""
    print("\n[发送流程离线演练]")
    closed = _load(PANEL_OPEN)
    if closed is None or not TEMPLATES.exists():
        print("  SKIP (缺少素材)")
        return True

    from PIL import ImageDraw

    panel_open = closed
    panel_box = (166, 784, 863, 1393)
    closed_frame = panel_open.copy()
    draw = ImageDraw.Draw(closed_frame)
    draw.rectangle(panel_box, fill=CHAT_BG)
    draw.rectangle([panel_box[0], panel_box[1], 469, panel_box[3]], fill=LIST_BG)

    sent_frame = closed_frame.copy()
    ImageDraw.Draw(sent_frame).rectangle([760, 560, 1040, 700], fill=(149, 236, 105))

    state = {"phase": "closed", "clicks": []}

    from core.execution import wx_human, wx_sticker_sender
    from core.execution.wx_vision import WindowInfo

    saved = {
        "find": wx_vision.find_wechat_window,
        "capture": wx_vision.capture_window,
        "activate": wx_vision.activate_window,
        "click": wx_human.click,
        "scroll": wx_human.scroll,
        "key": wx_human.key_press,
        "enter_chat": wx_sticker_sender._enter_chat,
    }

    def fake_capture_window(win):
        frame = {"closed": closed_frame, "open": panel_open, "sent": sent_frame}[state["phase"]]
        return frame.copy(), (0, 0, 1618, 1537)

    def fake_click(x, y, **kwargs):
        state["clicks"].append((x, y))
        if state["phase"] == "closed":
            state["phase"] = "open"
        elif state["phase"] == "open":
            state["phase"] = "sent"
        return x, y

    try:
        wx_vision.find_wechat_window = lambda **kw: WindowInfo(
            hwnd=1, left=0, top=0, right=1618, bottom=1537, title="微信",
            cls="Qt51514QWindowIcon", exe="weixin.exe", pid=1, dpi=144)
        wx_vision.capture_window = fake_capture_window
        wx_vision.activate_window = lambda win, **kw: True
        wx_human.click = fake_click
        wx_human.scroll = lambda *a, **k: None
        wx_human.key_press = lambda *a, **k: None
        wx_human.set_profile(speed=200.0, think_ms=(0, 0), pre_click_ms=(0, 0),
                             click_hold_ms=(0, 0), key_hold_ms=(0, 0))
        wx_sticker_sender._enter_chat = lambda win, who: (True, "")

        mood = _live_panel_mood()
        result = wx_sticker_sender.send_sticker("Elaina", mood, platform="wechat",
                                                account_id="wxid_seed02")
        live_clicks = list(state["clicks"])

        # Rehearsal mode must stop before the sticker click: this is what makes
        # it safe to try against a real person or a real group, so it is worth
        # pinning down.  Only the emoji button may be pressed.
        state["phase"] = "closed"
        state["clicks"] = []
        dry = wx_sticker_sender.send_sticker("Elaina", mood, platform="wechat",
                                             account_id="wxid_seed02", dry_run=True)
        dry_clicks = list(state["clicks"])
    finally:
        wx_vision.find_wechat_window = saved["find"]
        wx_vision.capture_window = saved["capture"]
        wx_vision.activate_window = saved["activate"]
        wx_human.click = saved["click"]
        wx_human.scroll = saved["scroll"]
        wx_human.key_press = saved["key"]
        wx_sticker_sender._enter_chat = saved["enter_chat"]

    send_ok = bool(result.get("ok")) and len(live_clicks) == 2
    print("  %s 真实发送 ok=%s clicks=%s cell=%s score=%s" % (
        "✓" if send_ok else "✗", result.get("ok"), live_clicks,
        result.get("cell"), result.get("score")))
    if not send_ok:
        print("     error: %s" % result.get("error"))

    dry_ok = (bool(dry.get("ok")) and dry.get("dry_run") is True
              and len(dry_clicks) == 1 and dry.get("would_click_screen"))
    print("  %s 演练模式 ok=%s clicks=%s 停在点击前=%s" % (
        "✓" if dry_ok else "✗", dry.get("ok"), dry_clicks,
        bool(dry.get("would_click_screen"))))
    if not dry_ok:
        print("     error: %s" % dry.get("error"))

    return send_ok and dry_ok




def test_text_sender_path_is_reachable():
    """WxSender's own methods must run without a NameError.

    The sticker tests all go through wx_chat, so when a refactor moved a helper
    out of wx_sender and forgot one alias, every test still passed while text
    sending was completely dead.  This drives the text path directly.
    """
    print("\n[文字发送路径可达性]")
    from core.execution import wx_chat, wx_human
    from core.execution.wx_sender import WxSender

    saved = {
        "find": wx_vision.find_wechat_window,
        "activate": wx_vision.activate_window,
        "claim": wx_vision.claim_window_frame,
        "foreground": wx_vision.foreground_hwnd,
        "open_chat": wx_chat.open_chat,
        "verify": wx_chat.text_verify_on_open,
        "guard": wx_chat.guard_active_chat,
        # 2026-11-22：打字前的硬闸现在会调 verify_active_chat_title；
        # 这条用例只验「文本路径可达」，标题用替身直接放行。
        "title": wx_chat.verify_active_chat_title,
    }
    win = wx_vision.WindowInfo(hwnd=7, left=0, top=0, right=1618, bottom=1537,
                               title="微信", exe="weixin.exe", pid=1, dpi=144)
    calls = []
    try:
        wx_vision.find_wechat_window = lambda **kw: win
        wx_vision.activate_window = lambda w, **kw: True
        wx_vision.claim_window_frame = lambda w, **kw: (None, None)
        wx_vision.foreground_hwnd = lambda: 7
        wx_chat.open_chat = lambda who, **kw: calls.append(("open_chat", who)) or {"ok": True}
        wx_chat.text_verify_on_open = lambda: False
        wx_chat.guard_active_chat = lambda *a, **k: None
        wx_chat.verify_active_chat_title = lambda *a, **k: (True, "文件传输助手")
        wx_human.paste_text = lambda text, **kw: calls.append(("paste", text)) or True
        wx_human.key_press = lambda *a, **k: calls.append(("key", a[0] if a else None))

        sender = WxSender.__new__(WxSender)
        sender._send_key = "enter"  # 2026-11-22：__new__ 绕过了 __init__
        sender._min_interval = 0.0
        sender._last_send_at = 0.0
        sender._send_lock = __import__("threading").Lock()
        sender._connected = True
        sender._ocr_verify = True

        connected = sender.is_connected
        result = sender.send_text("文件传输助手", "测试")
    finally:
        wx_vision.find_wechat_window = saved["find"]
        wx_vision.activate_window = saved["activate"]
        wx_vision.claim_window_frame = saved["claim"]
        wx_vision.foreground_hwnd = saved["foreground"]
        wx_chat.open_chat = saved["open_chat"]
        wx_chat.text_verify_on_open = saved["verify"]
        wx_chat.guard_active_chat = saved["guard"]

    ok = connected is True and isinstance(result, dict) and "error" not in result
    print("  %s is_connected=%s send_text=%s 调用链=%s" % (
        "✓" if ok else "✗", connected, result.get("ok"), calls))
    if not ok:
        print("     error: %s" % result.get("error"))
    return ok

def test_active_chat_memory_round_trip():
    """The reuse memory answers only while the exact same header is on screen."""
    print("\n[会话复用记忆]")
    from core.execution import wx_chat
    from core.execution.wx_vision import WindowInfo

    base = _load(PANEL_OPEN)
    if base is None:
        print("  SKIP (缺少素材)")
        return True
    layout = wx_vision.detect_layout(base)
    win = WindowInfo(hwnd=11, left=0, top=0, right=base.size[0], bottom=base.size[1],
                     title="微信", exe="weixin.exe", pid=99, dpi=144)

    saved = {
        "claim": wx_vision.claim_window_frame,
        "looks": wx_vision.frame_looks_like_wechat,
        "foreground": wx_chat.ensure_foreground,
        "enabled": wx_chat._ACTIVE_ENABLED,
        "ocr": wx_vision.ocr_items,
    }
    try:
        wx_vision.frame_looks_like_wechat = lambda lay: True
        wx_chat.ensure_foreground = lambda: win.hwnd
        wx_chat.invalidate_active_chat("test_setup")

        wx_vision.claim_window_frame = lambda w, **kw: (base.copy(), layout)
        wx_chat.remember_active_chat("Elaina", "wxid_seed01", win, base, layout,
                                     title_verified=True)
        hit = wx_chat.try_reuse_chat("Elaina", raw_who="wxid_seed01", win=win)
        other = wx_chat.try_reuse_chat("别人", raw_who="wxid_other", win=win)
        verified_seen = wx_chat.try_reuse_chat("Elaina", raw_who="wxid_seed01", win=win)
        guard_ok = wx_chat.match_active_chat("Elaina", "wxid_seed01", win, base, layout)

        changed = base.copy()
        hx, hy, _hw, _hh = layout.header
        changed.paste((255, 0, 0), (hx + 3, hy + 3, hx + 23, hy + 23))
        # 2026-11-22：像素变了不再直接 abort（未读角标/滚动都会变），要 OCR 复核标题。
        # 让 OCR 读回另一个名字，才是「真的换了会话」的那种 header 变化。
        wx_vision.ocr_items = lambda *a, **k: [{"text": "别人"}]
        guard_changed = wx_chat.match_active_chat("Elaina", "wxid_seed01", win, changed, layout)

        wx_chat.remember_active_chat("Elaina", "wxid_seed01", win, base, layout,
                                     title_verified=True)
        wx_vision.claim_window_frame = lambda w, **kw: (changed, layout)
        pixel_miss = wx_chat.try_reuse_chat("Elaina", raw_who="wxid_seed01", win=win)

        wx_vision.claim_window_frame = lambda w, **kw: (base.copy(), layout)
        wx_chat.remember_active_chat("Elaina", "wxid_seed01", win, base, layout,
                                     title_verified=False)
        denied = wx_chat.try_reuse_chat("Elaina", raw_who="wxid_seed01", win=win)
        allowed = wx_chat.try_reuse_chat("Elaina", raw_who="wxid_seed01", win=win,
                                         allow_unverified=True)

        wx_chat._ACTIVE_ENABLED = False
        disabled_hit = wx_chat.try_reuse_chat("Elaina", raw_who="wxid_seed01", win=win)
        disabled_remember = wx_chat.remember_active_chat(
            "Elaina", "wxid_seed01", win, base, layout, title_verified=True)
        wx_chat._ACTIVE_ENABLED = saved["enabled"]
    finally:
        wx_vision.claim_window_frame = saved["claim"]
        wx_vision.frame_looks_like_wechat = saved["looks"]
        wx_vision.ocr_items = saved["ocr"]
        wx_chat.ensure_foreground = saved["foreground"]
        wx_chat._ACTIVE_ENABLED = saved["enabled"]
        wx_chat.invalidate_active_chat("test_teardown")

    checks = [
        ("同一 header 命中复用", bool(hit and hit.get("reused"))),
        ("不同 who 不复用", other is None),
        ("已校验条目可直接复用", verified_seen is not None),
        ("点击前守卫放行", guard_ok is True),
        ("header 变化守卫中止", guard_changed is False),
        ("pixel 不一致不复用", pixel_miss is None),
        ("未校验条目需显式允许", denied is None and allowed is not None),
        ("kill switch 关闭复用", disabled_hit is None and disabled_remember is False),
    ]
    ok = True
    for name, good in checks:
        ok &= good
        print("  %s %s" % ("✓" if good else "✗", name))
    return ok


def test_send_sticker_reuses_active_chat_offline():
    """A remembered, title-verified chat skips search and title OCR entirely."""
    print("\n[发送流程会话复用]")
    from PIL import ImageDraw

    from core.execution import wx_chat, wx_human, wx_sticker_sender
    from core.execution.wx_vision import WindowInfo

    panel_open = _load(PANEL_OPEN)
    if panel_open is None or not TEMPLATES.exists():
        print("  SKIP (缺少素材)")
        return True

    panel_box = (166, 784, 863, 1393)
    closed_frame = panel_open.copy()
    draw = ImageDraw.Draw(closed_frame)
    draw.rectangle(panel_box, fill=CHAT_BG)
    draw.rectangle([panel_box[0], panel_box[1], 469, panel_box[3]], fill=LIST_BG)
    sent_frame = closed_frame.copy()
    ImageDraw.Draw(sent_frame).rectangle([760, 560, 1040, 700], fill=(149, 236, 105))

    state = {"phase": "closed", "clicks": [], "entered": 0}
    win = WindowInfo(hwnd=1, left=0, top=0, right=1618, bottom=1537, title="微信",
                     cls="Qt51514QWindowIcon", exe="weixin.exe", pid=1, dpi=144)
    win.is_foreground = lambda: True

    saved = {
        "find": wx_vision.find_wechat_window,
        "capture": wx_vision.capture_window,
        "activate": wx_vision.activate_window,
        "claim": wx_vision.claim_window_frame,
        "click": wx_human.click,
        "scroll": wx_human.scroll,
        "key": wx_human.key_press,
        "enter_chat": wx_sticker_sender._enter_chat,
    }

    def fake_capture_window(w):
        frame = {"closed": closed_frame, "open": panel_open, "sent": sent_frame}[state["phase"]]
        return frame.copy(), (0, 0, 1618, 1537)

    def fake_click(x, y, **kwargs):
        state["clicks"].append((x, y))
        if state["phase"] == "closed":
            state["phase"] = "open"
        elif state["phase"] == "open":
            state["phase"] = "sent"
        return x, y

    def enter_should_not_run(win_obj, who):
        state["entered"] += 1
        return True, ""

    try:
        wx_chat.invalidate_active_chat("test_setup")
        wx_vision.find_wechat_window = lambda **kw: win
        wx_vision.capture_window = fake_capture_window
        wx_vision.activate_window = lambda w, **kw: True
        wx_vision.claim_window_frame = lambda w, **kw: (
            closed_frame.copy(), wx_vision.detect_layout(closed_frame))
        wx_human.click = fake_click
        wx_human.scroll = lambda *a, **k: None
        wx_human.key_press = lambda *a, **k: None
        wx_human.set_profile(speed=200.0, think_ms=(0, 0), pre_click_ms=(0, 0),
                             click_hold_ms=(0, 0), key_hold_ms=(0, 0))
        wx_sticker_sender._enter_chat = enter_should_not_run

        layout = wx_vision.detect_layout(closed_frame)
        wx_chat.remember_active_chat("Elaina", "Elaina", win, closed_frame, layout,
                                     title_verified=True)
        result = wx_sticker_sender.send_sticker("Elaina", _live_panel_mood(),
                                                platform="wechat",
                                                account_id="wxid_seed02")
    finally:
        wx_vision.find_wechat_window = saved["find"]
        wx_vision.capture_window = saved["capture"]
        wx_vision.activate_window = saved["activate"]
        wx_vision.claim_window_frame = saved["claim"]
        wx_human.click = saved["click"]
        wx_human.scroll = saved["scroll"]
        wx_human.key_press = saved["key"]
        wx_sticker_sender._enter_chat = saved["enter_chat"]
        wx_chat.invalidate_active_chat("test_teardown")

    ok = bool(result.get("ok")) and state["entered"] == 0 and len(state["clicks"]) == 2
    print("  %s 复用发送 ok=%s 搜索次数=%d clicks=%s score=%s" % (
        "✓" if ok else "✗", result.get("ok"), state["entered"], state["clicks"],
        result.get("score")))
    if not ok:
        print("     error: %s" % result.get("error"))
    return ok


def test_unreadable_title_on_reuse_falls_back_to_search():
    """An unverified reuse without a readable title must run the full search."""
    print("\n[复用标题不可读回退]")
    from PIL import ImageDraw

    from core.execution import wx_chat, wx_human, wx_sticker_sender
    from core.execution.wx_vision import WindowInfo

    panel_open = _load(PANEL_OPEN)
    if panel_open is None or not TEMPLATES.exists():
        print("  SKIP (缺少素材)")
        return True

    panel_box = (166, 784, 863, 1393)
    closed_frame = panel_open.copy()
    draw = ImageDraw.Draw(closed_frame)
    draw.rectangle(panel_box, fill=CHAT_BG)
    draw.rectangle([panel_box[0], panel_box[1], 469, panel_box[3]], fill=LIST_BG)
    sent_frame = closed_frame.copy()
    ImageDraw.Draw(sent_frame).rectangle([760, 560, 1040, 700], fill=(149, 236, 105))

    state = {"phase": "closed", "clicks": [], "entered": 0}
    win = WindowInfo(hwnd=1, left=0, top=0, right=1618, bottom=1537, title="微信",
                     cls="Qt51514QWindowIcon", exe="weixin.exe", pid=1, dpi=144)
    win.is_foreground = lambda: True

    saved = {
        "find": wx_vision.find_wechat_window,
        "capture": wx_vision.capture_window,
        "activate": wx_vision.activate_window,
        "claim": wx_vision.claim_window_frame,
        "click": wx_human.click,
        "scroll": wx_human.scroll,
        "key": wx_human.key_press,
        "enter_chat": wx_sticker_sender._enter_chat,
        "read_title": wx_sticker_sender._read_chat_title,
    }

    def fake_capture_window(w):
        frame = {"closed": closed_frame, "open": panel_open, "sent": sent_frame}[state["phase"]]
        return frame.copy(), (0, 0, 1618, 1537)

    def fake_click(x, y, **kwargs):
        state["clicks"].append((x, y))
        if state["phase"] == "closed":
            state["phase"] = "open"
        elif state["phase"] == "open":
            state["phase"] = "sent"
        return x, y

    def enter_chat(win_obj, who):
        state["entered"] += 1
        return True, ""

    try:
        wx_chat.invalidate_active_chat("test_setup")
        wx_vision.find_wechat_window = lambda **kw: win
        wx_vision.capture_window = fake_capture_window
        wx_vision.activate_window = lambda w, **kw: True
        wx_vision.claim_window_frame = lambda w, **kw: (
            closed_frame.copy(), wx_vision.detect_layout(closed_frame))
        wx_human.click = fake_click
        wx_human.scroll = lambda *a, **k: None
        wx_human.key_press = lambda *a, **k: None
        wx_human.set_profile(speed=200.0, think_ms=(0, 0), pre_click_ms=(0, 0),
                             click_hold_ms=(0, 0), key_hold_ms=(0, 0))
        wx_sticker_sender._enter_chat = enter_chat
        wx_sticker_sender._read_chat_title = lambda sess, layout: ""

        layout = wx_vision.detect_layout(closed_frame)
        # Unverified: this is what a text-only open leaves behind.
        wx_chat.remember_active_chat("Elaina", "Elaina", win, closed_frame, layout,
                                     title_verified=False)
        result = wx_sticker_sender.send_sticker("Elaina", _live_panel_mood(),
                                                platform="wechat",
                                                account_id="wxid_seed02")
    finally:
        wx_vision.find_wechat_window = saved["find"]
        wx_vision.capture_window = saved["capture"]
        wx_vision.activate_window = saved["activate"]
        wx_vision.claim_window_frame = saved["claim"]
        wx_human.click = saved["click"]
        wx_human.scroll = saved["scroll"]
        wx_human.key_press = saved["key"]
        wx_sticker_sender._enter_chat = saved["enter_chat"]
        wx_sticker_sender._read_chat_title = saved["read_title"]
        wx_chat.invalidate_active_chat("test_teardown")

    ok = bool(result.get("ok")) and state["entered"] == 1 and len(state["clicks"]) == 2
    print("  %s 回退发送 ok=%s 搜索次数=%d clicks=%s" % (
        "✓" if ok else "✗", result.get("ok"), state["entered"], state["clicks"]))
    if not ok:
        print("     error: %s" % result.get("error"))
    return ok


def test_await_panel_accepts_tabless_panel_quickly():
    """A usable panel without a tab bar must not burn the 3s timeout."""
    print("\n[无页签面板等待]")
    import time as _time
    from types import SimpleNamespace

    from core.execution import wx_human, wx_sticker_sender

    class FakeSession:
        def __init__(self):
            self.frame = object()
            self.grabs = 0

        def grab(self):
            self.grabs += 1
            self.frame = object()
            return self.frame

    saved = {
        "panel": wx_vision.detect_panel,
        "diff": wx_vision.detect_panel_by_diff,
        "sleep": wx_human.sleep_human,
        "grace": wx_sticker_sender._PANEL_TAB_GRACE_S,
    }
    results = []
    try:
        wx_vision.detect_panel = lambda frame, layout: SimpleNamespace(tabs=[])
        wx_vision.detect_panel_by_diff = lambda before, after, layout: None
        wx_human.sleep_human = lambda *a, **k: _time.sleep(0.03)
        wx_sticker_sender._PANEL_TAB_GRACE_S = 0.12
        sess = FakeSession()
        started = _time.perf_counter()
        tabless = wx_sticker_sender._await_panel(sess, object(), object())
        tabless_elapsed = _time.perf_counter() - started
        results.append(("无页签面板短宽限期返回", tabless is not None and tabless_elapsed < 1.0))

        wx_vision.detect_panel = lambda frame, layout: SimpleNamespace(tabs=[1, 2])
        started = _time.perf_counter()
        with_tabs = wx_sticker_sender._await_panel(FakeSession(), object(), object())
        tabs_elapsed = _time.perf_counter() - started
        results.append(("有页签面板立即返回", with_tabs is not None and tabs_elapsed < 0.2))
    finally:
        wx_vision.detect_panel = saved["panel"]
        wx_vision.detect_panel_by_diff = saved["diff"]
        wx_human.sleep_human = saved["sleep"]
        wx_sticker_sender._PANEL_TAB_GRACE_S = saved["grace"]

    ok = True
    for name, good in results:
        ok &= good
        print("  %s %s" % ("✓" if good else "✗", name))
    print("  无页签耗时 %.2fs（旧行为 3.0s，有页签 %.2fs）" % (tabless_elapsed, tabs_elapsed))
    return ok


def test_text_send_reuse_guard_aborts():
    """A reused text send must not press Enter when the chat changed.

    护栏只中止，不再清空/改动输入框（主人要求移除发送前检查与删除内容代码）。
    """
    print("\n[文本复用发送前守卫]")
    import threading

    from core.execution import wx_chat, wx_human, wx_sender
    from core.execution.wx_sender import WxSender

    state = {"cleared": 0, "keys": []}
    saved = {
        "ensure": wx_sender.ensure_foreground,
        "open": wx_chat.open_chat,
        "guard": wx_chat.guard_active_chat,
        "window": wx_chat._wechat_window,
        "clear": wx_chat._clear_focused_field,
        "paste": wx_human.paste_text,
        "key": wx_human.key_press,
    }

    class FakeWin:
        hwnd = 7

        def is_foreground(self):
            return True

    try:
        wx_sender.ensure_foreground = lambda: 7
        wx_chat.open_chat = lambda who, **kw: {"ok": True, "reused": True}
        wx_chat.guard_active_chat = lambda *a, **k: False
        wx_chat._wechat_window = lambda **kw: FakeWin()
        wx_chat._clear_focused_field = lambda: state.__setitem__("cleared", state["cleared"] + 1)
        wx_human.paste_text = lambda text, **kw: True
        wx_human.key_press = lambda *a, **k: state["keys"].append(a[0] if a else None)

        sender = WxSender.__new__(WxSender)
        sender._send_key = "enter"  # 2026-11-22：__new__ 绕过了 __init__
        sender._min_interval = 0.0
        sender._last_send_at = 0.0
        sender._send_lock = threading.Lock()
        sender._connected = True
        sender._ocr_verify = True

        result = sender.send_text("文件传输助手", "测试")
    finally:
        wx_sender.ensure_foreground = saved["ensure"]
        wx_chat.open_chat = saved["open"]
        wx_chat.guard_active_chat = saved["guard"]
        wx_chat._wechat_window = saved["window"]
        wx_chat._clear_focused_field = saved["clear"]
        wx_human.paste_text = saved["paste"]
        wx_human.key_press = saved["key"]

    ok = (not result.get("ok") and state["cleared"] == 0 and not state["keys"])
    print("  %s 已中止=%s 输入框被改动=%d Enter次数=%d" % (
        "✓" if ok else "✗", not result.get("ok"), state["cleared"], len(state["keys"])))
    if not ok:
        print("     result=%s" % result)
    return ok


def test_tool_markup_is_not_sent_as_text():
    """Raw tool-call syntax must never reach a chat as a message."""
    print("\n[工具调用泄漏防护]")
    from agents.base import sanitize_tool_markup

    leaked = ('@Elaina send_sticker({"sticker": "日常", "who": "10000000001@chatroom", '
              '"platform": "wechat"})')
    normal = "好呀，今天也陪你聊～"
    mixed = normal + "\n" + leaked

    checks = [
        ("整条结构化调用被剥离", sanitize_tool_markup(leaked) == ""),
        ("正常文本保持不变", sanitize_tool_markup(normal) == normal),
        ("混合内容只留人话", sanitize_tool_markup(mixed) == normal),
    ]
    ok = True
    for name, good in checks:
        ok &= good
        print("  %s %s" % ("✓" if good else "✗", name))
    return ok


def _ensure_active_persona() -> None:
    """隔离 meta 库是空的，先给它一条 active persona。

    ``SendStickerTool`` 走 ``core.speech.turn.snapshot_from_context`` →
    ``PersonaResolver``，没有 active persona 就直接
    ``RuntimeError: no active persona available for speech resolution``（生产库里
    那条是 Web 端建的，迁移链不建；隔离库自然没有）。这里用 **owner 侧 API** 建一条
    再跑一次 ``SpeechBootstrap.ensure_seed()``（口径与 ``init_singletons`` 启动时
    那一次相同），不碰 ``get_repo`` / 不替身任何被测对象——本用例判的还是工具自己
    把 ``who`` 填成当前会话、并把表情放进待发队列。
    """
    from core.bootstrap import get_meta_repo
    from core.speech.bootstrap import SpeechBootstrap
    from core.speech.store import SpeechStore

    meta = get_meta_repo()
    if meta.personas.list_active():
        return
    persona = meta.personas.upsert_from_dict({"name": "隔离用例人设", "is_active": 1})
    SpeechBootstrap(SpeechStore(meta.engine), meta).ensure_seed()
    print("  [隔离] meta 库无 active persona，已建 id=%s" % getattr(persona, "id", "?"))


def test_send_sticker_tool_uses_current_context():
    """send_sticker must auto-fill who and defer to after the text reply."""
    print("\n[send_sticker 会话上下文]")
    import asyncio

    from agents.base import AgentContext
    from agents.tools.send_sticker import SendStickerTool

    _ensure_active_persona()

    from core.routing.turn_decision import TurnDecision

    ctx = AgentContext(
        extra={
            "chat_id": "10000000001@chatroom",
            "account_id": "wxid_seed02",
        },
        turn_decision=TurnDecision(
            action="reply_now", source="direct",
            defer_sticker_until_after_reply=True,
        ),
    )
    assert "defer_sticker_until_after_reply" not in (ctx.extra or {})
    ctx.accounts = {"wechat": "wxid_seed02"}

    tool = SendStickerTool()
    tool.set_context(ctx)

    async def run():
        return await tool.execute(emotion="日常")

    result = asyncio.run(run())
    pending = ctx.extra.get("_pending_stickers") or []

    checks = [
        ("不再报缺少 who", not result.is_error),
        ("表情包进入待发队列", len(pending) == 1),
        ("自动填当前群 ID", bool(pending) and pending[0].chat_id == "10000000001@chatroom"),
    ]
    ok = True
    for name, good in checks:
        ok &= good
        print("  %s %s" % ("✓" if good else "✗", name))
    if not ok:
        print("     result: %r" % result)
    return ok


def test_active_chat_memory_window_move_and_resize():
    """Moving keeps the memory valid; resizing must invalidate it."""
    print("\n[窗口移动/缩放下的会话复用]")
    from core.execution import wx_chat
    from core.execution.wx_vision import WindowInfo

    base = _load(PANEL_OPEN)
    if base is None:
        print("  SKIP (缺少素材)")
        return True
    layout = wx_vision.detect_layout(base)
    width, height = base.size

    moved = WindowInfo(hwnd=31, left=120, top=80, right=120 + width, bottom=80 + height,
                       title="微信", exe="weixin.exe", pid=77, dpi=144)
    resized_frame = _insert_cols(base, 400, 120, LIST_BG)
    resized_layout = wx_vision.detect_layout(resized_frame)
    resized_win = WindowInfo(hwnd=31, left=0, top=0,
                             right=resized_frame.size[0], bottom=resized_frame.size[1],
                             title="微信", exe="weixin.exe", pid=77, dpi=144)

    saved = {
        "claim": wx_vision.claim_window_frame,
        "looks": wx_vision.frame_looks_like_wechat,
        "foreground": wx_chat.ensure_foreground,
        "enabled": wx_chat._ACTIVE_ENABLED,
    }
    try:
        wx_vision.frame_looks_like_wechat = lambda lay: True
        wx_chat.ensure_foreground = lambda: 31
        wx_chat._ACTIVE_ENABLED = True
        wx_chat.invalidate_active_chat("test_setup")

        wx_vision.claim_window_frame = lambda w, **kw: (base.copy(), layout)
        wx_chat.remember_active_chat("Elaina", "wxid_seed01", moved, base, layout,
                                     title_verified=True)
        moved_hit = wx_chat.try_reuse_chat("Elaina", raw_who="wxid_seed01", win=moved)

        wx_chat.remember_active_chat("Elaina", "wxid_seed01", moved, base, layout,
                                     title_verified=True)
        wx_vision.claim_window_frame = lambda w, **kw: (
            resized_frame.copy(), resized_layout)
        resize_miss = wx_chat.try_reuse_chat("Elaina", raw_who="wxid_seed01", win=resized_win)
        cache_cleared = not wx_chat._ACTIVE_CHAT.get("at")
    finally:
        wx_vision.claim_window_frame = saved["claim"]
        wx_vision.frame_looks_like_wechat = saved["looks"]
        wx_chat.ensure_foreground = saved["foreground"]
        wx_chat._ACTIVE_ENABLED = saved["enabled"]
        wx_chat.invalidate_active_chat("test_teardown")

    checks = [
        ("只移动时仍然命中", bool(moved_hit and moved_hit.get("reused"))),
        ("缩放后复用失效", resize_miss is None),
        ("失效后缓存已清空", cache_cleared),
    ]
    ok = True
    for name, good in checks:
        ok &= good
        print("  %s %s" % ("✓" if good else "✗", name))
    return ok


def test_hunt_cache_key_depends_on_frame_geometry():
    """The cached sticker cell must be keyed by window/panel geometry."""
    print("\n[表情格缓存几何键]")
    from types import SimpleNamespace

    from core.execution import wx_sticker_sender

    panel = SimpleNamespace(x0=1, y0=2, x1=3, y1=4, content=(1, 2, 3, 4),
                            active_tab=2, tab_y=5)
    shifted = SimpleNamespace(x0=9, y0=2, x1=3, y1=4, content=(1, 2, 3, 4),
                              active_tab=2, tab_y=5)
    layout = SimpleNamespace(chat_left=469, toolbar_y=1475)
    k1 = wx_sticker_sender._hunt_geometry_key(panel, layout, (1298, 1537))
    k2 = wx_sticker_sender._hunt_geometry_key(panel, layout, (1298, 1337))
    k3 = wx_sticker_sender._hunt_geometry_key(shifted, layout, (1298, 1537))

    checks = [
        ("窗口高度变化会失效", k1 != k2),
        ("面板位置变化会失效", k1 != k3),
    ]
    ok = True
    for name, good in checks:
        ok &= good
        print("  %s %s" % ("✓" if good else "✗", name))
    return ok


def test_incremental_decrypt_skips_unchanged_sources():
    """WX_INCREMENTAL_DECRYPT reuses outputs while source signatures match."""
    print("\n[增量解密签名]")
    import shutil
    import uuid

    from db import decrypt as dec

    root = ROOT / "logs" / ("decrypt_test_" + uuid.uuid4().hex)
    src = root / "db_storage"
    out = root / "decrypted"
    src.mkdir(parents=True, exist_ok=True)
    # 2026-11-22：解密链 2026-10-27 起跳过 <64KB 的空壳库，替身文件太小会被当成空壳跳过。
    (src / "message_0.db").write_bytes(b"x" * 70000)
    (src / "contact.db").write_bytes(b"y" * 70000)

    calls = {"n": 0}
    saved = dec._decrypt_single_db

    def fake_decrypt(key, db_path, out_path, version=4):
        calls["n"] += 1
        Path(out_path).write_bytes(Path(db_path).read_bytes() + b"dec")
        return True

    try:
        dec._decrypt_single_db = fake_decrypt
        first = dec.decrypt_and_merge("k", str(src), str(out), incremental=True)
        first_calls = calls["n"]
        second = dec.decrypt_and_merge("k", str(src), str(out), incremental=True)
        second_calls = calls["n"] - first_calls
    finally:
        dec._decrypt_single_db = saved
        shutil.rmtree(root, ignore_errors=True)

    checks = [
        ("首次全量解密两个库", first_calls == 2),
        ("未变化时第二次不再解密", second_calls == 0),
        ("两次都返回合并结果", first is not None and second is not None),
    ]
    ok = True
    for name, good in checks:
        ok &= good
        print("  %s %s" % ("✓" if good else "✗", name))
    return ok


def test_ui_lock_serializes_senders():
    """Text and sticker automation must never overlap on the same mouse."""
    print("\n[微信 UI 全局锁]")
    import threading
    import time as _time

    from core.execution import wx_chat

    events = []

    @wx_chat.ui_locked
    def job(tag: str, delay: float):
        events.append((tag, "enter"))
        _time.sleep(delay)
        events.append((tag, "exit"))

    first = threading.Thread(target=job, args=("a", 0.06))
    second = threading.Thread(target=job, args=("b", 0.0))
    first.start()
    _time.sleep(0.01)
    second.start()
    first.join()
    second.join()

    active = None
    overlap = False
    for tag, action in events:
        if action == "enter":
            if active is not None:
                overlap = True
            active = tag
        else:
            if active != tag:
                overlap = True
            active = None
    ok = (not overlap and len(events) == 4 and active is None)
    print("  %s 两路发送无交叠: %s" % ("✓" if ok else "✗", events))
    return ok


def main():
    tests = [
        test_layout_on_real_captures,
        test_window_geometry_variations,
        test_panel_structure,
        test_template_match_round_trip,
        test_match_rejects_when_absent,
        test_frame_plausibility_rejects_non_wechat,
        test_thumbnail_binding_assignment,
        test_send_sticker_flow_offline,
        test_active_chat_memory_round_trip,
        test_send_sticker_reuses_active_chat_offline,
        test_unreadable_title_on_reuse_falls_back_to_search,
        test_await_panel_accepts_tabless_panel_quickly,
        test_text_sender_path_is_reachable,
        test_text_send_reuse_guard_aborts,
        test_tool_markup_is_not_sent_as_text,
        test_send_sticker_tool_uses_current_context,
        test_active_chat_memory_window_move_and_resize,
        test_hunt_cache_key_depends_on_frame_geometry,
        test_incremental_decrypt_skips_unchanged_sources,
        test_ui_lock_serializes_senders,
    ]
    results = []
    for test in tests:
        try:
            results.append((test.__name__, bool(test())))
        except Exception:  # pragma: no cover - diagnostics only
            import traceback

            traceback.print_exc()
            results.append((test.__name__, False))

    print("\n" + "=" * 60)
    for name, ok in results:
        print("  %s %s" % ("PASS" if ok else "FAIL", name))
    failed = [n for n, ok in results if not ok]
    print("=" * 60)
    print("总计: %d/%d 通过" % (len(results) - len(failed), len(results)))
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
