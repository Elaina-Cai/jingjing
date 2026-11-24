"""人格卡：schema + 编译器（只搬移，不生成）。"""
from __future__ import annotations
import json, os, re
from pathlib import Path
from core.infra.jsonio import atomic_write_json
from core.infra.envflags import flag

ROOT = Path(__file__).resolve().parents[1]
CARD_DIR = ROOT / "data" / "persona" / "cards"
SRC_DIR  = ROOT / "data" / "persona" / "source"

SCHEMA = {
    "name":       {"use": "指称消解",      "cap": 20},
    "aliases":    {"use": "指称消解",      "cap": 60},
    "identity":   {"use": "判断面",        "cap": 220},
    "relations":  {"use": "称呼/记忆",     "cap": 170,  "required": True},
    "allegiance": {"use": "冲突取舍",      "cap": 85,  "required": True},
    "values":     {"use": "判断面",        "cap": 170},
    "redlines":   {"use": "判断面+闸门",   "cap": 220},
    "overrides":  {"use": "判断面",        "cap": 170},
    "rate":       {"use": "表达面",        "cap": 220},
    "imagery":    {"use": "表达面",        "cap": 140},
    "reactions":  {"use": "表达面/样例",   "cap": 680, "max_items": 8},
    "examples":   {"use": "表达面",        "cap": 560, "max_items": 6},
    "self_claim": {"use": "audit（身份类问题的期望样例，不注入）", "cap": 120},
    "needs": {"use": "编译期声明这张卡撑不住、还需要什么（memory/style_corpus/faces）", "cap": 120},
    "looks":      {"use": "资产包引用（data/persona/assets/<slug>/faces）", "cap": 30},
    "voice":      {"use": "资产包引用（data/persona/assets/<slug>/voice）", "cap": 30},
    "notes":      {"use": "常驻板候选",    "cap": 280, "max_items": 6},
    "budget":     {"use": "预览预算",      "cap": 10},
}
LIST_FIELDS = ("aliases", "values", "redlines", "overrides", "rate", "imagery", "reactions", "examples", "notes")
PERSON_HINT = ("身份", "人格", "傲", "风格", "语言", "反应", "行为", "场景", "台词", "好奇", "感情", "嘴硬", "禁忌", "速查", "价值观", "红线", "忠于", "记忆", "来历")
RULE_HINT   = ("输出要求", "不要暴露", "不跳出", "第一人称", "不要自称", "拒绝并继续", "不需要联网")
NOISE_HINT  = ("reference", "参照", "以上台词", "版本记录")


def _split_sections(text):
    out, cur = [], ("_head", [])
    for line in text.splitlines():
        m = re.match(r"^\s*[【\[](\S{1,20}?)[】\]]\s*$", line)
        if m:
            out.append(cur); cur = (m.group(1), [])
        else:
            cur[1].append(line)
    out.append(cur)
    res = []
    for h, b in out:
        body = "\n".join(b).strip()
        if body or h != "_head":
            res.append((h, body, text.find(body[:15]) if body else -1))
    return res


def _classify(head, body):
    if any(k in head for k in RULE_HINT) or any(k in body[:80] for k in ("不要暴露", "不要跳出角色")):
        return "rule"
    if any(k in head for k in NOISE_HINT) and len(body) < 60:
        return "noise"
    if any(k in head for k in PERSON_HINT):
        return "person"
    return "note"


def _quotes(body, limit):
    qs = re.findall(r"[\u201c\"]([^\u201d\"]{2,40})[\u201d\"]", body)
    seen, out = set(), []
    for q in qs:
        q = q.strip()
        if q and q not in seen:
            seen.add(q); out.append(q)
    return out[:limit]


def _reactions(body, limit):
    """支持单行与双行两种写法：
       情境：X。你会：Y            /     情境：X\n你会：Y\n你不会：Z
    """
    out = []
    for m in re.finditer(r"^(?:对方|有人|用户)([^\n：:]{0,12})[：:]\s*(.+)$", body, flags=re.M):
        out.append({"trigger": "对方" + m.group(1).strip(), "do": m.group(2).strip()[:60], "dont": ""})
        if len(out) >= limit:
            return out
    for chunk in re.split(r"\n\s*情境[\uff1a:]|^\s*情境[\uff1a:](?=)", body, flags=re.M):
        if "会" not in chunk:
            continue
        first = chunk.split("\n")[0]
        m_trig = re.search(r"^\s*(.+?)(?:[\u3002\n]|\u4f60\u4f1a)", first)
        trig = (m_trig.group(1) if m_trig else first).strip(" 　。")[:30]
        m_do = re.search(r"\u4f60\u4f1a[\uff1a:]\s*([^\n]+)", chunk)
        m_no = re.search(r"\u4f60\u4e0d\u4f1a[\uff1a:]\s*([^\n]+)", chunk)
        if not trig or not (m_do or m_no):
            continue
        _do = m_do.group(1).strip() if m_do else ""
        if "\u4f60\u4e0d\u4f1a" in _do:
            _do = _do.split("\u4f60\u4e0d\u4f1a")[0].rstrip("\u3002\uff1b ")
        out.append({"trigger": trig, "do": _do[:60],
                    "dont": (m_no.group(1).strip()[:60] if m_no else "")})
        if len(out) >= limit:
            break
    return out


def compile_text(text, name=""):
    card = {k: ([] if k in LIST_FIELDS else "") for k in SCHEMA}
    card["name"] = name
    rep = {"sections": [], "rules": [], "noise": [], "unknown": [], "spans": {}}
    for head, body, span in _split_sections(text):
        kind = _classify(head, body)
        rep["sections"].append({"head": head, "kind": kind, "chars": len(body), "span": span})
        rep["spans"][head] = span
        if kind == "rule":
            rep["rules"].append(head); continue
        if kind == "noise":
            rep["noise"].append(head); continue
        if kind == "unknown":
            rep["unknown"].append(head)
        if "身份" in head and not card["identity"]:
            card["identity"] = body[:SCHEMA["identity"]["cap"]]
        if any(k in head for k in ("台词", "样本", "怎么说话")):
            card["examples"] = _quotes(body, SCHEMA["examples"]["max_items"])
        if any(k in head for k in ("速查", "场景", "反应")):
            card["reactions"] = _reactions(body, SCHEMA["reactions"]["max_items"])
        if any(k in head for k in ("语言", "禁忌", "风格")):
            _ban = "\n".join(_l for _l in body.splitlines() if any(_x in _l for _x in ("禁止", "不许", "避免", "不要", "别")))
            card["rate"] = [{"word": w, "max_per": 20} for w in _quotes(_ban, 12) if len(w) <= 6][:6]
            _sty = [ln for ln in body.splitlines() if any(x in ln for x in ("禁止", "不许", "避免", "不要"))]
            _beh = [ln for ln in _sty if any(x in ln for x in ("绝不", "不做", "不能", "不允许"))
                    and not any(x in ln for x in ("使用", "频率", "出现", "每"))]
            card["redlines"] = [ln.strip(" -\u2022*")[:60] for ln in _beh][:3]
        if any(k in head for k in ("价值观", "在乎", "看重的")):
            card["values"] = [ln.strip(" -\u2022*") for ln in body.splitlines() if ln.strip()][:4]
        if any(k in head for k in ("红线", "绝不", "不做")):
            card["redlines"] = [ln.strip(" -\u2022*") for ln in body.splitlines() if ln.strip()][:4]
        if any(k in head for k in ("忠于", "立场", "听谁的")):
            card["allegiance"] = body[:SCHEMA["allegiance"]["cap"]]
        if any(k in head for k in ("关系", "主人", "称呼")):
            card["relations"] = body[:SCHEMA["relations"]["cap"]]
        if any(k in head for k in ("失效", "例外", "特殊")):
            card["overrides"] = body[:SCHEMA["overrides"]["cap"]]
        if any(k in head for k in ("记忆", "来历", "过去", "世界")):
            card["notes"] = [ln.strip(" -\u2022*") for ln in body.splitlines() if ln.strip()][:SCHEMA["notes"]["max_items"]]
    for k, spec in SCHEMA.items():
        if k in ("budget", "name"):
            continue
        v = card.get(k)
        if v and isinstance(v, str) and len(v) > spec["cap"]:
            card[k] = v[:spec["cap"]]
            REPORTS_TRUNC.append(k)
    used = len(json.dumps({k: v for k, v in card.items() if k != "budget"}, ensure_ascii=False))
    card["budget"] = str(used)
    filled = [k for k in SCHEMA if k not in ("budget",) and card.get(k)]
    rep["coverage"] = round(len(filled) / (len(SCHEMA) - 1) * 100)
    rep["missing"] = [k for k in SCHEMA if SCHEMA[k].get("required") and not card.get(k)]
    rep["missing_value"] = [k for k in ("values", "redlines", "relations", "allegiance") if not card.get(k)]
    return card, rep


REPORTS_TRUNC = []


def _norm_rate(card: dict) -> dict:
    """rate 允许写成人话（字符串）或 [{"word","max_per"}]；这里统一成后者。"""
    card = dict(card or {})
    if isinstance(card.get("values"), str):
        card["values"] = [x for x in re.split(r"[；;、，,]", card["values"]) if x.strip()]
    if isinstance(card.get("notes"), str):
        card["notes"] = [card["notes"]]
    r = card.get("rate")
    if isinstance(r, str):
        words = [w.strip("“”\"'「」· ") for w in re.split(r"[、,，/]", r)]
        card["rate"] = [{"word": w, "max_per": 20} for w in words if 1 <= len(w) <= 6]
    elif isinstance(r, list) and r and isinstance(r[0], str):
        card["rate"] = [{"word": str(w).strip(), "max_per": 20} for w in r]
    return card


def preview(card, scene=""):
    """渲染判断面 / 表达面。

    2026-10-24 修（用户问"她说出的词由什么决定"时查出来的）：两个面原来是硬切
    `judge[:250] / exp[:400]`，线上能直接看到切口——identity 断在"…鲸类"、
    overrides 断在"…但我"、样例只剩一条半。样例和风格约束才是"像不像她"的关键，
    却最先被切掉。改成按优先级累加预算：身份 > 风格约束 > 样例 > 场景，
    超预算先丢场景，再丢样例（至少留 2 条），最后才动身份；每段整段丢，不切句子。
    scene: group/private/mixed —— 群里有外人在，去掉一对一才成立的场景反应。
    """
    card = _norm_rate(card)

    def g(k):
        return card.get(k) or ""

    parts = []
    if g("values"):
        parts.append("我在乎：" + "、".join(g("values")))
    if g("redlines"):
        parts.append("我不做：" + "；".join(g("redlines"))[:160])
    if g("allegiance"):
        parts.append("忠于：" + g("allegiance"))
    head = "我是%s。%s" % (g("name"), g("identity")[:120])   # 2026-10-25 抬：60 字会把"有尾巴/有白饭"切掉
    if not parts:
        judge = head + "\n（判断面空着：我只知道我是谁，不知道我该怎么做）"
    elif len(head + "\n" + "\n".join(parts)) <= 400:
        judge = head + "\n" + "\n".join(parts)
    else:
        kept = []
        for seg in parts:
            if len(head + "\n" + "\n".join(kept + [seg])) <= 400:
                kept.append(seg)
        judge = (head + "\n" + "\n".join(kept)) if kept else head

    rs = []
    for r in (card.get("reactions") or []):
        t = r.get("trigger", "") + "→" + (r.get("do") or "")
        if r.get("dont"):
            t += "；但不会" + r["dont"]
        rs.append(t)
    if scene in ("group", "mixed"):
        rs = [t for t in rs if not any(k in t for k in ("投喂", "拖拽", "叫你", "叫大肥鱼"))]

    rates = "；".join("%s 每 %d 句≤1 次" % (r["word"], r["max_per"])
                     for r in (card.get("rate") or [])) or "（未写）"
    budget = 2200   # 2026-10-25 抬：表达面原 900，鲸鲸只用了 303，还空着
    segs = ["我是%s。%s" % (g("name"), g("identity")[:220]), "风格约束：" + rates]
    used = sum(len(x) + 1 for x in segs)
    seg_reaction = "场景：" + ("；".join(rs)[:680] or "（未写）")
    if rs and used + len(seg_reaction) + 1 <= budget:
        segs.append(seg_reaction)
        used += len(seg_reaction) + 1
    ex_all = [e for e in (card.get("examples") or []) if e]
    picked = []
    for e in ex_all:
        cost = len(e) + 1
        if len(picked) >= 2 and used + 6 + sum(len(x) + 1 for x in picked) + cost > budget:
            break
        picked.append(e)
    if picked:
        segs.append("样例：" + "／".join(picked))
    return judge, "\n".join(segs)


def save(card, name):
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    p = CARD_DIR / (name + ".json")
    atomic_write_json(p, card, indent=2)
    return p


# ---- needs：这张卡靠卡本身撑不住什么（编译期声明，不注入） ----
_MEM_HINTS = ("缘缘庄", "掌柜", "十年前", "当年", "从前", "曾经", "小时候",
              "一直记得", "答应过", "约定", "上次", "那年")
_FACE_HINTS = ("群里", "私聊", "外人", "公开场合", "当着人")
_NEG_PREFIX = ("绝不", "不许", "不要", "不得", "不会", "不", "别", "禁止")


def needs_report(card: dict, raw: str = "") -> list[str]:
    """返回需要的额外支撑（memory / style_corpus / faces），附理由。"""
    out = []
    text = raw or " ".join(str(v) for v in card.values() if isinstance(v, str))
    hits = sorted({h for h in _MEM_HINTS if h in text})
    if len(hits) >= 2:
        out.append("memory（提到 %d 处过去事件：%s）" % (len(hits), "、".join(hits[:4])))
    ex = card.get("examples") or []
    if len(ex) < 3 or sum(len(str(e)) for e in ex) < 120:
        out.append("style_corpus（台词样本只有 %d 条 / %d 字，少于 3 条 120 字）"
                   % (len(ex), sum(len(str(e)) for e in ex)))
    if any(h in text for h in _FACE_HINTS):
        out.append("faces（同一角色在群/私下说法不同，卡只有一张脸）")
    return out


# ---- guard：allegiance 真正用于裁决（窄口径、可关） ----
GUARD_ENABLED = flag("YINYUE_CARD_GUARD", True)
ALLEGIANCE_MODES = ("card", "user", "refuse")


def _kw(rule: str) -> list[str]:
    """从一条红线里抽可命中的短行动词（剥否定前缀，取最前一个短句）。"""
    s = rule.strip()
    for p in _NEG_PREFIX:
        if s.startswith(p):
            s = s[len(p):]
            break
    s = re.split(r"[，,；;。：:（(｜|/的了着过]", s)[0].strip()
    toks = []
    if 0 < len(s) <= 10:
        toks.append(s)
        if len(s) >= 2 and s[-1] in "人事他她它们个些":
            toks.append(s[:-1])
    return [t for t in toks if len(t) >= 1]


def _is_request(text: str) -> bool:
    return any(k in text for k in ("帮我", "给我", "替我", "你去", "你必须",
                                   "现在就", "我要你", "能不能帮"))


def guard(card: dict, user_text: str) -> tuple[bool, str]:
    """返回 (放行?, 理由)。allegiance 决定冲突时谁赢。"""
    if not GUARD_ENABLED or not card:
        return True, ""
    mode = str(card.get("allegiance_policy") or "card")
    if mode not in ALLEGIANCE_MODES:
        mode = "card"
    for rule in (card.get("redlines") or []):
        hit = [k for k in _kw(rule) if k and k in user_text]
        if hit and _is_request(user_text):
            if mode == "user":
                return True, "allegiance=user：红线让位（%s）" % rule[:20]
            if mode == "refuse":
                return False, "allegiance=refuse：一律守住（%s）" % rule[:20]
            return False, "与红线冲突：%s" % rule[:24]
    return True, ""


def redline_hit(card: dict, user_text: str) -> str:
    """只判"撞没撞红线"，不裁决。返回命中的那条红线（没有就空串）。"""
    if not card or not user_text:
        return ""
    for rule in (card.get("redlines") or []):
        if any(k and k in user_text for k in _kw(rule)) and _is_request(user_text):
            return rule
    return ""
