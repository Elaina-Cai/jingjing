"""配方（recipe）：加一个 App = 加一个文件。

一条配方是「在这台电脑上照做一遍」的**声明**，不是代码。它只写两件事：

* **做什么**（`wake / navigate / click / type / wait`）；
* **凭什么算成功**（每步 `expect` 必填）。

怎么点、点哪里，属于 backend（CDP / UIA）；配方不知道自己跑在哪个 backend 上，
所以换 harness 不用改配方——这正是「加一个 App 只加一个文件」的含义。

四条纪律（与 `docs/decisions/软件适配器.md` §13.20 一致）：

1. **每步都要 `expect`**：跑完不满足就停，不猜下一步（没有 expect 的步骤等于
   「永远成功」，是假绿的源头）；
2. **默认不启用**（`enabled: false`）：由人改成 true，执行器还要过三级授权；
3. **`external_send` 不许写进配方**：发消息只能走出口注册表（R3），配方只能
   在本机界面上动手；
4. **未知字段一律报错**：`expect` 拼成 `expec` 若被静默忽略，这条配方就变成
   「永远成功」——所以这里不认识的字比错字更早失败。

模块无 I/O 副作用（除了读文件），不 import `core.speech` / `core.routing` /
`agents` / `scheduler` / `web`（`scripts/check_layering.py` 规则 10）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from core.execution.gui.base import (
    GuiError,
    check_nav_url,
    check_selector,
    check_text,
)

#: 配方目录（一个文件一条配方；`_` 开头的不加载，留给模板）。
APP_DIR = "configs/apps"

#: 一条配方最多几步（与「一件事的任务累计 ≤12 次工具调用」同一个数字）。
MAX_STEPS = 1000000   # 2026-10-31 取消上限（原来 12 步）

#: 配方名只认小写字母、数字、下划线。
NAME_MAX_CHARS = 40
EXPECT_MAX_CHARS = 120
NOTE_MAX_CHARS = 200
WAIT_MAX_S = 30.0

#: 步骤动词（`expect` 不是动词，是每步都有的判据）。
ALLOWED_DO = ("wake", "navigate", "click", "type", "wait")

#: 会动的步骤：要做这些，必须声明窗口标识（否则事先无法验前台/标题）。
MOTION_DO = ("navigate", "click", "type")

#: 配方里允许出现的 effects。`external_send` 永远不许写进配方。
ALLOWED_EFFECTS = ("read", "write_local", "ui_control")

#: 由步骤推出的 effects（声明值必须是它的超集）。
_DERIVED_EFFECTS = {
    "wake": "read",
    "navigate": "read",
    "click": "ui_control",
    "type": "ui_control",
    "wait": "read",
}

_RECIPE_KEYS = frozenset(
    {"name", "version", "enabled", "window_title", "window_class", "exe",
     "effects", "steps", "note"}
)
_STEP_KEYS = frozenset({"n", "do", "target", "text", "expect", "wait_s", "safe_point", "note"})
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


class RecipeError(ValueError):
    """配方写错时的唯一异常；`code` 稳定、`detail` 给人看。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Step:
    """一步。`expect` 是「凭什么算成功」，不许为空。"""

    n: int
    do: str
    target: str = ""
    text: str = ""
    expect: str = ""
    wait_s: float = 0.0
    safe_point: bool = True
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "do": self.do,
            "target": self.target,
            "text": self.text,
            "expect": self.expect,
            "wait_s": self.wait_s,
            "safe_point": self.safe_point,
            "note": self.note,
        }


@dataclass(frozen=True)
class Recipe:
    """一条配方：窗口标识 + 一串步骤 + 它需要的 effects。"""

    name: str
    steps: tuple[Step, ...] = ()
    window_title: str = ""
    window_class: str = ""
    exe: str = ""
    effects: tuple[str, ...] = ("read",)
    enabled: bool = False
    version: int = 1
    note: str = ""

    @property
    def needs_motion(self) -> bool:
        return any(step.do in MOTION_DO for step in self.steps)

    def step(self, n: int) -> Step | None:
        for step in self.steps:
            if step.n == n:
                return step
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "window_title": self.window_title,
            "window_class": self.window_class,
            "exe": self.exe,
            "effects": list(self.effects),
            "enabled": self.enabled,
            "version": self.version,
            "note": self.note,
            "steps": [step.as_dict() for step in self.steps],
        }


def _text(value: object, *, field: str, limit: int, allow_empty: bool = True) -> str:
    raw = value if isinstance(value, str) else ("" if value is None else str(value))
    out = raw.strip()
    if not out and not allow_empty:
        raise RecipeError("empty_field", f"{field} 不能为空")
    if len(out) > limit:
        raise RecipeError("field_too_long", f"{field} 超过 {limit} 字")
    return out


def _coerce_step(raw: Mapping[str, Any], *, index: int) -> Step:
    unknown = set(raw) - _STEP_KEYS
    if unknown:
        raise RecipeError("unknown_field", f"第 {index} 步有不认识的字段：{sorted(unknown)}")

    do = _text(raw.get("do"), field=f"第 {index} 步的 do", limit=20, allow_empty=False).lower()
    if do not in ALLOWED_DO:
        raise RecipeError("bad_do", f"第 {index} 步的 do={do!r} 不在 {ALLOWED_DO}")

    expect = _text(
        raw.get("expect"), field=f"第 {index} 步的 expect", limit=EXPECT_MAX_CHARS,
        allow_empty=False,
    )

    target = _text(raw.get("target"), field=f"第 {index} 步的 target", limit=400)
    text = ""
    wait_s = 0.0

    if do in ("navigate", "click"):
        if not target:
            raise RecipeError("empty_target", f"第 {index} 步（{do}）必须写 target")
    if do == "navigate":
        try:
            target = check_nav_url(target)
        except GuiError as exc:
            raise RecipeError("bad_url", f"第 {index} 步的 target 不是 http(s) 地址：{exc}") from exc
    if do == "click":
        try:
            target = check_selector(target)
        except GuiError as exc:
            raise RecipeError("bad_selector", f"第 {index} 步的 target 不是合法选择器：{exc}") from exc
    if do == "type":
        if not target:
            raise RecipeError("empty_target", f"第 {index} 步（type）必须写 target")
        try:
            target = check_selector(target)
            text = check_text(raw.get("text"))
        except GuiError as exc:
            raise RecipeError("bad_typed_text", f"第 {index} 步打不了这段字：{exc}") from exc
    if do == "wait":
        raw_wait = raw.get("wait_s", 1.0)
        try:
            wait_s = float(raw_wait)
        except (TypeError, ValueError) as exc:
            raise RecipeError("bad_wait", f"第 {index} 步的 wait_s 不是数字：{raw_wait!r}") from exc
        if not 0.0 < wait_s <= WAIT_MAX_S:
            raise RecipeError("bad_wait", f"第 {index} 步的 wait_s 必须在 (0, {WAIT_MAX_S}]")

    safe_point = raw.get("safe_point", True)
    if not isinstance(safe_point, bool):
        raise RecipeError("bad_safe_point", f"第 {index} 步的 safe_point 只能是 true/false")

    return Step(
        n=index,
        do=do,
        target=target,
        text=text,
        expect=expect,
        wait_s=wait_s,
        safe_point=safe_point,
        note=_text(raw.get("note"), field=f"第 {index} 步的 note", limit=NOTE_MAX_CHARS),
    )


def _derived_effects(steps: Iterable[Step]) -> set[str]:
    out: set[str] = set()
    for step in steps:
        out.add(_DERIVED_EFFECTS[step.do])
    return out


def parse_recipe(data: Mapping[str, Any], *, source: str = "") -> Recipe:
    """把一份已解析的 YAML 变成 `Recipe`；写错了抛 `RecipeError`。"""
    where = f"（{source}）" if source else ""
    if not isinstance(data, Mapping):
        raise RecipeError("bad_shape", f"配方顶层必须是映射{where}")

    unknown = set(data) - _RECIPE_KEYS
    if unknown:
        raise RecipeError("unknown_field", f"不认识的字段：{sorted(unknown)}{where}")

    name = _text(data.get("name"), field="name", limit=NAME_MAX_CHARS, allow_empty=False)
    bad = set(name.lower()) - _NAME_CHARS
    if bad:
        raise RecipeError("bad_name", f"name 只能用小写字母/数字/下划线，出现：{sorted(bad)}")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise RecipeError("no_steps", f"steps 必须是非空列表{where}")
    if len(raw_steps) > MAX_STEPS:
        raise RecipeError("too_many_steps", f"最多 {MAX_STEPS} 步，这条有 {len(raw_steps)} 步")

    steps: list[Step] = []
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, Mapping):
            raise RecipeError("bad_step", f"第 {index} 步必须是映射")
        declared_n = raw.get("n", index)
        if declared_n != index:
            raise RecipeError(
                "bad_step_number",
                f"第 {index} 步写了 n={declared_n!r}：步号必须从 1 起连续（不许跳号/重号）",
            )
        steps.append(_coerce_step(raw, index=index))

    title = _text(data.get("window_title"), field="window_title", limit=120)
    klass = _text(data.get("window_class"), field="window_class", limit=120)
    exe = _text(data.get("exe"), field="exe", limit=120)

    derived = _derived_effects(steps)
    if not (title or klass or exe) and derived & {"ui_control"}:
        raise RecipeError(
            "no_window",
            "有会动的步骤（click/type）却没写窗口标识（window_title/window_class/exe）："
            "事后无法确认点在了哪个窗口",
        )

    declared_raw = data.get("effects")
    if declared_raw is None:
        effects = tuple(sorted(derived))
    else:
        if not isinstance(declared_raw, list) or not declared_raw:
            raise RecipeError("bad_effects", "effects 必须是非空列表")
        declared = {str(item).strip() for item in declared_raw}
        illegal = declared - set(ALLOWED_EFFECTS)
        if illegal:
            raise RecipeError(
                "forbidden_effect",
                f"配方里不许出现 {sorted(illegal)}：发消息只能走出口注册表（R3）",
            )
        missing = derived - declared
        if missing:
            raise RecipeError(
                "effects_insufficient",
                f"步骤需要 {sorted(missing)}，但 effects 只写了 {sorted(declared)}",
            )
        effects = tuple(sorted(declared))

    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        raise RecipeError("bad_enabled", "enabled 只能是 true/false")

    version = data.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise RecipeError("bad_version", "version 必须是 ≥1 的整数")

    return Recipe(
        name=name,
        steps=tuple(steps),
        window_title=title,
        window_class=klass,
        exe=exe,
        effects=effects,
        enabled=enabled,
        version=version,
        note=_text(data.get("note"), field="note", limit=NOTE_MAX_CHARS),
    )


def load_recipe_file(path: str | Path) -> Recipe:
    """读一条配方文件。文件读不出、YAML 坏了、写错了——全抛 `RecipeError`。"""
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise RecipeError("unreadable", f"{p} 读不出来：{exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RecipeError("bad_yaml", f"{p.name} 不是合法 YAML：{exc}") from exc
    return parse_recipe(data, source=p.name)


def load_recipes(directory: str | Path = APP_DIR) -> dict[str, Recipe]:
    """加载一个目录里的全部配方（`_` 开头跳过）。写错的配方**不会**被静默略过。"""
    root = Path(directory)
    out: dict[str, Recipe] = {}
    if not root.is_dir():
        return out
    for p in sorted(root.glob("*.yaml")) + sorted(root.glob("*.yml")):
        if p.name.startswith("_"):
            continue
        recipe = load_recipe_file(p)
        if recipe.name in out:
            raise RecipeError("duplicate_name", f"两条配方都叫 {recipe.name}（{p.name}）")
        out[recipe.name] = recipe
    return out


def enabled_recipes(directory: str | Path = APP_DIR) -> tuple[Recipe, ...]:
    """人显式 `enabled: true` 的那些，按名字排序。"""
    return tuple(
        recipe for _, recipe in sorted(load_recipes(directory).items()) if recipe.enabled
    )


def describe(recipe: Recipe) -> str:
    """给日志/提示词用的一行摘要（不含正文，避免把用户输入带进日志）。"""
    window = recipe.window_title or recipe.window_class or recipe.exe or "-"
    return (
        f"{recipe.name} · {len(recipe.steps)} 步 · 窗口={window} · "
        f"effects={','.join(recipe.effects)} · {'启用' if recipe.enabled else '未启用'}"
    )
