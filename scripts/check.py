# -*- coding: utf-8 -*-
# 改完必跑的四件事，一条命令。
#
#   .venv\Scripts\python.exe scripts\check.py all
#   .venv\Scripts\python.exe scripts\check.py events --update-baseline
#
# 为什么要它：2026-10-26 一天改坏四个文件、日志里 478 条假异常、36 处开关各抄一遍——
# 全是"没人拦"导致的。这四检就是那道拦：语法、事件名、开关写法、JSON 是否原子落盘。
# 基线（scripts/check_baseline.json）是历史欠账的白名单，只允许变小，不允许变大。
from __future__ import annotations

import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGES = ("core", "loop", "agents", "db", "llm", "scheduler", "console")
BASELINE = os.path.join(ROOT, "scripts", "check_baseline.json")
SKIP_DIRS = {"__pycache__", ".git", "tests", "data", "output", "assets", "backups", "docs"}
LOG_FUNCS = {"warn", "warning", "error", "exception", "critical"}
# 事件名：小写下划线，允许点分层次（`direct_send.state_unreadable`）。
# 2026-11-09：原正则不容点号，与库内 1265/1380 条既有事件名相反，把合规的点分名判成新债。
EVENT_RE = re.compile(r"^[a-z][a-z0-9_]{2,}(\.[a-z0-9_]+)*$")
FLAG_RES = (
    re.compile(r"os\.(?:environ\.get|getenv)\([^)]*\)\s*==\s*[\"']1[\"']"),
    re.compile(r"os\.(?:environ\.get|getenv)\([^)]*\)\s*in\s*[\(\{][^\)\}]*[\"']true[\"']"),
    re.compile(r"^\s*return\s+\w+\s+in\s*[\(\{][^\)\}]*[\"']true[\"']"),
)


def py_files():
    for base in PACKAGES + ("scripts",):
        for dp, dn, fn in os.walk(os.path.join(ROOT, base)):
            dn[:] = [d for d in dn if d not in SKIP_DIRS and not d.startswith(".")]
            for f in fn:
                if f.endswith(".py"):
                    yield os.path.join(dp, f)


def rel(fp):
    return os.path.relpath(fp, ROOT).replace("\\", "/")


def read(fp):
    # 源码可能带 `# -*- coding: gbk -*-` 或 BOM，按 PEP 263 解码，别用 utf-8 硬解。
    try:
        import tokenize

        with tokenize.open(fp) as fh:
            return fh.read()
    except (OSError, SyntaxError, UnicodeDecodeError):
        try:
            return open(fp, encoding="utf-8", errors="replace").read()
        except OSError:
            return ""


def check_py():
    bad = []
    for fp in py_files():
        try:
            compile(read(fp), fp, "exec")
        except SyntaxError as exc:
            bad.append("%s:%s %s" % (rel(fp), exc.lineno, exc.msg))
    return bad


def _spans(tree):
    """函数跨度表：(首行, 末行, 名字)。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno, node.name))
    return out


def _owner(spans, lineno):
    hit = [s for s in spans if s[0] <= lineno <= s[1]]
    return max(hit, key=lambda s: s[0])[2] if hit else "<module>"


def mark(relpath, owner, shape):
    """违规身份：文件 + 所在函数 + 形态。故意不带行号——行号会让"上面插一行注释"把旧债变成新债。"""
    return "%s::%s::%s" % (relpath, owner, shape)


def _const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


_TRUTHY = ("1", "true", "yes", "on")


def _env_read(node):
    """os.environ.get(...) / os.getenv(...) / os.environ[...] → 键名，或 None。"""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
        key = _const_str(node.args[0])
        fn = node.func
        if key is None:
            return None
        if fn.attr == "get" and isinstance(fn.value, ast.Attribute) and fn.value.attr == "environ":
            if getattr(fn.value.value, "id", "") == "os":
                return key
        if fn.attr == "getenv" and getattr(fn.value, "id", "") == "os":
            return key
        return None
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
        if node.value.attr == "environ" and getattr(node.value.value, "id", "") == "os":
            return _const_str(node.slice)
    return None


def _truthy_shape(compare):
    """把开关当字符串/集合判真 → 形态字符串；不是这个形状返回 None。"""
    for op, right in zip(compare.ops, compare.comparators):
        if isinstance(op, ast.Eq):
            lit = _const_str(right)
            if lit is not None and lit.strip().lower() in _TRUTHY:
                return "eq:%s" % lit.strip()
        if isinstance(op, (ast.In, ast.NotIn)):
            for elt in getattr(right, "elts", None) or ():
                lit = _const_str(elt)
                if lit is not None and lit.strip().lower() in _TRUTHY:
                    return "in:%s" % lit.strip()
    return None


def _flag_offences(text, relpath):
    """开关裸判（拿 "1"/"true" 直接比）。按 AST 判：注释、字符串常量都不算；`noqa: flag` 放行。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [mark(relpath, "<parse>", "unparsable")]
    spans, body, bad = _spans(tree), text.splitlines(), []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not _env_read(node.left):
            continue
        shape = _truthy_shape(node)
        if not shape:
            continue
        line = body[node.lineno - 1] if node.lineno <= len(body) else ""
        if "noqa: flag" in line:
            continue
        bad.append(mark(relpath, _owner(spans, node.lineno), shape))
    return bad


_LOGGERS = ("log", "LOG", "logger", "LOGGER", "logging", "_log", "safelog", "_safelog", "self.log")


def _event_offences(text, relpath):
    """日志事件名（safelog 的首参）。常量首参照旧形状（基线里 1380 条按这个键登记），f-string 新判。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [mark(relpath, "<parse>", "unparsable")]
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute):
            # 只认"日志器"上的方法：argparse 的 parser.error(...) 不是日志（2026-10-29 实测误报）
            recv = fn.value
            owner = recv.id if isinstance(recv, ast.Name) else getattr(recv, "attr", "")
            if owner not in _LOGGERS:
                continue
            name = fn.attr
        else:
            name = getattr(fn, "id", "")
        if name not in LOG_FUNCS or not node.args:
            continue
        a0 = node.args[0]
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
            ev = a0.value
            if ".py:" in ev or " " in ev or not EVENT_RE.match(ev):
                bad.append("%s|%s" % (relpath, ev[:60]))
            continue
        if isinstance(a0, ast.JoinedStr):
            head = ""
            if a0.values and isinstance(a0.values[0], ast.Constant) and isinstance(a0.values[0].value, str):
                head = a0.values[0].value
            prefix = head.split(":", 1)[0]
            if ":" in head and EVENT_RE.match(prefix):
                continue
            bad.append("%s|fstring:%s" % (relpath, (prefix or head)[:40]))
    return bad


def check_events():
    bad = []
    for fp in py_files():
        bad.extend(_event_offences(read(fp), rel(fp)))
    return bad


def check_flags():
    skip = rel(os.path.join(ROOT, "core", "infra", "envflags.py"))
    bad = []
    for fp in py_files():
        r = rel(fp)
        if r == skip or r.startswith("scripts/"):
            continue
        bad.extend(_flag_offences(read(fp), r))
    return bad

def check_json():
    """裸 `json.dump`（非原子写）的位置。

    定位键是 `文件::函数名`，**不是行号**：行号会漂（插一行 import 就把 15 条
    历史欠账全变成“新增”），而函数名不会。这条规则存在的意义是“欠账只减不增”，
    不是“每次挪行都逼人刷基线”。
    """
    bad = []
    for fp in py_files():
        r = rel(fp)
        if r in ("core/infra/jsonio.py",):
            continue
        try:
            tree = ast.parse(read(fp))
        except SyntaxError:
            continue
        owner = {}

        def mark(node, name):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                owner.setdefault(id(sub), name)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                mark(node, node.name)
            elif isinstance(node, ast.ClassDef):
                mark(node, node.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "dump" and getattr(node.func.value, "id", "") == "json":
                    bad.append("%s::%s" % (r, owner.get(id(node), "<module>")))
    return bad


# ——— 2026-10-28 五修·锁门：业务代码不许裸读 YINYUE_* ———
BIZ_ROOTS = ("core", "loop", "agents", "console", "scheduler", "db", "llm")
RAW_ENV_RE = re.compile(r"os\.(?:environ\.get|getenv)\(\s*[\"']YINYUE_")


def _biz_files():
    for pkg in BIZ_ROOTS:
        for root, dirs, files in os.walk(os.path.join(ROOT, pkg)):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", ".venv_it")]
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(root, f)


def _envread_offences(text, relpath):
    """YINYUE_* 裸读。按 AST 判：注释、字符串常量都不算；`noqa: envread` 放行。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [mark(relpath, "<parse>", "unparsable")]
    spans, body, bad = _spans(tree), text.splitlines(), []
    for node in ast.walk(tree):
        key = _env_read(node)
        if not key or not key.startswith("YINYUE_"):
            continue
        line = body[node.lineno - 1] if node.lineno <= len(body) else ""
        if "noqa: envread" in line:
            continue
        bad.append(mark(relpath, _owner(spans, node.lineno), "read:" + key))
    return bad


def check_envread():
    """YINYUE_* 只许经 core/infra/envflags.py 读取（flag() 判开关 / text() 取值）。

    为什么：envflags 曾是"门做了、没锁"——12 处走 flag()，106 处照样裸读，
    所以门禁不认"有人在用"，只认"有没有第二处读口"。
    """
    skip = rel(os.path.join(ROOT, "core", "infra", "envflags.py"))
    bad = []
    for fp in _biz_files():
        r = rel(fp)
        if r == skip:
            continue
        bad.extend(_envread_offences(read(fp), r))
    return bad

# ——— 2026-10-28 六修·接线：用了别名却没 import 就是运行期 NameError ———
# 事故：批量把 os.environ.get 换成 _text(...)，12 个文件漏了 `text as _text` 的 import，
# py_compile 全过（编译期不查名字），一 import 就 NameError——引擎起不来。
_ALIASES = ("_text", "_flag")



def check_undef():
    """业务代码里用到 ``_text``/``_flag`` 却既没 import 也没定义 → 违规。"""
    bad = []
    for pkg in BIZ_ROOTS:
        for root, dirs, files in os.walk(os.path.join(ROOT, pkg)):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", ".venv_it")]
            for f in files:
                if not f.endswith(".py"):
                    continue
                fp = os.path.join(root, f)
                r = rel(fp)
                if r.endswith("core/infra/envflags.py"):
                    continue
                src = read(fp)
                try:
                    tree = ast.parse(src)
                except SyntaxError:
                    continue
                names = set()
                for n in ast.walk(tree):
                    if isinstance(n, ast.ImportFrom):
                        for a in n.names:
                            names.add(a.asname or a.name)
                    elif isinstance(n, ast.Import):
                        for a in n.names:
                            names.add((a.asname or a.name).split(".")[0])
                for n in tree.body:
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names.add(n.name)
                    elif isinstance(n, ast.Assign):
                        for tg in n.targets:
                            if isinstance(tg, ast.Name):
                                names.add(tg.id)
                for n in ast.walk(tree):
                    if (isinstance(n, ast.Name) and n.id in _ALIASES
                            and isinstance(n.ctx, ast.Load) and n.id not in names):
                        bad.append("%s:%d %s" % (r, n.lineno, n.id))
    return bad


def _hb_leaves():
    """心跳文件名从契约里取。

    规则自己抄一份文件名，就等于亲手造出第二个口径 —— 这条规则要防的正是这件事。
    """
    import sys as _sys
    if ROOT not in _sys.path:
        _sys.path.insert(0, ROOT)
    from core.infra import liveness as _liveness
    return tuple(_liveness._HEARTBEATS.values())


def check_hbpath():
    """心跳文件名只许出现在 `core/infra/liveness.py`。

    2026-10-28 的事故：引擎写一个心跳文件、监管器读另一个，面板和体检又各读一个
    —— 四个地方四个口径，谁都不算错，合起来就是没人看引擎。
    文件名是**契约**，只能有一个地方知道它（`core/infra/liveness.py` 的 `_HEARTBEATS`）。
    """
    leaves = _hb_leaves()
    bad = []
    for pkg in tuple(BIZ_ROOTS) + ("scripts",):
        base = os.path.join(ROOT, pkg)
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", ".venv_it")]
            for f in files:
                if not f.endswith(".py"):
                    continue
                fp = os.path.join(root, f)
                r = rel(fp)
                if r.endswith("core/infra/liveness.py") or r.endswith("scripts/check.py"):
                    continue  # 契约的定义处与规则的实现处，本来就该写着文件名
                for i, line in enumerate(read(fp).splitlines(), 1):
                    if any(k in line for k in leaves) and "noqa: hbpath" not in line:
                        bad.append("%s:%d %s" % (r, i, line.strip()[:80]))
    return bad


def check_cardfile():
    """人设卡必须符合 `loop.selfcard.CARD` 声明的契约（行尾/字节上限/头部三键/正文非空）。

    为什么在 check 里而不只在读的时候：卡是整块注入的稳定前缀，写坏了要么她读腰斩的卡、
    要么头部三行坏了导致声线/资产绑不上。改完必跑能抓住，而不是等她自己发现。
    """
    import sys as _sys
    if ROOT not in _sys.path:
        _sys.path.insert(0, ROOT)
    try:
        from loop import selfcard
        from core.infra import textfile
        contract = selfcard.CARD
    except Exception as exc:  # noqa: BLE001 - 导入失败本身就是问题，如实报
        return ["无法导入 loop.selfcard / core.infra.textfile：%r" % (exc,)]
    d = os.path.join(ROOT, "data", "persona", "cards")
    if not os.path.isdir(d):
        return []
    bad = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".md"):
            continue
        try:
            raw = read(os.path.join(d, fn))
        except Exception as exc:  # noqa: BLE001
            bad.append("%s: 读不出来 %r" % (fn, exc))
            continue
        for why in textfile.blockers(raw, contract):
            bad.append("%s: %s" % (fn, why))
    return bad


# --- 2026-10-28 六修·锁门：模块级代码不许用未绑定的名字 ---
# 形状规则（不列名字）：模块作用域里被读取的名字，必须在本模块 import 过、
# 或在模块作用域被定义/赋值，或是内建。UI-066 把 `POLL_MS = to_ms(...)` 写在模块级、
# 而 `to_ms` 只在 `from core.infra import duration` 里 —— py_compile 过、四检过、
# 一 import 就 NameError。这条规则把那类错误提前到静态检查。
import builtins as _builtins


def _module_level_names(tree):
    bound, used, star = set(), [], [False]

    def bind_target(node):
        if isinstance(node, ast.Name):
            bound.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for e in node.elts:
                bind_target(e)
        elif isinstance(node, ast.Starred):
            bind_target(node.value)

    def scan(nodes):
        for n in nodes:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(n.name)
                for d in getattr(n, "decorator_list", []):
                    scan([d])
                for d in list(getattr(getattr(n, "args", None), "defaults", []) or []):
                    scan([d])
                continue
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    if a.name == "*":
                        star[0] = True
                    else:
                        bound.add((a.asname or a.name).split(".")[0])
                continue
            if isinstance(n, ast.Name):
                if isinstance(n.ctx, ast.Store):
                    bound.add(n.id)
                else:
                    used.append((n.id, n.lineno))
                continue
            if isinstance(n, (ast.For, ast.AsyncFor)):
                bind_target(n.target)
                scan([n.iter]); scan(n.body); scan(n.orelse)
                continue
            if isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    if item.optional_vars is not None:
                        bind_target(item.optional_vars)
                    scan([item.context_expr])
                scan(n.body)
                continue
            if isinstance(n, ast.Try):
                scan(n.body)
                for h in n.handlers:
                    if h.name:
                        bound.add(h.name)
                    scan(h.body)
                scan(n.orelse); scan(n.finalbody)
                continue
            for child in ast.iter_child_nodes(n):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    bound.add(getattr(child, "name", ""))
                    continue
                scan([child])

    scan(tree.body)
    if star[0]:
        return []
    ok = bound | set(dir(_builtins)) | {"__name__", "__file__", "__doc__", "__package__", "__spec__"}
    return [(n, ln) for n, ln in used if n not in ok]


def check_modname():
    """模块级读取的名字必须已绑定：否则 py_compile 过、一 import 就 NameError。"""
    bad = []
    for pkg in BIZ_ROOTS + ("scripts",):
        base = os.path.join(ROOT, pkg)
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", ".venv_it")]
            for f in files:
                if not f.endswith(".py"):
                    continue
                fp = os.path.join(root, f)
                src = read(fp)
                if "noqa: modname" in src:
                    continue
                try:
                    tree = ast.parse(src)
                except SyntaxError:
                    continue
                for name, line in _module_level_names(tree):
                    bad.append("%s:%d %s" % (rel(fp), line, name))
    return bad


def _egress_factories():
    """哪些“调用”等于自己开一条出去的连接。"""
    return {
        "httpx.Client", "httpx.AsyncClient", "httpx.stream", "httpx.get",
        "httpx.post", "httpx.request",
        "requests.Session", "requests.get", "requests.post", "requests.request",
        "aiohttp.ClientSession",
        "urllib.request.urlopen", "urllib.request.Request",
        # 裸 socket.socket 不列：仓里三种正当用途（本机 TCP 探活、端口互斥 bind、
        # websockets 库内部），要分“连还是绑、本机还是公网”得追数据流，超出这条锁的本分。
        # 真出网必须经过 create_connection 或下面某个 HTTP 客户端。
        "socket.create_connection",
        "http.client.HTTPConnection", "http.client.HTTPSConnection",
    }


_GOING_OUT = ("httpx", "requests", "aiohttp", "urllib3")


def _dotted(node):
    """`a.b.c` 这种调用目标的字面名；认不出就空串。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _egress_offences(text, where):
    """从一份源码里找出“自己开了一条出网连接”的位置。

    为什么走 AST 而不是搜关键字：`import httpx as hx` + `hx.Client(` 能绕过字符串搜索，
    而注释/文档字符串里提到 `httpx.Client(` 又会被误报。这里只认一种形状——**真的调用了
    构造/发送函数**（名字经 import 别名还原后落在 `_egress_factories()` 里）。
    单纯 `import httpx` 不算（很多人只是拿它当异常类型用）。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return ["%s 解析不了：%s" % (where, exc.msg)]
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top in _GOING_OUT or a.name.startswith(("urllib.request", "socket", "http.client")):
                    aliases[a.asname or top] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            mod = node.module
            if mod.split(".")[0] in _GOING_OUT or mod.startswith(("urllib.request", "socket", "http.client")):
                for a in node.names:
                    aliases[a.asname or a.name] = "%s.%s" % (mod, a.name)
    lines = text.splitlines()
    factory = _egress_factories()
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if not name:
            continue
        head, _, tail = name.partition(".")
        full = (aliases[head] + ("." + tail if tail else "")) if head in aliases else name
        if full not in factory:
            continue
        src = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
        if "noqa: egress" in src:
            continue
        bad.append("%s:%d %s" % (where, node.lineno, full))
    return bad


def check_egress():
    """出网客户端只许在 `core/infra/egress.py` 里构造（只扫运行时；`scripts/` 是离线工具）。

    DSH-045 的核心不是“多检查几个 URL”，而是**出网只有一扇门**：只要能构造
    客户端，就能绕过 https/公网/不跟随重定向/响应上限/总超时这一整套。
    所以这里管的是“构造动作”这种形状（真的开一条出去的连接），不是列业务场景；
    判据落在 `_egress_offences()` 里走 AST，别名与注释都骗不过它。
    """
    bad = []
    for pkg in BIZ_ROOTS:
        base = os.path.join(ROOT, pkg)
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "node_modules")]
            for f in files:
                if not f.endswith(".py"):
                    continue
                fp = os.path.join(root, f)
                r = rel(fp)
                if r.endswith("core/infra/egress.py") or r.endswith("scripts/check.py"):
                    continue
                bad.extend(_egress_offences(read(fp), r))
    return bad


_ABS_STOP = chr(34) + r"\s',;<>|"
_ABS_PATH = re.compile(r"[A-Za-z]:\\[^" + _ABS_STOP + r"]+")


def _abs_candidates(text):
    out = []
    for match in _ABS_PATH.finditer(text):
        raw = match.group(0).rstrip("\\")
        out.append(raw.replace("\\\\", "\\"))
    return out


def check_abspath(files=None):
    """配置里写死的绝对路径必须存在。

    2026-11-24：项目从 yinyue 改名 jingjing 后，`.env` 里 RECALL_CHAIN_DIR 和
    LEGACY_READ_BINDING 还指着旧目录——深度召回空转、启动报 FileNotFoundError。
    这类事故只改目录名就会复发，所以进静态门禁。
    """
    offenders = []
    if files is None:
        files = [os.path.join(ROOT, ".env")]
        cfg = os.path.join(ROOT, "configs")
        if os.path.isdir(cfg):
            for name in sorted(os.listdir(cfg)):
                if name.endswith((".json", ".toml")):
                    files.append(os.path.join(cfg, name))
    for fp in files:
        if not os.path.isfile(fp):
            continue
        try:
            text = read(fp)
        except OSError as exc:
            print("WARN abspath_unreadable %s: %s" % (fp, exc), file=sys.stderr)
            continue
        for candidate in _abs_candidates(text):
            if any(ch in candidate for ch in "*?%"):
                continue
            if not os.path.exists(candidate):
                offenders.append("%s :: %s" % (rel(fp), candidate))
    return offenders


CHECKS = {"py": check_py, "events": check_events, "flags": check_flags,
          "envread": check_envread, "undef": check_undef, "json": check_json,
          "hbpath": check_hbpath, "cardfile": check_cardfile,
          "egress": check_egress,
          "modname": check_modname, "abspath": check_abspath}


def load_baseline():
    if not os.path.exists(BASELINE):
        return {}
    try:
        return json.loads(read(BASELINE))
    except ValueError:
        return {}


def main(argv):
    which = [a for a in argv if not a.startswith("-")] or ["all"]
    if which == ["all"]:
        which = list(CHECKS)
    base = load_baseline()
    fresh, new = {}, {}
    for key in which:
        found = CHECKS[key]()
        fresh[key] = sorted(set(found))
        known = set(base.get(key, []))
        new[key] = [x for x in fresh[key] if x not in known]
    if "--update-baseline" in argv:
        base.update(fresh)
        open(BASELINE, "w", encoding="utf-8", newline="\n").write(
            json.dumps(base, ensure_ascii=False, indent=1, sort_keys=True))
    report = {k: {"total": len(fresh[k]), "new": len(new[k])} for k in which}
    print("CHECK " + json.dumps(report, ensure_ascii=False))
    for k in which:
        for item in new[k][:8]:
            print("  [%s] %s" % (k, item))
    if any(new[k] for k in which) and "--update-baseline" not in argv:
        print("FAIL 新增违规，先修再提交（历史欠账见 scripts/check_baseline.json）")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
