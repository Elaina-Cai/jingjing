"""自定义服务商：往 `configs/llm.toml` 里加 `[endpoints.<名字>]`。

为什么要走 TOML 而不是我自己另存一份：
`llm/config.py::load_llm_config()` 的**唯一真源**就是它（内置映射 + TOML 覆盖），
路由器的 endpoints / capabilities / 默认链都从那里读。所以"自由填供应商"=
往这张表里写一节，重启后生效；写别处等于自己发明第二套配置，迟早打架。

约定：
- `api_key` 落 `.env`（`<NAME>_API_KEY`），TOML 里只写 `api_key_env`（不把 key 写进版本库）；
- 写入是**按块替换**：同名覆盖、不同名追加，注释与别的端点原样保留；
- 名字统一小写、只允许 [a-z0-9_-]。
"""
from __future__ import annotations

import os
import re
from core.infra.envflags import text
from core.infra.jsonio import atomic_write_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUILTIN = {"deepseek", "dashscope", "siliconflow", "openai", "moonshot", "ollama", "custom"}
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


KINDS = ("openai_compat", "anthropic")


def _env_write(pairs: dict) -> None:
    """把关键值写进 .env（原来借 web/routes_service._env_write；web 已下线）。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    lines = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception:  # noqa: BLE001
        lines = []
    seen = set()
    for i, ln in enumerate(lines):
        t = ln.strip()
        if t.startswith("#") or "=" not in t:
            continue
        if t.split("=", 1)[0].strip() in pairs:
            k = t.split("=", 1)[0].strip()
            lines[i] = "%s=%s" % (k, pairs[k])
            seen.add(k)
    for k, v in pairs.items():
        if k not in seen:
            lines.append("%s=%s" % (k, v))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def toml_path() -> str:
    env = text("YINYUE_LLM_CONFIG")
    if env:
        return env if os.path.isabs(env) else os.path.join(ROOT, env)
    try:
        from core.config.settings import config as _cfg
        got = getattr(getattr(_cfg, "llm", None), "llm_config_path", "") or ""
    except Exception:  # noqa: BLE001
        got = ""
    return os.path.join(ROOT, got or "configs/llm.toml")


def _read() -> str:
    p = toml_path()
    if not os.path.exists(p):
        return ""
    return open(p, encoding="utf-8").read()


def list_custom() -> list:
    """TOML 里定义过的端点（含内置的同名覆盖），带 base_url / model / 能力。"""
    txt = _read()
    if not txt:
        return []
    try:
        import tomllib
        raw = tomllib.loads(txt)
    except Exception:  # noqa: BLE001 - 写坏了也不该让设置页炸
        return []
    out = []
    for name, data in sorted((raw.get("endpoints") or {}).items()):
        if not isinstance(data, dict):
            continue
        caps = data.get("capabilities") or {}
        out.append({
            "id": name,
            "builtin": (name in _BUILTIN) and (data.get("origin") != "ui"),
            "kind": data.get("kind") or "openai_compat",
            "base_url": data.get("base_url") or "",
            "model": data.get("model") or "",
            "api_key_env": data.get("api_key_env") or "",
            "tools": bool(caps.get("tools", True)),
            "vision": bool(caps.get("vision", False)),
            "context_window": caps.get("context_window"),
        })
    return out


def _block(name: str, base_url: str, model: str, api_key_env: str, tools: bool, vision: bool,
           kind: str = "openai_compat") -> str:
    # origin 标明"这条是设置页建的"。以前靠"名字是否属于内置"区分来源，
    # 结果用户用内置名（deepseek）建卡时会被当成内置、界面上看不到卡片。
    return (
        "[endpoints.%s]\n"
        'kind = "%s"\n'
        'base_url = "%s"\n'
        'model = "%s"\n'
        'api_key_env = "%s"\n'
        'origin = "ui"\n'
        "\n"
        "[endpoints.%s.capabilities]\n"
        "tools = %s\n"
        "vision = %s\n"
    ) % (name, kind, base_url, model, api_key_env, name,
         "true" if tools else "false", "true" if vision else "false")

def _strip_block(txt: str, name: str) -> str:
    """删掉 `[endpoints.<name>]` 与它的子表（到下一个 `[` 头为止）。"""
    out, skipping = [], False
    for line in txt.split("\n"):
        head = line.strip()
        if head.startswith("[") and head.endswith("]"):
            target = head[1:-1].strip()
            skipping = target == ("endpoints.%s" % name) or target.startswith("endpoints.%s." % name)
        if not skipping:
            out.append(line)
    return "\n".join(out)


def upsert(name: str, *, base_url: str, model: str, api_key: str = "",
           tools: bool = True, vision: bool = False,
           kind: str = "openai_compat") -> dict:
    """加/改一个服务商。key 写 .env（`<NAME>_API_KEY`），TOML 只留变量名。"""
    name = (name or "").strip().lower()
    kind = (kind or "openai_compat").strip()
    if kind not in KINDS:
        return {"ok": False, "error": "不支持的协议：%s" % kind}
    if not _NAME_RE.match(name):
        return {"ok": False, "error": "名字只能用 a-z 0-9 _ -，2–32 位"}
    if not (base_url or "").strip() or not (model or "").strip():
        return {"ok": False, "error": "base_url 和 model 都要填"}
    # 2026-10-25：允许与内置同名（用户想用 deepseek 这个名字完全合理）。
    # 区分方式从"名字是不是内置的"改成块里显式的 origin 标记，见 _block()。
    key_env = "%s_API_KEY" % re.sub(r"[^A-Z0-9]", "_", name.upper())  # 环境变量名不能带连字符
    key_note = ""
    if (api_key or "").strip():
        try:
            # _env_write 收的是 dict（{KEY: VALUE}）；以前传成两个位置参数，
            # TypeError 被 except 吞掉，key 一直没写进 .env，卡片建好了却"不会说话"。
            _env_write({key_env: api_key.strip()})
        except Exception as exc:  # noqa: BLE001 - 写 .env 失败不阻断建卡，但要如实回报
            key_note = "（key 没写进 .env：%s）" % exc
    p = toml_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    txt = _read()
    if not txt.strip():
        txt = ("# 2026-10-25 由设置页「自定义服务商」维护\n"
               "# 每个 [endpoints.<名字>] 就是一个可选服务商；key 在 .env 里，这里只写变量名\n\n")
    txt = _strip_block(txt, name).rstrip("\n") + "\n\n" + _block(
        name, base_url.strip().rstrip("/"), model.strip(), key_env, tools, vision, kind=kind)
    if not atomic_write_text(p, txt):
        raise OSError("写不进 %s（被占用或磁盘问题）" % p)
    return {"ok": True, "id": name, "api_key_env": key_env, "path": p, "note": key_note}


def remove(name: str) -> dict:
    name = (name or "").strip().lower()
    p = toml_path()
    txt = _read()
    if not txt:
        return {"ok": False, "error": "还没有 llm.toml"}
    new = _strip_block(txt, name).rstrip("\n") + "\n"
    if new == txt:
        return {"ok": False, "error": "没找到 %s" % name}
    open(p, "w", encoding="utf-8", newline="\n").write(new)
    return {"ok": True, "id": name}
