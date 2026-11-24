"""她做过什么：只有一本流水（行 I/O + 两个读判据）。

为什么在这里（PIT-111，2026-10-29）：流水原来只长在 ``loop/ledger.py``，而问
「她最近碰过这个会话吗」的还有一层在 ``core/``（``terminal/proactive_service``
判冷却时读 DB 的 ``last_send_at``），``core/`` 又不该 import ``loop/``。
于是同一个问题有了两个答案：她刚被动回过一句，冷却那侧看不见，就出现
「刚回完又搭一句」。行格式与读判据下移到这一层：两层都能问，但只有一处定义。

约定：文件仍是 ``data/loop/ledger-<YYYY-MM-DD>.jsonl``，追加式、一行一个动作；
``loop/ledger.py`` 只留语义（承诺、摘要、被拦目标），不再自己拼行。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from core.infra import skips
from core.infra.envflags import text as _env_text  # 环境值只从这一层读（check.py envread）

from core.infra import duration
from core.infra.jsonio import atomic_write_text
from safelog import warn

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 2026-11-22：流水也要能改道。以前 DIR 写死在生产 data/loop，测试（尤其直接
# pytest、不走 run_all_tests 隔离）会把「say sent」这种假行写进她的真实账本，
# 「最近开口」就会读到她从没说过的话。
DIR = _env_text("YINYUE_LEDGER_DIR") or os.path.join(ROOT, "data", "loop")


def path(day: str | None = None) -> str:
    day = day or datetime.now().strftime("%Y-%m-%d")
    os.makedirs(DIR, exist_ok=True)
    return os.path.join(DIR, "ledger-%s.jsonl" % day)


def append_row(row: dict) -> dict:
    """唯一写口：追加一行。记账失败不许影响一拍。"""
    try:
        with open(path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        warn("actions_append_row_failed", err=repr(__import__("sys").exc_info()[1]))
    return row


def merge_last(row: dict) -> bool:
    # 2026-10-28: same block/fail writes two rows within seconds; merge into last line
    try:
        if row.get("result") not in ("error", "failed"):
            return False
        p = path()
        with open(p, encoding="utf-8") as fh:
            rows = [l for l in fh if l.strip()]
        if not rows:
            return False
        last = json.loads(rows[-1])
        if (last.get("turn_id") != row.get("turn_id")
                or last.get("act") != row.get("act")
                or last.get("target") != row.get("target")
                or last.get("result") not in ("error", "failed")
                or last.get("at") != row.get("at")):
            return False
        merged = dict(last)
        merged.update({k: v for k, v in row.items() if v not in (None, "", [])})
        if not merged.get("text") and last.get("text"):
            merged["text"] = last["text"]
        rows[-1] = json.dumps(merged, ensure_ascii=False) + chr(10)
        atomic_write_text(p, "".join(rows))
        return True
    except Exception:
        return False


def append_receipt(*, act: str, target: str = "", why: str = "", refs=None,
                   result: str = "", blocked_reason: str = "", turn_id: str = "",
                   at: str = "", ack: str = "", text: str = "",
                   extra: dict | None = None) -> dict:
    row = {"at": at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "turn_id": turn_id,
           "act": act, "target": target, "why": why, "refs": refs or [],
           "result": result, "blocked_reason": blocked_reason,
           "ack": ack}
    if text or act == "say":  # 2026-10-28 say 一律留正文位
        row["text"] = text
    if extra:  # 2026-10-27：发送计时等结构化旁证落账
        row.update({k: v for k, v in extra.items() if v is not None})
    if merge_last(row):  # 2026-10-28 onerow
        return row
    return append_row(row)


def read_receipts(limit: int = 10, *, day: str | None = None) -> list[dict]:
    try:
        with open(path(day), encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
        return rows[-limit:]
    except FileNotFoundError:
        # 新的一天还没写过流水是正常状态，不是读失败（实测 1617 条噪声都来自这里）。
        return []
    except Exception:  # noqa: BLE001
        warn("actions_read_receipts_failed", err=repr(__import__("sys").exc_info()[1]))
        return []


def receipt_in_scope(row: dict, scope: tuple = ()) -> bool:
    """Whether a receipt belongs to the active platform/account/chat scope."""
    if not scope:
        return True
    saved = (row or {}).get("scope")
    if isinstance(saved, dict):
        return (
            str(saved.get("platform") or ""),
            str(saved.get("account_id") or ""),
            str(saved.get("chat_id") or ""),
        ) == scope
    return False


def rows_for_days(days: int) -> list[dict]:
    out = []
    for i in range(days):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            with open(path(day), encoding="utf-8") as fh:
                out.extend(json.loads(l) for l in fh if l.strip())
        except Exception:  # noqa: BLE001 - 那天的账不在就是没有
            skips.skip("actions.rows_for_days", "absent", day)
            continue
    return out


_KIND_CACHE = {}


def rows_for_kinds(kinds: tuple[str, ...]) -> list[dict]:
    """读取保留流水中的指定事件；日期不决定事件有效期，缓存只保存可重建结果。"""
    import copy
    wanted = frozenset(kinds)
    try:
        with os.scandir(DIR) as entries:
            files = sorted((e.path for e in entries
                            if e.name.startswith("ledger-") and e.name.endswith(".jsonl")
                            and e.is_file()))
    except FileNotFoundError:
        return []
    out, cache = [], {}
    for filename in files:
        try:
            stat = os.stat(filename)
            stamp = (stat.st_mtime_ns, stat.st_size, wanted)
            hit = _KIND_CACHE.get(filename)
            if hit is not None and hit[0] == stamp:
                rows = hit[1]
            else:
                rows = []
                with open(filename, encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError("流水行必须是对象")
                        if row.get("kind") in wanted:
                            rows.append(row)
            cache[filename] = (stamp, rows)
            out.extend(rows)
        except (OSError, ValueError) as exc:
            warn("actions_kind_read_failed", path=filename, error=repr(exc))
            raise
    _KIND_CACHE.clear()
    _KIND_CACHE.update(cache)
    return copy.deepcopy(out)


def _last_say(target: str, *, scope: tuple = ()) -> tuple[float, str]:
    """(最近一次真对他说出话的时刻 epoch 秒, 那一句所属 turn_id)；没说过 (0.0, "")。

    2026-11-02：原来只有 `last_say_at` 一个出口，把「多久前」和「哪一回合」分开了问
    就变成了两个实现。合到一处扫，`last_say_at` / `said_this_turn` 都从这里拿。
    """
    if not target:
        return 0.0, ""
    newest, turn = 0.0, ""
    for d in read_receipts(limit=1000 if scope else 300):
        # 注意：普通回执行没有 kind 字段（只有投递补账行才有 kind=="delivery"）。
        if d.get("kind") == "delivery":
            continue
        if not receipt_in_scope(d, scope):
            continue
        if d.get("act") != "say" or d.get("result") != "sent":
            continue
        if str(d.get("target") or "") != target:
            continue
        try:
            ts = datetime.strptime(d.get("at", ""), "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:  # noqa: BLE001
            skips.skip("actions.last_say_at", "malformed", str(d.get("at"))[:40])
            continue
        if ts >= newest:  # 同一秒里取后面那一行（账本是追加的，后面的更新）
            newest, turn = ts, str(d.get("turn_id") or "")
    return newest, turn


def last_say_at(target: str, *, scope: tuple = ()) -> float:
    """这个人最近一次被我真正说过话的时刻（epoch 秒，没说过返回 0）。"""
    return _last_say(target, scope=scope)[0]


def said_this_turn(target: str, turn_id: str, *, scope: tuple = ()) -> bool:
    """这一回合里我是不是已经对这个 target 说过一句。

    为什么要这个判据（2026-11-02）：`SAME_TARGET_COOLDOWN_MS`（25 秒）的本意是
    **跨回合**的「刚跟这个人说完，等对方先回一句」——防的是两拍之间贴脸连发。
    可它在同一回合内也照拦：她一拍里想发第二条气泡，1.5 秒后就被判「等对方先回一句」，
    而 1.5 秒里不可能有人回过话。提示词又明确允许她「想发几条就发几条」，
    于是「说得出来、发不出去」。同日账本 3 次实测都是这个。
    """
    if not target or not turn_id:
        return False
    ts, turn = _last_say(target, scope=scope)
    return bool(ts) and turn == turn_id


def quiet_ms(target: str, *, scope: tuple = ()) -> int | None:
    """距我上次真正对这个 target 说话过了多少**毫秒**；没说过返回 None。

    PIT-111：这是「同一窗口多久没说话」的**唯一**来源。谁要判发车间隔都调它，
    别在别处自己存时间戳——两处各判一次，迟早出现「刚发完又发 / 一直被自己拦住」。
    """
    t = last_say_at(target, scope=scope)
    return None if t <= 0 else max(0, duration.now_ms() - duration.from_epoch_s(t))
