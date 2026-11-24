"""Phase 4 proactive whitelist / frequency control CLI.

Examples:
    python scripts/phase4_proactive.py list
    python scripts/phase4_proactive.py upsert --platform wechat \
        --bot-account wxid_bot --chat-id wxid_friend --chat-type private \
        --max-hour 6 --max-day 20 --cooldown 60 --quiet 23-7 --note "主人指定"
    python scripts/phase4_proactive.py disable --platform wechat \
        --bot-account wxid_bot --chat-id wxid_friend
    python scripts/phase4_proactive.py delete --id 1
    python scripts/phase4_proactive.py logs --limit 50
    python scripts/phase4_proactive.py check --platform wechat \
        --bot-account wxid_bot --chat-id wxid_friend

A row is the owner's explicit whitelist for one bot account / conversation.
``max-hour`` / ``max-day`` / ``cooldown`` = 0 means no limit / no cooldown.
Quiet hours are ``START-END`` in local time and may wrap midnight, e.g. 23-7.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _parse_quiet(value: str) -> list[int]:
    raw = str(value or "").strip()
    if not raw:
        return []
    try:
        start_s, end_s = raw.replace("~", "-").split("-", 1)
        start, end = int(start_s.strip()), int(end_s.strip())
    except Exception as exc:
        raise argparse.ArgumentTypeError(
            f"免打扰时段格式应为 START-END，例如 23-7: {value!r}"
        ) from exc
    if not (0 <= start <= 23 and 0 <= end <= 23):
        raise argparse.ArgumentTypeError("免打扰小时必须在 0-23 之间")
    return [start, end]


def _load_repo(meta_db: str):
    from db.meta_models import init_meta_db
    from db.repo_proactive import ProactiveTargetRepository

    path = Path(meta_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    # create_all + migrations are idempotent; this also creates the Phase 4
    # proactive tables on a fresh meta DB.
    engine = init_meta_db(str(path))
    return engine, ProactiveTargetRepository(engine)


def _target_dict(row: Any) -> dict[str, Any]:
    quiet: list[int] = []
    try:
        raw = json.loads(getattr(row, "quiet_hours_json", "[]") or "[]")
        if isinstance(raw, list):
            quiet = [int(item) for item in raw[:2]]
    except Exception:
        quiet = []
    return {
        "id": getattr(row, "id", 0),
        "platform": getattr(row, "platform", ""),
        "bot_account_id": getattr(row, "bot_account_id", ""),
        "chat_id": getattr(row, "chat_id", ""),
        "chat_type": getattr(row, "chat_type", "private"),
        "enabled": bool(getattr(row, "enabled", 0)),
        "allow_sticker": bool(getattr(row, "allow_sticker", 0)),
        "max_per_hour": int(getattr(row, "max_per_hour", 0) or 0),
        "max_per_day": int(getattr(row, "max_per_day", 0) or 0),
        "cooldown_minutes": int(getattr(row, "cooldown_minutes", 0) or 0),
        "quiet_hours": quiet,
        "note": getattr(row, "note", "") or "",
        "created_at": getattr(row, "created_at", None).isoformat()
        if getattr(row, "created_at", None) else "",
        "updated_at": getattr(row, "updated_at", None).isoformat()
        if getattr(row, "updated_at", None) else "",
    }


def _log_dict(row: Any) -> dict[str, Any]:
    return {
        "id": getattr(row, "id", 0),
        "platform": getattr(row, "platform", ""),
        "bot_account_id": getattr(row, "bot_account_id", ""),
        "chat_id": getattr(row, "chat_id", ""),
        "sent_at": getattr(row, "sent_at", None).isoformat()
        if getattr(row, "sent_at", None) else "",
        "kind": getattr(row, "kind", ""),
        "trigger": getattr(row, "trigger", ""),
        "content_preview": getattr(row, "content_preview", "") or "",
        "ok": bool(getattr(row, "ok", 0)),
        "error": getattr(row, "error", "") or "",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Proactive whitelist control")
    parser.add_argument("command", choices=[
        "list", "upsert", "enable", "disable", "delete", "logs", "check",
    ])
    parser.add_argument(
        "--meta-db", default=str(ROOT / "data" / "yinyue_meta.db"),
    )
    parser.add_argument("--id", type=int, default=0)
    parser.add_argument("--platform", default="wechat")
    parser.add_argument("--bot-account", default="")
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--chat-type", choices=["private", "group"],
                        default="private")
    parser.add_argument("--max-hour", type=int, default=6)
    parser.add_argument("--max-day", type=int, default=20)
    parser.add_argument("--cooldown", type=int, default=60)
    parser.add_argument("--quiet", default="", help="START-END, e.g. 23-7")
    parser.add_argument("--note", default="")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--enabled-only", action="store_true")
    enabled_group = parser.add_mutually_exclusive_group()
    enabled_group.add_argument("--enable", dest="enabled", action="store_true")
    enabled_group.add_argument("--disable", dest="enabled", action="store_false")
    parser.set_defaults(enabled=None)
    sticker_group = parser.add_mutually_exclusive_group()
    sticker_group.add_argument("--sticker", dest="allow_sticker",
                               action="store_true")
    sticker_group.add_argument("--no-sticker", dest="allow_sticker",
                               action="store_false")
    parser.set_defaults(allow_sticker=None)
    return parser


def _require_key(args: argparse.Namespace) -> tuple[str, str, str]:
    platform = str(args.platform or "").strip()
    chat_id = str(args.chat_id or "").strip()
    if not platform or not chat_id:
        raise SystemExit("需要 --platform 和 --chat-id（或改用 --id）")
    return platform, str(args.bot_account or "").strip(), chat_id


def main() -> int:
    args = _build_parser().parse_args()
    engine, repo = _load_repo(args.meta_db)
    try:
        if args.command == "list":
            rows = repo.list_targets(
                enabled_only=bool(args.enabled_only), limit=max(1, args.limit),
            )
            result = [_target_dict(row) for row in rows]
        elif args.command == "logs":
            rows = repo.list_send_log(limit=max(1, args.limit))
            result = [_log_dict(row) for row in rows]
        elif args.command == "upsert":
            platform, bot_account, chat_id = _require_key(args)
            row = repo.upsert_target(
                platform=platform,
                bot_account_id=bot_account,
                chat_id=chat_id,
                chat_type=args.chat_type,
                enabled=True if args.enabled is None else bool(args.enabled),
                allow_sticker=(
                    True if args.allow_sticker is None
                    else bool(args.allow_sticker)
                ),
                max_per_hour=max(0, int(args.max_hour or 0)),
                max_per_day=max(0, int(args.max_day or 0)),
                cooldown_minutes=max(0, int(args.cooldown or 0)),
                quiet_hours=_parse_quiet(args.quiet),
                note=args.note,
            )
            result = _target_dict(row)
            _invalidate()
        elif args.command in ("enable", "disable"):
            enabled = args.command == "enable"
            if args.id:
                ok = repo.set_enabled_by_id(args.id, enabled)
            else:
                platform, bot_account, chat_id = _require_key(args)
                ok = repo.set_enabled(platform, bot_account, chat_id, enabled)
            result = {"ok": ok}
            _invalidate()
        elif args.command == "delete":
            if args.id:
                ok = repo.delete_by_id(args.id)
            else:
                platform, bot_account, chat_id = _require_key(args)
                ok = repo.delete_by_id_values(
                    platform=platform,
                    bot_account_id=bot_account,
                    chat_id=chat_id,
                )
            result = {"ok": ok}
            _invalidate()
        else:  # check
            if args.id:
                row = repo.get_target_by_id(args.id)
            else:
                platform, bot_account, chat_id = _require_key(args)
                row = repo.get_target(platform, bot_account, chat_id)
            result = _check(repo, row)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        engine.dispose()
    return 0


def _check(repo: Any, row: Any) -> dict[str, Any]:
    if row is None:
        return {"allowed": False, "reason": "not_whitelisted"}
    from core.terminal.proactive_service import ProactivePolicyService

    class _Wrapper:
        proactive = repo

    service = ProactivePolicyService(_Wrapper())
    decision = service.evaluate(
        platform=str(getattr(row, "platform", "") or ""),
        bot_account_id=str(getattr(row, "bot_account_id", "") or ""),
        chat_id=str(getattr(row, "chat_id", "") or ""),
        chat_type=str(getattr(row, "chat_type", "") or "private"),
        kind="text",
        now=datetime.now(),
    )
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "target": _target_dict(decision.target) if decision.target else None,
    }


def _invalidate() -> None:
    """Best-effort same-process cache drop (usually a separate CLI process)."""
    try:
        from core.terminal.proactive_runtime import invalidate_proactive_caches

        invalidate_proactive_caches()
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
