"""pytest 风格用例的公共前置（只有 tests/pytest_style/ 下的用例会用到）。

存在的理由：这些用例搬到子目录后，`Path(__file__).parents[N]` 的层级变了。每个文件里
仍然各自算了一遍仓库根（那样单独 `pytest tests/pytest_style/x.py` 也能跑），这里再补一次
是给「忘了算」的新文件兜底 —— conftest 先于用例模块导入，插进 sys.path 后 `from core...`
在任何层级都成立。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
TESTS = ROOT / "tests"

for path in (str(ROOT), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)
