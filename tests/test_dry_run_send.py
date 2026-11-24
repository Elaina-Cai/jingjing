# -*- coding: utf-8 -*-
"""发送 dry-run 开关（YINYUE_FEATURE_DRY_RUN_SEND）检查：走完整链路但不真发。"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV = "YINYUE_FEATURE_DRY_RUN_SEND"


def _isolated_gateway():
    import core.bootstrap as bootstrap
    from sqlalchemy import create_engine

    from core.speech.outbound import OutboundGateway
    from core.speech.store import SpeechStore
    from db.meta_models import MetaBase

    bootstrap.get_account_db_manager = lambda: None
    engine = create_engine("sqlite+pysqlite:///:memory:")
    MetaBase.metadata.create_all(engine)
    return OutboundGateway(SpeechStore(engine)), engine


def _fake():
    from core.speech.domain import (
        AudienceKind, ConversationTarget, ResolvedPersona, SpeakingPolicy,
        SpeechSnapshot, TurnContext,
    )
    try:
        from core.speech.domain import OutboundDraft
    except Exception:
        from core.speech.outbound import OutboundDraft

    target = ConversationTarget(platform="wechat", account_id="wxid_bot",
                                chat_id="c1", chat_type="group")
    turn = TurnContext(request_id="req-dry", target=target,
                       audience=AudienceKind.GROUP_MENTIONED, is_mentioned=True,
                       trigger_reason="mention", msg_type="text")
    persona = ResolvedPersona(persona_id=1, version=1, key="yinyue", name="鲸鲸")
    policy = SpeakingPolicy(revision="rev-1", target=target,
                            audience=AudienceKind.GROUP_MENTIONED, persona=persona)
    snapshot = SpeechSnapshot(turn=turn, audience=AudienceKind.GROUP_MENTIONED,
                              persona=persona, policy=policy)
    draft = OutboundDraft(request_id="req-dry", kind="text", target=target,
                          content="hello", who="c1", extra={})
    return snapshot, draft


def _fake_provider():
    sent = []

    async def _send(target, content, filepath=None):
        sent.append(content)
        return SimpleNamespace(ok=True, msg_id="m1", error="")

    return SimpleNamespace(send=_send), sent


def _audit_statuses(engine):
    from sqlalchemy import inspect, text
    tables = inspect(engine).get_table_names()
    name = "outbound_audit" if "outbound_audit" in tables else next(
        (t for t in tables if "outbound" in t and "audit" in t), "")
    if not name:
        return []
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text(f"select status from {name} order by id"))]


def test_off_sends_normally():
    old = os.environ.pop(ENV, None)
    try:
        gateway, engine = _isolated_gateway()
        provider, sent = _fake_provider()
        gateway._resolve_provider = lambda platform, account_id="": provider
        snapshot, draft = _fake()
        result = asyncio.run(gateway.send_text(draft, snapshot, None))
        assert result.ok is True, result
        assert sent == ["hello"], sent
        assert _audit_statuses(engine) == ["sent"], _audit_statuses(engine)
    finally:
        if old is not None:
            os.environ[ENV] = old


def test_on_skips_transport_and_records_dry_run():
    os.environ[ENV] = "1"
    try:
        gateway, engine = _isolated_gateway()
        provider, sent = _fake_provider()
        gateway._resolve_provider = lambda platform, account_id="": provider
        snapshot, draft = _fake()
        result = asyncio.run(gateway.send_text(draft, snapshot, None))
        assert result.ok is True, result
        assert sent == [], sent
        assert _audit_statuses(engine) == ["dry_run"], _audit_statuses(engine)
        assert "dry-run" in (result.output or ""), result.output
    finally:
        os.environ.pop(ENV, None)


def test_static_wiring():
    feat = (ROOT / "core/features.py").read_text(encoding="utf-8")
    assert 'DRY_RUN_SEND = "dry_run_send"' in feat
    env = (ROOT / "tests/test_env_flag_isolation.py").read_text(encoding="utf-8")
    assert "YINYUE_FEATURE_DRY_RUN_SEND" in env


def test_low_level_guards_present():
    checks = {
        "core/features.py": "def dry_run_send(chat_id",
        "core/execution/wx_sender.py": "_features.dry_run_send(str(",
        "core/execution/wx_sticker_sender.py": "_features.dry_run_send(str(",
        "core/execution/qq_provider.py": "_features.dry_run_send(str(",
        "core/speech/outbound.py": "_dry_run_send_enabled(str(",
    }
    for path, needle in checks.items():
        body = (ROOT / path).read_text(encoding="utf-8")
        assert needle in body, f"{path} 缺少 dry-run 守卫：{needle}"


TESTS = [test_off_sends_normally, test_on_skips_transport_and_records_dry_run,
         test_static_wiring, test_low_level_guards_present]


def main() -> bool:
    ok = True
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
            ok = False
        else:
            print(f"PASS {test.__name__}")
    if ok:
        print(f"dry-run send checks: {len(TESTS)}/{len(TESTS)} passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
