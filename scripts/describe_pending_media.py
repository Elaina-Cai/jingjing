"""Describe the images that were recovered, so history can show what they were.

Why this exists
---------------
Resolving an image file makes it visible *this turn*.  Being able to refer back
to it later needs a description, and a description is only produced when someone
actually looks at the picture.  Pictures recovered by the media backfill were
never looked at, so their transcript lines still read "[图片]" and the rolling
summary cannot mention them either.

This walks the media rows that have a file but no description, asks the vision
model once per distinct file, and stores the answer on the row.

Usage::

    python scripts/describe_pending_media.py --dry-run
    python scripts/describe_pending_media.py --limit 20
    python scripts/describe_pending_media.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

from core.conversation.media_registry import (  # noqa: E402
    pending_descriptions,
    remember_description,
)
from core.infra.envflags import flag

# 2026-10-26：与主循环（core.conversation.media._IMAGE_BRIEF_QUESTION）同一套问法。
# 原问法会被推理型视觉模型整段复述进 reasoning，写回后成了「角色：图片描述器。
# 输出要求：…」这类脚手架噪声。
PROMPT = ("用一句话说清这张图里最值得说的东西，30 字以内：画面主体是什么、"
          "在做什么，有文字就把文字读出来。不要寒暄，不要以「这张图」开头。"
          "不要写分析步骤或编号，只给最后那一句话。")

try:  # 2026-10-26：与主循环同款提示，让描述顺手写清辨识特征（零额外成本）
    from core.identity.self_look import describe_hint as _describe_hint

    _hint = _describe_hint()
    if _hint:
        PROMPT = PROMPT + ("\n画面里如果是动漫/卡通人物，把看得见的辨识特征并进那句话："
                           + _hint + "。看不出来就不写，不要猜。")
except Exception:  # noqa: BLE001
    pass

_SCAFFOLD = (
    "图片描述器", "输出要求", "输出限制", "不要客套话", "不超过", "内容要求",
    "长度限制", "风格限制", "分析用户请求", "字数", "≤", "角色：",
)


def _tidy(text: str) -> str:
    """去掉模型爱顺手写下的自我提示：「（30字）」「去掉…：」这类。"""
    out = str(text or "").strip()
    out = re.sub(r"[（(]\s*\d+\s*字\s*[)）]", "", out)
    out = re.sub(r"^[^：:]{0,40}?(?:去掉|只给|不要|字数|限制)[^：:]{0,40}?[：:]\s*", "", out)
    out = out.replace("**", "").strip(" -—·　")
    return out[:60]


def _looks_like_scaffold(text: str) -> bool:
    return any(marker in str(text or "") for marker in _SCAFFOLD)


def _last_description_line(raw: str) -> str:
    """兜底：从一整段推理里挑最后一句像描述的话。"""
    kept: list[str] = []
    for line in str(raw or "").splitlines():
        item = re.sub(r"^\s*(?:[-*•#]|\d+[.、)])\s*", "", line).replace("**", "").strip()
        if len(item) < 6 or _looks_like_scaffold(item):
            continue
        kept.append(item)
    return (kept[-1] if kept else "")[:60]


async def describe_file(uri: str) -> str:
    """问一次视觉模型，只要一句话。

    2026-10-26：原来走 ``llm.chat_vision(...)`` 直接拿返回值，而 deepseek-flash 这类
    推理型视觉模型把答案写在 ``reasoning_blocks`` 里、``text`` 是空串，于是每张图都
    被判成「模型返回空」。这里改用与主循环同一套取文与清洗（``_pick_vision_text`` /
    ``_clean_brief``），并把 max_tokens 放开，免得推理把输出预算吃光。
    """
    from core.bootstrap import get_llm_router
    from core.conversation.media import _clean_brief, _pick_vision_text

    router = get_llm_router()
    result = await router.call(
        "vision",
        agent="media_describe",
        task="vision",
        messages=[
            {"role": "system", "content": "你是图片描述器，只输出一句简短描述。"},
            {"role": "user", "content": PROMPT},
        ],
        images=[uri],
        temperature=0.2,
        max_tokens=600,
    )
    raw = _pick_vision_text(result)
    text = _tidy(_clean_brief(raw))
    if not text or _looks_like_scaffold(text):
        text = _tidy(_last_description_line(raw))
    return text


async def run(limit: int, dry_run: bool, db_path: str = "") -> int:
    from core.bootstrap import init_singletons
    from core.conversation.media import media_path_to_data_uri

    # 2026-10-26：裸调 init_singletons() 会去迁移**生产** meta 库，被 db/write_guard
    # 以 migration_blocked_production_path 拦住（本脚本根本不需要 meta 库）。
    # 这里把 meta/平台目录指到隔离根，只让描述写回 --db 指定的账号库。
    os.chdir(ROOT)
    iso = os.path.join(tempfile.gettempdir(), "yinyue_describe_isolate")
    os.makedirs(iso, exist_ok=True)
    os.environ.setdefault("DATA_DIR", iso)
    os.environ.setdefault("YINYUE_DATA_DIR", iso)
    os.environ.setdefault("META_DB_PATH", os.path.join(iso, "yinyue_meta.db"))
    os.environ.setdefault("PLATFORMS_DIR", os.path.join(iso, "platforms"))
    try:
        init_singletons()
    except Exception as exc:  # noqa: BLE001
        print(f"init_singletons 跳过：{str(exc)[:100]}")

    db = Path(db_path) if db_path else (
        ROOT / "data/platforms/wechat/wxid_seed02/account.db"
    )
    engine = create_engine("sqlite:///" + db.as_posix())

    # One vision call per distinct file: the same picture can back several rows
    # (sent twice, quoted later) and describing it twice would be waste.
    rows = pending_descriptions(engine, limit=max(limit * 4, 200))
    seen: dict[str, list] = {}
    for row in rows:
        path = str(row.local_path or "")
        if path and Path(path).exists():
            seen.setdefault(path, []).append(row)

    targets = list(seen.items())[:limit]
    print(f"待描述: {len(seen)} 个不同文件（本轮处理 {len(targets)}）")
    if dry_run:
        for path, group in targets:
            print(f"  {Path(path).name}  <- {len(group)} 行")
        return 0

    stored = failed = 0
    for index, (path, group) in enumerate(targets, start=1):
        uri = media_path_to_data_uri(path)
        if not uri:
            failed += 1
            print(f"  [{index}/{len(targets)}] {Path(path).name} 无法读取")
            continue
        try:
            description = (await describe_file(uri) or "").strip()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [{index}/{len(targets)}] {Path(path).name} 失败: {str(exc)[:70]}")
            continue
        if not description:
            failed += 1
            print(f"  [{index}/{len(targets)}] {Path(path).name} 模型返回空")
            continue
        short = " ".join(description.split())[:60]
        remember_description(engine, path, short)
        stored += 1
        print(f"  [{index}/{len(targets)}] {Path(path).name} -> {short}")

    remaining = len(pending_descriptions(engine, limit=10000))
    covered = 0
    try:
        from sqlalchemy import text

        with engine.connect() as con:
            total = con.execute(text(
                "select count(*) from message_media where local_path is not null"
            )).fetchone()[0]
            covered = con.execute(text(
                "select count(*) from message_media where local_path is not null "
                "and description is not null and description <> ''"
            )).fetchone()[0]
    except Exception:  # noqa: BLE001
        pass

    print(f"\n本轮写入 {stored}，失败 {failed}")
    print(f"有描述的媒体行: {covered}（还有 {remaining} 行待描述）")
    engine.dispose()
    return 0 if not failed else 1


def _ensure_isolation() -> None:
    """把 meta/平台目录指到隔离根后再干活。

    裸跑 init_singletons() 会去迁移**生产** meta 库，被 db/write_guard 以
    migration_blocked_production_path / production_write_blocked 拦住；而本脚本
    根本不需要 meta 库，只需要把描述写回 --db 给的账号库。配置在 import 期就会读
    环境变量，所以这里用 execve 重新起一个进程，而不是在本进程里 setenv。
    """
    if flag("YINYUE_DESCRIBE_CHILD") or "--allow-production" in sys.argv:
        return
    iso = os.path.join(tempfile.gettempdir(), "yinyue_describe_isolate")
    os.makedirs(iso, exist_ok=True)
    child_env = dict(os.environ)
    child_env.update({
        "YINYUE_DESCRIBE_CHILD": "1",
        "DATA_DIR": iso,
        "YINYUE_DATA_DIR": iso,
        "META_DB_PATH": os.path.join(iso, "yinyue_meta.db"),
        "PLATFORMS_DIR": os.path.join(iso, "platforms"),
    })
    # 用子进程转一遍（Windows 上 os.execve 会直接崩），退出码原样透传。
    rc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), *sys.argv[1:]], env=child_env
    ).returncode
    raise SystemExit(rc)


def main() -> int:
    _ensure_isolation()
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default="", help="账号库路径（默认微信那个）")
    parser.add_argument("--allow-production", action="store_true",
                        help="允许本进程直接碰生产 meta 库（仅服务入口该用）")
    args = parser.parse_args()
    return asyncio.run(run(args.limit, args.dry_run, args.db))


if __name__ == "__main__":
    raise SystemExit(main())
