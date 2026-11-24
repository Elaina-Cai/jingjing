"""面板 UX 的三道护栏（2026-10-31 晚）。

起因：四个主机按钮点了会重复拉起、卡编辑器关掉就丢、请求没有超时。
这些都不是"看着不对"，是会让新人做错事的坑，所以钉成源码级断言：
改面板时若把护栏拆了，这里会红。
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = os.path.join(ROOT, "console", "panel.html")


def _src() -> str:
    with open(PANEL, encoding="utf-8") as fh:
        return fh.read()


def _fn(src: str, header: str) -> str:
    """截出 `header` 开头的那个函数的源码块（按大括号配平，够用）。"""
    i = src.index(header)
    depth = 0
    started = False
    for j in range(i, len(src)):
        ch = src[j]
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
    raise AssertionError("找不到函数结尾：" + header)


def test_law_fetch_has_timeout_and_friendly_errors():
    fn = _fn(_src(), "async function lawFetch(path, body, timeoutMs)")
    assert "new AbortController()" in fn, "请求必须可取消"
    assert "setTimeout" in fn and "abort()" in fn, "必须有超时"
    assert "clearTimeout" in fn, "必须有 finally 清理，否则定时器泄漏"
    assert 'indexOf("application/json")' in fn, "非 JSON 响应要翻成人话，不能只 await r.json()"
    assert "没回应" in fn and "重开" in fn, "超时文案要告诉人下一步怎么做"


def test_card_editor_guards_unsaved_edits():
    src = _src()
    close = _fn(src, "function pcClose(){")
    assert "PC_DIRTY" in close and "confirm(" in close, "改过没保存就关，必须问一句"
    assert "PC_DIRTY = false;" in close, "确认丢弃后要清标记"
    open_ = _fn(src, "async function pcOpen(name){")
    assert "PC_DIRTY = false;" in open_, "打开时必须是干净状态"
    assert ".focus()" in open_, "打开就该能直接打字"
    assert "PC_DIRTY = true;" in src, "输入时必须标脏"


def test_host_buttons_lock_during_long_ops():
    src = _src()
    assert 'const HOST_BTN_IDS = ["#host-start", "#host-restart", "#host-stop", "#host-restart-engine"]' in src
    busy = _fn(src, "function hostBusy(on, hint){")
    assert "disabled" in busy, "长操作期间四个按钮都要禁用"
    do = _fn(src, "async function hostDo(cmd, ask, waitMs){")
    assert "hostBusy(true" in do and "hostBusy(false)" in do, "进出都要切"
    assert "别重复点" in do, "要写清为什么不能重复点"
    assert 'hostDo("svc_start", "", 120000)' in src, "启动要等 25 秒起步，超时不能是默认 15 秒"
    assert "240000" in src, "重启引擎要等到 150 秒的服务端上限之外"


def test_stop_confirm_says_shell_go_offline():
    src = _src()
    m = re.search(r'\$\("#host-stop"\)\.onclick\s*=\s*\(\)=>hostDo\("svc_stop",\s*"([^"]*)"', src)
    assert m, "停止按钮的确认语要能被读到"
    assert "离线" in m.group(1), "停止＝她会离线，必须在确认语里说"


def test_topline_exists_and_is_wired():
    src = _src()
    assert 'id="topline"' in src
    for wid in ("tl-run", "tl-say", "tl-queue", "tl-world"):
        assert 'id="%s"' % wid in src, "摘要条四段都要有位置"
    assert "function renderTopline()" in src
    render_fn = _fn(src, "function render(){")
    assert "renderTopline();" in render_fn, "必须由 render 驱动，否则永远不刷新"


def test_no_dev_jargon_left_in_panel():
    src = _src()
    for bad in ("连不上执行器", "哨兵", "console/collector.py 没起", "端口 39871 不通"):
        assert bad not in src, "面板里不该出现开发者术语：" + bad


def test_no_window_prompt_left():
    """原生 prompt() 风格突兀、没法校验、Esc 行为不一致，一律换成面板内弹窗。"""
    src = _src()
    assert "prompt(" not in src, "面板里不该再有 window.prompt"
    assert "function askText(opt)" in src, "要有自建输入弹窗"


def test_card_name_validation_is_testable():
    src = _src()
    assert "function cardNameProblem(v, have)" in src, "校验要能单独被单测"
    fn = _fn(src, "function cardNameProblem(v, have){")
    for key in ("名字不能空着", "最多 20 个字", "已经有一张叫"):
        assert key in fn, "缺一类校验：" + key


def test_audit_list_can_expand_and_says_its_cap():
    """原来只画 12 条、也不说为什么 —— 第 13 次改动就再也点不到回滚。"""
    src = _src()
    assert "const LAW_AUDIT_PEEK = 8;" in src
    assert "function lawAuditToggle()" in src, "要有展开/收起"
    render = _fn(src, "function renderLawAudit(){")
    assert "audit_cap" in render, "要说明读到多少条就到底了"
    assert "展开全部" in render, "要能给出口"


def test_write_offers_one_click_undo():
    src = _src()
    assert 'id="law-undo"' in src, "写完当场要有撤销入口"
    assert "async function lawUndoLast()" in src
    commit = _fn(src, "async function lawCommit(){")
    assert "ids.push(res.audit_id)" in commit, "写入要记住这次产生的审计 id"
    assert "LAW_LAST_AUDIT = ids" in commit, "撤销要靠这批 id"
    undo = _fn(src, "async function lawUndoLast(){")
    assert "rollback" in undo and "reverse()" in undo, "多次写入要倒序逐条回滚"


def test_panel_has_basic_a11y_markers():
    src = _src()
    assert 'role="navigation"' in src
    assert 'role="dialog"' in src and 'aria-modal="true"' in src
    assert src.count('aria-live="polite"') >= 4, "状态行都要能被读屏播报"
    assert 'if(e.key !== "Tab") return;' in src, "弹窗里 Tab 要留在弹窗内"


def test_audit_state_limit_raised_in_backend():
    """前端只能回滚看得见的行，所以后端一次要多给一些。"""
    with open(os.path.join(ROOT, "console", "executor.py"), encoding="utf-8") as fh:
        ex = fh.read()
    assert "AUDIT_STATE_LIMIT = 80" in ex
    assert "audit_rows(AUDIT_STATE_LIMIT)" in ex, "state() 必须用这个上限"
    assert '"audit_cap": AUDIT_STATE_LIMIT' in ex, "要告诉前端读到多少条就到底"


def test_timestamps_are_humanised():
    # 2026-11-01：面板上不许出现 2026-10-31 21:48:54.000000 这种数据库原始值。
    src = _src()
    assert "function humanTs(ts)" in src, "要有统一的转人话函数"
    assert "esc(humanTs(w.max_at))" in src and "esc(humanTs(dec.newest))" in src
    with open(os.path.join(ROOT, "console", "collector.py"), encoding="utf-8") as fh:
        col = fh.read()
    assert "没新消息" in col and 'time.strftime("%Y")' in col, "数据源那一行也要只到分钟"


def test_room_survives_refresh():
    # 刷新/重开窗口要回到你刚才在的那一页。
    src = _src()
    assert 'localStorage.setItem("panel.room"' in src
    assert 'localStorage.getItem("panel.room")' in src
    assert "hashchange" in src, "地址栏改 hash 也要跟着切"


def test_logs_page_has_level_filter_and_pause():
    # 志页：能只看 warning/error，能暂停跟随（不然边刷新边被拉到底）。
    src = _src()
    assert 'id="lg-level"' in src and 'id="lg-pause"' in src
    draw = _fn(src, "function lgDraw(){")
    assert "_lvOk" in draw, "级别过滤要真的参与筛选"
    assert "暂停跟随" in src


def test_sticker_grid_is_paginated():
    # 887 张缩略图不能一次全渲染（首屏几百 KB 的 data URI）。
    src = _src()
    assert "STK_PER=60" in src
    assert 'id="stk-page"' in src and "function stkPage(" in src
    fn = _fn(src, "function renderStickers(){")
    assert "_stkRows()" in fn and "pageRows" in fn, "渲染必须走过滤+分页"


def test_diagnose_dump_button_and_backend():
    # 出问题时能一键把现场打包成文件，而不是让人截图。
    src = _src()
    assert 'id="check-dump"' in src and "diagnose_dump" in src
    with open(os.path.join(ROOT, "console", "executor.py"), encoding="utf-8") as fh:
        ex = fh.read()
    assert "def diagnose_dump(" in ex
    assert 'if cmd == "diagnose_dump":' in ex
    for sec in ("processes", "checkup", "queue", "log_tail", "heartbeat"):
        assert sec in ex, "诊断包要含 " + sec


def test_logs_room_has_a_backend_source():
    # /state.logs 曾经压根没人写，于是志页永远是"还没有日志"。
    with open(os.path.join(ROOT, "console", "collector.py"), encoding="utf-8") as fh:
        col = fh.read()
    assert "def _logs_tail(" in col
    assert '"logs": _logs_tail()' in col, "build_state 必须把 logs 挂进去"
    assert 'rec.get("event")' in col, "日志是 structlog 的 JSON 行，要按 JSON 解析"


def test_sticker_pick_function_exists():
    # 点已登记的图曾经调一个不存在的函数 —— 于是"点了没反应"。
    src = _src()
    assert "function stickerPick(md5){" in src
    assert "stickerPick(md5); return;" in _fn(src, "function stickerClick(md5){")


def test_world_fallback_defines_is_sys():
    """2026-11-01：`_world("")` 是"一个账号库都找不到"时的兜底路径，却用了没定义的 `_is_sys`。

    平时 `build_state` 走 `_world_multi`（那里有定义），所以这个 NameError 只会在
    最需要它的那一刻炸：库读不到 → 世界视图 NameError → 被外层 except 吞成
    一句"读不出来"。判据两处必须同一条，不许再各写一遍。
    """
    with open(os.path.join(ROOT, "console", "collector.py"), encoding="utf-8") as fh:
        col = fh.read()
    expr = '_is_sys = bool("<sysmsg" in _raw or _raw.lstrip().startswith("<?xml"))'
    assert col.count(expr) == 2, "两个世界视图要共用同一条系统消息判据"
    i = col.index("def _world(db_path")
    body = col[i:col.index("def _world_multi(")]
    assert body.index(expr) < body.index("if _is_sys:"), "_world 里要先算再用"
    assert body.index(expr) < body.index('"who": ("系统" if _is_sys')


def main() -> bool:
    """2026-11-01 补：这个文件以前只有 def test_、没有运行入口（19 条断言一条都没跑，
    scripts/audit_vacuous_tests.py --run 判「空转」）。现在实跑每一个用例。"""
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
