"""把微信解密副本里的群成员关系同步到**旁路表**（CB-139 甲）。默认 dry-run。

只写 ``data/wechat_roster.db``（``YINYUE_ROSTER_DB`` 可改），不碰
``group_memberships`` / ``person_notes`` —— 那两处是「她认识谁」，由主人确认与
入站消息各自维护（理由见 ``core/context/room_members.py`` 的 docstring）。

用法：
    python scripts/sync_wx_roster.py            # 只打印差异
    python scripts/sync_wx_roster.py --apply    # 真写
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.context import room_members  # noqa: E402  (必须在 sys.path 之后)
from core.execution import wx_roster


def newest_contact_db() -> Path | None:
    found = sorted((ROOT / "data" / "decrypted").glob("wechat/*/contact.db"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0] if found else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contact-db", default="")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    path = Path(args.contact_db) if args.contact_db else newest_contact_db()
    if path is None or not path.exists():
        print("找不到解密的 contact.db（先跑 db/decrypt.py 那条流水）")
        return 1
    rows = wx_roster.read_decrypted(path)
    owners = wx_roster.read_owners(path)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
    print("源：%s（副本时间 %s）" % (path, stamp))
    for chat in sorted(rows, key=lambda c: -len(rows[c])):
        print("  %-24s %3d 人%s" % (chat, len(rows[chat]),
                                    "  群主 " + owners.get(chat, "?")[:12] if owners.get(chat) else ""))
    print("合计 %d 个群 / %d 人次" % (len(rows), sum(len(v) for v in rows.values())))
    if not args.apply:
        print("dry-run：没有写库（加 --apply 才写 %s）" % room_members.db_path())
        return 0
    con = room_members.connect()
    try:
        got = room_members.write_rows(rows, synced_at=stamp, owners=owners, con=con)
    finally:
        con.close()
    print("已写入 %s：%d 群 / %d 人次" % (room_members.db_path(), got["rooms"], got["members"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
