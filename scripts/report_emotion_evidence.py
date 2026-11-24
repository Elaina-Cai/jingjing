# -*- coding: utf-8 -*-
"""P1 情绪层量具基线报表（只读）。

口径见 docs/decisions/穷举整改_情绪层_P1设计.md §5「量具口径」：
  1) 证据覆盖率      带 basis.message_id 的 emotion_evidence 事件 / 同期回合
  2) 每轮候选数分布  0 / 1 / 2+
  3) 读侧差异率      默认透镜（本脚本独立重放）vs 日志里现状 current_mood/tone_hint 读数
  4) scope 放开面    TRACKED_TIERS 白名单之外但仍可达的私聊对象

用法：
  .\\.venv_it\\Scripts\\python.exe scripts\\report_emotion_evidence.py --since 2026-10-01T00:00
  .\\.venv_it\\Scripts\\python.exe scripts\\report_emotion_evidence.py --json output\\emotion_evidence.json

只读：世界模型 JSON + logs/yinyue.log + data/yinyue_meta.db（read_only_connection）。
不调 LLM、不写库、不改状态；默认不落盘（--json 才写）。
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import temporal  # noqa: E402

SHADOW_EVENT = "emotion_layers_shadow"
OUTBOUND_EVENT = "emotion_layers_outbound"
AFFECT_EVENT = "affect_observed"
TONE_EVENT = "tone_target_readout"
_TOUCHED = (SHADOW_EVENT, OUTBOUND_EVENT, AFFECT_EVENT, TONE_EVENT)

# core/affect/scope.py:26 的白名单（现状口径，用于算放开面）
TRACKED_TIERS = frozenset({"亲密", "熟悉"})
GROUP_TIER = "群聊"

_STAMP_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")
MOOD_HALF_LIFE_HOURS = 48.0  # core/context/emotion.py:305
_MOOD_HALF_LIFE_ENV = "YINYUE_MOOD_HALF_LIFE_HOURS"
_MOOD_HALF_LIFE_MIN = 1.0
_MOOD_HALF_LIFE_MAX = 720.0

_TONE_RE = re.compile(r"^此刻心境(?P<mood>.+?)、对(?P<who>.*?)态度(?P<attitude>.+?)：")
_REF_KEYS = ("message_id", "basis", "span", "start", "end")


def _parse_stamp(raw):
    """世界模型事件 at（本地 naive；格式表照抄 core/context/emotion.py:53）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in _STAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _log_time(raw):
    """日志 timestamp（UTC ISO Z）-> 本地 naive，和事件 at 同一时间轴。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return temporal.to_naive_local(parsed)


def mood_half_life_hours() -> float:
    """照抄 core/context/emotion.py:331（env 覆盖 + 区间钳制）。"""
    raw = (os.environ.get(_MOOD_HALF_LIFE_ENV) or "").strip()
    if not raw:
        return MOOD_HALF_LIFE_HOURS
    try:
        value = float(raw)
    except ValueError:
        return MOOD_HALF_LIFE_HOURS
    if value <= 0:
        return MOOD_HALF_LIFE_HOURS
    return min(max(value, _MOOD_HALF_LIFE_MIN), _MOOD_HALF_LIFE_MAX)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def label_of(level: float) -> str:
    """core/context/emotion.py:344 的 5 档。"""
    if level >= 0.5:
        return "很暖"
    if level >= 0.15:
        return "偏暖"
    if level > -0.15:
        return "平"
    if level > -0.5:
        return "偏冷"
    return "很冷"


def tone_branch(mood_level: float, attitude_level: float) -> str:
    """core/context/emotion.py:399 的 4 分支名（顺序即判定顺序）。"""
    if mood_level <= -0.5 or attitude_level <= -0.5:
        return "cold"
    if mood_level >= 0.35 or attitude_level >= 0.35:
        return "warm"
    if mood_level <= -0.15 or attitude_level <= -0.15:
        return "plain"
    return "neutral"


def tone_hint(mood_level: float, mood_label: str, attitude_level: float, attitude_label: str, who: str) -> str:
    """core/context/emotion.py:399 的 4 分支文案。"""
    name = who or "他"
    if mood_level <= -0.5 or attitude_level <= -0.5:
        return "此刻心境%s、对%s态度%s：收着点，少接话、别热络。" % (mood_label, name, attitude_label)
    if mood_level >= 0.35 or attitude_level >= 0.35:
        return "此刻心境%s、对%s态度%s：可以放松一点，主动也不突兀。" % (mood_label, name, attitude_label)
    if mood_level <= -0.15 or attitude_level <= -0.15:
        return "此刻心境%s、对%s态度%s：保持平常，不必刻意亲近。" % (mood_label, name, attitude_label)
    return "此刻心境%s、对%s态度%s：照常即可。" % (mood_label, name, attitude_label)


def fold_emotion_events(events, *, now, hours: float, who_id: str = ""):
    """独立重放 core/context/emotion.py:356 的折叠（0.5 ** (age/H)）。"""
    total = 0.0
    samples = 0
    for event in events:
        if not isinstance(event, dict) or str(event.get("kind") or "") != "emotion_observed":
            continue
        payload = event.get("payload") or {}
        if who_id and str(payload.get("who_id") or "") != who_id:
            continue
        stamp = _parse_stamp(event.get("at"))
        if stamp is None or stamp > now:
            continue
        age_h = max(0.0, (now - stamp).total_seconds() / 3600.0)
        total += float(payload.get("level", 0.0) or 0.0) * (0.5 ** (age_h / hours))
        samples += 1
    return _clamp(total, -1.0, 1.0), samples


def load_world_models(directory):
    models = {}
    directory = pathlib.Path(directory)
    if not directory.exists():
        return models
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏文件不该炸报表
            continue
        if isinstance(data, list):
            models[path.stem] = data
    return models


def iter_log(log_path, since: str = ""):
    path = pathlib.Path(log_path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not any(key in line for key in _TOUCHED):
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001 - 非 JSON 行跳过
            continue
        stamp = str(obj.get("timestamp") or "")
        if since and stamp and stamp < since:
            continue
        yield obj


def _observed_stamps(model):
    out = []
    for event in model or ():
        if isinstance(event, dict) and str(event.get("kind") or "") == "emotion_observed":
            stamp = _parse_stamp(event.get("at"))
            if stamp is not None:
                out.append(stamp)
    out.sort()
    return out


def _rate(num: int, den: int):
    if not den:
        return "n/a"
    return round(num / den, 4)


def collect_world_models(models, chat_index):
    per_chat = []
    totals = collections.Counter()
    for stem, events in sorted(models.items()):
        kinds = collections.Counter(str(e.get("kind") or "") for e in events if isinstance(e, dict))
        totals.update(kinds)
        observed = [e for e in events if isinstance(e, dict) and str(e.get("kind")) == "emotion_observed"]
        with_span = 0
        who_empty = 0
        who_named = 0
        for event in observed:
            payload = event.get("payload") or {}
            if any(payload.get(key) for key in _REF_KEYS):
                with_span += 1
            if str(payload.get("who_id") or ""):
                who_named += 1
            else:
                who_empty += 1
        stamps = _observed_stamps(events)
        per_chat.append({
            "file": stem,
            "chat_id": chat_index.get(stem, ""),
            "events": len(events),
            "emotion_observed": len(observed),
            "emotion_observed_with_span": with_span,
            "who_id_empty": who_empty,
            "who_id_named": who_named,
            "message_observed": kinds.get("message_observed", 0),
            "emotion_per_message": _rate(len(observed), kinds.get("message_observed", 0)),
            "first_observed_at": stamps[0].isoformat(sep=" ") if stamps else "",
            "last_observed_at": stamps[-1].isoformat(sep=" ") if stamps else "",
            "kinds": dict(sorted(kinds.items())),
        })
    observed_total = sum(row["emotion_observed"] for row in per_chat)
    return {
        "files": len(per_chat),
        "emotion_observed": observed_total,
        "emotion_observed_with_span": sum(row["emotion_observed_with_span"] for row in per_chat),
        "emotion_observed_with_who_id": sum(row["who_id_named"] for row in per_chat),
        "message_observed": sum(row["message_observed"] for row in per_chat),
        "kinds": dict(sorted(totals.items())),
        "per_chat": per_chat,
    }


def collect_turn_metrics(log_events, models, *, window_seconds: int):
    per_chat = collections.defaultdict(list)
    for obj in log_events:
        if obj.get("event") != SHADOW_EVENT:
            continue
        chat_id = str(obj.get("chat_id") or "")
        stamp = _log_time(obj.get("timestamp"))
        if chat_id and stamp is not None:
            per_chat[chat_id].append((stamp, obj))
    buckets = collections.Counter()
    turns = 0
    with_evidence = 0
    without_model = 0
    per_turn = []
    for chat_id, rows in per_chat.items():
        rows.sort(key=lambda item: item[0])
        model = models.get(hashlib.sha1(chat_id.encode("utf-8")).hexdigest()[:16])
        stamps = _observed_stamps(model)
        prev = None
        for stamp, _obj in rows:
            turns += 1
            low = stamp - timedelta(seconds=window_seconds)
            if prev is not None and prev > low:
                low = prev
            count = sum(1 for s in stamps if low < s <= stamp)
            bucket = "0" if count == 0 else "1" if count == 1 else "2+"
            buckets[bucket] += 1
            if count:
                with_evidence += 1
            if model is None:
                without_model += 1
            per_turn.append({
                "chat_id": chat_id,
                "at": stamp.isoformat(sep=" "),
                "observed_in_turn": count,
                "has_model": model is not None,
            })
            prev = stamp
    return {
        "source": SHADOW_EVENT,
        "window_seconds": window_seconds,
        "turns": turns,
        "buckets": {key: buckets.get(key, 0) for key in ("0", "1", "2+")},
        "buckets_rate": {key: _rate(buckets.get(key, 0), turns) for key in ("0", "1", "2+")},
        "turns_with_any_evidence": with_evidence,
        "turns_without_model": without_model,
        "per_turn": per_turn,
    }


def collect_read_side(log_events, models, *, hours: float):
    rows = []
    for obj in log_events:
        if obj.get("event") not in (SHADOW_EVENT, OUTBOUND_EVENT):
            continue
        chat_id = str(obj.get("chat_id") or "")
        stamp = _log_time(obj.get("timestamp"))
        model = models.get(hashlib.sha1(chat_id.encode("utf-8")).hexdigest()[:16]) if chat_id else None
        rec = {
            "event": str(obj.get("event") or ""),
            "chat_id": chat_id,
            "at": stamp.isoformat(sep=" ") if stamp else "",
            "has_model": model is not None,
            "comparable": False,
            "mood_level_diff": None,
            "mood_label_diff": None,
            "mood_samples_diff": None,
            "tone_diff": None,
            "who": "",
        }
        if model is None or stamp is None:
            rows.append(rec)
            continue
        level, samples = fold_emotion_events(model, now=stamp, hours=hours)
        att_level, _att_samples = fold_emotion_events(model, now=stamp, hours=hours, who_id="")
        mood_label = label_of(level)
        att_label = label_of(att_level)
        logged_tone = str(obj.get("tone") or "")
        match = _TONE_RE.match(logged_tone)
        who = match.group("who") if match else ""
        replay_tone = tone_hint(level, mood_label, att_level, att_label, who)
        try:
            logged_level = round(float(obj.get("mood_level")), 3)
        except (TypeError, ValueError):
            logged_level = None
        rec.update({
            "comparable": True,
            "who": who,
            "logged_level": logged_level,
            "replay_level": round(level, 3),
            "logged_label": str(obj.get("mood_label") or ""),
            "replay_label": mood_label,
            "logged_samples": int(obj.get("mood_samples") or 0),
            "replay_samples": samples,
            "logged_tone": logged_tone,
            "replay_tone": replay_tone,
            "logged_attitude_level": obj.get("attitude_level"),
            "replay_attitude_level": round(att_level, 3),
            "branch": tone_branch(level, att_level),
        })
        rec["mood_level_diff"] = logged_level is not None and abs(logged_level - level) >= 0.001
        rec["mood_label_diff"] = rec["logged_label"] != mood_label
        rec["mood_samples_diff"] = rec["logged_samples"] != samples
        rec["tone_diff"] = logged_tone != replay_tone
        rows.append(rec)
    comparable = [r for r in rows if r["comparable"]]
    diff = [r for r in comparable if r["mood_level_diff"] or r["mood_label_diff"] or r["tone_diff"]]
    branches = collections.Counter(str(r.get("branch") or "") for r in comparable)
    levels = collections.Counter(str(r.get("logged_level")) for r in comparable)
    return {
        "rows": len(rows),
        "comparable": len(comparable),
        "no_model": len([r for r in rows if not r["comparable"]]),
        "no_model_targets": sorted({r["chat_id"] for r in rows if not r["comparable"]}),
        "diff_rows": len(diff),
        "diff_rate": _rate(len(diff), len(comparable)),
        "mood_level_diff": len([r for r in comparable if r["mood_level_diff"]]),
        "mood_label_diff": len([r for r in comparable if r["mood_label_diff"]]),
        "mood_samples_diff": len([r for r in comparable if r["mood_samples_diff"]]),
        "tone_diff": len([r for r in comparable if r["tone_diff"]]),
        "attitude_equals_mood": len([
            r for r in comparable
            if r.get("logged_attitude_level") is not None
            and abs(float(r.get("logged_attitude_level") or 0.0) - float(r.get("logged_level") or 0.0)) < 0.001
        ]),
        "branches_covered": dict(sorted(branches.items())),
        "logged_level_values": dict(sorted(levels.items())),
        "detail": rows,
    }


def collect_legacy_readouts(log_events):
    affect = {
        "rows": 0,
        "with_evidence_ref": 0,
        "bands": {},
        "reasons": {},
        "signal_combos": {},
        "rendered": 0,
        "injected": 0,
        "clamped_levels": 0,
        "block_chars_avg": "n/a",
    }
    tone = {
        "rows": 0,
        "modes": {},
        "applied": 0,
        "hits_total": 0,
        "rows_with_hits": 0,
        "hits": {},
        "forms": {},
        "chars_avg": "n/a",
        "sentences_avg": "n/a",
    }
    bands = collections.Counter()
    reasons = collections.Counter()
    combos = collections.Counter()
    modes = collections.Counter()
    hits = collections.Counter()
    forms = collections.Counter()
    chars = []
    sentences = []
    block_chars = []
    for obj in log_events:
        event = str(obj.get("event") or "")
        if event == AFFECT_EVENT:
            affect["rows"] += 1
            bands[str(obj.get("band") or "")] += 1
            reasons[str(obj.get("reason") or "")] += 1
            signals = obj.get("signals") if isinstance(obj.get("signals"), dict) else {}
            combos["owner=%s,replied=%s,waited>0=%s" % (
                bool(signals.get("owner_initiated")),
                bool(signals.get("replied_to_her")),
                float(signals.get("waited_minutes") or 0.0) > 0,
            )] += 1
            if obj.get("rendered"):
                affect["rendered"] += 1
            if obj.get("injected"):
                affect["injected"] += 1
            if any(obj.get(key) for key in _REF_KEYS):
                affect["with_evidence_ref"] += 1
            try:
                if abs(float(obj.get("affect_level") or 0.0)) >= 1.0:
                    affect["clamped_levels"] += 1
            except (TypeError, ValueError):
                pass
            try:
                block_chars.append(float(obj.get("block_chars") or 0))
            except (TypeError, ValueError):
                pass
        elif event == TONE_EVENT:
            tone["rows"] += 1
            modes[str(obj.get("mode") or "")] += 1
            if obj.get("applied"):
                tone["applied"] += 1
            row_hits = list(obj.get("hits") or ())
            tone["hits_total"] += len(row_hits)
            if row_hits:
                tone["rows_with_hits"] += 1
            for name in row_hits:
                hits[str(name)] += 1
            for name in obj.get("forms") or ():
                forms[str(name)] += 1
            try:
                chars.append(float(obj.get("chars") or 0))
                sentences.append(float(obj.get("sentences") or 0))
            except (TypeError, ValueError):
                pass
    affect["bands"] = dict(sorted(bands.items()))
    affect["reasons"] = dict(sorted(reasons.items()))
    affect["signal_combos"] = dict(sorted(combos.items()))
    affect["block_chars_avg"] = round(sum(block_chars) / len(block_chars), 1) if block_chars else "n/a"
    tone["modes"] = dict(sorted(modes.items()))
    tone["hits"] = dict(sorted(hits.items()))
    tone["forms"] = dict(sorted(forms.items()))
    tone["chars_avg"] = round(sum(chars) / len(chars), 1) if chars else "n/a"
    tone["sentences_avg"] = round(sum(sentences) / len(sentences), 1) if sentences else "n/a"
    return {"affect_observed": affect, "tone_target_readout": tone}


def derive_tier(relation_type, intimacy) -> str:
    """core/speech/turn.py:15-37 的桶化：先信 relation_type，否则按 intimacy 分三桶。"""
    explicit = str(relation_type or "").strip()
    if explicit:
        return explicit
    try:
        value = float(intimacy)
    except (TypeError, ValueError):
        value = 0.5
    if value >= 0.8:
        return "亲密"
    if value >= 0.5:
        return "熟悉"
    return "普通"


def _is_group_target(target: str, tier: str) -> bool:
    text = str(target or "")
    return tier == GROUP_TIER or "@chatroom" in text or text.startswith("group:")


def _read_only_connection(meta_db):
    from db.write_guard import read_only_connection

    path = str(meta_db or "").strip()
    if not path or not pathlib.Path(path).exists():
        raise FileNotFoundError("meta 库不存在")
    return read_only_connection(path)


def collect_scope(meta_db):
    result = {
        "relationships_rows": "n/a",
        "tier_counts": {},
        "blocked_private_rows": "n/a",
        "blocked_private_targets": [],
        "private_chats_total": "n/a",
        "private_chats_without_row": "n/a",
        "private_chats_without_row_list": [],
        "notes": [],
    }
    try:
        from sqlalchemy import text
    except Exception as exc:  # noqa: BLE001
        result["notes"].append(f"sqlalchemy 不可用：{exc}")
        return result
    try:
        with _read_only_connection(meta_db) as conn:
            rel = conn.execute(text("select relation_type, intimacy, target_wxid from relationships")).fetchall()
            tiers = collections.Counter()
            blocked = []
            for relation_type, intimacy, target in rel:
                tier = derive_tier(relation_type, intimacy)
                tiers[tier] += 1
                if tier not in TRACKED_TIERS and not _is_group_target(str(target or ""), tier):
                    blocked.append({
                        "target": str(target or ""),
                        "tier": tier,
                        "relation_type": str(relation_type or ""),
                        "intimacy": intimacy,
                    })
            total_private = conn.execute(text(
                "select count(distinct chat_id) from terminal_events where chat_type = 'private'"
            )).scalar() or 0
            missing = conn.execute(text(
                "select distinct t.chat_id from terminal_events t "
                "left join relationships r on r.target_wxid = t.chat_id "
                "where t.chat_type = 'private' and t.chat_id not like '%@chatroom%' and r.target_wxid is null "
                "order by 1"
            )).fetchall()
        result.update({
            "relationships_rows": len(rel),
            "tier_counts": dict(sorted(tiers.items())),
            "blocked_private_rows": len(blocked),
            "blocked_private_targets": blocked[:10],
            "private_chats_total": int(total_private),
            "private_chats_without_row": len(missing),
            "private_chats_without_row_list": [str(row[0]) for row in missing][:10],
        })
    except Exception as exc:  # noqa: BLE001 - 读数失败不该炸
        result["notes"].append(f"DB 读数失败：{exc}")
    return result


def collect_turns_cross_check(meta_db, *, lo, hi):
    out = {
        "turns_total": "n/a",
        "turns_in_window": "n/a",
        "turns_by_chat_type": {},
        "replies_in_window": "n/a",
        "window": [lo.isoformat(sep=" ") if lo else "", hi.isoformat(sep=" ") if hi else ""],
        "notes": [],
    }
    try:
        from sqlalchemy import text
    except Exception as exc:  # noqa: BLE001
        out["notes"].append(f"sqlalchemy 不可用：{exc}")
        return out
    try:
        with _read_only_connection(meta_db) as conn:
            out["turns_total"] = int(conn.execute(text(
                "select count(distinct turn_id) from terminal_events where coalesce(turn_id,'') <> ''"
            )).scalar() or 0)
            for chat_type, count in conn.execute(text(
                "select chat_type, count(distinct turn_id) from terminal_events "
                "where coalesce(turn_id,'') <> '' group by 1 order by 2 desc"
            )).fetchall():
                out["turns_by_chat_type"][str(chat_type or "")] = int(count)
            if lo is not None and hi is not None:
                args = {"lo": lo.strftime("%Y-%m-%d %H:%M:%S"), "hi": hi.strftime("%Y-%m-%d %H:%M:%S")}
                out["turns_in_window"] = int(conn.execute(text(
                    "select count(distinct turn_id) from terminal_events where coalesce(turn_id,'') <> '' "
                    "and occurred_at >= :lo and occurred_at <= :hi"
                ), args).scalar() or 0)
                out["replies_in_window"] = int(conn.execute(text(
                    "select count(*) from terminal_events where event_type = 'reply' "
                    "and occurred_at >= :lo and occurred_at <= :hi"
                ), args).scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        out["notes"].append(f"DB 读数失败：{exc}")
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:  # noqa: BLE001 - 老终端不支持就算了
        pass
    parser = argparse.ArgumentParser(description="P1 情绪层量具基线（只读）")
    parser.add_argument("--log", default=str(ROOT / "logs" / "yinyue.log"))
    parser.add_argument(
        "--world-model-dir",
        default=os.environ.get("YINYUE_WORLD_MODEL_DIR") or str(ROOT / "data" / "world_models"),
    )
    parser.add_argument("--meta-db", default=str(ROOT / "data" / "yinyue_meta.db"))
    parser.add_argument("--since", default="", help="UTC ISO 前缀，例如 2026-10-01T00:00")
    parser.add_argument("--window-seconds", type=int, default=120, help="每轮证据匹配窗口（默认 120s）")
    parser.add_argument("--json", default="", help="可选：把报表写到该路径")
    args = parser.parse_args()

    log_events = list(iter_log(args.log, args.since))
    chat_ids = sorted({str(o.get("chat_id") or "") for o in log_events if o.get("chat_id")})
    chat_index = {hashlib.sha1(c.encode("utf-8")).hexdigest()[:16]: c for c in chat_ids}
    models = load_world_models(args.world_model_dir)
    hours = mood_half_life_hours()

    counters = collections.Counter(str(o.get("event") or "") for o in log_events)
    world = collect_world_models(models, chat_index)
    turns = collect_turn_metrics(log_events, models, window_seconds=args.window_seconds)
    read_side = collect_read_side(log_events, models, hours=hours)
    scope = collect_scope(args.meta_db)
    legacy = collect_legacy_readouts(log_events)
    per_turn_times = [_log_time(item.get("timestamp")) for item in log_events if item.get("event") == SHADOW_EVENT]
    per_turn_times = [t for t in per_turn_times if t is not None]
    cross = collect_turns_cross_check(
        args.meta_db,
        lo=min(per_turn_times) if per_turn_times else None,
        hi=max(per_turn_times) if per_turn_times else None,
    )

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sources": {
            "log": str(args.log),
            "world_model_dir": str(args.world_model_dir),
            "meta_db": str(args.meta_db),
            "since": args.since,
            "log_events": dict(sorted(counters.items())),
            "world_model_files": world["files"],
            "mood_half_life_hours": hours,
        },
        "1_evidence_coverage": {
            "designed_numerator": world["emotion_observed_with_span"],
            "designed_denominator": turns["turns"],
            "designed_rate": _rate(world["emotion_observed_with_span"], turns["turns"]),
            "legacy_numerator": turns["turns_with_any_evidence"],
            "legacy_rate": _rate(turns["turns_with_any_evidence"], turns["turns"]),
            "note": "设计口径分子=带 basis.message_id 的 emotion_evidence 事件（现状 0）；"
                    "legacy 分子=本轮区间内有 emotion_observed 写侧留痕",
        },
        "2_candidates_per_turn": turns,
        "3_read_side_parity": read_side,
        "4_scope_exposure": scope,
        "5_legacy_readouts": legacy,
        "6_turns_cross_check": cross,
        "world_models": world,
    }
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
