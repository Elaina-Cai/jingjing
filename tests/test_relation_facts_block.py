"""`[关系事实]` 只放事实、`[人格]` 放「怎么对人」的恒定规矩。

2026-10-23：两块原来混在一起 —— 关系事实块里塞了 6 条规矩（@ 谁、提醒落库、
称呼边界…），模型会把规矩当"这一轮的事实"读，改一条也容易碰坏另一类。

这个守卫按**性质**锁，不按行数：
- 事实块：允许任意条事实（当前发言人 / 关系层级 / 亲密度…），不数条数；
- 事实块：**禁止**出现已分流出去的规矩句（逐条点名）；
- 人格块：必须承接「对外不叫主人」「群友礼貌热络」「普通联系人别提主人」。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.terminal.persona_brain import build_persona_blocks  # noqa: E402

FAILS: list[str] = []

#: 已分流出去的规矩句（在关系事实块里一律不许出现）
FORBIDDEN_IN_FACTS = (
    "以 @",
    "先调用工具落库",
    "不叫「主人」",
    "只属于主人的称呼",
    "不要称呼对方为「主人」",
    "回复简洁自然",
)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok " if ok else "FAIL ") + name + ("" if ok else f" {detail}"))
    if not ok:
        FAILS.append(name)


def _snapshot(*, is_group: bool = True, is_owner: bool = True):
    persona = SimpleNamespace(
        name="鲸鲸",
        identity={"身份": "狐狼侍从"},
        traits={"忠诚": 0.9, "狡黠": 0.7},
    )
    principal = SimpleNamespace(
        platform="wechat",
        person_id="wxid_owner",
        display_name="Elaina",
        relation_to_bot="owner",
        intimacy=0.9,
        is_group=is_group,
        is_owner=is_owner,
    )
    return SimpleNamespace(persona=persona, turn=None, policy=None), principal


def _blocks(*, is_group: bool = True, is_owner: bool = True) -> dict[str, str]:
    snap, principal = _snapshot(is_group=is_group, is_owner=is_owner)
    out: dict[str, str] = {}
    for block in build_persona_blocks(snap, is_group=is_group, principal=principal):
        head = block.split("\n", 1)[0].strip()
        out[head] = block
    return out


def t_facts_only() -> None:
    for group in (True, False):
        for owner in (True, False):
            blocks = _blocks(is_group=group, is_owner=owner)
            facts = blocks.get("[关系事实]", "")
            label = f"group={group} owner={owner}"
            check(f"[关系事实] 有事实行（{label}）", "当前发言人" in facts, facts[:80])
            leftovers = [item for item in FORBIDDEN_IN_FACTS if item in facts]
            check(f"[关系事实] 不含已分流的规矩（{label}）", not leftovers, str(leftovers))


def t_facts_not_capped() -> None:
    """不按行数截：事实有多少列多少（这里只锁"没有硬上限逻辑"）。"""
    src = (ROOT / "core" / "terminal" / "persona_brain.py").read_text(encoding="utf-8")
    seg_start = src.index("lines: list[str] = []")
    seg_end = src.index('blocks.append("[关系事实]')
    seg = src[seg_start:seg_end]
    check("[关系事实] 段里没有对 lines 做切片/限长", "lines[:" not in seg and "lines[-" not in seg, seg[-200:])


def t_persona_takes_the_rules() -> None:
    blocks = _blocks()
    persona = blocks.get("[人格]", "")
    for phrase in ("对外不叫「主人」", "对群友礼貌热络", "普通联系人"):
        check(f"[人格] 承接了「{phrase}」", phrase in persona, persona[:120])


def t_mechanical_rules_are_gone() -> None:
    src = (ROOT / "core" / "terminal" / "persona_brain.py").read_text(encoding="utf-8")
    check("不再往 prompt 写「以 @…开头」（机械侧负责）", "开头，让对方收到提醒" not in src)
    # 「提醒必须落库」这条**保留**了，但归位到 [人格]（只在群聊注入）——
    # 工具 schema 管不到"答应了却没调用"这种失职，所以它不能删，只能搬。
    # 注意：人格块也在同一个函数里、且排在关系事实之前 —— 所以不能从函数头切，
    # 要从**关系事实自己的** `lines` 初始化处切（取它前面最后一个）。
    _append_at = src.index('blocks.append("[关系事实]')
    _init_at = src.rindex("lines: list[str] = []", 0, _append_at)
    facts_seg = src[_init_at:_append_at]
    check("事实块里不再有「先调用工具落库」", "先调用工具落库" not in facts_seg)


def main() -> int:
    t_facts_only()
    t_facts_not_capped()
    t_persona_takes_the_rules()
    t_mechanical_rules_are_gone()
    print(f"\nRESULT {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())