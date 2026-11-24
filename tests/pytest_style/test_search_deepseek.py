"""DeepSeek 自研搜索/Fetch 接入的三态契约（off 逐字旧行为 / shadow 只读数 / on 换首顺位）。"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agents.tools import web as web_mod
from agents.tools.web import FetchUrlTool, WebSearchTool
from core import features
from core.config.settings import config


# ---------------------------------------------------------------- payload


def test_payload_freshness_maps_to_after_operator(monkeypatch):
    payload = web_mod._deepseek_payload("鲸鲸 版本", 5, "week")
    assert payload["count"] == 10  # count 默认 10（按请求计费，多要本地裁）
    assert payload["need_content"] is True
    day = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    assert payload["query"].endswith(f"after:{day}")
    assert payload["query"].startswith("鲸鲸 版本")


def test_payload_count_clamped_and_query_truncated():
    assert web_mod._deepseek_payload("q", 1, "", count=0)["count"] == 1
    assert web_mod._deepseek_payload("q", 1, "", count=999)["count"] == 50
    long_query = "很长" * 200
    assert len(web_mod._deepseek_payload(long_query, 5, "")["query"]) == 128


def test_payload_no_freshness_keeps_query_clean():
    payload = web_mod._deepseek_payload("今天 天气", 5, "")
    assert payload["query"] == "今天 天气"


# ---------------------------------------------------------------- results


def test_results_mapped_to_local_shape():
    data = {
        "results": [
            {
                "title": "鲸鲸 v2",
                "url": "https://example.com/a",
                "snippet": "短摘",
                "content": "正文" * 800,
                "date": "2026-10-01",
                "authority": 4,
                "rerank_score": 2.58,
            }
        ]
    }
    results = web_mod._deepseek_results(data)
    assert len(results) == 1
    r = results[0]
    assert r["title"] == "鲸鲸 v2" and r["url"] == "https://example.com/a"
    assert r["source"] == "deepseek"
    assert r["published_date"] == "2026-10-01"
    assert r["authority"] == 4 and r["score"] == 2.58
    assert len(r["content"]) == 1200  # 与生产实测同量级截断
    assert web_mod._deepseek_results({}) == []


# ---------------------------------------------------------------- flag


def test_flag_registered_and_off_by_default(monkeypatch):
    monkeypatch.delenv("YINYUE_FEATURE_SEARCH_DEEPSEEK", raising=False)
    assert features.SEARCH_DEEPSEEK in features.TRI_STATE_FLAGS
    assert features.SEARCH_DEEPSEEK in features.FLAG_REGISTRY
    assert features.mode(features.SEARCH_DEEPSEEK, features.MODE_OFF) == features.MODE_OFF
    # +JOB_LEDGER（U-10 在手的事台账）：77 → 78
    assert len(features.TRI_STATE_FLAGS) == 59  # 2026-11-03 P1-1 销账 +1（OPEN_LOOP_CLOSE）57 -> 58；历史：68 → 读场/按需记忆 -5 → 63 → A6 出站语义三项（REPEAT_GUARD / ASSERTION_GUARD / REPEAT_SUPPRESS）-3… 实为 59（见 core/features.py 表头注释）


# ---------------------------------------------------------------- dispatch


def _tool(monkeypatch, *, legacy, deepseek=None):
    calls: list[str] = []

    async def fake_legacy(self, query, num, freshness=""):
        calls.append("legacy")
        return legacy

    async def fake_deepseek(self, query, num, freshness=""):
        calls.append("deepseek")
        if deepseek is None:
            raise RuntimeError("boom")
        return deepseek

    monkeypatch.setattr(WebSearchTool, "_search_tavily", fake_legacy)
    monkeypatch.setattr(WebSearchTool, "_search_deepseek", fake_deepseek)
    return WebSearchTool(), calls


def test_off_mode_uses_legacy_only(monkeypatch):
    monkeypatch.setenv("YINYUE_FEATURE_SEARCH_DEEPSEEK", "off")
    tool, calls = _tool(monkeypatch, legacy=[{"title": "old"}], deepseek=[{"title": "new"}])
    out = asyncio.run(tool._search_single("q", 3))
    assert out == [{"title": "old"}]
    assert calls == ["legacy"]


def test_on_mode_prefers_deepseek(monkeypatch):
    monkeypatch.setenv("YINYUE_FEATURE_SEARCH_DEEPSEEK", "on")
    tool, calls = _tool(monkeypatch, legacy=[{"title": "old"}], deepseek=[{"title": "new"}])
    out = asyncio.run(tool._search_single("q", 3))
    assert out == [{"title": "new"}]
    assert calls == ["deepseek"]


def test_on_mode_falls_back_when_deepseek_fails(monkeypatch):
    monkeypatch.setenv("YINYUE_FEATURE_SEARCH_DEEPSEEK", "on")
    tool, calls = _tool(monkeypatch, legacy=[{"title": "old"}], deepseek=None)
    out = asyncio.run(tool._search_single("q", 3))
    assert out == [{"title": "old"}]
    assert calls == ["deepseek", "legacy"]


def test_shadow_mode_keeps_legacy_result_and_runs_comparison(monkeypatch):
    monkeypatch.setenv("YINYUE_FEATURE_SEARCH_DEEPSEEK", "shadow")
    seen: list[str] = []

    async def fake_shadow(self, query, num, freshness):
        seen.append(query)

    monkeypatch.setattr(WebSearchTool, "_shadow_deepseek", fake_shadow)
    tool, calls = _tool(monkeypatch, legacy=[{"title": "old"}], deepseek=[{"title": "new"}])

    async def scenario():
        out = await tool._search_single("q", 3)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return out

    assert asyncio.run(scenario()) == [{"title": "old"}]
    assert calls == ["legacy"]
    assert seen == ["q"]


# ---------------------------------------------------------------- fetch


def test_deepseek_key_present_for_on_mode():
    # on 档的前置：生产 .env 里 DeepSeek key 已配置（这里只断言非空，不打印明文）
    assert bool(config.llm.deepseek_api_key)


class _FakeResponse:
    """egress 改为流式读取（`resp.iter_bytes` / `resp.aiter_bytes`）后，假响应得跟着长。"""

    def __init__(self, text: str):
        self.text = text
        self.headers = {"content-type": "text/html"}
        self.status_code = 200
        self._body = text.encode("utf-8")

    def raise_for_status(self):
        return None

    async def aiter_bytes(self, chunk_size: int = 0):
        yield self._body


class _FakeStream:
    """`client.stream(...)` 的异步上下文：只服务 `egress.arequest` 那条读法。"""

    async def __aenter__(self):
        return _FakeResponse("<html><body><p>本地抓到的正文</p></body></html>")

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return _FakeResponse("<html><body><p>本地抓到的正文</p></body></html>")

    def stream(self, method, url, **kwargs):
        return _FakeStream()


def test_fetch_disabled_by_knob_even_when_flag_on(monkeypatch):
    """A/B 只证了 search；fetch 未量 → 开关 on 也不许换抓取。"""
    monkeypatch.setenv("YINYUE_FEATURE_SEARCH_DEEPSEEK", "on")
    monkeypatch.setattr(config.search, "deepseek_fetch_enabled", False)
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    async def forbidden(self, url):
        raise AssertionError("fetch 未验收时不许调 DeepSeek fetch")

    monkeypatch.setattr(FetchUrlTool, "_fetch_deepseek", forbidden)
    result = asyncio.run(FetchUrlTool().execute(url="https://example.com/a"))
    assert "本地抓到的正文" in result.output
    assert (result.data or {}).get("type") != "deepseek_fetch"


def test_fetch_off_never_calls_deepseek(monkeypatch):
    monkeypatch.setenv("YINYUE_FEATURE_SEARCH_DEEPSEEK", "off")
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    async def forbidden(self, url):
        raise AssertionError("off 档不许调 DeepSeek fetch")

    monkeypatch.setattr(FetchUrlTool, "_fetch_deepseek", forbidden)
    result = asyncio.run(FetchUrlTool().execute(url="https://example.com/a"))
    assert "本地抓到的正文" in result.output
    assert (result.data or {}).get("type") != "deepseek_fetch"


def test_fetch_on_uses_deepseek(monkeypatch):
    monkeypatch.setenv("YINYUE_FEATURE_SEARCH_DEEPSEEK", "on")
    monkeypatch.setattr(config.search, "deepseek_fetch_enabled", True)

    async def fake_fetch(self, url):
        return "# 标题\n\nDeepSeek 正文"

    monkeypatch.setattr(FetchUrlTool, "_fetch_deepseek", fake_fetch)
    result = asyncio.run(FetchUrlTool().execute(url="https://example.com/a"))
    assert "DeepSeek 正文" in result.output
    assert (result.data or {}).get("type") == "deepseek_fetch"
