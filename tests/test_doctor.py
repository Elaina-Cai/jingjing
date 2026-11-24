"""体检（scripts/doctor.py）回归测试。

只用标准库；临时目录放项目内 ``.tmp_test``（本沙箱的 tempfile 目录不可写）。
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import doctor as doc  # noqa: E402

TMP = ROOT / ".tmp_test" / f"doctor_{os.getpid()}"


def _fresh(name: str) -> Path:
    d = TMP / name
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _outbox_db(root: Path, platform: str, account: str, *,
               lease_column: bool = True) -> Path:
    """造一个账号库（含 event_outbox）；lease_column=False 模拟 v50 旧库。"""
    d = root / "data" / "platforms" / platform / account
    d.mkdir(parents=True, exist_ok=True)
    db = d / "account.db"
    cols = ("id integer primary key, chat_id text, status text, attempts integer, "
            "created_at text, delivered_at text, last_error text")
    if lease_column:
        cols += ", claimed_at text, claimed_by text, lease_until text"
    conn = sqlite3.connect(str(db))
    conn.execute(f"create table event_outbox({cols})")
    conn.commit()
    conn.close()
    return db


def _insert_outbox(db: Path, rows: list[tuple[str, str, str]]) -> None:
    """rows = [(status, created_at, lease_until)]。"""
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "insert into event_outbox(chat_id, status, attempts, created_at, "
        "claimed_by, lease_until) values('c', ?, 0, ?, 'dead', ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_python_ok() -> None:
    result = doc.check_python()
    assert result.level == doc.OK, result


def test_dependency_detection() -> None:
    result = doc.check_dependencies({"json": "json", "no_such_module_xyz": "x"})
    assert result.level == doc.ERROR
    assert "no_such_module_xyz" in result.title
    assert doc.check_dependencies({"json": "json"}).level == doc.OK


def test_env_checks() -> None:
    d = _fresh("env")
    p = d / ".env"
    p.write_text(
        "PROVIDER=deepseek\nDEEPSEEK_API_KEY=\nWECHAT_SEND_ENABLED=false\nA=1\nA=2\n",
        encoding="utf-8",
    )
    env_check = doc.check_env_file(p)
    assert env_check.level == doc.WARN and "A" in env_check.title
    assert doc.check_llm_key(p).level == doc.ERROR
    assert doc.check_send_switch(p).level == doc.WARN
    assert doc.check_timezone(p).level == doc.WARN
    assert doc.check_wechat_env(p).level == doc.WARN

    good = d / ".env.good"
    good.write_text(
        "PROVIDER=deepseek\nDEEPSEEK_API_KEY=sk-x\nWECHAT_SEND_ENABLED=true\n"
        "TEMPORAL_TIMEZONE=Asia/Shanghai\nWECHAT_KEY=ab\nWECHAT_YINYUE_WXID=wxid_bot\n"
        "WECHAT_OWNER_WXIDS=wxid_owner\nWECHAT_DB_PATH=C:/x\n",
        encoding="utf-8",
    )
    assert doc.check_env_file(good).level == doc.OK
    assert doc.check_llm_key(good).level == doc.OK
    assert doc.check_send_switch(good).level == doc.OK
    assert doc.check_timezone(good).level == doc.OK
    assert doc.check_wechat_env(good).level == doc.OK


def test_env_missing() -> None:
    d = _fresh("env_missing")
    missing = d / ".env"
    assert doc.check_env_file(missing).level == doc.ERROR
    assert doc.check_llm_key(missing).level == doc.ERROR


def test_autostart_check() -> None:
    d = _fresh("bat")
    hard = d / "hard.bat"
    hard.write_text('@echo off\ncd /d "C:\\old\\yinyue"\n', encoding="utf-8")
    result = doc.check_autostart(hard)
    assert result.level == doc.WARN and "写死" in result.title
    good = d / "good.bat"
    good.write_text('@echo off\ncd /d "%~dp0"\n', encoding="utf-8")
    assert doc.check_autostart(good).level == doc.OK


def test_ollama_unreachable() -> None:
    result = doc.check_ollama(url="http://127.0.0.1:9/api/tags", timeout=0.3)
    assert result.level == doc.WARN and "Ollama" in result.title


def test_service_closed_port() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    result = doc.check_service(port=port, timeout=0.3)
    assert result.level == doc.WARN


def test_wechat_probe() -> None:
    assert doc.check_wechat_running(probe=lambda: True).level == doc.OK
    assert doc.check_wechat_running(probe=lambda: False).level == doc.WARN
    assert doc.check_wechat_running(probe=lambda: None).level == doc.WARN


def test_data_check() -> None:
    d = _fresh("data")
    assert doc.check_data(d, db_path=d / "nope.db").level == doc.WARN

    db = d / "meta.db"
    conn = sqlite3.connect(str(db))
    conn.execute("create table terminal_events(id integer)")
    conn.execute("create table alembic_version(version_num text)")
    conn.execute("insert into alembic_version values('42')")
    conn.execute("insert into terminal_events(id) values(1)")
    conn.commit()
    conn.close()
    ok = doc.check_data(d, db_path=db)
    assert ok.level == doc.OK and "terminal_events=1" in ok.title and "42" in ok.title

    empty = d / "empty.db"
    sqlite3.connect(str(empty)).close()
    assert doc.check_data(d, db_path=empty).level == doc.WARN


def test_outbox_check() -> None:
    """REC-3：outbox 三档阈值 + 空库/缺表/v50 旧库（缺 lease 列）不炸。"""
    now = datetime.now()
    age_30m = (now - timedelta(minutes=30)).isoformat(" ")

    # ① 还没有 platforms 目录：不算问题
    assert doc.check_outbox(_fresh("outbox_empty")).level == doc.OK

    # ② 账号库存在但没建 outbox 表（迁移 47 未落）：跳过，不炸
    no_table = _fresh("outbox_no_table")
    (no_table / "data" / "platforms" / "wechat" / "wxid_a").mkdir(parents=True)
    sqlite3.connect(
        str(no_table / "data" / "platforms" / "wechat" / "wxid_a" / "account.db")
    ).close()
    assert doc.check_outbox(no_table).level == doc.OK

    # ③ v50 旧库（没有 lease_until 列）：照常计数，stale 记 0
    old = _fresh("outbox_old_lib")
    old_db = _outbox_db(old, "wechat", "wxid_old", lease_column=False)
    conn = sqlite3.connect(str(old_db))
    conn.execute(
        "insert into event_outbox(chat_id, status, attempts, created_at) "
        "values('c', 'pending', 0, ?)", (age_30m,),
    )
    conn.commit()
    conn.close()
    check = doc.check_outbox(old)
    assert check.level == doc.WARN, check
    assert "outbox pending=1" in check.title and "stale=0" in check.title, check.title

    # ④ failed>0 → WARN
    failed = _fresh("outbox_failed")
    _insert_outbox(_outbox_db(failed, "wechat", "wxid_f"), [("failed", now.isoformat(" "), None)])
    check = doc.check_outbox(failed)
    assert check.level == doc.WARN and "failed=1" in check.title, check
    assert doc.exit_code([check]) == 0

    # ⑤ stale>0（租约过期）→ ERROR，退出码 1
    stale = _fresh("outbox_stale")
    _insert_outbox(
        _outbox_db(stale, "wechat", "wxid_s"),
        [("inflight", (now - timedelta(minutes=10)).isoformat(" "),
          (now - timedelta(minutes=5)).isoformat(" "))],
    )
    check = doc.check_outbox(stale)
    assert check.level == doc.ERROR and "stale=1" in check.title, check
    assert doc.exit_code([check]) == 1

    # ⑥ pending>500（积压）→ ERROR
    backlog = _fresh("outbox_backlog")
    _insert_outbox(
        _outbox_db(backlog, "wechat", "wxid_b"),
        [("pending", now.isoformat(" "), None)] * 501,
    )
    check = doc.check_outbox(backlog)
    assert check.level == doc.ERROR and "pending=501" in check.title, check

    # ⑦ 全部 delivered → OK（同时多账号库聚合）
    healthy = _fresh("outbox_ok")
    _insert_outbox(
        _outbox_db(healthy, "wechat", "wxid_ok"),
        [("delivered", now.isoformat(" "), None)],
    )
    check = doc.check_outbox(healthy)
    assert check.level == doc.OK and "outbox pending=0" in check.title, check
    assert "inflight=0 failed=0 stale=0" in check.title, check.title


def test_work_ledger_check() -> None:
    """P1 残余风险①：META 里 ``work_items`` / ``work_item_runs`` 的存在性。

    缺表 → ``[!!]`` + 修复提示（Migration 52 没落地 / 纯迁移链建的库）；
    两态都要稳：元数据库还没有时不重复报 check_data 的那一档。
    """
    # ① 元数据库还没建：跳过（check_data 已经报过），不算问题
    empty = _fresh("ledger_no_db")
    assert doc.check_work_ledger(empty).level == doc.OK

    # ② 有元数据库但没这两张表（52 未应用）→ WARN + 修复提示
    d = _fresh("ledger_missing")
    db = d / "meta.db"
    conn = sqlite3.connect(str(db))
    conn.execute("create table terminal_events(id integer)")
    conn.commit()
    conn.close()
    check = doc.check_work_ledger(d, db_path=db)
    assert check.level == doc.WARN, check
    assert "work_items" in check.title and "work_item_runs" in check.title, check.title
    assert "52" in check.fix and doc.exit_code([check]) == 0

    # ③ 只缺一张也算缺
    conn = sqlite3.connect(str(db))
    conn.execute("create table work_items(work_item_id text)")
    conn.commit()
    conn.close()
    check = doc.check_work_ledger(d, db_path=db)
    assert check.level == doc.WARN and "work_item_runs" in check.title, check.title

    # ④ 两张都在 → OK
    conn = sqlite3.connect(str(db))
    conn.execute("create table work_item_runs(run_id text)")
    conn.commit()
    conn.close()
    check = doc.check_work_ledger(d, db_path=db)
    assert check.level == doc.OK and "work_items" in check.title, check

    # ⑤ 写好的检查必须挂进 run_checks，否则等于没查（静态核对同一行）
    assert "check_work_ledger(root)" in (
        ROOT / "scripts" / "doctor.py"
    ).read_text(encoding="utf-8")


def test_render_and_exit_code() -> None:
    checks = [
        doc.Check(doc.OK, "ok 项"),
        doc.Check(doc.WARN, "warn 项", fix="修法 W"),
        doc.Check(doc.ERROR, "error 项", fix="修法 E"),
    ]
    text = doc.render(checks)
    assert "[OK]" in text and "[!!]" in text and "[XX]" in text
    assert "修法 E" in text and "2 项" not in text  # 汇总只数数
    assert doc.exit_code(checks) == 1
    assert doc.exit_code([doc.Check(doc.OK, "x")]) == 0
    assert "全部正常" in doc.render([doc.Check(doc.OK, "x")])


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    try:
        for test in tests:
            try:
                test()
                print(f"  ok   {test.__name__}")
            except AssertionError as exc:
                failed += 1
                print(f"  FAIL {test.__name__}: {exc!r}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"doctor checks: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
