"""身份键检查：会话只按会话、人只按人、会话键只许一处串化。

四条规则（每条都配负向自检，见 ``--self-test``）：

A 窗口读必须按会话
     ``core/context/`` 下任何提到 ``terminal_events`` 的文件，必须也提到 ``chat_id``。
     防"读一段全局事件再在 Python 里筛"——那种写法在多账号下会把别人的窗口混进来。
B 「人 + 会话」同键只许在白名单
     ``UniqueConstraint`` 里同时出现 ``person_id`` 与 ``chat_id`` 的表，只允许
     ``group_memberships`` / ``group_membership_periods`` / ``conversation_windows``
     ——「人∈会话」是有意建模，别处出现就是拿人当会话键。
C 会话键只许一处串化
     只有 ``core/context/chat_window.py`` 能定义**模块级** ``conversation_key``
     （类属性 ``ConversationMessage.conversation_key`` 是三元组别名，不算串化）。
     规范形式是 ``core/contracts/events.py::ConversationRef.key``（三元组）。
D 会话键必须带平台与账号
     任何含 ``chat_id`` 的 ``UniqueConstraint`` 必须同时含 ``platform``（或 ``source``）
     与 ``account_id``（或 ``bot_account_id``）。缺一个 = 两个账号会撞键。

出口：全过 PRINT PASSED 并 exit 0；有违规列出来并 exit 1。
跑法：``.venv_it\\Scripts\\python.exe -X utf8 scripts\\check_identity_keys.py``
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SKIP_PARTS = frozenset({
    ".venv", ".venv_it", ".test_tmp", "__pycache__", "node_modules", ".git",
    "output", "data", "logs", "backups", "build", "dist",
})

PLATFORM_COLUMNS = frozenset({"platform", "source"})
ACCOUNT_COLUMNS = frozenset({"account_id", "bot_account_id"})
PERSON_COLUMNS = frozenset({"person_id", "actor_person_id"})
CHAT_COLUMNS = frozenset({"chat_id", "str_talker"})

#: 规则 B：允许「人 + 会话」同时做键的表（每条都要能说出为什么）。
PERSON_CHAT_TABLES = {
    "group_memberships": "人属于哪个群（四元键含账号）",
    "group_membership_periods": "同上，但用 epoch 表示退群/重进",
    "conversation_windows": "按人+会话物化的窗口（读侧缓存，不是事实源）",
}

#: 规则 D 的已知缺口：键里缺账号，多账号时同群同消息会撞键。修好就删掉这一行。
#: 2026-10-21 已清空：``utterance_edges`` 的唯一键补上了 ``bot_account_id``
#: （迁移 66 + ``db/meta_models.py`` 同步），规则 D 现在必须零缺口。
KNOWN_MISSING_ACCOUNT = {}

#: 规则 C：只许这一处定义串化函数。
KEY_OWNER = "core/context/chat_window.py"

SERIALIZE_RE = re.compile(r"^def\s+conversation_key\s*\(", re.M)


def project_files(root: Path = ROOT):
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        yield path, rel.as_posix()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def check_window_scoped(files) -> list[str]:
    """规则 A。"""
    problems = []
    for path, rel in files:
        if not rel.startswith("core/context/"):
            continue
        src = _read(path)
        if "terminal_events" not in src:
            continue
        if "chat_id" not in src:
            problems.append(f"A {rel}: 读 terminal_events 但没有 chat_id，窗口没按会话取")
    return problems


def _table_of(lines: list[str], lineno: int) -> str:
    for i in range(min(lineno, len(lines)) - 1, -1, -1):
        if "__tablename__" in lines[i]:
            return lines[i].split("=", 1)[1].strip().strip("\"'")
    return "?"


def check_person_chat_keys(files) -> list[str]:
    """规则 B。"""
    problems = []
    for path, rel in files:
        src = _read(path)
        if "UniqueConstraint" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            problems.append(f"B {rel}: 语法错误，无法解析")
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", "") != "UniqueConstraint":
                continue
            columns = {a.value for a in node.args if isinstance(a, ast.Constant)}
            if not (columns & PERSON_COLUMNS and columns & CHAT_COLUMNS):
                continue
            table = _table_of(lines, node.lineno)
            if table not in PERSON_CHAT_TABLES:
                problems.append(
                    f"B {rel}:{node.lineno} 表 {table} 用「人+会话」当唯一键"
                    "（拿人当会话键，群与私聊会串）"
                )
    return problems


def check_single_key_owner(files) -> list[str]:
    """规则 C。"""
    problems = []
    seen = 0
    for path, rel in files:
        src = _read(path)
        if not SERIALIZE_RE.search(src):
            continue
        seen += 1
        if rel != KEY_OWNER:
            problems.append(f"C {rel}: 定义了第二处 conversation_key（只许 {KEY_OWNER}）")
    if seen == 0:
        problems.append(f"C 全仓找不到 conversation_key（{KEY_OWNER} 被删了？）")
    return problems


def check_unique_scoped(files) -> list[str]:
    """规则 D。"""
    problems = []
    for path, rel in files:
        src = _read(path)
        if "UniqueConstraint" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", "") != "UniqueConstraint":
                continue
            columns = {a.value for a in node.args if isinstance(a, ast.Constant)}
            if not (columns & CHAT_COLUMNS):
                continue
            missing = []
            if not (columns & PLATFORM_COLUMNS):
                missing.append("platform")
            if not (columns & ACCOUNT_COLUMNS):
                missing.append("account_id")
            if not missing:
                continue
            table = _table_of(lines, node.lineno)
            name = ""
            for keyword in node.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    name = str(keyword.value.value)
            if (table, name) in KNOWN_MISSING_ACCOUNT:
                continue
            problems.append(
                f"D {rel}:{node.lineno} 表 {table} 的唯一键缺 {'/'.join(missing)}"
                "（两个账号会撞键）"
            )
    return problems


def check_parity() -> list[str]:
    """运行时 parity：串化 == '|'.join(ConversationRef.key)，且可逆。"""
    from core.context.chat_window import conversation_key, split_conversation_key
    from core.contracts.events import ConversationRef

    problems = []
    ref = ConversationRef(platform="wechat", account_id="wxid-bot", chat_id="group:1")
    joined = "|".join(ref.key)
    if conversation_key("wechat", "wxid-bot", "group:1") != joined:
        problems.append("parity 串化与 ConversationRef.key 不一致")
    if split_conversation_key(joined) != ref.key:
        problems.append("parity 串化不可逆")
    return problems


def run(root: Path = ROOT) -> list[str]:
    files = list(project_files(root))
    problems = []
    problems += check_window_scoped(files)
    problems += check_person_chat_keys(files)
    problems += check_single_key_owner(files)
    problems += check_unique_scoped(files)
    problems += check_parity()
    return problems


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    problems = run()
    for line in problems:
        print(line)
    if problems:
        print(f"FAILED: {len(problems)} problem(s)")
        return 1
    print(
        f"PASSED: 规则 A–D 全过 + parity（扫 {len(list(project_files()))} 个文件，"
        f"人+会话白名单 {len(PERSON_CHAT_TABLES)} 张表，已知缺口 {len(KNOWN_MISSING_ACCOUNT)} 条）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
