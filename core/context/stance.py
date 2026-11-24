"""立场卡 + 姿态映射的剩余 owner 面（P2 社交分寸）。

WP-10 第 5 行（2026-10-10，owner 覆盖）起：[局况] 姿态行/立场行退役，
``decide_stance`` / ``render_stance_line`` / ``build_stance_block`` 与两个
``YINYUE_FEATURE_STANCE_*`` 开关一并删除。本模块只保留：

* ``STANCES`` / ``position_map``：react_playbook ``positions`` 的读口；
* ``load_cards`` / ``list_cards`` / ``upsert_card`` / ``deactivate_card`` /
  ``rollback_card``：stance_cards 表（迁移 42）的 owner 读写；
* ``render_cards_line``：卡文本渲染（CLI/测试用）。

设计：docs/社交分寸与立场_设计.md。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence
from safelog import warn

# 6 种姿态（设计文档 §三 部件④；「轻接」= 应一声但不展开）。
STANCES = ("看戏", "吐槽", "轻接", "正经答", "拆台", "沉默")

# 8 类位置 → (默认姿态, 白话理由) 的**唯一数据源已迁至**
# ``configs/speech_seed/react_playbook.json`` 的 ``positions``（WP-2A，2026-10-08）。
# 这里不再留第二份常量：数据缺失 = 没有姿态（fail-soft，与 react_playbook 同口径）。


def position_map() -> dict[str, tuple[str, str]]:
    """位置 → (默认姿态, 白话理由)；数据来自 react_playbook.json 的 ``positions``。

    文件缺失/损坏 → 空 dict（调用方按"没有姿态"处理，不编、不猜）。
    """
    try:
        from core.speech.react_playbook import position_map as _data_position_map

        return _data_position_map()
    except Exception:  # noqa: BLE001 - 只读数据，坏了就当没有
        warn("core/context/stance.py:37 Exception", err=repr(__import__("sys").exc_info()[1]))
        return {}


_CARD_LIMIT = 2
_CARD_MAX = 60
_LINE_MAX = 170


def _flat(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def render_cards_line(cards: Sequence[str], *, limit: int = _LINE_MAX) -> str:
    """姿态决策关、立场卡开时的退化行：``- 你的立场：…``。"""
    cleaned = [
        card
        for card in (
            _flat(item, _CARD_MAX).rstrip("。；;") for item in (cards or ())
        )
        if card
    ]
    if not cleaned:
        return ""
    return _flat("- 你的立场：" + "；".join(cleaned[:_CARD_LIMIT]), limit)


# ---------------------------------------------------------------- 立场卡 IO
def load_cards(
    chat_id: str,
    *,
    person_keys: Sequence[str] = (),
    platform: str = "",
    account_id: str = "",
    limit: int = _CARD_LIMIT,
    meta_repo: Any = None,
) -> list[str]:
    """读生效中的立场卡（群卡 + 全局卡；``person_keys`` 给定时连分人卡）。

    任何失败都返回 ``[]``。排序：群卡在前（比全局卡更具体），再按 id 旧→新。
    ``meta_repo`` 供测试注入；缺省走 ``core.bootstrap.get_meta_repo``。
    """
    try:
        if meta_repo is None:
            from core.terminal.store_port import get_meta_repo

            meta_repo = get_meta_repo()
        from sqlalchemy import text as _sql

        keys = [str(key) for key in (person_keys or ()) if str(key or "").strip()]
        params: dict[str, Any] = {
            "chat": str(chat_id or ""),
            "platform": str(platform or ""),
        }
        person_clause = "person_key = ''"
        if keys:
            placeholders = ",".join(f":pk{i}" for i in range(len(keys)))
            person_clause = f"(person_key = '' or person_key in ({placeholders}))"
            for i, key in enumerate(keys):
                params[f"pk{i}"] = key
        with meta_repo.engine.connect() as conn:
            rows = conn.execute(
                _sql(
                    "select content from stance_cards "
                    "where status = 'active' "
                    "and (chat_id = :chat or chat_id = '') "
                    "and (platform = '' or platform = :platform) "
                    "and " + person_clause + " "
                    "order by case when chat_id = :chat then 0 else 1 end, id"
                ),
                params,
            ).mappings().all()
        out: list[str] = []
        for row in rows:
            card = _flat(row["content"], _CARD_MAX)
            if card:
                out.append(card)
            if len(out) >= max(1, int(limit)):
                break
        return out
    except Exception:
        warn("core/context/stance.py:118 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []


def list_cards(
    chat_id: str = "", *, active_only: bool = True, meta_repo: Any = None
) -> list[dict]:
    """列出立场卡（``--list`` 用；含版本与状态）。``meta_repo`` 供测试注入。"""
    try:
        if meta_repo is None:
            from core.terminal.store_port import get_meta_repo

            meta_repo = get_meta_repo()
        from sqlalchemy import text as _sql

        sql = (
            "select id, scope, platform, account_id, chat_id, person_key, content, "
            "source, version, status, created_at, updated_at from stance_cards"
        )
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if chat_id:
            clauses.append("(chat_id = :chat or chat_id = '')")
            params["chat"] = str(chat_id)
        if active_only:
            clauses.append("status = 'active'")
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by chat_id, person_key, version"
        with meta_repo.engine.connect() as conn:
            return [dict(row) for row in conn.execute(_sql(sql), params).mappings()]
    except Exception:
        warn("core/context/stance.py:149 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []


def upsert_card(
    *,
    content: str,
    scope: str = "group",
    chat_id: str = "",
    person_key: str = "",
    platform: str = "",
    account_id: str = "",
    source: str = "owner",
    meta_repo: Any = None,
) -> dict:
    """写入一张立场卡：append-only 版本化。

    同键已有 active 且内容相同 → 只刷新 updated_at，不产生新版本；
    内容不同 → 旧版置 ``superseded``，插入 ``version=旧版+1`` 的新行。
    返回 ``{"ok", "id", "version", "changed"}``。
    """
    from sqlalchemy import text as _sql

    body = _flat(content, 10 ** 6)
    if not body:
        return {"ok": False, "reason": "empty"}
    scope = str(scope or "group").strip().lower()
    if scope not in ("group", "person"):
        scope = "group"
    person_key = str(person_key or "") if scope == "person" else ""
    if meta_repo is None:
        from core.terminal.store_port import get_meta_repo

        meta_repo = get_meta_repo()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    key = {
        "scope": scope,
        "platform": str(platform or ""),
        "account_id": str(account_id or ""),
        "chat_id": str(chat_id or ""),
        "person_key": person_key,
    }
    with meta_repo.engine.begin() as conn:
        current = conn.execute(
            _sql(
                "select id, version, content from stance_cards "
                "where scope = :scope and platform = :platform "
                "and account_id = :account_id and chat_id = :chat_id "
                "and person_key = :person_key and status = 'active' "
                "order by version desc limit 1"
            ),
            key,
        ).mappings().first()
        if current is not None and _flat(current["content"], 10 ** 6) == body:
            conn.execute(
                _sql("update stance_cards set updated_at = :now where id = :id"),
                {"now": now, "id": int(current["id"])},
            )
            return {
                "ok": True,
                "id": int(current["id"]),
                "version": int(current["version"]),
                "changed": False,
            }
        version = int(current["version"]) + 1 if current is not None else 1
        if current is not None:
            conn.execute(
                _sql(
                    "update stance_cards set status = 'superseded', updated_at = :now "
                    "where scope = :scope and platform = :platform "
                    "and account_id = :account_id and chat_id = :chat_id "
                    "and person_key = :person_key and status = 'active'"
                ),
                {**key, "now": now},
            )
        result = conn.execute(
            _sql(
                "insert into stance_cards "
                "(scope, platform, account_id, chat_id, person_key, content, source, "
                "version, status, created_at, updated_at) "
                "values (:scope, :platform, :account_id, :chat_id, :person_key, "
                ":content, :source, :version, 'active', :now, :now)"
            ),
            {**key, "content": body, "source": str(source or "owner"), "version": version, "now": now},
        )
        return {
            "ok": True,
            "id": int(result.lastrowid or 0),
            "version": version,
            "changed": True,
        }


def deactivate_card(card_id: int, *, meta_repo: Any = None) -> dict:
    """下架某张 active 卡（置 superseded；不删行，可再 --set 新卡或回滚）。"""
    from sqlalchemy import text as _sql

    if meta_repo is None:
        from core.terminal.store_port import get_meta_repo

        meta_repo = get_meta_repo()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with meta_repo.engine.begin() as conn:
        result = conn.execute(
            _sql(
                "update stance_cards set status = 'superseded', updated_at = :now "
                "where id = :id and status = 'active'"
            ),
            {"now": now, "id": int(card_id)},
        )
    return {"ok": bool(result.rowcount), "id": int(card_id)}


def rollback_card(card_id: int, *, meta_repo: Any = None) -> dict:
    """把某个历史版本恢复生效（以新版本形式复活；append-only 不删行）。"""
    from sqlalchemy import text as _sql

    if meta_repo is None:
        from core.terminal.store_port import get_meta_repo

        meta_repo = get_meta_repo()
    with meta_repo.engine.connect() as conn:
        row = conn.execute(
            _sql(
                "select id, scope, platform, account_id, chat_id, person_key, "
                "content, source, version, status from stance_cards where id = :id"
            ),
            {"id": int(card_id)},
        ).mappings().first()
    if row is None:
        return {"ok": False, "reason": "not_found"}
    if str(row["status"]) == "active":
        return {"ok": False, "reason": "already_active", "id": int(row["id"])}
    result = upsert_card(
        content=str(row["content"]),
        scope=str(row["scope"]),
        chat_id=str(row["chat_id"]),
        person_key=str(row["person_key"]),
        platform=str(row["platform"]),
        account_id=str(row["account_id"]),
        source=str(row["source"] or "owner"),
        meta_repo=meta_repo,
    )
    if result.get("ok"):
        result["restored_from_version"] = int(row["version"])
    return result


__all__ = [
    "STANCES",
    "deactivate_card",
    "list_cards",
    "load_cards",
    "position_map",
    "render_cards_line",
    "rollback_card",
    "upsert_card",
]
