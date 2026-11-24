"""声明式工具（`configs/tools/*.yaml`）：加一个能力 = 加一个文件。

只测四件事：**不合规的一律不装**、**白名单真的拦得住**、**HTTP 状态认得清**、
**默认不启用**。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.tools import declarative as dc  # noqa: E402
from core.tools.registry import Effect, ToolRegistry  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：mkdtemp 不给 dir= 时系统 TEMP 不可写会静默回落到仓库根。
from _tempdirs import make_db_dir  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        FAILED.append(name)
        print(f"FAIL {name} {detail}".rstrip())


GOOD = """name: demo_tool
family: web
effects: [read]
args: {city: str}
returns: 一行结果
limits: {timeout_s: 3, max_chars: 20}
allow_hosts: [example.com]
http:
  method: GET
  url: "https://example.com/q?city={city}"
enabled: false
"""


def write(tmp: Path, name: str, body: str) -> Path:
    p = tmp / name
    p.parent.mkdir(parents=True, exist_ok=True)
    io.open(p, "w", encoding="utf-8", newline="\n").write(body)
    return p


class Reply:
    def __init__(self, status=200, text="", error=""):
        self.status = status
        self.text = text
        self.error = error


def fake(status=200, text="晴 20C", error=""):
    seen = []

    def _get(url, *, timeout_s=8, headers=None):
        seen.append((url, timeout_s))
        return Reply(status, text, error)

    return _get, seen


def main() -> int:
    src = io.open(str(ROOT / "core/tools/declarative.py"), encoding="utf-8").read()

    tmp = make_db_dir("decl_tools_")
    good = write(tmp, "demo.yaml", GOOD)

    # ── 装得出来：七字段齐、owner 指向文件、默认不启用 ──────────────────
    getter, seen = fake()
    tool = dc.load_tool_file(good, http_get=getter)
    check("名称/family/returns", tool.name == "demo_tool"
          and tool.spec.family == "web" and tool.spec.returns == "一行结果")
    check("效果只有 read", tool.spec.effects == frozenset({Effect.READ}), str(tool.spec.effects))
    check("参数与上限", dict(tool.spec.args) == {"city": "str"}
          and tool.spec.limits["timeout_s"] == 3.0 and tool.spec.limits["max_chars"] == 20)
    check("默认不启用", tool.spec.enabled is False)
    check("owner 指向配置文件", "demo.yaml" in tool.spec.owner and tool.spec.owner.startswith("YAML:"),
          tool.spec.owner)
    check("enabled_in_file 读文件", tool.enabled_in_file is False)

    # ── 调得通：占位符、状态码、截断、缺参 ────────────────────────────
    got = tool.handler(what="杭州")
    check("占位符替换 + 引用", got.status == "ok" and seen[-1][0] == "https://example.com/q?city=%E6%9D%AD%E5%B7%9E",
          str(seen[-1:]))
    check("source 是 URL、at 有时间", got.source.startswith("https://example.com") and bool(got.at))
    check("超时透传", seen[-1][1] == 3.0, str(seen[-1]))

    getter, seen = fake(text="x" * 100)
    tool2 = dc.load_tool_file(good, http_get=getter)
    got = tool2.handler(what="杭州")
    check("超过 max_chars 截断并标 truncated",
          got.truncated is True and len(got.content) == 20, str(len(got.content)))

    getter, _ = fake(text="")
    got = dc.load_tool_file(good, http_get=getter).handler(what="杭州")
    check("200 但正文空 = no_result（不是 ok）", got.status == "no_result", got.status)

    getter, _ = fake(status=500)
    check("500 → http_5xx", dc.load_tool_file(good, http_get=getter).handler(what="x").status == "http_5xx")
    getter, _ = fake(status=404)
    check("404 → http_4xx", dc.load_tool_file(good, http_get=getter).handler(what="x").status == "http_4xx")
    getter, _ = fake(status=0, error="timeout: URLError")
    check("连不上 → timeout", dc.load_tool_file(good, http_get=getter).handler(what="x").status == "timeout")

    def boom(url, *, timeout_s=8, headers=None):
        raise RuntimeError("炸了")

    check("取数器抛错不冒泡", dc.load_tool_file(good, http_get=boom).handler(what="x").status == "timeout")

    getter, seen = fake()
    got = dc.load_tool_file(good, http_get=getter).handler(params={})
    check("缺参数 → no_result 且不发请求", got.status == "no_result" and "city" in got.content and seen == [],
          f"{got.status} {seen}")

    # ── 不合规的一律不装 ───────────────────────────────────────────
    def reject(name: str, body: str, reason: str) -> None:
        p = write(tmp, name, body)
        try:
            dc.load_tool_file(p, http_get=fake()[0])
            check(f"拒绝：{reason}", False, "没抛")
        except dc.DeclarativeToolError as exc:
            check(f"拒绝：{reason}", exc.reason == reason, f"实际 {exc.reason}")

    reject("ext.yaml", GOOD.replace("effects: [read]", "effects: [external_send]"), "forbidden_effect")
    reject("ui.yaml", GOOD.replace("effects: [read]", "effects: [ui_control]"), "forbidden_effect")
    reject("weird.yaml", GOOD.replace("effects: [read]", "effects: [telepathy]"), "unknown_effect")
    reject("nohost.yaml", GOOD.replace("allow_hosts: [example.com]", "allow_hosts: []"), "no_allow_hosts")
    reject("dynhost.yaml", GOOD.replace('url: "https://example.com/q?city={city}"',
                                        'url: "https://{city}.example.com/q"'), "dynamic_host")
    reject("hostmismatch.yaml", GOOD.replace("allow_hosts: [example.com]", "allow_hosts: [other.com]"),
           "host_not_allowed")
    reject("post.yaml", GOOD.replace("method: GET", "method: POST"), "forbidden_method")
    reject("ftp.yaml", GOOD.replace('url: "https://example.com/q?city={city}"',
                                    'url: "ftp://example.com/q?city={city}"'), "bad_scheme")
    reject("noname.yaml", GOOD.replace("name: demo_tool", "name: ''"), "no_name")
    reject("noret.yaml", GOOD.replace("returns: 一行结果", "returns: ''"), "no_returns")

    # ── 装载目录：坏文件跳过，`_` 开头的示例不装 ─────────────────────────
    write(tmp, "bad.yaml", "name: [1,2,3]\n")
    write(tmp, "_doc.yaml", GOOD)
    tools = dc.load_declared_tools(tmp, http_get=fake()[0])
    names = [t.name for t in tools]
    check("目录装载：只装合法的、跳过坏文件与 _ 示例", names == ["demo_tool"], str(names))

    # ── 注册：默认只登记不启用；文件 enabled: true + enable=True 才启用 ──
    reg = ToolRegistry()
    bound = dc.bind_declared(reg, tmp, enable=False, http_get=fake()[0])
    check("登记上了", bound == ("demo_tool",), str(bound))
    check("默认不启用（连 prompt 都进不去）",
          reg.check_call("demo_tool", {}, turn_id="t1")[0] is False)
    check("查不到就 not_enabled", reg.check_call("demo_tool", {}, turn_id="t1")[1] == "not_enabled",
          str(reg.check_call("demo_tool", {}, turn_id="t1")))

    # 单独一个目录，避免与 demo.yaml 同名（同名第二个会以 bad_spec 被拒）
    tmp_yes = make_db_dir("decl_yes_")
    write(tmp_yes, "yes.yaml", GOOD.replace("enabled: false", "enabled: true"))

    reg2 = ToolRegistry()
    dc.bind_declared(reg2, tmp_yes, enable=False, http_get=fake()[0])
    check("文件 enabled 但调用方 enable=False：仍不启用",
          reg2.check_call("demo_tool", {}, turn_id="t1")[0] is False)

    reg3 = ToolRegistry()
    dc.bind_declared(reg3, tmp_yes, enable=True, http_get=fake()[0])
    check("两边都同意才启用", reg3.check_call("demo_tool", {}, turn_id="t1")[0] is True,
          str(reg3.check_call("demo_tool", {}, turn_id="t1")))

    reg4 = ToolRegistry()
    dc.bind_declared(reg4, tmp, enable=True, http_get=fake()[0])
    check("文件没写 enabled：调用方 enable=True 也不启用（授权必须写在文件里）",
          reg4.check_call("demo_tool", {}, turn_id="t1")[1] == "not_enabled",
          str(reg4.check_call("demo_tool", {}, turn_id="t1")))

    # ── 结构纪律 ───────────────────────────────────────────────────
    check("静态：没有 shell / eval / exec", "os.system" not in src and "subprocess" not in src
          and "eval(" not in src and "exec(" not in src)
    check("静态：external_send 与 ui_control 不在白名单里",
          "Effect.EXTERNAL_SEND" not in src and "Effect.UI_CONTROL" not in src)
    check("静态：默认目录是 configs/tools", 'TOOL_DIR = "configs/tools"' in src)

    print(f"RESULT {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
