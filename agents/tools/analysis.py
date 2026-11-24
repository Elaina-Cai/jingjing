from __future__ import annotations

import os
import json
import structlog
from datetime import datetime, timedelta
from pathlib import Path

from agents.tools.base import BaseTool, ToolResult
from safelog import warn

log = structlog.get_logger()



from core.infra import egress


def _to_data_uri(path: str) -> str:
    """本地图片路径 → data URI；已是 data:/http(s) 的原样返回。

    放在工具侧做，是因为 adapter 的重编码分支遇到"判不出 mime / 太大"会返回空串，
    而 messages_with_images() 是静默丢弃 —— 图丢了但调用方只看到"模型说看不到图"。
    """
    text = str(path or "")
    print("[DBG2] to_data_uri called arg=%r" % (text[:100],), flush=True)
    if text.startswith(("data:", "http://", "https://")):
        return text
    try:
        from core.conversation.media import media_path_to_data_uri
        _u = media_path_to_data_uri(text) or ""
        return _u
    except Exception:  # noqa: BLE001
        return ""




def _pick_text(res) -> str:
    """从 ChatResult 里取“她真正说的话”。

    2026-10-25：思考型模型（deepseek-flash）常把回答写进 reasoning，content 为空，
    之前只取 .text ⇒ 图明明送进去了却判定“模型返回为空”。这里按优先级兜一遍。
    """
    for attr in ("text", "content", "reasoning"):
        v = getattr(res, attr, None)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # 2026-10-26：ChatResult 没有 reasoning/content 属性，思考型模型的观察在
    # reasoning_blocks[*].text 里。不读块就会一直得到空串（图送进去了却当成没返回）。
    blocks = getattr(res, "reasoning_blocks", None) or ()
    parts = []
    for block in blocks:
        piece = (block.get("text") if isinstance(block, dict)
                 else getattr(block, "text", None))
        if isinstance(piece, str) and piece.strip():
            parts.append(piece.strip())
    if parts:
        return "\n".join(parts).strip()
    meta = getattr(res, "raw_meta", None) or {}
    if isinstance(meta, dict):
        for key in ("reasoning_tail", "reasoning", "reasoning_content", "text"):
            v = meta.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""

class GetTodayAnalysisTool(BaseTool):
    name = "get_today_analysis"
    description = "获取今日微信分析结果：谁找了我（私聊重要性）、群聊摘要、每日日报。用户让你整理微信、看今日分析时调用。"
    parameters = {
        "days": {"type": "integer", "description": "查看几天内的分析，默认1（今天）", "required": False},
    }

    _SPECIAL = {
        "filehelper": "文件传输助手", "weixin": "微信团队",
        "fmessage": "朋友验证消息", "daily_digest": "每日日报",
    }

    def __init__(self, repo, unified_query=None):
        self.repo = repo
        self._uq = unified_query

    def _display_name(self, chat_id: str) -> str:
        if not chat_id:
            return "未知"
        if self._uq:
            name = self._uq.get_contact_display_name_any(chat_id)
            if name != chat_id:
                return name
        c = self.repo.contacts.get_by_wxid(chat_id)
        if c:
            return c.remark or c.nickname or chat_id
        return self._SPECIAL.get(chat_id, chat_id)

    def _parse(self, raw: str) -> dict:
        if not raw:
            return {}
        try:
            import ast
            return ast.literal_eval(raw)
        except Exception as _:
            warn("agents/tools/analysis.py:47 Exception", err=repr(_))
            return {"summary": raw}

    async def execute(self, **kwargs) -> ToolResult:
        from datetime import date

        from core.analysis.service import AnalysisService

        days = max(1, int(kwargs.get("days", 1)))
        today = date.today()
        start = datetime.combine(today - timedelta(days=days - 1), datetime.min.time())

        # Phase 5 D2: analysis reads go through AnalysisService.  Legacy rows
        # come back marked ``legacy=True`` instead of being read directly.
        service = AnalysisService(legacy_repo=self.repo)
        records = service.recent(since=start, limit=80, principal=self.principal)
        digest_items = service.daily_items(today, principal=self.principal)

        if not records and not digest_items:
            return ToolResult("今日还没有微信分析数据。可能同步还没跑，或者今天还没有新消息。")

        lines = []

        if digest_items:
            lines.append("【每日日报】")
            for item in digest_items:
                mark = "（历史）" if item.legacy else ""
                lines.append(f"{mark}{item.result[:600] if item.result else '(空)'}")
            lines.append("")

        private = [r for r in records if r.analysis_type in ("private_importance", "message_analysis")]
        group = [r for r in records if r.analysis_type == "group_summary"]
        other = [r for r in records if r.analysis_type not in ("private_importance", "message_analysis", "group_summary", "daily_digest")]

        if private:
            lines.append("【谁找了我（私聊）】")
            for r in private:
                p = self._parse(r.result)
                name = self._display_name(r.chat_id)
                level = p.get("level", "?")
                summary = p.get("summary", r.result[:80] if r.result else "")
                action = "需回复" if p.get("action_needed") else ""
                tag = f"[{level}]" if level != "?" else ""
                act = f" {action}" if action else ""
                t = r.created_at.strftime("%H:%M") if r.created_at else ""
                mark = "（历史）" if r.legacy else ""
                lines.append(f"- {t} {name} {mark}{tag}{act}: {summary}")
            lines.append("")

        if group:
            lines.append("【群聊摘要】")
            for r in group:
                p = self._parse(r.result)
                name = self._display_name(r.chat_id)
                summary = p.get("summary", r.result[:100] if r.result else "")
                t = r.created_at.strftime("%H:%M") if r.created_at else ""
                mark = "（历史）" if r.legacy else ""
                lines.append(f"- {t} {name} {mark}: {summary}")
            lines.append("")

        if other:
            lines.append("【其他分析】")
            for r in other:
                name = self._display_name(r.chat_id)
                t = r.created_at.strftime("%H:%M") if r.created_at else ""
                mark = "（历史）" if r.legacy else ""
                lines.append(f"- {t} {name} {mark}({r.analysis_type}): {r.result[:80] if r.result else ''}")

        return ToolResult("\n".join(lines))


class AnalyzeImageTool(BaseTool):
    name = "analyze_image"
    description = (
        "看图片内容：文字、场景、物体、表情、情绪。"
        "用户刚发了一张图或一个表情包时，直接调用本工具并【不要填 image_url】——"
        "会自动使用这轮正在讨论的那张图：被引用的图/表情包优先，其次是当前会话"
        "最近收到的那张图。"
        "只有分析更早的历史图片或网络图片时才填 image_url（本地路径或 http 链接）。"
        "绝对不要自己编造 URL，也不要填“[图片]”这类占位文字。"
    )
    parameters = {
        "msg_id": {
            "type": "string",
            "description": "指定某一条消息里的图：消息 id，或 msg:123。填了就不用填 image_url。"
        },
        "image_url": {
            "type": "string",
            "description": "留空即用当前会话最近一张图；也可填本地文件路径、http(s) 链接或 data URI。",
        },
        "question": {"type": "string",
                     "description": "关于图片的问题，空字符串表示描述图片内容"},
    }

    # What the model tends to send when it cannot see the picture: the
    # placeholder text of the message itself, or a URL it invented.  Treated as
    # "no image given" rather than as a value to fetch, which is what produced
    # "Invalid base64 data" and then a reply claiming she could not see it.
    _PLACEHOLDERS = frozenset({
        "", "[图片]", "[图片]", "[媒体消息]", "[表情包]", "[视频]", "[文件]",
        "图片", "媒体消息", "表情包", "image", "photo", "null", "none",
    })

    # A fallback may only stand in for the picture the user is talking about.
    # Beyond this many seconds from the message being answered, an old picture
    # is not "the image" any more -- describing it confidently is worse than
    # saying the picture could not be read.
    _FALLBACK_WINDOW_SECONDS = 900

    def _current_chat(self) -> str:
        extra = getattr(self.ctx, "extra", {}) or {}
        return str(extra.get("chat_id") or "").strip()

    def _current_account(self) -> str:
        extra = getattr(self.ctx, "extra", {}) or {}
        return str(extra.get("account_id") or "").strip()

    def _reference_time(self):
        """The moment being answered: this turn's message time, else now.

        Using the message's own time rather than the wall clock matters when a
        turn is processed late or replayed -- the picture to pick is the one
        belonging to that moment, not whichever arrived most recently overall.
        """

        extra = getattr(self.ctx, "extra", {}) or {}
        raw = extra.get("message_time")
        if isinstance(raw, datetime):
            return raw
        if raw:
            try:
                return datetime.fromisoformat(str(raw))
            except ValueError:
                warn("agents/tools/analysis.py:175 ValueError", err=repr(__import__("sys").exc_info()[1]))
        return datetime.now()

    def _media_by_msg_id(self, msg_id: str) -> str:
        """按消息 id 取那条消息的图片路径。群里连发多张图时用它指定是哪一张。"""
        import sqlite3

        _raw = str(msg_id or "").strip()
        if _raw.startswith("msg:"):
            _raw = _raw[4:].strip()
        try:
            _mid = int(_raw)
        except Exception:  # noqa: BLE001
            return ""
        _db = ""
        try:
            from loop.ports import pick_db
            _db = pick_db()
        except Exception:  # noqa: BLE001
            _db = ""
        if not _db:
            try:
                from core.config.settings import config as _cfg
                _db = _cfg.get_account_db_path("wechat", self._account_id())
            except Exception:  # noqa: BLE001
                _db = ""
        if not _db:
            return ""
        try:
            _uri = "file:%s?mode=ro" % str(_db).replace("\\", "/")
            _conn = sqlite3.connect(_uri, uri=True, timeout=2.0)
            try:
                _cur = _conn.execute(
                    "select media_path from messages where id=? and coalesce(media_path,'')<>'' limit 1",
                    (_mid,))
                _row = _cur.fetchone()
            finally:
                _conn.close()
        except Exception:  # noqa: BLE001
            return ""
        _mp = str(_row[0] or "") if _row else ""
        if _mp and not os.path.isabs(_mp):
            try:
                from loop import ports as _ports
                _mp = os.path.join(str(getattr(_ports, "ROOT", "")), _mp)
            except Exception:  # noqa: BLE001
                pass
        return _mp

    def _latest_chat_image(self) -> str:
        """The image this turn is about, or "" when there is not one.

        Chosen by proximity to the moment being answered, so a late-processed
        turn still picks its own picture instead of whatever row was written
        last.  An image outside the window is refused rather than substituted.
        """
        chat_id = self._current_chat()
        if not chat_id:
            return ""
        try:
            from core.terminal.store_port import get_repo
            from core.conversation.media_registry import rows_for_chat

            rows = rows_for_chat(get_repo().engine, chat_id, limit=20,
                                 resolved_only=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("analyze_image_lookup_failed", error=str(exc)[:140])
            return ""

        reference = self._reference_time()
        account_id = self._current_account()
        best = None
        best_delta = None
        skipped_self = 0
        for row in rows:
            path = str(row.local_path or "")
            if not path or not Path(path).exists():
                continue
            # 她发出去的图不是"这轮要看的那张"：2026-09-24 的事故里，兜底把她
            # 自己 7 分钟前发出的表情包当成了别人引用的那张去描述。宁可拒绝，
            # 也不要分析自己的输出。
            sender = str(getattr(row, "sender_id", "") or "")
            if account_id and sender and sender == account_id:
                skipped_self += 1
                continue
            moment = row.created_at
            if moment is None:
                continue
            delta = abs((reference - moment).total_seconds())
            if delta <= self._FALLBACK_WINDOW_SECONDS and (best_delta is None
                                                           or delta < best_delta):
                best, best_delta = path, delta
        if best is None:
            log.info("analyze_image_no_recent_image", chat_id=chat_id,
                     reference=reference.isoformat(timespec="seconds"),
                     candidates=len(rows), skipped_self=skipped_self)
            return ""
        log.info("analyze_image_picked_by_time", chat_id=chat_id,
                 path=Path(best).name, seconds_away=round(best_delta or 0),
                 skipped_self=skipped_self)
        return best

    def _mark_availability(self, available: bool) -> None:
        """Tell the turn whether an image could be read.

        The agent uses this to forbid answering as if the picture had been seen:
        getting a lookup failure and then describing the picture anyway is the
        worst outcome, because it reads exactly like a correct answer.
        """
        extra = getattr(self.ctx, "extra", None)
        if isinstance(extra, dict):
            extra["image_available"] = bool(available)

    def _resolve_image(self, raw: str) -> tuple[str, str, str]:
        """Return ``(url_or_data_uri, source_path, error)``.

        Order matters and is deliberate: the picture this turn is *about* wins
        over any picture that merely happens to be recent.  A quoted sticker /
        image is that picture; a picture she sent herself never is.
        """
        from core.conversation.media import media_path_to_data_uri

        candidate = str(raw or "").strip()
        looks_like_a_value = (
            candidate.lower() not in self._PLACEHOLDERS
            and not candidate.startswith("[")
            and "example.com" not in candidate.lower()
        )
        if looks_like_a_value:
            if candidate.startswith(("http://", "https://", "data:")):
                return candidate, "", ""
            uri, path = self._uri_for_path(candidate)
            if uri:
                return uri, path, ""
            log.info("analyze_image_ref_unusable", value=candidate[:60])

        extra = getattr(self.ctx, "extra", {}) or {}

        # 0) 回合锚（WorldState 的第一块）：结构化的"这轮在看什么"，优先于一切
        #    按名字/时间的兜底。锚里 quoted 高于 current/attached；锚命中不了时
        #    直接诚实失败，绝不换一张图。
        referent = extra.get("referent") or {}
        ref_media = [
            item for item in (referent.get("media") or [])
            if isinstance(item, dict)
        ] if isinstance(referent, dict) else []
        if ref_media:
            target = [
                item for item in ref_media if item.get("role") == "quoted"
            ] or ref_media
            for item in target:
                uri, path = self._uri_for_path(str(item.get("path") or ""))
                if uri:
                    log.info("analyze_image_used_referent_path",
                             role=str(item.get("role") or ""),
                             path=Path(path).name)
                    return uri, path, ""
                md5 = str(item.get("md5") or "")
                if md5:
                    uri, path = self._sticker_uri(md5)
                    if uri:
                        log.info("analyze_image_used_referent_sticker",
                                 role=str(item.get("role") or ""),
                                 sticker_id=md5, path=Path(path).name)
                        return uri, path, ""
            log.info("analyze_image_referent_unresolved",
                     chat_id=self._current_chat(), candidates=len(target))
            return "", "", ("这轮要看的图/表情包没有可用的图片文件。"
                            "不要拿别的图顶替：直说读不到这张，请对方重发。")

        # 1) 引用指向的图/表情包：这轮问的就是它，拿不到就如实说，不换一张。
        quoted_path = str(extra.get("quoted_media_path") or "").strip()
        quoted_sticker = str(extra.get("quoted_sticker_id") or "").strip()
        if quoted_path or quoted_sticker:
            uri, path = self._uri_for_path(quoted_path) if quoted_path else ("", "")
            if uri:
                log.info("analyze_image_used_quoted_path", path=Path(path).name)
                return uri, path, ""
            uri, path = self._sticker_uri(quoted_sticker)
            if uri:
                log.info("analyze_image_used_quoted_sticker",
                         sticker_id=quoted_sticker, path=Path(path).name)
                return uri, path, ""
            log.info("analyze_image_quoted_unresolved",
                     chat_id=self._current_chat(), has_path=bool(quoted_path),
                     sticker_id=quoted_sticker[:12])
            return "", "", ("这轮问的是被引用的那张图/表情包，但它的图片文件没拿到。"
                            "不要拿最近的图顶替：直说读不到这张，请对方重发。")

        # 2) 本轮已经解析出的媒体（她/对方刚发的图、刚才那张）：直接用。
        turn_paths = [str(p) for p in (extra.get("media_paths") or []) if str(p)]
        for raw_path in turn_paths:
            uri, path = self._uri_for_path(raw_path)
            if uri:
                log.info("analyze_image_used_turn_media", path=Path(path).name)
                return uri, path, ""
        if turn_paths:
            log.info("analyze_image_turn_media_unusable",
                     chat_id=self._current_chat(), candidates=len(turn_paths))
            return "", "", ("这轮消息附带的图片文件读不到，不要拿别的图顶替："
                            "直说你看不到这张。")

        # 3) 当前消息本身就是表情包：按 md5 找缓存里的图（媒体行可能还是
        #    pending，但表情包文件通常已经在 emoji 缓存里）。
        try:
            from core.conversation.media import extract_sticker_md5
            own_sticker = extract_sticker_md5(
                str(extra.get("user_message") or "")
            )
        except Exception:  # noqa: BLE001
            warn("agents/tools/analysis.py:339 Exception", err=repr(__import__("sys").exc_info()[1]))
            own_sticker = ""
        if own_sticker:
            uri, path = self._sticker_uri(own_sticker)
            if uri:
                log.info("analyze_image_used_message_sticker",
                         sticker_id=own_sticker, path=Path(path).name)
                return uri, path, ""
            log.info("analyze_image_message_sticker_unresolved",
                     sticker_id=own_sticker)
            return "", "", ("这条消息是表情包，但它的图片文件没拿到，"
                            "不要拿别的图顶替：直说你看不到这张。")

        # 4) 兜底：按时间找这轮要回应时附近的图（不含她自己发出的）。
        path = self._latest_chat_image()
        if path:
            uri = media_path_to_data_uri(path)
            if uri:
                return uri, path, ""
            self._note_unreadable(path)
        return "", "", ("没拿到可用的图片：这个会话最近没有解析到图片文件。"
                        "请让用户重发，或先说明你看不到这张图。")

    def _existing_path(self, raw: str) -> Path | None:
        """A path that really is on disk, tolerating relative project paths."""
        candidate = str(raw or "").strip()
        if not candidate:
            return None
        path = Path(candidate)
        if not path.is_absolute():
            # The model may hand back a path relative to the project root.
            for base in (Path.cwd(), Path.cwd() / "data"):
                if (base / candidate).exists():
                    path = base / candidate
                    break
        if path.exists() and path.is_file():
            return path
        return None

    def _uri_for_path(self, raw: str) -> tuple[str, str]:
        """``(data_uri, path)`` for a stored path, else two empty strings.

        The media layer refuses a file that cannot be decoded, so a truncated
        picture returns ``("", "")`` here and the caller's existing honest
        message ("直说读不到这张，请对方重发") runs -- instead of a vision call
        that the provider was always going to answer ``400 unsupported image``.
        """
        from core.conversation.media import media_path_to_data_uri

        candidate = str(raw or "").strip()
        if not candidate:
            return "", ""
        path = self._existing_path(candidate)
        if path is not None:
            uri = media_path_to_data_uri(str(path))
            if not uri:
                self._note_unreadable(str(path))
                return "", ""
            return uri, str(path)
        # Stored paths are often just the file name that lives in the cache.
        uri = media_path_to_data_uri(candidate)
        if uri:
            return uri, candidate
        self._note_unreadable(candidate)
        return "", ""

    @staticmethod
    def _note_unreadable(path: str) -> None:
        """Record why a picture was refused, next to the turn that asked for it.

        The judgement is the media layer's (``image_integrity`` -- its single
        owner); this only leaves the trace an operator reads when she answers
        "读不到这张", so a refusal is never silent.
        """
        try:
            from core.conversation.media import image_integrity

            verdict = image_integrity(path)
        except Exception as exc:  # noqa: BLE001
            warn("agents/tools/analysis.py:417 Exception", err=repr(exc))
            verdict = f"unknown({str(exc)[:40]})"
        log.warning("analyze_image_unreadable", verdict=verdict,
                    path=Path(path).name)

    def _sticker_uri(self, md5: str) -> tuple[str, str]:
        """``(data_uri, path)`` for a sticker's cached image, else two empty."""
        sticker = str(md5 or "").strip().lower()
        if not sticker:
            return "", ""
        try:
            from core.preprocess.emoji_recognition import find_emoji_image_path
        except Exception:  # noqa: BLE001
            warn("agents/tools/analysis.py:429 Exception", err=repr(__import__("sys").exc_info()[1]))
            return "", ""
        return self._uri_for_path(find_emoji_image_path(sticker))

    @staticmethod
    def _remember(path: str, description: str) -> None:
        """Store the description so the picture stays readable later.

        Both places matter: the in-process cache serves the same turn, and the
        media row is what survives a restart -- which is what lets a transcript,
        or the rolling summary built from it, name what the picture showed.
        """
        try:
            from core.conversation.media import remember_image_description

            remember_image_description([path], description)
        except Exception as exc:  # noqa: BLE001
            log.debug("analyze_image_remember_failed", error=str(exc)[:100])
        try:
            from core.terminal.store_port import get_repo
            from core.conversation.media_registry import remember_description

            stored = remember_description(get_repo().engine, path, description)
            if stored:
                log.info("image_description_stored", rows=stored,
                         path=Path(path).name)
        except Exception as exc:  # noqa: BLE001
            log.debug("analyze_image_store_failed", error=str(exc)[:100])

    async def execute(self, **kwargs) -> ToolResult:
        raw = kwargs.get("image_url", "")
        _mid_kw = str(kwargs.get("msg_id") or "").strip()
        if _mid_kw and not str(raw).strip():
            _picked = self._media_by_msg_id(_mid_kw)
            if _picked:
                raw = _picked
            else:
                return ToolResult(output="", error="消息 %s 里没有图片，或者找不到这条 id" % _mid_kw)
        question = str(kwargs.get("question") or "").strip() or "请详细描述这张图片的内容"

        image, source, error = self._resolve_image(raw)
        if not image:
            self._mark_availability(False)
            return ToolResult("", error=error)
        self._mark_availability(True)

        if source:
            # Already described this file -- reuse it rather than paying for the
            # same picture twice, which also keeps the answer consistent.
            try:
                from core.conversation.media import cached_image_description

                cached = cached_image_description([source])
            except Exception:  # noqa: BLE001
                warn("agents/tools/analysis.py:475 Exception", err=repr(__import__("sys").exc_info()[1]))
                cached = ""
            if cached:
                log.info("analyze_image_cache_hit", path=Path(source).name)
                return ToolResult(f"图片分析: {cached}")

        try:
            from core.bootstrap import get_llm_router
            router = get_llm_router()
            _res = (await router.call(
                "vision",
                agent="image_analyzer",
                task="vision",
                messages=[
                    {"role": "system", "content": "你是一个图片分析专家。请仔细观察图片，准确回答用户的问题。"},
                    {"role": "user", "content": question},
                ],
                # 2026-10-25：本地路径交给 adapter 会走"重新编码"那条易碎分支（mime 判不出/体积大就
                # 静默丢图）。这里先自己转成 data URI，adapter 的 _image_url() 就会原样透传。
                images=[_to_data_uri(image)],
                temperature=0.3,
                max_tokens=500,
            ))
            # 2026-10-25：思考型模型常把回答写进 reasoning、content 为空（deepseek-flash），
            # 所以两个都看；之前只取 .text，图送进去了却判定“模型返回为空”。
            result = _pick_text(_res)
            if result:
                if source:
                    self._remember(source, result)
                return ToolResult(f"图片分析: {result}")
            return ToolResult("", error="图片分析失败，模型返回为空")
        except Exception as e:
            warn("agents/tools/analysis.py:501 Exception", err=repr(e))
            return ToolResult("", error=f"图片分析失败: {str(e)[:100]}")


class DecisionHelperTool(BaseTool):
    name = "decision_helper"
    description = "决策辅助：分析用户面临的抉择，生成各选项的优缺点、打分和推荐。用户纠结/选择困难时调用。"
    parameters = {
        "question": {"type": "string", "description": "用户面临的决策问题", "required": True},
        "options": {"type": "string", "description": "各选项，用|分隔，如'选项A|选项B|选项C'。空字符串则由LLM从问题中提取"},
        "context": {"type": "string", "description": "额外背景信息（用户偏好、约束条件等），空字符串表示无"},
    }

    def __init__(self, llm=None):
        self._llm = llm

    async def execute(self, **kwargs) -> ToolResult:
        question = kwargs.get("question", "").strip()
        options_str = kwargs.get("options", "").strip()
        context = kwargs.get("context", "").strip()
        if not question:
            return ToolResult("", error="question 不能为空")

        if not self._llm:
            return ToolResult("", error="LLM 未初始化，无法进行决策分析")

        options = [o.strip() for o in options_str.split("|") if o.strip()] if options_str else []

        if not options:
            try:
                resp = await self._llm.chat(
                    system="从用户的决策问题中提取各选项。返回JSON: {\"options\": [\"选项1\", \"选项2\", ...]}\n如果问题中没有明确选项，根据问题推断2-3个常见选项。",
                    user=question,
                    temperature=0.2,
                    max_tokens=200,
                )
                parsed = self._llm.parse_json(resp, {"options": []})
                options = [o for o in parsed.get("options", []) if o]
            except Exception as e:
                log.warning("decision_extract_options_failed", error=str(e))
                options = ["选项A", "选项B"]

        if len(options) < 2:
            options = options + ["其他方案"]

        context_section = ""
        if context:
            context_section = f"\n\n用户提供的额外背景:\n{context}"

        analysis_prompt = f"""你是一个决策分析助手。请对以下决策进行结构化分析。

决策问题: {question}
选项: {', '.join(options)}
{context_section}

请用JSON格式回复:
{{
  "summary": "一句话概括决策核心",
  "options": [
    {{
      "name": "选项名",
      "pros": ["优点1", "优点2", "优点3"],
      "cons": ["缺点1", "缺点2", "缺点3"],
      "score": 1-10的推荐分数,
      "risk": "主要风险或注意事项"
    }}
  ],
  "key_factors": ["影响决策的关键因素1", "关键因素2"],
  "recommendation": "综合推荐及理由",
  "suggestion": "给用户的建议，像朋友一样口语化"
}}

要求:
- 每个选项至少3个优点和3个缺点
- score基于综合考量，考虑用户背景
- recommendation要结合所有选项的优劣给出明确倾向
- suggestion用温暖口语，像朋友帮分析一样"""

        try:
            resp = await self._llm.chat(
                system=analysis_prompt,
                user=f"帮我分析: {question}",
                temperature=0.5,
                max_tokens=800,
            )
            parsed = self._llm.parse_json(resp, {
                "summary": "", "options": [], "key_factors": [],
                "recommendation": "", "suggestion": "",
            })
        except Exception as e:
            log.warning("decision_analysis_failed", error=str(e))
            return ToolResult("", error=f"决策分析失败: {e}")

        # 2026-10-25：思考型模型（deepseek-flash）常把内容写在 reasoning、content 为空，
        # 客户端只回 ""，于是上面 parse_json 得到全空、界面显示「🤔 决策分析: 」后面没内容。
        # 这里补一次"要正文、不要 JSON"的兜底请求。
        if not str(resp or "").strip():
            log.warning("decision_empty_json_retry")
            try:
                resp2 = await self._llm.chat(
                    system="你是朋友，帮他分析这个选择。用 3~5 句中文说清：各自的好处在哪、你更倾向哪个、为什么。不要 JSON，不要分点。",
                    user=f"{question}\n（选项：{', '.join(options)}）",
                    temperature=0.6,
                    max_tokens=700,
                )
            except Exception as exc:  # noqa: BLE001
                resp2 = ""
                log.warning("decision_retry_failed", error=str(exc)[:120])
            if str(resp2 or "").strip():
                return ToolResult("💬 " + str(resp2).strip())
            return ToolResult("", error="决策分析没有返回正文（模型只给了思考）")
        summary = parsed.get("summary", "")
        opt_list = parsed.get("options", [])
        key_factors = parsed.get("key_factors", [])
        recommendation = parsed.get("recommendation", "")
        suggestion = parsed.get("suggestion", "")

        # 2026-10-25：思考型模型把内容写进 reasoning、content 为空时，parse_json 得到
        # 一个字段全空的 dict（resp 本身不是空串，所以上面的 resp 判空拦不住）。
        # 这里按"解析结果是否全空"再兜一次：换成要正文、不要 JSON 的普通请求。
        if not any([summary, opt_list, key_factors, recommendation, suggestion]):
            log.warning("decision_parsed_empty_retry")
            try:
                resp2 = await self._llm.chat(
                    system="你是朋友，帮他分析这个选择。用 3~5 句中文说清：各自的好处在哪、你更倾向哪个、为什么。不要 JSON，不要分点。",
                    user=f"{question}\n（选项：{', '.join(options)}）",
                    temperature=0.6,
                    max_tokens=700,
                )
            except Exception as exc:  # noqa: BLE001
                resp2 = ""
                log.warning("decision_retry_failed", error=str(exc)[:120])
            if str(resp2 or "").strip():
                return ToolResult("💬 " + str(resp2).strip())
            return ToolResult("", error="决策分析没有返回正文（模型只给了思考）")

        lines = [f"🤔 决策分析: {summary}\n"]
        for opt in opt_list:
            name = opt.get("name", "?")
            pros = opt.get("pros", [])
            cons = opt.get("cons", [])
            score = opt.get("score", 5)
            risk = opt.get("risk", "")
            lines.append(f"📌 {name} (推荐度: {score}/10)")
            if pros:
                lines.append("  ✅ 优点: " + " | ".join(pros))
            if cons:
                lines.append("  ❌ 缺点: " + " | ".join(cons))
            if risk:
                lines.append(f"  ⚠️ 风险: {risk}")
            lines.append("")

        if key_factors:
            lines.append("🔑 关键因素: " + " | ".join(key_factors))
        if recommendation:
            lines.append(f"\n💡 综合推荐: {recommendation}")
        if suggestion:
            lines.append(f"\n💬 {suggestion}")

        output = "\n".join(lines)
        return ToolResult(output, data={
            "summary": summary,
            "options": opt_list,
            "key_factors": key_factors,
            "recommendation": recommendation,
            "suggestion": suggestion,
        })


class WeatherTool(BaseTool):
    name = "get_weather"
    description = "查询天气信息：当前天气、温度、湿度、风力、未来预报。用户问天气时调用。"
    parameters = {
        "city": {"type": "string", "description": "城市名，如'上海'、'北京'。空字符串使用默认城市"},
        "days": {"type": "integer", "description": "预报天数 1-5，默认1（仅当天）"},
    }

    _WEATHER_CN = {
        "clear sky": "晴", "few clouds": "少云", "scattered clouds": "多云",
        "broken clouds": "阴", "overcast clouds": "阴天", "light rain": "小雨",
        "moderate rain": "中雨", "heavy intensity rain": "大雨",
        "very heavy rain": "暴雨", "light snow": "小雪", "snow": "雪",
        "heavy snow": "大雪", "thunderstorm": "雷暴", "mist": "薄雾",
        "fog": "雾", "haze": "霾", "drizzle": "毛毛雨",
        "shower rain": "阵雨", "light intensity drizzle": "细雨",
    }

    async def execute(self, **kwargs) -> ToolResult:
        from core.config.settings import config

        city = kwargs.get("city", "").strip()
        defaulted_city = ""
        if not city:
            # 2026-10-24：schema 一直写着「空字符串使用默认城市」，但这里以前直接
            # 报 city_required —— 模型白烧一次工具额度，用户还什么都看不到。
            # 现在真的回落到 config.weather.default_city（env: WEATHER_DEFAULT_CITY）。
            city = str(getattr(config.weather, "default_city", "") or "").strip()
            if city:
                defaulted_city = city
                log.info("weather_city_defaulted city=%s", city)
            else:
                return ToolResult(
                    "还没确定是哪个城市：先问用户「你在哪个城市」，别猜；"
                    "拿到城市后再调用一次本工具（本轮不要重复调用它）。"
                )

        days = min(max(kwargs.get("days", 1), 1), 5)

        api_key = config.weather.api_key
        if not api_key:
            result = await self._weather_without_api(city, days)
            return self._note_default_city(result, defaulted_city)

        try:
            result = await self._fetch_openweather(city, days, api_key, config.weather.lang)
        except Exception as e:
            log.warning("weather_api_failed", error=str(e)[:100])
            result = await self._weather_without_api(city, days)
        return self._note_default_city(result, defaulted_city)

    @staticmethod
    def _note_default_city(result: ToolResult, defaulted_city: str) -> ToolResult:
        # 把「用的是默认城市」写进结果，免得她把默认城市当成用户说的城市。
        if not defaulted_city or not getattr(result, "output", None):
            return result
        note = " ｜（这次没指定城市，用的是默认城市「" + defaulted_city + "」；如果不是这里，让用户告你个城市，再查一次。）"
        try:
            result.output = result.output + note
            return result
        except Exception:
            warn("agents/tools/analysis.py:693 Exception", err=repr(__import__("sys").exc_info()[1]))
            import dataclasses
            return dataclasses.replace(result, output=result.output + note)

    async def _fetch_openweather(self, city: str, days: int, api_key: str, lang: str) -> ToolResult:
        async with egress.async_client(timeout_ms=10_000) as client:
            geo_resp = await client.get(
                "https://api.openweathermap.org/geo/1.0/direct",
                params={"q": city, "limit": 1, "appid": api_key},
            )
            geo_resp.raise_for_status()
            geo_data = egress.parse_json(geo_resp, default=[])
            if not geo_data:
                return ToolResult("", error=f"找不到城市: {city}")
            lat = geo_data[0]["lat"]
            lon = geo_data[0]["lon"]
            resolved_name = geo_data[0].get("local_name", city)

            if lang == "zh_cn":
                for n in ("local_names",):
                    local = geo_data[0].get(n, {})
                    if isinstance(local, dict) and "zh" in local:
                        resolved_name = local["zh"]

            weather_resp = await client.get(
                "https://api.openweathermap.org/data/2.5/forecast",
                params={
                    "lat": lat, "lon": lon,
                    "appid": api_key, "units": "metric",
                    "lang": lang, "cnt": days * 8,
                },
            )
            weather_resp.raise_for_status()
            data = egress.parse_json(weather_resp)

        rows = data.get("list") or []
        if not rows:
            return ToolResult("", error="天气服务这次没给预报数据（返回体不是预期的 JSON）")
        current = rows[0]
        now_temp = current["main"]["temp"]
        now_feels = current["main"]["feels_like"]
        now_humidity = current["main"]["humidity"]
        now_desc = current["weather"][0]["description"]
        now_wind = current["wind"].get("speed", 0)

        lines = [f"📍 {resolved_name}  {now_desc}"]
        lines.append(f"🌡️ {now_temp:.0f}°C（体感 {now_feels:.0f}°C）")
        lines.append(f"💧 湿度 {now_humidity}%  🌬️ 风速 {now_wind:.1f}m/s")

        if days > 1:
            lines.append(f"\n📅 未来{days}天预报:")
            seen_dates = set()
            for item in rows:
                dt_txt = item["dt_txt"]
                date_part = dt_txt.split(" ")[0]
                if date_part in seen_dates:
                    continue
                if "12:00:00" not in dt_txt and "15:00:00" not in dt_txt:
                    continue
                seen_dates.add(date_part)
                temp_h = item["main"]["temp_max"]
                temp_l = item["main"]["temp_min"]
                desc = item["weather"][0]["description"]
                weekday = self._weekday(date_part)
                lines.append(f"  {weekday} {date_part}: {desc}，{temp_l:.0f}~{temp_h:.0f}°C")
                if len(seen_dates) >= days:
                    break

        return ToolResult("\n".join(lines), data={
            "city": resolved_name, "temp": now_temp,
            "feels_like": now_feels, "humidity": now_humidity,
            "description": now_desc, "wind_speed": now_wind,
        })

    async def _weather_without_api(self, city: str, days: int) -> ToolResult:
        try:
            result = await self._search_weather(city)
            if result:
                return result
        except Exception as e:
            log.warning("weather_search_failed", error=str(e)[:80])
        return ToolResult(
            f"未配置天气API密钥（WEATHER_API_KEY），无法获取{city}的实时天气。\n"
            f"请在 .env 中设置 WEATHER_API_KEY（从 openweathermap.org 免费获取）",
            error="no_api_key",
        )

    async def _search_weather(self, city: str) -> ToolResult | None:
        from agents.tools.web import WebSearchTool
        search_tool = WebSearchTool(llm=self._llm)
        result = await search_tool.execute(query=f"{city} 天气", num=3)
        if result and not result.is_error:
            return ToolResult(f"🔍 {city} 天气（搜索结果）:\n{result.output}")
        return None

    @staticmethod
    def _weekday(date_str: str) -> str:
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        try:
            from datetime import datetime as _dt
            d = _dt.strptime(date_str, "%Y-%m-%d")
            return weekdays[d.weekday()]
        except Exception as _:
            warn("agents/tools/analysis.py:794 Exception", err=repr(_))
            return ""
