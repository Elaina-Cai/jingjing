"""控制台 API 契约：面板里要的每个路径，collector 必须有分支接住（CONS-005）。

清单原文（docs/该抄的清单.md:96-98）：「新增控制台 API 必须同步补 mock，否则套件静默失效」——
新增接口前端调了，服务端没那分支，面板只是某个按钮点了没反应，没人会翻日志。

本仓没有前端 mock 体系（没有 .mjs、没有 node 测试），所以换等价做法：
**把 collector 的 do_GET/do_POST 分支当唯一真源**，反向检查面板里每一处请求。
这样「新增接口」这件事天然被覆盖：加接口不改前端不会红，前端先要了不存在的接口会红。

两条写法约定（改面板前先读）：
1. 请求路径必须以 "/" 开头的**字面量**出现：`API + "/x"`、`lawFetch("/x")`。
   不要写 `"models"` 这种相对路径 —— 它跟着页面 URL 走（/app 时是 /models，
   一旦变成 /app/ 就是 /app/models 直接 404，而且没有任何测试会红）。
2. 端口只在 `const HTTP` 一处出现，其余一律 `API + "/x"`。
"""

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COLLECTOR = os.path.join("console", "collector.py")
PANEL = os.path.join("console", "panel.html")

# 服务端有、前端不会用 XHR 去要的路径（浏览器/系统直接打开）。加了新端点别往这里塞。
SERVER_ONLY_OK = {
    "/thumb/": "面板经 collector 下发的 thumb URL 间接使用（it.thumb），源码里没有字面量",
    "/app": "页面本身：地址栏打开，不是 XHR",
    "/app/": "同上（末尾斜杠写法）",
    "/health": "探活：答第 1+2 层（进程在、accept 在转），给看护脚本直连，面板不调",
    "/icon.ico": "浏览器自动请求",
}

_REQ_SITE = re.compile(r"(?:fetch\(|lawFetch\(|\.src\s*=\s*)(.*)")
_ABS_LITERAL = re.compile(r'"(\/[^"]*)"')
# 表达式开头的标识符：`url + (force?...)` → url，再去查 const 初始化（只跟一层）
_IDENT = re.compile(r"^\s*([A-Za-z_$][\w$]*)")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _server_routes():
    """从 collector 的 do_GET / do_POST 里抽路径字面量（唯一真源，不另抄一份表）。"""
    tree = ast.parse(_read(COLLECTOR))
    routes = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in cls.body
                   if isinstance(n, ast.FunctionDef) and n.name in ("do_GET", "do_POST")]:
            routes[fn.name] = {
                c.value for c in ast.walk(fn)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
                and c.value.startswith("/") and len(c.value) > 1
            }
    return routes


def _function_span(lines, header):
    """找 `header` 那个函数的行区间（用于跳过 helper 自己内部的 fetch）。"""
    for i, line in enumerate(lines):
        if header in line and "{" in line:
            depth = 0
            for j in range(i, len(lines)):
                depth += lines[j].count("{") - lines[j].count("}")
                if depth <= 0:
                    return (i, j)
    return None


def _const_init_path(name, lines, seen):
    """`const url = API + "/checkup";` → "/checkup"（只跟一层，够用且不会递归失控）。"""
    if name in seen:
        return None
    seen.add(name)
    pat = re.compile(r"^\s*(?:const|let|var)\s+" + re.escape(name) + r"\s*=\s*(.+)$")
    for line in lines:
        m = pat.match(line)
        if m:
            return _path_of(m.group(1), lines, seen)
    return None


def _path_of(expr, lines, seen=None):
    m = _ABS_LITERAL.search(expr)
    if m:
        return m.group(1).split("?")[0]
    m = _IDENT.match(expr)
    if m:
        return _const_init_path(m.group(1), lines, seen or set())
    return None


def _parse_panel():
    """返回 (前端要的路径 → 行号列表, 归不出来的行号列表)。"""
    lines = _read(PANEL).splitlines()
    span = _function_span(lines, "function lawFetch(")
    wants, unresolved = {}, []
    for i, line in enumerate(lines):
        if span and span[0] <= i <= span[1]:
            continue  # helper 定义内部那一行 fetch(API+path)：path 就是入参，不用归
        m = _REQ_SITE.search(line)
        if not m:
            continue
        p = _path_of(m.group(1), lines)
        if p:
            wants.setdefault(p, []).append(i + 1)
        else:
            unresolved.append(i + 1)
    return wants, unresolved


def test_panel_requests_are_all_served():
    """前端要的每个路径，collector 必须接得住。红了就是「前端已经上线、服务端没这接口」。"""
    served = set().union(*_server_routes().values())
    wants, _ = _parse_panel()
    unknown = sorted(set(wants) - served)
    assert not unknown, "未知 API（面板在要，collector 没这个分支）：" + "、".join(
        "%s（panel.html:%s）" % (p, ",".join(map(str, wants[p]))) for p in unknown)


def test_every_request_site_is_a_rooted_literal():
    """请求路径必须是 "/x" 字面量；相对路径（"models"）会跟着页面 URL 变，见文件头。"""
    wants, unresolved = _parse_panel()
    assert not unresolved, (
        "这些行归不出请求路径，改成 API + \"/x\" 或 lawFetch(\"/x\")：panel.html:%s"
        % ",".join(map(str, unresolved)))
    assert not [p for p in wants if not p.startswith("/")]


def test_no_endpoint_silently_dropped():
    """服务端有的接口，要么被前端用，要么在 SERVER_ONLY_OK 里写明理由（防「写了没人用」）。"""
    served = set().union(*_server_routes().values())
    wants, _ = _parse_panel()
    unused = sorted(served - set(wants))
    assert unused == sorted(SERVER_ONLY_OK), (
        "collector 里有前端没调、也没登记理由的接口：%s"
        % sorted(set(unused) - set(SERVER_ONLY_OK)))


def test_port_literal_lives_in_one_place():
    """端口只允许出现在 `const HTTP` 那一行 —— 否则 file:// 与同源两种模式会各自漂。"""
    hits = [(i + 1, l.strip()) for i, l in enumerate(_read(PANEL).splitlines())
            if '"http://127.0.0.1:39872' in l]
    assert len(hits) == 1, "端口字面量应只有 1 处（const HTTP），现在 %d 处：%s" % (
        len(hits), [h[0] for h in hits])
    assert "const HTTP" in hits[0][1]


def test_extractors_are_not_vacuous():
    """防静默失效：抽不到东西就红，而不是「0 个未知 = 通过」。"""
    served = set().union(*_server_routes().values())
    wants, _ = _parse_panel()
    assert len(served) >= 12, "只从 collector 抽出 %d 个路径，抽取规则可能被写歪了" % len(served)
    assert len(wants) >= 10, "只从面板抽出 %d 个路径，抽取规则可能被写歪了" % len(wants)
    assert "/cmd" in wants and "/cmd" in served, "面板的主通道 /cmd 没被抽到"

def _collector_module():
    """真导入 console.collector：探活载荷要按行为验，不是按源码字符串验。"""
    import importlib
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    return importlib.import_module("console.collector")


def test_health_answers_layer_one_and_two_only():
    """SEC-009：「能力按边界拆」在本仓的最小形状 = 探活只承诺进程在、accept 在。"""
    payload = _collector_module()._health_payload()
    assert set(payload) == {"ok", "at"}, "探活载荷只该有 ok/at，多了：%s" % sorted(set(payload) - {"ok", "at"})
    assert payload["ok"] is True
    assert isinstance(payload["at"], (int, float)), "at 该是时间戳，实际 %r" % (payload["at"],)


def test_health_branch_reads_only_the_clock():
    """探活一旦有副作用，它就不再是探活：不许碰 executor、文件、DB、进程身份。"""
    tree = ast.parse(_read(COLLECTOR))
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_health_payload"]
    assert len(fns) == 1, "collector 里该只有一份 _health_payload，现在 %d 份" % len(fns)
    fn = fns[0]
    called = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
    assert called == {"_now"}, "探活只该调 _now()，现在调了 %s" % sorted(called)
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    for bad in ("executor", "os", "db", "open", "write_state", "getpid"):
        assert bad not in names, "_health_payload 里出现了 %r，探活不该碰它" % bad


def test_post_surface_is_control_plane_only():
    """POST 只有两个口：/cmd（执行器，有写能力）与 /rehearse（排练，只读算力）。"""
    assert _server_routes()["do_POST"] == {"/rehearse", "/cmd"}, (
        "POST 面变了：%s —— 只读探活不要挂在写的 server 上（SEC-009）" % sorted(_server_routes()["do_POST"]))
