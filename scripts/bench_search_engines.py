# -*- coding: utf-8 -*-
"""外部检索 A/B 量具：DeepSeek /v1/tools/search vs Tavily（真实网络，只读）。

协议直接复刻（刻意不 import agents/tools/web.py，避免被正在改动的业务代码带偏）：
- deepseek: POST {base}/tools/search {"query", "count": 10, "need_content": true}
- tavily:   POST https://api.tavily.com/search {api_key, query, max_results: 5,
            search_depth: advanced, include_answer: true}

指标口径
- hit@1 / hit@3：该题 expect 任一出现在第 1 条 / 前 3 条的正文里
  （正文 = title + snippet + summary + content 摊平；不比对 URL）。
- hit_any：expect 出现在全部结果正文里；hit_answer：出现在 answer / 首条 summary 里。
- answer_len：tavily 的 answer 长度；deepseek 取首条 summary 长度。
- est_cost_cny：DeepSeek 按请求计费，只对 200 且结果非空计 0.005 元；Tavily 未给价，记 null。
- 失败与空结果是两回事：status / error 字段如实记（timeout / http_<码> / empty / exc_<类名>）。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config.settings import config  # noqa: E402

QUERIES_PATH = ROOT / "bench" / "search_queries.json"
TAVILY_URL = "https://api.tavily.com/search"
DEEPSEEK_COST_CNY = 0.005
FRESHNESS_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
MAX_BODY = 300
TAVILY_MAX_RESULTS = 5
DEEPSEEK_QPS_SLEEP = 0.25


def _flat(value: Any) -> str:
    """摊平成单行字符串（None 当空）。"""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(math.ceil(p / 100.0 * len(ordered))) - 1))
    return round(ordered[idx], 1)


def _freshness_days(value: str | None) -> int:
    f = str(value or "").strip().lower()
    if not f:
        return 0
    if f.isdigit():
        return int(f)
    return FRESHNESS_DAYS.get(f, 0)


def _deepseek_payload(query: str, *, count: int, need_content: bool,
                      freshness: str | None) -> dict:
    q = str(query or "").strip()
    days = _freshness_days(freshness)
    if days:
        day = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        q = f"{q} after:{day}"
    return {
        "query": q[:128],
        "count": max(1, min(int(count or 10), 50)),
        "need_content": bool(need_content),
    }


def _tavily_payload(query: str, *, freshness: str | None) -> dict:
    payload = {
        "api_key": config.search.tavily_api_key,
        "query": query,
        "max_results": TAVILY_MAX_RESULTS,
        "include_answer": True,
        "include_raw_content": False,
        "search_depth": "advanced",
    }
    days = _freshness_days(freshness)
    if days:
        payload["topic"] = "news"
        payload["days"] = days
    return payload


@dataclass
class Outcome:
    engine: str
    ok: bool = False
    status: int = 0
    error: str = ""
    error_text: str = ""
    latency_ms: float = 0.0
    texts: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    answer: str = ""
    answer_len: int = 0
    n_results: int = 0

    @property
    def first_title(self) -> str:
        return self.texts[0][:80] if self.texts else ""

    @property
    def first_url(self) -> str:
        return self.urls[0] if self.urls else ""


def _call_deepseek(client: httpx.Client, query: str, freshness: str | None,
                   timeout_s: float) -> Outcome:
    out = Outcome(engine="deepseek")
    payload = _deepseek_payload(
        query,
        count=int(getattr(config.search, "deepseek_count", 10) or 10),
        need_content=bool(getattr(config.search, "deepseek_need_content", True)),
        freshness=freshness,
    )
    base = str(config.llm.deepseek_base_url or "https://api.deepseek.com").rstrip("/")
    t0 = time.perf_counter()
    try:
        resp = client.post(
            f"{base}/tools/search",
            headers={
                "Authorization": f"Bearer {config.llm.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout_s,
        )
        out.status = resp.status_code
        out.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        if resp.status_code != 200:
            out.error = f"http_{resp.status_code}"
            out.error_text = resp.text[:MAX_BODY]
            return out
        data = resp.json()
        results = data.get("results") or []
        out.n_results = len(results)
        for r in results:
            parts = [
                r.get("title"), r.get("snippet"), r.get("summary"), r.get("content"),
                r.get("date"), r.get("authority"), r.get("rerank_score"),
            ]
            out.texts.append(" ".join(p for p in (_flat(x) for x in parts) if p))
            out.urls.append(str(r.get("url") or ""))
        if results:
            out.answer = _flat(results[0].get("summary") or results[0].get("snippet") or "")
        out.answer_len = len(out.answer)
        out.ok = out.n_results > 0
        if not out.ok:
            out.error = "empty"
            out.error_text = json.dumps(data, ensure_ascii=False)[:MAX_BODY]
    except httpx.TimeoutException as exc:
        out.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        out.error = "timeout"
        out.error_text = f"{type(exc).__name__}: {exc}"[:MAX_BODY]
    except Exception as exc:  # noqa: BLE001 - 量具要如实记任何异常
        out.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        out.error = f"exc_{type(exc).__name__}"
        out.error_text = f"{type(exc).__name__}: {exc}"[:MAX_BODY]
    return out


def _call_tavily(client: httpx.Client, query: str, freshness: str | None,
                 timeout_s: float) -> Outcome:
    out = Outcome(engine="tavily")
    t0 = time.perf_counter()
    try:
        resp = client.post(TAVILY_URL, json=_tavily_payload(query, freshness=freshness),
                           timeout=timeout_s)
        out.status = resp.status_code
        out.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        if resp.status_code != 200:
            out.error = f"http_{resp.status_code}"
            out.error_text = resp.text[:MAX_BODY]
            return out
        data = resp.json()
        out.answer = _flat(data.get("answer") or "")
        out.answer_len = len(out.answer)
        results = data.get("results") or []
        out.n_results = len(results)
        for r in results:
            parts = [r.get("title"), r.get("content"), r.get("snippet")]
            out.texts.append(" ".join(p for p in (_flat(x) for x in parts) if p))
            out.urls.append(str(r.get("url") or ""))
        out.ok = bool(out.n_results) or bool(out.answer)
        if not out.ok:
            out.error = "empty"
            out.error_text = json.dumps(data, ensure_ascii=False)[:MAX_BODY]
    except httpx.TimeoutException as exc:
        out.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        out.error = "timeout"
        out.error_text = f"{type(exc).__name__}: {exc}"[:MAX_BODY]
    except Exception as exc:  # noqa: BLE001
        out.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        out.error = f"exc_{type(exc).__name__}"
        out.error_text = f"{type(exc).__name__}: {exc}"[:MAX_BODY]
    return out


def _score(out: Outcome, expects: list[str]) -> dict:
    needles = [str(e).strip().casefold() for e in expects if str(e).strip()]
    texts = [t.casefold() for t in out.texts]

    def hit(k: int) -> bool:
        return any(any(n in t for n in needles) for t in texts[:k])

    joined = " ".join(texts)
    answer = out.answer.casefold()
    return {
        "hit@1": hit(1),
        "hit@3": hit(3),
        "hit_any": any(n in joined for n in needles),
        "hit_answer": bool(answer) and any(n in answer for n in needles),
    }


def _rate(rows: list[dict], key: str) -> float:
    if not rows:
        return 0.0
    return round(sum(1 for r in rows if r.get(key)) / len(rows), 4)


def _block(items: list[dict], engine: str) -> dict:
    oks = [r for r in items if r["ok"]]
    lat = [float(r["latency_ms"]) for r in oks]
    return {
        "engine": engine,
        "requests": len(items),
        "ok": len(oks),
        "failed": len(items) - len(oks),
        "empty": sum(1 for r in items if r["error"] == "empty"),
        "timeout": sum(1 for r in items if r["error"] == "timeout"),
        "http_error": sum(1 for r in items if str(r["error"]).startswith("http_")),
        "exception": sum(1 for r in items if str(r["error"]).startswith("exc_")),
        "hit@1": _rate(oks, "hit@1"),
        "hit@3": _rate(oks, "hit@3"),
        "hit_any": _rate(oks, "hit_any"),
        "hit_answer": _rate(oks, "hit_answer"),
        "latency_p50": _pct(lat, 50),
        "latency_p90": _pct(lat, 90),
        "latency_max": round(max(lat), 1) if lat else 0.0,
        "n_results_mean": round(statistics.fmean(r["n_results"] for r in oks), 2) if oks else 0.0,
        "answer_nonempty": sum(1 for r in oks if r["answer_len"] > 0),
        "est_cost_cny": round(sum(float(r["est_cost_cny"] or 0.0) for r in items), 4),
    }


def summarize(rows: list[dict], engines: list[str]) -> dict:
    by_engine = {e: _block([r for r in rows if r["engine"] == e], e) for e in engines}
    cats = sorted({r["category"] for r in rows})
    by_cat: dict[str, dict] = {}
    for e in engines:
        for c in cats:
            items = [r for r in rows if r["engine"] == e and r["category"] == c]
            if items:
                by_cat[f"{e}|{c}"] = _block(items, e)
    return {"by_engine": by_engine, "by_engine_category": by_cat, "categories": cats}


def _render_md(report: dict) -> str:
    lines: list[str] = []
    lines.append("# 外部检索 A/B：DeepSeek /tools/search vs Tavily")
    lines.append("")
    lines.append(f"生成时间：{report["generated_at"]}；查询 {report["n_queries"]} 条；"
                 f"repeat {report["repeat"]}；引擎 {", ".join(report["engines"])}")
    lines.append("")
    lines.append("## 1. 总表（hit 率只算成功请求；失败单列）")
    lines.append("")
    lines.append("| 引擎 | 请求 | 成功 | 失败(空/超时/HTTP/异常) | hit@1 | hit@3 | hit_any | "
                 "p50 ms | p90 ms | max ms | 平均条数 | 估算成本 ¥ |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for eng, b in report["summary"]["by_engine"].items():
        fails = f"{b["failed"]}({b["empty"]}/{b["timeout"]}/{b["http_error"]}/{b["exception"]})"
        lines.append(
            f"| {eng} | {b["requests"]} | {b["ok"]} | {fails} | {b["hit@1"]:.2%} | "
            f"{b["hit@3"]:.2%} | {b["hit_any"]:.2%} | {b["latency_p50"]} | {b["latency_p90"]} | "
            f"{b["latency_max"]} | {b["n_results_mean"]} | {b["est_cost_cny"]} |"
        )
    lines.append("")
    lines.append("## 2. 引擎 × 类别")
    lines.append("")
    lines.append("| 类别 | 引擎 | 成功/请求 | hit@1 | hit@3 | hit_any | p50 ms | p90 ms |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for key, b in report["summary"]["by_engine_category"].items():
        eng, cat = key.split("|", 1)
        lines.append(
            f"| {cat} | {eng} | {b["ok"]}/{b["requests"]} | {b["hit@1"]:.2%} | "
            f"{b["hit@3"]:.2%} | {b["hit_any"]:.2%} | {b["latency_p50"]} | {b["latency_p90"]} |"
        )
    lines.append("")
    lines.append("## 3. 失败原文（时间/引擎/query/错误）")
    lines.append("")
    failures = [r for r in report["rows"] if not r["ok"]]
    if not failures:
        lines.append("无失败请求。")
    else:
        for r in failures[:20]:
            body = (r["error_text"] or "").replace("|", "/")[:200]
            lines.append(f"- `{r["engine"]}` `{r["id"]}` status={r["status"]} "
                         f"error={r["error"]} query={r["query"]} :: {body}")
    lines.append("")
    lines.append("## 4. 口径与注意")
    lines.append("")
    lines.append("- hit@1/hit@3 只算成功请求（分母 = 成功数）；失败与空结果分开计数，不混为 0 命中。")
    lines.append("- Tavily 成本未给价，记 null；DeepSeek 只对 200 且非空结果计 0.005 元/次。")
    lines.append("- expect 由人工按稳定史实/AI 论文 ID/错误串/站点名给出，口径见 "
                 "bench/search_queries.json 的 note 字段。")
    lines.append("- 本脚本只读：不写生产库、不调 LLM、不 import agents/tools/web.py。")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepSeek vs Tavily 检索 A/B 量具（只读）")
    parser.add_argument("--engines", default="deepseek,tavily")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条查询（0=全部）")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", default=str(ROOT / ".test_tmp" / "search_ab" / "report.json"))
    parser.add_argument("--md", default=str(ROOT / ".test_tmp" / "search_ab" / "report.md"))
    parser.add_argument("--queries", default=str(QUERIES_PATH))
    parser.add_argument("--deepseek-timeout", type=float, default=0.0, help="0=用 config 值")
    parser.add_argument("--tavily-timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    engines = [e.strip().lower() for e in str(args.engines).split(",") if e.strip()]
    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    if args.limit and args.limit > 0:
        queries = queries[: args.limit]
    repeat = max(1, int(args.repeat))

    ds_timeout = float(args.deepseek_timeout or 0.0)
    if ds_timeout <= 0:
        ds_timeout = float(getattr(config.search, "deepseek_timeout_seconds", 20.0) or 20.0)

    rows: list[dict] = []
    with httpx.Client() as client:
        for engine in engines:
            for item in queries:
                for rep in range(repeat):
                    if engine == "deepseek":
                        out = _call_deepseek(client, item["query"], item.get("freshness"),
                                             ds_timeout)
                        time.sleep(DEEPSEEK_QPS_SLEEP)
                    elif engine == "tavily":
                        out = _call_tavily(client, item["query"], item.get("freshness"),
                                           float(args.tavily_timeout))
                    else:
                        raise SystemExit(f"未知引擎：{engine}")
                    rec = {
                        "engine": engine,
                        "id": item["id"],
                        "category": item["category"],
                        "query": item["query"],
                        "expect": item["expect"],
                        "freshness": item.get("freshness"),
                        "repeat": rep,
                        "ok": out.ok,
                        "status": out.status,
                        "error": out.error,
                        "error_text": out.error_text,
                        "n_results": out.n_results,
                        "latency_ms": out.latency_ms,
                        "answer_len": out.answer_len,
                        "first_title": out.first_title,
                        "first_url": out.first_url,
                        "est_cost_cny": (DEEPSEEK_COST_CNY if engine == "deepseek" and out.ok
                                         else (None if engine == "tavily" else 0.0)),
                    }
                    rec.update(_score(out, item["expect"]))
                    rows.append(rec)
                    flag = "OK" if out.ok else f"FAIL:{out.error}"
                    print(f"PZ {engine} {item["id"]} {item["category"]} "
                          f"hit1={int(rec["hit@1"])} hit3={int(rec["hit@3"])} "
                          f"n={out.n_results} {out.latency_ms}ms {flag}", flush=True)

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_queries": len(queries),
        "repeat": repeat,
        "engines": engines,
        "deepseek_timeout_s": ds_timeout,
        "tavily_timeout_s": float(args.tavily_timeout),
        "summary": summarize(rows, engines),
        "rows": rows,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = Path(args.md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_render_md(report), encoding="utf-8")

    print("PZSUMMARY " + json.dumps(report["summary"]["by_engine"], ensure_ascii=False))
    for key, b in report["summary"]["by_engine_category"].items():
        print(f"PZCAT {key} ok={b["ok"]}/{b["requests"]} hit1={b["hit@1"]} hit3={b["hit@3"]} "
              f"p50={b["latency_p50"]} p90={b["latency_p90"]}")
    for r in rows:
        if not r["ok"]:
            body = (r["error_text"] or "").replace("\n", " ")[:160]
            print(f"PZFAIL {r["engine"]} {r["id"]} status={r["status"]} "
                  f"error={r["error"]} query={r["query"]} :: {body}")
    print(f"PZOUT {out_path} | {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
