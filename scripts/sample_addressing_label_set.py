"""抽一批「无事实边」的受话标注样本（只读）。

口径
----
* 只取**她真的发过话**的回合：拿 her message 往前找 120s 内最近的一条非她消息当触发句。
* 触发句在 ``utterance_edges`` 里必须是 ``evidence='none'``（没有私聊/引用/正文@证据）。
* 触发句与她自己的回复都必须是**真文本**：sysmsg/表情包/图片/文件/视频/语音占位一律丢弃。
* 上下文给标注者：触发句前 8 条（同群，含时间/发言人/引用对象）。
* 分层：按群的比例抽样，固定随机种子（可复现）。

盲标
----
``--blind`` 时正文**不显示她的回复**（避免拿她的答案当标准答案），并多一列
「她该不该接」；她的实际回复仍存在 JSON 里，标完用来对答案。
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = ROOT / "data/platforms/wechat/wxid_seed02/account.db"
META = ROOT / "data/yinyue_meta.db"
OUT_JSON = ROOT / "bench/addressing_label_v2.json"
OUT_MD = ROOT / "docs/decisions/受话标注集_50条.md"

NOISE_PREFIXES = ("<sysmsg", "[表情包:", "[图片]", "[文件]", "[视频]", "[语音]", "[链接]")


def is_noise(text: str) -> bool:
    body = (text or "").strip()
    if len(body) < 2:
        return True
    if body.startswith(NOISE_PREFIXES):
        return True
    return "拍了拍" in body and len(body) < 40


def show(text: str) -> str:
    body = (text or "").strip()
    if body.startswith("[表情包:"):
        return "(表情包)"
    if body.startswith(("[图片]", "[文件]", "[视频]", "[语音]", "[链接]")):
        return body.split("]", 1)[0] + "]"
    if body.startswith("<sysmsg"):
        return "(系统消息)"
    return body[:160]


def connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.execute("pragma temp_store=memory")
    return con


def read_account(days: float, limit: int = 40000) -> list[dict]:
    con = connect_ro(ACCOUNT)
    con.row_factory = sqlite3.Row
    since = (datetime.now() - timedelta(days=days)).isoformat(sep=" ")
    rows = con.execute(
        "select msg_id, chat_id, chat_type, sender_id, sender_name, content, "
        "created_at, is_self, quote_reply_id, quote_reply_user "
        "from messages where created_at >= ? order by chat_id, created_at limit ?",
        (since, limit),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def evidence_map() -> dict[str, str]:
    con = connect_ro(META)
    out = {str(m): str(e) for m, e in con.execute("select msg_id, evidence from utterance_edges")}
    con.close()
    return out


def build(rows: list[dict], evidence: dict[str, str]) -> list[dict]:
    by_chat: dict[str, list[dict]] = {}
    for r in rows:
        by_chat.setdefault(str(r["chat_id"]), []).append(r)
    items: list[dict] = []
    for chat_id, seq in by_chat.items():
        for i, row in enumerate(seq):
            if not (row.get("is_self") and str(row.get("sender_name") or "") == "鲸鲸"):
                continue
            if is_noise(str(row.get("content") or "")):
                continue
            trigger = None
            j = i - 1
            while j >= 0:
                prev = seq[j]
                if prev.get("is_self"):
                    j -= 1
                    continue
                try:
                    gap = (datetime.fromisoformat(str(row["created_at"]))
                           - datetime.fromisoformat(str(prev["created_at"]))).total_seconds()
                except ValueError:
                    break
                if gap <= 120:
                    trigger = prev
                break
            if trigger is None or is_noise(str(trigger.get("content") or "")):
                continue
            if evidence.get(str(trigger["msg_id"]), "none") != "none":
                continue
            ctx = seq[max(0, j - 8): j + 1]
            items.append({
                "chat_id": chat_id,
                "chat_type": str(row.get("chat_type") or ""),
                "trigger_msg_id": str(trigger["msg_id"]),
                "trigger_time": str(trigger["created_at"]),
                "trigger_speaker": str(trigger.get("sender_name") or ""),
                "trigger_text": str(trigger.get("content") or "")[:300],
                "her_reply": str(row.get("content") or "")[:300],
                "context": [
                    {
                        "t": str(c.get("created_at")),
                        "who": "她" if c.get("is_self") else str(c.get("sender_name") or ""),
                        "text": show(str(c.get("content") or "")),
                        "quote": str(c.get("quote_reply_user") or ""),
                    }
                    for c in ctx
                ],
            })
    return items


def sample(items: list[dict], total: int, seed: int) -> list[dict]:
    rnd = random.Random(seed)
    by_chat: dict[str, list[dict]] = {}
    for it in items:
        by_chat.setdefault(it["chat_id"], []).append(it)
    picked: list[dict] = []
    for chat in sorted(by_chat, key=lambda c: -len(by_chat[c])):
        share = max(1, round(total * len(by_chat[chat]) / max(1, len(items))))
        picked.extend(rnd.sample(by_chat[chat], min(share, len(by_chat[chat]))))
    if len(picked) > total:
        picked = rnd.sample(picked, total)
    picked.sort(key=lambda x: (x["chat_id"], x["trigger_time"]))
    for n, it in enumerate(picked, 1):
        it["id"] = f"A{n:02d}"
        it["label_should_reply"] = ""
        it["label_target"] = ""
        it["label_clarity"] = ""
        it["label_note"] = ""
    return picked


def write_md(items: list[dict], path: Path, *, blind: bool) -> None:
    lines = [
        "# 受话标注集（50 条，无事实边回合，盲标）",
        "",
        "口径：她真的发过话、触发句在 `utterance_edges` 里 `evidence='none'`（没有私聊/引用/正文@证据），",
        "且触发句与她的回复都是真文本（sysmsg/表情/图片占位已剔）。正文**不显示她的回复**，标完再对答案。",
        "",
        "**要填的四列**：",
        "",
        "- `她该不该接`：接 / 不该接（对全群发言但没人在等答案，通常算「不该接」；被点名或明确在问她，算「接」）",
        "- `该回谁`：人名，多人用逗号；`群里所有人`=broadcast，`不确定`=unknown，`不该接` 时留空",
        "- `清晰度`：清晰 / 模糊 / 判不出",
        "- `备注`：可选",
        "",
    ]
    for it in items:
        lines.append(f"## {it['id']} · {it['chat_id']} · {it['trigger_time']}")
        lines.append("")
        for c in it["context"][:-1]:
            q = f"（引用 {c['quote']}）" if c["quote"] else ""
            lines.append(f"- `{c['t'][11:19]}` {c['who']}：{c['text']}{q}")
        last = it["context"][-1]
        lines.append(f"- **`{last['t'][11:19]}` {it['trigger_speaker']}：{it['trigger_text']}** ← 触发句")
        lines.append("")
        if blind:
            lines.append("- 她该不该接：____　该回谁：____　清晰度：____　备注：")
        else:
            lines.append(f"- 她的回复：{it['her_reply']}")
            lines.append("- 她该不该接：____　该回谁：____　清晰度：____　备注：")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--total", type=int, default=50)
    ap.add_argument("--seed", type=int, default=20261018)
    ap.add_argument("--blind", action="store_true")
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    ap.add_argument("--out-md", type=Path, default=OUT_MD)
    args = ap.parse_args()
    rows = read_account(args.days)
    items = build(rows, evidence_map())
    picked = sample(items, args.total, args.seed)
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(picked, ensure_ascii=False, indent=2), encoding="utf-8")
    write_md(picked, args.out_md, blind=args.blind)
    print(f"messages={len(rows)} candidates={len(items)} picked={len(picked)} blind={args.blind}")
    print(f"json={args.out_json}")
    print(f"md={args.out_md}")
    by_chat: dict[str, int] = {}
    for it in picked:
        by_chat[it["chat_id"]] = by_chat.get(it["chat_id"], 0) + 1
    print("by chat:", by_chat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
