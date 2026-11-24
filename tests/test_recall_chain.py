"""P3 召回链（core/memory/recall_chain.py）自测：开关三态 + 多路召回 + 窗口聚合 + hub 接线。

standalone 风格（同 tests/test_needs_memory_wiring.py）：`python tests/test_recall_chain.py`。
不碰生产库：load_rows 被 monkeypatch 或指向合成语料；FTS 索引落在 .test_tmp/recall_chain/。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["YINYUE_FEATURE_RECALL_CHAIN"] = "off"
os.environ["YINYUE_RECALL_CHAIN_DIR"] = str(ROOT / ".test_tmp" / "recall_chain" / "test_fts")

from core import features  # noqa: E402
from core.memory import recall_chain as rc  # noqa: E402
from core.memory.contracts import MemoryItem  # noqa: E402
from core.memory.hub import KIND_SOURCES, MemoryHub  # noqa: E402

PRINCIPAL = SimpleNamespace(
    person_id="wxid_owner",
    chat_id="recall_chain_test@chatroom",
    chat_type="group",
    platform="wechat",
    bot_account_id="wxid_bot",
)
GOLD = "我奶奶以前在长沙开过一家米粉店，叫白果园"


def _rows(count: int = 80) -> list[dict]:
    rows = []
    for index in range(count):
        content = f"第{index}条：今天天气不错，随便聊两句。"
        if index == 40:
            content = GOLD
        rows.append(
            {
                "event_id": f"e{index}",
                "at": f"2026-09-30 21:{index % 60:02d}:00",
                "type": "message",
                "actor": "wxid_x",
                "name": "P6",
                "content": content,
                "chat_type": "group",
                "chat_id": PRINCIPAL.chat_id,
            }
        )
    return rows

def _mode(value: str | None) -> None:
    if value is None:
        os.environ.pop("YINYUE_FEATURE_RECALL_CHAIN", None)
    else:
        os.environ["YINYUE_FEATURE_RECALL_CHAIN"] = value


def check(name: str, ok: bool) -> bool:
    print(("  PASS " if ok else "  FAIL ") + name)
    return bool(ok)


def test_flag_registered_and_default_off() -> bool:
    spec = features.FLAG_REGISTRY.get(features.RECALL_CHAIN)
    ok = check("登记 FLAG_REGISTRY", spec is not None)
    ok = check("kind=tri", bool(spec) and spec.kind == "tri") and ok
    ok = check("default=off", bool(spec) and str(spec.default) == features.MODE_OFF) and ok
    ok = check("进 TRI_STATE_FLAGS", features.RECALL_CHAIN in features.TRI_STATE_FLAGS) and ok
    ok = check("env 名规范", features.env_key(features.RECALL_CHAIN) == "YINYUE_FEATURE_RECALL_CHAIN") and ok
    _mode(None)
    ok = check("未设 env 时 mode()=off", rc.recall_chain_mode() == features.MODE_OFF) and ok
    _mode("on")
    ok = check("env=on 时 injecting", rc.recall_chain_mode() == features.MODE_ON and rc.chain_injecting()) and ok
    _mode("off")
    ok = check("回到 off", rc.recall_chain_mode() == features.MODE_OFF and not rc.chain_tracing()) and ok
    return ok


def test_off_mode_does_no_io() -> bool:
    _mode("off")
    original = rc.load_rows

    def boom(*args, **kwargs):
        raise AssertionError("off 档不该读库/建索引")

    rc.load_rows = boom  # type: ignore[assignment]
    try:
        items = rc.RecallChainProvider().recall(PRINCIPAL, "长沙米粉店")
    finally:
        rc.load_rows = original  # type: ignore[assignment]
    return check("off 档直接空手而归", list(items) == [])


def test_chain_rank_finds_planted_row() -> bool:
    rows = _rows()
    hits = rc.chain_rank(rows, "奶奶在长沙开的米粉店叫什么", use_vector=False, cache_key="t_plant")
    top = hits[0] if hits else None
    ok = check("召回有窗口", bool(hits))
    ok = check("窗口覆盖金行 40", bool(top) and top.start <= 40 < top.end) and ok
    ok = check("窗口原文含金句", bool(top) and "白果园" in top.content) and ok
    ok = check("金行在 members", bool(top) and 40 in top.members) and ok
    ok = check("至少两路命中同一窗口", bool(top) and len(top.routes) >= 2) and ok
    return ok


def test_short_query_still_works() -> bool:
    rows = _rows()
    hits = rc.chain_rank(rows, "长沙", use_vector=False, cache_key="t_short")
    ok = check("2 字查询不抛异常", True)
    ok = check("2 字查询仍命中金行窗口", bool(hits) and hits[0].start <= 40 < hits[0].end) and ok
    return ok


def test_fuse_and_windows_are_deterministic() -> bool:
    fused = rc.fuse({"vector": [40, 10], "fts": [10], "keyword": []})
    ok = check("双路命中压过单路", fused[10][0] > fused[40][0])
    ok = check("单路分数仍大于零", fused[40][0] > 0)
    ok = check("多路命中合进 routes", set(fused[10][1]) == {"vector", "fts"}) and ok
    ok = check("空路线不参与", fused.get(0) is None) and ok
    windows = rc.build_windows(_rows(), fused, span=2, topk=2)
    ok = check("窗口按分数降序", len(windows) == 2 and windows[0].score >= windows[1].score) and ok
    ok = check("各窗锚各自的行（允许重叠）", len({w.row_index for w in windows}) == len(windows)) and ok
    ok = check("窗口含命中行", any(40 in w.members or 10 in w.members for w in windows)) and ok
    return ok


def test_window_score_prefers_strong_anchor() -> bool:
    """task-10 变体：锚行分更高的窗口排在前面（top1 + top2 的机械定义）。"""
    fused = rc.fuse({"vector": [40, 10], "fts": [10], "keyword": [40]})
    windows = rc.build_windows(_rows(), fused, span=2, topk=2)
    ok = check("两个窗口", len(windows) == 2)
    ok = check("高分锚窗在前", bool(windows) and windows[0].row_index == 10) and ok
    ok = check("分数非升序不成立", bool(windows) and windows[0].score > windows[1].score) and ok
    return ok

def test_shadow_reads_but_returns_nothing() -> bool:
    rows = _rows()
    original_rows = rc.load_rows
    original_vector = rc.vector_ranks
    rc.load_rows = lambda chat_id, limit=rc.ROW_LIMIT: rows  # type: ignore[assignment]
    rc.vector_ranks = lambda *args, **kwargs: []  # type: ignore[assignment]
    detail: dict = {}
    try:
        _mode("shadow")
        items = rc.RecallChainProvider().recall(PRINCIPAL, "长沙米粉店", detail=detail)
        ok = check("shadow 不注入", list(items) == [])
        ok = check("shadow 留读数", detail.get("recall_chain", {}).get("rows") == len(rows)) and ok
        _mode("on")
        items = rc.RecallChainProvider().recall(PRINCIPAL, "长沙米粉店", detail=detail)
        first = items[0] if items else None
        ok = check("on 返回窗口", bool(items)) and ok
        ok = check("是 MemoryItem", isinstance(first, MemoryItem)) and ok
        ok = check("kind=recall_chain", bool(first) and first.kind == rc.RecallChainProvider.kind) and ok
        ok = check("窗口含金句", bool(first) and "白果园" in first.content) and ok
        ok = check(
            "metadata 带 row_index/role/window/score/routes",
            bool(first)
            and {"row_index", "role", "window", "score", "routes"} <= set(first.metadata),
        ) and ok
        ok = check("首个 item 是窗口锚行", bool(first) and first.metadata.get("role") == "anchor") and ok
        ok = check(
            "锚行内容就是那一行原文",
            bool(first)
            and str(first.content) == str(_rows()[int(first.metadata["row_index"])]["content"]),
        ) and ok
        ok = check("chat_id 对齐", bool(first) and first.chat_id == PRINCIPAL.chat_id) and ok
        ok = check("occurred_at 是 datetime", bool(first) and first.occurred_at is not None) and ok
        ok = check("memory_id 可溯源", bool(first) and first.memory_id.startswith("recall:")) and ok
    finally:
        rc.load_rows = original_rows  # type: ignore[assignment]
        rc.vector_ranks = original_vector  # type: ignore[assignment]
        _mode("off")
    return ok


def test_hub_routes_episode_to_recall_chain() -> bool:
    ok = check("KIND_SOURCES episode 含 recall_chain", "recall_chain" in KIND_SOURCES["episode"])

    class _Stub:
        kind = "recall_chain"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def recall(self, principal, query="", **kwargs):
            self.calls.append(str(query))
            return []

    stub = _Stub()
    hub = MemoryHub()
    hub.register(stub)
    hub.recall(PRINCIPAL, "长沙米粉店", kinds=["episode"], detail={})
    ok = check("episode 查询问到召回链", stub.calls == ["长沙米粉店"]) and ok
    return ok


def _struct(value: str | None) -> None:
    name = "YINYUE_FEATURE_RECALL_STRUCT"
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def test_struct_kind_picks_one_mode() -> bool:
    ok = check("文件名题 -> file", rc.struct_kind("我发你的压缩包，你还记得文件名吗？") == "file")
    ok = check("@串号题 -> at", rc.struct_kind("哎，我@你时后面那串号是多少？") == "at") and ok
    ok = check("重复数字题 -> rep", rc.struct_kind("我刚发的那串重复数字是多少来着？") == "rep") and ok
    ok = check("日期题 -> date", rc.struct_kind("那场斗蛐蛐是几月几号几点开始的喵？") == "date") and ok
    ok = check("无关题 -> 空（不做 union）", rc.struct_kind("你还记得我说那老师也教过咱们哪门课吗？") == "") and ok
    ok = check("file 优先于其它", rc.struct_kind("我发你的文件名里那串重复数字是几月几号？") == "file") and ok
    return ok


def test_periodic_digits_rejects_sticker_hash_and_short_runs() -> bool:
    ok = check("8080808080 是周期", rc.periodic_digits("8080808080"))
    ok = check("369369369 是周期", rc.periodic_digits("369369369")) and ok
    ok = check("表情包 hex 不算", not rc.periodic_digits("[表情包:a346aa0f320732]")) and ok
    ok = check("非周期数字串", not rc.periodic_digits("1234567")) and ok
    ok = check("太短不算", not rc.periodic_digits("8080", min_digits=6)) and ok
    return ok


def test_struct_ranks_file_mode_needs_raw_text() -> bool:
    _struct("on")
    try:
        raw = [{"content": "我发你的是惠登斯电桥测电阻，双臂桥测低值电阻.pdf"}, {"content": "随手一句"}]
        ok = check("原文命中 .pdf", rc.struct_ranks(raw, "那个电桥实验的文件全名是啥？") == [0])
        normalized_only = [{"content": "惠登斯电桥测电阻，双臂桥测低值电阻pdf"}]
        ok = check("_norm 后的文本不命中（必须传原文）", rc.struct_ranks(normalized_only, "文件名是啥？") == []) and ok
        _struct("off")
        ok = check("off 档零候选", rc.struct_ranks(raw, "文件名是啥？") == []) and ok
    finally:
        _struct(None)
    return ok


def test_struct_weight_default_and_fuse() -> bool:
    ok = check("默认权重 4.0", rc.STRUCT_WEIGHT_DEFAULT == 4.0 and rc.ROUTE_WEIGHTS.get("struct") == 4.0)
    fused = rc.fuse({"struct": [3], "vector": [3]})
    ok = check("struct 有一票", fused.get(3, (0.0, ()))[1] == ("struct", "vector")) and ok
    return ok


def test_chain_rank_file_question_covers_pdf_row() -> bool:
    _struct("on")
    try:
        rows = _rows(80)
        rows[40]["content"] = "我发你的是惠登斯电桥测电阻，双臂桥测低值电阻.pdf"
        hits = rc.chain_rank(rows, "那个电桥实验的文件全名是啥？", use_vector=False)
        ok = check("命中窗口覆盖 40", bool(hits) and hits[0].start <= 40 <= hits[0].end)
        ok = check("routes 带 struct", bool(hits) and "struct" in hits[0].routes) and ok
    finally:
        _struct(None)
    return ok


def test_shadow_probe_logs_fields() -> bool:
    from structlog.testing import capture_logs

    rows = _rows()
    original_rows = rc.load_rows
    original_vector = rc.vector_ranks
    rc.load_rows = lambda chat_id, limit=rc.ROW_LIMIT: rows  # type: ignore[assignment]
    rc.vector_ranks = lambda *args, **kwargs: []  # type: ignore[assignment]
    context = SimpleNamespace(extra={"user_message": "奶奶在长沙开的那个米粉店叫什么名字来着"})
    try:
        _mode("shadow")
        with capture_logs() as logs:
            items = rc.RecallChainProvider().recall(
                PRINCIPAL, "长沙 米粉店 名字", context=context, detail={}
            )
        ok = check("shadow 仍不注入", list(items) == [])
        shadow = [row for row in logs if row.get("event") == "recall_chain_shadow"]
        ok = check("记 recall_chain_shadow", len(shadow) == 1) and ok
        row = shadow[0] if shadow else {}
        fields = {
            "mode",
            "chat_id",
            "query",
            "query_len",
            "routes_json",
            "hits",
            "hits_top",
            "suppressed_by_window",
            "elapsed_ms",
            "inject_cap",
            "inject_plan",
        }
        ok = check("字段齐全", fields <= set(row)) and ok
        ok = check("mode=shadow", row.get("mode") == "shadow") and ok
        top = (row.get("hits_top") or [{}])[0]
        ok = check(
            "hits_top 带 memory_id/行号/分数",
            {"memory_id", "row", "score"} <= set(top)
            and str(top.get("memory_id")).startswith("recall:"),
        ) and ok
        ok = check(
            "routes_json 可解析", isinstance(json.loads(str(row.get("routes_json"))), dict)
        ) and ok
        ok = check(
            "inject_plan 是注入顺序",
            bool(row.get("inject_plan"))
            and {"memory_id", "row", "role", "score"} <= set(row["inject_plan"][0]),
        ) and ok
        diff = [item for item in logs if item.get("event") == "query_style_diff"]
        ok = check("记 query_style_diff", len(diff) == 1) and ok
        cell = diff[0] if diff else {}
        ok = check(
            "三口径 Jaccard 在 [0,1]",
            all(
                0.0 <= float(cell.get(name) or 0.0) <= 1.0
                for name in ("jaccard_prod_raw", "jaccard_prod_bag", "jaccard_bag_raw")
            ),
        ) and ok
        ok = check(
            "bag/raw 口径有值", bool(cell.get("bag_query")) and bool(cell.get("raw_query"))
        ) and ok
    finally:
        rc.load_rows = original_rows  # type: ignore[assignment]
        rc.vector_ranks = original_vector  # type: ignore[assignment]
        _mode("off")
    return ok


def test_shadow_off_logs_nothing() -> bool:
    from structlog.testing import capture_logs

    _mode("off")
    original_rows = rc.load_rows

    def boom(*args, **kwargs):
        raise AssertionError("off 档不该读库")

    rc.load_rows = boom  # type: ignore[assignment]
    try:
        with capture_logs() as logs:
            items = rc.RecallChainProvider().recall(PRINCIPAL, "长沙 米粉店")
    finally:
        rc.load_rows = original_rows  # type: ignore[assignment]
    events = [str(row.get("event") or "") for row in logs]
    ok = check("off 档零注入", list(items) == [])
    ok = check(
        "off 档不记影子/对照",
        not any(name in ("recall_chain_shadow", "query_style_diff") for name in events),
    ) and ok
    return ok


def test_shadow_plan_matches_on_items() -> bool:
    from structlog.testing import capture_logs

    rows = _rows()
    original_rows = rc.load_rows
    original_vector = rc.vector_ranks
    rc.load_rows = lambda chat_id, limit=rc.ROW_LIMIT: rows  # type: ignore[assignment]
    rc.vector_ranks = lambda *args, **kwargs: []  # type: ignore[assignment]
    try:
        _mode("on")
        items = rc.RecallChainProvider().recall(PRINCIPAL, "长沙 米粉店", limit=5)
        on_ids = [item.memory_id for item in items]
        _mode("shadow")
        with capture_logs() as logs:
            rc.RecallChainProvider().recall(PRINCIPAL, "长沙 米粉店", limit=5)
        shadow = [row for row in logs if row.get("event") == "recall_chain_shadow"]
        plan = (shadow[0].get("inject_plan") if shadow else []) or []
        plan_ids = [str(entry.get("memory_id")) for entry in plan]
        plan_event_ids = [str(entry.get("memory_id_event")) for entry in plan]
        on_row_ids = [
            f"recall:{PRINCIPAL.chat_id}:{int(item.metadata.get('row_index'))}" for item in items
        ]
        ok = check("on 有注入", bool(on_ids))
        ok = check("shadow 行号键与 on 同序", bool(plan_ids) and plan_ids == on_row_ids) and ok
        ok = check("shadow 事件键与 on 注入同 id", bool(plan_event_ids) and plan_event_ids == on_ids) and ok
        ok = check(
            "cap 跟 limit", bool(shadow) and int(shadow[0].get("inject_cap") or 0) == 5
        ) and ok
    finally:
        rc.load_rows = original_rows  # type: ignore[assignment]
        rc.vector_ranks = original_vector  # type: ignore[assignment]
        _mode("off")
    return ok


def test_chain_rank_stats_reports_routes() -> bool:
    rows = _rows()
    stats: dict = {}
    hits = rc.chain_rank(rows, "长沙 米粉店", use_vector=False, cache_key="t_stats", stats=stats)
    ok = check("有窗口", bool(hits))
    ok = check(
        "route_sizes 含 keyword/fts", {"keyword", "fts"} <= set(stats.get("route_sizes") or {})
    ) and ok
    routes = stats.get("routes") or {}
    first = (routes.get("keyword") or [{}])[0]
    ok = check("每路带 top 行号与融合分", {"row", "fused", "pos"} <= set(first)) and ok
    ok = check(
        "window_top 对齐", float(stats.get("window_top") or 0.0) == float(hits[0].score)
    ) and ok
    return ok


def test_bag_query_and_suppression_helpers() -> bool:
    bag = rc.bag_query("奶奶在长沙开的一个米粉店叫什么名字")
    ok = check("bag 非空", bool(bag))
    ok = check("bag 无单字 token", all(len(token) >= 2 for token in bag.split())) and ok
    ok = check("bag 留实体词", "长沙" in bag) and ok
    ok = check("空输入 → 空", rc.bag_query("") == "") and ok

    old_flag = os.environ.get("YINYUE_FEATURE_CHAT_WINDOW")
    old_chats = os.environ.get("YINYUE_CHAT_WINDOW_CHATS")
    try:
        os.environ["YINYUE_FEATURE_CHAT_WINDOW"] = "0"
        ok = check("窗口关 → 未接管", rc.suppressed_by_window(PRINCIPAL.chat_id) is False) and ok
        os.environ["YINYUE_FEATURE_CHAT_WINDOW"] = "1"
        os.environ["YINYUE_CHAT_WINDOW_CHATS"] = PRINCIPAL.chat_id
        ok = check("窗口开+列了会话 → 接管", rc.suppressed_by_window(PRINCIPAL.chat_id) is True) and ok
        os.environ["YINYUE_CHAT_WINDOW_CHATS"] = "other@chatroom"
        ok = check("窗口开但没列 → 未接管", rc.suppressed_by_window(PRINCIPAL.chat_id) is False) and ok
    finally:
        for key, value in (
            ("YINYUE_FEATURE_CHAT_WINDOW", old_flag),
            ("YINYUE_CHAT_WINDOW_CHATS", old_chats),
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return ok


def test_raw_inbound_reads_new_sources() -> bool:
    """task-20 ②：raw_inbound 兼容新键 / Mapping context / window_turns，且绝不抛。"""
    ok = check("None → 空", rc.raw_inbound(None) == "")
    ok = check(
        "extra.raw_inbound",
        rc.raw_inbound(SimpleNamespace(extra={"raw_inbound": "原话一句"})) == "原话一句",
    ) and ok
    ok = check(
        "extra 旧键优先",
        rc.raw_inbound(
            SimpleNamespace(extra={"user_message": "旧键", "raw_inbound": "新键"})
        )
        == "旧键",
    ) and ok
    ok = check("Mapping context 的 current", rc.raw_inbound({"current": "问句原话"}) == "问句原话") and ok
    ok = check(
        "window_turns 最后一条 user 轮",
        rc.raw_inbound(
            SimpleNamespace(
                extra={"window_turns": [{"role": "user", "content": "轮里的原话"}]}
            )
        )
        == "轮里的原话",
    ) and ok
    ok = check("空白串不算原文", rc.raw_inbound({"current": "   "}) == "") and ok
    ok = check("坏对象不抛", rc.raw_inbound(object()) == "") and ok
    return ok


def test_provider_use_vector_is_threaded() -> bool:
    """task-20：provider 发射臂必须能关向量（生产 .venv 无 sentence_transformers）。"""
    import inspect

    signature = inspect.signature(rc.RecallChainProvider.recall)
    ok = check("签名有 use_vector=True", signature.parameters["use_vector"].default is True)

    seen: dict[str, object] = {}
    rows = _rows(12)

    def _fake_load_rows(chat_id, *, limit=None):  # type: ignore[no-untyped-def]
        return rows

    def _fake_chain_rank(rows_arg, query, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return []

    old_rows, old_rank = rc.load_rows, rc.chain_rank
    rc.load_rows, rc.chain_rank = _fake_load_rows, _fake_chain_rank
    try:
        _mode("shadow")
        rc.RecallChainProvider().recall(PRINCIPAL, "长沙", use_vector=False)
        ok = check("provider 透传 use_vector=False", seen.get("use_vector") is False) and ok
        rc.RecallChainProvider().recall(PRINCIPAL, "长沙")
        ok = check("默认仍为 True", seen.get("use_vector") is True) and ok
    finally:
        rc.load_rows, rc.chain_rank = old_rows, old_rank
        _mode("off")
    return ok



def test_embed_arm_env_and_local_fallback() -> bool:
    """task-21（2026-11-02 改契约）：EMBED_ARM env 取值照旧；local 臂已随去向量
    移除 —— ST 缺失时 `_load_embedder` 必须响亮地抛 ImportError，不许再退回
    一个不存在的 `_local_get_embedder()`（那是个被 except 吞掉的 NameError）。"""
    ok = True

    saved = os.environ.pop(rc.EMBED_ARM_ENV, None)
    try:
        ok &= check("arm_default_auto", rc.embed_arm() == "auto")
        os.environ[rc.EMBED_ARM_ENV] = "ST"
        ok &= check("arm_env_lowered", rc.embed_arm() == "st")
        os.environ[rc.EMBED_ARM_ENV] = "local"
        ok &= check("arm_env_local", rc.embed_arm() == "local")
        os.environ[rc.EMBED_ARM_ENV] = "auto"
        saved_st = sys.modules.get("sentence_transformers")
        sys.modules["sentence_transformers"] = None  # type: ignore[assignment]
        try:
            try:
                rc._load_embedder()
                ok &= check("st_missing_raises_loudly", False)
            except ImportError as exc:
                ok &= check("st_missing_raises_loudly", "local_embed" in str(exc))
        finally:
            if saved_st is None:
                sys.modules.pop("sentence_transformers", None)
            else:
                sys.modules["sentence_transformers"] = saved_st
    finally:
        os.environ.pop(rc.EMBED_ARM_ENV, None)
        if saved is not None:
            os.environ[rc.EMBED_ARM_ENV] = saved
    return ok

def test_warm_only_gate_and_env_flags() -> bool:
    """task-21: warm-only 默认开；冷缓存不加载模型、直接返回空向量路。"""
    import shutil

    ok = True
    rc.reset_embedder()
    saved_warm = os.environ.pop(rc.VECTOR_WARM_ONLY_ENV, None)
    saved_dir = os.environ.get("YINYUE_RECALL_CHAIN_DIR")
    saved_spawn = rc.warm_embedder_async
    tmp = ROOT / ".test_tmp" / "recall_chain" / "test_warm_gate"
    shutil.rmtree(tmp, ignore_errors=True)
    os.environ["YINYUE_RECALL_CHAIN_DIR"] = str(tmp)
    rc.warm_embedder_async = lambda: None  # type: ignore[assignment]
    try:
        ok &= check("warm_only_default_true", rc.vector_warm_only() is True)
        os.environ[rc.VECTOR_WARM_ONLY_ENV] = "0"
        ok &= check("warm_only_off_value", rc.vector_warm_only() is False)
        os.environ[rc.VECTOR_WARM_ONLY_ENV] = "on"
        ok &= check("warm_only_on_value", rc.vector_warm_only() is True)
        os.environ.pop(rc.VECTOR_WARM_ONLY_ENV, None)
        rows = _rows(12)
        ok &= check("cold_cache_returns_none", rc.row_vectors(rows, cache_key="gate_missing") is None)
        ok &= check("cold_cache_kept_model_unloaded", rc.embedder_ready() is False)
        ok &= check(
            "cold_cache_ranks_empty",
            rc.vector_ranks(rows, "长沙", cache_key="gate_missing") == [],
        )
        os.environ[rc.VECTOR_DEADLINE_ENV] = "nope"
        ok &= check(
            "deadline_bad_value_falls_back",
            rc.vector_deadline_seconds() == rc.VECTOR_DEADLINE_DEFAULT,
        )
    finally:
        os.environ.pop(rc.VECTOR_DEADLINE_ENV, None)
        rc.warm_embedder_async = saved_spawn  # type: ignore[assignment]
        if saved_dir is None:
            os.environ.pop("YINYUE_RECALL_CHAIN_DIR", None)
        else:
            os.environ["YINYUE_RECALL_CHAIN_DIR"] = saved_dir
        if saved_warm is not None:
            os.environ[rc.VECTOR_WARM_ONLY_ENV] = saved_warm
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


def test_query_embed_deadline() -> bool:
    """task-21: 查询嵌入超过 deadline 就放弃（不卡住回合），够了就返回。"""
    import time as _time

    class _Slow:
        def encode(self, texts, **kwargs):  # noqa: ANN001, ANN003
            _time.sleep(0.25)
            return [[0.0, 1.0]]

    ok = True
    rc._QUERY_INFLIGHT = False
    ok &= check("deadline_miss_returns_none", rc._encode_query(["q"], _Slow(), deadline=0.05) is None)
    _time.sleep(0.35)
    rc._QUERY_INFLIGHT = False
    got = rc._encode_query(["q"], _Slow(), deadline=2.0)
    ok &= check("deadline_hit_returns_vector", got is not None and len(got) == 1)
    rc._QUERY_INFLIGHT = False
    ok &= check("deadline_zero_waits", rc._encode_query(["q"], _Slow(), deadline=0.0) is not None)
    rc._QUERY_INFLIGHT = False
    return ok



# ---- task-22: incremental vector cache tests ----


def _fake_model():
    import numpy as np

    class _Model:
        batch_size = 8

        def encode(self, texts, **kwargs):
            out = []
            for text in texts:
                seed = float(sum(ord(ch) for ch in str(text)) % 97 + 1)
                out.append([seed, 1.0, 0.5])
            return np.asarray(out, dtype="float32")

    return _Model()


def _cache_dir(name: str) -> Path:
    path = ROOT / ".test_tmp" / "recall_chain" / "test_vec" / name
    path.mkdir(parents=True, exist_ok=True)
    for stale in path.glob("vec_*.npz"):
        try:
            stale.unlink()
        except OSError:
            pass
    return path


@contextmanager
def _cache_env(directory: Path, warm_only: str = "1"):
    names = ("YINYUE_RECALL_CHAIN_DIR", rc.VECTOR_WARM_ONLY_ENV)
    saved = {name: os.environ.get(name) for name in names}
    os.environ["YINYUE_RECALL_CHAIN_DIR"] = str(directory)
    os.environ[rc.VECTOR_WARM_ONLY_ENV] = warm_only
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_vector_cache_incremental_appends_one_row() -> bool:
    rows = [{"content": "甲甲甲"}, {"content": "乙乙乙"}, {"content": "丙丙丙"}]
    with _cache_env(_cache_dir("inc")):
        block = rc.row_vectors(rows, cache_key="t_inc", model=_fake_model())
        ok = check("首次全量 computed", bool(block) and block.mode == "computed" and block.known == 3)
        old_first = block.vectors[0].copy() if block else None
        rows2 = rows + [{"content": "丁丁丁"}]
        hit = rc.row_vectors(rows2, cache_key="t_inc")
        ok = check("增量模式", bool(hit) and hit.mode == "incremental") and ok
        ok = check("老行已知=3 新行 missing=1", bool(hit) and hit.known == 3 and hit.missing == 1) and ok
        ok = check("新行标 -1", bool(hit) and int(hit.index_map[-1]) == -1) and ok
        ok = check("老行向量不变", bool(hit) and bool((hit.vectors[0] == old_first).all())) and ok
        ranks = rc.vector_ranks(rows2, "乙乙乙", cache_key="t_inc", model=_fake_model())
        ok = check("向量路仍可用", bool(ranks) and all(0 <= int(index) <= 2 for index in ranks)) and ok
    return ok


def test_vector_cache_drops_only_the_changed_row() -> bool:
    rows = [{"content": "甲甲甲"}, {"content": "乙乙乙"}, {"content": "丙丙丙"}]
    with _cache_env(_cache_dir("chg")):
        rc.row_vectors(rows, cache_key="t_chg", model=_fake_model())
        rows2 = [{"content": "甲甲甲"}, {"content": "乙乙改"}, {"content": "丙丙丙"}]
        block = rc.row_vectors(rows2, cache_key="t_chg")
        ok = check("增量模式", bool(block) and block.mode == "incremental")
        ok = check("只掉一行", bool(block) and block.known == 2 and block.missing == 1) and ok
        ok = check("掉的是被改那行", bool(block) and int(block.index_map[1]) == -1) and ok
        ok = check(
            "未改行仍命中",
            bool(block) and int(block.index_map[0]) >= 0 and int(block.index_map[2]) >= 0,
        ) and ok
    return ok


def test_legacy_cache_without_hashes_takes_full_path() -> bool:
    import numpy as np

    rows = [{"content": "甲甲甲"}, {"content": "乙乙乙"}, {"content": "丙丙丙"}]
    directory = _cache_dir("legacy")
    path = directory / "vec_t_legacy.npz"
    texts = [" ".join(str(row["content"]).split())[: rc.EMBED_CHARS] for row in rows]
    np.savez(
        str(path),
        vectors=np.eye(3, dtype="float32"),
        fingerprint=np.asarray(rc._fingerprint(texts)),
    )
    with _cache_env(directory):
        exact = rc.row_vectors(rows, cache_key="t_legacy")
        ok = check("旧缓存指纹一致 -> exact", bool(exact) and exact.mode == "exact" and exact.known == 3)
        bigger = rc.row_vectors(rows + [{"content": "丁丁丁"}], cache_key="t_legacy")
        ok = check("旧缓存无法对齐 -> 冷启动 None", bigger is None) and ok
    return ok


def test_vector_ranks_skips_rows_without_vectors() -> bool:
    rows = [{"content": "甲甲甲"}, {"content": "乙乙乙"}]
    with _cache_env(_cache_dir("mask")):
        rc.row_vectors(rows, cache_key="t_mask", model=_fake_model())
        rows3 = rows + [{"content": "丙丙丙"}]
        stats: dict = {}
        ranks = rc.vector_ranks(
            rows3, "甲甲甲", cache_key="t_mask", model=_fake_model(), stats=stats
        )
        ok = check("只对已知行排序", bool(ranks) and 2 not in ranks)
        ok = check(
            "known/missing 进 stats",
            stats.get("vector_rows_known") == 2 and stats.get("vector_rows_missing") == 1,
        ) and ok
        ok = check("cache_mode=incremental", stats.get("vector_cache_mode") == "incremental") and ok
    return ok


def test_warm_script_append_merges_missing_rows() -> bool:
    import importlib.util

    rows = [{"content": "甲甲甲"}, {"content": "乙乙乙"}]
    directory = _cache_dir("warm")
    path = directory / "vec_t_warm.npz"
    try:
        path.unlink()
    except OSError:
        pass
    spec = importlib.util.spec_from_file_location(
        "warm_embed_cache_probe", ROOT / "scripts" / "warm_embed_cache.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows3 = rows + [{"content": "丙丙丙"}]
    texts3 = [" ".join(str(row["content"]).split())[: rc.EMBED_CHARS] for row in rows3]
    with _cache_env(directory):
        rc.row_vectors(rows, cache_key="t_warm", model=_fake_model())
        code = module._append_chat("t_warm", texts3, path=path, emb=_fake_model(), batch=2)
        ok = check("append 返回 0", code == 0)
        cached = rc._read_vector_cache(path)
        ok = check(
            "合并后 3 行向量",
            bool(cached) and len(cached[0]) == 3 and cached[1] is not None and len(cached[1]) == 3,
        ) and ok
        block = rc.row_vectors(rows3, cache_key="t_warm")
        ok = check("append 后指纹全等", bool(block) and block.mode == "exact" and block.known == 3) and ok
    return ok


# ---- task-23: background cache warmer tests ----


def test_warm_cache_for_tops_up_missing_rows() -> bool:
    rows = [{"content": "甲甲甲"}, {"content": "乙乙乙"}, {"content": "丙丙丙"}]
    with _cache_env(_cache_dir("topup"), warm_only="0"):
        first = rc.row_vectors(rows[:2], cache_key="t_topup", model=_fake_model())
        ok = check("先只缓存 2 行", bool(first) and first.mode == "computed" and first.known == 2)
        stats = rc.warm_cache_for("t_topup", rows=rows, max_new=10, model=_fake_model())
        ok = check(
            "补齐只算缺失 1 行",
            stats.get("added") == 1 and stats.get("missing_before") == 1,
        ) and ok
        ok = check("known=3", stats.get("known") == 3) and ok
        block = rc.row_vectors(rows, cache_key="t_topup", model=_fake_model())
        ok = check(
            "补齐后 row_vectors=exact", bool(block) and block.mode == "exact" and block.known == 3
        ) and ok
        again = rc.warm_cache_for("t_topup", rows=rows, max_new=10, model=_fake_model())
        ok = check(
            "已最新则加 0 行", again.get("added") == 0 and again.get("missing_before") == 0
        ) and ok
    return ok


def test_warm_interval_and_should_start() -> bool:
    saved = os.environ.pop(rc.WARM_INTERVAL_ENV, None)
    try:
        ok = check("默认 10 分钟", rc.warm_interval_minutes() == 10.0)
        os.environ[rc.WARM_INTERVAL_ENV] = "0"
        ok = check("0 = 关闭", rc.warm_interval_minutes() == 0.0) and ok
        ok = check("interval=0 不启动", not rc.warmer_should_start(0.0, "on")) and ok
        os.environ[rc.WARM_INTERVAL_ENV] = "-3"
        ok = check("负值 = 关闭", not rc.warmer_should_start(rc.warm_interval_minutes(), "on")) and ok
        os.environ[rc.WARM_INTERVAL_ENV] = "junk"
        ok = check("非法值回落默认", rc.warm_interval_minutes() == 10.0) and ok
        ok = check("on 档启动", rc.warmer_should_start(10.0, "on")) and ok
        ok = check("off 档不启动", not rc.warmer_should_start(10.0, "off")) and ok
    finally:
        os.environ.pop(rc.WARM_INTERVAL_ENV, None)
        if saved is not None:
            os.environ[rc.WARM_INTERVAL_ENV] = saved
    return ok


def test_warm_cache_for_reports_model_cold() -> bool:
    calls = []
    saved = rc.warm_embedder_async
    rc.warm_embedder_async = lambda: calls.append(True)  # type: ignore[assignment]
    try:
        with _cache_env(_cache_dir("cold")):
            rc.reset_embedder()
            rows = [{"content": "只有一行"}, {"content": "还有一行"}]
            stats = rc.warm_cache_for("t_cold", rows=rows, max_new=10)
        ok = check("模型冷时 skipped=model_cold", stats.get("skipped") == "model_cold")
        ok = check("并触发一次后台加载", len(calls) == 1) and ok
        ok = check("没有抛异常", isinstance(stats, dict)) and ok
    finally:
        rc.warm_embedder_async = saved  # type: ignore[assignment]
        rc.reset_embedder()
    return ok

def _warm_script_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "warm_embed_cache_probe24", ROOT / "scripts" / "warm_embed_cache.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_warm_script_full_save_writes_hashes() -> bool:
    """task-24：--force 落盘必须带 hashes，否则新消息一来整份作废。"""
    import numpy as np

    rows = [{"content": "甲甲甲"}, {"content": "乙乙乙"}]
    directory = _cache_dir("fullsave")
    path = directory / "vec_t_full.npz"
    texts = [" ".join(str(row["content"]).split())[: rc.EMBED_CHARS] for row in rows]
    fingerprints = rc._fingerprint(texts)
    vectors = np.asarray([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype="float32")
    module = _warm_script_module()
    module._save_full(path, vectors, texts, fingerprints)
    data = np.load(str(path), allow_pickle=False)
    ok = check("npz 含 hashes", "hashes" in data.files and len(data["hashes"]) == 2)
    ok = check(
        "hashes 与 row_hashes 一致",
        [str(item) for item in data["hashes"]] == [str(item) for item in rc.row_hashes(texts)],
    ) and ok
    with _cache_env(directory):
        block = rc.row_vectors(rows, cache_key="t_full")
        ok = check(
            "row_vectors 判 exact", bool(block) and block.mode == "exact" and block.known == 2
        ) and ok
    return ok


def test_warm_script_repair_hashes_upgrades_legacy() -> bool:
    """task-24：--repair-hashes 不加载模型即可把 legacy npz 升级成 hashed。"""
    import numpy as np

    rows = [{"content": "甲甲甲"}, {"content": "乙乙乙"}]
    directory = _cache_dir("repair")
    path = directory / "vec_t_rep.npz"
    texts = [" ".join(str(row["content"]).split())[: rc.EMBED_CHARS] for row in rows]
    np.savez(
        str(path),
        vectors=np.eye(2, dtype="float32"),
        fingerprint=np.asarray(rc._fingerprint(texts)),
    )
    module = _warm_script_module()
    code = module._repair_hashes(path, texts)
    data = np.load(str(path), allow_pickle=False)
    ok = check("repair 返回 0", code == 0)
    ok = check("升级后含 hashes", "hashes" in data.files and len(data["hashes"]) == 2) and ok
    ok = check("指纹保留", str(data["fingerprint"].item()) == rc._fingerprint(texts)) and ok
    with _cache_env(directory):
        block = rc.row_vectors(rows + [{"content": "丙丙丙"}], cache_key="t_rep")
        ok = check(
            "升级后可增量对齐", bool(block) and block.mode == "incremental" and block.known == 2
        ) and ok
    return ok


def test_warm_script_repair_hashes_refuses_row_mismatch() -> bool:
    """task-24：行数对不上时 repair 拒绝（提示 --force/--append），不写坏缓存。"""
    import numpy as np

    rows = [{"content": "甲甲甲"}, {"content": "乙乙乙"}]
    directory = _cache_dir("repairskip")
    path = directory / "vec_t_skip.npz"
    texts = [" ".join(str(row["content"]).split())[: rc.EMBED_CHARS] for row in rows]
    np.savez(
        str(path),
        vectors=np.eye(2, dtype="float32"),
        fingerprint=np.asarray(rc._fingerprint(texts)),
    )
    module = _warm_script_module()
    code = module._repair_hashes(path, texts + ["丙丙丙"])
    data = np.load(str(path), allow_pickle=False)
    ok = check("行数不符返回 1", code == 1)
    ok = check("没写 hashes", "hashes" not in data.files) and ok
    ok = check("向量原样", data["vectors"].shape == (2, 2)) and ok
    return ok


def _media(value: str | None) -> None:
    if value is None:
        os.environ.pop("YINYUE_FEATURE_MEDIA_RECALL", None)
    else:
        os.environ["YINYUE_FEATURE_MEDIA_RECALL"] = value


def test_media_recall_flag_registered() -> bool:
    from core import features

    spec = features.FLAG_REGISTRY.get(features.MEDIA_RECALL)
    ok = check("开关在 tri 集合", features.MEDIA_RECALL in features.TRI_STATE_FLAGS)
    ok = check(
        "默认 off",
        spec is not None and spec.kind == "tri" and spec.default == "off",
    ) and ok
    _media(None)
    ok = check("读侧默认 off", rc.media_recall_mode() == "off") and ok
    return ok


def test_media_route_needs_gate_and_question_cue() -> bool:
    rows = _rows(12)
    rows[5] = dict(
        rows[5],
        content="实验报告.pdf",
        media=[{"id": 1, "kind": "file", "name": "实验报告.pdf"}],
    )
    _media("off")
    ok = check("off 零候选", rc.media_ranks(rows, "我发你的那份实验报告，叫什么来着？") == [])
    _media("on")
    ok = check("没有媒体词不出路", rc.media_ranks(rows, "今天天气怎么样") == []) and ok
    order = rc.media_ranks(rows, "我发你的那份实验报告文件，叫什么来着？")
    ok = check("命中附件行", order[:1] == [5]) and ok
    _media(None)
    return ok


def test_media_route_strong_match_filters_weak_rows() -> bool:
    rows = _rows(12)
    rows[3] = dict(
        rows[3],
        content="大学物理实验.zip",
        media=[{"id": 3, "kind": "file", "name": "大学物理实验.zip"}],
    )
    rows[7] = dict(
        rows[7],
        content="课程表.xlsx",
        media=[{"id": 7, "kind": "file", "name": "课程表.xlsx"}],
    )
    _media("on")
    order = rc.media_ranks(rows, "我发你的压缩包叫什么名字？")
    ok = check("压缩包类命中排第一", order[:1] == [3])
    ok = check("弱候选被滤掉", order == [3]) and ok
    order = rc.media_ranks(rows, "上次那个文件叫什么来着？")
    ok = check("无强匹配退近因", order[:2] == [7, 3]) and ok
    _media(None)
    return ok


def test_annotate_rows_with_media_appends_label() -> bool:
    rows = _rows(4)
    media = [
        {
            "id": 11,
            "event_id": "e2",
            "msg_id": "",
            "kind": "file",
            "status": "ready",
            "original_name": "作业.pdf",
            "text_excerpt": "第一题 求电场",
        },
        {
            "id": 12,
            "event_id": "",
            "msg_id": "m9",
            "kind": "file",
            "status": "unsupported",
            "original_name": "扫描件.pdf",
            "text_excerpt": "",
        },
    ]
    annotated = rc.annotate_rows_with_media(rows, media)
    ok = check(
        "按 event_id 命中",
        "〔媒体：文件「作业.pdf」（P6 发）〕" in annotated[2]["content"],
    )
    ok = check("正文摘要进文本", "〔正文：第一题 求电场〕" in annotated[2]["content"]) and ok
    ok = check("media 列表带上", [item["name"] for item in annotated[2]["media"]] == ["作业.pdf"]) and ok
    ok = check("未命中的行原样", annotated[0]["content"] == rows[0]["content"]) and ok
    annotated2 = rc.annotate_rows_with_media([dict(rows[0], msg_id="m9")], media)
    ok = check(
        "按 msg_id 兜底",
        "扫描件.pdf" in annotated2[0]["content"] and "unsupported" in annotated2[0]["content"],
    ) and ok
    # 串人防御：同 msg_id 但发送人不同 ⇒ 不得贴到这一行
    other = [dict(media[1], sender_id="wxid_other")]
    annotated3 = rc.annotate_rows_with_media([dict(rows[0], msg_id="m9")], other)
    ok = check("发送人不同不贴标签", "〔媒体：" not in annotated3[0]["content"]) and ok
    # 图片/表情包的哈希名不当文件名
    shot = [{"id": 13, "event_id": "e1", "msg_id": "", "kind": "image", "status": "ok", "original_name": "c887310dâ¦_b.jpg"}]
    annotated4 = rc.annotate_rows_with_media([rows[1]], shot)
    ok = check("图片用类名不用哈希名", "〔媒体：图片" in annotated4[0]["content"] and "c887310d" not in annotated4[0]["content"]) and ok
    return ok


def test_chain_rank_media_route_only_when_enabled() -> bool:
    rows = _rows(12)
    rows[6] = dict(
        rows[6],
        content="大学物理实验.zip",
        media=[{"id": 6, "kind": "file", "name": "大学物理实验.zip"}],
    )
    question = "我发你的压缩包叫什么名字？"
    _mode("on")
    _struct("on")
    _media("off")
    stats_off: dict = {}
    rc.chain_rank(rows, question, cache_key="media_route_test", stats=stats_off)
    ok = check("关闸时无 media 路", "media" not in stats_off.get("routes", {}))
    _media("on")
    stats_on: dict = {}
    hits = rc.chain_rank(rows, question, cache_key="media_route_test", stats=stats_on)
    ok = check("开闸后有 media 路", "media" in stats_on.get("routes", {})) and ok
    ok = check("媒体行成为锚", bool(hits) and hits[0].row_index == 6) and ok
    _media(None)
    _mode(None)
    _struct(None)
    return ok


def test_row_memory_id_lens_follows_mode() -> bool:
    rows = _rows(12)
    ok = check("off 用行号", rc.row_memory_id("c", rows, 3, mode="off") == "recall:c:3")
    ok = check("shadow 用行号", rc.row_memory_id("c", rows, 3, mode="shadow") == "recall:c:3") and ok
    ok = check("on 用 event_id", rc.row_memory_id("c", rows, 3, mode="on") == "recall:c:e3") and ok
    ok = check(
        "老数据没有 event_id 时退回行号",
        rc.row_memory_id("c", [{}], 0, mode="on") == "recall:c:0",
    ) and ok
    ok = check("越界行号不抛", rc.row_memory_id("c", rows, 99, mode="on") == "recall:c:99") and ok
    return ok


def test_anchor_span_reads_unknown_as_empty() -> bool:
    ok = check("没有 span 列读成空", rc.anchor_span({}) == [])
    ok = check(
        "-1/-1 读成空", rc.anchor_span({"content_start": -1, "content_end": -1}) == []
    ) and ok
    ok = check("真实 span 原样返回", rc.anchor_span({"content_start": 2, "content_end": 7}) == [2, 7]) and ok
    ok = check(
        "end<start 视为未知", rc.anchor_span({"content_start": 7, "content_end": 2}) == []
    ) and ok
    ok = check(
        "脏值不抛", rc.anchor_span({"content_start": "x", "content_end": None}) == []
    ) and ok
    return ok


def test_anchor_identity_survives_new_rows() -> bool:
    rows = _rows(30)
    shifted = _rows(10) + rows  # 10 条新消息插到最早处：位置整体 +10
    before = rc.row_memory_id("c", rows, 12, mode="on")
    after = rc.row_memory_id("c", shifted, 22, mode="on")
    ok = check("同一事件在新旧语料里 id 相同", before == after == "recall:c:e12")
    ok = check(
        "同一事件的行号口径确实漂了",
        rc.row_memory_id("c", rows, 12, mode="off")
        != rc.row_memory_id("c", shifted, 22, mode="off"),
    ) and ok
    anchor, neighbors = rc.anchor_identity(rows, 12, (10, 11, 12, 13, 14))
    ok = check("锚行 id 取自窗口锚", anchor == "e12") and ok
    ok = check("邻居不吃锚行、保持成员序", neighbors == ("e10", "e11", "e13", "e14")) and ok
    ok = check("邻居数不超过 ±span", len(neighbors) <= rc.WINDOW_SPAN * 2) and ok
    return ok

def test_on_items_carry_stable_anchor_metadata() -> bool:
    rows = _rows()
    original_rows = rc.load_rows
    original_vector = rc.vector_ranks
    rc.load_rows = lambda chat_id, limit=rc.ROW_LIMIT: rows  # type: ignore[assignment]
    rc.vector_ranks = lambda *args, **kwargs: []  # type: ignore[assignment]
    try:
        _mode("on")
        items = rc.RecallChainProvider().recall(PRINCIPAL, "长沙米粉店", limit=6)
        ok = check("on 有注入项", bool(items))
        first = items[0] if items else None
        ok = check(
            "memory_id 走 event 锚",
            bool(first) and first.memory_id == f"recall:{PRINCIPAL.chat_id}:e40",
        ) and ok
        ok = check("metadata 保留 row_index", bool(first) and "row_index" in first.metadata) and ok
        ok = check(
            "metadata 有 anchor_message_id",
            bool(first) and first.metadata.get("anchor_message_id") == "e40",
        ) and ok
        ok = check(
            "metadata 有 anchor_span（老数据为 []）",
            bool(first) and first.metadata.get("anchor_span") == [],
        ) and ok
        ok = check(
            "neighbors 是 list 且长度受限",
            all(
                isinstance(item.metadata.get("neighbors"), list)
                and len(item.metadata["neighbors"]) <= rc.WINDOW_SPAN * 2
                for item in items
            ),
        ) and ok
        ok = check(
            "source_event_ids 保留真实事件锚",
            bool(first) and first.source_event_ids == ("e40",),
        ) and ok
        _mode("off")
        hits = rc.chain_rank(rows, "长沙米粉店", cache_key="anchor_identity", use_vector=False)
        ok = check(
            "chain_rank 也带稳定锚",
            bool(hits) and hits[0].anchor_event_id == f"e{hits[0].row_index}",
        ) and ok
        ok = check(
            "窗口邻居不超过 ±span",
            all(len(hit.neighbor_event_ids) <= rc.WINDOW_SPAN * 2 for hit in hits),
        ) and ok
    finally:
        rc.load_rows = original_rows  # type: ignore[assignment]
        rc.vector_ranks = original_vector  # type: ignore[assignment]
        _mode("off")
    return ok


def test_revoked_recall_row_is_not_injected() -> bool:
    from core.memory import belief_commit

    rows = _rows()
    original_rows = rc.load_rows
    original_vector = rc.vector_ranks
    original_state = belief_commit.evidence_state
    rc.load_rows = lambda chat_id, limit=rc.ROW_LIMIT: rows  # type: ignore[assignment]
    rc.vector_ranks = lambda *args, **kwargs: []  # type: ignore[assignment]
    try:
        _mode("on")
        belief_commit.evidence_state = (  # type: ignore[assignment]
            lambda ref, scope: "unknown" if ref == "e40" else "alive"
        )
        unknown_items = rc.RecallChainProvider(verify_evidence=True).recall(
            PRINCIPAL, "长沙米粉店", limit=6,
        )
        unknown_gold = [item for item in unknown_items if "白果园" in item.content]
        ok = check(
            "unknown evidence stays but is labelled",
            bool(unknown_gold)
            and unknown_gold[0].source_event_ids == ("e40",)
            and unknown_gold[0].metadata.get("evidence_state") == "unknown",
        )

        belief_commit.evidence_state = (  # type: ignore[assignment]
            lambda ref, scope: "dead" if ref == "e40" else "alive"
        )
        dead_items = rc.RecallChainProvider(verify_evidence=True).recall(
            PRINCIPAL, "长沙米粉店", limit=6,
        )
        ok = ok and check(
            "revoked evidence is not injected",
            all("白果园" not in item.content for item in dead_items),
        )
    finally:
        belief_commit.evidence_state = original_state  # type: ignore[assignment]
        rc.load_rows = original_rows  # type: ignore[assignment]
        rc.vector_ranks = original_vector  # type: ignore[assignment]
        _mode("off")
    return ok

def test_load_rows_tolerates_old_table_without_span_columns() -> bool:
    import sqlite3

    from sqlalchemy import create_engine, text as _ddl

    import core.bootstrap as bootstrap
    import db.write_guard as write_guard

    db_path = ROOT / ".test_tmp" / "recall_chain" / "old_shape.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "create table terminal_events (id integer primary key, event_id text, occurred_at text,"
            " sequence integer, event_type text, actor_person_id text, content text,"
            " chat_type text, chat_id text)"
        )
        conn.execute(
            "insert into terminal_events (event_id, occurred_at, event_type, actor_person_id,"
            " content, chat_type, chat_id) values ('e1','2026-10-01 10:00:00','message','wxid_x',"
            "'老库照样能召回','group','old@chatroom')"
        )
        conn.commit()
    finally:
        conn.close()
    engine = create_engine(f"sqlite:///{db_path}")
    original_engine = write_guard.read_only_engine
    original_path = bootstrap.get_meta_db_path
    original_names = rc.load_names
    rc._SPAN_COLUMNS.clear()
    write_guard.read_only_engine = lambda *args, **kwargs: engine  # type: ignore[assignment]
    bootstrap.get_meta_db_path = lambda: str(db_path)  # type: ignore[assignment]
    rc.load_names = lambda engine: {}  # type: ignore[assignment]
    try:
        rows = rc.load_rows("old@chatroom")
        ok = check("老形状库能读", len(rows) == 1 and rows[0]["content"] == "老库照样能召回")
        ok = check("老形状库没有 span 键", bool(rows) and "content_start" not in rows[0]) and ok
        with engine.connect() as conn2:
            conn2.execute(
                _ddl("alter table terminal_events add column content_start integer default -1 not null")
            )
            conn2.execute(
                _ddl("alter table terminal_events add column content_end integer default -1 not null")
            )
            conn2.commit()
        rc._SPAN_COLUMNS.clear()
        rows2 = rc.load_rows("old@chatroom")
        ok = check(
            "m061 之后能读 span（老行 -1）",
            bool(rows2) and rows2[0].get("content_start") == -1 and rows2[0].get("content_end") == -1,
        ) and ok
    finally:
        write_guard.read_only_engine = original_engine  # type: ignore[assignment]
        bootstrap.get_meta_db_path = original_path  # type: ignore[assignment]
        rc.load_names = original_names  # type: ignore[assignment]
        rc._SPAN_COLUMNS.clear()
        engine.dispose()
    return ok


def _episode(value: str | None) -> None:
    if value is None:
        os.environ.pop("YINYUE_FEATURE_RECALL_EPISODE", None)
    else:
        os.environ["YINYUE_FEATURE_RECALL_EPISODE"] = value


def test_episode_flag_registered_with_counts() -> bool:
    from core import features

    spec = features.FLAG_REGISTRY.get(features.RECALL_EPISODE)
    ok = spec is not None and spec.kind == "tri" and spec.default == features.MODE_OFF
    ok = ok and features.RECALL_EPISODE in features.TRI_STATE_FLAGS
    ok = ok and rc.recall_episode_mode() == "off"
    ok = ok and rc.episode_tracing() is False
    _episode("shadow")
    ok = ok and rc.recall_episode_mode() == "shadow"
    ok = ok and rc.episode_tracing() is True
    _episode(None)
    ok = ok and rc.episode_tracing() is False
    return check("episode flag registered (tri/off) and traces shadow", ok)


def test_episode_windows_overlap_and_cover() -> bool:
    rows = _rows(80)
    windows = rc.episode_windows(rows, size=rc.EPISODE_SIZE)
    ok = bool(windows)
    ok = ok and all(0 <= begin < end <= len(rows) for begin, end, _ in windows)
    ok = ok and all(end - begin <= rc.EPISODE_SIZE for begin, end, _ in windows)
    covered = set()
    for begin, end, _blob in windows:
        covered.update(range(begin, end))
    ok = ok and covered == set(range(len(rows)))
    ok = ok and all(len(blob) <= rc.EPISODE_CHARS for _b, _e, blob in windows)
    ok = ok and rc.episode_windows(_rows(1), size=rc.EPISODE_SIZE) == []
    return check("episode windows overlap and cover every row", ok)


def test_episode_ranks_use_window_cache_and_find_gold() -> bool:
    import numpy as np

    class _WinModel:
        batch_size = 8

        def encode(self, texts, **kwargs):
            out = []
            for text in texts:
                hit = 1.0 if "白果园" in str(text) else 0.0
                out.append([hit, 1.0, 0.0])
            return np.asarray(out, dtype="float32")

    rows = _rows(80)
    directory = _cache_dir("episode")
    with _cache_env(directory, "0"):
        first = rc.episode_ranks(rows, "白果园", cache_key="ep_unit", model=_WinModel())
        stats: dict = {}
        second = rc.episode_ranks(
            rows, "白果园", cache_key="ep_unit", model=_WinModel(), stats=stats
        )
    ok = 40 in first and 40 in second
    ok = ok and stats.get("episode_mode") == "exact"
    ok = ok and stats.get("episode_windows", 0) > 0
    ok = ok and stats.get("episode_known", 0) > 0
    ok = ok and stats.get("episode_source") in ("row_max", "blob")
    with _cache_env(_cache_dir("episode_cold"), "1"):
        cold = rc.episode_ranks(rows, "白果园", cache_key="ep_cold_%d" % os.getpid(), model=None)
    ok = ok and cold == []
    return check("episode ranks use the window cache and find the gold episode", ok)


def test_episode_prior_lifts_window_without_becoming_a_route() -> bool:
    """task-28b：情节级向量只做窗口聚合层先验，不再作为 route 灌行。"""
    import numpy as np

    class _WinModel:
        batch_size = 8

        def encode(self, texts, **kwargs):
            out = []
            for text in texts:
                hit = 1.0 if "白果园" in str(text) else 0.0
                out.append([hit, 1.0, 0.0])
            return np.asarray(out, dtype="float32")

    rows = _rows(80)
    directory = _cache_dir("ep_prior")
    with _cache_env(directory, "0"):
        _episode("off")
        try:
            off_stats: dict = {}
            off_hits = rc.chain_rank(
                rows, "白果园", cache_key="ep_prior", model=_WinModel(), stats=off_stats
            )
            _episode("on")
            os.environ[rc.EPISODE_PRIOR_ENV] = "6"
            on_stats: dict = {}
            on_hits = rc.chain_rank(
                rows, "白果园", cache_key="ep_prior", model=_WinModel(), stats=on_stats
            )
        finally:
            _episode(None)
            os.environ.pop(rc.EPISODE_PRIOR_ENV, None)
    ok = bool(off_hits) and bool(on_hits)
    ok = ok and "episode_window_top" not in off_stats
    top = on_stats.get("episode_window_top") or []
    ok = ok and bool(top) and int(top[0]["begin"]) <= 40 < int(top[0]["end"])
    ok = ok and "episode" not in (on_stats.get("routes") or {})
    ok = ok and "episode" not in (off_stats.get("routes") or {})
    ok = ok and any(hit.start <= 40 < hit.end for hit in on_hits)
    ok = ok and rc.episode_prior() == rc.EPISODE_PRIOR_DEFAULT == 0.0
    os.environ[rc.EPISODE_PRIOR_ENV] = "6"
    try:
        ok = ok and rc.episode_prior() == 6.0
    finally:
        os.environ.pop(rc.EPISODE_PRIOR_ENV, None)
    return check("episode prior lifts the gold window and stays out of route_ranks", ok)


def test_tie_break_is_opt_in_and_keeps_top1() -> bool:
    rows = _rows(20)
    fused = {3: (2.0, ("keyword",)), 7: (2.0, ("keyword",)), 9: (1.0, ("keyword",))}
    order = [3, 7, 9]
    default_off = rc.tie_break_enabled() is False
    os.environ[rc.TIE_BREAK_ENV] = "1"
    try:
        enabled = rc.tie_break_enabled() is True
        reordered = rc.tie_break_order(order, rows, "白果园", fused=fused)
    finally:
        os.environ.pop(rc.TIE_BREAK_ENV, None)
    ok = default_off and enabled
    ok = ok and reordered and reordered[0] == 3
    ok = ok and set(reordered) == {3, 7, 9}
    ok = ok and reordered[-1] == 9
    ok = ok and rc.tie_break_enabled() is False
    return check("tie-break is opt-in and never displaces top-1", ok)


def _prim(value: str | None, turn_weight: str | None = None) -> None:
    for name, item in (
        ("YINYUE_FEATURE_RECALL_PRIMITIVES", value),
        (rc.TURN_WEIGHT_ENV, turn_weight),
    ):
        if item is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = item


def test_prim_flag_registered_and_turn_weight_gates_it() -> bool:
    from core import features

    spec = features.FLAG_REGISTRY.get(features.RECALL_PRIMITIVES)
    ok = spec is not None and spec.kind == "tri" and spec.default == features.MODE_OFF
    ok = ok and features.RECALL_PRIMITIVES in features.TRI_STATE_FLAGS
    ok = ok and rc.recall_primitives_mode() == "off" and rc.primitives_tracing() is False
    ok = ok and rc.turn_tracing() is False
    _prim("shadow")
    ok = ok and rc.recall_primitives_mode() == "shadow"
    ok = ok and rc.turn_tracing() is True
    _prim("on", "0")
    ok = ok and rc.primitives_tracing() is True and rc.turn_tracing() is False
    _prim(None, None)
    ok = ok and rc.primitives_tracing() is False and rc.turn_tracing() is False
    return check("RECALL_PRIMITIVES is tri/off and turn weight gates the turn arm", ok)


def test_bm25_scores_are_tf_idf_length_normalised() -> bool:
    docs = [list("甲乙丙丁"), list("甲乙戊己庚辛壬癸"), list("子丑寅卯")]
    scores = rc.bm25_scores(docs, ["甲", "乙", "子"])
    ok = len(scores) == 3
    ok = ok and all(score > 0.0 for score in scores)  # 只在一篇出现的「子」照样参与（没有 DF 阈值）
    ok = ok and abs(scores[2] - max(scores)) < 1e-9
    ok = ok and scores[1] < scores[0]  # 同 tf、文档更长 -> 分更低
    ok = ok and rc.bm25_scores([], ["甲"]) == []
    ok = ok and rc.bm25_scores(docs, []) == [0.0, 0.0, 0.0]
    return check("bm25 scores use tf/idf/length and never DF-filter the query", ok)


def test_prim_lexical_routes_rank_the_literal_hit() -> bool:
    rows = _rows(30)
    rows[7]["content"] = "那老师在课件里讲过白果园的事"
    rows[19]["content"] = "白果园门口那家店"
    stats: dict = {}
    ranks = rc.prim_lexical_ranks(rows, "白果园是哪门课", stats=stats)
    ok = "lex3" in ranks and "lexw" in ranks
    ok = ok and 7 in ranks["lex3"] and 7 in ranks["lexw"]
    ok = ok and 7 in ranks["lex3"][:2] and 19 in ranks["lex3"][:3]
    ok = ok and stats.get("prim_lex3", {}).get("hits", 0) > 0
    ok = ok and stats.get("prim_lex3", {}).get("top")
    ok = ok and rc.prim_lexical_ranks(rows, "") == {}
    return check("prim lexical routes rank the literal hit first (lex3 + lexw)", ok)


def test_chain_rank_off_keeps_old_routes_on_replaces_them() -> bool:
    rows = _rows(30)
    rows[11]["content"] = "白果园那个老师"
    query = "白果园是哪门课"
    _prim(None, "0")
    with _cache_env(_cache_dir("prim_off"), "1"):
        stats_off: dict = {}
        rc.chain_rank(rows, query, cache_key="prim_off", use_vector=False, stats=stats_off)
    _prim("on", "0")
    with _cache_env(_cache_dir("prim_on"), "1"):
        stats_on: dict = {}
        rc.chain_rank(rows, query, cache_key="prim_on", use_vector=False, stats=stats_on)
    _prim(None, None)
    ok = set(stats_off.get("route_sizes") or {}) == {"keyword", "fts"}
    ok = ok and stats_off.get("primitives") is None
    ok = ok and set(stats_on.get("route_sizes") or {}) == {"lex3", "lexw"}
    ok = ok and stats_on.get("primitives") == "on"
    return check("primitives off keeps keyword+fts, on replaces them with lex3+lexw", ok)


def test_turn_segments_follow_speakers_and_reply_edges() -> bool:
    rows: list[dict] = []
    for actor, count in (("a", 3), ("b", 2), ("a", 1), ("c", 4), ("a", 2)):
        for _ in range(count):
            rows.append(
                {"event_id": f"t{len(rows)}", "actor": actor, "content": f"第{len(rows)}句"}
            )
    segments = rc.turn_segments(rows)
    covered: list[int] = []
    ok = True
    for begin, end, blob in segments:
        covered.extend(range(begin, end))
        ok = ok and bool(blob) and len(blob) <= rc.TURN_CHARS
    ok = ok and covered == list(range(len(rows)))
    ok = ok and all(2 <= end - begin <= rc.TURN_MAX for begin, end, _ in segments)
    ok = ok and [span[:2] for span in segments] == [(0, 3), (3, 6), (6, 10), (10, 12)]

    same_actor = [
        {"event_id": f"s{index}", "actor": "a", "content": f"第{index}句"} for index in range(6)
    ]
    same_actor[3]["parent"] = "s1"  # 一条 reply 边（在回第 1 句）也要切段
    reply_spans = [span[:2] for span in rc.turn_segments(same_actor)]
    ok = ok and reply_spans == [(0, 3), (3, 6)]

    long_run = [
        {"event_id": f"l{index}", "actor": "a", "content": f"第{index}句"} for index in range(8)
    ]
    long_spans = [span[:2] for span in rc.turn_segments(long_run)]
    ok = ok and long_spans == [(0, rc.TURN_MAX), (rc.TURN_MAX, 8)]
    ok = ok and rc.turn_segments(_rows(1)) == []
    return check("turn segments split on speaker change, reply edges and the 6-row cap", ok)


def test_turn_ranks_expand_the_best_segment() -> bool:
    import numpy as np

    class _TurnModel:
        batch_size = 8

        def encode(self, texts, **kwargs):
            return np.asarray(
                [
                    [1.0 if "白果园" in str(text) else 0.0, 1.0, 0.0]
                    for text in texts
                ],
                dtype="float32",
            )

    rows = []
    for index in range(20):
        content = "白果园那个老师教过咱们" if index == 12 else f"第{index}句闲聊"
        rows.append({"event_id": f"r{index}", "actor": "a" if index % 2 == 0 else "b", "content": content})
    _prim("on", "1")
    with _cache_env(_cache_dir("turn"), "0"):
        stats: dict = {}
        order = rc.turn_ranks(
            rows, "白果园是哪门课", cache_key="turn_unit", model=_TurnModel(), stats=stats
        )
    _prim(None, None)
    ok = stats.get("turn_segments", 0) > 0
    ok = ok and stats.get("turn_cache_mode") == "computed"
    ok = ok and 2.0 <= float(stats.get("turn_rows_mean") or 0) <= float(rc.TURN_MAX)
    ok = ok and 12 in order
    ok = ok and 12 in order[: rc.TURN_MAX]
    ok = ok and order[0] in range(12, 18)
    ok = ok and stats.get("turn_top")
    return check("turn_ranks expands the best segment and reports segment stats", ok)


def test_turn_route_only_when_weight_positive() -> bool:
    rows = _rows(30)
    rows[11]["content"] = "白果园那个老师"
    query = "白果园是哪门课"
    _prim("on", "0")
    with _cache_env(_cache_dir("turn_off"), "1"):
        stats_off: dict = {}
        rc.chain_rank(rows, query, cache_key="turn_off", use_vector=False, stats=stats_off)
    _prim(None, None)
    ok = "turn" not in (stats_off.get("route_sizes") or {})
    ok = ok and stats_off.get("turn_segments") is None
    return check("turn route stays out of the chain while its weight is 0", ok)


def test_needs_memory_gate_is_retired() -> bool:
    """负锁：按需记忆注入门整族退役（2026-10-23），旧大脑 conversational.py
    已于退役清理中整文件删除 —— 现在连 import 都该失败。"""
    try:
        import agents.conversational as conv
    except ModuleNotFoundError:
        conv = None
    ok = check("旧大脑 conversational.py 已删", conv is None)
    if conv is not None:
        ok = check("_needs_memory_block 已退役", not hasattr(conv, "_needs_memory_block")) and ok
        ok = check("_needs_memory_limits 已退役", not hasattr(conv, "_needs_memory_limits")) and ok
    for flag in ("NEEDS_MEMORY_INJECT", "RECALL_IN_WINDOW", "MEMORY_TOOL_VISIBILITY", "MEMORY_TOOL_FALLBACK"):
        ok = check(f"{flag} 已退役", not hasattr(features, flag)) and ok
    return ok


def main() -> bool:
    names = sorted(name for name in globals() if name.startswith("test_"))
    ok = True
    for name in names:
        fn = globals()[name]
        try:
            result = bool(fn())
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
            result = False
        print(("ok   " if result else "FAIL ") + name)
        ok = ok and result
    print(f"recall chain: {len(names)} tests, {'all passed' if ok else 'FAILED'}")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
