#!/usr/bin/env python
"""立场卡（owner 手编边界）管理 CLI（P2 社交分寸）。

    python scripts/stance.py --list                          # 看生效中的卡
    python scripts/stance.py --list --chat 10000000002@chatroom
    python scripts/stance.py --history --chat <chat>         # 全部版本（含 superseded）
    python scripts/stance.py --set "不替人催债背锅；玩笑只吐槽不立档" --chat <chat>
    python scripts/stance.py --set-person wxid_xxx "对他：..." --chat <chat>
    python scripts/stance.py --off 3                         # 下架某张卡（可回滚）
    python scripts/stance.py --rollback 2                    # 恢复历史版本（新版本复活）
    python scripts/stance.py --render --chat <chat>          # 看当前卡文本

只写 stance_cards 表（迁移 42），append-only 版本化、可回滚；不发消息、不动记忆。
WP-10 第 5 行（2026-10-10）起 [局况] 姿态行/立场行已退役，卡不再注入 prompt；
本 CLI 只作为 owner 数据的唯一写入口保留。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.context.stance import (  # noqa: E402
    deactivate_card,
    list_cards,
    load_cards,
    render_cards_line,
    rollback_card,
    upsert_card,
)


def _print_row(row: dict) -> None:
    scope = str(row.get("scope") or "")
    key = f"{scope}:{row.get('person_key') or '（群/全局）'}"
    print(
        f"#{row['id']:<4} v{row['version']:<3} {row['status']:<10} "
        f"chat={str(row.get('chat_id') or '(全局)'):<26} {key:<38} "
        f"{row['source']:<8} {str(row['content'])[:70]}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", default="", help="会话 id（空=只操作全局卡）")
    parser.add_argument("--list", action="store_true", help="列出生效中的卡")
    parser.add_argument("--history", action="store_true", help="列出全部版本")
    parser.add_argument("--set", metavar="CONTENT", help="写群/全局卡")
    parser.add_argument(
        "--set-person",
        nargs=2,
        metavar=("WXID", "CONTENT"),
        help="写分人卡",
    )
    parser.add_argument("--off", type=int, default=0, help="下架指定 id 的卡")
    parser.add_argument("--rollback", type=int, default=0, help="恢复指定 id 的版本")
    parser.add_argument("--render", action="store_true", help="看注入效果与开关状态")
    args = parser.parse_args()

    if args.set:
        result = upsert_card(content=args.set, scope="group", chat_id=args.chat)
        if not result.get("ok"):
            print(f"写入失败：{result.get('reason')}")
            return 2
        verb = "更新" if result.get("changed") else "未变化（内容相同）"
        print(f"已写入群卡 #{result['id']} v{result['version']}（{verb}）")
        print("提示：已入库留档；姿态行退役后卡不再注入 [局况]。")
        return 0

    if args.set_person:
        wxid, content = args.set_person
        result = upsert_card(
            content=content, scope="person", person_key=wxid, chat_id=args.chat
        )
        if not result.get("ok"):
            print(f"写入失败：{result.get('reason')}")
            return 2
        verb = "更新" if result.get("changed") else "未变化（内容相同）"
        print(f"已写入分人卡 #{result['id']} v{result['version']}（{verb}）")
        print("提示：分人卡已在库内留档；按人注入等 P4 社交反馈闭环接线。")
        return 0

    if args.off:
        result = deactivate_card(args.off)
        print("已下架" if result.get("ok") else "未找到生效中的该 id", result)
        return 0 if result.get("ok") else 1

    if args.rollback:
        result = rollback_card(args.rollback)
        if result.get("ok"):
            print(
                f"已恢复 #{args.rollback} 的内容为新版本 v{result['version']}"
                f"（新 id #{result['id']}）"
            )
            return 0
        print(f"恢复失败：{result.get('reason')}", result)
        return 1

    if args.render:
        cards = load_cards(args.chat)
        print(f"chat={args.chat or '(全局)'}")
        print(render_cards_line(cards) or "(无生效卡)")
        return 0

    rows = list_cards(
        args.chat if not args.history else args.chat,
        active_only=not args.history,
    )
    if not rows:
        print("(无卡)")
        return 0
    for row in rows:
        _print_row(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
