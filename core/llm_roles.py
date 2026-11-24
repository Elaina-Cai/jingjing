"""按角色绑定模型（chat / summary / vision / embedding）。

对标 coding agent 的做法：模型不是一个全局变量，而是**一张表**——
主对话用哪个、摘要用哪个、看图用哪个，各自可以不同。
这里只存"选哪个"（provider + model），不存 key、不存 base_url：
key 和地址仍由 `.env` + 设置页那张「模型」卡管，免得两处打架。

文件：`data/llm_roles.json`
    {"chat": {"provider": "deepseek", "model": "deepseek-chat", "fallback": []},
     "summary": {...}, "vision": {...}, "embedding": {"note": "走 EMBEDDING_*"}}

设计纪律：
- 读失败/文件没有 → 返回空表，让调用方退回原有路由，绝不因为这张表把模型弄坏；
- 2 秒缓存（每拍都会读），保存后立刻失效，所以"改完就生效"，不用重启。
"""
from __future__ import annotations

import json
import os
import time
from core.infra.jsonio import atomic_write_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "llm_roles.json")

ROLES = ("chat", "summary", "vision")
ROLE_LABELS = {
    "chat": "说话（主对话）",
    "summary": "摘要（小本本 / 压缩）",
    "vision": "看图",
}

_TTL_S = 2.0
_CACHE = {"at": 0.0, "cfg": {}}


def _blank_role():
    return {"provider": "", "model": "", "fallback": []}


def load(force: bool = False) -> dict:
    """读这张表；坏文件当空表，绝不抛。"""
    now = time.time()
    if not force and (now - _CACHE["at"]) < _TTL_S:
        return _CACHE["cfg"]
    cfg = {}
    try:
        if os.path.exists(PATH):
            raw = json.load(open(PATH, encoding="utf-8"))
            if isinstance(raw, dict):
                for role in ROLES:
                    item = raw.get(role)
                    if isinstance(item, dict):
                        cfg[role] = {
                            "provider": str(item.get("provider") or "").strip(),
                            "model": str(item.get("model") or "").strip(),
                            "fallback": [
                                {"provider": str(f.get("provider") or "").strip(),
                                 "model": str(f.get("model") or "").strip()}
                                for f in (item.get("fallback") or [])
                                if isinstance(f, dict) and str(f.get("provider") or "").strip()
                            ],
                        }
    except Exception:  # noqa: BLE001 - 这张表坏了不该影响她说话
        cfg = {}
    _CACHE["at"] = now
    _CACHE["cfg"] = cfg
    return cfg


def save(cfg: dict) -> dict:
    """写表（只保留白名单字段），写完立即失效缓存。"""
    out = {}
    for role in ROLES:
        item = (cfg or {}).get(role) or {}
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip()
        model = str(item.get("model") or "").strip()
        if not provider and not model:
            continue
        out[role] = {
            "provider": provider,
            "model": model,
            "fallback": [
                {"provider": str(f.get("provider") or "").strip(),
                 "model": str(f.get("model") or "").strip()}
                for f in (item.get("fallback") or [])
                if isinstance(f, dict) and str(f.get("provider") or "").strip()
            ][:2],
        }
    atomic_write_json(PATH, out, indent=2)
    _CACHE["at"] = 0.0
    _CACHE["cfg"] = out
    return out


def override_for(role: str, cfg: dict | None = None) -> dict:
    """该角色指定的 {provider, model, fallback}；没配就返回空 dict。"""
    if not role:
        return {}
    return ((cfg if cfg is not None else load()) or {}).get(role) or {}


def role_for_call(*, kind: str = "", agent: str = "", task: str = "") -> str:
    """一次调用属于哪个角色。宁可粗，不要猜错——猜错就等于用错模型。"""
    k = (kind or "").lower()
    a = (agent or "").lower()
    t = (task or "").lower()
    if k == "vision":
        return "vision"
    if k in ("embedding", "embed"):
        return "embedding"
    if "vision" in k or "vision" in t:
        return "vision"
    if any(word in a for word in ("histor", "summar", "compact", "memo")) or \
       any(word in t for word in ("summar", "compact", "digest")):
        return "summary"
    return "chat"


def describe(providers=None) -> list:
    """给前端的行：角色 / 标签 / 当前选择 / 兜底。"""
    cfg = load(force=True)
    rows = []
    for role in ROLES:
        item = cfg.get(role) or _blank_role()
        rows.append({
            "role": role,
            "label": ROLE_LABELS.get(role, role),
            "provider": item.get("provider", ""),
            "model": item.get("model", ""),
            "fallback": item.get("fallback", []),
            "note": "这一项仍走 .env 的 EMBEDDING_* 配置" if role == "embedding" else "",
        })
    return rows
