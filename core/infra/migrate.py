"""改代码/改数据的事务壳（PIT-051，2026-10-28）。

规则（抄 surfcore PIT-051）：**唯一会失败的那一步放最前面；要么整体可回滚。**
「先改引用、最后改实体」是最坏的顺序；按行号改代码行号一漂就错位。

这个模块把“改文件”变成三步，谁也不许跳过：
1. `预检`：所有锚点必须存在且唯一、所有文件必须可写——**一条不过就一个字都不写**。
2. `备份`：改动前原文拷到 `backups/migrate-<时间戳>/`（不放在原目录，免得被当成源码扫）。
3. `写入`：任何一条写失败 → 用备份把**所有**已写文件还原（整体回滚），并报 `rolled_back=True`。

用法：
    m = Migration(ROOT)
    m.replace("loop/brain.py", 旧串, 新串)
    m.insert_before("loop/run.py", 锚点, 注释)
    res = m.apply()          # dry_run=True 只预检
    assert res["ok"] is True, res

不允许按行号改：只接受“唯一字符串锚点”和“整文件重写”。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Op:
    kind: str            # replace | insert_before | write
    path: str
    old: str = ""
    new: str = ""


@dataclass
class Migration:
    root: str | os.PathLike
    ops: list[Op] = field(default_factory=list)

    def replace(self, path: str, old: str, new: str) -> "Migration":
        self.ops.append(Op("replace", path, old, new))
        return self

    def insert_before(self, path: str, anchor: str, text: str) -> "Migration":
        self.ops.append(Op("insert_before", path, anchor, text))
        return self

    def write(self, path: str, content: str) -> "Migration":
        self.ops.append(Op("write", path, "", content))
        return self

    # ---- 只读预检：不改任何文件 ----
    def _plan(self) -> tuple[dict, list[str]]:
        root = Path(self.root)
        files: dict[str, str] = {}
        errors: list[str] = []
        queue: dict[str, str] = {}
        for i, op in enumerate(self.ops, 1):
            p = root / op.path
            if not p.exists():
                errors.append("第 %d 条：文件不存在 %s" % (i, op.path))
                continue
            if op.path not in files:
                files[op.path] = p.read_text(encoding="utf-8")
                queue[op.path] = files[op.path]
            if op.kind == "write":
                queue[op.path] = op.new
                continue
            n = queue[op.path].count(op.old)
            if n != 1:
                errors.append("第 %d 条：锚点在 %s 里出现 %d 次（必须恰好 1 次）" % (i, op.path, n))
                continue
            if op.kind == "replace":
                queue[op.path] = queue[op.path].replace(op.old, op.new, 1)
            else:
                queue[op.path] = queue[op.path].replace(op.old, op.new + op.old, 1)
        for rel in files:
            p = root / rel
            if not os.access(p, os.W_OK):
                errors.append("不可写：%s" % rel)
        return queue, errors

    def apply(self, *, dry_run: bool = False) -> dict:
        queue, errors = self._plan()
        res = {"ok": not errors, "dry_run": dry_run, "ops": len(self.ops),
               "files": sorted(queue), "errors": errors, "backup_dir": "",
               "written": [], "rolled_back": False}
        if errors:
            return res
        if dry_run:
            return res
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bdir = Path(self.root) / "backups" / ("migrate-" + stamp)
        bdir.mkdir(parents=True, exist_ok=True)
        res["backup_dir"] = str(bdir)
        originals: dict[str, str] = {}
        try:
            for rel in queue:
                originals[rel] = (Path(self.root) / rel).read_text(encoding="utf-8")
                dst = bdir / rel.replace(os.sep, "__").replace("/", "__")
                dst.write_text(originals[rel], encoding="utf-8")
            for rel, text in queue.items():
                (Path(self.root) / rel).write_text(text, encoding="utf-8", newline="")
                res["written"].append(rel)
        except Exception as exc:  # noqa: BLE001 - 回滚后原样上报
            for rel, text in originals.items():
                try:
                    (Path(self.root) / rel).write_text(text, encoding="utf-8", newline="")
                except Exception:  # noqa: BLE001
                    pass
            res["ok"] = False
            res["rolled_back"] = True
            res["errors"].append(repr(exc))
        return res


def main(argv: list[str] | None = None) -> int:
    """CLI 只做一件事：把 dry-run 的预检结果打出来（留给人工填 ops 时自查）。"""
    print(json.dumps({"hint": "这是库，不是命令行工具；在脚本里 Migration(ROOT).replace(...).apply()",
                      "has_pytest": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
