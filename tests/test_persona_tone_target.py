# -*- coding: utf-8 -*-
"""语气目标「7 分迷之自信 3 分心软」：形态契约 + 夹具回归 + observe-only 守卫。

背景（主人 28 条真实回合标注，语气类是第二大问题）
--------------------------------------------------
* 核心目标：**「7 分迷之自信 3 分心软」**；
* 逐条失败形态（主人原话）：太凶（#12）／太毒舌、不好相处（#25）／推脱（#11）／
  说辞不像人话、缺人类式短反应（#3）／莫名其妙附和、太尬（#26）；
* 正例必须保住：会问、会解释、有原则（#1 / #4 / #13 / #14 / #15，主人标「懂」）。

为什么是"形态"而不是形容词
--------------------------
``docs/任务清单.md`` 的 RET-2 写死过："只写在 prompt 里的自然语言纪律 = 会被念出来 /
不起作用"。所以语气段**不许**是"要傲娇、不要毒舌"这种形容词堆：每条契约都必须带
①一个**可观察的锚**（数字或「」引起来的具体片段）②一个**可机检的形态探针**
（``core.speech.tone_target.PROBES``）。本文件把这两件事、加上"打掉失败形态 / 保住正例"
一起钉住。

owner / 落点
------------
* 契约表 + 探针：``core/speech/tone_target.py``（唯一 owner）；
* prompt 侧：``core/terminal/persona_brain.py::_tone_target_lines`` 把形态行折进**已登记**的
  ``[语气风格]`` 块（不新增块头 ⇒ 装配器登记表不动）；
* 出口侧：``core/speech/outbound.py::OutboundGateway._observe_tone_target``——**observe-only**
  （只写审计，不改文本、不拦发送、不在 ``GATE_RULES`` 里）。闸门拿词表当主判据是主人
  明确否掉的，所以本文件断言"它不是判据"，而不是"它拦得对"。

开关（**三态，默认 off**，开关位在 persona 数据而不是 env）
------------------------------------------------------------
``scene_styles.<scene>.tone_target`` = ``off`` / ``shadow`` / ``on``（缺省 off）：

* ``off``    不注入、不观测 —— 与接线前**逐字节一致**（用例 ``test_off_is_byte_identical``）；
* ``shadow`` 不注入 prompt（一个字不改），出口只写形态读数；
* ``on``     注入 ``[语气风格]`` + 出口写形态读数。

**本文件不钉任何 ``YINYUE_FEATURE_*`` 开关**：这条链的档位不在 env 里（见
``test_no_env_flag_and_only_one_owner``），所以"钉兄弟组"那条纪律在这里没有对象；
本文件的作用域是"抽出来的 ``[语气风格]`` 块"，别的块在不在都不影响这些断言。

跑法（双跑）::

    .\\.venv\\Scripts\\python.exe tests\\test_persona_tone_target.py
    .\\.venv_it\\Scripts\\python.exe -m pytest tests/test_persona_tone_target.py -q
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent

# 库/日志隔离（照 tests/test_addressee_guard.py:53-58）：本文件只用纯函数，但保持同一
# 纪律——绝不碰生产库 / 生产日志。
_ISO_ROOT = ROOT / "data" / "_test_persona_tone_target"
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(_ISO_ROOT / "platforms"))
os.environ.setdefault("DATA_DIR", str(_ISO_ROOT / "data"))
os.environ.setdefault("LOG_DIR", str(_ISO_ROOT / "logs"))

from core.speech import tone_target as T  # noqa: E402
from core.speech.domain import (  # noqa: E402
    AudienceKind,
    ConversationTarget,
    OutboundDraft,
    ResolvedPersona,
    SpeakingPolicy,
    SpeechSnapshot,
    TurnContext,
)
from core.speech.outbound import OutboundGateway  # noqa: E402
from core.terminal.persona_brain import build_persona_blocks  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "persona_tone_target_samples.json"
FIXTURE_V3 = ROOT / "tests" / "fixtures" / "persona_tone_target_samples_v3.json"
TURNS = ROOT / "output" / "人工评估" / "turns.jsonl"
STYLE_HEADER = "[语气风格]"
LEGACY_STYLE_LEAD = "按下面示例的节奏与语气说话，不要照抄具体词："
EXAMPLES = "在吗 → 在，咋啦？\n好累 → 咋啦？"

#: 纯形容词堆的形态（主人否掉的那种写法）：结构锁里禁止出现。
_ADJECTIVE_SOUP_RE = re.compile(
    r"要(温柔|傲娇|毒舌|可爱|凶|体贴|亲切|黏人|机灵)|(温柔|傲娇|毒舌)一点|像个?人一点"
)
#: 可观察的锚：数字，或「」引起来的具体片段。**每条契约行都必须有**。
_OBSERVABLE_RE = re.compile(r"[0-9０-９]|「[^」]{1,24}」")


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------
def _persona(scene_styles: dict | None = None) -> ResolvedPersona:
    return ResolvedPersona(
        persona_id=1,
        version=40,
        key="yinyue",
        name="鲸鲸",
        scene_styles=dict(scene_styles or {}),
    )


def _snapshot(persona: ResolvedPersona, *, is_group: bool = False) -> SpeechSnapshot:
    target = ConversationTarget(
        platform="wechat",
        account_id="wxid_bot",
        chat_id="12345678@chatroom" if is_group else "wxid_owner",
        chat_type="group" if is_group else "private",
        sender_name="主人",
    )
    audience = AudienceKind.GROUP_AMBIENT if is_group else AudienceKind.OWNER_PRIVATE
    turn = TurnContext(
        request_id="req-tone", target=target, audience=audience, msg_type="text"
    )
    policy = SpeakingPolicy(
        revision="rev-tone", target=target, audience=audience, persona=persona
    )
    return SpeechSnapshot(turn=turn, audience=audience, persona=persona, policy=policy)


def _persona_with_tone(mode: str | None, *, scene: str = "private") -> ResolvedPersona:
    block: dict = {"examples": EXAMPLES}
    if mode is not None:
        block[T.TONE_TARGET_KEY] = mode
    return _persona({scene: block})


def _style_blocks(snapshot: SpeechSnapshot, *, is_group: bool = False) -> list[str]:
    return [
        block
        for block in build_persona_blocks(snapshot, is_group=is_group)
        if block.strip().startswith(STYLE_HEADER)
    ]


def _style_block(snapshot: SpeechSnapshot, *, is_group: bool = False) -> str:
    """2026-10-23 四层化后 [语气风格] 块已退役 ⇒ 预期 0 块，返回空串。

    语气契约本身没死：`core/speech/tone_target.py` 仍是 owner，出口照常写读数；
    死掉的只是「把它拼进人设提示词」这一臂。
    """
    found = _style_blocks(snapshot, is_group=is_group)
    assert len(found) <= 1, f"[语气风格] 至多一块，实测 {len(found)}"
    return found[0] if found else ""


def _legacy_style_block() -> str:
    """接线前的 [语气风格] 逐字渲染（off 必须与它逐字节一致）。"""
    return f"{STYLE_HEADER}\n{LEGACY_STYLE_LEAD}\n{EXAMPLES}"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _fixture_v3() -> dict:
    return json.loads(FIXTURE_V3.read_text(encoding="utf-8"))


def _all_samples() -> list[dict]:
    return list(_fixture()["samples"]) + list(_fixture_v3()["samples"])


def _turns_replies() -> dict[int, str]:
    """``turns.jsonl`` 里的正文（只读；文件不在就返回空，夹具自带正文）。"""
    out: dict[int, str] = {}
    if not TURNS.exists():
        return out
    for line in TURNS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in row and "reply" in row:
            out[int(row["id"])] = str(row["reply"])
    return out


def _report(checks: list[tuple[str, bool]]) -> bool:
    ok = True
    for name, good in checks:
        ok &= bool(good)
        print(f"  {'✓' if good else '✗'} {name}")
    return bool(ok)


# ---------------------------------------------------------------------------
# ③ 结构锁：语气段是"行为/形态条目"，不是形容词堆
# ---------------------------------------------------------------------------
def test_contract_is_forms_not_adjectives() -> bool:
    print("\n[③ 结构锁：每条契约都必须可观察 + 可机检，不是形容词]")
    fixture_ids = {int(s["id"]) for s in _all_samples()}
    lines = T.contract_lines()
    checks: list[tuple[str, bool]] = [
        ("契约非空", len(T.ENTRIES) >= 8),
        ("渲染行数与条目一一对应", len(lines) == len(T.ENTRIES)),
    ]
    bad_probe = [e.key for e in T.ENTRIES if e.probe not in T.PROBES]
    checks.append(("每条都绑定登记过的形态探针", not bad_probe))
    bad_axis = [e.key for e in T.ENTRIES if e.axis not in (T.AXIS_SOFT, T.AXIS_PROUD, T.AXIS_BAN, T.AXIS_HUMAN)]
    bad_verdict = [e.key for e in T.ENTRIES if e.verdict not in (T.VERDICT_HAVE, T.VERDICT_NOT, T.VERDICT_ALLOW)]
    checks.append(("轴 / 判定取值合法", not bad_axis and not bad_verdict))
    no_anchor = [e.key for e in T.ENTRIES if not _OBSERVABLE_RE.search(e.text)]
    checks.append(("每条都带可观察的锚（数字或「」片段）", not no_anchor))
    soup = [e.key for e in T.ENTRIES if _ADJECTIVE_SOUP_RE.search(e.text)]
    checks.append(("没有纯形容词条目（要温柔/要傲娇…）", not soup))
    no_cover = [e.key for e in T.ENTRIES if not set(e.covers) <= fixture_ids or not e.covers]
    checks.append(("每条的 covers 都指向夹具里真实存在的回合", not no_cover))
    # 探针表与契约表**双向**覆盖：没有孤儿探针，也没有没进契约的禁止形态。
    used = {e.probe for e in T.ENTRIES}
    checks.append(("禁止形态探针全部进了契约", set(T.BANNED_PROBES) <= used))
    checks.append(("探针表没有孤儿", used == set(T.PROBES)))

    # V3 比例：标题写明「7 分迷之自信、3 分心软」；条目数不再当比例判据。
    proud = [e for e in T.ENTRIES if e.axis == T.AXIS_PROUD and e.verdict == T.VERDICT_HAVE]
    cute = [e for e in T.ENTRIES if e.axis == T.AXIS_SOFT and e.verdict == T.VERDICT_HAVE]
    bans = [e for e in T.ENTRIES if e.axis == T.AXIS_BAN]
    print(f"    比例读数：嚣张(该有)={len(proud)} 蠢萌(该有)={len(cute)} 禁={len(bans)}")
    checks.append(("标题把 V3 比例写成了可读数字", ("7 分迷之自信" in T.HEADING and "3 分心软" in T.HEADING)))
    return _report(checks)


# ---------------------------------------------------------------------------
# ① 夹具读数：失败样本的形态被读出来、正例干净
# ---------------------------------------------------------------------------
def test_fixture_reads_failures_and_positives_apart() -> bool:
    print("\n[① 夹具：5 条主人原话失败样本 + 2 条上级清单 + 5 条正例]")
    samples = _all_samples()
    turns = _turns_replies()
    checks: list[tuple[str, bool]] = [
        ("夹具非空", len(samples) >= 10),
        ("id 唯一", len({s["id"] for s in samples}) == len(samples)),
    ]
    verdicts = {s["id"]: s["verdict"] for s in samples}
    checks.append(("失败样本 >= 5", sum(1 for v in verdicts.values() if v == "failure") >= 5))
    checks.append(("正例 >= 5（旧 5 条 + V3 草案正例）", sum(1 for v in verdicts.values() if v == "positive") >= 5))
    bad_probe = [
        s["id"]
        for s in samples
        for name in list(s.get("expect_present") or []) + list(s.get("expect_absent") or [])
        if name not in T.PROBES
    ]
    checks.append(("夹具只引用登记过的探针", not bad_probe))
    if turns:
        drift = [s["id"] for s in samples if int(s["id"]) in turns and turns[int(s["id"])] != s["reply"]]
        checks.append(("夹具正文与 turns.jsonl 逐字一致", not drift))
    else:  # pragma: no cover - 只在夹具被单独拷走时
        print("    （turns.jsonl 不在，跳过逐字对账）")

    print("    壳 | 判定        | 命中形态                        | 主人原话")
    failure_hits = 0
    positive_hits = 0
    for sample in samples:
        readout = T.tone_readout(sample["reply"])
        present = list(sample.get("expect_present") or [])
        absent = list(sample.get("expect_absent") or [])
        ok_present = all(readout.forms[name] for name in present)
        ok_absent = all(not readout.forms[name] for name in absent)
        if sample["verdict"] == "failure":
            failure_hits += 1 if readout.hits else 0
            checks.append((f"#{sample['id']} 声明的禁止形态读出来了 {present}", ok_present and bool(readout.hits)))
            if absent:
                checks.append((f"#{sample['id']} 声明的缺形态确实缺 {absent}", ok_absent))
        else:
            positive_hits += 1 if readout.hits else 0
            checks.append((f"#{sample['id']} 正例的该有形态读出来了 {present}", ok_present))
            checks.append((f"#{sample['id']} 正例没有禁止形态", not readout.hits))
        print(
            f"    #{int(sample['id']):>2} | {sample['verdict']:<11} | "
            f"{','.join(readout.hits) or '-':<30} | {str(sample['owner_words'])[:28]}"
        )
    failures = sum(1 for v in verdicts.values() if v == "failure")
    checks.append((f"全部 {failures} 条失败样本都命中 >=1 条禁止形态", failure_hits == failures))
    checks.append(("5 条正例命中禁止形态 = 0", positive_hits == 0))
    # 双向覆盖：失败样本的每条禁止形态都要有对应的"不该有"条目，正例的每条形态都要有"该有"条目。
    entries = list(T.ENTRIES)
    missing_entry: list[str] = []
    for sample in samples:
        for name in sample.get("expect_present") or []:
            if sample["verdict"] == "failure":
                hit = any(e.probe == name and e.verdict == T.VERDICT_NOT for e in entries)
            else:
                hit = any(e.probe == name and e.verdict == T.VERDICT_HAVE for e in entries)
            if not hit:
                missing_entry.append(f"#{sample['id']}:{name}")
        for name in sample.get("expect_absent") or []:
            if not any(e.probe == name and e.verdict == T.VERDICT_ALLOW for e in entries):
                missing_entry.append(f"#{sample['id']}:{name}(缺)")
    checks.append(("夹具的每条形态都能在契约里找到对应条目（双向覆盖）", not missing_entry))
    return _report(checks)


# ---------------------------------------------------------------------------
# ② 正例不被"傲娇"吃掉：会问 / 会解释 / 有原则仍在
# ---------------------------------------------------------------------------
def test_positives_keep_asking_explaining_and_principles() -> bool:
    print("\n[② 正例的形态仍在：会问 / 会答 / 据实不改口]")
    samples = [s for s in _all_samples() if s["verdict"] == "positive"]
    positive_probes = {name for s in samples for name in (s.get("expect_present") or [])}
    have = {e.probe for e in T.ENTRIES if e.verdict == T.VERDICT_HAVE}
    ban = {e.probe for e in T.ENTRIES if e.verdict == T.VERDICT_NOT}
    proud = [e for e in T.ENTRIES if e.axis == T.AXIS_PROUD]
    checks = [
        ("正例的每个形态在契约里都是「该有」", positive_probes <= have),
        ("「不该有」只收禁止形态，不会把正例形态禁掉", not (positive_probes & ban)),
        ("禁止条目 == 禁止探针（一一对应）", ban == set(T.BANNED_PROBES)),
        ("「会问」在契约里（问句不被傲娇吃掉）", "has_question" in have),
        ("「嚣张」有据实不改口那一条，且读得出正例 #14", any(e.probe == "facts" and 14 in e.covers for e in proud)),
        ("允许条目给了「？」这种人类式短反应", any(e.probe == "bare_reaction" for e in T.ENTRIES if e.verdict == T.VERDICT_ALLOW)),
    ]
    # 正例读一遍：禁止形态必须一条都不沾（防"为了傲娇把正例改坏"）。
    for sample in samples:
        readout = T.tone_readout(sample["reply"])
        checks.append(
            (f"#{sample['id']} 正例读数无禁止形态且仍有该有形态", not readout.hits and all(readout.forms[n] for n in sample["expect_present"]))
        )
    return _report(checks)


# ---------------------------------------------------------------------------
# ⑤ off = 逐字节一致（三种人都可能给 off：缺省 / 显式 "off" / 未知值）
# ---------------------------------------------------------------------------
def test_off_is_byte_identical() -> bool:
    print("\n[⑤ off：不注入、与接线前逐字节一致]")
    legacy = _legacy_style_block()
    absent = _persona({"private": {"examples": EXAMPLES}})
    explicit = _persona_with_tone("off")
    checks = [
        ("缺省档位 = off", T.tone_mode(absent, "private") == T.MODE_OFF),
        ("显式 off = off", T.tone_mode(explicit, "private") == T.MODE_OFF),
        ("off 不产生 prompt 行", T.tone_prompt_lines(explicit, "private") == []),
        ("off/缺省都不产生风格块（块已退役）", _style_blocks(_snapshot(absent)) == [] and _style_blocks(_snapshot(explicit)) == []),
        ("退役后提示词里不再出现风格块头", legacy not in "\n".join(build_persona_blocks(_snapshot(absent)))),
        ("off 时也不出现语气形态标题", T.HEADING not in "\n".join(build_persona_blocks(_snapshot(explicit)))),
    ]
    for raw in ("0", "false", "no", "", "maybe", "  ", None, False):
        persona = _persona({"private": {"examples": EXAMPLES, T.TONE_TARGET_KEY: raw}})
        checks.append(
            (f"值 {raw!r} 一律按 off（未知值不猜）", T.tone_mode(persona, "private") == T.MODE_OFF and not T.tone_prompt_lines(persona, "private"))
        )
    # shadow：**不注入**（一个字都不改 prompt），但档位确实是 shadow（出口照常写读数）。
    shadow = _persona_with_tone("shadow")
    checks.append(("shadow 不注入 prompt", T.tone_prompt_lines(shadow, "private") == []))
    checks.append(("shadow 也不产生风格块", _style_blocks(_snapshot(shadow)) == []))
    checks.append(("shadow 档位读得出来", T.tone_mode(shadow, "private") == T.MODE_SHADOW))
    # on：只在 off 的块后面追加（前缀性质），且每行都在。
    on = _persona_with_tone("on")
    checks.append(("on 档位读得出来", T.tone_mode(on, "private") == T.MODE_ON))
    checks.append(("on 的契约行逐行取得到（owner 仍活）", T.contract_lines() == T.contract_lines() and bool(T.contract_lines())))
    checks.append(("但 on 也不再拼进人设提示词", _style_blocks(_snapshot(on)) == []))
    # 场景隔离：private 开、group 不开（数据按场景给）。
    checks.append(
        ("档位按场景取（group 没给 = off）", T.tone_mode(on, "group") == T.MODE_OFF and T.tone_mode(on, "private") == T.MODE_ON)
    )
    return _report(checks)


# ---------------------------------------------------------------------------
# ④ 出口读数：observe-only，且不是主判据
# ---------------------------------------------------------------------------
class _ExplodingReadout:
    """证明 off 下**连读数都不算**：被调用就计数并炸（调用点会吞异常，所以看计数）。"""

    def __init__(self) -> None:
        self.called = 0

    def __call__(self, *args, **kwargs):
        self.called += 1
        raise AssertionError("tone_target 档位 off 时不该算语气读数")


def test_readout_is_observe_only_not_a_gate() -> bool:
    print("\n[④ 出口读数：只写审计；不改文本、不拦发送、不是 GATE_RULES 的一条]")
    module_src = (ROOT / "core" / "speech" / "tone_target.py").read_text(encoding="utf-8")
    guards_src = (ROOT / "core" / "speech" / "guards.py").read_text(encoding="utf-8")
    outbound_src = textwrap.dedent(inspect.getsource(OutboundGateway._send_text_or_file))

    from core.speech.guards import GATE_RULES

    imported: set[str] = set()
    core_imports: set[str] = set()
    for node in ast.walk(ast.parse(module_src)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            if node.module.split(".")[0] == "core":
                core_imports.add(node.module)
    checks = [
        ("语气的 owner 模块只 import 标准库 + canonical 的归属 owner", imported <= {"__future__", "re", "dataclasses", "typing", "core"}),
        (
            "去 @点名走 canonical（core 侧只准 import core.conversation.addressing）",
            core_imports == {"core.conversation.addressing"}
            and "归属判定的 owner 只有" in module_src,
        ),
        (
            "owner 模块自己不带 @/[引用 归属正则",
            not [m for m in re.findall(r're\.compile\(\s*r?["\']([^"\']+)', module_src) if "@" in m],
        ),
        ("owner 模块里没有 PolicyDecision / dataclasses.replace", ("PolicyDecision" not in module_src and "dataclasses.replace" not in module_src)),
        ("出口闸门（guards）不引用语气读数（它不是 GATE_RULES 的一条）", "tone_target" not in guards_src),
        ("GATE_RULES 仍是既有清单、没有新增语气规则", GATE_RULES == ("group_max_chars", "unfounded_claim", "roster_leak")),
        ("发送路径上有且只有一处读数调用（挂在 @ 目标裁定之后）", outbound_src.count("self._observe_tone_target(") == 1),
    ]

    # 结构证据：调用点写成一条**语句**（返回值被丢弃）→ 不可能用它改草稿。
    tree = ast.parse(textwrap.dedent(inspect.getsource(OutboundGateway._send_text_or_file)))
    bare_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "attr", "") == "_observe_tone_target"
    ]
    checks.append(("读数调用是裸语句（结果被丢弃，不是赋值）", len(bare_calls) == 1))
    observer_src = textwrap.dedent(inspect.getsource(OutboundGateway._observe_tone_target))
    obs = ast.parse(observer_src)
    returns_with_value = [
        node for node in ast.walk(obs) if isinstance(node, ast.Return) and node.value is not None
    ]
    checks.append(
        (
            "读数方法不可能返回草稿/判定（所有 return 无值）",
            not returns_with_value
            and "dataclasses.replace" not in observer_src
            and "PolicyDecision" not in observer_src,
        )
    )

    # 行为证据（不跑完整发送链路）：off 不算读数；on 只写审计、草稿一个字不动。
    gateway = OutboundGateway.__new__(OutboundGateway)
    draft = OutboundDraft(
        request_id="req-tone",
        kind="text",
        target=_snapshot(_persona_with_tone("on")).turn.target,
        content="萨莉亚那是穷鬼套餐，你拿它比，赢了也不算本事。",
        who="12345678@chatroom",
    )
    original = draft.content
    saved_readout = T.tone_readout
    saved_record = None
    try:
        import core.observability.decision_trace as trace

        saved_record = trace.record_decision
        off_boom = _ExplodingReadout()
        T.tone_readout = off_boom
        gateway._observe_tone_target(draft, _snapshot(_persona_with_tone("off")))
        gateway._observe_tone_target(draft, _snapshot(_persona({"private": {"examples": EXAMPLES}})))
        checks.append(("off 下连探针都没跑（读数函数一次没被调用）", off_boom.called == 0))
        checks.append(("读数前后草稿逐字节不动", draft.content == original))

        captured: list[dict] = []
        trace.record_decision = lambda record, **kw: captured.append(dict(record)) or True

        def _fake_readout(text):
            return SimpleNamespace(
                forms={"banned_derogatory": True, "short": True},
                hits=("banned_derogatory",),
                chars=len(str(text)),
                sentences=1,
            )

        T.tone_readout = _fake_readout
        gateway._observe_tone_target(draft, _snapshot(_persona_with_tone("on")))
        checks.append(("on 下写出读数审计行", len(captured) == 1 and captured[0].get("kind") == "tone_target_readout"))
        checks.append(("审计行里 applied 恒 False（没有「改正」这个动作）", bool(captured) and captured[0].get("applied") is False))
        checks.append(("审计行带回读数（hits/scene/mode）", bool(captured) and list(captured[0].get("hits") or []) == ["banned_derogatory"] and captured[0].get("mode") == "on"))
        checks.append(("命中禁止形态也不改文本（草稿逐字节不动）", draft.content == original))

        on_boom = _ExplodingReadout()
        T.tone_readout = on_boom
        gateway._observe_tone_target(draft, _snapshot(_persona_with_tone("on")))
        checks.append(
            (
                "读数抛异常被吞掉、草稿不动（不可能因此拦下发送）",
                on_boom.called == 1 and draft.content == original,
            )
        )
    finally:
        T.tone_readout = saved_readout
        if saved_record is not None:
            import core.observability.decision_trace as trace

            trace.record_decision = saved_record
    return _report(checks)


# ---------------------------------------------------------------------------
# owner / 单一真源 / 没有 env flag
# ---------------------------------------------------------------------------
def test_no_env_flag_and_only_one_owner() -> bool:
    print("\n[owner：契约只有一份；档位在 persona 数据，不在 env；不新增块头]")
    features_src = (ROOT / "core" / "features.py").read_text(encoding="utf-8")
    persona_src = (ROOT / "core" / "terminal" / "persona_brain.py").read_text(encoding="utf-8")
    module_src = (ROOT / "core" / "speech" / "tone_target.py").read_text(encoding="utf-8")
    assembler_src = (ROOT / "core" / "context" / "assembler.py").read_text(encoding="utf-8")

    others = [
        path
        for path in (ROOT / "core").rglob("*.py")
        if path.name != "tone_target.py" and T.HEADING in path.read_text(encoding="utf-8", errors="ignore")
    ]
    # "有没有新增块头"：块头 = **整串就是 [中文名] 的双引号字面量**（``[语气风格]``）。
    # 正则字符类（``r"[0-9０-９]"`` / ``r"[^\s…]"``）不含中日韩汉字，天然被滤掉。
    header_literals = [
        item
        for item in re.findall(r'"(\[[^\]\n"]{1,24}\])"', module_src)
        if re.search(r"[\u4e00-\u9fff]", item)
    ]
    rendered = "\n".join([T.HEADING, T.LEAD, *T.contract_lines()])
    checks = [
        ("契约标题只活在一个文件里（单一真源）", not others),
        ("没有新增 YINYUE_FEATURE_* 开关（档位在 persona 数据）", "tone_target" not in features_src),
        ("persona_brain 只消费 owner（不复制契约文本）", "tone_prompt_lines" in persona_src and T.HEADING not in persona_src),
        ("装配器里不再登记 [语气风格]（块已退役）", '("[语气风格]",' not in assembler_src),
        ("owner 模块里没有新的块头字面量（不新增块的证据）", not header_literals),
        ("渲染出来的形态段里没有以 [ 开头的行（不会被 _classify 当成新块）", not re.search(r"(?:^|\n)\s*\[", rendered)),
        ("[语气风格] 不再作为块出现（0 块）", _style_blocks(_snapshot(_persona_with_tone("on"))) == []),
    ]
    return _report(checks)


def test_short_is_a_criterion_not_a_hard_number() -> bool:
    """2026-10-10：「短」改成可判断的准则（接话 vs 交东西），不是全局硬数字。

    背景：旧文案「短——一次 1~3 句说完…不铺陈、不收尾总结」是**全局禁令**——
    讲笑话/讲故事/讲步骤都被压成一句梗（私聊实测：狐狸笑话被压、狼笑话 61 字无结构）。
    2026-10-23 四层化后 [基本表达] 块已退役，提示词里只剩语气契约这一处；
    本用例钉住的是：**接话→短；交一个完整的东西→由内容定长**，
    且这条准则不再以全局禁令的形式漏进人设提示词。
    """
    print("\n[⑥ 短=接话默认；交东西由内容定长（三处同一准则）]")
    entry = next(e for e in T.ENTRIES if e.key == "short")
    blocks = build_persona_blocks(_snapshot(_persona_with_tone("on")), is_group=False)
    prompt = "\n".join(blocks)
    checks = [
        ("tone 条目仍带可观察锚（数字）", bool(_OBSERVABLE_RE.search(entry.text))),
        ("tone 条目写明「接话」是短的适用范围", "接话" in entry.text),
        ("tone 条目写明完整内容由内容定长",
         "完整" in entry.text and ("由它自己决定" in entry.text or "由内容决定" in entry.text)),
        ("tone 条目不再有全局禁令（不铺陈）", "不铺陈" not in entry.text),
        ("短句探针仍是登记过的形态（观测口径不变）",
         entry.probe == "short" and "short" in T.PROBES),
        ("探针注释说明它只读「接话」面", "接话" in (T.probe_short.__doc__ or "")),
        ("人设提示词里不再有 [基本表达] 块", "[基本表达]" not in prompt),
        ("提示词里也不再出现全局禁令措辞", "不铺陈" not in prompt and "不要列 1234" not in prompt),
    ]
    return _report(checks)


def main() -> bool:
    print("=" * 64)
    print("persona tone target checks（7 分迷之自信 3 分心软）")
    print("=" * 64)
    tests = [
        test_contract_is_forms_not_adjectives,
        test_fixture_reads_failures_and_positives_apart,
        test_positives_keep_asking_explaining_and_principles,
        test_off_is_byte_identical,
        test_readout_is_observe_only_not_a_gate,
        test_no_env_flag_and_only_one_owner,
        test_short_is_a_criterion_not_a_hard_number,
    ]
    results = []
    for test in tests:
        try:
            results.append((test.__name__, bool(test())))
        except Exception:  # pragma: no cover - diagnostics only
            import traceback

            traceback.print_exc()
            results.append((test.__name__, False))
    print("\n" + "=" * 64)
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    print(f"{passed}/{len(results)} passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
