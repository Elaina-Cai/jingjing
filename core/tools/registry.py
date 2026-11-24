# -*- coding: utf-8 -*-
"""工具包的契约 + 注册表实现。

出处：`docs/decisions/工具包.md` §2（三动词）/ §4（四种 effects）/ §5（注册七字段）
/ §6（注入上限）/ §7（有界）/ §11（防同名不同义）。

三条硬纪律（本文件的实现就是它们的落地）：
1. ``external_send`` **一律 blocked**：要对外说话只能走出口注册表；
2. 未注册的工具名或参数 → ``unknown_tool`` / ``bad_args``，不许「尽力猜一个」；
3. 每回合 ≤2 次调用（含失败），超了就是 ``too_many_calls``。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from core.constants import TOOL_ARGS_SPLIT_OVER, TOOL_IN_PROMPT_MAX, TOOL_MAX_CALLS_PER_TURN


class Effect(str, Enum):
    """工具包.md §4：四种 effects，按**可逆性**分层。"""

    READ = "read"
    WRITE_LOCAL = "write_local"
    UI_CONTROL = "ui_control"
    EXTERNAL_SEND = "external_send"


#: 默认态：读默认开；写本地与控 UI 默认影子；对外发一律不许（只能走出口）
DEFAULT_EFFECT_MODE: Mapping[Effect, str] = {
    Effect.READ: "enabled",
    Effect.WRITE_LOCAL: "shadow",
    Effect.UI_CONTROL: "shadow",
    Effect.EXTERNAL_SEND: "blocked",
}

#: 未注册工具 / 规格不合法 / 参数不合法 / 越界 / 次数用尽 —— 全是 blocked 的 reason
TOOL_REASONS: tuple[str, ...] = (
    "unknown_tool",
    "bad_spec",
    "bad_args",
    "external_send_via_exit",
    "effect_blocked",
    "not_enabled",
    "too_many_calls",
    "timeout",
)


#: 工具返回的 ``status`` 封闭集合（工具包.md §6 失败码那一列 + §4.1 的 ui_blocked）。
#:
#: 为什么要是封闭集合：``ok`` / ``no_result`` / ``timeout`` 三者的区别决定了下游
#: 能不能说「没查到」——写错一个词，下游就只能靠猜。没登记过的码一律当缺陷。
#: 失败之后"下一步该干什么"——写给人话给模型看的，不是给人看的日志。
#:
#: 判据（2026-10-21）：模型下一轮唯一能读到的就是这段文字。只给 `no_target`
#: 这种码，它只能猜；给一句"先看窗口是不是最小化到托盘"，它就能换方法。
NEXT_STEP_HINTS: dict[str, str] = {
    "no_target": "先确认目标名字对不对（用 gui_state 看一眼当前是什么），再换 target 重试",
    "not_connected": "后端没连上：浏览器要开调试端口，桌面要窗口没最小化；先别重试，先看环境",
    "ui_blocked": "三条判据有一条没过：把目标窗口切到前台、等一下再试",
    "no_change": "动作做了但世界没变：换个定位方式（换 selector / 加 wait），不要重复点同一处",
    "not_enabled": "这个工具还没被人启用：不要重试，告诉主人你需要它",
    "bad_args": "参数名或个数不对：按工具说明里的参数名重新组织",
    "too_many_calls": "本回合工具额度用完了：把结果先说出来，剩下的事下一回合再做",
    "timeout": "超时：缩小范围（更短的查询/更小的页面）或过一会儿再试",
    "rate_limited": "被限流：等一会，或换一个来源",
    "http_4xx": "地址或权限不对：检查 URL 与登录态，不要用同一个地址硬重试",
    "http_5xx": "对方服务器出问题：换来源，不要等它",
    "no_result": "查到了但结果是空：可以换个说法再搜一次，别断言'没有'",
    "not_found": "指向的东西不存在：先确认它真的存在（列一下），再决定",
    "not_extractable": "内容抽不出来：换成截图/结构化读取，别反复抓正文",
    "unsupported_type": "这个格式不支持：换工具或换输入形态",
    "unknown_tool": "没有这个工具：只在清单里挑，不要臆造工具名",
    "external_send_via_exit": "要真发消息不能直接调工具：用 send_plan 开一件事，等主人批准",
    "failed": "工具自己报错：换方法或降低目标，不要重复同一调用",
}


def hint_for(status: str) -> str:
    """失败码 → 一句"下一步"。`ok` / 空 / 不认识的码 → 空串（不编）。"""
    return NEXT_STEP_HINTS.get(str(status or "").strip(), "")


STATUS_CODES: frozenset[str] = frozenset({
    "ok",
    "no_result",          # 查了，没有（≠ 查不了）
    "timeout",            # 查不了：超时
    "rate_limited",       # 查不了：被限流
    "http_4xx",           # 查不了：地址/权限不对
    "http_5xx",           # 查不了：对方服务器坏了
    "too_large",          # 查不了：内容超出上限
    "unsupported_type",   # 查不了：格式不支持
    "not_found",          # 指向的东西不存在
    "not_extractable",    # 存在但抽不出正文
    "vision_unavailable", # 图片理解不可用
    "ui_blocked",         # 工具包.md §4.1：前台可见三条没同时成立
    "not_connected",      # GUI 驱动：端口没开 / 没有 ws 地址（软件适配器 §13.3）
    "no_target",          # GUI 驱动：连上了但没有这个页面 / 窗口
    "no_change",          # 写动作做完了但什么都没变（工具包 §4.1：不许当成成功）
    "failed",             # 调用了，对方说不行（MCP `isError` / handler 内部异常）
})


def is_known_status(code: str) -> bool:
    """返回码是否登记过（未登记的码属于缺陷，不是「今天先用着」）。"""
    return str(code or "") in STATUS_CODES


#: 「截图可验」的门槛：目标区域至少这么多个非背景像素变化（工具包.md §4.1 第 3 条）
UI_MIN_PIXELS_CHANGED = 100

#: 「进程在前台」失败时的统一失败码
UI_BLOCKED = "ui_blocked"


def can_ui_control(
    spec: "ToolSpec",
    *,
    foreground_ok: bool,
    title_ok: bool,
    pixels_changed: int,
    value_changed: bool | None = None,
) -> tuple[bool, str]:
    """``ui_control`` 能不能动手（工具包.md §4.1）。

    三条**同时成立**才放行：进程在前台 → 标题包含注册项声明的 ``window_title``
    → 截图可验（目标区域 ≥100 个非背景像素变化）。任何一条不成立就是
    ``ui_blocked``——**不许「猜一个窗口点下去」**。

    第三条的另一种等价证据：往输入框里打字时，「输入框的值变了」比像素更准
    （打一个字只变十几个像素）。调用方给 ``value_changed`` 时，第三条就以它为准，
    ``pixels_changed`` 不再参与——**两个证据不会同时用，也不会都不用**。

    这里为什么不自己调 ``GetForegroundWindow``：那三个观测（前台/标题/像素差）
    都由调用方在动作前后取好传进来。本函数只做**判据**，保证「三条同时成立」只有
    一份实现；取观测值那部分留在执行层，测试才不必依赖真窗口。
    """
    effects = spec.effects
    if isinstance(effects, str):
        is_ui = effects == Effect.UI_CONTROL.value
    else:
        is_ui = Effect.UI_CONTROL in set(effects or ())
    if not is_ui:
        return (True, "")  # 不控 UI 的工具不需要前台
    if not str(spec.window_title or "").strip():
        return (False, UI_BLOCKED)  # 没声明标题就不许控 UI（不许猜）
    if not (foreground_ok and title_ok):
        return (False, UI_BLOCKED)
    if value_changed is not None:
        if not value_changed:
            return (False, UI_BLOCKED)
        return (True, "")
    if int(pixels_changed or 0) < UI_MIN_PIXELS_CHANGED:
        return (False, UI_BLOCKED)
    return (True, "")


@dataclass(frozen=True)
class ToolSpec:
    """注册七字段 + 状态（工具包.md §5）。缺任一必填 → 注册即拒。"""

    name: str
    family: str = ""
    effects: frozenset[Effect] = frozenset()
    args: Mapping[str, str] = field(default_factory=dict)
    returns: str = ""
    perms: tuple[str, ...] = ()
    limits: Mapping[str, Any] = field(default_factory=dict)
    idempotent: bool = True
    owner: str = ""
    enabled: bool = False  # 默认 off，由人启用
    window_title: str = ""  # ui_control 的目标窗口标题子串（工具包.md §4.1 第 2 条）
    purpose: str = ""  # 一句人话"什么时候用它"，进 prompt（工具包.md §7.1）

    def validate(self) -> tuple[bool, str]:
        """注册前自检：七字段齐、args 数量不超 ``TOOL_ARGS_SPLIT_OVER``。"""
        if not str(self.name or "").strip():
            return (False, "bad_spec")
        if not str(self.family or "").strip():
            return (False, "bad_spec")
        if not self.effects:
            return (False, "bad_spec")
        if not str(self.returns or "").strip():
            return (False, "bad_spec")
        if not str(self.owner or "").strip():
            return (False, "bad_spec")
        if len(self.args or {}) > TOOL_ARGS_SPLIT_OVER:
            return (False, "bad_args")  # 参数 >4 个就要拆成两个工具
        return (True, "")


@dataclass(frozen=True)
class ToolResult:
    """工具返回的形状：三动词共用（工具包.md §2）。

    ``status`` 的取值里，``ok`` 与 ``no_result`` **必须分开**：
    「查不了」（timeout）不是「没查到」（ok + 空结果），不许拿最像的顶。
    """

    status: str = "ok"          # ok / no_result / timeout / rate_limited / http_4xx / not_found / not_extractable / vision_unavailable
    content: str = ""
    source: str = ""
    at: str = ""
    truncated: bool = False
    evidence: str = ""          # act 的证据（截图路径 / 回执），缺失即缺陷（工具包.md §5）
    reversible: bool = True
    #: 状态差异式回执（`docs/decisions/办_在手的事.md` §17.2）：写操作没有幂等键，
    #: 只能拿「动手前长什么样 / 动手后长什么样」证明它真的变了。
    state_before: str = ""
    state_after: str = ""
    #: 变了什么（空 = 什么都没变 = 上层必须判 `no_change`，不许当成写成功）。
    changed: Mapping[str, Any] = field(default_factory=dict)


class ToolRegistry:
    """工具注册表 + 每回合调用计数 + 三动词派发。

    ``max_calls_per_turn`` 计数键是 ``turn_id``：**失败也算一次**（工具包.md §7）。
    """

    def __init__(self, *, max_calls_per_turn: int = TOOL_MAX_CALLS_PER_TURN) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._calls: dict[str, int] = {}
        self._handlers: dict[str, Callable[..., ToolResult]] = {}
        self._max_calls = max(0, int(max_calls_per_turn))

    # ── 注册 / 取 / 启用 ─────────────────────────────────────────
    def register(self, spec: ToolSpec) -> tuple[bool, str]:
        ok, reason = spec.validate()
        if not ok:
            return (False, reason)
        if spec.name in self._specs:
            return (False, "bad_spec")
        self._specs[spec.name] = spec
        return (True, "")

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(str(name or "").strip())

    def enable(self, name: str) -> tuple[bool, str]:
        """由人打开（工具包.md §5：默认 disabled）。``external_send`` 连开都不许开。"""
        key = str(name or "").strip()
        spec = self._specs.get(key)
        if spec is None:
            return (False, "unknown_tool")
        if Effect.EXTERNAL_SEND in spec.effects:
            return (False, "external_send_via_exit")
        self._specs[key] = replace(spec, enabled=True)
        return (True, "")

    def bind(self, name: str, handler: Callable[..., ToolResult]) -> None:
        """把工具名接到实现上（没有 handler 的工具只能被选中，不能被调）。"""
        self._handlers[str(name or "").strip()] = handler

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def select_for_prompt(self, *, limit: int = TOOL_IN_PROMPT_MAX) -> Sequence[ToolSpec]:
        """本轮进 prompt 的 schema：只含启用的，条数**不许超过** ``limit``。"""
        items = [s for s in self._specs.values() if s.enabled]
        items.sort(key=lambda s: (s.family, s.name))
        return tuple(items[: max(0, int(limit))])

    # ── 调用前判定 ───────────────────────────────────────────────
    def check_call(
        self, name: str, args: Mapping[str, Any] | None = None, *, turn_id: str = ""
    ) -> tuple[bool, str]:
        """返回 ``(ok, reason)``，reason 取自 ``TOOL_REASONS``。"""
        spec = self.get(name)
        if spec is None:
            return (False, "unknown_tool")
        if Effect.EXTERNAL_SEND in spec.effects:
            return (False, "external_send_via_exit")
        if not spec.enabled:
            return (False, "not_enabled")
        if args:
            unknown = set(args) - set(spec.args or {})
            if unknown:
                return (False, "bad_args")
        key = str(turn_id or "-")
        if self._calls.get(key, 0) >= self._max_calls:
            return (False, "too_many_calls")
        self._calls[key] = self._calls.get(key, 0) + 1
        return (True, "")

    def calls_in_turn(self, turn_id: str) -> int:
        return self._calls.get(str(turn_id or "-"), 0)

    # ── 三动词（工具包.md §2）────────────────────────────────────
    def observe(self, target: str, what: str, params: Mapping[str, Any] | None = None) -> ToolResult:
        return self._invoke("observe", target, what, params)

    def act(self, target: str, action: str, params: Mapping[str, Any] | None = None) -> ToolResult:
        return self._invoke("act", target, action, params)

    def schedule(self, when: str, what: str, params: Mapping[str, Any] | None = None) -> ToolResult:
        return self._invoke("schedule", when, what, params)

    def _invoke(
        self, verb: str, target: str, what: str, params: Mapping[str, Any] | None
    ) -> ToolResult:
        payload = dict(params or {})
        tool = str(payload.pop("tool", "") or "").strip()
        turn_id = str(payload.pop("turn_id", "") or "")
        spec = self.get(tool)
        if spec is None:
            return ToolResult(status="unknown_tool", content=f"未注册工具：{tool or verb}")
        ok, reason = self.check_call(tool, None, turn_id=turn_id)
        if not ok:
            return ToolResult(status=reason, content=f"blocked:{reason}")
        handler = self._handlers.get(tool)
        if handler is None:
            return ToolResult(status="not_enabled", content=f"未绑定 handler：{tool}")
        return handler(target=target, what=what, params=payload)
