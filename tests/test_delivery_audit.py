# -*- coding: utf-8 -*-
"""观察窗对账（delivery_audit）—— 2026-11-01

守住三件事：
1. 配对规则（同会话 + 5 分钟内 + 文本优先）不能退化成"随便找一条"；
2. 结论口径（认账 / 没回来认账 / 没进队列 / 被拦）别弄混 —— 她只能看到 `queued`，
   这四个词是家里人判断"话到底到没到"的唯一依据；
3. 面板和 /state 必须真的连着（`delivery` 字段 + `#deliver-box`）。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ext():
    import importlib
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    return importlib.import_module("collector_ext")


def _say(at_ts, chat, text, result="sent", why="", group_id="", queue_ids=None):
    return {"at": "2026-11-01 17:00:00", "ts": at_ts, "target": chat, "text": text,
            "why": why, "result": result, "blocked_reason": "", "turn_id": "t",
            "group_id": group_id, "queue_ids": queue_ids or []}


def _row(rid, chat, preview, state, ts, ack="", settled=None, why="", group_id=""):
    return {"id": rid, "chat_id": chat, "group_id": group_id, "preview": preview,
            "state": state, "ack": ack, "created_ts": ts, "settled_ts": settled,
            "why": why}


def test_text_preferred_over_time():
    C = _ext()
    says = [_say(1000.0, "c1", "在的呀。")]
    rows = [_row(1, "c1", "别的话", "sent", 999.0, "confirmed"),
            _row(2, "c1", "在的呀。", "sent", 1002.0, "confirmed")]
    out = C.audit_join(says, rows, {})
    assert out["rows"][0]["queue_id"] == 2
    assert out["rows"][0]["verdict"] == "confirmed"


def test_no_queue_means_no_queue():
    C = _ext()
    out = C.audit_join([_say(1000.0, "c1", "在的呀。")], [_row(1, "c1", "别的", "sent", 9000.0)], {})
    assert out["counts"]["no_queue"] == 1
    assert out["rows"][0]["queue_id"] is None


def test_unconfirmed_vs_confirmed():
    C = _ext()
    says = [_say(1000.0, "c1", "甲"), _say(1100.0, "c1", "乙")]
    rows = [_row(1, "c1", "甲", "sent", 1000.0, "pending"),
            _row(2, "c1", "乙", "sent", 1100.0, "confirmed")]
    out = C.audit_join(says, rows, {})
    got = {r["text"]: r["verdict"] for r in out["rows"]}
    assert got["甲"] == "unconfirmed" and got["乙"] == "confirmed"


def test_receipt_confirms_even_if_queue_says_pending():
    C = _ext()
    out = C.audit_join([_say(1000.0, "c1", "在的呀。")],
                       [_row(1, "c1", "在的呀。", "sent", 1000.0, "pending")],
                       {("c1", "在的呀。"): {"ack": "confirmed"}})
    assert out["rows"][0]["verdict"] == "confirmed"


def test_blocked_is_not_a_delivery_problem():
    C = _ext()
    out = C.audit_join([_say(1000.0, "c1", "", result="blocked")], [], {})
    assert out["counts"]["blocked"] == 1
    assert out["blind"] == 0


def test_failed_and_expired_keep_their_names():
    C = _ext()
    says = [_say(1000.0, "c1", "甲"), _say(1200.0, "c1", "乙")]
    rows = [_row(1, "c1", "甲", "failed", 1000.0, why="无法激活微信窗口"),
            _row(2, "c1", "乙", "expired", 1200.0, why="超过 600 秒没发出去")]
    out = C.audit_join(says, rows, {})
    got = {r["text"]: r["verdict"] for r in out["rows"]}
    assert got["甲"] == "failed" and got["乙"] == "expired"


def test_latency_and_median():
    C = _ext()
    says = [_say(1000.0, "c1", "甲"), _say(2000.0, "c1", "乙")]
    rows = [_row(1, "c1", "甲", "sent", 1000.0, "confirmed", settled=1010.0),
            _row(2, "c1", "乙", "sent", 2000.0, "confirmed", settled=2030.0)]
    out = C.audit_join(says, rows, {})
    assert out["median_latency_s"] == 30.0
    assert out["reach"] == 2


# ---------------------------------------------------------------------------
# 2026-11-01：一次开口 = N 个气泡（1:N），配对单位从「一行」改成「一组」
# ---------------------------------------------------------------------------

def test_group_id_joins_all_bubbles():
    """同 group_id 的 N 个气泡要算成**一条**开口，不能只抓第一个。"""
    C = _ext()
    says = [_say(1000.0, "c1", "甲", group_id="g1")]
    rows = [_row(1, "c1", "甲", "sent", 1000.0, "confirmed", group_id="g1"),
            _row(2, "c1", "甲·下", "sent", 1002.0, "confirmed", group_id="g1"),
            _row(3, "c1", "甲·再下", "sent", 1004.0, "confirmed", group_id="g1")]
    out = C.audit_join(says, rows, {})
    r = out["rows"][0]
    assert r["bubbles"] == 3, r
    assert r["queue_ids"] == [1, 2, 3], r
    assert r["verdict"] == "confirmed", r


def test_group_one_bubble_missing_fails_whole_utterance():
    """一句话拆 3 个气泡，漏了第 3 个 —— 对方只看到半句，不能报送达。"""
    C = _ext()
    says = [_say(1000.0, "c1", "甲", group_id="g1")]
    rows = [_row(1, "c1", "甲", "sent", 1000.0, "confirmed", group_id="g1"),
            _row(2, "c1", "甲·下", "sent", 1002.0, "confirmed", group_id="g1"),
            _row(3, "c1", "甲·再下", "failed", 1004.0, group_id="g1",
                 why="无法激活微信窗口")]
    out = C.audit_join(says, rows, {})
    r = out["rows"][0]
    assert r["bubbles"] == 3 and r["verdict"] == "failed", r
    assert "无法激活" in r["why_failed"], r


def test_group_half_sent_is_waiting_not_confirmed():
    C = _ext()
    says = [_say(1000.0, "c1", "甲", group_id="g1")]
    rows = [_row(1, "c1", "甲", "sent", 1000.0, "confirmed", group_id="g1"),
            _row(2, "c1", "甲·下", "pending", 1002.0, group_id="g1")]
    out = C.audit_join(says, rows, {})
    assert out["rows"][0]["verdict"] == "waiting", out["rows"][0]


def test_group_id_without_rows_is_no_queue_not_a_guess():
    """写了 group_id 却在队列里找不到这一组：如实报没进队列，不许拿旁边气泡顶上。"""
    C = _ext()
    says = [_say(1000.0, "c1", "甲", group_id="g1")]
    rows = [_row(9, "c1", "甲", "sent", 1001.0, "confirmed", group_id="g9")]
    out = C.audit_join(says, rows, {})
    r = out["rows"][0]
    assert r["verdict"] == "no_queue" and r["bubbles"] == 0, r
    assert out["counts"]["confirmed"] == 0, "没有 group_id 命中就不许报送达"


def test_legacy_say_without_group_id_still_falls_back_to_fuzzy():
    """老账本行没 group_id（写入侧 2026-11-01 才补），要退回时间窗+文本，不能塌成黑洞。"""
    C = _ext()
    says = [_say(1000.0, "c1", "在的呀。")]
    rows = [_row(1, "c1", "在的呀。", "sent", 1002.0, "confirmed")]
    out = C.audit_join(says, rows, {})
    r = out["rows"][0]
    assert r["verdict"] == "confirmed" and r["join"] == "fuzzy", r


def test_orphan_bubbles_surfaced():
    """队列里有、账本里没有的气泡必须露出来 —— 不经过账本的那条失败不能被吞掉。"""
    C = _ext()
    says = [_say(1000.0, "c1", "甲", group_id="g1")]
    rows = [_row(1, "c1", "甲", "sent", 1000.0, "confirmed", group_id="g1"),
            _row(2, "c2", "自测", "failed", 1005.0, group_id="g2", why="激活失败")]
    out = C.audit_join(says, rows, {})
    assert out["counts"]["confirmed"] == 1, out["counts"]
    ids = [o["id"] for o in out["orphans"]]
    assert ids == [2], ids
    assert out["orphans"][0]["state"] == "failed"
    assert "激活失败" in out["orphans"][0]["why"]


def test_orphans_exposed_to_panel():
    """面板读的出口视图 `outbound_view` 要带上孤儿数，否则前端无从显示。"""
    src = open(os.path.join(ROOT, "collector_ext.py"), encoding="utf-8").read()
    assert "def _orphan_bubbles(" in src, "反向对账要有独立函数"
    i = src.find("def outbound_view(")
    assert i > 0
    tail = src[i:i + 3000]
    assert "orphan_n" in tail and "orphan_bad" in tail, "outbound_view 要外泄孤儿数与失败数"


def test_queue_ids_join_is_exact():
    """队列行号是最准的键：说好哪几行就只认哪几行，不牵连旁边的气泡。"""
    C = _ext()
    says = [_say(1000.0, "c1", "甲", queue_ids=[1, 3])]
    rows = [_row(1, "c1", "甲", "sent", 1000.0, "confirmed", group_id="g1"),
            _row(2, "c1", "别人的", "sent", 1000.5, "confirmed", group_id="g9"),
            _row(3, "c1", "甲·下", "sent", 1002.0, "confirmed", group_id="g1")]
    out = C.audit_join(says, rows, {})
    r = out["rows"][0]
    assert r["queue_ids"] == [1, 3], r
    assert r["bubbles"] == 2 and r["verdict"] == "confirmed", r
    assert out["orphans"] and out["orphans"][0]["id"] == 2, "没被认领的仍是孤儿"


def test_queue_ids_beats_group_id():
    """两个键都在时先用行号（行号是主键，组号可能跨条复用）。"""
    C = _ext()
    says = [_say(1000.0, "c1", "甲", group_id="g1", queue_ids=[2])]
    rows = [_row(1, "c1", "甲", "failed", 1000.0, group_id="g1"),
            _row(2, "c1", "甲·下", "sent", 1001.0, "confirmed", group_id="g1")]
    out = C.audit_join(says, rows, {})
    r = out["rows"][0]
    assert r["join"] == "queue_ids", r
    assert r["queue_ids"] == [2] and r["verdict"] == "confirmed", r


def test_say_many_fallback_never_drops_the_link_keys():
    """2026-11-01：`say_many` 的兜底分支（生产走的就是这条）原本把每个气泡的
    group_id / queue_ids 全丢了，账本想记也记不到 —— 对账只能靠文本猜。"""
    src = open(os.path.join(ROOT, "loop", "ports.py"), encoding="utf-8").read()
    i = src.find("async def say_many(")
    assert i > 0
    body = src[i:src.find("\nasync def ", i + 20)]
    assert "_ids_all" in body and "_gids_all" in body, "兜底分支要收集链接键"
    assert '"queue_ids": _ids_all' in body, "成功返回要带上行号"
    assert 'data={"bubbles_sent"' in body and '"queue_ids": _ids_all' in body
    # 半截失败：已经发出去的那几句也要对得上账
    j = body.find('data={"bubbles_sent"')
    assert '"group_ids": _gids_all' in body[j:j + 320]


def test_say_many_enqueue_has_no_ghost_name():
    """2026-11-01：整组入队那行曾写 `_time.time()`，而本函数只 `import time`。

    结果每一次开口都 NameError → 被 except 吞掉 → 静默退化成逐条发送；
    silent_warnings.log 里躺着 136 条，最早一条在 2026-10-28（这个分支上线当天）。
    组保序 / 整组 TTL / 整组成败 因此从未真正生效过。
    这条锁的是"用到的名字必须真的存在"——幽灵名字会被 except 吞成静默降级。
    """
    src = open(os.path.join(ROOT, "loop", "ports.py"), encoding="utf-8").read()
    i = src.find("async def say_many(")
    body = src[i:src.find("\nasync def ", i + 20)]
    j = body.find("if not dry_run:")
    blk = body[j:body.find("sent = []", j)]
    gid_line = [ln for ln in blk.splitlines() if "_gid =" in ln]
    assert gid_line, "整组入队要生成组号"
    assert "time.time()" in gid_line[0], "组号里要有毫秒时间戳"
    assert "_time" not in gid_line[0], "本函数没有 `_time` 这个别名，写了就是 NameError"


def test_join_stats_reported():
    """配对质量要外泄：group_id 命中数只增不减，fuzzy 只减不增。"""
    C = _ext()
    says = [_say(1000.0, "c1", "甲", group_id="g1"), _say(1100.0, "c1", "乙")]
    rows = [_row(1, "c1", "甲", "sent", 1000.0, "confirmed", group_id="g1"),
            _row(2, "c1", "乙", "sent", 1100.0, "confirmed")]
    out = C.audit_join(says, rows, {})
    assert out["join"]["group_id"] == 1 and out["join"]["fuzzy"] == 1, out["join"]


def test_queue_rows_sql_carries_group_id():
    """取队列行必须带上 group_id：不带就没法整组配对，只有单气泡能碰上。"""
    src = open(os.path.join(ROOT, "collector_ext.py"), encoding="utf-8").read()
    assert "group_id" in src
    i = src.find("from outbound_queue where created_ts")
    assert i > 0
    assert "group_id" in src[max(0, i - 400):i], "_queue_rows 的 select 里要取 group_id"


def test_wiring_state_panel_and_archive():
    ext = open(os.path.join(ROOT, "collector_ext.py"), encoding="utf-8").read()
    assert "def delivery_audit(" in ext and "_DELIVERY_CACHE" in ext
    assert "queue-archive" in ext, "清过队列的话历史行要从 archive 补回来，否则会被误判成没进队列"
    col = open(os.path.join(ROOT, "console", "collector.py"), encoding="utf-8").read()
    assert '"outbound"' in col and "outbound_view(24)" in col
    panel = open(os.path.join(ROOT, "console", "panel.html"), encoding="utf-8").read()
    assert "s.outbound" in panel, "面板要从统一的出口视图取数"


def test_delivery_field_removed_after_switch():
    """2026-11-01 切换即删：`delivery` 与 `outbound` 是同一份数据，删掉前者。

    这条锁的是"不许回潮"：哪天再有人把 delivery 塞回 /state，
    面板上就会出现两块内容相同、说法不同的对账（每轮还多传 20KB）。
    """
    col = open(os.path.join(ROOT, "console", "collector.py"), encoding="utf-8").read()
    i = col.find("def build_state(")
    assert i > 0
    body = col[i:col.find("\ndef ", i + 20)]   # 只看组装 /state 那段（_ledger 里过滤 delivery 行是另一回事）
    assert '"delivery"' not in body, "/state 不再单独发一份 delivery"
    assert "delivery_audit(24)" not in body
    panel = open(os.path.join(ROOT, "console", "panel.html"), encoding="utf-8").read()
    assert "st.delivery" not in panel and "s.delivery" not in panel
    assert "deliver-box" not in panel


def test_ledger_no_longer_ships_says_and_fake_receipts():
    """`ledger.says` 无消费者（7KB/轮）；`ledger.deliveries` 恒 0 却被人当真数显示。"""
    col = open(os.path.join(ROOT, "console", "collector.py"), encoding="utf-8").read()
    i = col.find("def _ledger(")
    assert i > 0
    body = col[i:col.find("def ", i + 20)]
    assert 'out["says"]' not in body, "says 已由 outbound.rows 取代"
    assert "deliveries" not in body, "恒为 0 的回执数不许再发（真数在 outbound.counts）"
    panel = open(os.path.join(ROOT, "console", "panel.html"), encoding="utf-8").read()
    assert "L.deliveries" not in panel, "账页的回执数要读 outbound.counts"
    assert "lg-rows" not in panel, "读 ledger.rows 的空盒子已删（那个键不存在）"
    assert "_oc0" in panel, "账页回执数改读 outbound.counts"


def main() -> bool:
    """2026-11-01 补：这个文件以前只有 def test_、没有运行入口，8 条断言一条都没跑过
    （scripts/audit_vacuous_tests.py --run 判「空转」）。现在实跑每一个用例。"""
    names = sorted(n for n in globals() if n.startswith("test_"))
    fails = []
    for n in names:
        fn = globals()[n]
        if not callable(fn):
            continue
        try:
            fn()
            print("  ok   %s" % n)
        except Exception as exc:  # noqa: BLE001 - 汇总用，单个用例炸了要继续跑完
            fails.append(n)
            print("  FAIL %s  %s" % (n, repr(exc)[:200]))
    print("RESULT %d failed / %d" % (len(fails), len(names)))
    return not fails


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
