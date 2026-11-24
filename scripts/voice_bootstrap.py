# -*- coding: utf-8 -*-
# 她 vs 真人：等样本量自举对照（bootstrap）。为什么必须自举——词面多样性随样本量下降，
# 293 条 vs 14289 条直接比 TTR 会得出假结论。这里把真人随机抽到和她一样的条数，重复 400 次，
# 看她的值落在真人分布的哪个位置（|z|>2 才算真有差别）。同批会话、可复现（固定种子）。
#
#   python scripts\voice_bootstrap.py [draws]
from __future__ import annotations

import json
import os
import random
import sqlite3
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from voice_eval import metrics  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "world.db").replace("\\", "/")
SEED = 20261028


def con():
    return sqlite3.connect("file:%s?mode=ro" % DB, uri=True)


def main(draws=400):
    c = con()
    her_rows = c.execute("select text, chat_id from outbound_receipt where length(text)>0 "
                         "order by id desc limit 3000").fetchall()
    chats = sorted({r[1] for r in her_rows if r[1]})
    q = ",".join("?" * len(chats))
    pool = [t.strip() for (t,) in c.execute(
        "select text from inbound_event where length(text)>0 and chat_id in (%s)" % q, chats) if t and t.strip() and not t.strip().startswith("[")]
    c.close()
    hers = [r[0].strip() for r in her_rows if r[0] and r[0].strip()]
    n = len(hers)
    base = metrics(hers)
    keys = [k for k in base if isinstance(base[k], float)]
    rng = random.Random(SEED)
    if len(pool) <= n:
        return {"error": "pool_too_small", "pool": len(pool), "her_n": n}
    samples = {k: [] for k in keys}
    for _ in range(int(draws)):
        m = metrics(rng.sample(pool, n))
        for k in keys:
            samples[k].append(m[k])
    rows = {}
    for k in keys:
        mu, sd = statistics.mean(samples[k]), (statistics.pstdev(samples[k]) or 1e-9)
        z = (base[k] - mu) / sd
        rows[k] = [base[k], round(mu, 3), round(sd, 3), round(z, 1)]
    out = {"her_n": n, "pool_n": len(pool), "chats": len(chats), "draws": int(draws),
           "rows_her_mean_sd_z": rows,
           "sig": sorted([k for k in keys if abs(rows[k][3]) >= 2], key=lambda k: -abs(rows[k][3]))}
    os.makedirs(os.path.join(ROOT, ".test_tmp"), exist_ok=True)
    open(os.path.join(ROOT, ".test_tmp", "voice_bootstrap.json"), "w", encoding="utf-8", newline="\n").write(
        json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:2]))
