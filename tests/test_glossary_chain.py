#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M1 上沉链端到端：候选 → pending 提案 → 批准（人补释义）→ 群级备注 → [场外补充] 命中。

上游审计用 `scripts/audit_sinking.py`（只读）；本文件验证"下游半条链"：
    1) 写入一份临时 recall trace（3 次同一群术语）→ propose_sinking 落 pending；
    2) 模拟主人补释义 approve → 写进 person_notes 群级行；
    3) 下一轮渲染（load_chat_notes / build_chat_context_block）能命中该词条。
全程使用临时 meta DB 与临时 trace，不碰生产库、不调 LLM。

归属：docs/梗与群术语设计.md（M1 通电验链）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# 数据库隔离（照 tests/test_event_chain_links.py / test_command_service_db.py）：
# 本文件会经 core.bootstrap.init_singletons() 触发 init_meta_db + run_migrations
# （bootstrap 兜底、runtime 访问器等），不隔离就把 pending 迁移落到**真实**
# meta / account 库。2026-09-29 实测：Migration 51 被本套测试落到了生产库
# （data/yinyue_meta.db + 4 个账号库，applied_at 14:58:19Z）。必须在 import core.*
# 之前设好：PathSettings 在 core.config.settings **首次**导入时就取值。
# ---------------------------------------------------------------------------
_ISO_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "_test_glossary_chain_iso"
)
os.makedirs(_ISO_ROOT, exist_ok=True)
os.environ["META_DB_PATH"] = os.path.join(_ISO_ROOT, "meta.db")
os.environ["PLATFORMS_DIR"] = os.path.join(_ISO_ROOT, "platforms")
os.environ["DATA_DIR"] = os.path.join(_ISO_ROOT, "data")

from core.config.settings import config as _config  # noqa: E402

# 守卫：env 只在 core.config.settings 首次导入前设好才有效，否则真实路径已经
# 固化进 config。这里硬失败，绝不静默把迁移/写操作落到生产库。
assert os.path.realpath(_config.paths.meta_db_path) != os.path.realpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "yinyue_meta.db"
    )
), f"测试数据库隔离失效：meta_db_path={_config.paths.meta_db_path}"


ROOT = Path(__file__).resolve().parent.parent

CHAT = "88888888@chatroom"
BOT = "wxid_glossarybot0001"
TERM = "kxkx"
DEFINITION = "kxkx=群里约面试的暗号"


def _write_trace(path: Path) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    records = [
        {
            "at": now,
            "chat_id": CHAT,
            "platform": "wechat",
            "bot_account_id": BOT,
            "query": TERM,
            "hits": [{"memory_id": "proj:aaaabbbbccccddddeeeeffff00001111", "score": 0.7}],
        }
        for _ in range(3)
    ]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")


def main() -> int:
    tmp_root = ROOT / ".tmp_test"
    tmp_root.mkdir(exist_ok=True)
    tmp = tmp_root / f"glossary_chain_{os.getpid()}_{int(datetime.now().timestamp())}"
    os.makedirs(tmp, exist_ok=False)
    os.environ["YINYUE_FEATURE_MEMORY_SINKING"] = "1"
    os.environ["YINYUE_FEATURE_PERSON_NOTES"] = "1"
    os.environ["YINYUE_PERSON_NOTES_CHATS"] = CHAT
    trace = tmp / "trace.jsonl"
    _write_trace(trace)

    import core.bootstrap as boot

    boot.META_DB_PATH = str(tmp / "meta.db")
    boot._initialized = False
    boot._singletons.clear()
    try:
        from core.quality.memory_sinking import propose_sinking

        stats = propose_sinking(days=14, min_occurrences=3, path=trace)
        assert stats.get("staged") == 1, f"staged={stats}"
        print("ok   上沉提案落库（pending）:", stats)

        from core.bootstrap import get_meta_repo
        from sqlalchemy import text as _sql

        with get_meta_repo().engine.connect() as conn:
            row = conn.execute(
                _sql(
                    "select id, content, status from memory_proposals "
                    "where slot='lexicon' order by id desc limit 1"
                )
            ).mappings().first()
        assert row and row["status"] == "pending", dict(row or {})
        assert TERM in str(row["content"]), row["content"]
        pid = int(row["id"])
        print("ok   提案内容（释义留白、词条在手）:", str(row["content"])[:64])

        from core.quality.memory_proposals import approve

        assert approve(pid, content=DEFINITION, decided_by="owner") is True
        print("ok   批准 + 人补释义")

        from core.context.person_notes import (
            build_chat_context_block,
            load_chat_notes,
            notes_enabled_for,
        )

        assert notes_enabled_for(CHAT) is True
        notes = load_chat_notes(CHAT, platform="wechat")
        assert any(DEFINITION in n for n in notes), notes
        block = build_chat_context_block(CHAT, platform="wechat")
        assert block.startswith("[场外补充]") and DEFINITION in block, block
        print("ok   群级备注写入 + [场外补充] 命中:", DEFINITION)

        # 硬条件 A：同名多义并存（第二张卡不覆盖第一张）
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_meta_repo().engine.begin() as conn:
            conn.execute(
                _sql(
                    "insert into memory_proposals "
                    "(slot, subject_name, subject_key, chat_id, platform, content, reason, "
                    " evidence_json, status, created_at) "
                    "values ('lexicon','','',:chat,'',"
                    " '群内术语「kxkx」被反复检索（4 次），建议沉淀其释义',"
                    " 'repeat','{}','pending',:now)"
                ),
                {"chat": CHAT, "now": now_str},
            )
        with get_meta_repo().engine.connect() as conn:
            pid2 = int(
                conn.execute(
                    _sql(
                        "select id from memory_proposals where slot='lexicon' "
                        "and status='pending' order by id desc limit 1"
                    )
                ).scalar()
            )
        assert approve(pid2, content="kxkx=另一个场合的用法", decided_by="owner") is True
        notes2 = load_chat_notes(CHAT, platform="wechat")
        assert any(
            DEFINITION in n and "kxkx=另一个场合的用法" in n for n in notes2
        ), notes2
        block2 = build_chat_context_block(CHAT, platform="wechat")
        assert DEFINITION in block2 and "kxkx=另一个场合的用法" in block2, block2
        print("ok   同名多义并存（硬条件 A）")

        assert approve(pid, content="x=y") is False
        print("ok   重复批准被拒（幂等）")

        # 隔离：词条只挂在该群；别的群 / 私聊取不到
        assert load_chat_notes("88888777@chatroom", platform="wechat") == []
        assert load_chat_notes("wxid_private_x", platform="wechat") == []
        print("ok   隔离检查（词条只在该群）")

        print("glossary chain: PASS")
        return 0
    finally:
        boot._initialized = False
        boot._singletons.clear()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
