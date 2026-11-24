#!/usr/bin/env python
"""从 requirements*.txt 生成精确版本锁文件（requirements.lock）。

为什么不能直接 `pip freeze > requirements.lock`：
    本机环境混着整套语音训练工具链（demucs / bigvgan / ctranslate2 …），
    直接冻结会把这些无关依赖一并写进锁文件，换机器装出一堆用不到的东西。

做法：以 requirements*.txt 里的直接依赖为起点，用 importlib.metadata 递归求
传递闭包，只锁定真正会被装上的包。环境标记（如 linux-only 的 triton、
extras 里的 dev 依赖）按当前解释器求值后跳过。

用法：
    python scripts/gen_lock.py                 # 锁核心依赖 -> requirements.lock
    python scripts/gen_lock.py --check         # 只校验锁文件是否与当前环境一致
    python scripts/gen_lock.py --include-optional
"""
from __future__ import annotations

import argparse
import re
import sys
from importlib.metadata import PackageNotFoundError, requires, version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent


def direct_deps(files: list[str]) -> tuple[list[str], list[str]]:
    """返回 (可解析的直接依赖名, 原始声明行)。"""
    names, raw = [], []
    for fn in files:
        p = ROOT / fn
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            raw.append(f"{line}    # 来自 {fn}")
            req = Requirement(line)
            names.append(canonicalize_name(req.name))
    return names, raw


def closure(roots: list[str]) -> tuple[dict[str, str], list[str]]:
    """递归求传递闭包（**正确处理 extras**）。

    为什么必须处理 extras：像 `uvicorn[standard]` 这种带 extra 的依赖，其额外
    依赖（httptools / watchfiles / uvloop 等）由 marker `extra == "standard"` 控制。
    若按「marker 为假就跳过」的朴素做法，这些包会被漏掉，锁文件就不是完整快照
    —— 2026-09-27 真实踩到过：chromadb 依赖 uvicorn[standard]，漏锁了
    httptools / watchfiles。

    做法：对每个已解析的包，记住调用方请求了它的哪些 extras；遍历其 Requires-Dist
    时，用「无 extra」和「每个已请求 extra」分别求值 marker，任一为真即纳入，
    并把该依赖自身声明的 extras 继续往下传递。

    返回 ({规范化包名: 版本}, [未安装的直接依赖])。
    """
    resolved: dict[str, str] = {}
    extras_of: dict[str, set[str]] = {}
    missing: list[str] = []
    queue: list[tuple[str, frozenset[str]]] = [(n, frozenset()) for n in roots]

    while queue:
        name, extras = queue.pop()
        known = extras_of.setdefault(name, set())
        new_extras = set(extras) - known
        if name in resolved and not new_extras:
            continue
        known |= extras

        if name not in resolved:
            try:
                resolved[name] = version(name)
            except PackageNotFoundError:
                missing.append(name)
                continue

        for req_str in requires(name) or []:
            req = Requirement(req_str)
            if req.marker is None:
                active = True
            else:
                # marker 里可能引用 extra：无 extra 与每个已请求 extra 各试一次
                active = req.marker.evaluate({"extra": ""})
                if not active:
                    for e in known:
                        if req.marker.evaluate({"extra": e}):
                            active = True
                            break
            if active:
                queue.append((canonicalize_name(req.name), frozenset(req.extras)))

    return resolved, missing


def render(resolved: dict[str, str], raw: list[str], missing: list[str]) -> str:
    lines = [
        "# ============================================================",
        "#  鲸鲸 · 精确版本锁文件（自动生成，勿手改）",
        "# ============================================================",
        "#",
        "#  生成： python scripts/gen_lock.py",
        "#  安装： pip install -r requirements.lock",
        "#  校验： python scripts/gen_lock.py --check",
        "#",
        "#  说明：本文件锁定的是 requirements.txt 里直接依赖的【传递闭包】，",
        "#  不是 `pip freeze` 的全量输出 —— 本机环境混有语音训练工具链",
        "#  （demucs / bigvgan / ctranslate2 等），全量冻结会把无关依赖一起带进来。",
        "#",
        "#  用途：换机器 / 重建环境时保证装出完全一致的版本组合。",
        "#  更新依赖后请重跑生成器，别手改本文件。",
        "#",
        "# ------------------------------------------------------------",
        "#  直接依赖（改动请改 requirements.txt，不要改这里）",
        "# ------------------------------------------------------------",
    ]
    lines += [f"# {r}" for r in raw]
    lines.append("#")
    lines.append("# ------------------------------------------------------------")
    lines.append(f"#  传递闭包：共 {len(resolved)} 个包")
    lines.append("# ------------------------------------------------------------")
    for name in sorted(resolved):
        lines.append(f"{name}=={resolved[name]}")
    if missing:
        lines.append("")
        lines.append("# 以下直接依赖未安装在本机环境中，未能锁定版本：")
        lines += [f"#   {m}  （可选依赖，装后可重跑生成器补上）" for m in sorted(missing)]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    ap.add_argument("--include-optional", action="store_true",
                    help="把 requirements-optional.txt 也算进来")
    args = ap.parse_args()

    files = ["requirements.txt"]
    if args.include_optional:
        files.append("requirements-optional.txt")
    names, raw = direct_deps(files)
    resolved, missing = closure(names)
    content = render(resolved, raw, missing)

    target = ROOT / "requirements.lock"
    if args.check:
        if not target.exists():
            print("✗ requirements.lock 不存在，请先运行 python scripts/gen_lock.py")
            return 1
        if target.read_text(encoding="utf-8") == content:
            print(f"✓ requirements.lock 与当前环境一致（{len(resolved)} 个包）")
            return 0
        print("✗ requirements.lock 已过期，请重跑 python scripts/gen_lock.py")
        return 1

    target.write_text(content, encoding="utf-8", newline="\n")
    print(f"✓ 已写入 {target.relative_to(ROOT)}")
    print(f"  直接依赖 {len(names)} 个 -> 传递闭包 {len(resolved)} 个包")
    if missing:
        print(f"  未安装（未锁定）：{', '.join(sorted(missing))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
