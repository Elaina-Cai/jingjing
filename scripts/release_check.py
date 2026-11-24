# 发布前自检（2026-10-26）：私有资产 / 许可 / 署名
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.assets import paths  # noqa: E402

FORBIDDEN = (".pth", ".pt", ".ckpt", ".safetensors", ".index", ".onnx", ".wav", ".mp3", ".flac")
MAX_BYTES = 2 * 1024 * 1024
ALLOW_BIG = {"data/web_memes/memes_index.json"}


def tracked() -> list:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=str(REPO), capture_output=True,
                         text=True, encoding="utf-8", errors="replace").stdout
    return [x for x in out.split("\0") if x]


def scan_git() -> list:
    problems = []
    for f in tracked():
        if f.lower().endswith(FORBIDDEN):
            problems.append("git 跟踪了私有资产：" + f)
        p = REPO / f
        if f in ALLOW_BIG or not p.is_file():
            continue
        if p.stat().st_size > MAX_BYTES:
            problems.append("被跟踪文件超过 2MB：%s" % f)
    return problems


def scan_packs() -> tuple:
    rows, problems = [], []
    root = paths.asset_root()
    if not root.is_dir():
        return rows, problems
    slugs = set()
    for base in (root, root / paths.PRIVATE_ZONE):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.is_dir() and (child / "faces").is_dir() or (child.is_dir() and (child / "voice").is_dir()):
                slugs.add(child.name)
    for slug in sorted(slugs):
        kinds = [k for k in ("faces", "voice") if paths.kind_dir(slug, k)]
        blockers = paths.export_blockers(slug, kinds=kinds)
        rows.append({"slug": slug, "zone": paths.zone_of(slug), "kinds": kinds,
                     "redistributable": not blockers,
                     "blockers": blockers})
    return rows, problems


def notice(rows) -> str:
    lines = ["# NOTICE", "", "本项目代码可开源；下列资产**不随包分发**，使用者需自行准备。", ""]
    for row in rows:
        for b in row.get("blockers") or []:
            lines.append("- `%s/%s`：来源 %s；作者 %s；说明 %s"
                         % (row["slug"], b["kind"], b.get("source") or "未记录",
                            b.get("author") or "未记录", b.get("note") or "-"))
    return "\n".join(lines) + "\n"


def main() -> int:
    problems = scan_git()
    rows, more = scan_packs()
    problems += more
    for row in rows:
        print("[PACK] %-12s zone=%-7s kinds=%s redistributable=%s blockers=%s"
              % (row["slug"], row["zone"], ",".join(row["kinds"]),
                 row["redistributable"],
                 [(b["kind"], b["reason"]) for b in row["blockers"]]))
    out = REPO / "output" / "NOTICE.draft.md"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(notice(rows), encoding="utf-8")
        print("[NOTICE] wrote", out.relative_to(REPO).as_posix())
    except Exception as exc:  # noqa: BLE001
        print("[NOTICE] skip:", repr(exc)[:120])
    if problems:
        for p in problems:
            print("[FAIL]", p)
        return 2
    print("[OK] git 检查通过；%d 个包已登记许可状态" % len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
