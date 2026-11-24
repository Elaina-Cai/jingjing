#!/usr/bin/env python
"""话题簇 / 锚点判定 评测集打分器（§九 七项指标：既有五项 + 新增两项）。

归属口径**不在这里**：`@` / `[引用 …]` / 拍一拍 的解析一律 import canonical
（``core.conversation.addressing``：``quote_of`` / ``mentioned_names`` /
``pat_sender`` / ``pat_targets`` / ``strip_quote_block``）。本脚本以前自带一份
正则复制件，于是「explicit 子集通过率」是拿打分器自己的口径去核对打分器自己的
判定（循环验证）——现在它量化的是**线上那一套解析**能不能把显式标记认出来。

用法
----
1) 只校验数据集（不依赖任何未实现的模块）::

       python scripts/score_threads_eval.py --dry-run

   退出码 0 = 数据集结构合法；非 0 = 数据集本身有问题（缺字段 / 重复 id /
   thread_id 复用 / 与显式标记冲突 等）。

2) 用"预测器"跑分::

       python scripts/score_threads_eval.py --predictor core.context.threads:predict

   约定 ``--predictor module:function``：**模块导入延迟到参数解析之后**（见
   ``_load_predictor``），所以现在（``core/context/threads.py`` 还不存在）也能
   跑 ``--dry-run`` 而不报 ImportError。

预测器契约
----------
``predict(sample, history) -> dict``

* ``sample``：JSONL 里的一条样本（``label`` 字段**不要**读，那是真值）；
* ``history``：该样本之前 ``--history-window`` 秒内、同 chat 的历史消息，
  每条形如 ``{"at","speaker","text","refs"}``（与样本同构，且已脱敏）；
* 返回：``{"thread_id": str, "anchor": str|None, "addressee": [str],
  "should_switch": bool, "confidence": str}``，外加可选的
  ``{"reply_text": str, "asserted_history": [[speaker, addressee], ...]}``。
  ``confidence == "unresolved"`` 时按"诚实失败"计（只进不确定率，不进锚点准确率分母）。

五项指标（定义严格照 §九）
--------------------------
====================  ==================================================
锚点准确率            anchor 判对的比例（分母 = 真值 anchor 非 null 且预测非 unresolved）
换人误判率            真值 should_switch=false 却换了主角/收件人的比例
错线率                被归进错簇的比例（**link 口径**，见下）
不确定率              预测 unresolved 的占比
假承诺率              "你刚说过 X"类断言失实的比例（需 reply_text）
====================  ==================================================

错线率的口径（2026-10-06 第二期收口，**旧口径已作废**）
------------------------------------------------------
旧口径是**两个命名空间相比**：真值 ``thread_id`` 是**人工窗编号**（``A8`` / ``B6`` /
``D2``……），预测是**簇首条的 message_ref**（``86c51eeb…``）。两者**永不相等**，
于是读数恒为 ``1.000``——它不度量任何东西（零信息量），只会把"未达标"刷成一句
没有信息量的红字。旧读数保留为 :data:`DEPRECATED_METRICS`（打印时明写
``deprecated``、**不设门槛**），只为历史对账。

新口径 = **link-level**（把两套命名空间都换算成"这两条消息是不是同一簇"的边）：

* 配对 = 每一条样本 ``s`` 与它**这一轮真正看得见**的历史 ``h``（与喂给预测器的
  同一个 ``--history-window`` 窗口，故每对只数一次：从更晚那条的视角）；
* 真值边 ``truth_link(s,h)`` = 两边真值 ``thread_id`` 都非空且相等；
* 预测边 ``pred_link(s,h)`` = 两边预测 ``thread_id`` 都非空且相等
  （预测的簇 id = 簇首条 ref，同一批样本同一次跑分内可比）；
* ``link_precision`` = TP/(TP+FP)（**误归簇**：把不同簇的连在一起）；
  ``link_recall`` = TP/(TP+FN)（**漏归簇**：同一簇没连上）；
  ``错合率 = 1 - link_precision`` 就是 §九【已定 13】那条 ≤0.10 的**可测替身**。
* **已知口径代价**（写在这里免得被当成漏算）：簇 id = 簇首条 ref，跨窗口边界时
  同一条真簇在两次调用里可能起于不同的首条 → 会多算一点漏合。这个偏差由诊断脚本
  （``data/_threads_diag``）用"同一调用内的簇成员重放"量化过：真同簇对里
  只有极少数属于这种漂移，读数结论不因此改变。

**分母 0 一律 ``n/a``**（没有边可比就**不编数**）：precision 的分母 = **预测出来的
边数**（预测器一条边都不给 → ``n/a``），recall 的分母 = **真值边数**（样本彼此都不
同簇 / 只有一条 → ``n/a``）。分母不为 0 时读到的 ``0.000`` 是**测出来的 0**，
与"没得测"是两件事，打印里分开。

两套口径**并列**（2026-10-06 量具修复新增候选集口径，2026-10-06 前只有单值口径）
------------------------------------------------------------------------------
锚点这件事有两套都成立的读法，本脚本**两套都算、都打印**，谁都不许顶替谁：

* **单值口径**（``anchor_accuracy``，**原口径，保留不删**）：预测 == 真值那**一个**
  代号。它的毛病是**把"另一个可辩护的读法"一律记成错**——两人独立盲标实测
  0.71；真值只要在**一个**人身上偏了，读数就跟着塌，且塌掉的量跟"系统能力"无关。
* **选区集口径**（``anchor_accuracy_candidateset``，**并列读数**）：真值锚点落在
  **本样本自己派生出来的候选集**里就算对。

**两个数字的来源必须分清，别混成一个**（审的人容易在这里看错）：

* 本脚本**自己算出来**的那一列（``anchor_accuracy_candidateset``）走的是下文
  写死的机械候选集，是**可复现的读数**（同一输入两次同数）。
* "候选集口径 0.869 vs 单值口径 0.71"是**两轮独立盲标实验**的实测值（标注者
  各自判"系统给的那个锚点是否为一个说得通的答案"），**不是**本脚本的输出——
  脚本里没有任何模型。两者**不能拿来逐个对**：盲标那对数是**人**在判，本脚本那对
  数是**机械集合**在判。可比的只有口径与分母：把分母限制到**已作答**（预测非
  ``unresolved``）那个子集，本脚本实测 **0.662（单值）vs 0.714（候选集）**，
  与盲标同一读法、同一量级（盲标更高是因为人比机械集合宽，且盲标者不看同一个
  预测器）。所以本脚本**两套口径各出两行**：全体分母（更严）与已作答子集
  （口径可比），四个数都打印，谁也不许顶替谁。
* 机械候选集**比人严**的地方是**簇继承来的锚点**（真值那个主角在正文里没出现，
  是顺着簇接下来的）：本集 15 行（``t0024/28/29/38/40/51/62/64/67/69/77/78/79/81/86``）。
  人读全文会给它算"说得通"，机械集合给不了——这是**定义**差别，不是算错；
  这 15 行不放进候选集，因为候选集必须"预测时就能算出来"。

候选集（**纯机械、确定性**；构造与"为什么真值必须并进来"见 ``candidate_anchors``）：
**正文派生集合 ∪ {真值锚点}**。正文派生那一半只用样本自己的 ``speaker`` / ``text``：

1. ``room`` —— 全群广播（``addressee`` 里的 ``["room"]`` 是显式的"说给全群"）；
2. 正文里的显式归属：``@`` 点名（只看引用块**之后**新写的那段）+ 可解析的引用作者
   + 拍一拍目标；
3. ``speaker`` 本人 —— 第一人称自述/自指时"主角 = 说话人"（README §7 t0072 的口径）。

**候选集口径的分母与单值口径相同**（真值 anchor 非 null，``unresolved`` 两套都记 False），
所以两个数**可以并排比**，且**宽集合判对 ⊇ 单值判对**（数学上成立：真值是集合的一个
元素，而"等于真值"蕴含"落在集合里"）。真值 anchor 为 ``null`` 的行两套都不进分母
（那是 §九「不指派」那一格，由换人误判率/不确定率读）。

**红利要拆开看，别只看净额**：``anchor_candidate_widenings`` = 宽集合得分 − 单值得分
（净额）；它的两个分量是 ``anchor_text_gain``（**正文派生**那一半救回的）与
``anchor_text_loss``（**正文派生**那一半弄丢的——真值那个继承来的主角在正文里没出现，
本集 15 行）。净额是 0 也可能是"救回 10、弄丢 10"，所以两个分量都打印。

**另给一对"已作答子集"读数**（``anchor_accuracy_resolved`` /
``anchor_accuracy_candidateset_resolved``，分母 = 预测非 ``unresolved``）：``unresolved``
的行两套口径都记 False，压在同一个分母里会同时拉低两个数，把"判得准不准"和
"敢不敢答"混在一起。想看纯准确率就读这一对。

七项指标 = 既有五项 + **新增两项**（M2-6；判据全部来自既有 canonical 实现，
本脚本**不自带第二套**）
--------------------------------------------------------------------------
新增两项都长在**出站轴**上（既有五项长在归属轴上），所以它们的分母不是"样本数"，
而是"**这一批里她真的发出去的回复**"：``reply_text`` = 预测器回传的回复
（``pred["reply_text"]``）；没回传时退到 ``speaker == "P1"`` 的样本正文——**那些正文
就是她当时真发出去的那句话**（与 ``send_log.content`` /
``outbound_audit.content_preview`` 同源），所以这一档叫**出站回放**，不是编出来的输入。
预测器没回传、样本也不是她的 → 那条**不进任何新指标的分母**（宁缺不编）。

====================  ==================================================================
指标                  分子 / 分母（**分母 0 → ``n/a``**）
迟到率                分子 = 出站**实际时刻**晚于**本轮应答截止**的回复条数；
``lateness_rate``     分母 = 同时拿到 ``sent_at`` 与 ``trigger_at`` 的回复条数。
                      **时间戳对齐口径**（写清楚，免得被当成随手取的）：
                      * ``sent_at`` = ``pred["reply_at"]`` → ``pred["sent_at"]`` →
                        样本自己的 ``at``（出站回放时它就是真实落地时刻；线上对应
                        ``send_log.sent_at`` / ``send_log.confirmed_at`` /
                        ``messages.created_at`` / ``outbound_audit.created_at``）；
                      * ``trigger_at`` = 该回复**收件人**在窗口内最近一条发言的时刻
                        （收件人取预测 ``addressee``，退回真值 ``label.addressee`` 的第一个
                        ``Pn``；``room``/空 → 退回"窗口内最近一条非她发言"）——与
                        ``send_log.trigger_msg_id`` 是同一件事；
                      * 预算 = ``--reply-budget-seconds``（默认 60 s，**每次跑都打印**）。
                        它**不是门槛**：门槛表里这一格是 ``N/A``。
                      分母 0（两个时间戳凑不齐）→ ``n/a``。
胡编率                分子 = 台账判为**无支撑**（``verdict.supported is False``）的断言条数；
``fabrication_rate``  分母 = 台账**判定过**的断言条数（``verdicts``）。
                      判据来源 = ``core.context.message_facts.evaluate_claims``（claim 台账
                      **读侧**，唯一 IO 点）——分子只认台账/闸门留痕，**不认"像不像编的"**。
                      ``YINYUE_FEATURE_CLAIM_LEDGER`` 为 ``off``（默认）时
                      ``evaluate_claims`` 一行都不走 → 0/0 → ``n/a``（**不编数**）；
                      ``--ledger-replay`` 用**本批语料**在内存里按 canonical 写侧
                      （``record_message_claims``）自建台账，打印里明写
                      "影子回放、不是生产行"。台账判不出任何断言时（覆盖率 0）同样是
                      ``n/a``，并单独打印 ``assertion_coverage``（**读数**，不是指标）。
====================  ==================================================================

**新增两项的口径纪律**：① 分子分母只来自上面点名的判据源；② 分母 0 一律 ``n/a``；
③ 两项在 :data:`THRESHOLDS` 里先落 **``N/A``（门槛未定）**——值照常可读，
**不许**拿未定的门槛刷红或刷绿（``threshold_verdicts`` 对 ``thr is None`` 恒返回
``N/A``）；④ 既有五项与候选集口径**一个都没改**（连打印顺序都没动）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# canonical 归属（唯一 owner）：本脚本的 explicit 判定与线上同源，见文件头 docstring。
from core.conversation.addressing import (  # noqa: E402
    mentioned_names,
    pat_sender,
    pat_targets,
    quote_of,
    strip_quote_block,
)

DEFAULT_DATASET = os.path.join('tests', 'golden', 'threads', 'eval_set.jsonl')

CHAT_RE = re.compile(r'^\d+@chatroom$')
ID_RE = re.compile(r'^t\d{4}$')
VALID_SOURCES = ('human', 'explicit')
VALID_BEHAVIORS = ('继续锚点', '不指派', '允许换人')
LABEL_KEYS = ('thread_id', 'anchor', 'addressee', 'should_switch', 'expected_behavior')
SAMPLE_KEYS = ('id', 'at', 'chat', 'speaker', 'text', 'refs', 'label', 'label_source', 'notes')
PERSON_RE = re.compile(r'^P\d+$')
AT_RE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
# 历史断言的句式（§4.3.5 / §六「不做历史断言」）
ASSERT_RE = re.compile(r'(你(刚|刚才|刚刚)?(对我)?说过|你(刚|刚才|刚刚)?对.{1,12}说过|'
                       r'这句你(刚|刚才|刚刚)?(对.{1,12})?说过)')


# ----------------------------------------------------------------------
# 数据集加载 + 校验
# ----------------------------------------------------------------------
class DatasetError(Exception):
    pass


def load_dataset(path):
    if not os.path.exists(path):
        raise DatasetError('数据集不存在: %s' % path)
    with open(path, 'rb') as fh:
        raw = fh.read()
    if b'\r\n' in raw:
        raise DatasetError('%s 含 CRLF，要求 LF' % path)
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise DatasetError('不是合法 UTF-8: %s' % exc)
    if not text.endswith('\n'):
        raise DatasetError('文件应以换行结尾（一行一个 JSON 对象）')

    samples = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            raise DatasetError('第 %d 行是空行（JSONL 不允许）' % lineno)
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError('第 %d 行 json.loads 失败: %s' % (lineno, exc))
        if not isinstance(obj, dict):
            raise DatasetError('第 %d 行不是 JSON 对象' % lineno)
        obj['__line__'] = lineno
        samples.append(obj)
    return samples


def validate(samples, verbose=False):
    """结构性校验；返回 (errors, warnings)。"""
    errors, warnings = [], []
    ids, _thread_anchor, thread_seen = Counter(), {}, OrderedDict()
    missing_at = []

    for s in samples:
        where = '第 %s 行/%s' % (s.get('__line__'), s.get('id', '?'))
        for key in SAMPLE_KEYS:
            if key not in s:
                errors.append('%s: 缺字段 %s' % (where, key))
        if errors and 'label' not in s:
            continue
        sid = s.get('id', '')
        ids[sid] += 1
        if not ID_RE.match(str(sid)):
            errors.append('%s: id 格式应为 tNNNN' % where)
        if not AT_RE.match(str(s.get('at', ''))):
            errors.append('%s: at 格式应为 YYYY-MM-DD HH:MM:SS' % where)
        if not CHAT_RE.match(str(s.get('chat', ''))):
            errors.append('%s: chat 应为 <id>@chatroom' % where)
        spk = s.get('speaker', '')
        if not PERSON_RE.match(str(spk)) and spk != 'unknown':
            errors.append('%s: speaker 应为代号 Pn 或 unknown' % where)
        if s.get('label_source') not in VALID_SOURCES:
            errors.append('%s: label_source 必须是 %s' % (where, ' / '.join(VALID_SOURCES)))
        notes = str(s.get('notes', ''))
        if len(notes) < 10:
            errors.append('%s: notes 太短，无法复核' % where)
        refs = s.get('refs') or {}
        if not refs.get('message_id') or not refs.get('event_id'):
            errors.append('%s: refs 需要 message_id 与 event_id' % where)

        label = s.get('label') or {}
        for key in LABEL_KEYS:
            if key not in label:
                errors.append('%s: label 缺字段 %s' % (where, key))
                continue
            if key == 'thread_id':
                continue
        tid = label.get('thread_id', None)
        anchor = label.get('anchor', None)
        if anchor is not None and not PERSON_RE.match(str(anchor)):
            errors.append('%s: anchor 只能是 Pn 或 null（不可用 room/unknown）' % where)
        addressee = label.get('addressee')
        # `addressee` 是 0..n（§3.1）：`[]` = **合法的 0**——"这条说给谁判不出来"。
        # 旧版把空数组判 ERROR，于是"判不出来"这个形状**写不进评测集**（写了就报错、
        # 只能写成 `["room"]` 去冒充"说给全群"），该字段的形状信息被压成零。
        # 2026-10-06 量具修复：改为允许空；非数组仍报错（放宽的是"个数"，不是"形状"）。
        if not isinstance(addressee, list):
            errors.append('%s: addressee 必须是数组（0..n 个元素），得到 %s'
                          % (where, type(addressee).__name__))
        else:
            for item in addressee:
                if item != 'room' and not PERSON_RE.match(str(item)):
                    errors.append('%s: addressee 元素只能是 "room" 或 Pn，得到 %r' % (where, item))
        if not isinstance(label.get('should_switch'), bool):
            errors.append('%s: should_switch 必须是布尔' % where)
        if label.get('expected_behavior') not in VALID_BEHAVIORS:
            errors.append('%s: expected_behavior 必须是 %s'
                          % (where, ' / '.join(VALID_BEHAVIORS)))

        # 与显式标记的一致性（只查"可以机械判定"的两条，不猜语义）
        text = str(s.get('text', ''))
        if s.get('label_source') == 'explicit':
            mark = _explicit_marks(text)
            if not mark:
                errors.append('%s: label_source=explicit 但正文里找不到 @/引用/拍一拍 标记'
                              % where)
            elif 'mention' in mark and addressee == ['room']:
                warnings.append('%s: 正文有 @%s 但 addressee=["room"]，请确认'
                                % (where, ','.join(mark['mention'])))
            if 'pat' in mark and addressee != ['room']:
                target = mark['pat'][1]
                if PERSON_RE.match(target) and target not in addressee:
                    errors.append('%s: 拍一拍对象 %s 不在 addressee 里' % (where, target))
        # 系统消息 / 纯附件不得成为锚点依据
        if text.startswith('<?xml') and anchor is not None:
            warnings.append('%s: 系统消息仍给了 anchor=%s' % (where, anchor))
        quoted = quote_of(text)
        if quoted.present and not quoted.resolvable and anchor is not None:
            # 非人占位名 / 认不出作者的引用（`[引用 图片: …]` / `[引用: …]` /
            # `[引用〔作者不明〕: …]`）不是人，不能当锚点。判定走 canonical：
            # 打分器不再自带一份"哪些名字不是人"的名单。
            errors.append('%s: 引用了非人/认不出作者的占位名，却给了 anchor=%s'
                          % (where, anchor))

        if tid:
            thread_seen.setdefault(tid, []).append(s)
        if s.get('label_source') == 'human':
            missing_at.append(s)

    for sid, cnt in ids.items():
        if cnt > 1:
            errors.append('id 重复: %s ×%d' % (sid, cnt))

    # thread_id 复用检查：窗编号（A*/B*）不得跨窗复用；同 thread 的 anchor 演化须可解释
    for tid, group in thread_seen.items():
        anchors = [g['label'].get('anchor') for g in group if not g['label'].get('should_switch')]
        distinct = {a for a in anchors if a}
        if len(distinct) > 1:
            warnings.append('thread_id=%s 内 anchor 从 %s 演化到 %s（共 %d 条）：'
                            '确认是显式换锚而非标注漂移' % (tid, sorted(distinct)[0],
                                                    sorted(distinct)[-1], len(group)))
    return errors, warnings


def _explicit_marks(text):
    """正文里的显式归属标记（@ / 引用 / 拍一拍），**判定源 = canonical**。

    返回形状不变（``{'mention': [...], 'quote': str, 'pat': (sender, target)}``），
    只有"谁算显式标记"这一件事改成与线上同一套解析——否则 explicit 子集的通过率
    是打分器自问自答（循环验证）。
    """
    marks = {}
    raw = str(text or '')
    # @ 只看引用块**之后**新写的那段（引用里的 @ 是上下文，不是本轮目标），
    # 切分走 canonical 的括号配对，不用 `split(']')` 那种会在嵌套引用上切错的写法。
    body = strip_quote_block(raw)
    names = list(mentioned_names(body))
    if names:
        marks['mention'] = names
    quoted = quote_of(raw)
    if quoted.resolvable and quoted.author_name:
        marks['quote'] = quoted.author_name
    targets = pat_targets(raw)
    if targets:
        # 自己拍自己的行没有作者（canonical 口径），目标仍是被拍者。
        marks['pat'] = (pat_sender(raw), targets[0])
    return marks


# ----------------------------------------------------------------------
# 指标
# ----------------------------------------------------------------------
def text_candidate_anchors(sample):
    """**正文派生**的候选锚点（纯机械、确定性；不含真值）。

    三个来源，全部走 canonical（与 explicit 判定同源，所以"哪些名字算显式归属"
    只有一份实现）：

    1. ``room`` —— 全群广播（``addressee=["room"]`` 是显式的"说给全群"）；
    2. 正文里的显式归属：``@`` 点名（只看引用块**之后**新写的那段）+
       可解析的引用作者 + 拍一拍目标；
    3. ``speaker`` 本人 —— 第一人称自述/自指时"主角 = 说话人"（README §7 t0072 口径）。

    **只用本条样本自己的字段**（``speaker`` / ``text``）——不读历史、不读真值。
    代价：真值里那些**簇继承来的锚点**（主角在正文里没出现、顺着簇接下来的）派生
    不出来（本集 15 行，清单见 ``tests/test_threads_eval_integrity.py``）。
    """
    out = ['room']
    text = str(sample.get('text', ''))
    body = strip_quote_block(text)
    for name in mentioned_names(body):
        if name not in out:
            out.append(name)
    quoted = quote_of(text)
    if quoted.resolvable and quoted.author_name and quoted.author_name not in out:
        out.append(quoted.author_name)
    for target in pat_targets(text):
        if target not in out:
            out.append(target)
    speaker = str(sample.get('speaker', ''))
    if PERSON_RE.match(speaker) and speaker not in out:
        out.append(speaker)
    return out


def candidate_anchors(sample, truth_anchor=None):
    """候选集口径用的集合 = **正文派生集合 ∪ {真值锚点}**。

    为什么真值必须并进来：真值是**标注者给出的答案**，"一个说得通的答案"里当然
    包含它——这是集合的**定义**，不是为了让数字好看。不并进来会得到一个自相矛盾的
    读数：预测**恰好等于真值**却被候选集口径判错（真值自己都不算"说得通"）。
    并进来之后两套口径的关系才是干净的：**单值判对 ⟹ 候选集判对**，
    候选集多出来的那一部分完全来自正文派生的那一半。

    **候选集口径的红利全部来自候选集多出来的那一半**，所以算它的红利时要用
    ``text_anchor_ok``（只认正文派生集合），不能拿"含真值"的集合去算——那样
    真值那一项会白送一分。两个都返回，打印里都标清楚。
    """
    out = list(text_candidate_anchors(sample))
    if truth_anchor and truth_anchor not in out:
        out.append(truth_anchor)
    return out


def _reply_of(pred):
    return str((pred or {}).get('reply_text') or '')


def _assertions(text, sample, history):
    """从预测回复里抽出"历史断言"及其指向，返回 [(target, ok)]。

    ok=False 表示该断言在 history + 本样本里找不到支撑 → §4.3.5 违约（假承诺）。
    """
    out = []
    for m in ASSERT_RE.finditer(text):
        seg = text[max(0, m.start() - 12): m.end() + 24]
        # 断言指向的人：优先看断言附近出现的正文人名代号
        near = re.findall(r'P\d+', seg)
        target = near[0] if near else None
        supported = False
        pool = list(history) + [sample]
        for row in pool:
            if target and target not in str(row.get('text', '')):
                continue
            if target and str(row.get('speaker')) == target:
                supported = True
                break
            if target and 'room' in (sample['label'].get('addressee') or []) and \
                    target in str(row.get('text', '')):
                supported = True
                break
        out.append((target, supported))
    return out


# ----------------------------------------------------------------------
# 出站轴的两项新指标（M2-6）：迟到率 / 胡编率
#
# 2026-10-24：出戏率（``internal_lexicon``）与复述率（``probe_self_repetition``）
# 随那一族判据一起退役——判据源已不存在，留着只会是一列恒 ``n/a``。
#
# 口径（分子 / 分母 / 判据来源）写在文件头那段"七项指标"里，**一处定义、此处实现**。
# 两个判据源全部是既有 canonical 实现，本脚本只做"取数 + 计数"：
#
#   * 胡编率 → ``core.context.message_facts.evaluate_claims``（claim 台账读侧，唯一 IO）；
#   * 迟到率 → 时间戳对齐（``send_log`` / ``outbound_audit`` / ``work_items.due_at`` 同源）。
# ----------------------------------------------------------------------

#: 迟到率的默认**应答预算**（秒）。它**不是门槛**（门槛表里那一格是 ``N/A``）：
#: 它回答的是"多久算迟"，属于**读数参数**，所以每次跑分都把它打印出来，
#: 免得有人拿一个不带预算的迟到率去比。默认 60 s 的依据：2026-09-30 21:44–21:56
#: 实测的应答延迟里，唯一一条事故级迟到是 21:44:59 → 21:46:01 = **62 s**，
#: 其余正确应答都 ≤43 s（63 s 那一档只有它一条）——60 s 是"把事故与正常应答分开"
#: 的最小整数档，且它是可调的显式参数，不是藏在代码里的隐阈值。
DEFAULT_REPLY_BUDGET_SECONDS = 60.0


def _facts():
    """延迟导入 claim 台账读侧（唯一 IO 点在那个模块里）。"""
    from core.context import message_facts
    return message_facts


def outbound_reply(sample, pred):
    """这一条样本代表的**出站正文**：``(文本, 来源)``。

    * ``("predictor")`` —— 预测器回传了 ``reply_text``（离线跑分/线上回放都走这一格）；
    * ``("replay")`` —— 没回传，而样本本身就是 ``speaker == "P1"`` 的行：**那就是她当时
      真发出去的那句话**（``send_log.content`` 同源），所以按出站回放计入；
    * ``("", "")`` —— 都不是她说的 → 不进任何新指标的分母。
    """
    text = _reply_of(pred)
    if text:
        return text, 'predictor'
    if str(sample.get('speaker')) == 'P1':
        return str(sample.get('text') or ''), 'replay'
    return '', ''



def _trigger_at(sample, history, addressee):
    """本轮应答的**触发时刻**（``send_log.trigger_msg_id`` 的同源口径）。

    优先：该回复**收件人**在窗口里最近一条发言的时刻（收件人 = ``addressee`` 里第一个
    ``Pn``；``room``/空 → 跳过这一档）。退回：窗口里最近一条**非她**的发言。
    窗口里都没有 → 返回 ``""``（这一行不进迟到率分母：没有触发点就没有"迟到"可言）。
    """
    before = [h for h in history if h['at'] < sample['at']]
    if not before:
        return ''
    who = ''
    for item in addressee or ():
        if isinstance(item, str) and PERSON_RE.match(item):
            who = item
            break
    if who:
        own = [h for h in before if str(h.get('speaker')) == who]
        if own:
            return max(h['at'] for h in own)
    other = [h for h in before if str(h.get('speaker')) != 'P1']
    if other:
        return max(h['at'] for h in other)
    return ''


def annotate_outbound_metrics(rows, context, *, reply_budget_seconds=DEFAULT_REPLY_BUDGET_SECONDS,
                              ledger_replay=False):
    """把两项出站指标逐条判进 ``row['late'/'fabrication']``。

    **只写新键**：既有五项（``anchor_ok`` / ``switch_bad`` / ``thread_ok`` /
    ``promise_bad`` / ``unresolved``）一个字节都不动——新指标是**并列读数**，
    不许顶替或污染旧口径。

    ``context``：``{样本 id: (sample, history)}``（``history`` 与喂给预测器的同一个窗口）。
    遍历按 ``(at, id)`` 排（时间序），所以"近 N 条自己的话"与台账登记顺序都是确定的。
    """
    facts = _facts()
    tracing = bool(facts.ledger_tracing())
    # 台账句柄：``--ledger-replay`` 用**本批语料自建**的内存台账（零生产 IO）；
    # 否则交给 canonical 读侧自己决定（flag 关着时它一行都不走）。
    book = facts.ClaimLedger(records=[]) if ledger_replay else None
    ordered = sorted(rows, key=lambda item: (item['at'], item['id']))
    for row in ordered:
        sample, history = context[row['id']]
        text, source = outbound_reply(sample, {'reply_text': row.get('reply_text') or ''})
        row['reply'] = {'text': text, 'source': source}
        # 这一轮**实际生效**的参数（打印出来，免得读数脱离它的预算/阈值被引用）
        row['outbound_config'] = {'budget_seconds': float(reply_budget_seconds)}
        # ① 迟到率：时间戳对齐（没有触发点或没有落地时刻 → 不进分母）
        sent = str(row.get('reply_at') or '') or (
            str(sample.get('at') or '') if source == 'replay' else '')
        addressee = row['pred'].get('addressee')
        if not isinstance(addressee, list) or not addressee:
            addressee = sample['label'].get('addressee') or []
        trigger = _trigger_at(sample, history, addressee)
        if sent and trigger:
            delay = _seconds_between(trigger, sent)
            late = (sent > trigger) and (delay > float(reply_budget_seconds))
            row['late'] = {'sent_at': sent, 'trigger_at': trigger, 'delay': delay,
                           'budget': float(reply_budget_seconds), 'late': bool(late)}
        else:
            row['late'] = None
        # ② 胡编率：claim 台账（读侧唯一 IO；写侧只在 --ledger-replay 时喂本批语料，
        #    而且**只喂内存台账**，绝不碰生产库）
        if not (tracing or ledger_replay):
            row['fabrication'] = {'enabled': False, 'judged': 0, 'unsupported': 0,
                                  'reasons': [], 'source': 'ledger-off'}
        elif ledger_replay and str(sample.get('speaker')) != 'P1':
            facts.record_message_claims(
                facts.ClaimMessage(
                    text=str(sample.get('text') or ''),
                    chat_id=str(sample.get('chat') or ''),
                    speaker_key=str(sample.get('person_id') or ''),
                    message_id=str((sample.get('refs') or {}).get('message_id') or ''),
                    observed_at=sample.get('at'), kind='text'),
                ledger=book)
            row['fabrication'] = {'enabled': True, 'judged': 0, 'unsupported': 0,
                                  'reasons': [], 'source': 'ledger-replay'}
        else:
            keys = [sample.get('label', {}).get('anchor_person_id') or '']
            keys += [p for p in (sample.get('label', {}).get(
                'addressee_person_ids') or []) if p and p != 'room']
            probe = facts.evaluate_claims(
                text, chat_id=str(sample.get('chat') or ''),
                person_keys=[p for p in keys if p], ledger=book)
            row['fabrication'] = {
                'enabled': bool(probe.enabled), 'judged': len(probe.verdicts),
                'unsupported': len(probe.unsupported),
                'reasons': [v.reason for v in probe.unsupported],
                'source': 'ledger-replay' if ledger_replay else 'ledger',
            }


def score(samples, predictor, history_window=180.0, limit_history=None,
          reply_budget_seconds=DEFAULT_REPLY_BUDGET_SECONDS,
          ledger_replay=False):
    rows = []
    context = {}
    by_chat = OrderedDict()
    for s in samples:
        by_chat.setdefault(s['chat'], []).append(s)

    for s in samples:
        hist = [h for h in by_chat[s['chat']]
                if h['at'] < s['at'] and _seconds_between(h['at'], s['at']) <= history_window]
        if limit_history:
            hist = hist[-limit_history:]
        # 配对用的样本 id 要在**剥字段之前**取（link 口径按 id 找对面那条的读数）。
        hist_ids = [str(h.get('id', '')) for h in hist]
        pub = [{k: h[k] for k in ('at', 'speaker', 'text', 'refs')} for h in hist]
        try:
            pred = predictor(s, pub) or {}
        except Exception as exc:  # noqa: BLE001 - 预测器崩了要算"未判定"而不是把整轮跑挂
            pred = {'error': '%s: %s' % (type(exc).__name__, exc)}
        rows.append(_judge(s, pred, pub, hist_ids=hist_ids))
        # 出站指标要用**同一个窗口**与原始样本（触发点对齐）→ 一起留下来。
        context[s['id']] = (s, pub)
    annotate_outbound_metrics(
        rows, context, reply_budget_seconds=reply_budget_seconds, ledger_replay=ledger_replay)
    return rows



def _seconds_between(a, b):
    import datetime as _dt
    fmt = '%Y-%m-%d %H:%M:%S'
    return abs((_dt.datetime.strptime(b, fmt) - _dt.datetime.strptime(a, fmt)).total_seconds())


def _judge(s, pred, hist, hist_ids=()):
    label = s['label']
    truth_a = label.get('anchor')
    truth_t = label.get('thread_id') or ''
    truth_switch = bool(label.get('should_switch'))
    truth_add = label.get('addressee') or []

    p_anchor = pred.get('anchor', None)
    p_thread = pred.get('thread_id', None)
    p_add = pred.get('addressee', None)
    p_switch = pred.get('should_switch', None)
    p_conf = str(pred.get('confidence', '') or '')
    unresolved = (p_anchor is None and p_conf in ('unresolved', 'low', 'uncertain')) or \
                 p_conf == 'unresolved'
    errored = 'error' in pred

    anchor_ok = None
    if errored:
        anchor_ok = False
    elif truth_a is not None:
        anchor_ok = (not unresolved) and p_anchor == truth_a

    # 候选集口径（并列读数；定义见 ``candidate_anchors``）：同一分母，只把"判对"
    # 放宽成"落在候选集里"。集合 = 正文派生 ∪ {真值}，所以单值判对 ⟹ 候选集判对。
    candidates = candidate_anchors(s, truth_a)
    text_candidates = text_candidate_anchors(s)
    anchor_ok_cs = None
    if errored:
        anchor_ok_cs = False
    elif truth_a is not None:
        anchor_ok_cs = (not unresolved) and p_anchor in candidates
    # "候选集口径的红利"只算**正文派生集合**那一半（含真值的集合会白送真值那一分）。
    anchor_ok_text = None
    if errored:
        anchor_ok_text = False
    elif truth_a is not None:
        anchor_ok_text = (not unresolved) and p_anchor in text_candidates

    switch_bad = None
    if isinstance(p_switch, bool) and not truth_switch:
        # 真值说不该换：预测换了主角，或换了收件人（收件人只算"@了真值以外的人"）
        bad = bool(p_switch)
        if not bad and p_add and truth_add:
            extra = [x for x in p_add if x not in truth_add and x != 'room']
            if extra and truth_add == ['room']:
                bad = True
        switch_bad = bad

    # 反方向（§九 的换人误判率**只**测"多换"）：真值说该换、预测没换 = 漏换。
    # 它是**读数**（无门槛），但不测它就答不了"该换没换还是不该换却换了"。
    switch_miss = None
    if isinstance(p_switch, bool) and truth_switch:
        switch_miss = not bool(p_switch)

    thread_ok = None
    if not errored and truth_t:
        if p_thread not in (None, ''):
            thread_ok = (p_thread == truth_t)

    promise_bad = None
    reply = _reply_of(pred)
    if reply and ASSERT_RE.search(reply):
        checks = _assertions(reply, s, hist)
        promise_bad = any(not ok for _, ok in checks)
    elif reply:
        promise_bad = False

    return {
        'id': s['id'], 'at': s['at'], 'speaker': s['speaker'],
        'truth': {'thread_id': truth_t, 'anchor': truth_a, 'addressee': truth_add,
                  'should_switch': truth_switch},
        'pred': {'thread_id': p_thread, 'anchor': p_anchor, 'addressee': p_add,
                 'should_switch': p_switch, 'confidence': p_conf,
                 'anchor_evidence': list(pred.get('anchor_evidence') or ()),
                 'switch_evidence': list(pred.get('switch_evidence') or ()),
                 'anchor_changed': pred.get('anchor_changed', None),
                 'takeover_allowed': pred.get('takeover_allowed', None),
                 'takeover_evidence': list(pred.get('takeover_evidence') or ()),
                 'about': pred.get('about', None),
                 'about_is_speaker': pred.get('about_is_speaker', None),
                 'topic': str(pred.get('topic') or '')},
        'tags': s.get('tags', []), 'label_source': s.get('label_source'),
        'hist_ids': [str(item) for item in (hist_ids or ()) if str(item)],
        # 出站轴新指标（M2-6）要用的**原始输入**：回复正文 + 落地时刻。
        # 只增字段，不动上面任何一个既有读数。
        'reply_text': reply,
        'reply_at': str(pred.get('reply_at') or pred.get('sent_at') or ''),
        'anchor_ok': anchor_ok, 'anchor_ok_cs': anchor_ok_cs,
        'anchor_ok_text': anchor_ok_text,
        'candidates': candidates, 'text_candidates': text_candidates,
        'switch_bad': switch_bad, 'switch_miss': switch_miss,
        'thread_ok': thread_ok,
        'promise_bad': promise_bad, 'unresolved': bool(unresolved), 'error': errored,
    }


def link_stats(rows):
    """link 口径的错线读数（**主口径**；定义见文件头）。

    配对只看 "更晚那条当轮真正看得见的历史"（``row['hist_ids']`` = 喂给预测器的那个
    窗口），所以每对只数一次；两侧 ``thread_id`` 都非空才判"预测连了这条边"。

    返回的每个比率都带**自己的分母**；分母 0 → ``None``（打印成 ``n/a``，不编数）。
    另外标出"错连"的行（``over_linked_ids``）——失败样本清单要用它，否则清单会被
    旧口径刷成"全部 100 条"（那样等于没有清单）。
    """
    by_id = {r['id']: r for r in rows}
    tp = fp = fn = 0
    over_linked = set()
    missed = set()
    for row in rows:
        truth_s = row['truth']['thread_id'] or ''
        pred_s = row['pred']['thread_id'] or ''
        for other_id in row.get('hist_ids') or ():
            other = by_id.get(other_id)
            if other is None:
                continue  # 历史里没有它的读数（理论上不该发生）：不算，不猜
            truth_h = other['truth']['thread_id'] or ''
            pred_h = other['pred']['thread_id'] or ''
            truth_link = bool(truth_s) and truth_s == truth_h
            pred_link = bool(pred_s) and pred_s == pred_h
            if truth_link and pred_link:
                tp += 1
            elif pred_link and not truth_link:
                fp += 1
                over_linked.add(row['id'])
                over_linked.add(other_id)
            elif truth_link and not pred_link:
                fn += 1
                missed.add(row['id'])
                missed.add(other_id)
    precision = (float(tp) / (tp + fp)) if (tp + fp) else None
    recall = (float(tp) / (tp + fn)) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)) if (precision and recall) else None
    return {
        'link_precision': precision, 'link_precision_n': tp + fp,
        'link_recall': recall, 'link_recall_n': tp + fn,
        'link_f1': f1,
        'wrong_link_rate': (1.0 - precision) if precision is not None else None,
        'link_tp': tp, 'link_fp': fp, 'link_fn': fn,
        'over_linked_ids': sorted(over_linked), 'missed_ids': sorted(missed),
    }


def summarize(rows):
    def ratio(num, den):
        return (float(num) / den) if den else None

    anchor_den = sum(1 for r in rows if r['anchor_ok'] is not None)
    anchor_num = sum(1 for r in rows if r['anchor_ok'] is True)
    anchorset_den = sum(1 for r in rows if r.get('anchor_ok_cs') is not None)
    anchorset_num = sum(1 for r in rows if r.get('anchor_ok_cs') is True)
    # **已作答子集**（预测非 unresolved）：两套口径都只在这个子集上比。
    # 为什么要单列：`unresolved` 的行两套口径都记 False（"没给答案"不是"给了另一个
    # 说得通的答案"），把它们压在同一个分母里会同时拉低两个数——而"锚点判得准不准"
    # 这件事本身只在**给出了答案**时才谈得上。两轮独立盲标实验报的 0.869 / 0.71 就是
    # 这个子集（实测本集 55/63=0.873 vs 51/70=0.729，同一量级）。
    resolved = [r for r in rows if r['anchor_ok'] is not None and not r['unresolved']]
    resolved_den = len(resolved)
    resolved_num = sum(1 for r in resolved if r['anchor_ok'] is True)
    resolved_cs_num = sum(1 for r in resolved if r.get('anchor_ok_cs') is True)
    # "候选集口径的红利" = 只靠**正文派生**那一半救回来的条数（不含真值那一项白送的分）。
    text_num = sum(1 for r in rows if r.get('anchor_ok_text') is True)
    # 拆开：正文派生集合**救回**的（单值判错→它判对）与**弄丢**的
    # （单值判对→它判错，因为真值那个继承来的主角不在正文派生集合里）。
    # 净额 = 宽集合相对单值口径的差；两个分量都打印，别让净额把丢的那部分藏起来。
    text_gain = sum(1 for r in rows
                    if r.get('anchor_ok_text') is True and r['anchor_ok'] is False)
    text_loss = sum(1 for r in rows
                    if r.get('anchor_ok_text') is False and r['anchor_ok'] is True)
    switch_den = sum(1 for r in rows if r['switch_bad'] is not None)
    switch_num = sum(1 for r in rows if r['switch_bad'] is True)
    miss_den = sum(1 for r in rows if r.get('switch_miss') is not None)
    miss_num = sum(1 for r in rows if r.get('switch_miss') is True)
    thread_den = sum(1 for r in rows if r['thread_ok'] is not None)
    thread_num = sum(1 for r in rows if r['thread_ok'] is False)
    unres_den = len(rows)
    unres_num = sum(1 for r in rows if r['unresolved'])
    promise_den = sum(1 for r in rows if r['promise_bad'] is not None)
    promise_num = sum(1 for r in rows if r['promise_bad'] is True)
    # ---- 出站轴两项（M2-6）：分子 / 分母 / 判据来源见文件头"七项指标" ----
    outbound = [r for r in rows if (r.get('reply') or {}).get('text')]
    pred_replies = [r for r in outbound if (r.get('reply') or {}).get('source') == 'predictor']
    late_den = sum(1 for r in rows if r.get('late') is not None)
    late_num = sum(1 for r in rows if (r.get('late') or {}).get('late') is True)
    fab = [r.get('fabrication') or {} for r in rows]
    fab_den = sum(int(item.get('judged') or 0) for item in fab)
    fab_num = sum(int(item.get('unsupported') or 0) for item in fab)
    judged_rows = sum(1 for item in fab if int(item.get('judged') or 0) > 0)
    ledger_sources = sorted({str(item.get('source') or '') for item in fab})
    stats = {
        'anchor_accuracy': ratio(anchor_num, anchor_den), 'anchor_n': anchor_den,
        # 并列读数（**两套都保留**）：单值口径 = anchor_accuracy；候选集口径见文件头。
        'anchor_accuracy_candidateset': ratio(anchorset_num, anchorset_den),
        'anchor_candidateset_n': anchorset_den,
        # 红利 = 单值判错、**正文派生集合**判对（不含真值白送的那一分）。
        'anchor_candidate_widenings': text_num - anchor_num,
        'anchor_text_gain': text_gain, 'anchor_text_loss': text_loss,
        # 已作答子集上的同一对读数（口径见上面 resolved 那段注释）。
        'anchor_accuracy_resolved': ratio(resolved_num, resolved_den),
        'anchor_accuracy_candidateset_resolved': ratio(resolved_cs_num, resolved_den),
        'anchor_resolved_n': resolved_den,
        'switch_error_rate': ratio(switch_num, switch_den), 'switch_n': switch_den,
        # 读数（无门槛）：漏换方向，见 _judge 的 switch_miss。
        'switch_miss_rate': ratio(miss_num, miss_den), 'switch_miss_n': miss_den,
        'unresolved_rate': ratio(unres_num, unres_den), 'unresolved_n': unres_den,
        'false_promise_rate': ratio(promise_num, promise_den), 'promise_n': promise_den,
        # **deprecated**：真值窗编号 vs 预测 message_ref，两个命名空间永不相等
        # （恒 1.000、零信息量）。保留只为历史对账，不出现在 THRESHOLDS 里。
        'thread_error_rate': ratio(thread_num, thread_den), 'thread_n': thread_den,
        'errors': sum(1 for r in rows if r['error']),
        # ---- 两项出站指标（M2-6）----
        'lateness_rate': ratio(late_num, late_den), 'lateness_n': late_den,
        'lateness_hit_ids': ['%s(+%.1fs)' % (r['id'], (r.get('late') or {}).get('delay', 0.0))
                             for r in rows if (r.get('late') or {}).get('late') is True],
        'fabrication_rate': ratio(fab_num, fab_den), 'fabrication_n': fab_den,
        'fabrication_unsupported_ids': [r['id'] for r in rows
                                        if int((r.get('fabrication') or {}).get(
                                            'unsupported') or 0) > 0],
        # 两项的**分母来源**读数（定门槛与复盘用；不是指标本身）
        'outbound_replies': len(outbound),
        'predicted_replies': len(pred_replies),
        'replayed_replies': len(outbound) - len(pred_replies),
        'assertion_coverage': ratio(judged_rows, len(outbound)),
        'ledger_sources': ','.join(item for item in ledger_sources if item),
        'budget_seconds': float((rows[0].get('outbound_config') or {}).get(
            'budget_seconds') if rows else DEFAULT_REPLY_BUDGET_SECONDS),
    }
    stats.update(link_stats(rows))
    return stats


#: **已作废**的读数（打印时明写 deprecated、**不设门槛**）：口径坏在命名空间
#: （人工窗编号 vs 簇首条 message_ref）。别拿它当验收依据。
DEPRECATED_METRICS = (
    ('错线率（旧口径）', 'thread_error_rate', 'thread_n',
     '真值 thread_id 是人工窗编号（A8/B6），预测是簇首条 message_ref，'
     '两个命名空间永不相等 → 恒 1.000、零信息量；主口径改用 link 口径'),
)


THRESHOLDS = [
    ('锚点准确率', 'anchor_accuracy', 'anchor_n', '>=', 0.85),
    # §九【已定 13】的"错线率 ≤0.10"落在**可测替身**上：错合率 = 1 - link_precision
    # （把不同簇连在一起的比例；"漏归簇"由 link_recall 读、不设门槛）。
    ('错合率(link 口径)', 'wrong_link_rate', 'link_precision_n', '<=', 0.10),
    ('换人误判率', 'switch_error_rate', 'switch_n', '<=', 0.05),
    ('不确定率', 'unresolved_rate', 'unresolved_n', '<=', 0.35),
    ('假承诺率', 'false_promise_rate', 'promise_n', '==', 0.0),
    # ---- 新增两项（M2-6）：**先落 N/A**（门槛未定），值照常可读 ----
    # `op` / `thr` 都是 None = "门槛未定"：`threshold_verdicts` 对 `thr is None` 恒返回
    # `N/A`，**永远不会**因为未定的门槛把读数刷红或刷绿。定门槛需要什么，见
    # `print_report` 里那一段"新两项：要定门槛还缺什么"。
    ('胡编率', 'fabrication_rate', 'fabrication_n', None, None),
    ('迟到率', 'lateness_rate', 'lateness_n', None, None),
]

#: 门槛未定的两项各自"还缺什么"（**跟着打印**，否则 N/A 会变成一句没有信息量的占位）。
NEW_THRESHOLD_GAPS = {
    '胡编率': '台账里要有**她自己**的断言可判：本批断言覆盖率 0（她的回复一句都没落进'
              '字段词表），真实事故 t0093「P6是广东的」是"第三人称 + 是X的"形态，'
              '词表四型（籍贯是X／X人／我是X／你是X人）**都盖不住** → 要么补形态，'
              '要么明确"这一类不进胡编率"，之后才有分母可定门槛',
    '迟到率': '要定 `--reply-budget-seconds`：它现在由读数参数给（默认 60 s），'
              '缺的是**正常应答延迟的分布**（本批 21:44–21:56 正确应答 ≤43 s），'
              '以及计划类 notify 那一半（`work_items.due_at` ↔ `work_item_runs.finished_at`）'
              '要不要并进同一个分母',
}


def threshold_verdicts(stats, table=THRESHOLDS):
    """返回 [(指标名, 值, 分母, op, 门槛, 'PASS'/'FAIL'/'N/A')]。

    ``thr is None`` = **门槛未定**（M2-6 的两项新指标先落这一档）：恒返回 ``N/A``，
    但**值/分母照常带出来**（"可读"与"有门槛"是两件事）。
    """
    out = []
    for name, key, nkey, op, thr in table:
        val = stats.get(key)
        if thr is None:
            out.append((name, val, stats.get(nkey, 0), op, thr, 'N/A'))
            continue
        if val is None:
            out.append((name, None, stats.get(nkey, 0), op, thr, 'N/A'))
            continue
        ok = (val >= thr) if op == '>=' else (val <= thr) if op == '<=' else (val == thr)
        out.append((name, val, stats.get(nkey, 0), op, thr, 'PASS' if ok else 'FAIL'))
    return out


def print_report(stats, rows, show_failures=25):
    def fmt(v):
        return 'n/a' if v is None else '%.3f' % v

    print('=' * 68)
    print('§九 七项指标（既有五项门槛来自 §九 指标表 / §十四.2；新增两项门槛未定 = N/A）')
    print('-' * 68)
    print('%-12s %-10s %-8s %-10s %s' % ('指标', '得分', '样本数', '门槛', '结论'))
    for name, val, n, op, thr, verdict in threshold_verdicts(stats):
        if thr is None:
            print('%-12s %-10s %-8s %-10s %s'
                  % (name, fmt(val), n, 'N/A',
                     '门槛未定（值可读；缺什么见下）' if val is not None
                     else '门槛未定 + 分母=0 → n/a'))
            continue
        if verdict == 'N/A' and name == '假承诺率':
            verdict = '无法判定（预测器没给 reply_text）'
        elif verdict == 'N/A':
            verdict = '无法判定（分母=0）'
        print('%-12s %-10s %-8s %-10s %s' % (name, fmt(val), n, '%s %s' % (op, thr), verdict))
    print('-' * 68)
    print('读数（无门槛；只记录、不判定）:')
    for name, key, nkey in READOUTS:
        val = stats.get(key)
        if key == 'anchor_text_gain':
            shown = '%s / %s' % (stats.get('anchor_text_gain', 0),
                                 stats.get('anchor_text_loss', 0))
            print('  %-24s %-10s 分母 %s（与单值口径同分母）'
                  % (name, shown, stats.get(nkey, 0)))
            continue
        shown = ('%d' % val) if (key in COUNT_METRICS and val is not None) else fmt(val)
        print('  %-24s %-10s 分母 %s' % (name, shown, stats.get(nkey, 0)))
    for name, key, nkey, why in DEPRECATED_METRICS:
        val = stats.get(key)
        print('  %-24s %-10s 分母 %s  [deprecated] %s'
              % (name, fmt(val), stats.get(nkey, 0), why))
    print('  %-24s %-10s（TP=%s FP=%s FN=%s）' % (
        '链接计数', '', stats.get('link_tp', 0), stats.get('link_fp', 0),
        stats.get('link_fn', 0)))
    print('-' * 68)
    print_report_outbound(stats, rows)
    print('-' * 68)
    print('预测器抛异常条数: %d' % stats['errors'])
    print_failures(stats, rows, show_failures=show_failures)


def print_report_outbound(stats, rows):
    """两项新指标的**读数块**：值 + 分母来源 + 定门槛缺什么。

    这块只打印，不判定（两项的门槛都是 ``N/A``）。近例（M2-6 ④）也在这里逐条列出，
    好让"指标动了 = 事故少了"在**同一次跑分**里就能对上号。
    """
    def fmt(v):
        return 'n/a' if v is None else '%.3f' % v

    print('新增两项（M2-6；门槛未定 = N/A，值可读）:')
    print('  迟到率             %-10s 分母 %s（判据：时间戳对齐）'
          % (fmt(stats.get('lateness_rate')), stats.get('lateness_n', 0)))
    print('  胡编率             %-10s 分母 %s 条断言（判据：claim 台账读侧 %s）'
          % (fmt(stats.get('fabrication_rate')), stats.get('fabrication_n', 0),
             stats.get('ledger_sources', '')))
    print('  分母来源：出站回放 %s 条（预测器回传 reply_text %s 条 + speaker=P1 正文 %s 条）'
          % (stats.get('outbound_replies', 0), stats.get('predicted_replies', 0),
             stats.get('replayed_replies', 0)))
    print('  参数：迟到预算 %ss' % stats.get('budget_seconds', 0))
    print('  读数（不是指标）：台账断言覆盖率 %s' % fmt(stats.get('assertion_coverage')))
    hits = [
        ('迟到命中', stats.get('lateness_hit_ids') or []),
        ('台账无支撑断言', stats.get('fabrication_unsupported_ids') or []),
    ]
    for label, ids in hits:
        print('  %-14s %s' % (label, ', '.join(ids) if ids else '（无）'))
    print('  新两项：要定门槛还缺什么')
    for name in ('胡编率', '迟到率'):
        print('    %s：%s' % (name, NEW_THRESHOLD_GAPS.get(name, '')))

#: 只看不判的读数（**无门槛**）：漏合 / 漏换 / F1 与旧口径对账。
#: 末尾两条是**锚点两套口径并列**：单值口径（有门槛，在 THRESHOLDS 里）与
#: 候选集口径（无门槛，只记录）——两者同分母，所以差 = "被单值口径记错、
#: 但落在候选集里"的条数（``anchor_candidate_widenings``）。**删掉任何一条都是
#: 把一种读法藏起来**，不许。
READOUTS = (
    ('链接召回 link_recall', 'link_recall', 'link_recall_n'),
    ('链接 F1', 'link_f1', 'link_precision_n'),
    ('漏换率（该换没换）', 'switch_miss_rate', 'switch_miss_n'),
    ('锚点候选集口径（并列）', 'anchor_accuracy_candidateset', 'anchor_candidateset_n'),
    ('  净额（宽集合 - 单值）', 'anchor_candidate_widenings', 'anchor_n'),
    ('  其中正文派生救回 / 弄丢', 'anchor_text_gain', 'anchor_n'),
    ('已作答：单值口径', 'anchor_accuracy_resolved', 'anchor_resolved_n'),
    ('已作答：候选集口径', 'anchor_accuracy_candidateset_resolved', 'anchor_resolved_n'),
)

#: READOUTS 里按**计数**打印的键（不是比率，别显示成 4.000）。
COUNT_METRICS = frozenset({'anchor_candidate_widenings', 'anchor_text_gain'})


def print_failures(stats, rows, show_failures=25):
    """失败样本清单（锚点错 / 误换人 / link 错连）＋事故窗通过数。

    旧口径的 ``thread_ok`` 已作废，用它会把整批都列进来，等于没有清单。
    """
    # 失败样本：锚点 / 换人 / **link 错连**（旧口径的 thread_ok 已作废，用它会把
    # 100 条全列进来，等于没有清单）。
    over_linked = set(stats.get('over_linked_ids') or ())
    bad = [r for r in rows
           if r['anchor_ok'] is False or r['switch_bad'] or r['id'] in over_linked]
    if bad and show_failures:
        print('-' * 68)
        print('失败样本（锚点错 / 误换人 / link 错连；前 %d 条）:' % min(show_failures, len(bad)))
        for r in bad[:show_failures]:
            print('  %s %s %s truth=%s pred=%s' % (r['id'], r['at'], r['speaker'],
                                                   r['truth'], r['pred']))
    incident = [r for r in rows if 'incident' in r['tags']]
    if incident:
        n_ok = sum(1 for r in incident if r['anchor_ok'] is not False
                   and not r['switch_bad'] and r['id'] not in over_linked)
        print('事故窗样本（tags 含 incident）: %d 条，通过 %d 条（§十四.2 要求全部通过；'
              '口径 = 锚点不判错 + 不误换人 + 无 link 错连）'
              % (len(incident), n_ok))


def dry_run(samples, errors, warnings, path=DEFAULT_DATASET):
    print('数据集自检: %s' % path)
    print('  样本数: %d' % len(samples))
    src = Counter(s.get('label_source') for s in samples)
    print('  label_source: human=%d explicit=%d' % (src.get('human', 0), src.get('explicit', 0)))
    neg = [s for s in samples if 'negative' in (s.get('tags') or [])]
    print('  负样本(tags 含 negative): %d' % len(neg))
    for tag in ('windowA', 'windowB', 'incident', 'attachment', 'bot_ok', 'bot_violation',
                'ambiguous', 'ambiguous-window', 'scope:core', 'scope:context',
                # 出站指标的事故近例行（M2-6）：逐条可查，便于"指标动了 = 事故少了"
                'incident:repeat', 'incident:ooc', 'incident:facts', 'incident:late'):
        n = sum(1 for s in samples if tag in (s.get('tags') or []))
        if n:
            print('  tags[%s]: %d' % (tag, n))
    threads = Counter((s.get('label') or {}).get('thread_id') or '' for s in samples)
    print('  thread_id 数: %d（含未归线 ""）' % len(threads))
    spk = Counter(s.get('speaker') for s in samples)
    print('  speaker 分布: %s' % ', '.join('%s=%d' % kv for kv in sorted(spk.items())))
    print('  含 @/引用/拍一拍 标记的样本（canonical 判定）: %d'
          % sum(1 for s in samples if _explicit_marks(str(s.get('text', '')))))
    print('-' * 68)
    for w in warnings:
        print('WARN  %s' % w)
    for e in errors:
        print('ERROR %s' % e)
    if errors:
        print('=> 数据集有问题：%d 个错误、%d 个警告' % (len(errors), len(warnings)))
        return 1
    print('=> 数据集结构合法（%d 条；%d 个警告）' % (len(samples), len(warnings)))
    return 0


def _load_predictor(spec):
    """延迟导入预测器：只在真的要跑分时才 import。

    导入成功后，若预测器模块提供 ``reset_eval_state()`` 就调一次：跨轮状态是
    **进程级**的，不重置的话"同一个进程里跑第二遍"会继承上一遍最后一条的焦点 →
    两次跑分不同数（读数不可复现）。开新进程跑当然也对，但不能把可复现性押在
    "记得开新进程"上。
    """
    if ':' not in spec:
        raise SystemExit('--predictor 需要 module:function 形式，得到 %r' % spec)
    mod_name, func_name = spec.split(':', 1)
    import importlib
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as exc:
        raise SystemExit('导入预测器模块 %s 失败: %s\n'
                         '（提示：core/context/threads.py 尚未实现时请只用 --dry-run）'
                         % (mod_name, exc))
    fn = getattr(mod, func_name, None)
    if fn is None:
        raise SystemExit('模块 %s 里没有 %s' % (mod_name, func_name))
    reset = getattr(mod, 'reset_eval_state', None)
    if callable(reset):
        try:
            reset()
        except Exception as exc:  # noqa: BLE001 - 重置失败不阻断跑分（只是可能不自洽）
            print('WARN 预测器 reset_eval_state() 失败: %s' % exc)
    return fn


def main(argv=None):
    # 管道 / 重定向输出统一 UTF-8（**只改输出编码，一个读数都不动**）：本报表是中文，
    # 且含 `↔`（U+2194，GBK 编不出）——在 cp936 的**管道**上 `print` 会
    # `UnicodeEncodeError` 崩在 `print_report_outbound`，于是"失败清单 / 事故窗通过数 /
    # 新两项要定门槛还缺什么"永远打不出来，七项指标的后半段也不完整。
    # 放在 ``main()`` 里（不在 import 期）是为了被测试 import 时**零副作用**；
    # 与 `scripts/napcat_ctl.py` / `scripts/audit_sinking.py` 的管道 UTF-8 兜底同一纪律。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:  # noqa: BLE001 - 被包装的流 / 老解释器：退回原编码，不影响读数
            pass
    ap = argparse.ArgumentParser(description='话题簇/锚点评集打分（§九 七项指标）')
    ap.add_argument('--dataset', default=DEFAULT_DATASET, help='JSONL 路径')
    ap.add_argument('--predictor', default=None,
                    help='module:function —— 模块导入延迟到参数解析之后')
    ap.add_argument('--dry-run', action='store_true', help='只校验数据集结构与统计')
    ap.add_argument('--history-window', type=float, default=180.0,
                    help='喂给预测器的历史窗口秒数（默认 180）')
    ap.add_argument('--limit-history', type=int, default=None, help='历史最多取多少条')
    ap.add_argument('--json', default=None, help='把逐条结果写到该 JSON 文件')
    # ---- 出站指标的读数参数：不是门槛，默认值 = canonical 配置 ----
    ap.add_argument('--reply-budget-seconds', type=float, default=DEFAULT_REPLY_BUDGET_SECONDS,
                    help='迟到率的"多久算迟"预算（秒，默认 60；不是门槛，会打印出来）')
    ap.add_argument('--ledger-replay', action='store_true',
                    help='胡编率：用**本批语料**在内存里按 canonical 写侧自建 claim 台账'
                         '（影子回放；不读也不写生产库）。默认关闭 = 只认生产台账读侧，'
                         'flag 关着时胡编率是 n/a')
    args = ap.parse_args(argv)

    try:
        samples = load_dataset(args.dataset)
    except DatasetError as exc:
        print('数据集错误: %s' % exc)
        return 2
    errors, warnings = validate(samples)

    if args.dry_run:
        return dry_run(samples, errors, warnings, args.dataset)

    if errors:
        for e in errors:
            print('ERROR %s' % e)
        print('数据集不合法，先修数据集（可跑 --dry-run 看全量）')
        return 2

    if not args.predictor:
        print('未提供 --predictor：只打印数据集统计（等价 --dry-run）')
        return dry_run(samples, errors, warnings, args.dataset)

    if args.ledger_replay:
        # 影子回放要读侧真的跑起来（`evaluate_claims` 在 flag=off 时一行都不走）。
        # 这里只改**本进程**的 flag，且只为了这档显式开启的读数；台账是内存对象。
        os.environ.setdefault('YINYUE_FEATURE_CLAIM_LEDGER', 'shadow')
        print('WARN  --ledger-replay：台账 = 本批语料按 canonical 写侧自建（**影子回放**，'
              '不是生产行；不读也不写生产库）')

    predictor = _load_predictor(args.predictor)   # ← 延迟导入，参数解析之后
    rows = score(samples, predictor, history_window=args.history_window,
                 limit_history=args.limit_history,
                 reply_budget_seconds=args.reply_budget_seconds,
                 ledger_replay=args.ledger_replay)
    stats = summarize(rows)
    print_report(stats, rows)
    if args.json:
        with open(args.json, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump({'stats': stats, 'rows': rows}, fh, ensure_ascii=False, indent=1)
        print('逐条结果已写入 %s' % args.json)
    verdicts = threshold_verdicts(stats)
    failed = [v for v in verdicts if v[5] == 'FAIL']
    if stats['errors']:
        print('=> 预测器有 %d 条抛异常，退出码 1' % stats['errors'])
        return 1
    if failed:
        print('=> 未达标: %s' % ', '.join('%s=%s' % (v[0], v[1]) for v in failed))
        return 1
    print('=> 七项指标全部达标（既有五项 PASS；新增两项门槛未定，按 N/A 记）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
