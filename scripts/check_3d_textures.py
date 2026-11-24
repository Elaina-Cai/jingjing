# -*- coding: utf-8 -*-
"""贴图 ↔ 文档 口径校验（防漂移）。

为什么存在：2026-10-19 发现 3D 观察器上有 6 处文字还在按旧口径写
（四段输出 / 每回合留痕 / 没有 ui_control / 候选三次一致无「不同消息号」…），
因为文档先改、贴图后没跟上。这个脚本把「贴图文字与文档关键口径」变成断言，
改文档漏改贴图会在这里报红。

只读：不渲染、不改文件。退出码 0 = 一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "3d"
GEN_LLM = ROOT / "scripts" / "3d" / "render_llm_board.py"
GEN_BOARDS = ROOT / "scripts" / "3d" / "render_boards_round2.py"
GEN_TOOLBOX = ROOT / "scripts" / "3d" / "render_toolbox_panel.py"
GEN_SELF = ROOT / "scripts" / "3d" / "render_self_boards.py"
GEN_PERSONA = ROOT / "scripts" / "3d" / "render_persona_board.py"
GEN_JOB = ROOT / "scripts" / "3d" / "render_job_board.py"

# (板子说明, 生成脚本, 必须出现, 必须不出现)
TEXT_RULES = [
    ("判/投/记 板", GEN_LLM, ["三必填", "open_loop", "ui_control"],
     ["四段", "silent / abstain 也要留痕（带 reading"]),
    ("留痕条", GEN_BOARDS, ["判决行每回合", "完整留痕只在开口"], ["每回合落一行"]),
    ("候选纸片", GEN_BOARDS, ["不同消息号"], []),
    ("记忆②索引板", GEN_BOARDS, ["显式 scope"], []),
    ("账本板", GEN_BOARDS, ["追加新行"], []),
    ("感知层板", GEN_BOARDS, ["幂等", "失败留痕", "两个时钟"], []),
    ("出口板", GEN_BOARDS, ["无回执", "dry-run"], []),
    ("工具包板", GEN_TOOLBOX, ["ui_control", "默认 shadow"], []),
    ("态度板", GEN_SELF, ["不存数值", "消息号"], ["valence", "arousal"]),
    # 注意：「半衰期」是反向提及（"没有半衰期"），不能当旧口径禁掉
    ("情绪板", GEN_SELF, ["事件清单", "72 小时", "不存数值"], ["valence", "arousal"]),
    ("人设铭牌", GEN_PERSONA, ["只读", "版本化"], []),
    ("在手的事板", GEN_JOB, ["四槽", "安全点", "等批准"], ["双核"]),
]

# (板子, 贴图, 生成脚本, 文档, 文档里必须有的口径)
DOC_RULES = [
    ("判/投/记", "t_llm.png", "判投记_契约.md", ["三必填", "两行制"]),
    ("留痕条", "t_trace.png", "判投记_契约.md", ["判决行", "完整留痕"]),
    ("候选纸片", "t_mem_cand.png", "记忆立方体.md", ["不同外部消息号"]),
    ("记忆②索引板", "t_mem_b.png", "记忆立方体.md", ["不进常规检索"]),
    ("工具包板", "t_toolbox.png", "工具包.md", ["ui_control"]),
    ("出口板", "t_exit.png", "出口.md", ["不写死话术"]),
    ("感知层板", "t_perceive.png", "感知层.md", ["幂等", "失败留痕"]),
    ("外部世界横板", "t_outer_flat.png", "外部世界.md", ["外部软件", "落点"]),
    ("在手的事板", "t_job.png", "办_在手的事.md", ["安全点", "四槽"]),
]

# 贴图与 viewer 的 CSS 尺寸比例（2026-10-19 实测，容差 0.6%）
ASPECTS = {
    "t_llm.png": 560 / 560,
    "t_trace.png": 560 / 130,
    "t_ledger.png": 700 / 200,
    "t_perceive.png": 1300 / 620,
    "t_mem_a.png": 9.6 / 8.58,
    "t_mem_b.png": 9.6 / 8.58,
    "t_mem_c.png": 9.6 / 8.58,
    "t_mem_cand.png": 240 / 120,
    "t_exit.png": 300 / 220,
    "t_soft.png": 380 / 240,
    "t_job.png": 300 / 220,
}


def main() -> int:
    fails: list[str] = []
    checked = 0

    for name, gen, must, mustnot in TEXT_RULES:
        src = gen.read_text(encoding="utf-8")
        for m in must:
            checked += 1
            if m not in src:
                fails.append(f"{name}: 生成脚本缺「{m}」（{gen.name}）")
        for m in mustnot:
            checked += 1
            if m in src:
                fails.append(f"{name}: 生成脚本仍在用旧口径「{m}」（{gen.name}）")

    for name, png, doc, must in DOC_RULES:
        dp = ROOT / "docs" / "decisions" / doc
        dt = dp.read_text(encoding="utf-8")
        for m in must:
            checked += 1
            if m not in dt:
                fails.append(f"{name}: 文档 {doc} 里找不到口径「{m}」（贴图 {png} 可能已先行改过）")

    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover
        fails.append(f"PIL 不可用，无法校验贴图比例：{exc}")
    else:
        for png, want in ASPECTS.items():
            p = OUT / png
            checked += 1
            if not p.exists():
                fails.append(f"贴图缺失：{png}")
                continue
            with Image.open(p) as im:
                got = im.width / im.height
            if abs(got - want) / want > 0.006:
                fails.append(f"{png}: 比例 {got:.4f} 与 viewer 的 {want:.4f} 偏差 > 0.6%")

    total_rules = len(TEXT_RULES) + len(DOC_RULES) + len(ASPECTS)
    if fails:
        print(f"FAIL 贴图↔文档 一致性：{len(fails)} 条（共 {checked} 项断言 / {total_rules} 组规则）")
        for f in fails:
            print("  -", f)
        return 1
    print(f"PASS 贴图↔文档 一致性：{checked} 项断言全部通过"
          f"（{len(TEXT_RULES)} 组文字规则 / {len(DOC_RULES)} 组文档锚点 / {len(ASPECTS)} 张贴图比例）")
    return 0


if __name__ == "__main__":
    sys.exit(main())