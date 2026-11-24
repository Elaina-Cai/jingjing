"""重启恢复不允许把陈旧回读行重新拉起、再抄送主人一次（2026-11-16）。"""
import time

from loop import desk


def _row(qid, *, age_s, attempt_no=1):
    return {
        "queue_id": qid, "attempt_no": attempt_no, "platform": "wechat",
        "account_id": "a", "chat_id": "room", "text": "same", "receipt_id": 9,
        "baseline": 1, "source_token": "[]", "queue_created_ts": time.time() - age_s,
        "queue_ack": "pending",
    }


def test_stale_observation_is_settled_once_without_owner_copy(monkeypatch):
    calls = {"resumed": [], "unknown": [], "copied": []}
    monkeypatch.setattr(desk.W, "pending_outbound_observations", lambda: [_row(347, age_s=10 ** 6)])
    monkeypatch.setattr(desk.W, "mark_confirmation_unknown",
                        lambda qid, why, *, attempt_no, connection=None: calls["unknown"].append((qid, why)) or 1)
    monkeypatch.setattr(desk, "_confirm_queue_later", lambda *a, **k: calls["resumed"].append(a))
    import loop.owner_copy as owner_copy
    monkeypatch.setattr(owner_copy, "note", lambda *a, **k: calls["copied"].append((a, k)))
    monkeypatch.setattr(desk, "_log", lambda *a, **k: None)

    out = desk.recover_observations()

    assert out["resumed"] == 0 and out["expired"] == 1
    assert calls["resumed"] == [] and calls["copied"] == []
    assert calls["unknown"] == [(347, "重启前发送，已超过回读窗口（585 秒），不再追认")]


def test_fresh_observation_still_resumes(monkeypatch, tmp_path):
    calls = []
    account = tmp_path / "data" / "platforms" / "wechat" / "a" / "account.db"
    account.parent.mkdir(parents=True)
    account.write_bytes(b"")
    monkeypatch.setattr(desk, "ROOT", str(tmp_path))
    monkeypatch.setattr(desk.W, "pending_outbound_observations", lambda: [_row(400, age_s=5)])
    monkeypatch.setattr(desk.W, "mark_confirmation_unknown", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not settle")))
    monkeypatch.setattr(desk, "_confirm_queue_later", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(desk, "_log", lambda *a, **k: None)

    out = desk.recover_observations()

    assert out == {"resumed": 1, "unavailable": 0, "expired": 0}
    assert len(calls) == 1 and calls[0][0] == 400
