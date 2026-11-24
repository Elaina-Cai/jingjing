"""人工清理微信输入框（唯一保留「清空」能力的地方）。

自动发送路径已经不许清空输入框了（2026-10-28 事故「哦」）：
框里留下字 = 证据，不该被看不见的手擦掉。真要清，人工跑这个脚本。

    .venv\\Scripts\\python.exe scripts\\wx_clear_box.py --yes
"""
import sys

from core.execution import wx_chat


def main(argv: list[str]) -> int:
    if "--yes" not in argv:
        print("这会清空微信当前会话的输入框。确认后加 --yes 再跑。")
        return 2
    ok = wx_chat.clear_message_box()
    print("cleared=%s" % bool(ok))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
