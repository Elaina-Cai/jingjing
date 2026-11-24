"""鲸鲸体检：把「缺什么、怎么修」用人话列出来（stdlib only）。

用法::

    python scripts/doctor.py            # 项目内执行
    python scripts/doctor.py --project-root D:\\yinyue

双击项目根目录的「体检鲸鲸.bat」等价。

设计原则：本模块只用标准库，不 import 任何第三方依赖——它要在环境坏掉时
也能跑起来报告问题。每项检查给出 [OK]/[!!]/[XX] 三档和一行「修复」建议；
存在 [XX] 时退出码为 1（方便脚本把关），否则为 0。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import socket
import sqlite3
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: 别给子进程弹控制台窗

OK = "ok"
WARN = "warn"
ERROR = "error"

MIN_PYTHON = (3, 12)

# 与 scripts/setup_wizard.REQUIRED_MODULES 保持一致（有测试核对）。
REQUIRED_MODULES: dict[str, str] = {
    "sqlalchemy": "sqlalchemy",
    "alembic": "alembic",
    "openai": "openai",
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "jinja2": "jinja2",
    "multipart": "python-multipart",
    "structlog": "structlog",
    "schedule": "schedule",
    "lz4": "lz4",
    "zstandard": "zstandard",
    "httpx": "httpx",
    "pymem": "pymem",
    "Cryptodome": "pycryptodomex",
    "cv2": "opencv-python",
    "uiautomation": "uiautomation",
}

PROVIDER_KEY_FIELDS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
}


@dataclass
class Check:
    level: str
    title: str
    detail: str = ""
    fix: str = ""


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def read_env(path: Path) -> tuple[dict[str, str], dict[str, int]]:
    """返回（键值表, 重复键计数）；键值表沿用「先出现」口径，仅用于展示。"""
    values: dict[str, str] = {}
    counts: dict[str, int] = {}
    if not path.exists():
        return values, counts
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values, counts
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        if key not in values:
            values[key] = value.strip()
    duplicates = {key: n for key, n in counts.items() if n > 1}
    return values, duplicates


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------


def check_python(minimum: tuple[int, int] = MIN_PYTHON) -> Check:
    info = sys.version_info
    label = f"{info.major}.{info.minor}.{info.micro}"
    if info[:2] >= minimum:
        return Check(OK, f"Python {label}", str(Path(sys.executable)))
    return Check(
        ERROR,
        f"Python {label}（需要 ≥{minimum[0]}.{minimum[1]}）",
        fix="装新版本 Python（安装时勾选 Add python.exe to PATH），或双击 安装鲸鲸.bat",
    )


def check_dependencies(modules: dict[str, str] | None = None) -> Check:
    table = modules if modules is not None else REQUIRED_MODULES
    missing = [name for name in table if importlib.util.find_spec(name) is None]
    if not missing:
        return Check(OK, f"第三方依赖齐全（{len(table)} 项）")
    human = ", ".join(f"{name}（{table[name]}）" for name in missing[:6])
    more = "" if len(missing) <= 6 else f" 等 {len(missing)} 项"
    return Check(
        ERROR,
        f"缺少依赖: {human}{more}",
        fix="双击 安装鲸鲸.bat；或手动 pip install -r requirements.txt",
    )


def check_env_file(env_path: Path) -> Check:
    if not env_path.exists():
        return Check(ERROR, "没有 .env 配置文件", fix="双击 安装鲸鲸.bat 自动生成并填写")
    _, duplicates = read_env(env_path)
    if duplicates:
        names = ", ".join(sorted(duplicates))
        return Check(
            WARN,
            f".env 有重复键: {names}",
            detail="普通键是「后面的覆盖前面的」，YINYUE_* 开关是「先出现的生效」，容易配错",
            fix="删掉重复行（或重跑 安装鲸鲸.bat，它会把重复键统一成一个值）",
        )
    return Check(OK, "存在 .env 配置文件")


def check_llm_key(env_path: Path) -> Check:
    env, _ = read_env(env_path)
    provider = (env.get("PROVIDER") or "deepseek").strip().lower() or "deepseek"
    field = PROVIDER_KEY_FIELDS.get(provider, f"{provider.upper()}_API_KEY")
    value = (env.get(field) or "").strip()
    if value:
        return Check(OK, f"LLM key 已配置（{field}）")
    return Check(
        ERROR,
        f"没有可用的 LLM key（{field} 为空）",
        fix=f"去 {provider} 官网申请 key，双击 安装鲸鲸.bat 填进去",
    )


def check_wechat_env(env_path: Path) -> Check:
    env, _ = read_env(env_path)
    missing = [
        field
        for field in ("WECHAT_YINYUE_WXID", "WECHAT_OWNER_WXIDS", "WECHAT_DB_PATH")
        if not (env.get(field) or "").strip()
    ]
    key_ok = bool((env.get("WECHAT_KEY") or "").strip())
    if not missing and key_ok:
        return Check(OK, "微信账号/数据库/密钥已配置")
    parts = []
    if missing:
        parts.append("缺账号信息（" + ", ".join(missing) + "）")
    if not key_ok:
        parts.append("缺微信数据库密钥 WECHAT_KEY")
    return Check(
        WARN,
        "微信配置不完整：" + "；".join(parts),
        fix="以管理员运行 python start.py（首次会检测账号并提取密钥）",
    )


def check_send_switch(env_path: Path) -> Check:
    env, _ = read_env(env_path)
    raw = (env.get("WECHAT_SEND_ENABLED") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return Check(OK, "发送开关已打开（WECHAT_SEND_ENABLED=true）")
    return Check(
        WARN,
        "发送开关没打开：鲸鲸只能看、不能回",
        fix=".env 里设 WECHAT_SEND_ENABLED=true（或重跑 安装鲸鲸.bat）",
    )


def check_timezone(env_path: Path) -> Check:
    env, _ = read_env(env_path)
    tz = (env.get("TEMPORAL_TIMEZONE") or "").strip()
    if tz:
        return Check(OK, f"展示时区: {tz}")
    return Check(
        WARN,
        "没设展示时区，将用系统本地时区",
        fix=".env 里设 TEMPORAL_TIMEZONE=Asia/Shanghai（不设一般也能跑）",
    )


def check_autostart(path: Path) -> Check:
    if not path.exists():
        return Check(WARN, "没有 autostart.bat", fix="重跑 安装鲸鲸.bat 或从仓库补齐")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Check(ERROR, f"autostart.bat 读不了: {exc}")
    match = re.search(r'^cd /d "([^"]*)"', text, re.MULTILINE)
    if match and match.group(1) != "%~dp0":
        return Check(
            WARN,
            f"autostart.bat 的路径是写死的: {match.group(1)}",
            fix="重跑 安装鲸鲸.bat（会把该行改成 %~dp0 自适应）",
        )
    return Check(OK, "autostart.bat 路径自适应（%~dp0）")


def check_ollama(
    *,
    url: str = "http://localhost:11434/api/tags",
    model: str = "qwen2.5:7b",
    timeout: float = 2.0,
) -> Check:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.load(resp)
        names = [str(item.get("name", "")) for item in data.get("models", [])]
    except Exception:  # noqa: BLE001 - 服务不可达/超时都算没起来
        return Check(
            WARN,
            "Ollama 没起来（快路径/降级会用到它）",
            fix="装好并启动 Ollama（https://ollama.com/download），或忽略本项（DeepSeek 可用即可）",
        )
    if model and not any(name == model or name.startswith(model) for name in names):
        return Check(WARN, f"Ollama 在跑，但缺模型 {model}", fix=f"执行: ollama pull {model}")
    return Check(OK, f"Ollama 在跑，模型 {model} 已就绪")


def _window_title_is_wechat(title: str) -> bool:
    value = str(title or "")
    return any(marker in value for marker in ("微信", "WeChat", "Weixin"))


def _wechat_window_probe() -> bool:
    """窗口兜底：微信主进程可能以更高完整性运行，进程枚举被拒但窗口可枚举。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        if user32.FindWindowW("Qt51514QWindowIcon", "微信"):
            return True
        found = {"hit": False}

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if _window_title_is_wechat(buf.value):
                found["hit"] = True
                return False
            return True

        user32.EnumWindows(_cb, 0)
        return bool(found["hit"])
    except Exception:  # noqa: BLE001 - 探测失败只影响展示，不影响任何流转
        return False


def _wechat_probe() -> bool | None:
    """微信是否在运行；无法确定时返回 None。

    进程探测在受限/高完整性会话里可能拿不到真实客户端（tasklist 报 Access denied
    或返回空），所以 2026-10-06 起：**先记下"进程列表查不到"，永远再做一次窗口
    探测**；窗口也没有才考虑判否。另：WeChat 4.x 主进程名可能是 ``Weixin.exe``
    或 ``WeChat.exe``，两者都查。
    """
    tasklist_clean = False
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_NO_WINDOW,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if "Weixin.exe" in output or "WeChat.exe" in output:
            return True
        if "Access denied" not in output and result.returncode == 0:
            # 空/无主进程：可能真没开，也可能名字/权限不同 —— 不在这里收口。
            tasklist_clean = True
    except Exception:  # noqa: BLE001 - 换下一种探测方式
        pass
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "if (Get-Process -Name Weixin,WeChat -ErrorAction SilentlyContinue) "
                "{ 'yes' } else { 'no' }",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=_NO_WINDOW,
        )
        answer = (result.stdout or "").strip().lower()
        if answer.startswith("yes"):
            return True
    except Exception:  # noqa: BLE001 - 换下一种探测方式
        pass
    if _wechat_window_probe():
        return True
    if tasklist_clean:
        return False
    return None


def check_wechat_running(*, probe=None) -> Check:
    """probe() -> bool | None：None 表示无法确定（非 Windows 等）。"""
    if probe is None:
        probe = _wechat_probe

    running = probe()
    if running is None:
        return Check(WARN, "无法确认微信是否在运行（跳过）")
    if running:
        return Check(OK, "微信客户端在运行（进程或窗口探测）")
    return Check(WARN, "微信没在运行（不启动也能体检，但发不了消息）", fix="先打开微信并登录鲸鲸号")


def check_service(*, host: str = "127.0.0.1", port: int = 39871, timeout: float = 1.0) -> Check:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return Check(
            WARN,
            f"服务没在 {host}:{port} 上跑",
            fix="登录微信后双击 autostart.bat（成功标志：日志 scheduler_running）",
        )
    api = ""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=timeout) as resp:
            payload = json.load(resp)
        api = f"（/api/status: {str(payload)[:80]}）"
    except Exception:  # noqa: BLE001 - 状态接口拿不到也不代表没在跑
        api = ""
    return Check(OK, f"服务在 {host}:{port} 上跑{api}")


def check_data(root: Path, *, db_path: Path | None = None) -> Check:
    db = db_path or (root / "data" / "yinyue_meta.db")
    if not db.exists():
        return Check(
            WARN,
            "元数据库还没建（第一次启动会自动创建）",
            fix="双击 autostart.bat 跑一次即可",
        )
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            try:
                events = conn.execute("SELECT count(*) FROM terminal_events").fetchone()[0]
            except sqlite3.Error:
                return Check(
                    WARN,
                    "元数据库还没有 terminal_events 表（首次启动会自动建）",
                    fix="双击 autostart.bat 跑一次即可",
                )
            try:
                version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            except Exception:  # noqa: BLE001 - 老库可能没有版本表
                version = "?"
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return Check(
            ERROR,
            f"元数据库打不开/读不了: {exc}",
            fix="看 docs/恢复手册.md（先停服，再按备份恢复）",
        )
    return Check(OK, f"元数据库正常（terminal_events={events}, schema={version}）")


def check_work_ledger(root: Path, *, db_path: Path | None = None) -> Check:
    """任务台账（P1）：META 里 ``work_items`` / ``work_item_runs`` 在不在。

    缺失 = Migration 52 没落在这个 META 库上（新库还没建过；或库是**纯迁移链**
    建的、52 的 META 守卫没命中）。提醒本身照常投递（``reminders`` 是执行权威），
    但任务模型这一侧零投影，排查时看着像「任务丢了」，所以给 ``[!!]`` + 修复提示。
    stdlib only、只读（``mode=ro``），不 import 应用代码。
    """
    db = db_path or (root / "data" / "yinyue_meta.db")
    if not db.exists():
        # check_data 已经报了「元数据库还没建」，这里不重复告警。
        return Check(OK, "任务台账：元数据库还没建（第一次启动会自动创建）")
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check(WARN, f"任务台账：元数据库读不了，跳过（{exc}）")
    missing = [name for name in ("work_items", "work_item_runs") if name not in tables]
    if not missing:
        return Check(OK, "任务台账正常（work_items / work_item_runs 都在）")
    return Check(
        WARN,
        f"任务台账缺表: {', '.join(missing)}",
        detail="Migration 52 没应用到这个 META 库：提醒照常投递，但 work_items 投影一直是空的",
        fix="重启一次服务（双击 autostart.bat / python start.py）让 Migration 52 落地；"
            "如果这个库是纯迁移链建的，就用 db.migrations.run_migrations 指向它重跑",
    )


# ---------------------------------------------------------------------------
# 事件 outbox（REC-3）
# ---------------------------------------------------------------------------

# 阈值：failed>0 或「有 pending 且最旧超过 10 分钟」→ 提醒；stale>0（租约过期
# 没人收尾）或 pending>500（积压到该人工介入）→ 错误（退出码 1）。
OUTBOX_PENDING_WARN_AGE_SECONDS = 600
OUTBOX_PENDING_ERROR_ROWS = 500


def _outbox_db_counts(db: Path) -> dict | None:
    """只读一个 account.db 的 outbox 计数；表不存在/读不了返回 None。

    stdlib only（照 check_data 用 ``mode=ro`` URI）：不 import 应用代码，
    环境坏掉时也能出报告。旧库没有 ``lease_until`` 列 ⇒ stale 记 0。
    """
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        try:
            cols = {
                row[1] for row in conn.execute("PRAGMA table_info(event_outbox)")
            }
        except sqlite3.Error:
            return None
        if not cols:
            return None                      # 迁移 47 还没落：没有 outbox 可看
        stale_expr = "0"
        if "lease_until" in cols:
            stale_expr = (
                "SUM(CASE WHEN status='inflight' AND lease_until IS NOT NULL "
                "AND julianday(lease_until) <= julianday(:now) THEN 1 ELSE 0 END)"
            )
        row = conn.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status='inflight' THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END), 0), "
            f"COALESCE({stale_expr}, 0), "
            "MIN(CASE WHEN status='pending' THEN created_at END) "
            "FROM event_outbox",
            {"now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    age = 0
    if row[5]:
        try:
            age = max(
                0,
                int((datetime.now() - datetime.fromisoformat(str(row[5]))).total_seconds()),
            )
        except ValueError:
            age = 0
    return {
        "pending": int(row[0] or 0),
        "inflight": int(row[1] or 0),
        "failed": int(row[2] or 0),
        "delivered": int(row[3] or 0),
        "stale": int(row[4] or 0),
        "oldest_pending_age_seconds": age,
    }


def check_outbox(root: Path, *, platforms_dir: Path | None = None) -> Check:
    """事件 outbox 的积压 / 失败 / 租约过期（REC-3）：``data/platforms/*/*/account.db``。

    只看真实落库的计数，不联网也不起服务：failed>0 → [!!]；有 pending 且最旧
    超过 10 分钟 → [!!]；stale>0 或 pending>500 → [XX]（退出码 1）。
    """
    base = platforms_dir or (root / "data" / "platforms")
    dbs = sorted(base.glob("*/*/account.db")) if base.exists() else []
    rows: list[tuple[Path, dict]] = []
    for db in dbs:
        counts = _outbox_db_counts(db)
        if counts is not None:
            rows.append((db, counts))
    if not rows:
        return Check(OK, "事件 outbox：还没有账号库（或都还没建 outbox 表）")
    totals = {key: 0 for key in
              ("pending", "inflight", "failed", "delivered", "stale",
               "oldest_pending_age_seconds")}
    for _, counts in rows:
        for key in ("pending", "inflight", "failed", "delivered", "stale"):
            totals[key] += counts[key]
        totals["oldest_pending_age_seconds"] = max(
            totals["oldest_pending_age_seconds"],
            counts["oldest_pending_age_seconds"],
        )
    line = (
        f"outbox pending={totals['pending']} inflight={totals['inflight']} "
        f"failed={totals['failed']} stale={totals['stale']}"
    )
    bad = [
        (db, counts) for db, counts in rows
        if counts["failed"] or counts["stale"] or counts["pending"]
    ]
    detail = "; ".join(
        f"{db.parent.parent.name}/{db.parent.name}"
        f"(pending={counts['pending']}, failed={counts['failed']}, "
        f"stale={counts['stale']})"
        for db, counts in bad[:5]
    )
    if totals["stale"] or totals["pending"] > OUTBOX_PENDING_ERROR_ROWS:
        return Check(
            ERROR,
            f"事件 outbox 异常：{line}",
            detail=detail,
            fix="租约过期=投递方崩了/卡住（下一轮 drain 会回收，一直 stale 就看日志 "
                "outbox_delivery_failed）；pending>500 是投递停摆，先看服务在不在跑",
        )
    if totals["failed"] or (
        totals["pending"]
        and totals["oldest_pending_age_seconds"] > OUTBOX_PENDING_WARN_AGE_SECONDS
    ):
        return Check(
            WARN,
            f"事件 outbox 有缺口：{line}"
            f"（最久 pending {totals['oldest_pending_age_seconds']}s）",
            detail=detail,
            fix="failed 行是终态，需要人工排查（见 docs/恢复手册.md）；pending 超 10 分钟"
                "没投出说明 drain 卡住",
        )
    return Check(OK, f"事件 outbox 正常：{line}", detail=f"账号库 {len(rows)} 个")


def check_expression_baseline(root: Path, *, window_days: int = 7) -> Check:
    """表达层基线（《控制元规则》§4 的验收位）：近期的人设违例 / 设定外扬 / 名单形态。

    只读。判据**不复制**——直接调 ``scripts/expr_signal_report.py`` 的检查模式，
    免得同一个口径出现两份实现（那正是架构元规则禁的东西）。

    样本不足时返回 OK：宁可放过，也不要在数据少的时候误报。
    """
    import importlib.util

    path = root / "scripts" / "expr_signal_report.py"
    if not path.exists():
        return Check(OK, "表达层基线：脚本缺失，跳过")
    try:
        spec = importlib.util.spec_from_file_location("_expr_baseline", path)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        rows, _noise = mod.load_replies(None)
        fails, oks = mod.check_recent(rows, window_days=window_days)
    except Exception as exc:  # noqa: BLE001 - 体检不该被它拖垮
        return Check(WARN, "表达层基线：读取失败", detail=str(exc)[:120],
                     fix="手动跑 python scripts\\expr_signal_report.py --check")
    if fails:
        return Check(ERROR, f"表达层基线：{len(fails)} 项超标",
                     detail="；".join(fails[:3]),
                     fix="python scripts\\expr_signal_report.py --check 看全文")
    return Check(OK, "表达层基线：近期无退化",
                 detail="；".join(oks[:2]) if oks else "")


def check_imports(root: Path, *, timeout: float = 180.0) -> Check:
    """核心模块能不能 import。

    2026-11-02：`py_compile` 查不出装饰器冲突——删类时漏删一行 `@dataclass`，
    两个 `frozen=True` 叠在下一个类头上，编译全绿、import 才抛，
    调度器起不来、整条链静默死掉。这里用子进程跑，不把应用代码 import 进体检进程。
    """
    script = root / "scripts" / "check_imports.py"
    if not script.is_file():
        return Check(WARN, "核心模块 import", "找不到 scripts/check_imports.py")
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 体检不该因为一个子项崩掉
        return Check(WARN, "核心模块 import", "没跑起来：%r" % (exc,),
                     fix="手动跑 python scripts\\check_imports.py")
    if proc.returncode == 0:
        return Check(OK, "核心模块 import", "全部可 import")
    fails = [ln.strip() for ln in (proc.stdout or "").splitlines()
             if ln.strip().startswith("FAIL")]
    detail = "；".join(f.split()[-1] for f in fails[:5]) if fails else "有模块 import 失败"
    return Check(ERROR, "核心模块 import", detail, fix="python scripts\\check_imports.py 看明细")


def run_checks(
    *,
    root: Path | None = None,
    env_path: Path | None = None,
    port: int = 39871,
    ollama_url: str = "http://localhost:11434/api/tags",
    ollama_timeout: float = 2.0,
    ollama_model: str = "qwen2.5:7b",
    process_probe=None,
    modules: dict[str, str] | None = None,
) -> list[Check]:
    root = root or project_root()
    env_path = env_path or (root / ".env")
    return [
        check_python(),
        check_imports(root),
        check_dependencies(modules),
        check_env_file(env_path),
        check_llm_key(env_path),
        check_wechat_env(env_path),
        check_send_switch(env_path),
        check_timezone(env_path),
        check_autostart(root / "autostart.bat"),
        check_ollama(url=ollama_url, model=ollama_model, timeout=ollama_timeout),
        check_wechat_running(probe=process_probe),
        check_service(port=port),
        check_data(root),
        check_work_ledger(root),
        check_outbox(root),
        check_expression_baseline(root),
    ]


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

_MARKS = {OK: "[OK]", WARN: "[!!]", ERROR: "[XX]"}


def render(checks: list[Check]) -> str:
    lines = ["鲸鲸体检报告", "=" * 52]
    for index, item in enumerate(checks, 1):
        mark = _MARKS.get(item.level, "[??]")
        first = f"{mark} {index:>2}. {item.title}"
        if item.detail:
            first += f"\n         {item.detail}"
        lines.append(first)
        if item.fix and item.level != OK:
            lines.append(f"         修复: {item.fix}")
    errors = sum(1 for item in checks if item.level == ERROR)
    warns = sum(1 for item in checks if item.level == WARN)
    lines.append("=" * 52)
    if not errors and not warns:
        lines.append("结论: 全部正常，可以启动鲸鲸（双击 autostart.bat）。")
    else:
        bits = []
        if errors:
            bits.append(f"{errors} 项错误")
        if warns:
            bits.append(f"{warns} 项提醒")
        lines.append("结论: " + "、".join(bits) + "，按上面的「修复」处理。")
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(item.level == ERROR for item in checks) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="鲸鲸体检")
    parser.add_argument("--project-root", default="", help="项目目录（默认取本脚本上级目录）")
    parser.add_argument("--port", type=int, default=39871)
    args = parser.parse_args(argv)
    try:  # 让中文/符号在 GBK 控制台也不炸
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    root = Path(args.project_root).resolve() if args.project_root else project_root()
    checks = run_checks(root=root, port=args.port)
    print(render(checks))
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
