"""Move sticker data into the per-account isolation layout.

Before this script the stickers lived in data/wechat/<account>/, which is not
the layout docs/data_isolation_architecture.md defines, and which cannot express
the fact that each account's custom stickers belong only to that account.

    data/wechat/<account>/sticker_index.json   -> data/platforms/wechat/<account>/emoji/registry.json
    data/wechat/<account>/sticker_templates/   -> data/platforms/wechat/<account>/emoji/templates/
    data/wechat/<account>/sticker_thumbs/      -> data/platforms/wechat/<account>/emoji/thumbs/

The anonymous root index (data/wechat/sticker_index.json, used when no account is
named) is merged into the account that actually sends -- the one registered as
role="yinyue" -- because in the isolation model every sticker belongs to someone.

Template paths inside the registry are rewritten to be relative to the sticker
root (templates/<id>.png), so the registry stays valid no matter where the
checkout lives.

Usage:
    python scripts/migrate_stickers_to_accounts.py --dry-run
    python scripts/migrate_stickers_to_accounts.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import sticker_store  # noqa: E402

LEGACY_ROOT = ROOT / "data" / "wechat"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def _yinyue_account(platform: str, candidates: list) -> str:
    """Which account should adopt the anonymous root index."""
    try:
        from core.bootstrap import get_meta_repo

        rows = get_meta_repo().account_registry.list_by_platform(platform)
        for row in rows:
            if getattr(row, "role", "") == "yinyue":
                return row.account_id
    except Exception:
        pass
    return candidates[0] if len(candidates) == 1 else (candidates[0] if candidates else "")


def _looks_like_sticker_source(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_SUFFIXES


def plan() -> dict:
    """Work out what would move, without touching anything."""
    stores = sticker_store.list_stores()
    account_ids = [s.account_id for s in stores if s.platform == "wechat"]

    legacy_accounts = []
    if LEGACY_ROOT.exists():
        for child in sorted(LEGACY_ROOT.iterdir()):
            if not child.is_dir() or child.name in ("ui_templates", "sticker_templates",
                                                    "debug_capture", "diagnose",
                                                    "match_debug_live"):
                continue
            if (child / "sticker_index.json").exists() or (child / "sticker_templates").exists():
                legacy_accounts.append(child)

    root_index = LEGACY_ROOT / "sticker_index.json"
    root_templates = LEGACY_ROOT / "sticker_templates"

    return {
        "stores": stores,
        "account_ids": account_ids,
        "legacy_accounts": legacy_accounts,
        "root_index": root_index if root_index.exists() else None,
        "root_templates": root_templates if root_templates.exists() else None,
        "anonymous_target": _yinyue_account("wechat", account_ids),
    }


def migrate_one(source_dir: Path, platform: str, account_id: str, *, apply: bool,
                backup_dir: Path | None, extra_index: Path | None = None,
                extra_templates: Path | None = None) -> dict:
    """Move one legacy account directory into its isolation home."""
    target = sticker_store.StickerStore(platform, account_id)
    index_candidates = [p for p in (extra_index, source_dir / "sticker_index.json") if p and p.exists()]

    registry: dict = {}
    if target.registry_path.exists():
        registry = json.loads(target.registry_path.read_text(encoding="utf-8"))

    moved_images = 0
    adopted = []

    for index_path in index_candidates:
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print("  ! 索引读不了 %s: %s" % (index_path, exc))
            continue

        source_templates = (index_path.parent / "sticker_templates")
        if not source_templates.exists() and extra_templates is not None:
            source_templates = extra_templates

        for emotion, entry in (data or {}).items():
            if not isinstance(entry, dict):
                continue
            raw = entry.get("template") or ""
            names = raw if isinstance(raw, list) else [raw] if raw else []

            new_templates = []
            for name in names:
                if not name:
                    continue
                candidate = Path(name)
                if not candidate.is_absolute():
                    candidate = ROOT / candidate
                if not candidate.exists():
                    candidate = source_templates / Path(name).name
                if not candidate.exists():
                    print("  ! 找不到模板 %s (%s)" % (name, emotion))
                    continue

                sticker_id = candidate.stem
                dest_name = sticker_id + candidate.suffix
                dest = target.templates_dir / dest_name
                if apply:
                    target.templates_dir.mkdir(parents=True, exist_ok=True)
                    if not dest.exists():
                        shutil.copy2(str(candidate), str(dest))
                moved_images += 1
                new_templates.append("templates/" + dest_name)

            if not new_templates:
                continue

            merged = dict(registry.get(emotion) or {})
            merged.update({
                "id": Path(new_templates[0]).stem,
                "tab": entry.get("tab", 2) if not isinstance(entry.get("tab"), str) else 2,
                "template": new_templates if len(new_templates) > 1 else new_templates[0],
            })
            for field in ("label", "aliases", "context", "scope"):
                if entry.get(field):
                    merged[field] = entry[field]
            if entry.get("thumb"):
                merged["thumb"] = "thumbs/" + Path(entry["thumb"]).name

            registry[emotion] = merged
            adopted.append(emotion)

    # learned thumbnails
    for thumb_dir in (source_dir / "sticker_thumbs",):
        if thumb_dir.exists():
            for image in sorted(thumb_dir.glob("*.png")):
                if apply:
                    target.thumbs_dir.mkdir(parents=True, exist_ok=True)
                    dest = target.thumbs_dir / image.name
                    if not dest.exists():
                        shutil.copy2(str(image), str(dest))
                moved_images += 1

    if apply and registry:
        target.root.mkdir(parents=True, exist_ok=True)
        target.registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"account_id": account_id, "emotions": adopted,
            "images": moved_images, "registry": str(target.registry_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="真的执行(默认只预演)")
    parser.add_argument("--dry-run", action="store_true", help="只预演,不改任何东西")
    parser.add_argument("--no-backup", action="store_true", help="跳过备份(不建议)")
    args = parser.parse_args()
    apply = args.apply and not args.dry_run

    info = plan()
    print("=" * 78)
    print("隔离布局下的账号仓库:")
    for s in info["stores"]:
        print("   %-8s %-24s registry存在=%s" % (s.platform, s.account_id, s.exists()))
    print()
    print("待迁移的旧目录:")
    for d in info["legacy_accounts"]:
        print("   %s" % d.relative_to(ROOT))
    if info["root_index"]:
        print("   匿名根索引 %s -> 归入账号 %s"
              % (info["root_index"].relative_to(ROOT), info["anonymous_target"]))
    print("=" * 78)
    print("模式:", "APPLY (会真的改动)" if apply else "DRY-RUN (不改动)")
    print()

    if apply and not args.no_backup:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = LEGACY_ROOT / ("_legacy_backup_" + stamp)
        backup.mkdir(parents=True, exist_ok=True)
        for item in ("sticker_index.json", "sticker_templates"):
            src = LEGACY_ROOT / item
            if src.exists():
                dest = backup / item
                if src.is_dir():
                    shutil.copytree(str(src), str(dest))
                else:
                    shutil.copy2(str(src), str(dest))
        for d in info["legacy_accounts"]:
            dest = backup / d.name
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(d), str(dest), dirs_exist_ok=True)
        print("已备份到 %s" % backup.relative_to(ROOT))
        print()

    results = []
    for source_dir in info["legacy_accounts"]:
        account_id = source_dir.name
        print("-> %s" % account_id)
        result = migrate_one(source_dir, "wechat", account_id, apply=apply, backup_dir=None)
        results.append(result)
        for emotion in result["emotions"]:
            print("     %s" % emotion)
        print("     图片 %d 张 -> %s" % (result["images"], result["registry"]))

    if info["root_index"] and info["anonymous_target"]:
        print()
        print("-> 匿名根索引 归入 %s" % info["anonymous_target"])
        result = migrate_one(LEGACY_ROOT, "wechat", info["anonymous_target"], apply=apply,
                             backup_dir=None, extra_index=info["root_index"],
                             extra_templates=info["root_templates"])
        for emotion in result["emotions"]:
            print("     %s" % emotion)
        print("     图片 %d 张" % result["images"])
        results.append(result)

    print()
    total_emotions = sum(len(r["emotions"]) for r in results)
    total_images = sum(r["images"] for r in results)
    print("合计: %d 个情绪, %d 张图" % (total_emotions, total_images))

    if apply:
        print()
        print("迁移后各账号仓库:")
        for s in sticker_store.list_stores():
            stickers = s.load()
            print("   %-8s %-24s %d 个" % (s.platform, s.account_id, len(stickers)))
            for emotion, sticker in sorted(stickers.items()):
                print("        %-8s images=%d" % (emotion, len(sticker.images(s.root))))
    else:
        print()
        print("这是预演。加 --apply 才会真的执行。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
