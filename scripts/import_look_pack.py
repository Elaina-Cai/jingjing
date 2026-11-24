"""把一堆图建成一个形象包（开源用户自助建档）。

七步：收图 -> 去重 -> 抽槽位 -> 求共识 -> 算权重 -> 自检离群 -> 生成 canon。

用法::

    python scripts/import_look_pack.py --slug hua --name 小花 --from D:/pics/hua
    python scripts/import_look_pack.py --slug hua --name 小花 --from a.jpg b.jpg --dry-run
    python scripts/import_look_pack.py --slug hua --name 小花 --from D:/pics --no-vision

纪律（和 P1/P2 一脉相承）：

1. 不猜：视觉模型只回答这一张里看得见什么，不回答这是谁。
2. 不覆盖：目标目录已有 pack.json 时，除非加 --force，否则直接拒绝。
3. 永不抛：单张图失败只记进报告，最后一起打印。

为什么不做 embedding：通用 CLIP 是自然图训练的，二次元角色挤在一起，
正好把别的蓝发角色认成她。符号槽位（发色/耳朵/尾巴/服装）可解释、可手改。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

IMAGE_SUFFIXES = ('.jpg', '.jpeg', '.png', '.webp', '.bmp')
SLOT_ORDER = ('hair_color', 'hair_len', 'ahoge', 'fins', 'tail', 'eyes', 'outfit', 'mascot')
IDENTITY_SLOTS = ('fins', 'tail', 'mascot')

_SLOT_ASK = {
    'hair_color': '发色（蓝色系？偏深还是偏浅？有没有渐变）',
    'hair_len': '头发多长（及腰/超长/短发）',
    'ahoge': '头顶有没有一根细长弯钩的呆毛',
    'fins': '头两侧有没有像鲸鱼鳍/鱼鳍一样的耳朵（蓝底白边一类）',
    'tail': '身后有没有一条鲸鱼或鱼类的尾巴（宽大、会分叉）',
    'eyes': '眼睛什么颜色',
    'outfit': '穿的什么（女仆装？围裙？什么颜色？头饰或蝴蝶结？）',
    'mascot': '衣服或配件上有没有小鲸鱼、小鱼或 logo 图案',
}

_PROMPT = (
    '只看这一张图，回答「画里这个人（如果画面里有人）身上看得见哪些特征」。'
    '只描述看得见的，看不出来就不要写；绝对不要猜她是谁，也不要猜作品名。'
    '严格输出一行 JSON，不要代码块、不要解释：'
    '{"slots": {' + ','.join('"%s": ["词", "词"]' % k for k in SLOT_ORDER) +
    '}, "style": "normal|chibi|meme", "gist": "一句不超过 20 字的话"}'
    '\n各槽位要回答的问题：' + '；'.join('%s=%s' % (k, _SLOT_ASK[k]) for k in SLOT_ORDER)
)


def _collect(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        item = Path(raw).expanduser()
        if item.is_dir():
            for child in sorted(item.rglob('*')):
                if child.is_file() and child.suffix.lower() in IMAGE_SUFFIXES:
                    out.append(child)
        elif item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES:
            out.append(item)
    return out


def _dedupe(paths: list[Path]) -> tuple[list[Path], list[dict]]:
    """md5 相同算同一张；dhash 距离 <=2 也算（转存/压缩/加水印）。"""
    from core.identity import self_look as sl

    kept: list[Path] = []
    seen: list[tuple[str, str]] = []
    dropped: list[dict] = []
    for item in paths:
        md5 = sl.md5_of(str(item))
        dh = sl.dhash(str(item))
        why = ''
        if md5:
            for other_md5, _other_dh in seen:
                if other_md5 and other_md5 == md5:
                    why = 'md5'
                    break
        if not why and dh:
            for _other_md5, other_dh in seen:
                if other_dh and sl.ham(dh, other_dh) <= 2:
                    why = 'dhash'
                    break
        if why:
            dropped.append({'file': item.name, 'why': 'dup_' + why})
            continue
        kept.append(item)
        seen.append((md5, dh))
    return kept, dropped


def _extract(path: Path, *, budget_s: float = 60.0) -> dict:
    """让视觉模型把槽位词抽出来（只抽描述，不判身份）。"""
    from core.conversation.media import _ask_vision_sync

    text = _ask_vision_sync(str(path), _PROMPT, budget_s)
    if not text:
        return {'slots': {}, 'style': 'normal', 'gist': '', 'error': 'no_text'}
    body = text.strip()
    if body.startswith('```'):
        body = re.sub(r'^```[a-zA-Z]*\s*|```$', '', body).strip()
    hit = re.search(r'\{.*\}', body, re.S)
    if not hit:
        return {'slots': {}, 'style': 'normal', 'gist': '', 'error': 'no_json'}
    try:
        data = json.loads(hit.group(0))
    except Exception as exc:  # noqa: BLE001
        return {'slots': {}, 'style': 'normal', 'gist': '', 'error': 'bad_json:' + repr(exc)[:80]}
    slots_raw = data.get('slots') if isinstance(data.get('slots'), dict) else {}
    slots: dict[str, list[str]] = {}
    for key, val in slots_raw.items():
        if key not in SLOT_ORDER:
            continue
        terms = val if isinstance(val, list) else [val]
        clean = [str(t).strip() for t in terms if str(t).strip()]
        if clean:
            slots[key] = clean[:4]
    return {
        'slots': slots,
        'style': str(data.get('style') or 'normal')[:12],
        'gist': str(data.get('gist') or '')[:40],
        'error': '',
    }


def _consensus(extracts: list[dict], min_freq: float) -> dict[str, dict]:
    """一个槽位要在 >=60% 的图里出现，才算这个角色的特征。"""
    total = max(1, len([e for e in extracts if not e.get('error')]))
    out: dict[str, dict] = {}
    for slot in SLOT_ORDER:
        counter: dict[str, int] = {}
        hit = 0
        for item in extracts:
            terms = (item.get('slots') or {}).get(slot) or []
            if terms:
                hit += 1
            for term in terms:
                counter[term] = counter.get(term, 0) + 1
        if not hit:
            continue
        freq = hit / total
        if freq < min_freq:
            continue
        ranked = sorted(counter.items(), key=lambda kv: (-kv[1], len(kv[0])))
        out[slot] = {
            'freq': round(freq, 3),
            'terms': [t for t, _n in ranked[:6]],
            'weight': max(1, min(3, round(freq * 3))),
            'identity': slot in IDENTITY_SLOTS and freq >= 0.8,
        }
    return out


def _canon(slots: dict[str, dict], name: str) -> str:
    """第一人称的自我描述。允许手改——canon.txt 才是 [我的样子] 的来源。"""
    def top(slot: str) -> str:
        info = slots.get(slot) or {}
        return '、'.join((info.get('terms') or [])[:3])

    parts: list[str] = []
    hair = ''.join(x for x in (top('hair_color'), top('hair_len')) if x)
    if hair:
        parts.append('我有' + hair + '。')
    if top('ahoge'):
        parts.append('头顶有一根' + top('ahoge') + '。')
    if top('eyes'):
        parts.append('眼睛是' + top('eyes') + '。')
    if top('fins'):
        parts.append('头两侧是' + top('fins') + '。')
    if top('tail'):
        parts.append('身后有' + top('tail') + '。')
    if top('outfit'):
        parts.append('常穿' + top('outfit') + '。')
    if top('mascot'):
        parts.append('身上还有' + top('mascot') + '。')
    if not parts:
        return '（还没有共识特征：图太少或太杂，先多收几张正面/全身图再跑一次。）\n'
    head = ('我是%s。' % name) if name else ''
    return head + ''.join(parts) + '\n'


def _outliers(kept: list[Path], extracts: list[dict]) -> list[dict]:
    """自检：命中槽位明显少于中位数的图，多半是混进来的别的角色或截图。"""
    counts = [len(item.get('slots') or {}) for item in extracts]
    if not counts:
        return []
    ordered = sorted(counts)
    median = ordered[len(ordered) // 2]
    if median <= 1:
        return []
    out = []
    for path, item in zip(kept, extracts):
        n = len(item.get('slots') or {})
        if n <= max(0, median // 2):
            out.append({'file': path.name, 'slots_hit': n, 'median': median})
    return out


def build(
    *,
    slug: str,
    name: str = '',
    sources: list[str],
    vision: bool = True,
    dry_run: bool = False,
    force: bool = False,
    min_freq: float = 0.6,
) -> dict:
    clean_slug = re.sub(r'[^0-9A-Za-z_\-]', '', str(slug or ''))
    if not clean_slug:
        return {'ok': False, 'why': 'slug 不合法（只允许字母、数字、下划线、连字符）'}
    root_dir = ROOT / 'data' / 'persona' / 'assets' / clean_slug / 'faces'
    if (root_dir / 'pack.json').exists() and not force:
        return {'ok': False, 'why': '已经存在 %s ；要重建请加 --force' % (root_dir / 'pack.json')}

    files = _collect(sources)
    if not files:
        return {'ok': False, 'why': '没找到图片（支持 jpg / jpeg / png / webp / bmp）'}
    kept, dropped = _dedupe(files)
    if not kept:
        return {'ok': False, 'why': '全部被去重掉了', 'dropped': dropped}

    extracts: list[dict] = []
    for path in kept:
        if vision:
            extracts.append(_extract(path))
        else:
            extracts.append({'slots': {}, 'style': 'normal', 'gist': '', 'error': 'skip_vision'})

    slots = _consensus(extracts, min_freq) if vision else {}
    outliers = _outliers(kept, extracts) if vision else []
    plan = {
        'ok': True,
        'slug': clean_slug,
        'name': name,
        'dir': str(root_dir),
        'images': len(kept),
        'dropped_dups': dropped,
        'outliers': outliers,
        'slots': {k: {'terms': v['terms'], 'weight': v['weight'], 'identity': v['identity']}
                  for k, v in slots.items()},
        'dry_run': bool(dry_run),
    }
    if dry_run:
        plan['canon_preview'] = _canon(slots, name)
        return plan

    gallery = root_dir / 'gallery'
    gallery.mkdir(parents=True, exist_ok=True)
    from core.identity import self_look as sl

    index_images = []
    for i, path in enumerate(kept, start=1):
        style = (extracts[i - 1].get('style') or 'normal') if extracts else 'normal'
        stem = re.sub(r'[^0-9A-Za-z_\-]', '', path.stem)[:30] or 'img'
        target = gallery / ('%02d_%s_%s%s' % (i, style, stem, path.suffix.lower()))
        shutil.copy2(path, target)
        index_images.append({
            'file': target.name,
            'md5': sl.md5_of(str(target)),
            'dhash': sl.dhash(str(target)),
            'style': style,
            'gist': (extracts[i - 1].get('gist') or '') if extracts else '',
            'note': '',
        })

    pack = {
        'kind': 'faces',
        'slug': clean_slug,
        'name': name or clean_slug,
        'version': 1,
        'built_by': 'scripts/import_look_pack.py',
        'slots': {
            k: {
                'weight': v['weight'],
                'identity': v['identity'],
                'terms': v['terms'],
                'hint': _SLOT_ASK.get(k, ''),
            }
            for k, v in slots.items()
        },
        'distinctive_open_vocab': [],
        'hard_veto': [],
        'required_any': [k for k, v in slots.items() if v['identity']],
        'thresholds': {'text_self': 0.7, 'text_maybe': 0.45, 'text_scale': 6.0,
                       'dhash_max_distance': 6, 'color_max_distance': 24},
        'notes': [
            '自动生成：terms 来自视觉模型对每张图的属性抽取，按出现频率求共识。',
            'canon.txt 才是 [我的样子] 的来源，可以手改。',
            'hard_veto 建议手工补上肯定不是她的颜色或物种（例如白发、兽耳）。',
        ],
    }
    (root_dir / 'pack.json').write_text(json.dumps(pack, ensure_ascii=False, indent=2) + '\n',
                                       encoding='utf-8')
    (root_dir / 'index.json').write_text(json.dumps(
        {'slug': clean_slug, 'name': name or clean_slug,
         'count': len(index_images), 'images': index_images},
        ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (root_dir / 'canon.txt').write_text(_canon(slots, name or clean_slug), encoding='utf-8')
    plan['written'] = [str(root_dir / 'pack.json'), str(root_dir / 'index.json'),
                       str(root_dir / 'canon.txt')]
    plan['gallery'] = len(index_images)
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='把一堆图建成形象包（data/persona/assets/<slug>/faces）')
    ap.add_argument('--slug', required=True)
    ap.add_argument('--name', default='')
    ap.add_argument('--from', dest='sources', nargs='+', required=True,
                    help='图片目录，或若干图片路径')
    ap.add_argument('--no-vision', action='store_true', help='只建索引，不抽槽位（省钱/离线）')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--min-freq', type=float, default=0.6)
    args = ap.parse_args(argv)
    out = build(slug=args.slug, name=args.name, sources=list(args.sources),
                vision=not args.no_vision, dry_run=args.dry_run, force=args.force,
                min_freq=args.min_freq)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get('ok') else 2


if __name__ == '__main__':
    raise SystemExit(main())
