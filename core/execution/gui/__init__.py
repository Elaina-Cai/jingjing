# -*- coding: utf-8 -*-
"""手：GUI 驱动层（一个 adapter 家族，backend 可插：CDP → ADB → UIA → Vision）。

出处：`docs/decisions/软件适配器.md` §18（GUI 驱动：只读先行）。

这一层只做两件事：`snapshot`（把界面读成文本）与 `act`（点 / 输）。
**第一片只做读**，因为两种失败的方向不对称：读错 = 少看一眼，
点错 = 点重一下就有第二单（写操作没有幂等键，只能靠状态差异）。

不许 import `core.speech` / `core.routing` / `agents` / `scheduler` / `web`
（`scripts/check_layering.py` 规则 11）。
"""
