r"""把 `docs/` 里「数字 + 仓库口径」的断言打出来，和当前实测并排（只读报表，退出码恒 0）。

它当初是被一个**过滤器漏洞**造出来的：做 `docs/` 定量断言审计时，我先按「仓库口径名词」
（张 / 条 / 个模块 / 个旗标…）筛行，结果 `docs/目标与蓝图.md:112` 的「915 个 `.py` / 276,989 行」
既不在我的名词表里、也不在任何工具眼里，就这么漏了一天。所以这里反过来做：**先按单位抽
（个 / 张 / 条 / 行 / 例 / 道），再看左边有没有仓库名词**——它不会因为「我忘了把某个名词加进过滤表」而漏。

为什么要它：散文里的数字没有任何东西在核。`check_docs_consistency.py` 只核锚点，
`gen_repo_map.py --check` 只核它自己生成的那些数，剩下那一片（"注册表 75 个""七道门禁""表 81 张"）
每轮都是靠人肉 grep 才发现的。这个脚本把那一轮 grep 变成一行命令。

判据（写文档时也照这个问）：**这个数变了，会不会改变读者要做的事？**
会 → 留在散文里，但写清口径与取数方式（例："注册表 83 个、全部三态"）；
不会 → 指向生成物（"见 `现状地图.md` §8"），或者干脆不写数。

自动核算的口径只有四个（其余一律标 `manual` 交给人看）：
  migrations   `db/migrations.py` 里 `_mNNN` 函数个数
  flags        `core/features.py` 的 `FLAG_REGISTRY` 条数
  test_files   `tests/test_*.py` 文件数
  tablenames   `db/` 下 `__tablename__ = "..."` 声明处数（同名模型会重复声明 → 同时打"唯一表名"）

跑法：
    .\.venv_it\Scripts\python.exe -X utf8 -u scripts\report_doc_numbers.py
    ... --only mismatch   只打对不上的
    ... --include-generated   连 `docs/现状地图.md` 这种生成物一起扫（默认跳过）

这是**报表**，不是门禁：不进 `run_all_tests.py`、不阻断提交。理由同 `docs/重构进度.md` §五
的另两个体检工具 —— 先让它在没人盯的时候也稳定，再谈要不要接。

已知不覆盖：散文里**没有单位**的数（例如「耦合热点前三」里的名次）、以及代码块里那些
陈年注释——这两类没有可核的口径，靠人；报表只负责把「有单位的」全列出来。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
GENERATED = {"docs/现状地图.md"}

#: (口径, 正则, 取数字的组号)。数字与名词必须**贴着写**（"66 个迁移" / "迁移 62 个" / "注册表 75"），
#: 因为 "迁移 64" 指的是**第 64 号迁移**，不是"有 64 条迁移"——这个区别正是误报的来源。
AUTO_PATTERNS: tuple[tuple[str, str, int], ...] = (
    ("migrations", r"(\d+)\s*个\s*迁移(?!表|链|脚本|的)", 1),
    ("migrations", r"(?<!非法)(?<!合法)迁移\s*(\d+)\s*(?:个|条)", 1),
    ("flags", r"注册表\s*\*{0,2}(\d+)", 1),
    ("flags", r"(\d+)\s*个\s*三态(?:开关|旗标)?", 1),
    ("flags", r"`?TRI_STATE_FLAGS`?\s*(?:共)?\s*(\d+)", 1),
    ("test_files", r"(\d+)\s*个\s*独立脚本", 1),
    ("test_files", r"`?tests/test_\*\.py`?\s*(?:实测\s*)?\*{0,2}\s*(\d+)", 1),
    ("tablenames", r"表\s*\*{0,2}(\d+)\s*张", 1),
    ("tablenames", r"(\d+)\s*处\s*`?__tablename__", 1),
)

#: 认得出来、但现在算不了的口径 → 打 `manual`，让人当天自己去看。
MANUAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("checkers", r"\d+\s*个\s*检查器|\d+\s*道\s*门禁|`check_\*\.py`?\s*共\s*\d+|"
                 r"[一二三四五六七八九十]+\s*道\s*门禁"),
    ("invariants", r"[一二三四五六七八九十]+\s*条\s*(?:不变量|不变式)"),
    ("invariants", r"I1\s*[–\-—]\s*I6"),
    ("slices", r"\d+\s*条\s*切片|\d+\s*个\s*切片"),
    ("modules", r"\d+\s*个\s*(?:新)?模块"),
    ("loc", r"\d+\s*行\s*非空代码|\d+\s*个\s*`?\.py`?"),
    ("tests", r"\d+\s*个\s*(?:用例|断言|测试文件)"),
    ("ordinal", r"第\s*\d+\s*个"),
)

#: 这些提示词说明该行**故意**写历史数（"2026-10-22 23:51 · 455/455"），不报。
HISTORY_HINTS = ("历史", "此前", "当时", "上一轮", "那一轮", "2026-09-", "v0.")


CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(text: str) -> str:
    """把"七道门禁"里的"七"换成"7"，只为了在这一列里好比较；不做完整数字解析（十几这种很少见，见到就原样留着）。"""
    match = re.match(r"([一二三四五六七八九十]+)", text)
    if not match:
        return text
    word = match.group(1)
    value: int | None = None
    if word == "十":
        value = 10
    elif len(word) == 1:
        value = CN_DIGITS.get(word)
    elif len(word) == 2 and word[0] == "十":
        value = 10 + (CN_DIGITS.get(word[1]) or 0)
    elif len(word) == 2 and word[1] == "十":
        value = (CN_DIGITS.get(word[0]) or 0) * 10
    if value is None:
        return text
    return str(value) + text[len(word):]


#: 每个数字是怎么算出来的。报表要能自证，否则读者会重算一遍然后得到另一个数。
BASIS: dict[str, str] = {
    "migrations": "ast 解析 `db/migrations.py` 顶层 `def _mNNN` 的名字，按名字去重后计数",
    "flags": "`import core.features` 后取 `len(features.FLAG_REGISTRY)`（键是小写名，不带 `YINYUE_FEATURE_` 前缀）",
    "test_files": "`Path('tests').glob('test_*.py')` 的个数（不递归子目录）",
    "tablenames": "`db/**.py` 里正则 `__tablename__ = [\"']…[\"']` 的命中**处数**（同名模型在两套表里各声明一次，所以这个数大于表数）",
    "tablenames_unique": "同上，取引号里的表名做 set 去重后的个数",
}


def measure() -> dict[str, object]:
    data: dict[str, object] = {}

    try:
        tree = ast.parse((ROOT / "db" / "migrations.py").read_text(encoding="utf-8-sig"))
        data["migrations"] = len(
            {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and re.fullmatch(r"_m\d{3}", n.name)}
        )
    except (OSError, SyntaxError):
        data["migrations"] = None

    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from core import features  # noqa: PLC0415 - 报表工具，晚导入

        data["flags"] = len(features.FLAG_REGISTRY)
    except Exception:  # noqa: BLE001 - 一条口径算不出来不该拖垮整张报表
        data["flags"] = None

    data["test_files"] = len(list((ROOT / "tests").glob("test_*.py")))

    decls = 0
    unique: set[str] = set()
    for path in (ROOT / "db").rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r"""__tablename__\s*=\s*["']([^"']+)["']""", text):
            decls += 1
            unique.add(match.group(1))
    data["tablenames"] = decls
    data["tablenames_unique"] = len(unique)
    return data


def scan(path: Path, rel: str, data: dict[str, object]) -> list[tuple[str, int, str, str, object, bool | None]]:
    """返回 (相对路径, 行号, 口径, 文档写的数, 实测, 是否一致；None = 只能人工看)。"""
    out: list[tuple[str, int, str, str, object, bool | None]] = []
    in_fence = False
    for lineno, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if raw.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        line = raw.replace("**", "").replace("`", "")
        if in_fence:
            # 代码块里也常有陈旧口径（例如 `# I1–I6 + refactor_plan.yaml 自检`）。
            # 只认人工口径，不用自动口径去判代码示例里的数字，免得把示例输出当成断言。
            for label, pattern in MANUAL_PATTERNS:
                match = re.search(pattern, line)
                if match:
                    out.append((rel, lineno, label, cn_to_int(match.group(0)), None, None))
                    break
            continue
        if any(hint in line for hint in HISTORY_HINTS):
            continue
        hit_auto = False
        for label, pattern, group in AUTO_PATTERNS:
            for match in re.finditer(pattern, line):
                head = line[max(0, match.start() - 2): match.start()]
                if "第" in head and label == "flags":
                    # "第 91 个三态开关" 是序号不是计数：序号天然腐，正确修法是删掉序号而不是改成 83。
                    out.append((rel, lineno, "ordinal", match.group(0), None, None))
                    hit_auto = True
                    continue
                said = match.group(group)
                actual = data.get(label)
                same = None if not isinstance(actual, int) else actual == int(said)
                out.append((rel, lineno, label, said, actual, same))
                hit_auto = True
        if hit_auto:
            continue
        for label, pattern in MANUAL_PATTERNS:
            match = re.search(pattern, line)
            if match:
                out.append((rel, lineno, label, cn_to_int(match.group(0)), None, None))
                break
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 docs/ 里「数字 + 仓库口径」的断言与当前实测并排打出来（只读报表）",
        epilog="它不判断对不对：只把文档说的数和实测数并排摆出来，判定留给人；退出码恒 0。",
    )
    parser.add_argument("--only", choices=("all", "auto", "mismatch", "manual"), default="all")
    parser.add_argument("--include-generated", action="store_true", help="连生成物（现状地图）一起扫")
    args = parser.parse_args(argv)

    data = measure()
    items: list[tuple[str, int, str, str, object, bool | None]] = []
    for path in sorted(DOCS.rglob("*.md")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in GENERATED and not args.include_generated:
            continue
        items.extend(scan(path, rel, data))

    if args.only == "auto":
        items = [r for r in items if r[5] is not None]
    elif args.only == "mismatch":
        items = [r for r in items if r[5] is False]
    elif args.only == "manual":
        items = [r for r in items if r[5] is None]

    width = max((len(r[2]) for r in items), default=8)
    bad = 0
    for rel, lineno, label, said, actual, same in items:
        mark = "manual" if same is None else ("一致" if same else "不一致")
        if same is False:
            bad += 1
        shown = "?" if actual is None else str(actual)
        print(f"{rel}:{lineno}  {label.ljust(width)}  文档 {said:>6}  实测 {shown:>6}  {mark}")

    auto = sum(1 for r in items if r[5] is not None)
    print()
    print(f"共 {len(items)} 条：能自动核 {auto}（不一致 {bad}），只能人工看 {len(items) - auto}")
    print("实测基准（含取数口径，免得下一个人自己再算一遍并算出 84）：")
    for key, value in data.items():
        basis = BASIS.get(key, "?")
        print(f"  {key} = {value}    <- {basis}")
    print("（只读报表，退出码恒 0；不进 run_all_tests，接不接门禁由人定）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
