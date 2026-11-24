"""Command line diagnostics for the mouse-driven WeChat automation.

Run any subcommand with no arguments to see what it does:

    python -m core.execution.wx_diagnose status
    python -m core.execution.wx_diagnose probe-emoji
    python -m core.execution.wx_diagnose selftest
    python -m core.execution.wx_diagnose thumbs --account wxid_...
    python -m core.execution.wx_diagnose send --who 张三 --mood 调皮
    python -m core.execution.wx_diagnose mouse-test

Everything here is read-mostly: only probe-emoji, thumbs, send and mouse-test
actually move the mouse, and only after printing what they are about to do.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from core.execution import wx_human as human
from core.execution import wx_vision as vision
from safelog import warn

DEBUG_DIR = Path("data") / "wechat" / "diagnose"


def _say(message: str = "") -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        warn("core/execution/wx_diagnose.py:33 UnicodeEncodeError", err=repr(__import__("sys").exc_info()[1]))
        sys.stdout.write(message.encode("utf-8", "replace").decode("ascii", "replace") + chr(10))


def _dump(obj) -> None:
    _say(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _annotate(image, layout, panel=None):
    """Draw what the detector believes, so a human can confirm it at a glance."""
    from PIL import ImageDraw

    debug = image.copy()
    draw = ImageDraw.Draw(debug)

    draw.line([layout.chat_left, 0, layout.chat_left, layout.height], fill=(0, 170, 0), width=3)
    if layout.header:
        hx, hy, hw, hh = layout.header
        draw.rectangle([hx, hy, hx + hw, hy + hh], outline=(0, 170, 0), width=2)
    if layout.input_box:
        ix0, iy0, ix1, iy1 = layout.input_box
        draw.rectangle([ix0, iy0, ix1, iy1], outline=(255, 140, 0), width=2)

    for index, icon in enumerate(layout.icons):
        colour = (255, 0, 0) if index == 0 else (0, 120, 255)
        draw.rectangle([icon.x - 3, icon.y - 3, icon.x + icon.w + 3, icon.y + icon.h + 3],
                       outline=colour, width=2)
    if layout.emoji is not None:
        draw.ellipse([layout.emoji.cx - 6, layout.emoji.cy - 6,
                      layout.emoji.cx + 6, layout.emoji.cy + 6], fill=(255, 0, 0))

    if panel is not None:
        draw.rectangle([panel.x0, panel.y0, panel.x1, panel.y1], outline=(160, 0, 200), width=3)
        draw.rectangle(list(panel.content), outline=(255, 0, 255), width=2)
        for index, tab in enumerate(panel.tabs):
            colour = (255, 0, 0) if index == panel.active_tab else (0, 90, 200)
            draw.rectangle([tab.x - 4, tab.y - 4, tab.x + tab.w + 4, tab.y + tab.h + 4],
                           outline=colour, width=2)
    return debug


def cmd_status(args) -> int:
    win = vision.find_wechat_window()
    if win is None:
        _say("未找到微信窗口。请先启动并登录微信 PC 客户端。")
        return 2

    if args.activate:
        vision.activate_window(win)
        human.sleep_human(0.4)

    _say("微信窗口:")
    _dump(win.as_dict())

    foreground = vision.foreground_hwnd()
    occluded = foreground != win.hwnd
    _say("前台窗口句柄: %s（微信为 %s）" % (foreground, win.hwnd))
    if occluded and not args.activate:
        _say("注意: 微信当前不在前台，截屏抓到的可能是遮挡它的窗口。")
        _say("      加 --activate 会先把微信切到前台再识别。")

    image, rect = vision.capture_window(win)
    layout = vision.detect_layout(image)
    panel = vision.detect_panel(image, layout)

    _say("")
    _say("识别结果:")
    _dump({"chat_left": layout.chat_left, "toolbar_y": layout.toolbar_y,
           "emoji": layout.emoji_point(), "notes": layout.notes,
           "panel_open": panel is not None})

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    out = DEBUG_DIR / "status.png"
    _annotate(image, layout, panel).save(str(out))
    _say("")
    _say("标注图已保存: %s" % out)

    if not vision.frame_looks_like_wechat(layout):
        _say("警告: 这一帧不像微信的聊天界面（窗口可能被遮挡，或微信停在登录/设置页）。")
        return 3
    if layout.emoji is None:
        _say("警告: 没能定位表情按钮。请确认微信停在某个聊天界面。")
        return 3
    return 0


def cmd_probe_emoji(args) -> int:
    win = vision.find_wechat_window()
    if win is None:
        _say("未找到微信窗口。")
        return 2
    if not args.yes:
        image, layout = vision.claim_window_frame(win)
        if not vision.frame_looks_like_wechat(layout):
            _say("无法确认画面是微信聊天窗口（可能被其他窗口遮挡，或微信停在登录页）。")
            return 3
        point = layout.emoji_point()
        if point is None:
            _say("未定位到表情按钮。")
            return 3
        _say("将点击表情按钮: 窗口内坐标 %s -> 屏幕坐标 %s" % (point, win.to_screen(*point)))
        _say("（加 --yes 才会真的点击；现在只做检测）")
        return 0

    from core.execution.wx_sticker_sender import open_sticker_panel

    opened = open_sticker_panel(win)
    if not opened.get("ok"):
        _say("打开表情面板失败: %s" % opened.get("error"))
        return 4
    layout = opened["layout"]
    panel = opened["panel"]
    after = opened["session"].frame

    cells, meta = vision.detect_sticker_cells(vision._as_rgb(after), panel)
    _say("表情面板已打开:")
    _dump(panel.as_dict())
    _say("贴纸格子:")
    _dump(meta)

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    debug = _annotate(after, layout, panel)
    from PIL import ImageDraw

    draw = ImageDraw.Draw(debug)
    for cell in cells:
        draw.rectangle([cell.x0, cell.y0, cell.x1, cell.y1], outline=(255, 140, 0), width=2)
    out = DEBUG_DIR / "panel.png"
    debug.save(str(out))
    _say("标注图已保存: %s" % out)

    human.key_press(human.VK_ESCAPE)
    return 0


def cmd_selftest(args) -> int:
    if args.image:
        images = [Path(args.image)]
    elif args.dir:
        images = sorted(Path(args.dir).glob("*.png"))
    else:
        # Only whole-window captures are meaningful here; panel crops and
        # sticker art live in the same tree and would be false failures.
        images = [Path("_debug_click.png"),
                  Path("data/wechat/match_debug_live/panel_detection.png")]
    images = [p for p in images if p.exists()]
    if not images:
        _say("没有可用的截图。用 --image 指定一张，或先运行 capture。")
        return 2

    failures = 0
    for path in images:
        if not path.exists():
            continue
        from PIL import Image

        with Image.open(str(path)) as handle:
            image = handle.convert("RGB")
        layout = vision.detect_layout(image)
        panel = vision.detect_panel(image, layout)
        _say("%s  %dx%d" % (path, image.size[0], image.size[1]))
        _say("   chat_left=%s  emoji=%s  toolbar_y=%s  icons=%d" % (
            layout.chat_left, layout.emoji_point(), layout.toolbar_y, len(layout.icons)))
        if layout.emoji is None:
            _say("   ** 未找到表情按钮 **")
            failures += 1
        if panel is not None:
            cells, meta = vision.detect_sticker_cells(vision._as_rgb(image), panel)
            _say("   panel=%s tabs=%d active=%s cells=%d" % (
                (panel.x0, panel.y0, panel.x1, panel.y1), len(panel.tabs),
                panel.active_tab, len(cells)))
            if not cells:
                _say("   ** 面板打开但未识别到贴纸格子 **")
                failures += 1
        _say("")

    _say("自检失败项: %d" % failures)
    return 0 if failures == 0 else 1


def cmd_capture(args) -> int:
    win = vision.find_wechat_window()
    if win is None:
        _say("未找到微信窗口。")
        return 2
    image, rect = vision.capture_window(win)
    out = Path(args.out) if args.out else (DEBUG_DIR / ("capture_%d.png" % int(time.time())))
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(out))
    _say("已保存 %s  (%dx%d, 窗口矩形 %s)" % (out, image.size[0], image.size[1], list(rect)))
    return 0


def cmd_thumbs(args) -> int:
    from core.execution.wx_sticker_sender import bind_thumbs, capture_panel_thumbs

    result = capture_panel_thumbs(platform=args.platform, account_id=args.account,
                                  max_pages=args.pages)
    _dump({k: v for k, v in result.items() if k != "thumbs"})
    if not result.get("ok"):
        return 1

    _say("")
    _say("共保存 %d 张面板缩略图到 %s" % (result["count"], result["dir"]))

    bound = bind_thumbs(platform=args.platform, account_id=args.account)
    _say("")
    if bound.get("ok"):
        _say("已为 %d 个表情绑定面板缩略图（共采集 %d 张）:" % (bound["bound"], bound["gallery"]))
        for item in bound["applied"]:
            _say("   %-10s score=%.3f  %s" % (item["mood"], item["score"],
                                              Path(item["thumb"]).name))
        if bound.get("unbound"):
            _say("未绑定: %s（这些表情不在当前面板里，或相似度不足）" % ", ".join(bound["unbound"]))
        _say("已写入 %s" % bound["index"])
        _say("以后匹配会直接用像素级缩略图，速度和准确率都显著提高。")
    else:
        _dump(bound)
    return 0


def cmd_bind(args) -> int:
    from core.execution.wx_sticker_sender import bind_thumbs

    result = bind_thumbs(platform=args.platform, account_id=args.account)
    _dump({k: v for k, v in result.items() if k != "index"})
    return 0 if result.get("ok") else 1


def cmd_send(args) -> int:
    from core.execution.wx_sticker_sender import send_sticker

    _say("即将用模拟鼠标键盘给 %s 发送表情 '%s' ..." % (args.who, args.mood))
    result = send_sticker(args.who, args.mood, emotion=args.emotion,
                          platform=args.platform, account_id=args.account,
                          dry_run=args.dry_run)
    _dump(result)
    return 0 if result.get("ok") else 1


def cmd_mouse_test(args) -> int:
    import math

    x, y = human.cursor_pos()
    _say("当前光标: %s" % ((x, y),))
    _say("将画一个半径 120px 的圆并回到原位，用来确认 SendInput 生效。")
    if not args.yes:
        _say("（加 --yes 才会真的移动鼠标）")
        return 0

    import ctypes

    ctypes.windll.user32.SetProcessDPIAware()
    for index in range(72):
        angle = index / 72.0 * 2 * math.pi
        human._emit_move(x + 120 * math.cos(angle), y + 120 * math.sin(angle))
        time.sleep(0.01)
    human.move_to(x, y)
    _say("完成，光标已回到 %s" % ((x, y),))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="wx_diagnose", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="报告窗口几何与识别结果，并保存标注图")
    p.add_argument("--activate", action="store_true", help="先把微信切到前台再识别")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("probe-emoji", help="打开表情面板并报告其结构")
    p.add_argument("--yes", action="store_true", help="真的点击（默认只检测）")
    p.set_defaults(func=cmd_probe_emoji)

    p = sub.add_parser("selftest", help="对已有截图跑一遍感知流程")
    p.add_argument("--image", help="指定单张整窗截图")
    p.add_argument("--dir", help="指定一个装整窗截图的目录")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("capture", help="保存一张微信窗口截图")
    p.add_argument("--out", help="输出路径")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("thumbs", help="扫一遍表情面板，保存所有缩略图作为模板")
    p.add_argument("--platform", default="wechat")
    p.add_argument("--account", default="")
    p.add_argument("--pages", type=int, default=12)
    p.set_defaults(func=cmd_thumbs)

    p = sub.add_parser("bind", help="把已采集的缩略图绑定到 sticker_index.json")
    p.add_argument("--platform", default="wechat")
    p.add_argument("--account", default="")
    p.set_defaults(func=cmd_bind)

    p = sub.add_parser("send", help="完整走一遍发送流程")
    p.add_argument("--who", required=True)
    p.add_argument("--mood", required=True)
    p.add_argument("--emotion", default="")
    p.add_argument("--platform", default="wechat")
    p.add_argument("--account", default="")
    p.add_argument("--dry-run", action="store_true",
                   help="只走到定位为止，不点下去，不发出任何消息")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("mouse-test", help="确认 SendInput 能驱动光标")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_mouse_test)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        warn("core/execution/wx_diagnose.py:345 KeyboardInterrupt", err=repr(__import__("sys").exc_info()[1]))
        _say("已中断。")
        return 130


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        warn("core/execution/wx_diagnose.py:353 Exception", err=repr(__import__("sys").exc_info()[1]))
    raise SystemExit(main())
