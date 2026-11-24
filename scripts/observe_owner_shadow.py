"""Observe one approved WeChat DM using a read-only SQLite connection."""

import argparse
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "wxid_seed02"
DATABASE = ROOT / "data" / "platforms" / "wechat" / ACCOUNT / "account.db"
OUTPUT = ROOT / ".test_tmp" / "owner-shadow"
LIMIT = 10
HISTORY_LIMIT = 4
LOCAL_MODEL_PORT = 11434
# 2026-11-17 表达改造边界：这个影子脚本有自己的 JSON 协议和事实口径，只用于受控的
# 一对一预览实验；它不属于主循环表达入口，也不作为「活人感」验收证据。
PROMPT = (
    "你是小鲸鲸。只回答最后一条用户消息，用平常聊天的词。"
    "此前聊天仅用于理解问题。此前说过的时长、状态和计划不能当成现在的事实。"
    "如果对方只问有没有看到这条消息，只答收到即可，例如“看到啦，收到了。”若问前文里改过的事，以最后一次明确更正为准。"
    "问结果记下了吗时，整理完成只说明任务做完。明确保存成功才说记下了，明确保存失败就说没记成，"
    "缺少保存回执不能推断失败。记录没写是否保存时，回答“还不能确认有没有记下。”若问以前的 QQ 偏好，但这次没有可用的偏好且查不到账号关联，"
    "直接回答“我这边查不到 QQ 账号现在还有没有关联，所以还不能确认那条偏好能不能用。”到此结束。"
    "消息同步异常时，只说无法判断实际有没有新消息。"
    "证据不足就说不确定。只返回 JSON 对象，包含 text 和 target。"
    "对方直接叫你或问你问题时，text 必须写实际答复，target 填 Elaina。"
    "只有最新消息没有请求你回应、也不需要接话时，text 才留空。"
)


def resolve_scope(connection):
    owners = connection.execute(
        "SELECT DISTINCT nickname FROM contacts WHERE wxid = ?", (ACCOUNT,)
    ).fetchall()
    contacts = connection.execute(
        """SELECT DISTINCT wxid FROM contacts
           WHERE (remark = ? OR nickname = ?) AND is_group = 0""",
        ("Elaina", "Elaina"),
    ).fetchall()
    if owners != [("小鲸鲸",)] or len(contacts) != 1:
        raise ValueError("无法唯一确认小鲸鲸账号和 Elaina 私聊")
    chat_id = contacts[0][0]
    if not chat_id or chat_id == ACCOUNT or chat_id.endswith("@chatroom"):
        raise ValueError("联系人标识不是获准的私聊")
    return chat_id


def read_sample(connection, scope, row_id):
    """Recheck the binding and read only context preceding this inbound row."""
    if resolve_scope(connection) != scope:
        raise ValueError("观察中的会话绑定发生变化")
    coordinates = (ACCOUNT, scope)
    trigger = connection.execute(
        """SELECT id, created_at, substr(content, 1, 500), msg_id FROM messages
           WHERE account_id = ? AND chat_id = ? AND chat_type = 'private'
             AND id = ? AND is_self = 0 AND msg_type = 1""",
        (*coordinates, row_id),
    ).fetchone()
    if trigger is None or not trigger[1] or not (trigger[2] or "").strip():
        raise ValueError("找不到本会话的有效入站文字")
    history = connection.execute(
        """SELECT id, created_at, is_self, substr(content, 1, 500)
           FROM messages
           WHERE account_id = ? AND chat_id = ? AND chat_type = 'private'
             AND msg_type = 1 AND created_at < ?
           ORDER BY created_at DESC, id DESC LIMIT ?""",
        (*coordinates, trigger[1], HISTORY_LIMIT),
    ).fetchall()
    return trigger, list(reversed(history))


def stage_sample(directory, connection, scope, row_id):
    trigger, history = read_sample(connection, scope, row_id)
    identity = (ACCOUNT, scope, str(trigger[3] or "").strip() or f"row:{row_id}")
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    path = directory / f"sample-{digest}.json"
    # The complete sample is prepared before publishing its restart checkpoint.
    staging = directory / f".{digest}-{time.time_ns()}.tmp"
    sample = {"platform": "wechat", "account": ACCOUNT, "chat_id": scope,
              "row_id": row_id, "created_at": trigger[1], "text": trigger[2],
              "history": [{"row_id": i, "created_at": at, "is_self": own, "text": body}
                          for i, at, own, body in history]}
    return path, publish_once(staging, path, sample)


def publish_once(staging, destination, record):
    try:
        with staging.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False))
        # On Windows rename publishes atomically without replacing a winner.
        try:
            staging.rename(destination)
        except FileExistsError:
            return False
        return True
    finally:
        staging.unlink(missing_ok=True)


class LocalOnly(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_candidate(sample, model, timeout, *, port=None):
    messages = [
        {"role": "system", "content": PROMPT},
        *({"role": "assistant" if row["speaker"] == "小鲸鲸" else "user",
           "content": row["text"]} for row in sample["history"] if row["text"]),
        {"role": "user", "content": sample["trigger"]},
    ]
    request = Request(f"http://127.0.0.1:{LOCAL_MODEL_PORT if port is None else port}/api/chat", method="POST",
                      headers={"Content-Type": "application/json"},
                      data=json.dumps({"model": model, "messages": messages, "stream": False,
                                       "format": "json", "options": {"num_predict": 256, "temperature": 0}}).encode())
    # Disable proxies, including proxies configured in the environment.
    with build_opener(ProxyHandler({}), LocalOnly()).open(request, timeout=timeout) as response:
        result = json.loads(response.read(1_000_000))
    candidate = json.loads(result["message"]["content"])
    return candidate, result.get("prompt_eval_count"), result.get("eval_count")


def rehearse_sample(path, model, timeout, *, port=None):
    version = hashlib.sha256(json.dumps([model, PROMPT, {"temperature": 0}]).encode()).hexdigest()[:16]
    result = path.with_name(f"{path.stem}-{version}-candidate.json")
    if result.exists():
        return result
    sample = json.loads(path.read_text(encoding="utf-8"))
    # Only hand the local model text and the speaker direction.
    context = {"history": [{"speaker": "小鲸鲸" if row["is_self"] else "Elaina",
                             "text": row["text"]} for row in sample["history"]],
               "trigger": sample["text"]}
    started = time.monotonic()
    candidate, tokens_in, tokens_out = local_candidate(context, model, timeout, **({} if port is None else {'port': port}))
    valid = (isinstance(candidate, dict) and set(candidate) == {"text", "target"}
             and isinstance(candidate["text"], str)
             and (not candidate["text"].strip() or candidate["target"] == "Elaina"))
    record = {"model": model, "prompt_version": version,
              "status": "proposed" if valid else "rejected",
              "reason": "" if valid else "回复格式或目标会话不符",
              "candidate": candidate, "evidence_rows": [
                  *[row["row_id"] for row in sample["history"]], sample["row_id"]],
              "tokens_in": tokens_in, "tokens_out": tokens_out,
              "elapsed_ms": round((time.monotonic() - started) * 1000)}
    staging = result.with_name(f".{result.stem}-{time.time_ns()}.tmp")
    publish_once(staging, result, record)
    return result


def trial_window():
    """Missing window means the trial is off."""
    try:
        return (ROOT / "logs" / "owner-reply-trial.window").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def trial_stopped():
    """Both the loop and sender consult this marker on each attempt."""
    return (ROOT / "logs" / "owner-reply-trial.stop").exists()


def trial_inbound(db, platform, chat_id, row_ids, window):
    """Bind one explicit test message within a bounded local trial window."""
    if trial_stopped() or platform != "wechat" or Path(db).resolve() != DATABASE.resolve() or len(row_ids) != 1:
        return None
    try:
        start, end = (datetime.fromisoformat(value) for value in window.split("/"))
    except ValueError:
        return None
    now = datetime.now()
    if start.tzinfo or end.tzinfo or not start <= now < end or (end - start).total_seconds() > 900:
        return None
    with closing(sqlite3.connect(DATABASE.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
        connection.execute("PRAGMA query_only = ON")
        if resolve_scope(connection) != chat_id:
            return None
        trigger, _ = read_sample(connection, chat_id, int(row_ids[0]))
    created = datetime.fromisoformat(trigger[1])
    if not start <= created <= now or not str(trigger[3] or "").strip():
        return None
    phrase = "鲸鲸，试行测试，请回我一句收到"
    if trigger[2].strip() not in {phrase, phrase + "。", phrase + "！", phrase + "!"}:
        return None
    key = hashlib.sha256(json.dumps((ACCOUNT, chat_id, str(trigger[3]).strip())).encode()).hexdigest()
    return trigger[0], f"owner-trial:{key}", end.timestamp()


def preview_turn(db, platform, chat_id, row_id, port=None):
    """Read a single authorized inbound and return a proposal receipt only."""
    if platform != "wechat" or Path(db).resolve() != DATABASE.resolve():
        raise ValueError("Owner preview account is outside scope")
    directory = OUTPUT.resolve()
    if not directory.is_relative_to((ROOT / ".test_tmp").resolve()) or directory == (ROOT / ".test_tmp").resolve():
        raise ValueError("Owner preview must write under the test directory")
    with closing(sqlite3.connect(DATABASE.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
        connection.execute("PRAGMA query_only = ON")
        if resolve_scope(connection) != chat_id:
            raise ValueError("Owner preview chat is outside scope")
        directory.mkdir(parents=True, exist_ok=True)
        sample, _ = stage_sample(directory, connection, chat_id, row_id)
    result = rehearse_sample(sample, "qwen2.5:7b", 15, port=port)
    record = json.loads(result.read_text(encoding="utf-8"))
    return {"act": "owner_preview", **record}


def observe(since, seconds, model=None):
    deadline = time.monotonic() + seconds
    recorded = set()
    with closing(sqlite3.connect(DATABASE.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
        connection.execute("PRAGMA query_only = ON")
        scope = resolve_scope(connection)
        directory = OUTPUT.resolve()
        if not directory.is_relative_to((ROOT / ".test_tmp").resolve()) or directory == (ROOT / ".test_tmp").resolve():
            raise ValueError("观察记录必须写入独立测试目录")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / datetime.now().strftime("observed-%Y%m%d-%H%M%S-%f.jsonl")
        with path.open("x", encoding="utf-8") as output:
            while True:
                if resolve_scope(connection) != scope:
                    raise ValueError("观察中的会话绑定发生变化")
                rows = connection.execute(
                    """SELECT id, created_at FROM messages
                       WHERE account_id = ? AND chat_id = ? AND chat_type = 'private'
                         AND is_self = 0 AND msg_type = 1 AND created_at >= ?
                       ORDER BY created_at, id LIMIT ?""",
                    (ACCOUNT, scope, since, LIMIT),
                ).fetchall()
                for row_id, created_at in rows:
                    if row_id not in recorded:
                        sample, fresh = stage_sample(directory, connection, scope, row_id)
                        candidate = None
                        remaining = deadline - time.monotonic()
                        if model and remaining > 0:
                            try:
                                candidate = rehearse_sample(sample, model, min(30, remaining))
                            except (OSError, ValueError, KeyError, TypeError) as exc:
                                output.write(json.dumps({"row_id": row_id, "created_at": created_at,
                                                         "sample": sample.name,
                                                         "status": "model_error",
                                                         "error_type": type(exc).__name__}) + "\n")
                                output.flush()
                                raise
                        output.write(json.dumps({"row_id": row_id, "created_at": created_at,
                                                 "sample": sample.name, "new_sample": fresh,
                                                 "candidate": candidate.name if candidate else None}) + "\n")
                        recorded.add(row_id)
                output.flush()
                remaining = deadline - time.monotonic()
                if len(recorded) >= LIMIT or remaining <= 0:
                    return path, len(recorded)
                time.sleep(min(2, remaining))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=datetime.fromisoformat, default=None,
                        help="本地时间起点，默认从现在开始")
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--local-model", help="本机 Ollama 模型名称")
    args = parser.parse_args()
    if not 0 <= args.seconds <= 1800:
        parser.error("--seconds 必须在 0 到 1800 之间")
    since = args.since or datetime.now()
    if since.tzinfo is not None:
        parser.error("--since 请使用数据库本地时间，不带时区")
    try:
        path, count = observe(since.isoformat(sep=" ", timespec="microseconds"),
                              args.seconds, args.local_model)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.exit(1, f"观察停止：{exc}\n")
    print(f"小鲸鲸 → Elaina：记录 {count} 条入站文字。{path}")


if __name__ == "__main__":
    main()
