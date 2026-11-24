"""门禁规则自身的形状锁（漏账 ③，2026-10-29）。

为什么：`check_flags` / `check_envread` 曾是逐行正则——注释里写一句 `os.environ.get("X") == "1"`
也会被算成违规；身份里带 `:行号`，在上面插一行注释就把旧债变成新债；`ast.parse` 失败还静默返回 []。
这里锁四件事：身份不带行号、注释不算代码、解析失败要露面（fail-closed）、`parser.error` 不是日志。
"""

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _check():
    spec = importlib.util.spec_from_file_location("checkrules", ROOT / "scripts" / "check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHK = _check()

NAKED_FLAG = 'import os\n\n\ndef f():\n    return os.environ.get("YINYUE_X") == "1"\n'
COMMENTED_FLAG = 'import os\n\n\n# 老写法是 os.environ.get("YINYUE_X") == "1"，别学\ndef f():\n    return 1\n'
NAKED_READ = 'import os\n\n\ndef f():\n    return os.environ.get("YINYUE_FLAG_A")\n'
COMMENTED_READ = 'import os\n\n\n# 老写法 os.environ.get("YINYUE_FLAG_A")\ndef f():\n    return 1\n'
EVENT_OK = 'def f(log):\n    log.info("job_started", n=1)\n'
EVENT_FSTRING = 'def f(log):\n    log.warning(f"job_failed error={1}")\n'
ARGPARSE = 'def f(parser):\n    parser.error(f"bad {1}")\n'
UNPARSABLE = 'def f(:\n'


def test_identity_has_no_line_number():
    got = CHK.mark("core/x.py", "f", "eq:1")
    assert got == "core/x.py::f::eq:1"
    assert not re.search(r"\.py:\d", got)


def test_naked_flag_is_caught():
    assert CHK._flag_offences(NAKED_FLAG, "core/x.py") == ["core/x.py::f::eq:1"]


def test_commented_flag_is_not_code():
    assert CHK._flag_offences(COMMENTED_FLAG, "core/x.py") == []


def test_naked_envread_is_caught_with_shape():
    assert CHK._envread_offences(NAKED_READ, "core/x.py") == ["core/x.py::f::read:YINYUE_FLAG_A"]


def test_commented_envread_is_not_code():
    assert CHK._envread_offences(COMMENTED_READ, "core/x.py") == []


def test_event_with_fields_is_fine():
    assert CHK._event_offences(EVENT_OK, "core/x.py") == []


def test_event_name_must_not_carry_fields():
    bad = CHK._event_offences(EVENT_FSTRING, "core/x.py")
    assert len(bad) == 1
    assert "fstring" in bad[0]


def test_argparse_error_is_not_a_logger():
    assert CHK._event_offences(ARGPARSE, "core/x.py") == []


@pytest.mark.parametrize("fn", ["_flag_offences", "_envread_offences", "_event_offences"])
def test_unparsable_file_is_loud_not_silent(fn):
    assert getattr(CHK, fn)(UNPARSABLE, "core/x.py") == ["core/x.py::<parse>::unparsable"]


# 2026-11-09：EVENT_RE 放开点分层次（`direct_send.state_unreadable`）。
# 库内 1265/1380 条既有事件名带点号；不放行会把合规名字判成新债。
# 但「名字里塞字段 / 中文句子 / 文件名当名字」仍须拦。
EVENT_DOTTED = 'def f(log):\n    log.info("job.started", n=1)\n'
EVENT_DOTTED_FIELDS = 'def f(log):\n    log.warning("job.failed err=%s", 1)\n'
EVENT_SENTENCE = 'def f(log):\n    log.warning("%s 起来了：距上一条 %.1f 小时", 1, 2)\n'
EVENT_FILENAME = 'def f(log):\n    log.warning("core/x.py Exception")\n'


def test_dotted_event_name_is_allowed():
    assert CHK._event_offences(EVENT_DOTTED, "core/x.py") == []


def test_dotted_name_with_inline_fields_is_still_caught():
    assert len(CHK._event_offences(EVENT_DOTTED_FIELDS, "core/x.py")) == 1


def test_sentence_as_event_name_is_caught():
    assert len(CHK._event_offences(EVENT_SENTENCE, "core/x.py")) == 1


def test_filename_as_event_name_is_caught():
    assert len(CHK._event_offences(EVENT_FILENAME, "core/x.py")) == 1
