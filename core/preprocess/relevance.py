import re
import json
import structlog
from collections import Counter, defaultdict
from datetime import datetime

from agents.base import BaseAgent, AgentContext, AgentResult
from core.infra.event_bus import EventType
from core.config.settings import config
# [去向量] 移除: from db.vector import VectorStore
from llm.client import LLMClient
from core.terminal.store_port import get_repo
from core.bootstrap import get_llm, get_embedding, get_vector_store
from sqlalchemy import select
from safelog import warn

log = structlog.get_logger()

_NOUN_POS = frozenset(('n', 'nr', 'ns', 'nt', 'nz', 'ng', 'nrt', 'nrfg'))
_ATTACH_POS = frozenset(('eng', 'x', 'm', 'q'))
_ADJ_POS = frozenset(('a', 'ad', 'an', 'ag'))
_VERB_POS = frozenset(('v', 'vd', 'vn', 'vg'))


def _extract_phrases(text: str) -> tuple[set[str], set[tuple[str, str]]]:
    if not text or len(text) < 2:
        return set(), set()
    clean = re.sub(r'[^\w\u4e00-\u9fff]', '', text)
    if not clean:
        return set(), set()
    phrases: set[str] = set()
    bigrams: set[tuple[str, str]] = set()
    try:
        import jieba
        import jieba.posseg as pseg
        tagged = list(pseg.cut(clean))
        i = 0
        while i < len(tagged):
            word, pos = tagged[i]
            if len(word) < 2 and pos not in _ATTACH_POS:
                i += 1
                continue
            if pos in _NOUN_POS:
                phrase_parts = [word]
                j = i + 1
                while j < len(tagged) and j - i <= 4:
                    nw, np = tagged[j]
                    if np in _NOUN_POS or np in _ADJ_POS or np in _ATTACH_POS:
                        if len(nw) >= 1:
                            phrase_parts.append(nw)
                        j += 1
                    else:
                        break
                if len(phrase_parts) > 1:
                    full = ''.join(phrase_parts)
                    if len(full) >= 3:
                        phrases.add(full)
                    for part in phrase_parts:
                        if len(part) >= 2:
                            phrases.add(part)
                else:
                    phrases.add(word)
                i = j if len(phrase_parts) > 1 else i + 1
            elif pos in _VERB_POS or pos in _ADJ_POS:
                if len(word) >= 2:
                    phrases.add(word)
                i += 1
            else:
                if len(word) >= 2:
                    phrases.add(word)
                i += 1
        phrase_list = sorted(phrases)
        for a, b in zip(phrase_list, phrase_list[1:]):
            bigrams.add((a, b))
        phrases = set(list(phrases)[:15])
    except ImportError:
        warn("core/preprocess/relevance.py:75 ImportError", err=repr(__import__("sys").exc_info()[1]))
        for i in range(len(clean) - 1):
            bigram = clean[i:i + 2]
            if re.match(r'^[\w]{2}$', bigram):
                phrases.add(bigram)
    return phrases, bigrams


def _extract_keywords(text: str) -> set[str]:
    phrases, _ = _extract_phrases(text)
    return phrases


def _semantic_text(msg: dict) -> str:
    content = msg.get("content") or ""
    msg_type = msg.get("msg_type", 0)
    if not content:
        if msg_type == 3:
            content = "[图片]"
        elif msg_type == 47:
            content = "[表情包]"
    if msg_type == 47 and content and not content.startswith("[表情"):
        pass  # noqa: silent（有意忽略，见 CONTRACT 第八条）
    return content[:200]


def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    min_len = min(len(a), len(b))
    a = a[:min_len]
    b = b[:min_len]
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na < 1e-8 or nb < 1e-8:
        return 0.0
    return dot / (na * nb)


class ThreadPredictor:
    __slots__ = ("recent_keywords", "recent_bigrams", "recent_entities",
                 "active_members", "last_msg_time", "msg_count",
                 "centroid_dirty", "centroid", "centroid_count",
                 "recent_embeddings")

    def __init__(self):
        self.recent_keywords: Counter = Counter()
        self.recent_bigrams: Counter = Counter()
        self.recent_entities: set[str] = set()
        self.active_members: set[str] = set()
        self.last_msg_time: datetime | None = None
        self.msg_count: int = 0
        self.centroid_dirty: bool = True
        self.centroid: list[float] = []
        self.centroid_count: int = 0
        self.recent_embeddings: list[list[float]] = []

    @classmethod
    def from_snapshot(cls, snapshot: str | None) -> "ThreadPredictor":
        p = cls()
        if snapshot:
            try:
                data = json.loads(snapshot)
                p.recent_keywords = Counter(data.get("keywords", {}))
                raw_bigrams = data.get("bigrams", {})
                for key, count in raw_bigrams.items():
                    parts = key.split("|", 1)
                    if len(parts) == 2:
                        p.recent_bigrams[(parts[0], parts[1])] = count
                p.recent_entities = set(data.get("entities", []))
                p.active_members = set(data.get("members", []))
                p.msg_count = data.get("msg_count", 0)
                p.centroid = data.get("centroid", [])
                p.centroid_count = data.get("centroid_count", 0)
                p.recent_embeddings = data.get("recent_embs", [])
            except Exception as _:
                warn("core/preprocess/relevance.py:151 Exception", err=repr(_))
        return p

    def to_snapshot(self) -> str:
        bigram_items = list(self.recent_bigrams.items())[:30]
        bigram_dict = {}
        for (a, b), count in bigram_items:
            bigram_dict[f"{a}|{b}"] = count
        return json.dumps({
            "keywords": dict(self.recent_keywords.most_common(50)),
            "bigrams": bigram_dict,
            "entities": list(self.recent_entities)[:50],
            "members": list(self.active_members)[:30],
            "msg_count": self.msg_count,
            "centroid": self.centroid[:256] if self.centroid else [],
            "centroid_count": self.centroid_count,
            "recent_embs": [e[:128] for e in self.recent_embeddings[-3:]],
        }, ensure_ascii=False)

    def keyword_overlap_idf(self, msg_keywords: set[str],
                            idf_weights: dict[str, float] | None = None) -> float:
        if not msg_keywords or not self.recent_keywords:
            return 0.0
        total = 0.0
        for kw in msg_keywords:
            if kw in self.recent_keywords:
                idf = (idf_weights or {}).get(kw, 1.0)
                tf = min(self.recent_keywords[kw], 5)
                total += tf * idf
        max_possible = sum(
            min(self.recent_keywords[kw], 5) * ((idf_weights or {}).get(kw, 1.0))
            for kw in list(self.recent_keywords.keys())[:15]
        ) if self.recent_keywords else 1.0
        return total / max(max_possible, 1e-6)

    def bigram_overlap(self, msg_bigrams: set[tuple[str, str]]) -> float:
        if not msg_bigrams or not self.recent_bigrams:
            return 0.0
        overlap = sum(
            1 for bg in msg_bigrams if bg in self.recent_bigrams
        )
        return overlap / max(len(msg_bigrams), 1)

    def quick_surprise(self, msg: dict,
                       idf_weights: dict[str, float] | None = None) -> float:
        score = 0.0
        text = _semantic_text(msg)
        msg_phrases, msg_bigrams = _extract_phrases(text)

        has_phrase_overlap = False
        if msg_phrases:
            kw_overlap = self.keyword_overlap_idf(msg_phrases, idf_weights)
            if kw_overlap > 0.05:
                has_phrase_overlap = True
                score -= 1.5 * min(kw_overlap * 10, 4.0)
            else:
                score += 2.5

        if msg_bigrams:
            bg_overlap = self.bigram_overlap(msg_bigrams)
            if bg_overlap > 0.1:
                has_phrase_overlap = True
                score -= 2.0 * min(bg_overlap * 5, 3.0)

        msg_entities = set()
        extra_raw = msg.get("extra") or ""
        if extra_raw:
            try:
                extra_data = json.loads(extra_raw) if isinstance(extra_raw, str) else extra_raw
                msg_entities = set(extra_data.get("entities", []))
            except Exception as _:
                warn("core/preprocess/relevance.py:222 Exception", err=repr(_))
        if msg_entities and msg_entities & self.recent_entities:
            score -= 1.5

        sender_id = msg.get("wxid") or ""
        if sender_id and sender_id in self.active_members:
            score -= 0.8

        msg_time = msg.get("created_at")
        if msg_time and self.last_msg_time:
            if isinstance(msg_time, str):
                try:
                    msg_time = datetime.fromisoformat(msg_time)
                except Exception as _:
                    warn("core/preprocess/relevance.py:236 Exception", err=repr(_))
                    msg_time = None
            if msg_time:
                gap = (msg_time - self.last_msg_time).total_seconds()
                if gap < 120:
                    score -= 0.8
                elif gap < 600:
                    score -= 0.3
                elif gap > 3600:
                    score += 1.5

        if not has_phrase_overlap and not (sender_id and sender_id in self.active_members):
            score += 1.5

        return score

    def conversational_continuity(self, msg_embedding: list[float]) -> float:
        if not msg_embedding or not self.recent_embeddings:
            return 0.0
        sims = [_cosine_sim(msg_embedding, emb) for emb in self.recent_embeddings]
        return max(sims)

    def update(self, msg: dict, embedding: list[float] | None = None):
        text = _semantic_text(msg)
        phrases, bigrams = _extract_phrases(text)
        for kw in phrases:
            self.recent_keywords[kw] += 1
        for bg in bigrams:
            self.recent_bigrams[bg] += 1
        if self.msg_count > 0 and self.msg_count % 20 == 0:
            self._decay_keywords()

        sender_id = msg.get("wxid") or ""
        if sender_id:
            self.active_members.add(sender_id)

        msg_time = msg.get("created_at")
        if msg_time:
            if isinstance(msg_time, str):
                try:
                    msg_time = datetime.fromisoformat(msg_time)
                except Exception as _:
                    warn("core/preprocess/relevance.py:277 Exception", err=repr(_))
                    msg_time = None
            if msg_time:
                self.last_msg_time = msg_time

        self.msg_count += 1
        self.centroid_dirty = True

        if embedding and not all(v == 0.0 for v in embedding[:8]):
            self.recent_embeddings.append(embedding)
            if len(self.recent_embeddings) > 3:
                self.recent_embeddings = self.recent_embeddings[-3:]

    def _decay_keywords(self):
        for kw in list(self.recent_keywords):
            self.recent_keywords[kw] *= 0.8
            if self.recent_keywords[kw] < 0.5:
                del self.recent_keywords[kw]
        for bg in list(self.recent_bigrams):
            self.recent_bigrams[bg] *= 0.8
            if self.recent_bigrams[bg] < 0.5:
                del self.recent_bigrams[bg]

    def update_centroid(self, embedding: list[float]):
        if not embedding or all(v == 0.0 for v in embedding[:8]):
            return
        if not self.centroid or len(self.centroid) != len(embedding):
            self.centroid = list(embedding)
            self.centroid_count = 1
        else:
            alpha = 1.0 / (self.centroid_count + 1)
            for i in range(min(len(self.centroid), len(embedding))):
                self.centroid[i] = (1 - alpha) * self.centroid[i] + alpha * embedding[i]
            self.centroid_count += 1
        self.centroid_dirty = True
        self.recent_embeddings.append(list(embedding))
        if len(self.recent_embeddings) > 3:
            self.recent_embeddings = self.recent_embeddings[-3:]

    def cosine_similarity(self, embedding: list[float]) -> float:
        return _cosine_sim(self.centroid, embedding)
