from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import structlog
from datetime import datetime

from agents.tools.base import BaseTool, ToolResult
from safelog import warn
from core.infra import egress

log = structlog.get_logger()

_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_MAX = 50

_DOMAIN_HINTS: list[tuple[list[str], str]] = [
    (["崩铁", "星铁", "星穹铁道", "原神", "鸣潮", "舟", "方舟", "明日方舟"], "site:ngabbs.com OR site:bilibili.com OR site:miyoushe.com"),
    (["python", "报错", "TypeError", "ValueError", "ImportError", "js", "typescript", "rust"], "site:stackoverflow.com OR site:developer.mozilla.org"),
    (["论文", "arxiv", "paper", "研究"], "site:arxiv.org OR site:scholar.google.com"),
    (["是什么", "定义", "什么意思", "wiki"], "site:zh.wikipedia.org OR site:wikipedia.org"),
]


_FRESHNESS_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def _freshness_after(freshness: str) -> str:
    """freshness → DeepSeek 的 after: 操作符（空串表示不限时间窗）。"""
    f = str(freshness or "").strip().lower()
    days = _FRESHNESS_DAYS.get(f)
    if days is None and f.isdigit():
        days = int(f)
    if not days:
        return ""
    from datetime import datetime, timedelta, timezone

    day = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime("%Y-%m-%d")
    return f"after:{day}"


def _deepseek_payload(query: str, num: int, freshness: str = "", *,
                      count: int = 10, need_content: bool = True) -> dict:
    """DeepSeek /v1/tools/search 请求体（query ≤128 字符，count 1..50）。"""
    q = str(query or "").strip()
    op = _freshness_after(freshness)
    if op:
        q = f"{q} {op}".strip()
    want = int(count or num or 10)
    want = max(1, min(want, 50))
    return {"query": q[:128], "count": want, "need_content": bool(need_content)}


def _deepseek_results(data: dict) -> list[dict]:
    """把 /tools/search 的 results 映射成本仓 web_search 的 result dict。"""
    out: list[dict] = []
    for r in (data or {}).get("results") or []:
        summary = str(r.get("summary") or "")
        snippet = str(r.get("snippet") or "") or summary[:200]
        content = str(r.get("content") or "")
        out.append({
            "title": str(r.get("title") or ""),
            "url": str(r.get("url") or ""),
            "snippet": snippet[:400],
            "content": (content or summary)[:1200],
            "published_date": str(r.get("date") or ""),
            "source": "deepseek",
            "authority": r.get("authority"),
            "score": r.get("rerank_score"),
        })
    return out


def _tavily_payload(query: str, num: int, freshness: str = "") -> dict:
    """构造 Tavily 请求体。freshness 由模型按用户意图传入（day/week/month/year 或整数天）；
    代码只做单位换算，不判断"这是不是时效查询"（不硬编码）。不传则不限时间窗。
    """
    from core.config.settings import config

    payload = {
        "api_key": config.search.tavily_api_key,
        "query": query,
        "max_results": num,
        "include_answer": True,
        "include_raw_content": False,
        "search_depth": "advanced",
    }
    f = str(freshness or "").strip().lower()
    days = _FRESHNESS_DAYS.get(f)
    if days is None and f.isdigit():
        days = int(f)
    if days:
        payload["topic"] = "news"
        payload["days"] = days
    return payload


def _short_date(value) -> str:
    """published_date → YYYY-MM-DD（如实展示，不做新旧判断）。"""
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(str(value)).strftime("%Y-%m-%d")
    except Exception:
        warn("agents/tools/web.py:105 Exception", err=repr(__import__("sys").exc_info()[1]))
        return str(value or "")[:16]


_SOURCE_TIERS = (
    ("厂商", ("mihoyo.com", "hoyoverse.com", "kurogames.com", "hypergryph.com",
              "tencent.com", "netease.com", "163.com", "perfectworld.com", "pwrd.com")),
    ("百科", ("baike.baidu.com", "moegirl.org.cn", "wikipedia.org", "bangumi.tv")),
    ("媒体", ("17173.com", "gamersky.com", "3dmgame.com", "ali213.net", "gcores.com",
              "gnn.gamer.com.tw", "gamemeca.com", "a9vg.com", "vgtime.com", "youxiputao.com",
              "36kr.com", "sspai.com", "ithome.com")),
    ("社区", ("bilibili.com", "tieba.baidu.com", "zhihu.com", "nga.cn", "douban.com",
              "weibo.com", "mp.weixin.qq.com", "xiaohongshu.com", "reddit.com",
              "steamcommunity.com", "lofter.com")),
)


def _source_tier(url: str) -> str:
    """域名层级（厂商/百科/媒体/社区；未知返回空）。确定性归类，不判内容对错。"""
    u = str(url or "").lower()
    if not u:
        return ""
    for tier, domains in _SOURCE_TIERS:
        if any(d in u for d in domains):
            return tier
    return ""


def _source_structure(results: list) -> str:
    """来源结构：独立域名数 + 层级统计（确定性事实，供模型评估可信度）。"""
    from urllib.parse import urlparse

    domains: list = []
    tiers: dict = {}
    for r in results:
        u = str(r.get("url") or "")
        if not u:
            continue
        dom = urlparse(u).netloc.lower()
        if dom and dom not in domains:
            domains.append(dom)
        t = _source_tier(u)
        if t:
            tiers[t] = tiers.get(t, 0) + 1
    bits = []
    if domains:
        bits.append(f"独立来源 {len(domains)} 个")
    if tiers:
        bits.append("来源层级: " + "、".join(f"{k}×{v}" for k, v in tiers.items()))
    if not bits:
        return ""
    out = "｜" + "｜".join(bits)
    if len(domains) == 1:
        out += "（孤证，谨慎采信）"
    return out


def _quality_probe(query: str, results: list) -> str:
    """机械质检：查询词在结果里的覆盖度（确定性信号，不判"对错"）。

    结果为空 / 查询词零覆盖 → 提示换源重试；部分覆盖 → 报覆盖数。
    给模型一个可判定的质量信号，避免"读着顺就当答案"。
    """
    if not results:
        return "🔧 自检: 没有返回任何结果，建议换关键词或换来源重试。"
    q = re.sub(r"site:\S+", "", query or "")
    tokens = [t for t in re.split(r"\s+", q.strip()) if len(t) >= 2]
    if not tokens:
        return ""
    texts = []
    for r in results:
        if str(r.get("title") or "") == "AI Summary":
            continue
        blob = " ".join(str(r.get(k) or "") for k in ("title", "snippet", "content")).lower()
        if blob.strip():
            texts.append(blob)
    if not texts:
        return ""
    parts = []
    zero = 0
    for t in tokens[:4]:
        tl = t.lower()
        n = sum(1 for x in texts if tl in x)
        parts.append(f"'{t}' {n}/{len(texts)} 条")
        if n == 0:
            zero += 1
    head = "🔧 自检(机械): " + "，".join(parts)
    if zero > 0:
        head += " —— 有关键词零覆盖，结果可能不相关；建议换关键词或换来源重试。"
    head += _source_structure(results)
    return head



class WebSearchTool(BaseTool):
    name = "web_search"
    description = (
        "搜索互联网获取实时信息，用于回答时事、天气、新闻等需要联网的问题。"
        "支持 site: 语法限定站点；用户要求在某个网站查找时，应先确认官方域名再 site: 限定——"
        "常见对照：B站=bilibili.com、知乎=zhihu.com、贴吧=tieba.baidu.com、豆瓣=douban.com、"
        "微博=weibo.com、小红书=xiaohongshu.com、NGA=nga.cn、老福特=lofter.com、萌百=zh.moegirl.org.cn。"
        "不在表中的站点先搜「<站名> 官网」核对；域名与站名对不上或你不认识时先反问，不要猜。"
        "查最新/近期资讯时传 freshness（day/week/month/year 或天数）限定时间窗；"
        "结果中 📅 为文章发布时间（判断新鲜度用）；事件时间（版本/前瞻/复刻/活动日期）从正文提取，"
        "并结合当前时间换算成相对表达（如「后天」「已过」）。"
        "来源冲突时并列呈现并标注「说法不一」，不要挑单一结论；单一来源的说法标注「仅一处来源」。"
        "参考结果末尾的「🔧 自检」行：零覆盖→换词/换源；孤证/无来源→谨慎采信。"
    )
    parameters = {
        "query": {"type": "string", "description": "搜索关键词", "required": True},
        "num": {"type": "integer", "description": "返回结果数量，默认5"},
        "freshness": {"type": "string", "description": "可选：查最新/近期资讯时传时间窗（day/week/month/year 或天数）——'最新/这两天'传 week，'上个月'传 month，'去年'传 year；不确定就不传", "required": False},
    }

    _KNOWLEDGE_SIM_THRESHOLD = 0.7

    def __init__(self, llm=None, vector_store=None, embedding_client=None, repo=None):
        self._llm = llm
        self._vector_store = vector_store
        self._embedding_client = embedding_client
        self._repo = repo
        self._fetch_tool: FetchUrlTool | None = None
        self._bg_tasks: set = set()

    def set_fetch_tool(self, tool: FetchUrlTool):
        self._fetch_tool = tool

    async def execute(self, **kwargs) -> ToolResult:
        query = kwargs.get("query", "").strip()
        num = min(kwargs.get("num", 5), 8)
        freshness = str(kwargs.get("freshness", "") or "").strip()

        if not query:
            return ToolResult("", error="query 不能为空")

        expanded = await self._expand_query_with_llm(query)

        domain_hint = self._detect_domain_hint(expanded)

        if not freshness:
            # 时效查询跳过知识库优先：库里存的是旧条目，回答「最新/近期」会过时。
            kb_result = await self._search_knowledge_first(expanded)
            if kb_result:
                log.info("search_knowledge_hit", query=expanded)
                return kb_result

        cached = self._check_cache(expanded)
        if cached:
            log.info("search_cache_hit", query=expanded)
            return self._format_results(expanded, cached, from_cache=True)

        all_results: list[dict] = []

        queries = await self._generate_parallel_queries(expanded, domain_hint)

        if len(queries) > 1:
            tasks = [self._search_single(q, num, freshness) for q in queries]
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for r in gathered:
                if isinstance(r, list):
                    all_results.extend(r)
        else:
            all_results = await self._search_single(queries[0], num, freshness)

        all_results = self._deduplicate(all_results)

        if not all_results:
            retry_query = await self._retry_with_different_angle(expanded)
            if retry_query:
                all_results = await self._search_single(retry_query, num, freshness)
                all_results = self._deduplicate(all_results)

        if not all_results:
            return ToolResult(f"搜索 '{expanded}' 没有找到结果，可能网络受限")

        self._update_cache(expanded, all_results)

        if self._fetch_tool and all_results:
            all_results = await self._enrich_with_content(all_results[:2], expanded)

        await self._maybe_save_to_knowledge(expanded, all_results)

        return self._format_results(expanded, all_results)

    async def _search_knowledge_first(self, query: str) -> ToolResult | None:
        if not self._vector_store or not self._embedding_client:
            return None
        try:
            embedding = await self._embedding_client.embed(query)
            if not any(embedding):
                return None
            results = self._vector_store.search(embedding, top_k=3, collection="persona_knowledge")
            if not results:
                return None
            best_dist = results[0].get("distance", 9999)
            best_sim = max(0.0, 1.0 - best_dist)
            if best_sim < self._KNOWLEDGE_SIM_THRESHOLD:
                return None
            best_meta = results[0].get("metadata", {})
            original_query = best_meta.get("original_query", "")
            if original_query:
                can_answer = await self._verify_cache_relevance(query, original_query)
                if not can_answer:
                    log.info("knowledge_cache_stale", query=query, cached_query=original_query, sim=best_sim)
                    return None
            lines = ["知识库缓存命中（无需联网）:"]
            for r in results:
                dist = r.get("distance", 0)
                doc = r.get("document", "")[:400]
                meta = r.get("metadata", {})
                source = meta.get("source_file", meta.get("source_url", "未知来源"))
                sim = max(0.0, 1.0 - dist)
                lines.append(f"[来源: {source}, 相关度={sim:.2f}]\n{doc}")
                if self._repo:
                    try:
                        doc_id = int(r.get("id", "0").split("_")[-1])
                        if doc_id > 0:
                            existing = self._repo.knowledge_docs.get(doc_id) if hasattr(self._repo.knowledge_docs, 'get') else None
                            if existing:
                                self._repo.knowledge_docs.update(
                                    doc_id,
                                    access_count=(existing.access_count or 0) + 1,
                                    last_accessed_at=datetime.now(),
                                )
                    except Exception:
                        warn("agents/tools/web.py:329 Exception", err=repr(__import__("sys").exc_info()[1]))
            return ToolResult("\n---\n".join(lines), data=results)
        except Exception as e:
            log.debug("knowledge_preflight_failed", error=str(e)[:80])
            return None

    _NUM_PATTERN = __import__("re").compile(r"\d+(?:\.\d+)?")

    def _rule_verify(self, query: str, cached_query: str) -> bool | None:
        q_nums = set(self._NUM_PATTERN.findall(query))
        c_nums = set(self._NUM_PATTERN.findall(cached_query))
        if q_nums and c_nums:
            if q_nums == c_nums:
                return True
            return False
        return None

    async def _verify_cache_relevance(self, query: str, cached_query: str) -> bool:
        rule = self._rule_verify(query, cached_query)
        if rule is not None:
            return rule
        if not self._llm:
            return True
        try:
            result = await self._llm.chat(
                system="判断缓存能否准确回答当前查询，注意否定词、时间词等语义差异。只回答yes或no。",
                user=f"当前查询: {query}\n缓存原查询: {cached_query}\n能否准确回答？",
                agent_name="cache_verify",
                temperature=0.0,
                max_tokens=5,
            )
            return result.strip().lower().startswith("yes")
        except Exception:
            warn("agents/tools/web.py:362 Exception", err=repr(__import__("sys").exc_info()[1]))
            return True

    async def _maybe_save_to_knowledge(self, query: str, results: list[dict]):
        if not self._vector_store or not self._embedding_client or not self._repo:
            return
        try:
            from core.config.settings import config
            if not getattr(config.search, "knowledge_auto_save", True):
                return
        except Exception:
            warn("agents/tools/web.py:372 Exception", err=repr(__import__("sys").exc_info()[1]))
        await self._evict_if_needed()
        try:
            from db.models import KnowledgeDoc
            active = self._repo.personas.list_active()
            pid = active[0].id if active else 1
            for r in results[:3]:
                title = r.get("title", "")
                if str(title) == "AI Summary":
                    continue  # AI 摘要是二手合成（含标题党/幻觉风险），不入库
                url = r.get("url", "")
                snippet = r.get("snippet", "")
                content = r.get("content", "")
                text = f"{title}\n{snippet}\n{content}".strip()
                if len(text) < 30:
                    continue
                content_hash = hashlib.md5(text.encode()).hexdigest()
                existing = self._repo.knowledge_docs.get_by_hash(content_hash) if hasattr(self._repo.knowledge_docs, 'get_by_hash') else None
                if existing:
                    continue
                doc = KnowledgeDoc(
                    persona_id=pid,
                    title=title or query,
                    source_file=url or "web_search",
                    chunk_index=0,
                    content=text[:500],
                    char_count=min(len(text), 500),
                    tags="web_search",
                    created_at=datetime.now(),
                    source_type="web_search",
                    source_url=url,
                    original_query=query,
                    content_hash=content_hash,
                )
                saved = self._repo.knowledge_docs.add(doc)
                try:
                    emb = await self._embedding_client.embed(text[:500])
                    if any(emb):
                        self._vector_store.persona_knowledge.add(
                            f"kd_{saved.id}",
                            text[:500],
                            emb,
                            {"source_file": url or "web_search", "source_type": "web_search", "title": title or query, "original_query": query},
                        )
                except Exception as _:
                    warn("agents/tools/web.py:417 Exception", err=repr(_))
            log.debug("search_saved_to_knowledge", query=query, count=min(len(results), 3))
        except Exception as e:
            log.warning("save_to_knowledge_failed", error=str(e)[:100])

    async def _evict_if_needed(self):
        if not self._repo or not self._vector_store:
            return
        try:
            from core.config.settings import config
            max_docs = getattr(config.search, "knowledge_max_docs", 100000)
            stale_days = getattr(config.search, "knowledge_stale_days", 180)
        except Exception:
            warn("agents/tools/web.py:430 Exception", err=repr(__import__("sys").exc_info()[1]))
            max_docs = 100000
            stale_days = 180
        try:
            total = self._repo.knowledge_docs.count()
            if total < max_docs:
                return
            evict_count = max(1, total - max_docs + 100)
            stale = self._repo.knowledge_docs.get_stale(days=stale_days, limit=evict_count)
            for doc in stale:
                try:
                    self._vector_store.persona_knowledge.delete_batch(ids=[f"kd_{doc.id}"])
                except Exception:
                    warn("agents/tools/web.py:442 Exception", err=repr(__import__("sys").exc_info()[1]))
                self._repo.knowledge_docs.delete(doc.id)
            if stale:
                log.info("knowledge_evicted", count=len(stale), remaining=self._repo.knowledge_docs.count())
        except Exception as e:
            log.warning("knowledge_evict_failed", error=str(e)[:100])

    async def _search_single(self, query: str, num: int, freshness: str = "") -> list[dict]:
        from core import features
        from core.config.settings import config

        engine = config.search.default_engine
        ds_mode = features.mode(features.SEARCH_DEEPSEEK, features.MODE_OFF)
        if ds_mode == features.MODE_SHADOW:
            # 影子档：旧链照旧返回，新引擎只跑一次做对照读数。
            task = asyncio.create_task(self._shadow_deepseek(query, num, freshness))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        elif ds_mode == features.MODE_ON and config.llm.deepseek_api_key:
            try:
                results = await self._search_deepseek(query, num, freshness)
                if results:
                    return results
            except Exception as e:
                log.warning("deepseek_search_failed", error=str(e)[:120])

        if engine == "tavily" and config.search.tavily_api_key:
            try:
                results = await self._search_tavily(query, num, freshness)
                if results:
                    return results
            except Exception as e:
                log.warning("tavily_search_failed", error=str(e)[:100])

        if config.search.bing_api_key:
            try:
                results = await self._search_bing_api(query, num)
                if results:
                    return results
            except Exception as e:
                log.warning("bing_api_search_failed", error=str(e)[:100])

        try:
            results = await self._search_duckduckgo(query, num)
            if results:
                return results
        except Exception as _:
            warn("agents/tools/web.py:489 Exception", err=repr(_))

        try:
            results = await self._search_bing_cn(query, num)
            if results:
                return results
        except Exception as _:
            warn("agents/tools/web.py:496 Exception", err=repr(_))

        return []

    async def _search_deepseek(self, query: str, num: int, freshness: str = "") -> list[dict]:
        """DeepSeek 自研搜索（POST /v1/tools/search）。按请求计费，失败/空结果不计费。"""
        from core.config.settings import config
        base = str(config.llm.deepseek_base_url or "https://api.deepseek.com").rstrip("/")
        payload = _deepseek_payload(
            query, num, freshness,
            count=int(getattr(config.search, "deepseek_count", 10) or 10),
            need_content=bool(getattr(config.search, "deepseek_need_content", True)),
        )
        timeout = float(getattr(config.search, "deepseek_timeout_seconds", 20.0) or 20.0)
        if True:  # 出网只走 core.infra.egress（规矩见该模块头）
            resp = await egress.apost_json(
                f"{base}/tools/search",
                headers={
                    "Authorization": f"Bearer {config.llm.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            pass  # egress 已把 4xx/5xx 抛成 Upstream
            data = json.loads(resp.text)
        results = _deepseek_results(data)
        log.info("deepseek_search_ok", query=query, count=len(results),
                 want=payload["count"], need_content=payload["need_content"])
        return results

    async def _shadow_deepseek(self, query: str, num: int, freshness: str) -> None:
        """影子档对照：只写读数，异常不外抛。"""
        t0 = time.time()
        try:
            results = await self._search_deepseek(query, num, freshness)
            log.info("search_deepseek_shadow", query=query, count=len(results),
                     elapsed_ms=round((time.time() - t0) * 1000),
                     first=str((results[0] if results else {}).get("title") or "")[:60])
        except Exception as e:  # noqa: BLE001 - 影子失败不影响主链路
            log.info("search_deepseek_shadow_failed", query=query,
                     elapsed_ms=round((time.time() - t0) * 1000), error=str(e)[:120])

    async def _search_tavily(self, query: str, num: int, freshness: str = "") -> list[dict]:
        from core.config.settings import config
        if True:  # 出网只走 core.infra.egress（规矩见该模块头）
            resp = await egress.apost_json(
                "https://api.tavily.com/search",
            json=_tavily_payload(query, num, freshness),
            )
            pass  # egress 已把 4xx/5xx 抛成 Upstream
            data = json.loads(resp.text)

        results = []
        answer = data.get("answer", "")
        if answer:
            results.append({
                "title": "AI Summary",
                "url": "",
                "snippet": answer[:500],
                "content": answer[:500],
                "source": "tavily_answer",
            })

        for r in data.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": (r.get("content", "") or r.get("snippet", ""))[:400],
                "content": "",
                "source": "tavily",
            })

        log.info("tavily_search_ok", query=query, count=len(results))
        return results

    async def _search_bing_api(self, query: str, num: int) -> list[dict]:
        from core.config.settings import config
        if True:  # 出网只走 core.infra.egress（规矩见该模块头）
            resp = await egress.aget(
                "https://api.bing.microsoft.com/v7.0/search",
                params={"q": query, "count": num, "mkt": "zh-CN"},
                headers={"Ocp-Apim-Subscription-Key": config.search.bing_api_key},
            )
            pass  # egress 已把 4xx/5xx 抛成 Upstream
            data = json.loads(resp.text)

        results = []
        for r in data.get("webPages", {}).get("value", []):
            results.append({
                "title": r.get("name", ""),
                "url": r.get("url", ""),
                "snippet": r.get("snippet", "")[:400],
                "content": "",
                "source": "bing_api",
            })

        log.info("bing_api_search_ok", query=query, count=len(results))
        return results

    async def _search_duckduckgo(self, query: str, num: int) -> list[dict]:
        if True:  # 出网只走 core.infra.egress（规矩见该模块头）
            resp = await egress.aget(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query, "kl": "cn-zh"},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            pass  # egress 已把 4xx/5xx 抛成 Upstream
            html = resp.text

        results = []
        blocks = re.findall(
            r'<a[^>]+class="result-link"[^>]*>(.*?)</a>.*?'
            r'<td[^>]*>(.*?)</td>',
            html, re.DOTALL,
        )
        for title_html, snippet_html in blocks[:num]:
            title = re.sub(r'<[^>]+>', '', title_html).strip()
            snippet = re.sub(r'<[^>]+>', '', snippet_html).strip()[:400]
            url_m = re.search(r'href="([^"]+)"', title_html)
            url = url_m.group(1) if url_m else ""
            if title:
                results.append({"title": title, "url": url, "snippet": snippet, "content": "", "source": "ddg"})

        if not results:
            blocks2 = re.findall(
                r'<a[^>]+rel="nofollow"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                html, re.DOTALL,
            )
            for url, title_html in blocks2[:num]:
                title = re.sub(r'<[^>]+>', '', title_html).strip()
                if title:
                    results.append({"title": title, "url": url, "snippet": "", "content": "", "source": "ddg"})

        if not results:
            snippets = re.findall(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', html, re.DOTALL)
            for s in snippets[:num]:
                clean = re.sub(r'<[^>]+>', '', s).strip()
                if clean:
                    results.append({"title": "", "url": "", "snippet": clean[:400], "content": "", "source": "ddg"})

        return results

    async def _search_bing_cn(self, query: str, num: int) -> list[dict]:
        if True:  # 出网只走 core.infra.egress（规矩见该模块头）
            resp = await egress.aget(
                "https://cn.bing.com/search",
                params={"q": query, "count": num},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            pass  # egress 已把 4xx/5xx 抛成 Upstream
            html = resp.text

        results = []
        for m in re.finditer(r'<li class="b_algo"[^>]*>(.*?)</li>', html, re.DOTALL):
            block = m.group(1)
            h2_m = re.search(
                r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*</h2>',
                block, re.DOTALL,
            )
            if not h2_m:
                continue
            url = h2_m.group(1)
            title = re.sub(r'<[^>]+>', '', h2_m.group(2)).strip()
            cap_m = re.search(
                r'<div class="b_caption"[^>]*>.*?<p[^>]*>(.*?)</p>',
                block, re.DOTALL,
            )
            snippet = ""
            if cap_m:
                snippet = re.sub(r'<[^>]+>', '', cap_m.group(1)).strip()[:400]
            if title:
                results.append({"title": title, "url": url, "snippet": snippet, "content": "", "source": "bing_cn"})
            if len(results) >= num:
                break

        return results

    async def _expand_query_with_llm(self, query: str) -> str:
        # 已含 site: 限定的查询原样返回：扩写模型可能改写/丢弃 site: 语法
        if "site:" in query:
            return query
        if not self._llm:
            return query
        try:
            result = await self._llm.chat(
                system="你是搜索关键词扩写专家。把用户的口语化/缩写搜索词扩写为搜索引擎能理解的完整关键词。只输出扩写后的搜索词，不要其他内容。如果原词已经足够清晰，原样输出。\n常见扩写：崩铁→崩坏星穹铁道，舟→明日方舟，农→阴阳师，k8s→Kubernetes，pyjs→Python JavaScript",
                user=f"扩写搜索词: {query}",
                agent_name="query_expand",
                temperature=0.2,
                max_tokens=50,
            )
            result = result.strip().strip('"').strip("'")
            if result and len(result) <= 80:
                log.info("query_expanded", original=query, expanded=result)
                return result
        except Exception as e:
            log.debug("llm_expand_failed", error=str(e))
        return query

    async def _generate_parallel_queries(self, expanded: str, domain_hint: str) -> list[str]:
        # 站内搜索（已带 site:）保持原样：叠加领域提示/英文翻译都会破坏限定
        if "site:" in expanded:
            return [expanded]
        from core.config.settings import config
        if not config.search.parallel_queries:
            q = f"{expanded} {domain_hint}".strip() if domain_hint else expanded
            return [q]

        queries = [expanded]
        if domain_hint:
            queries.append(f"{expanded} {domain_hint}")

        has_cjk = any('\u4e00' <= c <= '\u9fff' for c in expanded)
        if has_cjk and len(expanded) <= 30:
            try:
                en_query = await self._llm.chat(
                    system="将中文搜索词翻译为英文搜索词，用于搜索英文互联网内容。只输出英文搜索词。",
                    user=expanded,
                    agent_name="query_translate",
                    temperature=0.1,
                    max_tokens=50,
                )
                en_query = en_query.strip()
                if en_query and en_query != expanded:
                    queries.append(en_query)
            except Exception:
                warn("agents/tools/web.py:730 Exception", err=repr(__import__("sys").exc_info()[1]))

        return queries[:3]

    def _detect_domain_hint(self, query: str) -> str:
        for keywords, hint in _DOMAIN_HINTS:
            if any(kw in query for kw in keywords):
                return hint
        return ""

    async def _retry_with_different_angle(self, query: str) -> str | None:
        if not self._llm:
            return None
        try:
            new_query = await self._llm.chat(
                system=f"原搜索词 '{query}' 没有找到结果。请换一个搜索角度，使用不同的关键词或更通用的表述。只输出新的搜索词。",
                user=query,
                agent_name="query_retry",
                temperature=0.5,
                max_tokens=50,
            )
            new_query = new_query.strip().strip('"').strip("'")
            if new_query and new_query != query:
                log.info("search_retry_angle", original=query, new=new_query)
                return new_query
        except Exception:
            warn("agents/tools/web.py:756 Exception", err=repr(__import__("sys").exc_info()[1]))
        return None

    async def _enrich_with_content(self, top_results: list[dict], query: str) -> list[dict]:
        if not self._fetch_tool:
            return top_results

        for r in top_results:
            url = r.get("url", "")
            if not url or not url.startswith("http"):
                continue
            try:
                fetch_result = await self._fetch_tool.execute(
                    url=url,
                    question=f"提取与 '{query}' 相关的核心内容",
                )
                if fetch_result.output and not fetch_result.error:
                    r["content"] = fetch_result.output[:800]
            except Exception as e:
                log.debug("fetch_content_failed", url=url, error=str(e)[:50])

        return top_results

    def _deduplicate(self, results: list[dict]) -> list[dict]:
        seen_urls: set[str] = set()
        seen_titles: set[str] = set()
        deduped = []
        for r in results:
            url = r.get("url", "")
            title = r.get("title", "")
            url_key = url.rstrip("/") if url else ""
            title_key = title.lower().strip() if title else ""
            if url_key and url_key in seen_urls:
                continue
            if title_key and title_key in seen_titles:
                continue
            if url_key:
                seen_urls.add(url_key)
            if title_key:
                seen_titles.add(title_key)
            deduped.append(r)
        return deduped

    def _check_cache(self, query: str) -> list[dict] | None:
        from core.config.settings import config
        key = hashlib.md5(query.encode()).hexdigest()
        if key in _SEARCH_CACHE:
            ts, results = _SEARCH_CACHE[key]
            if time.time() - ts < config.search.cache_ttl_minutes * 60:
                return results
            del _SEARCH_CACHE[key]
        return None

    def _update_cache(self, query: str, results: list[dict]):
        key = hashlib.md5(query.encode()).hexdigest()
        _SEARCH_CACHE[key] = (time.time(), results)
        if len(_SEARCH_CACHE) > _CACHE_MAX:
            oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
            del _SEARCH_CACHE[oldest]

    def _format_results(self, query: str, results: list[dict], from_cache: bool = False) -> ToolResult:
        lines = [f"搜索 '{query}' 的结果:"]
        sources = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("snippet", "")
            content = r.get("content", "")

            if title == "AI Summary":
                lines.append(f"\n🤖 AI摘要: {snippet}")
                continue

            entry = f"\n{i}. "
            if title and url:
                entry += f"[{title}]({url})"
                _d = _short_date(r.get("published_date"))
                if _d:
                    entry += f"　📅 {_d}"
                sources.append(url)
            elif title:
                entry += title
            if snippet:
                entry += f"\n   {snippet[:300]}"
            if content:
                entry += f"\n   📄 {content[:400]}"
            lines.append(entry)

        if sources:
            lines.append(f"\n📎 来源: {sources[0]}")
            if len(sources) > 1:
                lines.append(f"   备用: {', '.join(sources[1:3])}")

        probe = _quality_probe(query, results)
        if probe:
            lines.append(probe)

        if from_cache:
            lines.append("\n(cached)")

        return ToolResult(
            "\n".join(lines),
            data={"query": query, "results": results, "sources": sources},
        )


class FetchUrlTool(BaseTool):
    name = "fetch_url"
    description = "抓取网页内容并提取摘要。当用户发送链接/URL，或需要了解某个网页内容时调用。"
    parameters = {
        "url": {"type": "string", "description": "要抓取的网页URL", "required": True},
        "question": {"type": "string", "description": "关于网页内容的问题，空字符串表示提取摘要"},
        "save_to_knowledge": {"type": "boolean", "description": "是否将内容保存到知识库，默认false"},
    }


    def __init__(self, repo=None, vector_store=None, embedding_client=None, llm=None):
        self.repo = repo
        self.vector_store = vector_store
        self.embedding_client = embedding_client
        self._llm = llm

    def _validate_url(self, url: str) -> str | None:
        """目的地规则只有一份：core/infra/egress.py。"""
        try:
            egress.check(url)
        except egress.Blocked as exc:
            return str(exc)
        return None
    async def execute(self, **kwargs) -> ToolResult:
        url = kwargs.get("url", "").strip()
        question = kwargs.get("question", "")
        save = kwargs.get("save_to_knowledge", False)

        if not url:
            return ToolResult("", error="url 不能为空")

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        err = self._validate_url(url)
        if err:
            return ToolResult("", error=err)

        from core import features
        from core.config.settings import config as _cfg

        if (
            features.mode(features.SEARCH_DEEPSEEK, features.MODE_OFF) == features.MODE_ON
            and bool(getattr(_cfg.search, "deepseek_fetch_enabled", False))
            and _cfg.llm.deepseek_api_key
        ):
            try:
                text = await self._fetch_deepseek(url)
                if text.strip():
                    if save:
                        await self._save_to_knowledge(url, "", text)
                    return ToolResult(
                        text[:6000],
                        data={"url": url, "type": "deepseek_fetch", "chars": len(text)},
                    )
            except Exception as e:  # noqa: BLE001 - 失败回落本地抓取
                log.warning("deepseek_fetch_failed", url=url, error=str(e)[:120])

        try:
            resp = await egress.aget(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; YinyueBot/1.0)"},
                timeout_ms=15000,
            )
            raw_html = resp.text
        except egress.Blocked as exc:
            return ToolResult("", error=f"不允许访问该地址：{exc}")
        except egress.Upstream as exc:
            return ToolResult("", error=f"HTTP {exc.status}")
        except egress.TooLarge:
            return ToolResult("", error="网页太大（超过 2MB，按设计不抓）")
        except egress.TimedOut:
            return ToolResult("", error="网页请求超时")
        except egress.EgressError as exc:
            return ToolResult("", error=f"请求失败: {type(exc).__name__}")

        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                data = json.loads(raw_html)
                text = json.dumps(data, ensure_ascii=False, indent=2)[:3000]
            except json.JSONDecodeError:
                warn("agents/tools/web.py:960 json.JSONDecodeError", err=repr(__import__("sys").exc_info()[1]))
                text = raw_html[:3000]
            return ToolResult(f"JSON内容:\n{text}", data={"url": url, "type": "json"})

        if not any(t in content_type for t in ("text/html", "text/plain")):
            return ToolResult("", error=f"不支持的内容类型: {content_type[:50]}")

        text = self._extract_text(raw_html)
        title = self._extract_title(raw_html)

        if not text.strip():
            return ToolResult("", error="网页内容为空")

        if len(text) > 8000:
            text_for_summary = text[:8000]
        else:
            text_for_summary = text

        if self._llm:
            prompt = question if question else "请提取这个网页的核心内容，生成简洁摘要（300字以内）"
            try:
                summary = await self._llm.chat(
                    system=f"你是一个网页内容分析助手。网页标题: {title}\n请根据网页内容回答用户的问题。",
                    user=f"{prompt}\n\n网页内容:\n{text_for_summary}",
                    agent_name="url_fetcher",
                    temperature=0.3,
                    max_tokens=500,
                )
            except Exception as _:
                warn("agents/tools/web.py:988 Exception", err=repr(_))
                summary = text_for_summary[:500]
        else:
            summary = text_for_summary[:500]

        result_data = {"url": url, "title": title, "length": len(text)}

        if save and self.repo and self.vector_store and self.embedding_client:
            saved_count = await self._save_to_knowledge(url, title, text)
            result_data["saved_chunks"] = saved_count

        header = f"📄 [{title}]({url})" if title else f"📄 {url}"
        return ToolResult(f"{header}\n\n{summary}", data=result_data)

    async def _fetch_deepseek(self, url: str) -> str:
        """DeepSeek /v1/tools/fetch：直接返回 Markdown 正文。"""
        from core.config.settings import config
        base = str(config.llm.deepseek_base_url or "https://api.deepseek.com").rstrip("/")
        timeout = float(getattr(config.search, "deepseek_timeout_seconds", 20.0) or 20.0)
        if True:  # 出网只走 core.infra.egress（规矩见该模块头）
            resp = await egress.apost_json(
                f"{base}/tools/fetch",
                headers={
                    "Authorization": f"Bearer {config.llm.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={"url": url},
            )
            pass  # egress 已把 4xx/5xx 抛成 Upstream
            data = json.loads(resp.text)
        title = str((data or {}).get("title") or "")
        content = str((data or {}).get("content") or "")
        log.info("deepseek_fetch_ok", url=url, chars=len(content), title=title[:60])
        return f"# {title}\n\n{content}" if title else content

    def _extract_text(self, html: str) -> str:
        import re
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<nav[^>]*>.*?</nav>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<footer[^>]*>.*?</footer>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<header[^>]*>.*?</header>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _extract_title(self, html: str) -> str:
        import re
        m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
        if m:
            return re.sub(r'<[^>]+>', '', m.group(1)).strip()[:100]
        return ""

    async def _save_to_knowledge(self, url: str, title: str, text: str) -> int:
        from db.models import KnowledgeDoc
        chunk_size = 500
        overlap = 100
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            chunks.append(text[start:end])
            start += chunk_size - overlap
            if start >= len(text):
                break

        if not self.repo or not self.vector_store or not self.embedding_client:
            return 0

        active = self.repo.personas.list_active()
        pid = active[0].id if active else 1
        source = url

        count = 0
        for i, chunk in enumerate(chunks):
            doc = KnowledgeDoc(
                persona_id=pid,
                title=title or url,
                source_file=source,
                chunk_index=i,
                content=chunk,
                char_count=len(chunk),
                tags="url",
                created_at=datetime.now(),
                source_type="url_fetch",
                source_url=url,
                content_hash=hashlib.md5(chunk.encode()).hexdigest(),
            )
            saved = self.repo.knowledge_docs.add(doc)
            count += 1
            try:
                emb = await self.embedding_client.embed(chunk)
                if any(emb):
                    self.vector_store.persona_knowledge.add(
                        f"kd_{saved.id}",
                        chunk,
                        emb,
                        {"source_file": source, "chunk_index": i, "title": title or url, "source_type": "url_fetch"},
                    )
            except Exception as _:
                warn("agents/tools/web.py:1089 Exception", err=repr(_))
        return count
