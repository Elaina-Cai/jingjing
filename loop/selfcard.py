"""她的两面：判断面（决定要不要开口）与表达面（决定怎么开口）。

同一份"我"，按场合激活不同的那部分。原来的做法是把两千字人格整张塞给生成器，
结果判的时候带着一堆称呼规矩，说的时候又缺判断所需的取舍——两面都不够用。
"""

from __future__ import annotations

SCENES = {
    "group": "在场的有外人，我说话会被很多人看见",
    "private": "只有我和对方，没有第三个人看着",
    "mixed": "一屏里既有群也有私聊",
}


# ---------------------------------------------------------------------------
# 卡片式人设（2026-10-24 起）：data/persona/active.json 指向一张卡时，
# 判断面/表达面都由那张卡渲染；没有 active 卡就退回硬编码的鲸鲸（向后兼容）。
# ---------------------------------------------------------------------------
import json as _json
from contextlib import contextmanager as _contextmanager
from pathlib import Path as _Path
from . import cards as _cards
try:
    from .safelog import warn as _warn
except Exception:  # noqa: silent(safelog 缺失时降级为静默，避免 import 期崩溃)
    def _warn(*a, **k):
        return None

_ROOT = _Path(__file__).resolve().parents[1]
_ACTIVE = _ROOT / "data" / "persona" / "active.json"
_CARDS = _ROOT / "data" / "persona" / "cards"
# 2026-10-28（CB-069）：卡的形状由 `core/infra/textfile.py` 的契约定义，**上限只写在那一处**。
# 以前这里是「20000 字上限」+ 读到超限就裁尾巴：卡长过 2 万字，她读到的是腰斩的卡，
# 日志一条 warn，没人知道。现在读侧不裁、只在有问题时喊；超限由 `scripts/check.py` 拦在改完必跑里。
from core.infra import textfile  # noqa: E402 - 保持卡片段的位置不动

CARD = textfile.Contract(
    name="人设卡",
    max_bytes=60000,                      # ≈2 万个汉字，和原来 20,000 字的预算等价，改成按字节量
    head_keys=("name", "voice", "looks"),  # 别的程序靠这三行绑声线/资产包
    note="整块注入；程序只认头部三行",
)


def _parse_md(text):
    """把一个 .md 卡切成 (头部键值, 正文)。

    头部是第一段 `---` 之前的内容，一行一个 `键: 值`；不认识的键忽略（不是字段系统，
    只是 name/voice/looks 三个指针让别的模块还能拿到）。
    """
    if "\n---" not in "\n" + text:
        return {}, text.strip()
    head, body = ("\n" + text).split("\n---", 1)
    meta = {}
    for line in head.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip().lower()] = v.strip()
    return meta, body.strip()


def active_name():
    """数据 persona/active.json 里写的卡名；没有就空串。"""
    try:
        if not _ACTIVE.exists():
            return ""
        return str(_json.loads(_ACTIVE.read_text(encoding="utf-8")).get("card") or "")
    except Exception as exc:  # noqa: BLE001
        _warn("selfcard:active_name_failed", err=repr(exc))
        return ""


# 测试接缝（2026-11-17 表达改造）：只在显式 with 块里替换 card_text() 的返回值，
# 供隔离评测注入候选卡；生产路径从不写这个字典。
_CARD_OVERRIDE: dict = {}


@_contextmanager
def override_card_text(text: str):
    """临时把 card_text()/card_sections() 换成给定正文（候选卡评测用）。"""
    had = "text" in _CARD_OVERRIDE
    prev = _CARD_OVERRIDE.get("text", "")
    _CARD_OVERRIDE["text"] = str(text or "")
    try:
        yield
    finally:
        if had:
            _CARD_OVERRIDE["text"] = prev
        else:
            _CARD_OVERRIDE.pop("text", None)


def card_text():
    """当前生效卡的正文（一整块，原样）；没有 .md 卡就返回 ""。

    2026-10-27 用户要求：整张卡就是一块提示词，不拆分类别、不做归类，
    结构完全由写卡的人自己排版决定。所以这里除了字数软上限之外什么都不做。
    """
    if "text" in _CARD_OVERRIDE:
        return _CARD_OVERRIDE["text"]
    name = active_name()
    if not name:
        return ""
    f = _CARDS / (name + ".md")
    if not f.exists():
        return ""
    try:
        raw = f.read_text(encoding="utf-8")
        _, body = _parse_md(raw)
    except Exception as exc:  # noqa: BLE001 - 卡写坏了退回旧 json，不能让她没人格
        _warn("selfcard:card_md_failed", name=name, err=repr(exc))
        return ""

    problems = textfile.blockers(raw, CARD)
    if problems:
        # 不裁切：裁切会让"卡坏了"变成看不见的事实，她还会继续按腰斩的卡说话。
        # 真超限/缺头的卡会被 scripts/check.py 判红（改完必跑），这里只负责喊。
        _warn("selfcard:card_contract", name=name, problems="；".join(problems))
    return body


SECTIONS = ("我是谁", "样本", "我不知道", "怎么说")

# 2026-10-30（施工 Step 6）：卡里已经在用的标题照旧生效 —— 改代码不改她的卡。
# 只给**已经在写卡的人手上**的写法做别名，不做模糊匹配（别名表越长，越容易把
# 一段本该留在身份里的文字挪到别处）。
ALIASES = {
    "我是谁": ("我是谁",),
    "样本": ("样本", "场景参照"),      # 「场景参照（照这个语感说话…）」= 风格样本
    "我不知道": ("我不知道",),
    "怎么说": ("怎么说", "我怎么说话"),  # 「我怎么说话」= 输出形状那一段
}


def _split_sections(body: str) -> dict:
    """按 `## <标题>` 把正文切成几块；没有这几个标题就整块算「我是谁」。

    约定只有一条：正文里用 `## 我是谁` / `## 样本` / `## 我不知道` / `## 怎么说`
    分节。没写标题的卡照旧整块用（与 ``card_text()`` 逐字相同）。
    不认识的标题不丢内容：它后面的行继续算在**当前**这一节里 —— 宁可多留，
    也不要因为写了个别名标题就让她的记忆凭空少一截。
    """
    out: dict = {}
    cur = ""
    for line in str(body or "").splitlines():
        head = line.strip()
        title = head[3:].strip() if head.startswith("## ") else ""
        hit = ""
        for name, names in ALIASES.items():
            if title and any(title == a or title.startswith(a) for a in names):
                hit = name
                break
        if hit:
            cur = hit
            out.setdefault(cur, "")
            continue
        if cur:
            out[cur] = (out[cur] + "\n" + line) if out[cur] else line
        elif head:
            out["我是谁"] = (out.get("我是谁", "") + "\n" + line).strip() if out.get("我是谁") else line
    if not out:
        text = str(body or "").strip()
        return {"我是谁": text} if text else {}
    for k, v in list(out.items()):
        out[k] = v.strip()
    return out


def card_sections() -> dict:
    """当前生效卡的各节（见 ``_split_sections``）；没有卡就返回空字典。"""
    body = card_text()
    return _split_sections(body) if body else {}


def active_card():
    """返回当前生效的卡（dict）；没有就 None。"""
    try:
        if not _ACTIVE.exists():
            return None
        name = _json.loads(_ACTIVE.read_text(encoding="utf-8")).get("card") or ""
        if not name:
            return None
        md = _CARDS / (name + ".md")
        if md.exists():
            meta, _body = _parse_md(md.read_text(encoding="utf-8"))
            return {"name": meta.get("name") or name,
                    "voice": meta.get("voice") or "",
                    "looks": meta.get("looks") or "",
                    "_md": True}
        f = _CARDS / (name + ".json")
        if not f.exists():
            _warn("selfcard:active_card_missing", name=name)
            return None
        return _json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 卡坏了必须退回硬编码，不能让她没人格
        _warn("selfcard:active_card_failed", err=repr(exc))
        return None


NO_CARD = ("我还没有人格卡。先按最朴素的方式说话：不装、不编、不越界；"
           "拿不准就说不确定。")


def judgment(scene=""):
    card = active_card()
    if card:
        judge, _ = _cards.preview(card, scene)
        if judge.strip():
            return judge
    _warn("selfcard:no_active_card", hint="data/persona/active.json 缺或指向不存在的卡")
    return NO_CARD


def expression(scene=""):
    card = active_card()
    if card:
        _, expr = _cards.preview(card, scene)
        if expr.strip():
            return _with_looks(expr, card)
    return ""


def _with_looks(expr, card):
    """2026-10-26：外貌从 identity 搬到资产包，单独一块挂在表达面后面。

    为什么搬：identity 只进判断面，而且被 `preview` 切到 120 字（`identity[:120]`），
    外貌写在那儿等于她看不见；资产包里的 canon.txt 有独立预算（默认 400 字），
    还能配参考图，供 `core/identity/self_look.py` 判"这张图是不是我"。
    这里不走 core 也能工作：先试 self_look，失败就直接读 canon.txt。
    """
    try:
        from core.identity.self_look import self_block  # noqa: PLC0415
        block = self_block(str((card or {}).get("looks") or ""))
        if block.strip():
            return expr + "\n" + block
    except Exception as exc:  # noqa: BLE001 - 没有资产包也必须有表达面
        _warn("selfcard:looks_failed", err=repr(exc))
    try:
        slug = str((card or {}).get("looks") or "").strip()
        if slug:
            f = _ROOT / "data" / "persona" / "assets" / slug / "faces" / "canon.txt"
            if f.exists():
                text = " ".join(f.read_text(encoding="utf-8").split())
                if text:
                    return expr + "\n[我的样子] （这是我自己，不是别人给我定的）\n" + text[:400]
    except Exception as exc:  # noqa: BLE001
        _warn("selfcard:looks_file_failed", err=repr(exc))
    return expr


def save_card(name: str, text: str) -> str:
    """写回人设卡的**唯一入口**：先跑契约，再原子写。

    以后控制台/迁移脚本要改卡都走这里 —— 别各自再写一份"统一换行/去尾空行"。
    """
    return textfile.save(str(_CARDS / (str(name) + ".md")), text, CARD)
