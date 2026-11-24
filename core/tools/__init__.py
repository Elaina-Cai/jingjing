# -*- coding: utf-8 -*-
"""工具包（`docs/decisions/工具包.md`）。

命名注意：本包里的 ``observe`` / ``act`` / ``schedule`` 与
`core/affect/engine.py:106`、`core/speech/relationship.py:25`、
`core/context/intention_runner.py:135` 的同名方法**没有任何关系**——
那三处是情绪引擎 / 关系管理器 / 意图匹配的内部方法。工具契约一律走
``core.tools.registry``，不要用裸 ``grep observe(`` 判断「实现了没有」。
"""

from __future__ import annotations
