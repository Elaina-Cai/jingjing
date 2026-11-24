# loop / CONTRACT

一页纸。**改这个文件的人要先想清楚，因为它是新大脑唯一的宪法。**

## 形状

```
loop/  ── 新大脑（userland）：一个循环，她自己决定看什么、做什么、说不说、何时停
ports.py ── 唯一的门面：loop 与旧系统（驱动层）之间的全部接触面
```

## 七条不变式

1. **一个循环，不是流水线。** 一次 tick 里她可以反复 `look → 想 → do → say → 再看 → stop`。
2. **不许替她筛。** 视界全量给她；"亮起来"只是提示，不是门槛。
3. **确定性只留在边界。** 预算 / 禁区 / 时间 / 投递 四项由代码守，其余选择权是她的。
4. **她的每个决定都要留痕。** 每次 say/do/silent 写一行账：`at, act, target, why, refs, result`。`why` 用她自己的话。
5. **失败了也要留痕。** 任何 blocked / error 都要带 `blocked_reason`，不许静默丢弃。
6. **只许经过 ports。** `loop/` 里出现 `core.*` / `agents.*` / `db.*` / `llm.*` 的 import，测试直接红。
7. **可以随时收工。** 轮次上限只是保险丝，不是流程。

## ports 四个门面

| 门面 | 语义 | 谁实现 |
|---|---|---|
| `sense(account, *, since_hours, limit)` | 看见世界（会话/消息行） | 旧系统：sqlite 直读 |
| `look(ref)` | 展开一个 ref（win:/msg:/person:/mem:） | 旧系统：检索与画像 |
| `act(name, args, ctx)` | 动手（工具） | 旧系统：ToolRegistry |
| `say(text, ctx)` | 说话（含守卫/闸门/微信/QQ） | 旧系统：OutboundGateway |

## 一拍的形状

```
tick → snapshot(世界一屏 ≤1000 字) + digest(账摘要 ≤400 字)
     → brain: for step in range(3): chat_tools(四原语)
     → 每步写账
     → 出口（唯一门：守卫→闸门→平台）
```

## 删除清单

旧决策层什么时候删、删哪些、谁替代，见 `DELETE_LIST.md`。

## 八、静默失败禁令（2026-10-24 立）

一天里四个 bug 全是同一个病根：宽 `except` 或静默 `pass` 把错误吞掉，进程看着健康、其实在空转。

规矩：**每个 `except` 体内必须留一行痕迹**（`warn(...)` 或日志），或者显式 `raise`；**每个裸露的 `pass` 必须带注释**说明为什么可以忽略。

守门：`python scripts/check_silent_exceptions.py loop`（退出码 0 = 干净；建议每次改完 `loop/` 都跑）
痕迹落点：`logs/loop_warnings.log`（`loop/safelog.py::warn`，自己绝不抛异常）
