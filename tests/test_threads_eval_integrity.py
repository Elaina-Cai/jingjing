# -*- coding: utf-8 -*-
"""话题簇评测集**量具完整性**：代号撞车根治 + 正文忠实 + 打分器两套口径并列。

这个文件守的不是"判定质量"，是**量具本身**。被咬过的地方（两轮独立盲标 + 真库比对
共同证实，2026-10-06）：

1. **代号命名空间撞车**（9 行 + 全域扫描补 1 行 = **10 行**）：``truth.anchor`` 写 ``P2``
   而真库作者不是 P2 —— ``data/eval/threads/name_map.json`` 里 ``P2=Elaina``、
   ``P7=P6``，而这 10 行的正文/真库里的人都是 **P7**。逐行证据见每行 ``notes`` 的
   ``【量具修复 2026-10-06】`` 段（旧值 / 新值 / 真库原文+sha256 / 依据）。
2. **正文偏离真库**：``t0095`` / ``t0098`` 与真库脱敏结果**逐字一致**（旧报告里的
   0.526/0.586 相似度是撞车造成的误读；现在用"逐字相等"这个更强的判据钉住）。
3. ``addressee`` 被压扁：规格 §3.1 允许 **0..n**，但打分器把空数组判 ERROR →
   "判不出来"这个形状写不进评测集。现在 ``[]`` 是**合法的 0**。

本文件覆盖的格子（对应交付验收）
--------------------------------
① 每行 ``person_id`` **非空**且**能解析到规范身份**（无代号歧义）；
② 10 行改动**逐行可查**（旧值/新值/依据），且其余 **90 行逐字节未动**（哈希清单）；
③ ``t0095`` / ``t0098`` 正文与真库一致（或差异显式注记）；``t0008`` 的复核结论留档；
④ 打分器 ``addressee=[]`` **合法**且被计数（不再 ERROR）；
⑤ **候选集口径**与**单值口径**并列输出，且**两者都确定性**（同输入两次同数）；
⑥ **七项指标**齐全：既有五项门槛一字未改 + 新增两项（胡编率 / 迟到率）门槛 ``N/A``；
⑦ **两项新指标的近例**（M2-6 ④）：迟到率 / 胡编率各一条真实事故行
   （出戏率与复述率已随语义判据退役，2026-10-24，见 A6）；
⑧ 新增 28 条样本**逐条可查**（真库原文 + sha256 + 唯一事件 id + 分布与 README 一致）；
⑨ 新增两项**分母 0 → ``n/a``**（不编数）、**两次跑同数**、且**永不 FAIL**。

诚实边界：⑤ 里脚本算出的候选集口径是**机械候选集**（自己的 @/引用/说话人），
**不是**盲标实验那个 0.869（那是两轮独立盲标的实测值，脚本里没有模型、算不出来）。
两者的差别写在 ``scripts/score_threads_eval.py`` 文件头，别把两个数混成一个。
⑦ 的诚实边界更要紧：**胡编率**在真实事故上读数是 **n/a**——不是"事故不存在"，
而是**现行判据看不见它**（她的回复里没有一句落进 claim 字段词表）。⑦ 把这件事钉住：
判据一旦补齐，这一格立刻变红，逼着重跑基线（正是 M2-6 ③ 的纪律）。

测试卫生
--------
* 本模块 import 期把**自己碰到的每一个开关**显式钉死（"env 未设"≠"关"，生产 ``.env``
  会是隐式输入；本仓已因这条被咬三次）；
* import 期就钉死**依赖**：``core.context.threads``（预测器）、``scripts.score_threads_eval``
  （打分器）、``db.parser``（canonical 拍一拍作者）都在模块顶层 import —— 缺依赖要在
  **收集期**就红，不是跑一半才 ImportError；
* 只读：真库/评测集**只读**打开；本文件不写任何文件（``--ledger-replay`` 那一格用
  **内存台账**，写生产库会被 ``db.write_guard`` 拦下——见 ⑦ 的 docstring）。

跑法::

    .\\.venv\\Scripts\\python.exe tests\\test_threads_eval_integrity.py
    .\\.venv_it\\Scripts\\python.exe -m pytest tests/test_threads_eval_integrity.py -q
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------- 开关钉死表（显式）
# 本文件**不读任何 feature flag 决定行为**，但 import 链会经过读开关的模块，
# 所以照样全部显式钉死：生产 .env 不许成为本用例的隐式输入。
_OFF = {
    "YINYUE_FEATURE_THREADS": "off",
    "YINYUE_FEATURE_CHAT_WINDOW": "off",
    "YINYUE_FEATURE_CONTEXT_ASSEMBLER": "on",
    "YINYUE_FEATURE_CONTEXT_BUDGET": "on",
    "YINYUE_FEATURE_PAT_GRACE": "off",
    "YINYUE_FEATURE_SEMANTIC_UNIT": "off",
    "YINYUE_FEATURE_GROUP_COALESCE": "off",
    "YINYUE_FEATURE_UNIT_MEDIA": "off",
    "YINYUE_FEATURE_TURN_OWNERSHIP": "off",
    "YINYUE_FEATURE_OUTPUT_GATE": "off",
    "YINYUE_FEATURE_ADDRESSEE_GATE": "off",
    "YINYUE_FEATURE_REPLY_INTENT": "off",
    "YINYUE_ADMISSION_MODE": "off",
    "YINYUE_FEATURE_MEMORY_GATE": "off",
    "YINYUE_FEATURE_UNCERTAIN_VOICE": "off",
    "YINYUE_FEATURE_WORK_ITEM_LEDGER": "off",
    "YINYUE_FEATURE_WORK_ITEM_RUNS": "off",
    "YINYUE_FEATURE_CONVERSATION_SHADOW": "off",
    "YINYUE_FEATURE_CONVERSATION_ACTIVE": "off",
    "YINYUE_FEATURE_PLAN_LOOP": "off",
    "YINYUE_POSITION_GATE": "off",
    # M2-6 新增四项里"胡编率"的判据开关：**必须钉死**——不钉的话生产 .env 会让
    # 台账读侧跑起来，这一格就从"n/a（判据关着）"变成看库内容（不可复现）。
    "YINYUE_FEATURE_CLAIM_LEDGER": "off",
}
os.environ.update(_OFF)

# --------------------------------------------------- 依赖在 import 期钉死（故意的）
from core.context import threads as T                       # noqa: E402
from db.parser import extract_pat_sender                     # noqa: E402
from scripts import score_threads_eval as S                  # noqa: E402
from scripts import _threads_eval_repair as REPAIR           # noqa: E402

DATASET = ROOT / "tests" / "golden" / "threads" / "eval_set.jsonl"
NAME_MAP = ROOT / "data" / "eval" / "threads" / "name_map.json"
ACCOUNT_DB = (ROOT / "data" / "platforms" / "wechat" / "wxid_seed02"
              / "account.db")
META_DB = ROOT / "data" / "yinyue_meta.db"
CHAT = "10000000001@chatroom"
AUDIT_MARK = "【量具修复 2026-10-06】"

#: 评测集**总量**（首版 100 + M2-6 补的 28 条近例行 = 128）。README §11 记同一批数。
TOTAL_ROWS = 128
#: M2-6（2026-10-06）补进 21:44–21:56 段的样本：**逐条可查**（见 `NEW_ROW_TAGS`）。
NEW_IDS = ("t0101", "t0102", "t0103", "t0104", "t0105", "t0106", "t0107", "t0108",
           "t0109", "t0110", "t0111", "t0112", "t0113", "t0114", "t0115", "t0116",
           "t0117", "t0118", "t0119", "t0120", "t0121", "t0122", "t0123", "t0124",
           "t0125", "t0126", "t0127", "t0128")
#: 两项新指标的**近例行**（M2-6 ④；口径见各自那一格）：type -> 样本 id。
NEAR_EXAMPLES = {
    "迟到率": ("t0096", "t0103"),                 # 21:46:01 回执 vs 21:44:59 自报 = 62s
    "胡编率": ("t0093", "t0103"),                 # 21:45:20 断言 vs 21:44:59 自报（判据够不着）
}


#: 本量具修复**动了值**的行（9 行撞车 + 全域扫描补的 t0090）。t0008 值未动、
#: 只留复核结论，所以**不在**这一组里（它在 PRESERVED 组的哈希清单里）。
FIXED_IDS = ("t0007", "t0008", "t0009", "t0011", "t0056", "t0057", "t0060",
             "t0090", "t0095", "t0098")
VALUE_CHANGED_IDS = ("t0007", "t0009", "t0011", "t0056", "t0057", "t0060",
                     "t0090", "t0095", "t0098")

#: 真值锚点的**旧值**（量具修复前，来自 git 里那份 pristine 评测集）。
OLD_ANCHOR = {
    "t0007": "P2", "t0009": "P2", "t0011": "P2", "t0056": "P2", "t0057": "P2",
    "t0060": "P2", "t0090": None, "t0095": "P2", "t0098": "P2",
}
#: 真值锚点的**新值**（= 规格意图 + 真库作者：这 9 行的人都是 P7）。
NEW_ANCHOR = {sid: "P7" for sid in VALUE_CHANGED_IDS}
#: 旧 ``addressee``（用于"压扁"这条的可查证据）。
OLD_ADDRESSEE = {
    "t0007": ["P2"], "t0009": ["P2"], "t0011": ["P2"], "t0056": ["P2"],
    "t0057": ["room"], "t0060": ["room"], "t0090": ["P2"], "t0095": ["P2"],
    "t0098": ["P1"],
}
#: 真库作者（``messages.sender_name`` 经 name_map 归一后的代号）。
DB_AUTHOR = {
    "t0007": "P1", "t0008": "P7", "t0009": "P1", "t0011": "P1", "t0056": "P2",
    "t0057": "P7", "t0060": "P7", "t0090": "P1", "t0095": "P7", "t0098": "P7",
}

#: **未改动行**的字段哈希（除新增键之外的原字段；源 = 量具修复前那份评测集）。
#: 它把"我只动了这 10 行"变成可机检的硬事实——改任何别的行这里立刻红。
PRESERVED_CORE_HASHES = {
    't0001': 'e62d8672793463ff', 't0002': '3e3ab4d87547aafc',
    't0003': 'a6ed0aa4c6a02f00', 't0004': 'f7e215dfec1ddb64',
    't0005': '5af0c7552e54e04e', 't0006': '4a3b69db42474824',
    't0010': 'f5373447f192d966', 't0012': 'd7bc15146e63ef27',
    't0013': 'e4c29503a8731869', 't0014': '2009b34dc1d7f7b3',
    't0015': '5e538523a18875bf', 't0016': 'a9ab2932c004f69c',
    't0017': 'd32d9e32cee1db3b', 't0018': '7df172200d51571d',
    't0019': '7c8c54c64e99e0f5', 't0020': '7aa2c91d5056b49b',
    't0021': '693426e594ca8a34', 't0022': '32363f488ac2c65e',
    't0023': '26a8f272f012e517', 't0024': '5648f48b70827d17',
    't0025': 'eb6c3a200de219d1', 't0026': '6b2d6a617fe997db',
    't0027': '4062605d53435f0b', 't0028': '9cdf0504bd2b973f',
    't0029': '740a691090496437', 't0030': '068681b404fda0ed',
    't0031': '89833753d1132a3d', 't0032': '01f5a0a3ab0cccfc',
    't0033': 'dff49caf38e90c02', 't0034': '9948c8f583bd37d3',
    't0035': 'a9ca3f544c23a67d', 't0036': 'ab13a75bcad3d101',
    't0037': '42ca37e5e869d3c3', 't0038': '2b66202fa48bd581',
    't0039': '739b25dbf130a346', 't0040': 'c9582315591001a2',
    't0041': '9e52e3266355614c', 't0042': 'd3535b89f0041cd2',
    't0043': '9be475af22596d36', 't0044': '781300b8d2bd3b44',
    't0045': '38d9c8931afe4f88', 't0046': '1a974fac0ac2b516',
    't0047': 'cf1530899057fdf4', 't0048': 'cd9f8ddc55176afb',
    't0049': 'e2767b6af20908da', 't0050': 'c6485eeb175dcd92',
    't0051': '0dab7fe0cc747adf', 't0052': '94fa912823fb945c',
    't0053': 'c89fe1bf2bd22b89', 't0054': '61e96c8b3ac216e6',
    't0055': '000207028d12d7a7', 't0058': '5e7a8d2af23bca8a',
    't0059': 'f4279802cc30d623', 't0061': '29d892b3c035082f',
    't0062': '41af1157b125120e', 't0063': '8e4e5a167d239f85',
    't0064': '230d0a4fa6f034bf', 't0065': '54b1487b3142c8ad',
    't0066': '5903d602df829908', 't0067': '3c85be11380a1b58',
    't0068': '3747b0366f3dcf92', 't0069': 'c8d8d7415964b1fb',
    't0070': '370d3ad19845dd4f', 't0071': 'ba87fb2bd9f4025d',
    't0072': '5a009e802a47d1fd', 't0073': 'b4f8fd7c3f6d4f7e',
    't0074': '865474ce3cbe403c', 't0075': '8fa3efe3ad561b45',
    't0076': 'e47daa568b6cd23c', 't0077': 'bec81bab28d200c5',
    't0078': '86308bd3694c9af2', 't0079': 'fb552e91db10b595',
    't0080': '77f7530bb8c98756', 't0081': '94b9fbf4a24e97dd',
    't0082': '57529145d2ead496', 't0083': 'f7036cad4753cd4a',
    't0084': '6a685e81523ec291', 't0085': '55164538831fdb2f',
    't0086': 'bec68f4491296b9e', 't0087': '6ca2e8858492f836',
    't0088': 'e9afa71b3908d843', 't0089': 'df897514b9b9d8ac',
    't0091': '9ba6052ae8b830c1', 't0092': 'f52eca3c60a4a920',
    't0093': '65b49f43d392f2e7', 't0094': 'da975d8a48328093',
    't0096': '1c95821b018b2811', 't0097': '1f02d0e1c9a45036',
    't0099': 'b279d7350fd70d0f', 't0100': 'd36fff3696ecd854',
}


#: 真值锚点**不在**机械候选集里的行（**钉死**）：全是"簇继承来的锚点"——真值那个
#: 主角在本条正文里没出现，是顺着簇接下来的。人读全文会说"说得通"，机械候选集
#: （只用本条正文，见 ``candidate_anchors``）给不了。这是**定义**差别不是算错；
#: 钉在这里是为了"候选集被悄悄改窄/改宽"当场变红。
#: 末尾 7 条是 M2-6（2026-10-06）新增样本带来的**同类行**（簇继承 / 引用驱动 /
#: "被拍的是谁"靠上一条推），按同一判据**机器算出后钉进来**，不是手工挑的。
INHERITED_ANCHOR_OUTSIDE_CANDIDATES = [
    't0024', 't0028', 't0029', 't0038', 't0040', 't0051', 't0062', 't0064',
    't0067', 't0069', 't0077', 't0078', 't0079', 't0081', 't0086',
    't0105', 't0106', 't0109', 't0114', 't0115', 't0116', 't0122',
]


# ------------------------------------------------------------------ 夹具
def _rows(path: Path = DATASET):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh.read().splitlines()]


def _by_id(path: Path = DATASET):
    return {row["id"]: row for row in _rows(path)}


def _name_map():
    with open(NAME_MAP, encoding="utf-8") as fh:
        return json.load(fh)


def _code_to_wxid():
    """代号 → wxid（第三方的 wxid 允许为 None）。"""
    data = _name_map()
    out = {}
    for code, info in list(data["codes"].items()) + \
            list(data["third_party_codes"].items()):
        out[code] = info.get("wxid")
    return out


def _name_to_code():
    """真实昵称/别名 → 代号（与 ``_threads_eval_repair`` 同口径：长名优先由调用方排）。"""
    data = _name_map()
    out = {}
    for code, info in list(data["codes"].items()) + \
            list(data["third_party_codes"].items()):
        for name in [info["real_name"]] + list(info.get("aliases") or []):
            out[name] = code
    return out


def _account_rows(msg_ids):
    """只读真库：一次性取回这些 msg_id 的 (sender_name, content, created_at)。"""
    con = sqlite3.connect("file:%s?mode=ro" % ACCOUNT_DB.as_posix(), uri=True)
    try:
        placeholders = ",".join("?" for _ in msg_ids)
        got = {str(mid): (name, content, created)
               for mid, name, content, created in con.execute(
                   "SELECT msg_id, sender_name, content, created_at FROM messages "
                   "WHERE chat_id=? AND msg_id IN (%s)" % placeholders,
                   (CHAT,) + tuple(str(m) for m in msg_ids))}
    finally:
        con.close()
    return got


def _event_ids(msg_ids):
    """只读 meta 库：``external_msg_id → event_id``（``terminal_events``，铁律①的 ref）。"""
    con = sqlite3.connect("file:%s?mode=ro" % META_DB.as_posix(), uri=True)
    try:
        placeholders = ",".join("?" for _ in msg_ids)
        got = {}
        for mid, eid in con.execute(
                "SELECT external_msg_id, event_id FROM terminal_events "
                "WHERE external_msg_id IN (%s)" % placeholders,
                tuple(str(m) for m in msg_ids)):
            got.setdefault(str(mid), str(eid))
        return got
    finally:
        con.close()


def _core_hash(obj):
    """除本量具修复**新增的键**以外的原字段哈希（新键见 REPAIR 的常量）。"""
    clean = {k: v for k, v in obj.items() if k not in ("person_id", "person_ids",
                                                       "text_verified")}
    clean["label"] = {k: v for k, v in clean["label"].items()
                      if k not in ("anchor_person_id", "addressee_person_ids")}
    blob = json.dumps(clean, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _audit_line(row):
    for line in row["notes"].split("\n"):
        if AUDIT_MARK in line:
            return line
    return ""


# ===========================================================================
# ① 规范身份：每行 person_id 非空、可解析、且与代号一一对应（无代号歧义）
# ===========================================================================
def test_every_row_has_resolvable_person_ids() -> None:
    """★① 100/100 行都带 ``person_id``；每个 ``pn:<CODE>`` 都能在 name_map 里落地。

    这是撞车的**根治手段**：标签不再依赖"哪个 P 是谁"这种口头约定，而是显式指到
    本地脱敏映射（``data/eval/threads/name_map.json``，含真实昵称/wxid，不入库）。
    **变异自检①**：把任意一行的 ``person_id`` 置空 → 本用例当场红。
    """
    rows = _rows()
    code_to_wxid = _code_to_wxid()
    assert len(rows) == TOTAL_ROWS, "评测集规模变了（%d 条，见 README §11）" % TOTAL_ROWS

    empty, unresolved, mismatch = [], [], []
    for row in rows:
        pid = row.get("person_id", "")
        if not pid:
            empty.append(row["id"])
        elif row["speaker"] == "unknown":
            # 系统/撤回消息没有归属，但身份位**不许空**：记 `unknown`（可解析的代号）。
            if pid != "unknown":
                mismatch.append((row["id"], pid, "unknown"))
        else:
            if pid != "pn:" + row["speaker"]:
                mismatch.append((row["id"], pid, row["speaker"]))
            elif pid[3:] not in code_to_wxid:
                unresolved.append((row["id"], pid))
        # label 侧的规范身份同样要能落地
        label = row["label"]
        for key in ("anchor_person_id",):
            value = label.get(key, "")
            if value and value[3:] not in code_to_wxid:
                unresolved.append((row["id"], value))
        for value in label.get("addressee_person_ids") or []:
            if value != "room" and value[3:] not in code_to_wxid:
                unresolved.append((row["id"], value))

    assert not empty, "这些行的 person_id 是空的（规范身份缺失）：%s" % empty
    assert not unresolved, "这些 person_id 在 name_map 里查不到：%s" % unresolved
    assert not mismatch, "person_id 与 speaker 不一致：%s" % mismatch


def test_person_ids_cover_every_code_the_row_mentions() -> None:
    """★① 每行 ``person_ids`` 覆盖该行**引用到的所有代号**（speaker/anchor/addressee）。

    覆盖缺一个，就等于那一处又回到"靠代号命名空间"的旧状态。
    """
    missing = []
    for row in _rows():
        mapping = row.get("person_ids") or {}
        label = row["label"]
        referenced = [row["speaker"], label.get("anchor")]
        referenced += [a for a in (label.get("addressee") or []) if a != "room"]
        for code in referenced:
            if not code or code == "unknown":
                continue
            if mapping.get(code) != "pn:" + code:
                missing.append((row["id"], code, mapping.get(code)))
    assert not missing, "person_ids 没覆盖到的代号：%s" % missing


def test_label_person_ids_track_the_codes() -> None:
    """★① ``anchor_person_id`` / ``addressee_person_ids`` 必须与代号**逐位对应**。

    ``addressee`` 允许 0..n（§3.1），所以两个数组必须**等长同序**；长度不等就是
    又压扁了。
    """
    bad = []
    for row in _rows():
        label = row["label"]
        anchor = label.get("anchor")
        want_anchor = ("pn:" + anchor) if anchor else ""
        if label.get("anchor_person_id") != want_anchor:
            bad.append((row["id"], "anchor", label.get("anchor_person_id"), want_anchor))
        want_add = ["room" if a == "room" else "pn:" + a
                    for a in (label.get("addressee") or [])]
        got_add = list(label.get("addressee_person_ids") or [])
        if got_add != want_add:
            bad.append((row["id"], "addressee", got_add, want_add))
    assert not bad, "label 的规范身份与代号视图不一致：%s" % bad


def test_every_referenced_code_resolves_to_one_person() -> None:
    """★① 代号在 name_map 里是**一对多**的（同一 wxid 挂着多个代号）→ 必须报错。

    这条防的正是本次撞车的**根**：只要同一个人的两个代号都能解析，命名空间就还有
    歧义，标签就还可能指错人。
    """
    by_wxid = {}
    for code, wxid in _code_to_wxid().items():
        if not wxid:
            continue
        by_wxid.setdefault(wxid, []).append(code)
    dup = {wxid: codes for wxid, codes in by_wxid.items() if len(codes) > 1}
    assert not dup, "同一个人挂了多个代号（代号命名空间有歧义）：%s" % dup


def test_dataset_has_no_deprecated_code_aliases() -> None:
    """★① ``name_map.json`` 里不许出现**已废弃代号**的注记键。

    有人想用"标一个 deprecated 代号"来记录撞车——那等于把两套编号都留在文件里，
    下一个人照样会挑错。根治办法只有一个：**一个代号 = 一个人**。
    """
    data = _name_map()
    offenders = []
    for section in ("codes", "third_party_codes"):
        for code, info in (data.get(section) or {}).items():
            for key in info:
                if "deprecat" in key.lower() or "alias_of" in key.lower():
                    offenders.append((section, code, key))
    assert not offenders, "name_map 里出现了废弃代号注记键：%s" % offenders


# ===========================================================================
# ② 10 行改动逐行可查；其余 90 行逐字节未动
# ===========================================================================
def test_the_10_fixed_rows_are_documented_line_by_line() -> None:
    """★② 10 行每行都有审计注记：旧值 / 新值 / 真库原文 / 作者 / 依据。

    **逐行可查**是可复核性的底线：光有"改好了"没有"为什么"的文件，下一个人会
    照原样再犯一次。
    """
    rows = _by_id()
    problems = []
    for sid in FIXED_IDS:
        row = rows.get(sid)
        if row is None:
            problems.append((sid, "行不见了"))
            continue
        line = _audit_line(row)
        if not line:
            problems.append((sid, "没有审计注记"))
            continue
        for token in ("旧值", "新值", "依据=", "真库原文", "sha256="):
            if token not in line:
                problems.append((sid, "注记缺 %s" % token))
        if sid in VALUE_CHANGED_IDS:
            if "代号撞车修复" not in line:
                problems.append((sid, "值改了却没写'代号撞车修复'"))
        elif "原样保留" not in line:
            problems.append((sid, "值未改却没写'原样保留'的复核结论"))
    assert not problems, "审计注记不完整：%s" % problems


def test_each_fixed_row_matches_its_evidence_and_the_ground_truth_db() -> None:
    """★② 逐行对账：旧值→新值 与真库作者/正文**对得上**。

    判据（不是"看感觉"）：本行正文里的人（@/引用/@作者）或说话人，在真库作者那一侧
    必须落在同一个规范身份上；``t0095``/``t0098`` 另有逐字比对（下一格）。
    """
    rows = _by_id()
    code_to_wxid = _code_to_wxid()
    truth = _account_rows([rows[sid]["refs"]["message_id"] for sid in FIXED_IDS])
    problems = []
    for sid in FIXED_IDS:
        row = rows[sid]
        row_truth = truth.get(str(row["refs"]["message_id"]))
        if row_truth is None:
            problems.append((sid, "真库找不到这一行"))
            continue
        sender_name, content, _created = row_truth
        author_code = _name_to_code().get(sender_name, "")
        if author_code != DB_AUTHOR[sid]:
            problems.append((sid, "真库作者变了", author_code, DB_AUTHOR[sid]))
        # 新值必须落在"本行可辩护的人"里：正文派生的候选 + 说话人 + 真库作者
        candidates = (set(S.text_candidate_anchors(row)) | {author_code, row["speaker"]})
        if row["label"]["anchor"] not in candidates:
            problems.append((sid, "新 anchor 不在可辩护集合里",
                             row["label"]["anchor"], sorted(candidates)))
        # 注记里必须留下**旧值**（不许悄悄换）；旧值口径 = 修复前那份评测集。
        # t0008 值未动（"原样保留"）→ 它的旧值就是当前值。
        line = _audit_line(row)
        old = OLD_ANCHOR[sid] if sid in VALUE_CHANGED_IDS else row["label"]["anchor"]
        if ("'anchor': %r" % old) not in line:
            problems.append((sid, "注记里没有旧 anchor 值", old))
        if sid in VALUE_CHANGED_IDS:
            if ("'addressee': %r" % OLD_ADDRESSEE[sid]) not in line:
                problems.append((sid, "注记里没有旧 addressee 值", OLD_ADDRESSEE[sid]))
            if row["label"]["anchor"] != NEW_ANCHOR[sid]:
                problems.append((sid, "anchor 不是预期的新值",
                                 row["label"]["anchor"], NEW_ANCHOR[sid]))
        else:
            # t0008 值未动 → 注记里旧值=新值，两处都要能看见。
            if "'addressee': ['P2']" not in line:
                problems.append((sid, "注记里没有 addressee 的值"))
    assert not problems, "逐行对账失败：%s" % problems


def test_untouched_rows_are_byte_identical() -> None:
    """★② 其余 **90 行逐字节未动**（哈希清单；改任何别的行这里立刻红）。

    这条防的是"顺手把别的样本也改了"——那会让基线读数动得**没来由**，
    等于悄悄换分母。

    M2-6（2026-10-06）扩充时**只许追加**：所以这里的判据从"集合相等"升级成
    "**旧集合 ⊆ 新集合** 且 **多出来的恰好是 ``NEW_IDS``**"。旧 100 行（含被修过的
    10 行）一个字节都不许动，新增的 28 行必须逐条在账上——两边漏一个都红。
    """
    rows = _by_id()
    old = set(PRESERVED_CORE_HASHES) | set(FIXED_IDS)
    assert old <= set(rows), "旧样本丢了：%s" % sorted(old - set(rows))
    assert set(rows) - old == set(NEW_IDS), (
        "多出来的行与账上的新增清单对不上：%s" % sorted(set(rows) - old - set(NEW_IDS)))
    drift = []
    for sid, want in PRESERVED_CORE_HASHES.items():
        got = _core_hash(rows[sid])
        if got != want:
            drift.append((sid, want, got))
    assert not drift, "这些**不该动**的行变了：%s" % drift


def test_thread_id_should_switch_and_tags_are_untouched() -> None:
    """★② 本量具修复**不碰**簇编号 / 是否允许换人 / 标签 / label_source。

    改了这三样就是改判定口径，不是修量具——那要另开一份规格变更，不许夹带。
    """
    old = {"t0007": "F1", "t0008": "F1", "t0009": "F1", "t0011": "F1",
           "t0056": "B2", "t0057": "B2", "t0060": "B2", "t0090": "I1",
           "t0095": "K1", "t0098": "K1"}
    old_switch = {"t0007": True, "t0008": True, "t0009": True, "t0011": True,
                  "t0056": False, "t0057": False, "t0060": False, "t0090": True,
                  "t0095": True, "t0098": True}
    rows = _by_id()
    for sid in FIXED_IDS:
        label = rows[sid]["label"]
        assert label["thread_id"] == old[sid], (sid, label["thread_id"])
        assert label["should_switch"] is old_switch[sid], (sid, label["should_switch"])
        assert label["expected_behavior"] in ("继续锚点", "不指派", "允许换人")
    # t0008 值未动：锚点/收件人保持 P2
    assert rows["t0008"]["label"]["anchor"] == "P2"
    assert rows["t0008"]["label"]["addressee"] == ["P2"]


# ===========================================================================
# ③ 正文忠实：t0095 / t0098 逐字等于真库脱敏结果；t0008 结论留档
# ===========================================================================
def test_texts_are_verbatim_from_the_ground_truth_db() -> None:
    """★③ 两条正文与真库**逐字一致**（canonical 脱敏后），且注记里有核验指纹。

    比"相似度 0.526"更硬的判据：相等就是相等，不用阈值。指纹（正文 sha256 前 12 位）
    写进 ``text_verified``，任何人事后都能拿真库重算一遍核对。
    """
    ids = ("t0095", "t0098")
    rows = _by_id()
    truth = _account_rows([rows[sid]["refs"]["message_id"] for sid in ids])
    problems = []
    for sid in ids:
        row = rows[sid]
        sender_name, content, created = truth[str(row["refs"]["message_id"])]
        redacted = REPAIR.desensitize(content, _name_to_code())
        if row["text"] != redacted:
            problems.append((sid, "正文与真库脱敏结果不逐字相等", row["text"], redacted))
        block = row.get("text_verified") or {}
        if block.get("match") is not True:
            problems.append((sid, "text_verified.match 不是 True", block))
        if block.get("redacted_sha256_12") != REPAIR.digest(redacted):
            problems.append((sid, "text_verified 指纹对不上", block))
        if block.get("db_sha256_12") != REPAIR.digest(content):
            problems.append((sid, "text_verified 真库指纹对不上", block))
        if block.get("created_at") != created:
            problems.append((sid, "时间戳对不上", block.get("created_at"), created))
    assert not problems, "正文忠实性失败：%s" % problems


def test_all_fixed_rows_texts_are_faithful_by_construction() -> None:
    """★③ 10 行**全部**逐字等于真库脱敏结果（不只是 t0095/t0098）。

    正文既然逐字对得上，那"正文偏离真库"就不可能是这 10 行读数变化的原因——
    读数变化只能来自**标签**（这正是本次修复要证明的因果）。
    """
    rows = _by_id()
    name_to_code = _name_to_code()
    truth = _account_rows([rows[sid]["refs"]["message_id"] for sid in FIXED_IDS])
    drift = []
    for sid in FIXED_IDS:
        _name, content, _created = truth[str(rows[sid]["refs"]["message_id"])]
        if rows[sid]["text"] != REPAIR.desensitize(content, name_to_code):
            drift.append(sid)
    assert not drift, "这些行的正文与真库脱敏结果不一致：%s" % drift


def test_t0008_verdict_is_recorded() -> None:
    """★③ ``t0008``（拍一拍"作者与被 @ 者同名"）的复核结论必须留档。

    结论：**正文忠实、真值本来就对、原样保留**——
    * 正文 ``"P7" 拍了拍 "P2"`` 经 canonical 脱敏后与真库 ``"P6" 拍了拍 "Elaina"``
      逐字对应；
    * 拍一拍的作者按 canonical ``extract_pat_sender()`` 取"拍了拍"**之前**那个名字
      = P6 = P7，与真库 ``messages.sender_id`` 指向的 P7 一致 → ``speaker=P7`` 不改；
    * 被拍者 = Elaina = P2 → ``anchor=P2`` / ``addressee=["P2"]`` 不改。
    """
    row = _by_id()["t0008"]
    assert row["text"] == '"P7" 拍了拍 "P2"'
    assert extract_pat_sender(row["text"]) == "P7", \
        "canonical 拍一拍作者口径变了：作者应是'拍了拍'之前的那个名字"
    assert row["label"]["anchor"] == "P2"
    assert row["label"]["addressee"] == ["P2"]
    line = _audit_line(row)
    assert "原样保留" in line and "pat_sender" in line, "t0008 的复核结论没留档"


# ===========================================================================
# ④ 打分器：addressee 允许 0..n；空数组 = 合法的 0，且被计数
# ===========================================================================
def _synthetic_row(**over):
    row = {
        "id": "t9001", "at": "2026-09-30 10:00:00", "chat": "1@chatroom",
        "speaker": "P1", "text": "随便说一句", "refs": {"message_id": "1", "event_id": "e"},
        "label": {"thread_id": "X1", "anchor": "P1", "addressee": [],
                  "should_switch": False, "expected_behavior": "继续锚点"},
        "label_source": "human", "notes": "手工夹具：为 addressee 形状给出的样本",
        "__line__": 1,
    }
    row.update(over)
    return row


def test_addressee_may_be_empty_and_is_counted() -> None:
    """★④ ``addressee=[]`` **合法**（= 判不出来），打分器不再报 ERROR。

    旧行为（``score_threads_eval.py`` 校验）把空数组判 ERROR → "判不出来"这个形状
    根本写不进评测集，字段形状被压成零信息。**变异自检②**：把这条 ERROR 加回去 →
    本格红。
    """
    empty = _synthetic_row()
    errors, warnings = S.validate([empty])
    assert not [e for e in errors if "addressee" in e], \
        "空 addressee 不该报错（§3.1 允许 0..n）：%s" % errors

    many = _synthetic_row(id="t9002", label={
        "thread_id": "X1", "anchor": "P1", "addressee": ["P2", "P3", "P4"],
        "should_switch": False, "expected_behavior": "继续锚点"})
    errors_many, _ = S.validate([many])
    assert not [e for e in errors_many if "addressee" in e], errors_many

    # 形状仍要管：字符串 / 非法元素照旧报错（放宽的是"个数"，不是"形状"）
    bad = _synthetic_row(id="t9003", label={
        "thread_id": "X1", "anchor": "P1", "addressee": "P2",
        "should_switch": False, "expected_behavior": "继续锚点"})
    assert [e for e in S.validate([bad])[0] if "addressee" in e], "非数组必须报错"
    bad2 = _synthetic_row(id="t9004", label={
        "thread_id": "X1", "anchor": "P1", "addressee": ["unknown"],
        "should_switch": False, "expected_behavior": "继续锚点"})
    assert [e for e in S.validate([bad2])[0] if "addressee" in e], "非法元素必须报错"

    # 被计数：空数组要真的进统计（不是被静默丢掉）
    samples = [empty, many]
    rows = S.score(samples, lambda sample, hist: {
        "thread_id": "", "anchor": "P1", "addressee": [], "should_switch": False,
        "confidence": "high"})
    assert len(rows) == 2
    assert rows[0]["truth"]["addressee"] == []
    assert rows[1]["truth"]["addressee"] == ["P2", "P3", "P4"]
    stats = S.summarize(rows)
    assert stats["unresolved_n"] == 2, "两条都要进分母（空 addressee 不许被丢）"


def test_validation_reports_zero_addressee_rows_in_the_real_set() -> None:
    """★④ 真实评测集里"0 个 addressee"的行**数是可读的**（不是被 ERROR 顶掉）。

    首版 100/100 都是恰好 1 个元素（形状被压扁）。本格不要求现在就补出空行
    （补不补是标注工作），只要求**写进去不再报错**、且现在统计得出条数。
    """
    rows = _rows()
    sizes = {}
    for row in rows:
        sizes[len(row["label"]["addressee"])] = \
            sizes.get(len(row["label"]["addressee"]), 0) + 1
    assert sizes.get(0, 0) >= 0
    assert sum(sizes.values()) == TOTAL_ROWS
    errors, _ = S.validate(rows)
    assert not [e for e in errors if "addressee" in e], errors


# ===========================================================================
# ⑤ 两套口径并列 + 都确定性
# ===========================================================================
def _run_once():
    samples = S.load_dataset(S.DEFAULT_DATASET)
    T.reset_eval_state()
    rows = S.score(samples, T.predict)
    return rows, S.summarize(rows)


def test_both_anchor_calibers_are_computed_and_printed_side_by_side() -> None:
    """★⑤ 单值口径**必须保留**，候选集口径与其**并列**输出（谁也不许顶替谁）。

    **变异自检③**：把候选集口径退化成单值（= 直接拿 ``anchor_ok`` 当候选集读数）
    → 本格红。
    """
    rows, stats = _run_once()
    # 单值口径（原口径）在、有门槛
    single = dict((n, k) for n, k, *_ in S.THRESHOLDS)
    assert "anchor_accuracy" in single.values() and single["锚点准确率"] == "anchor_accuracy"
    # 候选集口径在、是**并列读数**（不进门槛——门槛口径一动就是改判定标准）
    readout_keys = [item[1] for item in S.READOUTS]
    assert "anchor_accuracy_candidateset" in readout_keys, readout_keys
    assert S.THRESHOLDS[0][1] == "anchor_accuracy", "第一个门槛必须还是单值口径"
    assert stats["anchor_accuracy_candidateset"] is not None
    assert stats["anchor_candidateset_n"] == stats["anchor_n"], \
        "两套口径必须同分母，否则两个数不能并排比"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        S.print_report(stats, rows)
    out = buf.getvalue()
    assert "锚点准确率" in out, "单值口径的读数没打印"
    assert "锚点候选集口径（并列）" in out, "候选集口径没打印"
    # 两者**不同**：候选集口径是真放宽（相等就说明退化了）
    widen = stats["anchor_candidate_widenings"]
    assert widen == (stats["anchor_text_gain"] - stats["anchor_text_loss"]), \
        "净额必须等于（救回 − 弄丢）"
    assert stats["anchor_candidate_widenings"] > 0, \
        "候选集口径一条都没放宽 = 退化成单值口径了"


def test_candidate_caliber_is_a_true_relaxation_of_the_single_value_one() -> None:
    """★⑤ 候选集口径是单值口径的**真放宽**（同分母 + 判对集合是父集）。

    集合构造 = **正文派生 ∪ {真值}**（见 ``S.candidate_anchors``）。真值并进来是
    集合的**定义**（真值是标注者给出的答案，当然算"说得通"），并进来之后
    "单值判对 ⟹ 候选集判对"才成立，两套口径的关系才干净。

    验三条：
    * **同分母**：``anchor_candidateset_n == anchor_n``（两个数才可并排比）；
    * **父集**：``anchor_ok is True`` ⟹ ``anchor_ok_cs is True``；
    * **真值恒在宽集合里**；而**不在正文派生集合里**的只有钉死的 15 行
      （簇继承来的锚点：主角在本条正文里没出现）。那 15 行是"继承来的主角"，
      **不派生出更好的人**，而正文派生集合本身**不要求**包含真值——所以清单钉死，
      集合被悄悄改窄/改宽都当场红。

    红利要拆开看：净额 = ``anchor_candidate_widenings``，分量 =
    ``anchor_text_gain`` / ``anchor_text_loss``（正文派生的救回与弄丢）。
    """
    rows, stats = _run_once()
    assert all(r.get("candidates") for r in rows), "候选集不能为空"
    assert stats["anchor_candidateset_n"] == stats["anchor_n"], "两套口径必须同分母"
    assert stats["anchor_resolved_n"] > 0

    for row in rows:
        if row["truth"]["anchor"] is not None:
            assert row["truth"]["anchor"] in row["candidates"], \
                "(%s) 真值不在宽集合里 = 集合构造坏了" % row["id"]
        if row["anchor_ok"] is True:
            assert row.get("anchor_ok_cs") is True, \
                "(%s) 单值判对、候选集却判错 = 真值没并进集合" % row["id"]

    outside = sorted(r["id"] for r in rows
                     if r["truth"]["anchor"] is not None
                     and r["truth"]["anchor"] not in r["text_candidates"])
    assert outside == INHERITED_ANCHOR_OUTSIDE_CANDIDATES, (
        "真值不在**正文派生**集合里的行变了（候选集定义被改窄/改宽了？）：%s" % outside)

    # 净额 = 救回 - 弄丢，且与统计里的三个数自洽（不许只报净额把丢的藏起来）
    gain = sum(1 for r in rows
               if r["anchor_ok_text"] is True and r["anchor_ok"] is False)
    loss = sum(1 for r in rows
               if r["anchor_ok_text"] is False and r["anchor_ok"] is True)
    assert stats["anchor_text_gain"] == gain and stats["anchor_text_loss"] == loss
    assert stats["anchor_candidate_widenings"] == gain - loss


def test_resolved_subset_prints_both_calibers() -> None:
    """★⑤ "已作答子集"两行必须都在（口径可比的那一对）。

    ``unresolved`` 的行两套口径都记 False；把它们压在同一个分母里会同时拉低两个数，
    于是"锚点判得准不准"被"敢不敢答"污染。所以另给一对**同分母的已作答**读数。
    """
    _rows_, stats = _run_once()
    assert stats["anchor_accuracy_resolved"] is not None
    assert stats["anchor_accuracy_candidateset_resolved"] is not None
    assert (stats["anchor_accuracy_candidateset_resolved"]
            >= stats["anchor_accuracy_resolved"]), "已作答子集上候选集口径不许更严"
    keys = [item[1] for item in S.READOUTS]
    for key in ("anchor_accuracy_resolved", "anchor_accuracy_candidateset_resolved"):
        assert key in keys, "已作答子集读数没并排打印：%s" % keys


def test_both_calibers_are_deterministic_across_two_runs() -> None:
    """★⑤ 同输入两次同数（两套口径都算）。跨轮焦点状态是进程级的，跑前必须重置。"""
    rows_a, stats_a = _run_once()
    rows_b, stats_b = _run_once()
    for key in ("anchor_accuracy", "anchor_accuracy_candidateset",
                "anchor_candidate_widenings", "anchor_n", "anchor_candidateset_n",
                "link_precision", "link_recall", "switch_error_rate",
                "unresolved_rate"):
        assert stats_a[key] == stats_b[key], (key, stats_a[key], stats_b[key])
    assert [r["anchor_ok_cs"] for r in rows_a] == [r["anchor_ok_cs"] for r in rows_b]
    assert [r["candidates"] for r in rows_a] == [r["candidates"] for r in rows_b]


def test_candidate_set_definition_is_the_documented_one() -> None:
    """★⑤ 正文派生候选集 = 文件头写死的那三条（机械、可复核），不许是别的什么。"""
    sample = {
        "speaker": "P1", "text": "[引用 P4: 旧话]\n@P2 你看 P5 那事",
    }
    got = S.text_candidate_anchors(sample)
    assert got == ["room", "P2", "P4", "P1"], got
    # 引用块里的 @ 不算（那是上下文，不是本轮目标）——canonical strip_quote_block
    nested = S.text_candidate_anchors({
        "speaker": "P3", "text": "[引用 P2: @P6 你说的]\n这个是对的"})
    assert "P6" not in nested, nested
    # 拍一拍目标算（canonical pat_targets）
    pat = S.text_candidate_anchors({"speaker": "P7", "text": '"P7" 拍了拍 "P2"'})
    assert pat == ["room", "P2", "P7"], pat
    # unknown 说话人不是人，不进候选
    assert S.text_candidate_anchors({"speaker": "unknown", "text": "x"}) == ["room"]
    # 宽集合 = 正文派生 ∪ {真值}；真值已在里面时**不重复**
    assert S.candidate_anchors(sample, "P4") == ["room", "P2", "P4", "P1"]
    assert S.candidate_anchors(sample, "P6")[-1] == "P6"
    assert S.candidate_anchors({"speaker": "P5", "text": "嗯"}, None) == ["room", "P5"]


def test_baseline_predictor_keeps_both_calibers_readable() -> None:
    """★⑤ 基线预测器（最近说话人）上两套口径都要有读数（n/a 也要读得出）。"""
    from scripts.baseline_recent_speaker import predict as baseline_predict

    samples = S.load_dataset(S.DEFAULT_DATASET)
    stats = S.summarize(S.score(samples, baseline_predict))
    assert stats["anchor_accuracy"] is not None
    assert stats["anchor_accuracy_candidateset"] is not None
    assert (stats["anchor_accuracy_candidateset"] >= stats["anchor_accuracy"]), \
        "候选集口径不许比单值口径更严"


def test_scorer_never_errors_reading_the_repaired_dataset() -> None:
    """⑤ 修完的数据集必须**零校验错误**（否则打分器会把整轮跑挂成退出码 2）。"""
    errors, _warnings = S.validate(S.load_dataset(S.DEFAULT_DATASET))
    assert not errors, errors


# ===========================================================================
# ⑥ 七项指标：既有五项门槛一字未改 + 新增两项落 N/A（可读、不刷红绿）
# ===========================================================================
#: 既有五项门槛的**逐字快照**（改任何一格都红——门槛表是最不该悄悄动的东西）。
FROZEN_THRESHOLDS = (
    ('锚点准确率', 'anchor_accuracy', 'anchor_n', '>=', 0.85),
    ('错合率(link 口径)', 'wrong_link_rate', 'link_precision_n', '<=', 0.10),
    ('换人误判率', 'switch_error_rate', 'switch_n', '<=', 0.05),
    ('不确定率', 'unresolved_rate', 'unresolved_n', '<=', 0.35),
    ('假承诺率', 'false_promise_rate', 'promise_n', '==', 0.0),
)
#: 新增两项（M2-6）：门槛必须还是 ``None``（未定）。**变异自检④**：给它们编一个
#: 门槛 → 本格红（"先落 N/A、再定门槛"是任务条款，不许自己拍）。
NEW_THRESHOLD_KEYS = ('fabrication_rate', 'lateness_rate')


def test_seven_metrics_all_present_with_frozen_old_thresholds() -> None:
    """★⑥ 门槛表 = **既有五项（逐字未改）** + **新增两项（``op``/``thr`` 皆为 None）**。

    打印里七项必须**都出现**（新增两项可以是 ``n/a``，但不能不打印——"可读"是硬要求）。
    """
    assert len(S.THRESHOLDS) == 7, S.THRESHOLDS
    assert tuple(S.THRESHOLDS[:5]) == FROZEN_THRESHOLDS, "既有五项门槛被改了"
    for row in S.THRESHOLDS[5:]:
        assert row[1] in NEW_THRESHOLD_KEYS, row
        assert row[3] is None and row[4] is None, \
            "新增两项的门槛必须先落 N/A（未定），不许自己拍：%s" % (row,)
    assert {row[1] for row in S.THRESHOLDS[5:]} == set(NEW_THRESHOLD_KEYS)

    _rows_, stats = _run_once()
    verdicts = S.threshold_verdicts(stats)
    assert len(verdicts) == 7
    for name, _val, _n, _op, thr, verdict in verdicts:
        if thr is None:
            assert verdict == 'N/A', (name, verdict)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        S.print_report(stats, rows=_rows_)
    out = buf.getvalue()
    for name in ('锚点准确率', '错合率(link 口径)', '换人误判率', '不确定率', '假承诺率',
                 '胡编率', '迟到率'):
        assert name in out, "七项指标里有没打印的：%s" % name


def test_new_metrics_are_never_fail_capped() -> None:
    """★⑥ 门槛未定的两项**永不 FAIL**（不许拿未定的门槛把读数刷红）。

    ``threshold_verdicts`` 对 ``thr is None`` 恒返回 ``N/A``——把这条钉住，
    免得有人"顺手"给新两项定一个门槛让报表好看/难看。
    """
    _rows_, stats = _run_once()
    fake = dict(stats)
    fake['fabrication_rate'] = 0.99
    fake['lateness_rate'] = 0.99
    for name, _val, _n, _op, thr, verdict in S.threshold_verdicts(fake):
        if thr is None:
            assert verdict == 'N/A', (name, verdict)


# ===========================================================================
# ⑦ 两项新指标的近例（M2-6 ④）：真实事故行 + 读数
#
# 出戏率（``internal_lexicon``）与复述率（``probe_self_repetition`` 的相似度读数）
# 两条判据已在 2026-10-24 随 A6 退役（主人裁定：出站质检只留机械项），对应的两条
# 近例用例（t0124 / t0104）随之退役——指标没有分母，也就没有可钉的读数。# ===========================================================================


def _run_outbound(**kwargs):
    """跑一遍全量（带上出站轴两项新指标的参数），返回 (rows, stats)。"""
    samples = S.load_dataset(S.DEFAULT_DATASET)
    T.reset_eval_state()
    rows = S.score(samples, T.predict, **kwargs)
    return rows, S.summarize(rows)


def _by_id_rows(rows):
    return {row['id']: row for row in rows}

# ===========================================================================
def test_near_example_lateness_is_a_real_62_second_gap() -> None:
    """★⑦ **迟到率近例 = t0096**（21:46:01 才补上 21:44:59 的自报回执 = **62 s**）。

    时间戳对齐口径（判据来源 = 真实时间戳，不是估计）：``sent_at`` = 样本自己的 ``at``
    （出站回放；线上同 ``send_log.sent_at`` / ``messages.created_at``），
    ``trigger_at`` = 收件人 ``P6`` 在窗口里最近一条发言（21:44:59）。
    **变异自检⑥**：把预算调到 100 s → 这条不再算迟到（读数动了），说明分母/分子真是
    被预算和时间戳驱动的。
    """
    rows, stats = _run_outbound()
    by_id = _by_id_rows(rows)
    late = by_id['t0096'].get('late') or {}
    assert late.get('trigger_at') == '2026-09-30 21:44:59', late
    assert late.get('sent_at') == '2026-09-30 21:46:01', late
    assert abs(float(late.get('delay')) - 62.0) < 1e-9, late
    assert late.get('late') is True, late
    assert any(item.startswith('t0096(') for item in stats['lateness_hit_ids'])

    _r2, stats2 = _run_outbound(reply_budget_seconds=100.0)
    by_id2 = _by_id_rows(_r2)
    assert (by_id2['t0096'].get('late') or {}).get('late') is False, "预算没起作用"
    assert stats2['budget_seconds'] == 100.0


def test_near_example_fabrication_is_invisible_to_the_ledger_judge() -> None:
    """★⑦ **胡编率近例 = t0093**（21:45:20「…P6是广东的…」）vs t0103（21:44:59 自报湖南长沙）。

    诚实结论（本轮实测）：**判据够不着**。台账读侧（``evaluate_claims``）对 t0093 的
    正文**一条断言都判不出来**（字段词表的四型是"籍贯是X／X人／我是X／你是X人"，
    而事故句是 **第三人称 + 是X的**："P6是广东的"）→ 断言覆盖率 0 → 胡编率 = **n/a**
    （不是 0，也不是"没事发生"）。

    这一格把缺口钉死，并证明**判据本身是通的**：同一句话换成被覆盖的形态
    （"你是广东人吧"——闸门自己的静态形态表 ''身份断言'' 写的就是这个形状），
    在**同一条真实台账**（t0103 的自报，经 canonical 写侧登记）下立刻判出
    ``unsupported=1``。所以"指标会动"是可验证的，缺的是**形态覆盖**。

    **变异自检⑦**：让 t0093 落进字段词表（或把台账写侧断掉）→ 本格红。
    """
    samples = {row['id']: row for row in S.load_dataset(S.DEFAULT_DATASET)}
    batch = [samples['t0103'], samples['t0093']]
    prev = os.environ.get('YINYUE_FEATURE_CLAIM_LEDGER')
    os.environ['YINYUE_FEATURE_CLAIM_LEDGER'] = 'shadow'   # 只为这一格显式开启
    try:
        # ``ledger_replay=True`` = 台账走**内存**（写侧喂本批语料、读侧判她的回复），
        # 这条路上一个生产 IO 都没有。
        rows = S.score(batch, lambda sample, hist: {}, ledger_replay=True)
        # 判据本身是通的：同一条真实台账 + 被词表覆盖的形态 → 分子立刻动
        facts = S._facts()
        book = facts.ClaimLedger(records=[])
        facts.record_message_claims(
            facts.ClaimMessage(text=samples['t0103']['text'], chat_id=CHAT,
                               speaker_key='pn:P6',
                               message_id=samples['t0103']['refs']['message_id'],
                               observed_at=samples['t0103']['at'], kind='text'),
            ledger=book)
        probe = facts.evaluate_claims('你是广东人吧', chat_id=CHAT, person_keys=['pn:P6'],
                                      ledger=book)
    finally:
        if prev is None:
            os.environ.pop('YINYUE_FEATURE_CLAIM_LEDGER', None)
        else:
            os.environ['YINYUE_FEATURE_CLAIM_LEDGER'] = prev
    stats = S.summarize(rows)
    assert stats['fabrication_n'] == 0, "判据不该判出断言（覆盖缺口）：%s" % stats['fabrication_n']
    assert stats['fabrication_rate'] is None, "分母 0 必须读 n/a，不许编数"
    rows_by = {row['id']: row for row in rows}
    fab = rows_by['t0093'].get('fabrication') or {}
    # 关键区别："判据跑了但一条都判不出" ≠ "判据没开"——前者才是覆盖缺口。
    assert fab.get('enabled') is True and fab.get('source') == 'ledger-replay', fab
    assert fab.get('judged') == 0, fab

    assert probe.enabled is True
    assert len(probe.verdicts) == 1 and len(probe.unsupported) == 1, probe.verdicts
    assert '湖南长沙' in probe.unsupported[0].reason


# ===========================================================================
# ⑧ 新增 28 条：逐条可查（真库原文 + 指纹 + 事件 id + 标签形状）
# ===========================================================================
def test_new_rows_are_documented_line_by_line_and_match_the_db() -> None:
    """★⑧ 新增 28 行：每条都能在真库里对上（正文逐字 = 脱敏后真库原文 + sha256）。

    判据（不是"看感觉"）：``text_verified`` 里的三枚指纹必须与**此刻**的真库重算一致，
    ``refs.event_id`` 必须真是 ``terminal_events`` 里那条消息的事件 id。
    """
    rows = _by_id()
    ids = [sid for sid in NEW_IDS]
    account = _account_rows([rows[sid]['refs']['message_id'] for sid in ids])
    events = _event_ids([rows[sid]['refs']['message_id'] for sid in ids])
    name_to_code = _name_to_code()
    problems = []
    for sid in ids:
        row = rows[sid]
        key = str(row['refs']['message_id'])
        if key not in account:
            problems.append((sid, '真库找不到'))
            continue
        _name, content, created = account[key]
        redacted = REPAIR.desensitize(content, name_to_code)
        if row['text'] != redacted:
            problems.append((sid, '正文与真库脱敏结果不逐字相等', row['text'], redacted))
        block = row.get('text_verified') or {}
        if block.get('match') is not True:
            problems.append((sid, 'text_verified.match 不是 True'))
        if block.get('db_sha256_12') != REPAIR.digest(content):
            problems.append((sid, '真库指纹对不上'))
        if block.get('redacted_sha256_12') != REPAIR.digest(redacted):
            problems.append((sid, '脱敏指纹对不上'))
        if block.get('created_at') != created:
            problems.append((sid, '时间戳对不上', block.get('created_at'), created))
        if events.get(key) != row['refs']['event_id']:
            problems.append((sid, 'event_id 与 terminal_events 对不上',
                             row['refs']['event_id'], events.get(key)))
        if len(str(row.get('notes') or '')) < 40:
            problems.append((sid, 'notes 太短，无法复核'))
        for token in ('真库原文', 'sha256='):
            if token not in str(row.get('notes') or ''):
                problems.append((sid, 'notes 缺 %s' % token))
    assert not problems, '新增行对账失败：%s' % problems


def test_new_rows_keep_the_documented_tag_shapes() -> None:
    """★⑧ 新增行的标签形状：四类近例标签各有主，``negative`` ⟺ ``should_switch=false``。

    `incident`（首版 7 条事故窗）**一条都不许加**——§十四.2 的 7/7 门槛依赖它；
    新事故用 ``incident:repeat / :ooc / :facts / :late`` 四个**新命名空间**记账。
    """
    rows = _by_id()
    incident = [sid for sid in rows if 'incident' in (rows[sid].get('tags') or [])]
    assert len(incident) == 7 and not [sid for sid in incident if sid in NEW_IDS], \
        '首版 incident 子集不许被新样本污染：%s' % incident
    want = {'incident:repeat': ['t0101', 't0104'],
            'incident:ooc': ['t0107', 't0122', 't0124'],
            'incident:facts': ['t0102', 't0103'],
            'incident:late': ['t0103']}
    for tag, ids in want.items():
        got = sorted(sid for sid in NEW_IDS if tag in (rows[sid].get('tags') or []))
        assert got == ids, (tag, got, ids)
    bad = [sid for sid in NEW_IDS
           if ('negative' in (rows[sid].get('tags') or []))
           != (rows[sid]['label']['should_switch'] is False)]
    assert not bad, '新增行里 negative 与 should_switch 不同步：%s' % bad
    assert sum(1 for sid in NEW_IDS
               if 'negative' in (rows[sid].get('tags') or [])) == 24
    assert [rows[sid]['at'] for sid in NEW_IDS] == sorted(rows[sid]['at'] for sid in NEW_IDS)
    assert min(rows[sid]['at'] for sid in NEW_IDS) == '2026-09-30 21:44:20'
    assert max(rows[sid]['at'] for sid in NEW_IDS) == '2026-09-30 21:56:11'


def test_readme_and_dataset_agree_on_totals() -> None:
    """★⑧ README §11 的账（总量 / 新增条数 / 分布）必须与文件实测一致。

    记账对不上就等于"悄悄换分母"——这条把 README 变成可机检的账本。
    """
    readme = (ROOT / 'tests' / 'golden' / 'threads' / 'README.md').read_text(encoding='utf-8')
    rows = _rows()
    neg = sum(1 for row in rows if 'negative' in (row.get('tags') or []))
    assert '**%d 条**' % TOTAL_ROWS in readme, 'README 没写新总量'
    assert '新增 **28** 条' in readme, 'README 没写新增条数'
    assert '负样本（`tags` 含 `negative`） | **%d**' % neg in readme, \
        'README 的负样本数与文件不一致（实测 %d）' % neg
    for sid in NEW_IDS:
        assert sid in readme, 'README 没逐条记账：%s' % sid


# ===========================================================================
# ⑨ 两项新指标：分母 0 → n/a、两次跑同数
# ===========================================================================
def test_new_metrics_report_na_when_denominator_is_zero() -> None:
    """★⑨ **分母 0 → ``n/a``**（不许编数）：两个新指标各造一个"没得测"的批次。

    * 迟到率：有出站但窗口里没有触发点（历史为空）；
    * 胡编率：台账读侧关着（``_OFF`` 里钉死）→ 一行都不走 → 0 条断言。

    **变异自检①**：让任何一个"分母 0"的分支返回数字（例如把 ``ratio`` 的 0 分母
    改回 0.0）→ 本格红。
    """
    sample = _synthetic_row(id='t9101', speaker='P2', at='2026-09-30 22:00:00',
                            label={'thread_id': 'X1', 'anchor': 'P2', 'addressee': ['room'],
                                   'should_switch': False, 'expected_behavior': '继续锚点'})
    rows = S.score([sample], lambda s, h: {})
    stats = S.summarize(rows)
    for key in ('lateness_rate', 'fabrication_rate'):
        assert stats[key] is None, (key, stats[key])
    for key in ('lateness_n', 'fabrication_n'):
        assert stats[key] == 0, (key, stats[key])
    verdicts = {name: verdict for name, _v, _n, _o, _t, verdict
                in S.threshold_verdicts(stats)}
    for name in ('胡编率', '迟到率'):
        assert verdicts[name] == 'N/A'
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        S.print_report(stats, rows)
    assert 'n/a' in buf.getvalue()


def test_new_metrics_are_deterministic_across_two_runs() -> None:
    """★⑨ 同一批输入两次跑 → 两项新指标（含每条的判据明细）**逐位相同**。

    跨轮焦点状态是进程级的，跑前必须 ``reset_eval_state()``；台账这一格在默认
    （flag off）下**不读库**，所以确定性不依赖任何外部状态。
    **变异自检②**：让任一判据带上非确定的东西（例如窗口用集合遍历顺序）→ 本格红。
    """
    rows_a, stats_a = _run_outbound()
    rows_b, stats_b = _run_outbound()
    keys = ('lateness_rate', 'lateness_n', 'fabrication_rate', 'fabrication_n',
            'outbound_replies', 'assertion_coverage', 'lateness_hit_ids')
    for key in keys:
        assert stats_a[key] == stats_b[key], (key, stats_a[key], stats_b[key])
    for key in ('late', 'fabrication', 'reply'):
        assert [r.get(key) for r in rows_a] == [r.get(key) for r in rows_b], key


def main() -> bool:
    tests = [fn for name, fn in globals().items() if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
