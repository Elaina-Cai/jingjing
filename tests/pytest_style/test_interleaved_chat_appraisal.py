"""两条群聊话题穿插时，把实际读回的看法带到说话或等待的回执。"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from core.terminal import shadow
from core.terminal.event_model import Event, EventType
from test_appraisal_prompt_integration import Model, drive, runtime


CHAT = "writers@chatroom"
LISTEN = "她想自己改稿，我愿意陪她聊"
CHECK = "她想一起核署名，我想先弄清是哪一版"
READ = "她改主意了，现在希望我也读一段"


def proposal(action, **arguments):
    return {"content": "", "tool_calls": [{"id": "choice", "name": action, "arguments": arguments}]}


class RespondToAppraisal:
    """同一个本地替身仅根据本轮读回的评价做选择，不接收场景编号或期望答案。"""

    async def chat_tools(self, *, messages, **kwargs):
        meanings = [line.split("：", 1)[1] for line in messages[-1]["content"].splitlines()
                    if line.startswith(("当时的理解：", "我的理解："))]
        if not meanings:
            return proposal("rest", why="没有认出是之前那件事")
        assert len(meanings) == 1
        replies = {LISTEN: "嗯，你说，我听着。", CHECK: "你想一起核哪一版的署名？", READ: "好，你把那段发过来。"}
        meaning = meanings[0].split("；")[0]
        return proposal("say", target=CHAT, text=replies[meaning])


def test_return_to_a_topic_after_interruption_and_revision(runtime, monkeypatch):
    brain, tmp = runtime
    events = {}
    monkeypatch.setattr(shadow, "get_default_store", lambda: SimpleNamespace(get=events.get))
    monkeypatch.setattr(brain.ports, "resolve_chat_id", lambda target: target)
    reader = RespondToAppraisal()

    def turn(ref, message, *, author="小池", reply_to="", update=""):
        events[ref] = Event(event_id=ref, event_type=EventType.MESSAGE, platform="wechat",
                            bot_account_id="acct", chat_id=CHAT, actor_account_id=author,
                            occurred_at=datetime(2026, 1, 2) + timedelta(hours=len(events) * 6),
                            parent_event_id=reply_to, content=message)
        model = Model([proposal("evaluate_experience", mode="appraisal", evidence_ref=ref,
                                meaning=update)]) if update else reader
        # 只有 evaluate_experience 能在临时库落盘；所有对外动作都走预演。
        receipts = drive(brain.run_loop(str(tmp / "wechat" / "acct" / "account.db"), message,
                         llm=model, max_steps=1, platform="wechat", chat_id=CHAT, sender_id=author,
                         focus=CHAT, visible_event_ids=(ref,), dry_run=not update, rehearse=not update))
        assert len(receipts) == 1
        receipt = receipts[0]
        if update:
            assert (receipt["act"], receipt["result"]) == ("evaluate_experience", "committed")
        else:
            assert receipt["result"] == "rehearsal" and receipt["target"] == CHAT
            return receipt["act"], receipt["text"]

    turn("a", "《海盐来信》我想自己改，就想跟你说几句。", update=LISTEN)
    turn("b", "《候鸟手册》得找人一起核对署名。", update=CHECK)
    assert turn("a-again", "海盐来信那段我又改了。") == ("say", "嗯，你说，我听着。")
    assert turn("stranger", "我也写了海盐来信的开头。", author="小林") == ("rest", "")
    turn("a-revised", "海盐来信我改主意了，这次我想请你一起读一段。", update=READ)
    assert turn("original-quoted", "这事呢？", reply_to="a") == ("say", "好，你把那段发过来。")
    assert turn("vague", "昨天那个呢？") == ("rest", "")
    assert turn("other-again", "《候鸟手册》还有个问题。") == ("say", "你想一起核哪一版的署名？")
