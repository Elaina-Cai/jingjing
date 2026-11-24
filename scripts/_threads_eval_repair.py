"""一次性（可重跑）量具修复脚本：给评测集回填规范 person_id + 修代号撞车。

设计约束
--------
* 只读真库（``account.db`` / ``yinyue_meta.db`` 以 ``mode=ro`` 打开）；
* 幂等：已经带 ``person_ids`` 的行**原样跳过**（重复执行不会叠加注记）；
* 不改文件名/行序/id；不改 ``label.thread_id`` / ``should_switch`` /
  ``expected_behavior`` / ``tags`` / ``label_source``；
* 只在 ``label.anchor`` / ``label.addressee`` / ``speaker`` 三处修**已证实**的
  代号撞车，并在 ``notes`` 里留下"旧值 / 新值 / 依据"。

用法::

    .venv\\Scripts\\python.exe scripts/_threads_eval_repair.py --check   # 只报告
    .venv\\Scripts\\python.exe scripts/_threads_eval_repair.py --write   # 落盘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.path.join(ROOT, 'tests', 'golden', 'threads', 'eval_set.jsonl')
NAME_MAP = os.path.join(ROOT, 'data', 'eval', 'threads', 'name_map.json')
ACCOUNT_DB = os.path.join(ROOT, 'data', 'platforms', 'wechat',
                          'wxid_seed02', 'account.db')
CHAT = '10000000001@chatroom'

PERSON_ID_PREFIX = 'pn:'
CODE_RE = re.compile(r'^P\d+$')
CODE_FIND_RE = re.compile(r'P\d+')   # 同一条代号形状，用于 findall（不能带 ^$ 锚）

#: 撞车行的**逐行**改动（旧值→新值）+ 依据。``speaker`` 为 ``None`` = 不动。
#: 依据必备字段：``db``（真库原文，逐字）、``author``（真库 sender 的规范身份）、
#: ``why``（规格意图：自述/自指 → 主角=说话人；@/引用 → addressee）。
EDITS = {
    't0007': {
        'anchor': 'P7', 'addressee': ['P7'], 'speaker': None,
        'why': '规格意图：bot @P7 且在说"P7 能不能来上课" → 主角=P7、收件人=P7。'
               '旧值 P2 与正文里的 @P7 自相矛盾（正文没有任何 P2）。',
    },
    't0008': {
        'anchor': 'P2', 'addressee': ['P2'], 'speaker': None,
        'preserved': True,
        'why': '真值本来就对（被拍者=Elaina=P2）：正文 `"P7" 拍了拍 "P2"` 逐字等于真库；'
               '拍一拍作者按 canonical `pat_sender()`＝引号里"拍了拍"**之前**那个名字=P7，'
               '与真库 sender_id=wxid_seed06(P7) 一致 → 说话人不用改。'
               '**原样保留**，此注记只为留档复核结论。',
    },
    't0009': {
        'anchor': 'P7', 'addressee': ['P7'], 'speaker': None,
        'why': '规格意图：bot @P7，被问的是 P7 的问题、回应的是 P7 → 主角=P7、收件人=P7。',
    },
    't0011': {
        'anchor': 'P7', 'addressee': ['P7'], 'speaker': None,
        'why': '规格意图：bot @P7 拒绝"代上课"（P7 的请求）→ 主角=P7、收件人=P7。',
    },
    't0056': {
        'anchor': 'P7', 'addressee': ['P7'], 'speaker': None,
        'why': '引用作者=P7（真库 `[引用 P6: …]`）→ §4.2① 主角=P7；'
               '正文"你也要走的说啥"质问的就是 P7 → 收件人=P7。原 notes 自己写的就是'
               '"anchor=P7"，label 里却填了 P2（撞车）。',
    },
    't0057': {
        'anchor': 'P7', 'addressee': None, 'speaker': None,
        'why': '簇 B2 = P7 的离场簇（真库 sender=P6=P7），按簇继承主角=P7。'
               'addressee=["room"] 保持（对全群道别）。',
    },
    't0060': {
        'anchor': 'P7', 'addressee': None, 'speaker': None,
        'why': '簇 B2 = P7 的离场簇（真库 sender=P6=P7）的纯表情包，归线不改主角=P7。',
    },
    't0090': {
        'anchor': 'P7', 'addressee': ['P7'], 'speaker': None,
        'why': '**同一缺陷的漏网行**（两轮盲标清单外，本次全域扫描发现）：'
               '正文前缀 "@P7" 与 t0007/t0009/t0011 完全同形（bot @P7），'
               'addressee 却写 P2 → 同一条缺陷，同一处置。',
    },
    't0095': {
        'anchor': 'P7', 'addressee': ['P4', 'P2', 'P5', 'P3', 'P6'], 'speaker': None,
        'why': '规格意图：句首"我是浙江的"=第一人称自述 → 主角=说话人=P7（不是 P2）；'
               '正文同时 @ 了 5 人（§3.1 强边可多条）→ addressee 记全 5 人。'
               '旧 addressee 只有 1 个元素 = 被压扁（规格允许 0..n）。',
    },
    't0098': {
        'anchor': 'P7', 'addressee': ['P1'], 'speaker': None,
        'why': '规格意图：引用块作者=P7（自引用）+ 句首自述 → 主角=说话人=P7；'
               '引用块之后新写 "@P1 这个是对的" → addressee=["P1"]（原本就对）。',
    },
}

#: 正文与真库逐字一致、需在注记里显式备案核验方式的行。
TEXT_VERIFY_IDS = ('t0095', 't0098')

AUDIT_MARK = '【量具修复 2026-10-06】'


def load_name_map():
    with open(NAME_MAP, encoding='utf-8') as fh:
        data = json.load(fh)
    code_to_wxid = {}
    for code, info in data['codes'].items():
        code_to_wxid[code] = info.get('wxid')
    for code, info in data['third_party_codes'].items():
        code_to_wxid[code] = info.get('wxid')
    return data, code_to_wxid


def person_id_of(code):
    """代号 → 规范身份 id。``pn:<CODE>``；代号在 name_map 里查得到才算合法。

    规范身份的**权威**是 ``data/eval/threads/name_map.json``（代号↔昵称↔wxid，
    不入库）。本文件里的 ``person_id`` 就是它的一层薄引用：**不再依赖代号命名
    空间的"哪个 P 是谁"这种口头约定**，而是显式指过去。
    """
    if not code or not CODE_RE.match(str(code)):
        return ''
    return PERSON_ID_PREFIX + str(code)


def norm_text(value):
    """正文归一：``\\u2005`` 与 ``\\r`` → 空格/去回车（与 README §6.3 同口径）。"""
    return str(value or '').replace('\u2005', ' ').replace('\r\n', '\n').replace('\r', '\n')


def desensitize(value, name_to_code):
    """canonical 脱敏（README §6）：真名→代号，长名优先，``\\u2005``→普通空格。

    与 README 记录的那套（也是当年生成 eval_set 的那套）逐字同口径：正文级替换，
    不做别的加工。
    """
    out = norm_text(value)
    for name in sorted(name_to_code, key=len, reverse=True):
        out = out.replace(name, name_to_code[name])
    return out


def digest(value):
    return hashlib.sha256(norm_text(value).encode('utf-8')).hexdigest()[:12]


def read_rows(path):
    with open(path, 'rb') as fh:
        raw = fh.read()
    if b'\r\n' in raw:
        raise SystemExit('%s 含 CRLF，要求 LF' % path)
    lines = raw.decode('utf-8').splitlines()
    if not raw.endswith(b'\n'):
        raise SystemExit('%s 应以换行结尾' % path)
    return [json.loads(line) for line in lines]


def db_truth(con, msg_id):
    rows = list(con.execute(
        'SELECT sender_name, content, created_at FROM messages WHERE msg_id=?',
        (str(msg_id),)))
    return rows[0] if rows else None


def backfill_people(sample, code_to_wxid):
    """回填 person_id / label.*_person_id / person_ids；返回"是否真的写了新值"。

    **按值判等**（不看有没有这个键）：重复执行不会改动任何字节，也不会重复追加注记。
    """
    label = sample['label']
    speaker = sample['speaker']
    # `unknown` 不是人，但**也要有一个非空的规范身份**：撤回/系统消息没有归属，
    # 记成 `'unknown'`（与 speaker 同一枚代号，可解析、不是空串）。
    speaker_id = 'unknown' if speaker == 'unknown' else person_id_of(speaker)
    anchor = label.get('anchor')
    anchor_id = person_id_of(anchor) if anchor else ''
    addressee = list(label.get('addressee') or [])
    addressee_ids = []
    for item in addressee:
        if item == 'room':
            addressee_ids.append('room')
        else:
            addressee_ids.append(person_id_of(item))
    mapping = {}
    for code in [speaker, anchor] + [a for a in addressee if a != 'room']:
        if code and code != 'unknown' and CODE_RE.match(str(code)) and code not in mapping:
            pid = person_id_of(code)
            if not pid:
                raise SystemExit('代号 %r 解析不出规范身份' % code)
            mapping[code] = pid
    unknown = [c for c, p in mapping.items() if c not in code_to_wxid]
    if unknown:
        raise SystemExit('这些代号不在 name_map 里: %s' % unknown)
    changed = (sample.get('person_id') != speaker_id
               or sample.get('person_ids') != mapping
               or label.get('anchor_person_id') != anchor_id
               or label.get('addressee_person_ids') != addressee_ids)
    sample['person_id'] = speaker_id
    sample['person_ids'] = mapping
    label['anchor_person_id'] = anchor_id
    label['addressee_person_ids'] = addressee_ids
    return changed


def audit_line(sample_id, old, new, why, db_line, author, fp):
    return ('%s %s：旧值 %s → 新值 %s；依据=%s；真库原文 %s（作者=%s，sha256=%s）。'
            % (AUDIT_MARK, sample_id, old, new, why, db_line, author, fp))


def main(argv=None):
    ap = argparse.ArgumentParser(description='评测集量具修复（回填 person_id + 修撞车）')
    ap.add_argument('--write', action='store_true', help='真正落盘（默认只报告）')
    ap.add_argument('--check', action='store_true', help='只报告差异')
    args = ap.parse_args(argv)

    name_map, code_to_wxid = load_name_map()
    name_to_code = {}
    for code, info in list(name_map['codes'].items()) + \
            list(name_map['third_party_codes'].items()):
        for name in [info['real_name']] + list(info.get('aliases') or []):
            name_to_code[name] = code

    con = sqlite3.connect('file:%s?mode=ro' % ACCOUNT_DB.replace('\\', '/'), uri=True)
    samples = read_rows(DATASET)
    changed, untouched = [], []
    for sample in samples:
        sid = sample['id']
        touched = False
        truth = db_truth(con, sample['refs']['message_id'])
        if truth is None:
            raise SystemExit('%s 在真库里找不到（msg_id=%s）' % (sid, sample['refs']['message_id']))
        sender_name, content, created = truth
        author_code = name_to_code.get(sender_name) or ''
        fp = digest(content)

        edit = EDITS.get(sid)
        if edit:
            old = {'anchor': sample['label'].get('anchor'),
                   'addressee': list(sample['label'].get('addressee') or []),
                   'speaker': sample['speaker']}
            if edit['anchor'] != old['anchor']:
                sample['label']['anchor'] = edit['anchor']
            if edit['addressee'] is not None and edit['addressee'] != old['addressee']:
                sample['label']['addressee'] = list(edit['addressee'])
            if edit['speaker'] is not None and edit['speaker'] != old['speaker']:
                sample['speaker'] = edit['speaker']
            new = {'anchor': sample['label'].get('anchor'),
                   'addressee': list(sample['label'].get('addressee') or []),
                   'speaker': sample['speaker']}
            if old != new or (edit.get('preserved') and AUDIT_MARK not in sample['notes']):
                # 注记只在**真的改了值**时追加一次（按值判等 → 重跑不叠加）；
                # `preserved=True` 的行值不变，但复核结论（"原样保留"）必须留档，
                # 故以 AUDIT_MARK 是否已在 notes 里判重，保证重跑不多写一行。
                sample['notes'] = '%s\n%s' % (sample['notes'], audit_line(
                    '原样保留' if edit.get('preserved') else '代号撞车修复',
                    old, new, edit['why'], repr(content), author_code, fp))
                if old != new:
                    touched = True
            if sid in TEXT_VERIFY_IDS:
                redacted = desensitize(content, name_to_code)
                same = norm_text(sample['text']) == redacted
                block = {
                    'vs': 'db',
                    'table': 'messages',
                    'created_at': created,
                    'db_sha256_12': fp,
                    'redacted_sha256_12': digest(redacted),
                    'match': bool(same),
                    'method': 'canonical_desensitize(name_map v1, \\u2005→空格)',
                    'note': '正文逐字回填自真库；旧文本另存于 notes（旧值注记）',
                }
                if sample.get('text_verified') != block:
                    sample['text_verified'] = block
                    touched = True
                if not same:
                    print('WARN %s 正文与真库脱敏结果不一致，需人工裁决' % sid)

        if backfill_people(sample, code_to_wxid):
            touched = True
        if touched:
            changed.append(sid)
        else:
            untouched.append(sid)

        # 撞车指纹自检：改完之后，标签里的代号必须都能在正文或说话人里找到。
        # 只看本次改过的行——其余行的 anchor/addressee 允许是"继承来的簇主角"
        # （规格允许 label 里的人不在本条正文里），那不是撞车。
        if sid not in EDITS:
            continue
        text_codes = set(CODE_FIND_RE.findall(str(sample['text'])))
        label = sample['label']
        for item in list(label.get('addressee') or []) + [label.get('anchor')]:
            if not item or item == 'room':
                continue
            if item not in text_codes and item != sample['speaker']:
                print('WARN %s 标签仍指向正文/说话人里没有的代号 %s' % (sid, item))
    con.close()

    body = ''.join(json.dumps(s, ensure_ascii=False) + '\n' for s in samples)
    if args.write:
        with open(DATASET, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(body)
        print('已写入 %s（%d 行）' % (DATASET, len(samples)))
    else:
        print('（--check 模式，未落盘）')
    print('  本次真的改了: %d 行 → %s' % (len(changed), ', '.join(changed)))
    print('  已是目标状态（未改）: %d 行' % len(untouched))
    return 0


if __name__ == '__main__':
    sys.exit(main())
