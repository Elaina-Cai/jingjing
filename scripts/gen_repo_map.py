# -*- coding: utf-8 -*-
r"""生成 docs/现状地图.md —— 所有数字由脚本从代码抽取，不接受手改。

用法：.\.venv_it\Scripts\python.exe scripts\gen_repo_map.py
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "现状地图.md"

CODE_ROOTS = ["core", "agents", "db", "scheduler", "llm", "web", "tools", "scripts", "tests", "migrations", "notify", "bench"]
SKIP_PARTS = {".venv", ".venv_it", ".git", ".test_tmp", ".tmp_test", "node_modules", "_archive",
              "__pycache__", "data", "output", "backups", "logs", "napcat", ".dsh-meow", ".idea",
              ".ruff_cache", ".pytest_cache", "site-packages"}
SKIP_RE = re.compile(r"^(meme_lib_|meme_prov_|pip-|self_stop_test_|wg_log|hb_|tmp|pytest-of-|edge_cdp_)")


def py_files(base: Path) -> list[Path]:
    out = []
    for p in base.rglob("*.py"):
        if p.parent == ROOT and p.name.startswith("_"):
            continue  # 根目录 _*.py 是临时脚本，不计入项目代码
        parts = p.relative_to(ROOT).parts[:-1]
        if set(parts) & SKIP_PARTS:
            continue
        if any(SKIP_RE.match(x) for x in parts):
            continue
        out.append(p)
    return sorted(out)


def loc(p: Path) -> int:
    try:
        return sum(1 for l in p.read_text(encoding="utf-8-sig", errors="replace").splitlines() if l.strip())
    except OSError:
        return 0


def parse(p: Path):
    try:
        return ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"), filename=str(p))
    except SyntaxError:
        return None
    except OSError:
        return None  # 文件在扫描过程中被删/换掉（并发编辑），当解析失败处理


def unq(node) -> str:
    raw = ast.unparse(node).strip()
    return raw.strip('"').strip("'")

# ---------------- 抽取 ----------------
ALL = py_files(ROOT)
TOTAL_LOC = sum(loc(f) for f in ALL)

PER_ROOT = []
for r in CODE_ROOTS:
    d = ROOT / r
    if d.exists():
        fs = py_files(d)
        PER_ROOT.append((r, len(fs), sum(loc(f) for f in fs)))

SUB = defaultdict(lambda: [0, 0])
for f in py_files(ROOT / "core"):
    rel = f.relative_to(ROOT / "core")
    key = rel.parts[0] if len(rel.parts) > 1 else "(顶层)"
    SUB[key][0] += 1
    SUB[key][1] += loc(f)

PARSE_FAIL = [f.relative_to(ROOT).as_posix() for f in ALL if parse(f) is None]

INTERNAL = set(CODE_ROOTS)
IN_DEG: Counter = Counter()
ENTRIES: list[str] = []
for f in ALL:
    rel = f.relative_to(ROOT).as_posix()
    tree = parse(f)
    if tree is None:
        continue
    deps = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in INTERNAL:
                    deps.add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in INTERNAL:
            deps.add(node.module)
        elif isinstance(node, ast.If):
            src = ast.unparse(node.test)
            if "__name__" in src and "__main__" in src:
                ENTRIES.append(rel)
    mod = rel[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[:-9]
    deps.discard(mod)
    # 必须排序：deps 是 set[str]，迭代顺序受 PYTHONHASHSEED 影响，会让 IN_DEG 的插入顺序
    # （进而 most_common 里并列项的先后）每次跑都不同 —— 那 `--check` 就永远红。
    for d in sorted(deps):
        IN_DEG[d] += 1

FEAT = ROOT / "core" / "features.py"
FLAGS = []
ftree = parse(FEAT)
if ftree is not None:
    reg = None
    for node in ftree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "FLAG_REGISTRY":
            reg = node.value
        elif isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "FLAG_REGISTRY" for t in node.targets):
            reg = node.value
    if isinstance(reg, ast.Dict):
        for k, v in zip(reg.keys, reg.values):
            name = k.value if isinstance(k, ast.Constant) else unq(k)
            kind = owner = default = ""
            kill = ""
            if isinstance(v, ast.Call):
                args = [unq(a) for a in v.args]
                kind = args[0] if args else ""
                owner = args[1] if len(args) > 1 else ""
                default = args[2] if len(args) > 2 else ""
                kill = next((x for x in args if re.fullmatch(r"20\d\d-\d\d-\d\d", x)), "")
                for kw in v.keywords:
                    if kw.arg == "kill_date":
                        kill = unq(kw.value)
            FLAGS.append((str(name), kind, owner, default, kill))


# ---------------- 4.1 生产 .env 里的开关 ----------------
ENV_RAW = []
ENV_FLAGS = []
env_path = ROOT / ".env"
if env_path.exists():
    reg_names = {f[0].upper() for f in FLAGS}
    for line in env_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        m = re.match(r"\s*YINYUE_FEATURE_([A-Z0-9_]+)\s*=\s*(\S*)", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip().strip('"').strip("'")
        ENV_RAW.append((key, val))

CODE_TEXT = "\n".join(f.read_text(encoding="utf-8-sig", errors="replace") for f in ALL if f.name != "features.py")

LEGACY_CONSTS = {}
if ftree is not None:
    for node in ftree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                _v = node.value.value
                if re.fullmatch(r"[a-z][a-z0-9_]{2,}", _v):
                    LEGACY_CONSTS[node.targets[0].id] = _v

DB_ROWS = []
for f in sorted((ROOT / "db").glob("*.py")):
    tree = parse(f)
    if tree is None:
        continue
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        tname, cols = None, 0
        for st in node.body:
            if isinstance(st, ast.Assign) and any(getattr(t, "id", "") == "__tablename__" for t in st.targets):
                if isinstance(st.value, ast.Constant):
                    tname = str(st.value.value)
            if isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(st.value, ast.Call):
                if getattr(st.value.func, "id", "") in {"Column", "mapped_column"}:
                    cols += 1
        if tname:
            DB_ROWS.append((tname, cols, f.name))

MIGS = []
mtree = parse(ROOT / "db" / "migrations.py")
if mtree is not None:
    for node in mtree.body:
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and getattr(dec.func, "id", "") == "migration" and len(dec.args) >= 2:
                    try:
                        MIGS.append((int(ast.literal_eval(dec.args[0])), str(ast.literal_eval(dec.args[1])), node.name))
                    except Exception:
                        pass
MIGS.sort()

ENV_FILES = defaultdict(set)
ENV_RE = re.compile(r"YINYUE_[A-Z0-9_]+")
for f in ALL:
    txt = f.read_text(encoding="utf-8-sig", errors="replace")
    for name in ENV_RE.findall(txt):
        ENV_FILES[name].add(f.relative_to(ROOT).as_posix())

if ENV_RAW:
    _reg = {f[0].upper() for f in FLAGS}
    for _k, _v in ENV_RAW:
        _pat = r"(enabled\(\s*(?:\w+\.)?" + _k + r"\b|mode\(\s*(?:\w+\.)?" + _k + r"\b|YINYUE_FEATURE_" + _k + r"\b)"
        _has_read = bool(re.search(_pat, CODE_TEXT))
        if _k in _reg:
            _where = "注册表"
        elif _k in LEGACY_CONSTS:
            _where = "旧式布尔（有读取点）" if _has_read else "常量在、无读取点"
        elif ("YINYUE_FEATURE_" + _k) in ENV_FILES:
            _where = "仅字面量（无常量定义）"
        else:
            _where = "无引用"
        ENV_FLAGS.append((_k, _v, _where))

DOC_TERMS = set()
for p2 in (ROOT / "docs").rglob("*.md"):
    if p2.name == "现状地图.md":
        continue
    for term in re.findall(r"`([a-z_][a-z0-9_]{3,})`", p2.read_text(encoding="utf-8")):
        DOC_TERMS.add(term)
BLOB = "\n".join(f.read_text(encoding="utf-8-sig", errors="replace") for f in ALL)
MISSING = sorted(t for t in DOC_TERMS if t not in BLOB)

TEST_FILES = sorted((ROOT / "tests").glob("test_*.py"))
RED = ROOT / "docs" / "测试_既有红清单.md"
RED_CNT = len(re.findall(r"^\|\s*`?tests/", RED.read_text(encoding="utf-8"), re.M)) if RED.exists() else 0

BOM_PY = [f.relative_to(ROOT).as_posix() for f in ALL if f.read_bytes()[:3] == b"\xef\xbb\xbf"]

SCRATCH = sorted(f.name for f in ROOT.iterdir() if f.is_file() and f.name.startswith("_"))
JAR = sorted(d.name for d in ROOT.iterdir() if d.is_dir() and SKIP_RE.match(d.name))


try:
    HEAD = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=20).stdout.strip()
except Exception:
    HEAD = "?"

L = []
A = L.append
A("# 现状地图（自动生成）")
A("")
A("- 生成时间：`" + datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") + "`")
A("- 生成脚本：`scripts/gen_repo_map.py`（改脚本、重跑；不要手改本文件）")
A("- HEAD：`" + HEAD + "`")
A("")
A("> 所有数字与清单都由脚本从代码抽取。任何一处与代码不符，都应当被视为脚本 bug。")
A("")
A("## 0. 口径")
A("")
A("- 计入：" + "、".join("`" + r + "/`" for r in CODE_ROOTS))
A("- 排除：虚拟环境、`.test_tmp/.tmp_test`、`data/ output/ backups/ logs/`、`napcat/`（第三方）、`node_modules`、`_archive/`、`__pycache__`、以及顶层历史临时目录（`meme_*`/`pip-*`/`self_stop_test_*`/`wg_log*`/`hb_*`/`edge_cdp_*`/`pytest-of-*`/`tmp*`）")
A("- 命中：**" + str(len(ALL)) + "** 个 `.py`，**" + format(TOTAL_LOC, ",") + "** 行非空代码")
A("")
A("| 目录 | 文件 | 非空行 |")
A("|---|---:|---:|")
for name, n, ln in PER_ROOT:
    A("| `" + name + "/` | " + str(n) + " | " + format(ln, ",") + " |")
A("")
A("## 1. `core/` 子包")
A("")
A("| 子包 | 文件 | 非空行 |")
A("|---|---:|---:|")
for k, (n, ln) in sorted(SUB.items(), key=lambda kv: (-kv[1][1], kv[0])):
    A("| `core/" + ("" if k == "(顶层)" else k) + "` | " + str(n) + " | " + format(ln, ",") + " |")
A("")
A("## 2. 耦合热点（被内部导入次数）")
A("")
A("| 模块 | 被导入 |")
A("|---|---:|")
for mod, n in sorted(IN_DEG.items(), key=lambda kv: (-kv[1], kv[0]))[:25]:
    A("| `" + mod + "` | " + str(n) + " |")
A("")
A("## 3. 入口点（含 `if __name__ == \"__main__\"`）")
A("")
_uniq_entries = sorted(set(ENTRIES))
A("共 **" + str(len(_uniq_entries)) + "** 个文件含 `__main__` 入口，下面列出前 90 个：")
A("")
for e in _uniq_entries[:90]:
    A("- `" + e + "`")
A("")
A("## 4. 开关（`core/features.py::FLAG_REGISTRY`）")
A("")
A("共 **" + str(len(FLAGS)) + "** 个；tri **" + str(sum(1 for f in FLAGS if f[1] == "tri")) + "**、bool **" + str(sum(1 for f in FLAGS if f[1] == "bool")) + "**。")
A("")
A("| 开关 | 类型 | 默认 | owner | kill_date |")
A("|---|---|---|---|---|")
for name, kind, owner, default, kill in FLAGS:
    A("| `" + name + "` | " + kind + " | `" + default + "` | `" + owner + "` | " + (kill or "—") + " |")
A("")

A("### 4.1 生产 `.env` 里被显式设置的开关")
A("")
A("| 开关 | 值 | 代码里的位置 |")
A("|---|---|---|")
for key, val, where in sorted(ENV_FLAGS):
    A("| `" + key + "` | `" + val + "` | " + where + " |")
A("")
A("共 **" + str(len(ENV_FLAGS)) + "** 条：" + "、".join(
    str(sum(1 for x in ENV_FLAGS if x[2] == k)) + " 条" + k for k in ["注册表", "旧式布尔（有读取点）", "仅字面量（无常量定义）", "常量在、无读取点", "无引用"]) + "。")
A("")
A("说明：`注册表` = `FLAG_REGISTRY` 里有 FlagSpec（带 owner / kill_date）；`旧式布尔（有读取点）` = `core/features.py` 里是旧式布尔常量，且代码里确有 `enabled(<常量>)` / `mode(<常量>)` 或 `YINYUE_FEATURE_<NAME>` 字面量；`仅字面量（无常量定义）` = 只在代码里以 env 字面量出现；`常量在、无读取点` = 常量定义了但没人读，改它不生效；`无引用` = 全仓找不到。")
A("")
A("## 5. 数据库")
A("")
A("表 **" + str(len(DB_ROWS)) + "** 张；迁移 **" + str(len(MIGS)) + "** 条，最高 **" + (str(MIGS[-1][0]) if MIGS else "0") + "**。")
A("")
A("| 表 | 列数 | 定义于 |")
A("|---|---:|---|")
for t, c, f in DB_ROWS:
    A("| `" + t + "` | " + str(c) + " | `db/" + f + "` |")
A("")
A("最近 8 条迁移：")
A("")
A("| 版本 | 说明 | 函数 |")
A("|---:|---|---|")
for v, d, fn in MIGS[-8:]:
    A("| " + str(v) + " | " + d + " | `" + fn + "` |")
A("")
A("## 6. 测试")
A("")
A("- `tests/test_*.py`：**" + str(len(TEST_FILES)) + "** 个（独立脚本式）")
A("- 全量入口：`scripts/run_all_tests.py`（快照 + 隔离根，逐用例）")
A("- `docs/测试_既有红清单.md` 表格行：**" + str(RED_CNT) + "**")
A("")
A("## 7. 环境变量（代码出现过的 `YINYUE_*`）")
A("")
A("| 变量 | 出现文件数 |")
A("|---|---:|")
for name, fs in sorted(ENV_FILES.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:40]:
    A("| `" + name + "` | " + str(len(fs)) + " |")
A("")
A("共 **" + str(len(ENV_FILES)) + "** 个不同变量名。")
A("")
A("## 8. 文档写了、代码里没有")
A("")
A("从 `docs/` 抽反引号标识符 **" + str(len(DOC_TERMS)) + "** 个，其中 **" + str(len(MISSING)) + "** 个在代码里一处都没有：")
A("")
A("| 标识符 |")
A("|---|")
for t in MISSING[:80]:
    A("| `" + t + "` |")
A("")
A("## 9. 卫生")
A("")
A("- 根目录 `_*` 临时文件：**" + str(len(SCRATCH)) + "** 个")
A("- 带 UTF-8 BOM 的 `.py`：**" + str(len(BOM_PY)) + "** 个（`ast.parse` 前必须用 `utf-8-sig` 读，否则整份文件解析失败）")
A("- 根目录历史临时目录：**" + str(len(JAR)) + "** 个（" + ", ".join(JAR[:14]) + (" …" if len(JAR) > 14 else "") + "）")
for name in [".test_tmp", ".tmp_test", "backups", "output", "data", "logs"]:
    # 活体目录：测试一跑就在涨，写进库只有两种下场——要么每次生成都弄脏工作区，
    # 要么 --check 得把这几行白名单掉（那就不是真比对）。所以不写数。
    A("- `" + name + "/`：活体，不写数（随时被测试/运行改写，`--check` 不比对）")
A("")

# ---------------- 自检：用第二种方法数一遍 ----------------
def count_re(pat: str, paths) -> int:
    return sum(len(re.findall(pat, Path(x).read_text(encoding="utf-8"), re.M)) for x in paths)


C_MIG = count_re(r"^@migration\(", [ROOT / "db" / "migrations.py"])
C_TBL = count_re(r"__tablename__", sorted((ROOT / "db").glob("*.py")))
C_FLAG = count_re(r"FlagSpec\(", [ROOT / "core" / "features.py"])
try:
    GIT_TRACKED = set(subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, timeout=60).stdout.split())
    GIT_UNTRACKED = set(subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "*.py"], cwd=ROOT, capture_output=True, text=True, timeout=60).stdout.split())
    C_GIT, C_UNTRACKED = len(GIT_TRACKED), len(GIT_UNTRACKED)
except Exception:
    GIT_TRACKED, GIT_UNTRACKED = set(), set()
    C_GIT, C_UNTRACKED = -1, -1


def mark(a: int, b: int) -> str:
    return "OK" if a == b else "MISMATCH"


CHECKS = [
    ("迁移条数", "AST 装饰器", len(MIGS), "正则 ^@migration(", C_MIG),
    ("模型表数", "AST __tablename__", len(DB_ROWS), "正则 __tablename__", C_TBL),
    ("开关条数", "AST FLAG_REGISTRY", len(FLAGS), "正则 FlagSpec(", C_FLAG),
]

A("---")
A("")
A("## 10. 怎么用")
A("")
A("1. 改模块前先看 §2：`core/` 热点不能随手改签名。")
A("2. 加开关只动 §4；默认必须 off。")
A("3. 碰库看 §5：新增列必须走新迁移。")
A("4. §8 是「文档写了但代码没有」的清单——它就是重构要补的洞，不是已实现。")
A("5. §11 是同一份数据用两种方法数出来的对照；出现 MISMATCH 说明是脚本 bug，先修脚本。")
A("6. 不接受手改本文件：改脚本、重跑。")
A("")
A("## 11. 自检")
A("")
A("| 项目 | 方法 A | 数量 | 方法 B | 数量 | 结果 |")
A("|---|---|---:|---|---:|---|")
for name, ma, ca, mb, cb in CHECKS:
    A("| " + name + " | " + ma + " | " + str(ca) + " | " + mb + " | " + str(cb) + " | " + mark(ca, cb) + " |")
A("")
A("AST 解析失败：**" + str(len(PARSE_FAIL)) + "** 个" + ("（" + "、".join(PARSE_FAIL[:5]) + (" …" if len(PARSE_FAIL) > 5 else "") + "）" if PARSE_FAIL else "（全仓可解析）"))
A("")
ALL_REL = {p.relative_to(ROOT).as_posix() for p in ALL}
ONLY_GIT = sorted(GIT_TRACKED - ALL_REL)
ONLY_TREE = sorted(ALL_REL - GIT_TRACKED - GIT_UNTRACKED)
DELTA = len(ALL_REL) - C_GIT - C_UNTRACKED
A("本脚本命中的 **" + str(len(ALL)) + "** 个 `.py`（`CODE_ROOTS` 这 12 个目录里、排除 `SKIP_PARTS` / `SKIP_RE` 命中的）"
  " = git 已跟踪 **" + str(C_GIT) + "** + git 可见的未跟踪 **" + str(C_UNTRACKED) + "**"
  " − 生成器口径排除掉的跟踪文件 **" + str(len(ONLY_GIT)) + "**"
  " + 树里有、git 两套都不列的 **" + str(len(ONLY_TREE)) + "**"
  "（差值 **" + str(DELTA) + "**）。**差值为负是正常的**：恒等式是 `差值 = git 两套都不列的个数 − 口径排除的跟踪文件个数`，"
  "被排除的跟踪文件比前者多就是负数——不是脚本 bug。`git ls-files` 会列出本脚本口径之外的文件（例如 `SKIP_PARTS` 里的 `_archive/`、"
  "或不在 `CODE_ROOTS` 里的根目录脚本），这些不计入本脚本口径。")
if ONLY_GIT:
    A("")
    A("口径排除的跟踪文件（" + str(len(ONLY_GIT)) + " 个" + ("，前 12 个" if len(ONLY_GIT) > 12 else "") + "）："
      + "、".join("`" + x + "`" for x in ONLY_GIT[:12]) + (" …" if len(ONLY_GIT) > 12 else ""))
A("")
_TS_LINE_RE = re.compile(r"^- 生成时间：.*$", re.M)
_HEAD_LINE_RE = re.compile(r"^- HEAD：`.*$", re.M)
_DIR_LINE_RE = re.compile(r"^- `[^`]+/`：[\d,]+ 个文件，[\d.,]+ MB$", re.M)


def _mask_ts(text: str) -> str:
    """抹平三种「必然或频繁变化、与内容正确性无关」的行：--check 只关心内容。

    1. 「生成时间」：每次跑都不一样。
    2. 「HEAD」：记的是**生成时的** HEAD，而生成完才提交，所以哪怕内容一字不差也必然
       落后一个提交。
    3. §9 旧版留下的活体目录计数（`.test_tmp/`、`output/`、`logs/` … 的「N 个文件，X MB」）：
       2026-10-22 起 §9 已经不写数了（写死「活体，不写数」），这条抹平只是给旧文件兜底，
       留着无害。至此 --check 比对的每一行都是「由代码抽取、与运行期无关」的内容。
    """
    text = _TS_LINE_RE.sub("- 生成时间：`<忽略>`", text)
    text = _HEAD_LINE_RE.sub("- HEAD：`<忽略>`", text)
    return _DIR_LINE_RE.sub("- `<活体目录>`：<不比对>", text)


def _first_diff(current: str, generated: str) -> str:
    """给第一处不一致的上下文（各 2 行 + 两边的差异行）。"""
    a, b = current.splitlines(), generated.splitlines()
    for idx in range(max(len(a), len(b))):
        la = a[idx] if idx < len(a) else "<EOF>"
        lb = b[idx] if idx < len(b) else "<EOF>"
        if la != lb:
            out = ["第 " + str(idx + 1) + " 行起不一致："]
            for ctx in range(max(0, idx - 2), idx):
                out.append("  ctx " + str(ctx + 1).rjust(5) + "| " + a[ctx])
            out.append(" 磁盘 " + str(idx + 1).rjust(5) + "| " + la)
            out.append(" 生成 " + str(idx + 1).rjust(5) + "| " + lb)
            return "\n".join(out)
    return "行数不一致（磁盘 " + str(len(a)) + " 行 / 生成 " + str(len(b)) + " 行）"


def main(argv=None) -> int:
    """0 = 已写出 / 一致；1 = --check 发现不一致。"""
    parser = argparse.ArgumentParser(description="生成 docs/现状地图.md：数字全部由代码抽取，不接受手改")
    parser.add_argument("--check", action="store_true",
                        help="只比对不落盘：docs/现状地图.md 与本次生成结果不一致时打印第一处差异并退出 1")
    args = parser.parse_args(argv)
    text = "\n".join(L) + "\n"
    if args.check:
        if not OUT.exists():
            print("--check: 目标文件不存在：" + str(OUT))
            return 1
        current = OUT.read_text(encoding="utf-8")
        if current == text:
            print("--check: " + str(OUT.relative_to(ROOT)) + " 与生成结果逐字节一致（" + str(len(text.encode())) + " B）")
            return 0
        # 只差「生成时间」「HEAD」不算不一致：见 _mask_ts 的注释（否则一提交就红）。
        if _mask_ts(current) == _mask_ts(text):
            print("--check: " + str(OUT.relative_to(ROOT)) + " 内容一致（「生成时间」「HEAD」按设计不比对；§9 已不写数）")
            return 0
        print("--check: " + str(OUT.relative_to(ROOT)) + " 已与生成结果不一致，请重跑本脚本落盘")
        print(_first_diff(_mask_ts(current), _mask_ts(text)))
        return 1
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print("wrote", OUT.relative_to(ROOT), len(text.encode()), "B")
    print("files", len(ALL), "loc", TOTAL_LOC, "flags", len(FLAGS), "tables", len(DB_ROWS), "migs", len(MIGS),
          "env", len(ENV_FILES), "dterms", len(DOC_TERMS), "missing", len(MISSING))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
