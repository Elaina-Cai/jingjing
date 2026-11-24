"""打字这件事只有一个权威实现（2026-10-28，SEC-006）。

抄的是 surfcore `SEC-006`：同一件事只留唯一权威实现，别的地方只许 re-export 或薄包装。
我们原来有四个入口：wx_human.paste_text / wx_chat.type_text / wx_sender.type_text 别名 /
wx_sender._type_text_via_clipboard 别名，外加 gui/wechat.py 在 _default_deps 里直连
wx_human.paste_text —— 改一处漏三处（补丁锚点找不到就是这么来的）。

规矩：
- 真实现只有 `core/execution/wx_human.py::paste_text`，且只允许 `wx_chat.type_text` 调它。
- 上层唯一入口是 `core/execution/wx_chat.py::type_text`。
"""
import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXEC = os.path.join(ROOT, "core", "execution")


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _code(rel):
    """去掉行内注释后的源码——注释里提到某个名字不算“还在用它”。"""
    return "\n".join(line.split("#", 1)[0] for line in _src(rel).splitlines())


def _py_files(base):
    for root, _, files in os.walk(base):
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def test_type_text_defined_in_one_module_only():
    owners = []
    for path in _py_files(EXEC):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "type_text":
                owners.append(os.path.relpath(path, ROOT))
    assert owners == [os.path.join("core", "execution", "wx_chat.py")], owners


def test_paste_text_called_only_from_wx_chat():
    callers = []
    for path in _py_files(os.path.join(ROOT, "core")):
        rel = os.path.relpath(path, ROOT)
        if rel.endswith(os.path.join("execution", "wx_human.py")):
            continue
        text = open(path, encoding="utf-8").read()
        code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        if "wx_human.paste_text(" in code:
            callers.append(rel)
    assert callers == [os.path.join("core", "execution", "wx_chat.py")], callers


def test_no_duplicate_aliases_in_sender():
    src = _src(os.path.join("core", "execution", "wx_sender.py"))
    assert "_type_text_via_clipboard" not in src
    assert len(re.findall(r"^type_text\s*=", src, re.M)) == 0 or src.count("type_text = wx_chat.type_text") == 1


def test_paste_text_docstring_marks_authority():
    assert "唯一实现" in _src(os.path.join("core", "execution", "wx_human.py"))


def test_wechat_deps_use_single_entry():
    code = _code(os.path.join("core", "execution", "gui", "wechat.py"))
    assert "wx_human.paste_text" not in code


def test_typing_authority_importable():
    import sys
    sys.path.insert(0, ROOT)
    from core.execution import wx_chat, wx_human  # noqa: F401
    assert callable(wx_chat.type_text) and callable(wx_human.paste_text)

def test_entry_has_two_ways_and_one_name():
    tree = ast.parse(_src(os.path.join("core", "execution", "wx_chat.py")))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "type_text" in fns, sorted(fns)
    a = fns["type_text"].args
    names = [x.arg for x in list(a.args) + list(a.kwonlyargs)]
    assert "via" in names, names
    vias = [n for n in tree.body if isinstance(n, ast.Assign)
            and any(getattr(x, "id", "") == "VIAS" for x in n.targets)]
    assert len(vias) == 1, vias
    # VIAS 的两个成员是常量名（VIA_CLIPBOARD / VIA_UNICODE），不是字面量；
    # 所以先看名字对不对，再去看这两个常量的值 —— 数结构，别数字面量。
    assert [getattr(e, "id", None) for e in vias[0].value.elts] == ["VIA_CLIPBOARD", "VIA_UNICODE"]
    vals = {c.targets[0].id: c.value.value for c in tree.body
            if isinstance(c, ast.Assign) and isinstance(c.value, ast.Constant)
            and getattr(c.targets[0], "id", "") in ("VIA_CLIPBOARD", "VIA_UNICODE")}
    assert vals == {"VIA_CLIPBOARD": "clipboard", "VIA_UNICODE": "unicode"}, vals


def test_unknown_way_is_rejected():
    import sys

    sys.path.insert(0, ROOT)
    from core.execution import wx_chat

    try:
        wx_chat.type_text("x", via="telepathy")
    except ValueError as exc:
        assert "telepathy" in str(exc), exc
    else:
        raise AssertionError("via 写错居然没报错")


def test_unicode_route_called_only_from_wx_chat():
    callers = []
    for path in _py_files(os.path.join(ROOT, "core")):
        rel = os.path.relpath(path, ROOT)
        if rel.endswith(os.path.join("execution", "wx_human.py")):
            continue
        code = "\n".join(l.split("#", 1)[0] for l in open(path, encoding="utf-8").read().splitlines())
        if "type_unicode(" in code:
            callers.append(rel)
    assert callers == [os.path.join("core", "execution", "wx_chat.py")], callers


def test_gui_backend_verb_is_not_named_type_text():
    for rel in ("core/execution/gui/cdp.py", "core/execution/gui/backends.py", "core/tools/builtin.py"):
        assert "type_text" not in _code(rel), rel
    assert "insert_text" in _code("core/execution/gui/backends.py")
    assert _code("core/execution/gui/cdp.py").count("def insert_text(") == 1

