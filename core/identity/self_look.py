"""她自己的样子：读资产包 + 判定一张图是不是她（L0 md5 / L1 dhash）。

设计（2026-10-26）：
- 卡片只写一行引用 `"looks": "<slug>"`，数据全在 `data/persona/assets/<slug>/faces/`。
  **本模块不出现任何角色名**——换一套图就是另一个角色，这是能开源给别人的前提。
- 分级判定，越前越便宜：L0 md5（原图、转存）→ L1 dhash（裁切/压缩/加水印）→
  L2 视觉抽槽位打分（尚未实现，返回 unknown，调用方保持原来的行为）。
- 永不抛：PIL 缺失、包不存在、图坏了，一律退回 unknown。loop 不该因为她没有形象包就崩。

为什么不用 CLIP 之类的图像 embedding：通用 CLIP 是自然图训练的，二次元角色之间挨得太近，
容易把别的蓝发角色认成她；而符号槽位（发色/鳍耳/鲸尾）可解释、能直接变成她说的话。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from core.assets import paths as _asset_paths  # 2026-10-26 资产分区

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "data" / "persona" / "assets"
CARDS = ROOT / "data" / "persona" / "cards"
ACTIVE = ROOT / "data" / "persona" / "active.json"

_ENV = "YINYUE_SELF_LOOK"
_OFF = {"0", "off", "false", "no", "none", "disable", "disabled"}
_log = logging.getLogger(__name__)


def enabled() -> bool:
    """`YINYUE_SELF_LOOK=off` 可整体关掉（缺省开）。"""
    try:
        return str(os.environ.get(_ENV) or "").strip().lower() not in _OFF
    except Exception:  # noqa: BLE001
        return True


def _read_json(path: Path) -> dict:
    # 走全库唯一读口：坏文件在那层备份 + 出声，这里不再自造一份「坏就当空」。
    from core.infra.jsonio import read_json

    got = read_json(path, default={})
    return got if isinstance(got, dict) else {}


def active_card() -> dict:
    """当前生效的人格卡（dict）；没有就空 dict。"""
    try:
        name = str(_read_json(ACTIVE).get("card") or "").strip()
        if not name:
            return {}
        return _read_json(CARDS / (name + ".json"))
    except Exception:  # noqa: BLE001
        return {}


def slug_of(card: dict | None = None) -> str:
    """卡片里 `looks` 指向的包名；没写就空串。"""
    try:
        card = card if isinstance(card, dict) else active_card()
        return str((card or {}).get("looks") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def faces_dir(slug: str = "") -> Path | None:
    try:
        slug = str(slug or "").strip() or slug_of()
        if not slug:
            return None
        d = _asset_paths.kind_dir(slug, "faces") or (ASSETS / slug / "faces")
        return d if d.is_dir() else None
    except Exception:  # noqa: BLE001
        return None


def load_pack(slug: str = "") -> dict:
    d = faces_dir(slug)
    return _read_json(d / "pack.json") if d else {}


def load_index(slug: str = "") -> dict:
    d = faces_dir(slug)
    return _read_json(d / "index.json") if d else {}


def canon_text(slug: str = "", *, budget: int = 400) -> str:
    """她的第一人称外貌自述；没有包就空串。"""
    d = faces_dir(slug)
    if not d:
        return ""
    try:
        text = (d / "canon.txt").read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        text = str(load_pack(slug).get("canon_text") or "").strip()
    text = " ".join(text.split())
    return text[:budget] if text else ""


def self_block(slug: str = "", *, budget: int = 400) -> str:
    """注入用的一整块：块头必须逐字在第一行（assembler 用 startswith 认块）。"""
    if not enabled():
        return ""
    text = canon_text(slug, budget=budget)
    return ("[我的样子] （这是我自己，不是别人给我定的）\n" + text) if text else ""


# ---- 低级：哈希 -------------------------------------------------------------

def md5_of(path: str | Path) -> str:
    try:
        h = hashlib.md5()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return ""


def dhash(path: str | Path, size: int = 8) -> str:
    """64 bit 差异哈希，只用 PIL；PIL 不在就空串（不抛）。"""
    try:
        from PIL import Image  # noqa: PLC0415 - 延迟导入：loop 侧不允许因缺 PIL 崩

        with Image.open(path) as im:
            small = im.convert("L").resize((size + 1, size), Image.LANCZOS)
        px = list(small.tobytes())  # L 模式每像素 1 字节
        bits = 0
        for row in range(size):
            base = row * (size + 1)
            for col in range(size):
                bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
        return "%016x" % bits
    except Exception:  # noqa: BLE001
        return ""


def _popcount(h: str) -> int:
    try:
        return bin(int(h, 16)).count("1")
    except Exception:  # noqa: BLE001
        return 0


def is_flat(path: str | Path, *, min_std: float = 6.0) -> bool:
    """纯色/纯白/极暗的图没有结构：dhash 会把它们算成同一个值，必须单独拦住。

    2026-10-26 踩过：两行 `Image.new("RGB", (96,96), 常数色)` 的 dhash 都是 0，
    汉明距离 0，于是一张纯白图片被判成"就是她"。这不是阈值问题，是这层判断
    对无结构图根本没有信息量——只能拒绝下判断。
    """
    try:
        from PIL import Image, ImageStat  # noqa: PLC0415

        with Image.open(path) as im:
            std = ImageStat.Stat(im.convert("L")).stddev[0]
        return float(std) < float(min_std)
    except Exception:  # noqa: BLE001
        return False


def mean_rgb(path: str | Path) -> tuple[float, float, float] | None:
    """整图平均色（缩到 8×8 再求，够用且快）。

    dHash 只看灰度结构、不看颜色：两张排版一样但配色不同的图会撞。
    2026-10-26 踩过：同款渐变+圆点、只换底色（蓝→绿）的图，汉明距离 0。
    所以 dHash 命中后必须再过一道颜色闸门。
    """
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(path) as im:
            small = im.convert("RGB").resize((8, 8))
            px = list(small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata())
        n = max(1, len(px))
        return (sum(p[0] for p in px) / n, sum(p[1] for p in px) / n, sum(p[2] for p in px) / n)
    except Exception:  # noqa: BLE001
        return None


def rgb_dist(a, b) -> float:
    """两色的平均通道差（0–255 尺度）。"""
    if not a or not b:
        return 999.0
    return (abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])) / 3.0


def ham(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:  # noqa: BLE001
        return 64


# ---- 判定 -------------------------------------------------------------------

def _slot_terms(pack: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, spec in (pack.get("slots") or {}).items():
        words = [str(w) for w in (spec.get("terms") or []) if str(w).strip()] if isinstance(spec, dict) else []
        if words:
            out[str(name)] = words
    return out


def _slot_weight(pack: dict, name: str) -> float:
    spec = (pack.get("slots") or {}).get(name)
    if isinstance(spec, dict):
        try:
            return float(spec.get("weight") or 1)
        except Exception:  # noqa: BLE001
            return 1.0
    return 1.0


def describe_hint(slug: str = "") -> str:
    """让写图片描述的人**顺手写清辨识特征**的一段话。

    为什么要这段：判定靠描述里的词（`judge_text`），可历史描述常常只写"蓝发角色"，
    于是自己的图也认不出来（2026-10-26：那张 12:54 的图就被判 unknown）。
    与其事后补一次视觉调用，不如在**本来就要写的那一次**里把槽位提示进 prompt，
    成本为零。这里只描述"看得见什么"，绝不让她猜"这是不是她"。
    """
    try:
        if not enabled():
            return ""
        pack = load_pack(slug)
        if not pack:
            return ""
        slots = pack.get("slots") or {}
        parts: list[str] = []
        for name, spec in slots.items():
            if not isinstance(spec, dict):
                continue
            hint = str(spec.get("hint") or "").strip()
            if hint:
                parts.append(hint)
        if not parts:
            return ""
        return "；".join(parts[:8])
    except Exception as exc:  # noqa: BLE001
        _log.debug("self_look_describe_hint_failed", extra={"err": repr(exc)})
        return ""


def judge_text(text: str, *, slug: str = "") -> dict:
    """从**一段文字描述**判断"这是不是我"。

    为什么不直接调视觉：`message_media.description` 已经是视觉模型写完的，
    每张图再问一次等于把成本翻倍。描述里"蓝发/女仆装/鲸鱼娘/呆毛"这些词
    本来就在，用它们打分是免费的，而且对已经存下来的几百张历史图同样有效。
    视觉抽槽位留给建包（P3），运行时不花这笔钱。

    返回 {"verdict": "self"|"maybe"|"unknown", "score": float, "hits": {slot: [词]},
          "vetoed": bool, "why": str}
    """
    out = {"verdict": "unknown", "score": 0.0, "hits": {}, "vetoed": False, "why": ""}
    try:
        if not enabled():
            out["why"] = "disabled"
            return out
        pack = load_pack(slug)
        if not pack:
            out["why"] = "no_pack"
            return out
        low = str(text or "").lower()
        if not low.strip():
            out["why"] = "empty"
            return out

        for word in [str(w) for w in (pack.get("veto_terms") or []) if str(w).strip()]:
            if word.lower() in low:
                out.update(verdict="unknown", vetoed=True, why="veto:%s" % word)
                return out

        hits: dict[str, list[str]] = {}
        weight = 0.0
        for name, words in _slot_terms(pack).items():
            got = [w for w in words if w.lower() in low]
            if got:
                hits[name] = got
                weight += _slot_weight(pack, name)

        scale = float((pack.get("thresholds") or {}).get("text_scale") or 6.0)
        th = pack.get("thresholds") or {}
        self_th = float(th.get("text_self") or 0.7)
        maybe_th = float(th.get("text_maybe") or 0.45)
        score = round(min(1.0, weight / max(1.0, scale)), 4)
        out["hits"] = hits
        out["score"] = score

        # 光"发色"不够（满大街都是某种发色）。要两个独立证据之一：
        # ① 命中被标了 identity 的槽位（鳍耳/鲸尾/鲸鱼这类身份指纹）
        # ② 同时命中两个以上不同槽位（发色 + 衣服这种组合）
        slots = pack.get("slots") or {}
        fingerprint = any(bool((slots.get(k) or {}).get("identity")) for k in hits)
        paired = len(hits) >= 2
        if not (fingerprint or paired):
            out["why"] = "no_identity_evidence hits=%s" % ",".join(sorted(hits))
            return out

        # 精度优先：只有命中身份指纹才敢说"是我"。
        # 2026-10-26 实测教训：只靠"蓝发+蓝眼"这种通用组合，会把别的蓝发角色
        # （戴绿帽/女巫帽的像素小人、拼豆挂件）全认成她 —— 37 个 self 里大半是假阳性。
        if fingerprint and score >= self_th:
            out["verdict"] = "self"
        elif score >= maybe_th:
            out["verdict"] = "maybe"
        out["why"] = "score %.2f fingerprint=%s hits=%s" % (score, fingerprint, ",".join(sorted(hits)))
        return out
    except Exception as exc:  # noqa: BLE001
        _log.debug("self_look_judge_text_failed", extra={"err": repr(exc)})
        out["why"] = "error"
        return out


def match_self(path: str | Path, *, slug: str = "") -> dict:
    """这张图是不是她。

    返回 {"is_self": "self"|"unknown", "level": "md5"|"dhash"|"none",
          "score": float, "why": str, "slug": str, "file": str}
    unknown = 不是"否定"，是"我这层判断不了"——调用方应保持原来的行为。
    """
    out = {"is_self": "unknown", "level": "none", "score": 0.0, "why": "", "slug": str(slug or ""), "file": ""}
    try:
        if not enabled():
            out["why"] = "disabled"
            return out
        target = str(path or "").strip()
        if not target or not Path(target).is_file():
            out["why"] = "no_file"
            return out
        pack = load_pack(slug)
        index = load_index(slug)
        if not pack or not index:
            out["why"] = "no_pack"
            return out
        out["slug"] = str(index.get("slug") or slug or "")
        images = [x for x in (index.get("images") or []) if isinstance(x, dict)]

        digest = md5_of(target)
        if digest:
            for item in images:
                if str(item.get("md5") or "") == digest:
                    out.update(is_self="self", level="md5", score=1.0,
                               why="md5 命中 %s" % item.get("file"), file=str(item.get("file")))
                    return out

        limit = int(((pack.get("thresholds") or {}).get("dhash_max_distance")) or 6)
        mine = dhash(target)
        if mine:
            pop = _popcount(mine)
            if is_flat(target) or pop <= 2 or pop >= 62:
                out["why"] = "low_detail"
                return out
            color_limit = float(((pack.get("thresholds") or {}).get("color_max_distance")) or 24)
            mine_rgb = mean_rgb(target)
            best = None
            color_blocked = ""
            for item in images:
                other = str(item.get("dhash") or "")
                if not other:
                    continue
                other_pop = _popcount(other)
                if other_pop <= 2 or other_pop >= 62:
                    continue
                dist = ham(mine, other)
                if best is None or dist < best[0]:
                    best = (dist, str(item.get("file")))
                if dist <= limit:
                    other_file = faces_dir(out["slug"]) / "gallery" / str(item.get("file"))
                    cdist = rgb_dist(mine_rgb, mean_rgb(other_file))
                    if cdist > color_limit:
                        color_blocked = "%s 配色差 %.0f > %.0f" % (item.get("file"), cdist, color_limit)
                        continue
                    out.update(is_self="self", level="dhash",
                               score=round(1.0 - dist / 64.0, 4),
                               why="dhash 距离 %d ≤ %d，最近 %s" % (dist, limit, item.get("file")),
                               file=str(item.get("file")))
                    return out
            if color_blocked:
                out["why"] = "color_mismatch: " + color_blocked
            elif best:
                out["why"] = "dhash 最近 %s（距离 %s）" % (best[1], best[0])
            else:
                out["why"] = "no_dhash"
            return out
        out["why"] = "no_dhash"
        return out
    except Exception as exc:  # noqa: BLE001 - 判定失败不许影响说话
        _log.debug("self_look_match_failed", extra={"err": repr(exc)})
        out["why"] = "error"
        return out


def is_self_image(path: str | Path, *, slug: str = "") -> bool:
    return match_self(path, slug=slug).get("is_self") == "self"
