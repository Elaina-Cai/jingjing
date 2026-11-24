r"""八条不变式的机器检查（I1–I8）+ 重构计划自检。

跑法：
    .\.venv_it\Scripts\python.exe -X utf8 -u scripts\check_invariants.py

三种状态：
  PASS     断言成立（附证据：文件:行 / 约束名 / 运行结果）
  FAIL     断言被违反 → 退出码 1，不许提交
  PENDING  该条依赖尚未落地的阶段（I5 在 E0 落表后自动升级为实扫）；不阻断提交，但会打印验收口径

设计原则（与 docs/重构总纲.md §6 一一对应）：
  1) 只做静态/纯函数检查，不连生产库、不写任何文件；
  2) 每条断言只允许一个真因，避免"红了一大片但不知道改哪"；
  3) 白名单写在代码里并打印出来 —— 白名单是设计的一部分，不是临时豁免。
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PY_DIRS = ("core", "agents", "db", "web", "scripts", "tests")

results: list[tuple[str, str, str]] = []  # (状态, 编号, 说明)


def ok(tag: str, msg: str) -> None:
    results.append(("PASS", tag, msg))


def bad(tag: str, msg: str) -> None:
    results.append(("FAIL", tag, msg))


def pending(tag: str, msg: str) -> None:
    results.append(("PENDING", tag, msg))


def py_files() -> list[Path]:
    out: list[Path] = []
    for d in PY_DIRS:
        base = ROOT / d
        if base.is_dir():
            out.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(out)


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def scan(pattern: str, *, flags: int = 0) -> list[tuple[str, int, str]]:
    """返回 (相对路径, 行号, 该行去掉首尾空白)。"""
    rx = re.compile(pattern, flags)
    hits: list[tuple[str, int, str]] = []
    for p in py_files():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append((rel(p), i, line.strip()))
    return hits

PROD_DIRS = ("core", "agents", "db", "web")


def scan_prod(pattern: str, *, flags: int = 0) -> list[tuple[str, int, str]]:
    """只扫生产代码（core/agents/db/web），并排除本检查器自身。"""
    rx = re.compile(pattern, flags)
    hits: list[tuple[str, int, str]] = []
    for p in py_files():
        relpath = rel(p)
        if not relpath.startswith(PROD_DIRS):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append((relpath, i, line.strip()))
    return hits


# ---------------------------------------------------------------- I1 唯一写入口
WRITE_ENTRY = "core/terminal/event_store_db.py"
RAW_SQL_ALLOW = ("db/migrations.py", "scripts/")


def check_i1() -> None:
    inserts = scan_prod(r"insert\(TerminalEvent\)")
    files = sorted({f for f, _, _ in inserts})
    outside_counts = len(scan(r"insert\(TerminalEvent\)")) - len(inserts)
    if files == [WRITE_ENTRY]:
        ok("I1a", f"生产侧 insert(TerminalEvent) 只有一处：{files[0]}:{inserts[0][1]}（另有 {outside_counts} 处测试夹具/脚本）")
    else:
        bad("I1a", f"insert(TerminalEvent) 出现在 {files}（应只有 {WRITE_ENTRY}）")

    deletes = scan_prod(r"delete\(TerminalEvent\)")
    outside = [f for f, _, _ in deletes if f != WRITE_ENTRY]
    if outside:
        bad("I1b", f"ORM 删除出现在 {sorted(set(outside))}（只许 {WRITE_ENTRY} 的 clear()）")
    elif deletes:
        entry_src = (ROOT / WRITE_ENTRY).read_text(encoding="utf-8", errors="replace")
        gated = (
            "allow_clear: bool = False" in entry_src
            and "raise ClearForbiddenError" in entry_src
        )
        if not gated:
            bad(
                "I1b",
                f"{WRITE_ENTRY} 的 clear() 没有闸：整表删除可被无参调用（U-00 要求 "
                "clear() 需 allow_clear=True）",
            )
        else:
            ok(
                "I1b",
                f"ORM 删除仅 1 处，位于 clear()：{deletes[0][0]}:{deletes[0][1]}"
                "（clear() 需 allow_clear=True，测试/运维显式清空）",
            )
    else:
        ok("I1b", "没有任何 ORM 删除")

    raw = scan_prod(r"\b(UPDATE|DELETE\s+FROM)\s+terminal_events\b", flags=re.IGNORECASE)
    illegal = [(f, i) for f, i, _ in raw if not any(f == a or f.startswith(a) for a in RAW_SQL_ALLOW)]
    if illegal:
        bad("I1c", f"原始 SQL 改动账本出现在非白名单文件：{illegal[:5]}")
    else:
        where = sorted({f for f, _, _ in raw})
        ops = len({f for f, _, _ in scan(r"\b(UPDATE|DELETE\s+FROM)\s+terminal_events\b", flags=re.IGNORECASE)}) - len(where)
        ok("I1c", f"生产侧原始 SQL 改动 {len(raw)} 处，全在迁移：{where}；另有 {max(ops, 0)} 个脚本/测试文件属运维面（不计违规）")


# ---------------------------------------------------------------- I2 幂等
def check_i2() -> None:
    try:
        from db.meta_models import TerminalEvent
    except Exception as exc:  # pragma: no cover - 环境缺失时报清楚
        pending("I2", f"无法导入 db.meta_models（{exc!r}）")
        return
    need = {"platform", "bot_account_id", "chat_id", "external_msg_id"}
    found = None
    table = TerminalEvent.__table__
    for idx in table.indexes:
        cols = {c.name for c in idx.columns}
        if idx.unique and cols == need:
            found = idx.name
            break
    if found is None:
        for c in table.constraints:
            cols = {col.name for col in getattr(c, "columns", [])}
            if cols == need and c.__class__.__name__ == "UniqueConstraint":
                found = c.name
                break
    if found:
        ok("I2", f"唯一约束存在：{found}（platform+bot_account_id+chat_id+external_msg_id）")
    else:
        bad("I2", "TerminalEvent 缺少四列唯一约束（同一条外部消息可重复入账）")


# ---------------------------------------------------------------- I3 窗口可重建
def check_i3() -> None:
    import inspect

    from core.context.chat_window import WindowMessage, build_chat_window, build_window_parts

    if "now" not in inspect.signature(build_window_parts).parameters:
        bad("I3a", "build_window_parts 没有 now 参数（渲染会隐式依赖墙上时钟）")
        return
    ok("I3a", "build_window_parts / build_chat_window 均接受显式 now")

    pinned = datetime(2026, 10, 19, 23, 0, 0)
    rows = [
        WindowMessage(occurred_at=datetime(2026, 10, 19, 22, 14), speaker="P5", content="那个文件叫啥来着"),
        WindowMessage(occurred_at=datetime(2026, 10, 19, 22, 15), speaker="鲸鲸", content="课表_第8周.xlsx", is_self=True),
        WindowMessage(occurred_at=datetime(2026, 10, 19, 22, 16), speaker="P6", content="在呢在呢", is_pat=True),
        WindowMessage(occurred_at=datetime(2026, 10, 19, 22, 17), speaker="", content="撤回了一条消息", is_revoke=True),
    ]
    first = build_window_parts("audit:rebuild", messages=rows, now=pinned)
    second = build_window_parts("audit:rebuild", messages=rows, now=pinned)
    same = (
        first.text_full == second.text_full
        and first.text_digest == second.text_digest
        and first.rows == second.rows
        and [m.content for m in first.recent] == [m.content for m in second.recent]
    )
    if not same:
        bad("I3b", "同一 (rows, now) 两次渲染不一致（窗口不是纯函数）")
        return
    t1 = build_chat_window("audit:rebuild", messages=rows, now=pinned)
    t2 = build_chat_window("audit:rebuild", messages=rows, now=pinned)
    if t1 != t2:
        bad("I3b", "build_chat_window 两次结果不一致")
    else:
        ok(
            "I3b",
            f"同一 (rows, now) 两次渲染一致（full {len(first.text_full)} 字 / digest {len(first.text_digest)} 字 / rows {first.rows}）",
        )


# ---------------------------------------------------------------- I4 可以空手而归
def check_i4() -> None:
    from core.memory.recall_chain import fuse

    cases = {
        "全空": {},
        "四路皆空": {"vector": [], "fts": [], "keyword": [], "struct": []},
        "未注册路由有票": {"unknown_route": [0, 1, 2]},
    }
    for name, payload in cases.items():
        try:
            out = fuse(payload)
        except Exception as exc:
            bad("I4", f"fuse({name}) 抛异常：{exc!r}")
            return
        if out:
            bad("I4", f"fuse({name}) 返回了非空结果 {out}（等于拿最像的填空）")
            return
    ok("I4", "空输入/未注册路由都返回空，不会兜底补一条")

# ---------------------------------------------------------------- I5 每句可指回
def check_i5() -> None:
    mod = ROOT / "core/terminal/turn_trace.py"
    rep = ROOT / "scripts/report_turn_trace.py"
    if not mod.exists():
        pending(
            "I5",
            "尚无 core/terminal/turn_trace.py（E0 未落地）。验收口径：respond/ask 的每条 basis "
            "必须能定位到 event_id，抽样 50 条指回率 ≥90%",
        )
        return
    src = mod.read_text(encoding="utf-8", errors="replace")
    if '"basis"' not in src and "basis=" not in src:
        bad("I5", "turn_trace 已存在但没写 basis 字段：指回率无法计算（只存计数等于红线失效）")
        return
    if "UNIQUE" not in src.upper() and "PRIMARY KEY" not in src.upper():
        bad("I5", "turn_trace 未声明 turn_id 唯一性：重复写会产生双行")
        return
    if not rep.exists():
        pending("I5", "turn_trace 已落表，但 scripts/report_turn_trace.py 缺失（读数无出口）")
        return
    rsrc = rep.read_text(encoding="utf-8", errors="replace")
    if "指回率" not in rsrc:
        bad("I5", "报表脚本没有输出指回率口径")
        return
    ok("I5", f"turn_trace 落表且报表带指回率（{mod.relative_to(ROOT)} / {rep.relative_to(ROOT)}）")


# ---------------------------------------------------------------- I6 规范不由状态自改
PERSONA_ALLOW = ("core/speech/store.py", "web/", "scripts/")


def check_i6() -> None:
    calls = scan(r"create_persona_version\(")
    illegal = [
        (f, i) for f, i, _ in calls if not any(f == a or f.startswith(a) for a in PERSONA_ALLOW)
    ]
    if illegal:
        bad("I6a", f"人设版本写入出现在禁止位置：{illegal[:5]}")
    else:
        where = sorted({f for f, _, _ in calls})
        ok("I6a", f"create_persona_version 只在数据层/Web/脚本：{where}")

    orm_writes = scan(r"(insert|update)\(Persona\)")
    illegal_orm = [
        (f, i)
        for f, i, _ in orm_writes
        if not (f.startswith("web/") or f.startswith("scripts/") or f.startswith("db/") or f == "core/speech/store.py")
    ]
    if illegal_orm:
        bad("I6b", f"personas 表被非规范层写入：{illegal_orm[:5]}")
    else:
        ok("I6b", "personas/persona_versions 的写入点不含 agents/ 与 core/memory/")


# ---------------------------------------------------------------- 计划自检
def check_i7() -> list:
    """I7：extra 键必须先登记（strict 档抛错会被 fail-closed 吞掉）。"""
    try:
        from core.routing.extra_contract import EXTRA_CONTRACT
        from scripts.audit_extra_contract import TARGETS, scan_file
    except Exception as exc:  # noqa: BLE001 - 导入失败就是这条闸自己坏了
        bad("I7", f"无法导入契约审计：{type(exc).__name__}: {exc}")
        return
    unregistered: list[str] = []
    scanned = 0
    for path in TARGETS:
        if not path.exists():
            continue
        scanned += 1
        for key in sorted(scan_file(path)):
            if key not in EXTRA_CONTRACT:
                unregistered.append(f"{path.name}:{key}")
    if unregistered:
        bad("I7", f"{len(unregistered)} 个 extra 键未登记：{unregistered[:6]}")
    else:
        ok("I7", f"extra 键全部已登记（扫 {scanned} 个文件 / 契约 {len(EXTRA_CONTRACT)} 键）")


def check_plan() -> None:
    path = ROOT / "refactor_plan.yaml"
    if not path.exists():
        bad("P1", "refactor_plan.yaml 不存在")
        return
    try:
        import yaml
    except Exception as exc:  # pragma: no cover
        pending("P1", f"没有 PyYAML（{exc!r}）")
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    slices = data.get("slices") or []
    if not slices:
        bad("P1", "refactor_plan.yaml 里没有 slices")
        return
    ids = [str(s.get("id", "")) for s in slices]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        bad("P1", f"切片 id 重复：{dup}")
        return
    need = {"id", "stage", "title", "files", "acceptance", "rollback", "risk"}
    new_paths = set()
    for s in slices:
        for f in s.get("files") or []:
            spec = f if isinstance(f, str) else str(f)
            name, _, flag = spec.partition("::")
            if flag.strip().lower() in {"new", "新增"}:
                new_paths.add(name.strip().replace("\\", "/"))
    problems: list[str] = []
    for s in slices:
        miss = need - set(s)
        if miss:
            problems.append(f"{s.get('id')} 缺字段 {sorted(miss)}")
            continue
        if str(s["stage"]) not in {"E0", "E1", "E2", "E3", "E4"}:
            problems.append(f"{s['id']} 阶段非法：{s['stage']}")
        # 2026-11-22：已交付（done）或已被后续架构取代（superseded）的切片不再校验文件是否还在。
        # 这两类切片的文件常常是后来被删/搬走的模块（agents/conversational.py、
        # core/routing/turn_pipeline.py），继续校验只会让这一条长期假红。
        if str(s.get("status") or "").strip().lower() in {"done", "superseded"}:
            continue
        for f in s["files"] or []:
            spec = f if isinstance(f, str) else str(f)
            name, _, flag = spec.partition("::")
            mark = flag.strip().lower() in {"new", "新增"}
            target = ROOT / name.replace("\\", "/")
            if not mark and name.replace("\\", "/") not in new_paths and not target.exists():
                problems.append(f"{s['id']} 文件不存在：{name}")
        for cmd in s["acceptance"] or []:
            for m in re.findall(r"scripts[\\/][\w\-./\\]+\.py", str(cmd)):
                if not (ROOT / m.replace("\\", "/")).exists() and m.replace("\\", "/") not in new_paths:
                    problems.append(f"{s['id']} 验收引用不存在的脚本：{m}")
    if problems:
        bad("P1", f"{len(problems)} 处计划问题：{problems[:6]}")
    else:
        ok("P1", f"{len(slices)} 个切片字段齐全、文件/脚本都存在（阶段分布：{sorted({s['stage'] for s in slices})}）")



def check_i8() -> None:
    """I8：一个工具只许有一个效果等级（否则闸只能按最高档执行，读也被授权）。"""
    from core.tools.builtin import all_specs as _all_specs

    mixed = []
    for spec in _all_specs():
        levels = {getattr(e, "value", str(e)) for e in (spec.effects or ())}
        if len(levels) > 1:
            mixed.append("%s=%s" % (spec.name, sorted(levels)))
    if mixed:
        bad("I8", f"{len(mixed)} 个工具混挂效果：{mixed[:6]}")
    else:
        ok("I8", f"每个工具只有一个效果等级（扫 {len(_all_specs())} 个 spec）")


def main() -> int:
    check_i1()
    check_i2()
    check_i3()
    check_i4()
    check_i5()
    check_i6()
    check_i7()
    check_i8()
    check_plan()

    width = max(len(t) for _, t, _ in results)
    for state, tag, msg in results:
        mark = {"PASS": "✅", "FAIL": "❌", "PENDING": "⏳"}[state]
        print(f"{mark} {tag.ljust(width)}  {msg}")
    fails = [r for r in results if r[0] == "FAIL"]
    pend = [r for r in results if r[0] == "PENDING"]
    print(f"\n{'✅ 全绿' if not fails else f'❌ {len(fails)} 条被违反'}（PENDING {len(pend)}）")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
