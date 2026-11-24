#!/usr/bin/env python
"""Phase 0: capture the current context-assembly baseline (no LLM calls).

Dumps the *system prompt* produced by today's assembly path for 20
representative scenarios into ``tests/golden/context/``.  Phase 1 (the
equivalent refactor) must reproduce these byte-for-byte.

What is deterministic here (and why):

* vector recall is disabled (``YINYUE_FEATURE_MEMORY_VECTOR_RECALL=0``), so the
  memory block is lexical-only and reproducible;
* the time block is excluded on purpose -- we call
  ``_build_system_prompt_from_snapshot`` directly, and the clock block is added
  by ``_with_time_block`` in the caller;
* the principal is attached explicitly, so identity/config lookups cannot drift.

Usage:
    python scripts/context_baseline.py
    python scripts/context_baseline.py --out tests/golden/context
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("YINYUE_FEATURE_MEMORY_VECTOR_RECALL", "0")
# Golden capture stays on the legacy assembly path; --via-assembler flips it to
# core.context.assembler for the Phase 1 equivalence check.
os.environ.setdefault("YINYUE_FEATURE_CONTEXT_ASSEMBLER", "0")
# WP-6E：群聊切幕已是基线（开关已删）；基线只需打开 private 总闸。
os.environ.setdefault("YINYUE_FEATURE_REACT_PLAYBOOK", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.terminal.principal import (  # noqa: E402
    ChatType,
    PrincipalContext,
    RelationLevel,
    VisibilityScope,
)

OWNER_ID = "wxid_seed01"
BOT_ID = "wxid_seed02"
FRIEND_ID = "wxid_friend_test"
GROUP_ID = "12345678@chatroom"


def _principal(kind: str, *, is_group: bool) -> PrincipalContext:
    chat_id = GROUP_ID if is_group else {
        "owner": OWNER_ID,
        "ordinary": FRIEND_ID,
        "stranger": "wxid_stranger_test",
        "group_member": FRIEND_ID,
    }.get(kind, FRIEND_ID)
    relation = {
        "owner": RelationLevel.OWNER,
        "ordinary": RelationLevel.ORDINARY,
        "stranger": RelationLevel.UNKNOWN,
        "group_member": RelationLevel.ORDINARY,
    }.get(kind, RelationLevel.UNKNOWN)
    scopes = (
        frozenset({VisibilityScope.GROUP_MEMBERS})
        if is_group and kind != "owner"
        else frozenset()
    )
    return PrincipalContext(
        platform="wechat",
        bot_account_id=BOT_ID,
        chat_id=chat_id,
        chat_type=ChatType.GROUP if is_group else ChatType.PRIVATE,
        person_id=OWNER_ID if kind == "owner" else f"p_{kind}",
        display_name="主人" if kind == "owner" else "测试",
        relation_to_bot=relation,
        group_id=GROUP_ID if is_group else "",
        memory_scopes=scopes,
    )


def _scenarios() -> list[dict]:
    long_group = "\n".join(
        f"{name}: 这是群里第{i}条最近消息，用于观察群上下文块的体积。"
        for i, name in enumerate(
            ["小A", "小B", "小C", "小D", "主人", "小E", "小F", "小G", "小H", "小I"] * 2,
            1,
        )
    )
    long_text = "这是一条很长的消息，" * 40
    return [
        {"name": "01_owner_private_memory", "kind": "owner", "text": "你还记得我上次说的那个项目吗"},
        {"name": "02_owner_private_casual", "kind": "owner", "text": "在干嘛呀"},
        {"name": "03_owner_private_correction", "kind": "owner", "text": "不对，不是周五，是周六"},
        {"name": "04_owner_private_long", "kind": "owner", "text": long_text},
        {"name": "05_friend_private", "kind": "ordinary", "text": "你好呀"},
        {"name": "06_friend_asks_owner", "kind": "ordinary", "text": "你主人最近在忙什么"},
        {"name": "07_stranger_private", "kind": "stranger", "text": "你是谁"},
        {"name": "08_group_mentioned", "kind": "group_member", "is_group": True,
         "text": "@鲸鲸 帮我看看这个", "extra": {"is_mentioned": True}, "group": 3},
        {"name": "09_group_not_mentioned", "kind": "group_member", "is_group": True,
         "text": "今天天气不错", "group": 3},
        {"name": "10_group_recent_long", "kind": "group_member", "is_group": True,
         "text": "@鲸鲸 总结一下", "extra": {"is_mentioned": True}, "group_text": long_group},
        {"name": "11_group_owner_member", "kind": "owner", "is_group": True,
         "text": "别聊这个了", "group": 3},
        {"name": "12_image_unavailable_private", "kind": "owner",
         "text": "看看这张图", "extra": {"image_available": False}},
        {"name": "13_image_unavailable_group", "kind": "group_member", "is_group": True,
         "text": "@鲸鲸 看看图", "extra": {"is_mentioned": True, "image_available": False}},
        {"name": "14_proactive_whitelist", "kind": "owner",
         "text": "", "extra": {"proactive": True}},
        {"name": "15_proactive_force_speak", "kind": "owner",
         "text": "", "extra": {"proactive": True, "force_speak": True}},
        {"name": "16_correction_in_group", "kind": "owner", "is_group": True,
         "text": "不对，我没说过这个", "group": 3},
        {"name": "17_group_member_memory", "kind": "group_member", "is_group": True,
         "text": "@鲸鲸 你记得我说过什么吗", "extra": {"is_mentioned": True}, "group": 3},
        {"name": "18_empty_text", "kind": "owner", "text": ""},
        {"name": "19_multiline", "kind": "owner", "text": "第一行\n第二行\n第三行"},
        {"name": "20_english", "kind": "owner", "text": "do you remember our last plan?"},
    ]


def _block_headers(prompt: str) -> list[str]:
    headers: list[str] = []
    for block in prompt.split("\n\n"):
        first = block.strip().splitlines()[0] if block.strip() else ""
        if first.startswith("[") and "]" in first:
            headers.append(first.split("]")[0] + "]")
    return headers


def prompt_for_item(agent, item: dict, snapshot_from_context) -> str:
    """Set up one scenario exactly like the live turn and return the prompt."""
    is_group = bool(item.get("is_group"))
    principal = _principal(item["kind"], is_group=is_group)
    text = str(item.get("text") or "")
    extra = {
        "platform": "wechat",
        "account_id": BOT_ID,
        "chat_id": principal.chat_id,
        "sender_id": principal.person_id,
        "sender_name": principal.display_name,
        "user_message": text,
        "is_group": is_group,
        "group_id": principal.group_id,
    }
    extra.update(item.get("extra") or {})
    context = SimpleNamespace(
        extra=extra,
        principal=principal,
        account_context=None,
        get=lambda key, default=None: extra.get(key, default),
    )
    if item.get("group_text"):
        group_recent = item["group_text"]
    elif item.get("group"):
        group_recent = "\n".join(
            f"测试用户{i}: 群里的第{i}句话"
            for i in range(1, int(item["group"]) + 1)
        )
    else:
        group_recent = ""

    snapshot = snapshot_from_context(
        context,
        chat_type="group" if is_group else "private",
        is_owner=principal.is_owner,
        is_mentioned=bool(extra.get("is_mentioned")),
        group_recent_context=group_recent,
    )
    agent._last_context = context
    agent._inbound_content = text
    agent._group_recent_context = group_recent
    agent._speech_snapshot = snapshot
    return agent._build_system_prompt_from_snapshot(snapshot, is_group)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "tests" / "golden" / "context"))
    parser.add_argument("--via-assembler", action="store_true")
    args = parser.parse_args()
    if args.via_assembler:
        os.environ["YINYUE_FEATURE_CONTEXT_ASSEMBLER"] = "1"

    from core.bootstrap import build_agent
    from core.speech.turn import snapshot_from_context

    # Freeze the clock so the [时间感知] block is byte-reproducible: the block
    # itself stays in the golden (position/content covered), only "now" is fixed.
    from core import temporal as _temporal

    fixed_now = datetime(2026, 1, 1, 12, 0)
    _original_prompt_block = _temporal.prompt_block
    _temporal.prompt_block = lambda now_value=None, **kw: _original_prompt_block(
        fixed_now, **kw
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    agent = build_agent()
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "flags": {
            "memory_vector_recall": "off (deterministic lexical recall)",
            "time_block": "frozen at 2026-01-01 12:00 (block included, clock fixed)",
            "principal": "attached explicitly",
        },
        "scenarios": [],
    }

    for item in _scenarios():
        is_group = bool(item.get("is_group"))
        prompt = prompt_for_item(agent, item, snapshot_from_context)
        raw = prompt.encode("utf-8")
        target = out_dir / f"{item['name']}.txt"
        target.write_bytes(raw)  # bytes, so the sha256 above matches the file
        manifest["scenarios"].append(
            {
                "name": item["name"],
                "file": target.name,
                "chars": len(prompt),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "blocks": _block_headers(prompt),
                "principal": item["kind"],
                "is_group": is_group,
            }
        )
        print(f"  captured {item['name']}: {len(prompt)} chars, {len(_block_headers(prompt))} blocks")

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"baseline -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
