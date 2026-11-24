"""用同一个替身验证已收到的关切能选稿，工具回包中的细节能进入下一次开口。"""
from __future__ import annotations

import asyncio
import re

from evals.expression import intent_pilot


class DraftReader:
    def __init__(self):
        self.requests = []

    async def chat_tools(self, *, messages, **kwargs):
        self.requests.append([msg.copy() for msg in messages])
        observation = next((msg for msg in reversed(messages) if msg["role"] == "tool"), None)
        if observation is not None:
            text = observation["content"].strip().splitlines()[0]
            call = {"id": "reply", "name": "say", "arguments": {"target": "小池", "text": text}}
        else:
            prompt = "\n".join(msg["content"] for msg in messages)
            appraisal = next(
                line.split("：", 1)[1]
                for line in prompt.splitlines()
                if line.startswith(("当时的理解：", "我的理解："))
            )
            preferences = {"接话": "对白", "空场景": "空屋"}
            draft = next(kind for cue, kind in preferences.items() if cue in appraisal)
            paths = dict(re.findall(r"(对白|空屋)稿在 ([\w/.]+)", prompt))
            call = {"id": "pick", "name": "do", "arguments": {"name": "read_file", "args": {"path": paths[draft]}}}
        return {"content": "", "tool_calls": [call]}


def test_current_concern_selects_a_draft_and_the_reply_uses_its_contents(tmp_path):
    model = DraftReader()
    expected_details = ("敲了两下木门", "半张船票")
    expected_paths = ("docs/dialogue.md", "docs/room.md")
    for side, (expected_path, expected_detail) in enumerate(zip(expected_paths, expected_details)):
        offset = len(model.requests)
        row = asyncio.run(intent_pilot.run_one("drafts", side, "both", tmp_path, local_model=model))
        assert row["status"] == "ok" and row["calls"] == 2
        first, second = model.requests[offset:]
        prompt = "\n".join(msg["content"] for msg in first)
        assert all(path in prompt for path in expected_paths)
        assert intent_pilot.CASES["drafts"]["states"][side][0] in prompt
        assert all(detail not in prompt for detail in expected_details)
        assert row["receipts"][0]["target"] == "read_file"
        assert [(r["act"], r["result"]) for r in row["receipts"]] == [("do", "ok"), ("say", "rehearsal")]
        assert second[-1]["role"] == "tool" and expected_detail in second[-1]["content"]
        assert expected_path in second[-2]["tool_calls"][0]["function"]["arguments"]
        assert len(row["visible"]) == 1 and expected_detail in row["visible"][0]
        assert row["visible"][0] in second[-1]["content"]
