# -*- coding: utf-8 -*-
"""萌娘百科垂直查询工具：番剧/游戏角色的设定、性格、身世。

比通用搜索精确（直接读条目正文，而不是搜索引擎的摘要碎片）；
同名条目用作品名消歧；未命中时明确返回"未找到"（而不是硬报错），
由上层 agent 改用 web_search 继续——工具自己兜底，让"选错能回头"。
"""
from __future__ import annotations

from urllib.parse import quote

import structlog

from agents.tools.base import BaseTool, ToolResult

log = structlog.get_logger()

_API = "https://zh.moegirl.org.cn/api.php"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


from core.infra import egress


class AcgLookupTool(BaseTool):
    name = "acg_lookup"
    description = (
        "查番剧/动画/游戏/漫画角色的【静态设定】——性格、身世、能力、萌点时优先用我（萌娘百科垂直源，比通用搜索精确）。"
        "【游戏资讯/版本更新/活动/公告等动态内容不要用我】，改用 web_search（时效模式）。"
        "keyword=角色名；work=作品名可选（用于消歧，如「凡人修仙传」）。"
        "我没找到时会明确返回『未找到』，届时请改用 web_search 继续查"
        "（可配合 site:zh.moegirl.org.cn 或 site:bilibili.com 站内搜索）。"
    )
    parameters = {
        "keyword": {"type": "string", "description": "角色/条目名", "required": True},
        "work": {"type": "string", "description": "作品名（可选，用于消歧）", "required": False},
    }

    async def execute(self, **kwargs) -> ToolResult:
        keyword = str(kwargs.get("keyword", "") or "").strip()
        work = str(kwargs.get("work", "") or "").strip()
        if not keyword:
            return ToolResult("", error="keyword 不能为空")
        try:
            async with egress.async_client(
                timeout_ms=20_000, headers={"User-Agent": _UA}
            ) as c:
                titles = await self._search_titles(c, keyword)
                if not titles:
                    return self._miss(keyword, work, [])
                for title in titles[:3]:
                    extract = await self._fetch_extract(c, title)
                    if not extract:
                        continue
                    # 消歧：给了作品名时，要求作品名出现在标题或正文开头
                    if work and work not in (title + extract[:400]):
                        continue
                    return self._hit(title, extract)
                return self._miss(keyword, work, titles)
        except Exception as exc:  # noqa: BLE001 - 工具没测成（网络/解析），与"未找到"区分
            log.warning("acg_lookup_failed", keyword=keyword, error=str(exc)[:120])
            return ToolResult("", error=f"萌娘百科查询失败: {str(exc)[:120]}")

    async def _search_titles(self, c, keyword: str) -> list:
        r = await c.get(_API, params={
            "action": "opensearch", "search": keyword, "format": "json", "limit": 8,
        })
        data = egress.parse_json(r, default=[])
        # opensearch 返回: [query, [titles], [descriptions], [urls]]
        if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
            return [str(t) for t in data[1] if t]
        return []

    async def _fetch_extract(self, c, title: str) -> str:
        r = await c.get(_API, params={
            "action": "query", "titles": title, "prop": "extracts",
            "explaintext": "1", "format": "json", "redirects": "1",
        })
        pages = (egress.parse_json(r).get("query") or {}).get("pages") or {}
        for _pid, pg in pages.items():
            if "missing" not in pg:
                return str(pg.get("extract") or "")
        return ""

    def _hit(self, title: str, extract: str) -> ToolResult:
        url = "https://zh.moegirl.org.cn/" + quote(title)
        body = extract.strip()
        text = (
            f"萌娘百科『{title}』条目（{url}）：\n"
            "（同名条目可能多个；若与你要的作品不符，请带 work 参数重查或改用 web_search）\n"
            f"{body[:1800]}"
        )
        if len(body) > 1800:
            text += "\n…（条目过长，已截断）"
        return ToolResult(text, data={"title": title, "url": url, "chars": len(body)})

    def _miss(self, keyword: str, work: str, titles: list) -> ToolResult:
        cand = "、".join(titles[:5]) if titles else "无"
        work_note = f"（作品：{work}）" if work else ""
        hint = (
            f"萌娘百科未找到『{keyword}』{work_note}的对应条目。"
            f"（opensearch 候选：{cand}）"
            "建议改用 web_search 继续查：可试 site:zh.moegirl.org.cn、"
            "site:bilibili.com（解析视频/专栏）或通用关键词重搜。"
        )
        return ToolResult(hint, data={"found": False, "candidates": titles})



