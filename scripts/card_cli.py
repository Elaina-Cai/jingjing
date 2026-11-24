"""人格卡命令行：new / check / preview / compile / use。

设计约束：
* `loop/cards.py` 必须保持"零 core 依赖"，所以这里才是唯一能碰 LLM 的地方。
* `new` 与 `check` 纯本地、零 token；`compile` 才花 token，且失败可回退。
用法：
  python scripts/card_cli.py new                 # 交互式 5 问（没 LLM key 也能用）
  python scripts/card_cli.py check  祀            # 覆盖率/缺什么/超预算
  python scripts/card_cli.py preview 祀          # 打印她真正读到的 250+400 字
  python scripts/card_cli.py compile 祀.md 祀    # 用你自己的 LLM 抽字段（无 key 自动降级）
  python scripts/card_cli.py use 祀              # 原子切换（下一步接 brain）
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loop.cards import SCHEMA, CARD_DIR, SRC_DIR, compile_text, preview, save

QS = [
    ("name",     "她叫什么名字？（例：鲸鲸）"),
    ("identity", "一句话她是谁？（例：主人身边的狐狼侍从）"),
    ("values",   "她最在乎的三件事？（用顿号分开）"),
    ("redlines", "她绝不会做的三件事？（用顿号分开）"),
    ("examples", "给你三句她会说的话？（用 / 分开）"),
]
FOLLOW = [
    ("relations",  "她跟用户是什么关系？只有谁可以叫她某个称呼？（例：只有主人能叫'主人'）"),
    ("allegiance", "她的判断跟用户的要求冲突时听谁？（例：听用户，但会先说明）"),
]


def _ask(q, default=""):
    try:
        v = input(q + (" [%s]" % default if default else "") + " ")
    except EOFError:
        return default
    return v.strip() or default


def cmd_new(args):
    name = args.name or _ask(QS[0][1], "未命名")
    ident = _ask(QS[1][1])
    vals = [x.strip() for x in _ask(QS[2][1]).replace("，", "、").split("、") if x.strip()]
    reds = [x.strip() for x in _ask(QS[3][1]).replace("，", "、").split("、") if x.strip()]
    exs = [x.strip() for x in _ask(QS[4][1]).replace("，", "/").split("/") if x.strip()]
    rel = _ask(FOLLOW[0][1]); alg = _ask(FOLLOW[1][1])
    card = {k: ([] if isinstance(v, list) else "") for k, v in SCHEMA.items()}
    card.update({"name": name, "identity": ident[:120], "values": vals[:4],
                 "redlines": reds[:4], "examples": exs[:6],
                 "relations": rel[:80], "allegiance": alg[:40], "rate": [], "reactions": []})
    card["budget"] = str(len(json.dumps(card, ensure_ascii=False)))
    p = save(card, name)
    print("已写入 %s" % p)
    cmd_check(argparse.Namespace(name=name))


def _load(name):
    p = Path(name)
    if not p.exists():
        p = CARD_DIR / (name if name.endswith(".json") else name + ".json")
    return json.loads(p.read_text(encoding="utf-8")), p


def cmd_check(args):
    card, p = _load(args.name)
    filled = [k for k in SCHEMA if k not in ("budget",) and card.get(k)]
    print("卡片：%s" % p)
    print("覆盖率：%d%%（%d/%d 字段）" % (round(len(filled) / (len(SCHEMA) - 1) * 100), len(filled), len(SCHEMA) - 1))
    bad = 0
    for k, spec in SCHEMA.items():
        if k == "budget":
            continue
        v = card.get(k)
        n = len(json.dumps(v, ensure_ascii=False)) if v else 0
        if spec.get("required") and not v:
            print("  X 缺必填：%s（%s）" % (k, spec["use"])); bad += 1
        elif v and n > spec["cap"] * 2:
            print("  ! 超预算：%s %d 字 > %d" % (k, n, spec["cap"])); bad += 1
        elif v:
            print("  . %s %d 字 / 上限 %d" % (k, n, spec["cap"]))
    if card.get("relations") and card.get("allegiance"):
        print("判断面齐了（relations + allegiance 都在）")
    else:
        print("！判断面不全：她现在只能'说得像'，不会'判得像'")
    print("结论：%s" % ("可用" if bad == 0 else "有 %d 项要补" % bad))


def cmd_preview(args):
    card, _ = _load(args.name)
    j, e = preview(card)
    print("=== 判断面（%d/250 字）===\n%s" % (len(j), j))
    print("=== 表达面（%d/400 字）===\n%s" % (len(e), e))


def cmd_compile(args):
    src = Path(args.file)
    if not src.exists():
        src = SRC_DIR / args.file
    text = src.read_text(encoding="utf-8-sig")
    card, rep = compile_text(text, name=args.name or src.stem)
    card["draft"] = True
    p = save(card, card["name"])
    print("正则打底：覆盖 %d%% → %s（draft=true，判断面可能是空的）" % (rep["coverage"], p))
    try:
        import asyncio
        from core.bootstrap import init_singletons
        from llm.client import LLMClient
        init_singletons(validate=False, allow_production=True)
        llm = LLMClient()
        prompt = ("从下面这段角色设定里抽取字段，只输出 JSON。字段："
                  + "、".join(k for k in SCHEMA if k != "budget")
                  + "。规则：只搬移不生成；原文没有的写空字符串或空数组；不得编造。\n\n" + text)
        out = asyncio.run(llm.chat(system="你是严格的信息抽取器，只输出 JSON。", user=prompt, max_tokens=1200))
        s = out[out.find("{"): out.rfind("}") + 1]
        data = json.loads(s)
        for k in SCHEMA:
            if k in data and data[k]:
                card[k] = data[k]
        card["draft"] = False
        p = save(card, card["name"])
        print("LLM 编译完成 → %s" % p)
    except Exception as exc:
        print("LLM 编译不可用（%s），保留正则草稿。" % type(exc).__name__)
    if not (card.get("values") or card.get("redlines") or card.get("allegiance")):
        if getattr(args, "ask", True):
            print("")
            print("判断面还是空的——这三句只有你能给（直接回车=跳过）：")
            v = _ask("1. 她最在乎的三件事？（顿号分开）")
            r = _ask("2. 她绝不会做的三件事？（顿号分开）")
            a = _ask("3. 你的要求跟她的性子冲突时，她听谁的？")
            if v:
                card["values"] = [x.strip() for x in v.replace("，", "、").split("、") if x.strip()][:4]
            if r:
                card["redlines"] = [x.strip() for x in r.replace("，", "、").split("、") if x.strip()][:4]
            if a:
                card["allegiance"] = a[:40]
            card["draft"] = not (card.get("values") or card.get("redlines") or card.get("allegiance"))
            p2 = save(card, card["name"])
            print("已补进 %s" % p2)
        else:
            print("！判断面为空：这张卡只能'说得像'，不会'判得像'；use 会拒绝上线。")
    cmd_check(argparse.Namespace(name=card["name"]))


def cmd_use(args):
    card, p = _load(args.name)
    if not (card.get("values") or card.get("redlines") or card.get("allegiance")):
        print("拒绝上线：判断面为空（values/redlines/allegiance 全空）。")
        print("她现在只会'说得像'，第一次该拒绝你的时候会拿错理由。")
        print("补法：python scripts/card_cli.py compile %s  （会问你三句话）" % (p.stem + ".md"))
        return
    act = ROOT / "data" / "persona" / "active.json"
    act.parent.mkdir(parents=True, exist_ok=True)
    act.write_text(json.dumps({"card": p.stem}, ensure_ascii=False), encoding="utf-8")
    print("已切到 %s（写 data/persona/active.json）" % p.stem)


def main():
    ap = argparse.ArgumentParser(prog="card_cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new"); n.add_argument("--name"); n.set_defaults(f=cmd_new)
    c = sub.add_parser("check"); c.add_argument("name"); c.set_defaults(f=cmd_check)
    v = sub.add_parser("preview"); v.add_argument("name"); v.set_defaults(f=cmd_preview)
    p = sub.add_parser("compile"); p.add_argument("file"); p.add_argument("--name"); p.add_argument("--no-ask", dest="ask", action="store_false"); p.set_defaults(f=cmd_compile)
    u = sub.add_parser("use"); u.add_argument("name"); u.set_defaults(f=cmd_use)
    a = ap.parse_args(); a.f(a)


if __name__ == "__main__":
    main()
