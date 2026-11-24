# -*- coding: utf-8 -*-
"""校验 `refactor_plan.yaml` 的验收是否**真的能验本片**。

为什么需要它：U-04 原来声明了 `tests/test_delivery_receipt.py:: new`，但验收只跑
`run_all_tests.py --filter outbound`（那条命令根本不收集这个新文件）——于是
「一片可以零代码而通过验收」。这个脚本把这种空验收变成可见的红。

跑法：``python -X utf8 -u scripts/check_plan_acceptance.py``（`--strict` 把 WARN 也算失败）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILS: list[str] = []
WARNS: list[str] = []


def fail(msg: str) -> None:
    print(f"FAIL {msg}")
    FAILS.append(msg)


def warn(msg: str) -> None:
    print(f"WARN {msg}")
    WARNS.append(msg)


def load_plan() -> dict:
    import yaml

    return yaml.safe_load((ROOT / "refactor_plan.yaml").read_text(encoding="utf-8"))


def main() -> int:
    data = load_plan()
    slices = data.get("slices") or []
    if not slices:
        fail("refactor_plan.yaml 里没有 slices")
        print(f"RESULT {len(FAILS)} failed / {len(WARNS)} warn")
        return 1

    for s in slices:
        sid = str(s.get("id") or "?")
        # 每一片都必须交代「怎么证明我做到了」与「怎么退回去」
        for key in ("why", "title", "rollback", "risk"):
            if not str(s.get(key) or "").strip():
                fail(f"{sid} 缺 {key}")

        files = [str(f) for f in (s.get("files") or [])]

        def _is_new(spec: str) -> bool:
            return spec.partition("::")[2].strip().lower() in {"new", "新增"}

        # 本片**新增**的测试必须进验收；只是被本片顺带碰到的旧测试可以只提示
        decl_tests = [f.partition("::")[0].strip().replace("\\", "/") for f in files
                      if "test_" in f and _is_new(f)]
        touched_tests = [f.partition("::")[0].strip().replace("\\", "/") for f in files
                         if "test_" in f and not _is_new(f)]
        acc = [str(c) for c in (s.get("acceptance") or [])]
        if not acc:
            fail(f"{sid} 没有 acceptance")
            continue

        # ① 本片新增的测试文件必须出现在某条验收命令里
        for tp in decl_tests:
            base = Path(tp).name
            if not any(base in c for c in acc):
                fail(f"{sid} 声明了测试 {tp}，但验收里没有任何一条跑它")
        for tp in touched_tests:
            base = Path(tp).name
            if not any(base in c for c in acc):
                warn("plan_acceptance_old_test_untracked", sid=sid, test=tp)

        # ② 直接跑测试的命令必须紧跟 RESULT 0 failed 的期望
        joined = "\n".join(acc)
        for i, line in enumerate(acc):
            if "tests" in line and "test_" in line and "cmd:" in line:
                nxt = acc[i + 1] if i + 1 < len(acc) else ""
                if "expect:" not in line and "expect:" not in nxt:
                    fail(f"{sid} 的测试命令没有紧跟 expect：{line[:80]}")

        # ③ 「只跑全量回归的过滤器」不算验本片
        points_to_self = any(
            any(Path(f.split('::')[0].strip()).name in c for f in files) for c in acc
        )
        if not points_to_self:
            warn("plan_acceptance_no_overlap", sid=sid)

        # ④ `:: new` 标记过期：文件已经存在了
        for f in files:
            name, _, flag = f.partition("::")
            if flag.strip().lower() in {"new", "新增"} and (ROOT / name.strip()).exists():
                warn("plan_acceptance_stale_new_mark", sid=sid, name=name.strip())

        # ⑤ 人工步骤必须写清判据（人工: 的 cmd 不许只有一句话）
        for c in acc:
            if "人工" in c and len(c) < 40:
                warn("plan_acceptance_manual_vague", sid=sid, step=c[:80])

        # ⑥ 「RESULT n/n passed」这种期望不可判定：必须给出具体数量或具体命令
        if "RESULT n/n passed" in joined and not any("RESULT 0 failed" in c for c in acc):
            warn("plan_acceptance_expected_shape", sid=sid)

    print(f"RESULT {len(FAILS)} failed / {len(WARNS)} warn")
    strict = "--strict" in sys.argv
    return 1 if FAILS or (strict and WARNS) else 0


if __name__ == "__main__":
    raise SystemExit(main())
