"""人格两张皮检查：active 卡 vs 数据库 persona（只读）。

生产回复只用卡（loop/selfcard.py）；数据库那份（personas/persona_versions）
只剩 Web 预览与语音快照在读。两边一旦分叉，用户看到的"预览"就不是她
真正说的话。这个脚本只报告，不改任何东西。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
META = ROOT / "data" / "yinyue_meta.db"
ACTIVE = ROOT / "data" / "persona" / "active.json"
CARDS = ROOT / "data" / "persona" / "cards"


def main() -> int:
    card_name = ""
    if ACTIVE.exists():
        card_name = (json.loads(ACTIVE.read_text(encoding="utf-8")).get("card") or "")
    card = {}
    if card_name and (CARDS / f"{card_name}.json").exists():
        card = json.loads((CARDS / f"{card_name}.json").read_text(encoding="utf-8"))
    print(f"[卡]  {card_name or '（无 active 卡）'}"
          f"  字段 {len([k for k, v in card.items() if v])} 个")

    if not META.exists():
        print("[库]  没找到 data/yinyue_meta.db")
        return 0
    con = sqlite3.connect(f"file:{META}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select p.name, v.version, v.id from personas p "
            "join persona_versions v on v.persona_id = p.id "
            "order by v.version desc limit 3").fetchall()
    except Exception as exc:  # noqa: BLE001 - 只读检查，失败就报出来
        print("[库]  查询失败:", exc)
        return 0
    finally:
        con.close()
    if not rows:
        print("[库]  没有 persona 记录")
    for name, ver, vid in rows:
        print(f"[库]  {name} v{ver} (id={vid})")

    if rows and card_name and rows[0][0] != card_name:
        print(f"\n[分叉] 卡是「{card_name}」，库里最新是「{rows[0][0]}」"
              "—— Web 预览会显示成库里那个，不是她真正在用的。")
    elif rows:
        print(f"\n[同名] 卡与库都是「{rows[0][0]}」，但内容仍是两份真源"
              "（改了卡，库不会跟着变）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
