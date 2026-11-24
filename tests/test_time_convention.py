"""时间约定回归测试（2026-09-22 时区统一后）。

锁住「全项目统一本地墙钟 naive」这一约定，防止未来有人把 UTC 又混回来：
- 应用时钟 `temporal.now()` 必须是 naive 本地；
- `parse_datetime` 保留本地墙钟数字；
- 混合 naive/aware 输入也能安全比较（不再抛 TypeError）；
- recency 不因 8 小时时区错位而失真；
- **静态守卫**：换算只有一个 owner（`temporal.to_naive_local` /
  `temporal.parse_naive_local`），生产代码里不许再出现手写的
  `....astimezone(...).replace(tzinfo=None)`（G3，2026-10-01 收口 24 处复制件）。
"""

from __future__ import annotations

import ast
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent


def test_temporal_now_is_naive_local() -> None:
    from core import temporal

    assert temporal.now().tzinfo is None, "应用时钟必须是 naive 本地墙钟"


def test_parse_datetime_preserves_local_digits() -> None:
    from core import temporal

    dt = temporal.parse_datetime("2026-09-22 17:07:45")
    assert dt is not None
    assert dt.tzinfo is None
    assert (dt.hour, dt.minute, dt.second) == (17, 7, 45)


def test_aware_input_normalizes_to_naive_local() -> None:
    from core import temporal

    aware = datetime(2026, 9, 22, 9, 7, 45, tzinfo=timezone.utc)
    local = temporal.parse_datetime(aware)
    assert local is not None and local.tzinfo is None


def test_membership_period_compares_mixed_inputs() -> None:
    """naive 与 aware 混合输入不得抛 TypeError（旧 bug）。"""
    from core.terminal.principal import GroupMembershipPeriod

    period = GroupMembershipPeriod(
        start_at=datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc),
    )
    naive_when = datetime(2026, 9, 22, 12, 0)
    assert period.covers(naive_when) is True
    assert period.covers(datetime(2026, 10, 1, 0, 0)) is False


def test_recency_not_offset_by_8h() -> None:
    """45 秒前的事件 recency 应接近 1；若误当 UTC 会变成约 8 小时前的值。"""
    from core.people.visibility import _recency_score

    now = datetime(2026, 9, 22, 17, 7, 45)
    just_now = _recency_score(datetime(2026, 9, 22, 17, 7, 0), now=now)
    eight_h_ago = _recency_score(datetime(2026, 9, 22, 9, 7, 0), now=now)
    assert just_now > 0.99, just_now
    assert eight_h_ago < just_now


# --------------------------------------------------------------------- 公开入口
def test_public_owner_normalizes_to_naive_local() -> None:
    """owner 就是公开入口：aware → 本地墙钟，naive 原样，解析失败给 default。"""
    from core import temporal

    utc = datetime(2026, 9, 22, 9, 7, 45, tzinfo=timezone.utc)
    local = temporal.to_naive_local(utc)
    assert local.tzinfo is None
    # 独立口径：naive UTC 数字 + 本地偏移（不借 owner 内部那段表达式自证）。
    offset = utc.astimezone().utcoffset() or timedelta(0)
    assert local == utc.replace(tzinfo=None) + offset

    naive = datetime(2026, 9, 22, 9, 7, 45)
    assert temporal.to_naive_local(naive) == naive

    assert temporal.parse_naive_local("2026-09-22 09:07:45") == naive
    assert temporal.parse_naive_local("2026-09-22T09:07:45Z").tzinfo is None
    assert temporal.parse_naive_local("2026-09-22T09:07:45+00:00") == naive + offset
    assert temporal.parse_naive_local(None) is None
    assert temporal.parse_naive_local("") is None
    assert temporal.parse_naive_local("  ") is None
    fallback = datetime(2020, 1, 1)
    assert temporal.parse_naive_local("不是时间", fallback) == fallback


# --------------------------------------------------------------------- 静态守卫
#: 换算唯一 owner（`to_naive_local` / `parse_naive_local` 都在这里）。
_TIMEZONE_OWNER = "core/temporal.py"
#: 生产代码扫描根。tests/ 不在内：测试里手写换算当**独立对照口径**（自己算一遍
#: 预期值）是刻意的，不是漂移来源；tests/golden 之类的数据文件更不是。
_TIMEZONE_ROOTS = ("core", "db", "agents", "scripts")
#: 非源码目录（与 scripts/check_layering.py 的 NON_SOURCE_DIRS 同口径）。
_TIMEZONE_SKIP_DIRS = frozenset({"__pycache__", "build", "site-packages", "_scratch"})
#: 白名单：文件 -> {符号集合 + 理由}。豁免口径是**符号体**，不是整个文件。
#:
#: 为什么按文件归组、不一条一个符号：这些理由是文件级的产品解释（「这个脚本为什么
#: 必须显式 UTC 归一」），拆成三条会把同一段话复制三遍，然后就不再有任何人读它。
#:
#: 三条硬判据（与 tests/test_message_attribution.py 同一套写法）：
#: ① 登记的符号必须在**该文件里恰好定义一次**——撞名或找不到就红，并报出同名候选；
#: ② 每个符号**恰好 1 处**手写换算；符号体之外再冒出一处照样红；
#: ③ 双向 stale：登记的符号里不再有换算就红；文件里出现未登记的换算也红。
_TIMEZONE_ALLOWED: dict[str, dict[str, object]] = {
    'scripts/observe_rollout.py': {
        'symbols': ('parse_utc',),
        'reason': (
            '离线回放：显式 astimezone(timezone.utc) -> naive UTC（对齐 UTC 存储口径，'
            '不是本地墙钟换算；不能改调 to_naive_local，否则比较基准整体偏 8 小时）'
        ),
    },
    'scripts/replay_log_cases.py': {
        'symbols': ('parse_utc',),
        'reason': '同上：显式 UTC 归一，用于与日志里的 UTC 时间戳比对',
    },
    'scripts/summarize_turn_relevance_shadow.py': {
        'symbols': ('_parse_stamp',),
        'reason': (
            '影子汇总：JSONL/console 两路日志都按 UTC 行对齐（console 也是 UTC ISO/Z），'
            '显式 UTC 归一只为跨文件去重与算 span；不能改 to_naive_local（会把 Z 再转本地，'
            '与 console 行差 8 小时）'
        ),
    },
}

def _iter_timezone_sources():
    """扫描根里每个可解析的 .py：`(相对路径, AST)`。"""
    for root in _TIMEZONE_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            if _TIMEZONE_SKIP_DIRS & set(path.parts):
                continue
            rel = path.relative_to(ROOT).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except SyntaxError:
                continue
            yield rel, tree


def _enclosing_symbol(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """节点所在的最近一层函数/方法名（`Cls.method` / `func`）；模块级返回 ""。"""
    chain: list[str] = []
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chain.append(cur.name)
        elif isinstance(cur, ast.ClassDef):
            chain.append(cur.name)
        cur = parents.get(cur)
    return ".".join(reversed(chain))


def _timezone_conversion_sites() -> list[tuple[str, int, str, str]]:
    """生产代码里手写的时区换算：``X.astimezone(...).replace(tzinfo=None)``。

    返回 ``(文件, 行号, 形态, 所在符号)``；符号是空串表示模块级（不在任何 def 里）。
    """
    found: list[tuple[str, int, str, str]] = []
    for rel, tree in _iter_timezone_sources():
        parents = {
            child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "replace"):
                continue
            if not any(
                kw.arg == "tzinfo"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is None
                for kw in node.keywords
            ):
                continue
            inner = func.value
            if not (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "astimezone"
            ):
                continue
            kind = "astimezone()" if not inner.args else "astimezone(<tz>)"
            found.append((rel, node.lineno, kind, _enclosing_symbol(node, parents)))
    return found


def _defined_names(tree: ast.AST) -> dict[str, int]:
    '''文件里每个符号定义了几次（模块级赋值名 / 函数 / 类 / 类的方法，含嵌套限定名）。

    只收**定义出来就有名字**的节点：函数里的局部赋值不是符号（那是实现细节），
    收进来只会让「同名」判定变吵，然后有人会去放宽判据。
    '''
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    counts: dict[str, int] = {}

    def bump(name: str) -> None:
        counts[name] = counts.get(name, 0) + 1

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            outer = _enclosing_symbol(node, parents)
            bump(f'{outer}.{node.name}' if outer else node.name)
        elif isinstance(node, ast.Assign):
            if _enclosing_symbol(node, parents):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bump(target.id)
        elif isinstance(node, ast.AnnAssign):
            if not _enclosing_symbol(node, parents) and isinstance(node.target, ast.Name):
                bump(node.target.id)
    return counts


def _symbol_definitions() -> tuple[dict[str, dict[str, int]], set[str]]:
    '''每个文件里的符号定义计数，以及扫到的文件集合（文件消失要能红）。'''
    defined: dict[str, dict[str, int]] = {}
    files: set[str] = set()
    for rel, tree in _iter_timezone_sources():
        files.add(rel)
        defined[rel] = _defined_names(tree)
    return defined, files

def _judge_whitelist(
    sites: list[tuple[str, int, str, str]],
    defined: dict[str, dict[str, int]],
    files: set[str],
    table: dict[str, dict[str, object]],
) -> list[str]:
    '''白名单的四向对账：文件在 / 符号恰好定义一次 / 恰好 1 处换算 / 失效与漂移。

    纯函数，喂合成数据也能跑——守卫的判据不该只有真树一条路（真树只有一个当下状态，
    而判据要管的是将来）。
    '''
    covered: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for rel, line, kind, symbol in sites:
        covered.setdefault((rel, symbol), []).append((line, kind))

    problems: list[str] = []
    for rel in sorted(table):
        entry = table[rel] or {}
        symbols = tuple(entry.get('symbols') or ())
        if not symbols:
            problems.append(f'{rel}：没有登记任何符号（空条目该删）')
        if rel not in files:
            problems.append(f'{rel}：文件不存在了（条目该删）')
            continue
        names = defined.get(rel) or {}
        for symbol in symbols:
            count = names.get(symbol, 0)
            if count == 0:
                leaf = symbol.split('.')[-1]
                near = sorted(
                    name for name in names if name != symbol and (leaf in name.split('.')[-1] or name.split('.')[-1] in leaf)
                )
                joined = '、'.join(near[:3])
                hint = f'（同名的：{joined}）' if near else ''
                problems.append(f'{rel}：这个文件里没有 {symbol}{hint}')
                continue
            if count > 1:
                problems.append(f'{rel}：{symbol} 在这个文件里定义了 {count} 次，说清是哪一个')
                continue
            hits = covered.get((rel, symbol)) or []
            if not hits:
                problems.append(
                    f'{rel}::{symbol}：条目已失效（这个符号里不再有手写换算），'
                    '删掉它免得白名单越滚越大'
                )
            elif len(hits) != 1:
                problems.append(
                    f'{rel}::{symbol}：登记 1 处，实际 {len(hits)} 处 {hits}——多出来的那处'
                    '要么改调 to_naive_local，要么把期望数改高（别把豁免放宽回整个文件）'
                )
    return problems


def _unregistered(
    sites: list[tuple[str, int, str, str]], table: dict[str, dict[str, object]]
) -> list[tuple[str, int, str, str]]:
    '''没登记的换算点（owner 自己那处实现不算）。'''
    allowed = {
        (rel, str(symbol))
        for rel, entry in table.items()
        for symbol in (entry or {}).get('symbols') or ()
    }
    return [
        (rel, line, kind, symbol or '<模块级>')
        for rel, line, kind, symbol in sites
        if rel != _TIMEZONE_OWNER and (rel, symbol) not in allowed
    ]

def test_guard_judgment_on_synthetic_sites() -> None:
    '''守卫的判据本身也要被守：喂合成数据，六种该红的情形各自都要红。

    这条替代「改一次真代码看它红不红」——真树只有一个当下状态，而判据要管的是将来。
    '''
    rel = 'scripts/demo_a.py'
    table = {rel: {'symbols': ('parse_utc',), 'reason': '合成理由'}}
    defined = {rel: {'parse_utc': 1}}
    files = {rel}
    good = (rel, 20, 'astimezone(<tz>)', 'parse_utc')

    clean = _judge_whitelist([good], defined, files, table)
    assert clean == [], clean
    assert _unregistered([good], table) == []

    # ① 文件整条消失 -> 红
    problems = _judge_whitelist([good], defined, set(), table)
    assert problems and '文件不存在' in problems[0], problems

    # ② 符号不在这个文件里 -> 红，并报出同名候选（别只说找不到）
    problems = _judge_whitelist([good], {rel: {'parse_utc_eq': 1}}, files, table)
    assert problems and '没有 parse_utc' in problems[0], problems
    assert 'parse_utc_eq' in problems[0], problems

    # ③ 符号在这个文件里定义了两次 -> 红（挡「符号写错但恰好有别的同名符号」）
    problems = _judge_whitelist([good], {rel: {'parse_utc': 2}}, files, table)
    assert problems and '定义了 2 次' in problems[0], problems

    # ④ 登记的符号里不再有换算 -> stale 红
    problems = _judge_whitelist([], defined, files, table)
    assert problems and '条目已失效' in problems[0], problems

    # ⑤ 同一个符号体里出现第二处 -> 红（登记 1 处就得是 1 处）
    twice = (rel, 44, 'astimezone()', 'parse_utc')
    problems = _judge_whitelist([good, twice], defined, files, table)
    assert problems and '登记 1 处，实际 2 处' in problems[0], problems

    # ⑥ 符号体之外（模块级）再冒一处 -> 未登记即红
    outside = (rel, 40, 'astimezone()', '')
    assert _unregistered([good, outside], table) == [(rel, 40, 'astimezone()', '<模块级>')]

    # ⑦ owner 自己那处实现不算未登记
    owner_site = (_TIMEZONE_OWNER, 10, 'astimezone()', 'to_naive_local')
    assert _unregistered([owner_site], table) == []


def test_whitelist_entries_name_one_defined_symbol() -> None:
    '''真树上的四向对账：文件在 / 符号恰好定义一次 / 恰好 1 处换算 / 理由非空。'''
    sites = _timezone_conversion_sites()
    defined, files = _symbol_definitions()
    problems = _judge_whitelist(sites, defined, files, _TIMEZONE_ALLOWED)
    assert not problems, (
        '时区白名单必须逐条指向「存在的文件里恰好定义一次」的符号，且每个符号恰好 1 处手写'
        '换算：\n  - ' + '\n  - '.join(problems) +
        '\n修法：符号改名就跟上；一个文件里有多处就写明是哪一处；这个符号已经改调 '
        'to_naive_local 了就把条目删掉（错的行号/符号名比没有白名单更糟）。'
    )
    assert all(
        str(entry.get('reason') or '').strip() for entry in _TIMEZONE_ALLOWED.values()
    ), f'白名单必须逐条写明理由：{_TIMEZONE_ALLOWED}'


def test_no_module_converts_timezone_by_hand() -> None:
    '''静态守卫：手写的 astimezone(...).replace(tzinfo=None) 只许 owner 与白名单符号里有。'''
    sites = _timezone_conversion_sites()
    assert any(rel == _TIMEZONE_OWNER for rel, _, _, _ in sites), (
        f'{_TIMEZONE_OWNER} 里没有换算语句了？守卫会退化成空断言'
        '（owner 必须自己保留唯一那一处实现）'
    )
    unregistered = _unregistered(sites, _TIMEZONE_ALLOWED)
    assert not unregistered, (
        '生产代码里出现手写的时区换算（应收口到 '
        f'{_TIMEZONE_OWNER}.to_naive_local / parse_naive_local）：{unregistered}\n'
        '真需要保留（例如要给 UTC 口径对齐）就登进 _TIMEZONE_ALLOWED：键是该文件，'
        'symbols 里写该符号名，reason 写明为什么不能改调 to_naive_local。'
    )

def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
