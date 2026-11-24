"""运行时配置的唯一入口（2026-10-26 配置收口）。

优先级：环境变量 > .env > configs/runtime.json > 内置默认。
别的模块**不要**再写死端口或哨兵路径，一律 `from core import runtime_config as rt`。
"""
from __future__ import annotations

import json
import os

from core.infra import skips

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JSON = os.path.join(_ROOT, "configs", "runtime.json")

DEFAULTS = {
    "engine_port": 39871,
    "engine_poll_s": 0.4,
    "watchdog_interval_s": 5.0,
    "desktop_boot_timeout_s": 30.0,
    "sentinels": {
        "scheduler": "logs/scheduler.stop",
        "loop": "logs/loop.stop",
        "watchdog": "logs/watchdog.stop",
    },
}


def _file() -> dict:
    try:
        with open(_JSON, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001 - 配置缺失不是致命错
        pass
    return {}


def section(name: str) -> dict:
    """configs/runtime.json 里的一个子树（例如 "tempo"）；没有/写坏给空 dict。

    给「一组相关旋钮」留的通用读口：名字与语义由调用方（core/infra/tempo.py）
    定义，这里只回答「文件里写了什么」。不缓存 —— 改完配置重启即可生效。
    """
    got = _file().get(name)

    return got if isinstance(got, dict) else {}


def _env_file(name: str) -> str:
    try:
        with open(os.path.join(_ROOT, ".env"), encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s.startswith(name) and "=" in s and not s.startswith("#"):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _pick(key: str, env_name: str = "", cast=int):
    """返回 (值, 来源)。来源写进日志和 /api/service/runtime，方便排“改了没生效”。"""
    if env_name:
        v = os.environ.get(env_name, "").strip()
        if v:
            try:
                return cast(v), "env:%s" % env_name
            except Exception as exc:  # noqa: BLE001
                # 写了值但解析不了 = malformed，不是「没写」：静默回默认会让人以为改了没生效
                skips.skip("runtime_config._pick:env:%s" % env_name, "malformed",
                           "%s: %s" % (type(exc).__name__, exc))
        v = _env_file(env_name)
        if v:
            try:
                return cast(v), ".env:%s" % env_name
            except Exception as exc:  # noqa: BLE001
                skips.skip("runtime_config._pick:dotenv:%s" % env_name, "malformed",
                           "%s: %s" % (type(exc).__name__, exc))
    data = _file()
    if key in data and data[key] not in (None, ""):
        try:
            return cast(data[key]), "runtime.json"
        except Exception as exc:  # noqa: BLE001
            skips.skip("runtime_config._pick:runtime.json:%s" % key, "malformed",
                       "%s: %s" % (type(exc).__name__, exc))
    return DEFAULTS[key], "default"




def engine_port() -> int:
    return _pick("engine_port", "", int)[0]


def engine_poll_s() -> float:
    return _pick("engine_poll_s", "", float)[0]


def watchdog_interval_s() -> float:
    return _pick("watchdog_interval_s", "", float)[0]


def desktop_boot_timeout_s() -> float:
    return _pick("desktop_boot_timeout_s", "", float)[0]


def sentinel(name: str) -> str:
    """哨兵文件的绝对路径：scheduler / loop / watchdog。"""
    data = _file()
    rel = (data.get("sentinels") or {}).get(name) or DEFAULTS["sentinels"].get(name, "")
    return os.path.join(_ROOT, rel) if rel else ""


def resolve() -> dict:
    rows = {}
    for key, env_name, cast in (
        ("engine_port", "", int),
        ("engine_poll_s", "", float),
        ("watchdog_interval_s", "", float),
        ("desktop_boot_timeout_s", "", float),
    ):
        v, src = _pick(key, env_name, cast)
        rows[key] = {"value": v, "source": src}
    rows["sentinels"] = {k: sentinel(k) for k in ("scheduler", "loop", "watchdog")}
    return rows


def check() -> list:
    """启动自检：返回“同一个事实有两处不同值”的告警行。"""
    out = []
    data = _file()
    if not data:
        out.append("configs/runtime.json 不存在或读不出，全部走内置默认")
    return out
