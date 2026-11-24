"""Small, dependency-free environment helpers.

Pydantic only loads fields it knows about; custom ``YINYUE_*`` entries
(feature flags, resolver tuning overrides) are read from ``os.environ``
directly.  This helper lets operators keep those custom values in ``.env``
without importing the heavy bootstrap module.

**测试进程不许继承生产 ``.env`` 的开关**（2026-10-02，同类事故第三次）
-------------------------------------------------------------------------
机制：``core.bootstrap.init_dirs()`` 会调 :func:`load_yinyue_env_defaults`，
它在 key **缺失**时把 ``.env`` 写进 ``os.environ``。于是生产配置成了测试的
**隐式输入**：``.env`` 打开 ``YINYUE_FEATURE_PAT_GRACE=on`` / ``...SEMANTIC_UNIT=on``
之后，``tests/test_trigger_grace.py`` / ``tests/test_pat_sync.py`` 里"flag 关 = 旧
行为"的断言就实际跑在开关打开之下（同一个坑咬了三次，见这两个文件的文件头）。

所以本模块把 ``.env`` 的键分两类：

* **开关类**（:func:`is_switch_key`）：``YINYUE_FEATURE_*`` 前缀 + :data:`SWITCH_KEYS`
  里那几条不带前缀的三态/布尔开关。测试进程**一律不注入**——要哪个开关，测试自己
  显式设（``os.environ[...] = "off"``），或者走 :data:`TEST_NO_ENV_FLAGS` 的显式开关；
* **资料类**：密钥 / 路径 / 模型 / 白名单（``YINYUE_CHAT_WINDOW_CHATS`` 等）。照旧注入，
  测试要的就是这些，不注入只会让用例退化成假绿。

"测试进程"怎么认（:func:`skip_switch_injection`，两条路，都留了显式出口）：

1. 显式环境变量 :data:`TEST_NO_ENV_FLAGS`——真值=跳过开关，假值（``0``/``off``/``no``/
   ``false``）=**照旧全注入**（对照/变异自检位，也留给"我就要生产口径"的人）；
2. 自动识别（未显式设置时）：pytest 进程（``PYTEST_CURRENT_TEST`` 或 ``sys.modules``
   里有 ``pytest``），或 ``__main__`` 是 ``tests/test_*.py``（``python tests/test_x.py``
   这种仓库里的正统直跑方式）。

自动识别是必要的：验收与日常排查都会**直接**跑单个测试文件（不经
``scripts/run_all_tests.py``），只靠 runner 传标记挡不住那条路。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from safelog import warn

#: 开关类键的前缀（``core.features.env_key()`` 用的就是它）。
_FEATURE_PREFIX = "YINYUE_FEATURE_"

#: 不带 ``YINYUE_FEATURE_`` 前缀的开关（三态或布尔）。与 ``core/features.py`` 的
#: ``ADMISSION_MODE_ENV`` 是线上变量名；其余几条
#: 各自在 ``core/group_state.py``（合并/时间戳）、``core/context/chat_window.py``
#: （轮次通道）、``core/context/answer_links.py`` 里直接读。**只放开关**：白名单类
#: 的 ``*_CHATS`` / ``*_GROUPS``、阈值类 ``*_SECONDS`` / ``*_MIN_SCORE``、路径类
#: ``*_PATH`` 都不在这里——它们是"资料"，测试照旧继承。
SWITCH_KEYS = frozenset({
    "YINYUE_ADMISSION_MODE",
    "YINYUE_TURNS_MODE",
    "YINYUE_TURNS_TIMESTAMP",
    "YINYUE_ANSWER_LINKS",
})

#: 显式标记：真值 → 测试进程；假值 → 强制照旧注入（对照位）；未设置 → 自动识别。
TEST_NO_ENV_FLAGS = "YINYUE_TEST_NO_ENV_FLAGS"

#: 与 ``core.features`` 的真值口径一致（``1``/``true``/``yes``/``on``）。
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off"})


def is_switch_key(key: str) -> bool:
    """``key`` 是不是"开关类"键（测试进程不继承的那一类）。"""
    return key.startswith(_FEATURE_PREFIX) or key in SWITCH_KEYS


def _main_script() -> Path | None:
    """当前进程的入口脚本（``__main__`` 优先，退回 ``argv[0]``）。"""
    main = sys.modules.get("__main__")
    raw = getattr(main, "__file__", None)
    if not raw:
        raw = sys.argv[0] if sys.argv else ""
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except (OSError, ValueError):  # pragma: no cover - 极端路径
        warn("core/env.py:81 (OSError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
        return None


def running_under_tests() -> bool:
    """当前进程是不是"测试进程"（pytest，或 ``tests/test_*.py`` 直跑）。"""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if "pytest" in sys.modules:
        return True
    path = _main_script()
    if path is None:
        return False
    return path.parent.name == "tests" and path.name.startswith("test_")


def skip_switch_injection() -> bool:
    """本次加载要不要跳过开关类键（判定见模块 docstring）。"""
    raw = os.environ.get(TEST_NO_ENV_FLAGS)
    if raw is not None:
        value = raw.strip().lower()
        if value in _FALSEY:
            return False
        if value in _TRUTHY or value == "":
            return True
        return True
    return running_under_tests()


def load_yinyue_env_defaults(env_path: str | Path = ".env") -> int:
    """Populate ``os.environ`` from custom ``YINYUE_*`` .env entries.

    Real process environment always wins, so ``$env:...`` / shell exports
    remain the highest-priority way to experiment.  Returns the number of
    keys populated.

    测试进程里**开关类键不注入**（见模块 docstring）——返回的计数因此也不含它们。
    """
    path = Path(env_path)
    if not path.exists():
        return 0
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        warn("core/env.py:124 OSError", err=repr(__import__("sys").exc_info()[1]))
        return 0
    skip_switches = skip_switch_injection()
    loaded = 0
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.startswith("YINYUE_"):
            continue
        if skip_switches and is_switch_key(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded
