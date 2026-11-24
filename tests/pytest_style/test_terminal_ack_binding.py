"""旧旁注补账没有操作绑定时不能覆盖任何回执。"""
from sqlalchemy import create_engine, text

from core import features
from core.terminal import delivery_receipts as receipts


def test_unbound_ack_preserves_all_same_target_receipts(monkeypatch, caplog):
    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr(receipts, "_engine", engine)
    monkeypatch.setattr(receipts, "_mode", lambda: features.MODE_ON)
    with engine.begin() as con:
        for sql in receipts.ddl_statements():
            con.exec_driver_sql(sql)
    for turn in ("first", "second"):
        assert receipts.record_receipt(dict(status="sent", kind="text", target="same", ack="pending"), turn_id=turn, chat_id="room")
    with engine.connect() as con:
        before = con.execute(text("select * from delivery_receipts order by receipt_id")).fetchall()
    with caplog.at_level("INFO", logger=receipts.__name__):
        assert receipts.update_pending_ack("same", ack="platform_id", platform_msg_id="7", client_msg_id="8") == 0
    with engine.connect() as con:
        assert con.execute(text("select * from delivery_receipts order by receipt_id")).fetchall() == before
    assert "delivery_receipt_ack_binding_missing" in caplog.text
    engine.dispose()


def test_unbound_ack_does_not_even_open_database(monkeypatch):
    monkeypatch.setattr(receipts, "_mode", lambda: features.MODE_ON)
    def forbidden():
        raise AssertionError("unbound callback cannot access store")
    monkeypatch.setattr(receipts, "_get_engine", forbidden)
    for target in ("", "same", "room"):
        assert receipts.update_pending_ack(target, ack="confirmed") == 0
