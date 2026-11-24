"""Manage owner-drawn account links in the Phase 2 link tables.

Examples:
    python scripts/phase2_link_manage.py list
    python scripts/phase2_link_manage.py add \
        --a-platform wechat --a-account yinyue_a \
        --b-platform wechat --b-account yinyue_b
    python scripts/phase2_link_manage.py remove \
        --a-platform wechat --a-account yinyue_a \
        --b-platform wechat --b-account yinyue_b

Mutations are append-only: removal sets ``revoked_at`` and bumps the link
revision; the next identity/recall resolution hot-swaps the graph.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _repo():
    from core.terminal.link_runtime import (
        get_link_repository,
        link_repository_error,
    )

    repository = get_link_repository()
    if repository is None:
        print(f"link repository unavailable: {link_repository_error()}", file=sys.stderr)
        raise SystemExit(2)
    return repository


def _list() -> int:
    from core.terminal.link_runtime import current_link_revision, load_live_link_graph

    repository = _repo()
    graph = load_live_link_graph()
    payload = {
        "revision": current_link_revision(),
        "edges": [
            {"a": str(left), "b": str(right)} for left, right in graph.links()
        ],
        "components": [
            sorted(str(account) for account in component)
            for component in graph.connected_components()
        ],
        "active_rows": len(repository.list_active_edges()),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _add(args) -> int:
    from core.terminal.link_runtime import add_link

    revision = add_link(
        args.a_platform,
        args.a_account,
        args.b_platform,
        args.b_account,
        created_by=args.created_by,
        note=args.note,
    )
    print(json.dumps({"ok": True, "revision": revision}, ensure_ascii=False))
    return 0


def _remove(args) -> int:
    from core.terminal.link_runtime import remove_link

    revision = remove_link(
        args.a_platform,
        args.a_account,
        args.b_platform,
        args.b_account,
    )
    print(json.dumps({"ok": True, "revision": revision}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage terminal account links")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list active links and connected components")

    add_parser = sub.add_parser("add", help="add an explicit account link")
    remove_parser = sub.add_parser("remove", help="revoke an explicit account link")
    for command in (add_parser, remove_parser):
        command.add_argument("--a-platform", required=True)
        command.add_argument("--a-account", required=True)
        command.add_argument("--b-platform", required=True)
        command.add_argument("--b-account", required=True)
    add_parser.add_argument("--created-by", default="owner")
    add_parser.add_argument("--note", default="")

    args = parser.parse_args()
    if args.command == "list":
        return _list()
    if args.command == "add":
        return _add(args)
    if args.command == "remove":
        return _remove(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
