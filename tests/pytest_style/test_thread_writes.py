from types import SimpleNamespace as NS

import pytest

from agents.auto_dream import AutoDream
from agents.feedback import FeedbackSystem


def fixture_repo(account="a", unknown=False):
    from db.models import Thread, ThreadMsgRef
    src = Thread(id=1, platform="wechat", account_id="" if unknown else "a", chat_id="room", msg_count=10)
    dst = Thread(id=2, platform="wechat", account_id=account, chat_id="room", msg_count=0)
    refs = [ThreadMsgRef(thread_id=1, msg_id=i, platform="wechat", account_id="a", chat_id="room", relevance=1, confidence=1) for i in range(10)]
    writes = []
    def create(t):
        t.id = 3
        writes.append(("create", t))
        return t
    repo = NS(threads=NS(get_by_id=lambda i: {1: src, 2: dst}[i],
                        create=create, update=lambda *a, **kw: writes.append(("update", kw))),
              thread_refs=NS(get_by_thread=lambda *a, **kw: refs,
                             get_by_msg=lambda i: [r for r in refs if r.msg_id == i],
                             add=lambda r: writes.append(("add", r)),
                             remove=lambda *a: writes.append(("remove", a))))
    return repo, refs, writes


@pytest.mark.parametrize("action", ["merge", "split", "feedback"])
def test_unknown_source_rejected_before_any_write(action):
    repo, refs, writes = fixture_repo(unknown=True)
    with pytest.raises(ValueError):
        if action == "merge":
            AutoDream(repo).execute_merge(1, 2)
        elif action == "split":
            AutoDream(repo).execute_split(1)
        else:
            FeedbackSystem(repo)._move_message(0, 1, 2)
    assert writes == []


@pytest.mark.parametrize("action", ["merge", "feedback"])
def test_cross_account_rejected_before_any_write(action):
    repo, refs, writes = fixture_repo(account="b")
    with pytest.raises(ValueError):
        if action == "merge":
            AutoDream(repo).execute_merge(1, 2)
        else:
            FeedbackSystem(repo)._move_message(0, 1, 2)
    assert writes == []


def test_split_preserves_source_on_thread_and_all_refs():
    from db.thread_scope import source_key
    repo, refs, writes = fixture_repo()
    assert AutoDream(repo).execute_split(1) == (1, 3)
    copied = [obj for kind, obj in writes if kind in {"create", "add"}]
    assert len(copied) == 6
    assert all(source_key(obj) == ("wechat", "a", "room") for obj in copied)


def test_feedback_keeps_original_when_destination_write_fails():
    repo, refs, writes = fixture_repo()
    def fail(ref):
        raise RuntimeError("write failed")
    repo.thread_refs.add = fail
    with pytest.raises(RuntimeError, match="write failed"):
        FeedbackSystem(repo)._move_message(0, 1, 2)
    assert writes == []
