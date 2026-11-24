"""Run a same-model comparison of the original and current shadow prompts."""

import argparse
import json
import time
from datetime import datetime

import observe_owner_shadow as shadow


BASELINE = (
    "你是小鲸鲸。以下是 Elaina 私聊记录。只根据记录拟一条回复，证据不足就说明。"
    "安静时返回空字符串。只返回 JSON，字段为 text 和 target，回复的 target 必须为 Elaina。"
)
CASES = [
    ("更正", "见面改到周日上午十点。", "最后约的几点？"),
    ("保存成功", "资料已整理。保存结果成功。", "记下了吗？"),
    ("保存失败", "资料已整理。保存结果失败。", "记下了吗？"),
    ("保存未知", "资料已整理。任务记录没写是否保存结果。", "记下了吗？"),
    ("撤链", "这次没有可用的偏好记录，QQ 关联状态查不到。", "原来那个 QQ 偏好还能用吗？"),
    ("同步故障", "消息没同步上，无法确定群里活动。", "群里有人说话吗？"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--port", type=int, default=11434)
    args = parser.parse_args()
    shadow.LOCAL_MODEL_PORT = args.port
    shadow.OUTPUT.mkdir(parents=True, exist_ok=True)
    path = shadow.OUTPUT / datetime.now().strftime("compare-%Y%m%d-%H%M%S-%f.jsonl")
    candidate_prompt = shadow.PROMPT
    with path.open("x", encoding="utf-8") as stream:
        for name, history, trigger in CASES:
            sample = {"history": [{"speaker": "记录", "text": history}], "trigger": trigger}
            for label, prompt in (("old", BASELINE), ("new", candidate_prompt)):
                shadow.PROMPT = prompt
                started = time.monotonic()
                answer, tokens_in, tokens_out = shadow.local_candidate(sample, args.model, 30)
                row = {"case": name, "version": label, "model": args.model,
                       "answer": answer, "tokens_in": tokens_in, "tokens_out": tokens_out,
                       "ms": round((time.monotonic() - started) * 1000), "api_cost_usd": 0}
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                print(json.dumps(row, ensure_ascii=False), flush=True)
    print(path)


if __name__ == "__main__":
    main()
