"""One-shot: repair base64-wrapped sticker labels in WeChat emoji registries.

背景：审计 D 分册发现部分 registry 条目的 label 存的是平台 protobuf 的 base64
（例如 点头 / 敬礼，遵命 -> 解码后是 "对"），导致渲染与提示词里出现脏串。
本脚本只做机械修复：label 是 base64 -> 解出其中可读中文，写回 label。
默认 dry-run，--apply 才写；写前整目录备份到 backups/sticker_registry_<stamp>/。
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import os
import re
import shutil

B64 = re.compile(r"^[A-Za-z0-9+/]{16,}={0,2}$")
READABLE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ffA-Za-z0-9]{1,12}")


def decode_label(raw: str) -> str:
    try:
        blob = base64.b64decode(raw + "=" * (-len(raw) % 4))
    except Exception:
        return ""
    text = blob.decode("utf-8", "ignore")
    parts = [m.group(0) for m in READABLE.finditer(text)]
    # 丢掉 protobuf 结构词，保留人可读的部分
    parts = [p for p in parts if p.lower() not in {"default", "proto", "type"}]
    return "".join(dict.fromkeys(parts))[:16]


def registries() -> list[str]:
    out: list[str] = []
    for root, _dirs, files in os.walk(os.path.join("data", "platforms")):
        if "registry.json" in files:
            out.append(os.path.join(root, "registry.json"))
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    total = 0
    for path in registries():
        data = json.load(io.open(path, encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        changed = []
        for name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label") or "").strip()
            if not B64.match(label):
                continue
            fixed = decode_label(label)
            if fixed and fixed != label:
                changed.append((name, label[:20], fixed))
                entry["label"] = fixed
        total += len(changed)
        print(f"{path}: {len(changed)} label(s) repairable")
        for name, old, new in changed:
            print(f"    {name!r}: {old!r} -> {new!r}")
        if args.apply and changed:
            bak = os.path.join("backups", f"sticker_registry_{stamp}", path.replace(os.sep, "__"))
            os.makedirs(os.path.dirname(bak), exist_ok=True)
            shutil.copy2(path, bak)
            with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
            print(f"    applied; backup -> {bak}")
    print(("APPLIED" if args.apply else "DRY-RUN") + f": {total} label(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
