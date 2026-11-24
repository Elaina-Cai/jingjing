#!/usr/bin/env python
"""文档一致性检查：文档 ↔ 3D 图 ↔ 契约。

规则分四类：
  A 几何：每份板文档里写的坐标/尺寸，必须在 output/3d/viewer_css.html 里真的存在
  B 术语：禁用已被撤回的说法（四段 / 判说记 / gap 字段），变更记录行除外
  C 常量：窗口、预算、量具阈值这类跨文档常量必须一致
  D 状态：已撤销的数值模型文档必须带状态横幅；每份文档必须有变更记录

用法：.venv_it\\Scripts\\python.exe -X utf8 scripts\\check_docs_consistency.py
     .venv_it\\Scripts\\python.exe -X utf8 scripts\\check_docs_consistency.py --report   # 只读锚点报告，恒 0
退出码 0 = 全绿。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DEC = DOCS / "decisions"
HTML = ROOT / "output" / "3d" / "viewer_css.html"

errors: list[str] = []
warns: list[str] = []


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _num(s: str):
    return float(s) if re.fullmatch(r"-?\d+(?:\.\d+)?", s) else s


def geom() -> dict[str, dict]:
    html = "\n".join(l for l in read(HTML).splitlines() if len(l) < 400)
    boards = {}
    for m in re.finditer(
        r"addBoard\(([\d.]+),\s*([\d.]+),\s*(-?[\d.]+),\s*(-?[\d.]+),\s*(\w+|-?[\d.]+),\s*'([^']*)'(?:,\s*'([^']*)')?\)",
        html,
    ):
        tag = m.group(7) or ""
        if tag:
            boards[tag] = (_num(m.group(1)), _num(m.group(2)), _num(m.group(3)), _num(m.group(4)), m.group(5))
    cubes = {
        m.group(3): (_num(m.group(1)), _num(m.group(2)))
        for m in re.finditer(r"makeWireCube\((-?[\d.]+),\s*(-?[\d.]+),\s*'([^']*)'\)", html)
    }
    # 自我立方体用 style.transform 直接定位；记忆立方体用 const MEM_X / MEM_Y
    m = re.search(r"cube\.style\.transform = 'translate3d\((-?[\d.]+)px,\s*(-?[\d.]+)px", html)
    if m:
        cubes["自我立方体"] = (_num(m.group(1)), _num(m.group(2)))
    m = re.search(r"const MEM_X = (-?[\d.]+), MEM_Y = (-?[\d.]+)", html)
    if m:
        cubes["记忆立方体"] = (_num(m.group(1)), _num(m.group(2)))
    flats = {
        (m.group(4) or "flat"): (_num(m.group(1)), _num(m.group(2)), _num(m.group(3)))
        for m in re.finditer(r"addFlat\(([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*'([^']*)'(?:,\s*'([^']*)')?\)", html)
    }
    return {"boards": boards, "cubes": cubes, "flats": flats}


G = geom()

# A 几何：文档里必须出现的原文串（与代码逐字一致）
GEOM_EXPECT = {
    "出口.md": ["addBoard(300, 220, 620, 0, LLM_Z, 't_exit.png', '出口')"],
    "软件适配器.md": ["addBoard(380, 240, 1050, 0, LLM_Z, 't_soft.png', '软件 · 适配器')"],
    "工具包.md": ["makeWireCube(950, 600)", "(-950, 600)"],
    "自我立方体.md": ["(-950, +600)", "-3.3 / 0 / +3.3"],
    "账本·事件流.md": ["(0, -600, 0)", "700 × 200", "自我 `-950`"],
    "世界窗口.md": ["translate3d(-250px, -300px, 0)"],
    "记忆立方体.md": ["MEM_X=780, MEM_Y=-320"],
    "感知层.md": ["y = 1000"],
    "外部世界.md": ["addFlat(1300, 620, 2000, 't_outer_flat.png')", "addSpine(1000, 1500)"],
    "办_在手的事.md": ["addBoard(300, 220, 620, -300, LLM_Z, 't_job.png', '在手的事')", "只送已批准", "任务预算"],
}

# B 术语：禁用说法（变更记录行里允许出现）
BAN = [
    (r"判\s*/\s*说\s*/\s*记", "板名应为「判 / 投 / 记」"),
    (r"强制四段|逐轮都要交四段", "输出已改为三必填 + 三条件"),
    (r"`?gap`?\s*(字段|分类)", "gap 字段已撤回（缺什么由工具调用表达）"),
]
ALLOW_LINE = ("变更记录", "v0.", "撤回", "改为", "已删除", "不再", "不设", "不是字段", "禁止", "不写")

# C 常量：文档 → 必须包含的串
CONST_EXPECT = {
    "世界窗口.md": ["40", "2 小时", "3600"],
    "记忆立方体.md": ["120", "400", "6"],
    "判投记_契约.md": ["90%", "5~15%", "≤2", "40 条"],
    "出口.md": ["重复发送", "0"],
    "工具包.md": ["≤2"],
    "账本·事件流.md": ["append-only", "幂等"],
    "办_在手的事.md": ["≤3", "≤60", "安全点", "100%"],
}

# D 状态横幅
BANNER_EXPECT = {
    "态度层_存储与读取.md": "状态（2026-10-20）",
    "情绪层_存储与读取.md": "状态（2026-10-20）",
    "人设层_存储与读取.md": "状态（2026-10-20）",
}

# 数值模型词：只允许出现在「带横幅的推导文档」和变更记录里
NUM_RE = re.compile(r"valence|arousal|±\s*0\.20|半衰期")
NUM_ALLOWED = {"态度层_存储与读取.md", "情绪层_存储与读取.md", "自我立方体.md", "判投记.md", "判投记_契约.md"}


def check_geometry() -> None:
    for name, needles in GEOM_EXPECT.items():
        p = DEC / name
        if not p.exists():
            errors.append(f"[A] 缺文件 {name}")
            continue
        t = read(p)
        for n in needles:
            if n not in t:
                errors.append(f"[A] {name} 未写与图一致的原文：{n!r}")
    # 反向：板子数量与图上注册一致
    n_boards = len(re.findall(r"addBoard\(", "\n".join(l for l in read(HTML).splitlines() if len(l) < 400)))
    if n_boards < 6:
        errors.append(f"[A] viewer_css.html 里只找到 {n_boards} 处 addBoard，可能被改动")
    for cube, xy in {"自我立方体": (-950, 600), "记忆立方体": (780, -320), "工具包": (950, 600)}.items():
        if G["cubes"].get(cube) != xy:
            errors.append(f"[A] 图上 {cube} 坐标是 {G['cubes'].get(cube)}，文档口径是 {xy}")


def check_terms() -> None:
    for p in sorted(DEC.glob("*.md")):
        for i, line in enumerate(read(p).splitlines(), 1):
            if any(a in line for a in ALLOW_LINE):
                continue
            for pat, why in BAN:
                if re.search(pat, line):
                    errors.append(f"[B] {p.name}:{i} {why}：{line.strip()[:60]}")


def check_consts() -> None:
    for name, needles in CONST_EXPECT.items():
        p = DEC / name
        if not p.exists():
            errors.append(f"[C] 缺文件 {name}")
            continue
        t = read(p)
        for n in needles:
            if n not in t:
                warns.append(f"[C] {name} 未出现常量 {n!r}（可能是换了写法，人工确认）")


def check_status() -> None:
    for name, mark in BANNER_EXPECT.items():
        p = DEC / name
        if p.exists() and mark not in read(p):
            errors.append(f"[D] {name} 缺状态横幅 {mark!r}")
    for p in sorted(DEC.glob("*.md")):
        if p.name in {"受话标注集_50条.md"}:
            continue
        t = read(p)
        if not re.search(r"(?m)^#{2,3}\s*[§\d.]*\s*变更记录", t):
            warns.append(f"[D] {p.name} 没有变更记录节")


def check_numbers() -> None:
    for p in sorted(DEC.glob("*.md")):
        if p.name in NUM_ALLOWED:
            continue
        for i, line in enumerate(read(p).splitlines(), 1):
            if any(a in line for a in ALLOW_LINE):
                continue
            if NUM_RE.search(line):
                errors.append(f"[D] {p.name}:{i} 出现已撤销的数值模型用词：{line.strip()[:60]}")



META_DOCS = [DOCS / "README.md", DOCS / "目标与蓝图.md", DOCS / "重构总纲.md",
             DOCS / "运行手册.md", DOCS / "设备与并发_实施手册.md"]
DEC_REF_RE = re.compile(r"docs/decisions/([^\s`)\]|]+?\.md)")
CONTRACT_DOCS = (
    "感知层.md", "账本·事件流.md", "世界窗口.md", "记忆立方体.md",
    "自我立方体.md", "判投记.md", "出口.md", "软件适配器.md", "工具包.md", "外部世界.md", "办_在手的事.md", "设备与并发.md",
)


def check_meta() -> None:
    """顶层元文档的连通性：只查"引用了不存在的东西"和"该互链的没链"。"""
    present = [p for p in META_DOCS if p.exists()]
    if not present:
        warns.append("[E] 顶层元文档一份都没有")
        return
    for p in present:
        text = read(p)
        for name in DEC_REF_RE.findall(text):
            if "*" in name:
                continue
            if not (DEC / name).exists():
                errors.append(f"[E] {p.name} 引用了不存在的文档：docs/decisions/{name}")
    spec = DOCS / "重构总纲.md"
    if spec.exists():
        text = read(spec)
        parts = text.split("## 1.", 1)
        sec1 = parts[1].split("## 2.", 1)[0] if len(parts) > 1 else ""
        for name in CONTRACT_DOCS:
            if name not in sec1:
                errors.append(f"[E] 重构总纲 §1 映射表未点名 {name}")
        for need in ("目标与蓝图", "docs/README.md"):
            if need not in text:
                errors.append(f"[E] 重构总纲 未与 {need} 互链")
    tp = DOCS / "目标与蓝图.md"
    if tp.exists() and "重构总纲" not in read(tp):
        errors.append("[E] 目标与蓝图 未与 重构总纲 互链")
    rp = DOCS / "README.md"
    if rp.exists():
        rt = read(rp)
        for name in ("目标与蓝图", "重构总纲"):
            if name not in rt:
                errors.append(f"[E] docs/README.md 索引未收录 {name}")


# ── 锚点校验：文档里写的 `文件.py:123` 是否还指向那一行 ──────────────
# 2026-10-22 加。先做只读报告模式：docs/ 里写死的行号会随代码增长而漂移
# （一次对账就抓到 5 处），但「合法但换了内容」和「真漂移」需要人看一眼才分得清，
# 所以 --report 只打印不判红；误报率可接受后再接成硬门禁。
ANCHOR_RE = re.compile(r"((?:[A-Za-z0-9_\-]+[/\\])*[A-Za-z0-9_\-]+\.py):(\d+)(?:\s*[-–]\s*(\d+))?")
ANCHOR_DOCS = [DOCS / "重构进度.md", DOCS / "现状地图.md", DOCS / "测试_既有红清单.md", *sorted(DEC.glob("*.md"))]
ANCHOR_HISTORY = "docs/decisions/"  # 历史文档：锚点按当时的事实记，不计漂移
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_ANCHOR_STOP = {
    "core", "docs", "scripts", "tests", "agents", "output", "data", "web", "db",
    "true", "false", "none", "self", "def", "class", "import", "from", "return",
    "async", "await", "try", "except", "with", "elif", "else", "while", "print",
    "raise", "yield", "lambda", "pass", "continue", "break", "not", "and", "or",
}
_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]+")


def _near_tokens(line: str, ref: str, at: int) -> list[str]:
    """锚点旁的关键字：ref 前后各 60 字内、且不属于任何其它锚点路径的标识符。

    - 限窗口：一行挂好几个锚点时，不把邻居的关键字算到自己头上。
    - 排掉别的锚点路径里的词：`A.py:1/2 与 B.py:3/4` 这种并列写法最常见。
    - 长的优先：私有函数名比 `payload` 这类泛词更能定位。
    """
    lo, hi = max(0, at - 60), min(len(line), at + len(ref) + 60)
    other = set()
    for m in ANCHOR_RE.finditer(line):
        if m.group(0) != ref:
            other.update(t.lower() for t in _PATH_TOKEN_RE.findall(m.group(1)))
    rest = line[lo:at] + " " + line[at + len(ref):hi]
    toks = {
        t.lower(): t
        for t in _TOKEN_RE.findall(rest)
        if t.lower() not in _ANCHOR_STOP and t.lower() not in other
    }
    return sorted(toks.values(), key=len, reverse=True)[:3]


def _resolve(path: str) -> Path | None:
    """解析锚点路径：全路径直接用；裸文件名（文档里「同前」写法）按唯一性补全。"""
    p = ROOT / path.replace("\\", "/")
    if p.exists():
        return p
    if "/" in path or "\\" in path:
        return None
    hits = sorted(q for q in ROOT.glob(f"**/{path}")
                  if ".git" not in q.parts and "output" not in q.parts)
    return hits[0] if len(hits) == 1 else None


def anchor_rows() -> list[tuple[str, int, str, str, str]]:
    rows: list[tuple[str, int, str, str, str]] = []
    for p in ANCHOR_DOCS:
        if not p.exists():
            continue
        rel = p.relative_to(ROOT).as_posix()
        for i, line in enumerate(read(p).splitlines(), 1):
            for m in ANCHOR_RE.finditer(line):
                ref = m.group(0)
                if rel.startswith(ANCHOR_HISTORY):
                    rows.append((rel, i, ref, "历史", "历史文档，只记录不计漂移"))
                    continue
                tgt = _resolve(m.group(1))
                n = int(m.group(2))
                if tgt is None:
                    rows.append((rel, i, ref, "缺文件", m.group(1)))
                    continue
                n2 = int(m.group(3)) if m.group(3) else n  # 区段锚点 `:34-42`
                lines = read(tgt).splitlines()
                if not 1 <= n <= len(lines):
                    rows.append((rel, i, ref, "越界", f"该文件共 {len(lines)} 行"))
                    continue
                kws = _near_tokens(line, ref, m.start())
                if not kws:
                    rows.append((rel, i, ref, "无关键字", "锚点旁没有可核的标识符"))
                    continue
                window = "\n".join(lines[max(0, n - 3): min(len(lines), n2 + 2)]).lower()
                hit = next((k for k in kws if k.lower() in window), "")
                if hit:
                    rows.append((rel, i, ref, "命中", hit))
                else:
                    rows.append((rel, i, ref, "内容已换", "该行 ±2 行内找不到 " + " / ".join(kws[:2])))
    return rows


def report() -> int:
    """只读报告模式：逐条打印「文档 → 锚点 → 实况」，恒 0。"""
    rows = anchor_rows()
    tally: dict[str, int] = {}
    for rel, i, ref, status, detail in rows:
        tally[status] = tally.get(status, 0) + 1
        print(f"{status:6} {rel}:{i} → {ref}" + (f"  [{detail}]" if detail else ""))
    order = ("命中", "历史", "无关键字", "内容已换", "越界", "缺文件")
    print("\n合计 " + " / ".join(f"{k} {tally[k]}" for k in order if tally.get(k)))
    return 0


def main() -> int:
    check_geometry()
    check_terms()
    check_consts()
    check_status()
    check_numbers()
    check_meta()
    print(f"图：{len(G['boards'])} 块板 / {len(G['cubes'])} 个立方体 / {len(G['flats'])} 块横板")
    for w in warns:
        print("WARN", w)
    for e in errors:
        print("FAIL", e)
    print(f"\n{'✅ 全绿' if not errors else f'❌ {len(errors)} 处不一致'}（WARN {len(warns)}）")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(report() if "--report" in sys.argv else main())